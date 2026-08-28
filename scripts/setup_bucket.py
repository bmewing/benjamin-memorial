"""Create the Spaces bucket and set the CORS rule the upload page needs.

Safe to re-run. Reads credentials from .env in the project root.

    python scripts/setup_bucket.py

Note: setting CORS is a bucket-admin operation, and the scoped key in .env
cannot do it -- you will get AccessDenied. Make a throwaway full-access key,
use it just for this, and delete it again:

    doctl spaces keys create tmp-cors --grants 'bucket=;permission=fullaccess'
    SPACES_KEY=... SPACES_SECRET=... ALLOWED_ORIGINS=https://site python scripts/setup_bucket.py
    doctl spaces keys delete <access-key>

ALLOWED_ORIGINS takes a comma-separated list; give it every name the site
answers to, or uploads from the ones you missed will fail in the browser.
"""
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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
    bucket = os.environ.get("SPACES_BUCKET", "benjamin-memorial")
    region = os.environ.get("SPACES_REGION", "nyc3")
    endpoint = os.environ.get("SPACES_ENDPOINT") or f"https://{region}.digitaloceanspaces.com"

    if not os.environ.get("SPACES_KEY") or not os.environ.get("SPACES_SECRET"):
        sys.exit("SPACES_KEY / SPACES_SECRET are empty in .env")

    s3 = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["SPACES_KEY"],
        aws_secret_access_key=os.environ["SPACES_SECRET"],
        config=Config(signature_version="s3v4"),
    )

    try:
        s3.head_bucket(Bucket=bucket)
        print(f"bucket {bucket} already exists")
    except ClientError:
        s3.create_bucket(Bucket=bucket)
        print(f"created bucket {bucket}")

    # The page uploads straight from the browser, so Spaces has to allow the
    # cross-origin PUT. Narrow AllowedOrigins to the real app URL once it exists.
    origins = [o for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o]
    s3.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "PUT", "HEAD"],
                    "AllowedOrigins": origins,
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        },
    )
    print(f"CORS set, origins={origins}")
    print("\nBucket stays private. The site hands out short-lived signed links.")


if __name__ == "__main__":
    main()
