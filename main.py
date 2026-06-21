import logging

# ── Logging must be configured before ANY other import ──────────────────────
# If basicConfig is called after pyrogram is imported it becomes a no-op
# because pyrogram initialises its loggers on import and the root logger
# already has handlers by then.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _pkg in (
    "pyrogram",
    "pyrogram.connection",
    "pyrogram.connection.connection",
    "pyrogram.session",
    "pyrogram.session.session",
    "pyrogram.crypto",
    "pyrogram.dispatcher",
    "pyrogram.parser",
):
    logging.getLogger(_pkg).setLevel(logging.WARNING)
# ────────────────────────────────────────────────────────────────────────────

import os
import asyncio
import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

from bot.db import init_db
from bot.backup import create_backup

from bot.start import start_handler, help_handler, status_handler, button_handler
from bot.download import download_handler
from bot.premium import (
    plans_handler, buy_callback, payment_method_callback,
    add_premium_handler, revoke_premium_handler,
)
from bot.admin import (
    stats_handler, users_handler, ban_handler, unban_handler,
    broadcast_handler, backup_handler, list_backups_handler, restore_handler,
)

logger = logging.getLogger(__name__)

API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

KNOWN_COMMANDS = [
    "start", "help", "status", "plans",
    "addpremium", "revokepremium", "ban", "unban",
    "stats", "broadcast", "backup", "restore", "listbackups", "users", "cancel",
]


async def auto_backup():
    try:
        result = await create_backup()
        logger.info("Auto-backup done: %s (%d users)", result["filename"], result["user_count"])
    except Exception:
        logger.exception("Auto-backup failed")


async def main():
    init_db()
    logger.info("Database initialised.")

    app = Client(
        "video_downloader_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        parse_mode=ParseMode.MARKDOWN,
        workers=4,
    )

    app.add_handler(MessageHandler(start_handler,  filters.command("start")  & filters.private))
    app.add_handler(MessageHandler(help_handler,   filters.command("help")   & filters.private))
    app.add_handler(MessageHandler(status_handler, filters.command("status") & filters.private))
    app.add_handler(MessageHandler(plans_handler,  filters.command("plans")  & filters.private))

    app.add_handler(MessageHandler(add_premium_handler,    filters.command("addpremium")    & filters.private))
    app.add_handler(MessageHandler(revoke_premium_handler, filters.command("revokepremium") & filters.private))
    app.add_handler(MessageHandler(ban_handler,            filters.command("ban")           & filters.private))
    app.add_handler(MessageHandler(unban_handler,          filters.command("unban")         & filters.private))
    app.add_handler(MessageHandler(stats_handler,          filters.command("stats")         & filters.private))
    app.add_handler(MessageHandler(users_handler,          filters.command("users")         & filters.private))
    app.add_handler(MessageHandler(broadcast_handler,      filters.command("broadcast")     & filters.private))
    app.add_handler(MessageHandler(backup_handler,         filters.command("backup")        & filters.private))
    app.add_handler(MessageHandler(list_backups_handler,   filters.command("listbackups")   & filters.private))
    app.add_handler(MessageHandler(restore_handler,        filters.command("restore")       & filters.private))

    app.add_handler(CallbackQueryHandler(button_handler,          filters.regex(r"^(help|status|plans)$")))
    app.add_handler(CallbackQueryHandler(buy_callback,            filters.regex(r"^buy_")))
    app.add_handler(CallbackQueryHandler(payment_method_callback, filters.regex(r"^pay_")))

    app.add_handler(MessageHandler(
        download_handler,
        filters.private & filters.text & ~filters.command(KNOWN_COMMANDS),
    ))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_backup, "cron", hour=3, minute=0)
    scheduler.start()
    logger.info("Scheduler started — daily backup at 03:00.")

    logger.info("Starting bot…")
    async with app:
        me = await app.get_me()
        logger.info("Bot online: @%s (ID: %d)", me.username, me.id)

        for admin_id in ADMIN_IDS:
            try:
                await app.send_message(
                    admin_id,
                    f"✅ Bot is online — @{me.username}\n"
                    f"🕐 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                )
            except Exception:
                logger.warning("Could not notify admin %d on startup", admin_id)

        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
