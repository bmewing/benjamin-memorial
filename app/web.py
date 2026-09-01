"""The page family and friends use to add photos to Benjamin's collection.

Uploads go straight from the browser to Spaces using a presigned PUT, so a
phone full of large photos never has to squeeze through this service. The
bucket stays private; the gallery hands out short-lived signed links.

Not everyone has a photo to send. A memory written with nothing attached is
stored the same way and shown in the family's view as its own item, because
words that arrive alone are the easiest thing in a system like this to lose.
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
    # Matches the story limit: the page now invites people to write at length,
    # and a long note arriving with photos must not be rejected for it.
    note: str = Field(default="", max_length=8000)


class StoryRequest(BaseModel):
    """A memory sent on its own, with no photograph attached."""
    uploader: str = Field(default="", max_length=120)
    # Roomier than a photo's caption: this is the whole submission.
    note: str = Field(max_length=8000)


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


@router.post("/api/story")
async def story(req: StoryRequest, background: BackgroundTasks) -> dict:
    """Take a memory with no photo attached.

    Unlike a photo, there is no presigned PUT to hand back -- the whole
    submission fits in this request, so it is written here and screened on the
    way out.
    """
    said = req.note.strip()
    if not said:
        raise HTTPException(400, "There is nothing written here yet.")

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"web/{day}/{uuid.uuid4().hex[:8]}-story.json"

    try:
        storage.client().put_object(
            Bucket=storage.BUCKET,
            Key=key,
            Body=json.dumps(
                {
                    "uploader": req.uploader.strip(),
                    "message": said,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ).encode(),
            ContentType="application/json",
        )
    except Exception:
        log.exception("could not save a written memory")
        raise HTTPException(500, "Could not save that. Please try again.")

    log.info("stored %s from %r", key, req.uploader.strip() or "anonymous")

    if moderation.enabled():
        background.add_task(moderation.review_text_key, storage, key,
                            said, req.uploader.strip())

    return {"ok": True}


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
    written: list[dict] = []
    present: set[str] = set()

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=storage.BUCKET):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if k.startswith("removed/"):
                continue  # taken out of the collection deliberately
            if k.endswith(moderation.REVIEW_SUFFIX):
                continue  # a verdict, not a submission
            if k.endswith(".json"):
                written.append({"key": k, "when": obj["LastModified"]})
                continue
            if obj["Size"] == 0:
                continue
            present.add(k)
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

    decorated += _written_items(s3, written, present, limit)
    decorated.sort(key=lambda i: i["when"], reverse=True)

    return {"count": len(decorated), "items": decorated[:limit]}


def _written_items(s3, written: list[dict], present: set[str], limit: int) -> list[dict]:
    """Words that arrived without a photo, as items of their own.

    Three shapes end up here, all of them invisible before: a text message with
    no picture attached, a memory written on the site, and the note from a web
    upload whose photos never actually made it into the bucket. Each is judged
    against `present` so nothing is listed twice -- a note that belongs to a
    photo already in the grid stays with that photo.
    """
    def load(item: dict) -> dict | None:
        k = item["key"]
        try:
            body = s3.get_object(Bucket=storage.BUCKET, Key=k)["Body"].read()
            doc = json.loads(body)
        except Exception:
            return None
        if not isinstance(doc, dict):
            return None

        if k.endswith("-story.json"):
            said, who = doc.get("message", ""), doc.get("uploader", "")
        elif k.endswith("-about.json"):
            # Only if every photo it was written for is missing; otherwise the
            # note is already showing under the picture it belongs to.
            if any(f in present for f in (doc.get("files") or [])):
                return None
            said, who = doc.get("note", ""), doc.get("uploader", "")
        elif k.startswith("sms/"):
            # A caption that came with a photo is shown on the photo.
            if doc.get("media_saved"):
                return None
            said, who = doc.get("message", ""), doc.get("from", "")
        else:
            return None

        said = (said or "").strip()
        if not said:
            return None

        verdict = {}
        try:
            verdict = json.loads(s3.get_object(
                Bucket=storage.BUCKET,
                Key=k + moderation.REVIEW_SUFFIX)["Body"].read())
        except Exception:
            pass

        when = doc.get("received_at") or item["when"].isoformat()
        return {
            "concern": verdict.get("concern", "unreviewed"),
            "flags": verdict.get("flags", []),
            "subject": verdict.get("subject", ""),
            "review_note": verdict.get("note", ""),
            "key": k,
            "when": when,
            "size": 0,
            "kind": "message",
            "caption": said,
            "from": who,
            "source": "sms" if k.startswith("sms/") else "web",
            "url": "",
        }

    if not written:
        return []
    written = sorted(written, key=lambda o: o["when"], reverse=True)[:limit]
    with ThreadPoolExecutor(max_workers=16) as pool:
        return [i for i in pool.map(load, written) if i]


class ItemRequest(BaseModel):
    item: str = Field(max_length=400)


def _guard(key: str) -> None:
    if not GALLERY_KEY or not secrets.compare_digest(key, GALLERY_KEY):
        raise HTTPException(404, "Not found.")


def _sane(item: str) -> str:
    """Only ever act on a submission we wrote, never an arbitrary path.

    Messages live in JSON objects, so those cannot be refused outright any
    more -- but only the three shapes the gallery actually lists, and never a
    .review.json verdict, which would let a click rewrite the screening record.
    """
    if not item.startswith(("sms/", "web/")) or ".." in item:
        raise HTTPException(400, "Not a submission.")
    if item.endswith(moderation.REVIEW_SUFFIX):
        raise HTTPException(400, "Not a submission.")
    if item.endswith(".json") and not (
            item.startswith("sms/") or item.endswith(("-story.json", "-about.json"))):
        raise HTTPException(400, "Not a submission.")
    return item


@router.post("/api/item/keep")
async def keep_item(req: ItemRequest, key: str = "") -> dict:
    """Mark a flagged item as fine. Records that a person decided, not a model."""
    _guard(key)
    item = _sane(req.item)
    s3 = storage.client()

    try:
        verdict = json.loads(s3.get_object(
            Bucket=storage.BUCKET,
            Key=item + moderation.REVIEW_SUFFIX)["Body"].read())
    except Exception:
        verdict = {}

    verdict.update({
        "concern": "none",
        "flags": [],
        "note": "",
        "cleared_by_human": True,
        "cleared_at": datetime.now(timezone.utc).isoformat(),
        "was": verdict.get("concern", "unreviewed"),
        "was_note": verdict.get("note", ""),
    })
    s3.put_object(Bucket=storage.BUCKET, Key=item + moderation.REVIEW_SUFFIX,
                  Body=json.dumps(verdict, indent=2).encode(),
                  ContentType="application/json")
    log.info("kept %s (was %s)", item, verdict["was"])
    return {"ok": True, "item": item}


@router.post("/api/item/remove")
async def remove_item(req: ItemRequest, key: str = "") -> dict:
    """Take an item out of the collection.

    It is moved under removed/ rather than erased. Deleting the wrong photo
    or the only copy of what someone wrote is the one mistake here that cannot
    be undone, and nothing about spam is urgent enough to be worth that risk.
    Emptying removed/ is a deliberate, separate act -- see
    scripts/purge_removed.py.
    """
    _guard(key)
    item = _sane(req.item)
    s3 = storage.client()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for suffix in ("", moderation.REVIEW_SUFFIX):
        src, dst = item + suffix, f"removed/{stamp}/{item}{suffix}"
        try:
            s3.copy_object(Bucket=storage.BUCKET, Key=dst,
                           CopySource={"Bucket": storage.BUCKET, "Key": src})
            s3.delete_object(Bucket=storage.BUCKET, Key=src)
        except Exception:
            if not suffix:               # the submission itself must move
                log.exception("could not remove %s", src)
                raise HTTPException(500, "Could not remove it.")

    log.info("removed %s -> removed/%s/", item, stamp)
    return {"ok": True, "item": item, "moved_to": f"removed/{stamp}/{item}"}


@router.get("/private")
async def private_gallery(key: str = "") -> FileResponse:
    """The family's view of what has arrived. Same key as the API."""
    if not GALLERY_KEY or not secrets.compare_digest(key, GALLERY_KEY):
        raise HTTPException(404, "Not found.")
    return FileResponse(STATIC / "private.html")
