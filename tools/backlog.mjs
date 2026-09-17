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
const EDITABLE = ["title", "summary", "priority", "category", "phase", "where", "note", "estimate", "deps", "refs"];

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
  const errs = validate(data);
  if (errs.length > 0) {
    console.error(c("red", `검증 실패로 저장하지 않았습니다 (${errs.length}건):`));
    for (const err of errs) console.error(`  - ${err}`);
    process.exit(1);
  }
  data.meta.updated = new Date().toISOString().slice(0, 10);
  const tmp = FILE + ".tmp";
  writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf-8");
  renameSync(tmp, FILE);
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
    console.error("사용법: node tools/backlog.mjs set <id> <상태> [--note \"사유\"]");
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

  const noteIdx = args.indexOf("--note");
  if (noteIdx !== -1 && args[noteIdx + 1]) {
    t.note = args[noteIdx + 1];
  }

  if (NEEDS_NOTE.has(newStatus) && (!t.note || !t.note.trim())) {
    console.error(c("red", `오류: '${newStatus}' 상태로 변경 시 --note "사유" 지정이 필수입니다.`));
    process.exit(1);
  }

  const oldStatus = t.status;
  t.status = newStatus;
  t.updated_at = new Date().toISOString().slice(0, 10);
  if (newStatus === "done" || newStatus === "cancelled") {
    t.done_at = t.updated_at;
  } else {
    t.done_at = null;
  }

  save(data);
  ensureDoc(t);
  console.log(c("green", `✓ [${t.id}] 상태 변경: ${SHORT[oldStatus]} → ${SHORT[newStatus]}`));
}

function cmdAdd(data, args) {
  let title = "";
  let category = "infra";
  let phase = data.phases[0];
  let priority = "P1";
  let summary = "";

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--title" && args[i + 1]) title = args[++i];
    if (args[i] === "--category" && args[i + 1]) category = args[++i];
    if (args[i] === "--phase" && args[i + 1]) phase = args[++i];
    if (args[i] === "--priority" && args[i + 1]) priority = args[++i];
    if (args[i] === "--summary" && args[i + 1]) summary = args[++i];
  }

  if (!title) {
    console.error("오류: --title 은 필수입니다.");
    process.exit(1);
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
    deps: [],
    where: null,
    refs: [],
    note: null,
    done_at: null,
    updated_at: new Date().toISOString().slice(0, 10)
  };

  data.tasks.push(newTask);
  save(data);
  const docPath = ensureDoc(newTask);
  console.log(c("green", `✓ 작업 추가 완료: [${nextId}] ${title}`));
  console.log(`  상세 문서 생성: ${docPath}`);
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
  node tools/backlog.mjs set <id> <상태> [--note "사유"]
      상태: todo(할일), in_progress(진행), review(리뷰), needs_decision(판단), blocked(대기), done(완료), cancelled(취소)
  node tools/backlog.mjs add --title "제목" --category quant_research --phase "1. 퀀트..."
`);
    break;
  default:
    console.error(`알 수 없는 명령어: ${command} (도움말: node tools/backlog.mjs --help)`);
    process.exit(1);
}
