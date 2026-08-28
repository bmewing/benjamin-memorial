"""Shared DigitalOcean Spaces helper."""
import os
import boto3
from botocore.config import Config


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


BUCKET = _env("SPACES_BUCKET", "benjamin-memorial")
REGION = _env("SPACES_REGION", "nyc3")
ENDPOINT = _env("SPACES_ENDPOINT") or f"https://{REGION}.digitaloceanspaces.com"


def client():
    return boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=ENDPOINT,
        aws_access_key_id=_env("SPACES_KEY"),
        aws_secret_access_key=_env("SPACES_SECRET"),
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def put(key: str, body: bytes, content_type: str, metadata: dict[str, str] | None = None) -> None:
    client().put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata={k: v for k, v in (metadata or {}).items() if v},
    )
