# 팩토리 절차 (도구 공통)

요구사항 한 줄을 받아, 검증 결과로 완료가 증명된 변경을 **승인 대기(APPROVAL_REQUIRED)** 상태로 올리는 절차다.
Claude Code·Codex 등 도구 전용 파일은 이 문서를 따르는 얇은 래퍼다.

## 핵심 규칙

- 상태는 `factory/state.json`에만 있고, **`python scripts/factory.py` 명령만** 상태를 바꾼다. 사람이 읽는 요약 `factory-state.md`도 이 명령이 다시 쓴다. 두 파일을 직접 고치지 않는다.
- 단계를 넘기는 판단(Red 확인, 검증 통과, 잠긴 파일 변경 여부, 시간·재시도 상한)은 `factory.py`가 직접 실행하고 기록한다. 에이전트의 "완료했다"는 말은 근거가 아니다.
- 한 번에 작업 하나만 진행한다 (Phase 1).

## 역할

| 역할 | 하는 일 | 하지 않는 일 |
|---|---|---|
| 오케스트레이터 | 작업 생성, spec·contract·수락 테스트 작성, Builder 호출, `factory.py` 단계 명령 실행 | 구현 코드 작성 |
| Builder | 잠긴 수락 테스트를 통과시키는 구현 | 테스트·contract·검증 설정·정책 파일 수정, 상태 명령 실행 |
| Validator | `factory.py validate`가 별도 프로세스로 실행. diff + contract + 검증 로그만 보고 판정 | 코드 수정 |
| 사람 | 승인·반려, BLOCKED 처리(`resume`, `drop`) | - |

Builder는 오케스트레이터의 대화를 넘겨받지 않는다. 호출할 때 job ID, spec 경로, 수락 테스트 경로만 전달한다.

## 상태 머신

```
QUEUED → SPEC_READY → BUILDING → VERIFYING → APPROVAL_REQUIRED → APPROVED
              ▲  (red 실패, 1회)      ▲            │
              └──────┘                └─ RETRY_REQUIRED (verify/validate 실패)
어느 단계에서든 → BLOCKED (사유 코드) → 사람이 resume 또는 drop
```

| 명령 | 허용 상태 | 하는 일 | 성공 시 | 실패 시 |
|---|---|---|---|---|
| `new "<요구사항>"` | 진행 중 작업 없음, 작업 트리 깨끗함 | job ID 부여, 브랜치 `factory/<ID>` 생성 | QUEUED | - |
| `spec <ID>` | QUEUED, SPEC_READY | `factory/specs/<ID>.md`의 contract 형식 검사 | SPEC_READY | 오류 출력, 상태 유지 |
| `red <ID>` | SPEC_READY | 수락 테스트 Red 확인 (refactor는 Green). 통과하면 파일 잠금 | BUILDING | 1회: SPEC_READY로 되돌림, 2회: BLOCKED(`SPEC_INVALID`) |
| `verify <ID>` | BUILDING, RETRY_REQUIRED | 잠긴 파일 대조 → 수락 테스트 Green → 전체 검증 → 위험 등급 계산 | VERIFYING | RETRY_REQUIRED 또는 BLOCKED |
| `validate <ID>` | VERIFYING | Validator 프로세스 실행 | APPROVAL_REQUIRED | RETRY_REQUIRED 또는 BLOCKED |
| `block <ID> --reason <코드> --detail "..."` | 진행 중 | 수동 BLOCKED (예: 수락 테스트가 틀렸다는 보고) | BLOCKED | - |
| `status [--json]` | 언제나 | 현재 상태 출력 | - | - |
| `approve <ID>` / `approve <ID> --reject "..."` | APPROVAL_REQUIRED | **사람 전용.** 승인 기록 (Phase 1은 머지하지 않음) | APPROVED / RETRY_REQUIRED | - |
| `resume <ID> [--extend]` | BLOCKED | **사람 전용.** checkpoint부터 재개, `--extend`는 상한 50% 연장 1회 | checkpoint 상태 | - |
| `drop <ID>` | 종료 안 된 작업 | **사람 전용.** 작업 종료 | DROPPED | - |

### BLOCKED 사유 코드

| 코드 | 조건 |
|---|---|
| `SPEC_INVALID` | Red 확인 2회 실패 (스펙 재작성은 1회만 허용) |
| `RETRY_LIMIT` | 검증 실패 누적 3회 |
| `REPEATED_FAILURE` | 같은 실패가 2회 연속 |
| `PROTECTED_FILE_CHANGED` | 잠긴 파일(수락 테스트, contract, 기존 테스트, 검증 설정)이 바뀜 |
| `TIMEOUT` | 시간 상한 초과, 또는 소프트 상한(80%) 이후 재시도가 필요해짐 |
| `TEST_INVALID` | Builder가 수락 테스트가 틀렸다고 보고 (`block`으로 기록) |
| `MANUAL` | 그 밖의 수동 중단 |

## spec 파일 형식

`factory/specs/<ID>.md`에 설명을 자유롭게 쓰고, 아래 형식의 contract를 **```json 코드 블록 하나**로 넣는다.

```json
{
  "type": "feature",
  "size": "small",
  "risk": "LOW",
  "risk_reason": "화면 문구만 바뀜",
  "acceptance_criteria": ["빈 목록에서 '첫 북마크를 추가해 보세요'가 보인다"],
  "acceptance_tests": ["frontend/src/components/BookmarkList.J001.accept.test.tsx"],
  "changed_tests": ["frontend/src/components/BookmarkList.test.tsx"],
  "allowed_config_changes": []
}
```

| 필드 | 값 |
|---|---|
| `type` | `feature` / `bugfix` / `copy` / `refactor` |
| `size` | `small` (상한 20분) / `medium` (상한 30분). 중형을 넘으면 작업을 만들지 말고 분할을 제안한다 |
| `risk` | `LOW` / `MEDIUM` / `HIGH` (최종 등급은 `verify`가 diff로 계산한 등급과 비교해 높은 쪽) |
| `acceptance_tests` | 1개 이상. 백엔드 `backend/tests/acceptance/test_<ID>.py`, 프론트엔드 `frontend/src/**/<Name>.<ID>.accept.test.tsx` |
| `changed_tests` | Spec 단계에서 바꾼 기존 테스트 파일 (없으면 `[]`) |
| `allowed_config_changes` | 바꿔도 되는 설정 파일 (예: 의존성 추가 시 `backend/pyproject.toml`). 있으면 위험 등급 MEDIUM 이상 |

## 오케스트레이터 진행 순서

1. `python scripts/factory.py new "<요구사항>"` → job ID 확인
2. 코드를 읽고 spec 파일과 수락 테스트를 쓴다. 기존 테스트를 바꿔야 하면 지금 바꾸고 `changed_tests`에 적는다. **구현 코드는 쓰지 않는다.**
3. `python scripts/factory.py spec <ID>` → 형식 오류가 있으면 고친다
4. `python scripts/factory.py red <ID>`
   - `BUILDING`이면 다음 단계
   - `SPEC_READY`로 돌아오면 출력된 이유를 보고 spec·수락 테스트를 **한 번만** 다시 써서 3번부터 반복
   - 이미 구현되어 있어 Red가 안 나는 요구사항이면 억지로 바꾸지 말고 그대로 두어 BLOCKED로 끝낸다
5. Builder를 호출한다 (job ID, spec 경로, 수락 테스트 경로만 전달)
6. `python scripts/factory.py verify <ID>`
   - `RETRY_REQUIRED`면 출력된 실패 내용을 Builder에게 전달해 5번부터 반복
   - Builder가 "수락 테스트가 틀렸다"고 보고하면 `block <ID> --reason TEST_INVALID --detail "..."`
7. `python scripts/factory.py validate <ID>`
   - `RETRY_REQUIRED`면 Validator 지적을 Builder에게 전달해 5번부터 반복
8. `APPROVAL_REQUIRED` 또는 `BLOCKED`가 되면 멈추고 `status`를 보고한다. 승인·재개는 사람이 한다.

## 위험 등급 계산 (verify가 diff로 계산)

| 등급 | 조건 |
|---|---|
| HIGH | `auth.py`·`settings.py`, 정책·절차 파일, CI 설정 변경 / `DELETE FROM`이 새로 추가됨 |
| MEDIUM | 의존성·검증 설정 파일 변경 / DB 스키마(`CREATE`·`ALTER TABLE`) 변경 / API 경로 추가·삭제 / 테스트 skip·xfail 추가 / 400줄 또는 15파일 이상 / refactor 유형 |
| LOW | 그 밖의 경우 |

최종 등급 = max(contract 선언, 계산 결과). Phase 1은 등급과 관계없이 전부 사람이 승인한다.
