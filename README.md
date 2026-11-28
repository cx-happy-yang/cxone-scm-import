# CxOne SCM Import

Python scripts that import all repositories of a GitHub organization or a self-hosted GitLab group into CxOne.

| Script | SCM | Import via |
| --- | --- | --- |
| `github_oauth_import.py` | GitHub | OAuth app (default) |
| `github_app_import.py` | GitHub | GitHub App |
| `gitlab_import.py` | Self-hosted GitLab | OAuth auth code |

Repos that already exist in CxOne are not imported again; their repo settings are refreshed instead.

## Notice
Please use Python3!

## Setup
1. create a python virtual environment: `python -m venv .venv`
2. activate the virtual environment:
   a. on Windows: `.\.venv\Scripts\activate`
   b. on Linux/MacOS: `source .venv/bin/activate`
3. install the dependencies: `pip install -r requirements.txt`
4. create an env file with the variables below. The scripts read `./happy.env` by default; use another file with `ENV_FILE=/path/to/file.env`. Any `*.env` file is git-ignored.

## Environment variables

### CxOne (all scripts)
    * CXONE_ACCESS_CONTROL_URL: Your cxone IAM url: https://sng.iam.checkmarx.net/
    * CXONE_SERVER: Your cxone server url, for example: https://sng.ast.checkmarx.net/
    * CXONE_TENANT_NAME: Your cxone tenant name
    * CXONE_GRANT_TYPE: refresh_token
    * CXONE_REFRESH_TOKEN: Your CxOne API Key

### GitHub (github_oauth_import.py, github_app_import.py)
    * GITHUB_ORG: your GitHub organization name, which is case-sensitive
    * GITHUB_TOKEN: github personal access token
    * CXONE_GITHUB_AUTH_CODE: your GitHub auth code (github_oauth_import.py)
    * CXONE_GITHUBAPP_AUTH_CODE: your GitHub App auth code (github_app_import.py)

Optional (same semantics as the GitLab script):
    * GITHUB_REPO_INCLUDE / GITHUB_REPO_EXCLUDE: comma separated "org/repo" patterns (wildcards ok),
      exclude wins over include; GITHUB_REPO_FILE adds one include pattern per line
    * BATCH_SIZE: repos per import batch (default 200)
    * DRY_RUN: `true` only reports what would be imported, nothing is changed
    * IMPORT_DEADLINE_HOURS: how long failed repos are retried (default 48)

GitHub's `asyncImport` runs asynchronously: the SDK waits for each batch's job to report 100% before
submitting the next, so imports are still strictly sequential. Archived repos and repos without
commits yet are skipped, repos already in CxOne are skipped (their repo settings are refreshed
instead), failed batches are retried with growing waits, and a restart is safe without any state
file: the script re-lists the org and skips what exists. Exit codes: 0 done, 1 deadline reached,
2 credential error (fix and run again).

### Self-hosted GitLab (gitlab_import.py)
Required:
    * GITLAB_URL: base url of your GitLab instance, for example https://gitlab.example.com
    * GITLAB_TOKEN: GitLab personal access token with the `read_api` scope. Only groups the token can see are found, use an admin token to see all groups
    * CXONE_GITLAB_AUTH_CODE: auth code of your self-hosted GitLab SCM in CxOne

Optional:
    * CXONE_GITLAB_INSTANCE_NAME (recommended): instance name of the GitLab SCM in CxOne, for example gitlab_20261126. Without it the SCM is matched by GITLAB_URL, which fails if zero or several SCMs match (for example a stale one whose tunnel was removed)
    * GITLAB_CA_BUNDLE: path to a CA certificate file (.pem) if your GitLab uses a self-signed or internal CA. TLS verification is never disabled
    * GITLAB_GROUP_INCLUDE: comma separated path patterns, only matching groups/projects are imported
    * GITLAB_GROUP_EXCLUDE: comma separated path patterns, matching groups/projects are skipped (exclude wins over include)
    * GITLAB_GROUP_FILE: file with one include pattern per line (`#` comments allowed), added to GITLAB_GROUP_INCLUDE
    * BATCH_SIZE: repos per import batch (default 200)
    * DRY_RUN: `true` only reports what would be imported, nothing is changed
    * IMPORT_DEADLINE_HOURS: how long failed repos are retried (default 48)

Find the auth codes in the CxOne Portal developer tools (network tab) while connecting to the SCM.

## How to run

### GitHub
    python github_oauth_import.py   # OAuth app
    python github_app_import.py   # GitHub App

### Self-hosted GitLab
1. In CxOne, configure your GitLab instance as an SCM (self-hosted GitLab) for the tenant.
2. Create a GitLab personal access token with the `read_api` scope.
3. Add to your env file:

        GITLAB_URL=https://gitlab.example.com
        GITLAB_TOKEN=<gitlab personal access token>
        CXONE_GITLAB_AUTH_CODE=<auth code from CxOne portal>
        CXONE_GITLAB_INSTANCE_NAME=<instance name of the GitLab SCM in CxOne>
        GITLAB_CA_BUNDLE=/path/to/gitlab.pem    # only for self-signed / internal CA

4. Preview first: `DRY_RUN=true python gitlab_import.py`
5. Run: `python gitlab_import.py`

#### Choosing groups
Patterns match the full path of a group or project and support `*`. A pattern also covers everything below it, so `team` covers `team/sub/repo`. Exclude wins over include.

    GITLAB_GROUP_INCLUDE=team-*,platform/payments    # only these
    GITLAB_GROUP_EXCLUDE=platform/legacy*            # but not these
    GITLAB_GROUP_FILE=groups.txt                     # one include pattern per line

Subgroups are imported together with their top-level group. The CxOne project name is the full path (`group/subgroup/project`). Archived projects and projects without commits yet are skipped.

#### How the import works
All GitLab projects are listed in one paginated pass, then the new ones are imported in sequential
batches of `BATCH_SIZE` (200). The import POST completes synchronously: its response carries the
final per-repo results (`SUCCESS` / error with message), so a batch that returns has finished -
CxOne never runs two import jobs at the same time, and there is no job-status polling.

* Repos already in CxOne are always skipped. Repos whose import failed are retried in the next
  batch with growing waits (5 min, 10 min, ... up to 1 hour) until everything is done or
  `IMPORT_DEADLINE_HOURS` has passed. Then the script exits with code 1 and lists what is left.
* Credential errors (401, 403, `Failed to load access token`, for example an expired auth code)
  are not retried. The script exits with code 2: put a new `CXONE_GITLAB_AUTH_CODE` in the env
  file and run it again to resume.
* A restart is safe without any state file: the script re-lists GitLab, skips whatever already
  exists in CxOne, and re-imports only what is missing (the server also ignores repos that were
  already imported).

## Two ways to import
1. using the documented REST API : [/api/repos-manager/scm-projects](https://checkmarx.stoplight.io/docs/checkmarx-one-api-reference-guide/branches/main/5yefdw9pm675i-import-code-repository)
2. using oauth code, and using an undocumented REST API:  /api/repos-manager/scms/{scm_id}/orgs/{org}/asyncImport (GitHub) or /repos (GitLab). The scripts in this repo use this one.

## Code layout
    * scm_common.py: shared helpers (env loading, GitHub helpers, skip/refresh already imported projects)
    * import_state.py: resume/retry state and the retry-until-deadline runner used by gitlab_import.py
    * github_oauth_import.py, github_app_import.py, gitlab_import.py: one entry point per SCM
