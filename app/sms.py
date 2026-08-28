"""Receives MMS sent to the memorial number and files each photo into Spaces.

Twilio POSTs here on every inbound message. We verify the request really came
from Twilio, pull down each media attachment, and write it to the bucket under
sms/<date>/. The sender's number and any words they typed are saved alongside
the photo so the message isn't lost.
"""
import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse, Response
from twilio.request_validator import RequestValidator

import moderation
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("memorial.sms")

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

# Set SKIP_SIGNATURE_CHECK=1 only for local testing with curl.
SKIP_SIGNATURE_CHECK = os.environ.get("SKIP_SIGNATURE_CHECK", "") == "1"

REPLY = (
    "Thank you for sharing this with us. "
    "It's been added to Benjamin's memorial collection."
)
REPLY_NO_MEDIA = (
    "Thank you for your message. If you'd like to add a photo, "
    "just send it as a picture message to this number."
)

router = APIRouter()


def _twiml(message: str) -> Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{message}</Message></Response>"
    )
    return Response(content=body, media_type="application/xml")


def _safe_number(raw: str) -> str:
    """+1 555 867 5309 -> 15558675309, for use inside an object key."""
    return re.sub(r"\D", "", raw or "") or "unknown"


def _extension(content_type: str, fallback: str = ".jpg") -> str:
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    # guess_extension gives .jpe for image/jpeg, which nothing opens happily.
    return {".jpe": ".jpg"}.get(ext, ext) or fallback


def _candidate_urls(request: Request) -> list[str]:
    """The URLs Twilio might have signed, best guess first.

    Twilio signs the exact URL typed into its console, so the check has to
    rebuild that string character for character. Two things get in the way:
    behind App Platform's proxy the scheme on request.url is http, and the
    site answers to more than one name -- the memorial domain and the
    ondigitalocean.app address. Deriving the host from the request means
    either one validates without a config change, which is one less way for
    a texted photo to be turned away.

    PUBLIC_BASE_URL is still honoured if set, as an override.
    """
    path = request.url.path
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc)
    scheme = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()

    urls = [f"{scheme}://{host}{path}"]
    if PUBLIC_BASE_URL:
        urls.append(f"{PUBLIC_BASE_URL}{path}")
    if str(request.url) not in urls:
        urls.append(str(request.url))
    return urls


async def _valid_signature(request: Request, form: dict[str, str]) -> bool:
    if SKIP_SIGNATURE_CHECK:
        return True
    if not AUTH_TOKEN:
        log.error("TWILIO_AUTH_TOKEN is unset; refusing request")
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(AUTH_TOKEN)
    # Trying a handful of spellings is safe: each one still has to match the
    # HMAC, which needs the auth token nobody else has.
    for url in _candidate_urls(request):
        if validator.validate(url, form, signature):
            return True

    # Say which URLs were tried -- a silent 403 here is very hard to diagnose,
    # and the usual cause is simply the wrong address in the Twilio console.
    log.warning("no signature match; tried %s", _candidate_urls(request))
    return False


@router.post("/twilio/sms")
async def sms(request: Request, background: BackgroundTasks) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}

    if not await _valid_signature(request, form):
        log.warning("rejected request with bad Twilio signature")
        return PlainTextResponse("invalid signature", status_code=403)

    sender = form.get("From", "")
    body_text = (form.get("Body") or "").strip()
    message_sid = form.get("MessageSid") or form.get("SmsSid") or "unknown"
    num_media = int(form.get("NumMedia", "0") or 0)

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"sms/{day}/{_safe_number(sender)}-{message_sid}"

    saved = 0
    for i in range(num_media):
        media_url = form.get(f"MediaUrl{i}")
        content_type = form.get(f"MediaContentType{i}", "image/jpeg")
        if not media_url:
            continue
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
                resp = await http.get(media_url, auth=(ACCOUNT_SID, AUTH_TOKEN))
                resp.raise_for_status()
                data = resp.content
        except Exception:
            log.exception("could not download media %s from %s", i, message_sid)
            continue

        key = f"{prefix}-{i}{_extension(content_type)}"
        try:
            storage.put(
                key,
                data,
                content_type,
                metadata={
                    "source": "sms",
                    "from": sender,
                    "message-sid": message_sid,
                    "caption": body_text[:1800],
                },
            )
        except Exception:
            log.exception("could not store media %s from %s", i, message_sid)
            continue

        saved += 1
        log.info("stored %s (%d bytes) from %s", key, len(data), sender)

        # Screen it after this request returns. The sender is thanked either
        # way, and Twilio times out long before a vision model would answer.
        if moderation.enabled():
            background.add_task(moderation.review_key, storage, key, body_text, sender)

    # Keep the words people sent, whether or not a photo came with them.
    if body_text or saved:
        storage.put(
            f"{prefix}.json",
            json.dumps(
                {
                    "from": sender,
                    "message": body_text,
                    "message_sid": message_sid,
                    "media_saved": saved,
                    "media_sent": num_media,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ).encode(),
            "application/json",
        )

    return _twiml(REPLY if saved else REPLY_NO_MEDIA)
