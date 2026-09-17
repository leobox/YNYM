---
name: offline-first-reviewer
description: Review Android and mobile application code for offline-first architecture, local SQLite/Room persistence, gym environment usability (touch target size, dark theme), and background session retention.
---

# Offline-First & Mobile Gym Usability Reviewer Skill

헬스장 모바일 앱(`gym-app`) 개발 시 오프라인 동작 신뢰성과 운동 중 사용성을 검토하는 스킬입니다.

## 📱 주요 검토 기준

### 1. 오프라인 퍼스트 아키텍처
- 인터넷 연결 상태와 무관하게 모든 데이터 생성/수정이 로컬 저장소(Room / SQLite / DataStore)에 즉시 커밋되는지 확인.
- 원격 동기화 실패 시 로컬 저장을 블로킹하지 않는 구조인지 점검.

### 2. 운동 세션 상태 보존
- 액티비티/프로세스가 백그라운드 전환 또는 OS 재생성 시에도 현재 타이머 시간, 입력 중이던 세트/무게 데이터가 ViewModel / SavedStateHandle / DB를 통해 복원되는지 확인.

### 3. 운동 환경 터치 및 시인성 (Gym Usability)
- 세트 완료, 중량 증감 버튼 등의 클릭 영역이 최소 `48dp` 이상인지 확인.
- 어두운 환경 및 배터리 절약을 위한 다크 테마 컬러 대비가 적절한지 점검.
- 음수 중량이나 음수 반복수와 같은 잘못된 값 입력 방지 유효성 검사 적용 여부.
