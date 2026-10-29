"""Every served-test rig must embed paths in generated `python -c` source as Python literals.

The rigs build little programs and hand them to `python -c` (through `run_in_home` /
`seed_in_home`). Several templates paste the checkout root straight into double quotes —
`sys.path.insert(0, "C:\\Users\\...")` — and on Windows that is a `\\U` escape inside a
string literal, so the generated program is a SyntaxError before it does anything and the
rig cannot even boot. The value the rigs interpolate is a `Path` (`REPO_ROOT` and friends),
whose `repr` (`WindowsPath(...)`) is not source for a string either — so the rigs embed
the path as one Python literal: `{REPO_ROOT.as_posix()!r}` in f-strings (the POSIX
spelling, the same location on every platform), `{root!r}` in `.format` templates whose
callers pass `str(...)`, `repr(str(...))` where a value is substituted textually, and, in
the shared templates that other files still format with a `Path`, `str({root!r})` with
`WindowsPath`/`PosixPath` bound to `Path` in the generated program so that `repr` evaluates.
A literal spells ANY path correctly: quotes and backslashes arrive escaped, never as
escapes of the host string.

This file never runs the generated code and never starts a daemon, so it runs on every
platform: it formats each rig template with a Windows-shaped root (and harmless values for
the other fields), compiles the result, and checks that where the program seeds `sys.path`
the compiled literal is exactly the root it was given (raw or POSIX spelling — one
location).

Two layers, both real production text:

* the module-level rig templates, formatted the way their owners format them;
* every call site in the in-scope rig files that builds such a script (`.format`,
  `.replace`, f-strings — including the inline ones inside fixtures), re-evaluated with the
  path-bearing names bound to the Windows-shaped root.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import inspect
from collections import ChainMap
from pathlib import Path

import pytest

#: A Windows-shaped checkout root: `\U`, `\r`, `\n` and `\x` all sit where a pasted path
#: would turn them into string escapes (or worse) inside generated source. The rigs
#: interpolate `Path` objects, so the given root is one too (the host flavor, exactly what
#: a rig's REPO_ROOT is).
WINDOWS_SHAPED_ROOT = r"C:\Users\runner\new\x"
ROOT_PATH = Path(WINDOWS_SHAPED_ROOT)
#: The same location as the rigs may spell it: raw, or the POSIX spelling an
#: `{x.as_posix()!r}` template embeds.
ROOT_LITERALS = {WINDOWS_SHAPED_ROOT, ROOT_PATH.as_posix()}

RIG_MODULES = (
    "tests._blackbox_served_rig",
    "tests._served_sufficiency_provider",
    "tests.repoops.test_forge_served_journey",
    "tests.test_a9_turn_routing_served",
    "tests.test_blackbox_run_once_served_proof",
    "tests.test_c18_language_parity_served",
    "tests.test_code_task_served_boundary",
    "tests.test_final_answer_authorship_served_p0",
    "tests.test_vool_browser_chat_journey",
    "tests.test_vool_database_chat_journey",
    "tests.test_vool_database_served_proof",
    "tests.test_memory_forget_served_p0",
    "tests.wallet._rig_provider",
    "tests.plugin_mcp_budgets.test_served_plugin_mcp_budgets",
    "tests.test_pb01_served_sufficiency_served",
)

#: The module-level rig templates and harmless values for their non-path fields. `root`
#: (and the other path-like field, the rollback `ws` workspace) get the Windows-shaped root:
#: as a `Path` for the shared templates that other files format with one (their programs
#: bind the Path names), and as the `str` every other template's callers pass.
TEMPLATE_FIELDS = {
    ("tests._blackbox_served_rig", "SEED_MANIFEST"): {"base_url": "http://127.0.0.1:1", "registered": ["m1"]},
    ("tests._served_sufficiency_provider", "SEED"): {"base_url": "http://127.0.0.1:1", "models": [("m1", {})]},
    ("tests.test_a9_turn_routing_served", "_SEED"): {
        "root": WINDOWS_SHAPED_ROOT,
        "local_base": "http://127.0.0.1:1", "paid_base": "http://127.0.0.1:2", "remote_base": "",
        "timeout": 4, "free_a": "m1", "free_b": "m2", "paid": "m3", "remote_mid": "m4"},
    ("tests.test_a9_turn_routing_served", "_RULE_SEED"): {
        "root": WINDOWS_SHAPED_ROOT, "chat_id": "c1", "remote_mid": "m4", "turns": 2},
    ("tests.test_blackbox_run_once_served_proof", "_ROLLBACK_SCRIPT"): {"turn": "t1", "ws": ROOT_PATH},
    ("tests.test_c18_language_parity_served", "_SEED"): {
        "local_base": "http://127.0.0.1:1", "paid_base": "http://127.0.0.1:2", "local": "m1", "paid": "m3"},
    ("tests.test_c18_language_parity_served", "_PREFERENCE_SEED"): {"session_id": "s1"},
    ("tests.test_final_answer_authorship_served_p0", "_SEED"): {
        "root": WINDOWS_SHAPED_ROOT, "base_url": "http://127.0.0.1:1", "registered": ["m1"], "certified": ["m1"]},
    ("tests.test_vool_database_chat_journey", "_CREATE_DB_IN_HOME"): {
        "root": WINDOWS_SHAPED_ROOT,
        "plugin": "vool-database", "ddl": "CREATE TABLE t (x INTEGER)", "seed": "INSERT INTO t VALUES (1)"},
    ("tests.test_vool_database_served_proof", "_STUB_SEED"): {
        "root": WINDOWS_SHAPED_ROOT, "base_url": "http://127.0.0.1:1", "model": "m1"},
    ("tests.test_memory_forget_served_p0", "SEED"): {
        "root": WINDOWS_SHAPED_ROOT, "base_url": "http://127.0.0.1:1", "models": ["m1"]},
    ("tests.wallet._rig_provider", "CERTIFY_SEED"): {"root": WINDOWS_SHAPED_ROOT, "registered": ["m1"]},
}

#: Attribute names that build a string at a rig call site. Calls to plain names
#: (`run_in_home(...)`, `seed_in_home(...)`) are deliberately NOT builders: evaluating one
#: would run the generated program, and this file only compiles it.
_STRING_BUILDERS = {"format", "replace", "dedent", "join", "strip", "lstrip", "rstrip"}


class _Stub:
    """A stand-in for a rig object a template call site interpolates (`provider.base_url`,
    `self.local.base_url`, ...): every attribute chain bottoms out in a value that is valid
    wherever a template needs a number, and quoted positions take it as plain text."""

    def __getattr__(self, _name: str) -> _Stub:
        return _Stub()

    def __iter__(self):
        return iter(("1",))

    def __str__(self) -> str:
        return "1"

    def __repr__(self) -> str:
        return "1"


#: Values for the names a real call site interpolates: path-bearing names get the
#: Windows-shaped root, everything else a harmless literal or a stub object.
CALL_SITE_VALUES = {
    "REPO_ROOT": ROOT_PATH,
    "plugins_root": ROOT_PATH,
    "workspace": ROOT_PATH,
    "turn_id": "t1",
    "runtime_chat_id": "c1",
    "canonical": lambda value: "c1",
    "self": _Stub(),
    "provider": _Stub(),
    "world": _Stub(),
    "rules": (),
}


def _bound_names(tree: ast.AST) -> set[str]:
    """The names a generated program binds itself: imports (under their alias) and plain
    assignments. A call to any other non-builtin name in that program is a NameError."""
    bound = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Assign):
            bound.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return bound


def _carries_root(node: ast.AST, bound: set[str]) -> bool:
    """The root must arrive as DATA, never spliced source text: one string literal of the
    given root, or (for the shared templates, whose out-of-file callers interpolate a Path)
    a str()/Path() built from exactly that literal, where the generated program itself
    binds the name it calls (a bare `{root!r}` of a Path emits `PosixPath(...)`, which only
    runs where the program binds it)."""
    if isinstance(node, ast.Constant):
        return node.value in ROOT_LITERALS
    if (isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Name)
            and node.func.id in {"str", "Path", "WindowsPath", "PosixPath"}
            and node.func.id in bound):
        return _carries_root(node.args[0], bound)
    return False


def _assert_embeds_root(source: str, label: str) -> None:
    try:
        compile(source, "<rig>", "exec")
    except SyntaxError as exc:
        pytest.fail(f"{label}: the generated rig source does not compile ({exc.msg}, line {exc.lineno}):\n{source}")
    tree = ast.parse(source)
    bound = _bound_names(tree)
    inserted = [call.args[1] for call in ast.walk(tree)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "insert" and isinstance(call.func.value, ast.Attribute)
                and call.func.value.attr == "path" and isinstance(call.func.value.value, ast.Name)
                and call.func.value.value.id == "sys" and len(call.args) > 1]
    assert inserted, f"{label}: the generated rig program never seeds sys.path"
    for argument in inserted:
        assert _carries_root(argument, bound), (
            f"{label}: sys.path.insert must take the root as one embedded literal (raw or POSIX "
            f"spelling, or a str()/Path() of exactly that literal that the program binds), "
            f"got {ast.dump(argument)}")


def _loaded_modules() -> dict:
    return {name: importlib.import_module(name) for name in RIG_MODULES}


def _template_cases():
    modules = _loaded_modules()
    return [pytest.param(modules[name], getattr(modules[name], attribute),
                         {"root": ROOT_PATH, **fields}, id=f"{name}::{attribute}")
            for (name, attribute), fields in TEMPLATE_FIELDS.items()]


@pytest.mark.parametrize(("module", "template", "fields"), _template_cases())
def test_module_template_embeds_the_root_as_one_literal(module, template, fields):
    """Each module-level rig template, formatted with a Windows-shaped root, must compile
    and seed sys.path with exactly that root."""
    _assert_embeds_root(template.format(**fields), f"{module.__name__} template")


def _assigned_strings(tree: ast.AST) -> dict:
    """Plain string constants bound to a name exactly once anywhere in the module: the local
    templates a call site formats (`script = '''...'''` then `script.replace(...)`)."""
    seen: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    seen.setdefault(target.id, []).append(node.value)
    return {name: values[0].value for name, values in seen.items()
            if len(values) == 1 and isinstance(values[0], ast.Constant) and isinstance(values[0].value, str)}


def _static_text(node: ast.AST, names: dict) -> str:
    """Every string the expression builds from before any value is interpolated: its own
    literals plus the template constants it names."""
    parts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    parts += [names[n.id] for n in ast.walk(node)
              if isinstance(n, ast.Name) and isinstance(names.get(n.id), str)]
    return "\n".join(parts)


def _interpolates(node: ast.AST) -> bool:
    """A real builder interpolates a value somewhere inside it: an f-string, or a
    `.format`/`.replace` call. A bare `textwrap.dedent('<raw template>')` only DEFINES a
    template (its placeholders are still unsubstituted); those definitions are covered by
    the module-template cases instead."""
    return any(isinstance(n, ast.JoinedStr)
               or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {"format", "replace"})
               for n in ast.walk(node))


def _runs_something(node: ast.AST) -> bool:
    """The expression calls a plain name that is neither a builtin nor a stubbed call-site
    value (`run_in_home(...)`): evaluating it would run the generated program, so it is
    never a builder to re-evaluate -- the template text inside it is."""
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and not hasattr(builtins, n.func.id) and n.func.id not in CALL_SITE_VALUES
               for n in ast.walk(node))


def _span(node: ast.AST):
    return (node.lineno, node.col_offset), (node.end_lineno, node.end_col_offset)


def _encloses(outer: ast.AST, inner: ast.AST) -> bool:
    outer_start, outer_end = _span(outer)
    inner_start, inner_end = _span(inner)
    return inner_start >= outer_start and inner_end <= outer_end


def _call_site_builders(module) -> list:
    """The outermost string-building expressions in this module whose text seeds
    `sys.path` — every place the file embeds a path into generated `python -c` source at a
    real call site."""
    tree = ast.parse(inspect.getsource(module))
    names = ChainMap(vars(module), _assigned_strings(tree))
    candidates = [node for node in ast.walk(tree)
                  if (isinstance(node, ast.JoinedStr)
                      or (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr in _STRING_BUILDERS))
                  and _interpolates(node) and not _runs_something(node)]
    marked = [node for node in candidates if "sys.path.insert(" in _static_text(node, names)]
    return [node for node in marked
            if not any(other is not node and _encloses(other, node) for other in marked)]


def _call_site_cases():
    cases = []
    for module in _loaded_modules().values():
        for node in _call_site_builders(module):
            cases.append(pytest.param(module, node, id=f"{module.__name__}:{node.lineno}"))
    return cases


@pytest.mark.parametrize(("module", "node"), _call_site_cases())
def test_call_site_script_embeds_the_root_as_one_literal(module, node):
    """Each real call site, re-evaluated with a Windows-shaped root bound to every
    path-bearing name it interpolates, must compile and seed sys.path with exactly that
    root."""
    expression = compile(ast.fix_missing_locations(ast.Expression(node)), "<rig-builder>", "eval")
    local_templates = _assigned_strings(ast.parse(inspect.getsource(module)))
    namespace = ChainMap(CALL_SITE_VALUES, vars(module), local_templates, vars(builtins))
    try:
        source = eval(expression, {}, dict(namespace))
    except NameError as exc:
        pytest.fail(f"{module.__name__}:{node.lineno}: no harmless value for {exc.name}; extend CALL_SITE_VALUES")
    assert isinstance(source, str), f"{module.__name__}:{node.lineno}: builder is not a string"
    _assert_embeds_root(source, f"{module.__name__}:{node.lineno}")
