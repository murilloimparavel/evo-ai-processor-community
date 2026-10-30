"""Validate the real builder helper without importing network/ADK clients."""
import ast
from pathlib import Path
import unittest

path = Path(__file__).parents[2] / 'src/services/adk/agents/llm_agent_builder.py'
tree = ast.parse(path.read_text())
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_pipeline_criteria_instructions')
scope = {}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
instructions = scope['_pipeline_criteria_instructions']


class PipelineCriteriaInstructionsTest(unittest.TestCase):
    def test_catalog_is_authoritative_and_legacy_requires_explicit_selection(self):
        value = instructions([{'pipelineId': 'configured-pipeline', 'instructions': 'Old stage hint'}])
        self.assertIn('action=get_pipeline_criteria', value)
        self.assertIn('criteria_source=catalog_v1', value)
        self.assertIn('ignore legacy semantic hints', value)
        self.assertIn('ONLY after the CRM explicitly returns criteria_source=legacy', value)
        self.assertIn('Old stage hint', value)
        self.assertIn('do not move and do not fall back', value)

    def test_no_pipeline_rules_is_not_permission_to_invent_a_destination(self):
        self.assertIn('do not invent a destination', instructions([]))

    def test_oversized_hints_are_omitted_whole_instead_of_truncated(self):
        value = instructions([{'pipelineId': 'configured', 'instructions': 'x' * 5000 + ' never move'}])
        self.assertNotIn('x' * 20, value)
        self.assertIn('do not move using incomplete or inferred rules', value)

    def test_builder_uses_the_shared_helper_in_the_actual_crm_instructions_block(self):
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_pipeline_criteria_instructions']
        self.assertEqual(len(calls), 1)
