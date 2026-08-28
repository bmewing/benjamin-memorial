# Benjamin's memorial photo collection

Two small services that gather photos of Benjamin into one private
DigitalOcean Spaces bucket:

| | |
|---|---|
| `webpage/` | The page people visit to upload photos and videos, plus a gallery of what's come in. |
| `twilio/` | Receives picture messages sent to the memorial phone number and files them into the same bucket. |
| `scripts/` | One-time bucket setup, and a downloader so you always hold your own copy. |

Both deploy as components of a single DigitalOcean App. The bucket is
**private** — nothing is publicly listable or guessable. The gallery works by
handing out links that expire after a few hours.

---

## Where things land in the bucket

```
sms/2026-09-14/15558675309-SM123abc-0.jpg   photo texted in
sms/2026-09-14/15558675309-SM123abc.json    who sent it + what they wrote
web/2026-09-14/8dfc5c3b-00-ben-at-lake.jpg  photo uploaded on the site
web/2026-09-14/8dfc5c3b-about.json          uploader's name + note
```

Texted photos carry the sender's number and any words they typed as object
metadata, so a caption is never separated from its picture.

---

## Setup

### 1. Make the bucket

In the DigitalOcean console: **Spaces → Create Bucket**, region **NYC3**,
name **`benjamin-memorial`**, file listing **Restricted**.

### 2. Make a key scoped to it

**Spaces → Access Keys → Generate New Key**, limited to `benjamin-memorial`
with read/write. Copy both halves — the secret is shown only once.

### 3. Fill in `.env`

```bash
cp .env.example .env
```

Paste the Spaces key/secret and your Twilio SID, auth token, and number.
`.env` is gitignored and must never be committed.

### 4. Apply the CORS rule

The upload page sends files straight from the browser to Spaces, which needs
Spaces to allow the cross-origin PUT:

```bash
python scripts/setup_bucket.py
```

Re-run this with `ALLOWED_ORIGINS=https://your-app-url` once the app is
deployed, to narrow it from `*` to just your site.

### 5. Deploy

Push this repo to GitHub, then:

```bash
doctl apps create --spec .do/app.yaml
```

Set `SPACES_KEY`, `SPACES_SECRET`, `TWILIO_ACCOUNT_SID`, and
`TWILIO_AUTH_TOKEN` as encrypted values in the app's settings — they are
marked `type: SECRET` in the spec and are deliberately left empty there.

### 6. Point Twilio at it

In the Twilio console, open your number and set **A message comes in** to:

```
https://<your-app>.ondigitalocean.app/twilio/sms      (HTTP POST)
```

App Platform strips the `/twilio` prefix before the request reaches the
service, which is why `PUBLIC_BASE_URL` in the spec ends in `/twilio` — the
signature check has to rebuild the exact URL Twilio signed.

---

## Running locally

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r webpage/requirements.txt

cd webpage && ../.venv/Scripts/python.exe -m uvicorn main:app --port 8099
```

Then open http://127.0.0.1:8099.

For the SMS side, set `SKIP_SIGNATURE_CHECK=1` so you can post test payloads
with curl. **Never set that in production** — it's what stops anyone on the
internet from writing into the bucket.

---

## Getting your copy of the photos

```bash
python scripts/download_all.py
```

Downloads everything into `downloads/`, skipping what you already have. Worth
running now and then so the collection isn't only in one place.

---

## Notes

- Photos and videos only; up to 512 MB each.
- The gallery does one metadata lookup per photo. Past a few hundred photos
  it'll want a cache or a stored index.
- `noindex` is set on the page, so it won't turn up in search results.
