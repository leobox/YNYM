---
name: quant-pattern-scanner
description: Implement or review the quant-research Korean hourly stock scanner, breakout confirmation, deterministic top-five selection and watchlist eligibility. Use for signal changes, not account execution research.
---

# 운영 패턴과 후보 선정

워크스페이스 루트 기준 `quant-research/AGENTS.md`, `quant-research/docs/rules.md`의 운영 패턴 절, 필요시 `quant-research/설계문서.md` §5를 읽는다.

- 변경 전 운영 코드와 연구 코드의 실제 경로·호출 관계를 확인한다. 이전 소스가 없으면 합성 점수 공식을 임의 구현해 동등하다고 주장하지 않는다.
- 기본 패턴 → 돌파·거래대금 → 바로 다음 완료봉 유지 확인을 별도 결과로 추적한다. ATR·고저 차·비교 대금의 0분모와 표본 부족은 명시적으로 다룬다.
- 같은 시간대 거래대금 기준에서 현재 봉을 제외한다. 확인봉 저가가 한 번이라도 돌파선 아래이면 회복 여부와 무관하게 탈락한다.
- 관찰은 현재 완료봉에서 기본 패턴·돌파·대금을 통과한 경우만 포함한다. 단순 직전봉 돌파나 기본 미달로 채우지 않는다.
- 최종 후보 정렬은 점수 내림차순·코드 오름차순, 최대 5개·0개 허용. 최종/관찰 중복을 제거하고 보조 현재가 조회가 자격·정렬에 영향을 주지 않게 한다.
- RSI 필수화, MA60·눌림·+3% 진입 필터 추가는 기존 운영 조건 보존이 아니다. 별도 실험으로 분리하고 채택 근거를 남긴다.

시점/신호 로직 변경 시 prefix 불변성, 경계값, 확인봉 미완료, 저가 이탈 후 회복, 거래대금 재증가 없음, 동점·무후보를 테스트한다. 보조 조회는 후보 코드별 중복 제거 및 무후보 0회를 검증한다.
