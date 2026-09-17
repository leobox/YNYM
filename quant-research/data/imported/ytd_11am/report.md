# 2026년 11시 고정 진입 검증

기간: 2026-01-01 ~ 2026-09-16, 172거래일, 현재 고정 종목군 150개.

학습 후보: hold_cap3_trendTrue; 학습 거래 수/낙폭 기준 통과: False; 운영 변경 채택: False.

## 실험 조건

- 초기 100만원, 단일 종목 전액·정수 주식 수, 보유 중 새 신호 무시. 11시 동시 후보는 점수순.
- +10% 전량 익절 / -5% 손절 / 최대 5거래일. 비용은 편도 0.175% 가정.
- hold=기존 다음 봉 유지 확인; money=돌파·거래대금 충족 후 진입. 둘 다 기존 기본패턴 적용.
- cap=돌파봉 종가 대비 진입가 상승 한도(None/3/5%). trend=True는 종가>60선, 60선이 6봉 전보다 상승.
- 1~4월 학습: 4개 지연/가격 스트레스 시나리오 중 최소 수익률 최대화. 각 8거래 이상, 시간봉 최대낙폭 20% 이내 필요.
- 5~6월 검증: 후보 1개만 기존과 비교. 각 5거래 이상·수익률 개선·승률 유지·낙폭 악화 2%p 이내 필요.
- 7월~9월16일 확인: 재선택 없이 후보 검증. 과거 분석에서 본 일부 기간이 겹치므로 미사용 OOS는 아님.
- 11시 직후 실제 체결가 대신 11시 시가 및 시가+0.3% 스트레스. 네이버 과거 시세는 복원하지 못함.

## 기존 조건과 학습 후보 결과

|설정|기간|지연(분)|매수가 가산|최종잔고|거래|수익거래율|+10% 도달률|시간봉 최대낙폭|
|---|---|---:|---:|---:|---:|---:|---:|---:|
|hold_capNone_trendFalse|validation|0|0.0%|1,265,223원|9|55.6%|55.6%|-9.8%|
|hold_capNone_trendFalse|validation|0|0.3%|1,264,698원|9|55.6%|55.6%|-9.9%|
|hold_capNone_trendFalse|validation|60|0.0%|1,092,905원|1|100.0%|100.0%|-0.8%|
|hold_capNone_trendFalse|validation|60|0.3%|1,093,184원|1|100.0%|100.0%|-1.1%|
|hold_cap3_trendTrue|validation|0|0.0%|1,433,070원|4|100.0%|100.0%|-1.4%|
|hold_cap3_trendTrue|validation|0|0.3%|1,308,183원|3|100.0%|100.0%|-1.4%|
|hold_cap3_trendTrue|validation|60|0.0%|1,000,000원|0|—|—|0.0%|
|hold_cap3_trendTrue|validation|60|0.3%|1,000,000원|0|—|—|0.0%|
|hold_capNone_trendFalse|check|0|0.0%|1,526,447원|17|58.8%|52.9%|-20.0%|
|hold_capNone_trendFalse|check|0|0.3%|1,341,719원|18|50.0%|50.0%|-24.2%|
|hold_capNone_trendFalse|check|60|0.0%|1,055,548원|4|50.0%|25.0%|-10.0%|
|hold_capNone_trendFalse|check|60|0.3%|1,052,642원|4|50.0%|25.0%|-10.3%|
|hold_capNone_trendFalse|all|0|0.0%|1,547,778원|44|47.7%|40.9%|-33.3%|
|hold_capNone_trendFalse|all|0|0.3%|1,205,898원|45|42.2%|37.8%|-38.7%|
|hold_capNone_trendFalse|all|60|0.0%|1,222,356원|13|53.8%|38.5%|-12.0%|
|hold_capNone_trendFalse|all|60|0.3%|1,213,783원|13|53.8%|38.5%|-12.3%|
|hold_cap3_trendTrue|check|0|0.0%|1,273,904원|10|60.0%|50.0%|-14.6%|
|hold_cap3_trendTrue|check|0|0.3%|1,184,918원|10|50.0%|50.0%|-21.3%|
|hold_cap3_trendTrue|check|60|0.0%|1,135,284원|2|100.0%|0.0%|-7.9%|
|hold_cap3_trendTrue|check|60|0.3%|1,068,526원|1|100.0%|0.0%|-7.9%|
|hold_cap3_trendTrue|all|0|0.0%|1,830,463원|28|57.1%|46.4%|-29.5%|
|hold_cap3_trendTrue|all|0|0.3%|1,428,506원|26|50.0%|42.3%|-26.7%|
|hold_cap3_trendTrue|all|60|0.0%|1,219,489원|6|66.7%|33.3%|-7.9%|
|hold_cap3_trendTrue|all|60|0.3%|1,147,808원|5|60.0%|40.0%|-7.9%|

## 적용 가이드

채택되지 않았다면 기존 코랩 조건을 유지한다. 가격 이격 표시는 참고 정보이며 매수 권고가 아니다. 11시 실행 시 데이터 기준봉과 네이버 시세 시각을 각각 확인한다. 네이버 현재가는 패턴 봉의 지연을 제거하지 않는다.

## 한계

Current fixed 150 cohort, historical constituents unavailable: selection/survivorship bias. 15:00-15:30 missing; max5day/end exits at 14-bar close. Continuous stops/targets assumed. Entry at 11:00 open, 0/60min bar delay and +0/+0.3% entry-price stress, not actual 11:01 Naver prices. Independent 1m accounts per split; full-period account continuous. Fee0.175% each side hypothetical. Part of check period previously examined; not untouched OOS. Fixed +10/-5; no promise of stable returns.

현재 종목군으로 과거를 계산한 선택·생존편향 때문에 실전 기대수익으로 해석할 수 없다. 15시~15시30분 구간이 없고, 익절/손절을 장중 계속 감시한다고 가정했다. 10%를 안정적으로 보장하는 조건은 아니다.