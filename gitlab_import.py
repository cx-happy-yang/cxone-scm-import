"""Import all projects of a self-hosted GitLab instance (any groups) into CxOne.

Required env vars: GITLAB_URL, GITLAB_TOKEN, CXONE_GITLAB_AUTH_CODE (+ the usual CXONE_* settings).
Optional env vars:
    GITLAB_GROUP_INCLUDE     comma separated path patterns (wildcards ok); only matching projects are imported
    GITLAB_GROUP_EXCLUDE     comma separated path patterns; matching projects are skipped (wins over include)
    GITLAB_GROUP_FILE        file with one include pattern per line (# comments allowed), added to GITLAB_GROUP_INCLUDE
    BATCH_SIZE               repos per import batch (default 200)
    DRY_RUN                  true: only report what would be imported, change nothing
    IMPORT_DEADLINE_HOURS    keep retrying failed repos for this long (default 48)
    CXONE_GITLAB_INSTANCE_NAME, GITLAB_CA_BUNDLE   see README

The import POST completes synchronously: the response carries the final per-repo results
(status, projectId, statusMessageError), so batches run strictly one after another and a
restart simply re-lists and skips whatever already exists in CxOne. No state file needed.
"""
import os
import time
from typing import List, Optional
from urllib.parse import urlparse

import gitlab
from CheckmarxPythonSDK.CxOne import RepoManagerAPI

from scm_common import get_existing_projects, is_permanent_error, is_selected, logger, parse_patterns, require_env, run_batches


# ---------- GitLab access ----------

def get_gitlab_client(gitlab_url: str, access_token: str, ca_bundle: str = None) -> gitlab.Gitlab:
    """ca_bundle: optional path to a CA certificate file, for instances with a self-signed/internal CA."""
    return gitlab.Gitlab(gitlab_url, private_token=access_token, ssl_verify=ca_bundle or True)


def to_cxone_repo(project) -> dict:
    """Map a GitLab project to the repo dict expected by batch_import_repo.

    GitLab repo ids are the numeric project ids; the CxOne project name is the full path with namespace,
    for example group/subgroup/project.
    """
    return {
        "id": str(project.id),
        "fullName": project.path_with_namespace,
        "url": project.web_url,
        "sshRepoUrl": project.ssh_url_to_repo,
        "defaultBranch": project.default_branch,
    }


def list_all_projects(gl: gitlab.Gitlab, attempts: int = 3) -> list:
    """All projects the token can see, in one paginated pass; transient GitLab errors are retried."""
    for attempt in range(1, attempts + 1):
        try:
            return list(gl.projects.list(iterator=True))
        except Exception as e:
            if attempt == attempts or is_permanent_error(e):
                raise
            logger.warning(f"project listing failed ({type(e).__name__}: {e}), retry {attempt}/{attempts - 1}")
            time.sleep(30)
    return []  # unreachable


# ---------- CxOne SCM ----------

def _norm(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def register_self_hosted_gitlab(api: RepoManagerAPI, gitlab_url: str, instance_name: str = None) -> None:
    """Point the SDK's "GITLAB" origin at the tenant's SCM type of this GitLab instance.

    The SDK hardcodes GITLAB to SCM id 2 (gitlab.com); a self-hosted instance has its own SCM id.
    instance_name (CXONE_GITLAB_INSTANCE_NAME) selects the SCM by its instance name in CxOne. Without it, a GitLab SCM
    matches when its base url equals gitlab_url (host + path) or its instance name equals the host of gitlab_url;
    exactly one must match.
    """
    scm_types = [s for s in api.get_all_scm_types_v2() if "gitlab" in (s.type or "").lower()]
    if instance_name:
        matches = [s for s in scm_types if s.instanceName == instance_name]
    else:
        target = _norm(gitlab_url)
        host = urlparse(gitlab_url).netloc.lower()
        matches = [
            s for s in scm_types
            if any(u and _norm(u) == target for u in (s.repoBaseUrl, s.authBaseUrl))
            or (s.instanceName or "").lower() == host
        ]
    if len(matches) != 1:
        available = [(s.id, s.instanceName, s.repoBaseUrl) for s in scm_types]
        raise SystemExit(
            f"{len(matches)} GitLab SCMs match; set CXONE_GITLAB_INSTANCE_NAME to one of: {available}"
        )
    logger.info(f"using GitLab SCM {matches[0].instanceName} (id {matches[0].id})")
    api.origin_dict["GITLAB"] = matches[0].id


# ---------- main flow ----------

def import_batches(api, gl, include, exclude, auth_code, dry_run, batch_size, deadline_hours,
                   scan_after_import=False) -> int:
    """List everything once, then import new repos in synchronous batches of batch_size.

    Returns the process exit code: 0 all imported, 1 deadline reached / unexpected error, 2 permanent error.
    """
    # -- discovery (one paginated pass over all projects, then filter) --
    projects = list_all_projects(gl)
    repos = []
    for p in projects:
        path = p.path_with_namespace
        if getattr(p, "archived", False):
            logger.info(f"skip archived project {path}")
        elif getattr(p, "empty_repo", False) or not p.default_branch:
            logger.info(f"skip empty project {path} (no commits yet)")
        elif not is_selected(path, include, exclude):
            logger.info(f"skip project {path} (excluded by include/exclude filter)")
        else:
            repos.append(to_cxone_repo(p))
    logger.info(f"{len(projects)} project(s) on GitLab, {len(repos)} importable after filters")

    existing = get_existing_projects()
    pending = [r for r in repos if r["fullName"] not in existing]
    logger.info(f"{len(pending)} new repo(s) to import "
                f"in {max(1, -(-len(pending) // batch_size))} batch(es) of {batch_size}")

    if dry_run:
        logger.info("DRY_RUN: nothing is imported")
        for r in pending:
            logger.info(f"  would import {r['fullName']}")
        return 0

    def submit(batch: List[dict]) -> List[dict]:
        """Import one batch; return the repos that still failed (the POST response has per-repo results)."""
        organization = batch[0]["fullName"].split("/")[0]
        results = api.batch_import_repo(
            repos=batch,
            origin="GITLAB",
            organization=organization,
            auth_code=auth_code,
            existing_project_names=existing.keys(),
            is_user=False,
            is_org_webhook_enabled=False,
            create_ast_project=True,
            scan_ast_project=scan_after_import,
            webhook_enabled=False,
            pr_decoration_enabled=False,
            sca_auto_pr_enabled=False,
            chunk_size=batch_size,
            delay_request_timeout=10,
        )
        # results is None only when the server answered async and the SDK polled the job instead
        if results is None:
            existing.update({r["fullName"]: None for r in batch})
            return []
        by_name = {r.get("name"): r for r in results}
        failed = []
        for repo in batch:
            name = repo["fullName"]
            res = by_name.get(name) or {}
            error = res.get("statusMessageError") or ""
            if res.get("status") == "SUCCESS" or "already imported" in error:
                existing[name] = None
            else:
                logger.warning(f"{name}: {res.get('status')} - {error}")
                failed.append(repo)
        return failed

    return run_batches(pending, batch_size, submit, existing, deadline_hours * 3600, description="repo")


if __name__ == '__main__':
    gitlab_url = require_env("GITLAB_URL")
    gitlab_token = require_env("GITLAB_TOKEN")
    auth_code = require_env("CXONE_GITLAB_AUTH_CODE")
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    batch_size = int(os.getenv("BATCH_SIZE", "200"))
    deadline_hours = float(os.getenv("IMPORT_DEADLINE_HOURS", "48"))
    scan_after_import = os.getenv("SCAN_AFTER_IMPORT", "").lower() in ("1", "true", "yes")
    include = parse_patterns(os.getenv("GITLAB_GROUP_INCLUDE"), os.getenv("GITLAB_GROUP_FILE"))
    exclude = parse_patterns(os.getenv("GITLAB_GROUP_EXCLUDE"))

    api = RepoManagerAPI()
    register_self_hosted_gitlab(api, gitlab_url, os.getenv("CXONE_GITLAB_INSTANCE_NAME"))
    gl = get_gitlab_client(gitlab_url, gitlab_token, os.getenv("GITLAB_CA_BUNDLE"))

    try:
        code = import_batches(api, gl, include, exclude, auth_code, dry_run, batch_size, deadline_hours,
                              scan_after_import)
    except Exception as e:
        logger.exception(f"unexpected error: {e}")
        code = 2 if is_permanent_error(e) else 1
    raise SystemExit(code)
