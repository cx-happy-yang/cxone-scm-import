"""Import all projects of a self-hosted GitLab group (including subgroups) into CxOne.

Env vars: GITLAB_URL, GITLAB_TOKEN, GITLAB_GROUP, CXONE_GITLAB_AUTH_CODE (+ the usual CXONE_* settings).
"""
from typing import List
from urllib.parse import urlparse

import gitlab
from CheckmarxPythonSDK.CxOne import RepoManagerAPI

from scm_common import import_new_repos, logger, require_env


def get_gitlab_projects_by_group(gitlab_url: str, access_token: str, group: str):
    gl = gitlab.Gitlab(gitlab_url, private_token=access_token)
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


def register_self_hosted_gitlab(api: RepoManagerAPI, gitlab_url: str) -> None:
    """Point the SDK's "GITLAB" origin at the tenant's SCM type of this GitLab instance.

    The SDK hardcodes GITLAB to SCM id 2 (gitlab.com); a self-hosted instance has its own SCM id.
    """
    host = urlparse(gitlab_url).netloc.lower()
    scm_types = api.get_all_scm_types_v2()
    for scm in scm_types:
        urls = [u for u in (scm.repoBaseUrl, scm.apiBaseUrl, scm.authBaseUrl) if u]
        if "gitlab" in (scm.type or "").lower() and any(urlparse(u).netloc.lower() == host for u in urls):
            logger.info(f"using SCM type id {scm.id} ({scm.displayName or scm.instanceName}) for {host}")
            api.origin_dict["GITLAB"] = scm.id
            return
    available = [(s.id, s.type, s.displayName, s.repoBaseUrl) for s in scm_types]
    raise SystemExit(f"No GitLab SCM type in CxOne matches host {host}. Available: {available}")


if __name__ == '__main__':
    gitlab_url = require_env("GITLAB_URL")
    gitlab_token = require_env("GITLAB_TOKEN")
    gitlab_group = require_env("GITLAB_GROUP")
    cxone_gitlab_auth_code = require_env("CXONE_GITLAB_AUTH_CODE")
    logger.info(f"gitlab {gitlab_url}, group: {gitlab_group}")

    api = RepoManagerAPI()
    register_self_hosted_gitlab(api, gitlab_url)

    repos = collect_repos(get_gitlab_projects_by_group(gitlab_url, gitlab_token, gitlab_group))
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
