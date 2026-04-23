import logging
import os
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
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

DB_PATH = os.getenv("DB_PATH", "data.db").strip() or "data.db"
SEARCH_LIMIT = max(1, min(20, _parse_int("SEARCH_LIMIT", 5)))
ADMIN_ID = _parse_optional_int("ADMIN_ID")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)"
        )
        await db.commit()


async def add_or_update_file(name: str, file_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM files WHERE file_id = ?",
            (file_id,),
        )
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


async def search_file(keyword: str, limit: int = SEARCH_LIMIT) -> list[tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, name
            FROM files
            WHERE name LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (f"%{keyword}%", limit),
        )
        rows = await cursor.fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]


async def get_file(record_id: int) -> Optional[tuple[str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT file_id, name FROM files WHERE id = ?",
            (record_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])


@dp.message(Command("start"))
async def start(msg: types.Message) -> None:
    await msg.answer("网盘 Bot 已启动，直接输入关键词即可搜索。")


@dp.message(Command("help"))
async def help_command(msg: types.Message) -> None:
    await msg.answer(
        "使用说明:\n"
        "1) 发送文件可自动收录\n"
        "2) 发送关键词可搜索文件\n"
        "3) 点击结果按钮可回传文件"
    )


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

    results = await search_file(keyword)

    if not results:
        await msg.answer("没找到相关文件。")
        return

    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"file_{file_record_id}")]
        for file_record_id, name in results
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await msg.answer(f"搜索结果（最多 {SEARCH_LIMIT} 条）:", reply_markup=keyboard)


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
    await call.message.answer_document(file_id, caption=name)
    await call.answer()


@dp.error()
async def on_error(event: types.ErrorEvent) -> None:
    logger.exception("Unhandled aiogram error: %s", event.exception)
