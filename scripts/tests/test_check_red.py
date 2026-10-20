import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_red import (  # noqa: E402
    EXIT_ERROR,
    EXIT_NO_TESTS,
    EXIT_OK,
    EXIT_PASSING,
    Outcome,
    TestResult,
    classify_message,
    decide,
    parse_junit,
    parse_vitest,
    run_pytest,
)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_a" name="test_passes"/>
  <testcase classname="tests.test_a" name="test_assert">
    <failure message="assert 404 == 200">tests/test_a.py:3: AssertionError</failure>
  </testcase>
  <testcase classname="tests.test_a" name="test_attr">
    <failure message="AttributeError: module has no attribute 'x'">trace</failure>
  </testcase>
  <testcase classname="tests.test_a" name="test_fixture_assert">
    <error message="failed on setup with &quot;assert 404 == 200&quot;">trace</error>
  </testcase>
  <testcase classname="" name="tests.test_b">
    <error message="collection failure">ImportError: cannot import name 'x'</error>
  </testcase>
  <testcase classname="tests.test_a" name="test_skip">
    <skipped message="later"/>
  </testcase>
</testsuite></testsuites>
"""


def by_name(results: list[TestResult]) -> dict[str, Outcome]:
    return {r.test_id.split("::")[-1]: r.outcome for r in results}


def test_parse_junit_classifies_each_case() -> None:
    outcomes = by_name(parse_junit(JUNIT))
    assert outcomes["test_passes"] is Outcome.PASSED
    assert outcomes["test_assert"] is Outcome.ASSERTION_FAILED
    assert outcomes["test_attr"] is Outcome.ERROR
    assert outcomes["test_fixture_assert"] is Outcome.ASSERTION_FAILED
    assert outcomes["tests.test_b"] is Outcome.ERROR
    assert outcomes["test_skip"] is Outcome.ERROR


VITEST = {
    "testResults": [
        {
            "name": "/repo/frontend/src/A.test.tsx",
            "status": "failed",
            "message": "",
            "assertionResults": [
                {"fullName": "A passes", "status": "passed", "failureMessages": []},
                {
                    "fullName": "A asserts",
                    "status": "failed",
                    "failureMessages": ["AssertionError: expected 1 to be 2"],
                },
                {
                    "fullName": "A missing element",
                    "status": "failed",
                    "failureMessages": ["TestingLibraryElementError: Unable to find role"],
                },
                {
                    "fullName": "A jest-dom",
                    "status": "failed",
                    "failureMessages": ["Error: expect(element).toBeInTheDocument()\n"],
                },
                {
                    "fullName": "A type error",
                    "status": "failed",
                    "failureMessages": ["TypeError: x is not a function"],
                },
            ],
        },
        {
            "name": "/repo/frontend/src/B.test.tsx",
            "status": "failed",
            "message": "Failed to resolve import './B'",
            "assertionResults": [],
        },
    ]
}


def test_parse_vitest_classifies_each_case() -> None:
    outcomes = by_name(parse_vitest(VITEST))
    assert outcomes["A passes"] is Outcome.PASSED
    assert outcomes["A asserts"] is Outcome.ASSERTION_FAILED
    assert outcomes["A missing element"] is Outcome.ASSERTION_FAILED
    assert outcomes["A jest-dom"] is Outcome.ASSERTION_FAILED
    assert outcomes["A type error"] is Outcome.ERROR
    assert outcomes["/repo/frontend/src/B.test.tsx"] is Outcome.ERROR


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("assert 1 == 2", True),
        ("AssertionError: boom", True),
        ('failed on setup with "assert 404 == 200"', True),
        ("ImportError: no module", False),
        ("SyntaxError: invalid syntax", False),
        ("NotImplementedError", False),
    ],
)
def test_classify_message(message: str, expected: bool) -> None:
    assert classify_message(message) is expected


def results(*outcomes: Outcome) -> list[TestResult]:
    return [TestResult(f"t{i}", o, "") for i, o in enumerate(outcomes)]


def test_red_ok_when_all_fail_by_assertion() -> None:
    code, _ = decide(results(Outcome.ASSERTION_FAILED, Outcome.ASSERTION_FAILED), "red")
    assert code == EXIT_OK


def test_red_rejects_a_passing_test() -> None:
    code, _ = decide(results(Outcome.ASSERTION_FAILED, Outcome.PASSED), "red")
    assert code == EXIT_PASSING


def test_red_rejects_errors_before_passing() -> None:
    code, _ = decide(results(Outcome.PASSED, Outcome.ERROR), "red")
    assert code == EXIT_ERROR


def test_no_tests_is_its_own_failure() -> None:
    assert decide([], "red")[0] == EXIT_NO_TESTS
    assert decide([], "green")[0] == EXIT_NO_TESTS


def test_green_ok_only_when_all_pass() -> None:
    assert decide(results(Outcome.PASSED, Outcome.PASSED), "green")[0] == EXIT_OK
    assert decide(results(Outcome.PASSED, Outcome.ASSERTION_FAILED), "green")[0] != EXIT_OK


def test_run_pytest_on_real_tests(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "def test_red():\n    assert 1 == 2\n\ndef test_green():\n    assert True\n",
        encoding="utf-8",
    )
    found = by_name(run_pytest(["test_sample.py"], cwd=tmp_path, base=[sys.executable, "-m"]))
    assert found == {"test_red": Outcome.ASSERTION_FAILED, "test_green": Outcome.PASSED}


def test_run_pytest_reports_import_error_as_error(tmp_path: Path) -> None:
    (tmp_path / "test_broken.py").write_text(
        "from nowhere import thing\n\ndef test_x():\n    assert thing\n", encoding="utf-8"
    )
    found = run_pytest(["test_broken.py"], cwd=tmp_path, base=[sys.executable, "-m"])
    assert found
    assert all(r.outcome is Outcome.ERROR for r in found)


def test_cli_prints_json_summary(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "check_red.py"
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "test_one.py").write_text(
        "def test_red():\n    assert 1 == 2\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(tmp_path),
            "--python-runner",
            "--json",
            "backend/test_one.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_OK, proc.stdout + proc.stderr
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["verdict"] == "RED_OK"
    assert summary["tests"][0]["outcome"] == "assertion_failed"
