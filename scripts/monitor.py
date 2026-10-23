"""팩토리 로컬 대시보드 (읽기 전용).

사용법:
    python scripts/monitor.py [--port 8765] [--jobs-dir ../<레포>.jobs] [--no-open]

`127.0.0.1`에서만 연다. 상태를 바꾸지 않고(GET만), 작업 코드를 실행하지 않는다(Git 조회만).
화면에 필요한 명령은 복사용 텍스트로만 보인다. 설계: sw-factory 기획안 `dashboard/` 폴더.

데이터 출처와 신뢰 등급 (설계안 7장):
- 세션 작성 (session): 작업 worktree 안 파일, 세션 로그, records/(상태의 복사본)
- 호스트 작성 (host): meta/<ID>/session.json, meta/<ID>/host.json, dispatch-*.json
- Git 확인 (git): 대시보드가 Git으로 직접 확인한 값
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report  # noqa: E402
from factory_config import BUDGET_DEFAULTS, ConfigError, load_config  # noqa: E402

JOB_ID = re.compile(r"^J\d{3,}$")
HEX = re.compile(r"^[0-9a-f]{7,40}$")
REVIEW_FILE = re.compile(r"^J\d{3,}-(\d+-(code|security|test)\.txt|approval\.md)$")
SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
)
REVIEWERS = ("code", "security", "test")

# 상태 → 보드 컬럼 (설계안 6.3). 여기 없는 상태는 경고로 올린다
COLUMN_OF = {
    "QUEUED": "spec",
    "SPEC_READY": "spec",
    "BUILDING": "build",
    "RETRY_REQUIRED": "build",
    "VERIFYING": "review",
    "APPROVAL_REQUIRED": "approval",
    "APPROVED": "merging",
    "MERGED": "merged",
    "BLOCKED": "blocked",
    "SETUP_FAILED": "error",
    "ERROR": "error",
    "DROPPED": "hidden",
}
# 레인 구간 (같은 구간이 이어지면 합친다)
STAGE_OF = {
    "QUEUED": "spec",
    "SPEC_READY": "spec",
    "BUILDING": "build",
    "RETRY_REQUIRED": "build",
    "VERIFYING": "review",
    "APPROVAL_REQUIRED": "wait",
    "APPROVED": "merge",
    "MERGED": "merged",
    "BLOCKED": "blocked",
    "DROPPED": "dropped",
}
CLOSED = {"MERGED", "DROPPED"}
# 충돌 예고에서 뺄 팩토리 산출물
ARTIFACTS = ("factory/specs/", "factory/reviews/")
ARTIFACT_FILES = {"factory/state.json", "factory-state.md", "factory/heartbeat.json"}


def column_of(status: str) -> str | None:
    return COLUMN_OF.get(status)


# ---------------------------------------------------------------- 파일 읽기


class FileCache:
    """(수정 시각, 크기)가 같으면 다시 읽지 않는다. 쓰는 중인 JSON은 이전 값을 유지한다."""

    def __init__(self) -> None:
        self._values: dict[Path, tuple[tuple[int, int], Any]] = {}
        self._errors: dict[Path, int] = {}
        self.reads = 0

    def read_json(self, path: Path) -> Any:
        try:
            st = path.stat()
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        cached = self._values.get(path)
        if cached and cached[0] == key:
            return cached[1]
        self.reads += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            self._errors[path] = self._errors.get(path, 0) + 1
            return cached[1] if cached else None
        self._errors[path] = 0
        self._values[path] = (key, value)
        return value

    def errors(self, path: Path) -> int:
        return self._errors.get(path, 0)


class LogTail:
    """append 전용 로그를 마지막 위치부터 이어 읽는다. 끝이 줄바꿈이 아닌 줄은 다음에."""

    def __init__(self) -> None:
        self._offset: dict[Path, int] = {}
        self.events: dict[Path, list[dict[str, Any]]] = {}

    def read_new(self, path: Path) -> list[dict[str, Any]]:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        offset = self._offset.get(path, 0)
        if size < offset:  # 새로 시작한 로그
            offset = 0
            self.events[path] = []
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self._offset[path] = offset + end + 1
        new = []
        for line in data[: end + 1].splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                new.append(event)
        self.events.setdefault(path, []).extend(new)
        return new


# ---------------------------------------------------------------- 작업 찾기 (D1)


def _read(path: Path, cache: FileCache | None) -> Any:
    if cache is not None:
        return cache.read_json(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def discover(root: Path, jobs_dir: Path, cache: FileCache | None = None) -> list[dict[str, Any]]:
    """worktree 상태, records/, 기본 checkout(대화형), dispatch 결과에만 있는 작업 (설계안 8.1)."""
    refs: dict[str, dict[str, Any]] = {}
    for path in sorted((jobs_dir / "records").glob("*.json")):
        if not JOB_ID.match(path.stem):
            continue
        job = _read(path, cache)
        if isinstance(job, dict) and job.get("id") == path.stem:
            refs[path.stem] = {"key": path.stem, "id": path.stem, "source": "record",
                               "path": path, "worktree": None, "job": job,
                               "status": job.get("status")}  # fmt: skip
    for state_path in sorted(jobs_dir.glob("*/factory/state.json")):
        folder = state_path.parents[1].name
        if not JOB_ID.match(folder):
            continue
        state = _read(state_path, cache)
        job = ((state or {}).get("jobs") or {}).get(folder) if isinstance(state, dict) else None
        if isinstance(job, dict):
            refs[folder] = {"key": folder, "id": folder, "source": "worktree", "path": state_path,
                            "worktree": state_path.parents[1], "job": job,
                            "status": job.get("status")}  # fmt: skip
        elif cache is not None and cache.errors(state_path) and folder not in refs:
            refs[folder] = {"key": folder, "id": folder, "source": "worktree", "path": state_path,
                            "worktree": state_path.parents[1], "job": None,
                            "status": "READ_ERROR"}  # fmt: skip
    local = _read(root / "factory" / "state.json", cache)
    for job_id, job in ((local or {}).get("jobs") or {}).items() if isinstance(local, dict) else []:
        if JOB_ID.match(job_id) and isinstance(job, dict):
            refs[f"local-{job_id}"] = {"key": f"local-{job_id}", "id": job_id,
                                       "source": "interactive",
                                       "path": root / "factory" / "state.json",
                                       "worktree": root, "job": job,
                                       "status": job.get("status")}  # fmt: skip
    for path in sorted(jobs_dir.glob("dispatch-*.json")):
        rep = _read(path, cache)
        for i, item in enumerate((rep or {}).get("jobs", []) if isinstance(rep, dict) else []):
            job_id = item.get("job_id")
            if job_id and job_id in refs:
                continue
            if item.get("status") not in {"SETUP_FAILED", "ERROR"}:
                continue
            key = f"dispatch-{rep.get('started', path.stem)}-{i}"
            refs[key] = {"key": key, "id": job_id or "-", "source": "dispatch", "path": path,
                         "worktree": None, "job": None, "status": item.get("status"),
                         "dispatch_item": item}  # fmt: skip
    return list(refs.values())


# ---------------------------------------------------------------- 레인 (D11a)


def lanes(
    jobs: list[dict[str, Any]], changed: dict[str, list[str]], now: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """작업별 구간과 같은 파일을 고치는 진행 중 작업 쌍 (설계안 6.0). 표시만 한다."""
    out = []
    for job in jobs:
        segments: list[dict[str, Any]] = []
        history = job.get("history") or []
        for entry in history:
            stage = STAGE_OF.get(entry["status"], "spec")
            if segments and segments[-1]["stage"] == stage:
                continue
            if segments:
                segments[-1]["end"] = entry["at"]
            segments.append({"stage": stage, "start": entry["at"], "end": None})
        if segments and job.get("status") in CLOSED:
            segments[-1]["end"] = segments[-1]["start"]
        out.append({"key": job["key"], "status": job.get("status"), "segments": segments})
    active = [j["key"] for j in jobs if j.get("status") not in CLOSED and j["key"] in changed]
    conflicts = []
    for i, a in enumerate(sorted(active)):
        for b in sorted(active)[i + 1 :]:
            shared = sorted(
                f
                for f in set(changed[a]) & set(changed[b])
                if not f.startswith(ARTIFACTS) and f not in ARTIFACT_FILES
            )
            if shared:
                conflicts.append({"a": a, "b": b, "files": shared})
    return out, conflicts


# ---------------------------------------------------------------- 수집 (D5~D7)


def _ts(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def mask(text: str) -> str:
    return SECRET.sub("[가림]", text)


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def activity_items(events: list[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    """세션 로그 이벤트 → 한 줄 활동 (도구 호출, 서브에이전트, 결과). 키 형태 문자열은 가린다."""
    items = []
    for n, event in enumerate(events, start=start + 1):
        kind = event.get("type")
        at = _ts(event.get("timestamp"))
        text = None
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    data = block.get("input") or {}
                    arg = data.get("command") or data.get("description") or data.get("file_path")
                    arg = arg or data.get("subagent_type") or ""
                    text = f"{block.get('name')}  {_short(arg)}"
                    break
        elif kind == "system" and event.get("subtype") in {"task_started", "task_notification"}:
            status = event.get("status") or "시작"
            text = (
                f"task  {_short(event.get('description') or event.get('summary') or '')} ({status})"
            )
        elif kind == "result":
            text = f"결과  {event.get('subtype', '')} · ${event.get('total_cost_usd', '-')}"
        elif kind == "item.completed":
            item = event.get("item") or {}
            what = item.get("command") or item.get("text") or ""
            text = f"{item.get('type', 'item')}  {_short(what)}"
        elif kind == "turn.completed":
            text = "turn 완료"
        if text:
            items.append({"n": n, "at": at, "text": mask(_short(text, 160))})
    return items


def _group_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in history:
        if out and out[-1]["status"] == entry["status"]:
            out[-1]["count"] += 1
        else:
            out.append({"status": entry["status"], "at": entry["at"], "count": 1})
    return out


class Collector:
    """화면에 줄 값을 모은다. 파일은 캐시로 바뀐 것만 다시 읽는다."""

    def __init__(self, root: Path, jobs_dir: Path, now: Callable[[], float] = time.time):
        self.root = root
        self.jobs_dir = jobs_dir
        self.now = now
        self.cache = FileCache()
        self.tail = LogTail()
        self._usage: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._changed: dict[str, tuple[float, list[str]]] = {}
        self._prices: tuple[float, dict[str, float]] = (0.0, {})

    # ------------------------------------------------ 공통
    def _git(self, cwd: Path, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)  # fmt: skip
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _log(self, job_id: str) -> Path:
        return self.jobs_dir / "logs" / f"{job_id}.jsonl"

    def prices(self) -> dict[str, float]:
        if self.now() - self._prices[0] > 30:
            logs = sorted((self.jobs_dir / "logs").glob("*.jsonl"))
            self._prices = (self.now(), report.unit_prices(logs))
        return self._prices[1]

    def usage(self, ref: dict[str, Any]) -> dict[str, Any] | None:
        if ref["source"] in {"dispatch", "interactive"}:
            return None
        log = self._log(ref["id"])
        try:
            st = log.stat()
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        cached = self._usage.get(log)
        if cached and cached[0] == key:
            return cached[1]
        builder = (((ref.get("job") or {}).get("models") or {}).get("builder") or {}).get("model")
        value = report.live_usage(log, self.prices(), builder_model=builder)
        self._usage[log] = (key, value)
        return value

    def changed(self, ref: dict[str, Any]) -> list[str]:
        """진행 중 worktree의 바뀐 파일 (Git 확인). 10초 캐시."""
        worktree = ref.get("worktree")
        job = ref.get("job") or {}
        if ref["source"] != "worktree" or not worktree or job.get("status") in CLOSED:
            return []
        cached = self._changed.get(ref["key"])
        if cached and self.now() - cached[0] < 10:
            return cached[1]
        fork = self._git(worktree, "merge-base", "HEAD", job.get("base_branch") or "main")
        files = [f["path"] for f in report.changed_files(worktree, fork)] if fork else []
        self._changed[ref["key"]] = (self.now(), files)
        return files

    def session_info(self, job_id: str) -> dict[str, Any] | None:
        if not JOB_ID.match(job_id):
            return None
        data = self.cache.read_json(self.jobs_dir / "meta" / job_id / "session.json")
        return data if isinstance(data, dict) else None

    def host_info(self, job_id: str) -> dict[str, Any] | None:
        if not JOB_ID.match(job_id):
            return None
        data = self.cache.read_json(self.jobs_dir / "meta" / job_id / "host.json")
        return data if isinstance(data, dict) else None

    def refs(self) -> list[dict[str, Any]]:
        return discover(self.root, self.jobs_dir, self.cache)

    def base_branch(self) -> str:
        return self._git(self.root, "branch", "--show-current") or "main"

    # ------------------------------------------------ 스냅샷 (D5)
    def snapshot(self) -> dict[str, Any]:
        refs = self.refs()
        warnings = []
        try:
            load_config(self.root)
        except ConfigError as error:
            warnings.append(f"설정 오류: {error}")
        cards, real_jobs, lane_jobs, changed = [], [], [], {}
        subscription = None
        sub_at = -1.0
        totals = {"in": 0, "out": 0, "cr": 0, "cw": 0, "usd": 0.0}
        estimated_any = False
        for ref in refs:
            status = str(ref.get("status"))
            column = column_of(status)
            if status == "READ_ERROR":
                warnings.append(f"{ref['key']}: 상태 파일을 읽지 못했어요 (다음 갱신에 다시 시도)")
                continue
            if column is None:
                warnings.append(f"알 수 없는 상태 `{status}` (작업 {ref['key']})")
                continue
            job = ref.get("job") or {}
            live = self.usage(ref)
            if live and live.get("subscription"):
                mtime = self._log(ref["id"]).stat().st_mtime
                if mtime > sub_at:
                    subscription, sub_at = {**live["subscription"], "at": mtime}, mtime
            cost = report.job_cost(job, live) if job else None
            tokens = dict((live or {}).get("total") or {})
            for r in job.get("reviews") or []:
                for k, v in (r.get("usage") or {}).items():
                    tokens[k] = int(tokens.get(k) or 0) + int(v or 0)
            if cost:
                totals["usd"] += cost["total_usd"]
                estimated_any = estimated_any or cost["estimated"]
            for k in ("in", "out", "cr", "cw"):
                totals[k] += int(tokens.get(k) or 0)
            if job:
                real_jobs.append(job)
                lane_jobs.append({**job, "key": ref["key"]})
                files = self.changed(ref)
                if files:
                    changed[ref["key"]] = files
            cards.append(self._card(ref, column, job, cost, tokens))
        lane_list, conflicts = lanes(lane_jobs, changed, self.now())
        kpi = report.metrics(real_jobs) if real_jobs else None
        leads = [m for m in (report._lead_minutes(j) for j in real_jobs) if m is not None]
        dispatches = sorted(self.jobs_dir.glob("dispatch-*.json"))
        latest = self.cache.read_json(dispatches[-1]) if dispatches else None
        return {
            "generated_at": self.now(),
            "repo": {"name": self.root.name, "base": self.base_branch()},
            "dispatch": (
                {
                    "stamp": latest.get("started"),
                    "total": len(latest.get("jobs", [])),
                    "max_parallel": latest.get("max_parallel"),
                }  # fmt: skip
                if isinstance(latest, dict)
                else None
            ),
            "subscription": subscription,
            "kpi": {
                "success_rate": kpi["success_rate"] if kpi else None,
                "intervention_rate": kpi["intervention_rate"] if kpi else None,
                "total": kpi["total"] if kpi else 0,
                "lead_time_min_median": statistics.median(leads) if leads else None,
                "cost_usd": round(totals["usd"], 4),
                "cost_estimated": estimated_any,
                "tokens": {k: totals[k] for k in ("in", "out", "cr", "cw")},
            },
            "attention": self._attention(cards),
            "jobs": cards,
            "lanes": lane_list,
            "conflicts": conflicts,
            "warnings": warnings,
        }

    def _card(self, ref: dict[str, Any], column: str, job: dict[str, Any],
              cost: dict[str, Any] | None, tokens: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
        latest_round = max((int(r.get("round", 0)) for r in job.get("reviews") or []), default=0)
        reviewers = {
            r["reviewer"]: r.get("verdict") or "UNREADABLE"
            for r in job.get("reviews") or []
            if int(r.get("round", 0)) == latest_round
        }
        info = self.session_info(ref["id"]) if ref["source"] != "interactive" else None
        history = job.get("history") or []
        item = ref.get("dispatch_item") or {}
        return {
            "key": ref["key"],
            "id": ref["id"],
            "source": ref["source"],
            "status": ref.get("status"),
            "column": column,
            "risk": job.get("risk"),
            "requirement": job.get("requirement") or item.get("requirement") or "",
            "agent": job.get("agent") or item.get("agent"),
            "builder_model": job.get("builder_model"),
            "promoted": bool(job.get("model_changes")),
            "attempts": job.get("attempts", 0),
            "time": job.get("time")
            and {
                "active_s": job["time"].get("active_s"),
                "cap_s": job["time"].get("cap_s"),
            },  # fmt: skip
            "cost": cost
            and {
                **cost,
                "cap_usd": (job.get("budgets_applied") or BUDGET_DEFAULTS)["job_total"],
            },  # fmt: skip
            "tokens": {k: int(tokens.get(k) or 0) for k in ("in", "out", "cr", "cw")},
            "reviewers": reviewers,
            "round": latest_round,
            "blocked": job.get("blocked"),
            "error": item.get("error"),
            "isolation": (info or {}).get("isolation"),
            "orchestrator": (info or {}).get("orchestrator"),
            "last_transition_at": history[-1]["at"] if history else None,
            "trust": "session" if job else "host",
        }

    @staticmethod
    def _attention(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order = {"APPROVAL_REQUIRED": 1, "BLOCKED": 2, "SETUP_FAILED": 3, "ERROR": 3}
        items = [c for c in cards if c["status"] in order]
        items.sort(key=lambda c: (order[c["status"]], c.get("risk") != "HIGH",
                                  c.get("last_transition_at") or 0))  # fmt: skip
        return [{"key": c["key"], "status": c["status"], "risk": c.get("risk")} for c in items]

    # ------------------------------------------------ 상세 (D6)
    def _ref(self, key: str) -> dict[str, Any] | None:
        if not (JOB_ID.match(key) or re.match(r"^local-J\d{3,}$", key)):
            return None
        return next((r for r in self.refs() if r["key"] == key and r.get("job")), None)

    def next_commands(self, ref: dict[str, Any]) -> list[str]:
        """상태별 다음 명령. 정해진 형식의 값(ID, dispatch가 만든 경로, 커밋 해시)만 넣는다."""
        job, job_id = ref["job"], ref["id"]
        status = job.get("status")
        dispatched = ref["source"] != "interactive"
        worktree = self.jobs_dir / job_id
        if status == "APPROVAL_REQUIRED" and dispatched:
            base = (
                job.get("base_branch")
                if re.match(r"^[\w./-]+$", str(job.get("base_branch")))
                else "main"
            )
            return [f"python scripts/dispatch.py --approve {job_id}",
                    f'python scripts/dispatch.py --reject {job_id} --reason "<이유>"',
                    f"git -C {worktree} diff "
                    f"$(git -C {worktree} merge-base HEAD {base})"]  # fmt: skip
        if status == "APPROVAL_REQUIRED":
            return [f"python scripts/factory.py approve {job_id}",
                    f'python scripts/factory.py approve {job_id} --reject "<이유>"']  # fmt: skip
        if status == "BLOCKED":
            where = f"cd {worktree} && " if dispatched else ""
            return [f"{where}python scripts/factory.py resume {job_id}",
                    f"{where}python scripts/factory.py resume {job_id} --extend",
                    f"{where}python scripts/factory.py drop {job_id}"]  # fmt: skip
        commit = str(job.get("merge_commit") or "")
        if status == "MERGED" and HEX.match(commit):
            return [f"git revert -m 1 {commit[:12]}"]
        return ["python scripts/factory.py status --all"]

    def detail(self, key: str) -> dict[str, Any] | None:
        ref = self._ref(key)
        if ref is None:
            return None
        job = ref["job"]
        job_id = ref["id"]
        live = self.usage(ref)
        info = self.session_info(job_id) if ref["source"] != "interactive" else None
        host = self.host_info(job_id) if ref["source"] != "interactive" else None
        verify = [v for v in job.get("validation") or [] if v.get("kind") == "verify"]
        commit = str(job.get("merge_commit") or "")
        merged = bool(HEX.match(commit)) and report.merged_into(
            self.root, commit, job.get("base_branch") or self.base_branch()
        )
        files = []
        if ref["source"] == "worktree" and ref.get("worktree"):
            fork = self._git(
                ref["worktree"], "merge-base", "HEAD", job.get("base_branch") or "main"
            )
            files = report.changed_files(ref["worktree"], fork) if fork else []
        return {
            "key": ref["key"],
            "id": job_id,
            "source": ref["source"],
            "status": job.get("status"),
            "requirement": job.get("requirement"),
            "risk": job.get("risk"),
            "risk_reasons": job.get("risk_reasons") or [],
            "agent": job.get("agent"),
            "worktree": str(ref["worktree"]) if ref.get("worktree") else None,
            "next": self.next_commands(ref),
            "evidence": {
                "trust": "session",
                "red": [r.get("verdict") for r in job.get("red") or []],
                "verify": [
                    {
                        "green": v.get("green_verdict"),
                        "exit": v.get("verify_exit"),
                        "args": v.get("verify_args"),
                    }
                    for v in verify
                ],  # fmt: skip
                "last_failure": job.get("last_failure"),
                "acceptance_tests": job.get("acceptance_tests") or [],
                "changed_tests": job.get("changed_tests") or [],
            },
            "host": {
                "trust": "host",
                "events": (host or {}).get("events", []),
                "merge_commit": commit or None,
                "merged_into_base": merged,
                "merge_trust": "git" if merged else None,
            },
            "orchestrator": {**(info or {}).get("orchestrator", {}), "trust": "host"}
            if info
            else None,
            "isolation": (info or {}).get("isolation"),
            "models": {"trust": "session", **(job.get("models") or {})},
            "reviews": [
                {
                    k: r.get(k)
                    for k in ("round", "reviewer", "verdict", "issues", "cost_usd", "usage", "path")
                }
                for r in job.get("reviews") or []
            ],
            "files": {"trust": "git", "items": files},
            "cost": self._cost(job, live),
            "history": _group_history(job.get("history") or []),
            "blocked": job.get("blocked"),
            "blocks": job.get("blocks") or [],
            "time": job.get("time"),
            "attempts": job.get("attempts", 0),
            "budgets_applied": job.get("budgets_applied"),
        }

    def _cost(self, job: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
        roles = []
        for name, role in ((live or {}).get("roles") or {}).items():
            roles.append({"role": name, "model": role.get("model"), "usd": role.get("usd"),
                          "usage": {k: role.get(k) for k in ("in", "out", "cr", "cw")},
                          "unattributed": role.get("unattributed", 0),
                          "estimated": bool((live or {}).get("estimated"))})  # fmt: skip
        per: dict[str, dict[str, Any]] = {}
        for r in job.get("reviews") or []:
            agg = per.setdefault(
                r["reviewer"], {"usd": 0.0, "usage": {"in": 0, "out": 0, "cr": 0, "cw": 0}}
            )
            agg["usd"] += float(r.get("cost_usd") or 0)
            for k, v in (r.get("usage") or {}).items():
                agg["usage"][k] = agg["usage"].get(k, 0) + int(v or 0)
        model_of = {
            k.removeprefix("reviewer_"): v.get("model")
            for k, v in (job.get("models") or {}).items()
        }
        for name in REVIEWERS:
            if name in per:
                roles.append({"role": f"reviewer_{name}", "model": model_of.get(name),
                              "usd": round(per[name]["usd"], 6), "usage": per[name]["usage"],
                              "unattributed": 0, "estimated": False})  # fmt: skip
        return {
            "basis": (live or {}).get("basis"),
            "job": report.job_cost(job, live),
            "roles": roles,
            "budgets": job.get("budgets_applied") or BUDGET_DEFAULTS,
        }

    # ------------------------------------------------ 활동 (D7)
    def activity(self, key: str, after: int = 0, limit: int = 30) -> list[dict[str, Any]]:
        ref = self._ref(key)
        if ref is None or ref["source"] == "interactive":
            return []
        log = self._log(ref["id"])
        self.tail.read_new(log)
        items = activity_items(self.tail.events.get(log, []))
        return [i for i in items if i["n"] > after][-limit:]
