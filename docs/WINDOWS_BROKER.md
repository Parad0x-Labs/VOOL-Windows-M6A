# Windows broker: design (Step 2 of the Windows port)

Status: design, not built. Read against windows/v0.7.1-port `1c26068`. Every file:line below was
read at that commit.

The broker is a native Windows program (.NET) that gives VOOL two things Python on Windows does
not have today:

1. **File operations by handle.** Every write, create, rename and delete is done relative to an
   open handle on an allowed root, and checked on the object actually opened, not on a path
   string. This closes the check-then-write race the Windows write gate admits
   (core/windows_write_gate.py:3-5) and the cases the gate cannot judge by text: hard links into
   a flagged folder, DOS device names, junctions and symlinks swapped in after the check, 8.3
   short names, and paths over 32,767 characters.
2. **A confined child launcher.** A child (plugin, MCP server, process agent, document decoder,
   media worker) runs in a Job object with a restricted identity that can write only to the
   roots it was given and, by default, has no network. This is the Windows backend that
   core/plugin_executor.py:62-64, core/agent_team/coordinator.py:46-48 and
   docs/SANDBOX_PROCESS_CONFINEMENT.md (limit 6) say does not exist yet.

Step 2 builds and proves the broker on its own. Step 3 makes VOOL use it (the write gate and the
writers call the broker; the confinement seam gets a Windows backend), together with approval
ingress. Until Step 3 lands, nothing in VOOL calls the broker, and every Windows refusal that
exists today stays exactly as it is.

## 1. What the broker does not claim

- **It is not a privilege boundary against VOOL itself.** The broker runs as the same Windows
  user as VOOL's Python. A compromised VOOL process could skip the broker and write directly.
  What the broker removes is the race and the name tricks between "the gate allowed this" and
  "this is what was written". Separating VOOL's own identity from the user's is out of scope for
  Step 2 and is recorded as an open question (section 9).
- **It does not decide policy.** Which roots are allowed, whether an intent needs approval, and
  what the user sees stay in Python (core/windows_write_gate.py, the permission decision, A6).
  The broker enforces the roots it is handed and refuses anything else.
- **It does not replace the gate before Step 3.** VOOL_WINDOWS_DISPOSABLE_WORKSPACES stays the
  only list of folders VOOL may change on Windows.

## 2. Processes and lifetime

- `VoolBroker.exe` is started by the supervisor (installed layout) or the launcher (source
  checkout), next to the API process, and stopped with it. One broker per VOOL instance.
  Wiring the start into installer/bundle/bundle_supervisor.py, installer/bundle/vool.iss and
  Start_VOOL.bat is phase B and waits for Job 7, which changes those files.
- The broker lives in the program folder (`{app}\broker\`) for the installed layout and in
  `windows\broker\bin\` for a source checkout. It never writes in the program folder at run time.
- If the broker exits, every child it launched dies with it (Job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), and VOOL's client returns a typed refusal for every
  call until a broker is back. Unknown state fails closed.

## 3. Channel

- One named pipe per broker: `\\.\pipe\vool-broker-<random 128-bit hex>`, created with
  `FILE_FLAG_FIRST_PIPE_INSTANCE` (no squatting), `PIPE_REJECT_REMOTE_CLIENTS`, and a DACL that
  grants access only to the current user's SID (no Everyone, no Authenticated Users, no
  AppContainer SIDs, so a confined child cannot connect).
- The parent that starts the broker passes the pipe name and a 256-bit session secret on the
  broker's stdin (never argv, never the environment, never a file). The client proves it holds
  the secret in its first message (HMAC over a broker nonce). A client that fails it is
  disconnected and the attempt is logged.
- The broker also checks `GetNamedPipeClientProcessId` against the API process id it was told
  at start. A mismatch is refused.
- Messages are length-prefixed UTF-8 JSON, one request, one response, with a request id.
  Maximum message 64 MiB; larger file contents are refused (`too_large`), not streamed, in
  Step 2.

## 4. Roots

- `open_root(path)` opens a directory handle with `FILE_FLAG_BACKUP_SEMANTICS |
  FILE_FLAG_OPEN_REPARSE_POINT`, refuses it if it is itself a reparse point, and records its
  identity: volume serial number plus 128-bit file id (`GetFileInformationByHandleEx`
  `FileIdInfo`) and its final path (`GetFinalPathNameByHandleW`). It returns a `root_id`.
- A root is accepted only if the request lists it and, from Step 3 on, only if Python's gate
  approved it from VOOL_WINDOWS_DISPOSABLE_WORKSPACES. In Step 2 the tests hand roots directly.
- The broker enforces exactly the root it was handed and never widens it: it never moves up to
  a parent, a repository's top folder, or a volume root on its own. A request the gate refused
  never reaches the broker; one that would need a wider root than the gate approved is refused
  (`path_escapes_root`).
- Root identity is compared by file id, never by spelling. This removes the "one spelling of
  the root" problem (junction, subst drive, 8.3 short name) noted in the Job 4 follow-ups.

## 5. File operations

Every operation names a `root_id` and a relative path. The relative path is split on `\` and
`/`, and each component is opened relative to the previous handle with `NtCreateFile` and
`OBJECT_ATTRIBUTES.RootDirectory` (Win32 `CreateFile` has no relative open). Rules, checked on
every component:

| Rule | Refusal code |
|---|---|
| Component is empty, `.` or `..` | `path_escapes_root` |
| Component contains `:` (alternate data stream), `\0`, or a character Windows rejects | `invalid_name` |
| Component ends in `.` or space (Win32 would strip it, so two names meet one object) | `invalid_name` |
| Component is a DOS device name (CON, PRN, AUX, NUL, COM0-9, LPT0-9, CONIN$, CONOUT$, with any extension, any case, superscript digits included) | `device_name` |
| The opened object is a reparse point (junction, symlink, mount point, any tag) | `reparse_point` |
| The opened object is a file whose link count is greater than 1 and the operation would change it | `blocked_by_hard_link` |
| Total relative path over 32,767 UTF-16 units | `path_too_long` |
| The final object's volume differs from the root's | `path_escapes_root` |

After the last component is opened, the broker reads its final path by handle and confirms it
is under the root's final path (defence in depth; the handle walk is the real check).

Operations (Step 2 set, chosen to cover every intent in `_GATED`, core/windows_write_gate.py:28-42):

| Operation | Done as | Covers |
|---|---|---|
| `stat(root, rel)` | open, `FileBasicInfo` / `FileStandardInfo` | checks before a change |
| `read(root, rel, max_bytes)` | open for read, read | diffs, rollback |
| `write_file(root, rel, bytes, mode, expect)` | create a temp file in the same parent handle, write, flush, then `SetFileInformationByHandle(FileRenameInfoEx, FILE_RENAME_FLAG_REPLACE_IF_EXISTS | POSIX_SEMANTICS)` with `RootDirectory` = parent handle. `mode` is `create` (fails if present) or `replace`; `expect` is the SHA-256 of the current content or `absent`, and a mismatch is refused (`changed_since_check`) | workspace.write_file, replace_in_file, machine.write_file, notes |
| `ensure_directory(root, rel)` | create each missing component relative to its parent | workspace/machine.ensure_directory |
| `rename(root, rel_from, root2, rel_to, replace)` | open source, `FileRenameInfoEx` onto the destination parent handle; both roots must be open | machine.move_path, operator.move_path, apply_unified_diff renames |
| `delete(root, rel)` | open with `DELETE`, `FileDispositionInfoEx` (`POSIX_SEMANTICS`) | rollback, notes restore |
| `apply_batch(root, ops[])` | each op as above, in order; on the first refusal the broker stops and reports exactly which ops ran | apply_unified_diff, rollback_last_change |

Every response says what happened: `executed` with the object's file id and new SHA-256, or a
refusal code with nothing changed, or for a batch the list of completed ops. A refusal never
leaves a temp file behind; a temp file left by a crash has a fixed prefix and is removed by the
broker at the next `open_root`.

Git (`repo.git`, `workspace.apply_unified_diff` through git) cannot be moved inside the broker:
git does its own file I/O. Those run in the launcher (section 6) with the root the gate approved
as the only write root. The broker does not choose that root: if the gate refuses a git call
(for example, only a subfolder of a repository is flagged), nothing reaches the broker, and the
broker never turns a subfolder into the repository's top folder.

## 6. Confined launcher

`launch(argv, cwd, write_roots[], read_roots[], network, limits)`:

1. Create a Job object: `KILL_ON_JOB_CLOSE`, `DIE_ON_UNHANDLED_EXCEPTION`, an active-process
   limit, a job memory limit, a CPU time limit when given, and UI restrictions (no desktop
   switching, no clipboard, no global atoms, no `ExitWindows`).
2. Create the child with `CreateProcessW`, `CREATE_SUSPENDED`, `EXTENDED_STARTUPINFO_PRESENT`,
   `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` limited to the child's own stdin/stdout/stderr pipes, and
   the identity in section 6.1. Assign it to the Job, then resume. A child that cannot be
   assigned is terminated before it runs.
3. Environment: an allow-list (same idea as sandbox/job_runner.py `_child_environment`), with
   TEMP/TMP pointing to a per-launch scratch folder inside a write root.
4. Return the process id, a launch id, and stdout/stderr; `kill(launch_id)` closes the Job.

### 6.1 Child identity (decision needed, section 9)

Proposed default, for the owner to decide (section 9): **AppContainer** (`CreateAppContainerProfile`, a per-VOOL-install profile,
`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`).
- No `internetClient` / `privateNetworkClientServer` capability unless `network=true`, so
  network is denied by the OS by default.
- An AppContainer can read only what grants it access (system folders, its own profile) and
  write nowhere except its own profile folder. Each write root and read root is opened for it
  by adding an ACE for the AppContainer SID to that folder, which is a change to the folder's
  ACL. The broker adds it at launch and removes it when the Job closes, records each change in
  a journal under VOOL_HOME (in the installed app the supervisor already sets VOOL_HOME to
  %LOCALAPPDATA%\VOOL), outside every folder a chat workspace can reach, and on start removes
  any ACE a crash left behind.
- Python interpreters, decoders and plugins need their program folders readable: those are
  added as read roots (same as `_program_read_roots` in core/plugin_executor.py).

Alternative: a restricted token at Low integrity, with write roots labelled Low. Simpler for
reads, but it also changes every write root's label, and Low integrity does not deny network.

### 6.2 Fit with the existing seam

Linux and macOS confine a child by wrapping its argv (sandbox/job_runner.py
`_kernel_network_isolation_prefix`:799, `_filesystem_only_isolation_prefix`:810). The broker
ships a small `vool-confine.exe` that takes the same policy on its command line and asks the
broker to launch the rest of the argv, relaying stdio and the exit code. In Step 3 that becomes
a third prefix in job_runner.py, so plugins, MCP servers and sandbox commands keep sharing one
confinement implementation and `_WINDOWS_REFUSES_BARE` can be lifted only where the prefix is
present and probed.

## 7. Refusal codes and VOOL receipts

The broker's codes map to statuses VOOL already reports:
- every file refusal -> `windows_filesystem_authority_unavailable` (core/windows_write_gate.py:23)
  with the broker's code as the reason, except `blocked_by_hard_link`, which keeps its own status
  (sandbox/sandbox_runner.py:209);
- broker absent, channel failure, unknown response -> `windows_filesystem_authority_unavailable`
  for files and `confinement_unavailable` for launches (fail closed);
- launch failures -> `confinement_unavailable` (core/plugin_executor.py:384).
No new receipt state is added in Step 2.

## 8. Code layout and tests

- `windows/broker/VoolBroker.sln`: `VoolBroker` (exe), `VoolBroker.Core` (handle walk, file
  ops, launcher, protocol), `VoolConfine` (exe), `VoolBroker.Tests` (xunit).
- Target .NET 10 LTS, x64. Proposed: published self-contained so users need no .NET runtime (owner decides, section 9).
- `core/windows_broker/client.py`: the Python client. Nothing imports it before Step 3.
- Tests that must run on real Windows (Steve, then the hosted Windows runner once the workflow
  OK is given), each against the real broker, never mocks:
  - escape attempts: `..`, a junction and a symlink inside a root pointing out, one swapped in
    between `stat` and `write_file` (a racing thread), a hard link to an outside file, an 8.3
    short name and a subst drive for the root, every DOS device name, an alternate data stream,
    trailing dot/space, a path over 32,767 units, a second volume;
  - every operation's success path and that each refusal changed nothing (hash the tree before
    and after);
  - launcher: the child cannot write outside its write roots, cannot read a folder outside its
    read roots, cannot reach the network when `network=false`, cannot connect to the broker's
    pipe, inherits no other handle, and dies when the broker is killed; ACEs are removed after
    the Job closes and after a broker crash;
  - channel: a client without the secret, a client from another process id, a second pipe
    instance, an oversized message.
- Linux CI: the Python client's protocol tests against a fake server, and a test that nothing
  outside tests imports `core.windows_broker` before Step 3.

## 9. Open questions

These are the owner's decisions. Nothing is built for any of them until they are made.

1. AppContainer with temporary ACEs on write roots (default) or Low integrity with labels.
   Either one changes the ACLs of the user's flagged folders while a child runs.
2. Whether a later step should run VOOL's Python under a separate identity so the broker becomes
   a real privilege boundary.
3. Self-contained publish (about 70 MB) or NativeAOT (smaller, but needs the C++ build tools on
   Steve, another install). Default: self-contained.
4. Building and testing the broker in CI needs a workflow change, which needs the owner's
   named OK.

## 10. Relation to the contracts doc

docs/WINDOWS_PORT_CONTRACTS.md (PR #21) is the source of truth for routes, invariants, receipt
states and gaps. This design redefines none of them. Proposed changes to it, to be raised as a
follow-up to that doc after PR #21 merges, not inside it:

1. Its gap-to-step map assigns gap 5 (the wallet UI capability hash is never set) to Step 2.
   This design does not address gap 5. Proposed: move gap 5 to the step that owns the wallet
   doors, or say what the broker would have to do for it.
2. Gap 8 (the media worker and the browser engine start unconfined) is currently unassigned.
   Proposed: assign it to Step 3, which would use the launcher in section 6.
3. The Job 3 follow-up (DOS device names and over-long paths judged by text) and the Job 5
   follow-up (hard links inside a flagged folder) are not numbered gaps. Proposed: record them
   as gaps closed by Step 3 through section 5.
