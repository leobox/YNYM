---
name: backlog-manager
description: Manage and query the project task backlog using the CLI (node tools/backlog.mjs). Use when checking what to do next, claiming a task (in_progress), completing a task (done), or recording blockers and user decisions.
---

# Backlog Manager Skill

Leobox 워크스페이스의 `backlog.json`은 직접 수정하지 않고, 항상 `node tools/backlog.mjs` CLI를 통해 조작합니다.

## 핵심 사용 워크플로

### 1. 작업 탐색 및 시작
```bash
# 지금 선행 조건이 만족되어 시작 가능한 작업 조회
node tools/backlog.mjs next

# 작업 상세 스펙 및 문서 확인
node tools/backlog.mjs get <id>

# 작업 착수 (코드 수정 전 필수 실행!)
node tools/backlog.mjs set <id> in_progress
```

### 2. 상태 변경 규칙
- 작업 완료 시:
  ```bash
  node tools/backlog.mjs set <id> done
  ```
- 사용자 판단 필요 시 (`needs_decision`은 반드시 `--note` 필요):
  ```bash
  node tools/backlog.mjs set <id> needs_decision --note "사용자 확인이 필요한 구체적 내용"
  ```
- 외부 대기/종속성 문제 시:
  ```bash
  node tools/backlog.mjs set <id> blocked --note "차단 사유"
  ```

### 3. 신규 작업 추가
```bash
node tools/backlog.mjs add --title "신규 기능 제목" --category quant_research --phase "1. 퀀트..." --priority P1
```

### 4. 무결성 검사
```bash
node tools/backlog.mjs check
```
