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
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

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
TEXT_COMMAND_ALIASES: dict[str, str] = {
    "开始": "start",
    "帮助": "help",
    "我的文件": "myfiles",
}


def _build_bot_commands() -> list[BotCommand]:
    commands = [
        BotCommand(command="start", description="启动与欢迎"),
        BotCommand(command="help", description="查看使用说明"),
        BotCommand(command="search", description="按关键词搜索"),
        BotCommand(command="recent", description="查看最近文件"),
        BotCommand(command="myfiles", description="查看我的文件"),
        BotCommand(command="get", description="按ID取回文件"),
        BotCommand(command="id", description="查看你的用户ID"),
        BotCommand(command="stats", description="查看文件统计"),
        BotCommand(command="types", description="查看支持类型"),
        BotCommand(command="ping", description="检查机器人在线"),
    ]
    if ADMIN_ID is not None:
        commands.append(BotCommand(command="delete", description="管理员删除文件"))
    return commands


async def register_bot_commands() -> None:
    try:
        await bot.set_my_commands(_build_bot_commands())
        logger.info("Bot command menu registered.")
    except Exception:
        logger.exception("Failed to register bot commands")


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


def _normalize_file_size(file_size: Optional[int]) -> int:
    if file_size is None:
        return 0
    if file_size < 0:
        return 0
    return int(file_size)


def _format_size(total_bytes: int) -> str:
    size = float(max(0, int(total_bytes)))
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0


class FileStore(Protocol):
    async def init(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def add_or_update_file(self, name: str, file_id: str, file_size: int) -> bool:
        ...

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool, int, int]:
        ...

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        ...

    async def get_file_detail(self, record_id: int) -> Optional[tuple[str, str, int]]:
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
                    file_size INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
            try:
                await db.execute(
                    "ALTER TABLE files ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                pass
            await db.commit()

    async def close(self) -> None:
        return None

    async def add_or_update_file(self, name: str, file_id: str, file_size: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute("SELECT 1 FROM files WHERE file_id = ?", (file_id,))
            exists = await cursor.fetchone() is not None

            await db.execute(
                """
                INSERT INTO files (name, file_id, file_size)
                VALUES (?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name,
                    file_size = excluded.file_size
                """,
                (name, file_id, _normalize_file_size(file_size)),
            )
            await db.commit()
            return not exists

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool, int, int]:
        async with aiosqlite.connect(self._db_path) as db:
            where_clause = "WHERE LOWER(name) LIKE LOWER(?)"
            where_params: list[object] = [f"%{keyword}%"]

            if extension is not None:
                where_clause += " AND LOWER(name) LIKE ?"
                where_params.append(f"%.{extension.lower()}")

            query = (
                "SELECT id, name FROM files "
                f"{where_clause} "
                "ORDER BY id DESC LIMIT ? OFFSET ?"
            )
            query_params = [*where_params, limit + 1, max(0, offset)]
            cursor = await db.execute(query, tuple(query_params))
            rows = await cursor.fetchall()

            stats_query = (
                "SELECT COUNT(1), COALESCE(SUM(file_size), 0) "
                f"FROM files {where_clause}"
            )
            stats_cursor = await db.execute(stats_query, tuple(where_params))
            stats_row = await stats_cursor.fetchone()

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        total_count = int(stats_row[0]) if stats_row is not None else 0
        total_size = int(stats_row[1]) if stats_row is not None else 0
        return (
            [(int(row[0]), str(row[1])) for row in visible_rows],
            has_next,
            total_count,
            total_size,
        )

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        detail = await self.get_file_detail(record_id)
        if detail is None:
            return None
        file_id, name, _ = detail
        return file_id, name

    async def get_file_detail(self, record_id: int) -> Optional[tuple[str, str, int]]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT file_id, name, file_size FROM files WHERE id = ?",
                (record_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return str(row[0]), str(row[1]), _normalize_file_size(int(row[2] or 0))

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
                    file_size BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_size BIGINT NOT NULL DEFAULT 0"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")

    async def close(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def add_or_update_file(self, name: str, file_id: str, file_size: int) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM files WHERE file_id = $1", file_id)
            await conn.execute(
                """
                INSERT INTO files (name, file_id, file_size)
                VALUES ($1, $2, $3)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    file_size = EXCLUDED.file_size
                """,
                name,
                file_id,
                _normalize_file_size(file_size),
            )
            return not bool(exists)

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool, int, int]:
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
                stats_row = await conn.fetchrow(
                    """
                    SELECT COUNT(1) AS total_count, COALESCE(SUM(file_size), 0) AS total_size
                    FROM files
                    WHERE name ILIKE $1
                    """,
                    f"%{keyword}%",
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
                stats_row = await conn.fetchrow(
                    """
                    SELECT COUNT(1) AS total_count, COALESCE(SUM(file_size), 0) AS total_size
                    FROM files
                    WHERE name ILIKE $1
                      AND LOWER(name) LIKE $2
                    """,
                    f"%{keyword}%",
                    f"%.{extension.lower()}",
                )

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        total_count = int(stats_row["total_count"]) if stats_row is not None else 0
        total_size = int(stats_row["total_size"]) if stats_row is not None else 0
        return (
            [(int(row["id"]), str(row["name"])) for row in visible_rows],
            has_next,
            total_count,
            total_size,
        )

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        detail = await self.get_file_detail(record_id)
        if detail is None:
            return None
        file_id, name, _ = detail
        return file_id, name

    async def get_file_detail(self, record_id: int) -> Optional[tuple[str, str, int]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT file_id, name, file_size FROM files WHERE id = $1",
                record_id,
            )

        if row is None:
            return None
        return (
            str(row["file_id"]),
            str(row["name"]),
            _normalize_file_size(int(row["file_size"] or 0)),
        )

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
                    file_size INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
            try:
                conn.execute("ALTER TABLE files ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
            conn.commit()
        finally:
            self._safe_close(conn)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        return None

    def _add_or_update_sync(self, name: str, file_id: str, file_size: int) -> bool:
        conn = self._connect()
        try:
            exists_cursor = conn.execute("SELECT 1 FROM files WHERE file_id = ?", (file_id,))
            exists = exists_cursor.fetchone() is not None

            conn.execute(
                """
                INSERT INTO files (name, file_id, file_size)
                VALUES (?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name,
                    file_size = excluded.file_size
                """,
                (name, file_id, _normalize_file_size(file_size)),
            )
            conn.commit()
            return not exists
        finally:
            self._safe_close(conn)

    async def add_or_update_file(self, name: str, file_id: str, file_size: int) -> bool:
        return await asyncio.to_thread(self._add_or_update_sync, name, file_id, file_size)

    def _search_sync(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool, int, int]:
        conn = self._connect()
        try:
            where_clause = "WHERE LOWER(name) LIKE LOWER(?)"
            where_params: list[object] = [f"%{keyword}%"]

            if extension is not None:
                where_clause += " AND LOWER(name) LIKE ?"
                where_params.append(f"%.{extension.lower()}")

            query = (
                "SELECT id, name FROM files "
                f"{where_clause} "
                "ORDER BY id DESC LIMIT ? OFFSET ?"
            )
            query_params = [*where_params, limit + 1, max(0, offset)]
            rows = conn.execute(query, tuple(query_params)).fetchall()

            stats_query = (
                "SELECT COUNT(1), COALESCE(SUM(file_size), 0) "
                f"FROM files {where_clause}"
            )
            stats_row = conn.execute(stats_query, tuple(where_params)).fetchone()
        finally:
            self._safe_close(conn)

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        total_count = int(stats_row[0]) if stats_row is not None else 0
        total_size = int(stats_row[1]) if stats_row is not None else 0
        return (
            [(int(row[0]), str(row[1])) for row in visible_rows],
            has_next,
            total_count,
            total_size,
        )

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool, int, int]:
        return await asyncio.to_thread(self._search_sync, keyword, extension, offset, limit)

    def _get_file_detail_sync(self, record_id: int) -> Optional[tuple[str, str, int]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT file_id, name, file_size FROM files WHERE id = ?",
                (record_id,),
            ).fetchone()
        finally:
            self._safe_close(conn)

        if row is None:
            return None
        return str(row[0]), str(row[1]), _normalize_file_size(int(row[2] or 0))

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        detail = await self.get_file_detail(record_id)
        if detail is None:
            return None
        file_id, name, _ = detail
        return file_id, name

    async def get_file_detail(self, record_id: int) -> Optional[tuple[str, str, int]]:
        return await asyncio.to_thread(self._get_file_detail_sync, record_id)

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

    async def add_or_update_file(self, name: str, file_id: str, file_size: int) -> bool:
        extension = _extract_extension(name)
        normalized_size = _normalize_file_size(file_size)
        existing = await self._files.find_one({"file_id": file_id}, {"_id": 0, "record_id": 1})

        if existing is not None:
            await self._files.update_one(
                {"file_id": file_id},
                {"$set": {"name": name, "ext": extension, "file_size": normalized_size}},
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
                    "file_size": normalized_size,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return True
        except self._DuplicateKeyError:
            await self._files.update_one(
                {"file_id": file_id},
                {"$set": {"name": name, "ext": extension, "file_size": normalized_size}},
            )
            return False

    async def search_file(
        self,
        keyword: str,
        extension: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[int, str]], bool, int, int]:
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
        total_count = int(await self._files.count_documents(query))

        total_size = 0
        pipeline = [
            {"$match": query},
            {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$file_size", 0]}}}},
        ]
        stats = await self._files.aggregate(pipeline).to_list(length=1)
        if stats:
            total_size = int(stats[0].get("total", 0))

        has_next = len(rows) > limit
        visible_rows = rows[:limit]
        return (
            [
                (int(row["record_id"]), str(row["name"]))
                for row in visible_rows
                if "record_id" in row and "name" in row
            ],
            has_next,
            total_count,
            total_size,
        )

    async def get_file(self, record_id: int) -> Optional[tuple[str, str]]:
        detail = await self.get_file_detail(record_id)
        if detail is None:
            return None
        file_id, name, _ = detail
        return file_id, name

    async def get_file_detail(self, record_id: int) -> Optional[tuple[str, str, int]]:
        row = await self._files.find_one(
            {"record_id": record_id},
            {"_id": 0, "file_id": 1, "name": 1, "file_size": 1},
        )
        if row is None:
            return None
        return (
            str(row["file_id"]),
            str(row["name"]),
            _normalize_file_size(int(row.get("file_size", 0) or 0)),
        )

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


async def add_or_update_file(name: str, file_id: str, file_size: int) -> bool:
    return await file_store.add_or_update_file(
        name=name,
        file_id=file_id,
        file_size=_normalize_file_size(file_size),
    )


async def search_file(
    keyword: str,
    extension: Optional[str] = None,
    offset: int = 0,
    limit: int = SEARCH_LIMIT,
) -> tuple[list[tuple[int, str]], bool, int, int]:
    return await file_store.search_file(
        keyword=keyword,
        extension=extension,
        offset=offset,
        limit=limit,
    )


async def get_file(record_id: int) -> Optional[tuple[str, str]]:
    return await file_store.get_file(record_id=record_id)


async def get_file_detail(record_id: int) -> Optional[tuple[str, str, int]]:
    return await file_store.get_file_detail(record_id=record_id)


async def delete_file_record(record_id: int) -> bool:
    return await file_store.delete_file_record(record_id=record_id)


def _build_search_keyboard(
    results: list[tuple[int, str]],
    token: str,
    filter_key: str,
    offset: int,
    has_next: bool,
    can_delete: bool,
    filters_expanded: bool = False,
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

    filter_label = FILTER_LABELS[filter_key]
    if filters_expanded:
        filter_buttons: list[InlineKeyboardButton] = []
        for current_filter_key, current_filter_label in FILTER_LABELS.items():
            button_label = (
                f"[{current_filter_label}]"
                if current_filter_key == filter_key
                else current_filter_label
            )
            filter_buttons.append(
                InlineKeyboardButton(
                    text=button_label,
                    callback_data=f"s:{token}:{current_filter_key}:0",
                )
            )

        for index in range(0, len(filter_buttons), FILTERS_PER_ROW):
            keyboard_rows.append(filter_buttons[index : index + FILTERS_PER_ROW])

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"收起类型筛选（当前：{filter_label}）",
                    callback_data=f"sf:{token}:{filter_key}:{offset}:0",
                )
            ]
        )
    else:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"筛选：{filter_label} / 展开类型",
                    callback_data=f"sf:{token}:{filter_key}:{offset}:1",
                )
            ]
        )

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
    filters_expanded: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    safe_filter = _normalize_filter(filter_key)
    safe_offset = max(0, offset)
    extension = _filter_to_extension(safe_filter)

    results, has_next, total_count, total_size = await search_file(
        keyword=keyword,
        extension=extension,
        offset=safe_offset,
        limit=SEARCH_LIMIT,
    )

    if not results and safe_offset > 0:
        safe_offset = max(0, safe_offset - SEARCH_LIMIT)
        results, has_next, total_count, total_size = await search_file(
            keyword=keyword,
            extension=extension,
            offset=safe_offset,
            limit=SEARCH_LIMIT,
        )

    filter_label = FILTER_LABELS[safe_filter]
    total_pages = max(1, (total_count + SEARCH_LIMIT - 1) // SEARCH_LIMIT)
    page_number = min(total_pages, safe_offset // SEARCH_LIMIT + 1)
    summary_line = (
        f"类型: {filter_label} | 第 {page_number}/{total_pages} 页\n"
        f"总文件: {total_count} | 总容量: {_format_size(total_size)} | 本页: {len(results)}"
    )

    if keyword:
        title = "搜索结果" if results else "没找到相关文件"
        keyword_line = f"关键词: {keyword}"
    else:
        title = "文件列表" if results else "当前还没有文件"
        keyword_line = "关键词: （全部）"

    text = f"{title}\n{keyword_line}\n{summary_line}"

    keyboard = _build_search_keyboard(
        results=results,
        token=token,
        filter_key=safe_filter,
        offset=safe_offset,
        has_next=has_next,
        can_delete=can_delete,
        filters_expanded=filters_expanded,
    )
    return text, keyboard


async def _send_recent_view(
    msg: types.Message,
    filter_key: str = "all",
    page: int = 1,
) -> None:
    offset = max(0, (max(1, page) - 1) * SEARCH_LIMIT)
    token = _create_search_token("")
    can_delete = _is_admin_user(msg.from_user)
    text, keyboard = await _build_search_view(
        keyword="",
        token=token,
        filter_key=filter_key,
        offset=offset,
        can_delete=can_delete,
    )
    await msg.answer(text, reply_markup=keyboard)


@dp.message(Command("start"))
async def start(msg: types.Message) -> None:
    await msg.answer("网盘 Bot 已启动，直接输入关键词即可搜索。")


@dp.message(Command("help"))
async def help_command(msg: types.Message) -> None:
    lines = [
        "使用说明:",
        "1) 发送文件可自动收录",
        "2) 发送关键词可搜索文件",
        "3) 搜索结果支持分页，并显示总文件数与总容量",
        "4) 支持常用文件类型筛选（文档/视频/音频/图片/压缩包）",
        "5) 点击结果按钮可回传文件",
    ]
    if ADMIN_ID is not None:
        lines.append("6) 管理员可点“删除”按钮，或使用 /delete <文件ID>")
    lines.append("命令搜索: /search 关键词")
    lines.append("命令浏览: /recent [类型] [页码]")
    lines.append("我的文件: /myfiles")
    lines.append("命令取回: /get 文件ID")
    lines.append("常用工具: /id /stats /types /ping")
    lines.append("中文快捷词: 开始 / 帮助 / 我的文件")

    await msg.answer("\n".join(lines))


@dp.message(Command("id"))
async def id_command(msg: types.Message) -> None:
    if msg.from_user is None:
        await msg.answer("无法识别当前用户 ID。")
        return

    lines = [
        f"你的 User ID: {msg.from_user.id}",
        f"当前 Chat ID: {msg.chat.id}",
        f"Chat 类型: {msg.chat.type}",
    ]
    if ADMIN_ID is not None:
        lines.append(f"是否管理员: {'是' if _is_admin_user(msg.from_user) else '否'}")
    await msg.answer("\n".join(lines))


@dp.message(Command("stats"))
async def stats_command(msg: types.Message, command: CommandObject) -> None:
    filter_key = "all"
    if command.args is not None and command.args.strip():
        requested = command.args.strip().lower()
        if requested not in FILTER_LABELS:
            available = ", ".join(FILTER_LABELS.keys())
            await msg.answer(
                f"不支持的类型: {requested}\n"
                f"用法: /stats [类型]\n"
                f"例如: /stats pdf\n"
                f"可选类型: {available}"
            )
            return
        filter_key = requested

    extension = _filter_to_extension(filter_key)
    _, _, total_count, total_size = await search_file(
        keyword="",
        extension=extension,
        offset=0,
        limit=1,
    )
    filter_label = FILTER_LABELS[filter_key]
    await msg.answer(
        f"数据库统计\n"
        f"类型: {filter_label}\n"
        f"文件总数: {total_count}\n"
        f"总容量: {_format_size(total_size)}"
    )


@dp.message(Command("types"))
async def types_command(msg: types.Message) -> None:
    all_types = [key.upper() for key in FILTER_LABELS.keys() if key != "all"]
    await msg.answer(
        "当前支持的文件类型:\n"
        + ", ".join(all_types)
        + "\n\n发送关键词后，也可以直接点筛选按钮切换类型。"
    )


@dp.message(Command("ping"))
async def ping_command(msg: types.Message) -> None:
    await msg.answer("pong")


@dp.message(Command("search"))
async def search_by_command(msg: types.Message, command: CommandObject) -> None:
    if command.args is None or not command.args.strip():
        await msg.answer("用法: /search 关键词\n例如: /search 设计文档")
        return

    keyword = command.args.strip()
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


@dp.message(Command("recent"))
async def recent_command(msg: types.Message, command: CommandObject) -> None:
    filter_key = "all"
    page = 1

    if command.args is not None and command.args.strip():
        for part in command.args.split():
            value = part.strip().lower()
            if not value:
                continue
            if value.isdigit():
                page = max(1, int(value))
                continue
            if value in FILTER_LABELS:
                filter_key = value
                continue

            await msg.answer(
                "用法: /recent [类型] [页码]\n"
                "例如: /recent\n"
                "例如: /recent pdf\n"
                "例如: /recent pdf 2"
            )
            return

    await _send_recent_view(msg, filter_key=filter_key, page=page)


@dp.message(Command("myfiles"))
async def myfiles_command(msg: types.Message) -> None:
    await _send_recent_view(msg, filter_key="all", page=1)


@dp.message(F.text.in_(set(TEXT_COMMAND_ALIASES.keys())))
async def chinese_alias_command(msg: types.Message) -> None:
    if msg.text is None:
        return

    alias_text = msg.text.strip()
    if not alias_text:
        return

    alias_target = TEXT_COMMAND_ALIASES.get(alias_text)
    if alias_target is None:
        return

    if alias_target == "start":
        await start(msg)
        return

    if alias_target == "help":
        await help_command(msg)
        return

    if alias_target == "myfiles":
        await myfiles_command(msg)
        return


@dp.message(Command("get"))
async def get_by_command(msg: types.Message, command: CommandObject) -> None:
    if command.args is None or not command.args.strip():
        await msg.answer("用法: /get 文件ID\n例如: /get 123")
        return

    try:
        record_id = int(command.args.strip())
    except ValueError:
        await msg.answer("文件ID 必须是整数。")
        return

    file_data = await get_file(record_id)
    if file_data is None:
        await msg.answer("文件不存在或已删除。")
        return

    file_id, name = file_data
    caption = name
    if _is_admin_user(msg.from_user):
        caption = f"[ID:{record_id}] {name}"

    await msg.answer_document(file_id, caption=caption)


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
    if not msg.document.file_id or not msg.document.file_name:
        await msg.answer("文件信息不完整，无法收录。")
        return

    is_new = await add_or_update_file(
        msg.document.file_name,
        msg.document.file_id,
        _normalize_file_size(msg.document.file_size),
    )

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
    if keyword in TEXT_COMMAND_ALIASES:
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


@dp.callback_query(F.data.startswith("sf:"))
async def toggle_search_filters(call: types.CallbackQuery) -> None:
    if call.data is None:
        return

    if call.message is None:
        await call.answer("当前上下文不可用", show_alert=True)
        return

    parts = call.data.split(":", maxsplit=4)
    if len(parts) != 5:
        await call.answer("无效请求", show_alert=True)
        return

    _, token, filter_key, offset_raw, expanded_raw = parts
    keyword = _get_search_keyword(token)
    if keyword is None:
        await call.answer("搜索会话已过期，请重新输入关键词。", show_alert=True)
        return

    try:
        offset = max(0, int(offset_raw))
    except ValueError:
        await call.answer("无效页码", show_alert=True)
        return

    filters_expanded = expanded_raw == "1"
    can_delete = _is_admin_user(call.from_user)
    text, keyboard = await _build_search_view(
        keyword=keyword,
        token=token,
        filter_key=filter_key,
        offset=offset,
        can_delete=can_delete,
        filters_expanded=filters_expanded,
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
