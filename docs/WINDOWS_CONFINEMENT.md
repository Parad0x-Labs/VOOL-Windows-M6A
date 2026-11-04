# Windows job confinement (VoolConfine)

Status: **interface only** (chunk 0 of the phased plan). Nothing here confines anything yet: the
Python side reports the backend unavailable, so every confined job on Windows is still refused
(`KernelIsolationUnavailableError`), exactly as before this document existed.

## 1. Where it plugs in

`sandbox.job_runner.JobRunner._with_network_isolation` is the one door every confined job goes
through. It wraps argv in a backend prefix or refuses. Windows adds one backend in the same shape:

    [<VoolConfine.exe>, "--policy", <policy.json>, "--", *argv]

`sandbox/windows_confinement.py` builds that prefix. It returns `None` whenever the backend cannot be
proven to work. `None` means the caller refuses the job. Nothing ever runs unwrapped.

## 2. The launcher, `VoolConfine.exe`

- Shipped self-contained in its own folder: `{app}\broker\confine\` (installed; vool.iss copies it with
  the broker) or `windows\broker\publish\confine\` (a checkout's publish). It is resolved from those
  two places only, never from PATH.
- **Pinned.** publish.ps1 writes a manifest `{relpath: sha256}` covering EVERY file in that folder.
  The installed copy is `{app}\config\confine-pin.json`; the checkout copy is
  `windows\broker\publish\confine-pin.json`. `sandbox/windows_confinement_pin.verified_launcher`
  accepts the launcher only when every listed file matches and no unlisted file is present, because
  a planted DLL is code too. A missing pin, a mismatch, an extra file or a missing file all mean
  unavailable.
- **Pin file shape.** UTF-8 without a BOM, exactly two keys:
  `{"schema": "vool.windows_confinement.pin.v1", "files": {"<relpath with />": "<sha256, lowercase hex>"}}`.
  Any other key or shape means unavailable.
- **The launcher's own environment (caller, chunk 3).** The pin proves the files, but the .NET host
  still loads code named by environment variables. The launcher is published with
  `StartupHookSupport=false`, and the caller drops every variable starting `CORECLR_`, `COR_`,
  `COMPlus_`, `DOTNET_` or `COREHOST_` (case-insensitive) from the env it launches VoolConfine.exe
  with. Otherwise a planted profiler or hook loads a DLL from outside the pinned folder, unconfined.
- **Lifetime (caller, chunk 3).** Each launcher runs inside VOOL's own kill-on-close job, the way
  the broker does, so no launcher outlives VOOL. Otherwise one would hold its files open and block
  an upgrade.
- Command line, three forms. All are run unconfined by VOOL, never by a job:
  - `VoolConfine.exe --policy <path> -- <program> [args...]` runs a job. It only VERIFIES the ACLs
    and never writes them; a verify miss is 125.
  - `VoolConfine.exe grant --policy <path>` writes the grants and deny ACEs (idempotent).
  - `VoolConfine.exe revoke --policy <path>` removes exactly what grant added.
  - `VoolConfine.exe scan --policy <path>` runs the repository scan alone (unconfined, no child)
    and rewrites the status file's `new_repositories` and `repositories_scanned`. Exit 0 when no
    unprotected layout exists, 123 when one does, 125 when the scan could not finish. The caller
    runs it after every job whose launcher it killed.
  - Grant never "heals" a layout that appeared after the root was granted: if a write root already
    holds its capability's grant and a repository layout under it lacks the deny, grant refuses
    (125, `repo_appeared`) and leaves it for the operator. Only a first grant protects existing repositories.
- For the run form: Everything after `--` is
  the child's argv, passed on with Windows quoting rules. `--policy` is required.
- The child runs in an **AppContainer** (no capabilities unless the policy grants network). It
  starts inside a **Job Object** with:
  - KILL_ON_JOB_CLOSE;
  - breakaway not allowed;
  - an active-process limit;
  - a job memory limit;
  - UI restrictions.
  The launcher holds the only job handle. Ending the launcher, for any reason, ends the child's
  whole tree.
- **argv[0] must be an absolute path to an `.exe`** (case-insensitive). The launcher never searches
  PATH; a relative program is 126. A `.cmd`, `.bat` or any other extension is 126 too, because
  CreateProcess runs a batch file through `cmd.exe` and its own quoting rules (argument injection).
  The caller resolves shims such as `npm` to `node.exe` plus the tool's JS entry point.
- stdin, stdout and stderr are the launcher's own, handed to the child through an explicit handle
  list. No other handle is inherited.
- Environment: the launcher's own (the caller builds it), with `TEMP`/`TMP` set to the policy's
  `temp_dir`, inside a write root (the caller uses its workspace `.sandbox-tmp`). Windows rewrites
  TEMP/TMP of an AppContainer process to the profile's shared `AC\Temp` whatever the env block says
  (measured, windows-broker run 41902285066). So the launcher starts a pinned trampoline,
  `VoolConfineStart.exe`, from the same confine folder. It sets TEMP/TMP to `temp_dir`, starts
  the program with its command line passed through verbatim, in the same job, and exits with its
  code. The confine folder gets a read grant for the package SID, and `AC\Temp` is denied write for
  the package SID, so anything still landing there fails instead of sharing one folder across jobs.

### Exit codes

| Code | Meaning |
|---|---|
| child's code | The child ran; this is its exit code, passed through unchanged. |
| 125 | Confinement could not be set up (profile, ACL verify, job, process creation). **The child never started.** |
| 126 | The command line or the policy is invalid. The child never started. |
| 123 | The child ran, but a repository layout (a `.git` dir, or a bare repo) appeared under a write root. The result is refused; the status file lists the paths. |
| 124 | `max_seconds` elapsed. The job was closed, so the whole tree is gone. |

A child may exit with ANY code, 124-126 included, and it shares the launcher's stderr, so neither
the code nor a stderr line proves anything. The trusted record is the **status file** at the
policy's `status_path`. It sits outside every writable root, so the confined child can't write it:

- The launcher writes `{"schema": "vool.windows_confinement.status.v1", "child_started": true}`
  only after the child was created SUSPENDED, assigned to the job, and only then resumed.
- At exit it rewrites the file with `"timed_out"` (bool) and `"exit_code"` (int).
- The caller trusts the exit code only when the status file says `child_started: true`. A missing,
  malformed or `child_started: false` status means **refused**, whatever the code.
- The stderr line `VOOLCONFINE-REFUSED: <reason>` exists for people reading logs only.

Status file fields:

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `vool.windows_confinement.status.v1` |
| `child_started` | bool | Required. Only true after the suspended-create, assign-to-job, resume sequence succeeded. |
| `exit_code` | int | The child's exit code, at exit. |
| `timed_out` | bool | `max_seconds` elapsed (the launcher also exits 124). |
| `limit` | `"time"`, `"memory"`, `"processes"` or null | The job limit that ended the child, if any. |
| `error` | string or null | Why setup failed, when `child_started` is false. |
| `new_repositories` | list of paths | Repository layouts that appeared under a write root during the job. Non-empty means refused (and exit 123). |
| `repositories_scanned` | bool | True only once the post-job repository scan COMPLETED. A launcher that was killed (cancel, the caller's timeout) never sets it, so an empty `new_repositories` without it proves nothing: the caller runs the `scan` form before it trusts or returns anything. |

### Identity: one profile, one capability per write root

- **Profile.** One fixed AppContainer profile named `VOOL.Confine.v1`. Its package SID is shared by
  every job, so it only ever receives READ grants, plus explicit DENY-write ACEs.
- **Read grants (package SID).** Read and execute on `read_roots`.
- **Deny ACEs.** Deny write on `deny_write_paths` and on every `.git` under a write root; deny all
  access on `deny_all_paths`.
  **A deny binds only the SIDs it names.** Windows judges the package SID and each capability on its
  own, and grants whatever any one of them is allowed. Measured in windows-broker run 41901543557:
  a package-SID deny did NOT stop a write that the root's capability was allowed. So:
  - under a write root, every deny names the package SID AND that root's capability, and a
    repository counts as protected only when both are denied;
  - outside every write root, the deny names the package SID, and Verify refuses (125) a deny path
    whose own DACL gives ALL APPLICATION PACKAGES or ALL RESTRICTED APP PACKAGES the denied access,
    because no deny we write there would bind them.
- **Write grants (per-root capability).** Read and write (inheritable) go to a capability SID made
  for that root only: `DeriveCapabilitySidsFromName("vool.root.<sha256 hex>")`, where the hash is of
  the root with `/` turned into `\`, trailing `\` stripped, ASCII letters upper-cased (nothing else), UTF-8
  (`sandbox.windows_confinement.root_capability_name` and `VoolConfine.Core.Identity.RootCapabilityName`,
  which both tests pin to the vector `C:\work\a` → `vool.root.b144b923…5d7e40`). The launcher puts in the token only the
  capabilities of THIS job's write roots, so a job can never write another job's workspace.
- **Network.** `internetClient` is added only when `network` is true.
- **No other writer.** Verify refuses (125) a write root on which any other AppContainer SID
  (`S-1-15-2-*` or `S-1-15-3-*`, including ALL APPLICATION PACKAGES and another root's capability)
  is allowed any write right, and a read root on which any of them is. It also refuses a NULL DACL
  anywhere it walks. Otherwise one job could write another's workspace or the toolchain.

### Teardown

The launcher holds the ONLY handle to the job:

- It never duplicates the job handle into the child.
- It never sets `JOB_OBJECT_LIMIT_BREAKAWAY_OK` or `SILENT_BREAKAWAY_OK`.
- It creates the child suspended, so there is no window in which the child runs outside the job.

So terminating the launcher (TerminateProcess, or `Popen.kill()`) closes the job and ends the whole
tree. The caller needs nothing else.

## 3. Policy file, schema `vool.windows_confinement.v1`

UTF-8 JSON. The Python side writes it, with the status file next to it, in a fresh folder **outside
every writable root**, and deletes both after the job.

```json
{
  "schema": "vool.windows_confinement.v1",
  "write_roots": ["C:\\work\\repo"],
  "read_roots": ["C:\\Python312", "C:\\work\\repo\\.venv"],
  "deny_write_paths": ["C:\\work\\repo\\vendor-code", "C:\\work\\repo\\.venv"],
  "deny_all_paths": ["C:\\Users\\me\\.vool_runtime", "C:\\Users\\me\\.ssh"],
  "deny_write_git": true,
  "network": false,
  "max_seconds": 120,
  "memory_mb": 512,
  "max_processes": 64,
  "temp_dir": "C:\\work\\repo\\.vool-tmp",
  "status_path": "C:\\Users\\me\\AppData\\Local\\Temp\\vool-confine-x1\\status.json"
}
```

Rules, all enforced by the launcher (126 if broken):

- **Paths.** One canonical spelling only, because every containment rule below is a string compare
  and Win32 rewrites some spellings before it opens them. Each path is `X:\` followed by
  backslash-separated segments, with no `\\?\`, UNC or device prefix and no `/`. No segment is
  empty, `.` or `..`, ends in a dot or a space, contains `~` (8.3 short names), `:` (streams),
  `<>"|?*` or a control character, or is a device name (`CON`, `NUL`, `COM1`, ... with or without an
  extension). Both sides check exactly this: `_check_path` in Python and `Policy.CheckPath` in C#.
  "Inside a root" is case-insensitive under either upper- or lower-casing, so a disagreement only
  ever refuses a status path.
- **Resolved on disk (chunk 1).** Before it creates the child, the launcher opens each write root and
  the status folder and compares `GetFinalPathNameByHandle` with the input, case-insensitively. Any
  difference (a junction, a symlink, a short name that slipped through) is 126.
- **Fresh status (chunk 3).** The caller creates a new, empty status folder per job and refuses to
  launch if a status file already exists there, so an earlier job's status can never be read as this one's.
- **`write_roots`.** Non-empty. Read and write for the container.
- **`read_roots`.** Read and execute only. A read root inside a write root is still writable through
  the write root's grant; to make it read-only (a workspace `.venv`), also list it in `deny_write_paths`.
- **`deny_write_paths`.** Explicitly denied write, beating any inherited grant (code roots).
- **`deny_all_paths`.** Denied read AND write, like the Mac's secret and state denies (VOOL runtime state, credentials).
- **`deny_write_git`.** When true, every `.git` directory under a write root is denied write.
  Nested `.git` directories created during the job are caught by the post-job scan on the Python side.
- **`network`.** Default false. Only true adds the `internetClient` capability. Loopback is never granted.
- **Limits.** `max_seconds`, `memory_mb` and `max_processes` are positive integers.
- **`temp_dir`.** Must sit inside a write root.
- **`status_path`.** Must sit OUTSIDE every write root. The caller creates its folder and deletes it after the job.
- **Unknown keys.** Rejected, so a newer caller never silently loses a rule.

## 4. Who owns which file (so parallel branches don't collide)

| Files | Owner (phased plan, jobs/broker/confinement-B/PLAN.md) |
|---|---|
| this doc, `sandbox/windows_confinement.py` dataclasses, schema, status and identity, `VoolConfine.Core/Policy.cs`, `Identity.cs`, the .sln | 🔐 Broker. Ask for changes; don't edit them in a chunk branch. |
| `VoolConfine/Program.cs`, `VoolConfine.Core/Launcher*.cs`, `Job*.cs`, `AppContainer*.cs` | Chunk 1, launcher |
| `VoolConfine.Core/Sids.cs` (package SID and capability SID derivation, one helper for chunks 1 and 2) | Chunk 1, launcher |
| `VoolConfine.Core/Acl*.cs`, `RepoScan*.cs` (grant/verify/revoke; the pre/post-job repository scan the launcher calls) | Chunk 2, ACL |
| bodies of `probe` / `confinement_prefix` in `sandbox/windows_confinement.py`, the Windows branch in `sandbox/job_runner.py` | Chunk 3, runner |
| `sandbox/windows_confinement_pin.py`, `windows/broker/publish.ps1`, the pin manifest | Chunk 4, shipping |
| new test files | each chunk adds its own; nobody edits an existing test |
