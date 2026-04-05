#!/usr/bin/env python3
"""MangaPlus Download Telegram Bot"""

import asyncio
import gc
import html
import logging
import os
import re
import shlex
import shutil
import tempfile
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from mangaplus_client import MangaPlusClient
from converter import create_pdf, create_cbz

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TARGET_CHANNEL = "@Manga_Free_Manga"
STICKER_ID = "CAACAgUAAxkBAAEQ2odpzUZf56shzcy8svTkc4ZPDypsxQACDgADQ3PJEgsK7SMGumuoOgQ"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
BATCH_REST_SECONDS = 30

client = MangaPlusClient()

# --- THE RAM SAVER: This forces the bot to only process ONE chapter at a time ---
global_dl_lock = asyncio.Lock()


def e(text: str) -> str:
    return html.escape(str(text))


def _ch_display(num: float) -> str:
    return str(int(num)) if num == int(num) else str(num)


def _parse_chapter_list(s: str) -> list[float]:
    s = s.strip()
    range_m = re.match(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$", s)
    if range_m:
        start = int(float(range_m.group(1)))
        end = int(float(range_m.group(2)))
        if start > end:
            start, end = end, start
        return [float(n) for n in range(start, end + 1)]
    if "," in s:
        nums = []
        for part in s.split(","):
            try:
                nums.append(float(part.strip()))
            except ValueError:
                pass
        return nums
    try:
        return [float(s)]
    except ValueError:
        return []


def _parse_dl_args(text: str) -> tuple[str | None, list[float], list[str]]:
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()

    if parts and parts[0].startswith("/"):
        parts = parts[1:]

    manga_name = None
    chapter_nums: list[float] = []
    formats: list[str] = []

    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "-c" and i + 1 < len(parts):
            chapter_nums = _parse_chapter_list(parts[i + 1])
            i += 2
        elif p.lower() in ("-pdf", "-cbz"):
            fmt = p.lower()[1:]
            if fmt not in formats:
                formats.append(fmt)
            i += 1
        elif not p.startswith("-") and manga_name is None:
            manga_name = p
            i += 1
        else:
            i += 1

    return manga_name, chapter_nums, formats


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:100] or "manga"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "👋 <b>MangaPlus Download Bot</b>\n\n"
        "I download manga chapters from MangaPlus and send them as PDF or CBZ files.\n\n"
        "📖 <b>Commands:</b>\n"
        "• <code>/search &lt;name&gt;</code> — Find a manga\n"
        "• <code>/chapters &lt;name&gt;</code> — Show available free chapters\n"
        "• <code>/dl \"Name\" -c N -pdf</code> — Download one chapter\n"
        "• <code>/dl \"Name\" -c 1-12 -pdf</code> — Batch download chapters 1–12\n"
        "• <code>/dl \"Name\" -c N -pdf -cbz</code> — Both formats\n"
        "• <code>/help</code> — Full usage guide\n\n"
        "⚠️ MangaPlus only exposes free chapters (first ~3 + latest ~3 per series)."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📖 <b>How to Use</b>\n\n"
        "<b>Search:</b>\n"
        "<code>/search One Piece</code>\n\n"
        "<b>Available chapters:</b>\n"
        "<code>/chapters One Piece</code>\n\n"
        "<b>Download one chapter:</b>\n"
        "<code>/dl \"One Piece\" -c 1 -pdf</code>\n"
        "<code>/dl \"One Piece\" -c 1 -cbz</code>\n"
        "<code>/dl \"One Piece\" -c 1 -pdf -cbz</code>\n\n"
        "<b>Batch download (e.g. chapters 1 to 12):</b>\n"
        "<code>/dl \"One Piece\" -c 1-12 -pdf</code>\n"
        "↳ Downloads each chapter one by one, uploads it, then waits 30 s before the next.\n\n"
        "<b>Formats:</b>\n"
        "• <code>-pdf</code> — PDF (works on any device)\n"
        "• <code>-cbz</code> — CBZ (comic readers, Kindle)\n\n"
        "⚠️ MangaPlus only allows free chapters. Use <code>/chapters</code> first to check."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: <code>/search Manga Name</code>", parse_mode=ParseMode.HTML)
        return

    query = " ".join(context.args)
    msg = await update.message.reply_text(f"🔍 Searching for <b>{e(query)}</b>...", parse_mode=ParseMode.HTML)

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, partial(client.search_manga, query))
    except Exception as ex:
        logger.error(f"Search error: {ex}")
        await msg.edit_text(f"❌ Search failed: {e(str(ex))}", parse_mode=ParseMode.HTML)
        return

    if not results:
        await msg.edit_text(f"❌ No results for <b>{e(query)}</b>. Try a different spelling.", parse_mode=ParseMode.HTML)
        return

    lines = [f"📚 <b>Results for \"{e(query)}\":</b>\n"]
    for r in results[:8]:
        author = f" — {e(r['author'])}" if r.get("author") else ""
        lines.append(f"• <code>{e(r['name'])}</code>{author}")

    lines.append("\n<i>Use /chapters to see available chapters, then /dl to download.</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_search_chapters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: <code>/chapters Manga Name</code>", parse_mode=ParseMode.HTML)
        return

    query = " ".join(context.args)
    msg = await update.message.reply_text(f"🔍 Looking up chapters for <b>{e(query)}</b>...", parse_mode=ParseMode.HTML)

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, partial(client.search_manga, query))
        if not results:
            await msg.edit_text(f"❌ No manga found for <b>{e(query)}</b>.", parse_mode=ParseMode.HTML)
            return
        top = results[0]
        info = await loop.run_in_executor(None, partial(client.get_title_info, top["title_id"]))
    except Exception as ex:
        logger.error(f"Chapters error: {ex}")
        await msg.edit_text(f"❌ Error: {e(str(ex))}", parse_mode=ParseMode.HTML)
        return

    chapters = info["chapters"]
    title_name = info["title_name"] or top["name"]
    next_date = info.get("next_chapter_date", "")

    if not chapters:
        await msg.edit_text(
            f"📖 <b>{e(title_name)}</b> — no free chapters found.\n\nMangaPlus only exposes select chapters for free.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📖 <b>{e(title_name)}</b> — available chapters:\n"]
    for ch in chapters:
        num = ch["chapter_num"]
        subtitle = ch["sub_title"]
        if num is not None:
            lines.append(f"• Ch {e(_ch_display(num))} — {e(subtitle)}")
        else:
            lines.append(f"• {e(subtitle)}")

    if next_date:
        lines.append(f"\n📅 Next chapter: <b>{e(next_date)}</b>")

    lines.append(f"\n<i>Download: /dl \"{e(title_name)}\" -c N -pdf</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _download_one_chapter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    loop,
    msg,
    title_name: str,
    title_id: int,
    chapter_num: float,
    formats: list[str],
    progress_label: str,
    next_chapter_date: str = "",
) -> bool:
    """Download, convert, and upload a single chapter with a strict lock to prevent OOM."""
    
    # Wait for the lock to become available before processing
    async with global_dl_lock:
        ch_str = _ch_display(chapter_num)

        chapter = await loop.run_in_executor(
            None, partial(client.find_chapter, title_id, chapter_num)
        )

        if not chapter:
            await update.message.reply_text(
                f"❌ {progress_label} Chapter {e(ch_str)} not found or not free.\n"
                f"Use <code>/chapters {e(title_name)}</code> to see available chapters.",
                parse_mode=ParseMode.HTML,
            )
            return False

        chapter_id = chapter["chapter_id"]
        subtitle = chapter.get("sub_title", "")
        thumbnail_url = chapter.get("thumbnail_url") or chapter.get("portrait_url") or ""
        if not next_chapter_date:
            next_chapter_date = chapter.get("next_chapter_date", "")

        await msg.edit_text(
            f"⬇️ {progress_label} Downloading Ch {e(ch_str)} — {e(subtitle)}...\n(Only 1 chapter runs at a time to save RAM)",
            parse_mode=ParseMode.HTML,
        )

        tmpdir = tempfile.mkdtemp(prefix="mangabot_")
        try:
            img_dir = os.path.join(tmpdir, "images")
            os.makedirs(img_dir)

            image_paths = await loop.run_in_executor(
                None, partial(client.download_chapter_images, chapter_id, img_dir)
            )
            page_count = len(image_paths)

            await msg.edit_text(
                f"📦 {progress_label} Converting {page_count} pages → {', '.join(f.upper() for f in formats)}...",
                parse_mode=ParseMode.HTML,
            )

            safe_name = _safe_filename(f"{title_name}_Ch{ch_str}")
            outputs = []

            for fmt in formats:
                out_path = os.path.join(tmpdir, f"{safe_name}.{fmt}")
                if fmt == "pdf":
                    await loop.run_in_executor(None, partial(create_pdf, image_paths, out_path))
                elif fmt == "cbz":
                    await loop.run_in_executor(None, partial(create_cbz, image_paths, out_path))
                outputs.append((fmt.upper(), out_path))

            await msg.edit_text(f"📤 {progress_label} Uploading Ch {e(ch_str)}...", parse_mode=ParseMode.HTML)

            for fmt_name, path in outputs:
                size = os.path.getsize(path)
                if size > MAX_UPLOAD_BYTES:
                    await update.message.reply_text(f"⚠️ {fmt_name} is too large ({size / 1024 / 1024:.1f} MB, limit 50 MB).")
                    continue
                
                file_caption = (
                    f"📖 <b>{e(title_name)}</b>\n"
                    f"Chapter {e(ch_str)} — {e(subtitle)}\n"
                    f"Format: {fmt_name} | Pages: {page_count}"
                )
                with open(path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=os.path.basename(path),
                        caption=file_caption,
                        parse_mode=ParseMode.HTML,
                    )

            # Post to channel
            await _post_to_channel(
                context=context, title_name=title_name, chapter_num=chapter_num,
                subtitle=subtitle, page_count=page_count, thumbnail_url=thumbnail_url,
                outputs=outputs, next_chapter_date=next_chapter_date,
            )
            return True

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            gc.collect()


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    manga_name, chapter_nums, formats = _parse_dl_args(text)

    if not manga_name:
        await update.message.reply_text("❌ Please specify a manga name.", parse_mode=ParseMode.HTML)
        return

    if not chapter_nums:
        await update.message.reply_text("❌ Please specify a chapter with <code>-c</code>.", parse_mode=ParseMode.HTML)
        return

    if not formats:
        formats = ["pdf"]

    is_batch = len(chapter_nums) > 1
    loop = asyncio.get_event_loop()
    msg = await update.message.reply_text(f"🔍 Searching for <b>{e(manga_name)}</b>...", parse_mode=ParseMode.HTML)

    try:
        results = await loop.run_in_executor(None, partial(client.search_manga, manga_name))
        if not results:
            await msg.edit_text(f"❌ No manga found matching <b>{e(manga_name)}</b>.", parse_mode=ParseMode.HTML)
            return

        top = results[0]
        title_name = top["name"]
        title_id = top["title_id"]

        next_chapter_date = ""
        if is_batch:
            try:
                info = await loop.run_in_executor(None, partial(client.get_title_info, title_id))
                next_chapter_date = info.get("next_chapter_date", "")
            except Exception:
                pass

        total = len(chapter_nums)
        if is_batch:
            await msg.edit_text(
                f"📚 <b>{e(title_name)}</b> — Batch download\n"
                f"Chapters: {e(_ch_display(chapter_nums[0]))}–{e(_ch_display(chapter_nums[-1]))} ({total} total)\n\n"
                f"⬇️ Added to Queue...", parse_mode=ParseMode.HTML
            )

        done = 0
        for i, ch_num in enumerate(chapter_nums):
            progress_label = f"[{i + 1}/{total}]" if is_batch else ""
            try:
                success = await _download_one_chapter(
                    update=update, context=context, loop=loop, msg=msg,
                    title_name=title_name, title_id=title_id, chapter_num=ch_num,
                    formats=formats, progress_label=progress_label, next_chapter_date=next_chapter_date,
                )
                if success:
                    done += 1
            except Exception as ex:
                logger.error(f"Chapter {ch_num} error: {ex}", exc_info=True)
                await update.message.reply_text(
                    f"❌ {progress_label} Chapter {e(_ch_display(ch_num))} failed: {e(str(ex))}",
                    parse_mode=ParseMode.HTML,
                )

            if is_batch and i < total - 1:
                await msg.edit_text(
                    f"⏳ [{i + 1}/{total}] Ch {e(_ch_display(ch_num))} done. Resting {BATCH_REST_SECONDS}s...",
                    parse_mode=ParseMode.HTML,
                )
                await asyncio.sleep(BATCH_REST_SECONDS)

        if is_batch:
            await msg.edit_text(f"✅ Batch complete! <b>{e(title_name)}</b>\nSuccessfully sent {done}/{total} chapters.", parse_mode=ParseMode.HTML)
        else:
            ch_str = _ch_display(chapter_nums[0])
            await msg.edit_text(f"✅ Done! <b>{e(title_name)}</b> Chapter {e(ch_str)} sent.", parse_mode=ParseMode.HTML)

    except Exception as ex:
        logger.error(f"Download command error: {ex}", exc_info=True)
        try:
            await msg.edit_text(f"❌ Error: {e(str(ex))}", parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _post_to_channel(
    context: ContextTypes.DEFAULT_TYPE, title_name: str, chapter_num: float, subtitle: str,
    page_count: int, thumbnail_url: str, outputs: list[tuple[str, str]], next_chapter_date: str = "",
) -> None:
    """Post chapter announcement, file, and sticker to the target channel."""
    bot = context.bot
    ch_str = _ch_display(chapter_num)
    hashtag = re.sub(r"[^a-zA-Z0-9]", "", title_name)
    next_line = f"\n📅 Next chapter: <b>{e(next_chapter_date)}</b>" if next_chapter_date else ""

    channel_caption = (
        f"📖 <b>{e(title_name)}</b>\n🔖 <b>Chapter {ch_str}</b> — {e(subtitle)}\n\n"
        f"📄 Pages: {page_count}{next_line}\n#MangaPlus #{e(hashtag)}"
    )

    try:
        if thumbnail_url:
            try:
                await bot.send_photo(chat_id=TARGET_CHANNEL, photo=thumbnail_url, caption=channel_caption, parse_mode=ParseMode.HTML)
            except Exception:
                await bot.send_message(chat_id=TARGET_CHANNEL, text=channel_caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=TARGET_CHANNEL, text=channel_caption, parse_mode=ParseMode.HTML)

        for fmt_name, path in outputs:
            if not os.path.exists(path): continue
            size = os.path.getsize(path)
            if size > MAX_UPLOAD_BYTES: continue
            
            file_caption = f"📖 <b>{e(title_name)}</b> — Chapter {ch_str} ({fmt_name})"
            with open(path, "rb") as f:
                await bot.send_document(chat_id=TARGET_CHANNEL, document=f, filename=os.path.basename(path), caption=file_caption, parse_mode=ParseMode.HTML)

        await bot.send_sticker(chat_id=TARGET_CHANNEL, sticker=STICKER_ID)

    except Exception as ex:
        logger.error(f"Channel post failed: {ex}", exc_info=True)


def start_dummy_server():
    """Runs a tiny web server in a background thread so Render doesn't kill the bot."""
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Manga Worker Bot is Alive and Ready!")
            
        def do_HEAD(self):
            """Handles HEAD requests which some uptime monitors use."""
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()

        def log_message(self, format, *args):
            """Prevents UptimeRobot from spamming your Render logs every 5 minutes."""
            pass

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"🌍 Dummy Web Server started on port {port}")


def main() -> None:
    start_dummy_server()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("chapters", cmd_search_chapters))
    app.add_handler(CommandHandler("dl", cmd_download))
    
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
