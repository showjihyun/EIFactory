# 팩토리 절차 (도구 공통)

요구사항 한 줄을 받아, 검증 결과로 완료가 증명된 변경을 **승인 대기(APPROVAL_REQUIRED)** 상태로 올리고, 위험이 낮은(LOW) 변경은 자동으로 머지하는 절차다.
Claude Code·Codex 등 도구 전용 파일은 이 문서를 따르는 얇은 래퍼다.

## 핵심 규칙

- 상태는 `factory/state.json`에만 있고, **`python scripts/factory.py` 명령만** 상태를 바꾼다. 사람이 읽는 요약 `factory-state.md`, 세션 표시 `factory/heartbeat.json`도 직접 고치지 않는다.
- 단계를 넘기는 판단(Red 확인, 검증 통과, 잠긴 파일 변경 여부, 위험 등급, 시간·재시도 상한, 머지 결과)은 `factory.py`가 직접 실행하고 기록한다. 에이전트의 "완료했다"는 말은 근거가 아니다.
- checkout(worktree) 하나에 작업 하나다. 여러 작업은 `scripts/dispatch.py`가 작업마다 worktree를 만들어 동시에 진행한다. 머지는 한 번에 하나씩이다 (merge lock).

## 역할

| 역할 | 하는 일 | 하지 않는 일 |
|---|---|---|
| 오케스트레이터 | 작업 생성, spec·contract·수락 테스트 작성, Builder 호출, `factory.py` 단계 명령 실행 (`merge`, `recover` 포함) | 구현 코드 작성 |
| Builder | 잠긴 수락 테스트를 통과시키는 구현 | 테스트·contract·검증 설정·정책 파일 수정, 상태 명령 실행 |
| Validator | `factory.py validate`가 별도 프로세스로 실행. diff + contract + 검증 로그만 보고 판정 | 코드 수정 |
| 사람 | MEDIUM·HIGH 승인·반려, BLOCKED 처리(`resume`, `drop`), 강제 이어받기(`recover --force`) | - |

Builder는 오케스트레이터의 대화를 넘겨받지 않는다. 호출할 때 job ID, spec 경로, 수락 테스트 경로만 전달한다.

## 상태 머신

```
QUEUED → SPEC_READY → BUILDING → VERIFYING ─┬─ (LOW) ──────────────────────→ APPROVED → MERGED
              ▲  (red 실패, 1회)      ▲      └─ (MEDIUM·HIGH) → APPROVAL_REQUIRED ─┘ (사람 approve)
              └──────┘                └─ RETRY_REQUIRED (verify/validate 실패, 사람 반려)
어느 단계에서든 → BLOCKED (사유 코드) → 사람이 resume 또는 drop
```

| 명령 | 허용 상태 | 하는 일 | 성공 시 | 실패 시 |
|---|---|---|---|---|
| `new "<요구사항>"` | 진행 중 작업 없음, 작업 트리 깨끗함 | job ID 부여, 브랜치 `factory/<ID>` 생성 | QUEUED | - |
| `spec <ID>` | QUEUED, SPEC_READY | `factory/specs/<ID>.md`의 contract 형식 검사 | SPEC_READY | 오류 출력, 상태 유지 |
| `red <ID>` | SPEC_READY | 수락 테스트·바꾼 기존 테스트의 형식·린트 확인 → 수락 테스트 Red 확인 (refactor는 Green). 통과하면 파일 잠금 | BUILDING | 형식·린트 문제: SPEC_READY 유지 (재작성 횟수 안 씀). Red 실패 1회: SPEC_READY로 되돌림, 2회: BLOCKED(`SPEC_INVALID`) |
| `verify <ID>` | BUILDING, RETRY_REQUIRED | 잠긴 파일 대조 → 수락 테스트 Green → 전체 검증 → 위험 등급 계산 → 검증한 변경의 지문 기록 | VERIFYING | RETRY_REQUIRED 또는 BLOCKED |
| `validate <ID>` | VERIFYING | 변경 지문 대조 → Validator 프로세스 실행 | LOW: APPROVED (자동 승인), 그 밖: APPROVAL_REQUIRED | RETRY_REQUIRED 또는 BLOCKED. verify 이후 변경이 있으면 BUILDING |
| `merge <ID>` | APPROVED | 아래 "머지" 순서대로 base 브랜치에 머지 | MERGED | BLOCKED(`CONFLICT`, `MERGE_CHECK_FAILED`, `TREE_CHANGED`) 또는 상태 유지(lock 사용 중) |
| `recover <ID>` | 진행 중, APPROVED | 끊긴 세션의 작업을 이어받는다 (아래 "세션 복구") | 같은 상태 (또는 MERGED) | 이전 세션이 5분 안에 활동했으면 거부 |
| `block <ID> --reason <코드> --detail "..."` | 진행 중 | 수동 BLOCKED (수락 테스트가 틀렸다는 보고, 사람 결정이 필요한 요구사항) | BLOCKED | - |
| `status [--json]` | 언제나 | 현재 상태 출력 | - | - |
| `approve <ID>` / `approve <ID> --reject "..."` | APPROVAL_REQUIRED | **사람 전용.** 승인 기록 후 바로 머지 / 반려 | MERGED / RETRY_REQUIRED | 머지 실패는 `merge`와 같음 |
| `resume <ID> [--extend]` | BLOCKED | **사람 전용.** checkpoint부터 재개, `--extend`는 상한 50% 연장 1회 | checkpoint 상태 | - |
| `recover <ID> --force` | 진행 중, APPROVED | **사람 전용.** 이전 세션이 끝난 것이 확실할 때 5분을 기다리지 않고 이어받기 | 같은 상태 | - |
| `drop <ID>` | 종료 안 된 작업 | **사람 전용.** 작업 종료, 브랜치 보관 | DROPPED | - |

### BLOCKED 사유 코드

| 코드 | 조건 |
|---|---|
| `SPEC_INVALID` | Red 확인 2회 실패 (스펙 재작성은 1회만 허용) |
| `RETRY_LIMIT` | 검증 실패 누적 3회 |
| `REPEATED_FAILURE` | 같은 실패가 2회 연속 |
| `PROTECTED_FILE_CHANGED` | 잠긴 파일(수락 테스트, contract, 기존 테스트, 검증 설정)이 바뀜 |
| `TIMEOUT` | 시간 상한 초과, 또는 소프트 상한(80%) 이후 재시도가 필요해짐 |
| `TEST_INVALID` | Builder가 수락 테스트가 틀렸다고 보고 (`block`으로 기록) |
| `NEEDS_DECISION` | 요구사항 자체가 사람의 결정을 요구함 (예: 검증 약화·테스트 건너뛰기 요청). `new` 직후 `block`으로 기록 |
| `TREE_CHANGED` | 검증·승인한 변경과 머지하려는 변경이 다름 |
| `CONFLICT` | base 브랜치 기준 rebase 또는 merge 충돌 |
| `MERGE_CHECK_FAILED` | rebase 후 재검증(수락 테스트 Green, 전체 검증) 실패 |
| `MANUAL` | 그 밖의 수동 중단 |

## 머지 (`merge`, 사람의 `approve` 뒤에도 같은 순서)

1. merge lock을 잡는다. 다른 작업이 잡고 있으면 상태를 바꾸지 않고 끝낸다 (나중에 다시 `merge`).
2. 작업 트리가 verify 때 기록한 변경 지문과 같은지 확인한다. 다르면 BLOCKED(`TREE_CHANGED`).
3. 자동 승인이면 위험 등급을 diff로 **다시 계산**한다. LOW가 아니면 머지하지 않고 APPROVAL_REQUIRED로 돌린다 (상태 파일이 조작돼도 HIGH 경로는 자동 머지되지 않는다).
4. 작업 브랜치에 변경을 커밋한다.
5. base 브랜치가 앞서 있으면 그 위로 rebase하고 수락 테스트 Green과 전체 검증을 다시 실행한다. 충돌은 BLOCKED(`CONFLICT`), 재검증 실패는 BLOCKED(`MERGE_CHECK_FAILED`). base가 그대로면 검증한 트리와 같으므로 다시 실행하지 않는다.
6. base 브랜치에 `--no-ff` 머지 커밋을 만든다. 커밋이 실제로 base에 들어간 것을 Git으로 확인한 뒤 MERGED로 바꾼다.
7. 되돌릴 때는 출력된 머지 커밋으로 `git revert -m 1 <머지 커밋>` 한 번이면 된다 (사람이 실행).

push는 하지 않는다. 원격 반영은 사람이 한다.

base 브랜치가 다른 worktree(보통 기본 checkout)에 checkout되어 있으면, rebase·재검증은 작업 worktree에서 하고 머지 커밋은 그 base checkout에서 만든다. base checkout에 커밋되지 않은 변경이 있으면 머지하지 않고 상태를 유지한다. 다른 작업이 머지 중이면 lock을 최대 3분 기다린다.

## 병렬 실행 (`scripts/dispatch.py`, 사람 전용)

```
python scripts/dispatch.py --max-parallel 3 "<요구사항 1>" "<요구사항 2>" "<요구사항 3>"
```

1. 기본 checkout이 base 브랜치 위에 있고 깨끗해야 한다 (머지가 이곳에서 일어난다). 실행 중에는 기본 checkout을 고치지 않는다.
2. 요구사항마다 `../<레포>.jobs/<작업 ID>/`에 worktree를 만들고 의존성을 설치한 뒤 `factory.py new --base <base>`로 작업을 만든다. 작업 상태는 worktree마다 따로 있다.
3. 작업마다 headless `claude -p` 세션을 띄운다. 권한은 `.claude/settings.json`의 allow 목록을 그대로 넘긴다 (새 worktree는 신뢰 승인 전이라 프로젝트 설정의 allow가 무시된다. hook은 동작한다).
4. 벽시계 상한(기본 40분)을 넘으면 세션을 프로세스 트리째 종료하고 BLOCKED(`TIMEOUT`)로 기록한다.
5. MERGED 작업의 worktree는 지우고, APPROVAL_REQUIRED·BLOCKED 작업은 남긴다. 사람은 그 worktree에서 `approve`·`resume`·`drop`을 실행한다.
6. 결과는 표로 출력하고 `../<레포>.jobs/dispatch-<시각>.json`에 남긴다. 세션 로그는 `../<레포>.jobs/logs/`.
- 전체 상태: `python scripts/factory.py status --all` (모든 worktree의 작업을 한 표로).
- 인증: `ANTHROPIC_API_KEY` 환경변수가 있으면 API 키, 없으면 로그인 세션(구독). `--require-api-key`로 API 키를 강제할 수 있다. 키 값은 출력·기록하지 않는다.

### 컨테이너 격리 (`--isolation docker`, Phase 3b)

```
python scripts/dispatch.py --isolation docker --max-parallel 3 "<요구사항>" ...
```

- 세션은 컨테이너(`.devcontainer/Dockerfile` 이미지) 안에서 돈다. 인증은 **API 키만** 쓴다 (`ANTHROPIC_API_KEY`, 컨테이너에는 변수 이름만 넘긴다).
- 네트워크: 컨테이너는 내부 전용 네트워크(`factory-internal`)에 있고, 프록시(`docker/proxy`, `factory-proxy`)를 거쳐 `docker/proxy/filter`의 도메인(`api.anthropic.com`)에만 HTTPS로 나간다.
- Git: 공유 `.git`은 읽기 전용으로 마운트한다. 컨테이너는 브랜치·설정·hooks를 바꿀 수 없다. worktree의 `.git` 파일은 컨테이너 경로를 가리키는 파일로 바꿔 끼운다.
- 의존성: 작업별 볼륨에 컨테이너로 설치한다 (이때만 일반 네트워크, 키 없음). Python은 이미지에 들어 있어 실행 중에 내려받지 않는다.
- 머지: 컨테이너 안의 `merge`는 아무것도 하지 않고(APPROVED 유지) 세션이 끝나면 호스트가 머지한다. 호스트는 기본 checkout의 `factory.py`를 쓰고, rebase 후 재검증은 오프라인 컨테이너에서 돌린다. **호스트는 에이전트가 쓴 코드를 실행하지 않는다.**
- `docker/`와 `.devcontainer/`는 잠금 대상이다. 팩토리 작업으로 바꾸면 BLOCKED(`PROTECTED_FILE_CHANGED`).

## 세션 복구 (`recover`)

- 진행 중 작업이 있으면 보호 hook이 도구 호출마다 `factory/heartbeat.json`에 세션 ID와 시각을 남긴다.
- 다른 세션의 heartbeat가 남아 있으면 hook이 수정·명령을 막는다. 같은 작업을 두 세션이 동시에 진행하지 않기 위해서다. `status`와 `recover`만 통과한다.
- 이전 세션의 마지막 활동 후 **5분**이 지나면 `recover <ID>`로 이어받는다. 5분이 안 됐으면 거부된다. 이전 세션이 끝난 것이 확실하면 사람이 `recover <ID> --force`를 실행한다.
- `recover`가 하는 일:
  - 끊긴 시간(마지막 활동 ~ 지금)을 활성 시간에서 뺀다.
  - 이전 세션이 남긴 rebase·merge를 중단하고, 그 작업이 잡은 merge lock을 푼다.
  - 작업 브랜치로 돌아간다. APPROVED인데 Git에 이미 머지가 있으면 MERGED로 바꾼다.
  - 상태(checkpoint)는 그대로 둔다. **진행 중이던 단계는 다시 실행한다.** 다음 명령을 출력한다.
- 단계 명령은 짧고 원자적이다. 상태 파일은 명령이 끝날 때만 쓰이므로, 명령 도중에 끊기면 그 명령은 없었던 것이 되고 다시 실행하면 된다.

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
| `size` | `small` (상한 15분) / `medium` (상한 30분). 중형을 넘으면 작업을 만들지 말고 분할을 제안한다 |
| `risk` | `LOW` / `MEDIUM` / `HIGH` (최종 등급은 `verify`가 diff로 계산한 등급과 비교해 높은 쪽) |
| `acceptance_tests` | 1개 이상. 백엔드 `backend/tests/acceptance/test_<ID>.py`, 프론트엔드 `frontend/src/**/<Name>.<ID>.accept.test.tsx` |
| `changed_tests` | Spec 단계에서 바꾼 기존 테스트 파일 (없으면 `[]`) |
| `allowed_config_changes` | 바꿔도 되는 설정 파일 (예: 의존성 추가 시 `backend/pyproject.toml`). 있으면 위험 등급 MEDIUM 이상 |

## 오케스트레이터 진행 순서

0. `python scripts/factory.py status`로 진행 중 작업이 있는지 본다.
   - dispatcher가 배정한 작업(프롬프트에 작업 ID가 있고 QUEUED)이면 recover 없이 3번부터 진행한다.
   - 그 밖에 진행 중(또는 APPROVED) 작업이 있으면 새 작업을 만들지 않는다. 이전 세션이 끊긴 것이므로 `recover <ID>`로 이어받고, 출력된 다음 단계부터 아래 순서를 이어 간다.
   - `recover`가 "5분이 지난 뒤"라며 거부하면 멈추고 보고한다.
1. 요구사항이 검증을 약하게 하거나(테스트 건너뛰기, 검사 끄기) 그 밖에 사람이 정해야 하는 내용이면, `new` 후 바로 `block <ID> --reason NEEDS_DECISION --detail "<이유>"`로 기록하고 멈춘다.
2. `python scripts/factory.py new "<요구사항>"` → job ID 확인
3. 코드를 읽고 spec 파일과 수락 테스트를 쓴다. 기존 테스트를 바꿔야 하면 지금 바꾸고 `changed_tests`에 적는다. **구현 코드는 쓰지 않는다.**
4. `python scripts/factory.py spec <ID>` → 형식 오류가 있으면 고친다
5. `python scripts/factory.py red <ID>`
   - `BUILDING`이면 다음 단계
   - 형식·린트 문제가 출력되면 그 내용대로 테스트 파일을 고치고 다시 `red` (미리 확인하려면 `python scripts/check_style.py <테스트 경로들>`)
   - `SPEC_READY`로 돌아오면 출력된 이유를 보고 spec·수락 테스트를 **한 번만** 다시 써서 4번부터 반복
   - 이미 구현되어 있어 Red가 안 나는 요구사항이면 억지로 바꾸지 말고 그대로 두어 BLOCKED로 끝낸다
6. Builder를 호출한다 (job ID, spec 경로, 수락 테스트 경로만 전달)
7. `python scripts/factory.py verify <ID>`
   - `RETRY_REQUIRED`면 출력된 실패 내용을 Builder에게 전달해 6번부터 반복
   - Builder가 "수락 테스트가 틀렸다"고 보고하면 `block <ID> --reason TEST_INVALID --detail "..."`
8. `python scripts/factory.py validate <ID>`
   - `RETRY_REQUIRED`면 Validator 지적을 Builder에게 전달해 6번부터 반복
   - `BUILDING`으로 돌아오면 7번부터 다시
9. `APPROVED`(자동 승인)면 `python scripts/factory.py merge <ID>`
   - 다른 작업이 머지 중이라 상태가 그대로면, 잠시 뒤 한 번 더 `merge`. 그래도 안 되면 멈추고 보고
10. `MERGED`, `APPROVAL_REQUIRED`, `BLOCKED` 중 하나가 되면 멈추고 `status`를 보고한다. 승인·재개는 사람이 한다.

## 위험 등급 계산 (verify가 diff로 계산, merge가 자동 승인 작업에 대해 다시 계산)

| 등급 | 조건 | 승인 |
|---|---|---|
| HIGH | `auth.py`·`settings.py`, 정책·절차 파일, CI 설정 변경 / `DELETE FROM`이 새로 추가됨 | 사람 |
| MEDIUM | 의존성·검증 설정 파일 변경 / DB 스키마(`CREATE`·`ALTER TABLE`) 변경 / API 경로 추가·삭제 / 테스트 skip·xfail 추가 / 400줄 또는 15파일 이상 / refactor 유형 | 사람 |
| LOW | 그 밖의 경우 | 자동 (Validator PASS 후 자동 머지) |

최종 등급 = max(contract 선언, 계산 결과).
