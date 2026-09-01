# Benjamin's memorial collection

One small service that gathers photos of Benjamin, and what people want to say
about him, into one private DigitalOcean Spaces bucket. Two ways in:

| | |
|---|---|
| `/` | The page people visit to add photos and videos, or to write a memory with nothing attached. |
| `/twilio/sms` | Where Twilio posts picture messages, and plain text messages, sent to the memorial phone number. |

Not everyone has a photograph. A memory sent on its own is stored the same way
and shown in the family's private view as an item in its own right, because
words that arrive alone are the easiest thing in a system like this to lose.

| | |
|---|---|
| `app/` | The service. `web.py` is the page and its API, `sms.py` the webhook, `storage.py` the bucket. |
| `scripts/` | One-time bucket setup, and a downloader so you always hold your own copy. |

It runs as a single DigitalOcean App component — $5/month, plus $5 for the
Spaces bucket. The bucket is **private**: nothing is publicly listable or
guessable, and the gallery works by handing out links that expire after a few
hours.

---

## Where things land in the bucket

```
sms/2026-09-14/15558675309-SM123abc-0.jpg   photo texted in
sms/2026-09-14/15558675309-SM123abc.json    who sent it + what they wrote
web/2026-09-14/8dfc5c3b-00-ben-at-lake.jpg  photo uploaded on the site
web/2026-09-14/8dfc5c3b-about.json          uploader's name + note
web/2026-09-14/1f2e3d4c-story.json          a memory written with no photo
```

Texted photos carry the sender's number and any words they typed as object
metadata, so a caption is never separated from its picture.

The private view lists a `.json` as an item of its own only when the words are
the whole submission: a text with no picture attached, a `-story.json`, or an
`-about.json` whose photos never made it into the bucket. A note that belongs
to a photo that did arrive stays with that photo instead of being listed twice.

---

## Setup

### 1. Make the bucket

In the DigitalOcean console: **Spaces → Create Bucket**, region **NYC3**,
name **`benjamin-memorial`**, file listing **Restricted**.

### 2. Make a key scoped to it

Limited to this one bucket, so it can't touch anything else in the account:

```bash
doctl --context personal spaces keys create benjamin-memorial-app   --grants 'bucket=benjamin-memorial;permission=readwrite'
```

Copy both halves — the secret is shown only once. (Or do the same in the
console under **Spaces → Access Keys**.)

### 3. Fill in `.env`

```bash
cp .env.example .env
```

Paste the Spaces key/secret and your Twilio SID, auth token, and number.
`.env` is gitignored and must never be committed.

`SPACES_ENDPOINT` is the **region** endpoint, `https://nyc3.digitaloceanspaces.com`
— not the bucket's own URL. boto3 prepends the bucket name itself, so giving it
`benjamin-memorial.nyc3...` would resolve to the bucket name twice and fail.

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
doctl --context personal apps create --spec .do/app.yaml
```

The `--context personal` matters if you also have a work DigitalOcean account
— see `doctl auth list`.

Set `SPACES_KEY`, `SPACES_SECRET`, `TWILIO_ACCOUNT_SID`, and
`TWILIO_AUTH_TOKEN` as encrypted values in the app's settings — they are
marked `type: SECRET` in the spec and are deliberately left empty there.

### 6. Point Twilio at it

In the Twilio console, open your number and set **A message comes in** to:

```
https://<your-app>.ondigitalocean.app/twilio/sms      (HTTP POST)
```

`PUBLIC_BASE_URL` is just the app's root URL. Behind App Platform's proxy the
scheme on the incoming request is `http`, so the signature check rebuilds the
`https` URL Twilio actually signed from that base plus the request path.

---

## Running locally

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r app/requirements.txt

cd app && ../.venv/Scripts/python.exe -m uvicorn main:app --port 8099
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

- Photos and videos only; up to 512 MB each. Written memories cap at 8,000
  characters.
- Everything is screened before the family sees it, photos by a vision model
  and words on their own by the same model on the text alone. Nothing is ever
  blocked from arriving and nothing is ever deleted -- see the notes at the top
  of `app/moderation.py`.
- The gallery does one metadata lookup per photo. Past a few hundred photos
  it'll want a cache or a stored index.
- `noindex` is set on the page, so it won't turn up in search results.
