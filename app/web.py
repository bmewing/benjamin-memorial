"""The page family and friends use to add photos to Benjamin's collection.

Uploads go straight from the browser to Spaces using a presigned PUT, so a
phone full of large photos never has to squeeze through this service. The
bucket stays private; the gallery hands out short-lived signed links.
"""
import json
import logging
import os
import re
import secrets
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import moderation
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("memorial.web")

STATIC = Path(__file__).parent / "static"

ALLOWED_PREFIXES = ("image/", "video/")
MAX_BYTES = 512 * 1024 * 1024  # 512 MB, generous enough for phone video
UPLOAD_URL_TTL = 60 * 60       # 1 hour to finish an upload
VIEW_URL_TTL = 60 * 60 * 6     # 6 hours for a gallery link

# The page does not show what people upload -- the photos are the family's, not
# an exhibit. The listing endpoint stays closed unless a key is set, so nobody
# can enumerate the collection by guessing the URL.
GALLERY_KEY = os.environ.get("GALLERY_KEY", "").strip()

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


class UploadedRequest(BaseModel):
    keys: list[str] = Field(max_length=50)


@router.post("/api/uploaded")
async def uploaded(req: UploadedRequest, background: BackgroundTasks) -> dict:
    """The page calls this once its files are in the bucket.

    Uploads go browser-to-Spaces so this service never touches the bytes and
    has no other way to know something arrived. Anything missed here is caught
    later by scripts/screen_pending.py.
    """
    queued = 0
    for key in req.keys:
        if not key.startswith("web/") or key.endswith(".json"):
            continue
        if moderation.enabled():
            background.add_task(moderation.review_key, storage, key, "", "")
        queued += 1
    return {"queued": queued}


@router.get("/api/gallery")
async def gallery(key: str = "", limit: int = 300) -> dict:
    """List what has come in. Closed unless GALLERY_KEY is set and matches."""
    if not GALLERY_KEY or not secrets.compare_digest(key, GALLERY_KEY):
        raise HTTPException(404, "Not found.")

    s3 = storage.client()
    items: list[dict] = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=storage.BUCKET):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if k.endswith(".json") or obj["Size"] == 0:
                continue  # covers the .review.json verdicts too
            items.append({"key": k, "when": obj["LastModified"], "size": obj["Size"]})

    items.sort(key=lambda o: o["when"], reverse=True)
    items = items[:limit]

    def sidecar_for(k: str) -> str:
        """web/2026-09-14/8dfc5c3b-00-photo.jpg -> web/2026-09-14/8dfc5c3b-about.json"""
        head, _, name = k.rpartition("/")
        return f"{head}/{name.split('-')[0]}-about.json"

    # A texted photo carries its sender and words as object metadata, but a web
    # upload goes browser-to-Spaces and cannot, so those live in one sidecar per
    # batch. Read them, or the notes people wrote never surface anywhere.
    def load_sidecar(sk: str) -> tuple[str, dict]:
        try:
            body = s3.get_object(Bucket=storage.BUCKET, Key=sk)["Body"].read()
            return sk, json.loads(body)
        except Exception:
            return sk, {}

    wanted = {sidecar_for(i["key"]) for i in items if i["key"].startswith("web/")}
    notes: dict[str, dict] = {}
    if wanted:
        with ThreadPoolExecutor(max_workers=16) as pool:
            notes = dict(pool.map(load_sidecar, wanted))

    # What the screening model made of each item, if it has run yet.
    with ThreadPoolExecutor(max_workers=16) as pool:
        verdicts = dict(pool.map(load_sidecar,
                                 [i["key"] + moderation.REVIEW_SUFFIX for i in items]))

    def decorate(item: dict) -> dict:
        k = item["key"]
        try:
            head = s3.head_object(Bucket=storage.BUCKET, Key=k)
            meta = head.get("Metadata", {})
            content_type = head.get("ContentType", "")
        except Exception:
            meta, content_type = {}, ""

        note = notes.get(sidecar_for(k), {}) if k.startswith("web/") else {}
        verdict = verdicts.get(k + moderation.REVIEW_SUFFIX, {})
        return {
            "concern": verdict.get("concern", "unreviewed"),
            "flags": verdict.get("flags", []),
            "subject": verdict.get("subject", ""),
            "review_note": verdict.get("note", ""),
            "key": k,
            "when": item["when"].isoformat(),
            "size": item["size"],
            "kind": "video" if content_type.startswith("video/") else "image",
            "caption": meta.get("caption") or note.get("note", ""),
            "from": meta.get("from") or note.get("uploader", ""),
            "source": meta.get("source") or ("sms" if k.startswith("sms/") else "web"),
            "url": s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": storage.BUCKET, "Key": k},
                ExpiresIn=VIEW_URL_TTL,
            ),
        }

    with ThreadPoolExecutor(max_workers=16) as pool:
        decorated = list(pool.map(decorate, items))

    return {"count": len(decorated), "items": decorated}


@router.get("/private")
async def private_gallery(key: str = "") -> FileResponse:
    """The family's view of what has arrived. Same key as the API."""
    if not GALLERY_KEY or not secrets.compare_digest(key, GALLERY_KEY):
        raise HTTPException(404, "Not found.")
    return FileResponse(STATIC / "private.html")
