"""Windows: refuse paths and file URLs that name something off this machine, before anything opens them.

On Windows, `\\\\host\\share\\x`, `//host/share/x`, a device path (`\\\\?\\UNC\\host\\share`,
`\\\\.\\pipe\\x`), an NT object path (`\\??\\UNC\\host\\share`) and a `file://host/share` URL all reach
the network the moment anything resolves, stats or opens them: Windows connects to the share over SMB
and sends the user's NTLM hash to whoever answers. A model's tool call, a route, a command or a chat
message can carry such a value, so it is refused here by its spelling alone, before the first
filesystem call. Two more spellings are not network paths but are not full paths either: `\\foo`
(root-relative) and `C:foo` (drive-relative) land on whatever drive or folder is current, which is
not the place anyone reviewed.

There are two tiers, because the same parameter is not always untrusted:
- The full rule refuses all of these forms. It applies where a value comes from outside and is not
  mixed with roots the server built: the tool doors, the command registry, served routes and the
  message lanes.
- The network tier (`network_only=True`) refuses only shares, device paths and NT object paths. It
  applies inside shared resolvers, whose same parameter also carries the server's own roots and
  paths it places on purpose (a machine write to `C:Documents\\x.txt` lands in the user's home), and
  to MCP and plugin tools, whose arguments mean whatever their server says. There the full rule would
  refuse the server's own work, and on Linux CI, with the rule switched on, every POSIX root reads
  as root-relative.

`classify`, `classify_drive` and `classify_url` are pure and answer the same on every host. Every
other function answers only on Windows: off Windows it returns "" or None at its first statement,
before any alias, registry or environment lookup, because on macOS and Linux `//host/share` is an
ordinary path and POSIX keeps exactly the answers it had. Tests switch the rule on through
`_IS_WINDOWS`, never through `os.name` or `sys.platform`, which pathlib branches on.

Not judged here: reserved DOS device names (`CON`, `NUL`, `COM1`), a local link or junction that
points at a share, and the text of shell commands, patches and file contents.
"""
from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Iterator, Mapping
from typing import Any, NamedTuple
from urllib.parse import unquote

#: Patched by tests to drive the Windows decision on any platform; read at call time.
_IS_WINDOWS: bool = os.name == "nt"
STATUS = "non_local_path_refused"
#: The kinds that reach another machine or a device. The network tier refuses only these.
NETWORK_KINDS = frozenset({"unc", "device", "nt_namespace"})

_DRIVE_ROOT_RE = re.compile(r"[A-Za-z]:[\\/]?")
_URL_SCHEME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9+.\-]*):")
_FILE_URL_DRIVE_RE = re.compile(r"^[A-Za-z](:|\|)(/|$)")
_FILE_URL_DRIVE_HOST_RE = re.compile(r"[A-Za-z][:|]")
# A browser drops tabs and line breaks anywhere in a URL, and control characters and spaces at
# either end (Python's urlsplit does the same), so `fi\tle://host/share` still opens the share.
_URL_DROPPED = dict.fromkeys(map(ord, "\t\n\r"))
_URL_TRIMMED = "".join(map(chr, range(0x21)))
# Every character `str.splitlines` breaks a line on.
_LINE_BREAK_RE = re.compile("[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]")

#: Argument names that always carry a filesystem path: the full rule applies.
PATH_KEYS = frozenset({
    "path", "paths", "file_path", "target_path", "destination", "destination_path", "destination_dir",
    "source_path", "directory", "cwd", "output_path", "outputs", "workspace", "workspace_root",
    "path_prefix", "root", "local_path", "project_root",
})
#: Argument names that are a path for file tools but something else elsewhere (an email folder such as
#: `/Inbox`, a message recipient): the full rule under `_PATH_INTENT_PREFIXES`, the network tier otherwise.
AMBIGUOUS_KEYS = frozenset({
    "target", "targets", "dest", "to", "from", "src", "source", "folder", "file", "filename", "filepath",
    "dir", "dir_path", "folder_path", "directory_path", "files",
})
#: Argument names that carry a URL, judged by `classify_url`. A git `remote` is a remote's name or a
#: URL; one on a share is looked up by the push check before it is refused, so it is judged here first.
URL_KEYS = frozenset({"url", "uri", "link", "remote"})
_PATH_INTENT_PREFIXES = (
    "workspace.", "machine.", "operator.", "pdf.", "skill.", "media.", "sandbox.", "code.", "repo.", "file.",
)
#: Step intents that run an inner call, checked as that inner call too.
_STEP_INTENTS = frozenset({"code.task.step", "repo.step"})

#: The command-registry fields that name a file or folder, by command. There is deliberately no
#: generic `path` field: more than 50 commands carry an HTTP route there (`/api/wallet/status`).
_COMMAND_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "archaeology.search": ("workspace_root", "path_prefix"),
    "archaeology.trace": ("workspace_root", "path"),
    "archaeology.recovery_plan": ("workspace_root", "path"),
    "archaeology.compare": ("workspace_root", "a", "b"),
    "archaeology.locate": ("workspace_root",),
    "agents.start": ("workspace",),
    "approvals.bypass.activate": ("workspace_root",),
    "blackbox.rollback": ("workspace_root",),
    "council.convene": ("workspace_root",),
    "update.rollback": ("expected_app_path", "expected_data_dir"),
    "logs.verify": ("segment_id",),
}
#: The fields of a chat request, and of its source context, that name the folder the turn works in,
#: and the VOOL home it opens its memory store under (`runtime_home`, which the served turn keeps when
#: a caller sets it).
_CHAT_WORKSPACE_FIELDS = ("workspace", "workspace_root", "cwd", "projectRoot", "runtime_home")
#: The fields of a turn's source context that hold the folder its tools resolve against.
_CONTEXT_WORKSPACE_FIELDS = ("workspace", "workspace_root")


def _text(value: Any) -> str | None:
    """`value` as text: a path object gives its path and bytes are decoded as the OS decodes a path.
    Anything else is not a path and gives None."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    return value if isinstance(value, str) else None


def _kind(spelled: str) -> str:
    """The kind of a stripped value whose `/` are already written as `\\`, or "" for a full local path
    or a relative name."""
    if spelled.startswith("~\\"):
        # The machine tools place `~\rest` as the home folder joined with `rest`, and a join onto a
        # rooted `rest` is `rest` itself: `~\\\host\share` (typed `~///host/share`) is the share.
        spelled = spelled[2:]
    if spelled.startswith("\\\\"):
        # `\\?\` and `\\.\` open devices, `\\?\UNC\host` included; anything else after two separators
        # is a server name, even an empty one.
        return "device" if spelled[2:3] in ("?", ".") else "unc"
    if spelled == "\\??" or spelled.startswith("\\??\\"):
        return "nt_namespace"
    if spelled.startswith("\\"):
        return "root_relative"
    if spelled[1:2] == ":":
        if not (spelled[0].isascii() and spelled[0].isalpha()):
            return "bad_drive"
        if spelled[2:3] != "\\":
            return "drive_relative"
    return ""


def classify(value: Any, *, network_only: bool = False) -> str:
    """What `value` is as a Windows path, judged on its spelling alone and the same on every host.

    Returns "" for a full local path (`C:\\Users\\you\\a.txt`) or a relative name (`docs\\a.txt`), and
    otherwise one of `unc`, `device` or `nt_namespace` (the network kinds), `root_relative`,
    `drive_relative` or `bad_drive`. `/` counts as `\\`, as it does on Windows, and surrounding
    whitespace is ignored, as callers strip it. `~\\` is judged by what follows it, because that is
    joined onto the home folder (`~///host/share` is the share). A value that passes as written is
    judged again with `%VAR%` and `$VAR` expanded, because some callers expand them:
    `%LOGONSERVER%\\share` is the share it opens. With `network_only`, only the network kinds are
    returned. Values that are not text, bytes or a path object give "".
    """
    text = _text(value)
    if text is None:
        return ""
    spelled = text.strip().replace("/", "\\")
    if not spelled:
        return ""
    kind = _kind(spelled)
    if not kind and ("%" in spelled or "$" in spelled):
        expanded = ntpath.expandvars(spelled).strip().replace("/", "\\")
        if expanded != spelled:
            kind = _kind(expanded)
    if network_only and kind not in NETWORK_KINDS:
        return ""
    return kind


def classify_drive(value: Any) -> str:
    """`classify` for a `drive` field, where a bare drive (`C:`, `d:/`, `D:\\`) is the expected value."""
    text = _text(value)
    if text is not None and _DRIVE_ROOT_RE.fullmatch(text.strip()):
        return ""
    return classify(value)


def classify_url(value: Any) -> str:
    """The kind of file the URL `value` opens: a network kind for a file on another machine,
    `root_relative` for a path with no drive on this one, otherwise "".

    Percent-escapes are decoded and `\\` read as `/` first. A `file:` URL's host is what follows `//`:
    any host but `localhost` is `unc`, and a drive there (`file://C:/x`) starts the path. Without one
    (`file:///x`, `file:/x`) the rest is a path on this machine: local when it names a drive
    (`file:///C:/x`, `file:///c|/x`), `root_relative` for `/Users/x` (on Windows, the current
    drive's), and judged as a path for `//host/s` (`file:////host/s` is the share). `view-source:` is judged by the URL it wraps. A value without a
    scheme is judged as a path with the network tier, so a route such as `/x` passes and `//host/x`
    does not. Other schemes (https, data, about, mailto) are not files and give "".
    """
    text = _text(value)
    if text is None:
        return ""
    text = text.translate(_URL_DROPPED)
    while True:
        text = text.strip(_URL_TRIMMED)
        match = _URL_SCHEME_RE.match(text)
        if match is None or len(match.group(1)) == 1:
            # No scheme, or a drive letter (`C:/x`) read as one.
            return classify(text, network_only=True)
        scheme, text = match.group(1).lower(), text[match.end():]
        if scheme != "view-source":
            break
    if scheme != "file":
        return ""
    body = unquote(text).replace("\\", "/")
    if body.startswith("//"):
        host, _slash, path = body[2:].partition("/")
        if _FILE_URL_DRIVE_HOST_RE.fullmatch(host):
            # `file://C:/x`: a drive where the host goes starts the path, as a browser and
            # `url2pathname` read it; it is no server named `C:`.
            path = f"{host}/{path}"
        elif host and host.lower() != "localhost":
            return "unc"
        body = "/" + path
    if not body.strip("/") or _FILE_URL_DRIVE_RE.match(body.lstrip("/")):
        return ""
    # A host-less path: `file:x` is `/x`, as a browser reads it.
    return classify(body if body.startswith("/") else "/" + body)


def refusal_text(value: Any, kind: str) -> str:
    """The sentence a refusal gives, naming `value` as it was written.

    The sentence is relayed inside markdown, so the value loses its backticks and line breaks and is
    cut to 120 characters.
    """
    text = _text(value)
    shown = _LINE_BREAK_RE.sub(" ", (str(value) if text is None else text).replace("`", ""))[:120]
    if kind in NETWORK_KINDS:
        return (
            f"`{shown}` is a network or device path. On Windows, VOOL only opens files on a local drive "
            "(like C:\\Users\\you\\file.txt) or inside the current folder, so nothing was opened."
        )
    return (
        f"`{shown}` is not a full local path. On Windows, use a drive letter and folder "
        "(like C:\\Users\\you\\file.txt) or a path inside the current folder. Nothing was opened."
    )


class NonLocalPathError(ValueError):
    """A path refused by `check`. It is a ValueError, so a caller that already turns a ValueError from
    a resolver into its own refusal carries this sentence."""

    def __init__(self, value: Any, kind: str):
        super().__init__(refusal_text(value, kind))
        self.value = value
        self.kind = kind


class PathRefusal(NamedTuple):
    """The argument or field that was refused, the value it carried, and that value's kind."""

    key: str
    value: str
    kind: str


def active() -> bool:
    """Whether this process applies the rule: on Windows, or when a test switches it on."""
    return _IS_WINDOWS


def refused_form(value: Any, *, network_only: bool = False) -> str:
    """On Windows, the kind `classify` gives `value` in this tier; off Windows, always ""."""
    if not _IS_WINDOWS:
        return ""
    return classify(value, network_only=network_only)


def refused_drive(value: Any) -> str:
    """On Windows, the kind `classify_drive` gives `value`; off Windows, always ""."""
    if not _IS_WINDOWS:
        return ""
    return classify_drive(value)


def refused_url(value: Any) -> str:
    """On Windows, the kind `classify_url` gives `value`; off Windows, always ""."""
    if not _IS_WINDOWS:
        return ""
    return classify_url(value)


def check(value: Any, *, network_only: bool = False) -> None:
    """Raise NonLocalPathError when, on Windows, this tier refuses `value`. Off Windows, never."""
    if not _IS_WINDOWS:
        return None
    kind = classify(value, network_only=network_only)
    if kind:
        raise NonLocalPathError(value, kind)
    return None


def _texts(value: Any) -> Iterator[str]:
    """The text a field carries: the value itself, or each text item of a list or tuple. Other values
    (numbers, None, nested objects) carry no path and give nothing."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item


def _argument_kind(intent: str, key: str, value: str, network_only: bool) -> str:
    if key in URL_KEYS:
        kind = classify_url(value)
    elif key == "drive":
        kind = classify_drive(value)
    elif key in PATH_KEYS:
        kind = classify(value)
    elif key in AMBIGUOUS_KEYS:
        kind = classify(value, network_only=not intent.startswith(_PATH_INTENT_PREFIXES))
    else:
        # content, patch, diff, command, query, text and every other field may mention any path
        # without opening it, so they are never judged.
        return ""
    if network_only and kind not in NETWORK_KINDS:
        return ""
    return kind


def _arguments_refusal(intent: str, arguments: Mapping[str, Any], network_only: bool) -> PathRefusal | None:
    for key, value in arguments.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue  # `_` keys are grants the server stamps; the model cannot set them
        for text in _texts(value):
            kind = _argument_kind(intent, key, text, network_only)
            if kind:
                return PathRefusal(key, text, kind)
    return None


def _call_refusal(intent: str, arguments: Mapping[str, Any], network_only: bool) -> PathRefusal | None:
    """The first refused argument of one call, as sent and as the handler will receive it."""
    from core import tool_argument_aliases

    refusal = _arguments_refusal(intent, arguments, network_only)
    if refusal is not None:
        return refusal
    schema = tool_argument_aliases.input_schema_for_intent(intent)
    bound = tool_argument_aliases.bind_known_argument_aliases(arguments, input_schema=schema)
    return _arguments_refusal(intent, bound, network_only)


def argument_refusal(intent: Any, arguments: Any, *, network_only: bool = False) -> PathRefusal | None:
    """On Windows, the first argument of the tool call `intent(arguments)` that names a non-local path,
    or None. Off Windows, always None.

    The call is checked as sent and as its handler will run it: the alias contract that the permission
    gate and the dispatcher read (`core.tool_argument_aliases`) binds a model's near-miss names, such as
    `file_path` or `uri`, onto the contract's own fields. A code-task or repo step is also checked as the
    inner call it runs, under that inner intent's own key rules. `network_only` applies the network tier
    to every argument; the executor passes it for MCP and plugin tools.
    """
    if not _IS_WINDOWS:
        return None
    if not isinstance(arguments, Mapping):
        return None
    name = str(intent or "").strip()
    refusal = _call_refusal(name, arguments, network_only)
    if refusal is None and name in _STEP_INTENTS:
        inner_intent, inner_arguments = arguments.get("intent"), arguments.get("arguments")
        if isinstance(inner_intent, str) and isinstance(inner_arguments, Mapping):
            refusal = _call_refusal(inner_intent.strip(), inner_arguments, network_only)
    return refusal


def _fields_refusal(fields: Mapping[str, Any], names: tuple[str, ...]) -> PathRefusal | None:
    for name in names:
        for text in _texts(fields.get(name)):
            kind = classify(text)
            if kind:
                return PathRefusal(name, text, kind)
    return None


def command_input_refusal(command_id: Any, input_data: Any) -> PathRefusal | None:
    """On Windows, the first filesystem field of a command-registry call that the full rule refuses, or
    None. Off Windows, always None. Only the fields in `_COMMAND_PATH_FIELDS` are read."""
    if not _IS_WINDOWS:
        return None
    if not isinstance(input_data, Mapping):
        return None
    return _fields_refusal(input_data, _COMMAND_PATH_FIELDS.get(str(command_id or ""), ()))


def chat_workspace_refusal(body: Any, source_context: Any) -> PathRefusal | None:
    """On Windows, the first workspace field of a chat request or of its source context that the full
    rule refuses, or None. Off Windows, always None."""
    if not _IS_WINDOWS:
        return None
    for fields in (body, source_context):
        if isinstance(fields, Mapping):
            refusal = _fields_refusal(fields, _CHAT_WORKSPACE_FIELDS)
            if refusal is not None:
                return refusal
    return None


def context_workspace_refusal(source_context: Any) -> PathRefusal | None:
    """On Windows, the workspace of a turn's source context when it is a share or a device path, or
    None. Off Windows, always None.

    Only the network tier: the chat door already refused a caller's own workspace field, so what is
    left here is the server's (an unbound chat's default folder, which may be a redirected Desktop).
    """
    if not _IS_WINDOWS:
        return None
    if not isinstance(source_context, Mapping):
        return None
    for name in _CONTEXT_WORKSPACE_FIELDS:
        text = _text(source_context.get(name))
        kind = classify(text, network_only=True) if text else ""
        if kind:
            return PathRefusal(name, text, kind)
    return None
