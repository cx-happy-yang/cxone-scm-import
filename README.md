# CxOneImportGitHubProjects

This python script aims to import all GitHub repos from one GitHub Organization into CxOne.

## Prerequisites for the script, you need to set the following environment variables
    * GITHUB_ORG: your GitHub organization name, which is case-sensitive
    * CXONE_GITHUB_AUTH_CODE: your GitHub auth code
    * CXONE_ACCESS_CONTROL_URL: Your cxone IAM url: https://sng.iam.checkmarx.net/
    * CXONE_SERVER: Your cxone server url, for example: https://sng.ast.checkmarx.net/
    * CXONE_TENANT_NAME: Your cxone tenant name
    * CXONE_GRANT_TYPE: refresh_token
    * CXONE_REFRESH_TOKEN: Your CxOne API Key
    * GITHUB_TOKEN: github personal access token
    

## Two ways to import  
1. using the documented REST API : [/api/repos-manager/scm-projects](https://checkmarx.stoplight.io/docs/checkmarx-one-api-reference-guide/branches/main/5yefdw9pm675i-import-code-repository)
2. using oauth code, and using an undocumented REST API:  /api/repos-manager/scms/1/orgs/{github_org}/asyncImport

## Notice
Please use Python3!

## How to run the script
1. create a python virtual environment: python -m venv .venv
2. activate the virtual environment: 
   a. on Windows: .\.venv\Scripts\activate
   b. on Linux/MacOS: source .venv/bin/activate
3. install the dependencies: pip install -r requirements.txt
4. update the auto_code in main.py. Please find the auth_code from CxOne Portal developer tools.
5. run the python script: python main.py (GitHub OAuth app), python github_app_import.py (GitHub App) or python gitlab_import.py (self-hosted GitLab)

## Importing a self-hosted GitLab group (gitlab_import.py)
Additional environment variables:
    * GITLAB_URL: base url of your GitLab instance, for example https://gitlab.example.com
    * GITLAB_TOKEN: GitLab personal access token (read_api scope)
    * GITLAB_GROUP: GitLab group path or id; subgroups are included
    * CXONE_GITLAB_AUTH_CODE: auth code of your self-hosted GitLab SCM in CxOne (from CxOne Portal developer tools)

The GitLab instance must already be configured as an SCM in your CxOne tenant. The script finds its SCM id by matching the host of GITLAB_URL. Archived and empty projects are skipped; CxOne project names are the full GitLab path (group/subgroup/project).

## Code layout
    * scm_common.py: shared helpers (env loading, skip/refresh already imported projects)
    * main.py, github_app_import.py, gitlab_import.py: one entry point per SCM