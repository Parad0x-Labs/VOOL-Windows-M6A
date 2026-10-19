# Windows broker: design (Step 2 of the Windows port)

Status: design, not built. Read against windows/v0.7.1-port `1c26068`. Every file:line below was
read at that commit. docs/WINDOWS_PORT_CONTRACTS.md (PR #21) is the source of truth for routes,
invariants, receipt states and gaps; this design redefines none of them, and section 11 lists the
changes it proposes to that doc.

The broker is a native Windows program (.NET) that gives VOOL two things Python on Windows does
not have today:

1. **File operations by handle.** Every write, create, rename and delete is done relative to open
   handles on an allowed root and on each folder below it, held for the whole operation, and
   checked on the objects actually opened, not on a path string. It is designed to close the
   check-then-write race the Windows write gate admits (core/windows_write_gate.py:3-5), and to
   refuse hard links into a flagged folder and links swapped in after a check. Whether it does is
   only known once the tests in section 9 pass on Windows.
2. **A confined child launcher.** A child (plugin, MCP server, process agent, document decoder,
   media worker) runs in a Job object with an AppContainer identity that cannot reach the user's
   profile or any folder not granted to it and, by default, has no TCP/UDP network. This is the
   Windows backend that core/plugin_executor.py:62-64, core/agent_team/coordinator.py:46-48 and
   docs/SANDBOX_PROCESS_CONFINEMENT.md (limit 6) say does not exist yet.

Step 2 builds and proves the broker on its own. Step 3 makes VOOL use it (the gate hands the broker
its approved roots, the writers call the broker, and the confinement seam gets a Windows backend),
together with approval ingress. Until Step 3 lands, nothing in VOOL calls the broker, and every
Windows refusal that exists today stays exactly as it is.

## 1. What the broker does not claim

- **It is not a privilege boundary against VOOL itself.** The broker runs as the same Windows
  user as VOOL's Python. A compromised VOOL process could skip the broker and write directly, or
  hand the broker any root. What the broker removes is the race and the name tricks between "the
  gate allowed this" and "this is what was written", and the chance for a confined child to
  reach anything it was not given. Separating VOOL's own identity from the user's is an open
  question (section 10).
- **It does not decide policy.** Which roots are allowed, whether an intent needs approval, and
  what the user sees stay in Python (core/windows_write_gate.py, the permission decision, A6).
  The broker cannot check what the gate decided; it enforces the roots it is handed, and only
  those that lie inside the flagged folders it opened itself at start (section 4).
- **It does not replace the gate before Step 3.** VOOL_WINDOWS_DISPOSABLE_WORKSPACES stays the
  only list of folders VOOL may change on Windows.

## 2. Processes and lifetime

- The VOOL API process starts `VoolBroker.exe` as its own child, inside a Job object the API holds
  with `KILL_ON_JOB_CLOSE`, so the broker cannot outlive the API. When the supervisor restarts the
  API, a new broker starts with a new pipe and secret; nothing carries over. Wiring this into
  installer/bundle/bundle_supervisor.py, installer/bundle/vool.iss and Start_VOOL.bat is phase B and
  waits for Job 7, which changes those files.
- Program files: `{app}\broker\` in the installed layout, `windows\broker\bin\` in a source
  checkout. The broker never writes in the program folder at run time.
- State: a journal (section 7.4) under VOOL_HOME. In the installed app VOOL_HOME is computed by
  installer/bundle/vool-launch.cmd:13-17 and passed on by bundle_supervisor.py:112
  (%LOCALAPPDATA%\VOOL); a source checkout sets it in Start_VOOL.bat:30-35. The journal is outside
  every folder a chat workspace can reach; the broker refuses to start if VOOL_HOME lies inside a
  flagged folder.
- If the broker exits, every child it launched dies with it (section 7.1), and the client fails
  closed (section 8).

## 3. Channel

- The API creates a 256-bit secret and starts the broker with it, and with the API's own process
  id and creation time, on the broker's stdin (never argv, never the environment, never a file).
- The broker creates `\\.\pipe\vool-broker-<random 128-bit hex>` with
  `FILE_FLAG_FIRST_PIPE_INSTANCE` for the first instance, `PIPE_REJECT_REMOTE_CLIENTS`, a fixed
  maximum instance count, and a DACL that grants access only to the current user's SID and
  explicitly denies `NT AUTHORITY\NETWORK` and every AppContainer (`ALL APPLICATION PACKAGES`), so
  a confined child cannot connect. It reports the pipe name back on its stdout.
- Each connection: the broker checks `GetNamedPipeClientProcessId` names a process whose id and
  creation time match the API's (so a reused process id fails), then a challenge: the client
  returns an HMAC of a broker nonce under the secret, and the broker answers with an HMAC of a
  client nonce, so the client also knows it reached its own broker. The client also checks
  `GetNamedPipeServerProcessId` is the broker it started. A failure disconnects and is logged.
- Messages are length-prefixed UTF-8 JSON, one request and one response, with a request id.
  Maximum 64 MiB; larger contents are refused (`too_large`) in Step 2.
- `vool-confine.exe` (section 7.5) never holds the secret. It presents a single-use launch ticket.

## 4. Roots

- **Flagged folders, opened at start.** The API passes the broker the flagged folders it read
  from VOOL_WINDOWS_DISPOSABLE_WORKSPACES at start, on the same stdin. The broker opens each one
  with `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, without `FILE_SHARE_DELETE`,
  refuses one that is itself a reparse point, and keeps those handles for its lifetime. Its
  identity is the volume serial number plus the 128-bit file id (`FileIdInfo`). A change to the
  variable needs a VOOL restart. Behaviour difference from the gate: the gate resolves a flagged
  folder that is a junction and accepts it (`flagged_roots`, windows_write_gate.py:51-58); the
  broker refuses it. That fails closed, and users with a junctioned folder will see it.
- **Roots for a call.** `open_root(flagged_index, rel)` walks `rel` from a flagged folder's handle
  (section 5 rules) and returns a `root_id`. So a root is always a flagged folder or a folder
  inside one, found by handle; an absolute path is never reopened. Which root Python hands for a
  call is the gate's job in Step 3: the folder the gate approved for that call, never wider. The
  broker never moves up to a parent, a repository's top folder, or a volume root on its own.
- Roots are compared by file id, never by spelling, so a junction, subst drive or 8.3 short name
  for the same folder is the same root.
- Step 2 accepts only NTFS volumes (`unsupported_filesystem` otherwise). On FAT and exFAT the POSIX
  rename and delete are refused and file ids are not stable across renames; ReFS and network
  shares are unknown. A flagged folder on a volume root is refused (`volume_root`).
- Minimum Windows: 10 version 1809 or later, to be pinned against the Windows SDK documentation
  for `FILE_RENAME_FLAG_POSIX_SEMANTICS` and `FILE_DISPOSITION_FLAG_POSIX_SEMANTICS` before
  phase A is built. The broker refuses to start on an older build.

## 5. The handle walk

Every operation names a `root_id` and a relative path. The path is split on `\` and `/`, and each
component is opened relative to the previous handle with `NtCreateFile` and
`OBJECT_ATTRIBUTES.RootDirectory` (Win32 `CreateFile` has no relative open), always with
`FILE_OPEN_REPARSE_POINT`, then checked with `FileAttributeTagInfo`. Every directory on the way,
and the root, is opened without `FILE_SHARE_DELETE` and held until the operation (or batch)
finishes, so none of them can be renamed or deleted out of the root meanwhile. The cost: while an
operation runs, the user cannot rename those folders. That this blocks a rename of an ancestor on
Windows is a claim the tests must prove (section 9).

| Rule | Refusal code |
|---|---|
| Component is empty, `.` or `..` | `path_escapes_root` |
| Component contains `:` (alternate data stream), `\0`, or a character Windows rejects | `invalid_name` |
| Component ends in `.` or space | `invalid_name` |
| Component is a DOS device name (CON, PRN, AUX, NUL, COM0-9, LPT0-9, CONIN$, CONOUT$, with any extension, any case, superscript digits included) | `device_name` |
| The opened object is a reparse point of any tag (junction, symlink, mount point, OneDrive placeholder, dedup, AppExecLink) | `reparse_point` |
| The opened object is a file with a link count above 1 (read or write) | `blocked_by_hard_link` |
| A directory on the way is case-sensitive (`FILE_CS_FLAG_CASE_SENSITIVE_DIR`) | `case_sensitive_dir` |
| The relative path is over 32,767 UTF-16 units | `path_too_long` |
| The object's volume differs from the root's | `path_escapes_root` |

Why each rule exists:
- Under a relative NT open, DOS device names, trailing dots and spaces, and 8.3 names are not
  escapes: `NtCreateFile` does not map `CON` to a device or strip a trailing dot, and an 8.3 name
  is only an alias that NTFS resolves to the same object. They are refused because Python, git,
  Explorer and other Win32 programs read those names differently, so a file VOOL created could not
  be handled safely by the rest of VOOL or by the user.
- Hard links: `write_file` replaces by rename (section 6), so it never changes a linked outside
  file in place; for writes the rule is parity with the Linux and macOS launcher
  (sandbox/job_runner.py `HardLinkedWritableRootError`). For reads it is protection: a link inside
  a flagged folder to an outside file would otherwise let `read` return the outside content. The
  count is read when the file is opened; a link added afterwards is not seen (creating a hard link
  needs no write access to the target for an unconfined program).
- Reparse points of every tag are refused in Step 2. Documents and Desktop are often
  OneDrive-backed, so this may refuse ordinary files there. The effect is unknown until tested on
  an OneDrive-backed folder; allowing specific tags is a later decision.
- Case-sensitive folders (WSL) allow two names that differ only in case, which the rest of VOOL
  treats as one.

After the walk, the broker reads the final object's path with `GetFinalPathNameByHandleW` and
`VOLUME_NAME_GUID` and confirms it is under the root's (drive letters and subst do not matter). It
is defence in depth: the held handles are the real check. If the call fails, the operation is
refused (`authority_check_failed`).

## 6. File operations

Operations (Step 2 set):

| Operation | Done as | For (from Step 3) |
|---|---|---|
| `stat(root, rel)` | walk, `FileBasicInfo` / `FileStandardInfo` | checks before a change |
| `read(root, rel, max_bytes)` | walk, open for read, read | diffs, rollback |
| `write_file(root, rel, bytes, mode, expect)` | see below | workspace.write_file, workspace.replace_in_file, machine.write_file |
| `ensure_directory(root, rel)` | create each missing component relative to its parent | workspace.ensure_directory, machine.ensure_directory |
| `rename(root, rel_from, root2, rel_to, replace)` | open source with `DELETE`, `FileRenameInfoEx` (`POSIX_SEMANTICS`) onto the destination parent handle | machine.move_path, operator.move_path |
| `delete(root, rel)` | open with `DELETE`, `FileDispositionInfoEx` (`POSIX_SEMANTICS`) | rollback |
| `apply_batch(root, ops[])` | see below | workspace.apply_unified_diff (section 6.1), workspace.rollback_last_change, code.task.rollback |

`write_file`:
1. Walk to the parent and hold it. If the destination exists, open it with `DELETE` access and
   share read only (no write, no delete sharing), so no one else can change it; check its link
   count and, when `expect` is a hash, hash it through this handle. A mismatch is refused
   (`changed_since_check`). `expect=absent` with an existing file is refused too.
2. Create a temp file in the parent (name recorded in the journal first, section 7.4), write,
   flush.
3. `SetFileInformationByHandle(FileRenameInfoEx, REPLACE_IF_EXISTS | POSIX_SEMANTICS)` with
   `RootDirectory` set to the parent handle. Whether a POSIX replace succeeds while the broker's
   own handle on the destination is open with these share modes must be proven on Windows; if it
   does not, `expect` is best effort and the design says so.

Moves across volumes are refused (`cross_volume`). Today `machine.move_path` copies across volumes
(core/machine_file_ops.py:203 `shutil.move`), so this is a behaviour change for Step 3 to state;
a copy-then-delete by handle, with its own partial-failure receipt, is later work.

`apply_batch` is all-or-nothing for refusals: the broker first walks every path, checks every rule
and every `expect`, and only then makes the first change. Only an I/O error after that point can
leave a batch part done; then the response lists exactly which ops completed, with each changed
object's file id and prior hash, and Python compensates (section 8).

Not covered by any broker file operation:
- `repo.git`: git does its own I/O, so it can only run in the launcher (section 6.1).
- `media.export`: written by the separate media worker; it can only be confined through the
  launcher (contracts gap 8).
- `operator.cleanup_temp_files`: stays refused on Windows (`_ALWAYS_REFUSED`,
  windows_write_gate.py:48).
- Operator notes: not gated today (contracts gap 13); the broker could serve them later.

### 6.1 Git and patches (target for Step 3)

This describes the contract Step 3 needs, not what the gate does today. At 1c26068 the gate's
`refusal()` returns allow or refuse and hands over no roots, and for `repo.git` and
`workspace.apply_unified_diff` it checks only the chat's workspace (`_GATED`:32, 35 are empty;
contracts gaps 14 and 15). Job 6 is changing that gate now. Step 3 then needs a gate function that
returns the exact roots it approved for a call, and the broker uses those and nothing wider:
- `repo.git` in the launcher: write roots are the top folder (`git rev-parse --show-toplevel`),
  the git dir (`--absolute-git-dir`) and the common dir (`--git-common-dir`; a linked worktree
  keeps its git dir and objects under the main repository's `.git`). Every one must be a root the
  gate approved. Whether git runs at all inside an AppContainer is unknown (Git for Windows reads
  `%USERPROFILE%\.gitconfig`, which an AppContainer cannot read, and needs its own folders under
  Program Files); a test must show it before Step 3 relies on it.
- `workspace.apply_unified_diff`: proposed, apply the patch through `apply_batch` (the broker's
  own engine) instead of git on Windows, so every path is walked by handle and nothing is left to
  a parser predicting what git will touch. Rename and copy headers are refused on Windows, as the
  code-task path already does (`_GIT_UNSUPPORTED_HEADERS`, core/execution/workspace_tools.py:538).
  If git is kept, its roots cannot be single files: git writes a new file next to the target and
  renames it, and a rename source needs delete rights in its folder, so each root is a parent
  folder, which is wider than the paths named. Choosing between these is Job 6's and Step 3's call.

## 7. Confined launcher

### 7.1 Job object and process creation

`launch(ticket)` (the policy comes from the ticket, section 7.5):
1. Create a Job object: `KILL_ON_JOB_CLOSE`, `DIE_ON_UNHANDLED_EXCEPTION`, an active-process
   limit, a job memory limit, a CPU time limit when given, UI restrictions (desktop, display
   settings, exit Windows, global atoms, handles of other processes' windows, clipboard read and
   write, system parameters). `BREAKAWAY_OK` and `SILENT_BREAKAWAY_OK` are never set.
2. Create the child with `CreateProcessW`, `EXTENDED_STARTUPINFO_PRESENT`,
   `PROC_THREAD_ATTRIBUTE_JOB_LIST` (the child starts inside the Job),
   `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` with only its stdin, stdout and stderr, and
   `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` (section 7.2).
3. Handle inheritance: every root and folder handle is created non-inheritable; the three stdio
   handles are made inheritable only inside a process-wide launch lock and closed in the broker
   right after `CreateProcessW`; the broker never calls .NET `Process.Start`.
4. Environment: an allow-list (as sandbox/job_runner.py `_child_environment`:522). TEMP and TMP
   point to the AppContainer profile's own `AC\Temp`, so scratch stays out of the user's folders.
5. Return a launch id and the child's stdio; `kill(launch_id)` closes the Job.

Limits: if VOOL itself runs inside a Job (a terminal, an IDE, the supervisor), the limits combine;
nested jobs are tested. A process created on the child's behalf by a service (WMI, Task Scheduler,
an out-of-process COM server) runs outside the Job; AppContainer blocks most of those routes.

### 7.2 Child identity

AppContainer, with a **new profile per launch** (`CreateAppContainerProfile`, deleted with
`DeleteAppContainerProfile` when the Job closes), so two children never share an identity and
cannot write each other's roots or open each other's processes and objects.
- Network: no `internetClient` / `privateNetworkClientServer` capability unless `network=true`,
  so outbound TCP/UDP is denied by the OS. Loopback is blocked for AppContainers even with those
  capabilities unless an admin sets a loopback exemption, so a `network=true` child cannot reach
  VOOL's API or Ollama on localhost. Whether isolation holds with Windows Firewall or its base
  filtering service off, or with another firewall product, is unknown. Named pipes and AF_UNIX
  sockets are governed by object permissions, not by these capabilities.
- Reads: an AppContainer can read what is granted to `ALL APPLICATION PACKAGES` (`C:\Windows`,
  usually `C:\Program Files`, parts of HKLM) and whatever it is granted. It cannot read the user's
  profile or a folder not granted. The tests enumerate what it can read.

### 7.3 Granting the child its folders (decision needed, section 10)

An AppContainer can write only where an ACE grants its SID access. How write and read roots get
that grant is the hardest open question. The options and their consequences:
- **A. Staged copy (proposed for plugins, decoders, MCP servers).** The child writes only in a
  folder inside its own profile; read roots are copied in, results are applied back by
  `apply_batch` and its rules. No ACL in the user's folders changes and hard links are never
  followed. Costs: copying time and disk, and it does not suit programs that must work in place.
- **B. Inheritable ACE on each root, propagated to existing files.** Needed for in-place work
  (process agents, git). Propagation rewrites the permissions of every file below each root on
  every launch and again on removal; a crash midway leaves a partly changed tree; and because hard
  links share one security descriptor, propagating through a hard link changes the outside file's
  permissions. So: refuse a root that holds any hard-linked file before granting (parity with
  `blocked_by_hard_link`, re-checked after), and refuse a root that contains reparse points.
  Whether propagation follows junctions or symlinks is unknown and must be tested. Read roots
  (interpreters, venvs, plugin folders under the user profile, `_program_read_roots`,
  core/plugin_executor.py:163) get grants too, so ACLs change outside the flagged folders.
- **C. Restricted token at Low integrity, write roots labelled Low.** Also changes every write
  root's label, does not deny network, and does not block the service routes in section 7.1.

### 7.4 Journal and recovery

- Every ACL change, profile creation and temp file is written to the journal (with the folder's
  file id, the name, and the previous security descriptor) and flushed **before** it is made.
- At start the broker undoes every journalled change that is still in place: removes the ACEs,
  deletes the profiles, and deletes only journalled temp files (by parent file id and exact name,
  never by name pattern).
- If the journal is missing or unreadable, the broker refuses to launch (`journal_unavailable`)
  and lists the AppContainer profiles it owns (by a fixed name prefix) for the user to clean up.
  It never guesses which ACEs to remove.
- Uninstall runs the same undo and deletes the broker's profiles.

### 7.5 Launch tickets and vool-confine.exe

Linux and macOS confine a child by wrapping its argv (sandbox/job_runner.py
`_kernel_network_isolation_prefix`:799, `_filesystem_only_isolation_prefix`:810). On Windows:
1. The API asks the broker for a ticket, naming the exact argv, working folder, root ids,
   network and limits. The broker checks the roots (section 4) and returns a random single-use
   ticket bound to that policy and argv, valid for a short time.
2. The API runs `vool-confine.exe <ticket> -- <argv>`. vool-confine connects to the pipe and
   presents only the ticket; the broker accepts it once, only from a process whose parent is the
   API (process id and creation time), and only if the argv matches. It then launches the child
   and relays stdio and the exit code.
3. vool-confine never holds the secret, so a leaked ticket allows at most that one launch, with
   that policy.
In Step 3 this becomes a third prefix in job_runner.py, so plugins, MCP servers and sandbox
commands keep one confinement implementation, and `_WINDOWS_REFUSES_BARE` is lifted only where the
prefix is present and probed.

## 8. Failures and VOOL receipts

Which existing state each outcome lands in (contracts §4 and §5); no new receipt state:

| Outcome | State |
|---|---|
| Refused by the broker (any section 5 rule, unknown root, bad ticket) | refused: `windows_filesystem_authority_unavailable` with the broker's code, except `blocked_by_hard_link`, which keeps its own status (sandbox/sandbox_runner.py:209); launch refusals `confinement_unavailable` (core/plugin_executor.py:384) |
| Broker absent, or the channel fails before the request is written | refused, same statuses |
| The request was written and no response came (broker died, pipe broke, timeout) | outcome_unknown (A6 `unknown`): the change may have happened |
| A launched child ran and the broker died | outcome_unknown |
| `apply_batch` part done after an I/O error | `ok=False` with the completed ops listed; Python restores them from the prior hashes and reports what it restored, with the same restore reporting the protected-write path uses. Contracts gap 6 stays open for this path: if restoring fails, A6 still classifies it as it does today |
| Executed | `executed`, with file ids and new hashes |

## 9. Code layout and tests

- `windows/broker/VoolBroker.sln`: `VoolBroker` (exe), `VoolBroker.Core` (walk, file operations,
  launcher, journal, protocol), `VoolConfine` (exe), `VoolBroker.Tests` (xunit).
- .NET 10 LTS, x64. On Steve the SDK is in VOOL Windows\toolchain\dotnet, called by full path.
- `core/windows_broker/client.py`: the Python client. Nothing imports it before Step 3.
- Tests on real Windows (Steve, then the hosted Windows runner once the workflow OK is given),
  each against the real broker, never mocks:
  - escapes: `..`; a junction and a symlink inside a root pointing out; one swapped in between
    `stat` and `write_file` by a racing thread; renaming an ancestor folder (and a flagged folder)
    out of the root while an operation holds it; a hard link to an outside file (write and read);
    an 8.3 name and a subst drive for a root; every DOS device name; an alternate data stream;
    trailing dot and space; a case-sensitive folder; a path over 32,767 units; a second volume;
    a FAT32 volume (refused); an OneDrive-backed folder (record what happens);
  - every operation's success path, and that each refusal changed nothing (hash the tree before
    and after); a batch with a late refusal changes nothing; `expect` against a racing writer;
  - launcher: the child cannot write outside its roots, cannot read the user profile or an
    ungranted folder, cannot reach the network or loopback when `network=false`, cannot connect
    to the broker's pipe, inherits no other handle, two children cannot reach each other, the
    child dies when the broker is killed, under an outer Job too; git runs (or the test records
    that it does not);
  - journal: ACEs, profiles and temp files are undone after the Job closes, after a broker crash
    at each step, and when the journal is missing (refused, nothing guessed);
  - channel: a client without the secret, a client from another process, a reused process id, a
    second pipe instance, a ticket used twice, a ticket with a different argv, an oversized
    message.
- Linux CI: the Python client's protocol tests against a fake server, and a test that nothing
  outside tests imports `core.windows_broker` before Step 3.

## 10. Open questions (the owner's decisions; nothing is built for them until made)

1. How confined children get their folders: staged copy (A), propagated ACEs (B), Low integrity
   (C), or A for plugins, decoders and MCP servers with B only for in-place work (proposed).
2. Whether a later step should run VOOL's Python under a separate identity so the broker becomes
   a real privilege boundary.
3. Self-contained publish (about 70 MB, proposed: users need no .NET runtime) or NativeAOT
   (smaller, but needs the C++ build tools on Steve, another install).
4. Building and testing the broker in CI needs a workflow change, which needs the owner's named
   OK.
5. User-visible behaviour changes Step 3 would bring, each needing the owner's OK before Step 3
   ships: moves across volumes are refused (today they copy); only NTFS volumes are accepted; a
   flagged folder that is itself a junction is refused (the gate accepts it today); reparse
   points of every tag, OneDrive placeholders included, are refused (effect on OneDrive-backed
   folders unknown until tested); hard-linked files are refused for reads as well as writes;
   folders an operation holds cannot be renamed by the user while it runs; Windows older than
   the pinned minimum build is not supported.

## 11. Proposed changes to the contracts doc

To be raised as a follow-up to that doc after PR #21 merges, not inside it:
1. Its gap-to-step map assigns gap 5 (the wallet UI capability hash is never set) to Step 2. This
   design does not address gap 5. Proposed: move it to Step 3's approval ingress, or mark it
   unassigned.
2. Gap 8 (the media worker and the browser engine start unconfined) is unassigned. Proposed: plan
   it for Step 3, through the launcher in section 7.
3. Gaps 14 and 15 are unassigned in the map; Job 6 is fixing their gate side. Proposed: record
   Job 6 as their owner, and Step 3 for the launcher's use of the roots the gate returns.
4. DOS device names and paths over 32,767 characters are recorded as known residuals of gap 11;
   hard links inside a flagged folder are not recorded. Proposed: reference gap 11 for the first
   two and record the hard-link case as a new gap, all planned for Step 3. They count as closed
   only once Step 3 is proven on Windows.
