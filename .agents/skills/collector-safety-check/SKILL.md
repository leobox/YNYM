---
name: collector-safety-check
description: Inspect quant data collection scripts and workflows to guarantee that NO order/trading execution logic exists, ensure idempotency, verify API timeout/retry handling, and check GitHub Actions workflow syntax and mobile dispatch.
---

# Collector Safety Check Skill

퀀트 시세 수집기(`quant-collector`) 코드 및 GitHub Actions 워크플로의 안전성과 운영 지속성을 검증하는 스킬입니다.

## 🚨 1순위 안전 검사: 주문/거래 로직 완전 배제
코드베이스 전체를 정규식/grep으로 검색하여 거래 주문 관련 API 호출이 전혀 없는지 확인합니다:
- 금지 키워드: `buy`, `sell`, `order`, `create_order`, `submit_order`, `balance_transfer`, `cancel_order`, `private_api`
- 허용 범위: `public_api`, `ticker`, `candles`, `orderbook_read`, `ohlcv`, `fetch_ohlcv` 등 순수 조회 로직만 허용.

## ⚙️ 2순위 안정성 검사
1. **타임아웃 설정**: 모든 HTTP / API 요청에 명시적 `timeout` 파라미터가 부여되어 있는가?
2. **지수 백오프(Exponential Backoff)**: 일시적 네트워크 단절 시 즉시 크래시되지 않고 최대 3회 재시도하는가?
3. **멱등성(Idempotency)**: 동일 시간에 중복 실행되어도 데이터가 덮어써지거나 중복 삽입되지 않는가?
4. **모바일 웹 지원**: `.github/workflows/*.yml`에 `workflow_dispatch`가 유지되고 있는가?
5. **시크릿 누출 방지**: 커밋에 토큰이나 API 비밀키가 평문으로 포함되지 않았는가?
