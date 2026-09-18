# GEMINI.md — Antigravity / Gemini 협업 가이드

이 프로젝트는 Antigravity(Gemini), Claude Code, OpenAI Codex가 함께 작업하는 멀티 루트 워크스페이스입니다.

---

## 🧭 공통 규칙 참조
모든 공통 골든 룰, 상태 머신 정의 및 백로그 CLI 작업 흐름은 [`AGENTS.md`](./AGENTS.md)에 기술되어 있으며 이를 엄격히 준수합니다.

@AGENTS.md

`quant-research` 작업은 `quant-research/AGENTS.md`와 `quant-research/docs/rules.md`를 추가로 읽습니다. 스킬 원본은 `.agents/skills/`, 훅 연결과 미지원 환경의 검사 명령은 [docs/ai-setup.md](docs/ai-setup.md)에 있습니다.

---

## 🛠️ Antigravity 전용 도구 및 스킬 활용
- 백로그 관리 시: `.agents/skills/backlog-manager/` 스킬 활용
- 변경 사항 검증 시: `.agents/skills/adversarial-reviewer/` 스킬 활용
- 도메인별 검증 시:
  - `quant-research`: `.agents/skills/quant-backtest-validator/`
  - `quant-collector`: `.agents/skills/collector-safety-check/`
  - `gym-app`: `.agents/skills/offline-first-reviewer/`
- 작업 완료 알림: 작업 마무리 시 `pwsh tools/notify_voice.ps1 "<메시지>"`를 호출하여 음성(TTS) 안내를 제공합니다.
