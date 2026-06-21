import os
import logging
import asyncio
from datetime import datetime
from pyrogram.types import Message
from bot.db import get_stats, get_all_users, ban_user, unban_user, get_user
from bot.backup import create_backup, list_backups, restore_backup

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]


def _is_admin(message: Message) -> bool:
    return message.from_user.id in ADMIN_IDS


async def stats_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return
    try:
        stats = get_stats()
    except Exception:
        logger.exception("Failed to fetch stats")
        await message.reply_text("Couldn't fetch stats right now.")
        return

    text = (
        f"📊 **Bot Stats**\n\n"
        f"👥 Users: `{stats['total_users']}`  "
        f"(💎 {stats['premium_users']}  🆓 {stats['free_users']}  🚫 {stats['banned_users']})\n\n"
        f"📥 Downloads: `{stats['total_downloads']}` total  •  `{stats['downloads_today']}` today\n\n"
        f"🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    await message.reply_text(text)


async def users_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    args  = message.command[1:]
    limit = 20
    if args:
        try:
            limit = int(args[0])
        except ValueError:
            pass

    try:
        users = get_all_users()[:limit]
    except Exception:
        logger.exception("Failed to fetch user list")
        await message.reply_text("Couldn't fetch users right now.")
        return

    if not users:
        await message.reply_text("No users yet.")
        return

    lines = [f"👥 **Last {len(users)} users:**\n"]
    for u in users:
        badge  = "💎" if u["is_premium"] else "🆓"
        banned = " 🚫" if u["is_banned"] else ""
        name   = u.get("first_name") or "Unknown"
        lines.append(f"{badge} `{u['user_id']}` — {name}{banned}  ({u['downloads_used']} DLs)")
    await message.reply_text("\n".join(lines))


async def ban_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    args = message.command[1:]
    if not args:
        await message.reply_text("Usage: `/ban <user_id>`")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.reply_text("Invalid user_id.")
        return

    if not get_user(target_id):
        await message.reply_text(f"User `{target_id}` not found.")
        return

    try:
        ban_user(target_id)
    except Exception:
        logger.exception("Failed to ban user %d", target_id)
        await message.reply_text("DB error — could not ban user.")
        return

    await message.reply_text(f"✅ `{target_id}` banned.")
    try:
        await client.send_message(target_id, "You've been banned from this bot.")
    except Exception:
        logger.warning("Could not notify user %d about ban", target_id)


async def unban_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    args = message.command[1:]
    if not args:
        await message.reply_text("Usage: `/unban <user_id>`")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await message.reply_text("Invalid user_id.")
        return

    try:
        unban_user(target_id)
    except Exception:
        logger.exception("Failed to unban user %d", target_id)
        await message.reply_text("DB error — could not unban user.")
        return

    await message.reply_text(f"✅ `{target_id}` unbanned.")
    try:
        await client.send_message(target_id, "You've been unbanned and can use the bot again.")
    except Exception:
        logger.warning("Could not notify user %d about unban", target_id)


async def broadcast_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.reply_text("Usage: `/broadcast <message>`")
        return

    broadcast_text = args[1]
    try:
        users = get_all_users()
    except Exception:
        logger.exception("Failed to fetch users for broadcast")
        await message.reply_text("Couldn't fetch user list.")
        return

    status_msg = await message.reply_text(f"📢 Sending to {len(users)} users...")
    sent = failed = 0

    for user in users:
        if user.get("is_banned"):
            continue
        try:
            await client.send_message(user["user_id"], broadcast_text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"📢 **Broadcast done**\n\n"
        f"✅ Sent: {sent}  ❌ Failed: {failed}  📊 Total: {len(users)}"
    )


async def backup_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    status_msg = await message.reply_text("💾 Creating backup...")
    try:
        result = await create_backup()
        await status_msg.edit_text(
            f"✅ **Backup done**\n\n"
            f"File: `{result['filename']}`\n"
            f"Users: `{result['user_count']}`  Downloads: `{result['download_count']}`\n"
            f"Size: `{result['size_kb']} KB`\n"
            f"Repo: `{os.environ.get('GITHUB_REPO')}`"
        )
    except Exception as e:
        logger.exception("Backup failed")
        await status_msg.edit_text(f"❌ Backup failed:\n\n`{str(e)[:300]}`")


async def list_backups_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    status_msg = await message.reply_text("🔍 Fetching backup list...")
    try:
        backups = await list_backups()
    except Exception as e:
        logger.exception("Failed to list backups")
        await status_msg.edit_text(f"❌ Could not fetch backups:\n\n`{str(e)[:300]}`")
        return

    if not backups:
        await status_msg.edit_text("No backups found in the GitHub repo.")
        return

    lines = [f"📦 **{len(backups)} backups:**\n"]
    for i, b in enumerate(backups[:15], 1):
        lines.append(f"`{i}.` {b['name']}  ({b['size'] // 1024} KB)")
    lines.append("\nUse `/restore <filename>` to restore.")
    await status_msg.edit_text("\n".join(lines))


async def restore_handler(client, message: Message):
    if not _is_admin(message):
        await message.reply_text("❌ Admin only.")
        return

    args = message.command[1:]
    if not args:
        await message.reply_text(
            "Usage: `/restore <filename>`\n"
            "Use `/listbackups` to see what's available."
        )
        return

    filename = args[0]
    if not filename.startswith("backups/"):
        filename = f"backups/{filename}"

    status_msg = await message.reply_text(f"🔄 Restoring `{filename}`...")
    try:
        result = await restore_backup(filename)
        await status_msg.edit_text(
            f"✅ **Restore complete**\n\n"
            f"Users: `{result['users']}`\n"
            f"Downloads: `{result['downloads']}`\n"
            f"Premium keys: `{result['premium_keys']}`"
        )
    except Exception as e:
        logger.exception("Restore failed for file %s", filename)
        await status_msg.edit_text(f"❌ Restore failed:\n\n`{str(e)[:300]}`")
