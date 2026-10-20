"""스크립트 공용 프로세스 도우미."""

from __future__ import annotations

import os
import shutil
import sys

WINDOWS_LAUNCHERS = (".exe", ".cmd", ".bat")


def resolve_command(cmd: list[str]) -> list[str]:
    """실행 파일 경로를 찾는다. Windows에서는 npm 같은 셸 스크립트 대신 .cmd/.exe를 고른다."""
    name = cmd[0]
    found: str | None = None
    if os.name == "nt" and not name.lower().endswith(WINDOWS_LAUNCHERS):
        for ext in WINDOWS_LAUNCHERS:
            found = shutil.which(name + ext)
            if found:
                break
    found = found or shutil.which(name)
    return [found, *cmd[1:]] if found else cmd


def use_utf8_output() -> None:
    """Windows 콘솔(cp949)에서도 한글 출력이 깨지지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
