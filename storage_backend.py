from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from io import BytesIO
from urllib.parse import urlparse


S3_SCHEME = "s3://"


def storage_backend() -> str:
    return os.getenv("STORAGE_BACKEND", "local").strip().lower()


def s3_bucket_name() -> str | None:
    return os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME") or os.getenv("BUCKET")


def s3_object_uri(key: str, bucket: str | None = None) -> str:
    bucket = bucket or s3_bucket_name()
    if not bucket:
        raise RuntimeError("S3_BUCKET is required for S3 object URIs.")
    return f"{S3_SCHEME}{bucket}/{key.strip('/')}"


def parse_s3_uri(value: str) -> tuple[str, str] | None:
    if not value or not value.startswith(S3_SCHEME):
        return None
    parsed = urlparse(value)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        return None
    return bucket, key


def is_s3_uri(value: str | None) -> bool:
    return bool(value and parse_s3_uri(value))


def guess_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def s3_client():
    import boto3
    from botocore.config import Config

    access_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("ACCESS_KEY_ID")
    secret_key = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("SECRET_ACCESS_KEY")
    region = os.getenv("S3_REGION") or os.getenv("AWS_DEFAULT_REGION") or os.getenv("REGION") or "auto"
    endpoint_url = os.getenv("S3_ENDPOINT_URL") or os.getenv("ENDPOINT")
    addressing_style = os.getenv("S3_ADDRESSING_STYLE", "virtual")

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": addressing_style}),
    )


def upload_file_to_s3(path: Path, key: str, bucket: str | None = None, client=None) -> str:
    bucket = bucket or s3_bucket_name()
    if not bucket:
        raise RuntimeError("S3_BUCKET is required to upload files.")

    client = client or s3_client()
    client.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"ContentType": guess_content_type(path)},
    )
    return s3_object_uri(key, bucket=bucket)


def upload_bytes_to_s3(data: bytes, key: str, content_type: str | None = None, bucket: str | None = None, client=None) -> str:
    bucket = bucket or s3_bucket_name()
    if not bucket:
        raise RuntimeError("S3_BUCKET or BUCKET is required to upload files.")

    client = client or s3_client()
    client.upload_fileobj(
        BytesIO(data),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type or "application/octet-stream"},
    )
    return s3_object_uri(key, bucket=bucket)


def open_s3_object(uri: str):
    parsed = parse_s3_uri(uri)
    if not parsed:
        raise ValueError(f"Not an S3 URI: {uri}")
    bucket, key = parsed
    response = s3_client().get_object(Bucket=bucket, Key=key)
    return response["Body"]


def s3_download_name(uri: str) -> str:
    parsed = parse_s3_uri(uri)
    if not parsed:
        return "download"
    return Path(parsed[1]).name
