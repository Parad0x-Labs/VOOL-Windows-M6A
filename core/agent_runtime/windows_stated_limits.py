"""On Windows, answer a message that asks for one of 0.7.1's stated limits before any lane reads it.

The 0.7.1 notes say VOOL on Windows runs no git, starts no plugin or MCP server, runs only Python and
pytest in its sandbox (no pip, ruff, npm, node, other interpreters), and opens no share or device
path. Each limit already has its owner and its plain sentence: `core.safe_git.GIT_REFUSED_TEXT`,
`core.plugin_executor.windows_plugin_mcp_refusal`, the job runner's phase-1 gate
(`sandbox.job_runner.windows_program_refusal`, asked through the same pre-decision check a tool call
gets) and `core.windows_path_policy.refusal_text`. The front door asked them, but only some phrasings
reached it, and only after the turn had passed the lanes that run ahead of it.

Clean-VM round 1 (Windows 11, build 828b8bda, qwen3:8b) showed both gaps:
- "commit this", "push to GitHub" and other git asks had no reader at all, so the model answered
  ("Nothing is running right now..." after the honesty validator replaced its "working on it").
- A bare "pip install requests" or "install requests with pip" was no run request, so the model
  offered to help.
- "run python -m pip install x" and "run <venv>\\Scripts\\python.exe t.py" were claimed by lanes ahead
  of the front door (the builder, the model), which a model-backed turn plan can route there.
- "write hi to \\\\?\\C:\\x.txt" was split at its "?" by the multi-part splitter ahead of the front
  door, so the front door saw "write hi to \\\\?" and "\\C:\\x.txt" and neither half was a device path.

So this check runs once, on the text the user typed, at the top of the turn: after the secret intake
and before the follow-up, conductor, demand, live-data and planner seams, the front door and the
model. It decides nothing itself. Its readers find what a message asks for, and the owners above say
whether it is refused and in what words. A message it does not read, or one the owners allow, goes on
exactly as before. Off Windows every owner says no, so nothing changes there.

What counts as a git ask is anchored on intent, never on the word alone: the message must be an
imperative for a git operation ("commit this", "make a commit", "push to GitHub", "create a branch
called x", "merge main into this branch", "stash my changes", "show me the diff", "show the git log",
"clone <repo>", "checkout main") or a git command line ("git status"). "what is git?", "explain git
rebase", "commit to memory that ...", "merge these two lists" and a code ask that names a file such
as `git_log.txt` are not git asks.
"""

from __future__ import annotations

import re

_LEAD = (
    r"^\s*(?:(?:please|pls|can\s+you|could\s+you|would\s+you|will\s+you|vool|ok(?:ay)?|now|"
    r"let'?s|go\s+ahead\s+and|i\s+(?:want|need)\s+(?:you\s+)?to|i'?d\s+like\s+(?:you\s+)?to|"
    r"help\s+me)[\s,]+)*"
)
_TAIL = r"\s*(?:,?\s*(?:please|for\s+me|now|thanks|thank\s+you))?\s*[.!?]*\s*$"

#: git's own subcommands: "git <one of these> ..." is a git command line.
_GIT_SUBCOMMANDS = (
    r"add|am|apply|archive|bisect|blame|branch|checkout|cherry-pick|clean|clone|commit|config|"
    r"describe|diff|fetch|fsck|gc|grep|init|lfs|log|ls-files|ls-remote|merge|mv|notes|pull|push|"
    r"rebase|reflog|remote|reset|restore|revert|rev-parse|rm|shortlog|show|sparse-checkout|stash|"
    r"status|submodule|switch|tag|worktree"
)
#: A remote, a branch or the repository: what a push, pull or merge is aimed at.
_GIT_PLACE = (
    r"(?:github|gitlab|bitbucket|origin|upstream|git|(?:the\s+)?remote|(?:the\s+|my\s+|this\s+)?"
    r"repo(?:sitory)?|main|master|develop|dev|(?:the|my|this|a|our)\s+(?:current\s+|feature\s+|"
    r"new\s+|main\s+|remote\s+)?branch|[\w.-]+/[\w./-]+)"
)
#: What a commit, push or stash takes: "this", "my changes", "all the staged files".
_CHANGES = (
    r"(?:this|that|it|them|these|those|everything|all|(?:all\s+(?:of\s+)?)?(?:my|the|our|these|this|"
    r"those|your)\s+(?:(?:current|staged|unstaged|pending|local|recent|latest|new|uncommitted|working|"
    r"latest)\s+)*(?:changes?|work|files?|edits?|code|fix(?:es)?|updates?|stuff|modifications?|"
    r"commits?))"
)
_PUSH_TAIL = (
    r"(?:\s+(?:with|using)\s+(?:the\s+)?(?:commit\s+)?(?:message|msg)\b.*"
    r"|\s+(?:and|then|&)\s+(?:push|sync|commit)\b.*"
    r"|\s+(?:to|on|into|up\s+to)\s+" + _GIT_PLACE + r"\b.*"
    r"|\s+(?:as|with)\s+['\"\u2018\u201c].*"
    r"|\s+-\S.*)?"
)
_BRANCH_NAME = r"(?:main|master|develop|dev|head\S*|origin/\S+|upstream/\S+|feature/\S+|release/\S+|[\w.-]+-branch)"

_GIT_ASKS = tuple(
    re.compile(_LEAD + body + _TAIL, re.IGNORECASE)
    for body in (
        # A git command line: "git status", "git commit -am x".
        r"git(?:\.exe)?\s+(?:" + _GIT_SUBCOMMANDS + r")\b.*",
        # "commit", "commit this", "commit my changes with message x", "commit and push".
        r"commit(?:\s+" + _CHANGES + r")?" + _PUSH_TAIL,
        # "make a commit", "create a new commit of my changes".
        r"(?:make|create|do|add)\s+(?:a|an|the|another)\s+(?:new\s+)?(?:git\s+)?commit"
        r"(?:\s+(?:of|for|with)\s+" + _CHANGES + r")?" + _PUSH_TAIL,
        # "push", "push this", "push my changes to GitHub", "push to origin".
        r"push(?:\s+" + _CHANGES + r")?" + _PUSH_TAIL,
        r"push\s+(?:the\s+|this\s+|my\s+)?(?:branch|repo(?:sitory)?)\b" + _PUSH_TAIL,
        # "pull", "pull from origin", "pull the latest changes"; never "pull qwen3:8b" (a model pull).
        r"pull(?:\s+(?:it|them|this|everything))?",
        r"pull\s+(?:the\s+)?(?:latest\s+|new\s+|recent\s+)?(?:changes?|commits?|code|updates?)"
        r"(?:\s+(?:from|down\s+from)\s+" + _GIT_PLACE + r")?\b.*",
        r"pull(?:\s+" + _CHANGES + r")?\s+(?:from|down\s+from)\s+" + _GIT_PLACE + r"\b.*",
        # Branches.
        r"(?:create|make|add|open|start|cut)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:git\s+|feature\s+|local\s+)?"
        r"branch(?:\s+(?:called|named|for|off|from|with\s+(?:the\s+)?name)\b.*|\s+[\w./-]+)?",
        r"(?:switch|change|move|go)\s+(?:over\s+|back\s+)?to\s+(?:the\s+)?(?:[\w./-]+\s+branch|branch\s+[\w./-]+|"
        + _BRANCH_NAME
        + r"(?:\s+branch)?)",
        r"(?:delete|remove|rename)\s+(?:the\s+|a\s+|this\s+|that\s+|my\s+)?(?:[\w./-]+\s+)?branch\b.*",
        r"(?:show|list|get)\s+(?:me\s+)?(?:all\s+)?(?:the\s+|my\s+)?(?:git\s+|local\s+|remote\s+)*branches\b.*",
        r"(?:what|which)\s+branch\s+(?:am\s+i|are\s+we|is\s+this(?:\s+repo)?)\s+on\b.*",
        # Merges aimed at a branch: never "merge these two lists" or "merge a.csv into b.csv".
        r"merge\s+(?:the\s+|this\s+|my\s+)?(?:[\w./-]+\s+)?(?:branch|pr|pull\s+request)\b.*",
        r"merge\s+(?:the\s+)?" + _BRANCH_NAME + r"(?:\s+branch)?(?:\s+(?:into|in|to|with)\s+.*)?",
        r"merge\s+[\w./-]+\s+(?:into|in|to|with)\s+(?:the\s+|this\s+|my\s+)?(?:current\s+)?(?:"
        + _BRANCH_NAME
        + r"|[\w./-]*\s*branch)\b.*",
        # Stash.
        r"stash(?:\s+" + _CHANGES + r")?(?:\s+(?:away|for\s+now)|\s+(?:and|then)\s+.*)?",
        r"(?:pop|apply|drop|list|show)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:last\s+|latest\s+)?stash(?:es)?\b.*",
        r"unstash\b.*",
        # The working tree's diff and the commit log: never "the diff between a.py and b.py".
        r"(?:show|give|print|display|get)\s+(?:me\s+)?(?:the\s+|a\s+|my\s+)?(?:(?:current|git|working|tree|"
        r"staged|uncommitted)\s+)*diff(?:\s+(?:of|for)\s+" + _CHANGES + r")?",
        r"diff(?:\s+" + _CHANGES + r")?",
        r"what(?:'?s|\s+has|\s+have\s+i)?\s+changed\s+since\s+(?:the\s+|my\s+)?(?:last|previous)\s+commit\b.*",
        r"(?:show|give|print|display|get|list)\s+(?:me\s+)?(?:the\s+)?(?:git\s+log|commit\s+(?:log|history)|"
        r"git\s+history|(?:recent\s+|latest\s+|last\s+\d+\s+)?commits)"
        r"(?:\s+(?:for|of|in|on)\s+(?:this|the)\s+(?:repo(?:sitory)?|project|branch|folder))?",
        r"(?:show|check|get|give\s+me|what'?s|what\s+is)\s+(?:me\s+)?the\s+(?:current\s+)?git\s+status\b.*",
        r"(?:show|check|get)\s+(?:me\s+)?git\s+status\b.*",
        # Clone a repository.
        r"clone\s+(?:(?:the|this|that|a|my)\s+)?(?:(?:git|github|gitlab)\s+)?"
        r"(?:repo(?:sitory)?\b.*|https?://\S+.*|git@\S+.*|\S+\.git\b.*|[\w.-]+/[\w.-]+(?:\s+.*)?)",
        # Checkout: a branch, a tag, a commit or a path, never "check out this article".
        r"checkout\s+(?:-[bB]\s+\S+.*|--\s+\S+.*|\.|(?:the\s+)?(?:" + _BRANCH_NAME + r"|[\w./-]+\s+branch|"
        r"branch\s+\S+|tag\s+\S+|commit\s+\S+|[0-9a-f]{7,40}|[\w.-]+/[\w./-]+|v\d\S*)(?:\s+branch)?)",
        r"check\s+out\s+(?:the\s+)?(?:" + _BRANCH_NAME + r"(?:\s+branch)?|[\w./-]+\s+branch|branch\s+\S+|"
        r"tag\s+\S+|commit\s+[0-9a-f]{7,40})",
        # History rewrites and a new repository.
        r"rebase(?:\s+(?:this|it|my\s+branch|the\s+branch|(?:on|onto)\s+\S+|-\S+))*(?:\s+(?:on|onto)\s+.*)?",
        r"cherry[- ]pick\b.*",
        r"amend\s+(?:the\s+|my\s+)?(?:last\s+|previous\s+|latest\s+)?commit\b.*",
        r"(?:undo|revert|reset|squash)\s+(?:the\s+|my\s+)?(?:last|previous|latest|\d+)\s+commits?\b.*",
        r"(?:init|initiali[sz]e|create|make|set\s+up|start)\s+(?:a\s+)?(?:new\s+)?git\s+repo(?:sitory)?\b.*",
    )
)

#: Starting, adding or connecting an MCP server, and installing, enabling or running a plugin or an
#: add-on with runnable parts (clean-VM R1-10: "add an MCP server ..." reached the builder and failed
#: on its prompt budget; "start the MCP server" was answered by the model). An imperative at the start
#: of the message, aimed at MCP or a plugin within a few words: never "what is MCP?", "explain
#: plugins" or "write a plugin loader in python". An add-on alone may be skills-only, which Windows
#: still enables, so it is read only when the message names its runnable parts.
_PLUGIN_MCP_ASKS = tuple(
    re.compile(_LEAD + body + r"\s*[.!?]*\s*$", re.IGNORECASE)
    for body in (
        r"(?:add|install|set\s+up|configure|register|start|restart|launch|run|connect(?:\s+(?:vool\s+)?to)?|"
        r"enable|activate|turn\s+on|spin\s+up|boot(?:\s+up)?|attach|hook\s+up|load|use|open)\b"
        r"[^\n]{0,60}?\bmcp\b[^\n]*",
        r"(?:install|add|enable|activate|turn\s+on|load|run|start|launch|execute|set\s+up|connect)\b"
        r"[^\n]{0,60}?\bplug-?ins?\b[^\n]*",
        r"(?:install|add|enable|activate|turn\s+on|load|run|start|launch|execute)\b[^\n]{0,60}?\badd-?ons?\b"
        r"[^\n]{0,60}?\b(?:tools?|commands?|hooks?|scripts?|servers?|binar(?:y|ies)|programs?|executables?)\b[^\n]*",
    )
)
#: A code ask that names a plugin or MCP is the builder's: "write a plugin", "create an MCP client".
_CODE_ASK_RE = re.compile(
    r"\b(?:write|code|implement|build|create|make|generate|scaffold|design)\s+(?:me\s+)?(?:a|an|the|some|my)?\s*"
    r"(?:python\s+|simple\s+|small\s+|new\s+)*(?:plug-?in|mcp|add-?on)\b",
    re.IGNORECASE,
)


def asks_for_plugin_or_mcp(text: str) -> bool:
    """Whether a one-line message asks to start, add or connect an MCP server, or to install, enable
    or run a plugin (or an add-on it names runnable parts of)."""
    raw = str(text or "").strip()
    if not raw or "\n" in raw or len(raw) > 300 or _CODE_ASK_RE.search(raw):
        return False
    return any(pattern.match(raw) for pattern in _PLUGIN_MCP_ASKS)


#: A message that asks VOOL to remember something only mentions a path; it is the memory lane's,
#: as on base (core.agent_runtime.turn_frontdoor places the path check after that lane).
_MEMORY_LEAD_RE = re.compile(
    r"^\s*(?:(?:please|pls|vool)[\s,]+)*(?:remember|memori[sz]e|commit\s+to\s+memory|keep\s+in\s+mind|"
    r"note\s+that|don'?t\s+forget|make\s+a\s+note)\b",
    re.IGNORECASE,
)


def asks_for_git(text: str) -> bool:
    """Whether a one-line message is an imperative for a git operation or a git command line."""
    raw = str(text or "").strip()
    if not raw or "\n" in raw or len(raw) > 300:
        return False
    return any(pattern.match(raw) for pattern in _GIT_ASKS)


def windows_limit_refusal(text: str, *, source_context: dict | None = None) -> tuple[str, str]:
    """On Windows, the plain refusal for a message that asks for a stated 0.7.1 limit, with its
    reason; ("", "") when the message asks for none of them, the owners allow it, or off Windows.

    In this order: a share or device path the message would write to (the front door's order), a
    plugin or MCP ask, a git ask, then a command line phase 1 does not run."""
    raw = str(text or "")
    if not raw.strip():
        return "", ""
    if not _MEMORY_LEAD_RE.match(raw):
        from core import windows_path_policy

        if windows_path_policy.active():
            from core.agent_runtime.turn_frontdoor import _non_local_typed_path

            # Shares and devices only: a path with no full local folder is the front door's to
            # answer, in its place, as on base (a build's `/api/users` route names no folder).
            path, kind, _lane = _non_local_typed_path(raw, "write", network_only=True)
            if kind:
                return windows_path_policy.refusal_text(path, kind, nothing="written"), "non_local_path_refused"
    from core.plugin_executor import windows_plugin_mcp_refusal

    if asks_for_plugin_or_mcp(raw):
        refusal = windows_plugin_mcp_refusal()
        if refusal:
            return refusal, "windows_plugin_mcp_refused"
    from core import safe_git

    if safe_git.git_refused_here() and asks_for_git(raw):
        return safe_git.GIT_REFUSED_TEXT, "windows_git_refused"
    from core.agent_runtime.windows_program_request import windows_program_request_refusal

    refusal = windows_program_request_refusal(raw, source_context=source_context)
    if refusal:
        return refusal, "windows_program_refused"
    return "", ""


__all__ = ["asks_for_git", "asks_for_plugin_or_mcp", "windows_limit_refusal"]
