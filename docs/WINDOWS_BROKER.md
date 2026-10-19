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

Phases. A (Step 2): the broker and its Python client, built and proven on their own; nothing in
VOOL calls them, and every Windows refusal that exists today stays exactly as it is. B: shipping
and starting the broker, after Job 7 merges. C (Step 3): VOOL uses the broker (the gate hands it
its approved roots, the writers call it, the confinement seam gets a Windows backend), together
with approval ingress; it starts only after **both Job 6 and Job 7 have merged**.

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
  installer/bundle/bundle_supervisor.py, installer/bundle/vool.iss and Start_VOOL.bat is phase B.
- Program files: `{app}\broker\` in the installed layout, `windows\broker\bin\` in a source
  checkout. The broker never writes in the program folder at run time.
- State: the journal (section 7.4) in `VOOL_HOME\broker\journal\`, a folder of its own, separate
  from the workspace and notes that Job 7 places under VOOL_HOME. In the installed app VOOL_HOME is
  computed by installer/bundle/vool-launch.cmd:13-17 and passed on by bundle_supervisor.py:112
  (%LOCALAPPDATA%\VOOL); a source checkout sets it in Start_VOOL.bat:30-37 (by default
  `.vool_runtime` next to the checkout). The broker refuses to start if the journal folder lies
  inside a flagged folder, so a developer who flags the folder holding a source checkout must set
  VOOL_HOME elsewhere (section 10, item 5).
- If the broker exits, every child it launched dies with it (section 7.1), and the client fails
  closed (section 8).

## 3. Channel

- The API creates a 256-bit secret and starts the broker with it, the API's own process id and
  creation time, and the flagged folders (section 4), on the broker's stdin (never argv, never the
  environment, never a file).
- The broker creates `\\.\pipe\vool-broker-<random 128-bit hex>` with
  `FILE_FLAG_FIRST_PIPE_INSTANCE` for the first instance, `PIPE_REJECT_REMOTE_CLIENTS`, at most 16
  instances, and a DACL that grants access only to the current user's SID. That DACL is what keeps
  AppContainer children and other users out; an explicit deny for `NT AUTHORITY\NETWORK` is added
  as a second layer. It reports the pipe name back on its stdout.
- Each control connection: the broker checks `GetNamedPipeClientProcessId` names a process whose
  id and creation time match the API's (so a reused process id fails), then a challenge: the
  client returns an HMAC of a broker nonce under the secret, and the broker answers with an HMAC of
  a client nonce, so the client also knows it reached its own broker. The client also checks
  `GetNamedPipeServerProcessId` is the broker it started. A failure disconnects and is logged.
- Control messages are length-prefixed UTF-8 JSON, one request and one response, with a request
  id. Maximum 64 MiB; larger contents are refused (`too_large`) in Step 2.
- A launched child's stdio does not use the control channel (section 7.5).

## 4. Roots

- **Flagged folders, opened at start.** The API passes the flagged folders it read from
  VOOL_WINDOWS_DISPOSABLE_WORKSPACES. The broker opens each one with
  `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, without `FILE_SHARE_DELETE`, and
  keeps those handles for its lifetime. It refuses an entry that is a reparse point, a volume root,
  or not on NTFS; **any refused entry stops the broker from starting**, with the reason, so the
  user sees it rather than VOOL quietly using fewer folders. A change to the variable needs a VOOL
  restart.
- **One list for gate and broker.** At start the broker returns, for each flagged folder, an id
  (from the volume serial number and the 128-bit file id, `FileIdInfo`) and its current final path.
  From Step 3 the Windows gate uses that list as its only list of flagged folders, instead of
  re-reading the variable per call (`flagged_roots`, windows_write_gate.py:51-58). Before handing
  a call to the broker, Python asks for the folder's current final path and refuses on a mismatch,
  so the path the gate checked and the approval ask showed is the one the broker writes in.
- **Roots for a call.** `open_root(flagged_id, rel)` walks `rel` from that folder's handle
  (section 5 rules) and returns a `root_id`. A root is always a flagged folder or a folder inside
  one, found by handle; an absolute path is never reopened. Which root Python hands for a call is
  the gate's job in Step 3: the folder the gate approved for that call, never wider. The broker
  never moves up to a parent, a repository's top folder, or a volume root on its own.
- Roots are compared by file id, never by spelling, so a junction, subst drive or 8.3 short name
  for the same folder is the same root. Behaviour difference from the gate: the gate resolves a
  flagged folder that is a junction and accepts it (by design, windows_write_gate.py:57); the
  broker refuses it (section 10, item 5).
- Holding the flagged folders without `FILE_SHARE_DELETE` means the user cannot rename or delete
  a flagged folder while VOOL runs, and, if the NTFS behaviour the tests are to prove holds, not
  its parent folders either (Documents or a development folder). Section 10, item 5.
- Step 2 accepts only NTFS volumes (`unsupported_filesystem` otherwise). On FAT and exFAT the POSIX
  rename and delete are refused and file ids are not stable across renames; ReFS and network
  shares are unknown.
- Minimum Windows: 10 version 1809 or later, to be pinned against the Windows SDK documentation
  for `FILE_RENAME_FLAG_POSIX_SEMANTICS` and `FILE_DISPOSITION_FLAG_POSIX_SEMANTICS` before
  phase A is built. The broker refuses to start on an older build.

## 5. The handle walk

Every operation names a `root_id` and a relative path. The path is split on `\` and `/`, and each
component is opened relative to the previous handle with `NtCreateFile` and
`OBJECT_ATTRIBUTES.RootDirectory` (Win32 `CreateFile` has no relative open), always with
`FILE_OPEN_REPARSE_POINT`, then checked with `FileAttributeTagInfo`. Every directory on the way,
and the root, is opened without `FILE_SHARE_DELETE` and held until the operation (or batch)
finishes, so none of them can be renamed or deleted out of the root meanwhile. While an operation
holds them, the user cannot rename those folders. That this blocks a rename of an ancestor on
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
| The object is held by another program (sharing violation) | `in_use` |

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
  needs no write access to the target for an unconfined program). A repository cloned from a
  local path hard-links its object files by default, so this rule may refuse ordinary
  repositories; how often is unknown, and it is tested (section 9) and listed in section 10.
  Telling a harmless link (every name inside a flagged folder) from a harmful one is not cheaply
  checkable, so Step 2 refuses both.
- Reparse points of every tag are refused in Step 2. Documents and Desktop are often
  OneDrive-backed, so this may refuse ordinary files there. The effect is unknown until tested on
  an OneDrive-backed folder; allowing specific tags is a later decision.
- Case-sensitive folders (WSL) allow two names that differ only in case, which the rest of VOOL
  treats as one.

After the walk, the broker reads the final object's path with `GetFinalPathNameByHandleW` and
`VOLUME_NAME_GUID` and confirms it is under the root's (drive letters and subst do not matter). It
is defence in depth: the held handles are the real check. If the call fails, the operation is
refused (`authority_check_failed`).

Concurrency: the broker runs operations on one root one at a time, and an operation reuses the
handles its own walk opened (a delete or rename of a folder the same batch walked is done through
that handle, not a second open). A sharing violation with another program maps to `in_use`, with
nothing changed.

## 6. File operations

Operations (Step 2 set):

| Operation | Done as | For (from Step 3) |
|---|---|---|
| `stat(root, rel)` | walk, `FileBasicInfo` / `FileStandardInfo` | checks before a change |
| `read(root, rel, max_bytes)` | walk, open for read, read | diffs, rollback |
| `write_file(root, rel, bytes, mode, expect)` | see below | workspace.write_file, workspace.replace_in_file, machine.write_file |
| `ensure_directory(root, rel)` | create each missing component relative to its parent | workspace.ensure_directory, machine.ensure_directory |
| `rename(root, rel_from, root2, rel_to, replace)` | open source with `DELETE`, `FileRenameInfoEx` (`POSIX_SEMANTICS`) onto the destination parent handle | machine.move_path, operator.move_path |
| `delete(root, rel)` | open with `DELETE`, rename aside (journalled) | rollback |
| `apply_batch(root, ops[])` | see below | workspace.apply_unified_diff (section 6.1), workspace.rollback_last_change, code.task.rollback |
| `restore_batch(batch_id)` | see below | undoing a part-done batch |

`write_file`:
1. Walk to the parent and hold it. If the destination exists, open it with `DELETE` access and
   share read only (no write, no delete sharing), so no one else can change it; check its link
   count and, when `expect` is a hash, hash it through this handle. A mismatch is refused
   (`changed_since_check`). A read-only destination is refused (`read_only`).
2. Create a temp file in the parent (name recorded in the journal first, section 7.4), write,
   flush.
3. `SetFileInformationByHandle(FileRenameInfoEx, POSIX_SEMANTICS)` with `RootDirectory` set to
   the parent handle: with `expect=absent`, without `REPLACE_IF_EXISTS`, so a file created
   meanwhile is refused (`changed_since_check`) rather than overwritten; otherwise with
   `REPLACE_IF_EXISTS`. Whether the replace succeeds while the broker's own handle on the
   destination is open with these share modes must be proven on Windows; the tests also try the
   alternative of holding the destination with delete sharing but no write sharing. If neither
   works, `expect` is best effort and the design says so.
4. Replacing by rename gives the new file its folder's inherited permissions: an explicit ACL,
   alternate data streams and attributes of the old file are not kept. That matches VOOL's
   Python writers, which also replace by rename.

Moves across volumes are refused (`cross_volume`). Today `machine.move_path` copies across volumes
(core/machine_file_ops.py:203 `shutil.move`), so this is a behaviour change (section 10, item 5);
a copy-then-delete by handle, with its own partial-failure receipt, is later work.

`apply_batch`:
- All-or-nothing for refusals: the broker first walks every path, checks every rule and every
  `expect`, and only then makes the first change.
- It keeps before-images itself: an overwritten or deleted file is renamed aside (to a journalled
  name in the same folder) instead of being replaced or deleted, and a rename records the original
  name. The batch's state (started, each op done, finished) is journalled.
- If an I/O error stops a batch part way, the broker restores it itself, by handle, from the aside
  files (`restore_batch`; Python never writes back by path), and the response says which ops ran,
  whether the restore was complete, and what is left.
- When a batch finishes, the broker deletes its aside files. A crash before that is resolved at the
  next start from the journal: a finished batch's asides are deleted, an unfinished batch is
  restored. If either fails, the journal keeps the entry and the files, and the broker reports it;
  this is the "committed but cleanup failed" case of contracts gap 6, which stays open.

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
contracts gaps 14 and 15). Job 6 plans (not merged at the time of writing) to make its resolver a
public function in core/windows_write_gate.py, called by `refusal()`, that returns the folders a
call writes in, refusing any call it cannot place. Step 3 takes the broker's roots from that same
function, with no second API, passed as (flagged id, relative path) per section 4, and the broker
uses those and nothing wider:
- `repo.git` in the launcher: the write roots are the top folder (`git rev-parse
  --show-toplevel`), the git dir (`--absolute-git-dir`) and the common dir (`--git-common-dir`; a
  linked worktree keeps its git dir and objects under the main repository's `.git`), as git itself
  resolves them, never inferred from where `.git` sits. Every folder that function returns must be
  one the gate approved; Job 6 may also refuse a repository whose work tree lies outside the
  workspace. The reason: a repository's own config (`core.worktree`) can move git's top to any
  folder that contains the workspace even when `.git` is inside the flagged folder, and in Auto
  mode VOOL can create such a `.git` with no ask; a rename patch then moved a file from
  ~/Documents into the flagged folder. This was reported by the seventh review of PR #21 and re-run
  by Job 6; it is not yet in the contracts doc (section 11, item 5).
- The resolution happens at check time and the launch later, and the config can change between
  them. So the enforcement is the launcher's grants: a git run with a changed config fails to
  write outside the granted roots instead of following it. `rev-parse` runs only after
  `safe_git.executable_local_config` (core/safe_git.py:89) has passed (contracts gap 9 leaves it
  unverified whether `rev-parse` can run a configured program), with every `GIT_*` variable
  removed from its environment and from the confined git's. `.git/config` lies inside a write
  root, so a confined git or hook can rewrite `core.worktree` for the next call; each call
  resolves and checks afresh and trusts nothing a previous run left.
- Whether git runs at all inside an AppContainer is unknown (Git for Windows reads
  `%USERPROFILE%\.gitconfig`, which an AppContainer cannot read, and needs its own folders under
  Program Files); a test must show it before Step 3 relies on it.
- `workspace.apply_unified_diff`: proposed, apply the patch through `apply_batch` (the broker's
  own engine) instead of git on Windows. Then every path is walked by handle by the broker itself,
  and nothing depends on a parser predicting what git will touch. Rename and copy headers are
  refused on Windows, as the code-task path already does (`_GIT_UNSUPPORTED_HEADERS`,
  core/execution/workspace_tools.py:538). If git is kept, its roots are the folders the resolver
  places (no parser of patch text can promise every path git touches, contracts gap 15), and they
  cannot be single files: git writes a new file next to the target and renames it, and a rename
  source needs delete rights in its folder, so each root is a parent folder, which is wider than
  the paths named. Choosing between these is Job 6's and Step 3's call.

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
4. Environment: exactly the environment bound in the ticket (section 7.5), with the broker
   removing `GIT_*` and other variables that redirect a program's configuration. TEMP and TMP point
   to the AppContainer profile's own `AC\Temp`, so scratch stays out of the user's folders.

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
- **B. An ACE for the launch's SID on each root and every object below it.** Needed for in-place
  work (process agents, git). Grants are applied per object by handle: `SetSecurityInfo` on each
  object opened by the section 5 walk, with the same rules, so the scan and the grant are one pass
  and a junction swapped in cannot redirect it (never `SetNamedSecurityInfo` or
  `TreeSetNamedSecurityInfo`, which work by path). Each object's previous security descriptor is
  journalled first. A root holding a hard-linked file or a reparse point is refused before any
  grant; if one is found during the pass (a link added meanwhile), the pass stops and every grant
  made so far is undone from the journal, including on the linked file. This rewrites the
  permissions of every object below each root on every launch and again on removal. Whether
  hard-linked git objects make it refuse ordinary repositories is unknown (section 5).
- **C. Restricted token at Low integrity, write roots labelled Low.** Also changes every write
  root's label, does not deny network, and does not block the service routes in section 7.1.

Read roots outside the flagged folders (Python, the VOOL program folder, plugin folders,
`_program_read_roots`, core/plugin_executor.py:163): a fixed list built at broker start, each
opened by handle with the section 5 rules and refused if it is a reparse point. Folders already
readable by `ALL APPLICATION PACKAGES` need no grant; any other (a venv or plugin folder under the
user profile) needs a grant by option B, so ACLs then change outside the flagged folders too.
Which folders those are is listed when phase A is built.

### 7.4 Journal and recovery

- Every ACL change, profile creation, temp file, aside file and batch step is written to the
  journal (with the object's file id, the name, and the previous security descriptor) and flushed
  **before** it is made.
- Undo happens when each launch's Job closes or each batch finishes, and again from the journal
  the next time the broker starts: it removes the ACEs, deletes the profiles, finishes or restores
  batches (section 6), and deletes only journalled temp and aside files (by parent file id and
  exact name, never by name pattern).
- If the journal is missing or unreadable, the broker refuses to launch or change files
  (`journal_unavailable`) and lists the AppContainer profiles it owns (by a fixed name prefix) for
  the user to clean up. It never guesses which ACEs to remove.
- Uninstall touches only the program folder `{app}`, as the security plan requires of the
  installer. It does not remove ACEs, profiles or files in user folders. Changes left by a crash
  that is followed by an uninstall, before the broker ever starts again, stay in place (section 10,
  item 5).

### 7.5 Launch tickets and vool-confine.exe

Linux and macOS confine a child by wrapping its argv (sandbox/job_runner.py
`_kernel_network_isolation_prefix`:799, `_filesystem_only_isolation_prefix`:810). On Windows:
1. The API asks the broker for a ticket, naming the exact argv, working folder, root ids, network,
   limits, and the child's complete environment (built by Python per job, as
   `_child_environment`, job_runner.py:522, does today). The broker checks the roots (section 4)
   and returns a random single-use ticket bound to all of that, valid for a short time.
2. The API runs `vool-confine.exe <ticket> -- <argv>`. vool-confine opens a launch connection to
   the pipe and presents only the ticket; the broker accepts it once, only if the argv matches,
   and launches the child with the bound environment.
3. Stdio: the broker creates the child's stdin, stdout and stderr pipes and duplicates the
   parent's ends into vool-confine (`DuplicateHandle`), which relays them byte for byte to its own
   stdio. This carries streaming protocols such as an MCP server's JSON-RPC; the control channel
   stays one request, one response.
4. Lifetime: the broker closes the launch's Job as soon as vool-confine's launch connection drops,
   so when job_runner kills vool-confine on a timeout or cancel (`_terminate_process_group`:439),
   the child dies too. When the child exits, the broker sends its exit code on the launch
   connection and vool-confine exits with it.
5. What the checks are worth: the ticket is on vool-confine's command line, which any process of
   the same user can read; it is single-use, bound to the argv and environment, and short-lived,
   so a stolen ticket allows at most that one launch. The broker also checks that vool-confine's
   parent is the API (process id and creation time), but a process of the same user can choose
   its reported parent, so that is a second layer, not the real check. vool-confine never holds
   the secret.
In Step 3 this becomes a third prefix in job_runner.py, so plugins, MCP servers and sandbox
commands keep one confinement implementation, and `_WINDOWS_REFUSES_BARE` is lifted only where the
prefix is present and probed.

## 8. Failures and VOOL receipts

Which existing state each outcome lands in (contracts §4 and §5); no new receipt state:

| Outcome | State |
|---|---|
| Refused by the broker (any section 5 rule, unknown root, bad ticket, `in_use` before the commit rename) | refused, nothing changed: `windows_filesystem_authority_unavailable` with the broker's code, except `blocked_by_hard_link`, which keeps its own status (sandbox/sandbox_runner.py:209); launch refusals `confinement_unavailable` (core/plugin_executor.py:384) |
| Broker absent, or the channel fails before the request is written | refused, same statuses |
| A file request was written and no response came (broker died, pipe broke, timeout) | the client raises `EffectOutcomeUnknown` (core/effect_reconciliation.py:61); it never returns `ok=False` for this. That becomes A6 `unknown` only for an A6-reserved intent holding a claim (`a6_reserved_intent`:53, contracts §4 and §5); every other intent ends as today's plain `error`, which is contracts gap 6. Mechanical resolvers exist only for workspace.write_file and machine.write_file (effect_reconciliation.py:219-220). Which of the broker's intents (the moves, the patch, the rollbacks) are A6-reserved is listed when phase C is designed |
| A launched child ran and the broker died | the child is dead (its Job closed); its result goes through `PluginToolResult` (or the process agent's own result), not A6, as `ok=False` with the reason, naming that its effects are unknown. No receipt state says "unknown" there today; that is recorded as part of gap 6 |
| `apply_batch` stopped part way by an I/O error | `ok=False` with the ops that ran and whether the broker's own restore (section 6) completed. If the restore did not complete, the entry stays in the journal and the result says what is left (gap 6) |
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
    a FAT32 volume (refused); an OneDrive-backed folder (record what happens); a repository cloned
    from a local path (record what the hard-link rule does);
  - every operation's success path, and that each refusal changed nothing (hash the tree before
    and after); a batch with a late refusal changes nothing; a batch stopped by an injected I/O
    error is restored; `expect` against a racing writer; `expect=absent` against a file created
    meanwhile; a read-only destination; a file held open by another program;
  - launcher: the child cannot write outside its roots, cannot read the user profile or an
    ungranted folder, cannot reach the network or loopback when `network=false`, cannot connect
    to the broker's pipe, inherits no other handle, two children cannot reach each other, the
    child dies when the broker is killed and when vool-confine is killed, under an outer Job too;
    an MCP-style child streams over stdio; the exit code arrives; git runs (or the test records
    that it does not);
  - journal: ACEs, profiles, temp and aside files are undone after the Job closes, after a broker
    crash at each step, and when the journal is missing (refused, nothing guessed);
  - channel: a client without the secret, a client from another process, a reused process id, a
    second pipe instance, a ticket used twice, a ticket with a different argv or environment, an
    oversized message.
- Linux CI: the Python client's protocol tests against a fake server, and a test that nothing
  outside tests imports `core.windows_broker` before Step 3.

## 10. Open questions (the owner's decisions; nothing is built for them until made)

1. How confined children get their folders: staged copy (A), per-object grants (B), Low integrity
   (C), or A for plugins, decoders and MCP servers with B only for in-place work (proposed).
2. Whether a later step should run VOOL's Python under a separate identity so the broker becomes
   a real privilege boundary.
3. Self-contained publish (about 70 MB, proposed: users need no .NET runtime) or NativeAOT
   (smaller, but needs the C++ build tools on Steve, another install).
4. Building and testing the broker in CI needs a workflow change, which needs the owner's named
   OK.
5. User-visible behaviour changes Step 3 would bring, each needing the owner's OK before Step 3
   ships:
   - moves across volumes are refused (today they copy);
   - only NTFS volumes are accepted;
   - a flagged folder that is itself a junction, a volume root or not on NTFS stops VOOL's broker
     from starting (the gate accepts a junction today);
   - reparse points of every tag, OneDrive placeholders included, are refused (effect on
     OneDrive-backed folders unknown until tested);
   - hard-linked files are refused for reads as well as writes, which may refuse repositories
     cloned from a local path;
   - while VOOL runs, the user cannot rename or delete a flagged folder, and possibly not its
     parent folders; folders an operation holds cannot be renamed while it runs;
   - a read-only file is not overwritten (`read_only`);
   - a developer who flags the folder holding a source checkout must set VOOL_HOME elsewhere;
   - permission changes left by a crash followed by an uninstall stay in place, because uninstall
     touches only the program folder;
   - Windows older than the pinned minimum build is not supported.

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
5. The `core.worktree` case (section 6.1), reported by the seventh review of PR #21 and re-run by
   Job 6, is not recorded. Proposed: record it as an extension of gap 14, with that run's evidence.
