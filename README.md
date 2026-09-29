## 📈 모드 2 · 상승 초입 전진 관찰

> [병렬 전진 관찰 Actions](.github/workflows/early-inception-forward.yml)은 평일 17:40 KST에 완료 일봉을 확인합니다. 매일 감시 종목군을 갱신하고, 모드 2와 상승 초입을 각각 최대 5종목씩 관찰합니다. 점수는 상대 순위이며 실제 매수 추천이나 체결 가격이 아닙니다.

<!-- DUAL_FORWARD:START -->
### 모드 2 · 상승 초입 병렬 전진 관찰

- 완료 일봉: 2026-09-28
- 데이터 수신: 2026-09-29T01:59:33.678414+09:00
- 자격 평가: 140/150종목
- 시장 폭: 79.3% (40% 미만이면 두 전략 모두 후보 없음)
- 모드 2 5종목 · 상승 초입 2종목
- 후속 평가 완료: 0건 · 대기: 7건

#### 모드 2 · 2026-09-28 종가

| 순위 | 종목 | 평가일 종가 | 상대점수 | 수치 근거 |
|---:|:---|---:|---:|:---|
| 1 | 코스맥스 (`192820`) | 271,000원 | 0.905 | 60→5거래일 +65.2% · 위험조정 1.06 · CMF20 +0.10 |
| 2 | 현대해상 (`001450`) | 48,950원 | 0.832 | 60→5거래일 +41.9% · 위험조정 0.77 · CMF20 +0.08 |
| 3 | 코스메카코리아 (`241710`) | 125,800원 | 0.786 | 60→5거래일 +63.0% · 위험조정 0.72 · CMF20 +0.02 |
| 4 | 뉴파워프라즈마 (`144960`) | 12,610원 | 0.745 | 60→5거래일 +26.6% · 위험조정 0.22 · CMF20 +0.20 |
| 5 | 지엔씨에너지 (`119850`) | 55,300원 | 0.727 | 60→5거래일 +94.6% · 위험조정 0.69 · CMF20 -0.16 |

#### 상승 초입 · 2026-09-28 종가

| 순위 | 종목 | 평가일 종가 | 상대점수 | 수치 근거 |
|---:|:---|---:|---:|:---|
| 1 | 카페24 (`042000`) | 18,780원 | 0.900 | 20일 변동폭 19.4% · 5일 1.91x · CMF20 +0.26 |
| 2 | 씨어스 (`458870`) | 27,850원 | 0.600 | 20일 변동폭 24.1% · 5일 1.40x · CMF20 +0.14 |

완료 일봉을 이용한 연구 관찰입니다. 실제 주문·매수 추천이나 체결 가격이 아닙니다.
후속 성과는 다음 관측 봉 시가 가상 진입 후 20거래일이 채워진 신호만 집계합니다.
<!-- DUAL_FORWARD:END -->
<!-- QUANT_DASHBOARD:START -->

> ⏱️ **수집 시각**: `2026-09-29 20:35 KST (15분 예약 주기)` | 📊 **감시 유니버스**: `350종목`

### 🧭 LRM-60 v2.0 · 4대 게이트 / 3슬롯

> **기준 완료봉:** `09-29 14:00 KST` · **4대 게이트 통과:** `0건` · **다음 봉 시가 대기:** `0건` · **가상 보유:** `2/3` · **가상 순자산:** `986,552원`

> 게이트별 통과: 정배열 `88` · 직전 20봉 돌파 `14` · 10억/2.5배 `26` · CLI/D_base `48` (평가 가능 `350`종목, 봉 결측 `0`종목)

> ⚠️ 마지막 세션의 **15:00~15:30 완료봉이 공급되지 않아** 14:00 봉까지만 평가했습니다. 종가를 현재가로 복원하거나 15시 신호를 추정하지 않습니다.

> 이번 기준봉에서 4대 게이트를 모두 통과한 종목이 없습니다.

**가상 보유 슬롯**

| 종목 | 가상 진입가 | 구조 손절선 | 목표가 | 보유 완료봉 |
|:---|---:|---:|---:|---:|
| GST (083450) | 51,377원 | 49,995원 | 55,487원 | 2 |
| 샘씨엔에스 (252990) | 19,279원 | 18,810원 | 20,821원 | 5 |

> 신호는 완료봉 기준 관측값입니다. 체결·순자산은 가상 계산이며 실제 주문이나 수익 보장이 아닙니다. 15:00 봉 누락·무거래·데이터 지연은 별도 확인하세요.

---

### 🧪 백테스트 후속안 · 분리 가상 계좌

> 손절 버퍼 3% · +5% 1/2 분할 익절(잔여 +8%) · 전일 확정 지수 종가 > 20일 평균
> 지수 국면: KOSPI 통과 · KOSDAQ 통과 · 대기 0건 · 보유 2/3 · 가상 순자산 977,005원

> 아직 재백테스트·전진 검증되지 않은 가설입니다. 공식 v2.0과 성과를 합산하지 않습니다.

---

### 🎯 금일 조건 충족 신규 진입 후보 (스마트 랭킹 우선순위)

> 돌파 건전도(이격 +1.2~+4.0% 안착 🟢), 오전 골든타임(09~10시), 거래대금 및 수급을 종합 평가한 진입 추천 순위입니다.

| 순위 | 종목(코드) | 돌파모드 | 현재가 | 돌파이격 (판정) | 거래대금 · VR | 신호시각 | 스마트점수 |
|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| 🥇 1위 | **이수스페셜티케미컬** (457190) | ⚡ 조기(10m) | 72,000원 | -4.41% (턱걸이⚠️) | 3.3억 (3.3x) | 09-29 14:00 | 66.3점 |

---


<!-- QUANT_DASHBOARD:END -->

---

## 🔎 모드 2 운영 관찰 · 동적 판정 탐색기

**현재 운영 기준은 일봉 3팩터(위험조정 모멘텀 40%·중기 모멘텀 30%·CMF20 30%)와 브레드스 방어입니다.** SUE 점수·실적 촉매 핫스왑은 시점 오류와 미재현 수치가 확인되어 운영에 포함하지 않습니다. 실제 주문은 없습니다.

- [병렬 전진 관찰 GitHub Actions](.github/workflows/early-inception-forward.yml): 평일 17:40 KST에 완료 일봉을 재확인하고 모드 2·상승 초입의 상위 최대 5종목을 각각 봉인합니다. 기록은 `quant-research/data/research/T-110/`에 저장합니다.

- **T-095 탐색기**: 저장된 완료 일봉의 일일 가상 판정과 3팩터 RAG 근거, 분기 EPS 참고 상태를 함께 표시합니다. EPS는 현재 스냅샷의 전년 동기 증감이며 표준화 SUE가 아닙니다. 운영 점수 가산점과 실제 주문은 없습니다. [구현·한계](docs/tasks/T-095.md)
- **T-096 봉인 저널**: 탐색 결과와 가격·EPS·상태·코드의 SHA-256을 기록합니다. 같은 입력은 기존 기록을 재사용하며, 오래된 일봉이나 판정 도중 바뀐 원본은 봉인하지 않습니다. [구현·검증](docs/tasks/T-096.md)

```powershell
python -B quant-research/scripts/pure_quant_portfolio_manager.py explore
python -B quant-research/research/forward_decision_journal.py          # 쓰기 없는 준비 상태 확인
python -B quant-research/research/forward_decision_journal.py --capture # 신선한 완료 일봉이 있을 때 기록
```

가격 이력과 EPS 캐시는 각 로컬 데이터 원본이 필요합니다. T-096의 준비 상태 명령으로 매번 최신 일봉의 신선도를 확인하세요. GitHub Actions의 일봉 패널은 별도의 공개 시세 수신 결과를 사용하며 EPS 캐시나 SUE 점수를 사용하지 않습니다.

---

# 📦 Leobox Multi-root Workspace

Gemini(Antigravity), Claude(Claude Code), OpenAI Codex / Copilot이 유기적으로 협업하는 멀티 루트 워크스페이스입니다.

---

## 📁 구성 서브 프로젝트

| 폴더 | 표시 이름 | 용도 및 규칙 |
| :--- | :--- | :--- |
| [`quant-research/`](quant-research/README.md) | **📈 Quant Research** | 퀀트 알고리즘 연구, 시계열 분석, 백테스팅 ([`AGENTS.md`](quant-research/AGENTS.md)) |
| [`quant-collector/`](quant-collector/README.md) | **⏱️ Quant Collector** | 15분 간격 GitHub Actions 자동 수집 및 모바일 수동 실행 ([`AGENTS.md`](quant-collector/AGENTS.md)) |
| [`gym-app/`](gym-app/README.md) | **🏋️ Gym App** | 오프라인 퍼스트 안드로이드 헬스 기록 앱 ([`AGENTS.md`](gym-app/AGENTS.md)) |

---

## 🤖 멀티 에이전트 협업 체계 (Gemini + Claude + Codex)

- **공통 골든 룰**: [`AGENTS.md`](AGENTS.md)
- **Claude Code 엔트리**: [`CLAUDE.md`](CLAUDE.md)
- **Antigravity(Gemini) 엔트리**: [`GEMINI.md`](GEMINI.md)
- **전문 스킬 (Skills)**:
  - [`backlog-manager`](.agents/skills/backlog-manager/SKILL.md) : 백로그 조회 및 상태 변경 CLI 제어
  - [`adversarial-reviewer`](.agents/skills/adversarial-reviewer/SKILL.md) : 완료 전 적대적 코드 결함 감사
  - [`quant-backtest-validator`](.agents/skills/quant-backtest-validator/SKILL.md) : 미래 데이터 누수(Lookahead Bias) 및 과적합 검증
  - [`collector-safety-check`](.agents/skills/collector-safety-check/SKILL.md) : 자동 매수/주문 로직 차단 및 멱등성 검사
  - [`offline-first-reviewer`](.agents/skills/offline-first-reviewer/SKILL.md) : 오프라인 저장 및 운동 UI 사용성 검증

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
3. [`leobox.code-workspace`](leobox.code-workspace) 선택
