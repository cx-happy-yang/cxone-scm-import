from CheckmarxPythonSDK.CxOne import batch_import_repo

from scm_common import get_github_repos_by_org, github_repo_to_cxone_repo, import_new_repos, logger, require_env


if __name__ == '__main__':
    github_org = require_env("GITHUB_ORG")
    logger.info(f"github organization: {github_org}")
    github_token = require_env("GITHUB_TOKEN")
    cxone_github_app_auth_code = require_env("CXONE_GITHUBAPP_AUTH_CODE")

    repos = [github_repo_to_cxone_repo(r) for r in get_github_repos_by_org(github_org, github_token)]
    import_new_repos(
        repos,
        import_fn=batch_import_repo,
        origin="GITHUBAPP",
        organization=github_org,
        is_user=False,
        is_org_webhook_enabled=False,
        create_ast_project=True,
        scan_ast_project=True,
        auth_code=cxone_github_app_auth_code,
        webhook_enabled=True,
        pr_decoration_enabled=True,
        sca_auto_pr_enabled=True,
        chunk_size=200,
        delay_request_timeout=30,
    )
