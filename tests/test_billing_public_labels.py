"""价目表给客户看的显示名:不出现供应商名;价目表本身的 label/点数不变(流水按 label 记账)。"""
from __future__ import annotations

import unittest

from app import billing


class PublicLabelTests(unittest.TestCase):
    def test_default_prices_hide_supplier_names(self):
        for action, row in billing.DEFAULT_PRICES.items():
            label = billing.public_label(action, row["label"])
            for word in ("RunningHub", "Seedance", "可灵", "HeyGen"):
                self.assertNotIn(word, label, action)
        self.assertEqual("数字人视频·基础版",
                         billing.public_label("avatar_basic", billing.DEFAULT_PRICES["avatar_basic"]["label"]))
        self.assertEqual("AI 带货短片(15秒)",
                         billing.public_label("avatar_sales", billing.DEFAULT_PRICES["avatar_sales"]["label"]))

    def test_price_table_itself_is_unchanged(self):
        # 扣点流水按原 label 记账、归类:原表不能被显示层改掉
        self.assertEqual("数字人视频·基础版(RunningHub)", billing.DEFAULT_PRICES["avatar_basic"]["label"])
        self.assertEqual(6, billing.DEFAULT_PRICES["avatar_basic"]["points"])

    def test_custom_labels_and_ledger_reasons_are_scrubbed_the_same_way(self):
        self.assertEqual("口播视频(30秒)", billing.public_label("x", "口播视频(30秒·HeyGen)"))
        self.assertEqual("声音克隆", billing.public_label("voice_clone", "声音克隆"))
        charge = billing.public_reason("数字人视频·基础版(RunningHub) · #12")
        refund = billing.public_reason("退回:数字人视频·基础版(RunningHub) · #12")
        self.assertEqual("数字人视频·基础版 · #12", charge)
        self.assertEqual("退回:" + charge, refund)
        self.assertEqual("内容流水线整单 · #3", billing.public_reason("内容流水线整单 · #3"))


if __name__ == "__main__":
    unittest.main()
