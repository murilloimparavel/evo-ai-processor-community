import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('hierarchy', Path(__file__).parents[2] / 'src/services/adk/agents/agent_hierarchy.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def agent(name):
    return SimpleNamespace(name=name, parent_agent=None, sub_agents=[])


class HierarchyTest(unittest.TestCase):
    def test_dynamic_children_get_parent_and_keep_descendants(self):
        root, closing, post_sale = agent('sofia'), agent('closing'), agent('post_sale')
        module.attach_sub_agents(closing, [post_sale])
        module.attach_sub_agents(root, [closing])
        self.assertIs(closing.parent_agent, root)
        self.assertIs(post_sale.parent_agent, closing)
        self.assertEqual(root.sub_agents, [closing])

    def test_other_parent_is_rejected_before_mutation(self):
        root, other, child = agent('root'), agent('other'), agent('child')
        module.attach_sub_agents(other, [child])
        with self.assertRaises(ValueError):
            module.attach_sub_agents(root, [child])
        self.assertEqual(root.sub_agents, [])
        self.assertIs(child.parent_agent, other)

    def test_self_and_duplicate_names_are_rejected(self):
        root = agent('root')
        with self.assertRaises(ValueError):
            module.attach_sub_agents(root, [root])
        with self.assertRaises(ValueError):
            module.attach_sub_agents(root, [agent('same'), agent('same')])


if __name__ == '__main__':
    unittest.main()
