import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import departments, gate


class ImmutableReleaseConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        departments._cache = None
        self.tmp.cleanup()

    @staticmethod
    def _department(name):
        return {
            "key": name,
            "name": name,
            "employees": [],
        }

    def test_departments_prefer_immutable_release_config_over_mutable_state(self):
        immutable = self.base / "config" / "departments"
        mutable = self.base / "data" / "departments"
        immutable.mkdir(parents=True)
        mutable.mkdir(parents=True)
        (immutable / "industry.json").write_text(
            json.dumps(self._department("immutable")),
            encoding="utf-8",
        )
        (mutable / "industry.json").write_text(
            json.dumps(self._department("mutable")),
            encoding="utf-8",
        )

        with (
            patch.object(departments, "CONFIG_DEPT_DIR", str(immutable)),
            patch.object(departments, "LEGACY_DEPT_DIR", str(mutable)),
            patch.object(departments, "MANIFEST_PATH", str(self.base / "missing-manifest")),
        ):
            departments._cache = None
            loaded = departments.list_depts()
        self.assertEqual(["immutable"], [row["key"] for row in loaded])

    def test_formal_release_never_falls_back_to_mutable_departments(self):
        manifest = self.base / "RELEASE-MANIFEST.json"
        immutable = self.base / "config" / "departments"
        mutable = self.base / "data" / "departments"
        manifest.write_text("{}\n", encoding="utf-8")
        mutable.mkdir(parents=True)
        (mutable / "industry.json").write_text(
            json.dumps(self._department("mutable")),
            encoding="utf-8",
        )

        with (
            patch.object(departments, "CONFIG_DEPT_DIR", str(immutable)),
            patch.object(departments, "LEGACY_DEPT_DIR", str(mutable)),
            patch.object(departments, "MANIFEST_PATH", str(manifest)),
            self.assertRaisesRegex(
                departments.DepartmentConfigError,
                "immutable",
            ),
        ):
            departments._cache = None
            departments.list_depts()

        immutable.mkdir(parents=True)
        (immutable / "broken.json").write_text("{broken", encoding="utf-8")
        with (
            patch.object(departments, "CONFIG_DEPT_DIR", str(immutable)),
            patch.object(departments, "LEGACY_DEPT_DIR", str(mutable)),
            patch.object(departments, "MANIFEST_PATH", str(manifest)),
            self.assertRaisesRegex(
                departments.DepartmentConfigError,
                "JSON",
            ),
        ):
            departments._cache = None
            departments.list_depts()

    def test_gate_initializes_missing_state_from_seed_then_prefers_hot_update(self):
        seed = self.base / "config" / "gate_rules.default.json"
        state = self.base / "data" / "gate_rules.json"
        seed.parent.mkdir(parents=True)
        seed.write_text(
            json.dumps({"sensitive_words": ["seed"], "notes": "seed"}),
            encoding="utf-8",
        )

        with (
            patch.object(gate, "SEED_RULES_PATH", str(seed)),
            patch.object(gate, "STATE_RULES_PATH", str(state)),
        ):
            self.assertEqual(["seed"], gate._rules()["sensitive_words"])
            self.assertTrue(state.is_file())
            self.assertEqual(0o600, state.stat().st_mode & 0o777)
            state.write_text(
                json.dumps({"sensitive_words": ["hot"], "notes": "hot"}),
                encoding="utf-8",
            )
            self.assertEqual(["hot"], gate._rules()["sensitive_words"])
        self.assertEqual(["seed"], json.loads(seed.read_text())["sensitive_words"])

    def test_invalid_hot_state_fails_closed_instead_of_falling_back_to_seed(self):
        seed = self.base / "config" / "gate_rules.default.json"
        state = self.base / "data" / "gate_rules.json"
        seed.parent.mkdir(parents=True)
        state.parent.mkdir(parents=True)
        seed.write_text(
            json.dumps({"sensitive_words": ["seed"], "notes": "seed"}),
            encoding="utf-8",
        )
        state.write_text("{not-json", encoding="utf-8")

        with (
            patch.object(gate, "SEED_RULES_PATH", str(seed)),
            patch.object(gate, "STATE_RULES_PATH", str(state)),
            self.assertRaises(json.JSONDecodeError),
        ):
            gate._rules()

    def test_formal_release_missing_gate_seed_fails_before_state_creation(self):
        manifest = self.base / "RELEASE-MANIFEST.json"
        seed = self.base / "config" / "gate_rules.default.json"
        state = self.base / "data" / "gate_rules.json"
        manifest.write_text("{}\n", encoding="utf-8")

        with (
            patch.object(gate, "MANIFEST_PATH", str(manifest)),
            patch.object(gate, "SEED_RULES_PATH", str(seed)),
            patch.object(gate, "STATE_RULES_PATH", str(state)),
            self.assertRaisesRegex(ValueError, "immutable gate"),
        ):
            gate._rules()
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
