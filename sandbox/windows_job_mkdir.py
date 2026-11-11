"""Windows, inside a confined VOOL job only: `os.mkdir(path, 0o700)` becomes a plain `os.mkdir(path)`.

CPython 3.12.4 and later (gh-118486) create a folder asked for with mode 0o700 on Windows with the
protected DACL `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`. No ACE there names a confined
job's capability, so the AppContainer job loses every right on a folder it has just made. pytest's
`tmp_path`, `tmpdir` and `tmp_path_factory`, its cache plugin, and `tempfile.mkdtemp` /
`TemporaryDirectory` all ask for 0o700, so all of them failed inside the Windows sandbox.

installer/bundle/build_bundle.ps1 ships this file into the bundled Windows Python as
`Lib\\site-packages\\vool_windows_job_mkdir.py`, with a one-line `vool_windows_job_mkdir.pth` that
imports it at every start. It does something only on Windows and only when `VOOL_WINDOWS_CONFINED_JOB`
is exactly "1", which VOOL's job runner sets in the environment of a confined Python job
(`sandbox.job_runner._windows_job_environment`) and nowhere else. Then a 0o700 folder is made like any
other: it inherits its parent's DACL, which inside the job's write root is the user, SYSTEM,
Administrators and that job's own capability. Every other mode, the path and dir_fd are passed on
unchanged, and nothing but `os.mkdir` is touched. The AppContainer and its ACLs stay the boundary:
this cannot reach a folder the job may not already write. It imports only `os` (stdlib only, no VOOL
code: the job can read the interpreter's folders, not VOOL's).
"""
import os

#: Set to "1" by VOOL's job runner for a confined Windows Python job, and nowhere else.
ENV_VAR = "VOOL_WINDOWS_CONFINED_JOB"
_MARK = "_vool_confined_job_mkdir"


def install(*, environ=None, module=os, nt=None):
    """Wrap `module.mkdir` when this is Windows and the variable is "1". True when it is wrapped."""
    environ = os.environ if environ is None else environ
    nt = (os.name == "nt") if nt is None else nt
    if not nt or environ.get(ENV_VAR) != "1":
        return False
    real = module.mkdir
    if getattr(real, _MARK, False):
        return True

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if mode == 0o700:
            mode = 0o777
        return real(path, mode, dir_fd=dir_fd)

    setattr(mkdir, _MARK, True)
    mkdir.__doc__ = real.__doc__
    module.mkdir = mkdir
    return True


install()
