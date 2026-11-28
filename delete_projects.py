"""Delete CxOne projects matching a name pattern (default: all SCM-imported "org/repo" projects).

Optional env vars:
    DELETE_PROJECT_PATTERN   fnmatch pattern on the project name (default "*/*")
    DELETE_WORKERS           concurrent delete workers sharing one API client (default 5)

Deletion itself lives in the SDK (delete_projects_by_pattern), which reuses one API
client (connection pool + cached token) for all deletes.
"""
import os

from scm_common import logger, require_env

# load the env file before importing the SDK (scm_common already did via dotenv)
from CheckmarxPythonSDK.CxOne import delete_projects_by_pattern  # noqa: E402


def main() -> int:
    pattern = os.getenv("DELETE_PROJECT_PATTERN", "*/*")
    workers = int(os.getenv("DELETE_WORKERS", "5"))
    logger.info(f"deleting projects matching {pattern} with {workers} worker(s)")
    failed = delete_projects_by_pattern(pattern=pattern, workers=workers)
    logger.info(f"failed: {failed if failed else 'none'}")
    return 1 if failed else 0


if __name__ == '__main__':
    require_env("CXONE_TENANT_NAME")  # sanity check that env is configured
    raise SystemExit(main())
