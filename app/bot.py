"""Telegram bot (aiogram v3): group management and SMS forwarding via inline buttons.

The user only sends /start — all other actions are handled via buttons.
"""

import asyncio
import logging
import re
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, unquote, urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import config, db, gmail_auth, msgplane

logger = logging.getLogger(__name__)
router = Router()

# Set when create_bot() is called in main.py
_bot: Bot | None = None

# Telegram OAuth state
_pending_flow = None          # current authorization Flow object
_awaiting_code: set[int] = set()  # admin IDs awaiting authorization code

# Contact add state: user_id -> {"step": 1} or {"step": 2, "msgplane": "Albert"}
_contact_add_state: dict[int, dict] = {}


HELP_TEXT = (
    "🤖 <b>RingCentral → Telegram SMS bot</b>\n\n"
    "Forwards SMS messages received on your RingCentral number to the selected group.\n\n"
    "<b>Setting up:</b>\n"
    "1️⃣ Add the bot to a group and make it an <b>admin</b>.\n"
    "2️⃣ Confirm the group via «✅ Confirm» button.\n"
    "3️⃣ Select it as the forwarding target via «📌 Select».\n"
    "4️⃣ All incoming SMS messages will be forwarded to that group.\n\n"
    "All actions are performed using the buttons below."
)


# ===== Permission filter (for Message and CallbackQuery) =====

class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and event.from_user.id in config.ADMIN_IDS


# ===== Keyboards =====

def _short(title: str | None, limit: int = 24) -> str:
    text = title or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Groups", callback_data="groups")
    b.button(text="📇 Contacts", callback_data="contacts")
    b.button(text="🧪 Test message", callback_data="test")
    b.button(text="🔑 Gmail authorization", callback_data="reauth")
    b.button(text="ℹ️ Help", callback_data="help")
    b.adjust(1)
    return b.as_markup()


def contacts_kb(rows) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        b.button(
            text=f"➖ Delete: {row['msgplane_username']}",
            callback_data=f"del_contact:{row['id']}",
        )
    b.button(text="➕ Add contact", callback_data="contact_add")
    b.button(text="🔄 Refresh", callback_data="contacts")
    b.button(text="⬅️ Back", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data="menu")
    return b.as_markup()


def groups_kb(rows) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        title = _short(row["title"] or str(row["chat_id"]))
        if row["status"] == "pending":
            b.button(text=f"✅ Confirm: {title}", callback_data=f"confirm:{row['chat_id']}")
        elif row["status"] == "active":
            if not row["is_target"]:
                b.button(text=f"📌 Select: {title}", callback_data=f"select:{row['chat_id']}")
            b.button(text=f"🗑 Remove: {title}", callback_data=f"remove:{row['chat_id']}")
    b.button(text="🔄 Refresh", callback_data="groups")
    b.button(text="⬅️ Back", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def confirm_kb(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Confirm", callback_data=f"confirm:{chat_id}")
    return b.as_markup()


# ===== /start (the only command) =====

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if uid not in config.ADMIN_IDS:
        await message.answer(
            "👋 Hello! This bot is for admins only.\n"
            f"Your Telegram ID: <code>{uid}</code>\n"
            "Give this ID to the bot owner to become an admin."
        )
        return

    # If Gmail token is missing or expired — request authorization
    if not await asyncio.to_thread(gmail_auth.has_valid_token):
        await request_authorization([uid], "⚠️ Gmail authorization required.")
        return

    await message.answer(
        "🤖 <b>Main menu</b>\nSelect an action:", reply_markup=main_menu_kb()
    )


# ===== Menu buttons (admin only) =====

@router.callback_query(F.data == "menu", IsAdmin())
async def cb_menu(cq: CallbackQuery) -> None:
    await _safe_edit(cq.message, "🤖 <b>Main menu</b>\nSelect an action:", main_menu_kb())
    await cq.answer()


@router.callback_query(F.data == "help", IsAdmin())
async def cb_help(cq: CallbackQuery) -> None:
    await _safe_edit(cq.message, HELP_TEXT, back_kb())
    await cq.answer()


@router.callback_query(F.data == "groups", IsAdmin())
async def cb_groups(cq: CallbackQuery) -> None:
    rows = await db.list_groups(["active", "pending"])
    await _safe_edit(cq.message, _groups_text(rows), groups_kb(rows))
    await cq.answer()


@router.callback_query(F.data == "contacts", IsAdmin())
async def cb_contacts(cq: CallbackQuery) -> None:
    rows = await db.list_contacts()
    await _safe_edit(cq.message, _contacts_text(rows), contacts_kb(rows))
    await cq.answer()


@router.callback_query(F.data == "contact_add", IsAdmin())
async def cb_contact_add(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    _contact_add_state[uid] = {"step": 1}
    await cq.answer()
    await cq.message.answer("Enter the agent's MsgPlane username (e.g. Albert):")


@router.callback_query(F.data.startswith("del_contact:"), IsAdmin())
async def cb_del_contact(cq: CallbackQuery) -> None:
    contact_id = int(cq.data.split(":", 1)[1])
    deleted = await db.delete_contact(contact_id)
    await cq.answer("✅ Deleted" if deleted else "⚠️ Not found")
    rows = await db.list_contacts()
    await _safe_edit(cq.message, _contacts_text(rows), contacts_kb(rows))


@router.callback_query(F.data.startswith("confirm:"), IsAdmin())
async def cb_confirm(cq: CallbackQuery) -> None:
    chat_id = int(cq.data.split(":", 1)[1])
    try:
        chat = await cq.bot.get_chat(chat_id)
        me = await cq.bot.get_me()
        member = await cq.bot.get_chat_member(chat_id, me.id)
    except Exception as exc:
        await cq.answer(f"❌ Error: {exc}", show_alert=True)
        return

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        await cq.answer("⚠️ The bot is not an admin in this group. Make it an admin first.", show_alert=True)
        return

    await db.set_group_active(chat.id, chat.title, chat.username, cq.from_user.id)

    # Auto-select as target if no other target exists
    existing_target = await db.get_target_chat_id()
    if existing_target is None:
        await db.set_target_group(chat.id)
        await cq.answer("✅ Confirmed and selected as forwarding target")
    else:
        await cq.answer("✅ Confirmed")

    rows = await db.list_groups(["active", "pending"])
    await _safe_edit(cq.message, _groups_text(rows), groups_kb(rows))


@router.callback_query(F.data.startswith("remove:"), IsAdmin())
async def cb_remove(cq: CallbackQuery) -> None:
    chat_id = int(cq.data.split(":", 1)[1])
    await db.set_group_status(chat_id, "removed")
    await cq.answer("🗑 Removed")
    rows = await db.list_groups(["active", "pending"])
    await _safe_edit(cq.message, _groups_text(rows), groups_kb(rows))


@router.callback_query(F.data.startswith("select:"), IsAdmin())
async def cb_select(cq: CallbackQuery) -> None:
    chat_id = int(cq.data.split(":", 1)[1])
    try:
        me = await cq.bot.get_me()
        member = await cq.bot.get_chat_member(chat_id, me.id)
    except Exception as exc:
        await cq.answer(f"❌ Error: {exc}", show_alert=True)
        return
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        await cq.answer("⚠️ The bot is not an admin in this group.", show_alert=True)
        return

    await db.set_target_group(chat_id)
    await cq.answer("📌 Selected as forwarding target")
    rows = await db.list_groups(["active", "pending"])
    await _safe_edit(cq.message, _groups_text(rows), groups_kb(rows))


@router.callback_query(F.data == "test", IsAdmin())
async def cb_test(cq: CallbackQuery) -> None:
    sms = {
        "from_number": "TEST",
        "to_name": "TEST",
        "text": "This is a test message ✅",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    sent = await forward_sms(sms)
    if sent:
        await cq.answer("📤 Test sent to the target group.", show_alert=True)
    else:
        await cq.answer("⚠️ No target group selected. Select one in 📋 Groups.", show_alert=True)


@router.callback_query(F.data == "reauth", IsAdmin())
async def cb_reauth(cq: CallbackQuery) -> None:
    await cq.answer()
    await request_authorization([cq.from_user.id], "🔑 Gmail re-authorization.")


# All other callbacks (non-admin or expired buttons)
@router.callback_query()
async def cb_fallback(cq: CallbackQuery) -> None:
    await cq.answer("⛔ Access denied or button expired.", show_alert=True)


# ===== Telegram OAuth: admin sends the code here =====

@router.message(IsAdmin(), F.text, F.chat.type == ChatType.PRIVATE)
async def on_admin_text(message: Message) -> None:
    uid = message.from_user.id

    if uid in _contact_add_state:
        await _handle_contact_add(message, uid)
        return

    if uid not in _awaiting_code:
        return  # not awaiting authorization, ignore other text

    code = _extract_code(message.text)
    if not code:
        await message.answer(
            "❌ Code not found. Send the <code>code</code> value from the URL or the full URL."
        )
        return

    global _pending_flow
    if _pending_flow is None:
        _awaiting_code.discard(uid)
        await message.answer("⚠️ Authorization session not found. Try again: 🔑 button or /start.")
        return

    try:
        await asyncio.to_thread(gmail_auth.finish_auth, _pending_flow, code)
    except Exception as exc:
        await message.answer(
            f"❌ Authorization error: {escape(str(exc))}\n"
            "Try again (🔑 button or /start)."
        )
        return

    _awaiting_code.clear()
    _pending_flow = None
    await message.answer(
        "✅ Gmail authorization successful! The bot can now read emails.",
        reply_markup=main_menu_kb(),
    )


# ===== Auto-detect bot being added to a group =====

@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    new_status = event.new_chat_member.status
    actor_id = event.from_user.id if event.from_user else None

    if new_status == ChatMemberStatus.ADMINISTRATOR:
        # Bot made admin → auto-confirm and auto-select as target
        await db.set_group_active(chat.id, chat.title, chat.username, actor_id)
        await db.set_target_group(chat.id)
        logger.info(
            "Bot added as admin: %s (%s) — auto-selected as target",
            chat.title, chat.id,
        )
        title = escape(chat.title or "—")
        text = (
            f"✅ Bot added as admin to: <b>{title}</b>\n"
            "📌 Automatically selected as the forwarding target.\n"
            "All SMS messages will now be forwarded to this group."
        )
        await _notify_admins(text)

    elif new_status == ChatMemberStatus.MEMBER:
        await db.upsert_pending_group(chat.id, chat.title, chat.username, actor_id)
        logger.info("Bot added to group: %s (%s) as member", chat.title, chat.id)
        title = escape(chat.title or "—")
        text = (
            f"➕ Bot added to group: <b>{title}</b>\n"
            "⚠️ Make the bot an <b>admin</b> — it will be auto-confirmed and selected."
        )
        await _notify_admins(text)

    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.set_group_status(chat.id, "removed")
        logger.info("Bot removed from group: %s (%s)", chat.title, chat.id)


# ===== Helper functions =====

async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edits a message; ignores error if content hasn't changed."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def _notify_admins(text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Sends a private message to all admins (ignores errors)."""
    if _bot is None:
        return
    for admin_id in config.ADMIN_IDS:
        try:
            await _bot.send_message(admin_id, text, reply_markup=kb)
        except Exception:
            pass  # admin may not have started the bot in private chat


def _extract_code(text: str) -> str | None:
    """Extracts the OAuth code from text sent by admin (code or full URL)."""
    text = (text or "").strip()
    if not text:
        return None
    if "code=" in text:
        try:
            qs = parse_qs(urlparse(text).query)
            if qs.get("code"):
                return qs["code"][0]
        except Exception:
            pass
        m = re.search(r"code=([^&\s]+)", text)
        if m:
            return unquote(m.group(1))
    return text


async def request_authorization(admin_ids, reason: str = "") -> None:
    """Creates a Gmail OAuth URL and sends it to admins, then awaits the code."""
    global _pending_flow
    if _bot is None:
        return
    try:
        flow = await asyncio.to_thread(gmail_auth.create_auth_flow)
        url = gmail_auth.authorization_url(flow)
    except Exception as exc:
        logger.error("Failed to create authorization URL: %s", exc)
        for aid in admin_ids:
            try:
                await _bot.send_message(
                    aid,
                    f"❌ Failed to create authorization URL: {escape(str(exc))}\n"
                    "Is the client_secret_*.json file in the project directory?",
                )
            except Exception:
                pass
        return

    _pending_flow = flow
    text = (
        (f"{reason}\n\n" if reason else "")
        + "🔑 <b>Gmail authorization</b>\n\n"
        "1️⃣ Open the link and <b>grant access</b> to your Google account:\n"
        f"{escape(url)}\n\n"
        "2️⃣ After granting access, the browser will redirect to <code>http://localhost/?code=...</code> "
        "(the page may not load — that's normal).\n"
        "3️⃣ Send the <b>code</b> value from that URL (or the full URL) to this chat."
    )
    for aid in admin_ids:
        _awaiting_code.add(aid)
        try:
            await _bot.send_message(aid, text, disable_web_page_preview=True)
        except Exception:
            pass


async def _handle_contact_add(message: Message, uid: int) -> None:
    state = _contact_add_state[uid]
    text = (message.text or "").strip()

    if state["step"] == 1:
        if not text:
            await message.answer("Please enter a valid MsgPlane username:")
            return
        _contact_add_state[uid] = {"step": 2, "msgplane": text}
        await message.answer(f"MsgPlane: <b>{escape(text)}</b>\nNow enter the Telegram username (@username):")

    elif state["step"] == 2:
        tg = text if text.startswith("@") else "@" + text
        msgplane_name = state["msgplane"]
        await db.upsert_contact(msgplane_name, tg)
        del _contact_add_state[uid]
        await message.answer(
            f"✅ Saved: <b>{escape(msgplane_name)}</b> → <code>{escape(tg)}</code>",
            reply_markup=main_menu_kb(),
        )


def _contacts_text(rows) -> str:
    if not rows:
        return (
            "📇 <b>Contacts</b>\n\n"
            "No contacts yet.\n"
            "Press <b>➕ Add contact</b> to link a MsgPlane agent to a Telegram username."
        )
    lines = [f"📇 <b>Contacts: {len(rows)}</b>\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {escape(row['msgplane_username'])} → <code>{escape(row['telegram_username'])}</code>")
    return "\n".join(lines)


def _groups_text(rows) -> str:
    if not rows:
        return (
            "📭 No groups yet.\n\n"
            "Add the bot to a group and make it an <b>admin</b> — "
            "the group will appear here."
        )
    lines = ["<b>📋 Groups</b>\n"]
    for row in rows:
        if row["status"] == "active" and row["is_target"]:
            emoji = "📌"
            status_text = "selected"
        elif row["status"] == "active":
            emoji = "✅"
            status_text = "active"
        else:
            emoji = "🕓"
            status_text = "pending"
        uname = f" (@{row['username']})" if row["username"] else ""
        title = escape(row["title"] or "—")
        lines.append(f"{emoji} {title}{uname} — <i>{status_text}</i>")
    lines.append("\n📌 = forwarding target\n🕓 = not confirmed")
    return "\n".join(lines)


def _format_sms(sms: dict, agent_mention: str = "") -> str:
    kind = sms.get("kind") or "sms"
    from_number = sms.get("from_number") or "—"
    to_name = sms.get("to_name") or "—"
    time_str = sms.get("time") or ""
    length = sms.get("length") or ""
    text = (sms.get("text") or "").strip()
    attachments = sms.get("attachments") or []

    if kind == "voice":
        header = "🎙 <b>New voice message</b>"
    elif kind == "fax":
        header = "📠 <b>New fax</b>"
    else:
        header = "📩 <b>New SMS</b>"

    lines = [
        header,
        f"👤 From: <code>{escape(str(from_number))}</code>",
        f"📥 Recipient: {escape(str(to_name))}",
    ]
    if time_str:
        lines.append(f"🕐 {escape(str(time_str))}")
    if kind == "voice" and length:
        lines.append(f"⏱ Duration: {escape(str(length))}")

    lines.append("──────────────")
    if text:
        if kind == "voice":
            lines.append("🗣 <i>Transcription:</i>")
        lines.append(escape(str(text)))
    elif attachments:
        lines.append("<i>(no text — attachment below)</i>")
    else:
        lines.append("<i>(empty message)</i>")

    result = "\n".join(lines)
    if agent_mention:
        result += f"\n\nAgent: {escape(agent_mention)}"
    return result


async def _resolve_agent_mention(order_id: str | None) -> str:
    """Returns agent mention string (e.g. '@albert_tg' or 'Albert') for the given order ID."""
    if not order_id or not config.MSGPLANE_API_KEY:
        return ""
    user_name = await msgplane.get_agent_name(
        config.MSGPLANE_API_KEY, order_id, config.MSGPLANE_API_URL
    )
    if not user_name:
        return ""
    contact = await db.get_contact_by_msgplane(user_name)
    return contact["telegram_username"] if contact else user_name


async def forward_sms(sms: dict) -> int:
    """Forwards an SMS to the selected target group.

    If no target group is selected, the SMS is sent to the admins'
    private chats as a fallback (so nothing is lost).
    Returns the number of chats the message was sent to.
    """
    if _bot is None:
        raise RuntimeError("Bot not created. Call create_bot() first.")

    target_id = await db.get_target_chat_id()
    agent_mention = await _resolve_agent_mention(sms.get("order_id"))
    text = _format_sms(sms, agent_mention=agent_mention)
    attachments = sms.get("attachments") or []

    # No target group — fallback to admin private chats
    if target_id is None:
        admin_ids = list(config.ADMIN_IDS)
        if not admin_ids:
            logger.warning("No target group or admins — SMS not forwarded.")
            return 0
        logger.info("No target group selected — sending SMS to admins.")
        fallback_text = (
            "⚠️ <i>No target group selected — SMS sent here instead.\n"
            "Use 📋 Groups → 📌 Select to choose a forwarding target.</i>\n\n"
            + text
        )
        sent = 0
        for admin_id in admin_ids:
            try:
                await _bot.send_message(admin_id, fallback_text)
                for fname, ctype, data in attachments:
                    await _send_attachment(admin_id, fname, ctype, data)
                sent += 1
            except Exception as exc:
                logger.error("Failed to send to admin (%s): %s", admin_id, exc)
        return sent

    # Send to the target group
    try:
        await _bot.send_message(target_id, text)
        for fname, ctype, data in attachments:
            await _send_attachment(target_id, fname, ctype, data)
        return 1
    except Exception as exc:
        logger.error("Failed to send to target group (%s): %s", target_id, exc)
        return 0


async def _send_attachment(chat_id: int, filename: str, ctype: str, data: bytes) -> None:
    """Sends an attachment based on its type: image/audio/video/document."""
    ctype = (ctype or "").lower()
    try:
        file = BufferedInputFile(data, filename=filename)
        if ctype.startswith("image/"):
            await _bot.send_photo(chat_id, file)
        elif ctype.startswith("audio/"):
            await _bot.send_audio(chat_id, file)
        elif ctype.startswith("video/"):
            await _bot.send_video(chat_id, file)
        else:
            await _bot.send_document(chat_id, file)
    except Exception:
        # Fall back to sending as a document
        await _bot.send_document(chat_id, BufferedInputFile(data, filename=filename))


def create_bot() -> tuple[Bot, Dispatcher]:
    """Creates the Bot and Dispatcher, and registers handlers."""
    global _bot
    _bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    return _bot, dp
