import asyncio
import json
import os
import tempfile
import unittest

import httpx
from fastapi import HTTPException

from app import auth, db


class ListPaginationContractTests(unittest.TestCase):
    """用户可翻过旧硬上限，且分页、筛选和租户边界都由服务端保证。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "pagination.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        self.user_id = db.insert("users", {
            "tenant_id": 2,
            "username": "pagination-owner",
            "password_hash": "fixture-pagination-hash",
            "role": "owner",
            "modules_json": json.dumps(["content", "library"]),
            "enabled": 1,
            "must_change_password": 0,
        })
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner-a",
            "role": "owner",
            "modules": ["content", "library"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _insert_many(self, table, count, values):
        ids = []
        for index in range(count):
            row = values(index)
            ids.append(db.insert(table, row))
        return ids

    def _assert_page(self, page, limit, offset, total):
        self.assertEqual(limit, page["limit"])
        self.assertEqual(offset, page["offset"])
        self.assertEqual(total, page["total"])
        self.assertEqual(page["has_more"], page["truncated"])
        expected_next = offset + limit if offset + len(page["items"]) < total else None
        self.assertEqual(expected_next, page["next_offset"])

    def test_knowledge_and_assets_cross_old_limits_with_server_filters(self):
        from app import main

        knowledge_ids = self._insert_many(
            "knowledge",
            505,
            lambda i: {
                "tenant_id": 2,
                "title": f"甲知识{i:03d}",
                "content": "正文",
                "tags_json": "[]",
                "source": "manual",
                "pinned": 0,
                "meta_json": json.dumps({
                    "platform": "公众号" if i % 2 == 0 else "抖音",
                    "category": "教程" if i % 3 == 0 else "案例",
                }),
            },
        )
        foreign_knowledge = db.insert("knowledge", {
            "tenant_id": 3,
            "title": "甲知识-外租户",
            "content": "正文",
            "tags_json": "[]",
            "source": "manual",
            "pinned": 0,
            "meta_json": json.dumps({"platform": "公众号", "category": "教程"}),
        })
        page = main.knowledge_list(limit=10, offset=500)
        self._assert_page(page, 10, 500, 505)
        self.assertEqual(knowledge_ids[:5][::-1], [row["id"] for row in page["items"]])
        self.assertNotIn(foreign_knowledge, [row["id"] for row in page["items"]])
        filtered = main.knowledge_list(
            limit=20, offset=0, q="甲知识", platform="公众号", category="教程"
        )
        self.assertTrue(filtered["items"])
        self.assertTrue(all(
            row["meta"]["platform"] == "公众号"
            and row["meta"]["category"] == "教程"
            and "甲知识" in row["title"]
            for row in filtered["items"]
        ))

        asset_ids = self._insert_many(
            "asset",
            205,
            lambda i: {
                "tenant_id": 2,
                "type": "topic",
                "payload_json": json.dumps({"title": f"甲资产{i:03d}"}),
                "meta_json": json.dumps({
                    "platform": "小红书" if i % 2 == 0 else "视频号",
                    "category": "种草" if i % 3 == 0 else "测评",
                }),
            },
        )
        foreign_asset = db.insert("asset", {
            "tenant_id": 3,
            "type": "topic",
            "payload_json": json.dumps({"title": "甲资产-外租户"}),
            "meta_json": json.dumps({"platform": "小红书", "category": "种草"}),
        })
        page = main.assets(type="topic", limit=10, offset=200)
        self._assert_page(page, 10, 200, 205)
        self.assertEqual(asset_ids[:5][::-1], [row["id"] for row in page["items"]])
        self.assertNotIn(foreign_asset, [row["id"] for row in page["items"]])
        self.assertEqual(asset_ids[0], main.asset_detail(asset_ids[0])["id"])
        with self.assertRaises(HTTPException) as caught:
            main.asset_detail(foreign_asset)
        self.assertEqual(404, caught.exception.status_code)
        filtered = main.assets(
            type="topic", limit=20, offset=0, q="甲资产",
            platform="小红书", category="种草",
        )
        self.assertTrue(filtered["items"])
        self.assertTrue(all(
            row["payload"]["title"].startswith("甲资产")
            and row["meta"]["platform"] == "小红书"
            and row["meta"]["category"] == "种草"
            for row in filtered["items"]
        ))

    def test_review_video_tool_and_publish_lists_cross_old_limits(self):
        from app import main

        cases = [
            (
                "censor_log",
                55,
                lambda i: {
                    "tenant_id": 2, "kind": "pre", "platform": "公众号",
                    "title": f"审查{i:03d}", "verdict": "pass", "issues_json": "[]",
                },
                lambda: main.censor_logs(
                    limit=10, offset=50, kind="pre", platform="公众号", q="审查"
                ),
                50,
            ),
            (
                "tv_job",
                25,
                lambda i: {
                    "tenant_id": 2, "params_json": json.dumps({"title": f"成片{i:03d}"}),
                    "status": "done", "billing_status": "charged", "steps_json": "[]",
                },
                lambda: main.text_video_list(
                    limit=10, offset=20, status="done", q="成片"
                ),
                20,
            ),
            (
                "tool_job",
                20,
                lambda i: {
                    "tenant_id": 2, "kind": "leads",
                    "params_json": json.dumps({"query": f"线索{i:03d}"}),
                    "status": "done", "billing_status": "charged",
                    "result_json": json.dumps({"ok": True}),
                },
                lambda: main.tool_jobs(
                    limit=10, offset=15, kind="leads", status="done"
                ),
                15,
            ),
            (
                "pub_task",
                25,
                lambda i: {
                    "tenant_id": 2, "platform": "xhs", "account": "a",
                    "payload_json": json.dumps({"title": f"发布{i:03d}"}),
                    "status": "done",
                },
                lambda: main.matrix_tasks(
                    limit=10, offset=20, platform="xhs", status="done", q="发布"
                ),
                20,
            ),
        ]
        for table, count, values, get_page, old_limit in cases:
            with self.subTest(table=table):
                own_ids = self._insert_many(table, count, values)
                foreign_values = values(999)
                foreign_values["tenant_id"] = 3
                foreign_id = db.insert(table, foreign_values)
                page = get_page()
                self._assert_page(page, 10, old_limit, count)
                self.assertEqual(
                    own_ids[:count - old_limit][::-1],
                    [row["id"] for row in page["items"]],
                )
                self.assertNotIn(foreign_id, [row["id"] for row in page["items"]])

    def test_boundaries_stable_order_and_empty_pages(self):
        from app import main

        ids = self._insert_many(
            "censor_log",
            7,
            lambda i: {
                "tenant_id": 2, "kind": "pre", "platform": "公众号",
                "title": f"记录{i}", "issues_json": "[]",
            },
        )
        first = main.censor_logs(limit=3, offset=0)
        second = main.censor_logs(limit=3, offset=first["next_offset"])
        combined = [row["id"] for row in first["items"] + second["items"]]
        self.assertEqual(ids[-1:-7:-1], combined)
        self.assertEqual(len(combined), len(set(combined)))

        empty = main.censor_logs(limit=3, offset=99)
        self._assert_page(empty, 3, 99, 7)
        self.assertEqual([], empty["items"])

        for kwargs in (
            {"limit": 0, "offset": 0},
            {"limit": 101, "offset": 0},
            {"limit": 10, "offset": -1},
            {"limit": 10, "offset": 1_000_001},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(HTTPException) as caught:
                main.censor_logs(**kwargs)
            self.assertEqual(422, caught.exception.status_code)

    def test_http_contract_exposes_metadata_and_rejects_bad_boundaries(self):
        from app import main

        self._insert_many(
            "censor_log",
            7,
            lambda i: {
                "tenant_id": 2, "kind": "pre", "platform": "公众号",
                "title": f"HTTP审查{i}", "issues_json": "[]",
            },
        )

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://paihuo.test",
                cookies={"cc_sess": auth.make_session(self.user_id)},
            ) as client:
                response = await client.get(
                    "/api/censor/logs",
                    params={"limit": 3, "offset": 3, "platform": "公众号"},
                )
                self.assertEqual(200, response.status_code, response.text)
                page = response.json()
                self._assert_page(page, 3, 3, 7)
                self.assertEqual(3, len(page["items"]))
                invalid = await client.get(
                    "/api/censor/logs", params={"limit": 101, "offset": 0}
                )
                self.assertEqual(422, invalid.status_code, invalid.text)

        asyncio.run(exercise())

    def test_legacy_calls_without_limit_keep_array_contracts(self):
        from app import main

        self.assertIsInstance(main.knowledge_list(), list)
        self.assertIsInstance(main.assets(), list)
        self.assertIsInstance(main.censor_logs(), list)
        self.assertIsInstance(main.text_video_list(), list)
        self.assertIsInstance(main.tool_jobs(), list)
        self.assertIsInstance(main.matrix_tasks(), list)


if __name__ == "__main__":
    unittest.main()
