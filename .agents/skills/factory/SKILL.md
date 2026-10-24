---
name: factory
description: 요구사항 한 줄을 팩토리 절차(spec → 수락 테스트 → Red 확인 → Builder 구현 → 검증 → 리뷰어 3명 → 위험 기반 승인·머지)로 처리한다. 끊긴 세션의 작업을 이어받을 때도 쓴다. 사용자가 "factory", "팩토리로 처리", "팩토리 작업"을 말하거나 dispatcher가 작업 ID를 배정했을 때 사용한다.
---

# factory (Codex 래퍼)

절차의 기준은 `factory/PROCESS.md`다. **먼저 그 문서를 읽고 그대로 따른다.** 이 파일에는 Codex에서 다르게 하는 부분만 적는다. Claude Code 래퍼는 `.claude/skills/factory/SKILL.md`다.

## Codex에서의 역할 배치

- 너(메인 에이전트)가 **오케스트레이터**다. spec·contract·수락 테스트를 쓰고 `python scripts/factory.py` 단계 명령을 실행한다. 구현 코드는 쓰지 않는다 (Spec 단계에서는 hook이 막는다).
- 구현은 **Builder 하위 에이전트**에게 맡긴다. `spawn_agent`를 아래 값으로 부른다. 다른 값이면 hook이 막는다.
  - `task_name`: `builder` (재시도는 `builder_2`처럼 `builder`로 시작)
  - `model`: `red`·`verify`·`validate`·`status` 출력의 **Builder 모델** 그대로 (실패 2회 뒤 factory.py가 승격한다)
  - `fork_turns`: `"none"` (Builder는 너의 대화를 넘겨받지 않는다)
  - `message`: 아래만 넣는다. 너의 분석이나 구현 방향을 넣지 않는다.
    ```
    factory/prompts/builder.md를 읽고 그대로 따른다.
    job ID: <ID>
    spec: factory/specs/<ID>.md
    수락 테스트: <acceptance_tests 목록>
    (재시도라면) factory.py가 출력한 실패 내용: <원문 그대로>
    ```
  - 띄운 뒤 `wait_agent`로 끝날 때까지 기다리고, Builder의 보고를 받은 다음 `verify`로 넘어간다.
- 리뷰어(code / security / test 3명)는 `python scripts/factory.py validate <ID>`가 별도 프로세스로 실행한다. 직접 리뷰하지 않는다.

## 명령 실행 형태

- 레포 루트에서 한 번에 명령 하나만 실행한다. `cd`, `;`, `&&`, `|`를 쓰지 않는다.
- 파일 수정은 `apply_patch`로 한다. 셸 리다이렉션(`>`)으로 파일을 쓰지 않는다.
- 상태 확인은 `python scripts/factory.py status`로 한다. `factory/state.json`을 직접 읽어 판단하지 않는다.

## 시작할 때

- 먼저 `python scripts/factory.py status`를 실행한다. 진행 중이거나 APPROVED인 작업이 있으면 새 작업을 만들지 말고 `python scripts/factory.py recover <ID>`로 이어받는다 (PROCESS.md 0번).
- 프롬프트가 dispatcher가 배정한 작업 ID를 주고 그 작업이 QUEUED이면, recover 없이 spec 작성부터 진행한다.
- `scripts/dispatch.py`는 실행하지 않는다 (사람 전용).
- hook이 "다른 세션이 진행 중"이라며 막으면 우회하지 말고 멈추고 보고한다.

## 멈춰야 할 때

- 상태가 `MERGED`, `APPROVAL_REQUIRED`, `BLOCKED` 중 하나가 되면 즉시 멈추고, `python scripts/factory.py status` 결과와 함께 요약을 보고한다.
- `APPROVED`(자동 승인)는 멈추는 상태가 아니다. `python scripts/factory.py merge <ID>`를 실행한다. 컨테이너 세션이면 merge가 "머지는 호스트가 합니다"라고 출력하고, 그때 멈춘다.
- merge가 `RETRY_REQUIRED (CONFLICT)`를 출력하면 멈추지 않는다. 출력된 충돌 파일과 원문을 Builder에게 전달해 6번부터 반복한다.
- 프롬프트가 충돌로 RETRY_REQUIRED가 된 작업을 이어서 처리하라고 하면, recover 없이 Builder 호출(PROCESS.md 6번)부터 진행한다.
- merge가 `SPEC_READY (CONFLICT)`를 출력하거나 프롬프트가 충돌로 SPEC_READY가 된 작업을 이어서 처리하라고 하면, 출력된 `git diff`로 이전 변경을 보고 기존 테스트를 새 base 기준으로 다시 바꾼 뒤 PROCESS.md 4번(`spec`)부터 진행한다.
- `approve`, `resume`, `drop`, `recover --force`는 사람 전용이다. 실행하지 말고 사람에게 안내만 한다.
- hook이 도구 호출을 막으면 우회하지 말고, 막힌 이유를 보고에 포함한다.

## 보고 형식

`.claude/skills/factory/SKILL.md`의 "보고 형식"과 같다 (작업 ID·상태·위험 등급, 바뀐 파일, Red/Green·검증 결과, 머지 커밋 또는 사람의 다음 명령).
