"""Schema 55 frontend contract: one person, immutable role generations."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(source: str, name: str) -> str:
    markers = (f"function {name}(", f"async function {name}(")
    starts = [source.find(marker) for marker in markers]
    starts = [position for position in starts if position >= 0]
    if not starts:
        raise AssertionError(f"missing JavaScript function: {name}")
    start = min(starts)
    candidates = [
        position
        for position in (
            source.find("\nfunction ", start + 1),
            source.find("\nasync function ", start + 1),
        )
        if position >= 0
    ]
    return source[start : min(candidates) if candidates else len(source)]


class Schema54FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_identity_and_person_status_are_independent(self):
        status = function(self.source, "employeeIdentityState")
        assign = function(self.source, "employeeCanAssignNew")
        learn = function(self.source, "employeeCanLearn")

        self.assertIn("person_status", status)
        self.assertIn("identity_status", status)
        self.assertIn("can_assign_new", assign)
        self.assertIn("can_learn", learn)
        self.assertIn("员工仍在岗 · 此任务使用历史岗位版本", self.source)
        self.assertNotIn("历史员工", self.source)
        self.assertNotIn("退役员工", self.source)
        self.assertNotIn("已退役", self.source)

    def test_current_cards_assign_and_learn_but_historical_tasks_only_continue(self):
        card = function(self.source, "specRoomCard")
        panel = function(self.source, "drawSpec")
        revision = function(self.source, "taskRevisionPanel")
        meeting = function(self.source, "meetingsView")

        self.assertIn("employeeCanAssignNew(e)", card)
        self.assertIn("employeeCanAssignNew(e)", panel)
        self.assertIn("employeeCanContinue(t)", revision)
        self.assertIn("employeeCanAssignNew(e)", meeting)
        self.assertIn("employeeCanLearn(e)", self.source)
        self.assertIn("employeeIdentityLabel(e)", panel)
        self.assertIn("config_revision", panel)

    def test_role_profile_is_grouped_escaped_and_read_only_for_frozen_identity(self):
        profile = function(self.source, "employeeProfessionalProfile")
        panel = function(self.source, "drawSpec")
        self.assertIn("专业知识域", profile)
        self.assertIn("技能树", profile)
        self.assertIn("核心能力", profile)
        self.assertIn("数据对象", profile)
        self.assertIn("工作流程", profile)
        self.assertIn("工具权限", profile)
        self.assertIn("升级路径", profile)
        self.assertIn("学习路径", profile)
        self.assertIn("esc(", profile)
        self.assertIn("readonly", profile)
        self.assertIn('ME.role!=="tour"?[["info","🪪 岗位档案"]]', panel)

    def test_dashboard_production_tasks_and_meetings_consume_frozen_identity(self):
        dashboard = function(self.source, "bossDashboardDraw")
        production = function(self.source, "productionView")
        task = function(self.source, "taskDetailView")
        member = function(self.source, "meetingMemberLabel")

        for block in (dashboard, production, member):
            self.assertIn("employeeIdentityLabel(", block)
        self.assertIn("employeeAssignmentState(t)", task)
        self.assertIn("employeeProfessionalProfile(t", task)
        self.assertIn("identity_ref", dashboard)
        self.assertIn("config_revision", dashboard)
        self.assertIn("identity_ref", production)
        self.assertIn("config_revision", production)

    def test_mutations_bind_the_exact_identity_and_config_revision(self):
        binding = function(self.source, "employeeIdentityMutationFields")
        create = function(self.source, "specSubmit")
        followup = function(self.source, "taskFollowup")
        meeting = function(self.source, "mtStart")

        self.assertIn("identity_ref", binding)
        self.assertIn("config_revision", binding)
        self.assertIn("config_sha256", binding)
        self.assertIn("...binding", create)
        self.assertIn("...binding", followup)
        self.assertIn("config_sha256", followup)
        self.assertIn("member_bindings", meeting)


if __name__ == "__main__":
    unittest.main()
