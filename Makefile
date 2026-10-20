# 검증 로직은 scripts/에 있고, Makefile은 진입점만 제공한다.
# make가 없는 환경(Windows 호스트 등)에서는 같은 스크립트를 python으로 직접 실행한다.
PYTHON ?= python3

.PHONY: setup verify verify-backend verify-frontend verify-scripts test-red test-green dev-backend dev-frontend

setup:
	cd backend && uv sync
	cd frontend && npm ci

verify:
	$(PYTHON) scripts/verify.py --area all

verify-backend:
	$(PYTHON) scripts/verify.py --area backend

verify-frontend:
	$(PYTHON) scripts/verify.py --area frontend

verify-scripts:
	$(PYTHON) scripts/verify.py --area scripts

# 사용 예: make test-red TESTS="backend/tests/test_x.py::test_y frontend/src/X.test.tsx"
test-red:
	@test -n "$(TESTS)" || (echo 'TESTS="..."를 지정하세요'; exit 4)
	$(PYTHON) scripts/check_red.py --expect red $(TESTS)

test-green:
	@test -n "$(TESTS)" || (echo 'TESTS="..."를 지정하세요'; exit 4)
	$(PYTHON) scripts/check_red.py --expect green $(TESTS)

dev-backend:
	cd backend && uv run uvicorn bookmarks.main:app_from_env --factory --app-dir src --reload --port 8000

dev-frontend:
	cd frontend && npm run dev
