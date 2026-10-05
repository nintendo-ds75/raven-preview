import json
import shlex
import unittest

from bridge.agent_launcher import command_for


class LauncherTests(unittest.TestCase):
    def test_claude_configuration_and_resume(self):
        args = command_for('claude', 'resume', 'http://localhost:7333/mcp', 'private')
        self.assertEqual(args[-1], '--resume')
        config = json.loads(args[2])
        self.assertEqual(config['mcpServers']['bridge']['headers']['Authorization'], 'Bearer private')
        self.assertNotIn('--dangerously-skip-permissions', args)

    def test_codex_preserves_arguments(self):
        args = command_for('codex', 'new', 'http://localhost:7333/mcp', "token'; echo unsafe")
        self.assertEqual(shlex.split(shlex.join(args)), args)
        self.assertNotIn('resume', args)
        self.assertEqual(command_for('codex', 'resume', 'http://localhost:7333/mcp', 'x')[-1], 'resume')

    def test_allowlist(self):
        for client, action in [('sh', 'new'), ('claude', 'exec'), ('cursor', 'new'), (None, None)]:
            with self.assertRaises(ValueError):
                command_for(client, action, 'http://localhost:7333/mcp', 'x')
