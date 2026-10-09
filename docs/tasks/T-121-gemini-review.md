# T-121-gemini-review: Codex T-121 원격 3일 엔진 관찰 패널 및 Actions 적대적 감사 보고서

- **검토자**: Gemini (Antigravity)
- **대상 작업**: T-121 (`quant-research: 최신 원격 3일 엔진 기반 관찰 패널 수정 및 Actions 실검증`)
- **담당 AI**: Codex
- **검토 대상 커밋**: `d4e1e96f20834d498baf1c5a0ec9ab9e2320cca6` (branch: `codex/t121-forward-panel`)
- **작업 디렉터리**: `D:/leobox/.worktrees/t121_forward_panel`
- **검토 일시**: 2026-10-09 KST
- **최종 판정**: **승인 (Approved) — 배포 및 Actions 실검증 진행 권고**

---

## 1. 📋 개요 및 총평

Codex의 T-121 요청에 따라 `D:/leobox/.worktrees/t121_forward_panel`의 커밋 `d4e1e96f`에 대해 읽기 전용 적대적 감사(Adversarial Audit)를 수행했습니다.

T-120에서 지적되었던 하드코딩 딱지 배포 결함(R1) 및 표본 외삽(R4) 우려를 근본적으로 해소하고, `origin/main`의 최신 3일 엔진(`early_inception_3d`), 누락 세션 자동 보충(`backfill_missing`), 연속 포착 거래일 수 집계(`consecutive_observation_days`), 그리고 `REUSED` 시점 패널 재반영 경로를 결함 없이 통합했음을 확인했습니다.

---

## 2. 🔍 항목별 검토 결과

### 1) 3일 엔진과 레거시 Mode 2 기록 구분 및 동일 날짜 목록 포함 딱지 판정
- **판정: 완전 충족 (No defect found)**
- **구현 검토 (`quant-research/scripts/run_early_inception_forward.py:387-446`)**:
  - `focus_blocks`: 전략 키가 `early_inception_3d`이거나 `mode2` 슬롯 중 `score_field == "inception_3d_score"`인 블록만 정확히 집중 관찰로 식별.
  - 레거시 Mode 2 (`composite_score` 사용):
    - `is_focus = False`, `is_basic = False`.
    - 제목: `### 모드 2 · 과거 봉인 기록`, 딱지: `과거 모드 2`.
    - 60일 모멘텀, 위험조정 모멘텀, CMF20 수치 근거 유지.
    - 이때 `focus_blocks`가 비어 있으므로 기본 상승초입 표의 딱지는 `⚪ 3일 판정 기록 없음`으로 올바르게 분기.
  - 최신 3일 엔진 (`early_inception_3d`):
    - `is_focus = True`.
    - 제목: `### 상승초입 3일 이내`, 딱지: `🎯 3일 목록 포함`.
    - `setup_type` 태그(`[압축돌파]`, `[압축+폭발]`, `[거래량폭발]`), 수축비, 윗꼬리, 거래량 배수, CMF20 수치 근거 표시.
    - 동일 봉인일(`as_of`)의 `focus_codes` 집합을 사전에 추출하여 기본 표(`early_inception`)의 각 후보가 3일 목록에 실제로 포함되는지 동적으로 대조 (`🎯 3일 목록 포함` vs `🟢 기본 목록만`).
  - **봉인 불변성**: 렌더러가 입력 `record` 객체를 변이(mutation)시키지 않음을 `panel_record` 픽스처 직렬화 비교로 증명.
  - **실제 과거 8개 봉인 파일 렌더링 검증**:
    - `2026-09-28.json` (레거시 모드2): `과거 모드 2` 및 `⚪ 3일 판정 기록 없음` 정상 출력.
    - `2026-10-07.json` (후보 4건): SAMG엔터, 미래에셋생명 등 `🎯 3일 목록 포함` 정상 출력.
    - `2026-10-08.json` (후보 0건): `| - | 조건 충족 없음 | - | - | - | - | - |` (7개 컬럼 정합성 유지) 정상 출력.

### 2) Backfill 및 연속 포착 보존, REUSED 실행의 GitHub 반영 경로
- **판정: 완전 충족 (No defect found)**
- **동작 보존**:
  - `backfill_missing(finding, args.manifest, args.runs)` 호출 경로 유지.
  - `consecutive_observation_days(record, args.runs, finding["universe"])` 계산 결과가 `render_panel`의 `observed_days`로 전달되어 `{days}거래일째` 표시 유지.
- **REUSED 시점 처리 (`run_early_inception_forward.py:456-485`)**:
  - 이미 봉인된 날짜에 새 스냅샷이 들어올 경우 `result = {"reused": True, ...}`로 처리하고 `status = "REUSED"`를 표준 출력 JSON으로 방출.
  - 최신 일봉 기준 `latest.md`, `outcomes.json`, `README.md`가 정상 갱신됨.
- **Actions 워크플로 정합성 (`.github/workflows/early-inception-forward.yml:88-115`)**:
  - `status == 'REUSED'` 조건에서도 커밋 단계가 트리거되도록 조건문 확장.
  - `REUSED`일 때는 원본 tar.gz나 manifest는 건드리지 않고, 수정된 `latest.md`, `outcomes.json`, `README.md`만 스테이징.
  - `git stash push --include-untracked -m reused-fetch-inputs -- quant-research/data/research/T-063/ quant-research/data/vcp_snapshots/`를 수행하여 fetch로 생성된 미추적 파일 충돌 없이 안전하게 `git pull --rebase` 및 `git push` 실행 가능.
  - 로컬 Git 테스트 결과, 작업 트리가 깨끗할 때도 `No local changes to save`로 exit code 0을 반환하여 `set -e` 파이프라인 중단 위험 없음 확인.

### 3) 신규 단위 테스트의 경계/혼합/레거시/마커 보존 커버리지
- **판정: 완전 충족 (No defect found)**
- **검증 항목 대조 (`quant-research/tests/test_early_inception_forward.py`)**:
  - **빈 후보 / 시장 폭 게이트**: `test_panel_empty_candidates_and_market_gate`
    - `market_breadth_pct < 40%` 시 `🔴 후보 선별 중단` 검증.
    - 후보 0건 시 7열 마크다운 표 정합성 (`| - | 조건 충족 없음 | ... |`) 검증.
  - **혼합 후보 / 동일 날짜 판정**: `test_panel_badges_use_same_record_membership_and_preserve_counts`
    - 집중 후보(`000001`)와 기본 후보(`000001`, `000002`) 혼합 시 교집합은 `🎯 3일 목록 포함`, 여집합은 `🟢 기본 목록만` 정확한 분기 검증.
    - 테이블 열 수 8개 파이프 (7개 컬럼) 일치 검증.
  - **과거 스키마 호환**:
    - `test_panel_legacy_mode2_does_not_become_three_day_selection`: 순수 레거시 모드2가 3일 엔진으로 둔갑하지 않음을 검증.
    - `test_panel_mode2_slot_with_three_day_score_uses_three_day_fields`: 과도기 슬롯(`mode2` 키 + `inception_3d_score`)에서 모멘텀 필드 부재로 인한 KeyError 방지 검증.
  - **README 마커 보존**:
    - `test_readme_update_keeps_other_dashboard_sections`: `<!-- DUAL_FORWARD:START -->` 외부의 헤더 및 `<!-- QUANT_DASHBOARD:START -->` 수집기 대시보드 무손실 보존 검증.
    - `test_main_reused_record_refreshes_panel_without_rewriting_seal`: REUSED 상황에서 README 패널 교체 및 원본 봉인 바이트 불변 검증.

---

## 3. 🧪 독립 실행 검증 증거

모든 검증은 `D:/leobox/.worktrees/t121_forward_panel`에서 직접 실행되었습니다.

| 검사 항목 | 실행 명령 | 결과 |
| :--- | :--- | :--- |
| **핵심 전진+엔진 테스트** | `python -m pytest quant-research/tests/test_early_inception_forward.py quant-research/tests/test_early_inception_engine.py -q` | **25 passed in 0.58s** |
| **전체 퀀트 테스트 스위트** | `python -m pytest quant-research/tests/ -q` | **179 passed in 13.21s** |
| **정적 불변식 검사 (quant-guard)** | `python tools/ai/quant_guard.py` | **89 source files checked pass** |
| **JS/CJS 구문 검사** | `node tools/ai/check_syntax.cjs` | **12개 대상 파일 구문 통과** |
| **봉인 기록 8개 렌더 검사** | `runs/*.json` 전수 렌더 스크립트 | **8개 파일 무오류 렌더 성공** |
| **Git Stash 멱등성** | `git stash push --include-untracked ...` | **정상 종료 (exit code 0)** |

---

## 4. 💡 결론 및 Codex 인수인계

- **판정 요약**: Codex가 작성한 커밋 `d4e1e96f`는 결함 없이 안전하며, 설계 및 운영 요구사항을 완벽히 충족합니다.
- **후속 조치**:
  1. Codex는 `codex/t121-forward-panel` 브랜치를 원격 `origin/main`으로 푸시할 수 있습니다 (`git push origin codex/t121-forward-panel:main`).
  2. GitHub Actions에서 `workflow_dispatch` 수동 실행을 트리거하여 원격 환경에서의 패널 반영을 실검증할 수 있습니다.
  3. 로컬 `D:/leobox`의 미추적/미커밋 파일은 일절 건드리지 않고 완벽히 격리·보존되었습니다.
