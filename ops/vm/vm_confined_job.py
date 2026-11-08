"""Clean-VM pass: a confined job in the INSTALLED VOOL app (Confinement B, port 3042a0b6 or later).

Run inside the VM, after a silent install, with the installed interpreter:
    & "$env:LOCALAPPDATA\\Programs\\VOOL\\python\\python.exe" -X utf8 vm_confined_job.py
(or wherever {app} is). It prints one JSON report and exits 0 only when every check passes.
It touches only a fresh temp folder of its own.
"""
import json
import os
import pathlib
import sys
import tempfile

APP_ROOT = pathlib.Path(sys.executable).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_ROOT))
os.chdir(APP_ROOT)

from sandbox import windows_confinement_pin as pin  # noqa: E402
from sandbox.job_runner import JobRunner  # noqa: E402
from sandbox.resource_limits import ExecutionPolicy  # noqa: E402

# The child reads its parameters from p.json in its workspace, so no outside path is in argv
# (the static argv guard would refuse it before the kernel is ever tested).
CHILD = r"""
import json, os, socket
P = json.load(open("p.json", encoding="utf-8"))
print("RAN", flush=True)
try:
    kind = P["kind"]
    if kind == "write_inside":
        open("inside.txt", "w").write("ok")
    elif kind == "write_outside":
        open(P["path"], "w").write("pwned")
    elif kind == "read_other":
        print("LEAK:" + open(P["path"]).read())
    elif kind == "plant_git":
        os.makedirs(os.path.join("nested", ".git", "hooks"))
        open(os.path.join("nested", ".git", "HEAD"), "w").write("ref: refs/heads/main\n")
    elif kind == "network":
        socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    elif kind == "real_profile":
        print("LIST:" + ",".join(os.listdir(P["path"])[:3]))
    print("ESCAPED", flush=True)
except Exception as exc:
    print("BLOCKED:" + type(exc).__name__, flush=True)
"""


def run(ws: pathlib.Path, params: dict) -> dict:
    (ws / "p.json").write_text(json.dumps(params), encoding="utf-8")
    try:
        result = JobRunner(ExecutionPolicy(workspace_root=ws)).run([sys.executable, "-c", CHILD], cwd=ws)
    except Exception as exc:  # a refusal is reported, never hidden
        return {"refused": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return {"rc": result.returncode, "stdout": result.stdout[-300:], "stderr": result.stderr[-300:]}


def ran_and(outcome: dict, word: str) -> bool:
    return "RAN" in outcome.get("stdout", "") and word in outcome.get("stdout", "")


base = pathlib.Path(tempfile.mkdtemp(prefix="vool-vm-confine-"))
a, b, outside = base / "a", base / "b", base / "outside"
for folder in (a, b, outside):
    folder.mkdir()
(a / ".env").write_text("A-SECRET", encoding="utf-8")
(outside / "secret.txt").write_text("original", encoding="utf-8")

report: dict = {"app_root": str(APP_ROOT), "launcher": str(pin.verified_launcher(APP_ROOT))}
report["write_inside"] = run(a, {"kind": "write_inside"})
report["write_outside"] = run(a, {"kind": "write_outside", "path": str(outside / "secret.txt")})
report["read_other_workspace"] = run(b, {"kind": "read_other", "path": str(a / ".env")})
report["network"] = run(a, {"kind": "network"})
report["real_profile"] = run(a, {"kind": "real_profile", "path": os.path.expanduser("~")})
report["plant_git"] = run(b, {"kind": "plant_git"})

checks = {
    "launcher is the installed, pinned one": report["launcher"].lower().endswith("\\broker\\confine\\voolconfine.exe")
    and str(APP_ROOT.parent).lower() in report["launcher"].lower(),
    "job ran and wrote inside its workspace": ran_and(report["write_inside"], "ESCAPED")
    and (a / "inside.txt").read_text(encoding="utf-8") == "ok",
    "write outside blocked": ran_and(report["write_outside"], "BLOCKED")
    and (outside / "secret.txt").read_text(encoding="utf-8") == "original",
    "other workspace unreadable": ran_and(report["read_other_workspace"], "BLOCKED")
    and "A-SECRET" not in report["read_other_workspace"].get("stdout", ""),
    "network blocked": ran_and(report["network"], "BLOCKED"),
    "real profile unlistable": ran_and(report["real_profile"], "BLOCKED"),
    "planted .git refused": "NestedGitMetadataError" in report["plant_git"].get("refused", ""),
}
report["checks"] = checks
report["pass"] = all(checks.values())
print(json.dumps(report, indent=2))
sys.exit(0 if report["pass"] else 1)
