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
    * GITLAB_URL: base url of your GitLab instance, for example https://gitlab.example.com
    * GITLAB_TOKEN: GitLab personal access token with the `read_api` scope
    * GITLAB_GROUP: GitLab group path or id; subgroups are included
    * CXONE_GITLAB_AUTH_CODE: auth code of your self-hosted GitLab SCM in CxOne

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
        GITLAB_GROUP=my-group
        CXONE_GITLAB_AUTH_CODE=<auth code from CxOne portal>

4. Run: `python gitlab_import.py`

What the script does:
1. finds the CxOne SCM type whose host matches `GITLAB_URL` (it exits and lists the available SCM types if none matches)
2. lists all projects in `GITLAB_GROUP` and its subgroups, skipping archived and empty (no default branch) projects
3. skips projects that already exist in CxOne (and refreshes their repo settings)
4. imports the rest in chunks of 200 and waits for each chunk to finish

CxOne project names are the full GitLab path (`group/subgroup/project`). The import flags (webhook, PR decoration, scan after import) are set in the `import_new_repos(...)` call at the bottom of `gitlab_import.py`; by default webhooks, PR decoration and scan-after-import are off.

Tip: try a small top-level group first. Nested group paths (`a/b`) may need URL-encoding or the numeric group id as the organization value.

## Two ways to import
1. using the documented REST API : [/api/repos-manager/scm-projects](https://checkmarx.stoplight.io/docs/checkmarx-one-api-reference-guide/branches/main/5yefdw9pm675i-import-code-repository)
2. using oauth code, and using an undocumented REST API:  /api/repos-manager/scms/{scm_id}/orgs/{org}/asyncImport (GitHub) or /repos (GitLab). The scripts in this repo use this one.

## Code layout
    * scm_common.py: shared helpers (env loading, skip/refresh already imported projects)
    * github_oauth_import.py, github_app_import.py, gitlab_import.py: one entry point per SCM
