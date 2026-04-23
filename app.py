import logging
import os
import secrets
import io
from pathlib import Path
from urllib.parse import quote

from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core import (
    ADMIN_ID,
    FILTER_LABELS,
    add_or_update_file,
    bot,
    close_db,
    delete_file_record,
    dp,
    get_file,
    init_db,
    register_bot_commands,
    search_file,
)

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip() or "/webhook"
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
WEB_UI_ENABLED = os.getenv("WEB_UI_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
WEB_ADMIN_TOKEN = os.getenv("WEB_ADMIN_TOKEN", "").strip()
WEB_UPLOAD_CHAT_ID = os.getenv("WEB_UPLOAD_CHAT_ID", "").strip()

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

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
if WEB_UI_ENABLED and WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


def _normalize_filter(filter_key: str) -> str:
    normalized = filter_key.strip().lower()
    if normalized in FILTER_LABELS:
        return normalized
    return "all"


def _extract_token(request: Request) -> str:
    header_token = request.headers.get("x-admin-token", "").strip()
    if header_token:
        return header_token

    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    query_token = request.query_params.get("token", "").strip()
    return query_token


def _is_web_admin(request: Request) -> bool:
    if not WEB_ADMIN_TOKEN:
        return False
    provided = _extract_token(request)
    if not provided:
        return False
    return secrets.compare_digest(provided, WEB_ADMIN_TOKEN)


def _require_web_admin(request: Request) -> None:
    if not WEB_ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="WEB_ADMIN_TOKEN is not configured. Delete API is disabled.",
        )
    if not _is_web_admin(request):
        raise HTTPException(status_code=401, detail="Invalid admin token.")


def _check_web_enabled() -> None:
    if not WEB_UI_ENABLED:
        raise HTTPException(status_code=404, detail="Web UI disabled.")


def _resolve_upload_chat_id() -> int | None:
    if WEB_UPLOAD_CHAT_ID:
        try:
            return int(WEB_UPLOAD_CHAT_ID)
        except ValueError:
            logger.warning("Invalid WEB_UPLOAD_CHAT_ID: %s", WEB_UPLOAD_CHAT_ID)

    if ADMIN_ID is not None:
        return int(ADMIN_ID)

    return None


@app.get("/")
async def root() -> object:
    if WEB_UI_ENABLED:
        return RedirectResponse(url="/drive", status_code=307)
    return {"ok": True, "message": "WangPanBot is running"}


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


@app.get("/drive")
async def drive_page() -> FileResponse:
    _check_web_enabled()
    drive_path = WEB_DIR / "drive.html"
    if not drive_path.exists():
        raise HTTPException(status_code=404, detail="drive.html not found.")
    return FileResponse(drive_path)


@app.get("/api/filters")
async def api_filters() -> dict[str, object]:
    _check_web_enabled()
    filters = [
        {"key": key, "label": label}
        for key, label in FILTER_LABELS.items()
    ]
    return {"ok": True, "filters": filters}


@app.get("/api/files")
async def api_files(
    request: Request,
    q: str = "",
    type: str = Query("all"),  # noqa: A002
    page: int = 1,
    limit: int = 8,
) -> dict[str, object]:
    _check_web_enabled()
    safe_keyword = q.strip()
    safe_limit = max(1, min(20, int(limit)))
    safe_page = max(1, int(page))
    safe_filter = _normalize_filter(type)
    extension = None if safe_filter == "all" else safe_filter
    offset = (safe_page - 1) * safe_limit

    results, has_next, total_count, total_size = await search_file(
        keyword=safe_keyword,
        extension=extension,
        offset=offset,
        limit=safe_limit,
    )
    total_pages = max(1, (total_count + safe_limit - 1) // safe_limit)

    if safe_page > total_pages:
        safe_page = total_pages
        offset = (safe_page - 1) * safe_limit
        results, has_next, total_count, total_size = await search_file(
            keyword=safe_keyword,
            extension=extension,
            offset=offset,
            limit=safe_limit,
        )

    items = [
        {
            "id": record_id,
            "name": name,
            "get_command": f"/get {record_id}",
        }
        for record_id, name in results
    ]

    return {
        "ok": True,
        "items": items,
        "pagination": {
            "page": safe_page,
            "limit": safe_limit,
            "has_next": has_next,
            "total_pages": total_pages,
        },
        "summary": {
            "keyword": safe_keyword,
            "filter": safe_filter,
            "filter_label": FILTER_LABELS[safe_filter],
            "total_count": total_count,
            "total_size_bytes": total_size,
        },
        "permissions": {
            "is_web_admin": _is_web_admin(request),
            "delete_enabled": bool(WEB_ADMIN_TOKEN),
            "upload_enabled": _resolve_upload_chat_id() is not None,
        },
    }


@app.post("/api/upload")
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, object]:
    _check_web_enabled()
    _require_web_admin(request)

    upload_chat_id = _resolve_upload_chat_id()
    if upload_chat_id is None:
        raise HTTPException(
            status_code=400,
            detail="Set WEB_UPLOAD_CHAT_ID or ADMIN_ID to enable web upload.",
        )

    filename = (file.filename or "upload.bin").strip() or "upload.bin"
    payload = await file.read()
    await file.close()

    if not payload:
        raise HTTPException(status_code=400, detail="Empty file.")

    document = types.BufferedInputFile(payload, filename=filename)
    try:
        sent_message = await bot.send_document(
            chat_id=upload_chat_id,
            document=document,
            caption=f"Web upload: {filename}",
        )
    except Exception as exc:
        logger.exception("Web upload send_document failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to upload file to Telegram storage chat.",
        ) from exc

    if sent_message.document is None or not sent_message.document.file_id:
        raise HTTPException(status_code=502, detail="Telegram did not return file_id.")

    file_size = sent_message.document.file_size
    if file_size is None:
        file_size = len(payload)

    is_new = await add_or_update_file(
        name=filename,
        file_id=sent_message.document.file_id,
        file_size=int(file_size),
    )

    return {
        "ok": True,
        "is_new": is_new,
        "item": {
            "name": filename,
            "file_id": sent_message.document.file_id,
            "file_size": int(file_size),
        },
    }


@app.get("/api/files/{record_id}/download")
async def api_download_file(record_id: int) -> StreamingResponse:
    _check_web_enabled()
    file_data = await get_file(record_id)
    if file_data is None:
        raise HTTPException(status_code=404, detail="File not found.")

    file_id, filename = file_data
    try:
        telegram_file = await bot.get_file(file_id)
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "file is too big" in message:
            raise HTTPException(
                status_code=413,
                detail=(
                    "文件超过 Telegram Bot API 直连下载限制（20MB）。"
                    "请在 Telegram 中使用 /get 文件ID 获取，"
                    "或改用本地 Bot API Server。"
                ),
            ) from exc
        logger.exception("Failed to get telegram file metadata: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to fetch file metadata.") from exc
    except Exception as exc:
        logger.exception("Failed to get telegram file metadata: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to fetch file metadata.") from exc

    if not telegram_file.file_path:
        raise HTTPException(status_code=502, detail="Telegram file path is empty.")

    buffer = io.BytesIO()
    try:
        await bot.download_file(telegram_file.file_path, destination=buffer)
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "file is too big" in message:
            raise HTTPException(
                status_code=413,
                detail=(
                    "文件超过 Telegram Bot API 直连下载限制（20MB）。"
                    "请在 Telegram 中使用 /get 文件ID 获取，"
                    "或改用本地 Bot API Server。"
                ),
            ) from exc
        logger.exception("Failed to download telegram file content: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to download file content.") from exc
    except Exception as exc:
        logger.exception("Failed to download telegram file content: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to download file content.") from exc

    buffer.seek(0)
    safe_name = quote(filename, safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        buffer,
        media_type="application/octet-stream",
        headers=headers,
    )


@app.delete("/api/files/{record_id}")
async def api_delete_file(record_id: int, request: Request) -> dict[str, object]:
    _check_web_enabled()
    _require_web_admin(request)
    deleted = await delete_file_record(record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found.")
    return {"ok": True, "deleted_id": record_id}


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    await register_bot_commands()
    await bot.set_webhook(webhook_target)
    logger.info("Webhook has been set to %s", webhook_target)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await bot.delete_webhook()
    await close_db()
    await bot.session.close()
