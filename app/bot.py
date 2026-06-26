"""Telegram bot (aiogram v3): inline tugmalar orqali guruh boshqaruvi va SMS uzatish.

Foydalanuvchi faqat /start yuboradi — qolgan barcha amallar tugmalar orqali.
"""

import logging
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import config, db

logger = logging.getLogger(__name__)
router = Router()

# main.py'da create_bot() chaqirilganda o'rnatiladi
_bot: Bot | None = None


HELP_TEXT = (
    "🤖 <b>RingCentral → Telegram SMS bot</b>\n\n"
    "RingCentral raqamiga kelgan SMS'larni tasdiqlangan guruhlarga uzatadi.\n\n"
    "<b>Guruh qo'shish:</b>\n"
    "1️⃣ Botni guruhga qo'shing va <b>admin</b> qilib tayinlang.\n"
    "2️⃣ Bot sizga «✅ Tasdiqlash» tugmasi bilan xabar yuboradi (yoki «📋 Guruhlar» menyusidan tasdiqlang).\n"
    "3️⃣ Tasdiqlangach, kelgan SMS'lar shu guruhga uzatiladi.\n\n"
    "Barcha amallar pastdagi tugmalar orqali bajariladi."
)


# ===== Ruxsat filtri (Message va CallbackQuery uchun) =====

class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and event.from_user.id in config.ADMIN_IDS


# ===== Klaviaturalar =====

def _short(title: str | None, limit: int = 24) -> str:
    text = title or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Guruhlar", callback_data="groups")
    b.button(text="🧪 Test xabar", callback_data="test")
    b.button(text="ℹ️ Yordam", callback_data="help")
    b.adjust(1)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Orqaga", callback_data="menu")
    return b.as_markup()


def groups_kb(rows) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        title = _short(row["title"] or str(row["chat_id"]))
        if row["status"] == "pending":
            b.button(text=f"✅ Tasdiqlash: {title}", callback_data=f"confirm:{row['chat_id']}")
        elif row["status"] == "active":
            b.button(text=f"🗑 O'chirish: {title}", callback_data=f"remove:{row['chat_id']}")
    b.button(text="🔄 Yangilash", callback_data="groups")
    b.button(text="⬅️ Orqaga", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def confirm_kb(chat_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tasdiqlash", callback_data=f"confirm:{chat_id}")
    return b.as_markup()


# ===== /start (yagona komanda) =====

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if uid in config.ADMIN_IDS:
        await message.answer("🤖 <b>Asosiy menyu</b>\nKerakli amalni tanlang:", reply_markup=main_menu_kb())
    else:
        await message.answer(
            "👋 Salom! Bu bot faqat adminlar uchun ishlaydi.\n"
            f"Sizning Telegram ID: <code>{uid}</code>\n"
            "Admin bo'lish uchun ushbu ID'ni bot egasiga bering."
        )


# ===== Menyu tugmalari (faqat admin) =====

@router.callback_query(F.data == "menu", IsAdmin())
async def cb_menu(cq: CallbackQuery) -> None:
    await _safe_edit(cq.message, "🤖 <b>Asosiy menyu</b>\nKerakli amalni tanlang:", main_menu_kb())
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


@router.callback_query(F.data.startswith("confirm:"), IsAdmin())
async def cb_confirm(cq: CallbackQuery) -> None:
    chat_id = int(cq.data.split(":", 1)[1])
    try:
        chat = await cq.bot.get_chat(chat_id)
        me = await cq.bot.get_me()
        member = await cq.bot.get_chat_member(chat_id, me.id)
    except Exception as exc:
        await cq.answer(f"❌ Xato: {exc}", show_alert=True)
        return

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        await cq.answer("⚠️ Bot bu guruhda admin emas. Avval admin qiling.", show_alert=True)
        return

    await db.set_group_active(chat.id, chat.title, chat.username, cq.from_user.id)
    await cq.answer("✅ Tasdiqlandi")
    rows = await db.list_groups(["active", "pending"])
    await _safe_edit(cq.message, _groups_text(rows), groups_kb(rows))


@router.callback_query(F.data.startswith("remove:"), IsAdmin())
async def cb_remove(cq: CallbackQuery) -> None:
    chat_id = int(cq.data.split(":", 1)[1])
    await db.set_group_status(chat_id, "removed")
    await cq.answer("🗑 O'chirildi")
    rows = await db.list_groups(["active", "pending"])
    await _safe_edit(cq.message, _groups_text(rows), groups_kb(rows))


@router.callback_query(F.data == "test", IsAdmin())
async def cb_test(cq: CallbackQuery) -> None:
    sms = {
        "from_number": "TEST",
        "to_name": "TEST",
        "text": "Bu test xabari ✅",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    sent = await forward_sms(sms)
    await cq.answer(f"📤 Test {sent} ta active guruhga yuborildi.", show_alert=True)


# Boshqa barcha callback'lar (admin bo'lmaganlar yoki eskirgan tugmalar)
@router.callback_query()
async def cb_fallback(cq: CallbackQuery) -> None:
    await cq.answer("⛔ Ruxsat yo'q yoki tugma eskirgan.", show_alert=True)


# ===== Botning guruhga qo'shilishini avtomatik aniqlash =====

@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    new_status = event.new_chat_member.status
    actor_id = event.from_user.id if event.from_user else None

    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        await db.upsert_pending_group(chat.id, chat.title, chat.username, actor_id)
        logger.info("Bot guruhga qo'shildi: %s (%s), status=%s", chat.title, chat.id, new_status)

        title = escape(chat.title or "—")
        if new_status == ChatMemberStatus.ADMINISTRATOR:
            text = f"➕ Bot admin qilib qo'shildi: <b>{title}</b>\nGuruhni tasdiqlaysizmi?"
            await _notify_admins(text, confirm_kb(chat.id))
        else:
            text = (
                f"➕ Bot guruhga qo'shildi: <b>{title}</b>\n"
                "⚠️ Bot hali admin emas. Avval admin qiling, so'ng «📋 Guruhlar» menyusidan tasdiqlang."
            )
            await _notify_admins(text)

    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.set_group_status(chat.id, "removed")
        logger.info("Bot guruhdan chiqarildi: %s (%s)", chat.title, chat.id)


# ===== Yordamchi funksiyalar =====

async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Xabarni tahrirlaydi; o'zgarmagan bo'lsa xatoni e'tiborsiz qoldiradi."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def _notify_admins(text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Barcha adminlarga shaxsiy xabar yuboradi (xato bo'lsa e'tiborsiz qoldiriladi)."""
    if _bot is None:
        return
    for admin_id in config.ADMIN_IDS:
        try:
            await _bot.send_message(admin_id, text, reply_markup=kb)
        except Exception:
            pass  # admin botni shaxsiy chatda ishga tushirmagan bo'lishi mumkin


def _groups_text(rows) -> str:
    if not rows:
        return (
            "📭 Hozircha guruh yo'q.\n\n"
            "Botni guruhga qo'shing va <b>admin</b> qiling — guruh shu yerda paydo bo'ladi, "
            "so'ng «✅ Tasdiqlash» tugmasini bosing."
        )
    lines = ["<b>📋 Guruhlar</b>\n"]
    for row in rows:
        emoji = "✅" if row["status"] == "active" else "🕓"
        uname = f" (@{row['username']})" if row["username"] else ""
        title = escape(row["title"] or "—")
        lines.append(f"{emoji} {title}{uname} — <i>{row['status']}</i>")
    lines.append("\n🕓 = tasdiqlanmagan. Tugma orqali tasdiqlang yoki o'chiring.")
    return "\n".join(lines)


def _format_sms(sms: dict) -> str:
    from_number = sms.get("from_number") or "—"
    to_name = sms.get("to_name") or "—"
    time_str = sms.get("time") or ""
    text = sms.get("text") or "(bo'sh xabar)"

    lines = [
        "📩 <b>Yangi SMS</b>",
        f"👤 Kimdan: <code>{escape(str(from_number))}</code>",
        f"📥 Qabul qiluvchi: {escape(str(to_name))}",
    ]
    if time_str:
        lines.append(f"🕐 {escape(str(time_str))}")
    lines.append("──────────────")
    lines.append(escape(str(text)))
    return "\n".join(lines)


async def forward_sms(sms: dict) -> int:
    """SMS'ni barcha 'active' guruhlarga uzatadi.

    Agar tasdiqlangan active guruh bo'lmasa, SMS yo'qolmasligi uchun
    adminlarning shaxsiy chatiga yuboriladi (zaxira yo'l).
    Yuborilgan chatlar sonini qaytaradi.
    """
    if _bot is None:
        raise RuntimeError("Bot yaratilmagan. Avval create_bot() chaqiring.")

    chat_ids = await db.get_active_chat_ids()
    fallback = False
    if not chat_ids:
        chat_ids = list(config.ADMIN_IDS)
        fallback = True
        if not chat_ids:
            logger.warning("Active guruh ham, admin ham yo'q — SMS uzatilmadi.")
            return 0
        logger.info("Active guruh yo'q — SMS adminlarga yuborilmoqda.")

    text = _format_sms(sms)
    if fallback:
        text = (
            "⚠️ <i>Hali tasdiqlangan guruh yo'q — SMS shu yerga yuborildi.</i>\n\n"
            + text
        )

    sent = 0
    for chat_id in chat_ids:
        try:
            await _bot.send_message(chat_id, text)
            sent += 1
        except Exception as exc:
            logger.error("Yuborib bo'lmadi (%s): %s", chat_id, exc)
            # Faqat haqiqiy guruhlar uchun holatni o'zgartiramiz (admin ID'lari emas)
            if not fallback:
                await db.set_group_status(chat_id, "removed")
    return sent


def create_bot() -> tuple[Bot, Dispatcher]:
    """Bot va Dispatcher yaratadi, handlerlarni ulaydi."""
    global _bot
    _bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    return _bot, dp
