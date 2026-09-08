from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Protocol

from gridlens.analysis.summary import export_run_zip
from gridlens.core.project import Project, ProjectData
from gridlens.runner.gridpack_runner import GridpackRunRequest, run_gridpack_case
from gridlens.webapi.security import AuthenticatedUser
from gridlens.webapi.service import (
    RunReference,
    ensure_projects_root,
    list_projects,
    list_run_outputs,
    load_project,
    load_run,
    project_root_for_name,
    projects_root_for_user,
    read_text_file,
    resolve_run_file,
)


class ProjectRepository(Protocol):
    """Metadata access for projects and runs."""

    def list_projects(self, user: AuthenticatedUser) -> list[dict]:
        ...

    def project_root_for_name(self, user: AuthenticatedUser, project_name: str) -> Path:
        ...

    def load_project(self, user: AuthenticatedUser, project_id: str) -> tuple[Project, ProjectData]:
        ...

    def load_run(self, user: AuthenticatedUser, project_id: str, run_id: str) -> RunReference:
        ...


class ObjectStore(Protocol):
    """File access for run logs, outputs, and exports."""

    def read_run_log(self, run_dir: str | Path) -> str:
        ...

    def list_run_outputs(self, run_dir: str | Path) -> list[dict]:
        ...

    def resolve_run_file(self, run_dir: str | Path, relative_path: str) -> Path:
        ...

    def export_run_zip(self, run_dir: str | Path) -> Path:
        ...


class JobRunner(Protocol):
    """Submits compute work for the API."""

    def submit_gridpack_run(self, project_id: str, run_id: str, request: GridpackRunRequest) -> Future:
        ...


class FilesystemProjectRepository:
    def __init__(self, projects_root: str | Path):
        self.projects_root = ensure_projects_root(projects_root)

    def user_root(self, user: AuthenticatedUser) -> Path:
        return projects_root_for_user(self.projects_root, user)

    def list_projects(self, user: AuthenticatedUser) -> list[dict]:
        return list_projects(self.user_root(user))

    def project_root_for_name(self, user: AuthenticatedUser, project_name: str) -> Path:
        return project_root_for_name(self.user_root(user), project_name)

    def load_project(self, user: AuthenticatedUser, project_id: str) -> tuple[Project, ProjectData]:
        return load_project(self.user_root(user), project_id)

    def load_run(self, user: AuthenticatedUser, project_id: str, run_id: str) -> RunReference:
        return load_run(self.user_root(user), project_id, run_id)


class LocalObjectStore:
    def read_run_log(self, run_dir: str | Path) -> str:
        return read_text_file(Path(run_dir) / "logs" / "run.log")

    def list_run_outputs(self, run_dir: str | Path) -> list[dict]:
        return list_run_outputs(run_dir)

    def resolve_run_file(self, run_dir: str | Path, relative_path: str) -> Path:
        return resolve_run_file(run_dir, relative_path)

    def export_run_zip(self, run_dir: str | Path) -> Path:
        return export_run_zip(run_dir)


class LocalDockerJobRunner:
    def __init__(self, executor: ThreadPoolExecutor, job_store: dict[str, Future], lock: Lock):
        self.executor = executor
        self.job_store = job_store
        self.lock = lock

    def submit_gridpack_run(self, project_id: str, run_id: str, request: GridpackRunRequest) -> Future:
        future = self.executor.submit(run_gridpack_case, request)
        job_key = f"{project_id}:{run_id}"
        with self.lock:
            self.job_store[job_key] = future
        return future
