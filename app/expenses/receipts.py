import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import update

from app import outbox, storage
from app.config import settings
from app.deps import CurrentUser, DbSession
from app.errors import AppError
from app.expenses import service
from app.models import Expense

router = APIRouter(prefix="/expenses/{expense_id}/receipt", tags=["receipts"])


class UploadUrlIn(BaseModel):
    content_type: str


class ConfirmIn(BaseModel):
    key: str


@router.post("/upload-url")
async def upload_url(expense_id: int, body: UploadUrlIn, user: CurrentUser, session: DbSession):
    await service.get_owned(session, user.id, expense_id)
    ext = storage.CONTENT_TYPES.get(body.content_type)
    if ext is None:
        raise AppError(422, "unsupported_file_type", "Only image/jpeg, image/png and application/pdf are allowed")
    key = f"receipts/{user.id}/{expense_id}/{uuid.uuid4()}.{ext}"
    post = await storage.presigned_upload(key, body.content_type)
    return {"key": key, "url": post["url"], "fields": post["fields"], "max_bytes": settings.max_receipt_bytes, "expires_in": 300}


@router.post("/confirm")
async def confirm(expense_id: int, body: ConfirmIn, user: CurrentUser, session: DbSession):
    exp = await service.get_owned(session, user.id, expense_id)
    # The client names the key, so it must be inside this user's and this expense's prefix.
    prefix = f"receipts/{user.id}/{expense_id}/"
    if not body.key.startswith(prefix) or "/" in body.key[len(prefix) :] or ".." in body.key:
        raise AppError(422, "invalid_receipt_key", "Receipt key does not belong to this expense")
    size = await storage.head(body.key)
    if size is None:
        raise AppError(422, "receipt_not_uploaded", "No uploaded file found for this key")
    problem = None
    if size > settings.max_receipt_bytes:
        problem = ("receipt_too_large", "File is larger than 5 MB")
    else:
        real_ext = storage.sniff(await storage.read_head(body.key))
        if real_ext is None or real_ext != body.key.rsplit(".", 1)[-1]:
            problem = ("invalid_file_type", "File content is not a valid JPG, PNG or PDF")
    if problem:
        await storage.delete(body.key)  # never keep a rejected upload
        raise AppError(422, *problem)
    old = exp.receipt_key
    await session.execute(update(Expense).where(Expense.id == exp.id).values(receipt_key=body.key))
    if old and old != body.key:
        outbox.enqueue(session, outbox.S3_DELETE, {"key": old})  # replaced receipt gets cleaned up too
    await session.commit()
    return {"receipt_key": body.key}


@router.get("")
async def download(expense_id: int, user: CurrentUser, session: DbSession):
    exp = await service.get_owned(session, user.id, expense_id)
    if not exp.receipt_key:
        raise AppError(404, "receipt_not_found", "This expense has no receipt")
    return {"url": await storage.presigned_download(exp.receipt_key), "expires_in": settings.receipt_download_ttl_seconds}
