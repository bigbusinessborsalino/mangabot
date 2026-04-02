#!/usr/bin/env python3
"""MangaPlus Download Telegram Bot"""

import asyncio
import html
import logging
import os
import shlex
import shutil
import tempfile
from functools import partial
import threading
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

client = MangaPlusClient()


def e(text: str) -> str:
    """Escape text for Telegram HTML mode."""
    return html.escape(str(text))


def _parse_dl_args(text: str) -> tuple[str | None, float | None, list[str]]:
    """Parse /dl command arguments.

    Syntax: /dl "Manga Name" -c <chapter> [-pdf] [-cbz]
    Returns: (manga_name, chapter_number, formats)
    """
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()

    if parts and parts[0].startswith("/"):
        parts = parts[1:]

    manga_name = None
    chapter_num = None
    formats = []

    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "-c" and i + 1 < len(parts):
            try:
                chapter_num = float(parts[i + 1])
            except ValueError:
                pass
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

    return manga_name, chapter_num, formats


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "👋 <b>MangaPlus Download Bot</b>\n\n"
        "I can download manga chapters from MangaPlus and send them as PDF or CBZ files.\n\n"
        "📖 <b>Commands:</b>\n"
        "• <code>/search &lt;manga name&gt;</code> — Find a manga and list available chapters\n"
        "• <code>/chapters &lt;manga name&gt;</code> — Show free chapters for a manga\n"
        "• <code>/dl \"Manga Name\" -c &lt;ch&gt; -pdf</code> — Download as PDF\n"
        "• <code>/dl \"Manga Name\" -c &lt;ch&gt; -cbz</code> — Download as CBZ\n"
        "• <code>/dl \"Manga Name\" -c &lt;ch&gt; -pdf -cbz</code> — Both formats\n"
        "• <code>/help</code> — Show detailed usage\n\n"
        "⚠️ Only free chapters (usually first 3 + latest 3) can be downloaded."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📖 <b>How to Use</b>\n\n"
        "<b>Search for manga:</b>\n"
        "<code>/search One Piece</code>\n\n"
        "<b>See available chapters:</b>\n"
        "<code>/chapters One Piece</code>\n\n"
        "<b>Download a chapter:</b>\n"
        "<code>/dl \"One Piece\" -c 1 -pdf</code>\n"
        "<code>/dl \"One Piece\" -c 1 -cbz</code>\n"
        "<code>/dl \"One Piece\" -c 1 -pdf -cbz</code>\n\n"
        "<b>With spaces in name (use quotes):</b>\n"
        "<code>/dl \"Jujutsu Kaisen\" -c 1 -pdf</code>\n\n"
        "<b>Formats:</b>\n"
        "• <code>-pdf</code> — PDF file (great for reading on any device)\n"
        "• <code>-cbz</code> — CBZ file (comic book format for Kindle/comic readers)\n"
        "• Use both flags to get both formats\n\n"
        "⚠️ <b>Note:</b> MangaPlus only provides free access to select chapters. "
        "Use <code>/chapters</code> to see which chapters are available."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/search Manga Name</code>", parse_mode=ParseMode.HTML
        )
        return

    query = " ".join(context.args)
    msg = await update.message.reply_text(
        f"🔍 Searching for <b>{e(query)}</b>...", parse_mode=ParseMode.HTML
    )

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, partial(client.search_manga, query))
    except Exception as ex:
        logger.error(f"Search error: {ex}")
        await msg.edit_text(f"❌ Search failed: {e(str(ex))}", parse_mode=ParseMode.HTML)
        return

    if not results:
        await msg.edit_text(
            f"❌ No results found for <b>{e(query)}</b>.\n\n"
            "Try a different spelling or part of the title.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📚 <b>Search results for \"{e(query)}\":</b>\n"]
    for r in results[:8]:
        lines.append(f"• <code>{e(r['name'])}</code> ({e(r['type'])})")

    lines.append("\n<i>Use /chapters to see available chapters, then /dl to download.</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_search_chapters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/chapters Manga Name</code>", parse_mode=ParseMode.HTML
        )
        return

    query = " ".join(context.args)
    msg = await update.message.reply_text(
        f"🔍 Looking up chapters for <b>{e(query)}</b>...", parse_mode=ParseMode.HTML
    )

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, partial(client.search_manga, query))
        if not results:
            await msg.edit_text(
                f"❌ No manga found for <b>{e(query)}</b>.", parse_mode=ParseMode.HTML
            )
            return

        top = results[0]
        info = await loop.run_in_executor(None, partial(client.get_title_info, top["title_id"]))
    except Exception as ex:
        logger.error(f"Chapters error: {ex}")
        await msg.edit_text(f"❌ Error: {e(str(ex))}", parse_mode=ParseMode.HTML)
        return

    chapters = info["chapters"]
    title_name = info["title_name"] or top["name"]

    if not chapters:
        await msg.edit_text(
            f"📖 <b>{e(title_name)}</b> — no free chapters found.\n\n"
            "MangaPlus only exposes the first few and latest few chapters for free.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"📖 <b>{e(title_name)}</b> — available chapters:\n"]
    for ch in chapters:
        num = ch["chapter_num"]
        subtitle = ch["sub_title"]
        if num is not None:
            lines.append(f"• Chapter {e(num)} — {e(subtitle)}")
        else:
            lines.append(f"• {e(subtitle)}")

    lines.append(f"\n<i>Download: /dl \"{e(title_name)}\" -c N -pdf</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    manga_name, chapter_num, formats = _parse_dl_args(text)

    if not manga_name:
        await update.message.reply_text(
            "❌ Please specify a manga name.\n\n"
            'Example: <code>/dl "One Piece" -c 1 -pdf</code>',
            parse_mode=ParseMode.HTML,
        )
        return

    if chapter_num is None:
        await update.message.reply_text(
            "❌ Please specify a chapter number with <code>-c</code>.\n\n"
            f'Example: <code>/dl "{e(manga_name)}" -c 1 -pdf</code>',
            parse_mode=ParseMode.HTML,
        )
        return

    if not formats:
        formats = ["pdf"]

    loop = asyncio.get_event_loop()
    msg = await update.message.reply_text(
        f"🔍 Searching for <b>{e(manga_name)}</b>...",
        parse_mode=ParseMode.HTML,
    )

    try:
        results = await loop.run_in_executor(None, partial(client.search_manga, manga_name))
        if not results:
            await msg.edit_text(
                f"❌ No manga found matching <b>{e(manga_name)}</b>.\n\n"
                "Use <code>/search</code> first to find the exact title name.",
                parse_mode=ParseMode.HTML,
            )
            return

        top = results[0]
        title_name = top["name"]
        title_id = top["title_id"]

        await msg.edit_text(
            f"📖 Found: <b>{e(title_name)}</b>\n"
            f"🔍 Looking for chapter {e(chapter_num)}...",
            parse_mode=ParseMode.HTML,
        )

        chapter = await loop.run_in_executor(
            None, partial(client.find_chapter, title_id, chapter_num)
        )

        if not chapter:
            info = await loop.run_in_executor(
                None, partial(client.get_title_info, title_id)
            )
            chapters = info["chapters"]
            if chapters:
                nums = [
                    str(ch["chapter_num"])
                    for ch in chapters
                    if ch["chapter_num"] is not None
                ]
                avail = ", ".join(nums[:20])
                await msg.edit_text(
                    f"❌ Chapter {e(chapter_num)} not found or not available for free.\n\n"
                    f"📖 <b>{e(title_name)}</b> — free chapters: {e(avail)}\n\n"
                    "MangaPlus provides free access only to select chapters.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await msg.edit_text(
                    f"❌ Chapter {e(chapter_num)} not found.\n\n"
                    f"Use <code>/chapters {e(manga_name)}</code> to see available chapters.",
                    parse_mode=ParseMode.HTML,
                )
            return

        chapter_id = chapter["chapter_id"]
        subtitle = chapter.get("sub_title", "")

        await msg.edit_text(
            f"⬇️ Downloading <b>{e(title_name)}</b> — Chapter {e(chapter_num)} {e(subtitle)}...",
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
                f"📦 Converting {page_count} pages to "
                f"{', '.join(f.upper() for f in formats)}...",
                parse_mode=ParseMode.HTML,
            )

            safe_name = _safe_filename(f"{title_name}_Ch{chapter_num}")
            outputs = []

            for fmt in formats:
                out_path = os.path.join(tmpdir, f"{safe_name}.{fmt}")
                if fmt == "pdf":
                    await loop.run_in_executor(
                        None, partial(create_pdf, image_paths, out_path)
                    )
                elif fmt == "cbz":
                    await loop.run_in_executor(
                        None, partial(create_cbz, image_paths, out_path)
                    )
                outputs.append((fmt.upper(), out_path))

            await msg.edit_text("📤 Uploading to Telegram...", parse_mode=ParseMode.HTML)

            thumbnail_url = chapter.get("thumbnail_url") or chapter.get("portrait_url") or ""

            for fmt_name, path in outputs:
                size = os.path.getsize(path)
                if size > MAX_UPLOAD_BYTES:
                    await update.message.reply_text(
                        f"⚠️ {fmt_name} file is too large "
                        f"({size / 1024 / 1024:.1f} MB) for Telegram (50 MB limit)."
                    )
                    continue

                file_caption = (
                    f"📖 <b>{e(title_name)}</b>\n"
                    f"Chapter {e(chapter_num)} {e(subtitle)}\n"
                    f"Format: {fmt_name} | Pages: {page_count}"
                )
                with open(path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=os.path.basename(path),
                        caption=file_caption,
                        parse_mode=ParseMode.HTML,
                    )

            await msg.edit_text(
                f"✅ Done! <b>{e(title_name)}</b> Chapter {e(chapter_num)} "
                f"sent ({page_count} pages).",
                parse_mode=ParseMode.HTML,
            )

            # Post to channel
            await _post_to_channel(
                context=context,
                title_name=title_name,
                chapter_num=chapter_num,
                subtitle=subtitle,
                page_count=page_count,
                thumbnail_url=thumbnail_url,
                outputs=outputs,
            )

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception as ex:
        logger.error(f"Download error: {ex}", exc_info=True)
        try:
            await msg.edit_text(
                f"❌ Error: {e(str(ex))}\n\n"
                "This chapter may be subscriber-only or temporarily unavailable.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def _post_to_channel(
    context: ContextTypes.DEFAULT_TYPE,
    title_name: str,
    chapter_num: float,
    subtitle: str,
    page_count: int,
    thumbnail_url: str,
    outputs: list[tuple[str, str]],
) -> None:
    """Post the manga chapter announcement + file + sticker to the target channel."""
    bot = context.bot

    # Build a clean chapter number string (drop .0 for whole numbers)
    ch_display = int(chapter_num) if chapter_num == int(chapter_num) else chapter_num

    # Caption for the channel post
    channel_caption = (
        f"📖 <b>{e(title_name)}</b>\n"
        f"🔖 <b>Chapter {ch_display}</b> — {e(subtitle)}\n\n"
        f"📄 Pages: {page_count}\n"
        f"#MangaPlus #{e(title_name.replace(' ', ''))}"
    )

    try:
        # 1) Send the cover / thumbnail image with caption
        if thumbnail_url:
            try:
                await bot.send_photo(
                    chat_id=TARGET_CHANNEL,
                    photo=thumbnail_url,
                    caption=channel_caption,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as photo_err:
                logger.warning(f"Channel photo send failed ({photo_err}), sending text instead")
                await bot.send_message(
                    chat_id=TARGET_CHANNEL,
                    text=channel_caption,
                    parse_mode=ParseMode.HTML,
                )
        else:
            await bot.send_message(
                chat_id=TARGET_CHANNEL,
                text=channel_caption,
                parse_mode=ParseMode.HTML,
            )

        # 2) Upload each file to the channel
        for fmt_name, path in outputs:
            size = os.path.getsize(path)
            if size > MAX_UPLOAD_BYTES:
                logger.warning(f"Skipping channel upload of {fmt_name}: too large")
                continue
            file_caption = (
                f"📖 <b>{e(title_name)}</b> — Chapter {ch_display} ({fmt_name})"
            )
            with open(path, "rb") as f:
                await bot.send_document(
                    chat_id=TARGET_CHANNEL,
                    document=f,
                    filename=os.path.basename(path),
                    caption=file_caption,
                    parse_mode=ParseMode.HTML,
                )

        # 3) Send the sticker
        await bot.send_sticker(
            chat_id=TARGET_CHANNEL,
            sticker=STICKER_ID,
        )

        logger.info(f"Channel post done for {title_name} Ch{chapter_num}")

    except Exception as ex:
        logger.error(f"Failed to post to channel {TARGET_CHANNEL}: {ex}", exc_info=True)


def _safe_filename(name: str) -> str:
    import re
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name[:100] or "manga"
    
    
def start_dummy_server():
    """Runs a tiny web server in a background thread so Render doesn't kill the bot."""
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Manga Worker Bot is Alive and Ready!")

    # Render provides the PORT environment variable dynamically
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    
    # Run the server in a daemon thread so it runs parallel to the Telegram bot
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"🌍 Dummy Web Server started on port {port}")


def main() -> None:
    # 1. Start the dummy web server for Render
    start_dummy_server()

    # 2. Build the Telegram bot
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("chapters", cmd_search_chapters))
    app.add_handler(CommandHandler("dl", cmd_download))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
