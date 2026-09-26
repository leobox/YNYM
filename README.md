## 🔎 모드 2 운영 관찰 · 동적 판정 탐색기

**현재 운영 기준은 일봉 3팩터(위험조정 모멘텀 40%·중기 모멘텀 30%·CMF20 30%)와 브레드스 방어입니다.** SUE 점수·실적 촉매 핫스왑은 시점 오류와 미재현 수치가 확인되어 운영에 포함하지 않습니다. 실제 주문은 없습니다.

- [모드 2 전용 GitHub Actions](.github/workflows/mode2-daily.yml): 평일 16:19·16:34·16:49 KST에 완료 일봉을 재확인하고 아래 관찰 패널을 갱신합니다. Actions 화면의 `Run workflow`로 수동 실행할 수 있습니다. 새 완료 일봉이 없어도 마지막 확인 일봉의 상위 10종목·상대점수·팩터 판정 근거를 한 표에 기준일과 함께 표시합니다. 새 월간 계획이나 체결을 기록하지 않습니다.

- **T-095 탐색기**: 저장된 완료 일봉의 일일 가상 판정과 3팩터 RAG 근거, 분기 EPS 참고 상태를 함께 표시합니다. EPS는 현재 스냅샷의 전년 동기 증감이며 표준화 SUE가 아닙니다. 운영 점수 가산점과 실제 주문은 없습니다. [구현·한계](docs/tasks/T-095.md)
- **T-096 봉인 저널**: 탐색 결과와 가격·EPS·상태·코드의 SHA-256을 기록합니다. 같은 입력은 기존 기록을 재사용하며, 오래된 일봉이나 판정 도중 바뀐 원본은 봉인하지 않습니다. [구현·검증](docs/tasks/T-096.md)

```powershell
python -B quant-research/scripts/pure_quant_portfolio_manager.py explore
python -B quant-research/research/forward_decision_journal.py          # 쓰기 없는 준비 상태 확인
python -B quant-research/research/forward_decision_journal.py --capture # 신선한 완료 일봉이 있을 때 기록
```

가격 이력과 EPS 캐시는 각 로컬 데이터 원본이 필요합니다. T-096의 준비 상태 명령으로 매번 최신 일봉의 신선도를 확인하세요. GitHub Actions의 일봉 패널은 별도의 공개 시세 수신 결과를 사용하며 EPS 캐시나 SUE 점수를 사용하지 않습니다.

---

## ⏱️ Quant Collector 모바일 운영 센터 (15분 수집 · 장마감 팩터 근거)

> [60분봉 수집 Actions](.github/workflows/lrm60_15m.yml)은 평일 장중 **15분 간격**이며, [모드 2 일봉 Actions](.github/workflows/mode2-daily.yml)는 완료 일봉을 **16:19·16:34·16:49 KST**에 재확인합니다. 일봉 팩터값은 완료 일봉이 새로 나와야 바뀝니다. 각 패널의 데이터 기준일과 확인 시각을 따로 확인하세요. 모드 2는 SUE를 제외한 가상 관찰·검토표이며 실제 주문이나 검증된 실전 수익률이 아닙니다. 기존 LRM-60 v2.0과 조기 돌파·청산 표는 별도 전략입니다. 상세 내용은 [`quant-collector/README.md`](quant-collector/README.md)를 참고하세요.

<!-- MODE2_DAILY:START -->

### 🧮 모드 2 · 일봉 팩터 관찰 (가상)

> 일봉 기준 `2026-09-23` · 확인 `2026-09-26 16:58 KST` · 유효 종목 `138/150` · 자격 통과 `33종목` · Breadth(SMA60 위) `53.6%` · **2026-09-23 기준 신규 편입 검토 가능**

> 팩터: 60일 모멘텀(최근 5일 제외) 30% · 변동성 조정 모멘텀 40% · CMF20 30%. 현재 시총 상위 종목군 기준이며 과거 3년 성과를 재현한 표본은 아닙니다.

> SUE 점수와 실적 촉매 핫스왑은 운영 판정에 포함하지 않습니다.

> 아래 이유는 [수신 원본·실패·해시](quant-collector/data/mode2_latest_manifest.json)와 [계산식](quant-research/scripts/pure_quant_portfolio_manager.py)에 연결된 수치 설명입니다. 회사 설명은 공식 출처와 확인일이 있는 항목만 별도 참고로 붙입니다. 출처 없는 473개 정적 설명은 사용하지 않습니다.

> 변동성 조정 모멘텀은 일반적인 샤프 지수가 아닙니다. CMF는 종가 위치·거래량 지표이며 기관·외국인 순매수를 식별하지 않습니다. 점수는 자격 통과 종목끼리의 상대 순위입니다.

> ⚠️ 오늘 날짜의 새 완료 일봉이 없습니다(휴장 또는 공급 지연). 아래는 2026-09-23 완료 일봉으로 계산한 마지막 판단입니다. 새 일봉·체결 가격을 추정하거나 새 월간 계획을 저장하지 않습니다.

**완료 일봉 팩터 상위 5종목 · 관찰 순위**

| 순위 | 종목·평가일 종가·상대점수 | 수치 기반 판정이유·출처 있는 회사 참고 |
|---:|:---|:---|
| 1 | **코스맥스** (192820)<br>272,000원 · **0.936** | 60→5거래일 수익률 **+75.6%** · 변동성 조정 모멘텀 **1.23** · CMF20 **+0.12**<br>점수 기여: 위험조정 0.400 + 추세 0.300 + CMF 0.236<br>회사 참고: 화장품 ODM 개발·제조 사업 ([공식 자료](https://www.cosmax.com/en/), 2026-09-25 확인) |
| 2 | **한미사이언스** (008930)<br>52,100원 · **0.836** | 60→5거래일 수익률 **+68.8%** · 변동성 조정 모멘텀 **0.82** · CMF20 **+0.07**<br>점수 기여: 위험조정 0.364 + 추세 0.282 + CMF 0.191<br>회사 참고: 한미그룹의 사업형 지주회사 ([공식 자료](https://sustainability.hanmiscience.co.kr/2025/en/ourcompany/companyintroduction), 2026-09-25 확인) |
| 3 | **한국콜마** (161890)<br>147,000원 · **0.758** | 60→5거래일 수익률 **+48.1%** · 변동성 조정 모멘텀 **0.63** · CMF20 **+0.04**<br>점수 기여: 위험조정 0.303 + 추세 0.273 + CMF 0.182<br>회사 참고: 화장품 ODM 사업 ([공식 자료](https://www.kolmar.co.kr/about/summary.php), 2026-09-25 확인) |
| 4 | **현대해상** (001450)<br>47,550원 · **0.724** | 60→5거래일 수익률 **+41.5%** · 변동성 조정 모멘텀 **0.77** · CMF20 **+0.02**<br>점수 기여: 위험조정 0.352 + 추세 0.236 + CMF 0.136 |
| 5 | **SK이노베이션** (096770)<br>149,200원 · **0.715** | 60→5거래일 수익률 **+46.9%** · 변동성 조정 모멘텀 **0.59** · CMF20 **+0.04**<br>점수 기여: 위험조정 0.279 + 추세 0.264 + CMF 0.173 |

> -15% 에어백 관찰: 가상 보유 기록 0건 · 검사 대상 없음.

> 월간 검토표: 새 계획은 아직 확정되지 않았습니다. 아래 기준일 순위는 관찰용이며 가상 체결 기록이 아닙니다.

**2026-09-23 완료 일봉 기준 상위 10종목 · 과거 관찰표**

> 마지막 확인 종가와 상대 점수입니다. 다음 거래일 시가나 신규 편입 확정을 뜻하지 않습니다.

| 순위 | 종목(코드) | 기준일 종가 | 상대 점수 |
|---:|:---|---:|---:|
| 1 | 코스맥스 (192820) | 272,000원 | 0.936 |
| 2 | 한미사이언스 (008930) | 52,100원 | 0.836 |
| 3 | 한국콜마 (161890) | 147,000원 | 0.758 |
| 4 | 현대해상 (001450) | 47,550원 | 0.724 |
| 5 | SK이노베이션 (096770) | 149,200원 | 0.715 |
| 6 | GS (078930) | 110,700원 | 0.715 |
| 7 | JB금융지주 (175330) | 30,150원 | 0.688 |
| 8 | 우리금융지주 (316140) | 35,350원 | 0.661 |
| 9 | 아모레퍼시픽 (090430) | 142,500원 | 0.648 |
| 10 | DB손해보험 (005830) | 184,600원 | 0.639 |

> 실거래 주문은 생성·전송하지 않습니다. 수익률은 후속 기간 검증 전까지 미확정입니다.
<!-- MODE2_DAILY:END -->

<!-- QUANT_DASHBOARD:START -->

> ⏱️ **수집 시각**: `2026-09-25 21:58 KST (15분 예약 주기)` | 📊 **감시 유니버스**: `350종목`

### 🧭 LRM-60 v2.0 · 4대 게이트 / 3슬롯

> **기준 완료봉:** `09-23 14:00 KST` · **4대 게이트 통과:** `0건` · **다음 봉 시가 대기:** `0건` · **가상 보유:** `0/3` · **가상 순자산:** `1,000,000원`

> 게이트별 통과: 정배열 `91` · 직전 20봉 돌파 `3` · 10억/2.5배 `18` · CLI/D_base `26` (평가 가능 `350`종목, 봉 결측 `0`종목)

> ⚠️ 마지막 세션의 **15:00~15:30 완료봉이 공급되지 않아** 14:00 봉까지만 평가했습니다. 종가를 현재가로 복원하거나 15시 신호를 추정하지 않습니다.

> 이번 기준봉에서 4대 게이트를 모두 통과한 종목이 없습니다.

> 신호는 완료봉 기준 관측값입니다. 체결·순자산은 가상 계산이며 실제 주문이나 수익 보장이 아닙니다. 15:00 봉 누락·무거래·데이터 지연은 별도 확인하세요.

---

### 🧪 백테스트 후속안 · 분리 가상 계좌

> 손절 버퍼 3% · +5% 1/2 분할 익절(잔여 +8%) · 전일 확정 지수 종가 > 20일 평균
> 지수 국면: KOSPI 통과 · KOSDAQ 통과 · 대기 0건 · 보유 0/3 · 가상 순자산 1,000,000원

> 아직 재백테스트·전진 검증되지 않은 가설입니다. 공식 v2.0과 성과를 합산하지 않습니다.

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
