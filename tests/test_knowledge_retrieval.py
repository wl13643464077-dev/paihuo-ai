"""知识沉淀按当前任务召回，并向执行日志反馈实际生效数量。"""
import json
import os
import tempfile
import unittest

from app import db
from app.skills import registry


class KnowledgeRetrievalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = os.path.join(self.tmp.name, "knowledge.db")
        db.conn()
        db.insert("tenants", {"id": 1, "name": "甲企业"})
        db.insert("tenants", {"id": 2, "name": "乙企业"})

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def _knowledge(self, title, content, tenant=1, pinned=0, deleted_at=None):
        return db.insert("knowledge", {
            "tenant_id": tenant,
            "title": title,
            "content": content,
            "tags_json": json.dumps(["运营", "经验"], ensure_ascii=False),
            "pinned": pinned,
            "deleted_at": deleted_at,
        })

    def test_old_relevant_knowledge_beats_many_new_irrelevant_rows(self):
        self._knowledge(
            "上海茶饮复购手册",
            "茶饮门店通过会员分层、到店提醒和新品试饮提升复购率。",
        )
        for index in range(30):
            self._knowledge(
                f"酒店布草巡检 {index}",
                "客房清洁、布草盘点与前台交接标准。",
            )
        self._knowledge(
            "其他租户机密",
            "上海茶饮复购的隐藏方案。",
            tenant=2,
            pinned=1,
        )
        self._knowledge(
            "已删除的茶饮方案",
            "上海茶饮复购。",
            deleted_at=123.0,
        )

        block, meta = registry.context_block(
            1, "上海茶饮门店怎么提升会员复购", with_meta=True
        )

        self.assertIn("上海茶饮复购手册", block)
        self.assertNotIn("其他租户机密", block)
        self.assertNotIn("已删除的茶饮方案", block)
        self.assertLessEqual(meta["selected"], 12)
        self.assertEqual(31, meta["total"])
        self.assertIn("从 31 条中匹配", block)

    def test_pinned_knowledge_is_kept_and_progress_reports_recall(self):
        self._knowledge("品牌红线", "禁止使用绝对化承诺。", pinned=1)
        self._knowledge("宠物洗护", "预约时段按犬种和体型拆分。")
        events = []
        ctx = {
            "tenant_id": 1,
            "brief": {
                "direction": "宠物洗护预约内容",
                "platforms": ["小红书"],
            },
            "profile": {},
            "progress": lambda kind, label: events.append((kind, label)),
        }

        bundle = registry.build_prompt(
            ctx,
            "trend",
            {"today": "2026-07-26", "channels": "公开渠道"},
        )

        self.assertIn("品牌红线", bundle.user)
        self.assertIn("宠物洗护", bundle.user)
        self.assertTrue(any(
            kind == "tool" and "公司知识库召回" in label
            for kind, label in events
        ))


if __name__ == "__main__":
    unittest.main()
