"""What a row expects where VOOL 0.7.1 refuses git, plugins/MCP or a program before anything starts.

0.7.1 for Windows states three limits: VOOL runs no git (core/safe_git.py, GIT_REFUSED_TEXT), starts no
plugin or MCP server (core/plugin_executor.py, WINDOWS_PLUGIN_MCP_REFUSAL), and runs no program but
Python/pytest in the sandbox. Rows written for Linux and macOS pin what happens once git or the plugin
runs. Where the limit holds, the honest answer is the refusal, so such a row checks three things there:
the user reads the exact refusal sentence, no process was started for it, and nothing under the row's
folders changed. Off Windows the row checks what it always did.

Usage, around the one call (or the few calls) the row is about:

    with windows_refusals.watch_git(repo) as seen:
        refusal = safe_git.lfs_endpoint_refusal(repo, ("merge", "other"))
    if seen.refused(refusal):
        return
    assert refusal == ""                        # the row's own expectation, unchanged off Windows

The switches are the modules' own: safe_git.git_refused_here() (test seam safe_git._REFUSE_GIT) and
plugin_executor.windows_plugin_mcp_refusal() (test seam _WINDOWS_REFUSES_PLUGINS_AND_MCP). A Linux run
can force either seam on to see the Windows expectation.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

from core import plugin_executor, safe_git

GIT_REFUSAL = safe_git.GIT_REFUSED_TEXT
GIT_STATUS = safe_git.GIT_REFUSED_STATUS
PLUGIN_REFUSAL = plugin_executor.WINDOWS_PLUGIN_MCP_REFUSAL
PLUGIN_STATUS = plugin_executor.WINDOWS_REFUSED_STATUS


def git_refused() -> bool:
    """Whether this process refuses git (Windows, or the seam forced on)."""
    return safe_git.git_refused_here()


def plugins_refused() -> bool:
    """Whether this process refuses to start plugins and MCP servers (Windows, or the seam forced on)."""
    return bool(plugin_executor.windows_plugin_mcp_refusal())


def tree_state(*roots: Path) -> dict[str, tuple]:
    """Every entry under `roots` (links not followed) with its kind and content, to tell any write apart."""
    state: dict[str, tuple] = {}
    for root in roots:
        root = Path(root)
        if not os.path.lexists(root):
            state[str(root)] = ("absent",)
            continue
        for folder, dirs, files in os.walk(root, followlinks=False):
            state[folder] = ("dir",)
            for name in dirs + files:
                path = os.path.join(folder, name)
                if os.path.islink(path):
                    state[path] = ("link", os.readlink(path))
                elif os.path.isfile(path):
                    with open(path, "rb") as handle:
                        state[path] = ("file", hashlib.sha256(handle.read()).hexdigest())
                elif not os.path.isdir(path):
                    state[path] = ("other",)
    return state


def _changes(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


class Watch:
    """What happened inside one `watch_*` block where the limit applies (`active`)."""

    def __init__(self, *, active: bool, sentence: str, status: str, what: str, roots: tuple[Path, ...]) -> None:
        self.active = active
        self.sentence = sentence
        self.status = status
        self.what = what
        self.roots = roots
        self.starts: list = []
        self.changed: list[str] = []

    def refused(self, *answers: object) -> bool:
        """False where the limit does not apply: the row goes on to its own expectation.

        Where it applies: each answer must carry the exact refusal sentence, the block must have
        started no process for it and changed nothing under the watched folders; then True, and the
        row ends there.
        """
        if not self.active:
            return False
        assert answers, "a refused row must name the answer the user reads"
        for answer in answers:
            text = answer if isinstance(answer, str) else repr(answer)
            assert self.sentence in text, f"expected the {self.what} refusal {self.sentence!r}, got {text!r}"
        assert self.starts == [], f"a {self.what} process was started where it is refused: {self.starts!r}"
        assert self.changed == [], f"the refused {self.what} call changed files: {self.changed!r}"
        return True

    def refused_tool(self, *results: dict) -> bool:
        """`refused` for VOOL tool results (dicts with ok, status and response_text): each one also
        failed with the limit's own status."""
        if not self.active:
            return False
        for result in results:
            assert result.get("ok") is False and result.get("status") == self.status, result
        return self.refused(*(result.get("response_text", "") for result in results))

    def refused_result(self, *results: object, text: str = "error") -> bool:
        """`refused` for result objects (ok, status and the sentence in attribute `text`, such as a
        PluginToolResult's `error`): each one also failed with the limit's own status."""
        if not self.active:
            return False
        for result in results:
            assert result.ok is False and result.status == self.status, result
        return self.refused(*(str(getattr(result, text) or "") for result in results))


@contextlib.contextmanager
def _watch(active: bool, sentence: str, status: str, what: str, counts: Callable[[object, dict], bool],
           roots: tuple[Path, ...]) -> Iterator[Watch]:
    seen = Watch(active=active, sentence=sentence, status=status, what=what, roots=roots)
    if not active:
        yield seen
        return
    real = subprocess.Popen

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            if counts(args, kwargs):
                seen.starts.append(args)
                raise AssertionError(f"a {what} process was started where it is refused: {args!r}")
            super().__init__(args, *rest, **kwargs)

    before = tree_state(*roots)
    subprocess.Popen = Recording  # type: ignore[misc]
    try:
        yield seen
    finally:
        subprocess.Popen = real  # type: ignore[misc]
        seen.changed = _changes(before, tree_state(*roots))


def watch_git(*roots: Path, aimed_at: Path | None = None) -> contextlib.AbstractContextManager[Watch]:
    """Around a git call: where git is refused, record every git start and any change under `roots`.

    `aimed_at` narrows the record to git started on that folder (named in its arguments or as its
    working directory): for a
    row driving the whole agent, whose turn also reads VOOL's own build commit from its program
    folder, which is not the user's repository the row is about."""
    if aimed_at is None:
        def counts(args, kwargs) -> bool:
            return safe_git.is_git_command(args, kwargs.get("executable"))
    else:
        folder = str(Path(aimed_at))

        def counts(args, kwargs) -> bool:
            named = repr(args) + " " + str(kwargs.get("cwd") or "")
            return safe_git.is_git_command(args, kwargs.get("executable")) and folder in named
    return _watch(git_refused(), GIT_REFUSAL, GIT_STATUS, "git", counts, tuple(Path(r) for r in roots))


def refuses_push(source: Path, remote: str, *roots: Path, check: bool = True, push: bool = True,
                 ref: str = "main") -> bool:
    """Where git is refused: GitRunner's push-destination check and a push of `ref` from `source` to
    `remote` both answer the git sentence (the push as a run that never started), no git starts and
    nothing under `roots` changes. Returns True then, and the row ends; False where git runs."""
    if not git_refused():
        return False
    from core.repoops.gitops import GitRunner

    answers = []
    with watch_git(*roots) as seen:
        runner = GitRunner(root=Path(source))
        if check:
            answers.append(runner.push_destination_refusal(remote))
        if push:
            code, _, err = runner.run(*runner.push_argv(remote=remote, ref=ref, sha=runner.head()))
            assert code == -1 and err.startswith("git was not run:"), err
            answers.append(err)
    assert seen.refused(*answers)
    return True


def refuses_repo_session(world: tuple, ctx: dict | None = None) -> bool:
    """For the RepoOps rigs (tests/repoops: `world` is (checkout, bare remote, recorded forge)).
    Where git is refused no RepoOps session opens: `repo.session.open` answers the git sentence,
    starts no git, changes neither repository and sends the forge nothing. Returns True then, and
    the row ends; False where git runs, and the row arms its session as it always did."""
    if not git_refused():
        return False
    from tests.repoops._harness import context, door

    root, bare, forge = world
    ctx = context(root) if ctx is None else ctx
    with watch_git(root, bare) as seen:
        opened = door(
            "repo.session.open",
            {"objective": "negative probe", "pull_request": "8", "provider": "github", "namespace": "o/r"},
            ctx,
        )
    assert opened.ok is False, opened.details
    assert seen.refused(opened.response_text)
    assert forge.calls == [], forge.calls
    return True


def programs_refused() -> bool:
    """Whether the sandbox runs only Python and pytest here (Windows 0.7.1, or the job runner's seam on)."""
    from sandbox import job_runner

    return bool(job_runner._is_windows_platform())


def program_refusal(command: str) -> str:
    """The sentence VOOL's own early check (the job runner's gate, asked before any approval) gives a
    `sandbox.run_command` of `command` where programs are refused, else ''."""
    from core import runtime_execution_tools

    refused = runtime_execution_tools.windows_program_refusal("sandbox.run_command", {"command": command})
    return str(refused.response_text or "") if refused is not None else ""


def watch_programs(command: str, *roots: Path) -> contextlib.AbstractContextManager[Watch]:
    """Around a call that would run `command`: where the sandbox refuses it, record every process
    start and any change under `roots`; the answer must carry the job runner's own sentence."""
    from core.runtime_execution_tools import WINDOWS_PROGRAM_STATUS

    active = programs_refused()
    return _watch(active, program_refusal(command) if active else "", WINDOWS_PROGRAM_STATUS, "program",
                  lambda _args, _kwargs: True, tuple(Path(r) for r in roots))


def watch_plugins(*roots: Path) -> contextlib.AbstractContextManager[Watch]:
    """Around a plugin or MCP call: where they are refused, record every process start and any change under `roots`."""
    return _watch(plugins_refused(), PLUGIN_REFUSAL, PLUGIN_STATUS, "plugin or MCP", lambda _args, _kwargs: True,
                  tuple(Path(r) for r in roots))


def child_seams() -> str:
    """Python lines that give a child interpreter this process's plugin/MCP switch.

    A seam forced on in this process does not cross into a `python -c` child, which would then
    decide from its own platform. On Windows the child refuses anyway and these lines change
    nothing; off Windows, with the seam left alone, they are empty."""
    if not plugin_executor._WINDOWS_REFUSES_PLUGINS_AND_MCP:
        return ""
    return "import core.plugin_executor as _seam; _seam._WINDOWS_REFUSES_PLUGINS_AND_MCP = True\n"


def refuses_activation(home: Path, script: str, plugin_id: str, *roots: Path,
                       env_extra: dict[str, str] | None = None) -> bool:
    """For an in-home activation (`tests._blackbox_served_rig.run_in_home` running install, verify
    and enable). Where plugins are refused the enable step refuses with the plugin sentence: the
    snippet fails with it, its lifecycle store holds `plugin_id` as not enabled, this session's
    store is unchanged, and nothing under `roots` changed. Returns True then, and the row ends;
    False where plugins run, and the row activates as it always did."""
    if not plugins_refused():
        return False
    import json

    import pytest

    from core import plugin_lifecycle
    from tests._blackbox_served_rig import run_in_home

    session_before = plugin_lifecycle.lifecycle_snapshot()["plugins"]
    before = tree_state(*roots)
    with pytest.raises(RuntimeError) as caught:
        run_in_home(home, script, env_extra=env_extra)
    failure = str(caught.value)
    assert PLUGIN_REFUSAL in failure, failure
    assert "lifecycle: installed+verified+enabled" not in failure, failure
    pinned = (env_extra or {}).get("VOOL_PLUGIN_LIFECYCLE_PATH")
    store = Path(pinned) if pinned else Path(home) / "data" / "plugin_lifecycle.json"
    if store.is_file():
        row = json.loads(store.read_text(encoding="utf-8")).get("plugins", {}).get(plugin_id) or {}
        assert row.get("enabled") is not True and row.get("stage") != "enabled", row
    assert plugin_lifecycle.lifecycle_snapshot()["plugins"] == session_before
    assert _changes(before, tree_state(*roots)) == []
    return True


class RefusedWorld(dict):
    """What a served fixture yields where its plugin activation was refused: no daemon was booted.

    A row with a Windows re-pin ends on `refused_on_windows`; a row without one that reaches for
    the daemon (or anything else the refusal left unmade) fails here, naming the refusal."""

    def __missing__(self, key: str):
        raise RuntimeError(f"the fixture's plugin activation was refused ({PLUGIN_REFUSAL!r}), so there is no "
                           f"{key!r}: this row has no Windows expectation of its own")


def cloud_models_refused() -> bool:
    """Whether this process refuses Ollama cloud models (Windows in 0.7.1, or the seam
    ollama_cloud_models.CLOUD_REFUSAL_ACTIVE forced on)."""
    from core import ollama_cloud_models

    return ollama_cloud_models.active()


def watch_cloud_model(model: str, *roots: Path) -> contextlib.AbstractContextManager[Watch]:
    """Around a call that names the Ollama cloud model `model`: where cloud models are refused, record
    every process start and any change under `roots`; the answer must carry the refusal sentence."""
    from core import ollama_cloud_models

    return _watch(cloud_models_refused(), ollama_cloud_models.refusal_text(model), "", "Ollama cloud model",
                  lambda _args, _kwargs: True, tuple(Path(r) for r in roots))


def assert_git_ask_answered_at_the_door(seen: Watch, result: dict, invocations) -> None:
    """For a whole-agent turn that asks for git (such as "show git status"), driven inside
    `watch_git(project, aimed_at=project)` where git is refused: the turn's front door answers the git
    sentence itself (route deterministic:windows_git_refused), buys no inference, runs no git tool,
    starts no git on the project and changes nothing there."""
    assert invocations.count == 0, invocations.timeline
    assert result["fast_path_hit"] is True and result["model_calls"] == 0, result
    assert result.get("route") == "deterministic:windows_git_refused", result.get("route")
    assert not any("git_status" in str(step) for step in invocations.timeline), invocations.timeline
    assert result["response"] == GIT_REFUSAL, result["response"]
    assert seen.refused(result["response"])


# ── exact forms (added beside the helpers above, which are unchanged) ─────────────────────────────


def refused_exactly(seen: Watch, *pairs: tuple[object, object]) -> bool:
    """A stricter `Watch.refused`. False where the limit does not apply: the row goes on to its own
    expectation, unchanged.

    Where it applies, each ``(actual, expected)`` pair is equal: a status or verdict field, or the
    whole text the user reads (never a part of it). At least one pair is that whole text and carries
    the limit's sentence. Then `seen.refused` holds for those texts: no process started for the limit
    and nothing under the watched folders changed. Returns True, and the row ends there."""
    if not seen.active:
        return False
    assert pairs, "a refused row must name what it compares"
    texts = []
    for actual, expected in pairs:
        assert actual == expected, f"expected {expected!r}, got {actual!r}"
        if isinstance(expected, str) and seen.sentence and seen.sentence in expected:
            texts.append(actual)
    assert texts, f"no pair is the whole {seen.what} refusal text"
    assert seen.refused(*texts)
    return True
