"""대시보드 화면 시험용 작업 폴더를 만든다 (scripts/ui/check_monitor.cjs가 쓴다).

    python scripts/ui/fixture.py <폴더>

진행 중·승인 대기·막힘(XSS 문자열 포함)·머지됨 기록·dispatch 오류를 하나씩 만든다.
"""

import json
import sys
import time
from pathlib import Path

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
XSS = "문구 <img src=x onerror=alert('xss')> <script>alert(1)</script> 가 글자로 보여야 함"
BUDGETS = {"session": 5.0, "reviewer_call": 0.5, "reviewer_total": 1.5, "job_total": 6.5}


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def job(job_id: str, status: str, now: float, **extra: object) -> dict[str, object]:
    history = [
        {"status": "QUEUED", "at": now - 900},
        {"status": "SPEC_READY", "at": now - 840},
        {"status": "BUILDING", "at": now - 700},
        {"status": status, "at": now - 120},
    ]
    builder = {"alias": "sonnet", "model": SONNET, "source": "auto", "reason": "Builder 기본"}
    data: dict[str, object] = {
        "id": job_id,
        "requirement": f"요구사항 {job_id}",
        "status": status,
        "created_at": now - 900,
        "history": history,
        "time": {"active_s": 600.0, "cap_s": 900},
        "risk": "LOW",
        "risk_reasons": ["contract 선언: LOW"],
        "acceptance_tests": [f"backend/tests/acceptance/test_{job_id}.py"],
        "changed_tests": [],
        "red": [{"verdict": "RED_OK"}],
        "validation": [],
        "reviews": [],
        "attempts": 0,
        "agent": "claude",
        "builder_model": "sonnet",
        "models": {"builder": builder},
        "model_changes": [],
        "blocked": None,
        "blocks": [],
        "base_branch": "main",
        "budgets_applied": BUDGETS,
    }
    data.update(extra)
    return data


def main(target: Path) -> None:
    now = time.time()
    usage = {"in": 8, "out": 1200, "cr": 30000, "cw": 3900}
    reviews = [
        {"round": 1, "reviewer": r, "verdict": "PASS", "issues": [], "cost_usd": 0.09,
         "usage": usage, "path": f"factory/reviews/J002-1-{r}.txt"}
        for r in ("code", "security", "test")
    ]  # fmt: skip
    write(
        target / "J001" / "factory" / "state.json", {"jobs": {"J001": job("J001", "BUILDING", now)}}
    )
    j002 = job("J002", "APPROVAL_REQUIRED", now, risk="MEDIUM", reviews=reviews,
               risk_reasons=["MEDIUM: DB 스키마 변경 (repository.py)"])  # fmt: skip
    write(target / "J002" / "factory" / "state.json", {"jobs": {"J002": j002}})
    (target / "J002" / "factory" / "reviews").mkdir(parents=True, exist_ok=True)
    (target / "J002" / "factory" / "reviews" / "J002-1-code.txt").write_text(
        "VERDICT: PASS\n<script>alert('review')</script>", encoding="utf-8"
    )
    blocked = {"reason": "CONFLICT", "detail": "<b>main</b> 기준 rebase 충돌"}
    j004 = job("J004", "BLOCKED", now, requirement=XSS, blocked=blocked)
    write(target / "J004" / "factory" / "state.json", {"jobs": {"J004": j004}})
    write(target / "records" / "J000.json", job("J000", "MERGED", now, merge_commit="abc1234def"))
    orchestrator = {"model": OPUS, "alias": "opus", "source": "auto",
                    "reason": "오케스트레이터 기본", "auth": "subscription"}  # fmt: skip
    write(target / "meta" / "J002" / "session.json",
          {"isolation": "host", "agent": "claude", "orchestrator": orchestrator})  # fmt: skip
    failed = {
        "job_id": None,
        "status": "SETUP_FAILED",
        "requirement": "태그 색",
        "error": "npm ci 실패",
    }
    write(target / "dispatch-20270101-000000.json",
          {"started": "20270101-000000", "base": "main", "jobs": [failed]})  # fmt: skip
    tokens = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000,
              "cache_creation_input_tokens": 100}  # fmt: skip
    tool = {"type": "tool_use", "name": "Bash",
            "input": {"command": "python scripts/factory.py red J001"}}  # fmt: skip
    message = {"id": "m1", "model": OPUS, "usage": tokens, "content": [tool]}
    event = {"type": "assistant", "parent_tool_use_id": None, "timestamp": now - 60,
             "message": message}  # fmt: skip
    (target / "logs").mkdir(parents=True, exist_ok=True)
    (target / "logs" / "J001.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
