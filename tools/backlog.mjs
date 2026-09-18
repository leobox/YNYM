#!/usr/bin/env node
/**
 * backlog.mjs — Leobox Multi-root Workspace 백로그 CLI.
 *
 * 대상 파일: 이 스크립트 상위 폴더의 backlog.json (BACKLOG_FILE 환경변수로 변경 가능).
 * 의존성 없음 (순수 Node.js 표준 라이브러리).
 *
 * 원칙
 *   - backlog.json은 손으로 고치지 않고 이 도구로만 바꾼다.
 *   - 모든 쓰기 전에 무결성을 검증하고, 실패 시 파일을 건드리지 않는다.
 *   - task.doc은 항상 docs/tasks/<id>.md다.
 */
import { readFileSync, writeFileSync, existsSync, mkdirSync, renameSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

const FILE = process.env.BACKLOG_FILE
  ? resolve(process.env.BACKLOG_FILE)
  : resolve(dirname(fileURLToPath(import.meta.url)), "..", "backlog.json");
const ROOT = dirname(FILE);

const TERMINAL = new Set(["done", "cancelled"]);
const ACTIVE = ["in_progress", "review", "needs_decision", "blocked", "todo"];
const STATUS_ORDER = ["in_progress", "review", "needs_decision", "blocked", "todo", "done", "cancelled"];
const SHORT = {
  todo: "할일",
  in_progress: "진행",
  review: "리뷰",
  needs_decision: "판단",
  blocked: "대기",
  done: "완료",
  cancelled: "취소"
};
const NEEDS_NOTE = new Set(["blocked", "needs_decision", "cancelled"]);
// update 서브커맨드가 건드릴 수 있는 필드. status/id/log/owner/claimed_at은 여기 없다 —
// status는 set 전용(선행조건 게이트·완료 근거·gate 확인을 강제하기 위함).
const FLAG_MAP = {
  title: "title", summary: "summary", priority: "priority", category: "category",
  phase: "phase", where: "where", note: "note", estimate: "estimate_min",
  deps: "deps", refs: "refs", "done-when": "done_when", gate: "gate", parent: "parent"
};
const EDITABLE = Object.keys(FLAG_MAP);

// ─────────────────────────────── 출력 유틸 ───────────────────────────────

const USE_COLOR = process.stdout.isTTY && !process.env.NO_COLOR;
const C = {
  reset: "\x1b[0m", dim: "\x1b[2m", bold: "\x1b[1m",
  red: "\x1b[31m", green: "\x1b[32m", yellow: "\x1b[33m",
  blue: "\x1b[34m", magenta: "\x1b[35m", cyan: "\x1b[36m",
};
function c(name, s) { return USE_COLOR && C[name] ? C[name] + s + C.reset : String(s); }
const STATUS_COLOR = {
  todo: "reset",
  in_progress: "cyan",
  review: "magenta",
  needs_decision: "yellow",
  blocked: "red",
  done: "green",
  cancelled: "dim"
};

function charWidth(cp) {
  if (cp >= 0x1100 && (
    cp <= 0x115f ||
    cp === 0x2329 || cp === 0x232a ||
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe6f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f300 && cp <= 0x1f64f) ||
    (cp >= 0x1f900 && cp <= 0x1f9ff)
  )) return 2;
  return 1;
}
function width(s) { let n = 0; for (const ch of String(s)) n += charWidth(ch.codePointAt(0)); return n; }
function pad(s, w) { const d = w - width(s); return d > 0 ? s + " ".repeat(d) : String(s); }

// ─────────────────────────────── 데이터 로드 & 쓰기 ───────────────────────────────

function load() {
  if (!existsSync(FILE)) {
    console.error(c("red", `오류: 백로그 파일이 없습니다: ${FILE}`));
    process.exit(1);
  }
  try {
    return JSON.parse(readFileSync(FILE, "utf-8"));
  } catch (e) {
    console.error(c("red", `오류: JSON 파싱 실패 (${FILE}): ${e.message}`));
    process.exit(1);
  }
}

function save(data) {
  const cyclePath = findDependencyCycle(data.tasks);
  if (cyclePath) {
    console.error(c("red", `검증 실패로 저장하지 않았습니다: 순환 의존성 발견`));
    console.error(`  - ${cyclePath.join(" -> ")}`);
    process.exit(1);
  }
  const errs = validate(data);
  if (errs.length > 0) {
    console.error(c("red", `검증 실패로 저장하지 않았습니다 (${errs.length}건):`));
    for (const err of errs) console.error(`  - ${err}`);
    process.exit(1);
  }
  data.meta.updated = new Date().toISOString().slice(0, 10);
  if (existsSync(FILE)) {
    writeFileSync(FILE + ".bak", readFileSync(FILE, "utf-8"), "utf-8");
  }
  const tmp = FILE + ".tmp";
  writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf-8");
  renameSync(tmp, FILE);
}

// deps 그래프에서 순환을 찾는다(DFS 3색 마킹). 순환이 있으면 경로(id 배열)를,
// 없으면 null을 돌려준다. 존재하지 않는 deps 참조는 validate()가 별도로 잡는다.
function findDependencyCycle(tasks) {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const WHITE = 0, GRAY = 1, BLACK = 2;
  const color = new Map(tasks.map((t) => [t.id, WHITE]));
  const stack = [];

  function visit(id) {
    color.set(id, GRAY);
    stack.push(id);
    const task = byId.get(id);
    for (const dep of task?.deps ?? []) {
      if (!byId.has(dep)) continue;
      const cState = color.get(dep);
      if (cState === GRAY) return [...stack.slice(stack.indexOf(dep)), dep];
      if (cState === WHITE) {
        const cyclePath = visit(dep);
        if (cyclePath) return cyclePath;
      }
    }
    stack.pop();
    color.set(id, BLACK);
    return null;
  }

  for (const t of tasks) {
    if (color.get(t.id) === WHITE) {
      const cyclePath = visit(t.id);
      if (cyclePath) return cyclePath;
    }
  }
  return null;
}

// ─────────────────────────────── 검증 ───────────────────────────────

function validate(data) {
  const errs = [];
  if (!data.meta || !data.enums || !data.tasks) {
    return ["meta, enums, tasks 필드가 모두 필요합니다."];
  }
  const idSet = new Set();
  const validStatus = new Set(data.enums.status);
  const validPriority = new Set(data.enums.priority);
  const validCategory = new Set(data.enums.category);
  const validPhases = new Set(data.phases);

  for (let i = 0; i < data.tasks.length; i++) {
    const t = data.tasks[i];
    const prefix = `task[${i}](${t.id || "no-id"})`;
    if (!t.id || !/^T-\d{3,}$/.test(t.id)) errs.push(`${prefix}: id 형식이 올바르지 않습니다 (예: T-001)`);
    if (idSet.has(t.id)) errs.push(`${prefix}: id 중복: ${t.id}`);
    idSet.add(t.id);

    if (!validStatus.has(t.status)) errs.push(`${prefix}: 유효하지 않은 status: ${t.status}`);
    if (!validPriority.has(t.priority)) errs.push(`${prefix}: 유효하지 않은 priority: ${t.priority}`);
    if (!validCategory.has(t.category)) errs.push(`${prefix}: 유효하지 않은 category: ${t.category}`);
    if (t.phase && !validPhases.has(t.phase)) errs.push(`${prefix}: 등록되지 않은 phase: ${t.phase}`);

    const expectedDoc = `docs/tasks/${t.id}.md`;
    if (t.doc !== expectedDoc) errs.push(`${prefix}: doc 경로는 ${expectedDoc}이어야 합니다 (현재: ${t.doc})`);

    if (NEEDS_NOTE.has(t.status) && (!t.note || !t.note.trim())) {
      errs.push(`${prefix}: ${t.status} 상태는 note(사유)가 필수입니다.`);
    }
  }

  for (const t of data.tasks) {
    if (Array.isArray(t.deps)) {
      for (const d of t.deps) {
        if (!idSet.has(d)) errs.push(`${t.id}: 존재하지 않는 선행 작업(deps): ${d}`);
      }
    }
    if (t.parent != null) {
      if (t.parent === t.id) errs.push(`${t.id}: parent가 자기 자신일 수 없습니다.`);
      else if (!idSet.has(t.parent)) errs.push(`${t.id}: 존재하지 않는 parent: ${t.parent}`);
    }
  }

  // 선행조건 게이트: status가 todo가 아니면 deps에 나열된 모든 작업이 done이어야 한다.
  for (const t of data.tasks) {
    if (t.status === "todo" || !Array.isArray(t.deps)) continue;
    for (const d of t.deps) {
      const dep = data.tasks.find((x) => x.id === d);
      if (dep && dep.status !== "done") {
        errs.push(`${t.id}: 선행 작업 "${d}"(status=${dep.status})가 done이 아닌데 ${t.id}의 status가 "${t.status}"입니다.`);
      }
    }
  }

  return errs;
}

// ─────────────────────────────── ID 검색 ───────────────────────────────

function resolveId(data, query) {
  if (!query) return null;
  const raw = String(query).trim();
  const numMatch = raw.match(/^\d+$/);
  if (numMatch) {
    const target = `T-${raw.padStart(3, "0")}`;
    return data.tasks.find(t => t.id === target) || null;
  }
  const upper = raw.toUpperCase();
  const exact = data.tasks.find(t => t.id === upper);
  if (exact) return exact;

  const matches = data.tasks.filter(t => t.title.toLowerCase().includes(raw.toLowerCase()));
  if (matches.length === 1) return matches[0];
  return null;
}

function normalizeStatus(data, s) {
  if (!s) return null;
  const raw = s.trim().toLowerCase();
  const map = {
    "할일": "todo", "todo": "todo",
    "진행": "in_progress", "작업중": "in_progress", "in_progress": "in_progress", "doing": "in_progress",
    "리뷰": "review", "review": "review",
    "판단": "needs_decision", "needs_decision": "needs_decision", "decision": "needs_decision",
    "대기": "blocked", "blocked": "blocked", "block": "blocked",
    "완료": "done", "done": "done",
    "취소": "cancelled", "cancelled": "cancelled", "cancel": "cancelled"
  };
  return map[raw] || null;
}

// ─────────────────────────────── 문서 생성 유틸 ───────────────────────────────

function ensureDoc(task) {
  const docPath = resolve(ROOT, task.doc);
  const docDir = dirname(docPath);
  if (!existsSync(docDir)) mkdirSync(docDir, { recursive: true });
  if (!existsSync(docPath)) {
    const content = `# ${task.id}: ${task.title}

- **상태**: ${task.status} (${SHORT[task.status] || task.status})
- **우선순위**: ${task.priority || "-"}
- **카테고리**: ${task.category}
- **단계**: ${task.phase || "-"}
- **예상 소요**: ${task.estimate_min || 30}분
- **작업 위치**: \`${task.where || "TBD"}\`
- **선행 작업**: ${task.deps && task.deps.length ? task.deps.join(", ") : "없음"}

---

## 📋 요약
${task.summary || "작업 설명"}

---

## 🎯 완료 조건 (DoD)
- [ ] 요구사항 구현 및 검증 완료
- [ ] lint / build 확인
- [ ] 문서 최신화

---

## 📝 작업 기록
- ${new Date().toISOString().slice(0, 10)}: 작업 생성
`;
    writeFileSync(docPath, content, "utf-8");
  }
  return docPath;
}

// ─────────────────────────────── CLI 명령어 ───────────────────────────────

function cmdList(data, args) {
  let showAll = args.includes("--all");
  let showDone = args.includes("--done");
  let jsonOutput = args.includes("--json");

  let tasks = data.tasks;
  if (!showAll && !showDone) {
    tasks = tasks.filter(t => !TERMINAL.has(t.status));
  } else if (showDone) {
    tasks = tasks.filter(t => t.status === "done");
  }

  if (jsonOutput) {
    console.log(JSON.stringify(tasks, null, 2));
    return;
  }

  console.log(`\n${c("bold", "📋 작업 목록")} (총 ${tasks.length}개)\n`);
  console.log(`${pad("ID", 7)} ${pad("상태", 6)} ${pad("우선", 5)} ${pad("단계", 22)} ${pad("카테고리", 16)} 제목`);
  console.log("-".repeat(80));

  for (const t of tasks) {
    const stCol = STATUS_COLOR[t.status] || "reset";
    const stText = pad(SHORT[t.status] || t.status, 6);
    console.log(
      `${pad(t.id, 7)} ${c(stCol, stText)} ${pad(t.priority || "-", 5)} ${pad(t.phase || "-", 22)} ${pad(t.category, 16)} ${t.title}`
    );
  }
  console.log("");
}

function cmdNext(data, args) {
  const doneSet = new Set(data.tasks.filter(t => t.status === "done").map(t => t.id));
  const ready = data.tasks.filter(t => {
    if (t.status !== "todo") return false;
    if (!t.deps || t.deps.length === 0) return true;
    return t.deps.every(d => doneSet.has(d));
  });

  console.log(`\n${c("bold", "🚀 지금 시작할 수 있는 작업 (선행 완료 기준)")} (총 ${ready.length}개)\n`);
  if (ready.length === 0) {
    console.log("  현재 바로 시작 가능한 todo 작업이 없습니다.");
  } else {
    for (const t of ready) {
      console.log(`  [${c("cyan", t.id)}] (${t.priority || "P-"}) ${t.title} [${t.category}]`);
      console.log(`       요약: ${t.summary || "-"}`);
      console.log(`       문서: ${t.doc}`);
    }
  }
  console.log("");
}

function cmdGet(data, idArg) {
  const t = resolveId(data, idArg);
  if (!t) {
    console.error(c("red", `작업을 찾을 수 없습니다: ${idArg}`));
    process.exit(1);
  }
  console.log(`\n${c("bold", `[${t.id}] ${t.title}`)}`);
  console.log(`  상태      : ${c(STATUS_COLOR[t.status], SHORT[t.status] || t.status)} (${t.status})`);
  console.log(`  우선순위  : ${t.priority || "-"}`);
  console.log(`  카테고리  : ${t.category}`);
  console.log(`  단계      : ${t.phase || "-"}`);
  console.log(`  예상 소요 : ${t.estimate_min || 30}분`);
  console.log(`  위치      : ${t.where || "-"}`);
  console.log(`  선행 작업 : ${t.deps && t.deps.length ? t.deps.join(", ") : "없음"}`);
  console.log(`  상세 문서 : ${t.doc}`);
  if (t.summary) console.log(`  요약      : ${t.summary}`);
  if (t.note) console.log(`  비고(사유): ${c("yellow", t.note)}`);
  console.log("");
}

function cmdSet(data, args) {
  const idArg = args[0];
  const stArg = args[1];
  if (!idArg || !stArg) {
    console.error("사용법: node tools/backlog.mjs set <id> <상태> [--note \"사유\"] [--expected-status <현재상태>] [--owner <이름>] [--ack-gate]");
    process.exit(1);
  }
  const t = resolveId(data, idArg);
  if (!t) {
    console.error(c("red", `작업을 찾을 수 없습니다: ${idArg}`));
    process.exit(1);
  }
  const newStatus = normalizeStatus(data, stArg);
  if (!newStatus) {
    console.error(c("red", `유효하지 않은 상태: ${stArg}`));
    process.exit(1);
  }

  // 선택적 동시성 확인: 다른 에이전트가 먼저 상태를 바꿨는데 모르고 덮어쓰는 걸 방지.
  const expIdx = args.indexOf("--expected-status");
  if (expIdx !== -1 && args[expIdx + 1]) {
    const expected = normalizeStatus(data, args[expIdx + 1]);
    if (expected && t.status !== expected) {
      console.error(c("red", `오류: 현재 상태 불일치 — --expected-status "${args[expIdx + 1]}"로 예상했지만 실제 status는 "${t.status}"입니다. 다시 조회한 뒤 재시도하세요.`));
      process.exit(1);
    }
  }

  const noteIdx = args.indexOf("--note");
  if (noteIdx !== -1 && args[noteIdx + 1]) {
    t.note = args[noteIdx + 1];
  }

  if (NEEDS_NOTE.has(newStatus) && (!t.note || !t.note.trim())) {
    console.error(c("red", `오류: '${newStatus}' 상태로 변경 시 --note "사유" 지정이 필수입니다.`));
    process.exit(1);
  }
  if (newStatus === "done" && (noteIdx === -1 || !args[noteIdx + 1] || !args[noteIdx + 1].trim())) {
    console.error(c("red", `오류: 'done' 상태로 변경 시 --note "완료 근거"가 필수입니다.`));
    process.exit(1);
  }
  if (newStatus === "done" && t.gate && !args.includes("--ack-gate")) {
    console.error(c("red", `오류: 이 작업에 gate가 있습니다: ${JSON.stringify(t.gate)}. 사람이 직접 확인한 뒤 --ack-gate로 확인했음을 표시하세요 (gate 문자열은 자동 실행되지 않습니다).`));
    process.exit(1);
  }

  const ownerIdx = args.indexOf("--owner");
  if (ownerIdx !== -1 && args[ownerIdx + 1]) {
    t.owner = args[ownerIdx + 1];
  }

  const oldStatus = t.status;
  t.status = newStatus;
  t.updated_at = new Date().toISOString().slice(0, 10);
  if (newStatus === "in_progress" && !t.claimed_at) {
    t.claimed_at = t.updated_at;
  }
  if (newStatus === "done" || newStatus === "cancelled") {
    t.done_at = t.updated_at;
  } else {
    t.done_at = null;
  }
  t.log = [...(Array.isArray(t.log) ? t.log : []), { at: t.updated_at, status: newStatus, note: t.note || "" }];

  save(data);
  ensureDoc(t);
  console.log(c("green", `✓ [${t.id}] 상태 변경: ${SHORT[oldStatus]} → ${SHORT[newStatus]}`));

  if (newStatus === "done" && process.platform === "win32" && !process.env.CI) {
    try {
      const psScript = join(ROOT, "tools", "notify_voice.ps1");
      if (existsSync(psScript)) {
        const msg = `클로드 작업이 완료 되었습니다.`;
        const child = spawn("pwsh", ["-NoProfile", "-File", psScript, msg], {
          detached: true,
          stdio: "ignore",
          windowsHide: true,
        });
        child.unref();
      }
    } catch {
      // 음성 안내 실패 시 백로그 로직에 영향 없음
    }
  }
}

function cmdAdd(data, args) {
  let title = "";
  let category = "infra";
  let phase = data.phases[0];
  let priority = "P1";
  let summary = "";
  let doneWhen = null;
  let gate = null;
  let parent = null;
  let deps = [];

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--title" && args[i + 1]) title = args[++i];
    if (args[i] === "--category" && args[i + 1]) category = args[++i];
    if (args[i] === "--phase" && args[i + 1]) phase = args[++i];
    if (args[i] === "--priority" && args[i + 1]) priority = args[++i];
    if (args[i] === "--summary" && args[i + 1]) summary = args[++i];
    if (args[i] === "--done-when" && args[i + 1]) doneWhen = args[++i];
    if (args[i] === "--gate" && args[i + 1]) gate = args[++i];
    if (args[i] === "--parent" && args[i + 1]) parent = args[++i];
    if (args[i] === "--deps" && args[i + 1]) deps = args[++i].split(",").map((s) => s.trim()).filter(Boolean);
  }

  if (!title) {
    console.error("오류: --title 은 필수입니다.");
    process.exit(1);
  }
  if (parent && !data.tasks.some((t) => t.id === parent)) {
    console.error(c("red", `오류: --parent "${parent}"가 존재하지 않는 id입니다.`));
    process.exit(1);
  }
  for (const d of deps) {
    if (!data.tasks.some((t) => t.id === d)) {
      console.error(c("red", `오류: --deps의 "${d}"가 존재하지 않는 id입니다.`));
      process.exit(1);
    }
  }

  let maxNum = 0;
  for (const t of data.tasks) {
    const m = t.id.match(/^T-(\d+)$/);
    if (m) {
      const n = parseInt(m[1], 10);
      if (n > maxNum) maxNum = n;
    }
  }
  const nextId = `T-${String(maxNum + 1).padStart(3, "0")}`;

  const now = new Date().toISOString().slice(0, 10);
  const newTask = {
    id: nextId,
    title,
    summary,
    doc: `docs/tasks/${nextId}.md`,
    status: "todo",
    priority,
    category,
    phase,
    estimate_min: 30,
    deps,
    where: null,
    refs: [],
    note: null,
    done_at: null,
    updated_at: now,
    done_when: doneWhen,
    gate,
    parent,
    owner: null,
    claimed_at: null,
    log: [{ at: now, status: "todo", note: "add로 생성" }]
  };

  data.tasks.push(newTask);
  save(data);
  const docPath = ensureDoc(newTask);
  console.log(c("green", `✓ 작업 추가 완료: [${nextId}] ${title}`));
  console.log(`  상세 문서 생성: ${docPath}`);
}

function cmdUpdate(data, args) {
  const idArg = args[0];
  if (!idArg) {
    console.error(`사용법: node tools/backlog.mjs update <id> [--${EDITABLE.join(" V] [--")} V]\n(status는 set으로만 변경합니다.)`);
    process.exit(1);
  }
  const t = resolveId(data, idArg);
  if (!t) {
    console.error(c("red", `작업을 찾을 수 없습니다: ${idArg}`));
    process.exit(1);
  }

  let changed = false;
  for (let i = 0; i < args.length; i++) {
    if (!args[i].startsWith("--")) continue;
    const flag = args[i].slice(2);
    if (!EDITABLE.includes(flag)) continue;
    const value = args[i + 1];
    if (value === undefined) continue;
    i++;
    const field = FLAG_MAP[flag];
    if (flag === "estimate") {
      t[field] = Number(value);
    } else if (flag === "deps" || flag === "refs") {
      t[field] = value.split(",").map((s) => s.trim()).filter(Boolean);
    } else {
      t[field] = value === "null" ? null : value;
    }
    changed = true;
  }

  if (!changed) {
    console.error(c("red", `오류: 변경할 필드가 없습니다. --${EDITABLE.join(", --")} 중 최소 하나를 지정하세요.`));
    process.exit(1);
  }

  t.updated_at = new Date().toISOString().slice(0, 10);
  save(data);
  console.log(c("green", `✓ [${t.id}] 필드 수정 완료`));
}

function cmdStats(data) {
  const total = data.tasks.length;
  const counts = {};
  for (const st of STATUS_ORDER) counts[st] = 0;
  for (const t of data.tasks) counts[t.status] = (counts[t.status] || 0) + 1;

  const doneCount = counts.done || 0;
  const progressPercent = total > 0 ? Math.round((doneCount / total) * 100) : 0;

  console.log(`\n${c("bold", "📊 백로그 진행 현황")}`);
  console.log(`  전체 작업: ${total}개 | 완료: ${doneCount}개 (${progressPercent}%)\n`);
  for (const st of STATUS_ORDER) {
    if (counts[st] > 0) {
      console.log(`  ${c(STATUS_COLOR[st], pad(SHORT[st], 6))}: ${counts[st]}개`);
    }
  }
  console.log("");
}

function cmdCheck(data) {
  const errs = validate(data);
  if (errs.length === 0) {
    console.log(c("green", `✓ 무결성 검증 통과: 총 ${data.tasks.length}개 작업 정상`));
  } else {
    console.error(c("red", `✗ 무결성 검증 오류 (${errs.length}건):`));
    for (const err of errs) console.error(`  - ${err}`);
    process.exit(1);
  }
}

// ─────────────────────────────── 메인 루틴 ───────────────────────────────

const argv = process.argv.slice(2);
const command = argv[0] || "list";
const data = load();

switch (command) {
  case "list":
    cmdList(data, argv.slice(1));
    break;
  case "next":
    cmdNext(data, argv.slice(1));
    break;
  case "get":
  case "show":
    cmdGet(data, argv[1]);
    break;
  case "set":
    cmdSet(data, argv.slice(1));
    break;
  case "add":
    cmdAdd(data, argv.slice(1));
    break;
  case "update":
    cmdUpdate(data, argv.slice(1));
    break;
  case "stats":
    cmdStats(data);
    break;
  case "check":
    cmdCheck(data);
    break;
  case "sync-db":
    try {
      const { execSync } = await import("node:child_process");
      execSync("node tools/sync-postgres.mjs", { stdio: "inherit" });
    } catch (e) {
      console.error("sync-db 실패: " + e.message);
    }
    break;
  case "--help":
  case "-h":
  case "help":
    console.log(`
Leobox 백로그 CLI 사용법:
  node tools/backlog.mjs [명령어]

조회:
  node tools/backlog.mjs                  # 진행 중/대기 작업 목록
  node tools/backlog.mjs list --all       # 전체 작업 목록
  node tools/backlog.mjs list --done      # 완료된 작업 목록
  node tools/backlog.mjs next             # 선행 완료되어 지금 착수 가능한 작업
  node tools/backlog.mjs get <id>         # 특정 작업 상세 보기
  node tools/backlog.mjs stats            # 전체 진행 통계
  node tools/backlog.mjs check            # 무결성 검사
  node tools/backlog.mjs sync-db          # PostgreSQL 데이터베이스 동기화

변경:
  node tools/backlog.mjs set <id> <상태> [--note "사유"] [--expected-status <현재상태>] [--owner <이름>] [--ack-gate]
      상태: todo(할일), in_progress(진행), review(리뷰), needs_decision(판단), blocked(대기), done(완료), cancelled(취소)
      done 전환 시 --note 필수. gate가 있는 작업은 --ack-gate 없이 done으로 못 바꿈.
      --expected-status를 주면 현재 상태와 다를 경우 거부(동시 변경 충돌 방지, 선택 사항).
  node tools/backlog.mjs add --title "제목" --category quant_research --phase "1. 퀀트..."
      [--done-when "완료 조건"] [--gate "사람 확인 필요 조건"] [--parent <id>] [--deps <id>,<id>]
  node tools/backlog.mjs update <id> [--${EDITABLE.join(" V] [--")} V]
      상태 외 필드(제목/요약/우선순위/카테고리/단계/위치/비고/예상소요/선행작업/참조/완료조건/gate/parent)만 수정.
`);
    break;
  default:
    console.error(`알 수 없는 명령어: ${command} (도움말: node tools/backlog.mjs --help)`);
    process.exit(1);
}
