"""Import all projects of a self-hosted GitLab group (including subgroups) into CxOne.

Env vars: GITLAB_URL, GITLAB_TOKEN, GITLAB_GROUP, CXONE_GITLAB_AUTH_CODE, optional GITLAB_CA_BUNDLE and CXONE_GITLAB_INSTANCE_NAME (+ the usual CXONE_* settings).
"""
import os
from typing import List
from urllib.parse import urlparse

import gitlab
from CheckmarxPythonSDK.CxOne import RepoManagerAPI

from scm_common import import_new_repos, logger, require_env


def get_gitlab_projects_by_group(gitlab_url: str, access_token: str, group: str, ca_bundle: str = None):
    """ca_bundle: optional path to a CA certificate file, for instances with a self-signed/internal CA."""
    gl = gitlab.Gitlab(gitlab_url, private_token=access_token, ssl_verify=ca_bundle or True)
    return gl.groups.get(group).projects.list(include_subgroups=True, iterator=True)


def to_cxone_repo(project) -> dict:
    """Map a GitLab project to the repo dict expected by batch_import_repo.

    GitLab repo ids are the numeric project ids; the CxOne project name is the full path with namespace.
    """
    return {
        "id": str(project.id),
        "fullName": project.path_with_namespace,
        "url": project.web_url,
        "sshRepoUrl": project.ssh_url_to_repo,
        "defaultBranch": project.default_branch,
    }


def collect_repos(projects) -> List[dict]:
    repos = []
    for project in projects:
        if getattr(project, "archived", False):
            logger.info(f"skip archived project {project.path_with_namespace}")
        elif not project.default_branch:
            logger.info(f"skip empty project {project.path_with_namespace} (no default branch)")
        else:
            repos.append(to_cxone_repo(project))
    return repos


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


if __name__ == '__main__':
    gitlab_url = require_env("GITLAB_URL")
    gitlab_token = require_env("GITLAB_TOKEN")
    gitlab_group = require_env("GITLAB_GROUP")
    cxone_gitlab_auth_code = require_env("CXONE_GITLAB_AUTH_CODE")
    logger.info(f"gitlab {gitlab_url}, group: {gitlab_group}")

    api = RepoManagerAPI()
    register_self_hosted_gitlab(api, gitlab_url, os.getenv("CXONE_GITLAB_INSTANCE_NAME"))

    repos = collect_repos(get_gitlab_projects_by_group(
        gitlab_url, gitlab_token, gitlab_group, ca_bundle=os.getenv("GITLAB_CA_BUNDLE")))
    import_new_repos(
        repos,
        import_fn=api.batch_import_repo,
        origin="GITLAB",
        organization=gitlab_group,
        is_user=False,
        is_org_webhook_enabled=False,
        create_ast_project=True,
        scan_ast_project=False,
        auth_code=cxone_gitlab_auth_code,
        webhook_enabled=False,
        pr_decoration_enabled=False,
        sca_auto_pr_enabled=False,
        chunk_size=200,
        delay_request_timeout=30,
    )
