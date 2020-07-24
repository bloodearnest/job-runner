import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from runner.exceptions import GitCloneError
from runner.exceptions import RepoNotFound
from runner.project import parse_project_yaml
from runner.utils import getlogger
from runner.utils import set_auth

from runner.exceptions import InvalidRepo

logger = getlogger(__name__)


class JobRunner:
    def __init__(self, job):
        self.job = job
        self.tmpdir = tempfile.TemporaryDirectory(
            dir=os.environ["HIGH_PRIVACY_STORAGE_BASE"]
        )
        self.workdir = Path(self.tmpdir.name)
        self.logger = self.get_job_logger()
        # Sets netrc authentication, used by docker and github clients
        set_auth()

    def __call__(self):
        return self.run()

    def run(self):
        self.logger.info(f"Starting job")
        os.chdir(self.workdir)
        self.fetch_study_source()
        self.validate_input_files()
        self.logger.info(f"Repo at {self.workdir} successfully validated")
        self.job = parse_project_yaml(self.workdir, self.job)
        self.logger.debug(f"Added runtime metadata to job")
        self.invoke_docker()
        return self.job

    def validate_input_files(self):
        """Assert that all the input files are text, not binary
        """
        workdir = Path(self.workdir)
        missing = []
        for required in ["project.yaml", "analysis", "codelists"]:
            if not (workdir / required).exists():
                missing.append(required)
        if missing:
            raise InvalidRepo(
                f"Folders {', '.join(missing)} must exist; is this an OpenSAFELY repo?",
                report_args=True,
            )
        for path in workdir.rglob("*"):
            path = str(path)
            if ".git" in path or "outputs" in path:
                continue
            # We shell out to system's libmagic implementation, rather than
            # using python, to reduce dependencies
            result = subprocess.check_output(
                ["file", "--brief", "--mime", path], encoding="utf8"
            )
            mimetype = result.split("/")[0]
            if mimetype not in ["tex", "inode"] and not result.startswith(
                "application/pdf"
            ):
                raise InvalidRepo(
                    f"All analysis input files must be text, found {result} at {path}",
                    report_args=True,
                )

    def __repr__(self):
        """An opaque string for use in logging to help trace events related to
        a specific job
        """
        match = re.match(r".*/([0-9]+)/?$", self.job["url"])
        if match:
            return "job#" + match.groups()[0]
        else:
            return "-"

    def get_job_logger(self):
        return logging.LoggerAdapter(logger, {"job_id": repr(self)})

    def invoke_docker(self):
        cmd = [
            "docker",
            "run",
            "--name",
            self.job["container_name"],
            "--rm",
            "--log-driver",
            "none",
            "-a",
            "stdout",
            "-a",
            "stderr",
            "--volume",
            f"{self.workdir}:/workspace",
        ] + self.job["docker_invocation"]

        os.chdir(self.workdir)
        self.logger.info("Running subdocker cmd `%s` in %s", cmd, self.workdir)
        result = subprocess.run(cmd, capture_output=True, encoding="utf8")
        if result.returncode == 0:
            self.logger.info("subdocker stdout: %s", result.stdout)
        else:
            raise self.job["docker_exception"](result.stderr, report_args=False)
        # Copy outputs to the expected location
        for output_name, output_filename in self.job.get("outputs", {}).items():
            target_path = os.path.join(self.job["output_path"], output_filename)
            shutil.move(os.path.join(self.workdir, output_filename), target_path)
            self.logger.info("Copied output to %s", target_path)

    def fetch_study_source(self):
        """Checkout source over Github API to a temporary location.
        """
        repo = self.job["repo"]
        branch_or_tag = self.job["tag"]
        max_retries = 3
        self.logger = self.get_job_logger()
        for attempt in range(max_retries + 1):
            cmd = [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch_or_tag,
                repo,
                self.workdir,
            ]
            self.logger.info("Running %s, attempt %s", cmd, attempt)
            try:
                subprocess.check_output(cmd, stderr=subprocess.STDOUT, encoding="utf8")
                break
            except subprocess.CalledProcessError as e:
                if "not found" in e.output:
                    raise RepoNotFound(e.output, report_args=True)
                elif attempt < max_retries:
                    self.logger.warning("Failed clone; sleeping, then retrying")
                    time.sleep(10)
                else:
                    raise GitCloneError(cmd, report_args=True) from e
