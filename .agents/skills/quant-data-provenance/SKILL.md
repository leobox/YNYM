---
name: quant-data-provenance
description: Validate or migrate quant-research market data, completed candles, timestamps, universes and reproducible run snapshots. Use for data adapters and research reproduction, not unrelated collectors.
---

# 시세 데이터와 재현성

워크스페이스 루트 기준 `quant-research/AGENTS.md` 및 `quant-research/docs/rules.md`의 데이터 절을 읽는다. 외부 프로젝트를 옮길 때에는 `quant-research/설계문서.md` §13의 목록과 실제 파일을 대조한다.

- 원본 존재·크기·해시를 먼저 확인한다. Git clone만으로 미추적 연구 결과가 이전된다고 가정하지 않는다. 데이터나 전략을 새로 수집·발명해 원본 재현이라고 보고하지 않는다.
- 시간봉 시작과 종료, 수신·발견 시각을 구별한다. 휴장, 데이터 지연, 요청 실패, 정상 무후보를 각각 재현하는 fixture를 만든다.
- 정규 시작 시각·미완료봉·중복·NaN/Inf·OHLC·음수 거래량·15시 표시용 봉을 검사한다. 14시 봉을 공식 종가로 취급하지 않는다.
- 신규 공급원은 실제 응답 스키마, 단위, 수정주가, 시장, 이용 조건, 보존 범위와 지연을 확인한다. 확인하지 못한 항목은 미확인으로 기록한다.
- run_id별 원본과 manifest를 함께 보존한다. 현재 고정 종목군인지 과거 시점 종목군인지 명시한다. 재실행과 재시도를 구분하고 원본 덮어쓰기·중복 저장을 검증한다.
- 네트워크에는 timeout과 유한 재시도를 둔다. 원본 응답 fixture로 오류·빈 응답·부분 실패를 검증하고, 라이브 API 호출 없이도 테스트가 가능하게 한다.

완료 보고에는 재현 가능한 실행 명령, 데이터 범위/출처, 누락·편향, 실제로 검증한 불변식을 적는다. 데이터가 없는 항목을 성공 처리하지 않는다.
