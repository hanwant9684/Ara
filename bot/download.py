import logging
import asyncio
import tempfile
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from bot.db import get_or_create_user, try_consume_download, record_download, is_premium, get_user
from bot.downloader import download_video, ProgressTracker, detect_platform, get_video_info
from bot.helpers import extract_url, format_size, format_duration, progress_bar

logger = logging.getLogger(__name__)


async def _safe_edit(msg, text: str):
    try:
        await msg.edit_text(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await msg.edit_text(text)
        except Exception:
            pass
    except Exception:
        pass


async def download_handler(client, message: Message):
    user = message.from_user
    text = message.text.strip()

    url = extract_url(text)
    if not url:
        await message.reply_text(
            "That doesn't look like a video link.\n\n"
            "Send me a URL from YouTube, Instagram, TikTok, Twitter, etc."
        )
        return

    try:
        get_or_create_user(user.id, user.username, user.first_name, user.last_name)
    except Exception:
        logger.exception("DB error registering user %d", user.id)

    try:
        allowed, reason = try_consume_download(user.id)
    except Exception:
        logger.exception("DB error in try_consume_download for user %d", user.id)
        await message.reply_text("Something went wrong checking your quota. Try again.")
        return

    if not allowed:
        if "free_limit_reached" in reason:
            parts      = reason.split(":")
            used, limit = parts[1], parts[2]
            await message.reply_text(
                f"You've used all {limit} free downloads.\n\n"
                f"Get Premium for unlimited downloads — /plans"
            )
        elif reason == "banned":
            await message.reply_text("You're banned from using this bot.")
        else:
            await message.reply_text(f"Download not allowed: {reason}")
        return

    platform  = detect_platform(url)
    short_url = url[:60] + ("..." if len(url) > 60 else "")
    status_msg = await message.reply_text(f"🔍 Looking up **{platform}**...\n\n`{short_url}`")

    try:
        info = await asyncio.wait_for(get_video_info(url), timeout=30)
    except asyncio.TimeoutError:
        logger.warning("Video info timeout for user %d url %s", user.id, url[:80])
        await _safe_edit(status_msg, "Timed out trying to fetch that link. The site might be slow — try again.")
        return
    except Exception as e:
        logger.error("Video info error for user %d: %s", user.id, str(e)[:200])
        await _safe_edit(
            status_msg,
            f"Couldn't get info for that link.\n\n"
            f"`{str(e)[:200]}`\n\n"
            f"Make sure the video is public and the URL is correct."
        )
        return

    title    = info.get("title", "Video")[:50]
    duration = format_duration(info.get("duration", 0))
    dur_text = f"  •  ⏱ {duration}" if duration else ""

    await _safe_edit(
        status_msg,
        f"📥 **Downloading...**\n\n"
        f"**{title}**\n"
        f"{platform}{dur_text}"
    )

    tracker = ProgressTracker()

    async def update_progress():
        last_percent = -1
        while True:
            await asyncio.sleep(4)
            if tracker.percent != last_percent and tracker.percent > 0:
                last_percent = tracker.percent
                bar = progress_bar(tracker.percent)
                await _safe_edit(
                    status_msg,
                    f"📥 **Downloading...**\n\n"
                    f"**{title}**\n"
                    f"{bar}\n"
                    f"{tracker.speed}  •  ETA {tracker.eta}"
                )
            if tracker.percent >= 100:
                break

    with tempfile.TemporaryDirectory() as tmpdir:
        progress_task = asyncio.create_task(update_progress())

        try:
            result = await asyncio.wait_for(
                download_video(url, tmpdir, tracker),
                timeout=600,
            )
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        except asyncio.TimeoutError:
            progress_task.cancel()
            logger.warning("Download timeout for user %d url %s", user.id, url[:80])
            await _safe_edit(status_msg, "Download timed out (10 min limit). Try a shorter video.")
            return
        except ValueError as e:
            progress_task.cancel()
            logger.error("Download ValueError for user %d: %s", user.id, str(e)[:300])
            await _safe_edit(status_msg, f"Download failed:\n\n`{str(e)}`")
            return
        except Exception as e:
            progress_task.cancel()
            logger.exception("Unexpected download error for user %d url %s", user.id, url[:80])
            await _safe_edit(status_msg, f"Something went wrong: `{str(e)[:200]}`")
            return

        filename       = result["filename"]
        file_size      = result["file_size"]
        original_title = result["title"]
        thumb_path     = result.get("thumbnail_path")

        await _safe_edit(
            status_msg,
            f"📤 Uploading... ({format_size(file_size)})"
        )

        caption = original_title

        if not is_premium(user.id):
            db_user = get_user(user.id)
            if db_user:
                remaining = max(0, db_user["free_limit"] - db_user["downloads_used"])
                if remaining <= 2:
                    caption += f"\n\n⚠️ {remaining} free download(s) left — /plans"

        try:
            await client.send_video(
                chat_id=message.chat.id,
                video=filename,
                caption=caption,
                thumb=thumb_path,
                supports_streaming=True,
            )
        except Exception:
            try:
                await client.send_document(
                    chat_id=message.chat.id,
                    document=filename,
                    caption=caption,
                    thumb=thumb_path,
                )
            except Exception as e:
                logger.error("Upload failed for user %d: %s", user.id, str(e)[:200])
                await _safe_edit(status_msg, f"Upload failed: `{str(e)[:200]}`")
                return

        try:
            record_download(user.id, url, platform, file_size, "success")
        except Exception:
            logger.exception("Failed to record download for user %d", user.id)

        db_user = get_user(user.id)
        if db_user and not is_premium(user.id):
            used_now  = db_user["downloads_used"]
            limit     = db_user["free_limit"]
            remaining = max(0, limit - used_now)
            footer    = f"\n🆓 {used_now}/{limit} used  •  {remaining} left"
        else:
            footer = "\n✅ Premium"

        await _safe_edit(status_msg, f"✅ Done!{footer}")
