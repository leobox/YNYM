# CLAUDE.md — Claude Code 협업 가이드

이 프로젝트는 Gemini(Antigravity), OpenAI Codex / Copilot과 함께 협업하는 멀티 루트 워크스페이스입니다.

상세 골든 룰 및 아키텍처는 [`AGENTS.md`](./AGENTS.md)를 따릅니다.

@AGENTS.md

`quant-research` 작업은 `quant-research/AGENTS.md`와 `quant-research/docs/rules.md`를 읽습니다. 스킬 원본은 `.agents/skills/`, 연결 안내는 [docs/ai-setup.md](docs/ai-setup.md)입니다.

---

## ⚡ 빠른 명령어 요약

```bash
# 백로그 확인 및 착수
node tools/backlog.mjs next
node tools/backlog.mjs set <id> in_progress

# 상태 변경 완료 시
node tools/backlog.mjs set <id> done

# 작업 추가 시
node tools/backlog.mjs add --title "제목" --category quant_research --phase "1. 퀀트..."
```

---

## 🚨 핵심 주의사항
1. `backlog.json`을 직접 수정하지 마십시오. 반드시 `tools/backlog.mjs`를 통해서만 변경합니다.
2. 코드를 작성하기 전에 반드시 `node tools/backlog.mjs set <id> in_progress`를 실행하십시오.
3. 퀀트 프로젝트(`quant-research`, `quant-collector`)에서는 주문/매매 API를 절대 구현하지 않습니다.
4. 사용자 결정이 필요한 사항은 `needs_decision` 상태와 사유(`--note`)를 명시하십시오.
