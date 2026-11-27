"""Import the projects of self-hosted GitLab groups (including subgroups) into CxOne.

Required env vars: GITLAB_URL, GITLAB_TOKEN, CXONE_GITLAB_AUTH_CODE (+ the usual CXONE_* settings).
Optional env vars:
    GITLAB_GROUP             comma separated top-level group paths or ids; default: every top-level group the token sees
    GITLAB_GROUP_INCLUDE     comma separated path patterns (wildcards ok); only matching groups/projects are imported
    GITLAB_GROUP_EXCLUDE     comma separated path patterns; matching groups/projects are skipped (wins over include)
    GITLAB_GROUP_FILE        file with one include pattern per line (# comments allowed), added to GITLAB_GROUP_INCLUDE
    DRY_RUN                  true: only report what would be imported, change nothing
    IMPORT_STATE_FILE        resume/retry state file (default ./gitlab_import_state.json)
    IMPORT_DEADLINE_HOURS    keep retrying failed groups for this long (default 48)
    CXONE_GITLAB_INSTANCE_NAME, GITLAB_CA_BUNDLE   see README
"""
import os
import time
from fnmatch import fnmatchcase
from typing import Callable, List, Optional
from urllib.parse import urlparse

import gitlab
from CheckmarxPythonSDK.CxOne import RepoManagerAPI

from import_state import ImportState, PermanentImportError, run_until_done
from scm_common import get_existing_projects, import_new_repos, logger, require_env


# ---------- GitLab access ----------

def get_gitlab_client(gitlab_url: str, access_token: str, ca_bundle: str = None) -> gitlab.Gitlab:
    """ca_bundle: optional path to a CA certificate file, for instances with a self-signed/internal CA."""
    return gitlab.Gitlab(gitlab_url, private_token=access_token, ssl_verify=ca_bundle or True)


def get_all_top_level_groups(gl: gitlab.Gitlab) -> List[str]:
    """Full paths of all top-level groups the token can see (their subgroups come with include_subgroups)."""
    return [group.full_path for group in gl.groups.list(top_level_only=True, iterator=True)]


def get_gitlab_projects_by_group(gl: gitlab.Gitlab, group: str):
    return gl.groups.get(group).projects.list(include_subgroups=True, iterator=True)


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


# ---------- include / exclude filters ----------

def parse_patterns(value: Optional[str] = None, file_path: Optional[str] = None) -> List[str]:
    patterns = [p.strip() for p in (value or "").split(",") if p.strip()]
    if file_path:
        with open(file_path, encoding="utf-8") as f:
            patterns += [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    return patterns


def _matches(path: str, pattern: str) -> bool:
    """True if pattern matches the path or one of its ancestor groups (so "team" covers "team/sub/repo")."""
    parts = path.split("/")
    return any(fnmatchcase("/".join(parts[:i]), pattern) for i in range(1, len(parts) + 1))


def is_selected(path: str, include: List[str], exclude: List[str]) -> bool:
    if any(_matches(path, p) for p in exclude):
        return False
    return not include or any(_matches(path, p) for p in include)


def group_may_match(group: str, include: List[str], exclude: List[str]) -> bool:
    """Cheap pre-check on a top-level group, so whole groups can be skipped without listing their projects."""
    if any(fnmatchcase(group, p) for p in exclude):
        return False
    # an include like "team/sub/*" must still open group "team"; compare against the pattern's first segment
    return not include or any(fnmatchcase(group, p.split("/")[0]) for p in include)


def collect_repos(projects, selected: Callable[[str], bool] = lambda path: True) -> List[dict]:
    repos = []
    for project in projects:
        path = project.path_with_namespace
        if getattr(project, "archived", False):
            logger.info(f"skip archived project {path}")
        elif getattr(project, "empty_repo", False) or not project.default_branch:
            logger.info(f"skip empty project {path} (no commits yet)")
        elif not selected(path):
            logger.info(f"skip project {path} (excluded by include/exclude filter)")
        else:
            repos.append(to_cxone_repo(project))
    return repos


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


# ---------- errors ----------

def is_permanent_error(exc: Exception) -> bool:
    """Errors that retrying cannot fix: bad/expired credentials. Stop and let the user fix them, state is kept."""
    if isinstance(exc, gitlab.exceptions.GitlabAuthenticationError):
        return True
    text = str(exc)
    return any(s in text for s in ("Failed to load access token", "HttpStatusCode: 401", "HttpStatusCode: 403"))


# ---------- main flow ----------

def wait_for_cxone_idle(api: RepoManagerAPI, max_wait: float = 7200, interval: float = 30,
                        sleep=time.sleep, now=time.monotonic) -> None:
    """Block until the CxOne import job reports 100%, so a restart never submits a group that is still importing."""
    deadline = now() + max_wait
    while True:
        try:
            percentage = api.get_job_status()
            logger.info(f"cxone import job status: {percentage}%")
            if percentage is not None and percentage >= 100:
                return
        except Exception as e:
            logger.info(f"cannot read cxone job status ({type(e).__name__}), will check again")
        if now() >= deadline:
            logger.warning(f"cxone import job did not report 100% within {max_wait}s, continuing anyway")
            return
        sleep(interval)


def select_groups(gl, explicit: List[str], include: List[str], exclude: List[str]) -> List[str]:
    groups = explicit or get_all_top_level_groups(gl)
    kept = [g for g in groups if group_may_match(g, include, exclude)]
    logger.info(f"{len(groups)} top-level group(s), {len(kept)} after include/exclude filter")
    return kept


def import_group(gl, api, group, existing, include, exclude, auth_code, dry_run=False) -> dict:
    repos = collect_repos(
        get_gitlab_projects_by_group(gl, group), selected=lambda path: is_selected(path, include, exclude)
    )
    new = [r for r in repos if r["fullName"] not in existing]
    logger.info(f"group {group}: {len(repos)} importable project(s), {len(new)} new")
    if dry_run:
        for r in new:
            logger.info(f"  would import {r['fullName']}")
        return {"importable": len(repos), "new": len(new)}
    import_new_repos(
        repos,
        import_fn=api.batch_import_repo,
        update_existing=False,
        existing=existing,
        origin="GITLAB",
        organization=group,
        is_user=False,
        is_org_webhook_enabled=False,
        create_ast_project=True,
        scan_ast_project=False,
        auth_code=auth_code,
        webhook_enabled=False,
        pr_decoration_enabled=False,
        sca_auto_pr_enabled=False,
        chunk_size=200,
        delay_request_timeout=30,
    )
    existing.update({r["fullName"]: None for r in new})
    return {"importable": len(repos), "new": len(new)}


if __name__ == '__main__':
    gitlab_url = require_env("GITLAB_URL")
    gitlab_token = require_env("GITLAB_TOKEN")
    auth_code = require_env("CXONE_GITLAB_AUTH_CODE")
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    state_file = os.getenv("IMPORT_STATE_FILE", "./gitlab_import_state.json")
    deadline_hours = float(os.getenv("IMPORT_DEADLINE_HOURS", "48"))
    include = parse_patterns(os.getenv("GITLAB_GROUP_INCLUDE"), os.getenv("GITLAB_GROUP_FILE"))
    exclude = parse_patterns(os.getenv("GITLAB_GROUP_EXCLUDE"))
    explicit_groups = parse_patterns(os.getenv("GITLAB_GROUP"))

    api = RepoManagerAPI()
    register_self_hosted_gitlab(api, gitlab_url, os.getenv("CXONE_GITLAB_INSTANCE_NAME"))
    gl = get_gitlab_client(gitlab_url, gitlab_token, os.getenv("GITLAB_CA_BUNDLE"))
    groups = select_groups(gl, explicit_groups, include, exclude)
    existing = get_existing_projects()  # loaded once, not per group

    def process(group: str) -> dict:
        return import_group(gl, api, group, existing, include, exclude, auth_code, dry_run=dry_run)

    if dry_run:
        logger.info("DRY_RUN: nothing is imported and the state file is not touched")
        totals = {"importable": 0, "new": 0}
        for group in groups:
            for k, v in process(group).items():
                totals[k] += v
        logger.info(f"DRY_RUN summary: {len(groups)} group(s), {totals['importable']} importable project(s), "
                    f"{totals['new']} would be imported")
        raise SystemExit(0)

    state = ImportState(state_file)
    try:
        finished = run_until_done(
            groups, process, state, deadline_seconds=deadline_hours * 3600, is_permanent=is_permanent_error,
            on_interrupted=lambda group: wait_for_cxone_idle(api),
        )
    except PermanentImportError as e:
        logger.error(f"stopped, fix the cause and run again to resume (state: {state_file}): {e}")
        raise SystemExit(2)
    summary = state.summary(groups)
    logger.info(f"finished: {summary}")
    if not finished:
        unfinished = state.unfinished(groups)
        logger.error(f"deadline reached, {len(unfinished)} group(s) not done, see {state_file}: {unfinished}")
        raise SystemExit(1)
