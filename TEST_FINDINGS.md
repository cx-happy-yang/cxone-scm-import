# GitLab import test run — findings (2026-11-27/28, tenant "happy")

## Final result (2026-11-28)

The old per-group design was replaced by a batched pipeline (see MAJOR finding 2): list all
GitLab projects in one pass → dedupe against CxOne → sequential batches of 200 repos → the
import POST completes synchronously and its response carries the per-repo results; failures are
retried with backoff. No state file needed (restart = re-list + skip what exists).

End-to-end validation: the remaining 334 repos imported in 2 batches, 52 s of POST time,
0 failures, exit 0. **All 1000 scale-test repos are in CxOne.** Estimated customer scale
(~20k repos): ~100 batches ≈ under an hour of POST time plus the one-pass GitLab listing.

SDK change committed on `release/1.9.5` (00bde39): `batch_import_repo` parses the import
response; when it carries `repoConfigResultList` the round is done synchronously (no job-status
polling), otherwise the existing polling path is kept (GitHub asyncImport).

## Test setup

## Bugs found (environment) — fixed

1. **python-gitlab 4.5.0 is broken on Python 3.14** (`AttributeError: 'Group' object has no
   attribute '__annotations__'` — PEP 649 changed `__annotations__` semantics; every
   `iterator=True` listing crashed). Fixed: 8.6.0. Committed in requirements.txt (staged — see note).
2. **PyGithub 1.58.0 has no `github.Auth`** → `scm_common.py` crashed at import. Fixed: 2.5.0.
3. PyPI downloads from this machine time out on larger wheels (SSL EOF on `cryptography`) —
   installs need a mirror: `-i https://pypi.tuna.tsinghua.edu.cn/simple`.

## MAJOR finding: job-status stream is blind for GitLab imports

`GET /api/ssegateway/job-status` **never reports anything about GitLab `/repos` import jobs**
on this tenant. Evidence (logs `sse_probe.log`, `sse_raw_probe2.log`):

- Stream replays the last event ever published — an old **GitHub** import
  (`happy-cook/happy-cx-one-GitHub-app`, `currentPhase: DONE, percentage: 100.0,
  jobRunning: false`) — and stays byte-identical while GitLab jobs run and complete.
- Probe1: ~80 polls over 5.5 min across ≥4 jobs → 0 changes, 0 `jobRunning: true`.
- Probe2: 59 polls over 3.7 min across 3 complete job cycles → byte-identical every time.

Consequences:

1. **SDK cd82e8d's `saw_job_start` can never become true on this path**, so every round exits
   via `settle_timeout` (60 s). Observed: ~66 s per 1-repo group ≈ ~17 h of pure dead time
   over the 1003-group run.
2. **Worse: a round that genuinely takes > 60 s breaks early** (stale 100 already shows),
   the next group's POST hits a running job → conflict → group fails and backoff-retries.
   For the customer's real repos (jobs can take minutes) this recreates the original
   "second job always fails" failure cascade, damped only by the backoff machinery.
3. `gitlab_import.py`'s `wait_for_cxone_idle` guard is also blind here (always sees the stale
   100/DONE and returns instantly). The 5-min-doubling retry backoff is the only real
   protection on this path.

## MAJOR finding 2 (2026-11-28): the GitLab POST is synchronous and returns the results

Tested by stopping the run and POSTing 3 repos from 3 different groups under one `organization`:

- **Cross-group pooling WORKS**: all repos in the batch are imported regardless of their GitLab group
  (twice verified, 6 projects created, all `status: SUCCESS` with projectIds).
- **The POST response IS the completion signal**: `POST .../orgs/{org}/repos` returns 200 in ~2.5 s
  (3 repos) with the final results in the body: `orgConfigResult.status` + `repoConfigResultList`
  (per repo: name, repoId, projectId, status SUCCESS/FAILED, statusMessageError, overriddenSettings).
  When the POST returns, the job is DONE — there is no async job, which explains why the SSE
  job-status stream never reports GitLab imports (MAJOR finding 1 is explained by this).
- Consequence: on the GitLab path the SDK's poll loop (`get_job_status_details` + `saw_job_start` +
  `settle_timeout` 60 s) is unnecessary — parse the response and return. The 60 s/round dead time
  disappears entirely. (GitHub `asyncImport` remains async and keeps the polling path.)
- New speed picture: list all 1005 projects in one GitLab pass (~32 s) + ~6 pooled POSTs of 200 →
  the whole 1003-group import could run in **minutes**, with GitLab listing as the only real cost.
- Open question: does the server stay synchronous for large batches (200 repos)? Verify with one
  200-repo POST before relying on it for the customer run.

## Recommendation for the SDK fix (hand to the SDK session)

The stale-100 fix (jobRunning signal) works for the GitHub path but the GitLab path needs a
different completion signal. Options, best first:

1. **Capture the `/api/repos-manager/scms/{id}/orgs/{org}/repos` POST response** — the SDK
   currently ignores it. If it carries a job id/status, poll per-job instead of the global stream.
2. Check what endpoint the CxOne portal UI polls during a manual GitLab import (network tab) —
   the UI must get job status from somewhere.
3. Fallback: after the POST, poll the CxOne project list until all round repos appear
   (exact, but one paginated listing per round — acceptable at 200-repo rounds).
4. Ask CxOne support for the job-status contract of the GitLab `/repos` endpoint.

For THIS test run: settle_timeout left at 60 s (zero failures observed); tuning it down is
unsafe without a real job-duration signal.

## Other notes

- **Design observation (import_state.run_until_done) — FIXED in working tree, not yet in the running
  process:** the `due` list was snapshotted once per while-pass, so a group failing early was NOT
  retried after its 300 s backoff — its retry waited for the whole first pass to drain (~17 h for
  1003 groups); only the final retry phase was paced by the backoff. Fixed: one unit per pass with
  `due` recomputed, so a failed unit is retried as soon as its retry time is due (tested: order
  `[a, b, a, c]` where a fails, b runs long, c pending). Takes effect on the next run; the current
  run still has the old code, so `scale-test-0032` will be retried in its final pass — fine under
  the 240 h deadline.
- **First run failure (23:46): `scale-test-0032`** — `MaxRetryError ... /api/v4/groups/scale-test-0032
  (Caused by ProxyError('Cannot connect to proxy.', RemoteDisconnected(...)))`. `HTTPS_PROXY`/`ALL_PROXY`
  are set system-wide with no `NO_PROXY`, so even the local `gitlab.happy.local` traffic goes through the
  proxy; one proxy hiccup failed the group. The retry machinery handled it as designed (retry in 300 s,
  run continued with the next group). Recommendation: set `NO_PROXY=gitlab.happy.local` (and any other
  internal hosts) for future runs to avoid this failure class — unless the proxy is the only route.
- **Security (SDK):** `--cx_debug` makes the SDK dump the full config at DEBUG —
  `client_secret`, username, **password** and the full **refresh_token** in plaintext
  (11× at startup). The config-dump debug lines should mask secrets. The run log is
  gitignored (`*.log`), but treat `gitlab_import_run.log` as containing live credentials.
- Minor: `config.ini` has `logging_level: DEUBG` (typo) — debug via the config file never
  worked; `--cx_debug` is required.
- Staged but uncommitted (GPG sign timed out overnight): requirements.txt dep fixes +
  .gitignore `*.log`. Commit when back.
