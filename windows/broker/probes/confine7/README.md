# Chunk 7 probe: a hand-made AppContainer on Steve, before chunk 1 has a build

`confine7_probe.py` runs git, node/npm, pip/python and cargo inside an AppContainer + Job Object with the same shape as docs/WINDOWS_CONFINEMENT.md (branch windows/port-confine-0-interface, d5f85973). The probes are P0-P8 in ../compat-matrix.md section 5.

How it matches chunk 0:
- The package SID gets read grants and deny ACEs.
- Write access goes to a per-root capability SID. It uses the same normalisation as `root_capability_name`; checked against the vector `C:\work\a` → b144b923.
- The network capability is internetClient only, and only in the `-inet` variants. Loopback is never granted.
- The child is created suspended, put in the job, then resumed. The job has KILL_ON_JOB_CLOSE.

How it is different, on purpose:
- Its own names: profile `VOOL.Confine.Probe7` and capabilities `vool.probe7.root.*`. So no ACE it adds can ever apply to a real VOOL job.
- No profile is registered unless `--create-profile` is given. Registering one writes HKCU.
- The job caps are high (8192 MB, 512 processes), so it **measures** peak memory and process count instead of tripping the caps.

Safety:
- It writes ACEs only on folders under `--base` (the VOOL Windows folder), or on a folder named with `--allow-grant`, and removes every one of them in `finally`.
- It installs nothing.
- It never reads a credential file's contents (P8 reports only whether open() succeeded).
- It doesn't run `git credential`.

Checked in the cloud: `py_compile` and `ruff --select F,E9` pass. It has **not been run on Windows yet**. The ctypes structure layouts are only proven by a Steve run.

Run, unconfined, in a VOOL Windows folder on Steve. Jesse OK'd the two read grants (coordinator relay of HQ m-1a1a81c48b8-00a, 4 Nov):

    python confine7_probe.py --base "C:\Users\Satoshi\Documents\VOOL Windows" ^
      --root "C:\Users\Satoshi\Documents\VOOL Windows\missions\confine7-probe" ^
      --python "<a python.exe under the VOOL Windows folder, e.g. a worktree .venv\Scripts\python.exe>" ^
      --allow-grant "<that venv's base Python folder (sys.base_prefix)>" ^
      --allow-grant "C:\Users\Satoshi\.rustup\toolchains\stable-x86_64-pc-windows-msvc" ^
      --timeout 20

Time limits:
- `--timeout` is per probe (default 20 s, max 60).
- `--budget` covers all probes together (default 480 s, max 600). Once it is used up, later probes are skipped.
- So the revoke in `finally` runs well inside an outer 20-minute kill.

Recovery and checks:
- Every ACE is journalled to `<root>\out\grants-journal.json` **before** icacls runs. If the run is killed anyway, run the same command with `--revoke-only`. It checks the journal's SIDs against the probe's own derived SIDs, then removes them and confirms with icacls that nothing is left.
- The journal is deleted only once the FULL `icacls <path> /T /C` listing of every listed path (every file and folder below it) exits 0 and names neither probe SID nor the profile. The 600-character tail is only for the log. Otherwise an `R-check` line says ACEs may remain.

Grant times are not chunk 2's numbers. `icacls /grant (OI)(CI)` rewrites the ACL of every existing file below the folder. Chunk 2 writes by handle and never propagates like that.

`--revoke-only` touches only journal paths under `--base` or an `--allow-grant`, so pass the same flags as the run. Anything else is skipped and reported, and the journal is kept.

Never granted, even if named with `--allow-grant`:
- `~\.cargo` (cargo is run as the toolchain cargo.exe from `rustup which cargo`; the `.cargo\bin` proxy is only probed, never granted);
- `~\.ssh`, `.aws`, `.gnupg`, `.config`, `.docker`, `.vool_runtime`;
- `%APPDATA%`;
- any folder at or above the profile, `%APPDATA%` or `%LOCALAPPDATA%`.

Optional, with a further Jesse OK only: `--create-profile`. It writes HKCU and is deleted at the end.

Results land in `<root>\out\results.jsonl` and `SUMMARY.md`. Copy both back to `jobs/broker/confinement-B/chunk7-probe/run-<date>/`.
