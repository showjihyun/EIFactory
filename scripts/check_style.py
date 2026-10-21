"""테스트 파일 형식·린트 확인 (factory.py red가 잠그기 전에 실행).

수락 테스트와 Spec 단계에서 바꾼 기존 테스트는 Red 확인 뒤 잠긴다. 잠긴 뒤에는 Builder가
고칠 수 없으므로, verify.py의 형식·린트 단계에 걸릴 문제는 잠그기 전에 찾아야 한다.

사용법: python scripts/check_style.py <레포 루트 기준 경로>...
마지막 줄에 JSON {"ok": bool, "problems": [...]}을 출력한다. 문제가 있으면 종료 코드 1.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proc import resolve_command, use_utf8_output  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

# verify.py의 형식·린트 단계와 같은 도구를 파일 단위로 실행한다
BACKEND_STEPS = [
    ("format", ["uv", "run", "ruff", "format", "--diff"]),
    ("lint", ["uv", "run", "ruff", "check"]),
]
FRONTEND_STEPS = [("lint", ["npx", "eslint"])]
SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


def run_command(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            resolve_command(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        return subprocess.CompletedProcess(cmd, 2, "", str(error))


def is_safe_path(path: str) -> bool:
    """레포 안의 평범한 상대 경로만 도구 인자로 넘긴다 (옵션·상위 경로·셸 문자 차단)."""
    parts = path.split("/")
    return bool(SAFE_PATH.fullmatch(path)) and not any(
        p in {"", ".."} or p[0] == "-" for p in parts
    )


def check(root: Path, paths: list[str], run: Runner = run_command) -> tuple[bool, list[str]]:
    files = sorted({p.split("::")[0] for p in paths})
    unsafe = [f for f in files if not is_safe_path(f)]
    if unsafe:
        return False, [f"허용하지 않는 경로: {', '.join(unsafe)}"]
    areas = [
        ("backend", BACKEND_STEPS, [f for f in files if f.startswith("backend/")]),
        ("frontend", FRONTEND_STEPS, [f for f in files if f.startswith("frontend/")]),
    ]
    problems = []
    for area, steps, selected in areas:
        if not selected:
            continue
        relative = [f.removeprefix(f"{area}/") for f in selected]
        for name, cmd in steps:
            proc = run([*cmd, "--", *relative], root / area)
            if proc.returncode != 0:
                output = (proc.stdout + proc.stderr).strip()[-1500:]
                problems.append(f"{area} {name}: {output}")
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    paths = sys.argv[1:] if argv is None else argv
    ok, problems = check(ROOT, paths)
    for problem in problems:
        print(problem)
    print(f"style: {'PASS' if ok else 'FAIL'}")
    print(json.dumps({"ok": ok, "problems": problems}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
