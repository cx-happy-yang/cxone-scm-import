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

### Self-hosted GitLab (gitlab_import.py)
Required:
    * GITLAB_URL: base url of your GitLab instance, for example https://gitlab.example.com
    * GITLAB_TOKEN: GitLab personal access token with the `read_api` scope. Only groups the token can see are found, use an admin token to see all groups
    * CXONE_GITLAB_AUTH_CODE: auth code of your self-hosted GitLab SCM in CxOne

Optional:
    * CXONE_GITLAB_INSTANCE_NAME (recommended): instance name of the GitLab SCM in CxOne, for example gitlab_20261126. Without it the SCM is matched by GITLAB_URL, which fails if zero or several SCMs match (for example a stale one whose tunnel was removed)
    * GITLAB_CA_BUNDLE: path to a CA certificate file (.pem) if your GitLab uses a self-signed or internal CA. TLS verification is never disabled
    * GITLAB_GROUP: comma separated top-level group paths or ids. If unset, every top-level group the token can see is imported
    * GITLAB_GROUP_INCLUDE: comma separated path patterns, only matching groups/projects are imported
    * GITLAB_GROUP_EXCLUDE: comma separated path patterns, matching groups/projects are skipped (exclude wins over include)
    * GITLAB_GROUP_FILE: file with one include pattern per line (`#` comments allowed), added to GITLAB_GROUP_INCLUDE
    * DRY_RUN: `true` only reports what would be imported, nothing is changed
    * IMPORT_STATE_FILE: state file for resume and retry (default `./gitlab_import_state.json`)
    * IMPORT_DEADLINE_HOURS: how long failed groups are retried (default 48)

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

#### Large instances, resume and retry
The groups are imported one after another (CxOne imports must not run in parallel). After every group the progress is written to the state file (`pending`, `in_progress`, `done` or `failed`, with attempts and the last error).
* If the script is killed, run it again: finished groups are skipped. A group that was cut off mid-import is detected, the script waits until CxOne reports its import job at 100%, and then runs that group again. Repos that already exist in CxOne are always skipped.
* A failed group is retried with growing waits (5 min, 10 min, ... up to 1 hour) until everything is done or `IMPORT_DEADLINE_HOURS` has passed. Then the script exits with code 1 and the state file lists what is left. Restarting gives a fresh deadline.
* Credential errors (401, 403, `Failed to load access token`, for example an expired auth code) are not retried. The script exits with code 2 and keeps the state: put a new `CXONE_GITLAB_AUTH_CODE` in the env file and run it again to resume.
* To start over, delete the state file.

## Two ways to import
1. using the documented REST API : [/api/repos-manager/scm-projects](https://checkmarx.stoplight.io/docs/checkmarx-one-api-reference-guide/branches/main/5yefdw9pm675i-import-code-repository)
2. using oauth code, and using an undocumented REST API:  /api/repos-manager/scms/{scm_id}/orgs/{org}/asyncImport (GitHub) or /repos (GitLab). The scripts in this repo use this one.

## Code layout
    * scm_common.py: shared helpers (env loading, GitHub helpers, skip/refresh already imported projects)
    * import_state.py: resume/retry state and the retry-until-deadline runner used by gitlab_import.py
    * github_oauth_import.py, github_app_import.py, gitlab_import.py: one entry point per SCM
