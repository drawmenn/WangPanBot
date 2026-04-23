import logging
import os

from aiogram import types
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core import bot, dp, init_db

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip() or "/webhook"
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()

if WEBHOOK_URL:
    webhook_target = WEBHOOK_URL
elif WEBHOOK_BASE_URL:
    webhook_target = f"{WEBHOOK_BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
elif RENDER_EXTERNAL_URL:
    webhook_target = f"{RENDER_EXTERNAL_URL.rstrip('/')}{WEBHOOK_PATH}"
else:
    raise RuntimeError(
        "Set WEBHOOK_URL (full url), WEBHOOK_BASE_URL (domain only), "
        "or provide RENDER_EXTERNAL_URL at runtime."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post(WEBHOOK_PATH)
async def webhook(req: Request):
    try:
        data = await req.json()
        update = types.Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("Failed to process webhook update")
        return JSONResponse(status_code=400, content={"ok": False})


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    await bot.set_webhook(webhook_target)
    logger.info("Webhook has been set to %s", webhook_target)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await bot.delete_webhook()
    await bot.session.close()
