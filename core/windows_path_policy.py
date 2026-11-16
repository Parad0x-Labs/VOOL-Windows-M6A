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
points at a share, and the text of shell commands, patches and file contents of VOOL's own tools. An
MCP or plugin tool's call is the exception: every text in it is judged (`_server_tool_refusal`).
"""
from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NamedTuple
from urllib.parse import unquote, unwrap

#: Patched by tests to drive the Windows decision on any platform; read at call time.
_IS_WINDOWS: bool = os.name == "nt"
STATUS = "non_local_path_refused"
#: The kinds that reach another machine or a device. The network tier refuses only these.
NETWORK_KINDS = frozenset({"unc", "device", "nt_namespace"})
#: The kinds an MCP or plugin call is refused for when VOOL cannot tell what a value would name on the
#: server: a `%NAME%` left unresolved, or arguments that cannot be judged.
_UNRESOLVED_VARIABLE_KIND = "unresolved_variable"
_UNJUDGED_KIND = "unjudged"

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
#: The command-registry fields that carry the base URL a command's own HTTP calls are sent to, by
#: command. The handler appends a route (`base_url + "/api/chat"`) and urllib opens the result, and
#: urllib's file handler stats a `file:` URL's path: `file:////host/s` opens `\\host\s\api\chat`.
_COMMAND_URL_FIELDS: dict[str, tuple[str, ...]] = {
    "agents.start": ("base_url",),
    "council.convene": ("base_url",),
    "council.resume": ("base_url",),
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


def refusal_text(value: Any, kind: str, *, nothing: str = "opened") -> str:
    """The sentence a refusal gives, naming `value` as it was written.

    The sentence is relayed inside markdown, so the value loses its backticks and line breaks and is
    cut to 120 characters. `nothing` names what did not happen: "opened" for a read, "written" when
    the refused path was where the user asked VOOL to write.
    """
    text = _text(value)
    shown = _LINE_BREAK_RE.sub(" ", (str(value) if text is None else text).replace("`", ""))[:120]
    if kind in (_UNRESOLVED_VARIABLE_KIND, _UNJUDGED_KIND):
        sentence = "VOOL can't tell where this value would point on the server, so the call was not run."
        return f"`{shown}`: {sentence}" if shown else sentence
    if kind in NETWORK_KINDS:
        return (
            f"`{shown}` is a network or device path. On Windows, VOOL only opens files on a local drive "
            f"(like C:\\Users\\you\\file.txt) or inside the current folder, so nothing was {nothing}."
        )
    return (
        f"`{shown}` is not a full local path. On Windows, use a drive letter and folder "
        f"(like C:\\Users\\you\\file.txt) or a path inside the current folder. Nothing was {nothing}."
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


# A drive letter followed by two separators: `C://host/s`, `C:\\\\host\\s`, `C:/\\host`, `C:\\/host`.
_DOUBLED_DRIVE_SEPARATOR_RE = re.compile(r"^\s*[\"'`]?[A-Za-z]:[\\/]{2}")


def refused_doubled_drive(value: Any) -> str:
    """On Windows, ``"unc"`` for a drive path with a doubled separator right after the colon
    (`C://host/s/x`, `C:\\\\host\\s`, `C:/\\x`, `C:\\/x`); off Windows, and for any other value, "".

    `classify` calls these full local paths, but a lane that reads text from its first `/` (the
    heartbeat poll, a URL-ish reader) sees the share `//host/s` in them, so they are refused like a
    share and fail closed: nothing that reads a path out of a message stats, lists or resolves one.
    The message lanes ask this beside `refused_form` (`write_root_honesty.typed_path_kind` and
    `_spaces_may_extend`, the marked-folder lanes, the planner's folder cleaner). Post-launch this
    rule belongs in `classify` itself (DIAGNOSIS, heartbeat-drive-share)."""
    if not _IS_WINDOWS:
        return ""
    text = _text(value)
    if text is None or not _DOUBLED_DRIVE_SEPARATOR_RE.match(text):
        return ""
    return "unc"


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
        # The network tier hands a root- or drive-relative value on to the tool, which may expand the
        # variables in it: on Windows `\%HOMEPATH%\x` is `\\Users\you\x`, a share, once expanded. So the
        # value is judged again as expanded, once, and refused only for a network kind there.
        expanded = ntpath.expandvars(value)
        kind = _argument_kind(intent, key, expanded, False) if expanded != value else ""
        return kind if kind in NETWORK_KINDS else ""
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
    to every argument, and judges a partial path that carries `%VAR%` or `$VAR` again as expanded;
    `tool_call_refusal` passes it for MCP and plugin tools.
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


def _server_defined_tool(intent: str) -> bool:
    """Whether `intent` is an MCP tool or a registered plugin tool. A registry that cannot be read
    answers False, which keeps the full rule."""
    try:
        from core.execution.mcp_bridge import is_mcp_intent
        from core.tool_registry import tool_for_intent

        if is_mcp_intent(intent):
            return True
        contract = tool_for_intent(intent)
    except Exception:
        return False
    return contract is not None and str(getattr(contract, "source", "") or "").startswith("plugin:")


# A `%NAME%` whose NAME has a variable name's shape: a letter or `_`, then letters, digits, `_` and
# parentheses (`%ProgramFiles(x86)%`), no spaces. `$NAME`, `${NAME}` and `$(` are judged only as
# expanded in VOOL's environment and never refused unresolved: they are everyday code and shell text.
_PERCENT_VARIABLE_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_()]*%")
# An http(s) URL as its authority, then the rest, where alone a `%XX` of two hex digits is percent-encoding.
_HTTP_URL_RE = re.compile(r"(?i)(https?://[^/?#\s\\]*)([^\s\"'<>`]*)")
_PERCENT_ENCODING_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_TOKEN_SEPARATORS_RE = re.compile(r"[\s\"'`<>|,;=()\[\]{}]+")
_RUN_RE = re.compile(r"\S*")
_PATH_SEPARATORS = "\\/"
_NT_OBJECT_PREFIX = "\\??\\"
_MAX_EXPANSION_ROUNDS = 8


def _server_tool_state(intent: str) -> bool | None:
    """Whether `intent` is an MCP tool or a registered plugin tool: True, False, or None when a
    registry cannot be read. The two registries are asked separately, so one that fails does not
    hide the other's answer."""
    unknown = False
    try:
        from core.execution.mcp_bridge import is_mcp_intent

        if is_mcp_intent(intent):
            return True
    except Exception:
        unknown = True
    try:
        from core.tool_registry import tool_for_intent

        contract = tool_for_intent(intent)
        plugin = contract is not None and str(getattr(contract, "source", "") or "").startswith("plugin:")
    except Exception:
        return None
    if plugin:
        return True
    return None if unknown else False


def _variable_expansions(text: str) -> tuple[list[str], bool]:
    """`text` and each further `%VAR%`, `${VAR}` and `$VAR` expansion of it in VOOL's environment,
    and whether the expansion had not settled after `_MAX_EXPANSION_ROUNDS` rounds."""
    levels = [text]
    current = text
    for _ in range(_MAX_EXPANSION_ROUNDS):
        expanded = ntpath.expandvars(current)
        if expanded == current:
            return levels, False
        levels.append(expanded)
        current = expanded
    return levels, True


def _unresolved_reference(text: str) -> bool:
    """Whether `text` still holds a `%NAME%` reference with a variable name's shape. A server may
    expand one in its own environment, so one left unresolved is an unknown value; text such as
    `50% off and 20% more` holds none. Only in the path, query and fragment of an http(s) URL, which
    no expansion turns into a share, is a `%XX` of two hex digits taken as percent-encoding instead of
    a reference."""

    def without_encoding(match: re.Match[str]) -> str:
        return match.group(1) + _PERCENT_ENCODING_RE.sub("", match.group(2))

    return _PERCENT_VARIABLE_RE.search(_HTTP_URL_RE.sub(without_encoding, text)) is not None


def _url_scheme_before(text: str, index: int) -> bool:
    """Whether the `//` at `index` follows a URL scheme of two or more characters (`https://`). A
    one-letter scheme is a drive (`C://host/s`), whose doubled separator reads as a share."""
    if index < 3 or text[index - 1] != ":":
        return False
    start = index - 1
    while start > 0 and (text[start - 1].isascii() and (text[start - 1].isalnum() or text[start - 1] in "+.-")):
        start -= 1
    scheme = text[start : index - 1]
    return len(scheme) >= 2 and scheme[0].isascii() and scheme[0].isalpha()


def _text_candidates(text: str) -> list[str]:
    """`text`, each word of it, and the run up to the next whitespace from every two adjacent path
    separators (slash or backslash, in any mix) and every NT object prefix in it, so a share or a
    device path glued to any character is still judged. Only a `//` right after a URL scheme
    (`https://`) starts no run; a pair right after a separator is inside a run already."""
    candidates = {text: None}
    for word in _TOKEN_SEPARATORS_RE.split(text):
        if word:
            candidates.setdefault(word, None)
    for index in range(len(text) - 1):
        if not text.startswith(_NT_OBJECT_PREFIX, index):
            if text[index] not in _PATH_SEPARATORS or text[index + 1] not in _PATH_SEPARATORS:
                continue
            if index and text[index - 1] in _PATH_SEPARATORS:
                continue
            if text[index : index + 2] == "//" and _url_scheme_before(text, index):
                continue
        candidates.setdefault(_RUN_RE.match(text, index).group(), None)
    return list(candidates)


def _server_candidate_kind(candidate: str) -> str:
    """The network kind of one candidate text of a server-defined tool's arguments, or "": judged as a
    path and as a URL (`file://host/share`), whatever argument name carries it."""
    kind = classify(candidate, network_only=True)
    if not kind:
        kind = classify_url(candidate)
    return kind if kind in NETWORK_KINDS else ""


def _server_text_refusal(where: str, text: str) -> PathRefusal | None:
    """Whether one text of a server-defined tool's arguments names a share or a device path, as a
    whole, as any word of it or as a run glued to any character, as written or as expanded, or holds
    a `%NAME%` VOOL cannot resolve, or expands past `_MAX_EXPANSION_ROUNDS` rounds."""
    levels, unsettled = _variable_expansions(text)
    for level in levels:
        for candidate in _text_candidates(level):
            kind = _server_candidate_kind(candidate)
            if kind:
                return PathRefusal(where, text, kind)
    if unsettled or _unresolved_reference(levels[-1]):
        return PathRefusal(where, text, _UNRESOLVED_VARIABLE_KIND)
    return None


def _server_tool_refusal(arguments: Any) -> PathRefusal | None:
    """The first text anywhere in a server-defined tool's arguments that names a share or a device
    path, or that holds a variable VOOL cannot resolve, or None.

    An MCP or plugin tool's arguments mean whatever its server says, so no key set tells which of
    them is a path: every text is judged, keys as well as values, under any name and inside nested
    mappings, lists, tuples and sets, and a text that parses as JSON is judged again as what it
    holds. Any text with `\\\\` or `//` in it (a URL's own `scheme://` aside) is refused, so XPath
    selectors and `//` code comments are refused too (owner decision D11, option A). Top-level `_`
    keys are judged like any other: the MCP bridge forwards every key to the server unstripped
    (`dict(arguments)`), so a `_` key the model sets reaches it. Arguments that are not a mapping are
    walked as given and also as the bridge reads them (`dict(arguments or {})`); when the bridge could
    not read them and nothing else refused, they are refused, because nothing could judge what the
    server would get. A value that is neither a container, a text, a number nor None cannot be judged
    and is refused. The walk keeps its own stack and holds every container it has seen, so deep or
    cyclic nesting cannot overflow or loop.
    """
    import json

    roots: list[Any] = [arguments]
    readable = True
    if not isinstance(arguments, Mapping) and arguments:
        try:
            roots.append(dict(arguments))
        except Exception:
            readable = False
    stack: list[tuple[str, Any]] = []
    for root in reversed(roots):
        if isinstance(root, Mapping):
            for key, value in reversed(list(root.items())):
                stack.append((str(key), value))
                stack.append((str(key), key))
        else:
            stack.append(("arguments", root))
    seen: dict[int, Any] = {}
    while stack:
        where, value = stack.pop()
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            if id(value) in seen:
                continue
            seen[id(value)] = value
            children: list[tuple[str, Any]] = []
            if isinstance(value, Mapping):
                for key, item in value.items():
                    children.append((f"{where}.{key}", key))
                    children.append((f"{where}.{key}", item))
            else:
                for index, item in enumerate(value):
                    children.append((f"{where}[{index}]", item))
            stack.extend(reversed(children))
            continue
        if isinstance(value, str):
            text = value
        elif isinstance(value, (bytes, bytearray)):
            text = bytes(value).decode("utf-8", "replace")
        elif isinstance(value, os.PathLike):
            text = os.fsdecode(value)
        else:
            return PathRefusal(where, "", _UNJUDGED_KIND)
        if not text:
            continue
        refusal = _server_text_refusal(where, text)
        if refusal is not None:
            return refusal
        if text.lstrip()[:1] in ("{", "[", '"'):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            stack.append((where, parsed))
    if not readable:
        return PathRefusal("arguments", str(arguments)[:120], _UNJUDGED_KIND)
    return None


def _server_tier_refusal(intent: str, arguments: Any) -> PathRefusal | None:
    """The network-tier judgment of a server-defined tool's call: `argument_refusal` on the network
    tier, then the walk of every text. A judgment that raises refuses."""
    try:
        if isinstance(arguments, Mapping):
            refusal = argument_refusal(intent, arguments, network_only=True)
            if refusal is not None:
                return refusal
        return _server_tool_refusal(arguments)
    except Exception:
        return PathRefusal("arguments", "", _UNJUDGED_KIND)


def tool_call_refusal(intent: Any, arguments: Any) -> PathRefusal | None:
    """On Windows, `argument_refusal` in the tier the tool doors judge `intent(arguments)` on, or None.
    Off Windows, always None.

    An MCP or plugin tool is judged on the network tier, because its arguments mean whatever its server
    says; every other tool, a step that runs one included, on the full rule. The model executor, the
    authorized execution boundary and the runtime door all ask this, so a call the executor put to the
    operator is not refused by the next door once it is approved. The network tier is applied directly,
    not only to what the full rule refuses: the full rule judges a URL name, and an ambiguous name of a
    tool that is not a file tool, on the network tier as typed, and only the network tier judges such a
    value again as expanded. An MCP or plugin tool's call is also walked by `_server_tool_refusal`: any
    text in it, under any name, that holds `\\\\` or `//` (a URL's `scheme://` aside) or a variable
    VOOL cannot resolve is refused. A registry that cannot be read applies both tiers.
    """
    if not _IS_WINDOWS:
        return None
    name = str(intent or "").strip()
    server_tool = _server_tool_state(name)
    if server_tool is True:
        return _server_tier_refusal(name, arguments)
    if server_tool is None:
        # A registry that cannot be read leaves the tier unknown: both tiers judge the call.
        refusal = _server_tier_refusal(name, arguments)
        if refusal is not None:
            return refusal
    return argument_refusal(intent, arguments)


def _base_url_kind(value: str) -> str:
    """`classify_url` for a base URL that a route is appended to before urllib opens it.

    The URL urllib opens is judged as well as the value: urllib first drops a `URL:` wrapper
    (`URL:file:////host/s` opens the share), and a file URL with no host and no path names a share
    once the route is appended (`file:///` plus `/api/chat` is `file:////api/chat`, the share
    `\\\\api\\chat`).
    """
    return classify_url(value) or classify_url(unwrap(value) + "/api")


def _fields_refusal(
    fields: Mapping[str, Any], names: tuple[str, ...], judge: Callable[[str], str] = classify
) -> PathRefusal | None:
    for name in names:
        for text in _texts(fields.get(name)):
            kind = judge(text)
            if kind:
                return PathRefusal(name, text, kind)
    return None


def command_input_refusal(command_id: Any, input_data: Any) -> PathRefusal | None:
    """On Windows, the first filesystem field of a command-registry call that the full rule refuses, or
    None. Off Windows, always None. Only the fields in `_COMMAND_PATH_FIELDS` are read, and the base
    URLs in `_COMMAND_URL_FIELDS`, which are judged as the URL their handler opens."""
    if not _IS_WINDOWS:
        return None
    if not isinstance(input_data, Mapping):
        return None
    command = str(command_id or "")
    return _fields_refusal(input_data, _COMMAND_PATH_FIELDS.get(command, ())) or _fields_refusal(
        input_data, _COMMAND_URL_FIELDS.get(command, ()), _base_url_kind
    )


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
