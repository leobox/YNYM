## ⏱️ Quant Collector 모바일 운영 센터 (15분 수집 · 장마감 모드 2)

> GitHub Actions의 60분봉 수집은 평일 장중 **15분 간격**, 모드 2 일봉 패널은 **16:17 KST**에 별도 갱신을 시도합니다. 각 패널의 데이터 기준일과 확인 시각을 따로 확인하세요. 모드 2는 가상 관찰·검토표이며 실제 주문이나 검증된 실전 수익률이 아닙니다. 기존 LRM-60 v2.0과 조기 돌파·청산 표는 별도 전략입니다. 상세 내용은 [`quant-collector/README.md`](quant-collector/README.md)를 참고하세요.

<!-- MODE2_DAILY:START -->

### 🧮 모드 2 · 일봉 팩터 관찰 (가상)

> 일봉 기준 `2026-09-23` · 확인 `2026-09-25 14:38 KST` · 유효 종목 `138/150` · Breadth(SMA60 위) `53.6%` · **새 일봉 전까지 판단 보류**

> 팩터: 60일 모멘텀(최근 5일 제외) 30% · 변동성 조정 모멘텀 40% · CMF20 30%. 현재 시총 상위 종목군 기준이며 과거 3년 성과를 재현한 표본은 아닙니다.

> ⚠️ 오늘 날짜의 새 완료 일봉이 없습니다(휴장 또는 공급 지연). 위에 표시된 과거 거래일 기준값만 관측하고 새 계획은 만들지 않았습니다.

**완료 일봉 팩터 상위 5종목 · 관찰 순위**

| 순위 | 종목(코드) | 종가 | 종합 순위점수 |
|---:|:---|---:|---:|
| 1 | 코스맥스 (192820) | 272,000원 | 0.936 |
| 2 | 한미사이언스 (008930) | 52,100원 | 0.858 |
| 3 | JB금융지주 (175330) | 30,150원 | 0.788 |
| 4 | 한국콜마 (161890) | 147,000원 | 0.782 |
| 5 | 현대해상 (001450) | 47,550원 | 0.752 |

> -15% 에어백 관찰: 가상 보유 기록 0건 · 검사 대상 없음.

> 월간 검토표: 완료된 최신 일봉과 충분한 팩터 후보가 확보될 때 생성합니다.

> 실거래 주문은 생성·전송하지 않습니다. 수익률은 후속 기간 검증 전까지 미확정입니다.
<!-- MODE2_DAILY:END -->

<!-- QUANT_DASHBOARD:START -->

> ⏱️ **수집 시각**: `2026-09-24 23:02 KST (15분 예약 주기)` | 📊 **감시 유니버스**: `350종목`

### 🧭 LRM-60 v2.0 · 4대 게이트 / 3슬롯

> **기준 완료봉:** `09-23 14:00 KST` · **4대 게이트 통과:** `0건` · **다음 봉 시가 대기:** `0건` · **가상 보유:** `0/3` · **가상 순자산:** `1,000,000원`

> 게이트별 통과: 정배열 `91` · 직전 20봉 돌파 `3` · 10억/2.5배 `18` · CLI/D_base `26` (평가 가능 `350`종목, 봉 결측 `0`종목)

> ⚠️ 마지막 세션의 **15:00~15:30 완료봉이 공급되지 않아** 14:00 봉까지만 평가했습니다. 종가를 현재가로 복원하거나 15시 신호를 추정하지 않습니다.

> 이번 기준봉에서 4대 게이트를 모두 통과한 종목이 없습니다.

> 신호는 완료봉 기준 관측값입니다. 체결·순자산은 가상 계산이며 실제 주문이나 수익 보장이 아닙니다. 15:00 봉 누락·무거래·데이터 지연은 별도 확인하세요.

---

### 🎯 최근 신규 포착 종목 (2026-09-23 스마트 랭킹 순위)

> 가장 최근 거래일(`2026-09-23`)에 신규 포착된 종목들을 스마트 랭킹 점수순으로 정렬한 진입 추천 순위입니다. 장 시작 전/장 마감 후에도 1~2위를 쉽게 판단할 수 있습니다.

| 순위 | 종목(코드) | 현재가(수익률) | 돌파이격 (판정) | 거래대금 · VR | 신호시각 | 스마트점수 |
|:---:|:---|:---:|:---:|:---:|:---:|:---:|
| 🥇 1위 | **솔브레인홀딩스** (036830) | 41,500 (🔵-0.12%) | +2.97% (안착🟢) | 1.2억 (1.6x) | 13:00 | **92.4점** |

---

<!-- QUANT_DASHBOARD:END -->

---

# 📦 Leobox Multi-root Workspace

Gemini(Antigravity), Claude(Claude Code), OpenAI Codex / Copilot이 유기적으로 협업하는 멀티 루트 워크스페이스입니다.

---

## 📁 구성 서브 프로젝트

| 폴더 | 표시 이름 | 용도 및 규칙 |
| :--- | :--- | :--- |
| [`quant-research/`](file:///D:/leobox/quant-research/README.md) | **📈 Quant Research** | 퀀트 알고리즘 연구, 시계열 분석, 백테스팅 ([`AGENTS.md`](file:///D:/leobox/quant-research/AGENTS.md)) |
| [`quant-collector/`](file:///D:/leobox/quant-collector/README.md) | **⏱️ Quant Collector** | 15분 간격 GitHub Actions 자동 수집 및 모바일 수동 실행 ([`AGENTS.md`](file:///D:/leobox/quant-collector/AGENTS.md)) |
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
