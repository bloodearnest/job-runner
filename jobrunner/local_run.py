"""
Run project.yaml actions locally

This creates and runs jobs in a way that's fairly close to what happens in
production, but with the key difference that rather than specifying a repo URL
and a commit we just supply a workspace directory and code is copied into a
Docker volume directly from there. In the past we've had an issue whereby
broken actions work locally by accident because the right output files happen
to exist anyway even though the action doesn't specify that it depends on them.
To try to avoid this, when copying code into a volume we ignore any files which
match any of the output patterns in the project. We then copy in just the
explicit dependencies of the action.

This is achieved by setting a LOCAL_RUN_MODE flag in the config which, in two
key places, tells the code not to talk to git but do something else instead.

Other than that, everything else runs entirely as it would in production. A
temporary database and log directory is created for each run and then thrown
away afterwards.
"""
import argparse
import os
from pathlib import Path
import random
import shlex
import shutil
import string
import sys
import textwrap

import jobrunner.run
from . import config
from . import docker
from .database import find_where
from .manage_jobs import METADATA_DIR
from .models import JobRequest, Job, State
from .create_or_update_jobs import (
    create_jobs,
    ProjectValidationError,
    JobRequestError,
    NothingToDoError,
)
from .log_utils import configure_logging
from .subprocess_utils import subprocess_run
from .string_utils import tabulate


HELP = __doc__.partition("\n\n")[0]


def add_arguments(parser):
    parser.add_argument("actions", nargs="+", help="Name of project action to run")
    parser.add_argument(
        "-f",
        "--force-run-dependencies",
        help="Re-run from scratch without using existing outputs",
        action="store_true",
    )
    parser.add_argument(
        "--project-dir",
        help="Project directory (default: current directory)",
        default=".",
    )
    return parser


def main(project_dir, actions, force_run_dependencies=False):
    project_dir = Path(project_dir).resolve()
    temp_log_dir = project_dir / METADATA_DIR / ".logs"
    # Generate unique docker label to use for all volumes and containers we
    # create during this run in order to make cleanup easy
    docker_label = "job-runner-local-{}".format(
        "".join(random.choices(string.ascii_uppercase, k=8))
    )

    try:
        success_flag = create_and_run_jobs(
            project_dir,
            actions,
            force_run_dependencies=force_run_dependencies,
            temp_log_dir=temp_log_dir,
            docker_label=docker_label,
        )
    except KeyboardInterrupt:
        print("\nKilled by user")
        print("Cleaning up Docker containers and volumes ...")
        success_flag = False
    finally:
        delete_docker_entities("container", docker_label, ignore_errors=True)
        delete_docker_entities("volume", docker_label, ignore_errors=True)
        shutil.rmtree(temp_log_dir, ignore_errors=True)
    return success_flag


def create_and_run_jobs(
    project_dir, actions, force_run_dependencies, temp_log_dir, docker_label
):
    # Configure
    docker.LABEL = docker_label
    config.LOCAL_RUN_MODE = True
    config.HIGH_PRIVACY_WORKSPACES_DIR = project_dir.parent
    config.DATABASE_FILE = ":memory:"
    config.JOB_LOG_DIR = temp_log_dir
    config.BACKEND = "expectations"
    config.USING_DUMMY_DATA_BACKEND = True

    # None of the below should be used when running locally
    config.WORK_DIR = None
    config.TMP_DIR = None
    config.GIT_REPO_DIR = None
    config.HIGH_PRIVACY_STORAGE_BASE = None
    config.MEDIUM_PRIVACY_STORAGE_BASE = None
    config.MEDIUM_PRIVACY_WORKSPACES_DIR = None

    # Create job_request and jobs
    job_request = JobRequest(
        id="local",
        repo_url=str(project_dir),
        commit="none",
        requested_actions=actions,
        workspace=project_dir.name,
        database_name="dummy",
        force_run_dependencies=force_run_dependencies,
        # The default behaviour of refusing to run if a dependency has failed
        # makes for an awkward workflow when interating in development
        force_run_failed=True,
        branch="",
        original={"created_by": os.environ.get("USERNAME")},
    )
    try:
        create_jobs(job_request)
    except NothingToDoError:
        print("=> All actions already completed")
        print("   Use -f option to force everything to re-run")
        return True
    except (ProjectValidationError, JobRequestError) as e:
        print(f"=> {type(e).__name__}")
        print(textwrap.indent(str(e), "   "))
        return False

    jobs = find_where(Job)

    missing_docker_images = get_missing_docker_images(jobs)
    if missing_docker_images:
        print("Fetching missing docker images")
        for image in missing_docker_images:
            print(f"\nRunning: docker pull {image}")
            try:
                docker.pull(image)
            except docker.DockerPullError as e:
                # TODO: Detect authentication errors and supply specific
                # instructions about how to obtain credentials
                print("Failed with error:")
                print(e)
                return False

    action_names = [job.action for job in jobs]
    print(f"\nRunning actions: {', '.join(action_names)}\n")

    # We don't need the full job ID in the log output here, it only clutters
    # things
    configure_logging(show_action_name_only=True)

    # Run everything
    jobrunner.run.main(exit_when_done=True)
    final_jobs = find_where(Job)

    # Pretty print details of each action
    print()
    for job in final_jobs:
        # If a job fails we don't want to clutter the output with its failed
        # dependants.
        if (
            job.state == State.FAILED
            # TODO: We should probably add error status codes so we don't have
            # to match on the string message like this.
            and job.status_message == "JobError: Not starting as dependency failed"
        ):
            continue
        print(f"=> {job.action}")
        print(textwrap.indent(job.status_message, "   "))
        print()
        print(f"   log file: {METADATA_DIR}/{job.action}.log")
        print("   outputs:")
        outputs = sorted(job.outputs.items()) if job.outputs else []
        print(tabulate(outputs, separator="  - ", indent=5, empty="(no outputs)"))
        print()

    success_flag = all(job.state == State.SUCCEEDED for job in final_jobs)
    return success_flag


# Copied from test/conftest.py to avoid a more complex dependency tree
def delete_docker_entities(entity, label, ignore_errors=False):
    ls_args = [
        "docker",
        entity,
        "ls",
        "--all" if entity == "container" else None,
        "--filter",
        f"label={label}",
        "--quiet",
    ]
    ls_args = list(filter(None, ls_args))
    response = subprocess_run(
        ls_args, capture_output=True, encoding="ascii", check=not ignore_errors
    )
    ids = response.stdout.split()
    if ids and response.returncode == 0:
        rm_args = ["docker", entity, "rm", "--force"] + ids
        subprocess_run(rm_args, capture_output=True, check=not ignore_errors)


def get_missing_docker_images(jobs):
    docker_images = {shlex.split(job.run_command)[0] for job in jobs}
    full_docker_images = {
        f"{config.DOCKER_REGISTRY}/{image}" for image in docker_images
    }
    # We always need this image to work with volumes
    full_docker_images.add(docker.MANAGEMENT_CONTAINER_IMAGE)
    return [
        image for image in full_docker_images if not docker.image_exists_locally(image)
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=HELP)
    parser = add_arguments(parser)
    args = parser.parse_args()
    success = main(**vars(args))
    sys.exit(0 if success else 1)
