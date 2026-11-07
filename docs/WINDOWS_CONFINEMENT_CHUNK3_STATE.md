# Windows confinement B, chunk 3 (runner): parked state, 4 Nov 2026

Jesse paused all option-B work on 4 Nov 2026 to save usage. Nothing from B is in this release.
This note is for whoever picks it up.

## Where it stands
- Branch windows/port-confine-3-runner, on top of windows/port-confine-0-interface merged up to 3a0274bc.
- No PR. Nothing here is proven on Windows; the launcher (chunk 1) and the ACL (chunk 2) aren't finished.
- Production and test changes are in separate commits:
  - prod: 72675c19, 0b86ac1d, b2dc46e8
  - tests: a8ed83ca, 454ef7eb, 61ab0c76 (one new file, tests/test_windows_confinement_runner.py)
- Linux evidence:
  - 117/117 interface and runner tests pass at 61ab0c76.
  - 19 mutation checks: each one turns at least one new test red.
  - At 454ef7eb, about 130 sandbox/runner/Windows test files gave 32 failed and 1598 passed. The base gave 32 failed and 1549 passed, and the failing sets were identical: the cloud container has no usable bwrap.
  - The regression rerun at 61ab0c76 was stopped when the work was paused, so it has no result.

## Reviews
- At 454ef7eb, 🔐 (owner) and 📝 both said RED for R1: a job that plants a repository and then hangs or is cancelled is never scanned.
  The reviews are in project files: jobs/broker/confinement-B/reviews/REVIEW-chunk3-*.md.
- b2dc46e8 is the R1 fix. Nobody has re-reviewed it yet. It does two things:
  - When the status has no repositories_scanned (a timeout, a cancel, a crash or no record), the runner runs `VoolConfine.exe scan`. It skips the scan only when the record says the child never started.
  - The runner also does its own before-and-after walk of the write roots on every outcome. This is extra on top of R1. 📝 said a Python scan isn't needed, so drop it if the owner prefers.
- The plant-then-timeout, plant-then-cancel and plant-then-crash tests are red on 454ef7eb and green on 61ab0c76.

## To pick up
1. Rerun the Linux regression at the branch head against 3a0274bc, then send the SHA to 🔐 and 📝 for an R1 re-review.
2. Merge any newer windows/port-confine-0-interface (merge, don't rebase).
3. Once chunks 1 and 2 build, check these on Steve:
   - the existing confinement tests turn green, with the suite counts;
   - a launcher can join VOOL's job while VOOL itself is in the supervisor's job;
   - Python starts inside the AppContainer with the env the runner builds;
   - CREATE_NO_WINDOW against builder A's "Access is denied" on nested creates;
   - the chunk 6 acceptance test (7440f083).
4. Phase 2: toolchains other than Python and pytest (git, npm and the rest) are refused until each one is proven.
