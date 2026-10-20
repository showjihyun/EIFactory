"""레포 전체 검증 (기획안 5장의 `make verify`).

사용법:
    python scripts/verify.py [--area backend|frontend|scripts|all]

모든 단계를 끝까지 실행한 뒤 결과 표를 출력한다. 하나라도 실패하면 종료 코드 1.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proc import resolve_command, use_utf8_output  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Step:
    area: str
    name: str
    cmd: list[str]
    cwd: str


@dataclass(frozen=True)
class StepResult:
    step: Step
    exit_code: int
    seconds: float
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


STEPS: list[Step] = [
    Step("backend", "lint", ["uv", "run", "ruff", "check", "."], "backend"),
    Step("backend", "format", ["uv", "run", "ruff", "format", "--check", "."], "backend"),
    Step("backend", "typecheck", ["uv", "run", "mypy"], "backend"),
    Step("backend", "test", ["uv", "run", "pytest", "--basetemp=.pytest_tmp"], "backend"),
    Step("frontend", "lint", ["npm", "run", "-s", "lint"], "frontend"),
    Step("frontend", "typecheck", ["npm", "run", "-s", "typecheck"], "frontend"),
    Step("frontend", "test", ["npm", "run", "-s", "test"], "frontend"),
    Step(
        "scripts",
        "lint",
        [
            "uv",
            "run",
            "--project",
            "backend",
            "ruff",
            "check",
            "--config",
            "backend/pyproject.toml",
            "scripts",
        ],
        ".",
    ),
    Step(
        "scripts",
        "test",
        [
            "uv",
            "run",
            "--project",
            "backend",
            "pytest",
            "scripts/tests",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp=.pytest_tmp_scripts",
        ],
        ".",
    ),
]


def _run(step: Step, root: Path) -> StepResult:
    cmd = resolve_command(step.cmd)
    started = time.monotonic()
    if shutil.which(cmd[0]) is None:
        return StepResult(step, 127, 0.0, f"명령을 찾을 수 없음: {step.cmd[0]}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=root / step.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        return StepResult(step, 126, time.monotonic() - started, f"실행 실패: {error}")
    return StepResult(step, proc.returncode, time.monotonic() - started, proc.stdout + proc.stderr)


def run_steps(steps: list[Step], root: Path) -> tuple[int, list[StepResult]]:
    report = [_run(step, root) for step in steps]
    return (0 if all(r.passed for r in report) else 1), report


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description="레포 전체 검증")
    parser.add_argument("--area", choices=["backend", "frontend", "scripts", "all"], default="all")
    args = parser.parse_args(argv)

    steps = [s for s in STEPS if args.area == "all" or s.area == args.area]
    code, report = run_steps(steps, ROOT)

    for r in report:
        if not r.passed:
            print(f"----- {r.step.area}/{r.step.name} 실패 (exit {r.exit_code}) -----")
            print(r.output.strip()[-3000:])
    print()
    print(f"{'영역':<10}{'단계':<12}{'결과':<8}{'시간':>8}")
    for r in report:
        mark = "PASS" if r.passed else "FAIL"
        print(f"{r.step.area:<10}{r.step.name:<12}{mark:<8}{r.seconds:>7.1f}s")
    print(f"verify: {'PASS' if code == 0 else 'FAIL'} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
