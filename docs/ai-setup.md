# 공통 AI 작업 설정

공통 규칙은 루트 및 대상 프로젝트의 `AGENTS.md`, 연구 세부 기준은
`quant-research/설계문서.md`와 `quant-research/docs/rules.md`다.
`CLAUDE.md`와 `GEMINI.md`는 이 원본으로 연결한다.

## 스킬

프로젝트 스킬 원본은 `.agents/skills/<이름>/SKILL.md`다. 자동 발견이 없는
환경에서는 작업에 맞는 파일을 직접 읽는다. AI별 스킬 사본을 만들지 않는다.

| 작업 | 스킬 |
|---|---|
| 백로그 착수·완료·인수인계 | `backlog-manager` |
| 스킬 검색·설치 | `find-skills` |
| 데이터 이전·완료봉·재현 기록 | `quant-data-provenance` |
| 돌파·유지·최종/관찰 후보 | `quant-pattern-scanner` |
| 체결·계좌·성과 검증 | `quant-backtest-validator` |
| Colab 단일 셀·모바일 표 | `quant-colab-release` |
| 완료 전 결함 검토 | `adversarial-reviewer` |

`find-skills`는 2026-09-17에
[Vercel 원본](https://skills.sh/vercel-labs/skills/find-skills)에서 설치했다.
사용자 전역의 기존 설치는 유지한다. 검색 명령은 `npx skills find <검색어>`이며
스킬 파일 설치와 CLI 패키지의 전역 설치는 별개다. 이 작업에서는 CLI를 전역 설치하지 않았다.

Codex와 Claude의 선택적 리뷰어 설정은 `.codex/agents/adversarial-reviewer.toml`,
`.claude/agents/adversarial-reviewer.md`에 있다. 모델은 지정하지 않고 세션 설정을
상속한다. 리뷰어는 읽기 전용이며 자동 호출이나 작업 완료 처리를 하지 않는다.

## 세션 훅

| 환경 | 설정 파일 | 시작 / 종료 이벤트 |
|---|---|---|
| Codex | `.codex/hooks.json` | `SessionStart` / `Stop` |
| Claude Code | `.claude/settings.json` | `SessionStart` / `Stop` |
| Gemini CLI | `.gemini/settings.json` | `SessionStart` / `AfterAgent` |
| Antigravity·그 밖의 클라이언트 | 아래 수동 검사 | Gemini CLI 지원과 동일하다고 가정하지 않음 |

세 설정은 `tools/ai/lifecycle.cjs`를 호출한다. Node에서 Git 루트를 찾아
하위 폴더에서도 같은 파일을 실행한다. Git, Node, Python 3가 PATH에 필요하다.
시작 훅은 규칙 위치와 백로그를 제공한다. 종료 훅은 루트 또는
`quant-research` 안에서 연구 정적 검사와 백로그 무결성 검사를 실행한다.
형제 프로젝트 안에서 시작한 세션은 연구 종료 검사를 건너뛴다.

실패 시 Codex/Claude는 `block`, Gemini는 `deny`를 반환한다.
이미 훅에 의해 재개된 턴(`stop_hook_active`)은 실패 이유만 알리고 추가 재개를
요청하지 않는다. 이 경우 통과한 것이 아니므로 남은 실패를 보고해야 한다.
훅은 파일 수정·자동 커밋·네트워크 조회·백로그 상태 변경을 하지 않는다.

설정 형식은 [Codex 훅](https://developers.openai.com/codex/hooks),
[Claude 훅](https://code.claude.com/docs/en/hooks),
[Gemini 훅](https://geminicli.com/docs/hooks/reference/) 공식 문서 기준이다.
Codex/Claude timeout은 초, Gemini는 밀리초로 지정한다.
Codex의 프로젝트 및 개별 훅 신뢰가 필요하며 새 세션의 `/hooks`에서 상태를
확인한다. 설정 파일 생성과 클라이언트 안에서의 자동 실행 확인은 별개다.
클라이언트 신뢰 저장소를 자동 변경하거나 신뢰 검사를 우회하지 않는다.

확인한 로컬 버전은 Codex CLI 0.154.0, Claude Code 2.1.274다.
Gemini CLI 실행 파일은 현재 PATH에서 발견되지 않았다. 명령·JSON 프로토콜은
로컬 테스트하고, 각 AI의 새 세션에서 이벤트가 자동 발생하는지는 별도 확인한다.

## Git 커밋 검사

`.githooks/pre-commit`은 `quant-research`의 **스테이징된 blob**을 검사한다.
작업 파일을 고쳤더라도 Git index에 이전 문제가 남아 있으면 실패한다.
현재 저장소에 활성화하는 명령:

```powershell
git config --local core.hooksPath .githooks
```

새 clone에는 로컬 Git 설정이 전달되지 않으므로 다시 실행한다.
기존 `core.hooksPath`가 있다면 기존 훅과 통합한 후 설정한다.

## 수동 검사와 보장 범위

워크스페이스 루트에서 실행한다.

```powershell
node tools/backlog.mjs get T-006
python tools/ai/quant_guard.py
python tools/ai/quant_guard.py --staged
python -m unittest discover -s tools/ai -p test_*.py -v
node tools/backlog.mjs check
node tools/ai/check_syntax.cjs
```

세션 종료(Stop) 시 `lifecycle.cjs`가 위 `quant_guard.py`, `backlog.mjs check`,
`check_syntax.cjs`(Node `.js`/`.cjs`/`.mjs` 구문 검사, 이 스택엔 번들러가 없어 이게 build를
대신한다) 세 가지를 Claude/Codex/Gemini 세 provider 모두에서 동일하게 실행한다.

정적 검사는 `quant-research/`, `quant-collector/` 두 폴더를 대상으로 Python/노트북
문법, 알려진 주문 API 호출명, 인식 가능한 requests 호출의 timeout 누락 및 잘못된
리터럴 값을 검사한다. 스테이징 검사에는 두 폴더의 `.env`, `.pem`, `.key` 파일
차단도 포함된다. 주석·가상 체결 변수명은 주문으로 판정하지 않는다.

이는 실제 거래 방지의 완전한 보안 경계가 아니다. 동적 호출, 사용자 정의 HTTP
래퍼, 런타임 timeout 값, 비밀키 문자열 전체는 검증하지 못한다. 네트워크 재시도,
미래정보 누출, 회계, 데이터 최신성, Python/노트북 동기화와 Colab 실실행은
해당 작업의 기능 테스트와 리뷰가 필요하다. 소스가 없거나 스테이징하지 않아
`0 source files checked`가 나오면 코드 재현이나 전략 성능 검증의 성공을 뜻하지 않는다.
