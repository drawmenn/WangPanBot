import asyncio
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Optional, Protocol

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _parse_optional_int(name: str) -> Optional[int]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None

    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {raw_value}") from exc


def _parse_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {raw_value}") from exc


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required. Please set it in environment variables.")

DB_PROVIDER = os.getenv("DB_PROVIDER", "sqlite").strip().lower() or "sqlite"
DB_PATH = os.getenv("DB_PATH", "data.db").strip() or "data.db"
SEARCH_LIMIT = max(1, min(20, _parse_int("SEARCH_LIMIT", 5)))
SEARCH_SESSION_TTL_SECONDS = max(300, _parse_int("SEARCH_SESSION_TTL_SECONDS", 1800))
POSTGRES_POOL_SIZE = max(1, min(20, _parse_int("POSTGRES_POOL_SIZE", 5)))
ADMIN_ID = _parse_optional_int("ADMIN_ID")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

FILTER_LABELS: dict[str, str] = {
    "all": "全部",
    "pdf": "PDF",
    "doc": "DOC",
    "docx": "DOCX",
    "xls": "XLS",
    "xlsx": "XLSX",
    "ppt": "PPT",
    "pptx": "PPTX",
    "txt": "TXT",
    "csv": "CSV",
    "md": "MD",
    "mp4": "MP4",
    "mkv": "MKV",
    "avi": "AVI",
    "mp3": "MP3",
    "wav": "WAV",
    "flac": "FLAC",
    "jpg": "JPG",
    "jpeg": "JPEG",
    "png": "PNG",
    "gif": "GIF",
    "zip": "ZIP",
    "rar": "RAR",
    "7z": "7Z",
    "tar": "TAR",
    "gz": "GZ",
    "epub": "EPUB",
    "mobi": "MOBI",
}
FILTERS_PER_ROW = 4

# token -> (keyword, last_access_unix_time)
_search_sessions: dict[str, tuple[str, float]] = {}


def _extract_extension(name: str) -> Optional[str]:
    cleaned = name.strip().lower()
    if "." not in cleaned:
        return None
    ext = cleaned.rsplit(".", maxsplit=1)[1]
    return ext or None


def _is_admin_user(user: Optional[types.User]) -> bool:
    return ADMIN_ID is not None and user is not None and user.id == ADMIN_ID


def _cleanup_search_sessions() -> None:
    now = time.time()
    expired_tokens = [
        token
        for token, (_, last_access) in _search_sessions.items()
        if now - last_access > SEARCH_SESSION_TTL_SECONDS
    ]
    for token in expired_tokens:
        _search_sessions.pop(token, None)


def _create_search_token(keyword: str) -> str:
    _cleanup_search_sessions()
    while True:
        token = secrets.token_hex(4)
        if token not in _search_sessions:
            _search_sessions[token] = (keyword, time.time())
            return token


def _get_search_keyword(token: str) -> Optional[str]:
    _cleanup_search_sessions()
    state = _search_sessions.get(token)
    if state is None:
        return None

    keyword, _ = state
    _search_sessions[token] = (keyword, time.time())
    return keyword


def _normalize_filter(filter_key: str) -> str:
    if filter_key in FILTER_LABELS:
        return filter_key
    return "all"


def _filter_to_extension(filter_key: str) -> Optional[str]:
    normalized_filter = _normalize_filter(filter_key)
    if normalized_filter == "all":
        return None
    return normalized_filter


def _short_button_text(text: str, limit: int = 42) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


class FileStore(Protocol):
    async def init(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def add_or_update_file(self, name: str, file_id: str) -> bool:
        ...

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool]:
        ...

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        ...

    async def delete_file_record(self, record_id: int) -> bool:
        ...


class SQLiteStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_id TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
            await db.commit()

    async def close(self) -> None:
        return None

    async def add_or_update_file(self, name: str, file_id: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute("SELECT 1 FROM files WHERE file_id = ?", (file_id,))
            exists = await cursor.fetchone() is not None

            await db.execute(
                """
                INSERT INTO files (name, file_id)
                VALUES (?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name
                """,
                (name, file_id),
            )
            await db.commit()
            return not exists

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool]:
        async with aiosqlite.connect(self._db_path) as db:
            query = """
                SELECT id, name
                FROM files
                WHERE LOWER(name) LIKE LOWER(?)
            """
            params: list[object] = [f"%{keyword}%"]

            if extension is not None:
                query += " AND LOWER(name) LIKE ?"
                params.append(f"%.{extension.lower()}")

            query += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit + 1, max(0, offset)])

            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        return [(int(row[0]), str(row[1])) for row in visible_rows], has_next

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT file_id, name FROM files WHERE id = ?",
                (record_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return str(row[0]), str(row[1])

    async def delete_file_record(self, record_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute("SELECT 1 FROM files WHERE id = ?", (record_id,))
            exists = await cursor.fetchone() is not None
            if not exists:
                return False

            await db.execute("DELETE FROM files WHERE id = ?", (record_id,))
            await db.commit()
            return True


class PostgresStore:
    def __init__(self, dsn: str, pool_size: int) -> None:
        self._dsn = dsn
        self._pool_size = pool_size
        self._pool: Optional["asyncpg.Pool"] = None

    async def _get_pool(self):
        if self._pool is not None:
            return self._pool

        import asyncpg

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=1,
            max_size=self._pool_size,
            statement_cache_size=0,
        )
        return self._pool

    async def init(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    file_id TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")

    async def close(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def add_or_update_file(self, name: str, file_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM files WHERE file_id = $1", file_id)
            await conn.execute(
                """
                INSERT INTO files (name, file_id)
                VALUES ($1, $2)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = EXCLUDED.name
                """,
                name,
                file_id,
            )
            return not bool(exists)

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if extension is None:
                rows = await conn.fetch(
                    """
                    SELECT id, name
                    FROM files
                    WHERE name ILIKE $1
                    ORDER BY id DESC
                    LIMIT $2 OFFSET $3
                    """,
                    f"%{keyword}%",
                    limit + 1,
                    max(0, offset),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, name
                    FROM files
                    WHERE name ILIKE $1
                      AND LOWER(name) LIKE $2
                    ORDER BY id DESC
                    LIMIT $3 OFFSET $4
                    """,
                    f"%{keyword}%",
                    f"%.{extension.lower()}",
                    limit + 1,
                    max(0, offset),
                )

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        return [(int(row["id"]), str(row["name"])) for row in visible_rows], has_next

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT file_id, name FROM files WHERE id = $1",
                record_id,
            )

        if row is None:
            return None
        return str(row["file_id"]), str(row["name"])

    async def delete_file_record(self, record_id: int) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM files WHERE id = $1 RETURNING 1",
                record_id,
            )
            return row is not None


class TursoStore:
    def __init__(self, database_url: str, auth_token: str, local_path: str) -> None:
        self._database_url = database_url
        self._auth_token = auth_token
        self._local_path = local_path

    def _connect(self):
        import libsql

        if self._local_path:
            return libsql.connect(
                self._local_path,
                sync_url=self._database_url,
                auth_token=self._auth_token or None,
            )

        if self._auth_token:
            return libsql.connect(self._database_url, auth_token=self._auth_token)

        return libsql.connect(self._database_url)

    @staticmethod
    def _safe_close(conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def _init_sync(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_id TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
            conn.commit()
        finally:
            self._safe_close(conn)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        return None

    def _add_or_update_sync(self, name: str, file_id: str) -> bool:
        conn = self._connect()
        try:
            exists_cursor = conn.execute("SELECT 1 FROM files WHERE file_id = ?", (file_id,))
            exists = exists_cursor.fetchone() is not None

            conn.execute(
                """
                INSERT INTO files (name, file_id)
                VALUES (?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name
                """,
                (name, file_id),
            )
            conn.commit()
            return not exists
        finally:
            self._safe_close(conn)

    async def add_or_update_file(self, name: str, file_id: str) -> bool:
        return await asyncio.to_thread(self._add_or_update_sync, name, file_id)

    def _search_sync(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool]:
        conn = self._connect()
        try:
            query = """
                SELECT id, name
                FROM files
                WHERE LOWER(name) LIKE LOWER(?)
            """
            params: list[object] = [f"%{keyword}%"]

            if extension is not None:
                query += " AND LOWER(name) LIKE ?"
                params.append(f"%.{extension.lower()}")

            query += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit + 1, max(0, offset)])

            rows = conn.execute(query, tuple(params)).fetchall()
        finally:
            self._safe_close(conn)

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        return [(int(row[0]), str(row[1])) for row in visible_rows], has_next

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool]:
        return await asyncio.to_thread(self._search_sync, keyword, extension, offset, limit)

    def _get_file_sync(self, record_id: int) -> Optional[tuple[str, str]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT file_id, name FROM files WHERE id = ?",
                (record_id,),
            ).fetchone()
        finally:
            self._safe_close(conn)

        if row is None:
            return None
        return str(row[0]), str(row[1])

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        return await asyncio.to_thread(self._get_file_sync, record_id)

    def _delete_sync(self, record_id: int) -> bool:
        conn = self._connect()
        try:
            exists = conn.execute("SELECT 1 FROM files WHERE id = ?", (record_id,)).fetchone()
            if exists is None:
                return False

            conn.execute("DELETE FROM files WHERE id = ?", (record_id,))
            conn.commit()
            return True
        finally:
            self._safe_close(conn)

    async def delete_file_record(self, record_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, record_id)


class MongoStore:
    def __init__(self, uri: str, db_name: str, collection_name: str) -> None:
        self._uri = uri
        self._db_name = db_name
        self._collection_name = collection_name

        self._client = None
        self._db = None
        self._files = None
        self._counters = None

        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo import ASCENDING, DESCENDING, ReturnDocument
        from pymongo.errors import ConfigurationError, DuplicateKeyError

        self._AsyncIOMotorClient = AsyncIOMotorClient
        self._ASCENDING = ASCENDING
        self._DESCENDING = DESCENDING
        self._ReturnDocument = ReturnDocument
        self._ConfigurationError = ConfigurationError
        self._DuplicateKeyError = DuplicateKeyError

    async def init(self) -> None:
        self._client = self._AsyncIOMotorClient(self._uri)

        if self._db_name:
            self._db = self._client[self._db_name]
        else:
            try:
                default_db = self._client.get_default_database()
            except self._ConfigurationError:
                default_db = None
            self._db = default_db if default_db is not None else self._client["wangpanbot"]

        self._files = self._db[self._collection_name]
        self._counters = self._db["counters"]

        await self._files.create_index([("file_id", self._ASCENDING)], unique=True)
        await self._files.create_index([("record_id", self._ASCENDING)], unique=True)
        await self._files.create_index([("name", self._ASCENDING)])
        await self._files.create_index([("ext", self._ASCENDING)])

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def _next_record_id(self) -> int:
        doc = await self._counters.find_one_and_update(
            {"_id": "files"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=self._ReturnDocument.AFTER,
        )
        return int(doc["seq"])

    async def add_or_update_file(self, name: str, file_id: str) -> bool:
        extension = _extract_extension(name)
        existing = await self._files.find_one({"file_id": file_id}, {"_id": 0, "record_id": 1})

        if existing is not None:
            await self._files.update_one(
                {"file_id": file_id},
                {"$set": {"name": name, "ext": extension}},
            )
            return False

        record_id = await self._next_record_id()
        try:
            await self._files.insert_one(
                {
                    "record_id": record_id,
                    "name": name,
                    "file_id": file_id,
                    "ext": extension,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return True
        except self._DuplicateKeyError:
            await self._files.update_one(
                {"file_id": file_id},
                {"$set": {"name": name, "ext": extension}},
            )
            return False

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool]:
        query: dict[str, object] = {
            "name": {"$regex": re.escape(keyword), "$options": "i"}
        }
        if extension is not None:
            query["ext"] = extension.lower()

        cursor = (
            self._files.find(query, {"_id": 0, "record_id": 1, "name": 1})
            .sort("record_id", self._DESCENDING)
            .skip(max(0, offset))
            .limit(limit + 1)
        )
        rows = await cursor.to_list(length=limit + 1)

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        return [
            (int(row["record_id"]), str(row["name"]))
            for row in visible_rows
            if "record_id" in row and "name" in row
        ], has_next

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        row = await self._files.find_one(
            {"record_id": record_id},
            {"_id": 0, "file_id": 1, "name": 1},
        )
        if row is None:
            return None
        return str(row["file_id"]), str(row["name"])

    async def delete_file_record(self, record_id: int) -> bool:
        result = await self._files.delete_one({"record_id": record_id})
        return result.deleted_count > 0


def _resolve_postgres_dsn(provider: str) -> str:
    if provider == "supabase":
        dsn = (
            os.getenv("SUPABASE_DATABASE_URL", "").strip()
            or os.getenv("SUPABASE_DB_URL", "").strip()
            or os.getenv("DATABASE_URL", "").strip()
        )
        if not dsn:
            raise RuntimeError(
                "For DB_PROVIDER=supabase, set SUPABASE_DATABASE_URL "
                "(or SUPABASE_DB_URL / DATABASE_URL)."
            )
        return dsn

    if provider == "neon":
        dsn = (
            os.getenv("NEON_DATABASE_URL", "").strip()
            or os.getenv("DATABASE_URL", "").strip()
        )
        if not dsn:
            raise RuntimeError(
                "For DB_PROVIDER=neon, set NEON_DATABASE_URL (or DATABASE_URL)."
            )
        return dsn

    raise RuntimeError(f"Unsupported postgres provider: {provider}")


def _build_file_store() -> FileStore:
    if DB_PROVIDER == "sqlite":
        return SQLiteStore(DB_PATH)

    if DB_PROVIDER in {"supabase", "neon"}:
        return PostgresStore(_resolve_postgres_dsn(DB_PROVIDER), POSTGRES_POOL_SIZE)

    if DB_PROVIDER == "mongodb":
        mongo_uri = os.getenv("MONGODB_URI", "").strip()
        if not mongo_uri:
            raise RuntimeError("For DB_PROVIDER=mongodb, set MONGODB_URI.")
        mongo_db_name = os.getenv("MONGODB_DB_NAME", "").strip()
        mongo_collection_name = os.getenv("MONGODB_COLLECTION_NAME", "files").strip() or "files"
        return MongoStore(
            uri=mongo_uri,
            db_name=mongo_db_name,
            collection_name=mongo_collection_name,
        )

    if DB_PROVIDER == "turso":
        turso_database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
        if not turso_database_url:
            raise RuntimeError("For DB_PROVIDER=turso, set TURSO_DATABASE_URL.")
        turso_auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
        turso_local_path = os.getenv("TURSO_LOCAL_PATH", "").strip()
        return TursoStore(
            database_url=turso_database_url,
            auth_token=turso_auth_token,
            local_path=turso_local_path,
        )

    raise RuntimeError(
        "Unsupported DB_PROVIDER. Use one of: sqlite, supabase, mongodb, turso, neon."
    )


file_store = _build_file_store()


async def init_db() -> None:
    await file_store.init()
    logger.info("Database initialized with provider: %s", DB_PROVIDER)


async def close_db() -> None:
    await file_store.close()


async def add_or_update_file(name: str, file_id: str) -> bool:
    return await file_store.add_or_update_file(name=name, file_id=file_id)


async def search_file(
    keyword: str,
    extension: Optional[str] = None,
    offset: int = 0,
    limit: int = SEARCH_LIMIT,
) -> tuple[list[tuple[int, str]], bool]:
    return await file_store.search_file(
        keyword=keyword,
        extension=extension,
        offset=offset,
        limit=limit,
    )


async def get_file(record_id: int) -> Optional[tuple[str, str]]:
    return await file_store.get_file(record_id=record_id)


async def delete_file_record(record_id: int) -> bool:
    return await file_store.delete_file_record(record_id=record_id)


def _build_search_keyboard(
    results: list[tuple[int, str]],
    token: str,
    filter_key: str,
    offset: int,
    has_next: bool,
    can_delete: bool,
) -> InlineKeyboardMarkup:
    keyboard_rows: list[list[InlineKeyboardButton]] = []

    for file_record_id, name in results:
        file_label = _short_button_text(name)
        file_button = InlineKeyboardButton(
            text=file_label,
            callback_data=f"file_{file_record_id}",
        )

        if can_delete:
            delete_button = InlineKeyboardButton(
                text="删除",
                callback_data=f"del:{file_record_id}:{token}:{filter_key}:{offset}",
            )
            keyboard_rows.append([file_button, delete_button])
        else:
            keyboard_rows.append([file_button])

    filter_buttons: list[InlineKeyboardButton] = []
    for current_filter_key, filter_label in FILTER_LABELS.items():
        button_label = f"[{filter_label}]" if current_filter_key == filter_key else filter_label
        filter_buttons.append(
            InlineKeyboardButton(
                text=button_label,
                callback_data=f"s:{token}:{current_filter_key}:0",
            )
        )

    for index in range(0, len(filter_buttons), FILTERS_PER_ROW):
        keyboard_rows.append(filter_buttons[index : index + FILTERS_PER_ROW])

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_offset = max(0, offset - SEARCH_LIMIT)
        nav_row.append(
            InlineKeyboardButton(
                text="上一页",
                callback_data=f"s:{token}:{filter_key}:{prev_offset}",
            )
        )
    if has_next:
        next_offset = offset + SEARCH_LIMIT
        nav_row.append(
            InlineKeyboardButton(
                text="下一页",
                callback_data=f"s:{token}:{filter_key}:{next_offset}",
            )
        )
    if nav_row:
        keyboard_rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


async def _build_search_view(
    keyword: str,
    token: str,
    filter_key: str,
    offset: int,
    can_delete: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    safe_filter = _normalize_filter(filter_key)
    safe_offset = max(0, offset)
    extension = _filter_to_extension(safe_filter)

    results, has_next = await search_file(
        keyword=keyword,
        extension=extension,
        offset=safe_offset,
        limit=SEARCH_LIMIT,
    )

    if not results and safe_offset > 0:
        safe_offset = max(0, safe_offset - SEARCH_LIMIT)
        results, has_next = await search_file(
            keyword=keyword,
            extension=extension,
            offset=safe_offset,
            limit=SEARCH_LIMIT,
        )

    filter_label = FILTER_LABELS[safe_filter]
    if results:
        page_number = safe_offset // SEARCH_LIMIT + 1
        text = (
            "搜索结果\n"
            f"关键词: {keyword}\n"
            f"类型: {filter_label} | 第 {page_number} 页 | 本页 {len(results)} 条"
        )
    else:
        text = (
            "没找到相关文件\n"
            f"关键词: {keyword}\n"
            f"类型: {filter_label}"
        )

    keyboard = _build_search_keyboard(
        results=results,
        token=token,
        filter_key=safe_filter,
        offset=safe_offset,
        has_next=has_next,
        can_delete=can_delete,
    )
    return text, keyboard


@dp.message(Command("start"))
async def start(msg: types.Message) -> None:
    await msg.answer("网盘 Bot 已启动，直接输入关键词即可搜索。")


@dp.message(Command("help"))
async def help_command(msg: types.Message) -> None:
    lines = [
        "使用说明:",
        "1) 发送文件可自动收录",
        "2) 发送关键词可搜索文件",
        "3) 搜索结果支持分页浏览",
        "4) 支持常用文件类型筛选（文档/视频/音频/图片/压缩包）",
        "5) 点击结果按钮可回传文件",
    ]
    if ADMIN_ID is not None:
        lines.append("6) 管理员可点“删除”按钮，或使用 /delete <文件ID>")

    await msg.answer("\n".join(lines))


@dp.message(Command("delete"))
async def delete_by_command(msg: types.Message, command: CommandObject) -> None:
    if not _is_admin_user(msg.from_user):
        await msg.answer("仅管理员可使用删除命令。")
        return

    if command.args is None:
        await msg.answer("用法: /delete 文件ID")
        return

    try:
        record_id = int(command.args.strip())
    except ValueError:
        await msg.answer("文件ID 必须是整数。")
        return

    deleted = await delete_file_record(record_id)
    if deleted:
        await msg.answer(f"已删除文件 ID: {record_id}")
    else:
        await msg.answer("文件不存在或已删除。")


@dp.message(F.document)
async def save_file(msg: types.Message) -> None:
    if (
        msg.chat.type == "private"
        and ADMIN_ID is not None
        and msg.from_user is not None
        and msg.from_user.id != ADMIN_ID
    ):
        await msg.answer("当前仅允许管理员在私聊上传文件。")
        return

    if not msg.document.file_id or not msg.document.file_name:
        await msg.answer("文件信息不完整，无法收录。")
        return

    is_new = await add_or_update_file(msg.document.file_name, msg.document.file_id)

    if msg.chat.type == "private":
        if is_new:
            await msg.answer(f"已收录: {msg.document.file_name}")
        else:
            await msg.answer(f"已更新: {msg.document.file_name}")


@dp.message(F.text)
async def search(msg: types.Message) -> None:
    if msg.text is None:
        return

    keyword = msg.text.strip()

    if not keyword or keyword.startswith("/"):
        return

    token = _create_search_token(keyword)
    can_delete = _is_admin_user(msg.from_user)
    text, keyboard = await _build_search_view(
        keyword=keyword,
        token=token,
        filter_key="all",
        offset=0,
        can_delete=can_delete,
    )
    await msg.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("s:"))
async def paginate_search(call: types.CallbackQuery) -> None:
    if call.data is None:
        return

    if call.message is None:
        await call.answer("当前上下文不可用", show_alert=True)
        return

    parts = call.data.split(":", maxsplit=3)
    if len(parts) != 4:
        await call.answer("无效请求", show_alert=True)
        return

    _, token, filter_key, offset_raw = parts
    keyword = _get_search_keyword(token)
    if keyword is None:
        await call.answer("搜索会话已过期，请重新输入关键词。", show_alert=True)
        return

    try:
        offset = max(0, int(offset_raw))
    except ValueError:
        await call.answer("无效页码", show_alert=True)
        return

    can_delete = _is_admin_user(call.from_user)
    text, keyboard = await _build_search_view(
        keyword=keyword,
        token=token,
        filter_key=filter_key,
        offset=offset,
        can_delete=can_delete,
    )

    try:
        await call.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise

    await call.answer()


@dp.callback_query(F.data.startswith("del:"))
async def delete_by_callback(call: types.CallbackQuery) -> None:
    if call.data is None:
        return

    if not _is_admin_user(call.from_user):
        await call.answer("仅管理员可删除。", show_alert=True)
        return

    parts = call.data.split(":", maxsplit=4)
    if len(parts) != 5:
        await call.answer("无效删除请求", show_alert=True)
        return

    _, record_id_raw, token, filter_key, offset_raw = parts

    try:
        record_id = int(record_id_raw)
        offset = max(0, int(offset_raw))
    except ValueError:
        await call.answer("无效文件ID", show_alert=True)
        return

    deleted = await delete_file_record(record_id)
    if not deleted:
        await call.answer("文件不存在或已删除。", show_alert=True)
        return

    if call.message is None:
        await call.answer("已删除")
        return

    keyword = _get_search_keyword(token)
    if keyword is None:
        await call.answer("已删除，原搜索会话已过期。", show_alert=True)
        return

    text, keyboard = await _build_search_view(
        keyword=keyword,
        token=token,
        filter_key=filter_key,
        offset=offset,
        can_delete=True,
    )

    try:
        await call.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise

    await call.answer("已删除")


@dp.callback_query(F.data.startswith("file_"))
async def send_file(call: types.CallbackQuery) -> None:
    if call.data is None:
        return

    try:
        file_record_id = int(call.data.split("_", maxsplit=1)[1])
    except (IndexError, ValueError):
        await call.answer("无效请求", show_alert=True)
        return

    file_data = await get_file(file_record_id)

    if file_data is None:
        await call.answer("文件不存在或已删除", show_alert=True)
        return

    if call.message is None:
        await call.answer("当前上下文无法发送文件", show_alert=True)
        return

    file_id, name = file_data
    caption = name
    if _is_admin_user(call.from_user):
        caption = f"[ID:{file_record_id}] {name}"

    await call.message.answer_document(file_id, caption=caption)
    await call.answer()


@dp.error()
async def on_error(event: types.ErrorEvent) -> None:
    logger.exception("Unhandled aiogram error: %s", event.exception)
