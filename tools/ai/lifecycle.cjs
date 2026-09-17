// Shared, read-only hook adapter for Codex, Claude Code and Gemini CLI.
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const ROOT = path.resolve(__dirname, '../..');

function run(program, args) {
  const result = spawnSync(program, args, {
    cwd: ROOT, encoding: 'utf8', timeout: 20000, windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, maxBuffer: 1024 * 1024,
  });
  return { ok: result.status === 0 && !result.error,
    message: result.error ? result.error.message : (result.stderr || result.stdout || 'check failed').trim() };
}

function respond(provider, phase, input) {
  if (phase === 'start') {
    const tasks = run(process.execPath, ['tools/backlog.mjs', 'list']);
    return { hookSpecificOutput: {
      hookEventName: 'SessionStart',
      additionalContext: [
        'Read AGENTS.md and the target project AGENTS.md. Shared skills: .agents/skills/.',
        'For quant-research use quant-research/설계문서.md and quant-research/docs/rules.md.',
        'Claim the task via backlog CLI before edits; record handoff in docs/tasks/<id>.md.',
        'No real trading APIs. Hook success is not proof of strategy validity.',
        tasks.ok ? tasks.message : `Backlog inspection failed: ${tasks.message}`,
      ].join('\n'),
    } };
  }
  if (phase !== 'stop') throw new Error(`Unknown hook phase: ${phase}`);
  // The worktree is shared; avoid blocking sibling-project work on research checks.
  const relative = path.relative(ROOT, path.resolve(input.cwd || ROOT));
  if (relative && relative.split(path.sep)[0] !== 'quant-research') return {};
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const checks = [run(python, ['tools/ai/quant_guard.py']),
    run(process.execPath, ['tools/backlog.mjs', 'check'])];
  const failures = checks.filter(result => !result.ok);
  if (!failures.length) return {};
  const reason = 'Fix these local checks, or report the specific unresolved limitation.\n'
    + failures.map(result => result.message).join('\n');
  // At most one automatic continuation. Never loop forever or claim a later pass.
  if (input.stop_hook_active) return { systemMessage: reason };
  return { decision: provider === 'gemini' ? 'deny' : 'block', reason };
}

function main(provider, phase) {
  try {
    if (!['codex', 'claude', 'gemini'].includes(provider)) throw new Error('Unknown provider');
    const input = JSON.parse(fs.readFileSync(0, 'utf8').replace(/^\uFEFF/, '') || '{}');
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('Expected hook JSON object');
    process.stdout.write(JSON.stringify(respond(provider, phase, input)) + '\n');
  } catch (error) {
    // Valid protocol output, no accidental log text in stdout.
    process.stdout.write(JSON.stringify({ systemMessage: `Leobox hook error: ${error.message}` }) + '\n');
    process.exitCode = 1;
  }
}
if (require.main === module) main(process.argv[2], process.argv[3]);
module.exports = { respond, main };
