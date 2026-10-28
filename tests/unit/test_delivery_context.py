import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("delivery_context", Path(__file__).parents[2] / "src/utils/delivery_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeliveryContextTest(unittest.TestCase):
    def test_cancelled_options_are_not_assumed_delivered(self):
        instruction = module.delivery_context_instruction({"crm_delivery_context": {
            "version": 1, "conversation_id": "c1", "messages": []}}, "c1")
        self.assertIn("cancelled and never sent", instruction)
        self.assertIn("present the actual options", instruction)
        self.assertTrue(instruction.endswith("[]"))

    def test_delivered_options_are_available(self):
        instruction = module.delivery_context_instruction({"crm_delivery_context": {
            "version": 1, "conversation_id": "c1", "messages": [
                {"id": "m1", "content": "Opção A: R$ 100"}]}}, "c1")
        self.assertIn("Opção A: R$ 100", instruction)
        self.assertIn("not a conversation reset", instruction)

    def test_other_conversation_is_ignored(self):
        self.assertIsNone(module.delivery_context_instruction({"crm_delivery_context": {
            "version": 1, "conversation_id": "c2", "messages": []}}, "c1"))

    def test_legacy_and_invalid_metadata_are_ignored(self):
        self.assertIsNone(module.delivery_context_instruction(None, "c1"))
        self.assertIsNone(module.delivery_context_instruction({"crm_delivery_context": "bad"}, "c1"))

if __name__ == "__main__":
    unittest.main()
