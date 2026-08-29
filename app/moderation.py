"""Look at what arrives before Benjamin's family has to.

A public phone number and an open upload form will eventually catch something
nobody should have to see unprepared. Every photo and message that comes in is
shown to a vision model first, which writes a small verdict alongside it.

Two rules shape everything here:

Nothing is ever deleted. A stranger's judgement about a grieving friend's
photograph is not good enough to throw it away, and a wrongly discarded
picture cannot be recovered. The verdict only decides whether the private
gallery shows a photo straight away or asks first.

Nothing is ever blocked from arriving. The reply the sender gets is the same
either way. Someone who has just sent a photo of a dead boy should not be told
a machine found their message wanting.

"Is this Benjamin?" is recorded but never counts against a photo. He hiked, he
turned wood, he cooked; a picture of Bays Mountain or a cutting board or a
group of friends is exactly the kind of thing worth keeping.
"""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("memorial.review")

BASE_URL = (os.environ.get("GRADIENT_BASE_URL", "").strip()
            or "https://inference.do-ai.run/v1")
API_KEY = os.environ.get("GRADIENT_API_KEY", "").strip()
MODEL = os.environ.get("GRADIENT_MODEL", "").strip() or "llama-4-maverick"

REVIEW_SUFFIX = ".review.json"

# Past this we do not try -- the request would be enormous and slow. The item
# is marked for a human to look at rather than quietly passed.
MAX_SCAN_BYTES = 8 * 1024 * 1024

# His portrait, sent along as a reference so "is this him" means something.
REFERENCE = Path(__file__).parent / "static" / "benjamin.jpg"

SYSTEM = """You screen submissions to an online memorial for a 16-year-old boy \
named Benjamin, who died by suicide. Family and friends send photographs and \
short messages. His parents read everything that arrives.

Your only job is to spot the rare submission that would hurt them: sexual or \
explicit imagery, gore, hateful or abusive content, threats, cruelty about his \
death, or obvious spam and advertising.

Be slow to flag. Grief is not a flag. A blurry, dark, mundane, or unrelated \
photo is not a flag. A sad, angry, or religious message is not a flag. A photo \
of a place, an object, a pet, a group of people, or a much younger Benjamin is \
completely normal and expected. Flag only what a reasonable person would agree \
his parents should be warned about before seeing.

Things Benjamin made and did, which people are sending in and which are \
precious to his family. None of this is ever spam:
- Woodturning. He ran a small business called "Ben's Pens" and sold turned \
pens, cutting boards, coasters and boxes at craft markets. Photos of his \
stall, his products, price signs and handwritten labels are his own work.
- Cooking and smoking meat, spice rubs, food at gatherings.
- Hiking and nature photography at Bays Mountain: trees, trails, insects.
- Laser cutting and 3D printing at a makerspace called the Inventor Center.

"Spam" means a stranger advertising something unrelated to Benjamin, or a \
scam or phishing link. A photograph of something Benjamin made himself, even \
with a price on it, is the opposite of spam.

Reply with JSON only, no prose, in exactly this shape:
{"concern": "none" | "review" | "serious",
 "flags": [],
 "subject": "benjamin" | "people" | "place_or_object" | "text_or_screenshot" | "unclear",
 "note": ""}

concern: "none" for anything fine. "review" if you are unsure or it looks like \
spam. "serious" only for content that is genuinely explicit, hateful, or cruel.
flags: short lowercase tags, e.g. ["spam"], ["explicit"], ["hateful"]. Empty when concern is none.
subject: your best guess at what the photo shows. Never a reason to flag.
note: one short sentence for the family, plain and factual. Empty when concern is none."""


def enabled() -> bool:
    return bool(API_KEY)


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def _reference_block() -> list[dict]:
    try:
        return [
            {"type": "text",
             "text": "For reference, this is Benjamin:"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{_b64(REFERENCE.read_bytes())}"}},
        ]
    except Exception:
        return []


def _verdict(concern: str, note: str, **extra) -> dict:
    out = {
        "concern": concern,
        "flags": [],
        "subject": "unclear",
        "note": note,
        "model": MODEL,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    out.update(extra)
    return out


def review(data: bytes, content_type: str, caption: str = "", sender: str = "") -> dict:
    """Return a verdict for one submission. Never raises."""
    if not enabled():
        return _verdict("unreviewed", "Screening is not configured.")

    if len(data) > MAX_SCAN_BYTES:
        return _verdict("review", "Too large to screen automatically.",
                        flags=["unscanned"])

    if not (content_type or "").startswith("image/"):
        # Video would need frame extraction; say so rather than imply it passed.
        return _verdict("review", "Video is not screened automatically.",
                        flags=["unscanned"])

    said = caption.strip()
    asked = "Here is the submission."
    if said:
        asked += f' The sender wrote: "{said}"'
    if sender:
        asked += " It arrived by text message." if sender.startswith("+") else ""

    content = _reference_block() + [
        {"type": "text", "text": asked},
        {"type": "image_url",
         "image_url": {"url": f"data:{content_type};base64,{_b64(data)}"}},
    ]

    try:
        r = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": MODEL,
                "max_tokens": 300,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": content},
                ],
            },
            timeout=90,
        )
        r.raise_for_status()
        said_back = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.exception("screening call failed")
        return _verdict("unreviewed", "Screening could not run.",
                        flags=["error"], error=str(exc)[:200])

    try:
        text = said_back.strip()
        if text.startswith("```"):                      # fenced JSON happens
            text = text.split("```")[1].removeprefix("json").strip()
        parsed = json.loads(text)
    except Exception:
        log.warning("screening returned unparseable output: %r", said_back[:200])
        return _verdict("review", "Screening result could not be read.",
                        flags=["error"])

    concern = str(parsed.get("concern", "review")).lower()
    if concern not in ("none", "review", "serious"):
        concern = "review"

    return _verdict(
        concern,
        str(parsed.get("note", ""))[:300],
        flags=[str(f)[:40] for f in (parsed.get("flags") or [])][:6],
        subject=str(parsed.get("subject", "unclear"))[:40],
    )


def review_key(storage, key: str, caption: str = "", sender: str = "") -> dict:
    """Screen an object already in the bucket and write its verdict beside it.

    Runs in the background after the sender has already been thanked, so a slow
    or failing model never delays or changes what they see.
    """
    s3 = storage.client()
    try:
        obj = s3.get_object(Bucket=storage.BUCKET, Key=key)
        data = obj["Body"].read()
        content_type = obj.get("ContentType", "")
        meta = obj.get("Metadata", {})
    except Exception:
        log.exception("could not read %s for screening", key)
        return _verdict("unreviewed", "Could not read the file to screen it.",
                        flags=["error"])

    # If a person has already looked at this and said it is fine, leave their
    # decision alone. A later sweep must not quietly re-flag what they cleared.
    try:
        prior = json.loads(s3.get_object(Bucket=storage.BUCKET,
                                         Key=key + REVIEW_SUFFIX)["Body"].read())
        if prior.get("cleared_by_human"):
            log.info("%s was cleared by a person; not re-screening", key)
            return prior
    except Exception:
        pass

    verdict = review(data, content_type,
                     caption or meta.get("caption", ""),
                     sender or meta.get("from", ""))
    verdict["key"] = key

    try:
        s3.put_object(
            Bucket=storage.BUCKET,
            Key=key + REVIEW_SUFFIX,
            Body=json.dumps(verdict, indent=2).encode(),
            ContentType="application/json",
        )
    except Exception:
        log.exception("could not store the verdict for %s", key)

    log.info("screened %s -> %s %s", key, verdict["concern"], verdict.get("flags") or "")
    return verdict
