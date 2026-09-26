# 📈 Quant Research (퀀트 알고리즘 연구)

모바일 Colab 한국 주식 60분봉 패턴 스캐너 및 백테스트를 재현하는 프로젝트입니다.

요구사항은 [설계문서.md](설계문서.md), 작업 기준은 [AGENTS.md](AGENTS.md)와
[docs/rules.md](docs/rules.md)를 따릅니다. AI 설정은 [공통 안내](../docs/ai-setup.md)를 참고합니다.

공통 작업 설정은 T-006, 이전 `D:\DEPO_M` 코드의 모듈 이전은
[T-007](../docs/tasks/T-007.md), 통합 검증·Colab 배포·데이터 재현은
[T-008](../docs/tasks/T-008.md)에 기록했습니다. 최신 상태는 백로그 CLI로 확인합니다.

## Colab 배포본

- 단일 원본: [`notebooks/pattern_colab.py`](notebooks/pattern_colab.py)
- 생성 노트북: [`notebooks/pattern_top5.ipynb`](notebooks/pattern_top5.ipynb)
- 생성: `python tools/build_notebook.py`
- 동기화 검사: `python tools/sync_check.py`

노트북은 출력이 제거된 한 개 코드 셀이며 로컬 프로젝트 import 없이 실행되도록 구성했습니다.
실행하면 `universe`, `scores`, `top5`, `watch` CSV와 해시·행 수가 있는 manifest를
`pattern_snapshots/`에 남깁니다. Colab 런타임이 초기화되기 전에 내려받아야 합니다.

## 모드 2 동적 EPS·RAG 판정 탐색기

모드 2 `daily-plan`은 저장된 완료 일봉의 종가로 다음 거래일의 가상
판정을 계산합니다. 브레드스 40% 미만 방어, 50% 이상 2일 회복,
20거래일 점검과 조기 퇴출을 적용합니다. 실제 주문 기능은 없습니다.

```powershell
python -B quant-research/scripts/pure_quant_portfolio_manager.py daily-plan --date YYYY-MM-DD
```

완료 일봉 기준 `daily-plan`의 일일 판정과 3팩터 RAG 근거, T-094의
무인증 분기 EPS 상태를 한 표에서 확인합니다. EPS는 표준화 SUE가 아니므로
운영 순위에 가산하지 않습니다. 과거 평가일 뒤 수집한 EPS는 숨깁니다.

```powershell
python -B quant-research/scripts/fetch_quarterly_sue.py
python -B quant-research/scripts/pure_quant_portfolio_manager.py explore --md-out quant-research/data/research/T-095/dynamic_explorer_latest.md
```

`explore`는 가상 상태를 변경하지 않으며 `--apply`를 받지 않습니다.
가격 이력과 EPS 캐시는 별도 로컬 데이터로 준비해야 합니다.
설계 근거와 한계는 [T-095 작업 기록](../docs/tasks/T-095.md)에 있습니다.

## VCP 슈퍼 신고가 스캐너 (운영)

- 판정 모듈: [`scanner/vcp.py`](scanner/vcp.py)
- 일괄 실행기: [`scripts/run_vcp_scanner.py`](scripts/run_vcp_scanner.py)
- Colab 배포본: [`scripts/vcp_scanner_colab.py`](scripts/vcp_scanner_colab.py)
- GitHub Actions: [`.github/workflows/vcp-scanner.yml`](../.github/workflows/vcp-scanner.yml)
- 최신 스캔 스냅샷: [`data/vcp_snapshots/latest_vcp.md`](data/vcp_snapshots/latest_vcp.md)

## 이전 데이터

`data/imported/`에는 `D:\DEPO_M\agent\results`의 선택된 6개 결과 트리를 보존했습니다.
상대경로·크기·파일 SHA256을 포함한 전체 트리 비교 결과는
[`data/imported/INVENTORY.md`](data/imported/INVENTORY.md)에 있습니다. 이 데이터는
현재 종목군과 Yahoo 60분봉의 과거 스냅샷이며 생존편향, 15:00~15:30 누락,
수정주가·시장 기준 미확인 한계를 그대로 가집니다.

## 현재 실행 가능한 검사

`quant-research` 폴더에서:

```powershell
python -m pytest tests -v --tb=short
python tools/sync_check.py
python tools/verify_data.py
python tools/gen_inventory.py
python ..\tools\ai\quant_guard.py
```

테스트와 정적 검사 통과는 라이브 Yahoo/네이버 응답, 실제 Colab 런타임,
전략의 미래 성과를 보장하지 않습니다. 점수는 확률이 아니며 실제 주문 기능은 없습니다.
