from __future__ import annotations

import json

import pytest

from gridlens.core.project import Project
from gridlens.webapi.backends import S3ObjectStore, StorageSettings, load_storage_settings, validate_upload_file_name, validate_upload_size
from gridlens.webapi.security import AuthenticatedUser
from gridlens.webapi.service import list_projects, load_project_configuration, load_run, projects_root_for_user, read_run_status, resolve_run_file

try:
    from fastapi.testclient import TestClient
    from gridlens.webapi.app import create_app
except RuntimeError:
    TestClient = None
    create_app = None


def test_list_projects_returns_saved_project_metadata(tmp_path) -> None:
    xml = tmp_path / "input.xml"
    xml.write_text("<Configuration />", encoding="utf-8")
    network = tmp_path / "network.raw"
    network.write_text("0 / END", encoding="utf-8")

    project = Project("Pilot Study", tmp_path / "api-projects" / "Pilot_Study")
    project_data = project.save([xml, network], "input.xml")

    projects = list_projects(tmp_path / "api-projects")

    assert projects == [
        {
            "project_id": "Pilot_Study",
            "name": project_data.name,
            "root_dir": str(project.root_dir),
            "xml_file_name": "input.xml",
            "input_files": ["input.xml", "network.raw"],
            "run_count": 0,
            "created_at": project_data.created_at,
            "updated_at": project_data.updated_at,
            "latest_run_id": "",
        }
    ]


def test_read_run_status_handles_missing_invalid_and_valid_files(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert read_run_status(run_dir)["status"] == "not started"

    (run_dir / "status.json").write_text("{invalid", encoding="utf-8")
    assert read_run_status(run_dir)["status"] == "unknown"

    (run_dir / "status.json").write_text(json.dumps({"status": "completed", "return_code": 0}), encoding="utf-8")
    status = read_run_status(run_dir)
    assert status["status"] == "completed"
    assert status["return_code"] == 0


def test_load_run_returns_project_and_run_reference(tmp_path) -> None:
    xml = tmp_path / "input.xml"
    xml.write_text("<Configuration />", encoding="utf-8")

    project = Project("Pilot Study", tmp_path / "api-projects" / "Pilot_Study")
    project.save([xml], "input.xml")
    run_dir = project.create_run_folder()

    reference = load_run(tmp_path / "api-projects", "Pilot_Study", run_dir.name)

    assert reference.project_id == "Pilot_Study"
    assert reference.run_id == run_dir.name
    assert reference.run_dir == run_dir


def test_load_project_configuration_supports_raw_only_project(tmp_path) -> None:
    network = tmp_path / "network.raw"
    network.write_text("0 / END", encoding="utf-8")

    project = Project("Pilot Study", tmp_path / "api-projects" / "Pilot_Study")
    project_data = project.save([network], "")

    values, network_names, monitor_branches, warning = load_project_configuration(project, project_data)

    assert values.network_file_name == "network.raw"
    assert values.xml_file_name == "input.xml"
    assert network_names == ["network.raw"]
    assert monitor_branches == []
    assert warning == ""


def test_projects_root_for_authenticated_user_is_namespaced(tmp_path) -> None:
    user = AuthenticatedUser(
        subject="user-subject-123",
        username="person@example.com",
        email="person@example.com",
    )

    resolved = projects_root_for_user(tmp_path / "api-projects", user)

    assert resolved.parent.name == "users"
    assert resolved.name == user.storage_namespace


def test_resolve_run_file_rejects_escape_paths(tmp_path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    target = run_dir / "logs" / "run.log"
    target.write_text("hello", encoding="utf-8")

    assert resolve_run_file(run_dir, "logs/run.log") == target.resolve()

    try:
        resolve_run_file(run_dir, "../outside.txt")
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - sanity guard
        raise AssertionError("Expected resolve_run_file to reject traversal paths")


def test_storage_settings_default_to_local(monkeypatch) -> None:
    monkeypatch.delenv("GRIDLENS_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("GRIDLENS_AWS_REGION", raising=False)
    monkeypatch.delenv("GRIDLENS_S3_BUCKET", raising=False)

    settings = load_storage_settings()

    assert settings.backend == "local"
    assert settings.aws_region == "us-east-2"
    assert settings.s3_bucket == ""


def test_storage_settings_require_bucket_for_s3(monkeypatch) -> None:
    monkeypatch.setenv("GRIDLENS_STORAGE_BACKEND", "s3")
    monkeypatch.delenv("GRIDLENS_S3_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="GRIDLENS_S3_BUCKET"):
        load_storage_settings()


def test_s3_object_store_scopes_input_keys_to_user() -> None:
    user = AuthenticatedUser(
        subject="user-subject-123",
        username="person@example.com",
        email="person@example.com",
    )

    key = S3ObjectStore.input_key(
        object.__new__(S3ObjectStore),
        user,
        "Pilot_Study",
        "file-123",
        "network.raw",
    )

    assert key == f"users/{user.storage_namespace}/projects/Pilot_Study/inputs/file-123/network.raw"


def test_upload_validation_rejects_paths_and_large_files() -> None:
    assert validate_upload_file_name("network.raw") == "network.raw"
    validate_upload_size(10)

    with pytest.raises(Exception, match="local file name"):
        validate_upload_file_name("../network.raw")

    with pytest.raises(Exception, match="too large"):
        validate_upload_size(501 * 1024 * 1024)


def test_webapi_configuration_and_export_endpoints(tmp_path, monkeypatch) -> None:
    if TestClient is None or create_app is None:
        pytest.skip("FastAPI test client dependencies are not installed.")
    projects_root = tmp_path / "api-projects"
    monkeypatch.setenv("GRIDLENS_API_PROJECTS_ROOT", str(projects_root))
    client = TestClient(create_app())

    network = tmp_path / "network.raw"
    network.write_text("0 / END", encoding="utf-8")

    project = Project("Pilot Study", projects_root / "Pilot_Study")
    project_data = project.save([network], "")
    run_dir = project.create_run_folder()
    result_file = run_dir / "work" / "success.txt"
    result_file.write_text("1,1,0\n", encoding="utf-8")
    (run_dir / "logs" / "run.log").write_text("run log", encoding="utf-8")
    (run_dir / "status.json").write_text(json.dumps({"status": "completed", "return_code": 0}), encoding="utf-8")

    response = client.get("/api/projects/Pilot_Study/configuration")
    assert response.status_code == 200
    assert response.json()["configuration"]["network_file_name"] == "network.raw"

    save_response = client.post(
        "/api/projects/Pilot_Study/configuration",
        json={
            "xml_file_name": "generated.xml",
            "network_file_name": "network.raw",
            "network_configuration_tag": "networkConfiguration",
            "full_branch_n1": True,
            "full_generator_n1": False,
            "contingency_rating": "C",
            "enforce_reactive_power_limit": True,
            "print_calc_files": False,
            "group_size": "1",
            "max_voltage": "1.1",
            "min_voltage": "0.9",
            "contingency_qlim_deadband": "0.1",
            "contingency_ltc": False,
            "write_stats": False,
            "contingency_output_format": "csv_flat",
            "contingency_output_file": "pilot_study",
            "contingency_list": "",
            "monitor_branches_file": "",
            "monitor_areas": "",
            "monitor_kv_min": "0",
            "monitor_kv_max": "0",
            "init_start": "warm",
            "switched_shunt": False,
            "powerflow_qlim_deadband": "0.1",
            "powerflow_ltc": False,
            "area_interchange": False,
            "max_controller_iterations": "10",
            "max_iteration": "50",
            "tolerance": "1.0e-4",
            "max_qlim_iterations": "3",
            "damping_factor": "1.0",
            "phase_shift_sign": "1.0",
            "petsc_prefix": "",
            "petsc_options": "-ksp_type preonly",
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["project"]["xml_file_name"] == "generated.xml"

    outputs_response = client.get(f"/api/projects/Pilot_Study/runs/{run_dir.name}/outputs")
    assert outputs_response.status_code == 200
    assert outputs_response.json()["files"]

    download_response = client.get(f"/api/projects/Pilot_Study/runs/{run_dir.name}/outputs/download/work/success.txt")
    assert download_response.status_code == 200
    assert download_response.text == "1,1,0\n"

    export_response = client.get(f"/api/projects/Pilot_Study/runs/{run_dir.name}/export")
    assert export_response.status_code == 200
    assert export_response.headers["content-type"] == "application/zip"


def test_webapi_upload_presign_requires_s3_backend(tmp_path, monkeypatch) -> None:
    if TestClient is None or create_app is None:
        pytest.skip("FastAPI test client dependencies are not installed.")
    projects_root = tmp_path / "api-projects"
    monkeypatch.setenv("GRIDLENS_API_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("GRIDLENS_STORAGE_BACKEND", "local")
    client = TestClient(create_app())

    network = tmp_path / "network.raw"
    network.write_text("0 / END", encoding="utf-8")
    project = Project("Pilot Study", projects_root / "Pilot_Study")
    project.save([network], "")

    response = client.post(
        "/api/projects/Pilot_Study/uploads",
        json={"file_name": "network.raw", "content_type": "application/octet-stream", "size_bytes": 10},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "S3 uploads are not enabled for this deployment."


def test_webapi_complete_s3_upload_records_project_input(tmp_path, monkeypatch) -> None:
    if TestClient is None or create_app is None:
        pytest.skip("FastAPI test client dependencies are not installed.")
    projects_root = tmp_path / "api-projects"
    monkeypatch.setenv("GRIDLENS_API_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("GRIDLENS_STORAGE_BACKEND", "local")
    client = TestClient(create_app())

    network = tmp_path / "network.raw"
    network.write_text("0 / END", encoding="utf-8")
    project = Project("Pilot Study", projects_root / "Pilot_Study")
    project.save([network], "")

    class FakeS3ObjectStore:
        def input_key(self, user, project_id: str, file_id: str, file_name: str) -> str:
            return f"users/{user.storage_namespace}/projects/{project_id}/inputs/{file_id}/{file_name}"

        def head_object(self, key: str) -> dict:
            return {"ContentLength": 42, "ETag": '"etag-value"'}

    client.app.state.storage_settings = StorageSettings(backend="s3", aws_region="us-east-2", s3_bucket="test-bucket")
    client.app.state.object_store = FakeS3ObjectStore()

    response = client.post(
        "/api/projects/Pilot_Study/uploads/upload-1/complete",
        json={"file_name": "remote.raw", "content_type": "application/octet-stream", "size_bytes": 42},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["project"]["input_files"] == ["network.raw", "remote.raw"]

    updated = project.load_data()
    local_namespace = AuthenticatedUser(subject="local-user", username="local-user", email="", is_authenticated=False).storage_namespace
    assert updated.input_files[-1].file_name == "remote.raw"
    assert updated.input_files[-1].storage_backend == "s3"
    assert updated.input_files[-1].object_key == f"users/{local_namespace}/projects/Pilot_Study/inputs/upload-1/remote.raw"

    _, network_names, _, _ = load_project_configuration(project, updated)
    assert network_names == ["network.raw", "remote.raw"]


def test_webapi_rejects_more_than_18_mpi_processes(tmp_path, monkeypatch) -> None:
    if TestClient is None or create_app is None:
        pytest.skip("FastAPI test client dependencies are not installed.")
    projects_root = tmp_path / "api-projects"
    monkeypatch.setenv("GRIDLENS_API_PROJECTS_ROOT", str(projects_root))
    client = TestClient(create_app())

    xml = tmp_path / "input.xml"
    xml.write_text("<Configuration />", encoding="utf-8")
    network = tmp_path / "network.raw"
    network.write_text("0 / END", encoding="utf-8")

    project = Project("Pilot Study", projects_root / "Pilot_Study")
    project.save([xml, network], "input.xml")

    response = client.post(
        "/api/projects/Pilot_Study/runs",
        data={
            "image": "pnnl/gridpack:latest",
            "executable": "ca.x",
            "xml_file_name": "input.xml",
            "mpi_processes": "19",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "MPI process count must be 18 or fewer for the hosted web app."
