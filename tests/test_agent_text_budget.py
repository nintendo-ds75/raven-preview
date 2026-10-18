"""What an agent reads from Raven stays short enough to be read.

The budgets come from docs/quality-rubric.md, dimension 6: at most 400
words injected per prompt by the installed hook and at most 2,500 words of
tool text. Measured before the cut: 558 words per prompt, 4,196 words of
tool text, and 1,390 words of initialize instructions of which Claude Code
showed the first ~2,150 characters, none of them the protocol steps.
"""
import unittest
from unittest.mock import patch

from bridge import host_client, mcp

HOOK_WORDS = 400
TOOL_WORDS = 2500
DESCRIPTION_WORDS = 60
# Claude Code renders roughly this much of a server's instructions.
VISIBLE_INSTRUCTION_CHARS = 2150


def words(text):
    return len(text.split())


def parameter_words(properties):
    total = 0
    for spec in properties.values():
        if not isinstance(spec, dict):
            continue
        total += words(spec.get('description', ''))
        total += parameter_words(spec.get('properties', {}))
        items = spec.get('items')
        if isinstance(items, dict):
            total += parameter_words(items.get('properties', {}))
    return total


class HookBudgetTests(unittest.TestCase):
    # A workspace with history, three kinds of source and three other MCP
    # servers: richer than the fixtures the host tests use, so the budget
    # holds for a real workspace, not only an empty one.
    context = {'repo': 'example/service', 'history': True,
               'sources': [{'kind': 'slack', 'id': '#billing-eng'}, {'kind': 'slack', 'id': '#platform'},
                           {'kind': 'slack', 'id': '#incidents'}, {'kind': 'jira', 'id': 'BILL'},
                           {'kind': 'jira', 'id': 'PLAT'}, {'kind': 'docs', 'id': 'https://docs.example.invalid'}],
               'servers': ['github', 'linear', 'sentry']}

    def hook_text(self, event):
        tools = [t['name'] for t in mcp.TOOLS]
        with patch.object(host_client, 'verify_project'), patch.object(host_client, 'capabilities', return_value=tools), \
             patch.object(host_client, 'call', return_value={'task_id': 'task-123456', 'context': self.context}), \
             patch.object(host_client, 'binding', return_value={}):
            out = host_client.hook({'project': '/example/checkout'}, 'claude',
                                   {'hook_event_name': event, 'session_id': 'synthetic', 'prompt': 'Fix the typo.'})
        return out['hookSpecificOutput']['additionalContext']

    def test_every_prompt_receives_at_most_the_budget(self):
        for event in ('SessionStart', 'UserPromptSubmit'):
            with self.subTest(event=event):
                text = self.hook_text(event)
                self.assertLessEqual(words(text), HOOK_WORDS, text)
                self.assertIn(host_client.DECISION_GUIDANCE, text)
                self.assertIn('task-123456', text)

    def test_the_hook_names_only_the_rules_the_tools_do_not_carry(self):
        text = self.hook_text('UserPromptSubmit')
        # The notification protocol lives on bridge_host_notification; the
        # tool list is the host's own; neither is repeated every prompt.
        self.assertNotIn('send_once', text)
        self.assertNotIn('Available tools', text)
        notification = next(t['description'] for t in mcp.TOOLS if t['name'] == 'bridge_host_notification')
        for rule in ('persists the request first', 'send once when send_once is true', 'never retry an unknown send',
                     'Answers and sign-off stay in Raven'):
            self.assertIn(rule, notification)


class ToolTextBudgetTests(unittest.TestCase):
    def test_tool_text_fits_the_budget(self):
        total = 0
        for tool in mcp.TOOLS:
            described = words(tool['description'])
            self.assertLessEqual(described, DESCRIPTION_WORDS, tool['name'])
            total += described + parameter_words(tool['inputSchema']['properties'])
        self.assertLessEqual(total, TOOL_WORDS, total)

    def test_instructions_lead_with_the_whole_protocol(self):
        self.assertTrue(mcp.INSTRUCTIONS.startswith(mcp.PROTOCOL))
        self.assertEqual(mcp.INSTRUCTIONS, mcp.PROTOCOL + host_client.DECISION_GUIDANCE + mcp.AUTHORIZATION)
        visible = mcp.INSTRUCTIONS[:VISIBLE_INSTRUCTION_CHARS]
        for step in ('1. Call bridge_start_task', '2. On engage', '3. Record what you settle', '4. Read bridge_get_tree',
                     '5. Call bridge_finish_task', '6. While the advisory review'):
            self.assertIn(step, visible)
        self.assertLessEqual(words(mcp.INSTRUCTIONS), 800)

    def test_each_rule_is_stated_once_per_surface(self):
        # The evidence block is in the hook and in the instructions through
        # DECISION_GUIDANCE, not restated in bridge_add_node's description.
        add_node = next(t['description'] for t in mcp.TOOLS if t['name'] == 'bridge_add_node')
        self.assertNotIn('bridge_context_sources', add_node)
        self.assertNotIn('bridge_search_context', add_node)
        self.assertEqual(mcp.INSTRUCTIONS.count('shell command substitution'), 1)
        self.assertEqual(mcp.INSTRUCTIONS.count('Evidence is not authorization'), 1)


if __name__ == '__main__':
    unittest.main()
