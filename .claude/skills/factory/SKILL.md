---
name: factory
description: 요구사항 한 줄을 팩토리 절차(spec → 수락 테스트 → Red 확인 → Builder 구현 → 검증 → Validator → 위험 기반 승인·머지)로 처리한다. 끊긴 세션의 작업을 이어받을 때도 쓴다. 사용자가 "factory", "팩토리로 처리", "팩토리 작업"을 말하거나 요구사항을 팩토리에 넣으라고 할 때 사용한다.
---

# factory (Claude Code 래퍼)

절차의 기준은 `factory/PROCESS.md`다. **먼저 그 문서를 읽고 그대로 따른다.** 이 파일에는 Claude Code에서 다르게 하는 부분만 적는다.

## Claude Code에서의 역할 배치

- 너(메인 세션)가 **오케스트레이터**다. spec·contract·수락 테스트를 쓰고 `python scripts/factory.py` 단계 명령을 실행한다. 구현 코드는 쓰지 않는다 (Spec 단계에서는 hook이 막는다).
- 구현은 **`builder` 서브에이전트**에게 맡긴다. 모델은 `red`·`verify`·`validate`·`status` 출력의 **Builder 모델**(`sonnet` 또는 `opus`)을 그대로 `model`에 넣는다 (실패 2회 뒤 factory.py가 승격하고, hook이 다른 모델 호출을 막는다). 프롬프트에는 아래만 넣는다. 너의 분석이나 구현 방향을 넣지 않는다.
  ```
  job ID: <ID>
  spec: factory/specs/<ID>.md
  수락 테스트: <acceptance_tests 목록>
  (재시도라면) factory.py가 출력한 실패 내용: <원문 그대로>
  ```
- Builder는 **앞에서(foreground) 실행한다**: 호출에 `run_in_background: false`를 꼭 넣는다 (생략하면 백그라운드로 떠서 hook이 막는다). Builder가 끝나 결과를 돌려준 뒤에 `verify`를 실행한다.
- 리뷰어(code / security / test 3명)는 `python scripts/factory.py validate <ID>`가 별도 프로세스로 실행한다. 직접 리뷰하지 않는다.

## 명령 실행 형태

- 레포 루트에서 한 번에 명령 하나만 실행한다. `cd`, `;`, `&&`, `|`를 쓰지 않는다.
- 상태 확인은 `python scripts/factory.py status`로 한다. `factory/state.json`을 직접 읽어 판단하지 않는다.

## 시작할 때

- 먼저 `python scripts/factory.py status`를 실행한다. 진행 중이거나 APPROVED인 작업이 있으면 새 작업을 만들지 말고 `python scripts/factory.py recover <ID>`로 이어받는다 (PROCESS.md 0번).
- 프롬프트가 dispatcher가 배정한 작업 ID를 주고 그 작업이 QUEUED이면, recover 없이 spec 작성부터 진행한다.
- 프롬프트가 충돌로 RETRY_REQUIRED가 된 작업을 이어서 처리하라고 하면, recover 없이 Builder 호출(PROCESS.md 6번)부터 진행한다.
- `scripts/dispatch.py`는 실행하지 않는다 (사람 전용).
- hook이 "다른 세션이 진행 중"이라며 막으면 우회하지 말고 멈추고 보고한다.

## 멈춰야 할 때

- 상태가 `MERGED`, `APPROVAL_REQUIRED`, `BLOCKED` 중 하나가 되면 즉시 멈추고, `python scripts/factory.py status` 결과와 함께 요약을 보고한다.
- `APPROVED`(자동 승인)는 멈추는 상태가 아니다. `python scripts/factory.py merge <ID>`를 실행한다.
- merge가 `RETRY_REQUIRED (CONFLICT)`를 출력하면 멈추지 않는다. 출력된 충돌 파일과 원문을 Builder에게 전달해 6번부터 반복한다.
- merge가 `SPEC_READY (CONFLICT)`를 출력하면 멈추지 않는다. 출력된 `git diff`로 이전 변경을 보고 기존 테스트를 새 base 기준으로 다시 바꾼 뒤 4번(`spec`)부터 반복한다 (이 단계의 테스트 수정은 오케스트레이터가 한다).
- 프롬프트가 충돌로 SPEC_READY가 된 작업을 이어서 처리하라고 하면, recover 없이 PROCESS.md 4번부터 진행한다.
- `approve`, `resume`, `drop`, `recover --force`는 사람 전용이다. 실행하지 말고 사람에게 안내만 한다.
- hook이 도구 호출을 막으면 우회하지 말고, 막힌 이유를 보고에 포함한다.

## 보고 형식

1. 작업 ID, 최종 상태, 위험 등급 (factory.py 출력 그대로)
2. 바뀐 파일 목록 (`git status --short`)
3. Red/Green 판정과 검증 결과 (factory.py 출력 인용)
4. MERGED라면 머지 커밋과 되돌리는 명령 (`git revert -m 1 <머지 커밋>`)
5. APPROVAL_REQUIRED라면 위험 등급 근거와 사람의 다음 명령 (`approve`, `approve --reject`)
6. BLOCKED라면 사유 코드와 사람이 할 수 있는 다음 명령 (`resume`, `resume --extend`, `drop`)
