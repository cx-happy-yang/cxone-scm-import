"""Helpers shared by the SCM import scripts (GitHub OAuth app, GitHub App, GitLab self-hosted)."""
import logging
import os
from typing import Callable, Dict, List, Optional

from dotenv import load_dotenv
from github import Auth, Github

# Load the env file before anything reads configuration. Override the default with ENV_FILE=...
load_dotenv(os.getenv("ENV_FILE", "./happy.env"), override=True)

from CheckmarxPythonSDK.CxOne import (  # noqa: E402
    get_all_projects,
    get_a_project_by_id,
    get_repo_by_id,
    update_repo_by_id,
)

logger = logging.getLogger("CheckmarxPythonSDK")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def get_github_repos_by_org(organization: str, access_token: str):
    return Github(auth=Auth.Token(access_token)).get_organization(organization).get_repos()


def github_repo_to_cxone_repo(repo, host_url: str = "https://github.com/") -> dict:
    """Map a GitHub repo to the repo dict expected by batch_import_repo."""
    full_name = repo.html_url.replace(host_url, "")
    return {
        "id": full_name.split("/")[1],
        "fullName": full_name,
        "url": repo.html_url,
        "sshRepoUrl": f"git@github.com:{full_name}.git",
        "defaultBranch": repo.default_branch,
    }


def get_existing_projects() -> Dict[str, object]:
    """CxOne projects keyed by project name (for GitHub/GitLab this is the full repo path)."""
    return {project.name: project for project in get_all_projects()}


def _flag(value):
    """Newer CxOne versions return settings as {"value": ..., "isEditable": ...}; the update API wants the plain value."""
    return value.get("value") if isinstance(value, dict) else value


def update_existing_repo_settings(project, sca_auto_pr_enabled: Optional[bool] = None) -> None:
    """Re-save the repo settings of an already imported project.

    sca_auto_pr_enabled: True/False forces the value, None keeps the current one.
    sshState is always set to SKIPPED.
    """
    project = get_a_project_by_id(project_id=project.id)
    if project.repo_id is None:
        logger.info(f"project {project.name} was not imported from an SCM, skip")
        return
    repo = get_repo_by_id(repo_id=project.repo_id)
    if sca_auto_pr_enabled is None:
        sca_auto_pr_enabled = _flag(repo.get("scaAutoPrEnabled"))
    update_repo_by_id(repo_id=project.repo_id, project_id=project.id, pay_load={
        "branches": repo.get("branches"),
        "kicsScannerEnabled": _flag(repo.get("kicsScannerEnabled")),
        "sastIncrementalScan": _flag(repo.get("sastIncrementalScan")),
        "sastScannerEnabled": _flag(repo.get("sastScannerEnabled")),
        "scaScannerEnabled": _flag(repo.get("scaScannerEnabled")),
        "apiSecScannerEnabled": _flag(repo.get("apiSecScannerEnabled")),
        "url": repo.get("url"),
        "webhookEnabled": repo.get("webhookEnabled"),
        "prDecorationEnabled": _flag(repo.get("prDecorationEnabled")),
        "secretsDetectionScannerEnabled": _flag(repo.get("secretsDetectionScannerEnabled")),
        "ossfScoreCardScannerEnabled": _flag(repo.get("ossfScoreCardScannerEnabled")),
        "scaAutoPrEnabled": sca_auto_pr_enabled,
        "webhookId": repo.get("webhookId"),
        "sshRepoUrl": repo.get("sshRepoUrl"),
        "sshState": "SKIPPED",
        "isRepoAdmin": repo.get("isRepoAdmin"),
        "containerScannerEnabled": _flag(repo.get("containerScannerEnabled")),
    })


def split_new_and_existing(
    repos: List[dict],
    update_existing: bool = True,
    sca_auto_pr_enabled: Optional[bool] = None,
    existing: Optional[Dict[str, object]] = None,
) -> List[dict]:
    """Return the repos not yet in CxOne; optionally refresh the settings of those already imported.

    Each repo dict needs "fullName" (the CxOne project name), as used by batch_import_repo.
    existing: CxOne projects keyed by name; pass it to avoid reloading all projects on every call.
    """
    if existing is None:
        existing = get_existing_projects()
    new_repos = []
    for repo in repos:
        full_name = repo["fullName"]
        project = existing.get(full_name)
        if project is None:
            new_repos.append(repo)
            continue
        logger.info(f"repo {full_name} already exists in cx one")
        if not update_existing:
            continue
        try:
            update_existing_repo_settings(project, sca_auto_pr_enabled=sca_auto_pr_enabled)
        except Exception:
            logger.exception(f"Failed to update project {full_name} repo settings")
    return new_repos


def import_new_repos(
    repos: List[dict],
    import_fn: Callable[..., None],
    update_existing: bool = True,
    sca_auto_pr_enabled_existing: Optional[bool] = None,
    existing: Optional[Dict[str, object]] = None,
    **import_kwargs,
) -> None:
    """Filter out already imported repos, then call import_fn(repos=new_repos, existing_project_names=..., **kwargs).

    existing: CxOne projects keyed by name. Pass it when calling repeatedly (e.g. once per group), so the full project
    list is loaded once, and the SDK does not reload it on every call.
    """
    if existing is None:
        existing = get_existing_projects()
    new_repos = split_new_and_existing(
        repos, update_existing=update_existing, sca_auto_pr_enabled=sca_auto_pr_enabled_existing,
        existing=existing,
    )
    if not new_repos:
        logger.info("no new repos to import")
        return
    logger.info(f"importing {len(new_repos)} new repos")
    import_fn(repos=new_repos, existing_project_names=existing.keys(), **import_kwargs)
