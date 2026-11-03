"""Drive an installed VOOL over loopback, one slice at a time, and write one JSON report.

Each slice is a check against the running API plus a negative control: the same check pointed where
it must fail (nothing listening, or a commit this build is not). A slice passes only when its check
passes and its control fails; a control that passes means the harness cannot tell good from bad, so
the slice fails rather than reporting VOOL healthy. Statuses: pass, refused_as_expected (a slice whose
honest answer is a refusal), fail. tools/windows_install_smoke.ps1 runs this after launch.

    python tools/installed_slices.py --base-url http://127.0.0.1:11435 --expected-commit <sha> --out slices.json
"""
from __future__ import annotations

import argparse
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = "vool.installed_slices.v1"
_TIMEOUT = 10.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is the answer, never a hop: a redirect could point anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Loopback only: no proxy from the environment or the registry, and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _require_loopback(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError(f"not an http loopback URL, so it is not opened: {url}")


def _get_json(url: str) -> tuple[int, Any]:
    _require_loopback(url)
    try:
        with _OPENER.open(url, timeout=_TIMEOUT) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def _probe(url: str) -> tuple[int, Any, str]:
    try:
        status, body = _get_json(url)
        return status, body, ""
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"


def _check_health(base_url: str) -> tuple[bool, dict[str, Any]]:
    status, body, error = _probe(base_url + "/healthz")
    ok = status == 200 and isinstance(body, dict) and body.get("ok") is True
    return ok, {"url": base_url + "/healthz", "http_status": status, "ok_field": (body or {}).get("ok") if isinstance(body, dict) else None, "error": error}


def _check_version(base_url: str, expected_commit: str) -> tuple[bool, dict[str, Any]]:
    status, body, error = _probe(base_url + "/api/runtime/version")
    stamp = body if isinstance(body, dict) else {}
    commit_full = str(stamp.get("commit_full") or "")
    dirty = stamp.get("dirty")
    ok = status == 200 and commit_full == expected_commit and dirty is False and bool(stamp.get("app_version"))
    return ok, {
        "url": base_url + "/api/runtime/version",
        "http_status": status,
        "commit_full": commit_full,
        "expected_commit": expected_commit,
        "dirty": dirty,
        "app_version": stamp.get("app_version"),
        "error": error,
    }


def _closed_loopback_url() -> str:
    """A loopback address nothing listens on: a port the OS just handed out and took back."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _other_commit(commit: str) -> str:
    return commit[:-1] + ("0" if commit[-1:] != "0" else "1")


def _slice(name: str, check: Callable[[], tuple[bool, dict]], control: Callable[[], tuple[bool, dict]], control_is: str) -> dict:
    passed, evidence = check()
    control_passed, control_evidence = control()
    row = {
        "name": name,
        "status": "pass" if passed and not control_passed else "fail",
        "evidence": evidence,
        "control": {"is": control_is, "status": "pass" if control_passed else "fail", "evidence": control_evidence},
        "reason": "",
    }
    if control_passed:
        row["reason"] = "the negative control passed, so this check cannot tell a good install from a bad one"
    elif not passed:
        row["reason"] = "the check failed"
    return row


def run(base_url: str, expected_commit: str) -> dict:
    slices = [
        _slice(
            "health",
            lambda: _check_health(base_url),
            lambda: _check_health(_closed_loopback_url()),
            "the same check where nothing listens",
        ),
        _slice(
            "version_identity",
            lambda: _check_version(base_url, expected_commit),
            lambda: _check_version(base_url, _other_commit(expected_commit)),
            "the same check expecting a different commit",
        ),
    ]
    status = "pass" if all(row["status"] in {"pass", "refused_as_expected"} for row in slices) else "fail"
    return {"schema": SCHEMA, "status": status, "base_url": base_url, "expected_commit": expected_commit, "slices": slices}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="installed_slices")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS or parsed.path not in {"", "/"}:
        parser.error("--base-url must be http://127.0.0.1:<port>: the slices only drive a local install")
    commit = args.expected_commit.strip().lower()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        parser.error("--expected-commit must be a full 40-character commit")
    report = run(args.base_url.rstrip("/"), commit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for row in report["slices"]:
        print(f"[{row['status'].upper()}] slice/{row['name']} {row['reason']}".rstrip())
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
