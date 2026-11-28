"""Delete CxOne projects matching a name pattern (default: all SCM-imported "org/repo" projects).

Optional env vars:
    DELETE_PROJECT_PATTERN   fnmatch pattern on the project name (default "*/*")
    DELETE_WORKERS           concurrent delete workers sharing one API client (default 5)

One RepoManagerAPI instance is created and reused: a fresh API per call would re-create the
httpx client and re-fetch the access token for every project, which dominates the runtime.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatchcase

from scm_common import logger, require_env

# load the env file before importing the SDK (scm_common already did via dotenv)
from CheckmarxPythonSDK.CxOne import ProjectsAPI, get_all_projects  # noqa: E402


def main() -> int:
    pattern = os.getenv("DELETE_PROJECT_PATTERN", "*/*")
    workers = int(os.getenv("DELETE_WORKERS", "5"))
    api = ProjectsAPI()  # one shared client: connection reuse + cached token

    projects = [p for p in get_all_projects() if fnmatchcase(p.name, pattern)]
    if not projects:
        logger.info(f"no projects match {pattern}")
        return 0
    logger.info(f"deleting {len(projects)} project(s) matching {pattern} with {workers} worker(s)")

    t0 = time.monotonic()
    done = 0
    failed = []

    def delete_one(p):
        nonlocal done
        try:
            if not api.delete_a_project(project_id=p.id):
                failed.append(p.name)
        except Exception as e:
            failed.append(f"{p.name} ({type(e).__name__}: {e})")
        done += 1
        if done % 100 == 0:
            rate = done / max(time.monotonic() - t0, 0.1)
            logger.info(f"  {done}/{len(projects)} deleted ({rate:.1f}/s), failed: {len(failed)}")

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(delete_one, projects))
    else:
        for p in projects:
            delete_one(p)

    logger.info(f"deleted {done - len(failed)}/{len(projects)} in {time.monotonic() - t0:.0f}s; "
                f"failed: {failed if failed else 'none'}")
    return 1 if failed else 0


if __name__ == '__main__':
    require_env("CXONE_TENANT_NAME")  # sanity check that env is configured
    raise SystemExit(main())
