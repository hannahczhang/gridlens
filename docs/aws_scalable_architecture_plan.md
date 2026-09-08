# GridLens Scalable AWS Architecture Plan

This plan describes the path from the current EC2-hosted GridLens web app to a scalable AWS deployment with per-user isolation, usage accounting, and compute that scales down when idle.

## Current Implementation

The current web deployment already includes:

- React frontend hosted separately from the API.
- Cognito sign-in support in the frontend.
- FastAPI backend with optional Cognito bearer-token verification.
- Per-user project isolation using the Cognito `sub` claim.
- Project upload, XML configuration generation, run creation, run logs, output listing, output downloads, and ZIP exports.
- GridPACK execution through Docker on a single EC2 host.
- Local filesystem storage under a configured projects root.
- A local in-process `ThreadPoolExecutor` for background run and analysis jobs.

This is enough for a controlled internal prototype, but it is not yet a true scalable multi-user compute service.

## Target Architecture

The target system should split into a control plane and a compute plane.

Control plane:

- Cognito authenticates users.
- API Gateway exposes the backend and validates user tokens.
- FastAPI handles project metadata, upload permissions, run requests, analysis requests, and download permissions.
- DynamoDB stores project, run, analysis, ownership, file metadata, and usage records.
- S3 stores all user inputs, run outputs, manifests, logs, generated XML files, graph data, and exports.

Compute plane:

- AWS Batch runs GridPACK containers.
- AWS Batch runs separate GridLens analysis containers.
- ECR stores the GridPACK and GridLens analysis images.
- EventBridge receives Batch job status events.
- A small Lambda event handler updates DynamoDB job records and usage accounting.
- CloudWatch stores logs and operational metrics.

## Recommended Data Model

DynamoDB can start with a single-table design or separate tables. For early implementation, separate tables are easier to reason about.

Projects table:

- `user_id`: Cognito `sub`.
- `project_id`: generated stable ID.
- `name`: user-facing name.
- `created_at`, `updated_at`.
- `status`: `created`, `ready`, `archived`.

Project files table:

- `user_id`.
- `project_id`.
- `file_id`.
- `s3_key`.
- `file_name`.
- `file_type`: `raw`, `xml`, `csv`, `other`.
- `size_bytes`.
- `checksum_sha256`.
- `created_at`.

Runs table:

- `user_id`.
- `project_id`.
- `run_id`.
- `batch_job_id`.
- `status`: `queued`, `running`, `completed`, `failed`, `canceled`.
- `requested_vcpus`.
- `requested_memory_mb`.
- `gridpack_image`.
- `submitted_at`, `started_at`, `completed_at`.
- `input_manifest_s3_key`.
- `output_manifest_s3_key`.
- `failure_reason`.

Analyses table:

- `user_id`.
- `project_id`.
- `run_id`.
- `analysis_id`.
- `batch_job_id`.
- `analysis_type`.
- `status`.
- `submitted_at`, `started_at`, `completed_at`.
- `input_parameters`.
- `output_manifest_s3_key`.

Usage table:

- `user_id`.
- `usage_id`.
- `project_id`, `run_id`, `analysis_id`.
- `batch_job_id`.
- `requested_vcpus`.
- `requested_memory_mb`.
- `runtime_seconds`.
- `vcpu_seconds`.
- `input_bytes`.
- `output_bytes`.
- `created_at`.

## S3 Key Layout

Use user-scoped keys so IAM and application checks both reinforce privacy.

```text
users/{user_id}/projects/{project_id}/inputs/{file_id}/{file_name}
users/{user_id}/projects/{project_id}/generated/input.xml
users/{user_id}/projects/{project_id}/runs/{run_id}/input-manifest.json
users/{user_id}/projects/{project_id}/runs/{run_id}/outputs/{file_name}
users/{user_id}/projects/{project_id}/runs/{run_id}/output-manifest.json
users/{user_id}/projects/{project_id}/runs/{run_id}/logs/{file_name}
users/{user_id}/projects/{project_id}/runs/{run_id}/exports/{run_id}.zip
users/{user_id}/projects/{project_id}/runs/{run_id}/analyses/{analysis_id}/manifest.json
users/{user_id}/projects/{project_id}/runs/{run_id}/analyses/{analysis_id}/data/{file_name}
```

## Phase 1: Harden The Current EC2 Deployment

Goal: keep the current app working while preparing for cloud storage and queued jobs.

- Keep GitHub Pages, Cognito, FastAPI, nginx, and EC2.
- Keep Docker-based GridPACK execution on EC2.
- Add stricter API request limits and upload-size checks.
- Add durable job state records on disk so restarts do not lose track of jobs.
- Add a queue limit so the API does not start too many simultaneous runs.
- Add run cancelation and clearer failure states.
- Add structured metadata manifests for uploads, runs, outputs, and analyses.

This phase is useful because it stabilizes the product before moving compute away from the API host.

## Phase 2: Move Files To S3

Goal: remove user files from EC2 disk.

- Create a private S3 bucket.
- Add a storage abstraction in the backend.
- Keep local disk as one implementation for development.
- Add S3 as the production implementation.
- Replace direct API file uploads with presigned S3 uploads.
- Add upload-complete verification endpoint.
- Store file metadata in a local JSON store first, then DynamoDB in Phase 3.

Important implementation rule: large RAW files should go directly from the browser to S3. Do not proxy them through Lambda or API Gateway.

## Phase 3: Move Metadata To DynamoDB

Goal: make project and job state durable and independent of one EC2 machine.

- Add a repository/data-access layer for projects, files, runs, analyses, and usage.
- Keep filesystem JSON as the local development repository.
- Add DynamoDB as the production repository.
- Migrate existing EC2 project records into DynamoDB if needed.
- Make every query require `user_id`.

This is the phase where per-user ownership becomes truly cloud-native instead of folder-based.

## Phase 4: Move GridPACK Runs To AWS Batch

Goal: make GridPACK compute scale with demand.

- Build a GridPACK Batch image and push it to ECR.
- Create an AWS Batch managed EC2 compute environment.
- Set minimum vCPUs to `0`.
- Set maximum vCPUs based on quota and budget.
- Create a job queue and job definition.
- Replace the current direct Docker runner with a Batch submitter.
- The Batch job downloads inputs from S3, runs GridPACK, uploads outputs, and writes an output manifest.
- EventBridge updates run state from Batch events.

At this point, compute can scale down when idle.

## Phase 5: Move GridLens Analysis To AWS Batch

Goal: run graph and table generation separately from GridPACK.

- Build a GridLens analysis-worker image and push it to ECR.
- Add analysis job definitions for the expected analysis types.
- Store analysis results and chart data in S3.
- Return presigned download URLs for result files.
- Let the frontend poll analysis status by `analysis_id`.

This keeps the API responsive while large analysis jobs run elsewhere.

## Phase 6: Add Usage Accounting

Goal: record enough data to explain usage and estimate cost.

- Record every Batch attempt.
- Capture requested vCPUs, memory, start time, stop time, status, retries, input size, output size, image version, and user ID.
- Compute `vcpu_seconds = requested_vcpus * runtime_seconds`.
- Summarize usage by user and month.
- Keep AWS billing separate from user accounting because EC2 warm capacity, logs, API calls, and transfer costs will not match one-to-one.

## Implementation Boundary Changes

The current backend should be refactored toward three replaceable interfaces:

- `ProjectRepository`: owns project, run, analysis, file, and usage metadata.
- `ObjectStore`: owns input/output storage, presigned upload URLs, and presigned download URLs.
- `JobRunner`: owns run submission, analysis submission, status refresh, and cancelation.

Local development implementations:

- `FilesystemProjectRepository`
- `LocalObjectStore`
- `DockerJobRunner`

AWS production implementations:

- `DynamoDbProjectRepository`
- `S3ObjectStore`
- `BatchJobRunner`

This lets the app remain testable locally while production uses AWS services.

## What Not To Do

- Do not run Docker inside a Batch container unless there is a strong operational reason.
- Do not send large RAW files through Lambda.
- Do not trust frontend-provided user IDs.
- Do not store user files under shared S3 prefixes without checking ownership in the API.
- Do not make S3 objects public; use short-lived presigned URLs for upload and download.
- Do not depend on one always-on EC2 instance for the final compute design.

## First Build Step

The next code step should be Phase 2 scaffolding:

1. Add `ObjectStore` and `ProjectRepository` interfaces.
2. Move current filesystem project operations behind those interfaces.
3. Keep all existing endpoints working.
4. Add presigned-upload API shapes behind feature flags.
5. Add tests proving user A cannot access user B's project metadata or files.

Once that boundary exists, the AWS pieces can be added incrementally without rewriting the frontend repeatedly.
