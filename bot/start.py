import os
import logging
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from bot.db import get_or_create_user, is_premium
from bot.helpers import get_help_text, format_time_left, PLANS

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]


async def start_handler(client, message: Message):
    user = message.from_user
    try:
        db_user = get_or_create_user(
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
    except Exception:
        logger.exception("Failed to get/create user %d in start_handler", user.id)
        await message.reply_text("Something went wrong. Try again in a moment.")
        return

    premium = is_premium(user.id)
    used  = db_user["downloads_used"]
    limit = db_user["free_limit"]

    if premium:
        expiry    = db_user.get("premium_expiry")
        quota_line = f"💎 **Premium** — expires in {format_time_left(expiry)}"
    else:
        quota_line = f"🆓 **Free** — {used}/{limit} downloads used"

    text = (
        f"Hey {user.first_name}! 👋\n\n"
        f"Send me any video link and I'll download it for you.\n\n"
        f"{quota_line}"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Help",      callback_data="help"),
            InlineKeyboardButton("📊 My Status", callback_data="status"),
        ],
        [
            InlineKeyboardButton("💎 Go Premium", callback_data="plans"),
        ],
    ])

    await message.reply_text(text, reply_markup=buttons)


async def help_handler(client, message: Message):
    is_admin = message.from_user.id in ADMIN_IDS
    await message.reply_text(get_help_text(is_admin))


async def status_handler(client, message: Message):
    user = message.from_user
    try:
        db_user = get_or_create_user(
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
    except Exception:
        logger.exception("DB error in status_handler for user %d", user.id)
        await message.reply_text("Couldn't fetch your info right now. Try again.")
        return

    premium = is_premium(user.id)

    if premium:
        expiry = db_user.get("premium_expiry")
        plan   = db_user.get("premium_plan", "—")
        status_text = f"💎 **Premium**\nPlan: {plan}\nExpires in: {format_time_left(expiry)}"
    else:
        used      = db_user["downloads_used"]
        limit     = db_user["free_limit"]
        remaining = max(0, limit - used)
        bar       = "█" * min(used, limit) + "░" * max(0, limit - used)
        status_text = f"🆓 **Free**\n{used}/{limit} downloads  [{bar}]\n{remaining} left"

    text = (
        f"**Your Account**\n\n"
        f"Name: {user.first_name}\n"
        f"ID: `{user.id}`\n"
        f"Username: @{user.username or '—'}\n\n"
        f"{status_text}"
    )

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Upgrade to Premium", callback_data="plans")],
    ]) if not premium else None

    await message.reply_text(text, reply_markup=buttons)


async def button_handler(client, callback_query: CallbackQuery):
    data     = callback_query.data
    user     = callback_query.from_user
    is_admin = user.id in ADMIN_IDS

    try:
        if data == "help":
            await callback_query.message.edit_text(get_help_text(is_admin))

        elif data == "status":
            db_user   = get_or_create_user(user.id, user.username, user.first_name, user.last_name)
            premium   = is_premium(user.id)
            if premium:
                expiry = db_user.get("premium_expiry")
                plan   = db_user.get("premium_plan", "—")
                status_text = f"💎 **Premium**\nPlan: {plan}\nExpires in: {format_time_left(expiry)}"
            else:
                used      = db_user["downloads_used"]
                limit     = db_user["free_limit"]
                remaining = max(0, limit - used)
                bar       = "█" * min(used, limit) + "░" * max(0, limit - used)
                status_text = f"🆓 **Free**\n{used}/{limit} downloads  [{bar}]\n{remaining} left"
            text    = f"**Your Account**\n\n{status_text}"
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Upgrade to Premium", callback_data="plans")],
            ]) if not premium else None
            await callback_query.message.edit_text(text, reply_markup=buttons)

        elif data == "plans":
            from bot.helpers import admin_url
            lines = ["💎 **Premium Plans**\n\nUnlimited downloads on every platform.\n"]
            buttons = []
            for key, plan in PLANS.items():
                lines.append(f"• **{plan['label']}** — ${plan['price_usd']}")
                buttons.append([InlineKeyboardButton(
                    f"{plan['label']}  —  ${plan['price_usd']}",
                    callback_data=f"buy_{key}",
                )])
            lines.append("\nAfter paying, send your receipt to the admin and you'll be activated within 24 hours.")
            url = admin_url()
            if url:
                buttons.append([InlineKeyboardButton("💬 Contact Admin", url=url)])
            await callback_query.message.edit_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons),
            )

    except Exception:
        logger.exception("Error in button_handler (data=%s, user=%d)", data, user.id)

    await callback_query.answer()
