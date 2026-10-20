@AGENTS.md

## Claude Code 전용

- 공통 규칙은 위 `AGENTS.md`를 따른다. 이 섹션에는 Claude Code에만 해당하는 내용만 적는다.
- 팩토리 작업은 `factory` 스킬(`.claude/skills/factory/`)로 실행한다. 구현은 `builder` 서브에이전트가 맡는다.
- `.claude/settings.json`의 PreToolUse hook(`scripts/hooks/guard.py`)이 단계에 맞지 않는 수정을 막는다. 막히면 우회하지 말고 보고한다.
