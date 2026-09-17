"""Offline regression tests; staged fixtures live only in temporary repositories."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from quant_guard import check, content_issues, python_issues

ROOT = Path(__file__).resolve().parents[2]


class GuardTests(unittest.TestCase):
    def test_simulation_is_not_order_execution(self):
        self.assertEqual([], python_issues(
            '# broker.submit_order()\nbuy_price = 100\nsell_price = 110\n', 'simulation.py'))
        self.assertTrue(python_issues('broker.submit_order()', 'live.py'))
        self.assertTrue(python_issues('from broker import submit_order as send\nsend()', 'live.py'))

    def test_request_aliases_and_sessions(self):
        for code in ['import requests as r\nr.get(url)',
                     'from requests import post as send\nsend(url)',
                     'import requests\ns = requests.Session()\ns.get(url)']:
            with self.subTest(code=code):
                self.assertTrue(python_issues(code, 'request.py'))
                self.assertEqual([], python_issues(code.replace('(url)', '(url, timeout=5)'), 'request.py'))

    def test_invalid_literal_timeouts(self):
        for value in ['None', '0', '-1', 'True', '(1, None)', '(0, 5)', '(1,)', '()', "'5'"]:
            with self.subTest(value=value):
                self.assertTrue(python_issues(f'import requests\nrequests.get(url, timeout={value})', 'request.py'))
        self.assertEqual([], python_issues('import requests\nrequests.get(url, timeout=(2, 10))', 'ok.py'))

    def test_notebooks_and_syntax(self):
        for code in ['broker.place_order()', '%pip install requests', 'if broken']:
            notebook = {'cells': [{'cell_type': 'code', 'source': [code]}]}
            self.assertTrue(content_issues('scanner.ipynb', json.dumps(notebook)))
        notebook = {'cells': [{'cell_type': 'markdown', 'source': ['broker.place_order()']},
                              {'cell_type': 'code', 'source': 'buy_price = 100'}]}
        self.assertEqual([], content_issues('scanner.ipynb', json.dumps(notebook)))
        self.assertTrue(content_issues('scanner.ipynb', '{'))

    def test_quant_collector_is_also_scanned(self):
        with tempfile.TemporaryDirectory(prefix='leobox guard ') as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            project = root / 'quant-collector'
            project.mkdir()
            (project / 'exit_engine.py').write_text('broker.submit_order()', encoding='utf-8')
            self.assertTrue(check(root)[0])
            subprocess.run(['git', '-C', folder, 'add', '.'], check=True)
            self.assertTrue(check(root, staged=True)[0])

    def test_staged_blob_is_checked_even_if_worktree_is_fixed(self):
        with tempfile.TemporaryDirectory(prefix='leobox guard ') as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            project = root / 'quant-research'
            project.mkdir()
            source = project / '스캐너.py'
            source.write_text('broker.place_order()', encoding='utf-8')
            subprocess.run(['git', '-C', folder, 'add', '.'], check=True)
            source.write_text('buy_price = 100', encoding='utf-8')
            self.assertTrue(check(root, staged=True)[0])
            self.assertEqual([], check(root)[0])
            subprocess.run(['git', '-C', folder, 'add', '.'], check=True)
            self.assertEqual([], check(root, staged=True)[0])
            (project / 'private.key').write_text('fixture, not a key', encoding='utf-8')
            subprocess.run(['git', '-C', folder, 'add', '.'], check=True)
            self.assertTrue(check(root, staged=True)[0])


class LifecycleTests(unittest.TestCase):
    def invoke(self, provider, phase, data, missing_backlog=False):
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
        if missing_backlog:
            env['BACKLOG_FILE'] = str(ROOT / 'tools/ai/missing-backlog-fixture.json')
        result = subprocess.run(['node', str(ROOT / 'tools/ai/lifecycle.cjs'), provider, phase],
                                input=json.dumps(data), text=True, encoding='utf-8',
                                capture_output=True, env=env, timeout=50)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_start_and_success(self):
        output = self.invoke('codex', 'start', {})
        self.assertIn('AGENTS.md', output['hookSpecificOutput']['additionalContext'])
        self.assertEqual({}, self.invoke('codex', 'stop', {'cwd': str(ROOT)}))

    def test_failure_and_loop_limit(self):
        for provider, decision in [('codex', 'block'), ('claude', 'block'), ('gemini', 'deny')]:
            with self.subTest(provider=provider):
                data = {'cwd': str(ROOT / 'quant-research')}
                output = self.invoke(provider, 'stop', data, missing_backlog=True)
                self.assertEqual(decision, output['decision'])
                self.assertTrue(output['reason'])
                output = self.invoke(provider, 'stop', {**data, 'stop_hook_active': True}, missing_backlog=True)
                self.assertNotIn('decision', output)
                self.assertTrue(output['systemMessage'])

    def test_sibling_project_is_not_blocked(self):
        self.assertEqual({}, self.invoke('codex', 'stop', {'cwd': str(ROOT / 'gym-app')}, missing_backlog=True))

    def test_malformed_protocol(self):
        result = subprocess.run(['node', str(ROOT / 'tools/ai/lifecycle.cjs'), 'codex', 'stop'],
                                input='[]', text=True, capture_output=True, timeout=10)
        self.assertEqual(1, result.returncode)
        self.assertIn('error', json.loads(result.stdout)['systemMessage'])

    def test_configured_commands_from_subdirectory(self):
        shells = []
        if os.name == 'nt':
            shells.append([os.environ['COMSPEC'], '/d', '/s', '/c'])
            pwsh = shutil.which('pwsh')
            if pwsh:
                shells.append([pwsh, '-NoProfile', '-Command'])
            git_sh = Path('C:/Program Files/Git/bin/sh.exe')
            if git_sh.exists():
                shells.append([str(git_sh), '-c'])
        else:
            shells.append(['/bin/sh', '-c'])
        for config in ['.codex/hooks.json', '.claude/settings.json', '.gemini/settings.json']:
            settings = json.loads((ROOT / config).read_text(encoding='utf-8'))
            for event, groups in settings['hooks'].items():
                command = groups[0]['hooks'][0]['command']
                for shell in shells:
                    with self.subTest(config=config, event=event, shell=shell[0]):
                        # cmd.exe needs a shell command string, not Python's argv quoting.
                        use_cmd = os.name == 'nt' and shell[0] == os.environ['COMSPEC']
                        result = subprocess.run(command if use_cmd else [*shell, command], shell=use_cmd,
                                                cwd=ROOT / 'quant-research',
                                                input=json.dumps({'cwd': str(ROOT / 'quant-research')}),
                                                capture_output=True, text=True, encoding='utf-8', timeout=50)
                        self.assertEqual(0, result.returncode, result.stderr)
                        output = json.loads(result.stdout)
                        if event == 'SessionStart':
                            self.assertIn('additionalContext', output['hookSpecificOutput'])
                        else:
                            self.assertEqual({}, output)


if __name__ == '__main__':
    unittest.main()
