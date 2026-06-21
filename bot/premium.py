import os
import logging
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from bot.db import get_or_create_user, add_premium, revoke_premium, get_user, add_payment_request
from bot.helpers import PLANS, PAYMENT_METHODS, format_time_left

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

PAYMENT_DETAILS = {
    "paypal":      os.environ.get("PAYPAL_EMAIL",   ""),
    "crypto":      os.environ.get("CRYPTO_ADDRESS", ""),
    "apple_pay":   os.environ.get("APPLE_PAY_INFO", ""),
    "credit_card": os.environ.get("CARD_INFO",      ""),
    "upi":         os.environ.get("UPI_ID",         ""),
}


async def plans_handler(client, message: Message):
    user = message.from_user
    try:
        get_or_create_user(user.id, user.username, user.first_name, user.last_name)
    except Exception:
        logger.exception("DB error in plans_handler for user %d", user.id)

    lines   = ["💎 **Premium Plans**\n\nUnlimited downloads on every platform.\n"]
    buttons = []

    for key, plan in PLANS.items():
        lines.append(f"• **{plan['label']}** — ${plan['price_usd']}")
        buttons.append([InlineKeyboardButton(
            f"{plan['label']}  —  ${plan['price_usd']}",
            callback_data=f"buy_{key}",
        )])

    lines += [
        "",
        "After paying, send your payment screenshot to the admin and"
        " you'll be activated within 24 hours.",
    ]

    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def buy_callback(client, callback_query: CallbackQuery):
    plan_key = callback_query.data.replace("buy_", "", 1)
    if plan_key not in PLANS:
        await callback_query.answer("Invalid plan.", show_alert=True)
        return
    await _show_payment_methods(callback_query.message, plan_key)
    await callback_query.answer()


async def _show_payment_methods(message, plan_key: str):
    plan    = PLANS[plan_key]
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"pay_{plan_key}_{key}")]
        for key, name in PAYMENT_METHODS.items()
    ]
    text = f"💎 **{plan['label']} — ${plan['price_usd']}**\n\nHow do you want to pay?"
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def payment_method_callback(client, callback_query: CallbackQuery):
    parts = callback_query.data.split("_", 2)
    if len(parts) < 3:
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    plan_key, method_key = parts[1], parts[2]
    user = callback_query.from_user

    if plan_key not in PLANS or method_key not in PAYMENT_METHODS:
        await callback_query.answer("Invalid selection.", show_alert=True)
        return

    plan           = PLANS[plan_key]
    method_name    = PAYMENT_METHODS[method_key]
    payment_detail = PAYMENT_DETAILS.get(method_key, "")

    try:
        request_id = add_payment_request(user.id, plan_key, plan["days"], plan["price_usd"], method_key)
    except Exception:
        logger.exception("Failed to save payment request for user %d", user.id)
        await callback_query.answer("Something went wrong. Try again.", show_alert=True)
        return

    detail_line = f"`{payment_detail}`" if payment_detail else "_Contact admin for payment details_"

    text = (
        f"**How to pay — {method_name}**\n\n"
        f"Plan: **{plan['label']}**\n"
        f"Amount: **${plan['price_usd']}**\n\n"
        f"Send to:\n{detail_line}\n\n"
        f"Once paid:\n"
        f"1. Screenshot your receipt\n"
        f"2. Send it to the admin along with:\n"
        f"   — Your ID: `{user.id}`\n"
        f"   — Request: `#{request_id}`\n\n"
        f"You'll be activated within 24 hours."
    )

    await callback_query.message.edit_text(text)

    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(
                admin_id,
                f"💳 **Payment Request #{request_id}**\n\n"
                f"User: {user.first_name} (`{user.id}`) @{user.username or '—'}\n"
                f"Plan: {plan['label']} ({plan['days']} days) — ${plan['price_usd']} via {method_name}\n\n"
                f"Activate: `/addpremium {user.id} {plan['days']}`",
            )
        except Exception:
            logger.warning("Could not notify admin %d about payment request %d", admin_id, request_id)

    await callback_query.answer()


async def add_premium_handler(client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply_text("❌ Admin only.")
        return

    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text("Usage: `/addpremium <user_id> <days>`")
        return

    try:
        target_id, days = int(args[0]), int(args[1])
    except ValueError:
        await message.reply_text("Both user_id and days must be numbers.")
        return

    if not get_user(target_id):
        await message.reply_text(f"User `{target_id}` not found.")
        return

    try:
        expiry = add_premium(target_id, days, f"{days} days")
    except Exception:
        logger.exception("Failed to add premium for user %d", target_id)
        await message.reply_text("DB error — could not activate premium.")
        return

    await message.reply_text(
        f"✅ Done — `{target_id}` is now premium.\n"
        f"Duration: {days} days\n"
        f"Expires: `{expiry.strftime('%Y-%m-%d %H:%M')}`"
    )
    try:
        await client.send_message(
            target_id,
            f"🎉 **You're now Premium!**\n\n"
            f"Duration: **{days} days**\n"
            f"Expires: `{expiry.strftime('%Y-%m-%d %H:%M')}`\n\n"
            f"Enjoy unlimited downloads!",
        )
    except Exception:
        logger.warning("Could not notify user %d about premium activation", target_id)


async def revoke_premium_handler(client, message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply_text("❌ Admin only.")
        return

    args = message.command[1:]
    if not args:
        await message.reply_text("Usage: `/revokepremium <user_id>`")
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
        revoke_premium(target_id)
    except Exception:
        logger.exception("Failed to revoke premium for user %d", target_id)
        await message.reply_text("DB error — could not revoke premium.")
        return

    await message.reply_text(f"✅ Premium revoked for `{target_id}`.")
    try:
        await client.send_message(target_id, "Your premium access has been removed.")
    except Exception:
        logger.warning("Could not notify user %d about premium revocation", target_id)
