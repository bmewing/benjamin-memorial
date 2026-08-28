"""The page family and friends use to add photos to Benjamin's collection.

Uploads go straight from the browser to Spaces using a presigned PUT, so a
phone full of large photos never has to squeeze through this service. The
bucket stays private; the gallery hands out short-lived signed links.
"""
import json
import logging
import os
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("memorial.web")

STATIC = Path(__file__).parent / "static"

ALLOWED_PREFIXES = ("image/", "video/")
MAX_BYTES = 512 * 1024 * 1024  # 512 MB, generous enough for phone video
UPLOAD_URL_TTL = 60 * 60       # 1 hour to finish an upload
VIEW_URL_TTL = 60 * 60 * 6     # 6 hours for a gallery link

router = APIRouter()


class FileRequest(BaseModel):
    name: str = Field(max_length=300)
    type: str = Field(default="", max_length=120)
    size: int = Field(default=0, ge=0)


class PresignRequest(BaseModel):
    files: list[FileRequest] = Field(max_length=50)
    uploader: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=1000)


def _slug(name: str) -> str:
    """Make a filename safe to sit in an object key without losing its shape."""
    stem = unicodedata.normalize("NFKD", Path(name).stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "photo"
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", Path(name).suffix)[:12]
    return f"{stem[:80]}{suffix}"


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@router.post("/api/presign")
async def presign(req: PresignRequest) -> dict:
    if not req.files:
        raise HTTPException(400, "No files given.")

    s3 = storage.client()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    batch = uuid.uuid4().hex[:8]
    out = []

    for i, f in enumerate(req.files):
        content_type = (f.type or "application/octet-stream").split(";")[0].strip()
        if not content_type.startswith(ALLOWED_PREFIXES):
            raise HTTPException(400, f"{f.name} is not a photo or video.")
        if f.size > MAX_BYTES:
            raise HTTPException(400, f"{f.name} is larger than 512 MB.")

        key = f"web/{day}/{batch}-{i:02d}-{_slug(f.name)}"
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": storage.BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=UPLOAD_URL_TTL,
        )
        out.append({"key": key, "url": url})

    # One sidecar per batch records who sent them and what they wanted to say.
    # Never let this stop the upload -- the photos matter more than the note.
    if req.uploader.strip() or req.note.strip():
        try:
            s3.put_object(
                Bucket=storage.BUCKET,
                Key=f"web/{day}/{batch}-about.json",
                Body=json.dumps(
                    {
                        "uploader": req.uploader.strip(),
                        "note": req.note.strip(),
                        "files": [o["key"] for o in out],
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                ).encode(),
                ContentType="application/json",
            )
        except Exception:
            log.exception("could not save the note for batch %s", batch)

    log.info("presigned %d file(s) for %r", len(out), req.uploader.strip() or "anonymous")
    return {"files": out}


@router.get("/api/gallery")
async def gallery(limit: int = 300) -> dict:
    s3 = storage.client()
    items = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=storage.BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".json") or obj["Size"] == 0:
                continue
            items.append({"key": key, "when": obj["LastModified"], "size": obj["Size"]})

    items.sort(key=lambda o: o["when"], reverse=True)
    items = items[:limit]

    def decorate(item: dict) -> dict:
        try:
            head = s3.head_object(Bucket=storage.BUCKET, Key=item["key"])
            meta = head.get("Metadata", {})
            content_type = head.get("ContentType", "")
        except Exception:
            meta, content_type = {}, ""
        return {
            "key": item["key"],
            "when": item["when"].isoformat(),
            "kind": "video" if content_type.startswith("video/") else "image",
            "caption": meta.get("caption", ""),
            "from": meta.get("from", ""),
            "source": meta.get("source", "web"),
            "url": s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": storage.BUCKET, "Key": item["key"]},
                ExpiresIn=VIEW_URL_TTL,
            ),
        }

    with ThreadPoolExecutor(max_workers=16) as pool:
        decorated = list(pool.map(decorate, items))

    return {"count": len(decorated), "items": decorated}
