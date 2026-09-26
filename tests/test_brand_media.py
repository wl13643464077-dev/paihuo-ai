"""Brand image input, public-store provenance, and conservative QA contracts."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from io import BytesIO
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from app import brand_media, brand_media_schema, textvideo


def _png(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


def _brand(**changes) -> dict:
    data = {
        "id": 31,
        "tenant_id": 7,
        "version": 3,
        "brand_name": "百味餐饮",
        "status": "confirmed",
        "fields": {
            "store_name": "百味小馆",
            "logo_url": "https://brand.example/logo.png",
            "tone": "温暖、真实",
            "slogan": "认真做好每一餐",
        },
    }
    data.update(changes)
    return data


class BrandMediaTests(unittest.TestCase):
    def test_brand_context_requires_confirmed_tenant_scoped_version(self):
        context = brand_media.load_brand_context(7, active=_brand())
        self.assertEqual("百味小馆", context["store_name"])
        self.assertEqual("认真做好每一餐", context["slogan"])
        for item, code in (
            (_brand(status="draft"), "brand_unconfirmed"),
            (_brand(tenant_id=8), "brand_scope_mismatch"),
            (_brand(version=0), "brand_version_invalid"),
        ):
            with self.subTest(code=code), self.assertRaises(brand_media.BrandMediaError) as caught:
                brand_media.load_brand_context(7, active=item)
            self.assertEqual(code, caught.exception.code)

    def test_tenant_store_first_brand_name_authoritative(self):
        branch = {"id": 10, "tenant_id": 7, "industry_key": "restaurant",
                  "name": "百味小馆", "region": "北京", "address": "朝阳路1号"}
        lookup = AsyncMock(return_value={"data": {"fields": []}, "web_sources": []})
        with patch.object(brand_media.db, "q", return_value=[branch]) as query:
            result = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10,
                industry_key="restaurant", public_lookup=lookup,
            ))
        self.assertEqual("百味小馆", result["fields"]["name"]["value"])
        self.assertEqual("confirmed_brand_package", result["fields"]["name"]["source"]["kind"])
        self.assertEqual("朝阳路1号", result["fields"]["address"]["value"])
        self.assertEqual("tenant_store_master", result["fields"]["address"]["source"]["kind"])
        self.assertIn("phone", result["missing"])
        self.assertIn(7, query.call_args.args[1])
        self.assertIn(10, query.call_args.args[1])

    def test_branch_conflict_and_multi_store_ambiguity_never_cross_fill(self):
        mismatch = {"id": 10, "tenant_id": 7, "industry_key": "restaurant",
                    "name": "另一个店名", "region": "北京", "address": "朝阳路1号"}
        lookup = AsyncMock(return_value={})
        with patch.object(brand_media.db, "q", return_value=[mismatch]):
            info = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10, public_lookup=lookup,
            ))
            with self.assertRaises(brand_media.BrandMediaError) as caught:
                asyncio.run(brand_media.generate_activity_image(
                    7, {"title": "周末尝鲜", "content": "招牌面第二份半价"},
                    active=_brand(), branch_id=10, logo_bytes=_png(),
                    public_lookup=lookup, image_editor=AsyncMock(),
                ))
        self.assertTrue(info["name_conflict"])
        self.assertEqual("", info["fields"]["address"]["value"])
        self.assertEqual("store_name_conflict", caught.exception.code)
        lookup.assert_not_awaited()
        branches = [
            {"id": 10, "tenant_id": 7, "industry_key": "restaurant",
             "name": "百味小馆", "region": "北京", "address": "朝阳路1号"},
            {"id": 11, "tenant_id": 7, "industry_key": "restaurant",
             "name": "百味小馆", "region": "上海", "address": "海路2号"},
        ]
        with patch.object(brand_media.db, "q", return_value=branches):
            ambiguous = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), public_lookup=lookup,
            ))
        self.assertTrue(ambiguous["selection_required"])
        self.assertEqual("", ambiguous["fields"]["address"]["value"])
        lookup.assert_not_awaited()

    def test_selected_branch_cannot_cross_tenant(self):
        with patch.object(brand_media.db, "q", return_value=[]) as query:
            with self.assertRaises(brand_media.BrandMediaError) as caught:
                asyncio.run(brand_media.resolve_store_info(
                    7, active=_brand(), branch_id=99,
                    public_lookup=AsyncMock(),
                ))
        self.assertEqual("branch_not_found", caught.exception.code)
        self.assertIn("tenant_id=?", query.call_args.args[0])

    def test_public_fill_requires_captured_url_and_same_page_quote(self):
        source_url = "https://example.org/baiwei"
        result = {
            "data": {"fields": [
                {"field": "address", "value": "朝阳路8号", "source_url": source_url,
                 "evidence_quote": "百味小馆位于北京市朝阳区朝阳路8号"},
                {"field": "phone", "value": "12345678",
                 "source_url": "https://invented.example/phone",
                 "evidence_quote": "百味小馆电话12345678"},
                {"field": "hours", "value": "24小时", "source_url": source_url,
                 "evidence_quote": "百味小馆营业时间24小时"},
                {"field": "region", "value": "上海", "source_url": source_url,
                 "evidence_quote": "北京市朝阳区朝阳路8号百味小馆，上海总部另设"},
            ]},
            "web_sources": [{"source_url": source_url,
                             "source_title": "百味小馆门店页"}],
        }

        async def page_fetch(url, **_kwargs):
            self.assertEqual(source_url, url)
            return {"source_url": url, "text": "百味小馆位于北京市朝阳区朝阳路8号。营业到22:00。"}

        active = _brand()
        active["fields"]["store_address"] = "北京市朝阳区朝阳路8号"
        with patch.object(brand_media.db, "q", return_value=[]):
            store = asyncio.run(brand_media.resolve_store_info(
                7, active=active, public_lookup=AsyncMock(return_value=result),
                page_fetcher=page_fetch,
            ))
        self.assertEqual("北京市朝阳区朝阳路8号", store["fields"]["address"]["value"])
        self.assertEqual("confirmed_brand_package", store["fields"]["address"]["source"]["kind"])
        self.assertEqual("北京", store["fields"]["region"]["value"])
        self.assertEqual("derived_from_confirmed_brand_package", store["fields"]["region"]["source"]["kind"])
        self.assertIn("phone", store["missing"])
        self.assertIn("hours", store["missing"])

    def test_no_branch_without_precise_confirmed_address_cannot_cross_fill(self):
        for address in ("", "北京", "朝阳路", "朝阳路8号"):
            with self.subTest(address=address):
                active = _brand()
                active["fields"]["store_address"] = address
                lookup = AsyncMock(return_value={
                    "data": {"fields": [{"field": "phone", "value": "87654321"}]},
                })
                with patch.object(brand_media.db, "q", return_value=[]):
                    store = asyncio.run(brand_media.resolve_store_info(
                        7, active=active, public_lookup=lookup,
                    ))
                self.assertIn("phone", store["missing"])
                self.assertTrue(any("具体地址" in item for item in store["warnings"]))
                lookup.assert_not_awaited()

    def test_selected_branch_public_fill_requires_address_in_same_quote(self):
        branch = {"id": 10, "tenant_id": 7, "industry_key": "restaurant",
                  "name": "百味小馆", "region": "北京", "address": "朝阳路1号"}
        source_url = "https://example.org/locations"
        wrong = "上海百味小馆朝阳路1号电话87654321"
        right = "北京百味小馆朝阳路1号电话12345678"
        response = {
            "data": {"fields": [
                {"field": "phone", "value": "87654321", "source_url": source_url,
                 "evidence_quote": wrong},
            ]},
            "web_sources": [{"source_url": source_url,
                             "source_title": "百味小馆门店列表"}],
        }

        async def page_fetch(url, **_kwargs):
            return {"source_url": url, "text": f"{wrong}。{right}"}

        with patch.object(brand_media.db, "q", return_value=[branch]):
            wrong_store = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10,
                public_lookup=AsyncMock(return_value=response),
                page_fetcher=page_fetch,
            ))
            response["data"]["fields"].append({
                "field": "phone", "value": "12345678", "source_url": source_url,
                "evidence_quote": right,
            })
            right_store = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10,
                public_lookup=AsyncMock(return_value=response),
                page_fetcher=page_fetch,
            ))
        self.assertIn("phone", wrong_store["missing"])
        self.assertEqual("12345678", right_store["fields"]["phone"]["value"])
        self.assertEqual("public_verified", right_store["fields"]["phone"]["source"]["kind"])

    def test_public_quote_with_two_store_records_cannot_join_other_phone(self):
        branch = {"id": 10, "tenant_id": 7, "industry_key": "restaurant",
                  "name": "百味小馆", "region": "北京", "address": "朝阳路1号"}
        source_url = "https://example.org/branches"
        quote = "北京百味小馆朝阳路1号；上海百味小馆海路2号电话87654321"
        response = {
            "data": {"fields": [{"field": "phone", "value": "87654321",
                                  "source_url": source_url, "evidence_quote": quote}]},
            "web_sources": [{"source_url": source_url, "source_title": "门店列表"}],
        }

        async def page_fetch(url, **_kwargs):
            return {"source_url": url, "text": quote}

        with patch.object(brand_media.db, "q", return_value=[branch]):
            result = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10,
                public_lookup=AsyncMock(return_value=response),
                page_fetcher=page_fetch,
            ))
        self.assertIn("phone", result["missing"])

        # A second branch may be referred to as just "上海店"; the comma is
        # still a record boundary and must not attach its phone to Beijing.
        response["data"]["fields"][0]["evidence_quote"] = (
            "北京百味小馆朝阳路1号，上海店电话87654321"
        )
        async def comma_page_fetch(url, **_kwargs):
            return {"source_url": url, "text": response["data"]["fields"][0]["evidence_quote"]}
        with patch.object(brand_media.db, "q", return_value=[branch]):
            comma_result = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10,
                public_lookup=AsyncMock(return_value=response),
                page_fetcher=comma_page_fetch,
            ))
        self.assertIn("phone", comma_result["missing"])

    def test_selected_branch_without_precise_address_keeps_public_fields_missing(self):
        for region in ("", "北京"):
            with self.subTest(region=region):
                branch = {"id": 10, "tenant_id": 7,
                          "industry_key": "restaurant", "name": "百味小馆",
                          "region": region, "address": ""}
                lookup = AsyncMock(return_value={})
                with patch.object(brand_media.db, "q", return_value=[branch]):
                    store = asyncio.run(brand_media.resolve_store_info(
                        7, active=_brand(), branch_id=10, public_lookup=lookup,
                    ))
                self.assertIn("phone", store["missing"])
                self.assertTrue(any("具体地址" in item for item in store["warnings"]))
                lookup.assert_not_awaited()

    def test_region_and_address_city_conflict_stops_public_fill(self):
        branch = {"id": 10, "tenant_id": 7, "industry_key": "restaurant",
                  "name": "百味小馆", "region": "北京", "address": "上海市朝阳路1号"}
        lookup = AsyncMock(return_value={})
        with patch.object(brand_media.db, "q", return_value=[branch]):
            store = asyncio.run(brand_media.resolve_store_info(
                7, active=_brand(), branch_id=10, public_lookup=lookup,
            ))
        self.assertIn("phone", store["missing"])
        self.assertTrue(any("城市冲突" in item for item in store["warnings"]))
        lookup.assert_not_awaited()
        self.assertEqual("乌鲁木齐", brand_media._city_anchor("新疆维吾尔自治区乌鲁木齐市天山区"))

    def test_logo_fetch_rejects_unsafe_url(self):
        for url in ("http://brand.example/logo.png", "file:///tmp/logo.png",
                    "https://127.0.0.1/logo.png"):
            with self.subTest(url=url):
                # Host safety for HTTPS is enforced by netfetch, not URL syntax.
                if url.startswith("https://"):
                    fetch = AsyncMock(side_effect=ValueError("private IP"))
                else:
                    fetch = AsyncMock(return_value=_png())
                with self.assertRaises(brand_media.BrandMediaError):
                    asyncio.run(brand_media.load_logo_bytes(
                        7, url, public_media_fetcher=fetch,
                    ))
        fetch = AsyncMock(return_value=_png())
        image = asyncio.run(brand_media.load_logo_bytes(
            7, "https://brand.example/logo.png", public_media_fetcher=fetch,
        ))
        self.assertEqual(_png(), image)
        self.assertEqual("image", fetch.call_args.kwargs["kind"])

    def test_image_edit_has_branded_reference_and_no_post_overlay(self):
        output = _png("blue")
        editor = AsyncMock(return_value=output)
        with patch.object(brand_media.db, "q", return_value=[]):
            result = asyncio.run(brand_media.generate_activity_image(
                7, {"title": "周末尝鲜", "content": "招牌面第二份半价"},
                active=_brand(), logo_bytes=_png("red"),
                public_lookup=AsyncMock(return_value={}),
                image_editor=editor,
            ))
        self.assertIs(result["image_bytes"], output)
        self.assertEqual("needs_manual_review", result["status"])
        args = editor.call_args.args
        self.assertEqual(160, args[0])
        self.assertIn("百味小馆", args[1])
        self.assertIn("周末尝鲜", args[1])
        self.assertIn("第二份半价", args[1])
        self.assertNotIn("派活出品", args[1])
        self.assertEqual("PNG", Image.open(BytesIO(args[2])).format)

    def test_missing_logo_and_store_name_fail_before_image_edit(self):
        editor = AsyncMock(return_value=_png())
        lookup = AsyncMock(return_value={})
        no_logo = _brand(fields={"store_name": "百味小馆", "tone": "温暖"})
        with self.assertRaises(brand_media.BrandMediaError) as caught:
            asyncio.run(brand_media.generate_activity_image(
                7, {"title": "周末尝鲜", "content": "全场体验"},
                active=no_logo, image_editor=editor, public_lookup=lookup,
            ))
        self.assertEqual("logo_missing", caught.exception.code)
        lookup.assert_not_awaited()
        editor.assert_not_awaited()
        no_store = _brand(fields={"logo_url": "https://brand.example/logo.png"})
        with patch.object(brand_media.db, "q", return_value=[]), \
             self.assertRaises(brand_media.BrandMediaError) as caught:
            asyncio.run(brand_media.generate_activity_image(
                7, {"title": "周末尝鲜", "content": "全场体验"},
                active=no_store, logo_bytes=_png(), image_editor=editor,
                public_lookup=lookup,
            ))
        self.assertEqual("store_name_missing", caught.exception.code)
        editor.assert_not_awaited()

    def test_quality_gate_rejects_model_self_report_and_missing_text(self):
        args = {"store_name": "百味小馆", "activity_title": "周末尝鲜",
                "activity_content": "招牌面第二份半价"}
        candidate = brand_media.review_activity_image(
            **args, evidence={"method": "vision_model", "logo_match": True,
                              "ocr_text": "百味小馆 周末尝鲜 招牌面第二份半价"},
        )
        self.assertEqual("needs_manual_review", candidate["status"])
        failed = brand_media.review_activity_image(
            **args, evidence={"method": "human", "logo_match": True,
                              "ocr_text": "百味小馆 周末尝鲜"},
        )
        self.assertEqual("failed_qa", failed["status"])
        self.assertIn("activity_content", failed["missing"])
        passed = brand_media.review_activity_image(
            **args, evidence={"method": "human", "logo_match": True,
                              "no_extra_claims": True,
                              "ocr_text": "百味小馆 周末尝鲜 招牌面第二份半价"},
        )
        self.assertEqual("passed", passed["status"])

    def test_quality_gate_rejects_platform_name_unapproved_prices_dates_claims(self):
        args = {"store_name": "百味小馆", "activity_title": "周末尝鲜",
                "activity_content": "招牌面第二份半价"}
        required = "百味小馆 周末尝鲜 招牌面第二份半价"
        for extra in ("派活出品", "活动价99元", "9月23日", "全网最低"):
            with self.subTest(extra=extra):
                quality = brand_media.review_activity_image(
                    **args, evidence={
                        "method": "human", "logo_match": True,
                        "no_extra_claims": True,
                        "ocr_text": f"{required} {extra}",
                    },
                )
                self.assertEqual("failed_qa", quality["status"])
                self.assertTrue(quality.get("unauthorized"))
        allowed = brand_media.review_activity_image(
            store_name="百味小馆", activity_title="周末尝鲜",
            activity_content="9月23日招牌面99元，买一送一",
            evidence={"method": "human", "logo_match": True,
                      "no_extra_claims": True,
                      "ocr_text": "百味小馆 周末尝鲜 9月23日招牌面99元，买一送一"},
        )
        self.assertEqual("passed", allowed["status"])

    def test_generated_video_script_receives_confirmed_brand_context(self):
        call = AsyncMock(return_value={"text": "品牌口播" * 15})
        with patch.object(textvideo, "_call_textvideo_employee", call):
            asyncio.run(textvideo.make_script(
                "百味小馆", "正文" * 200,
                brand_context="店名：百味小馆；口号：认真做好每一餐",
            ))
        prompt = call.call_args.args[1]
        self.assertIn("店名：百味小馆", prompt)
        self.assertIn("口号：认真做好每一餐", prompt)

    def test_review_schema_fragment_upgrades_earlier_attachment_table(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE task_activity_image(id INTEGER PRIMARY KEY, "
            "tenant_id INTEGER,task_id INTEGER,group_key TEXT,file_path TEXT,"
            "status TEXT,quality_json TEXT,brand_package_id INTEGER,"
            "brand_version INTEGER,created_at REAL)"
        )
        brand_media_schema.install_schema(connection)
        brand_media_schema.install_schema(connection)
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(task_activity_image)"
        )}
        self.assertIn("required_text_json", columns)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("task_activity_image_review", tables)
        connection.close()

    def test_task_artwork_schema_and_tenant_scoped_file_delivery(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE task(id INTEGER PRIMARY KEY, tenant_id INTEGER, "
            "emp_idx INTEGER, deleted_at REAL)"
        )
        connection.execute(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id INTEGER, "
            "role TEXT, enabled INTEGER)"
        )
        connection.execute(
            "CREATE TABLE billing_operation(op_key TEXT PRIMARY KEY, status TEXT)"
        )
        connection.executemany(
            "INSERT INTO users(id,tenant_id,role,enabled) VALUES(?,?,?,1)",
            [(9, 7, "owner"), (10, 7, "member"), (11, 8, "owner")],
        )
        connection.executemany(
            "INSERT INTO task(id,tenant_id,emp_idx,deleted_at) VALUES(?,?,?,NULL)",
            [(44, 7, 160), (45, 8, 160), (46, 7, 3)],
        )
        brand_media_schema.install_schema(connection)

        @contextmanager
        def atomic():
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        def one(sql, args=()):
            row = connection.execute(sql, args).fetchone()
            return dict(row) if row else None

        def q(sql, args=()):
            return [dict(row) for row in connection.execute(sql, args).fetchall()]

        artwork = {
            "image_bytes": _png("blue"),
            "status": "passed",  # generator never bypasses admin review
            "quality": {"status": "needs_manual_review"},
            "required_text": {"store_name": "百味小馆",
                              "activity_title": "周末尝鲜",
                              "activity_content": "招牌面第二份半价",
                              "authorized_texts": ["认真做好每一餐"]},
            "brand_package_id": 31,
            "brand_version": 3,
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(brand_media.assetfiles, "ASSET_ROOT", directory), \
             patch.object(brand_media.db, "atomic", atomic), \
             patch.object(brand_media.db, "one", one), \
             patch.object(brand_media.db, "q", q):
            saved = brand_media.save_task_artwork(7, 44, "活动效果图", artwork)
            self.assertNotIn("/files/", saved["stored_path"])
            path = brand_media.get_task_artwork_file(7, 44, saved["id"])
            with open(path, "rb") as stream:
                self.assertEqual(_png("blue"), stream.read())
            listed = brand_media.list_task_artwork(7, 44)
            self.assertEqual(1, len(listed))
            self.assertEqual("活动效果图", listed[0]["group_key"])
            self.assertEqual("needs_manual_review", listed[0]["status"])
            self.assertNotIn("file_path", listed[0])
            disposable = brand_media.save_task_artwork(7, 44, "备选图", artwork)
            disposable_path = brand_media.get_task_artwork_file(
                7, 44, disposable["id"],
            )
            self.assertFalse(brand_media.delete_task_artwork(8, 44, disposable["id"]))
            self.assertTrue(os.path.isfile(disposable_path))
            self.assertTrue(brand_media.delete_task_artwork(7, 44, disposable["id"]))
            self.assertFalse(os.path.exists(disposable_path))
            self.assertFalse(brand_media.delete_task_artwork(7, 44, disposable["id"]))
            self.assertEqual([], brand_media.list_task_artwork(8, 44))
            with self.assertRaises(brand_media.BrandMediaError):
                brand_media.get_task_artwork_file(8, 44, saved["id"])
            with self.assertRaises(brand_media.BrandMediaError) as caught:
                brand_media.save_task_artwork(7, 46, "活动效果图", artwork)
            self.assertEqual("task_employee_invalid", caught.exception.code)
            with self.assertRaises(brand_media.BrandMediaError) as caught:
                brand_media.review_task_artwork(
                    7, 44, saved["id"], "approve", 10, "", "百味小馆 周末尝鲜 招牌面第二份半价",
                    True, no_extra_claims=True,
                )
            self.assertEqual("review_forbidden", caught.exception.code)
            rejected = brand_media.review_task_artwork(
                7, 44, saved["id"], "reject", 9, "Logo 失真", "百味小馆 周末尝鲜",
                False,
            )
            self.assertEqual("failed_qa", rejected["status"])
            extra = brand_media.review_task_artwork(
                7, 44, saved["id"], "approve", 9, "复核",
                "百味小馆 周末尝鲜 招牌面第二份半价 派活出品",
                True, no_extra_claims=True,
            )
            self.assertEqual("failed_qa", extra["status"])
            approved = brand_media.review_task_artwork(
                7, 44, saved["id"], "approve", 9, "人工确认",
                "百味小馆 周末尝鲜 招牌面第二份半价",
                True, no_extra_claims=True,
            )
            self.assertEqual("passed", approved["status"])
            history = brand_media.list_task_artwork_reviews(7, 44, saved["id"])
            self.assertEqual(3, len(history))
            self.assertEqual(["reject", "approve", "approve"],
                             [entry["decision"] for entry in history])
            self.assertEqual([], brand_media.list_task_artwork_reviews(8, 44, saved["id"]))
            self.assertEqual("passed", brand_media.list_task_artwork(7, 44)[0]["status"])
            with self.assertRaises(brand_media.BrandMediaError) as caught:
                brand_media.delete_task_artwork(7, 44, saved["id"])
            self.assertEqual("artwork_reviewed", caught.exception.code)
            self.assertTrue(os.path.isfile(path))
        connection.close()


if __name__ == "__main__":
    unittest.main()
