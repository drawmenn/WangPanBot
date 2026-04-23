from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

try:
    from pyrogram import Client, raw, utils
    from pyrogram.errors import AuthBytesInvalid, RPCError
    from pyrogram.file_id import FileId, FileType, ThumbnailSource
    from pyrogram.session import Auth, Session

    PYROGRAM_AVAILABLE = True
except Exception as exc:  # pragma: no cover - only used when dependency is missing
    Client = Any  # type: ignore[assignment]
    Session = Any  # type: ignore[assignment]
    raw = None  # type: ignore[assignment]
    utils = None  # type: ignore[assignment]
    FileId = Any  # type: ignore[assignment]
    FileType = Any  # type: ignore[assignment]
    ThumbnailSource = Any  # type: ignore[assignment]
    Auth = Any  # type: ignore[assignment]
    AuthBytesInvalid = Exception
    RPCError = Exception
    PYROGRAM_AVAILABLE = False
    _PYROGRAM_IMPORT_ERROR = exc

logger = logging.getLogger(__name__)


def _parse_optional_int(name: str) -> Optional[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("%s must be an integer, got: %s", name, value)
        return None


def _parse_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def _parse_chunk_size() -> int:
    raw_value = os.getenv("MTPROTO_CHUNK_SIZE", "").strip()
    if not raw_value:
        return 1024 * 1024
    try:
        chunk_size = int(raw_value)
    except ValueError:
        logger.warning("MTPROTO_CHUNK_SIZE must be an integer, got: %s", raw_value)
        return 1024 * 1024
    return max(64 * 1024, min(4 * 1024 * 1024, chunk_size))


class MTProtoStreamer:
    def __init__(self) -> None:
        self._enabled = _parse_bool("MTPROTO_DOWNLOAD_ENABLED", True)
        self._api_id = _parse_optional_int("API_ID") or _parse_optional_int("TG_API_ID")
        self._api_hash = os.getenv("API_HASH", "").strip() or os.getenv("TG_API_HASH", "").strip()
        self._bot_token = os.getenv("MTPROTO_BOT_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()
        self._session_name = os.getenv("MTPROTO_SESSION_NAME", "wangpanbot_mtproto").strip() or "wangpanbot_mtproto"
        self._workdir = Path(os.getenv("MTPROTO_WORKDIR", ".mtproto")).resolve()
        self._chunk_size = _parse_chunk_size()
        self._client: Optional[Client] = None

    @property
    def is_configured(self) -> bool:
        return (
            PYROGRAM_AVAILABLE
            and self._enabled
            and self._api_id is not None
            and bool(self._api_hash)
            and bool(self._bot_token)
        )

    @property
    def is_available(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if not PYROGRAM_AVAILABLE:
            logger.warning(
                "MTProto streaming disabled because pyrogram is unavailable: %s",
                _PYROGRAM_IMPORT_ERROR,
            )
            return

        if not self._enabled:
            logger.info("MTProto streaming disabled by MTPROTO_DOWNLOAD_ENABLED.")
            return

        if self._api_id is None or not self._api_hash:
            logger.warning(
                "MTProto streaming is not configured. Set API_ID and API_HASH to enable >20MB web downloads."
            )
            return

        if not self._bot_token:
            logger.warning("MTProto streaming is not configured. BOT_TOKEN is missing.")
            return

        self._workdir.mkdir(parents=True, exist_ok=True)
        try:
            self._client = Client(
                name=self._session_name,
                api_id=self._api_id,
                api_hash=self._api_hash,
                bot_token=self._bot_token,
                no_updates=True,
                workdir=str(self._workdir),
            )
            await self._client.start()
            logger.info("MTProto streaming client started.")
        except Exception:
            logger.exception("Failed to start MTProto streaming client.")
            self._client = None

    async def stop(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop()
            logger.info("MTProto streaming client stopped.")
        except Exception:
            logger.exception("Failed to stop MTProto streaming client.")
        finally:
            self._client = None

    async def stream_response(
        self,
        request: Request,
        telegram_file_id: str,
        filename: str,
        file_size: int,
    ) -> StreamingResponse | Response:
        if not PYROGRAM_AVAILABLE or raw is None:
            raise HTTPException(status_code=503, detail="Pyrogram is not installed.")

        if self._client is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Large-file web download is not ready. "
                    "Set API_ID and API_HASH, then redeploy."
                ),
            )

        try:
            decoded_file_id = FileId.decode(telegram_file_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid Telegram file_id.") from exc

        media_session = await self._get_media_session(self._client, decoded_file_id)
        location = self._get_location(decoded_file_id)

        known_size = int(file_size) if int(file_size) > 0 else None
        range_header = request.headers.get("range", "").strip()

        if known_size is None and range_header:
            return Response(
                status_code=416,
                content="Range requests are not supported when file_size is unknown.",
            )

        if known_size is not None:
            start, end, is_partial = self._parse_range(range_header, known_size)
            response_length = end - start + 1
        else:
            start = 0
            end = None
            is_partial = False
            response_length = None

        aligned_offset = start - (start % self._chunk_size)
        first_chunk_cut = start - aligned_offset

        first_chunk = await self._read_chunk(media_session, location, aligned_offset)
        if not first_chunk:
            raise HTTPException(status_code=404, detail="File bytes not found on Telegram.")

        mime_type = mimetypes.guess_type(filename.lower())[0] or "application/octet-stream"
        disposition = "attachment"
        if (
            mime_type.startswith("video/")
            or mime_type.startswith("audio/")
            or mime_type.startswith("image/")
            or mime_type.startswith("text/")
            or mime_type.endswith("/html")
        ):
            disposition = "inline"

        safe_name = quote(filename, safe="")
        headers = {
            "Content-Type": mime_type,
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{safe_name}",
            "Cache-Control": "no-store",
        }

        if known_size is not None:
            headers["Accept-Ranges"] = "bytes"
            headers["Content-Length"] = str(response_length)
            if is_partial:
                headers["Content-Range"] = f"bytes {start}-{end}/{known_size}"

        body = self._iter_chunks(
            media_session=media_session,
            location=location,
            first_chunk=first_chunk,
            next_offset=aligned_offset + len(first_chunk),
            first_chunk_cut=first_chunk_cut,
            remaining=response_length,
        )

        return StreamingResponse(
            content=body,
            media_type=mime_type,
            status_code=206 if is_partial else 200,
            headers=headers,
        )

    async def _get_media_session(self, client: Client, file_id: FileId) -> Session:
        media_session = client.media_sessions.get(file_id.dc_id, None)
        if media_session is not None:
            return media_session

        if file_id.dc_id != await client.storage.dc_id():
            media_session = Session(
                client,
                file_id.dc_id,
                await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(),
                await client.storage.test_mode(),
                is_media=True,
            )
            await media_session.start()

            for _ in range(6):
                exported_auth = await client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                )
                try:
                    await media_session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes,
                        )
                    )
                    break
                except AuthBytesInvalid:
                    continue
            else:
                await media_session.stop()
                raise AuthBytesInvalid("Failed to import authorization for media DC.")
        else:
            media_session = Session(
                client,
                file_id.dc_id,
                await client.storage.auth_key(),
                await client.storage.test_mode(),
                is_media=True,
            )
            await media_session.start()

        client.media_sessions[file_id.dc_id] = media_session
        return media_session

    @staticmethod
    def _get_location(
        file_id: FileId,
    ) -> (
        raw.types.InputPhotoFileLocation
        | raw.types.InputDocumentFileLocation
        | raw.types.InputPeerPhotoFileLocation
    ):
        if raw is None or utils is None:
            raise HTTPException(status_code=503, detail="Pyrogram is not installed.")

        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id,
                    access_hash=file_id.chat_access_hash,
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )

            return raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )

        if file_type == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )

        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )

    async def _read_chunk(self, media_session: Session, location, offset: int) -> bytes:
        if raw is None:
            raise HTTPException(status_code=503, detail="Pyrogram is not installed.")

        try:
            result = await media_session.invoke(
                raw.functions.upload.GetFile(
                    location=location,
                    offset=offset,
                    limit=self._chunk_size,
                )
            )
        except RPCError as exc:
            message = str(exc)
            logger.exception("MTProto GetFile failed: %s", message)
            if "FILE_REFERENCE" in message.upper():
                raise HTTPException(
                    status_code=409,
                    detail="File reference expired. Re-upload this file to refresh metadata.",
                ) from exc
            raise HTTPException(status_code=502, detail="Telegram MTProto download failed.") from exc

        if not isinstance(result, raw.types.upload.File):
            return b""
        return bytes(result.bytes or b"")

    async def _iter_chunks(
        self,
        media_session: Session,
        location,
        first_chunk: bytes,
        next_offset: int,
        first_chunk_cut: int,
        remaining: Optional[int],
    ):
        chunk = first_chunk
        offset = next_offset
        cut = first_chunk_cut
        left = remaining

        while True:
            if cut:
                if cut >= len(chunk):
                    cut -= len(chunk)
                    chunk = b""
                else:
                    chunk = chunk[cut:]
                    cut = 0

            if chunk:
                if left is not None and len(chunk) > left:
                    chunk = chunk[:left]
                if chunk:
                    yield chunk
                    if left is not None:
                        left -= len(chunk)
                        if left <= 0:
                            break

            chunk = await self._read_chunk(media_session, location, offset)
            if not chunk:
                break
            offset += len(chunk)

    @staticmethod
    def _parse_range(range_header: str, file_size: int) -> tuple[int, int, bool]:
        if not range_header:
            return 0, file_size - 1, False
        if not range_header.startswith("bytes="):
            raise HTTPException(status_code=416, detail="Invalid Range header.")

        value = range_header[6:]
        if "," in value:
            raise HTTPException(status_code=416, detail="Multiple ranges are not supported.")

        start_raw, sep, end_raw = value.partition("-")
        if not sep:
            raise HTTPException(status_code=416, detail="Invalid Range header.")

        try:
            if start_raw == "":
                suffix_length = int(end_raw)
                if suffix_length <= 0:
                    raise ValueError
                start = max(file_size - suffix_length, 0)
                end = file_size - 1
            else:
                start = int(start_raw)
                end = int(end_raw) if end_raw else file_size - 1
        except ValueError as exc:
            raise HTTPException(status_code=416, detail="Invalid Range value.") from exc

        if start < 0 or end < start or start >= file_size:
            raise HTTPException(
                status_code=416,
                detail="Range not satisfiable.",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        end = min(end, file_size - 1)
        return start, end, True


mtproto_streamer = MTProtoStreamer()
