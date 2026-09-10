from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

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


STORAGE_BACKEND_LOCAL = "local"
STORAGE_BACKEND_S3 = "s3"


@dataclass(slots=True, frozen=True)
class StorageSettings:
    backend: str = STORAGE_BACKEND_LOCAL
    aws_region: str = "us-east-2"
    s3_bucket: str = ""

    @property
    def uses_s3(self) -> bool:
        return self.backend == STORAGE_BACKEND_S3


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


def load_storage_settings() -> StorageSettings:
    backend = os.environ.get("GRIDLENS_STORAGE_BACKEND", STORAGE_BACKEND_LOCAL).strip().lower() or STORAGE_BACKEND_LOCAL
    if backend not in {STORAGE_BACKEND_LOCAL, STORAGE_BACKEND_S3}:
        raise RuntimeError("GRIDLENS_STORAGE_BACKEND must be either 'local' or 's3'.")

    settings = StorageSettings(
        backend=backend,
        aws_region=os.environ.get("GRIDLENS_AWS_REGION", "us-east-2").strip() or "us-east-2",
        s3_bucket=os.environ.get("GRIDLENS_S3_BUCKET", "").strip(),
    )
    if settings.uses_s3 and not settings.s3_bucket:
        raise RuntimeError("GRIDLENS_S3_BUCKET is required when GRIDLENS_STORAGE_BACKEND=s3.")
    return settings


def object_store_from_settings(settings: StorageSettings) -> ObjectStore:
    if settings.uses_s3:
        return S3ObjectStore(settings)
    return LocalObjectStore()


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


class S3ObjectStore:
    """S3 storage scaffold for the future direct-upload and Batch-worker paths."""

    def __init__(self, settings: StorageSettings):
        self.settings = settings
        self.client = _boto3_client("s3", region_name=settings.aws_region)

    def user_project_prefix(self, user: AuthenticatedUser, project_id: str) -> str:
        return f"users/{user.storage_namespace}/projects/{project_id}"

    def input_key(self, user: AuthenticatedUser, project_id: str, file_id: str, file_name: str) -> str:
        safe_name = Path(file_name).name
        return f"{self.user_project_prefix(user, project_id)}/inputs/{file_id}/{safe_name}"

    def presigned_upload_url(self, key: str, *, content_type: str = "application/octet-stream", expires_in: int = 900) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.settings.s3_bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )

    def head_object(self, key: str) -> dict[str, Any]:
        return self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)

    def read_run_log(self, run_dir: str | Path) -> str:
        raise NotImplementedError("S3 run log reads will be enabled after runs write logs to S3 manifests.")

    def list_run_outputs(self, run_dir: str | Path) -> list[dict]:
        raise NotImplementedError("S3 output listing will be enabled after runs write output manifests to S3.")

    def resolve_run_file(self, run_dir: str | Path, relative_path: str) -> Path:
        raise NotImplementedError("S3 downloads will use presigned URLs instead of local file paths.")

    def export_run_zip(self, run_dir: str | Path) -> Path:
        raise NotImplementedError("S3 ZIP exports will be generated by the object storage export path.")


def _boto3_client(service_name: str, **kwargs: Any) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on optional production dependency
        raise RuntimeError("boto3 is required when GRIDLENS_STORAGE_BACKEND=s3. Install gridlens with the web extras.") from exc
    return boto3.client(service_name, **kwargs)


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
