# AGENTS.md — Leobox Multi-root Workspace

이 워크스페이스는 **Gemini(Antigravity), Claude(Claude Code), OpenAI Codex / Copilot** 등 복수의 AI 에이전트가 함께 협업하며 세 가지 독립 프로젝트를 발전시켜 나가는 다중 에이전트 환경입니다.

---

## 📂 프로젝트 구성

1. **`quant-research/`** : 모바일 Colab 주식 패턴 스캐너, 데이터 재현, 전략 백테스팅. 기준은 `quant-research/설계문서.md`와 `quant-research/AGENTS.md`.
2. **`quant-collector/`** : 평일 장중 15분 간격 GitHub Actions 자동 실행 및 모바일 수동 실행 데이터 수집 (주문/매수 절대 금지)
3. **`gym-app/`** : 오프라인 퍼스트 안드로이드 헬스/운동 루틴 기록 앱

---

## 🏆 골든 룰 (Golden Rules)

1. **추측하지 않는다 (Never Guess)**:
   - 파일 구조, 코드 심볼, 스키마, 라이브러리 버전, 실행 결과는 반드시 직접 확인한 뒤 행동한다.
   - 검증되지 않은 가정을 사실로 보고하거나 커밋하지 않는다.
2. **`backlog.json`은 직접 열어 편집하지 않는다**:
   - 조회, 추가, 상태 변경은 반드시 `node tools/backlog.mjs` CLI를 사용한다.
   - 백로그 파일은 모든 에이전트의 단일 진실 공급원(SSOT)이다.
3. **작업 시작 전 `in_progress` 전환 필수**:
   - `node tools/backlog.mjs set <id> in_progress` 명령을 실행하기 전에는 단 한 줄의 코드도 수정하지 않는다.
   - 작업 착수를 명시하지 않고 진행하면 다른 에이전트와 충돌하거나 중복 작업이 발생한다.
4. **절대 안전 원칙 (No Trading Order)**:
   - 퀀트 연구 및 수집기 프로젝트에서는 **실제 거래/매수/매도 주문 API를 절대 작성하거나 호출하지 않는다**.
   - 순수 데이터 수집, 지표 계산, 백테스팅 및 파일 기록에 한정한다.
5. **크로스 모델 협업 존중 (Gemini, Claude, Codex)**:
   - 이전 에이전트가 작성한 코드 및 결정 사항을 임의로 전면 재작성하지 않는다.
   - 작업 인수인계 및 맥락은 `docs/tasks/<id>.md`에 성실하게 기록한다.
   - 판단이 필요한 사항은 `needs_decision` 상태와 `--note "사유"`를 남긴다.
6. **작업 완료 시 모델별 음성 안내 (Voice Notification)**:
   - **제미나이(Antigravity)**: 작업 완료 시 반드시 `pwsh tools/notify_voice.ps1 "제미나이 작업이 종료되었습니다."`를 실행하여 음성으로 알린다.
   - **클로드(Claude Code)**: 작업 완료 시 `pwsh tools/notify_voice.ps1 "클로드 작업이 완료 되었습니다."` 또는 자체 설정에 따라 음성으로 알린다.

---

## 🔄 표준 작업 흐름 (Workflow)

AI별 진입 문서와 공통 훅 사용법은 [docs/ai-setup.md](docs/ai-setup.md)를 참고한다.
하위 프로젝트 작업은 해당 폴더의 `AGENTS.md`를 추가로 읽는다. 스킬 원본은 `.agents/skills/`에 두며 AI별 사본에 규칙을 중복 작성하지 않는다.

```bash
# 1. 지금 착수할 수 있는 작업 확인
node tools/backlog.mjs next

# 2. 작업 상세 내용 및 선행 조건 확인
node tools/backlog.mjs get <id>

# 3. 작업 착수 (코드 수정 전 필수!)
node tools/backlog.mjs set <id> in_progress

# 4. 코드 구현 및 로컬 테스트/검증

# 5. 리뷰 및 검증 후 완료 처리
node tools/backlog.mjs set <id> done
```

---

## 🚦 상태 전이 기준

| 상태 | 의미 | 비고 |
| :--- | :--- | :--- |
| `todo` | 할 일 | 선행 작업 대기 중이거나 아직 착수 전 |
| `in_progress` | 작업 중 | 현재 에이전트가 작업 중 |
| `review` | 리뷰 필요 | 구현 완료 후 다른 모델 또는 사용자의 검증 대기 |
| `needs_decision` | 사람 판단 필요 | 설정값, 계정, 외부 정책 등 사용자 결정 필요 (`--note` 필수) |
| `blocked` | 외부 대기 | 외부 API 이슈 또는 환경 미비로 지연 (`--note` 필수) |
| `done` | 완료 | DoD 충족 및 테스트 통과 |
| `cancelled` | 취소 | 기획 변경으로 폐기 (`--note` 필수) |
