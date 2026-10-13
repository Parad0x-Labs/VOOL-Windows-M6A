"""Authoritative local and push/PR verification gate for VOOL."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ops.process_tree import process_group_kwargs, stop_process_tree
from ops.pytest_shards import (
    JOB_TARGETS_FILE,
    JOB_TARGETS_SCHEMA,
    REQUIRED_EXECUTED_FILES,
    select_job_targets,
    validate_pytest_args,
)

REQUIRED_PYTHON = (3, 12, 13)

#: The tools whose EXACT version the gate refuses to run without. The versions are not written here
#: -- they are read from `pyproject.toml`, which is the single place they are declared.
_PINNED_TOOLS = ("pytest", "ruff")


def _required_tools() -> dict[str, str]:
    """The pinned tool versions, read from pyproject.toml rather than copied.

    They used to be a second copy of two strings that also live in `[project.optional-dependencies]
    dev`, and the pyproject comment beside them said "Bump both lines together." That is a manual
    coordination no automated dependency update can perform, and on 2026-08-18 it failed exactly
    there: dependabot PR #87 raised `ruff==0.15.16` to `0.16.3` in pyproject, could not know about
    this file, and the gate refused its own repository --

        !! verification tool mismatch: ruff required 0.15.16, got 0.16.3

    A pin that must be edited in two places to stay true is one edit away from being false, and the
    gate is the thing that must not be wrong. Reading it means a bump lands in one place and this
    file follows.
    """

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for name in _PINNED_TOOLS:
        match = re.search(rf'"{re.escape(name)}==([^"]+)"', text)
        if match is None:
            raise SystemExit(
                f"!! {name} has no exact pin in pyproject.toml; the gate cannot verify a version "
                "that is not declared"
            )
        found[name] = match.group(1).strip()
    return found


REQUIRED_TOOLS = _required_tools()


@dataclass(frozen=True)
class StageResult:
    name: str
    command: tuple[str, ...]
    returncode: int
    log_path: Path


def exact_python_version(version_info: Any = sys.version_info) -> tuple[int, int, int]:
    return (int(version_info.major), int(version_info.minor), int(version_info.micro))


def run_stage(
    *,
    name: str,
    command: Sequence[str],
    repo_root: Path,
    log_path: Path,
    timeout_seconds: float,
    runner: Callable[..., Any] | None = None,
) -> StageResult:
    """Run a stage with its return code as the only status authority."""

    normalized = tuple(str(item) for item in command)
    process: Any | None = None
    try:
        with log_path.open("w", encoding="utf-8") as handle:
            if runner is not None:
                completed = runner(
                    normalized,
                    cwd=str(repo_root),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=max(0.01, float(timeout_seconds)),
                )
            else:
                process = subprocess.Popen(
                    normalized,
                    cwd=str(repo_root),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    **process_group_kwargs(),
                )
                process.wait(timeout=max(0.01, float(timeout_seconds)))
                completed = process
        rc = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{name} timed out: {exc}\n")
        rc = 1
    except (OSError, subprocess.SubprocessError) as exc:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{name} could not launch: {type(exc).__name__}: {exc}\n")
        rc = 1
    finally:
        if process is not None:
            stop_process_tree(process)
    return StageResult(name=name, command=normalized, returncode=rc, log_path=log_path)


def run_verification(
    *,
    repo_root: Path,
    paths: Sequence[str] = (),
    workers: int = 4,
    pytest_args: Sequence[str] = (),
    timeout_seconds: float = 1800.0,
    collection_timeout_seconds: float = 300.0,
    tail_lines: int = 200,
    log_dir: Path | None = None,
    version_info: Any = sys.version_info,
    tool_versions: dict[str, str] | None = None,
    stage_runner: Callable[..., StageResult] = run_stage,
    focused: bool = False,
    job_index: int = 1,
    job_count: int = 1,
) -> int:
    actual_version = exact_python_version(version_info)
    actual_tools = tool_versions or {
        package: _installed_version(package) for package in REQUIRED_TOOLS
    }
    actual_log_dir = log_dir or Path(mkdtemp(prefix="vool-verification-"))
    actual_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Verification log root: {actual_log_dir}", flush=True)

    normalized_paths = tuple(str(item) for item in paths)
    split = job_count > 1
    mode = "focused" if focused else ("authoritative-shard" if split else "authoritative")
    job = {"index": int(job_index), "count": int(job_count)} if split else None

    stages: list[StageResult] = []
    if job_count < 1 or not 1 <= job_index <= job_count or (split and focused):
        # A split job is one slice of the authoritative full scope, never a focused run.
        print(
            f"!! job {job_index}/{job_count} is not a valid split of the authoritative scope",
            flush=True,
        )
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=stages,
            outcome="failed",
            failure="invalid_job_split",
            mode=mode,
            targets=normalized_paths,
            job=job,
        )
        return 1
    if bool(normalized_paths) != bool(focused):
        failure = "focused_mode_required" if normalized_paths else "focused_targets_required"
        print(
            "!! explicit targets require --focused, and --focused requires explicit targets",
            flush=True,
        )
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=stages,
            outcome="failed",
            failure=failure,
            mode=mode,
            targets=normalized_paths,
            job=job,
        )
        return 1
    try:
        validated_pytest_args = validate_pytest_args(pytest_args)
    except ValueError as exc:
        print(f"!! {exc}", flush=True)
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=stages,
            outcome="failed",
            failure="unsafe_pytest_args",
            mode=mode,
            targets=normalized_paths,
            job=job,
        )
        return 1
    if actual_version != REQUIRED_PYTHON:
        print(
            "!! interpreter mismatch: required "
            f"{'.'.join(map(str, REQUIRED_PYTHON))}, got {'.'.join(map(str, actual_version))}",
            flush=True,
        )
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=stages,
            outcome="failed",
            failure="interpreter_version",
            mode=mode,
            targets=normalized_paths,
            job=job,
        )
        return 1
    mismatched_tools = {
        package: actual_tools.get(package, "missing")
        for package, expected in REQUIRED_TOOLS.items()
        if actual_tools.get(package) != expected
    }
    if mismatched_tools:
        rendered = ", ".join(
            f"{package} required {REQUIRED_TOOLS[package]}, got {actual}"
            for package, actual in mismatched_tools.items()
        )
        print(f"!! verification tool mismatch: {rendered}", flush=True)
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=stages,
            outcome="failed",
            failure="tool_version",
            mode=mode,
            targets=normalized_paths,
            job=job,
        )
        return 1

    commands = (
        (
            "lint",
            (sys.executable, "-m", "ruff", "check", "."),
            min(300.0, max(0.01, float(timeout_seconds))),
        ),
        (
            "pytest",
            (
                sys.executable,
                str(repo_root / "ops" / "pytest_shards.py"),
                *normalized_paths,
                "--workers",
                str(max(1, int(workers))),
                "--timeout-seconds",
                str(max(0.01, float(timeout_seconds))),
                "--collection-timeout-seconds",
                str(max(0.01, float(collection_timeout_seconds))),
                "--artifact-root",
                str(actual_log_dir / "pytest-shards"),
                "--tail-lines",
                str(max(0, int(tail_lines))),
                *tuple(f"--pytest-arg={item}" for item in validated_pytest_args),
                *(
                    ("--job-index", str(int(job_index)), "--job-count", str(int(job_count)))
                    if split
                    else ()
                ),
            ),
            max(
                1.0,
                float(timeout_seconds) + float(collection_timeout_seconds) + 60.0,
            ),
        ),
    )
    for name, command, stage_timeout in commands:
        print(f"==> {name}\n$ {' '.join(command)}", flush=True)
        result = stage_runner(
            name=name,
            command=command,
            repo_root=repo_root,
            log_path=actual_log_dir / f"{name}.log",
            timeout_seconds=stage_timeout,
        )
        stages.append(result)
        if result.returncode != 0:
            print(f"!! {name} failed (exit {result.returncode})", flush=True)
            _print_log_tail(result.log_path, tail_lines=tail_lines)
            _write_receipt(
                actual_log_dir,
                actual_version=actual_version,
                tool_versions=actual_tools,
                stages=stages,
                outcome="failed",
                failure=name,
                mode=mode,
                targets=normalized_paths,
                job=job,
            )
            return 1
        print(f"ok {name}", flush=True)

    # The stage return codes say the shards ran; these artifacts say what they actually did.
    # Skip drift (a lane sliding from executed to skipped) and a required lane that executed
    # nothing are invisible in a return code, so the receipt carries the counts and the
    # authoritative mode refuses to pass while a required lane is release-incapable.
    # A split job only owns part of the suite, so a required lane owned by another job is
    # "missing_from_run" here; the aggregate job enforces every required lane over all jobs.
    tests_block, tests_problem = _tests_summary_block(actual_log_dir, job_mode=split)
    if not focused and tests_problem is not None:
        print(f"!! {tests_problem['detail']}", flush=True)
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=stages,
            outcome="failed",
            failure=tests_problem["failure"],
            mode=mode,
            targets=normalized_paths,
            tests=tests_block,
            job=job,
        )
        return 1

    _write_receipt(
        actual_log_dir,
        actual_version=actual_version,
        tool_versions=actual_tools,
        stages=stages,
        outcome="focused_passed" if focused else ("shard_passed" if split else "passed"),
        failure="",
        mode=mode,
        targets=normalized_paths,
        tests=tests_block,
        job=job,
    )
    if focused:
        print("FOCUSED VERIFICATION PASSED — NOT RELEASE GATE", flush=True)
    elif split:
        print(
            f"VERIFICATION SHARD {job_index}/{job_count} PASSED — THE AGGREGATE IS THE RELEASE GATE",
            flush=True,
        )
    else:
        print("VERIFICATION PASSED", flush=True)
    return 0


def run_aggregate(
    *,
    shards_root: Path,
    job_count: int,
    log_dir: Path | None = None,
    version_info: Any = sys.version_info,
    tool_versions: dict[str, str] | None = None,
) -> int:
    """The release gate for a split run: every job passed and together they ran everything.

    Each split job uploads its own log directory. This refuses to pass unless there is exactly
    one passing receipt per job position, every job ran the exact interpreter and tools, every
    job collected the identical canonical scope, the job slices are disjoint and cover that
    scope exactly as `select_job_targets` assigns it, and the required lanes executed when the
    manifests of all jobs are counted together.
    """

    actual_version = exact_python_version(version_info)
    actual_tools = tool_versions or {
        package: _installed_version(package) for package in REQUIRED_TOOLS
    }
    actual_log_dir = log_dir or Path(mkdtemp(prefix="vool-verification-aggregate-"))
    actual_log_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[dict[str, Any]] = []

    def fail(failure: str, detail: str, tests: dict[str, Any] | None = None) -> int:
        print(f"!! {detail}", flush=True)
        _write_receipt(
            actual_log_dir,
            actual_version=actual_version,
            tool_versions=actual_tools,
            stages=(),
            outcome="failed",
            failure=failure,
            mode="authoritative-aggregate",
            targets=(),
            tests=tests,
            jobs=jobs,
        )
        return 1

    if job_count < 2:
        return fail("invalid_job_split", f"an aggregate needs a split of 2 or more jobs, got {job_count}")
    if actual_version != REQUIRED_PYTHON:
        return fail(
            "interpreter_version",
            "interpreter mismatch: required "
            f"{'.'.join(map(str, REQUIRED_PYTHON))}, got {'.'.join(map(str, actual_version))}",
        )
    if any(actual_tools.get(package) != expected for package, expected in REQUIRED_TOOLS.items()):
        return fail("tool_version", f"verification tool mismatch: {actual_tools} != {REQUIRED_TOOLS}")

    receipts = sorted(shards_root.glob("*/verification-receipt.json"))
    seen: dict[int, Path] = {}
    collected: tuple[str, ...] | None = None
    covered: dict[str, int] = {}
    for receipt_path in receipts:
        job_dir = receipt_path.parent
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            job = receipt["job"]
            index, count = int(job["index"]), int(job["count"])
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return fail("job_receipt_unreadable", f"unreadable job receipt {receipt_path}: {exc}")
        jobs.append({"index": index, "count": count, "dir": job_dir.name})
        if count != job_count or not 1 <= index <= job_count:
            return fail(
                "job_split_mismatch",
                f"{job_dir.name} ran job {index}/{count}, the aggregate expects {job_count} jobs",
            )
        if index in seen:
            return fail(
                "job_duplicated",
                f"job {index} appears twice: {seen[index].name} and {job_dir.name}",
            )
        seen[index] = job_dir
        if (
            receipt.get("schema") != "vool.verification-receipt.v1"
            or receipt.get("mode") != "authoritative-shard"
            or receipt.get("outcome") != "shard_passed"
        ):
            return fail(
                "job_not_passed",
                f"job {index} did not pass: outcome {receipt.get('outcome')!r}, "
                f"failure {receipt.get('failure')!r}",
            )
        if receipt.get("python") != ".".join(map(str, REQUIRED_PYTHON)) or receipt.get(
            "tools"
        ) != dict(sorted(REQUIRED_TOOLS.items())):
            return fail(
                "job_toolchain_mismatch",
                f"job {index} ran python {receipt.get('python')} with tools {receipt.get('tools')}",
            )
        target_files = sorted(job_dir.glob(f"pytest-shards/*/{JOB_TARGETS_FILE}"))
        if len(target_files) != 1:
            return fail(
                "job_targets_missing",
                f"job {index} has {len(target_files)} {JOB_TARGETS_FILE} files, expected 1",
            )
        try:
            slice_record = json.loads(target_files[0].read_text(encoding="utf-8"))
            if slice_record.get("schema") != JOB_TARGETS_SCHEMA:
                raise ValueError("unexpected schema")
            job_collected = tuple(str(item) for item in slice_record["collected_targets"])
            job_targets = tuple(str(item) for item in slice_record["job_targets"])
            recorded = (int(slice_record["job_index"]), int(slice_record["job_count"]))
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return fail("job_targets_missing", f"unreadable {target_files[0]}: {exc}")
        if recorded != (index, count):
            return fail(
                "job_split_mismatch",
                f"job {index} recorded slice {recorded[0]}/{recorded[1]} in {JOB_TARGETS_FILE}",
            )
        if collected is None:
            collected = job_collected
        elif job_collected != collected:
            return fail(
                "job_collection_mismatch",
                f"job {index} collected a different canonical scope than the other jobs",
            )
        if job_targets != select_job_targets(collected, job_index=index, job_count=count):
            return fail(
                "job_coverage_gap",
                f"job {index} ran a slice that is not its canonical assignment",
            )
        for target in job_targets:
            covered[target] = covered.get(target, 0) + 1

    missing_jobs = sorted(set(range(1, job_count + 1)) - set(seen))
    if missing_jobs:
        return fail(
            "job_missing",
            f"no receipt from job(s) {', '.join(map(str, missing_jobs))} of {job_count}",
        )
    assert collected is not None
    uncovered = sorted(set(collected) - set(covered))
    repeated = sorted(target for target, times in covered.items() if times > 1)
    if uncovered or repeated or set(covered) - set(collected):
        return fail(
            "job_coverage_gap",
            f"jobs did not cover the scope exactly once: {len(uncovered)} unrun, "
            f"{len(repeated)} run twice",
        )
    print(
        f"{job_count} jobs covered {len(collected)} test files exactly once",
        flush=True,
    )

    tests_block, tests_problem = _tests_summary_block(
        shards_root, manifest_glob="*/pytest-shards/*/logs/shard-*-execution.json"
    )
    if tests_block.get("state") != "aggregated":
        return fail("job_manifests_unavailable", str(tests_block.get("reason")), tests_block)
    if tests_problem is not None:
        return fail(tests_problem["failure"], tests_problem["detail"], tests_block)

    _write_receipt(
        actual_log_dir,
        actual_version=actual_version,
        tool_versions=actual_tools,
        stages=(),
        outcome="passed",
        failure="",
        mode="authoritative-aggregate",
        targets=(),
        tests=tests_block,
        jobs=sorted(jobs, key=lambda item: item["index"]),
    )
    print("VERIFICATION PASSED", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the exact Python, Ruff, canonical collection, and stable shard gate used by CI."
        )
    )
    parser.add_argument("paths", nargs="*", help="Optional pytest targets for focused regression.")
    parser.add_argument(
        "--focused",
        action="store_true",
        help="Allow explicit targets and label the result non-authoritative.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pytest-arg", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--collection-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--tail-lines", type=int, default=200)
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Stable log directory (CI uses this for unconditional artifact upload).",
    )
    parser.add_argument(
        "--job-index",
        type=int,
        default=1,
        help="1-based position of this CI job when the authoritative scope is split across jobs.",
    )
    parser.add_argument(
        "--job-count",
        type=int,
        default=1,
        help="Number of CI jobs the authoritative scope is split across (1 = no split).",
    )
    parser.add_argument(
        "--aggregate-shards",
        type=Path,
        help=(
            "Directory holding one downloaded log directory per split job; proves the jobs "
            "covered the whole scope exactly once and writes the release-gate receipt."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # C15: the ONE bounded unattended preflight — verification runs are unattended runs.
    from core.unattended_preflight import preflight

    preflight("ops.verify")
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    log_dir = args.log_dir
    if log_dir is not None and not log_dir.is_absolute():
        log_dir = repo_root / log_dir
    if args.aggregate_shards is not None:
        if args.paths or args.focused:
            print("!! the aggregate gate takes no targets and is never focused", flush=True)
            return 1
        return run_aggregate(
            shards_root=args.aggregate_shards,
            job_count=int(args.job_count),
            log_dir=log_dir,
        )
    return run_verification(
        repo_root=repo_root,
        paths=tuple(str(item) for item in args.paths),
        workers=max(1, int(args.workers)),
        pytest_args=tuple(str(item) for item in args.pytest_arg),
        timeout_seconds=max(0.01, float(args.timeout_seconds)),
        collection_timeout_seconds=max(0.01, float(args.collection_timeout_seconds)),
        tail_lines=max(0, int(args.tail_lines)),
        log_dir=log_dir,
        focused=bool(args.focused),
        job_index=int(args.job_index),
        job_count=int(args.job_count),
    )


def _tests_summary_block(
    log_dir: Path,
    *,
    job_mode: bool = False,
    manifest_glob: str = "pytest-shards/*/logs/shard-*-execution.json",
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Aggregate the shard execution manifests into receipt counts and required-lane state.

    Returns ``(block, problem)``. ``problem`` is ``None`` unless the artifacts themselves
    violate the authoritative-gate invariants: a suite that executed nothing, or a required
    lane (REQUIRED_EXECUTED_FILES) that collected its scenarios but executed none of them.
    When the pytest stage produced no readable manifests the block says so -- the shard layer
    owns enforcement, this is the receipt's truth plus a second opinion.

    In ``job_mode`` the manifests cover one slice of a split run, so a required lane another
    job owns is recorded as ``missing_from_run`` without being a problem; a lane this job ran
    that executed nothing still is.
    """

    manifests = sorted(log_dir.glob(manifest_glob))
    if not manifests:
        return {"state": "unavailable", "reason": "no shard execution manifests found"}, None
    totals: dict[str, int] = {}
    files: dict[str, dict[str, int]] = {}
    shards = 0
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("schema") != "vool.pytest-execution.v1":
                raise ValueError("unexpected schema")
            summary = payload["summary"]
            if not isinstance(summary, dict):
                raise ValueError("summary is not an object")
            for key, value in summary.items():
                if key == "files":
                    continue
                totals[key] = totals.get(key, 0) + int(value)
            raw_files = summary["files"]
            if not isinstance(raw_files, dict):
                raise ValueError("summary files is not an object")
            for path, counts in raw_files.items():
                target = files.setdefault(str(path), {})
                for key, value in counts.items():
                    target[key] = target.get(key, 0) + int(value)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return (
                {
                    "state": "unavailable",
                    "reason": f"unparseable execution manifest {manifest.name}: {exc}",
                },
                None,
            )
        shards += 1
    required: dict[str, dict[str, Any]] = {}
    problem: dict[str, str] | None = None
    executed_total = int(totals.get("executed_total", 0))
    if executed_total == 0:
        problem = {
            "failure": "zero_executed_suite",
            "detail": (
                "verification is green with zero executed tests: "
                f"{int(totals.get('skipped', 0))} skipped across {shards} shard(s)"
            ),
        }
    for name in REQUIRED_EXECUTED_FILES:
        counts = files.get(name) or {}
        executed = int(counts.get("executed_total", 0))
        skipped = int(counts.get("skipped", 0))
        if executed > 0:
            state = "executed" if skipped == 0 else "partially_skipped"
        else:
            state = "zero_executed" if counts else "missing_from_run"
        required[name] = {"executed_total": executed, "skipped": skipped, "state": state}
        if job_mode and state == "missing_from_run":
            continue
        if state != "executed" and problem is None:
            problem = {
                "failure": "required_lane_zero_executed",
                "detail": (
                    f"required lane is not release-capable: {name} executed {executed} test(s) "
                    f"({skipped} skipped, state {state}); provision the lane "
                    "(python -m playwright install chromium) or fix the required suite"
                ),
            }
    block = {
        "state": "aggregated",
        "shards": shards,
        "totals": dict(sorted(totals.items())),
        "required_lanes": {"required_executed_files": dict(sorted(required.items()))},
    }
    return block, problem


def _print_log_tail(path: Path, *, tail_lines: int) -> None:
    if tail_lines <= 0:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"!! could not read log {path}: {exc}", flush=True)
        return
    print("\n".join(lines[-tail_lines:]), flush=True)


def _write_receipt(
    log_dir: Path,
    *,
    actual_version: tuple[int, int, int],
    tool_versions: dict[str, str],
    stages: Sequence[StageResult],
    outcome: str,
    failure: str,
    mode: str,
    targets: Sequence[str],
    tests: dict[str, Any] | None = None,
    job: dict[str, int] | None = None,
    jobs: Sequence[dict[str, Any]] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema": "vool.verification-receipt.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": ".".join(map(str, actual_version)),
        "required_python": ".".join(map(str, REQUIRED_PYTHON)),
        "tools": dict(sorted(tool_versions.items())),
        "required_tools": dict(sorted(REQUIRED_TOOLS.items())),
        "outcome": outcome,
        "failure": failure,
        "mode": mode,
        "targets": list(targets),
        "tests": tests if tests is not None else {"state": "not_reached"},
        "stages": [
            {
                "name": stage.name,
                "command": list(stage.command),
                "returncode": stage.returncode,
                "log": stage.log_path.name,
            }
            for stage in stages
        ],
    }
    if job is not None:
        payload["job"] = dict(job)
    if jobs is not None:
        payload["jobs"] = [dict(item) for item in jobs]
    temporary = log_dir / "verification-receipt.json.tmp"
    destination = log_dir / "verification-receipt.json"
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _installed_version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "missing"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
