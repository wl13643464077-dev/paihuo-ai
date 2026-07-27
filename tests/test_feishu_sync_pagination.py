"""飞书同步必须越过历史 300 条上限，同时保持租户和批次边界。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import auth, db, feishu


class FeishuSyncPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "feishu-sync.db")
        db.conn()
        db.insert("tenants", {"id": 2, "name": "租户甲"})
        db.insert("tenants", {"id": 3, "name": "租户乙"})
        auth.set_current({
            "id": 20,
            "tenant_id": 2,
            "username": "owner-a",
            "role": "owner",
            "modules": ["library"],
        })

    def tearDown(self):
        auth.set_current(None)
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def _sync_with_fake_feishu(self, kind: str, already: set = None):
        batch_create = AsyncMock()
        with (
            patch.object(
                feishu,
                "tenant_bitable",
                return_value="https://example.feishu.cn/base/abcdefghijklmnop",
            ),
            patch.object(feishu, "_token", new=AsyncMock(return_value="token")),
            patch.object(
                feishu,
                "_ensure_table",
                new=AsyncMock(return_value="table-id"),
            ),
            patch.object(
                feishu,
                "existing_sync_keys",
                new=AsyncMock(return_value=set(already or ())),
            ),
            patch.object(feishu, "_batch_create", new=batch_create),
        ):
            result = await feishu.sync(kind)
        batches = [
            call.args[4]
            for call in batch_create.await_args_list
        ]
        return result, batches

    async def test_knowledge_sync_covers_every_active_tenant_row(self):
        for index in range(325):
            db.insert("knowledge", {
                "tenant_id": 2,
                "title": f"知识-{index:03d}",
                "content": "正文",
                "tags_json": "[]",
                "source": "manual",
                "pinned": 0,
                "meta_json": json.dumps({"category": "案例"}),
            })
        db.insert("knowledge", {
            "tenant_id": 3,
            "title": "外租户知识",
            "content": "正文",
            "tags_json": "[]",
            "source": "manual",
            "pinned": 0,
            "meta_json": "{}",
        })
        db.insert("knowledge", {
            "tenant_id": 2,
            "title": "已删除知识",
            "content": "正文",
            "tags_json": "[]",
            "source": "manual",
            "pinned": 0,
            "meta_json": "{}",
            "deleted_at": 1,
        })

        first_page, cursor = feishu._sync_page("knowledge", 2, None)
        second_page, _ = feishu._sync_page("knowledge", 2, cursor)
        self.assertEqual(feishu.SYNC_READ_PAGE_SIZE, len(first_page))
        self.assertEqual(125, len(second_page))

        result, batches = await self._sync_with_fake_feishu("knowledge")

        records = [record for batch in batches for record in batch]
        self.assertEqual(325, result["synced"])
        self.assertEqual(325, len(records))
        self.assertEqual(325, len({record["标题"] for record in records}))
        self.assertNotIn("外租户知识", {record["标题"] for record in records})
        self.assertNotIn("已删除知识", {record["标题"] for record in records})
        self.assertTrue(all(0 < len(batch) <= 100 for batch in batches))

    async def test_asset_sync_covers_every_tenant_row(self):
        for index in range(325):
            db.insert("asset", {
                "tenant_id": 2,
                "type": "final",
                "payload_json": json.dumps({"title": f"资产-{index:03d}"}),
                "meta_json": json.dumps({"category": "成品"}),
            })
        db.insert("asset", {
            "tenant_id": 3,
            "type": "final",
            "payload_json": json.dumps({"title": "外租户资产"}),
            "meta_json": "{}",
        })

        first_page, cursor = feishu._sync_page("asset", 2, None)
        second_page, _ = feishu._sync_page("asset", 2, cursor)
        self.assertEqual(feishu.SYNC_READ_PAGE_SIZE, len(first_page))
        self.assertEqual(125, len(second_page))

        result, batches = await self._sync_with_fake_feishu("asset")

        records = [record for batch in batches for record in batch]
        self.assertEqual(325, result["synced"])
        self.assertEqual(325, len(records))
        self.assertEqual(325, len({record["标题"] for record in records}))
        self.assertNotIn("外租户资产", {record["标题"] for record in records})
        self.assertTrue(all(0 < len(batch) <= 100 for batch in batches))

    async def test_paginated_sync_stops_and_propagates_provider_failure(self):
        for index in range(225):
            db.insert("knowledge", {
                "tenant_id": 2,
                "title": f"知识-{index:03d}",
                "content": "正文",
                "tags_json": "[]",
                "source": "manual",
                "pinned": 0,
                "meta_json": "{}",
            })
        expected = feishu.FeishuProviderError("写入飞书多维表格暂时不可用，请稍后重试")
        batch_create = AsyncMock(side_effect=[None, expected])
        with (
            patch.object(
                feishu,
                "tenant_bitable",
                return_value="https://example.feishu.cn/base/abcdefghijklmnop",
            ),
            patch.object(feishu, "_token", new=AsyncMock(return_value="token")),
            patch.object(
                feishu,
                "_ensure_table",
                new=AsyncMock(return_value="table-id"),
            ),
            patch.object(
                feishu,
                "existing_sync_keys",
                new=AsyncMock(return_value=set()),
            ),
            patch.object(feishu, "_batch_create", new=batch_create),
        ):
            with self.assertRaises(feishu.FeishuProviderError) as caught:
                await feishu.sync("knowledge")

        self.assertIs(expected, caught.exception)
        self.assertEqual(2, batch_create.await_count)

    async def test_repeat_sync_does_not_duplicate_existing_rows(self):
        """飞书侧只能追加写:同步过的记录必须跳过,否则每点一次就多一整份。"""
        for index in range(5):
            db.insert("knowledge", {
                "tenant_id": 2,
                "title": f"知识-{index}",
                "content": "正文",
                "tags_json": "[]",
                "source": "manual",
                "pinned": 0,
                "meta_json": "{}",
            })

        first, batches = await self._sync_with_fake_feishu("knowledge")
        self.assertEqual(5, first["synced"])
        written = [record for batch in batches for record in batch]
        keys = {record[feishu.SYNC_KEY_FIELD] for record in written}
        self.assertEqual(5, len(keys))

        # 第二次同步:表里已有这 5 条,应当一条都不再写。
        second, second_batches = await self._sync_with_fake_feishu(
            "knowledge", already=keys
        )
        self.assertEqual(0, second["synced"])
        self.assertEqual([], [r for b in second_batches for r in b])

        # 新增一条后,只补写这一条。
        db.insert("knowledge", {
            "tenant_id": 2, "title": "新知识", "content": "正文",
            "tags_json": "[]", "source": "manual", "pinned": 0,
            "meta_json": "{}",
        })
        third, third_batches = await self._sync_with_fake_feishu(
            "knowledge", already=keys
        )
        self.assertEqual(1, third["synced"])
        added = [r for b in third_batches for r in b]
        self.assertEqual(["新知识"], [r["标题"] for r in added])
