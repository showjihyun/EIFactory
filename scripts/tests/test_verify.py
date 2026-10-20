import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proc import resolve_command  # noqa: E402
from verify import STEPS, Step, run_steps  # noqa: E402


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_all_steps_pass(tmp_path: Path) -> None:
    steps = [Step("a", "x", py("pass"), "."), Step("b", "x", py("pass"), ".")]
    code, report = run_steps(steps, tmp_path)
    assert code == 0
    assert [r.passed for r in report] == [True, True]


def test_failure_does_not_stop_later_steps(tmp_path: Path) -> None:
    steps = [
        Step(area="x", name="fails", cmd=py("raise SystemExit(3)"), cwd="."),
        Step(area="x", name="passes", cmd=py("pass"), cwd="."),
    ]
    code, report = run_steps(steps, tmp_path)
    assert code == 1
    assert [(r.step.name, r.exit_code) for r in report] == [("fails", 3), ("passes", 0)]


def test_missing_command_counts_as_failure(tmp_path: Path) -> None:
    code, report = run_steps([Step("ghost", "x", ["no-such-tool-xyz"], ".")], tmp_path)
    assert code == 1
    assert report[0].passed is False


def test_unlaunchable_command_counts_as_failure(tmp_path: Path) -> None:
    not_a_program = tmp_path / "not_a_program.txt"
    not_a_program.write_text("hello", encoding="utf-8")
    steps = [Step(area="x", name="bad", cmd=[str(not_a_program)], cwd=".")]
    code, report = run_steps(steps, tmp_path)
    assert code == 1
    assert report[0].passed is False


@pytest.mark.skipif(os.name != "nt", reason="Windows 전용")
def test_resolve_prefers_windows_launchers() -> None:
    resolved = resolve_command(["npm", "--version"])
    assert Path(resolved[0]).suffix.lower() in {".cmd", ".exe", ".bat"}


def test_every_area_has_lint_type_and_test_steps() -> None:
    for area in ("backend", "frontend"):
        names = {s.name for s in STEPS if s.area == area}
        assert {"lint", "typecheck", "test"} <= names, area
