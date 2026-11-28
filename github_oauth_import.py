"""Import all repos of a GitHub organization into CxOne via the OAuth app.

Required env vars: GITHUB_ORG, GITHUB_TOKEN, CXONE_GITHUB_AUTH_CODE (+ the usual CXONE_* settings).
Optional env vars:
    GITHUB_REPO_INCLUDE      comma separated "org/repo" patterns (wildcards ok); only matching repos are imported
    GITHUB_REPO_EXCLUDE      comma separated "org/repo" patterns; matching repos are skipped (wins over include)
    GITHUB_REPO_FILE         file with one include pattern per line (# comments allowed), added to GITHUB_REPO_INCLUDE
    BATCH_SIZE               repos per import batch (default 200)
    DRY_RUN                  true: only report what would be imported, change nothing
    IMPORT_DEADLINE_HOURS    keep retrying failed repos for this long (default 48)

GitHub's asyncImport runs asynchronously: the SDK waits for each round's job to reach 100% before
submitting the next, so batches run strictly one after another. Archived repos and repos without
commits yet are skipped. A restart is safe without any state file: the script re-lists the org and
skips whatever already exists in CxOne.
"""
import os
import time

from CheckmarxPythonSDK.CxOne import RepoManagerAPI

from scm_common import (collect_github_repos, get_existing_projects, get_github_repos_by_org,
                        import_new_repos, is_permanent_error, logger, parse_patterns,
                        require_env, run_batches, split_new_and_existing)


def run() -> int:
    github_org = require_env("GITHUB_ORG")
    logger.info(f"github organization: {github_org}")
    github_token = require_env("GITHUB_TOKEN")
    auth_code = require_env("CXONE_GITHUB_AUTH_CODE")
    api = RepoManagerAPI()  # one shared client for all batches (token + connections reused)
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    batch_size = int(os.getenv("BATCH_SIZE", "200"))
    deadline_hours = float(os.getenv("IMPORT_DEADLINE_HOURS", "48"))
    scan_after_import = os.getenv("SCAN_AFTER_IMPORT", "").lower() in ("1", "true", "yes")
    include = parse_patterns(os.getenv("GITHUB_REPO_INCLUDE"), os.getenv("GITHUB_REPO_FILE"))
    exclude = parse_patterns(os.getenv("GITHUB_REPO_EXCLUDE"))

    # -- discovery with a small retry for transient GitHub errors --
    repos = None
    for attempt in range(1, 4):
        try:
            repos = collect_github_repos(get_github_repos_by_org(github_org, github_token), include, exclude)
            break
        except Exception as e:
            if is_permanent_error(e) or attempt == 3:
                raise
            logger.warning(f"github repo listing failed ({type(e).__name__}: {e}), retry {attempt}/2")
            time.sleep(30)
    logger.info(f"{len(repos)} repo(s) in {github_org} after filters")

    existing = get_existing_projects()
    if dry_run:
        new = [r for r in repos if r["fullName"] not in existing]
        logger.info(f"DRY_RUN: nothing is imported, {len(new)} new repo(s) in {batch_size}-repo batches")
        for r in new:
            logger.info(f"  would import {r['fullName']}")
        return 0
    # refresh the settings of repos already in CxOne (oauth: force scaAutoPrEnabled on), keep the new ones
    new = split_new_and_existing(repos, update_existing=True, sca_auto_pr_enabled=True, existing=existing)
    logger.info(f"{len(new)} new repo(s) to import in {max(1, -(-len(new) // batch_size))} batch(es) of {batch_size}")

    def submit(batch):
        import_new_repos(
            batch,
            import_fn=api.batch_import_repo,
            update_existing=False,
            existing=existing,
            origin="GITHUB", organization=github_org,
            is_user=False, is_org_webhook_enabled=False, create_ast_project=True,
            scan_ast_project=scan_after_import,
            auth_code=auth_code,
            webhook_enabled=False, pr_decoration_enabled=False, sca_auto_pr_enabled=False,
            chunk_size=batch_size,
            delay_request_timeout=30,
        )
        return []

    return run_batches(new, batch_size, submit, existing, deadline_hours * 3600, description="repo")


if __name__ == '__main__':
    try:
        code = run()
    except Exception as e:
        logger.exception(f"unexpected error: {e}")
        code = 2 if is_permanent_error(e) else 1
    raise SystemExit(code)
