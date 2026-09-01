"""Screen anything in the bucket that has not been looked at yet.

The live path screens each photo and message as it arrives, but a browser that
closed too early, a restart mid-upload, or a spell with no API key all leave
gaps. This finds them and fills them in. Safe to re-run; it only touches items
with no verdict yet.

    python scripts/screen_pending.py           # screen what is missing
    python scripts/screen_pending.py --check   # just prove the setup works
    python scripts/screen_pending.py --all     # re-screen everything
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        sys.exit(f"No {env} yet. Copy .env.example to .env and fill it in.")
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def check() -> None:
    """Confirm the endpoint answers and the model can actually see an image."""
    import httpx
    import moderation

    if not moderation.enabled():
        sys.exit("GRADIENT_API_KEY is empty in .env -- nothing to check.")

    print(f"endpoint : {moderation.BASE_URL}")
    print(f"model    : {moderation.MODEL}")

    try:
        r = httpx.get(f"{moderation.BASE_URL}/models",
                      headers={"Authorization": f"Bearer {moderation.API_KEY}"},
                      timeout=30)
        r.raise_for_status()
        ids = sorted(m["id"] for m in r.json().get("data", []))
    except Exception as exc:
        sys.exit(f"could not list models: {exc}")

    print(f"\n{len(ids)} models reachable. Vision-capable names usually contain "
          "'gpt-4o', 'claude', 'llama-vision' or similar:")
    for i in ids:
        print("   ", i)

    if moderation.MODEL not in ids:
        print(f"\n!! GRADIENT_MODEL={moderation.MODEL!r} is not in that list.")
        print("   Set GRADIENT_MODEL in .env to one of the ids above.")
        return

    print("\nsending his portrait through the screener as a live test...")
    v = moderation.review(moderation.REFERENCE.read_bytes(), "image/jpeg",
                          "a test", "")
    print(f"  concern : {v['concern']}")
    print(f"  subject : {v['subject']}")
    print(f"  flags   : {v['flags']}")
    print(f"  note    : {v['note'] or '(none)'}")
    if v["concern"] == "unreviewed":
        print("\n!! the call did not succeed -- see the error above")
    elif v["subject"] == "benjamin":
        print("\nWorking, and it recognised him.")
    else:
        print("\nWorking. It did not label the reference as Benjamin, which only "
              "affects the subject guess, never whether a photo is flagged.")


def words_in(s3, storage, key: str) -> tuple[str, str]:
    """Pull the words and the sender out of a message object, or ("", "").

    Mirrors _written_items in app/web.py: a texted message counts only when no
    photo came with it, and an upload's note counts only when it is the whole
    submission.
    """
    import json

    try:
        doc = json.loads(s3.get_object(Bucket=storage.BUCKET, Key=key)["Body"].read())
    except Exception:
        return "", ""
    if not isinstance(doc, dict):
        return "", ""

    if key.endswith("-story.json"):
        return str(doc.get("message", "")).strip(), str(doc.get("uploader", ""))
    if key.endswith("-about.json"):
        return str(doc.get("note", "")).strip(), str(doc.get("uploader", ""))
    if key.startswith("sms/") and not doc.get("media_saved"):
        return str(doc.get("message", "")).strip(), str(doc.get("from", ""))
    return "", ""


def main() -> None:
    load_env()
    if "--check" in sys.argv:
        check()
        return

    import moderation
    import storage

    if not moderation.enabled():
        sys.exit("GRADIENT_API_KEY is empty in .env -- nothing would be screened.")

    redo = "--all" in sys.argv
    s3 = storage.client()

    keys, words, reviewed = [], [], set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=storage.BUCKET):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if k.startswith("removed/"):
                continue
            if k.endswith(moderation.REVIEW_SUFFIX):
                reviewed.add(k[: -len(moderation.REVIEW_SUFFIX)])
            elif k.endswith(".json"):
                # A message object. Whether it holds words worth screening is
                # decided when it is read, below.
                words.append(k)
            elif obj["Size"]:
                keys.append(k)

    todo = keys if redo else [k for k in keys if k not in reviewed]
    said = words if redo else [k for k in words if k not in reviewed]
    print(f"{len(keys)} photos, {len(reviewed)} already screened, {len(todo)} to do")
    print(f"{len(words)} message files to look through\n")

    def report(v, k):
        mark = {"none": "  ok", "review": "  ?", "serious": "  !!"}.get(v["concern"], "  -")
        print(f"{mark:<5} {v['concern']:<10} {k}")
        if v["note"]:
            print(f"        {v['note']}")

    for k in todo:
        report(moderation.review_key(storage, k), k)

    # Words that arrived on their own. A caption that came with a photo is
    # screened with that photo, so it is skipped here.
    for k in said:
        text, who = words_in(s3, storage, k)
        if not text:
            continue
        report(moderation.review_text_key(storage, k, text, who), k)

    print("\nDone. Look at anything flagged in the private gallery.")


if __name__ == "__main__":
    main()
