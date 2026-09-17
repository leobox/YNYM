# 🏋️ Gym App (안드로이드 헬스 앱)

개인 운동 기록 및 루틴 관리를 위한 안드로이드 앱. 현재 앱은 `time-fitness/` 하나이며,
`D:\RUNG'S GYM\time-fitness`(v1.5)에서 이전한 실제 운영 중인 프로젝트입니다.

## 📱 실제 구조 (기존 계획과 다름)

- Kotlin/Compose/Room 네이티브가 아니라 **WebView 쉘 + 단일 페이지 HTML/JS**입니다.
- 저장은 Room/SQLite가 아니라 **`SharedPreferences`에 JSON 한 덩어리**(Native ↔ JS 브리지).
- Gradle/Android Studio 없이 **Java 11 + Python 3 + 포터블 Android SDK**로 직접 빌드합니다 (`time-fitness/build.py`).
- 화면 테스트는 실기기/에뮬레이터가 아니라 **Playwright + headless Chromium**으로 `assets/index.html`을 직접 구동합니다.

상세 규칙과 도메인 불변식은 [`AGENTS.md`](AGENTS.md), 앱 자체의 사용법·빌드·검증 방법은
[`time-fitness/README.md`](time-fitness/README.md)를 따릅니다.

## 🔧 빌드 / 테스트 빠른 시작

```powershell
# 1. SDK 준비 (최초 1회, 없으면 다운로드)
python tools/setup_android.py

# 2. APK 빌드 (서명 포함)
python time-fitness/build.py

# 3. 화면 테스트
cd time-fitness
npm install
npm test
```

`time-fitness/signing/`(서명 키)은 git에 커밋하지 않습니다 (`.gitignore`). 새 환경에서는
기존 키를 그대로 복사해 와야 기존 설치 위에 업데이트 설치가 가능합니다 — 새로 생성하면
다른 서명이 되어 재설치가 필요합니다.

## 📁 디렉터리 구성

- `time-fitness/` — 앱 소스 (assets, java, res, AndroidManifest, 빌드/테스트 스크립트)
- `tools/setup_android.py` — 포터블 Android SDK(build-tools 34.0.0, platform 34, platform-tools) 다운로드·체크섬 검증
- `tools/android-sdk/` — (git 미추적) 위 스크립트로 생성되는 SDK 디렉터리
