# AGENTS.md — 🏋️ Gym App (헬스 앱 도메인 규칙)

안드로이드 헬스/피트니스 기록 애플리케이션 개발 시 모든 AI 에이전트가 준수해야 하는 모바일 도메인 규칙입니다.

## ⚠️ 실제 아키텍처 (계획과 다름, 임의로 되돌리지 말 것)

`time-fitness/`는 Kotlin/Compose/Room 네이티브가 **아니라** WebView 쉘 + 단일 페이지 HTML/JS이며,
`D:\RUNG'S GYM\time-fitness`(v1.5)에서 이전한 실제 운영 중인 앱입니다. 아래 표는 원래 계획했던
스택과 실제 스택의 대응 관계이므로, "Room으로 마이그레이션" 같은 재작성을 임의로 시작하지 않습니다.

| 원래 계획 (미채택) | 실제 구현 |
|---|---|
| Kotlin + Jetpack Compose | `java/com/timefitness/app/MainActivity.java` (WebView 셸) + `assets/index.html`·`app.js`·`data.js` |
| Room / SQLite | `SharedPreferences`에 JSON 한 덩어리 (JS ↔ `Native.save/load` 브리지) |
| Gradle / Android Studio 빌드 | `time-fitness/build.py` (Java `javac`/`d8`, 포터블 SDK의 `aapt`/`zipalign`/`apksigner`) |
| 에뮬레이터/기기 UI 테스트 | `time-fitness/test-*.cjs` (Playwright + headless Chromium으로 `assets/index.html` 직접 구동) |

기능을 고치는 작업은 대부분 `assets/app.js`, `assets/data.js`, `assets/index.html`만 건드리면 된다.
새 기능·리팩터 전에 관련 `test-*.cjs`를 읽고 어떤 동작이 이미 검증돼 있는지 확인한다.

---

## 📱 사용자 경험 (Gym UX) 불변식

1. **운동 환경 친화적 UI (한 손 조작 & 시인성)**:
   - 헬스장 환경(운동 기구 사용 중, 땀이 묻은 손, 거친 호흡)을 고려하여 모든 핵심 버튼의 터치 타깃은 CSS 기준 최소 48px 이상으로 큼직하게 설계합니다 (`assets/app.css`/`management.css`).
   - 어두운 실내 환경 및 아몰레드 배터리 절약을 위해 **다크 테마**를 기본으로 유지합니다 (`index.html`의 `theme-color`, 배경색 확인).
   - 복잡한 텍스트 입력 대신 숫자 입력(`inputmode="decimal"/"numeric"`)이나 선택형 UI를 우선합니다.

---

## 💾 오프라인 퍼스트 (Offline-First) 및 안정성

2. **완전한 오프라인 독립성**:
   - `AndroidManifest.xml`에 인터넷 권한이 없다 — 절대 추가하지 않는다. 서버·로그인·네트워크 호출을 들여오지 않는다.
   - 모든 사용자 입력은 **저장 버튼을 눌렀을 때** `Native.save`를 통해 `SharedPreferences`에 즉시 반영되어야 한다 (초안/입력 중 상태는 메모리에만 유지해도 되지만, 저장 누락으로 기록이 사라지면 안 된다).

3. **화면 전환/재시작 시 상태 보존 (Zero Data Loss)**:
   - 폴드 화면 접힘·펼침, 회전, 백그라운드 전환 시 `AndroidManifest.xml`의 `configChanges` 선언으로 Activity가 재시작되지 않아야 하며, 저장 전 입력값(초안)이 유지되어야 한다.
   - 저장 실패(파일 시스템 오류, 잘못된 백업 파일 가져오기 등) 시 기존 데이터를 덮어쓰지 않고 원복한다 — `test-*.cjs`의 "invalid file rejected", "restore rollback" 케이스가 이 불변식을 검증한다.

4. **데이터 단위 및 유효성 검증**:
   - 중량: kg 단위, 음수 입력 절대 불가 (`kg` input은 `min="0"`).
   - 횟수(Reps)·세트: 1 이상의 정수 (`min="1" step="1"`).
   - 백업/가져오기(JSON)는 가져오기 전 항목 수를 요약해 보여주고, 사용자가 확정하기 전에는 기존 데이터를 바꾸지 않는다.

---

## 🔐 서명 키 취급

- `time-fitness/signing/`(키스토어 + 비밀번호)는 `.gitignore`로 제외되어 있다. **절대 git에 커밋하지 않는다.**
- 같은 서명이 유지되어야 기존 설치 위에 업데이트 설치가 가능하다 — 이 폴더를 삭제하거나 새로 생성하지 않는다.
