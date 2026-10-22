"""수락 테스트의 Red/Green 상태를 판정한다 (기획안 7.5).

사용법:
    python scripts/check_red.py [--expect red|green] [--json] TEST [TEST ...]

TEST는 레포 루트 기준 경로다. `backend/`로 시작하면 pytest, `frontend/e2e/`로 시작하면
Playwright(사용자 흐름), 그 밖의 `frontend/`는 Vitest로 실행한다. pytest는 노드
ID(`backend/tests/test_x.py::test_y`)도 받는다.

Red로 인정하는 실패는 assertion 실패(화면 요소 없음 포함)뿐이다. import 오류, 문법 오류,
수집 실패, 그 밖의 예외, skip은 Red로 인정하지 않는다.

종료 코드:
    0  기대한 상태 (red: 전부 assertion 실패 / green: 전부 통과)
    1  red 기대인데 이미 통과하는 테스트가 있음, 또는 green 기대인데 실패가 있음
    2  assertion이 아닌 오류·skip이 있음
    3  실행된 테스트가 없음
    4  사용법 오류
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proc import resolve_command, use_utf8_output  # noqa: E402

EXIT_OK = 0
EXIT_PASSING = 1
EXIT_ERROR = 2
EXIT_NO_TESTS = 3
EXIT_USAGE = 4

ASSERTION_PREFIXES = (
    "assert ",
    "AssertionError",
    "TestingLibraryElementError",
    "Error: expect(",
    # Playwright: 화면 요소가 없어 동작(click 등)이 시간 초과 (Testing Library의 요소 없음과 같게)
    "TimeoutError: locator.",
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Outcome(Enum):
    PASSED = "passed"
    ASSERTION_FAILED = "assertion_failed"
    ERROR = "error"


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # pytest가 테스트 클래스로 수집하지 않도록

    test_id: str
    outcome: Outcome
    detail: str


def classify_message(message: str) -> bool:
    """실패 메시지가 assertion 실패이면 True."""
    text = message.strip()
    for wrapper in ('failed on setup with "', 'failed on call with "'):
        if text.startswith(wrapper):
            text = text.removeprefix(wrapper)
    first_line = text.splitlines()[0] if text else ""
    return first_line.startswith(ASSERTION_PREFIXES)


def parse_junit(xml_text: str) -> list[TestResult]:
    # 입력은 이 스크립트가 실행한 pytest가 로컬에 쓴 리포트다. 표준 ElementTree는
    # 외부 엔티티를 해석하지 않으므로 별도 의존성(defusedxml) 없이 읽는다.
    found: list[TestResult] = []
    for case in ET.fromstring(xml_text).iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        problem = next((c for c in case if c.tag in ("failure", "error", "skipped")), None)
        if problem is None:
            found.append(TestResult(test_id, Outcome.PASSED, ""))
            continue
        message = problem.get("message", "")
        if problem.tag == "skipped":
            found.append(TestResult(test_id, Outcome.ERROR, f"skipped: {message}"))
        elif classify_message(message):
            found.append(TestResult(test_id, Outcome.ASSERTION_FAILED, message))
        else:
            found.append(TestResult(test_id, Outcome.ERROR, message or (problem.text or "")))
    return found


def parse_vitest(report: dict[str, Any]) -> list[TestResult]:
    found: list[TestResult] = []
    for file_result in report.get("testResults", []):
        cases = file_result.get("assertionResults", [])
        if not cases and file_result.get("status") == "failed":
            found.append(
                TestResult(file_result["name"], Outcome.ERROR, file_result.get("message", ""))
            )
            continue
        for case in cases:
            test_id = case.get("fullName") or case.get("title", "")
            status = case.get("status")
            if status == "passed":
                found.append(TestResult(test_id, Outcome.PASSED, ""))
                continue
            messages = case.get("failureMessages") or []
            message = messages[0] if messages else f"status: {status}"
            outcome = (
                Outcome.ASSERTION_FAILED
                if status == "failed" and classify_message(message)
                else Outcome.ERROR
            )
            found.append(TestResult(test_id, outcome, message.splitlines()[0] if message else ""))
    return found


def parse_playwright(report: dict[str, Any]) -> list[TestResult]:
    """Playwright JSON 리포트. 테스트는 파일 > describe > 테스트 순으로 중첩된다."""
    found: list[TestResult] = []

    def walk(suite: dict[str, Any], path: list[str]) -> None:
        here = [*path, suite.get("title", "")]
        for spec in suite.get("specs", []):
            test_id = " > ".join([*here, spec.get("title", "")])
            for test in spec.get("tests", []):
                results = test.get("results") or [{"status": "skipped"}]
                last = results[-1]
                status = last.get("status")
                message = ANSI.sub("", (last.get("error") or {}).get("message", ""))
                if status == "passed":
                    found.append(TestResult(test_id, Outcome.PASSED, ""))
                elif status == "failed" and classify_message(message):
                    found.append(TestResult(test_id, Outcome.ASSERTION_FAILED, message))
                else:
                    detail = message.splitlines()[0] if message else f"status: {status}"
                    found.append(TestResult(test_id, Outcome.ERROR, detail))
        for child in suite.get("suites", []):
            walk(child, here)

    for suite in report.get("suites", []):
        walk(suite, [])
    for error in report.get("errors", []):
        message = ANSI.sub("", error.get("message", ""))
        found.append(TestResult("playwright", Outcome.ERROR, message.splitlines()[0][:300]))
    return found


def decide(results: list[TestResult], expect: str) -> tuple[int, str]:
    if not results:
        return EXIT_NO_TESTS, "NO_TESTS"
    if any(r.outcome is Outcome.ERROR for r in results):
        return EXIT_ERROR, "INVALID_RED" if expect == "red" else "ERROR"
    if expect == "red":
        if any(r.outcome is Outcome.PASSED for r in results):
            return EXIT_PASSING, "PASSING_BEFORE_IMPL"
        return EXIT_OK, "RED_OK"
    if any(r.outcome is not Outcome.PASSED for r in results):
        return EXIT_PASSING, "NOT_GREEN"
    return EXIT_OK, "GREEN_OK"


def run_pytest(targets: list[str], cwd: Path, base: list[str] | None = None) -> list[TestResult]:
    """pytest를 실행해 결과를 읽는다. base 기본값은 `uv run`."""
    base = base if base is not None else ["uv", "run"]
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        # checkout마다 임시 폴더를 따로 써서 동시에 실행되는 다른 worktree와 충돌하지 않게 한다.
        cmd = [
            *base,
            "pytest",
            *targets,
            f"--junitxml={report}",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp=.pytest_tmp_check_red",
        ]
        subprocess.run(
            resolve_command(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if not report.exists():
            return [TestResult(" ".join(targets), Outcome.ERROR, "pytest가 리포트를 만들지 못함")]
        return parse_junit(report.read_text(encoding="utf-8"))


def run_vitest(targets: list[str], cwd: Path) -> list[TestResult]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "vitest.json"
        cmd = ["npx", "vitest", "run", *targets, "--reporter=json", f"--outputFile={report}"]
        subprocess.run(
            resolve_command(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if not report.exists():
            return [TestResult(" ".join(targets), Outcome.ERROR, "vitest가 리포트를 만들지 못함")]
        data: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
        return parse_vitest(data)


def run_playwright(targets: list[str], cwd: Path) -> list[TestResult]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "playwright.json"
        env = {**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report)}
        cmd = ["npx", "playwright", "test", *targets, "--reporter=json"]
        subprocess.run(
            resolve_command(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        if not report.exists():
            return [
                TestResult(" ".join(targets), Outcome.ERROR, "playwright가 리포트를 만들지 못함")
            ]
        data: dict[str, Any] = json.loads(report.read_text(encoding="utf-8"))
        return parse_playwright(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tests", nargs="+")
    parser.add_argument("--expect", choices=["red", "green"], default="red")
    parser.add_argument("--json", action="store_true", help="마지막 줄에 JSON 요약 출력")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--python-runner",
        action="store_true",
        help="uv 대신 현재 파이썬으로 pytest 실행 (스크립트 테스트용)",
    )
    args = parser.parse_args(argv)
    use_utf8_output()

    backend: list[str] = []
    frontend: list[str] = []
    e2e: list[str] = []
    for target in args.tests:
        normalized = target.replace("\\", "/")
        if normalized.startswith("backend/"):
            backend.append(normalized.removeprefix("backend/"))
        elif normalized.startswith("frontend/e2e/"):
            e2e.append(normalized.removeprefix("frontend/"))
        elif normalized.startswith("frontend/"):
            frontend.append(normalized.removeprefix("frontend/"))
        else:
            print(f"알 수 없는 경로: {target} (backend/ 또는 frontend/로 시작해야 함)")
            return EXIT_USAGE

    results: list[TestResult] = []
    if backend:
        base = [sys.executable, "-m"] if args.python_runner else None
        results += run_pytest(backend, cwd=args.root / "backend", base=base)
    if frontend:
        results += run_vitest(frontend, cwd=args.root / "frontend")
    if e2e:
        results += run_playwright(e2e, cwd=args.root / "frontend")

    code, verdict = decide(results, args.expect)
    for r in results:
        print(f"[{r.outcome.value:>16}] {r.test_id}" + (f"  -- {r.detail}" if r.detail else ""))
    print(f"verdict: {verdict} (exit {code})")
    if args.json:
        summary = {
            "expect": args.expect,
            "verdict": verdict,
            "exit_code": code,
            "tests": [
                {"id": r.test_id, "outcome": r.outcome.value, "detail": r.detail} for r in results
            ],
        }
        print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
