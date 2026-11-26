import os

from github import (Github, Auth)
from CheckmarxPythonSDK.CxOne import batch_import_repo

from scm_common import import_new_repos, logger, require_env


def get_github_repos_by_org(organization: str, access_token: str):
    return Github(auth=Auth.Token(access_token)).get_organization(organization).get_repos()


def to_cxone_repo(repo, host_url: str = "https://github.com/") -> dict:
    full_name = repo.html_url.replace(host_url, "")
    return {
        "id": full_name.split("/")[1],
        "fullName": full_name,
        "url": repo.html_url,
        "sshRepoUrl": f"git@github.com:{full_name}.git",
        "defaultBranch": repo.default_branch,
    }


if __name__ == '__main__':
    github_org = require_env("GITHUB_ORG")
    logger.info(f"github organization: {github_org}")
    github_token = require_env("GITHUB_TOKEN")
    cxone_github_auth_code = require_env("CXONE_GITHUB_AUTH_CODE")

    repos = [to_cxone_repo(r) for r in get_github_repos_by_org(github_org, github_token)]
    import_new_repos(
        repos,
        import_fn=batch_import_repo,
        sca_auto_pr_enabled_existing=True,
        origin="GITHUB", organization=github_org,
        is_user=False, is_org_webhook_enabled=False, create_ast_project=True, scan_ast_project=False,
        auth_code=cxone_github_auth_code,
        webhook_enabled=False, pr_decoration_enabled=False, sca_auto_pr_enabled=False,
        chunk_size=200,
        delay_request_timeout=30,
    )
