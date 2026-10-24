"""팩토리 엔진 찾기·실행 (얇은 설치, 기획안 12.1).

`factory_kit.py install`이 엔진 태그에서 대상 레포로 복사하는 파일이다. 대상 레포의 shim
(`scripts/factory.py` 등)이 이 모듈로 레포 밖 엔진을 실행한다.

- 엔진 위치: `FACTORY_ENGINE_HOME`(기본 `~/.factory/engines`) 아래 `factory/kit.lock`의 태그 폴더.
- 실행 전에 엔진 파일 해시를 `factory/kit.lock`과 대조하고, 다르면 실행하지 않는다.
- 엔진에는 `FACTORY_REPO`로 이 레포를 알린다 (상태·설정을 읽을 레포, 신뢰 checkout).
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
KIT_LOCK_PATH = "factory/kit.lock"
ENGINE_HOME_ENV = "FACTORY_ENGINE_HOME"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def engine_home() -> Path:
    return Path(os.environ.get(ENGINE_HOME_ENV) or Path.home() / ".factory" / "engines")


def engine_dir(lock: dict[str, Any]) -> Path:
    return engine_home() / str(lock["engine"])


def engine_problems(lock: dict[str, Any]) -> list[str]:
    path = engine_dir(lock)
    if not path.is_dir():
        return [f"엔진 없음: {path} (엔진 원본 레포에서 factory_kit.py install로 설치)"]
    return [
        f"엔진 파일이 설치 기록과 다름: {rel}"
        for rel, digest in lock["engine_files"].items()
        if not (path / rel).is_file() or sha256(path / rel) != digest
    ]


def run(rel: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    try:
        lock = json.loads((REPO / KIT_LOCK_PATH).read_text(encoding="utf-8"))
        problems = engine_problems(lock)
    except (OSError, json.JSONDecodeError, KeyError, AttributeError):
        problems = [f"설치 기록을 읽을 수 없음: {KIT_LOCK_PATH}"]
    if problems:
        print("오류: 팩토리 엔진을 실행할 수 없습니다\n- " + "\n- ".join(problems), file=sys.stderr)
        sys.exit(2)
    os.environ["FACTORY_REPO"] = str(REPO)
    script = engine_dir(lock) / rel
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")
