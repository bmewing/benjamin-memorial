"""Permanently erase what was removed from the collection.

"Remove" in the private gallery moves an item under removed/ instead of
deleting it, so a wrong click is always recoverable. This is the separate,
deliberate step that actually erases those files. There is no undo.

    python scripts/purge_removed.py              # show what is in removed/
    python scripts/purge_removed.py --restore    # put it all back
    python scripts/purge_removed.py --erase      # erase it, for good
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
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_env()
    bucket = os.environ.get("SPACES_BUCKET", "benjamin-memorial")
    region = os.environ.get("SPACES_REGION", "nyc3")
    s3 = boto3.client(
        "s3", region_name=region,
        endpoint_url=os.environ.get("SPACES_ENDPOINT") or f"https://{region}.digitaloceanspaces.com",
        aws_access_key_id=os.environ["SPACES_KEY"],
        aws_secret_access_key=os.environ["SPACES_SECRET"],
        config=Config(signature_version="s3v4"),
    )

    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="removed/"):
        for obj in page.get("Contents", []):
            keys.append((obj["Key"], obj["Size"]))

    if not keys:
        print("removed/ is empty. Nothing has been taken out of the collection.")
        return

    photos = [k for k, _ in keys if not k.endswith(".json")]
    print(f"{len(keys)} file(s) under removed/, {len(photos)} of them photos:\n")
    for k, size in keys:
        print(f"  {size:>10,}  {k}")

    if "--restore" in sys.argv:
        print()
        for k, _ in keys:
            # removed/20260829T101500/sms/2026-08-28/x.jpg -> sms/2026-08-28/x.jpg
            original = k.split("/", 2)[2]
            s3.copy_object(Bucket=bucket, Key=original,
                           CopySource={"Bucket": bucket, "Key": k})
            s3.delete_object(Bucket=bucket, Key=k)
            print(f"  restored {original}")
        print("\nBack in the collection.")
        return

    if "--erase" not in sys.argv:
        print("\nNothing erased. Re-run with --erase to delete these for good,")
        print("or --restore to put them back in the collection.")
        return

    print(f"\nAbout to permanently erase {len(keys)} file(s). This cannot be undone.")
    answer = input('Type the word "erase" to confirm: ').strip()
    if answer != "erase":
        print("Stopped. Nothing was erased.")
        return

    for k, _ in keys:
        s3.delete_object(Bucket=bucket, Key=k)
        print(f"  erased {k}")
    print(f"\n{len(keys)} file(s) erased.")


if __name__ == "__main__":
    main()
