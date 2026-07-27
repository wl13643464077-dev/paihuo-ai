"""Static UX contracts for the purchase-intent commercial loop."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PurchaseFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.promo = (ROOT / "static" / "promo.html").read_text(encoding="utf-8")
        cls.login = (ROOT / "static" / "login.html").read_text(encoding="utf-8")

    def test_public_pricing_uses_server_catalog_and_honest_offline_copy(self):
        self.assertIn('fetch("/api/purchases/catalog")', self.promo)
        self.assertIn("不是在线支付", self.promo)
        self.assertIn("确认线下到账后", self.promo)
        self.assertIn("实时价格暂不可用", self.promo)
        self.assertIn("请登录后获取实时服务端报价", self.promo)
        for stale_price in ("¥69", "¥199", "¥599", "¥1999"):
            self.assertNotIn(stale_price, self.promo)
        for plan in ("trial", "startup", "biz", "flagship"):
            self.assertIn(f'data-plan="{plan}"', self.promo)
            self.assertIn(f"startPurchase('{plan}')", self.promo)

    def test_selected_plan_survives_application_and_login(self):
        self.assertIn("paihuo:pending-purchase", self.promo)
        self.assertIn("paihuo:pending-purchase", self.login)
        self.assertIn("paihuo:purchase-contact", self.login)
        self.assertIn("selectedPurchase() ? '/#/billing' : '/'", self.login)
        self.assertIn("[购买意向]", self.login)

    def test_owner_purchase_and_root_follow_up_are_separate(self):
        self.assertIn('api("/purchases",{method:"POST"', self.app_js)
        self.assertIn("contact,note,source", self.app_js)
        self.assertIn("PURCHASE_SOURCE_LABELS", self.app_js)
        self.assertIn('api(`/admin/purchases?${adminQuery.toString()}`)', self.app_js)
        self.assertIn('api("/admin/purchases/stats")', self.app_js)
        self.assertIn("仅企业主账号可以提交购买申请", self.app_js)
        self.assertIn("只有确认真实到账后才能点“已到账”", self.app_js)
        self.assertIn("确认真实到账", self.app_js)

    def test_request_key_is_persisted_for_safe_replay(self):
        self.assertIn("purchaseRequestStorageKey", self.app_js)
        self.assertIn("purchaseRequestId(plan,period)", self.app_js)
        self.assertIn("purchase:${Number(ME?.id||0)}:", self.app_js)


if __name__ == "__main__":
    unittest.main()
