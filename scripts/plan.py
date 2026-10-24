"""계획: 목표를 작업 초안 묶음으로 나눈 것을 규칙으로 검사한다 (기획안 6.9 P1).

사용법:
    python scripts/plan.py check <계획 ID>      # 예: P001
    (실행은 사람이 python scripts/dispatch.py --run-plan <계획 ID>)

계획 문서는 `factory/plans/<ID>.md`에 ```json 블록 하나로 둔다 (계획자가 쓰고 사람이 고친다):
    {"goal": "...", "jobs": [{"id", "requirement", "type", "size", "paths", "touches",
                              "depends_on", "assumptions", "design", "acceptance_hints"}]}

검사는 모델이 아니라 규칙이다.
- 예상 등급: `verify`와 같은 경로 규칙(`factory.path_risk`)·유형 규칙, 그리고 `touches`
  (api, db_schema, data_delete, test_skip)를 내용 규칙과 같은 사유로 대응시킨다. 참고값이고,
  실제 등급은 작업의 `verify`가 다시 계산한다.
- 크기: small·medium만. 더 크면 분할. medium은 설계 결정(`design`)이 있어야 한다.
- 실행 경로: 검증 약화(`test_skip`)는 needs_decision, 잠긴 경로(검증 설정·정책 파일·
  `scripts/` 등, 기존 테스트 제외)는 human(사람 직접), 나머지는 dispatch.
- `depends_on`: 있는 작업만, 순환 금지.

결과(작업별 예상 등급·근거·실행 경로)는 호스트 기록 `../<레포>.jobs/plans/<ID>.json`에
CHECKED로 남긴다 (레포 안이 아니라서 세션이 바꾸지 못한다, 기획안 9장). 검사한 문서의 해시도
남겨서, 검사 뒤 문서를 고치면 다시 검사해야 실행할 수 있다.

실행 순서(`schedule`)도 규칙이다. 선행 작업이 모두 MERGED인 작업만 시작하고, 예상 변경 경로가
겹치는 작업은 동시에 돌리지 않는다. 승인 대기·BLOCKED·사람 직접 항목에 기대는 작업은 사유와 함께
기다린다(PLAN_WAITING). 계획 상태는 CHECKED → RUNNING → DONE / STOPPED이고 작업 상태와 따로 둔다.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factory import (  # noqa: E402
    MAX_FILES,
    RISK_TOUCHES,
    RISKS,
    SIZE_CAPS,
    TYPES,
    _instructions,
    _matches,
    path_risk,
    stage_auth,
    stage_model,
    type_risk,
)
from factory_config import Config, ConfigError, load_config  # noqa: E402
from proc import resolve_command, use_utf8_output  # noqa: E402

PLAN_ID = re.compile(r"^P\d{3,}$")
PLANS_DIR = "factory/plans"
FIELDS = ("id", "requirement", "type", "size", "paths", "touches", "depends_on",
          "assumptions", "design", "acceptance_hints")  # fmt: skip
PLANNER_PROMPT = "factory/prompts/planner.md"
PLANNER_BUDGET_USD = "1.0"  # 호출당 상한 (임시값, 사람 결정)
_STRINGS = {"type": "array", "items": {"type": "string"}}
# 계획자 출력 형식. Claude는 --json-schema로 CLI가 강제한다
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "requirement": {"type": "string"},
                    "type": {"type": "string", "enum": sorted(TYPES)},
                    "size": {"type": "string", "enum": sorted(SIZE_CAPS)},
                    "paths": _STRINGS,
                    "touches": {"type": "array", "items": {"type": "string",
                                                            "enum": sorted(RISK_TOUCHES)}},
                    "depends_on": _STRINGS,
                    "assumptions": _STRINGS,
                    "design": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                    "acceptance_hints": _STRINGS,
                },
                "required": list(FIELDS),
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal", "jobs"],
    "additionalProperties": False,
}  # fmt: skip


class PlanError(Exception):
    pass


def jobs_dir(root: Path) -> Path:
    return root.parent / f"{root.name}.jobs"


def state_path(root: Path, plan_id: str) -> Path:
    return jobs_dir(root) / "plans" / f"{plan_id}.json"


def _doc(root: Path, plan_id: str) -> str:
    if not PLAN_ID.match(plan_id):
        raise PlanError(f"계획 ID는 P001 형식입니다: {plan_id}")
    path = root / PLANS_DIR / f"{plan_id}.md"
    if not path.is_file():
        raise PlanError(f"계획 문서가 없습니다: {PLANS_DIR}/{plan_id}.md")
    return path.read_text(encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_plan(root: Path, plan_id: str) -> dict[str, Any]:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", _doc(root, plan_id), re.S)
    if len(blocks) != 1:
        raise PlanError(f"{plan_id}.md에 ```json 블록이 정확히 하나 있어야 합니다")
    try:
        body = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise PlanError(f"계획 JSON 오류: {error}") from error
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list) or not body["jobs"]:
        raise PlanError("계획에는 goal과 비어 있지 않은 jobs 목록이 있어야 합니다")
    return body


def _strings(value: Any, allow_empty: bool = True) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(v, str) and v.strip() for v in value)
    )


def draft_problems(item: Any) -> list[str]:
    """작업 초안 하나의 형식 문제."""
    if not isinstance(item, dict):
        return ["작업 초안은 객체여야 합니다"]
    name = item.get("id", "?")
    errors = []
    missing = [f for f in FIELDS if f not in item]
    if missing:
        errors.append(f"{name}: 필드 없음 {missing}")
    if not isinstance(item.get("requirement"), str) or not item.get("requirement", "").strip():
        errors.append(f"{name}: requirement는 비어 있지 않은 문장")
    if item.get("type") not in TYPES:
        errors.append(f"{name}: type은 {sorted(TYPES)} 중 하나")
    if item.get("size") not in SIZE_CAPS:
        errors.append(f"{name}: size는 {sorted(SIZE_CAPS)} 중 하나 (더 크면 작업을 분할)")
    if item.get("size") == "medium" and not (
        isinstance(item.get("design"), str) and item["design"].strip()
    ):
        errors.append(f"{name}: 중형 작업에는 design(설계 결정)이 있어야 합니다")
    if not _strings(item.get("paths"), allow_empty=False):
        errors.append(f"{name}: paths(예상 변경 경로)는 비어 있지 않은 문자열 목록")
    touches = item.get("touches")
    if not isinstance(touches, list) or any(t not in RISK_TOUCHES for t in touches):
        errors.append(f"{name}: touches는 {sorted(RISK_TOUCHES)} 중에서 고른 목록")
    for key in ("depends_on", "assumptions", "acceptance_hints"):
        if not _strings(item.get(key)):
            errors.append(f"{name}: {key}는 문자열 목록 (없으면 [])")
    return errors


def _cycle(jobs: list[dict[str, Any]]) -> list[str]:
    graph = {j["id"]: j["depends_on"] for j in jobs}
    seen: dict[str, int] = {}  # 1 방문 중, 2 끝

    def visit(node: str, path: list[str]) -> list[str]:
        if seen.get(node) == 1:
            return [*path, node]
        if seen.get(node) == 2:
            return []
        seen[node] = 1
        for nxt in graph.get(node, []):
            found = visit(nxt, [*path, node])
            if found:
                return found
        seen[node] = 2
        return []

    for node in graph:
        found = visit(node, [])
        if found:
            return found
    return []


def predict(cfg: Config, item: dict[str, Any]) -> tuple[str, list[str]]:
    level, reasons = 0, []

    def raise_to(target: str, reason: str) -> None:
        nonlocal level
        reasons.append(f"{target}: {reason}")
        level = max(level, RISKS.index(target))

    if rule := type_risk(item["type"]):
        raise_to(*rule)
    for path in item["paths"]:
        if rule := path_risk(cfg, path):
            raise_to(*rule)
    for touch in item["touches"]:
        raise_to(*RISK_TOUCHES[touch])
    if len(item["paths"]) > MAX_FILES:
        raise_to("MEDIUM", f"변경 규모 {len(item['paths'])}파일 (예상)")
    return RISKS[level], reasons


def route(cfg: Config, item: dict[str, Any]) -> tuple[str, str]:
    if "test_skip" in item["touches"]:
        return "needs_decision", "검증 약화(테스트 건너뛰기) 요구: 사람이 정한다"
    locked = [
        p for p in item["paths"]
        if _matches(p, cfg.lock_patterns) and not _matches(p, cfg.test_patterns)
    ]  # fmt: skip
    if locked:
        return "human", f"팩토리 작업이 바꿀 수 없는 잠긴 경로: {', '.join(locked)}"
    return "dispatch", ""


def check_plan(root: Path, plan_id: str) -> dict[str, Any]:
    body = load_plan(root, plan_id)
    try:
        cfg = load_config(root)
    except ConfigError as error:
        raise PlanError(str(error)) from error
    errors = [e for item in body["jobs"] for e in draft_problems(item)]
    if errors:
        raise PlanError("작업 초안 형식 오류:\n- " + "\n- ".join(errors))
    jobs: list[dict[str, Any]] = body["jobs"]
    ids = [j["id"] for j in jobs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise PlanError(f"작업 id 중복: {dupes}")
    unknown = sorted({d for j in jobs for d in j["depends_on"] if d not in ids})
    if unknown:
        raise PlanError(f"depends_on에 없는 작업: {unknown}")
    cycle = _cycle(jobs)
    if cycle:
        raise PlanError(f"depends_on 순환: {' → '.join(cycle)}")
    checked = []
    for item in jobs:
        risk, reasons = predict(cfg, item)
        where, why = route(cfg, item)
        checked.append({**item, "expected_risk": risk, "risk_reasons": reasons,
                        "route": where, "route_reason": why})  # fmt: skip
    return {"id": plan_id, "goal": body.get("goal", ""), "status": "CHECKED",
            "checked_at": time.time(), "doc_sha": _sha(_doc(root, plan_id)),
            "jobs": checked}  # fmt: skip


def load_state(root: Path, plan_id: str) -> dict[str, Any] | None:
    path = state_path(root, plan_id)
    if not path.is_file():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def save_state(root: Path, state: dict[str, Any]) -> None:
    path = state_path(root, state["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_checked(root: Path, plan_id: str) -> dict[str, Any]:
    """실행할 계획 상태. 검사 기록이 없거나 검사 뒤 문서가 바뀌었으면 거부한다."""
    text = _doc(root, plan_id)
    state = load_state(root, plan_id)
    again = f"python scripts/plan.py check {plan_id}"
    if state is None:
        raise PlanError(f"{plan_id} 검사 기록이 없습니다. 먼저 {again}")
    if state.get("doc_sha") != _sha(text):
        raise PlanError(f"{plan_id}.md가 검사 뒤 바뀌었습니다. {again}로 다시 검사")
    return state


# ---------------------------------------------------------------- 실행 순서 (--run-plan)

STOP_STATUSES = {"BLOCKED", "SETUP_FAILED", "ERROR"}
# 처음부터 새 작업으로 다시 맡길 수 있는 상태 (아직 안 함, 사람이 버림, 시작 실패)
RESTART_STATUSES = {None, "DROPPED", "SETUP_FAILED", "ERROR"}


def _overlap(a: list[str], b: list[str]) -> bool:
    """예상 변경 경로가 겹치나 (같은 파일, 글롭, 폴더 포함)."""
    for x in a:
        for y in b:
            if x == y or fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x):
                return True
            if x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/"):
                return True
    return False


def _held(item: dict[str, Any], jobs: dict[str, dict[str, Any]],
          statuses: dict[str, str | None]) -> str:  # fmt: skip
    reasons = []
    for dep in item["depends_on"]:
        status = statuses.get(dep)
        if status == "MERGED":
            continue
        if jobs[dep]["route"] != "dispatch":
            reasons.append(f"{dep} 사람 직접 항목")
        elif status == "APPROVAL_REQUIRED":
            reasons.append(f"{dep} 승인 대기")
        elif status in STOP_STATUSES:
            reasons.append(f"{dep} {status}")
        elif status is None or status == "DROPPED":
            reasons.append(f"{dep} 진행 전")
        else:
            reasons.append(f"{dep} 진행 중 ({status})")
    return ", ".join(reasons)


def schedule(
    state: dict[str, Any], statuses: dict[str, str | None], skip: set[str]
) -> tuple[list[str], dict[str, str]]:
    """지금 시작할 계획 작업과, 선행 작업 때문에 기다리는 작업(사유).

    statuses: 계획 작업 → 연결된 팩토리 작업 상태 (안 했으면 없음).
    skip: 지금 도는 작업과 이번 실행에서 이미 시도한 작업 (같은 실행에서 다시 시작하지 않음).
    """
    jobs = {j["id"]: j for j in state["jobs"]}
    ready, waiting = [], {}
    for item in state["jobs"]:
        name = item["id"]
        if item["route"] != "dispatch" or name in skip:
            continue
        if statuses.get(name) not in RESTART_STATUSES:
            continue
        held = _held(item, jobs, statuses)
        if held:
            waiting[name] = held
        else:
            ready.append(name)
    start: list[str] = []
    # 지금 도는 작업 (skip 가운데 아직 결과가 없는 것)
    busy = [jobs[n]["paths"] for n in skip if n in jobs and statuses.get(n) in RESTART_STATUSES]
    for name in ready:
        if any(_overlap(jobs[name]["paths"], paths) for paths in busy):
            continue  # 경로가 겹치면 순서대로 (앞 작업이 끝난 뒤 시작)
        start.append(name)
        busy.append(jobs[name]["paths"])
    return start, waiting


def plan_status(state: dict[str, Any], statuses: dict[str, str | None]) -> str:
    jobs = {j["id"]: j for j in state["jobs"]}
    left = [
        j for j in state["jobs"] if j["route"] == "dispatch" and statuses.get(j["id"]) != "MERGED"
    ]
    if not left:
        return "DONE"
    if any(statuses.get(j["id"]) in STOP_STATUSES for j in left) or any(
        jobs[d]["route"] != "dispatch" for j in left for d in j["depends_on"]
    ):
        return "STOPPED"
    return "RUNNING"


def plan_context(plan_id: str, item: dict[str, Any]) -> str:
    """세션 프롬프트에 붙일 계획 내용. 사람이 계획을 실행했으므로 설계 결정은 합의된 것이다."""
    lines = [f"이 작업은 계획 {plan_id}의 작업 {item['id']}다 (사람이 계획을 확인하고 실행함)."]
    if item.get("design"):
        lines.append(
            f"설계 결정 (사람이 합의함. spec은 이를 따르고, 다르게 해야 하면 assumptions에 "
            f"이유를 적는다): {item['design']}"
        )
    lines += [f"계획의 가정: {a}" for a in item.get("assumptions") or []]
    lines += [f"수락 기준 힌트: {h}" for h in item.get("acceptance_hints") or []]
    lines.append(f"예상 변경 경로: {', '.join(item['paths'])} (예상 등급 {item['expected_risk']})")
    return "\n".join(lines)


def planner_command(root: Path) -> list[str]:
    """읽기 전용 계획자 프로세스 (리뷰어와 같은 방식). 지시문은 stdin으로 넘긴다."""
    model = stage_model(root, "planner", {"agent": "claude"})["model"]
    return [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--max-budget-usd", os.environ.get("FACTORY_PLANNER_BUDGET_USD") or PLANNER_BUDGET_USD,
        "--json-schema", json.dumps(PLAN_SCHEMA),
        "--allowedTools", "Read", "Glob", "Grep",
    ]  # fmt: skip


def planner_prompt(root: Path, goal: str) -> str:
    cfg = load_config(root)
    locked = [p for p in cfg.lock_patterns if not _matches(p, cfg.test_patterns)]
    touches = ", ".join(f"{k}({reason})" for k, (_, reason) in RISK_TOUCHES.items())
    rules = [
        f"- HIGH 경로: {', '.join(cfg.high_paths)}",
        f"- MEDIUM 경로: {', '.join(cfg.medium_paths)}",
        f"- 팩토리 작업이 바꿀 수 없는 잠긴 경로(기존 테스트 제외): {', '.join(locked)}",
        f"- touches 표시: {touches}",
        "- 중형(medium)에는 design(설계 결정)이 필요하다. 크기 상한: small 15분, medium 30분",
    ]
    default = "목표를 팩토리 작업 초안으로 나눈다. JSON으로만 답한다."
    return "\n\n".join([
        _instructions(root, PLANNER_PROMPT, default),
        f"## 목표\n{goal}",
        "## 이 레포의 규칙 (factory/config.json)\n" + "\n".join(rules),
    ])  # fmt: skip


def next_plan_id(root: Path) -> str:
    used = [int(p.stem[1:]) for p in (root / PLANS_DIR).glob("P*.md") if PLAN_ID.match(p.stem)]
    return f"P{max(used, default=0) + 1:03d}"


def draft_plan(root: Path, goal: str) -> str:
    """계획자를 한 번 불러 계획 문서를 쓰고 ID를 돌려준다. 검사는 따로 한다 (check)."""
    raw = os.environ.get("FACTORY_PLANNER_CMD")
    cmd = [str(a) for a in json.loads(raw)] if raw else planner_command(root)
    _, add, unset = stage_auth(root, "planner")
    env = {k: v for k, v in os.environ.items() if k not in unset} | add
    try:
        done = subprocess.run(
            resolve_command(cmd), input=planner_prompt(root, goal), cwd=root, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=900, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PlanError(f"계획자를 실행하지 못했습니다: {error}") from error
    body = None
    try:
        envelope = json.loads(done.stdout)
        structured = envelope.get("structured_output") if isinstance(envelope, dict) else None
        body = structured if isinstance(structured, dict) else json.loads(envelope["result"])
    except (json.JSONDecodeError, KeyError, TypeError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise PlanError(
            f"계획자 출력에서 계획 JSON을 찾지 못했습니다 (exit {done.returncode}): "
            f"{done.stderr[-300:]}"
        )
    plan_id = next_plan_id(root)
    path = root / PLANS_DIR / f"{plan_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"goal": body.get("goal") or goal, "jobs": body["jobs"]},
                      ensure_ascii=False, indent=2)  # fmt: skip
    path.write_text(
        f"# {plan_id} 계획 (계획자 초안, 사람이 확인·수정)\n\n"
        f"목표: {goal}\n\n"
        f"작업 초안을 고친 뒤 `python scripts/plan.py check {plan_id}`로 다시 검사한다.\n\n"
        f"```json\n{text}\n```\n",
        encoding="utf-8",
    )
    return plan_id


def render(state: dict[str, Any]) -> str:
    say = {"dispatch": "팩토리", "human": "사람 직접", "needs_decision": "결정 필요"}
    lines = [
        f"# {state['id']} 계획 검사: CHECKED",
        f"목표: {state['goal']}",
        "",
        "| 작업 | 요구사항 | 유형/규모 | 예상 등급 | 실행 | 선행 |",
        "|---|---|---|---|---|---|",
    ]
    for j in state["jobs"]:
        lines.append(
            f"| {j['id']} | {j['requirement']} | {j['type']}/{j['size']} | {j['expected_risk']} "
            f"| {say[j['route']]} | {', '.join(j['depends_on']) or '-'} |"
        )
    notes = [f"- {j['id']}: {j['route_reason']}" for j in state["jobs"] if j["route_reason"]]
    if notes:
        lines += ["", "팩토리로 보내지 않는 작업:", *notes]
    lines += [
        "",
        "예상 등급은 참고값이다. 실제 등급은 작업마다 verify가 다시 계산한다.",
        f"확인 후 실행 (사람 전용): python scripts/dispatch.py --run-plan {state['id']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    use_utf8_output()
    root = root or Path(os.environ.get("FACTORY_REPO") or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(prog="plan", description="계획 검사 (기획안 6.9 P1)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check").add_argument("plan_id")
    args = parser.parse_args(argv)
    try:
        state = check_plan(root, args.plan_id)
    except PlanError as error:
        print(f"오류: {error}")
        return 2 if "없습니다" in str(error) and "계획 문서" in str(error) else 1
    previous = load_state(root, args.plan_id) or {}
    # 다시 검사해도 이미 실행한 작업의 연결은 남긴다 (계획에 남아 있는 작업만)
    ids = {j["id"] for j in state["jobs"]}
    runs = {k: v for k, v in (previous.get("runs") or {}).items() if k in ids}
    if runs:
        state["runs"] = runs
    save_state(root, state)
    print(render(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
