---
name: quant-colab-release
description: Build or review the quant-research standalone Colab cell, synchronized notebook and mobile dark-table output. Use for scanner presentation and notebook delivery, not generic notebooks or web apps.
---

# Colab 배포와 모바일 표

워크스페이스 루트 기준 `quant-research/AGENTS.md`, `quant-research/docs/rules.md`의 Colab 절과 `quant-research/설계문서.md` §§6–7,12를 읽는다.

- 배포 Python 파일을 단일 원본으로 정하고 노트북 코드 셀을 동기화한다. 경로를 추측하지 말고 실제 배포본을 확인한다.
- 배포 코드에는 로컬 프로젝트 import가 없어야 한다. 외부 의존성 설치와 최초 실행을 구별하고 Colab 단일 셀 실행을 검증한다. 로컬 문법 검사만으로 Colab 실실행 성공이라고 쓰지 않는다.
- 종목별 2열 다크 표, 큰 현재가, 부호·문구·색상을 함께 사용한다. 차트나 별도 웹앱을 추가하지 않는다.
- ‘패턴 조건 충족’/‘다음 봉 마감 대기’를 명확히 구분한다. 돌파선·점수·+3/+5%를 추천 매수가·확률로 표현하지 않는다.
- 무후보·시세 실패·지연 fixture로 표시를 확인한다. 현재가 미제공을 0으로 대체하지 않고 패턴 결과를 유지한다. 가격·단위·기준 시각과 출처가 추적되어야 한다.
- 네 가지 CSV와 manifest 보존 및 런타임 초기화 시 유실 안내를 확인한다. 노트북 저장 출력에 개인 거래 내역·비밀키가 남지 않게 한다.

배포본의 코드 일치, 독립 실행, 작은 화면의 가독성, 정상/관찰/실패 상태를 검증하고 실행하지 못한 환경은 명시한다. 사용자 요청 없는 게시·알림 전송은 하지 않는다.
