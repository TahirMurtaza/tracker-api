"""All S3 access lives here. boto3 is blocking, so every call runs in a worker thread."""

import asyncio
import functools
from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import settings

# Allowed receipt types and the leading bytes that prove them.
CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


def sniff(head: bytes) -> str | None:
    """Return the extension implied by the file's first bytes, or None if unsupported."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"%PDF-"):
        return "pdf"
    return None


@functools.lru_cache
def _client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def reset_client() -> None:
    _client.cache_clear()


async def presigned_upload(key: str, content_type: str, expires: int = 300) -> dict:
    """Presigned POST (not PUT): only POST policies can enforce a size range server-side at S3."""

    def _make():
        return _client().generate_presigned_post(
            settings.s3_bucket,
            key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, settings.max_receipt_bytes]],
            ExpiresIn=expires,
        )

    return await asyncio.to_thread(_make)


async def presigned_download(key: str) -> str:
    def _make():
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key, "ResponseContentDisposition": "attachment"},
            ExpiresIn=settings.receipt_download_ttl_seconds,
        )

    return await asyncio.to_thread(_make)


async def head(key: str) -> int | None:
    """Size in bytes, or None if the object does not exist."""

    def _head():
        try:
            return _client().head_object(Bucket=settings.s3_bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    return await asyncio.to_thread(_head)


async def read_head(key: str, n: int = 16) -> bytes:
    def _read():
        body = _client().get_object(Bucket=settings.s3_bucket, Key=key, Range=f"bytes=0-{n - 1}")["Body"]
        return body.read()

    return await asyncio.to_thread(_read)


async def delete(key: str) -> None:
    """Idempotent: S3 returns success for a missing key."""
    await asyncio.to_thread(lambda: _client().delete_object(Bucket=settings.s3_bucket, Key=key))


async def upload_fileobj(fileobj: BinaryIO, key: str) -> None:
    await asyncio.to_thread(lambda: _client().upload_fileobj(fileobj, settings.s3_bucket, key))


async def download_file(key: str, path: str) -> None:
    await asyncio.to_thread(lambda: _client().download_file(settings.s3_bucket, key, path))
