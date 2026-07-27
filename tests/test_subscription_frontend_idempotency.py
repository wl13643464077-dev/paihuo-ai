"""Browser-side contract for subscription idempotency."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SubscriptionFrontendIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_order_id_is_persisted_and_sent_to_the_server(self):
        self.assertIn("const SUBSCRIPTION_ORDER_IDS=new Map()", self.source)
        self.assertIn("localStorage.setItem(key,JSON.stringify({id,created_at:Date.now()}))", self.source)
        self.assertIn("body:{plan,period,order_id:orderId}", self.source)
        self.assertNotIn(
            'body:{plan,period}})',
            self.source,
            "套餐请求不能再以无订单号的旧协议发送",
        )

    def test_network_retry_reuses_the_same_order_id(self):
        start = self.source.index("async function submitSubscription(")
        end = self.source.index("\nasync function tmSub(", start)
        helper = self.source[start:end]
        self.assertIn("for(let attempt=0;attempt<2;attempt++)", helper)
        self.assertIn("order_id:orderId", helper)
        self.assertNotIn("newSubscriptionOrderId", helper)

    def test_success_clears_pending_key_only_after_receipt(self):
        start = self.source.index("async function tmSub(")
        end = self.source.index("\nasync function tmTenantToggle(", start)
        handler = self.source[start:end]
        submit_at = handler.index("await submitSubscription")
        clear_at = handler.index("clearSubscriptionOrderId")
        self.assertLess(submit_at, clear_at)


if __name__ == "__main__":
    unittest.main()
