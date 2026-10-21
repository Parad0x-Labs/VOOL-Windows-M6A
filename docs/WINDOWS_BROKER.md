# Windows broker: design (Step 2 of the Windows port)

Status: Phase A built: channel, roots, handle walk, file operations, batches and journal. The
confined launcher (section 7) is not built: it is later work, and section 7 is design only. Not
wired: nothing in VOOL calls the broker. Where the build differs from the design, this document
describes the build, and section 12 lists the design decisions behind it. Read against
windows/v0.7.1-port `1c26068`: every file:line into VOOL's own code was read at that commit;
file:line references into the broker (windows/broker) are to the code this PR adds.
docs/WINDOWS_PORT_CONTRACTS.md (PR #21, read at `637fc6a`) is the source of truth for routes,
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
  API, a new broker starts with a new pipe and secret; nothing carries over. Phase B built this:
  apps/vool_api_server.py calls `core.windows_broker.process.start_for_api` at start-up and stops
  the broker in its exit path. The broker is assigned to the Job before it is sent its bootstrap.
  A broker that is missing, refused or slow (10 s for its start line) is logged and VOOL runs
  without it, because nothing is routed through it before Step 3. The flagged folders are the
  entries of VOOL_WINDOWS_DISPOSABLE_WORKSPACES as written, so the broker applies its own checks.
  As built, the broker also exits when its stdin closes or when the API process (matched by id
  and creation time) exits (Program.cs:236-239).
- Program files: `{app}\broker\` in the installed layout (installer/bundle/vool.iss, staged by
  build_bundle.ps1), `windows\broker\publish\` in a source checkout (windows/broker/publish.ps1,
  git-ignored). A checkout without a published broker starts VOOL without one. The broker never writes in the program folder at run time.
- State: the journal (section 7.4) in `VOOL_HOME\broker\journal\`, a folder of its own, separate
  from the workspace and notes that Job 7 places under VOOL_HOME. In the installed app VOOL_HOME is
  computed by installer/bundle/vool-launch.cmd:13-17 and passed on by bundle_supervisor.py:112
  (%LOCALAPPDATA%\VOOL); a source checkout sets it in Start_VOOL.bat:30-37 (by default
  `.vool_runtime` next to the checkout). The broker refuses to start if the journal folder lies
  inside a flagged folder, so a developer who flags the folder holding a source checkout must set
  VOOL_HOME elsewhere (section 10, item 5).
- If the broker exits, every child it launched dies with it (section 7.1), and the client fails
  closed (section 8).
- **Stop chain.** VOOL's stop matchers do not match `VoolBroker.exe`, `vool-confine.exe` or a
  staged child. That fails safe, because VOOL never stops a process it did not start, but it
  means stopping them depends on a chain. VOOL's stop ends the API. The API's Job (Phase B) ends
  the broker. The broker's own launch Jobs end every child and grandchild. The broker kills only
  through the Jobs it created: no kill by name or pid, and no taskkill (contracts r9 :92/:95).
  The stop and kill paths in PR #18 (Job 4) are not touched. In Phase A the broker launches no
  children, so the launch-Job link applies from the launcher's build on; that build must include
  a test that kills the broker and shows the child and grandchild die. Phase C item 15: the stop
  button must reach the broker.

## 3. Channel

- The API creates a 256-bit secret and starts the broker with it, the API's own process id and
  creation time, the flagged folders (section 4) and the journal folder, on the broker's stdin
  (never argv, never the environment, never a file). As built, this bootstrap is one
  length-prefixed JSON frame of at most 64 KiB with exactly the fields `secret`, `api_pid`,
  `api_creation_time`, `flagged` and `journal_dir`; anything else is refused `invalid_bootstrap`
  (Program.cs:12, 180).
- The broker creates `\\.\pipe\vool-broker-<random 128-bit hex>` with
  `FILE_FLAG_FIRST_PIPE_INSTANCE` for the first instance, `PIPE_REJECT_REMOTE_CLIENTS`, at most 16
  instances, and a DACL that grants access only to the current user's SID. That DACL is what keeps
  AppContainer children and other users out; an explicit deny for `NT AUTHORITY\NETWORK` is added
  as a second layer. It reports the pipe name back on its stdout.
- **Instances as built.** The accept loop makes the next instance before it serves a connection,
  while that stays under the cap, and also when a client leaves before the accept
  (`ERROR_NO_DATA`). So a client turned away at once never leaves the pipe with no instance, and
  15 connections plus a client that connects and drops cannot push it past 16 instances.
  The Python client waits for a free instance with `WaitNamedPipeW` when the pipe is busy.
- Each control connection: the broker checks `GetNamedPipeClientProcessId` names a process whose
  id and creation time match the API's (so a reused process id fails), then a challenge: the
  client returns an HMAC of a broker nonce under the secret, and the broker answers with an HMAC of
  a client nonce, so the client also knows it reached its own broker. The client also checks
  `GetNamedPipeServerProcessId` is the broker it started. A failure disconnects and is logged.
- Control messages are length-prefixed UTF-8 JSON, one request and one response, with a request
  id. Maximum 64 MiB; larger contents are refused (`too_large`) in Step 2.
- **Framing as built.** Each frame is a 4-byte little-endian unsigned length followed by that many
  bytes of UTF-8 JSON, up to 64 MiB. A larger length is answered `too_large` with `"id": null`,
  because the frame was not parsed and its id is unknown, and the connection is then closed. The
  Python client treats a `too_large` with a null id as `EffectOutcomeUnknown`, never as a plain
  refusal.
- **Framed JSON semantics, not canonical bytes.** The contract between client and broker is the
  JSON value each frame carries, not its exact body bytes. Python's `json` with
  `ensure_ascii=False` and .NET's `System.Text.Json` may write the same value with different
  bytes, for example in how non-ASCII text is escaped. Nothing hashes or signs a body:
  authentication is per connection (below). Every string field is taken exactly as sent or
  refused `invalid_name`. Two cases are refused and never decoded to U+FFFD:
  - raw bytes that are not valid UTF-8;
  - an escaped unpaired surrogate, which is what Python's `json` writes for a Windows name holding
    a lone surrogate.

  A repaired name could point at a different file.
- **Several requests per connection, authenticated once.** After the challenge and response, every
  frame on the connection is a request. There is no per-frame HMAC. A pipe connection is a kernel
  handle, and injecting frames into it needs that handle. A process can only get the handle by
  duplicating it, and a process that can do that could read the secret anyway.
  - Any frame before authentication finishes closes the connection. The one frame read before
    authentication, `client_proof`, is capped at 1 KiB: a larger length closes the connection
    without reading the body.
  - A client nonce is used once per broker run, compared by its bytes, so another spelling of the
    same hex is a replay too.
  - Request ids (a string or a number) are unique per connection (`replayed_request`). A request
    with no `id` or `op`, or an id over 256 characters, closes the connection.
  - A connection takes at most 4096 requests (`too_many_requests`, then the connection closes).
  - A connection holds at most 64 roots (`too_many_roots`).
  - A 30-second idle timeout runs only while the broker waits on the client (reading its next
    frame, or writing to it), never while it is working on a request.
    `VOOL_BROKER_IDLE_TIMEOUT_MS` may only shorten it (100 ms to under 30 s).
  - Each connection runs on its own thread with synchronous I/O, which the watchdog ends with
    `CancelSynchronousIo`.
  - Unknown fields on a request are refused (`invalid_request`).
  - An unexpected exception inside an operation is answered `authority_check_failed`, and the
    connection stays open.
- **The guard this rests on.** The one way around connection authentication is a pipe handle
  leaked into another process. Every handle the broker creates is non-inheritable. A test reads
  the system handle table, takes a settled snapshot before and after authentication, and requires
  every new handle to be non-inheritable.
- Windows lets another process of the same user create a second server instance with the same
  pipe name (`FILE_FLAG_FIRST_PIPE_INSTANCE` only protects the broker's own first instance). The
  client's `GetNamedPipeServerProcessId` check against the broker it started is what defends
  against that.
- **Start line.** The start line is one JSON line on stdout: `{pipe, broker_pid,
  broker_creation_time, flagged:[{id, path}], journal:{available, recovered, pending:[{batch_id,
  left}], torn:[{batch_id, bytes, kept_in}]}}`. `torn` lists each torn last journal record
  recovery cut off (section 7.4).
  - `flagged` has one entry per bootstrap entry, in order. Duplicates repeat the same id.
  - `path` is the DOS final path, with `\\?\` dropped for drive-letter paths.
  - A refused start prints `{error:{entry, code, message}}` or `{error:{code}}` and opens no pipe.
- A launched child's stdio does not use the control channel (section 7.5).

## 4. Roots

- **Flagged folders, opened at start.** The API passes the flagged folders it read from
  VOOL_WINDOWS_DISPOSABLE_WORKSPACES. The broker opens each one with
  `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, without `FILE_SHARE_DELETE`, and
  keeps those handles for its lifetime. Each entry is checked against the rules below. A change
  to the variable needs a VOOL restart.

  Rules for flagged folders, as built. G is core/windows_write_gate.py at `1c26068`
  (`flagged_roots`:51, `_inside`:61); J6 is Job 6's design, whose resolver feeds Step 3; C is the
  contracts doc (PR #21) at a7caed3; B is this document. The rule for the comparison:
  for the same input, the broker is never looser than G or J6.

| # | Input | G / J6 | Broker | Diff |
|---|---|---|---|---|
| F1 | Entry not absolute, or no drive letter (including a fully qualified `\\?\Volume{GUID}\...` or `\\?\GLOBALROOT\...` spelling) | G drops it silently (`os.path.isabs`). J6 refuses a folder that isn't absolute. | Refuse start, `invalid_flagged`. Checked after the F5 network/device checks, so F5 spellings keep their own code. | stricter: fails start instead of dropping |
| F2 | Entry doesn't exist or isn't a directory | G drops it silently (`isdir`) | Refuse start, `invalid_flagged` | stricter |
| F3 | Entry is a junction, symlink or other reparse point (any tag) | G resolves it with realpath and accepts it (windows_write_gate.py:57). C §3 treats name-surrogate tags as links. | Refuse start, `reparse_point`. Opened with FILE_FLAG_OPEN_REPARSE_POINT, checked with FileAttributeTagInfo. | stricter (B §4, §10 item 5) |
| F4 | Entry spelled through subst, an 8.3 short name, `\\?\X:\` or different case | G normcase + realpath gives one spelling | Accepted. Identity is volume serial + 128-bit file id (FileIdInfo). The reported final path is GetFinalPathNameByHandleW (long name). | same object, same result |
| F5 | UNC `\\server\share`, `\\?\UNC\`, `\\.\` device path, or a mapped network drive | G accepts any isdir | Refuse start, `unsupported_filesystem`: UNC, `\\?\UNC\` and `\\.\` by text before any open (before F1's drive-letter check); a mapped drive passes F1 and is refused when its final path is UNC. Real-broker rows: unc, long_unc, device. A mapped-drive row needs a share, so it runs on the hosted runner (section 9.2). | stricter |
| F6 | Volume root (`C:\`) | G accepts | Refuse start, `invalid_flagged` (B §4) | stricter |
| F7 | Volume not NTFS (FAT, exFAT, ReFS) | G accepts | Refuse start, `unsupported_filesystem`. Checked first, from the opened folder's handle (`GetVolumeInformationByHandleW`), before any other check on it; the refusal carries a message naming the file system. | stricter |
| F8 | Two entries naming the same folder, or one inside another | G keeps both | Accepted. One id per distinct file id; a nested entry keeps its own id. | same |
| F9 | Journal folder inside a flagged folder | n/a | Refuse start, `journal_inside_flagged` (B §2). The check resolves the deepest existing ancestor's final path, so junction, subst and 8.3 spellings are caught. | new |
| F10 | Any refused entry | n/a | The whole start is refused, naming the entry and its reason | stricter |

- A flagged path holding NUL is refused `invalid_flagged`, and a `journal_dir` holding NUL is
  refused `invalid_bootstrap`. Windows reads a path only up to a NUL, so `C:\a\0\b` would
  otherwise open `C:\a`.
- A flagged folder that is itself case-sensitive (`FILE_CS_FLAG_CASE_SENSITIVE_DIR`) is refused
  `case_sensitive_dir`, and so is the whole start (Roots.cs:82).
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
- `open_root(flagged_id, "")` is the flagged folder itself. The root uses the broker's held
  handle, and that handle is not opened again.
- Roots are compared by file id, never by spelling, so a junction, subst drive or 8.3 short name
  for the same folder is the same root. Behaviour difference from the gate: the gate resolves a
  flagged folder that is a junction and accepts it (by design, windows_write_gate.py:57); the
  broker refuses it (section 10, item 5).
- Holding the flagged folders without `FILE_SHARE_DELETE` means the user cannot rename or delete
  a flagged folder while VOOL runs, nor its parent folders (Documents or a development folder);
  P15 tests this (section 5). Section 10, item 5.
- Step 2 accepts only NTFS volumes (`unsupported_filesystem` otherwise). FAT32, exFAT and ReFS are
  refused (F7), and so are network shares, including a mapped drive (F5). On FAT and exFAT, file
  ids are not stable across renames.
- Minimum Windows: the minimum build is pinned at 10.0.17763 (1809). An older build is refused
  `unsupported_windows` (Roots.cs:30), and so is a broker not running as a 64-bit process
  (Roots.cs:32).

## 5. The handle walk

Every operation names a `root_id` and a relative path. The path is split on `\` and `/`, and each
component is opened relative to the previous handle with `NtCreateFile` and
`OBJECT_ATTRIBUTES.RootDirectory` (Win32 `CreateFile` has no relative open), always with
`FILE_OPEN_REPARSE_POINT`, then checked with `FileAttributeTagInfo`. Every directory on the way,
and the root, is opened without `FILE_SHARE_DELETE` and held until the operation (or batch)
finishes, so none of them can be renamed or deleted out of the root meanwhile. While an operation
holds them, the user cannot rename those folders, or their ancestors up to the flagged folder's
parent (P15, below).

P15, tested: while a root is held, renaming or deleting the held folder, its parent, the
flagged folder or the flagged folder's parent is refused.

| Rule | Refusal code |
|---|---|
| Component is empty, `.` or `..` | `path_escapes_root` |
| Component contains `:` (alternate data stream), `\0`, or a character Windows rejects | `invalid_name` |
| Component ends in `.` or space | `invalid_name` |
| Component is a DOS device name (CON, PRN, AUX, NUL, COM0-9, LPT0-9, CONIN$, CONOUT$, with any extension, any case, superscript digits included) | `device_name` |
| The opened object is a reparse point of any tag (junction, symlink, mount point, OneDrive placeholder, dedup, AppExecLink) | `reparse_point` |
| The opened object is a file with a link count above 1 (read or write); checked before the name, so a multi-link file is always refused with this code | `blocked_by_hard_link` |
| The component is not the object's own name (an 8.3 short name), compared case-insensitively with the name `FileNormalizedNameInfo` returns (the opened-name class gives an alias back as itself) | `short_name` |
| A directory on the way is case-sensitive (`FILE_CS_FLAG_CASE_SENSITIVE_DIR`) | `case_sensitive_dir` |
| The relative path is over 32,767 UTF-16 units | `path_too_long` |
| The object's volume differs from the flagged folder's | `path_escapes_root` |
| The object is held by another program (sharing violation) | `in_use` |

Other codes the build uses:
- `not_found`, `not_a_directory`, `is_a_directory`;
- `access_denied` (walk NTSTATUS 0xC0000022, flagged-folder error 5);
- `in_use` (also flagged-folder error 32, and delete-pending);
- `invalid_request`.
- NTSTATUS 0xC000050B maps to `reparse_point`, and 0xC0000033 to `invalid_name`.
- Any other open failure is `authority_check_failed`.

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

Why holding a folder works: every walk open and every flagged handle takes FILE_READ_DATA /
FILE_LIST_DIRECTORY, because NTFS records share access only for opens with data or delete access.
Without it, a held folder could still be renamed. Opens also
add SYNCHRONIZE, FILE_SYNCHRONOUS_IO_NONALERT and OBJ_DONT_REPARSE.

An empty folder can become a junction after the walk checked it, because `mklink /J` creates the
folder and then sets the reparse data. The open under it then returns
STATUS_REPARSE_POINT_NOT_RESOLVED (0xC0000280), which maps to `reparse_point`. Only an empty
folder can be turned into a junction, so nothing under it was reachable. A deterministic test
covers it.

After the walk, the broker reads the final object's path with `GetFinalPathNameByHandleW` and
`VOLUME_NAME_GUID` and confirms it is under the flagged folder's (drive letters and subst do not
matter). It is defence in depth: the held handles are the real check. If the call fails, the
operation is refused (`authority_check_failed`).

Concurrency: changes (`write_file`, `ensure_directory`, `delete`, `rename`, `apply_batch`,
`restore_batch`) run one at a time across the whole broker, under one lock. Reads (`stat`, `read`,
`open_root`) are not serialized with them, so a read may see a batch part way (Phase C item 18). A
batch reuses the handles its plan opened. A sharing violation with another program maps to
`in_use`, with nothing changed.

## 6. File operations

Operations (Step 2 set):

| Operation | Done as | For (from Step 3) |
|---|---|---|
| `stat(root, rel)` | walk, `FileAttributeTagInfo` / `FileStandardInfo` / `FileIdInfo`; replies kind, size, file id and read-only | checks before a change |
| `read(root, rel, max_bytes)` | walk, open for read, read | diffs, rollback |
| `write_file(root, rel, data, expect)` | see below | workspace.write_file, workspace.replace_in_file, machine.write_file |
| `ensure_directory(root, rel)` | create each missing component relative to its parent | workspace.ensure_directory, machine.ensure_directory |
| `rename(root, rel_from, root2, rel_to, replace)` | walk both, hold the source with `DELETE`, rename by `FileRenameInformation` onto the held destination parent, never replacing; an existing destination with `replace` is set aside first, as a delete is | machine.move_path, operator.move_path |
| `delete(root, rel)` | open with `DELETE`, rename aside (journalled) | rollback |
| `apply_batch(root, ops[])` | see below | workspace.apply_unified_diff (section 6.1), workspace.rollback_last_change, code.task.rollback (in its task's folder, section 6.1) |
| `restore_batch(batch_id)` | see below | undoing a part-done batch |

The protocol also has `ping` (no fields) and `close_root(root_id)`, which releases a root and the
folders it holds; a connection's roots are also released when it closes. A root is known only on
the connection that opened it (`unknown_root` elsewhere), and an unknown operation is refused
`unknown_op` (Channel.cs:221-240, 280, 320). For `rename`, `to_root_id` is optional; a rename
inside `apply_batch` stays in the batch's root.

- Every change goes through one runner: `write_file`, `ensure_directory`, `delete`, `rename` and
  `apply_batch` (up to 1024 operations).
  - **Plan** walks every path, holds every object with DELETE access and read sharing only, and
    checks every rule and every `expect`.
  - **Execute** runs only after the plan passes. A refusal in the plan changes nothing.
- Steps:
  - temp: a new object named `.vool-broker-tmp-<batch id[..12]>-<n>`;
  - aside: the old object renamed to `.vool-broker-aside-...`;
  - place: rename into place, never replacing.

  A file is never overwritten in place. A replace is not one atomic step: between aside and place
  the name is briefly empty, and a reader in that moment finds no file (Phase C item 18).
- `write_file` requires `expect`: `any`, `absent` or `sha256:<64 lowercase hex>`. There is no
  `mode` field. A hash mismatch is `changed_since_check`, and a read-only destination is
  `read_only`.
- `read` refuses a file larger than `max_bytes` with `too_large`. `max_bytes` may be at most
  47 MiB (a larger value is refused `invalid_request`, FileOps.cs:36), so that the base64
  reply fits in a 64 MiB frame even in the worst case, with replies written by a relaxed JSON
  encoder (raw `+`, `/` and non-ASCII). A read is never cut short. A reply that still cannot be
  encoded or does not fit is answered `reply_unencodable` with its request id, never `too_large`,
  a replacement character or a dropped connection.
- Refused `invalid_request`:
  - the same object named twice in a batch;
  - a path under another operation's object;
  - a rename that only changes letter case. With `to_root_id` naming a second root on the same
    folder, the plan sees two trees and the second `DELETE` open hits a sharing violation, so that
    case is refused `in_use` instead. Either way nothing changes.
- A folder delete must find the folder empty at plan time, and again once it is set aside
  (`not_empty`). Holding a folder does not stop another program creating something in it. An entry
  made before the commit stops the batch, which puts the folder back. One made after the commit is
  found by cleanup, which puts the folder back under its own name, entry and all, and reports it
  in `cleanup_left`.
- New codes:
  - `destination_exists`, `not_empty`, `read_only`, `cross_volume`, `changed_since_check`;
  - `journal_unavailable`, `journal_pending`, `journal_unreadable`;
  - `unknown_batch`, `restore_incomplete`, `too_many_roots`, `io_error`, `reply_unencodable`.
- **Replies for a finished change:** `ok:true` with `batch_id`, one result per operation
  (`results`) and `cleanup_left`. `restore_batch` replies `ok:true` with `batch_id` and
  `committed`, or `restore_incomplete` with `batch {batch_id, ran: null, restored: false, left}`
  (Channel.cs:276-277, 313-314).
- **Replies for a batch that made changes and then stopped:** `ok:false` with `batch {batch_id,
  ran, restored, left}`.
  - When `restored` is true, `error.code` is the cause.
  - When `restored` is false, `error.code` is `restore_incomplete` and `error.cause` is the cause.
  - `left` names what is still out of place. A restore that stops at a step whose journal record
    can't be written names that step, then `N later step(s) not attempted`; the batch stays in
    the journal and the next restore finishes it (undo is idempotent).

Moves across volumes are refused (`cross_volume`). Today `machine.move_path` copies across volumes
(core/machine_file_ops.py:203 `shutil.move`), so this is a behaviour change (section 10, item 5);
a copy-then-delete by handle, with its own partial-failure receipt, is later work.

Not covered by any broker file operation:
- `repo.git`: git does its own I/O, so it can only run in the launcher (section 6.1).
- `media.export`: written by the separate media worker; it can only be confined through the
  launcher (contracts gap 8).
- `operator.cleanup_temp_files`: stays refused on Windows (`_ALWAYS_REFUSED`,
  windows_write_gate.py:48).
- Operator notes: not gated today (contracts gap 13); the broker could serve them later.

### 6.1 Git, steps and patches (target for Step 3)

This describes the contract Step 3 needs, not what the gate does today. At 1c26068 the gate's
`refusal()` returns allow or refuse and hands over no roots, and for `repo.git` and
`workspace.apply_unified_diff` it checks only the chat's workspace (`_GATED`:32, 35 are empty;
contracts gaps 14 and 15). Job 6 (branch windows/port-landing-gate, not merged at the time of
writing) plans a public resolver in core/windows_write_gate.py, called by `refusal()`, that
places each call's writes and refuses any call it cannot place or that falls outside the flagged
folders. Step 3 takes the broker's roots from that same function, with no second API, passed as
(flagged id, relative path) per section 4, and the broker uses those and nothing wider. Switching
the gate to the broker's list of flagged folders (section 4) is a Step 3 change, not part of
Job 6.

- **`repo.git`** (and `repo.step` when its inner call is like `repo.git`) runs in the launcher.
  The write roots are the top folder (`git rev-parse --show-toplevel`), the git dir
  (`--absolute-git-dir`) and the common dir (`--git-common-dir`; a linked worktree keeps its git
  dir and objects under the main repository's `.git`), as git itself resolves them, never
  inferred from where `.git` sits. Every folder that function returns must be one the gate
  approved; Job 6 may also refuse a repository whose work tree lies outside the workspace. The
  reason: a repository's own config (`core.worktree`) can move git's top to any folder that
  contains the workspace even when `.git` is inside the flagged folder (contracts gap 15, at
  `637fc6a`).
- **Resolving safely.** `rev-parse` runs only after `safe_git.executable_local_config`
  (core/safe_git.py:89) has passed, with `safe_git.hardening_args()` (78) and
  `safe_git.safe_env()` (68-75, which removes every inherited `GIT_*` variable except the
  identity ones). The resolver refuses when the config read fails (not a repository, or an
  unreadable config) or the config has a program-running key. The resolution happens at check
  time and the launch later, and the config can change between them, so the enforcement is the
  launcher's grants: a git run with a changed config fails to write outside the granted roots
  instead of following it. The confined git gets the same `safe_env()`. `.git/config` lies inside
  a write root, so a confined git or hook can rewrite `core.worktree` for the next call; each call
  resolves and checks afresh and trusts nothing a previous run left.
- **Steps and rollbacks.** The resolver maps `repo.step`, `code.task.step` and
  `code.task.rollback` to the repo session's or code task's own folder, read from VOOL's own
  journals, for both the early gate and the dispatch gate (contracts gap 2). The broker gets that
  folder as the root, nothing wider.
- **Patches.** After Job 6, `workspace.apply_unified_diff` on Windows uses no git at all (so no
  `rev-parse` either): a patch goes through VOOL's own engine only, rename, copy, mode and binary
  sections are refused, and paths are placed inside the workspace. In Step 3 that engine applies
  the patch through `apply_batch`, so every path is walked by handle by the broker itself.
- Whether git runs at all inside an AppContainer is unknown (Git for Windows reads
  `%USERPROFILE%\.gitconfig`, which an AppContainer cannot read, and needs its own folders under
  Program Files); a test must show it before Step 3 relies on it.

## 7. Confined launcher

Not built in this PR. The launcher is later work, built under its own plan; everything in this
section is design.

A probe result kept as a fact for that work (Phase C item 17): on a Windows 11 build 26200 host,
ALL APPLICATION PACKAGES can read and execute C:\Windows and C:\Program Files, and nothing in the
host's own .NET SDK folder, the broker's bin folder, %LOCALAPPDATA%\Packages or the user profile.

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
4. Environment: exactly the environment bound in the ticket (section 7.5). For git that is
   `safe_git.safe_env()` (section 6.1); for other children, Python's per-job environment. TEMP and TMP point
   to the AppContainer profile's own `AC\Temp`, so scratch stays out of the user's folders.

Limits: if VOOL itself runs inside a Job (a terminal, an IDE, the supervisor), the limits combine;
nested jobs must be tested. A process created on the child's behalf by a service (WMI, Task
Scheduler, an out-of-process COM server) runs outside the Job; AppContainer blocks most of those
routes.

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
  profile or a folder not granted. The launcher's tests must enumerate what it can read.

### 7.3 Granting the child its folders (decided, section 10; not built)

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
Which folders those are, and how each is granted, is decided in phase C (section 10, item 6);
the launcher's build tests the mechanism on folders the tests choose.

### 7.4 Journal and recovery

File changes, as built:
- Location: `journal_dir` from the bootstrap (`VOOL_HOME\broker\journal`). It is outside `{app}`
  and every flagged folder (F9). Each batch has one append-only `batch-<id>.jsonl`.
- Each record is written as one write of the whole line plus its line end, and flushed to disk
  (WriteThrough plus Flush(true)) before the step it names.
- An error part way:
  1. the handles are released;
  2. the journal undoes the steps already made, newest first.
- A commit:
  1. the asides are deleted through the plan's held handles;
  2. the journal deletes any aside it could not;
  3. the batch file is removed.
- Recovery:
  - It opens folders by file id (OpenFileById), and only inside a folder that is flagged now.
  - It changes an object only when that object's file id matches the journal.
  - It records each step it undoes, so a later attempt skips that step.
- A torn last record: bytes after the last line end,
  where every earlier line parses and fits, are a record whose write never finished, so its action
  never began. Recovery writes those bytes to a new `batch-<id>.torn` (or `batch-<id>.N.torn`,
  never replacing) and flushes it. Only then does it cut them off the batch file, before it
  appends anything of its own. It reports this in the outcome and on the start line. If the bytes
  can't be kept, nothing is cut and the batch stays pending (`journal_unavailable`). A begin cut
  short with nothing after it ends as no batch. The broker never deletes `.torn` files.
  `restore_batch`'s reply does not report a torn record yet (client follow-up).
- Before acting, recovery flushes the batch file to disk, so a record whose own flush failed (a
  commit above all) is durable before anything acts on it. If that flush fails, nothing changes.
- Any other damage is never guessed at: a record before the last line end that does not parse or
  is not a record, an empty file, or a value that parses but cannot be read (a lone surrogate).
  The same goes for a journal whose records parse but do not fit its schema (begin first with
  its own batch id, steps numbered in order with exact fields per kind, the batch's own temp and
  aside names, done or undone only for a step that exists and at most once). Recovery
  stops for that batch with `journal_unreadable`, and the batch stays pending.
- Changes and `restore_batch` run one at a time under one lock. A temp is removed only when its
  file id matches, or, when the id is unknown (the create failed or a crash came first), only if
  it is exactly what a create leaves (an empty file with one link, or an empty folder). A temp a
  later place moved is not counted undone until that place is undone. A batch whose commit record
  reached the journal is reported done even if the flush then failed.
- While any batch is pending (unfinished, not fully restored, or unreadable), every new change is
  refused `journal_pending`. Reads still work. `restore_batch` can finish a batch, and answers
  `journal_unreadable` for a damaged one, which only a person can clear (Phase C item 6).
- No journal folder: the broker starts, every change is refused `journal_unavailable`, and reads
  work.
- `VOOL_BROKER_TEST_PAUSE_AT` is for tests only. It stops a batch at a named boundary until the
  broker is killed, so it can only do what a kill does. In Phase C the API starts the broker with
  an environment that does not carry this variable.
- The kill test kills the broker at every journal boundary of an 11-step batch (35 boundaries,
  70 broker runs) and checks that the next start puts everything back.
- The journal has no launcher records (profiles, stages, ACEs): the launcher is not built. The
  bullets below about ACL changes and profiles stay design.

Design (launcher part not built):
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
   connection and vool-confine exits with it. Ctrl-C: vool-confine handles a console Ctrl-C or
   Ctrl-Break itself by closing its launch connection and exiting with code 130, so the broker
   closes the Job and the child is killed at once, with no graceful signal; a child that needs a
   graceful stop is out of scope for Step 2.
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
| Refused by the broker (any section 5 rule, unknown root, bad ticket, `in_use` before the batch's first step) | refused, nothing changed: `windows_filesystem_authority_unavailable` with the broker's code, except `blocked_by_hard_link`, which keeps its own status (sandbox/sandbox_runner.py:209); launch refusals `confinement_unavailable` (core/plugin_executor.py:384) |
| Broker absent, or the channel fails before the request is written | refused, same statuses |
| A file request was written and no response came (broker died, pipe broke, timeout) | the client raises `EffectOutcomeUnknown` (core/effect_reconciliation.py:61); it never returns `ok=False` for this. That becomes A6 `unknown` only for an A6-reserved intent holding a claim (`a6_reserved_intent`:53, contracts §4 and §5); every other case (an intent that is not reserved, or a reserved one with no `checkpoint_id`) ends as today's plain `error`, which is contracts gap 6. Mechanical resolvers exist only for workspace.write_file and machine.write_file (effect_reconciliation.py:219-220). Phase C fixes the list of which broker-backed intents (the moves, the patch, the rollbacks) are A6-reserved; Step 2 does not. A broker change takes effect step by step (section 6): a batch with several operations changes the tree one step at a time before its commit, and any batch that stops part way is undone by the journal, so "written, no response" may mean it happened |
| A launched child ran and the broker died | the child is dead (its Job closed); its result goes through `PluginToolResult` (or the process agent's own result), not A6, as `ok=False` with the reason, naming that its effects are unknown. No receipt state says "unknown" there today; that is recorded as part of gap 6 |
| `apply_batch` stopped part way by an I/O error | `ok=False` with the ops that ran and whether the broker's own restore (section 6) completed. If the restore did not complete, the entry stays in the journal and the result says what is left (gap 6) |
| A handler returns `ok=False` after the broker changed files (a part-done batch whose restore did not complete) | as today: the executor ack records it as A6 `failed_safe_to_retry` / `dispatch_ack` (tool_intent_executor.py:1538-1549), the third outcome of contracts gap 6 |
| Executed | `executed`, with file ids and new hashes |

Client outcomes as built (`core/windows_broker/client.py`):
- `ConnectionError`: nothing was written.
- `BrokerRefusal`: refused with nothing changed; the connection stays usable. `BatchRestored`
  is the batch form: it keeps `batch`, and `batch.restored` is true.
- `EffectOutcomeUnknown`: the request may have had an effect. That covers a lost reply after a
  write, `reply_unencodable`, a reply for another id (a `too_large` with a null id among them)
  and a reply of the wrong shape. `BatchNotRestored` is the batch form: it keeps `batch`,
  `error.code` and `error.cause`, for `restore_incomplete` and `restored:false`. The connection
  is closed after either, so a caller reconnects to call `restore_batch`.

Client follow-up for Phase C:
- Map `EffectOutcomeUnknown` and `BatchNotRestored` to VOOL's "outcome unknown, not safe to
  retry" state, never to a plain refusal.
- Read `journal.torn` on the start line, and surface it to the person, with `pending`.
- When the launcher is built: map vool-confine's "effects unknown" outcome to not safe to retry
  as well.

Contracts r9 :99-104 has no such state for a launch, so that is a TDL follow-up for a
contracts-doc round at Phase C.

## 9. Code layout and tests

- `windows/broker/VoolBroker.sln` has three projects: `VoolBroker` (the exe: bootstrap, start
  line, serving the pipe), `VoolBroker.Core` (channel and protocol, roots, handle walk, file
  operations, batches, journal) and `VoolBroker.Tests` (xunit v3).
- There is no `VoolConfine` project and no launcher code. The confined launcher (section 7) is not
  built in this PR; it is later work.
- .NET 10, x64 (`net10.0-windows`). The SDK is pinned to 10.0.402 in `windows/broker/global.json`
  and the test packages in `Directory.Packages.props`. Builds and tests are framework-dependent.
  windows/broker/publish.ps1 publishes self-contained for win-x64 (Phase B), pinned to runtime
  10.0.13 (the one SDK 10.0.402 bundles), with `DisableTransitiveFrameworkReferenceDownloads` so
  the ASP.NET Core and Windows Desktop runtime packs the broker never uses are not needed.
- `core/windows_broker/client.py`: the Python client. Nothing imports it before Step 3.
- `core/windows_broker/process.py`: starts and stops the broker with the API (Phase B). It is the
  one module outside the tests that production code may import (tests/windows_broker/test_no_imports.py).
- Tests on real Windows (a non-admin Windows 11 development machine, and the hosted Windows runner
  in windows-broker.yml), each against the real broker, never mocks. Built in this PR:
  - escapes: `..`; a junction and a symlink inside a root pointing out; one swapped in during a
    walk by a racing thread; renaming an ancestor folder (and a flagged folder) out of the root
    while an operation holds it; a hard link to an outside file (write and read); an 8.3 name and
    a subst drive for a root; DOS device names; an alternate data stream; trailing dot and space;
    a case-sensitive folder; a path over 32,767 units; an object on a second volume; FAT32, exFAT
    and ReFS flagged folders (refused);
  - every operation's success path, and that each refusal changed nothing (hash the tree before
    and after); a batch with a late refusal changes nothing; a batch stopped by an injected I/O
    error is restored; `expect` against a racing writer; `expect=absent` against a file created
    meanwhile; a read-only destination; a file held open by another program;
  - journal: temp and aside files are undone or finished after a broker crash at each step, a
    damaged journal is never guessed at, and changes are refused when the journal is unavailable;
  - channel: a client without the secret, a client from another process, a reused process id
    (wrong creation time), a second pipe instance, an oversized message.
- Later work, not in this PR:
  - an OneDrive-backed folder (record what happens); a repository cloned from a local path (record
    what the hard-link rule does);
  - launcher: the child cannot write outside its roots, cannot read the user profile or an
    ungranted folder, cannot reach the network or loopback when `network=false`, cannot connect
    to the broker's pipe, inherits no other handle, two children cannot reach each other, the
    child dies when the broker is killed and when vool-confine is killed, under an outer Job too;
    an MCP-style child streams over stdio; the exit code arrives; git runs (or the test records
    that it does not);
  - journal: ACEs and profiles are undone after the Job closes and after a broker crash;
  - channel: a ticket used twice, a ticket with a different argv or environment.
- Python: 28 tests in `tests/windows_broker`: the client's protocol and failure-mapping tests
  against a fake server, a test that nothing outside tests imports `core.windows_broker` before
  Step 3, and 4 tests against the real broker, which skip off Windows.

### 9.1 As built

- On the hosted Windows runner (windows-broker.yml), the C# suite runs 210 tests: 209 pass and 1
  skips, the pre-1809 platform row (no hosted runner is that old).
- The 28 Python client tests run there against the built broker, and the workflow fails if any of
  them skips.
- On a non-admin development machine the admin rows (section 9.2) skip with their reason.
- Mutation testing and an independent review were done during development.

### 9.2 Hosted-runner rows, and P16

P7 (8.3 short name), P14 (object on another volume), F7 (non-NTFS flagged folder) and the F5
mapped-network-drive spelling can't run on a non-admin host: 8.3 creation is off on the
development machine, and a second volume, a mounted-volume folder or a share needs admin. There
their tests skip with that reason. They run in windows-broker.yml on a hosted Windows runner
(admin), and all pass there. Only the pre-1809 platform row skips, since no hosted runner is that
old.

The workflow (`windows-2025` runner; the set-up step runs only on a GitHub-hosted runner):

| Row | Setup | Expected result |
|---|---|---|
| P7 | NTFS VHDX `V:` with 8.3 names turned on before any folder is made (`fsutil 8dot3name set 2`, then `set V: 0`). The step fails unless a new folder gets a short name. | The short-name tests find a short name |
| P14 | A second NTFS VHDX mounted as the folder `V:\flagged\mnt` | Refused `reparse_point` |
| F7 | FAT32 `F:`, exFAT `E:`, ReFS `R:` | Refused `unsupported_filesystem` |
| F5 | SMB share `vooltest` on `V:\share`, mapped as `M:` with `net use` | Refused `unsupported_filesystem`: the real path is `\\?\UNC\…` (Roots.cs:77) |

- P14 expects `reparse_point`, since a mounted-volume folder is a reparse point; a relative walk
  has no other way onto another volume, so the volume check stays defence in depth and the test
  proves the refusal.
- Any set-up failure fails the job. ReFS is required: a failed ReFS set-up fails the job too.
- The tests read the volumes and the share from environment variables the workflow sets
  (`VOOL_BROKER_TEST_8DOT3_ROOT`, `VOOL_BROKER_TEST_MOUNTED_FOLDER`,
  `VOOL_BROKER_TEST_FAT32_ROOT`, `VOOL_BROKER_TEST_EXFAT_ROOT`, `VOOL_BROKER_TEST_REFS_ROOT`,
  `VOOL_BROKER_TEST_MAPPED_ROOT`).
- In CI, `VOOL_BROKER_TEST_REQUIRE_ADMIN_ROWS=1` makes a missing volume or share a failure, not a
  skip. The pre-1809 platform skip stays a plain skip.
- On a development machine the variables are unset, so the rows skip with their reason.

Unreachable by design, so no test:
- P16 (final-path containment): defence in depth. Every way a relative open has onto another
  volume or outside the root (mount point, junction, symlink, hard link) is refused earlier, and
  walked folders are held, so no input reaches it with an outside path. If any earlier refusal is
  loosened, P16 needs a test then.

## 10. Decisions and open questions

Decided:
- Folder access (section 7.3): staged copy (A) for plugins, decoders and MCP servers; per-object
  grants (B) only for process agents and git. The launcher's first build implements A; B is a
  second build.
- Packaging (item 3 below): self-contained publish.

Still open (nothing is built for them until decided):

1. (Decided above.)
2. Whether a later step should run VOOL's Python under a separate identity so the broker becomes
   a real privilege boundary.
3. (Decided above: self-contained, about 70 MB, so users need no .NET runtime. NativeAOT, which
   needs the C++ build tools, was not chosen.)
4. Building and testing the broker in CI needs a new workflow, windows-broker.yml (dispatch, and
   pull requests that touch the broker's folders or a root-level .NET build input), kept apart
   from the gauntlet so broker failures and build time never block other merges. (Done: this PR
   adds it.)
5. User-visible behaviour changes Step 3 would bring, each needing explicit approval before Step 3
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
   - the flagged-folder list is fixed when the broker starts, so changing
     VOOL_WINDOWS_DISPOSABLE_WORKSPACES takes a VOOL restart;
   - Windows older than the pinned minimum build is not supported.
6. The read roots outside the flagged folders (section 7.3): which folders, and how each is
   granted. Decided in phase C.

### Phase C decisions

Recorded now, to be decided at wiring. Phase A keeps the stricter default because
nothing is wired yet. Once the broker is wired, the gate and the broker must agree, and each item
below changes what the user sees. Items 12-14, 16 and 17 concern the launcher and stay recorded
for when it is built; item 15 (the stop button must reach the broker) applies to the broker itself
from Phase B.

1. A bad entry in VOOL_WINDOWS_DISPOSABLE_WORKSPACES stops the whole broker. The alternative is to
   drop that entry and show a visible warning.
2. OneDrive placeholders are refused. This matters a lot when a flagged folder sits in
   OneDrive-synced Documents.
3. Hard-linked files are refused, including git objects from a local clone. That collides with the
   in-place per-object git grants chosen for git (option B, section 10).
4. ReFS volumes, Windows Dev Drives included, are refused (NTFS only).
5. A flagged folder that is a junction is refused.
6. A batch the journal still holds (it stopped and could not be fully put back, or a crash left it
   and recovery could not finish, or its journal file is damaged) blocks every file change until it
   is finished with restore_batch or a person clears it. A damaged journal is never guessed at, so
   only a person can clear it: today that means checking the objects the start line lists under
   `pending`, then deleting that batch's file in VOOL_HOME\broker\journal. Decide how VOOL shows
   this and offers the clearing step.
7. If the journal folder cannot be created or written, the broker still starts, every file change
   is refused (journal_unavailable) and reads still work.
8. Reads over 47 MiB are refused (too_large), never cut short.
9. A rename that only changes letter case, and deleting a folder that is not empty, are refused.
10. UNC, `\\?\UNC\`, `\\.\` device-path, mapped-network-drive and volume-GUID flagged entries are
    refused, and so is the whole start (F1, F5, F10).
11. A file or folder name holding an unpaired UTF-16 surrogate (Python's json writes it as a lone
    surrogate escape, backslash-u then DC80) is refused invalid_name, never repaired, because a
    repaired name can point at a different file. Such names cannot be reached through the broker
    at all.
12. Each confined launch makes an AppContainer profile (%LOCALAPPDATA%\Packages\<name>) and HKCU
    mapping keys, outside {app}. The broker removes them after each launch and at the next start,
    but uninstall touches only {app}, so a crash followed by an uninstall leaves them. Decide what
    uninstall does about leftovers.
13. After a crash, staged copies of user files sit under %LOCALAPPDATA%\Packages until the next
    broker start.
14. Confined children get no network and no loopback (this protects the API and Ollama). HTTP MCP
    servers, children that call Ollama or the API, and vool-browser (contracts :41) cannot run
    confined. Decide how VOOL offers them.
15. The stop button must reach the broker. VOOL's stop matchers do not match VoolBroker,
    vool-confine or stage children, which fails safe, but stopping them depends on VOOL stop, then
    the broker (its own Job, Phase B), then the launch Jobs.
16. "Broker or connection lost, effects unknown" from a launch maps to not safe to retry, like
    restore_incomplete. Contracts r9 :99-104 has no such state: TDL follow-up for a contracts-doc
    round at Phase C.
17. No confined child can start until its interpreter and program folder are readable by its
    AppContainer (probe on a Windows 11 build 26200 host: ALL APPLICATION PACKAGES can read
    C:\Windows, including cmd.exe and the .NET Framework folder, but not the host's own .NET SDK
    folder, the broker's bin folder,
    %LOCALAPPDATA%\Packages, or Python and the venv under the user profile). Open question 6
    above (the read roots) is therefore a blocker for wiring any confined child. The launcher's
    build is to prove the mechanism first only with a C# 5 test child built by the Framework's
    csc.exe and staged into the profile. Layouts:
    - A per-machine install under Program Files inherits read and execute for ALL APPLICATION
      PACKAGES, so a bundled Python in {app} would load without new ACEs (checked read-only on a
      Windows 11 host: C:\Program Files grants ALL APPLICATION PACKAGES RX, inherited
      (OI)(CI)(IO)(GR,GE)).
    - A per-user install under %LOCALAPPDATA%, and the source-checkout layout (Python and .venv in
      user folders), need a grant.
    Options: an install-time grant on {app} only, or per-launch ACEs on the
    interpreter folder. Granting anything on user folders outside {app} is off the table unless
    explicitly decided otherwise.
    This item is also an input to Phase B: the bundle layout must not make confining harder.
18. A replace is not atomic, and reads are not serialized with changes. A
    replace is temp, then aside, then place, so between aside and place the destination name does
    not exist, and another program, or a broker read on another connection, gets not_found. VOOL's
    Python writers replace atomically (os.replace). Decide whether that window is acceptable for
    the tools Phase C wires in, or whether reads take the change lock and a replace uses another
    method.
19. The journal folder is used by path after the start checks. A same-user
    process that swaps it could hand restore_batch a crafted journal. Recovery stays contained: it
    only touches objects inside folders flagged now, by file id, and only this batch's own temp and
    aside names. The fix: hold the journal folder's handle, without delete sharing, for the
    broker's life, and make batch files relative to it. Decide at Phase C, together with Phase B's
    install layout.

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
5. None needed for the `core.worktree` case: it is recorded in gap 15 at `637fc6a`.

## 12. Design decisions

- **Authentication once per connection, not per frame.** After the challenge and response, every
  frame on the connection is a request, with no per-frame HMAC: injecting a frame needs the pipe
  handle, and a process that can duplicate that handle could read the secret anyway (section 3).
- **Framed JSON semantics, not canonical bytes.** Client and broker agree on the JSON value, not
  its bytes. Every string field is taken exactly as sent or refused `invalid_name`, never
  repaired (section 3).
- **Unpaired surrogates are refused, never repaired.** A name holding one is refused
  `invalid_name`, and a flagged folder whose real path is not valid Unicode stops the start with
  `invalid_name`, so replies carry only validated names. A repaired name could point at a
  different file.
- **A drive letter is required for a flagged folder.** A volume-GUID or GLOBALROOT spelling is
  fully qualified but is not a form VOOL's settings use, so it is refused (F1).
- **Each connection holds at most 64 roots** (`too_many_roots`), because each root holds a handle
  on every folder on its way.
- **The broker is never looser than the gate.** For the same input it is never looser than G or
  Job 6, and every "stricter" row is listed for Phase C (section 4, section 10).
- **Recovery keeps going past a step it cannot undo.** Every independent step is still undone; a
  temp a later place moved is not counted undone until that place is undone (section 7.4). It
  stops only when it cannot record an undo, and then names the steps not attempted (section 6).
- **A torn last record is not damage.** Each record is flushed before the action it names
  begins, so bytes after the last line end are a record whose action never started. Treating
  them as damage would turn an ordinary power loss into a stuck broker; they are kept in a
  `.torn` file and cut off instead (section 7.4).
- **P16 has no test.** It is unreachable by design; if an earlier refusal is loosened, it needs
  one (section 9.2).
- **Known limitation: cancelling the accept loop.** Stopping `ServeAsync` can leave a blocked
  `ConnectNamedPipe` call behind. Cancellation only happens as the broker exits, and only tests
  host the broker in process.
