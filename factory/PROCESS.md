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
| Builder | 잠긴 수락 테스트를 통과시키는 구현 (`factory/prompts/builder.md`) | 테스트·contract·검증 설정·정책 파일 수정, 상태 명령 실행 |
| 리뷰어 3명 | `factory.py validate`가 별도 프로세스로 동시에 실행. diff + contract + 검증 로그만 보고 각자 관점으로 판정 (code: 요구사항·범위·버그, security: 보안·위험 등급, test: 테스트 신뢰성) | 코드 수정, 승인 결정 |
| Approver | 모델이 아니라 `factory.py`의 규칙. 리뷰어 전원 PASS + 위험 LOW면 자동 승인, 그 밖에는 승인 요약(`factory/reviews/<ID>-approval.md`)을 쓰고 사람을 기다린다 | - |
| 사람 | MEDIUM·HIGH 승인·반려, BLOCKED 처리(`resume`, `drop`), 강제 이어받기(`recover --force`) | - |

Builder는 오케스트레이터의 대화를 넘겨받지 않는다. 호출할 때 job ID, spec 경로, 수락 테스트 경로만 전달한다.

**Builder 모델 (승격)**: Builder는 경량 모델로 시작한다. 검증·리뷰 실패가 2회 쌓이면 `factory.py`가 상위 모델로 올린다 (Claude Code: sonnet → opus, Codex: gpt-6.1-sol → gpt-6-astra). `red`·`verify`·`validate`·`status`가 "Builder 모델"을 출력하고, 오케스트레이터는 그 모델로 Builder를 부른다. 다른 모델로 부르면 hook이 막는다.

## 상태 머신

```
QUEUED → SPEC_READY → BUILDING → VERIFYING ─┬─ (LOW) ──────────────────────→ APPROVED → MERGED
              ▲  (red 실패, 1회)      ▲      └─ (MEDIUM·HIGH) → APPROVAL_REQUIRED ─┘ (사람 approve)
              └──────┘                └─ RETRY_REQUIRED (verify/validate 실패, 사람 반려, 머지 때 rebase 충돌)
어느 단계에서든 → BLOCKED (사유 코드) → 사람이 resume 또는 drop
```

| 명령 | 허용 상태 | 하는 일 | 성공 시 | 실패 시 |
|---|---|---|---|---|
| `new "<요구사항>"` | 진행 중 작업 없음, 작업 트리 깨끗함 | job ID 부여, 브랜치 `factory/<ID>` 생성 | QUEUED | - |
| `spec <ID>` | QUEUED, SPEC_READY | `factory/specs/<ID>.md`의 contract 형식 검사 | SPEC_READY | 오류 출력, 상태 유지 |
| `red <ID>` | SPEC_READY | 수락 테스트·바꾼 기존 테스트의 형식·린트 확인 → 수락 테스트 Red 확인 (refactor는 Green). 통과하면 파일 잠금 | BUILDING | 형식·린트 문제: SPEC_READY 유지 (재작성 횟수 안 씀). Red 실패 1회: SPEC_READY로 되돌림, 2회: BLOCKED(`SPEC_INVALID`) |
| `verify <ID>` | BUILDING, RETRY_REQUIRED | 잠긴 파일 대조 → 충돌 표시(`<<<<<<<`, `>>>>>>>`)가 남은 파일 확인 → 수락 테스트 Green → 전체 검증 → 위험 등급 계산 → 검증한 변경의 지문 기록 | VERIFYING | RETRY_REQUIRED 또는 BLOCKED |
| `validate <ID>` | VERIFYING | 변경 지문 대조 → 리뷰어 3명 동시 실행 (하나라도 FAIL이면 FAIL, 판정을 못 읽으면 상태 유지) | LOW: APPROVED (자동 승인), 그 밖: APPROVAL_REQUIRED + 승인 요약 | RETRY_REQUIRED 또는 BLOCKED. verify 이후 변경이 있으면 BUILDING |
| `merge <ID>` | APPROVED | 아래 "머지" 순서대로 base 브랜치에 머지 | MERGED | RETRY_REQUIRED(rebase 충돌, 새 base 위에 다시 펼침), BLOCKED(`CONFLICT`, `MERGE_CHECK_FAILED`, `TREE_CHANGED`) 또는 상태 유지(lock 사용 중) |
| `recover <ID>` | 진행 중, APPROVED | 끊긴 세션의 작업을 이어받는다 (아래 "세션 복구") | 같은 상태 (또는 MERGED) | 이전 세션이 5분 안에 활동했으면 거부 |
| `block <ID> --reason <코드> --detail "..."` | 진행 중 | 수동 BLOCKED (수락 테스트가 틀렸다는 보고, 사람 결정이 필요한 요구사항) | BLOCKED | - |
| `status [--json]` | 언제나 | 현재 상태 출력 | - | - |
| `approve <ID>` / `approve <ID> --reject "..."` | APPROVAL_REQUIRED | **사람 전용.** 승인 기록 후 바로 머지 / 반려 | MERGED / RETRY_REQUIRED | 머지 실패는 `merge`와 같음 |
| `resume <ID> [--extend]` | BLOCKED | **사람 전용.** checkpoint부터 재개, `--extend`는 상한 50% 연장 1회 | checkpoint 상태 | - |
| `recover <ID> --force` | 진행 중, APPROVED | **사람 전용.** 이전 세션이 끝난 것이 확실할 때 5분을 기다리지 않고 이어받기 | 같은 상태 | - |
| `drop <ID>` | 종료 안 된 작업 | **사람 전용.** 작업 종료, 브랜치 보관 | DROPPED | - |

### 대시보드 (`scripts/monitor.py`, 읽기 전용 + API 키 입력)

```
python scripts/monitor.py            # http://127.0.0.1:8765 를 브라우저로 연다
python scripts/monitor.py --no-open --port 8800 --jobs-dir ../<레포>.jobs
```

- 나를 기다리는 일(승인 대기·막힘·오류), 요약(성공률·개입률·리드타임·비용), 작업 레인(같은 파일 충돌 예고 ⚡), 보드, 작업 상세(다음 명령·검증 증거·호스트 기록·리뷰 원문·모델·인증·역할별 토큰과 비용·단계별 ≈·이력·최근 활동), ⚙ 설정(모델·인증·상한은 읽기 전용, API 키 입력).
- `127.0.0.1`에만 열고 GET만 받는다. 상태를 바꾸지 않고, 명령은 복사용 텍스트로만 보인다. 승인은 `dispatch.py --approve`, 설정은 `factory_settings.py`.
- **API 키 입력** (⚙ 설정 → 🔑 API 키): 대시보드가 바꾸는 유일한 값. `ANTHROPIC_API_KEY`, `CODEX_API_KEY`, `OPENAI_API_KEY`를 Windows 사용자 환경변수(`HKCU\Environment`, `setx`와 같은 곳)에 저장·삭제한다. 새로 연 터미널에서 실행한 dispatch부터 적용된다.
  - `POST /api/settings/api-key`만 받는다: 같은 출처(`Origin`) + **입력 코드**(`X-Factory-Token`) + JSON 본문(4KB 이하). 이름은 세 가지만, 값은 접두어(`sk-ant-` / `sk-`)와 글자 종류·길이를 검사한다.
  - 입력 코드는 `monitor.py`를 띄울 때마다 새로 만들어 **그 터미널에만** 출력한다 (`API 키 입력 코드: XXXXX-XXXXX`). 화면·API로는 내보내지 않는다. 다섯 번 틀리면 다시 켤 때까지 잠근다.
  - 값은 응답·화면·로그 어디에도 다시 내보내지 않는다 ("설정됨 / 없음"만). 입력칸은 가려진 입력이고, 보낸 뒤 바로 비운다.
  - Windows 밖에서는 저장하지 않고 셸 설정에 넣으라고 안내한다.
  - 같은 PC의 다른 프로세스는 입력 코드를 HTTP로 받을 수 없으므로 화면처럼 요청을 만들어도 키를 바꾸거나 지울 수 없다 (코드 50비트, 5회 잠금). 남은 위험: 키 값은 평문 사용자 환경변수다 (`setx`와 같은 수준, 같은 사용자의 프로세스는 읽을 수 있음).
- 값마다 출처를 표시한다: ⚠ 세션 기록(worktree 안, 위조 가능) / 🏠 호스트(`meta/`, dispatch) / ✓ Git 확인. 진행 중 비용은 ≈(추정), 끝나면 결과 이벤트의 확정값.
- 화면 시험: `node scripts/ui/check_monitor.cjs` (`verify.py`의 `scripts ui` 단계, 작업 검증에서는 scripts 영역과 함께 건너뜀).
- 설계: sw-factory `기획안/dashboard/` (설계안, DESIGN.md, 작업분해).

### 단계별 모델·인증 (`factory/config.json`의 `stages`)

| 단계 | 자동(auto) 모델 | 인증 |
|---|---|---|
| `orchestrator` (dispatch 세션) | Opus 5.5 | auto·subscription·api |
| `builder` (서브에이전트) | Sonnet 5.5, 실패 2회 뒤 Opus 5.5 | 오케스트레이터를 따름 (auto만) |
| `reviewer_code`, `reviewer_test` | Sonnet 5.5 | auto·subscription·api |
| `reviewer_security` | Sonnet 5.5, HIGH 위험이면 Opus 5.5 | auto·subscription·api |

- 모델은 Claude 최신 세 가지만 (`haiku`=claude-haiku-5-5, `sonnet`=claude-sonnet-5-5, `opus`=claude-opus-5-5). 우선순위: `FACTORY_MODEL_<단계>` 환경변수 > 설정 > 자동. 오케스트레이터는 `dispatch.py --model`이 가장 우선.
- 사람이 정한 Builder 모델은 실패가 쌓여도 승격하지 않는다. Builder 모델은 `new` 때 정해진다 (진행 중 작업은 그대로).
- 인증 auto = `ANTHROPIC_API_KEY`가 있으면 API, 없으면 구독. 오케스트레이터가 구독이면 dispatch가 키를 세션에서 숨기고(`FACTORY_ANTHROPIC_API_KEY_STASH`), api로 정한 리뷰어에만 되돌려 준다. 키 값은 출력·기록하지 않는다.
- 컨테이너 격리는 API 키만 쓴다: 오케스트레이터·리뷰어를 subscription으로 두면 dispatch가 시작하지 않는다.
- 쓴 모델·인증과 이유는 작업 기록 `models`(단계별 `model`, `source`, `reason`, `auth`)와 dispatch 결과 `orchestrator`, `meta/<ID>/session.json`에 남는다.
- 바꾸기: `python scripts/factory_settings.py show` / `set <단계> model|auth <값>` (사람 전용, guard·권한 deny로 막음).

### 작업 검증 범위 (`factory/config.json`의 `job_verify`)

`verify`와 머지 전 재검증은 작업 diff(기준점 대비, 추적 안 되는 새 파일 포함)를 보고 검증 범위를 정한다. diff에 `full_if_changed` 경로나 키트 파일(`scripts/`, 정책·절차 파일, 도구 래퍼, `docker/`, `.devcontainer/`)이 없으면 `args`(샘플 레포: `--skip scripts`)를 붙여 팩토리 자체 테스트를 건너뛴다. 작업은 `scripts/`를 바꿀 수 없으므로(잠금) 그 결과가 작업마다 같기 때문이다. 하나라도 있으면 전체를 검증한다. 붙인 인자는 검증 기록의 `verify_args`에 남는다. 사람이 `scripts/`를 고칠 때는 `python scripts/verify.py` 전체를 직접 돌린다.

### BLOCKED 사유 코드

| 코드 | 조건 |
|---|---|
| `SPEC_INVALID` | Red 확인 2회 실패 (스펙 재작성은 1회만 허용) |
| `RETRY_LIMIT` | 검증 실패 누적 3회 |
| `REPEATED_FAILURE` | 같은 실패가 2회 연속 |
| `PROTECTED_FILE_CHANGED` | 잠긴 파일(수락 테스트, contract, 기존 테스트, 검증 설정)이 바뀜 |
| `TIMEOUT` | 시간 상한 초과, 또는 소프트 상한(80%) 이후 재시도가 필요해짐 |
| `BUDGET` | 세션 비용이 상한에 닿아 세션이 멈춤 (dispatch가 기록) |
| `SESSION_ENDED` | dispatch 세션이 끝났는데 작업이 진행 중 상태로 남음 (예: Builder를 기다리지 못하고 끝남). dispatch가 기록 |
| `TEST_INVALID` | Builder가 수락 테스트가 틀렸다고 보고 (`block`으로 기록) |
| `NEEDS_DECISION` | 요구사항 자체가 사람의 결정을 요구함 (예: 검증 약화·테스트 건너뛰기 요청). `new` 직후 `block`으로 기록 |
| `TREE_CHANGED` | 검증·승인한 변경과 머지하려는 변경이 다름 |
| `CONFLICT` | 잠긴 파일(spec·수락 테스트·검증 설정)의 rebase 충돌, 작업이 바꾼 기존 테스트의 두 번째 충돌, 또는 merge 실패. 잠기지 않은 파일의 충돌은 RETRY_REQUIRED로 Builder가, 작업이 바꾼 기존 테스트의 첫 충돌은 SPEC_READY로 오케스트레이터가 다시 한다 |
| `MERGE_CHECK_FAILED` | rebase 후 재검증(수락 테스트 Green, 전체 검증) 실패 |
| `MANUAL` | 그 밖의 수동 중단 |

## 머지 (`merge`, 사람의 `approve` 뒤에도 같은 순서)

1. merge lock을 잡는다. 다른 작업이 잡고 있으면 상태를 바꾸지 않고 끝낸다 (나중에 다시 `merge`).
2. 작업 트리가 verify 때 기록한 변경 지문과 같은지 확인한다. 다르면 BLOCKED(`TREE_CHANGED`).
3. 자동 승인이면 위험 등급을 diff로 **다시 계산**한다. LOW가 아니면 머지하지 않고 APPROVAL_REQUIRED로 돌린다 (상태 파일이 조작돼도 HIGH 경로는 자동 머지되지 않는다).
4. 작업 브랜치에 변경을 커밋한다.
5. base 브랜치가 앞서 있으면 그 위로 rebase하고 수락 테스트 Green과 전체 검증을 다시 실행한다. 재검증 실패는 BLOCKED(`MERGE_CHECK_FAILED`). base가 그대로면 검증한 트리와 같으므로 다시 실행하지 않는다.
   - **rebase 충돌** (먼저 머지된 작업이 같은 줄을 고침): rebase를 되돌리고, 작업 변경을 base 끝 위에 다시 펼친다 (커밋하지 않은 변경, 충돌 파일에는 충돌 표시). 승인·검증 기록은 무효가 되고 RETRY_REQUIRED가 된다 (실패 횟수 1회로 센다. 같은 파일이 다시 충돌하면 `REPEATED_FAILURE`). Builder가 두 변경을 모두 살려 충돌을 풀고 verify부터 다시 한다. 다시 펼치기 전 작업 커밋은 작업 기록 `replays`에 남는다.
   - base가 깔끔하게 바꾼 잠긴 파일은 작업의 변경이 아니므로 다시 펼친 내용으로 다시 잠근다.
   - **이 작업이 바꾼 기존 테스트(`changed_tests`)만 충돌하면** Spec 단계부터 다시 한다: 새 base 끝 위에 spec·수락 테스트만 남기고 구현과 기존 테스트 변경은 뺀다 → SPEC_READY. 오케스트레이터가 기존 테스트를 새 base 기준으로 다시 바꾸고(이전 변경은 출력된 `git diff` 명령으로 본다) `spec` → `red`(새 base에서 Red 다시 확인) → Builder. 잠긴 테스트를 Builder가 고치지 않는 원칙은 그대로다. 한 작업에 한 번만, 다시 충돌하면 BLOCKED(`CONFLICT`).
   - 그 밖의 잠긴 파일(spec, 수락 테스트, 검증 설정)이 충돌하면 작업 브랜치를 되돌리고 BLOCKED(`CONFLICT`)로 사람에게 넘긴다.
   - MEDIUM·HIGH 작업은 고친 뒤 다시 사람 승인을 받는다 (승인한 diff와 달라졌으므로).
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
   - 비용 상한 (`--budget-usd`, 기본 $5, API 환산 임시값): Claude 세션에 `--max-budget-usd`로 건다. 상한에 닿아 세션이 작업 도중 멈추면 BLOCKED(`BUDGET`). 리뷰어 호출은 하나당 $1 (`FACTORY_REVIEW_BUDGET_USD`). Codex CLI에는 같은 옵션이 없어 벽시계 상한만 적용되고, 토큰 수는 기록한다.
   - 세션 비용·토큰은 결과 표와 `dispatch-<시각>.json`에 남는다.
5. 세션이 끝났는데 작업이 rebase 충돌로 RETRY_REQUIRED이면(호스트 머지, `--approve`, 세션이 멈춘 경우) 같은 worktree에서 세션을 이어서 띄운다 (최대 2회, 로그 `logs/<ID>.r<n>.jsonl`, 호스트 기록 `continue`). 이어서 처리한 세션 비용도 작업 비용에 들어간다. 사람이 반려한 작업도 같은 길로 이어서 고친다. 새 세션 전에 호스트가 `recover --force`로 끝난 세션을 이어받는다 (heartbeat 정리, 세션 없이 흐른 시계 되돌림). 이어서 처리를 다 쓰고도 RETRY_REQUIRED이면 BLOCKED(`SESSION_ENDED`)로 둔다 (factory-v7).
6. MERGED 작업의 worktree는 지우고, APPROVAL_REQUIRED·BLOCKED 작업은 남긴다.
   - 승인 대기는 **기본 checkout에서** `python scripts/dispatch.py --approve <ID>` (반려 `--reject <ID> --reason "..."`). 작업 worktree의 `scripts/`와 상태 파일은 세션이 바꿀 수 있으므로 그 worktree의 `factory.py approve`는 쓰지 않는다. 승인 요약(`factory/reviews/<ID>-approval.md`)도 세션이 쓴 파일이니 diff(`git -C <worktree> diff <base>`)를 직접 본다.
   - BLOCKED는 **기본 checkout에서** `python scripts/dispatch.py --continue <ID> [--reason "지시"] [--extend]`로 멈춘 단계부터 세션 하나로 이어서 처리한다 (resume + 이어서 처리, RETRY_REQUIRED도 받음, factory-v7). 버릴 때는 그 worktree에서 `drop`.
   - 리뷰 원문과 승인 요약은 기록할 때마다 `../<레포>.jobs/meta/<ID>/reviews/`에 복사해 worktree를 지운 뒤에도 남긴다 (factory-v7).
   - 호스트 머지(컨테이너·Codex 세션)와 `--approve`는 상태 파일을 믿지 않는다 (`FACTORY_UNTRUSTED_STATE=1`): 설정은 기본 checkout에서 읽고, 수락 테스트 목록은 diff에서도 찾고, base가 그대로여도 수락 테스트 Green과 `verify`를 다시 돌린다 (컨테이너 작업이면 오프라인 컨테이너에서). 잠긴 파일(기존 테스트, 검증 설정)이 바뀐 자동 승인 작업은 머지하지 않고 사람 승인으로 돌린다.
   - 세션 정보(격리 방식, 이미지)는 worktree 밖 `../<레포>.jobs/meta/<ID>/session.json`에 둔다.
7. 결과는 표로 출력하고 `../<레포>.jobs/dispatch-<시각>.json`에 남긴다. 세션 로그는 `../<레포>.jobs/logs/`.
- 전체 상태: `python scripts/factory.py status --all` (모든 worktree의 작업을 한 표로).
- 인증: `ANTHROPIC_API_KEY` 환경변수가 있으면 API 키, 없으면 로그인 세션(구독). `--require-api-key`로 API 키를 강제할 수 있다. 키 값은 출력·기록하지 않는다.
- 세션에는 `FACTORY_AGENT_SESSION=1`이 들어가고, 그 세션 안에서는 `factory.py`가 사람 전용 명령을 거부한다 (hook과 별개).

### 에이전트 도구 (`--agent claude|codex`, Phase 3c)

```
python scripts/dispatch.py --agent codex --isolation docker "<요구사항>" ...
python scripts/dispatch.py --agent claude --review-agent codex "<요구사항>"   # 리뷰어만 다른 도구
```

- 절차·상태·검증·hook(`scripts/hooks/guard.py`)은 같다. 래퍼만 다르다.

  | | Claude Code | Codex |
  |---|---|---|
  | 스킬 | `.claude/skills/factory/SKILL.md` | `.agents/skills/factory/SKILL.md` |
  | Builder | `builder` 서브에이전트 (`model`) | `spawn_agent` (`task_name: builder`, `model`, `fork_turns: none`) |
  | hook | `.claude/settings.json` (막으면 종료 코드 2) | `.codex/hooks.json` (`--json-deny`: 종료 코드 0 + 판정 JSON) |
  | 세션 | `claude -p` | `codex exec --json` (프로젝트 신뢰 설정 + hook 신뢰 확인 생략을 dispatcher가 넘긴다) |
  | 리뷰어 | `claude -p` (읽기 도구만) | `codex exec --sandbox read-only` |
  | 인증 | `ANTHROPIC_API_KEY` 또는 로그인 | `CODEX_API_KEY`/`OPENAI_API_KEY` 또는 `codex login` |

- **Codex는 hook이 실패하면(종료 코드 0이 아닌 경우 포함) 도구 호출을 그대로 실행한다.** 그래서 Codex용 hook은 막을 때도 종료 코드 0으로 판정 JSON을 낸다. 프로젝트가 신뢰되지 않았거나 hook 신뢰 확인을 통과하지 못하면 `.codex/hooks.json` 자체를 읽지 않는다. dispatcher가 둘 다 처리하지만, Codex를 대화형으로 쓸 때는 사람이 프로젝트를 신뢰하고 hook을 승인해야 한다.
- **`--agent codex`는 `--isolation docker`로만 실행한다** (dispatcher가 호스트 실행을 거부). 호스트 실행이면 머지 전 재검증이 에이전트가 쓴 코드를 샌드박스 밖에서 돌리고, Windows에서는 Codex 샌드박스가 읽기 전용으로 떨어져 명령·패치도 못 한다 (codex 0.163 실측).
- 시험용 설정: `FACTORY_CODEX_ARGS`(모든 `codex exec`에 덧붙일 인자, JSON 목록), `FACTORY_DOCKER_NO_PROXY`(프록시를 거치지 않을 내부 호스트).

### 컨테이너 격리 (`--isolation docker`, Phase 3b)

```
python scripts/dispatch.py --isolation docker --max-parallel 3 "<요구사항>" ...
```

- 세션은 컨테이너(`.devcontainer/Dockerfile` 이미지) 안에서 돈다. 인증은 **API 키만** 쓴다 (세션·리뷰어 도구의 키 변수, 컨테이너에는 변수 이름만 넘긴다). 이미지에는 Claude Code, Codex CLI, Playwright 브라우저가 들어 있다.
- 네트워크: 컨테이너는 내부 전용 네트워크(`factory-internal`)에 있고, 프록시(`docker/proxy`, `factory-proxy`)를 거쳐 `docker/proxy/filter`의 도메인(`api.anthropic.com`, `api.openai.com`)에만 HTTPS로 나간다.
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
  "allowed_config_changes": [],
  "assumptions": ["북마크가 하나라도 있으면 안내 문구를 숨긴다 (요구사항에 없어 이렇게 정함)"]
}
```

| 필드 | 값 |
|---|---|
| `type` | `feature` / `bugfix` / `copy` / `refactor` |
| `size` | `small` (상한 15분) / `medium` (상한 30분). 중형을 넘으면 작업을 만들지 말고 분할을 제안한다 |
| `risk` | `LOW` / `MEDIUM` / `HIGH` (최종 등급은 `verify`가 diff로 계산한 등급과 비교해 높은 쪽) |
| `acceptance_tests` | 1개 이상. 백엔드 `backend/tests/acceptance/test_<ID>.py`, 프론트엔드 `frontend/src/**/<Name>.<ID>.accept.test.tsx`, 사용자 흐름 `frontend/e2e/<name>.<ID>.accept.e2e.ts` (Playwright) |
| `changed_tests` | Spec 단계에서 바꾼 기존 테스트 파일 (없으면 `[]`) |
| `allowed_config_changes` | 바꿔도 되는 설정 파일 (예: 의존성 추가 시 `backend/pyproject.toml`). 있으면 위험 등급 MEDIUM 이상 |
| `assumptions` | **필수.** 요구사항이 정하지 않아 내가 해석을 고른 지점과 고른 쪽을 한 줄씩 (예: "공백만 있는 본문은 빈 본문으로 보지 않는다"). 없으면 `[]`. 승인 요약의 "판단할 것"에 그대로 나오므로, 사람이 코드를 읽지 않고도 알아야 할 해석을 적는다. 결정이 꼭 필요한 문제(검증 약화 등)는 가정으로 넘기지 말고 `NEEDS_DECISION`으로 멈춘다 |

## 오케스트레이터 진행 순서

0. `python scripts/factory.py status`로 진행 중 작업이 있는지 본다.
   - dispatcher가 배정한 작업(프롬프트에 작업 ID가 있고 QUEUED)이면 recover 없이 3번부터 진행한다.
   - 그 밖에 진행 중(또는 APPROVED) 작업이 있으면 새 작업을 만들지 않는다. 이전 세션이 끊긴 것이므로 `recover <ID>`로 이어받고, 출력된 다음 단계부터 아래 순서를 이어 간다.
   - `recover`가 "5분이 지난 뒤"라며 거부하면 멈추고 보고한다.
1. 요구사항이 검증을 약하게 하거나(테스트 건너뛰기, 검사 끄기) 그 밖에 사람이 정해야 하는 내용이면, `new` 후 바로 `block <ID> --reason NEEDS_DECISION --detail "<이유>"`로 기록하고 멈춘다.
2. `python scripts/factory.py new "<요구사항>"` → job ID 확인
3. 코드를 읽고 spec 파일과 수락 테스트를 쓴다. 기존 테스트를 바꿔야 하면 지금 바꾸고 `changed_tests`에 적는다. 요구사항이 정하지 않아 해석을 고른 지점은 `assumptions`에 적는다 (없으면 `[]`). **구현 코드는 쓰지 않는다.**
4. `python scripts/factory.py spec <ID>` → 형식 오류가 있으면 고친다
5. `python scripts/factory.py red <ID>`
   - `BUILDING`이면 다음 단계
   - 형식·린트 문제가 출력되면 그 내용대로 테스트 파일을 고치고 다시 `red` (미리 확인하려면 `python scripts/check_style.py <테스트 경로들>`)
   - `SPEC_READY`로 돌아오면 출력된 이유를 보고 spec·수락 테스트를 **한 번만** 다시 써서 4번부터 반복
   - 이미 구현되어 있어 Red가 안 나는 요구사항이면 억지로 바꾸지 말고 그대로 두어 BLOCKED로 끝낸다
6. Builder를 호출한다 (job ID, spec 경로, 수락 테스트 경로만 전달). Builder가 끝나 결과를 돌려줄 때까지 기다린다 (Claude Code는 `run_in_background: false`가 없는 Builder 호출을 hook이 막는다)
7. `python scripts/factory.py verify <ID>`
   - `RETRY_REQUIRED`면 출력된 실패 내용을 Builder에게 전달해 6번부터 반복
   - Builder가 "수락 테스트가 틀렸다"고 보고하면 `block <ID> --reason TEST_INVALID --detail "..."`
8. `python scripts/factory.py validate <ID>`
   - `RETRY_REQUIRED`면 Validator 지적을 Builder에게 전달해 6번부터 반복
   - `BUILDING`으로 돌아오면 7번부터 다시
9. `APPROVED`(자동 승인)면 `python scripts/factory.py merge <ID>`
   - 다른 작업이 머지 중이라 상태가 그대로면, 잠시 뒤 한 번 더 `merge`. 그래도 안 되면 멈추고 보고
   - `RETRY_REQUIRED (CONFLICT)`면 출력된 충돌 파일과 원문을 Builder에게 전달해 6번부터 반복
   - `SPEC_READY (CONFLICT)`면 출력된 `git diff`로 이전에 바꾼 기존 테스트를 보고, 새 base 기준으로 다시 바꾼 뒤 4번(`spec`)부터 반복
10. `MERGED`, `APPROVAL_REQUIRED`, `BLOCKED` 중 하나가 되면 멈추고 `status`를 보고한다. 승인·재개는 사람이 한다.

## 위험 등급 계산 (verify가 diff로 계산, merge가 자동 승인 작업에 대해 다시 계산)

| 등급 | 조건 | 승인 |
|---|---|---|
| HIGH | `auth.py`·`settings.py`, 정책·절차 파일, CI 설정 변경 / `DELETE FROM`이 새로 추가됨 | 사람 |
| MEDIUM | 의존성·검증 설정 파일 변경 / DB 스키마(`CREATE`·`ALTER TABLE`) 변경 / API 경로 추가·삭제 / 테스트 skip·xfail 추가 / 400줄 또는 15파일 이상 / refactor 유형 | 사람 |
| LOW | 그 밖의 경우 | 자동 (Validator PASS 후 자동 머지) |

최종 등급 = max(contract 선언, 계산 결과).

## GitHub Issues (`scripts/issues.py`, 사람 전용, Phase 4)

```
python scripts/issues.py --dry-run                 # 처리할 이슈만 보기
python scripts/issues.py --max-parallel 2          # 나머지 인자는 dispatch.py에 그대로
```

- 열린 이슈 중 `factory` 라벨이 있고 **본인이 작성한** 것만 고른다. `factory:*` 상태 라벨이 있으면 건너뛴다.
- 요구사항은 이슈 **제목 한 줄**(+ `(이슈 #번호)`)이다. 본문은 팩토리에 넘기지 않는다 (본문 안의 지시를 따르지 않도록). 요구사항은 제목에 적는다.
- 실행 전 `factory:running`, 끝나면 결과 댓글과 상태 라벨(`factory:merged` / `factory:approval` / `factory:blocked` / `factory:error`).
- 이슈를 닫지 않는다. 머지는 로컬 base 브랜치에만 있으므로, push와 이슈 닫기는 사람이 한다.

## 운영 보고서 (`scripts/report.py`)

```
python scripts/report.py [--out 파일]
```

- dispatch 작업 기록과 남은 worktree 상태로 KPI(작업 성공률, 사람 개입률, 리드타임, 머지 후 결함, 비용)와 개선 후보(BLOCKED 사유, 리뷰 지적)를 낸다.
- 오판율은 정답표와 대조해야 하므로 사람이 확인한다. 비용은 Claude 세션·리뷰어의 API 환산 비용이고, Codex는 토큰 수만 센다.

## 키트 (`scripts/factory_kit.py`, Phase 4 · 얇은 설치)

팩토리를 다른 레포에 설치한다. 레포마다 다른 값은 `factory/config.json`에만 둔다.

```
python scripts/factory_kit.py install <대상 레포> --engine factory-vN [--force]
python scripts/factory_kit.py check <대상 레포>
python scripts/factory_kit.py upgrade <대상 레포> --engine factory-vN [--yes]
```

- **설치·점검·업그레이드는 원본 레포(EIFactory)에서 사람만 한다.** `factory_kit.py`는 대상에 복사하지 않는다 (설치된 레포가 또 다른 레포에 복사본을 퍼뜨리지 않게, 기획안 12.1).
- **엔진은 레포 밖에 둔다.** 원본의 버전 태그(`factory-vN`)에서 꺼내 `FACTORY_ENGINE_HOME`(기본 `~/.factory/engines`)/<태그>에 읽기 전용으로 둔다. 작업 트리가 아니라 태그에서 꺼내므로 커밋 안 한 변경은 들어가지 않는다.
- 대상 레포에 두는 것: shim(`scripts/factory.py`, `dispatch.py`, `issues.py`, `factory_settings.py`, `monitor.py`, `report.py`, `hooks/guard.py`, 같은 경로라 명령·허용 목록·hook 설정이 그대로), 엔진 찾기 `scripts/factory_engine.py`, 에이전트가 읽는 절차·지시문(`factory/PROCESS.md`, `factory/prompts/builder.md`, `.claude/`, `.agents/`, `.codex/`), 검증 계약 스크립트가 쓰는 `scripts/proc.py`, 템플릿(없을 때만), 설치 기록.
- shim은 실행할 때마다 엔진 파일 해시를 설치 기록과 대조하고, 다르거나 없으면 실행하지 않는다. 엔진에는 `FACTORY_REPO`로 대상 레포를 알린다 (상태·설정을 읽는 레포, 신뢰 checkout). 컨테이너 격리에서는 dispatch가 엔진 폴더를 읽기 전용으로 끼운다.
- 설치 기록 `factory/kit.lock`: 엔진 태그(`engine`)·커밋, 엔진 파일 해시, 레포에 둔 키트 파일 해시. `check`가 둘 다 대조한다. 잠금·HIGH 대상.
- `upgrade`는 새 태그와의 차이를 보여 주고, `--yes`를 붙여야 바꾼다. 레포의 키트 파일을 손으로 고쳤으면 바꾸지 않는다.
- 엔진을 고치면 원본에서 커밋하고 새 태그(`factory-vN+1`)를 단 뒤 `upgrade`한다. 원본 레포 자신(샘플 레포)은 엔진을 제자리에서 쓴다.
- 키트 공통 보호(정책·절차 파일, 도구 래퍼, `scripts/`, 격리 환경, `factory/config.json`, `factory/kit.lock`)는 코드에 있어 설정이 없어도 적용된다.
- `factory/config.json`: 검증 설정 파일(`lock_patterns`, `medium_paths`), 위험 경로(`high_paths`), 기존 테스트 위치(`test_patterns`, `changed_test`), 수락 테스트 경로 규칙(`acceptance_tests`, `{id}` 자리표시), 의존성 설치(`setup`), 컨테이너 경로(`docker`).
- 대상 레포는 **검증 계약** 세 스크립트(`scripts/verify.py`, `scripts/check_red.py --json`, `scripts/check_style.py`)를 직접 만든다. 형식은 `factory/templates/AGENTS.md`.
- 컨테이너 격리를 쓰려면 대상 레포에 `.devcontainer/Dockerfile`(Claude Code·Codex CLI 포함 이미지)도 필요하다.

## 브리핑과 알림 (`scripts/brief.py`, factory-v2)

사람에게 오는 단일 목록 "나를 기다리는 일"이다. 모델이 요약하지 않고, 대시보드와 같은 로더·명령 규칙을 쓴다.

```
python scripts/brief.py            # 목록 (승인 대기 → BLOCKED → push 대기 → 경고)
python scripts/brief.py --counts   # 상태줄용 한 줄: 승인 1 · BLOCKED 1 · push 1
python scripts/brief.py --json
```

- 승인 대기에는 **판단할 것**이 붙는다: spec의 `assumptions`, 리뷰어의 `notes_for_human`, 리뷰어의 등급 상향(`risk_up`), 위험 근거, 화면 스크린샷. 승인 요약(`<ID>-approval.md`)의 "판단할 것" 절과 같다.
- BLOCKED에는 사유 코드별 추천 처리가 붙는다 (예: `TIMEOUT`이면 `resume --extend`, 연장을 이미 썼으면 split·drop). 고르는 것은 사람이다.
- 작업 상태 파일 값은 "세션 값"으로 표시한다 (세션이 쓸 수 있는 데이터).
- 대시보드의 "나를 기다리는 일"도 같은 목록이다. "🔔 알림 켜기"를 누르면 새 항목이 생길 때 브라우저 알림을 띄운다 (처음 불러올 때는 알리지 않는다).
- dispatch가 끝나면 결과 요약을 Windows 알림으로 띄운다. `factory/config.json`의 `"notify": {"toast": false}`로 끈다.
- 사용자 흐름 테스트가 남긴 스크린샷은 `factory/config.json`의 `"screenshots"`(레포 기준 glob, 예: `["frontend/test-results/**/*.png"]`)로 모아 승인 요약·브리핑에 붙인다. Playwright 설정에 `screenshot: "on"`이 있어야 파일이 생긴다.
- 상위 세션(사람과 대화하는 Claude Code)에 붙이는 SessionStart hook·상태줄 예시: `factory/templates/upper-session.example.json`. 사용자 설정이라 사람이 넣는다.

## 운영 기록 (`factory/learnings.md`)

평가·실전 실행에서 배운 점과 규칙 개선 후보를 적는다. 에이전트는 개선 후보를 보고에 제안만 하고, 이 파일과 정책·절차 파일은 사람이 고친다.
