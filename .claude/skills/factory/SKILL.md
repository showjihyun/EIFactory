---
name: factory
description: 요구사항 한 줄을 팩토리 절차(spec → 수락 테스트 → Red 확인 → Builder 구현 → 검증 → Validator)로 처리해 승인 대기 상태까지 올린다. 사용자가 "factory", "팩토리로 처리", "팩토리 작업"을 말하거나 요구사항을 팩토리에 넣으라고 할 때 사용한다.
---

# factory (Claude Code 래퍼)

절차의 기준은 `factory/PROCESS.md`다. **먼저 그 문서를 읽고 그대로 따른다.** 이 파일에는 Claude Code에서 다르게 하는 부분만 적는다.

## Claude Code에서의 역할 배치

- 너(메인 세션)가 **오케스트레이터**다. spec·contract·수락 테스트를 쓰고 `python scripts/factory.py` 단계 명령을 실행한다. 구현 코드는 쓰지 않는다 (Spec 단계에서는 hook이 막는다).
- 구현은 **`builder` 서브에이전트**에게 맡긴다. 프롬프트에는 아래만 넣는다. 너의 분석이나 구현 방향을 넣지 않는다.
  ```
  job ID: <ID>
  spec: factory/specs/<ID>.md
  수락 테스트: <acceptance_tests 목록>
  (재시도라면) factory.py가 출력한 실패 내용: <원문 그대로>
  ```
- Validator는 `python scripts/factory.py validate <ID>`가 별도 프로세스로 실행한다. 직접 리뷰하지 않는다.

## 명령 실행 형태

- 레포 루트에서 한 번에 명령 하나만 실행한다. `cd`, `;`, `&&`, `|`를 쓰지 않는다.
- 상태 확인은 `python scripts/factory.py status`로 한다. `factory/state.json`을 직접 읽어 판단하지 않는다.

## 멈춰야 할 때

- 상태가 `APPROVAL_REQUIRED` 또는 `BLOCKED`가 되면 즉시 멈추고, `python scripts/factory.py status` 결과와 함께 요약을 보고한다.
- `approve`, `resume`, `drop`은 사람 전용이다. 실행하지 말고 사람에게 안내만 한다.
- hook이 도구 호출을 막으면 우회하지 말고, 막힌 이유를 보고에 포함한다.

## 보고 형식

1. 작업 ID, 최종 상태, 위험 등급 (factory.py 출력 그대로)
2. 바뀐 파일 목록 (`git status --short`)
3. Red/Green 판정과 검증 결과 (factory.py 출력 인용)
4. BLOCKED라면 사유 코드와 사람이 할 수 있는 다음 명령 (`resume`, `resume --extend`, `drop`)
