---
name: adversarial-reviewer
description: Rigorously audit code changes across quant research, data collector, and mobile app projects before marking tasks done. Identifies domain invariant violations, edge cases, NaN/overflow issues, trading logic leaks, and breaking changes.
---

# Adversarial Reviewer Skill

코드 작성 후 `done`으로 상태를 변경하기 전에, 구현된 변경 사항의 결함과 취약점을 찾아내기 위해 활용하는 적대적 감사 스킬입니다.

## 🔍 감사 절차

1. **변경 사항 확인**:
   ```bash
   git status --porcelain
   git diff HEAD
   ```
2. **프로젝트별 불변식 위반 검사**:
   - `quant-research`: 미래 데이터 참조(`shift(-1)`, 미래 캔들 조회), 시드 미고정, 비정상 성과 계산.
   - `quant-collector`: **주문/거래 API 유출 여부**, 타임아웃 부재, 멱등성 실패, 비밀키 노출.
   - `gym-app`: 오프라인 상태 저장 누락, 세션 상태 증발, 유효하지 않은 수치 입력 허용.
3. **지적 원칙**:
   - 막연한 의견이나 취향 지양.
   - 반드시 **"입력값/상황 + 발생하는 비정상 동작 + 코드 위치"**를 명확히 제시.
   - 통과 시에만 `node tools/backlog.mjs set <id> done` 승인.
