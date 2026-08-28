"""Benjamin's memorial collection -- one service, two ways in.

The upload page and the Twilio webhook were separate App Platform components
once, which meant paying for two machines to do very little. They are one app
now: the page is served at /, and Twilio posts to /twilio/sms.

Because nothing strips a route prefix any more, PUBLIC_BASE_URL is simply the
app's root URL -- see sms.py for why that matters to signature checking.
"""
import logging

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import sms
import storage
import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Benjamin Memorial")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(web.router)
app.include_router(sms.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "bucket": storage.BUCKET}
