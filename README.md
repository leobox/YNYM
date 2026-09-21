## ⏱️ Quant Collector 실시간 운영 현황 (+5.35% 익절 전략)

> `quant-collector`가 한국 정규장 중 30분 주기로 자동 갱신합니다. 전체 이력·누적 통계는 [`quant-collector/README.md`](quant-collector/README.md)를 참고하세요.

<!-- QUANT_DASHBOARD:START -->

> **최근 스캔**: `2026-09-21 21:55 KST` | **유니버스**: `350종목` | **조건 충족**: `0건` | **관찰**: `0건` | **추적 중**: `16건`

### 🚨 [긴급] 실시간 매도·청산 권고 신호 발생!

> 청산 조건(익절/손절/돌파선붕괴)이 감지되었습니다. 상세 사유는 아래 표를 확인 후 MTS에서 대응하세요.

- **🔴 익절 · LS에코에너지** (229640) 61,300원 (🔴+0.82%)
- **🔴 익절 · 기가비스** (420770) 111,400원 (🔴+2.30%)
- **🔴 손절 · 롯데에너지머티리얼즈** (020150) 41,700원 (🔵-3.70%)
- **🔴 익절 · LIG디펜스앤에어로스페이스** (079550) 733,000원 (🔵-2.53%)
- **🔴 손절 · 한화비전** (489790) 47,100원 (🔵-2.18%)
- **🔴 손절 · 에스티팜** (237690) 91,100원 (🔵-2.67%)

---

### 📊 추적 중인 신호 현황 (가상 매수 100만원 가정)

> 조건 충족·관찰 등록된 모든 신호를 한 표로 모아 긴급도순(매도 > 재확인 > 주의 > 신규 > 보유)으로 정렬했습니다. 🔥는 돌파 당시 거래대금이 평소 대비 15배 이상 폭증한 과열 진입이니 참고만 하세요(확정 표본 쌓이기 전이라 제외는 안 함).

| 상태 | 종목(코드) | 현재가(수익률) | 손절가 | 경과 |
|:---:|:---|:---:|:---:|:---:|
| 🔴 SELL | **기가비스** (420770) | 111,400 (🔴+2.30%) | 103,455 | 2일차 |
| 🔴 SELL | **LS에코에너지** (229640) | 61,300 (🔴+0.82%) | 57,760 | 2일차 |
| 🔴 SELL | **한화비전** (489790) | 47,100 (🔵-2.18%) | 45,742 | 1일차 |
| 🔴 SELL | **LIG디펜스앤에어로스페이스** (079550) | 733,000 (🔵-2.53%) | 714,400 | 2일차 |
| 🔴 SELL | **에스티팜** (237690) | 91,100 (🔵-2.67%) | 88,920 | 1일차 |
| 🔴 SELL | **롯데에너지머티리얼즈** (020150) | 41,700 (🔵-3.70%) | 41,135 | 2일차 |
| 🟡 CAUTION | **삼성물산** (028260) | 368,500 (🔴+0.41%) | 348,650 | 1일차 |
| 🟢 HOLD | **케이씨텍** (281820) | 79,100 (🔴+7.91%) | 69,635 | 3일차 |
| 🟢 HOLD | **일진전기** (103590) | 73,900 (🔵-3.02%) | 72,390 | 2일차 |
| 🟢 HOLD | **휴림로봇** (090710) | 6,570 (🔴+2.66%) | 6,080 | 2일차 |
| 🟢 HOLD | **달바글로벌** (483650) | 183,500 (🔴+1.38%) | 171,950 | 2일차 |
| 🟢 HOLD | **메지온** (140410) | 79,300 (🔴+5.17%) | 71,630 | 1일차 |
| 🟢 HOLD | **피에스케이** (319660) | 138,300 (🔴+3.75%) | 126,635 | 1일차 |
| 🟢 HOLD | **테크윙** (089030) | 48,300 (🔴+1.47%) | 45,220 | 1일차 |
| 🟢 HOLD | **네패스** (033640) | 27,700 (🔴+1.65%) | 25,888 | 1일차 |
| 🟢 HOLD | **동진쎄미켐** (005290) | 42,700 (🔴+0.59%) | 40,328 | 1일차 |

---

### 📈 누적 전진 검증 성과 (+5.35% 익절 vs -5% 손절 / 5일 기준)

- **완료된 평가 표본 수**: `6건` (목표: 독립 표본 300건 이상)
- **TARGET_FIRST (익절 선접촉)**: `2건`
- **STOP_FIRST (손절 선접촉)**: `4건`
- **TIMEOUT (만기 종료)**: `0건`
- **익절 성공률 (Win Rate)**: **33.3%**

<!-- QUANT_DASHBOARD:END -->

---

# 📦 Leobox Multi-root Workspace

Gemini(Antigravity), Claude(Claude Code), OpenAI Codex / Copilot이 유기적으로 협업하는 멀티 루트 워크스페이스입니다.

---

## 📁 구성 서브 프로젝트

| 폴더 | 표시 이름 | 용도 및 규칙 |
| :--- | :--- | :--- |
| [`quant-research/`](file:///D:/leobox/quant-research/README.md) | **📈 Quant Research** | 퀀트 알고리즘 연구, 시계열 분석, 백테스팅 ([`AGENTS.md`](file:///D:/leobox/quant-research/AGENTS.md)) |
| [`quant-collector/`](file:///D:/leobox/quant-collector/README.md) | **⏱️ Quant Collector** | 1시간 주기 GitHub Actions 자동 수집 및 모바일 수동 실행 ([`AGENTS.md`](file:///D:/leobox/quant-collector/AGENTS.md)) |
| [`gym-app/`](file:///D:/leobox/gym-app/README.md) | **🏋️ Gym App** | 오프라인 퍼스트 안드로이드 헬스 기록 앱 ([`AGENTS.md`](file:///D:/leobox/gym-app/AGENTS.md)) |

---

## 🤖 멀티 에이전트 협업 체계 (Gemini + Claude + Codex)

- **공통 골든 룰**: [`AGENTS.md`](file:///D:/leobox/AGENTS.md)
- **Claude Code 엔트리**: [`CLAUDE.md`](file:///D:/leobox/CLAUDE.md)
- **Antigravity(Gemini) 엔트리**: [`GEMINI.md`](file:///D:/leobox/GEMINI.md)
- **전문 스킬 (Skills)**:
  - [`backlog-manager`](file:///D:/leobox/.agents/skills/backlog-manager/SKILL.md) : 백로그 조회 및 상태 변경 CLI 제어
  - [`adversarial-reviewer`](file:///D:/leobox/.agents/skills/adversarial-reviewer/SKILL.md) : 완료 전 적대적 코드 결함 감사
  - [`quant-backtest-validator`](file:///D:/leobox/.agents/skills/quant-backtest-validator/SKILL.md) : 미래 데이터 누수(Lookahead Bias) 및 과적합 검증
  - [`collector-safety-check`](file:///D:/leobox/.agents/skills/collector-safety-check/SKILL.md) : 자동 매수/주문 로직 차단 및 멱등성 검사
  - [`offline-first-reviewer`](file:///D:/leobox/.agents/skills/offline-first-reviewer/SKILL.md) : 오프라인 저장 및 운동 UI 사용성 검증

---

## 📋 백로그 관리 (CLI 기반 SSOT)

`backlog.json`은 직접 수정하지 않고 반드시 CLI 도구를 사용합니다:

```bash
# 1. 지금 바로 착수 가능한 작업 확인
node tools/backlog.mjs next

# 2. 작업 착수 (코드 수정 전 필수!)
node tools/backlog.mjs set <id> in_progress

# 3. 작업 완료 처리
node tools/backlog.mjs set <id> done

# 4. 판단/대기 상태 기록 (사유 필수)
node tools/backlog.mjs set <id> needs_decision --note "결정 필요 사유"

# 5. 작업 통계 및 무결성 검증
node tools/backlog.mjs stats
node tools/backlog.mjs check
```

---

## 📊 PostgreSQL & Grafana 모니터링 대시보드

백로그 현황을 시각화하고 진행 이력을 추적할 수 있는 하이브리드 대시보드 환경입니다.

### 1. 인프라 실행 (Docker Desktop 실행 후)
```bash
cd infra/monitoring
docker compose up -d
```
- **Grafana 웹 접속**: `http://localhost:3000` (ID: `admin` / PW: `admin`)
- 사전 구성된 **Leobox Multi-root Backlog Overview** 대시보드가 자동 로드됩니다.

### 2. 백로그 데이터베이스 동기화
```bash
# 백로그 변경 후 DB에 즉시 반영
node tools/backlog.mjs sync-db
```

---

## 🚀 멀티 루트 워크스페이스 열기

1. **Antigravity IDE / VS Code 실행**
2. **`File (파일)`** → **`Open Workspace from File... (파일에서 작업 영역 열기...)`** 클릭
3. [`leobox.code-workspace`](file:///D:/leobox/leobox.code-workspace) 선택
