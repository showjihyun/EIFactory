# 운영 기록 (learnings)

평가·실전 실행에서 배운 점과 규칙 개선 후보. **사람이 고친다.** 에이전트는 개선 후보를 보고에 제안만 한다 (`AGENTS.md` 원칙 10).
상세 근거는 기획 작업 공간의 `evals/phase*_results.md`에 있다.

## 반영된 교훈

| 출처 | 배운 점 | 반영 |
|---|---|---|
| Phase 0 | 단일 에이전트는 "재작성 1회" 지시를 받고도 2회 재작성했다 | 재작성 횟수를 `factory.py`가 세고 강제 |
| Phase 0 | 수락 테스트를 기존 테스트 파일에 섞으면 Red가 `PASSING_BEFORE_IMPL`로 잘못 나온다 | 작업별 전용 파일 |
| Phase 2 | 잠긴 수락 테스트가 형식 검사에 걸리면 고칠 수 없어 작업이 막혔다 | `red`가 잠그기 전에 `check_style.py` |
| Phase 2 | 바꾼 테스트 경로가 그대로 도구 인자로 넘어갔다 | 경로 검증 + `--` 구분자 |
| Phase 3a | 병렬 작업이 `__pycache__`를 커밋해 rebase 충돌 | `.git/info/exclude`에 실행 캐시 |
| Phase 3a | worktree를 옮기면 uv 가상환경이 깨진다 (절대 경로) | 옮긴 뒤 설치 |
| Phase 3b | `GIT_DIR` 환경변수로 컨테이너에 Git을 넘기자 테스트가 공유 `.git/config`를 고쳤다 | 공유 `.git` 읽기 전용 + `.git` 파일 바꿔 끼우기 |
| Phase 3b | 바깥 `FACTORY_*` 설정이 scripts 테스트에 새어 들었다 | 테스트마다 설정을 지우는 conftest |
| Phase 3c | Codex는 hook이 종료 코드 2를 내면 "hook 실패"로 보고 호출을 **그대로 실행**한다 | Codex용 hook은 종료 코드 0 + 판정 JSON (`--json-deny`) |
| Phase 3c | Codex는 신뢰되지 않은 프로젝트의 `.codex/hooks.json`을 읽지 않는다 (경고 없이 hook 없이 돈다) | dispatcher가 프로젝트 신뢰 + hook 신뢰 확인 생략을 넘긴다 |
| Phase 3c | 셸 명령에 패치 표시 글자를 넣으면 guard의 셸 검사를 피할 수 있었다 (보안 리뷰 지적) | 패치 도구 여부를 도구 이름으로만 판단 |
| Phase 3c | Codex 호스트 세션에 공유 `.git` 쓰기 권한을 주면 hook·config를 심어 호스트에서 코드를 실행할 수 있다 (보안 리뷰 지적) | 쓰기 권한을 주지 않고, 머지는 세션이 끝난 뒤 호스트가 한다 |
| Phase 3c | 머지된 worktree를 못 지우면(Windows 경로 길이 제한, 읽기 전용 Git objects) dispatcher가 결과 표 전체를 ERROR로 잃었다 | 긴 경로·읽기 전용 파일까지 지우고, 그래도 실패하면 결과는 두고 사유만 남긴다 |
| Phase 3c | 테스트에서 `sys.platform`을 바꾸면 리눅스 컨테이너에서 표준 라이브러리가 깨진다 (호스트에서는 통과) | 판정 함수를 따로 두고 그 함수만 바꾼다 |

## 개선 후보 (미반영)

- 컨테이너 안 `verify`가 Windows 바인드 마운트 때문에 느리다 (scripts 테스트 약 5~6분). 소형 상한 15분에 비해 여유가 적다.
- Codex hook이 실제로 읽혔는지 세션 시작 때 확인하는 장치가 없다. 지금은 dispatcher 설정과 `verify`의 잠금 대조·위험 재계산이 마지막 방어선이다.
