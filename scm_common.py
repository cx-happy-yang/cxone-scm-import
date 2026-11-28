"""Helpers shared by the SCM import scripts (GitHub OAuth app, GitHub App, GitLab self-hosted)."""
import logging
import os
import time
from fnmatch import fnmatchcase
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


# ---------- errors ----------

def is_permanent_error(exc: Exception) -> bool:
    """Errors that retrying cannot fix: bad/expired credentials. Retrying only wastes the deadline."""
    try:
        import gitlab
        if isinstance(exc, gitlab.exceptions.GitlabAuthenticationError):
            return True
    except ImportError:  # gitlab package not installed in this environment
        pass
    from github import GithubException
    if isinstance(exc, GithubException):
        # 403 can be a rate limit (transient) or a permission problem (permanent)
        if exc.status == 401:
            return True
        return exc.status == 403 and "rate limit" not in str(exc).lower()
    text = str(exc)
    return any(s in text for s in ("Failed to load access token", "HttpStatusCode: 401", "HttpStatusCode: 403"))


# ---------- GitHub access ----------

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


def collect_github_repos(repos, include: Optional[List[str]] = None,
                         exclude: Optional[List[str]] = None) -> List[dict]:
    """Map the org's repos to CxOne repo dicts; archived and empty repos (no commits yet) are skipped.

    include/exclude are path patterns on "org/repo", same semantics as the GitLab filters.
    """
    include, exclude = include or [], exclude or []
    collected = []
    for repo in repos:
        path = repo.full_name
        if getattr(repo, "archived", False):
            logger.info(f"skip archived repo {path}")
        elif not repo.default_branch:
            logger.info(f"skip empty repo {path} (no commits yet)")
        elif not is_selected(path, include, exclude):
            logger.info(f"skip repo {path} (excluded by include/exclude filter)")
        else:
            collected.append(github_repo_to_cxone_repo(repo))
    return collected


# ---------- CxOne projects ----------

def get_existing_projects() -> Dict[str, object]:
    """CxOne projects keyed by project name (for GitHub/GitLab this is the full repo path)."""
    return {project.name: project for project in get_all_projects()}


# ---------- batched import with retry (shared by all three scripts) ----------

def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def run_batches(pending: List[dict], batch_size: int, submit: Callable[[List[dict]], List[dict]],
                existing: Dict[str, object], deadline_seconds: float, description: str = "repo") -> int:
    """Import pending repos in sequential batches of batch_size, retrying failures with backoff.

    submit(batch) must import the batch and return the repos that still failed ([] = all imported);
    it raises on transport errors, which retry the whole batch. The CxOne project list is refreshed
    before each retry attempt, so repos created by a partially failed batch are not sent again.
    Returns the process exit code: 0 all imported, 1 deadline reached, 2 permanent error.
    """
    deadline = time.time() + deadline_seconds
    attempt = 0
    while pending:
        attempt += 1
        if attempt > 1:
            existing.update(get_existing_projects())
        still_failed: List[dict] = []
        for batch in chunked(pending, batch_size):
            try:
                still_failed.extend(submit(batch) or [])
            except Exception as e:
                if is_permanent_error(e):
                    logger.error(f"stopped, fix the cause and run again to resume: {e}")
                    return 2
                logger.exception(f"batch failed, its {description}s will be retried")
                still_failed.extend(batch)
        if not still_failed:
            logger.info(f"all {description}s imported")
            return 0
        pending = still_failed
        if time.time() >= deadline:
            break
        wait = min(3600, 300 * 2 ** max(attempt - 1, 0))  # 5 min doubling, cap 1 hour
        logger.info(f"{len(pending)} {description}(s) still failing, retrying in {wait:.0f}s")
        time.sleep(wait)
    logger.error(f"deadline reached, {len(pending)} {description}(s) not imported: "
                 f"{[r['fullName'] for r in pending]}")
    return 1


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
