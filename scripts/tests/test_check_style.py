import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_style import check  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class FakeRunner:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.failing = failing or set()

    def __call__(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, cwd.name))
        failed = any(name in cmd for name in self.failing)
        output = "문제 출력" if failed else ""
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, output, "")


def test_backend_files_get_ruff_format_and_lint_relative_to_backend() -> None:
    run = FakeRunner()
    ok, problems = check(ROOT, ["backend/tests/acceptance/test_J001.py::test_x"], run=run)
    assert ok and problems == []
    commands = [cmd for cmd, cwd in run.calls if cwd == "backend"]
    assert any("format" in c and "tests/acceptance/test_J001.py" in c for c in commands)
    assert any("check" in c and "tests/acceptance/test_J001.py" in c for c in commands)


def test_frontend_files_get_eslint_relative_to_frontend() -> None:
    run = FakeRunner()
    ok, _ = check(ROOT, ["frontend/src/components/List.J001.accept.test.tsx"], run=run)
    assert ok
    cmd, cwd = run.calls[0]
    assert cwd == "frontend"
    assert "eslint" in cmd
    assert "src/components/List.J001.accept.test.tsx" in cmd


def test_failure_reports_the_step_and_output() -> None:
    run = FakeRunner(failing={"format"})
    ok, problems = check(ROOT, ["backend/tests/acceptance/test_J001.py"], run=run)
    assert not ok
    assert "backend format" in problems[0]
    assert "문제 출력" in problems[0]


def test_other_paths_and_empty_input_are_ignored() -> None:
    run = FakeRunner()
    assert check(ROOT, ["factory/specs/J001.md"], run=run) == (True, [])
    assert run.calls == []


def test_real_ruff_accepts_a_formatted_file() -> None:
    ok, problems = check(ROOT, ["backend/tests/acceptance/__init__.py"])
    assert ok, problems


def test_file_list_follows_end_of_options_marker() -> None:
    run = FakeRunner()
    check(ROOT, ["backend/tests/acceptance/test_J001.py"], run=run)
    for cmd, _ in run.calls:
        assert cmd[cmd.index("--") + 1 :] == ["tests/acceptance/test_J001.py"]


def test_unsafe_paths_are_rejected_without_running_tools() -> None:
    run = FakeRunner()
    for bad in [
        "frontend/--config=evil.js",
        "frontend/src/../../../etc/passwd",
        "backend/tests/x.py;rm",
        "/etc/passwd",
    ]:
        ok, problems = check(ROOT, [bad], run=run)
        assert not ok, bad
        assert "허용하지 않는 경로" in problems[0]
    assert run.calls == []
