"""Pick the Linux test files a draft PR's change can reach.

Draft PRs into windows/v0.7.1-port do not run the ten-shard suite (ci.yml gates the
``tests`` and ``macos`` jobs on ready-for-review). They run lint, the canonical collection
and the files this script selects, so a draft still gets a fast, relevant signal on every
push. The full suite runs once the PR is marked ready, and on every push after that.

Selection, over the canonically collected files only:
  * a changed test file;
  * a test file that names a changed Python module (``core.safe_git``, ``from core
    import safe_git`` or its repo path) or the ``tests/test_<stem>.py`` file of a changed
    module; modules reached only through other modules are not followed;
  * a test file that names a changed non-Python file by its repo path or file name.
Files the ``macos`` job runs are left out (they are macOS-only). A change to shared test
setup (a conftest.py, pyproject.toml, requirements, uv.lock) is reported, not expanded:
that reach is the full suite's job.

This is a draft-time signal, never the merge gate: the selection may miss a test, and the
ready-for-review run of the full suite is what the merge is judged on.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

WIDE_REACH_NAMES = {"conftest.py", "pyproject.toml", "uv.lock", "setup.cfg", "pytest.ini"}
MIN_NAME_MATCH_LENGTH = 8
COLLECTION_LOG_TAIL_LINES = 200


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def collected_files(repo: Path) -> list[str]:
    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    result = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        # The node list is long; the collection errors and summary are at its end.
        tail = result.stdout.splitlines()[-COLLECTION_LOG_TAIL_LINES:]
        print("\n".join(tail), file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return sorted({line.split("::", 1)[0] for line in result.stdout.splitlines() if "::" in line})


def macos_job_files(workflow_text: str) -> set[str]:
    jobs = yaml.safe_load(workflow_text)["jobs"]
    step = next(s for s in jobs["macos"]["steps"] if s.get("name") == "Run the macOS-only suites")
    return {token for token in str(step["run"]).split() if token.startswith("tests/")}


def is_wide_reach(path: str) -> bool:
    name = Path(path).name
    return name in WIDE_REACH_NAMES or (name.startswith("requirements") and name.endswith(".txt"))


def _module_patterns(path: str) -> list[re.Pattern[str]]:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(part.isidentifier() for part in parts):
        return []
    dotted = ".".join(parts)
    patterns = [re.compile(r"(?<![\w.])" + re.escape(dotted) + r"(?![\w])")]
    if len(parts) > 1:
        parent, name = ".".join(parts[:-1]), parts[-1]
        patterns.append(re.compile(
            r"from\s+" + re.escape(parent) + r"\s+import\s+(\([^)]*|[^\n]*)\b" + re.escape(name) + r"\b"
        ))
    return patterns


def select(changed: list[str], collected: list[str], macos_only: set[str], read_text) -> tuple[list[str], list[str]]:
    """Return (selected test files, wide-reach changes that were reported, not expanded)."""
    candidates = [f for f in collected if f not in macos_only]
    candidate_set = set(candidates)
    selected: set[str] = set()
    wide = [path for path in changed if is_wide_reach(path)]
    texts: dict[str, str] = {}

    def text(test_file: str) -> str:
        if test_file not in texts:
            texts[test_file] = read_text(test_file)
        return texts[test_file]

    for path in changed:
        if path in candidate_set:
            selected.add(path)
            continue
        if is_wide_reach(path) or path.startswith("tests/"):
            continue
        if path.endswith(".py"):
            sibling = f"tests/test_{Path(path).stem}.py"
            if sibling in candidate_set:
                selected.add(sibling)
            patterns = _module_patterns(path)
            for test_file in candidates:
                body = text(test_file)
                if path in body or any(p.search(body) for p in patterns):
                    selected.add(test_file)
        else:
            needles = [path]
            name = Path(path).name
            if len(name) >= MIN_NAME_MATCH_LENGTH:
                needles.append(name)
            for test_file in candidates:
                body = text(test_file)
                if any(needle in body for needle in needles):
                    selected.add(test_file)
    return sorted(selected), wide


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the PR base commit")
    parser.add_argument("--head", required=True, help="the PR head commit")
    parser.add_argument("--output", required=True, help="file to write the selected test files to")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    changed = changed_files(repo, args.base, args.head)
    collected = collected_files(repo)
    macos_only = macos_job_files((repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    def read_text(test_file: str) -> str:
        return (repo / test_file).read_text(encoding="utf-8", errors="replace")

    selected, wide = select(changed, collected, macos_only, read_text)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(f"{f}\n" for f in selected), encoding="utf-8")
    print(f"{len(changed)} changed files; {len(selected)} affected test files selected")
    for path in wide:
        print(f"::notice::{path} reaches shared test setup; the full suite covers it when the PR is ready for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
