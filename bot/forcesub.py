import os
import logging
from pyrogram import Client
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChannelInvalid, PeerIdInvalid
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# Set this in .env as the channel username (@mychannel) or numeric ID (-100xxxxxxxxxx)
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "").strip()


async def is_subscribed(client: Client, user_id: int) -> bool:
    """Return True if the user is a member of the force-sub channel (or if force-sub is disabled)."""
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await client.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        # "left" and "kicked" mean they are not active members
        return member.status.value not in ("left", "banned", "kicked")
    except UserNotParticipant:
        return False
    except (ChatAdminRequired, ChannelInvalid, PeerIdInvalid):
        # Bot is not admin or channel is wrong — log once and fail open so users aren't locked out
        logger.warning(
            "Force-sub check failed: bot may not be admin in '%s'. "
            "Add the bot as admin with 'Invite Users' permission.",
            FORCE_SUB_CHANNEL,
        )
        return True
    except Exception:
        logger.exception("Unexpected error in force-sub check for user %d", user_id)
        return True  # fail open — never block users due to our own error


async def send_join_prompt(message: Message):
    """Send a 'please join our channel' message with a button."""
    channel = FORCE_SUB_CHANNEL

    # Build invite URL
    if channel.startswith("@"):
        invite_url = f"https://t.me/{channel.lstrip('@')}"
        display    = channel
    else:
        # Numeric ID — we can't build a public URL without the username.
        # The admin should set FORCE_SUB_CHANNEL as @username, not as a number.
        invite_url = f"https://t.me/{channel.lstrip('-100')}"
        display    = "our channel"

    await message.reply_text(
        f"You need to join {display} before you can use this bot.\n\n"
        f"Join and then send your link again.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Join channel 📢", url=invite_url)],
        ]),
    )
