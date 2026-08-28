"""Pull every photo, video, and message out of the bucket onto this machine.

Run this whenever you want your own copy. Re-running only fetches what is new.

    python scripts/download_all.py [destination]
"""
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        sys.exit(f"No {env} yet. Copy .env.example to .env and fill it in.")
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_env()
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "downloads"
    bucket = os.environ.get("SPACES_BUCKET", "benjamin-memorial")
    region = os.environ.get("SPACES_REGION", "nyc3")

    s3 = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=os.environ.get("SPACES_ENDPOINT") or f"https://{region}.digitaloceanspaces.com",
        aws_access_key_id=os.environ["SPACES_KEY"],
        aws_secret_access_key=os.environ["SPACES_SECRET"],
        config=Config(signature_version="s3v4"),
    )

    got = skipped = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            target = dest / obj["Key"]
            if target.exists() and target.stat().st_size == obj["Size"]:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(target))
            got += 1
            print(f"  {obj['Key']}")

    print(f"\n{got} new, {skipped} already had. Everything is in {dest}")


if __name__ == "__main__":
    main()
