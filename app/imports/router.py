import hashlib
import uuid
from uuid import UUID

from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import storage
from app.config import settings
from app.deps import CurrentUser, DbSession
from app.errors import AppError
from app.models import ImportJob

router = APIRouter(tags=["imports"])


class ImportStatus(BaseModel):
    job_id: UUID
    status: str
    total: int
    done: int
    failed: int
    errors: list[dict]  # [{"row": n, "error": "..."}], capped at MAX_ERRORS_STORED


@router.post("/expenses/import", status_code=202)
async def start_import(file: UploadFile, user: CurrentUser, session: DbSession):
    # Stream into the hash in 1 MB chunks; Starlette already spools big uploads to disk, never into RAM.
    digest, size = hashlib.sha256(), 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > settings.max_import_bytes:
            raise AppError(413, "file_too_large", f"CSV must be at most {settings.max_import_bytes // (1024 * 1024)} MB")
        digest.update(chunk)
    if size == 0:
        raise AppError(422, "empty_file", "The uploaded file is empty")
    sha = digest.hexdigest()

    existing = (
        await session.execute(select(ImportJob.id).where(ImportJob.user_id == user.id, ImportJob.file_sha256 == sha))
    ).scalar_one_or_none()
    if existing:  # same file again: same job, nothing new is created
        return JSONResponse({"job_id": str(existing), "duplicate": True}, 202)

    job_id = uuid.uuid4()
    key = f"imports/{user.id}/{job_id}.csv"
    await file.seek(0)
    await storage.upload_fileobj(file.file, key)
    # ON CONFLICT covers two identical uploads racing each other.
    inserted = (
        await session.execute(
            pg_insert(ImportJob)
            .values(id=job_id, user_id=user.id, file_sha256=sha, s3_key=key, status="pending")
            .on_conflict_do_nothing(constraint="uq_import_jobs_user_file")
            .returning(ImportJob.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        await storage.delete(key)  # lost the race; the winner's upload is the one kept
        winner = (
            await session.execute(select(ImportJob.id).where(ImportJob.user_id == user.id, ImportJob.file_sha256 == sha))
        ).scalar_one()
        return JSONResponse({"job_id": str(winner), "duplicate": True}, 202)
    await session.commit()
    return JSONResponse({"job_id": str(job_id), "duplicate": False}, 202)


@router.get("/imports/{job_id}", response_model=ImportStatus)
async def import_status(job_id: UUID, user: CurrentUser, session: DbSession):
    job = (
        await session.execute(select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user.id))
    ).scalar_one_or_none()
    if job is None:
        raise AppError(404, "import_not_found", "Import job not found")
    return ImportStatus(job_id=job.id, status=job.status, total=job.total, done=job.done, failed=job.failed, errors=job.errors)
