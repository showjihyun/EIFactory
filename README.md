# bookmark-factory

AI SW Factory 검증용 샘플 레포 (북마크 관리 웹앱).

## 빠른 시작

devcontainer(VS Code "Reopen in Container")로 열면 `make setup`이 자동 실행된다.

```bash
make verify          # 전체 검증
make dev-backend     # http://localhost:8000 (API)
make dev-frontend    # http://localhost:5173 (화면, /api는 백엔드로 프록시)
```

로그인 기본값은 `demo` / `demo`이다 (`BOOKMARKS_USERNAME`, `BOOKMARKS_PASSWORD`로 변경).

자세한 명령과 작업 규칙은 [AGENTS.md](AGENTS.md)를 본다.
