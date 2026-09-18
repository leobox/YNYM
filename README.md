# 📦 Leobox Multi-root Workspace

Gemini(Antigravity), Claude(Claude Code), OpenAI Codex / Copilot이 유기적으로 협업하는 멀티 루트 워크스페이스입니다.

---

## ⏱️ Quant Collector 실시간 현황

> `quant-collector`가 한국 정규장 중 30분 주기로 자동 갱신합니다. 전체 이력·누적 통계는 [`quant-collector/README.md`](quant-collector/README.md)를 참고하세요.

<!-- QUANT_DASHBOARD:START -->

> **최근 스캔**: `2026-09-18 15:48 KST` | **유니버스**: `350종목` | **조건 충족**: `0건` | **관찰**: `2건` | **추적 중**: `17건`

### 🧪 [실험] 거래대금 이상탐지 + 3봉 연속 가속 (검증 전 · 독자 설계)

> 최근 3개 완료봉이 모두 양봉(Close > Open) 및 연속 종가 상승(그 구간 자체 거래량이 직전 20봉 평균보다 커야 함) + 당일 종가가 전일 종가보다 높음(당일 전체 순매수 우위) + 당일 거래대금이 그 종목 자신의 최근 20거래일 평균 대비 z-score 2.0 이상인 이상치 + 일봉 SMA20 대비 +15% 이내(추격 방지)를 모두 만족하는 종목만 잡습니다. 두산밥캣 사례(갭하락 거래량과 음봉 섞인 약한 데드캣 바운스 오탐)에서 드러난 결함을 보강했습니다. [T-033] 과거 60일 워크포워드 검증(83건) 결과 기본 +10%/-5%/5일 기준보다 **+10%/-3%/5일 기준이 평균수익률이 더 나아(+0.45%) 아래 통계는 그 기준으로 집계합니다** (아직 통계적으로 유의하진 않아 계속 관찰 필요). **가상 매수이며 아래 매도 알림·누적 통계에는 포함되지 않습니다.**

| 상태 | 종목(코드) | 현재가(수익률) | 손절가 | 경과 |
|:---:|:---|:---:|:---:|:---:|
| 🆕 신규 | **메지온** (140410) | 75,100 (🔵-0.40%) | 71,630 | 0일차 |
| 🟢 HOLD | **기가비스** (420770) | 113,100 (🔴+0.00%) | 107,445 | 0일차 |

- **완료된 평가 표본 수**: `0건`
- **TARGET_FIRST (익절 선접촉)**: `0건`
- **STOP_FIRST (손절 선접촉)**: `0건`
- **TIMEOUT (만기 종료)**: `0건`
- **익절 성공률 (Win Rate)**: 데이터 축적 중

---

### 🧪 [실험] 매물대 돌파 + 거래량 급증 + RSI 과매도 반등 (검증 전 · 독자 설계)

> 직전 120봉(60분봉 기준 약 최근 20거래일)의 거래량 밀집구간(매물대) 상단을 이번 봉에 새로 돌파 + 돌파봉 거래량이 직전 20봉 평균 대비 2.5배 이상 + RSI(14)가 최근 10봉 내 과매도권(35 이하)을 찍은 뒤 이번 봉에 Signal(6)선을 상향 돌파를 모두 만족하는 종목만 잡습니다. 거래대금 z-score 실험을 대체하지 않고 별도로 병행 추적합니다. ⚠️ **[T-033] 과거 60일 워크포워드 검증(77건)에서 목표/손절/기간 16개 조합 전부 평균수익률이 마이너스였습니다** (최선이 -0.30%). 진입 조건 재설계 또는 폐기 검토 필요 — 사용자 결정 대기 중. **가상 매수이며 아래 매도 알림·누적 통계에는 포함되지 않습니다.**

*현재 추적 중인 실험 신호가 없습니다*

- **완료된 평가 표본 수**: `0건`
- **TARGET_FIRST (익절 선접촉)**: `0건`
- **STOP_FIRST (손절 선접촉)**: `0건`
- **TIMEOUT (만기 종료)**: `0건`
- **익절 성공률 (Win Rate)**: 데이터 축적 중

---

### 🚨 [긴급] 실시간 매도·청산 권고 신호 발생!

> 청산 조건(익절/손절/돌파선붕괴)이 감지되었습니다. 상세 사유는 아래 표를 확인 후 MTS에서 대응하세요.

- **🔴 손절 · 한화엔진** (082740) 51,300원 (🔵-3.57%)
- **🔴 손절 · 케이씨텍** (281820) 72,700원 (🔵-0.82%)
- **🔴 익절 · RFHIC** (218410) 60,100원 (🔴+3.44%)
- **🔴 손절 · 롯데에너지머티리얼즈** (020150) 42,300원 (🔵-2.31%)

---

### 📊 추적 중인 신호 현황 (가상 매수 100만원 가정)

> 조건 충족·관찰 등록된 모든 신호를 한 표로 모아 긴급도순(매도 > 재확인 > 주의 > 신규 > 보유)으로 정렬했습니다. 🔥는 돌파 당시 거래대금이 평소 대비 15배 이상 폭증한 과열 진입이니 참고만 하세요(확정 표본 쌓이기 전이라 제외는 안 함).

| 상태 | 종목(코드) | 현재가(수익률) | 손절가 | 경과 |
|:---:|:---|:---:|:---:|:---:|
| 🔴 SELL | **RFHIC** (218410) | 60,100 (🔴+3.44%) | 55,195 | 1일차 |
| 🔴 SELL | **케이씨텍** (281820) | 72,700 (🔵-0.82%) | 69,635 | 2일차 |
| 🔴 SELL | **롯데에너지머티리얼즈** (020150) | 42,300 (🔵-2.31%) | 41,135 | 1일차 |
| 🔴 SELL | **한화엔진** (082740) | 51,300 (🔵-3.57%) | 50,540 | 2일차 |
| 🟠 RECHECK | **제이앤티씨** (204270) | 23,550 (🔴+0.43%) | 22,278 | 1일차 |
| 🟠 RECHECK | **에스티팜** (237690) | 92,700 (🔵-0.96%) | 88,920 | 0일차 |
| 🟡 CAUTION | **한화비전** (489790) | 47,950 (🔵-0.42%) | 45,742 | 0일차 |
| 🟡 CAUTION | **메지온** (140410) | 75,100 (🔵-0.40%) | 71,630 | 0일차 |
| 🟢 HOLD | **LS에코에너지** (229640) | 60,500 (🔵-0.49%) | 57,760 | 1일차 |
| 🟢 HOLD | **일진전기** (103590) | 76,600 (🔴+0.52%) | 72,390 | 1일차 |
| 🟢 HOLD | **기가비스** (420770) | 113,100 (🔴+3.86%) | 103,455 | 1일차 |
| 🟢 HOLD | **휴림로봇** (090710) | 6,450 (🔴+0.78%) | 6,080 | 1일차 |
| 🟢 HOLD | **유진로봇** (056080) | 12,500 (🔵-1.96%) | 12,112 | 1일차 |
| 🟢 HOLD | **달바글로벌** (483650) | 180,300 (🔵-0.39%) | 171,950 | 1일차 |
| 🟢 HOLD | **한국피아이엠** (448900) | 56,700 (🔵-1.90%) | 54,910 | 1일차 |
| 🟢 HOLD | **LIG디펜스앤에어로스페이스** (079550) | 744,000 (🔵-1.06%) | 714,400 | 1일차 |
| 🟢 HOLD | **피노** (033790) | 8,000 (🔵-2.20%) | 7,771 | 1일차 |

---

### 📈 누적 전진 검증 성과 (+10% 익절 vs -5% 손절 / 5일 기준)

- **완료된 평가 표본 수**: `0건` (목표: 독립 표본 300건 이상)
- **TARGET_FIRST (익절 선접촉)**: `0건`
- **STOP_FIRST (손절 선접촉)**: `0건`
- **TIMEOUT (만기 종료)**: `0건`
- **익절 성공률 (Win Rate)**: 데이터 축적 중

<!-- QUANT_DASHBOARD:END -->

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
