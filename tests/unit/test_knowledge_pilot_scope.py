"""Deployment-owned selection cannot be influenced by the model or metadata."""
import os
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "knowledge_pilot_test", Path(__file__).resolve().parents[2] / "src/services/knowledge_runtime/pilot.py"
)
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
bound_grants_required = pilot.bound_grants_required
isolated_root_required = pilot.isolated_root_required
pilot_root_selected = pilot.pilot_root_selected

ROOT_A = "11111111-1111-4111-8111-111111111111"
ROOT_B = "22222222-2222-4222-8222-222222222222"


class PilotScopeTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("KNOWLEDGE_RUNTIME_PILOT_ROOT_IDS", None)
        os.environ["KNOWLEDGE_RUNTIME_ISOLATION_MODE"] = "required"
        os.environ["KNOWLEDGE_A2A_BOUND_GRANTS_ENABLED"] = "true"
        self.addCleanup(self.environment.stop)

    def test_explicit_pilot_selects_same_root_for_both_controls(self):
        os.environ["KNOWLEDGE_RUNTIME_PILOT_ROOT_IDS"] = ROOT_A
        for control in (bound_grants_required, isolated_root_required):
            self.assertTrue(control(ROOT_A))
            self.assertFalse(control(ROOT_B))

    def test_invalid_config_never_silently_selects_legacy(self):
        for invalid in ("", "bad", ROOT_A + ",", ROOT_A + "," + ROOT_A):
            os.environ["KNOWLEDGE_RUNTIME_PILOT_ROOT_IDS"] = invalid
            for root in (ROOT_A, ROOT_B):
                with self.assertRaises(ValueError):
                    pilot_root_selected(root)

    def test_absent_selector_rejects_enabled_features(self):
        with self.assertRaises(ValueError):
            isolated_root_required(ROOT_A)
        with self.assertRaises(ValueError):
            bound_grants_required(ROOT_B)
        os.environ["KNOWLEDGE_RUNTIME_ISOLATION_MODE"] = "off"
        os.environ["KNOWLEDGE_A2A_BOUND_GRANTS_ENABLED"] = "false"
        self.assertFalse(isolated_root_required(ROOT_A))
        self.assertFalse(bound_grants_required(ROOT_B))

    def test_unknown_mode_and_unknown_executor_fail_closed(self):
        os.environ["KNOWLEDGE_RUNTIME_PILOT_ROOT_IDS"] = ROOT_A
        os.environ["KNOWLEDGE_RUNTIME_ISOLATION_MODE"] = "typo"
        with self.assertRaises(ValueError):
            isolated_root_required(ROOT_A)
        with self.assertRaises(ValueError):
            isolated_root_required(None)

    def test_malformed_feature_flag_is_rejected(self):
        os.environ["KNOWLEDGE_RUNTIME_PILOT_ROOT_IDS"] = ROOT_A
        os.environ["KNOWLEDGE_A2A_BOUND_GRANTS_ENABLED"] = "treu"
        with self.assertRaises(ValueError):
            bound_grants_required(ROOT_A)
