"""Exercise workflow shell bodies offline, without importing signal algorithms."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/early-inception-forward.yml'


def bash_path():
    if os.name == 'nt':
        git = shutil.which('git')
        if git:
            candidate = Path(git).resolve().parents[1] / 'bin/bash.exe'
            if candidate.is_file():
                return str(candidate)
    return shutil.which('bash')


def step_script(name):
    source = WORKFLOW.read_text(encoding='utf-8')
    step = source.split('      - name: ' + name + '\n', 1)[1].split('      - name:', 1)[0]
    body = step.split('        run: |\n', 1)[1]
    return '\n'.join(line[10:] if line.startswith('          ') else line for line in body.splitlines())


@unittest.skipUnless(bash_path(), 'Bash is required to exercise GitHub Actions shell steps')
class DailyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='leobox-daily-workflow-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runs = self.root / 'quant-research/data/research/T-110/runs'
        self.runs.mkdir(parents=True)
        self.sector_runs = self.root / 'quant-research/data/research/T-150/runs'
        self.sector_runs.mkdir(parents=True)

    def run_shell(self, name, prelude, **env):
        script = 'set -euo pipefail\n' + prelude + '\n' + step_script(name)
        return subprocess.run([bash_path(), '--noprofile', '--norc', '-c', script],
                              cwd=self.root, env={**os.environ, 'TEST_PYTHON': sys.executable, **env},
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def test_stock_step_preserves_all_sealed_signals_across_reruns(self):
        # The actual workflow body runs only inside this isolated temporary tree.
        for day in range(1, 29):
            path = self.runs / f'2026-09-{day:02d}.json'
            path.write_text(json.dumps({'run_id': str(day), 'immutable': True}), encoding='utf-8')
        manifest = self.root / 'quant-research/data/research/T-063/fetch_manifest_3y_fixture.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}', encoding='utf-8')
        before = {path.name: path.read_bytes() for path in self.runs.iterdir()}
        prelude = r'''
python() {
  case "$1" in
    quant-research/scripts/fetch_daily_history.py) return 0 ;;
    quant-research/scripts/run_early_inception_forward.py) printf '{"status":"CAPTURED"}\n' ;;
    quant-research/scripts/refresh_forward_status.py) return 0 ;;
    *) "$TEST_PYTHON" "$@" ;;
  esac
}
'''
        for _ in range(2):
            result = self.run_shell('Update Stock 20d Breakout & Market Regime Panel', prelude)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.runs.iterdir()})

    def publish(self, mode):
        for name in ('outcomes.json', 'latest.md'):
            (self.runs.parent / name).write_text('fixture', encoding='utf-8')
        (self.sector_runs.parent / 'latest.md').write_text('sector-fixture', encoding='utf-8')
        prelude = r'''
pulls=0
pushes=0
git() {
  printf '%s\n' "$*" >> git-calls.txt
  case "$1" in
    diff) [ "$TEST_MODE" = 'no_change' ] ;;
    pull)
      pulls=$((pulls + 1))
      [ "$TEST_MODE" != 'pull_fails' ] ;;
    push)
      pushes=$((pushes + 1))
      if [ "$TEST_MODE" = 'push_fails' ]; then return 1; fi
      if [ "$TEST_MODE" = 'retry_success' ] && [ "$pushes" -lt 2 ]; then return 1; fi ;;
    rebase) return 0 ;;
    *) return 0 ;;
  esac
}
sleep() { printf 'sleep %s\n' "$*" >> git-calls.txt; }
'''
        result = self.run_shell('Commit and push README update', prelude, TEST_MODE=mode,
                                GITHUB_REF_NAME='test-branch')
        calls = (self.root / 'git-calls.txt').read_text(encoding='utf-8').splitlines()
        return result, calls

    def test_success_stops_after_one_push_and_stages_evaluation_outputs(self):
        result, calls = self.publish('success')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, sum(c.startswith('push ') for c in calls))
        self.assertIn('push origin HEAD:test-branch', calls)
        self.assertIn('add README.md quant-research/data/research/T-110/runs quant-research/data/research/T-150/runs', calls)
        self.assertIn('add quant-research/data/research/T-110/outcomes.json', calls)
        self.assertIn('add quant-research/data/research/T-110/latest.md', calls)
        self.assertIn('add quant-research/data/research/T-150/latest.md', calls)
        self.assertFalse(any(c.startswith('sleep ') for c in calls))

    def test_sector_v1_and_etf_steps_invoke_expected_cli_flags(self):
        prelude = r'''
python() {
  printf '%s\n' "$*" >> py-calls.txt
}
'''
        res_etf = self.run_shell('Update ETF 20d Breakout Panel', prelude)
        self.assertEqual(0, res_etf.returncode, res_etf.stderr)
        res_sec = self.run_shell('Update Sector RS v1.0 Observation Panel', prelude)
        self.assertEqual(0, res_sec.returncode, res_sec.stderr)
        py_calls = (self.root / 'py-calls.txt').read_text(encoding='utf-8').splitlines()
        self.assertEqual(2, len(py_calls))
        self.assertIn('quant-research/scripts/run_etf_observation.py --out-dir /tmp/etf_observation --readme README.md', py_calls[0])
        self.assertIn('quant-research/scripts/run_sector_v1_observation.py --output quant-research/data/research/T-150 --raw-dir /tmp/sector_v1_raw --readme README.md', py_calls[1])

    def test_exhausted_push_failure_is_nonzero(self):
        result, calls = self.publish('push_fails')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(3, sum(c.startswith('push ') for c in calls))
        self.assertIn('::error::', result.stdout)
        self.assertEqual(2, sum(c.startswith('sleep ') for c in calls))

    def test_failed_rebase_is_aborted_and_never_pushes(self):
        result, calls = self.publish('pull_fails')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(3, sum(c.startswith('pull ') for c in calls))
        self.assertEqual(3, calls.count('rebase --abort'))
        self.assertFalse(any(c.startswith('push ') for c in calls))
        self.assertFalse(any('-X ours' in c for c in calls))

    def test_transient_push_failure_retries_then_succeeds(self):
        result, calls = self.publish('retry_success')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, sum(c.startswith('push ') for c in calls))
        self.assertEqual(1, calls.count('rebase --abort'))

    def test_no_changes_does_not_commit_or_push(self):
        result, calls = self.publish('no_change')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(any(re.match(r'(commit|pull|push) ', c) for c in calls))


if __name__ == '__main__':
    unittest.main()
