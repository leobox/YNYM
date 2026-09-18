// Node 구문 검사(node --check). 이 저장소는 Node 스크립트에 대한 빌드 단계가
// 없어(bundler 없음) 이게 그 대신이다. Stop 단계에서 세 provider(Claude/Codex/
// Gemini) 공통으로 실행된다 — 특정 provider 전용 훅에 넣지 않는 이유는
// tools/ai/lifecycle.cjs 상단 주석 참고.
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..', '..');
const EXT = new Set(['.js', '.cjs', '.mjs']);
const EXCLUDE_DIRS = new Set([
  'node_modules', '.git', '__pycache__', 'data',
  'android-sdk', 'test-output', 'build',
  '.venv', 'venv', 'site-packages'
]);
const PER_FILE_TIMEOUT_MS = 10000;

function collectSourceFiles(dir) {
  const out = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (EXCLUDE_DIRS.has(entry.name) || entry.name.startsWith('test-output-')) continue;
    const full = path.join(dir, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      out.push(...collectSourceFiles(full));
    } else if (EXT.has(path.extname(entry.name))) {
      out.push(full);
    }
  }
  return out;
}

function main() {
  const targets = collectSourceFiles(ROOT);
  const failures = [];

  for (const file of targets) {
    const rel = path.relative(ROOT, file).split(path.sep).join('/');
    const res = spawnSync(process.execPath, ['--check', file], {
      timeout: PER_FILE_TIMEOUT_MS,
      encoding: 'utf8'
    });
    if (res.error && res.error.code === 'ETIMEDOUT') {
      failures.push(`${rel}: TIMEOUT(${PER_FILE_TIMEOUT_MS}ms 내 끝나지 않음) — 통과로 취급하지 않음`);
    } else if (res.status !== 0) {
      const firstLine = (res.stderr || res.error?.message || '알 수 없는 오류').trim().split('\n')[0];
      failures.push(`${rel}: ${firstLine}`);
    }
  }

  if (failures.length) {
    console.error('구문 검증(node --check) 실패:');
    failures.forEach((f) => console.error(`  - ${f}`));
    process.exit(1);
  }

  console.log(`구문 검증 통과 (대상 ${targets.length}개 파일)`);
  process.exit(0);
}

main();
