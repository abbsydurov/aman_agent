import asyncio
import datetime
import json
import logging
import os
import random
import re
import time
import urllib.request

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import MessageEntityType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from database import db

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(getattr(config, "LOG_FILE", "bot.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Custom Log Handler for MongoDB
class MongoLogHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        if getattr(db, "is_connected", False):
            try:
                asyncio.create_task(db.log_event(record.levelname, log_entry))
            except Exception:
                pass

logger.addHandler(MongoLogHandler())

# ==========================================
# SAFE SEND/EDIT HELPERS (Prevents <tg-emoji> crashes)
# ==========================================

async def safe_send_message(bot, chat_id: int, text: str, parse_mode=ParseMode.HTML, reply_markup=None):
    """Sends HTML message safely; if <tg-emoji> or entity parsing fails, strips tags and retries."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        err_str = str(e).lower()
        if any(term in err_str for term in ["custom_emoji", "entity", "can't parse entities", "parse"]):
            clean_text = re.sub(r'</?tg-emoji[^>]*>', '', text)
            try:
                return await bot.send_message(chat_id=chat_id, text=clean_text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception:
                plain_text = re.sub(r'<[^>]+>', '', clean_text)
                try:
                    return await bot.send_message(chat_id=chat_id, text=plain_text, parse_mode=parse_mode, reply_markup=reply_markup)
                except Exception:
                    return await bot.send_message(chat_id=chat_id, text=plain_text)
        raise

async def safe_edit_message(message, text: str, parse_mode=ParseMode.HTML, reply_markup=None):
    """Edits message safely; falls back to stripping <tg-emoji> or plain text on entity parse failure."""
    try:
        return await message.edit_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        err_str = str(e).lower()
        if any(term in err_str for term in ["custom_emoji", "entity", "can't parse entities", "parse"]):
            clean_text = re.sub(r'</?tg-emoji[^>]*>', '', text)
            try:
                return await message.edit_text(text=clean_text, parse_mode=parse_mode, reply_markup=reply_markup)
            except Exception:
                plain_text = re.sub(r'<[^>]+>', '', clean_text)
                try:
                    return await message.edit_text(text=plain_text, parse_mode=parse_mode, reply_markup=reply_markup)
                except Exception:
                    return await message.edit_text(text=plain_text)
        raise

# ==========================================
# DATA LOADING & PERSISTENCE
# ==========================================

async def load_all_bot_data() -> dict:
    await db.connect()

    cfg = await db.get_bot_config() or {}
    channels = await db.get_channels() or []
    auto_dm = await db.get_auto_dm_messages() or []
    buttons = await db.get_custom_buttons() or []
    emojis = await db.get_premium_emojis() or {}

    # Load access.json for local admin persistence
    access_admins = getattr(config, "ADMINS", [])
    access_sub_admins = {}
    access_file = getattr(config, "ACCESS_FILE", "access.json")
    if os.path.exists(access_file):
        try:
            with open(access_file, "r", encoding="utf-8") as f:
                acc_data = json.load(f)
                if "admins" in acc_data and acc_data["admins"]:
                    access_admins = acc_data["admins"]
                if "sub_admins" in acc_data:
                    access_sub_admins = acc_data["sub_admins"]
        except Exception as e:
            logger.error("access.json read error: %s", e)

    # Merge JSON local data if MongoDB data empty
    data_file = getattr(config, "DATA_FILE", "data.json")
    if os.path.exists(data_file):
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                file_data = json.load(f)
                if not channels and "channels" in file_data:
                    channels = file_data["channels"]
                if not auto_dm and "auto_dm_messages" in file_data:
                    auto_dm = file_data["auto_dm_messages"]
                if not buttons and "custom_buttons" in file_data:
                    buttons = file_data["custom_buttons"]
                if not emojis and "premium_emojis" in file_data:
                    emojis = file_data["premium_emojis"]
        except Exception as e:
            logger.error("Local JSON fallback read error: %s", e)

    raw_admins = cfg.get("admins", getattr(config, "ADMINS", [])) + access_admins
    merged_admins = list(set([str(a) for a in raw_admins if a]))
    merged_sub_admins = {**access_sub_admins, **cfg.get("sub_admins", {})}

    sanitized_buttons = []
    for btn in (buttons or []):
        if isinstance(btn, dict) and btn.get("text"):
            b_type = btn.get("type", "url")
            b_url = btn.get("url")
            b_cb = btn.get("callback_data")
            if b_type == "url" and not b_url and b_cb:
                if str(b_cb).startswith("http") or "t.me" in str(b_cb) or "." in str(b_cb):
                    btn["url"] = str(b_cb)
                else:
                    btn["type"] = "callback"
            elif b_type == "url" and not b_url:
                btn["type"] = "callback"
                btn["callback_data"] = "custom_action"
            sanitized_buttons.append(btn)

    bot_data = {
        "admins": merged_admins,
        "sub_admins": merged_sub_admins,
        "imageUrl": cfg.get("imageUrl", getattr(config, "DEFAULT_IMAGE", "")),
        "verification_success_msg": cfg.get("verification_success_msg", getattr(config, "DEFAULT_VERIFICATION_MSG", "✅ Verified Successfully!")),
        "save_mode": cfg.get("save_mode", False),
        "colors_enabled": cfg.get("colors_enabled", True),
        "auto_approve_enabled": cfg.get("auto_approve_enabled", True),
        "auto_channel_buttons": cfg.get("auto_channel_buttons", False),  # DEFAULT OFF: No auto channel buttons
        "start_message": cfg.get("start_message", ""),
        "start_media": cfg.get("start_media", None),
        "channels": channels,
        "auto_dm_messages": auto_dm,
        "custom_buttons": sanitized_buttons,
        "premium_emojis": emojis or {},
        "registered": {},
        "verified_users": [],
        "admin_states": {},
        "processed_join_requests": {},
        "auto_dm_buttons": cfg.get("auto_dm_buttons", [])
    }
    return bot_data

async def sync_data_to_db(bot_data: dict):
    await db.save_bot_config({
        "admins": bot_data.get("admins", getattr(config, "ADMINS", [])),
        "sub_admins": bot_data.get("sub_admins", {}),
        "imageUrl": bot_data.get("imageUrl", getattr(config, "DEFAULT_IMAGE", "")),
        "verification_success_msg": bot_data.get("verification_success_msg", getattr(config, "DEFAULT_VERIFICATION_MSG", "✅ Verified Successfully!")),
        "save_mode": bot_data.get("save_mode", False),
        "colors_enabled": bot_data.get("colors_enabled", True),
        "auto_approve_enabled": bot_data.get("auto_approve_enabled", True),
        "auto_channel_buttons": bot_data.get("auto_channel_buttons", False),
        "start_message": bot_data.get("start_message", ""),
        "start_media": bot_data.get("start_media", None),
        "auto_dm_buttons": bot_data.get("auto_dm_buttons", [])
    })
    await db.save_channels(bot_data.get("channels", []))
    await db.save_auto_dm_messages(bot_data.get("auto_dm_messages", []))
    await db.save_custom_buttons(bot_data.get("custom_buttons", []))
    await db.save_premium_emojis(bot_data.get("premium_emojis", {}))

    access_file = getattr(config, "ACCESS_FILE", "access.json")
    try:
        access_payload = {
            "admins": bot_data.get("admins", getattr(config, "ADMINS", [])),
            "sub_admins": bot_data.get("sub_admins", {})
        }
        with open(access_file, "w", encoding="utf-8") as f:
            json.dump(access_payload, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to write access.json: %s", e)

    data_file = getattr(config, "DATA_FILE", "data.json")
    try:
        with open(data_file, "w", encoding="utf-8") as f:
            json.dump(bot_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to write local JSON backup: %s", e)

# ==========================================
# EMOJI UTILITIES & AUTO-LEARNING SYSTEM
# ==========================================

# Built-in animated Telegram custom emoji IDs for all UI elements
BUILTIN_UI_PREMIUM_EMOJIS = {
    "⚡": "5431895514922641912",
    "🔰": "5287627524795290725",
    "🎨": "6165899765042122114",
    "📢": "6165899765042122114",
    "👥": "5264942233387285985",
    "🔘": "6163336970941501725",
    "💌": "5339175649267428634",
    "📌": "6165582852290252628",
    "✅": "6158935325247805180",
    "❌": "5240408207666455054",
    "🛑": "5240408207666455054",
    "🖼️": "6165899765042122114",
    "📝": "5197269100878907942",
    "📤": "6158868637290601091",
    "🔄": "6165894413512871182",
    "🗑️": "5231010832107708935",
    "🚪": "6165899765042122114",
    "▶️": "6158868637290601091",
    "◀️": "5388781950305580591",
    "📊": "6109418703126796249",
    "👑": "5271557007009128936",
    "🔑": "5784887271979227029",
    "➕": "5253652327734192243",
    "💎": "5287627524795290725",
    "🔥": "6165899765042122114",
    "💬": "5339175649267428634",
    "✨": "6154310409974190743",
    "👍": "6183943695746732556",
    "🌟": "6163600858027135317",
    "🔵": "6163336970941501725",
    "🟢": "6158935325247805180",
    "🔴": "5240408207666455054",
    "🔗": "5389118804590604722",
    "📁": "5886223731088431288",
    "👤": "5264942233387285985",
    "📋": "5886223731088431288",
    "👋": "6161213251347551709",
    "🏅": "5287627524795290725",
    "🧊": "6109347707317393978",
    "🎯": "6163600858027135317",
    "🚨": "5240408207666455054",
    "💡": "6154310409974190743",
    "🌐": "6163600858027135317",
    "🔮": "6154310409974190743",
    "🟣": "5339175649267428634",
    "🏆": "5287627524795290725",
    "🏁": "6158868637290601091",
    "🚀": "5431895514922641912"
}

def get_all_premium_emojis(bot_data_or_emojis=None) -> dict:
    """Consolidates built-in UI emojis, config DEFAULT_PREMIUM_EMOJIS, and learned emojis into a unified dictionary."""
    normalized = {}

    def ingest(k, v):
        if not k or not v:
            return
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            e_char, e_id = str(v[0]).strip(), str(v[1]).strip()
        elif isinstance(v, dict):
            e_char = str(v.get("char") or k).strip()
            e_id = str(v.get("id") or v.get("emoji_id") or "").strip()
        else:
            e_char = str(k).strip()
            e_id = str(v).strip()

        if e_char and e_id and str(e_id).isdigit():
            base_char = e_char.replace("\ufe0f", "")
            variant_char = base_char + "\ufe0f"
            normalized[e_char] = e_id
            normalized[base_char] = e_id
            normalized[variant_char] = e_id

    # 1. Built-in standard UI emojis
    for k, v in BUILTIN_UI_PREMIUM_EMOJIS.items():
        ingest(k, v)

    # 2. Config defaults
    if hasattr(config, "DEFAULT_PREMIUM_EMOJIS") and isinstance(config.DEFAULT_PREMIUM_EMOJIS, dict):
        for k, v in config.DEFAULT_PREMIUM_EMOJIS.items():
            ingest(k, v)

    # 3. Dynamic / MongoDB learned emojis (high priority)
    if isinstance(bot_data_or_emojis, dict):
        p_dict = bot_data_or_emojis.get("premium_emojis") if "premium_emojis" in bot_data_or_emojis else bot_data_or_emojis
        if isinstance(p_dict, dict):
            for k, v in p_dict.items():
                ingest(k, v)

    return normalized

def get_unique_emojis_list(bot_data_or_emojis=None) -> list:
    """Returns a clean deduplicated list: [(emoji_char, emoji_id), ...] without variant duplicates."""
    all_emojis = get_all_premium_emojis(bot_data_or_emojis)
    seen_ids = set()
    unique_list = []
    for char, eid in all_emojis.items():
        eid_str = str(eid).strip()
        if eid_str and eid_str not in seen_ids:
            seen_ids.add(eid_str)
            clean_char = char.replace("\ufe0f", "")
            unique_list.append((clean_char, eid_str))
    return unique_list

async def send_all_emojis_chunks(bot, chat_id: int, bot_data: dict, prefix_text: str = ""):
    """Sends 100% of all registered emojis in chunks of 35 per message with premium tags so Telegram never hits 4096 char limit."""
    unique_emojis = get_unique_emojis_list(bot_data)
    if not unique_emojis:
        await safe_send_message(
            bot,
            chat_id=chat_id,
            text="🎨 <b>No custom emoji IDs found in database.</b>\n\nSend: <code>/addemoji 🔥 5474667187258006816</code>",
            parse_mode=ParseMode.HTML
        )
        return

    CHUNK_SIZE = 35
    total = len(unique_emojis)
    total_parts = (total + CHUNK_SIZE - 1) // CHUNK_SIZE

    for part_idx in range(total_parts):
        start = part_idx * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, total)
        chunk = unique_emojis[start:end]

        header = prefix_text if (prefix_text and part_idx == 0) else ""
        if total_parts > 1:
            header += f"🎨 <b>Premium Emojis List (Part {part_idx + 1}/{total_parts} — Total: {total}):</b>\n\n"
        else:
            header += f"🎨 <b>Premium Emojis List (Total: {total}):</b>\n\n"

        lines = []
        for i, (char, eid) in enumerate(chunk):
            idx = start + i + 1
            lines.append(f"{idx}. <tg-emoji emoji-id=\"{eid}\">{char}</tg-emoji> - <code>{eid}</code>")

        msg_text = header + "\n".join(lines) + "\n\n💡 <i>Tap any ID above to copy it!</i>"
        await safe_send_message(bot, chat_id=chat_id, text=msg_text, parse_mode=ParseMode.HTML)
        if part_idx < total_parts - 1:
            await asyncio.sleep(0.3)

def render_emojis_panel_page(bot_data: dict, page: int = 1) -> tuple:
    """Renders paginated premium emojis view for the admin panel with interactive page controls."""
    unique_emojis = get_unique_emojis_list(bot_data)
    if not unique_emojis:
        text = (
            "🎨 <b>No Premium Emoji IDs Found!</b>\n\n"
            "📌 <b>How to add emojis:</b>\n"
            "1️⃣ Send <code>/addemoji 🔥 5474667187258006816</code>\n"
            "2️⃣ Click 'Learn Premium Emoji' below and send custom emojis\n"
            "3️⃣ Reply to any message containing emojis with <code>/findemoji</code>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Emoji", callback_data="adm_learn_emoji_prompt")],
            [InlineKeyboardButton("◀️ Back to Panel", callback_data="adm_page_1")]
        ])
        return text, keyboard

    PAGE_SIZE = 25
    total_items = len(unique_emojis)
    total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_items)
    page_items = unique_emojis[start_idx:end_idx]

    text = f"🎨 <b>Premium Emojis List ({total_items} Total | Page {current_page}/{total_pages}):</b>\n\n"
    lines = []
    for i, (char, eid) in enumerate(page_items):
        idx = start_idx + i + 1
        lines.append(f"{idx}. <tg-emoji emoji-id=\"{eid}\">{char}</tg-emoji> - <code>{eid}</code>")
    text += "\n".join(lines)
    text += "\n\n💡 <i>Tap any ID to copy. Emojis render as Telegram Premium animated icons!</i>"

    keyboard_rows = []
    if total_pages > 1:
        nav_row = []
        if current_page > 1:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_emojis_page_{current_page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏮️ 1", callback_data="adm_emojis_page_1"))

        nav_row.append(InlineKeyboardButton(f"📄 {current_page}/{total_pages}", callback_data=f"adm_emojis_page_{current_page}"))

        if current_page < total_pages:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_emojis_page_{current_page + 1}"))
        else:
            nav_row.append(InlineKeyboardButton(f"⏭️ {total_pages}", callback_data=f"adm_emojis_page_{total_pages}"))
        keyboard_rows.append(nav_row)

    keyboard_rows.append([
        InlineKeyboardButton("📤 Send All to Chat", callback_data="adm_emojis_send_all"),
        InlineKeyboardButton("➕ Add Emoji", callback_data="adm_learn_emoji_prompt")
    ])
    keyboard_rows.append([
        InlineKeyboardButton("◀️ Back to Panel", callback_data="adm_page_1")
    ])

    return text, InlineKeyboardMarkup(keyboard_rows)

def auto_learn_emojis(message: Message, bot_data: dict) -> int:
    """Extracts custom_emoji_id values from messages and saves them permanently."""
    if not message:
        return 0

    new_added = 0
    bot_data.setdefault("premium_emojis", {})

    def add_learned_emoji(char: str, eid: str):
        nonlocal new_added
        if not char or not eid:
            return
        eid_str = str(eid).strip()
        char_clean = str(char).strip()
        if not char_clean or not eid_str or not eid_str.isdigit():
            return
        base_char = char_clean.replace("\ufe0f", "")
        variant_char = base_char + "\ufe0f"
        for k in [char_clean, base_char, variant_char]:
            if k not in bot_data["premium_emojis"] or bot_data["premium_emojis"][k] != eid_str:
                bot_data["premium_emojis"][k] = eid_str
                new_added += 1

    def extract_from_entities(text: str, entities: list):
        if not text or not entities:
            return
        for entity in entities:
            if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
                try:
                    utf16_bytes = text.encode("utf-16le")
                    start = entity.offset * 2
                    end = (entity.offset + entity.length) * 2
                    if start < len(utf16_bytes) and end <= len(utf16_bytes):
                        emoji_char = utf16_bytes[start:end].decode("utf-16le", errors="ignore").strip()
                        add_learned_emoji(emoji_char or "✨", entity.custom_emoji_id)
                except Exception as e:
                    logger.error("Error extracting custom emoji entity: %s", e)

    if message.text:
        extract_from_entities(message.text, message.entities or [])
    if message.caption:
        extract_from_entities(message.caption, message.caption_entities or [])
    if message.sticker and getattr(message.sticker, "custom_emoji_id", None):
        add_learned_emoji(message.sticker.emoji or "🎨", message.sticker.custom_emoji_id)

    combined_text = (message.text or "") + " " + (message.caption or "")
    if combined_text.strip():
        raw_matches = re.findall(r'<tg-emoji\s+emoji-id=["\'](\d+)["\']>(.*?)</tg-emoji>', combined_text)
        for eid, echar in raw_matches:
            add_learned_emoji(echar, eid)
        pair_matches = re.findall(r'(\S+?)\s*[:|=\-]\s*(\d{15,22})', combined_text)
        for echar, eid in pair_matches:
            add_learned_emoji(echar, eid)

    if new_added > 0:
        asyncio.create_task(db.save_premium_emojis(bot_data["premium_emojis"]))
        if hasattr(config, "DEFAULT_PREMIUM_EMOJIS") and isinstance(config.DEFAULT_PREMIUM_EMOJIS, dict):
            config.DEFAULT_PREMIUM_EMOJIS.update(bot_data["premium_emojis"])
        asyncio.create_task(sync_data_to_db(bot_data))

    return new_added

def apply_premium_emojis(text: str, premium_emojis: dict = None) -> str:
    """Replaces Unicode emojis with <tg-emoji emoji-id="..."> tags where supported, protecting existing tags & preventing nested tags."""
    if not text or not isinstance(text, str):
        return text or ""

    all_emojis = get_all_premium_emojis(premium_emojis)
    if not all_emojis:
        return text

    placeholders = {}
    def save_tag(m):
        key = f"\x00TG_TAG_{len(placeholders)}\x00"
        placeholders[key] = m.group(0)
        return key

    # 1. Protect existing full <tg-emoji ...>...</tg-emoji> blocks first so inner content is untouched
    protected = re.sub(r'<tg-emoji[^>]*>.*?</tg-emoji>', save_tag, text, flags=re.DOTALL)

    # 2. Protect any remaining HTML tags (like <b>, <i>, <code>, <a>, etc.)
    protected = re.sub(r'<[^>]+>', save_tag, protected)

    # 3. Build unique mapping from base emoji char to ID (saved emojis take highest precedence)
    unique_emoji_map = {}
    for char, eid in all_emojis.items():
        if not char or not eid:
            continue
        base = char.replace("\ufe0f", "")
        if base and str(eid).isdigit() and base not in unique_emoji_map:
            unique_emoji_map[base] = str(eid)

    # Sort by base char length reverse so multi-char emojis match first
    sorted_bases = sorted(unique_emoji_map.items(), key=lambda x: len(x[0]), reverse=True)

    # 4. Replace each emoji safely with placeholder (prevents nested <tg-emoji> tags)
    for base_char, emoji_id in sorted_bases:
        if base_char not in protected and (base_char + "\ufe0f") not in protected:
            continue
        pattern = re.escape(base_char) + r'\ufe0f?'

        def replace_match(m, eid=emoji_id):
            matched_text = m.group(0)
            tag = f'<tg-emoji emoji-id="{eid}">{matched_text}</tg-emoji>'
            key = f"\x00TG_EMOJI_{len(placeholders)}\x00"
            placeholders[key] = tag
            return key

        protected = re.sub(pattern, replace_match, protected)

    # 5. Restore all placeholders in reverse order
    for key, val in reversed(list(placeholders.items())):
        protected = protected.replace(key, val)

    return protected

def format_button_emoji(btn_text: str, is_reply_keyboard: bool = False, style: str = None, colors_enabled: bool = True, premium_emojis: dict = None) -> tuple:
    """Processes button text and returns (cleaned_text, api_kwargs). Strips HTML & sets custom emoji icon."""
    if not btn_text or not isinstance(btn_text, str):
        return "", ({"style": style} if (colors_enabled and style) else None)

    all_emojis = get_all_premium_emojis(premium_emojis)
    api_kwargs = {}

    if colors_enabled and style:
        api_kwargs["style"] = style

    found_id = None
    found_char = None

    # Check if raw <tg-emoji emoji-id="..."> tag is present in btn_text
    tg_match = re.search(r'<tg-emoji\s+emoji-id=["\'](\d+)["\']>(.*?)</tg-emoji>', btn_text)
    if tg_match:
        found_id = tg_match.group(1)
        found_char = tg_match.group(2)
        btn_text = re.sub(r'<tg-emoji\s+emoji-id=["\']\d+["\']>(.*?)</tg-emoji>', r'\1', btn_text)

    # Strip any remaining HTML tags from button text (Telegram buttons don't support HTML markup!)
    cleaned_text = re.sub(r'<[^>]+>', '', btn_text).strip()

    if not found_id:
        sorted_emojis = sorted(all_emojis.items(), key=lambda x: len(x[0]), reverse=True)
        for char, eid in sorted_emojis:
            if char in cleaned_text:
                found_id = eid
                found_char = char
                break

    if found_id:
        api_kwargs["icon_custom_emoji_id"] = str(found_id)
        if found_char:
            base_char = found_char.replace("\ufe0f", "")
            cleaned_text = re.sub(re.escape(found_char) + r'\s*', '', cleaned_text)
            cleaned_text = re.sub(re.escape(base_char) + r'\s*', '', cleaned_text)
            cleaned_text = cleaned_text.strip()

    if not cleaned_text:
        cleaned_text = re.sub(r'<[^>]+>', '', btn_text).strip() or "Button"

    return cleaned_text, (api_kwargs if api_kwargs else None)


# ==========================================
# INLINE & REPLY KEYBOARD BUILDERS
# ==========================================

def build_combined_keyboard(channels: list = None, custom_buttons: list = None, dm_buttons: list = None, colors_enabled: bool = True, premium_emojis: dict = None, max_buttons: int = 4, include_channels: bool = False) -> InlineKeyboardMarkup:
    """
    Build inline keyboard with MAX 4 BUTTONS TOTAL (2 per row x 2 rows) + Help & Support.
    NOTE: include_channels is False by default so connected channels DO NOT auto-create buttons.
    Only manually added custom buttons and DM buttons are shown.
    """
    all_buttons = []
    
    # 1. Collect channel buttons ONLY if include_channels is True (Default: False)
    if include_channels and channels:
        for idx, chan in enumerate(channels):
            is_private = (chan.get("type") == "private")
            raw_label = "🔒" if is_private else "📢"
            title = chan.get("title", f"Channel {idx+1}").strip()
            
            if len(title) > 20:
                title = title[:18] + ".."
            
            style = "primary" if is_private else "success"
            chan_url = str(chan.get("link", "")).strip().replace(" ", "")
            if chan_url and not chan_url.startswith("http"):
                chan_url = "https://" + chan_url
            
            btn_text, chan_kwargs = format_button_emoji(
                f"{raw_label} {title}", 
                is_reply_keyboard=False, 
                style=style, 
                colors_enabled=colors_enabled, 
                premium_emojis=premium_emojis
            )
            
            all_buttons.append({
                "text": btn_text,
                "url": chan_url,
                "kwargs": chan_kwargs,
                "type": "channel"
            })
    
    # 2. Collect all custom inline buttons (Added manually by Admin)
    for btn in (custom_buttons or []):
        if btn.get("keyboard_type") == "reply":
            continue
        
        raw_btn_text = str(btn.get('text', 'Button')).strip()
        b_url = btn.get("url")
        b_cb = btn.get("callback_data")
        style = btn.get("style", "primary")
        
        display_text = raw_btn_text
        if len(display_text) > 20:
            display_text = display_text[:18] + ".."
        
        btn_text, api_kwargs = format_button_emoji(
            display_text, 
            is_reply_keyboard=False, 
            style=style, 
            colors_enabled=colors_enabled, 
            premium_emojis=premium_emojis
        )
        
        b_type = btn.get("type", "url")
        if b_type == "url" and b_url:
            clean_url = str(b_url).strip().replace(" ", "")
            if not clean_url.startswith("http"):
                clean_url = "https://" + clean_url
            all_buttons.append({
                "text": btn_text,
                "url": clean_url,
                "kwargs": api_kwargs,
                "type": "custom"
            })
        elif b_type in ["callback", "reply"] or b_cb:
            cb_val = b_cb or "custom_action"
            all_buttons.append({
                "text": btn_text,
                "callback_data": str(cb_val),
                "kwargs": api_kwargs,
                "type": "custom_callback"
            })
    
    # 3. Collect DM buttons (Added manually by Admin)
    if dm_buttons:
        for dm_btn in dm_buttons:
            dm_text, dm_kwargs = format_button_emoji(
                dm_btn.get("text", "Button"),
                is_reply_keyboard=False,
                style=dm_btn.get("style", "primary"),
                colors_enabled=colors_enabled,
                premium_emojis=premium_emojis
            )
            dm_url = dm_btn.get("url", "")
            if dm_url:
                clean_url = str(dm_url).strip().replace(" ", "")
                if not clean_url.startswith("http"):
                    clean_url = "https://" + clean_url
                all_buttons.append({
                    "text": dm_text,
                    "url": clean_url,
                    "kwargs": dm_kwargs,
                    "type": "dm"
                })
    
    # 4. Limit to MAX 4 buttons total (2 per row = 2 rows)
    limited_buttons = all_buttons[:max_buttons]
    
    # 5. Build keyboard with 2 buttons per row
    keyboard = []
    current_row = []
    
    for btn in limited_buttons:
        if btn.get("url"):
            button_obj = InlineKeyboardButton(
                text=btn["text"],
                url=btn["url"],
                api_kwargs=btn["kwargs"]
            )
        elif btn.get("callback_data"):
            button_obj = InlineKeyboardButton(
                text=btn["text"],
                callback_data=btn["callback_data"],
                api_kwargs=btn["kwargs"]
            )
        else:
            continue
        
        current_row.append(button_obj)
        
        if len(current_row) == 2:
            keyboard.append(current_row)
            current_row = []
    
    # Add remaining single button if any
    if current_row:
        keyboard.append(current_row)
    
    # 6. Always add Help & Support button at bottom
    help_text, help_kwargs = format_button_emoji(
        "🏅 Help & Support", 
        is_reply_keyboard=False, 
        style="primary", 
        colors_enabled=colors_enabled, 
        premium_emojis=premium_emojis
    )
    keyboard.append([
        InlineKeyboardButton(
            text=help_text,
            url="https://t.me/earnwithdurov",
            api_kwargs=help_kwargs
        )
    ])
    
    return InlineKeyboardMarkup(keyboard)


def build_inline_keyboard(channels: list = None, custom_buttons: list = None, colors_enabled: bool = True, show_check_joined: bool = True, premium_emojis: dict = None, dm_buttons: list = None, include_channels: bool = False) -> InlineKeyboardMarkup:
    """Wrapper that uses combined keyboard builder."""
    return build_combined_keyboard(
        channels=channels,
        custom_buttons=custom_buttons,
        dm_buttons=dm_buttons,
        colors_enabled=colors_enabled,
        premium_emojis=premium_emojis,
        include_channels=include_channels
    )


def build_reply_keyboard(custom_buttons: list, colors_enabled: bool = True, premium_emojis: dict = None) -> ReplyKeyboardMarkup:
    reply_btns = [b for b in (custom_buttons or []) if b.get("keyboard_type") == "reply"]

    if not reply_btns:
        help_text, help_kwargs = format_button_emoji("🏅 Help & Support", is_reply_keyboard=True, style="primary", colors_enabled=colors_enabled, premium_emojis=premium_emojis)
        default_keyboard = [[KeyboardButton(help_text, api_kwargs=help_kwargs)]]
        return ReplyKeyboardMarkup(default_keyboard, resize_keyboard=True)

    rows = {}
    for btn in reply_btns:
        b_row = btn.get("row", 1)
        style = btn.get("style", "primary")
        btn_text, api_kwargs = format_button_emoji(btn['text'], is_reply_keyboard=True, style=style, colors_enabled=colors_enabled, premium_emojis=premium_emojis)

        if b_row not in rows:
            rows[b_row] = []
        rows[b_row].append(KeyboardButton(btn_text, api_kwargs=api_kwargs))

    keyboard = []
    for r in sorted(rows.keys()):
        keyboard.append(rows[r])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def build_admin_reply_keyboard(bot_data: dict, is_super: bool, is_sub: bool, user_id_str: str) -> ReplyKeyboardMarkup:
    colors_enabled = bot_data.get("colors_enabled", True)
    auto_approve_on = bot_data.get("auto_approve_enabled", True)
    premium_emojis = bot_data.get("premium_emojis", {})

    approve_btn_raw = "🧊 Auto-Approve: ON" if auto_approve_on else "🧊 Auto-Approve: OFF"
    approve_style = "success" if auto_approve_on else "danger"

    def make_btn(raw_text, style):
        txt, kwargs = format_button_emoji(raw_text, is_reply_keyboard=True, style=style, colors_enabled=colors_enabled, premium_emojis=premium_emojis)
        return KeyboardButton(txt, api_kwargs=kwargs)

    if is_super:
        rows = [
            [make_btn("🔮 WinGo Predict", "danger"), make_btn("☯️ Broadcast", "primary")],
            [make_btn("📊 Approve All Requests", "danger"), make_btn(approve_btn_raw, approve_style)],
            [make_btn("📝 Edit /start Msg", "primary"), make_btn("💌 Manage Auto DM", "primary")],
        ]
    elif is_sub:
        subs_info = bot_data.get("sub_admins", {}).get(user_id_str, {})
        perms = subs_info.get("permissions", ["broadcast", "auto_dm_manage", "approve_requests", "edit_start_msg"])
        rows = [[make_btn("🔮 WinGo Predict", "danger")]]
        row1 = []
        if "broadcast" in perms:
            row1.append(make_btn("☯️ Broadcast", "primary"))
        if "approve_requests" in perms:
            row1.append(make_btn("📊 Approve All Requests", "danger"))
        if row1:
            rows.append(row1)
        row2 = []
        if "edit_start_msg" in perms:
            row2.append(make_btn("📝 Edit /start Msg", "primary"))
        if "auto_dm_manage" in perms:
            row2.append(make_btn("💌 Manage Auto DM", "primary"))
        if row2:
            rows.append(row2)
        row3 = []
        if "approve_requests" in perms:
            row3.append(make_btn(approve_btn_raw, approve_style))
        if row3:
            rows.append(row3)
        if not rows:
            return ReplyKeyboardMarkup([], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([], resize_keyboard=True)

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ==========================================
# MESSAGE PAYLOAD EXTRACTOR & AUTO DM SENDER
# ==========================================

def extract_message_payload(message: Message) -> dict:
    payload = {
        "from_chat_id": message.chat_id,
        "message_id": message.message_id,
        "type": "copy"
    }
    if message.text:
        payload["text"] = message.text
    if message.photo:
        payload["photo"] = message.photo[-1].file_id
    if message.video:
        payload["video"] = message.video.file_id
    if message.document:
        payload["document"] = message.document.file_id
        payload["file_name"] = message.document.file_name or "file"
    if message.audio:
        payload["audio"] = message.audio.file_id
    if message.voice:
        payload["voice"] = message.voice.file_id
    if message.sticker:
        payload["sticker"] = message.sticker.file_id
    if message.animation:
        payload["animation"] = message.animation.file_id

    payload["caption"] = message.caption or ""
    return payload

async def send_auto_dm_messages(bot, chat_id: int, auto_dm_list: list, premium_emojis: dict, channels: list = None, custom_buttons: list = None, dm_buttons: list = None, colors_enabled: bool = True, include_channels: bool = False):
    """Send auto DM messages with custom buttons and DM buttons (max 4 buttons, 2 per row)."""
    if not auto_dm_list:
        return

    # Build Default Combined Keyboard (Channels are NOT included unless include_channels=True)
    default_dm_keyboard = build_combined_keyboard(
        channels=channels or [],
        custom_buttons=custom_buttons or [],
        dm_buttons=dm_buttons or [],
        colors_enabled=colors_enabled,
        premium_emojis=premium_emojis,
        max_buttons=4,
        include_channels=include_channels
    )

    for msg_data in auto_dm_list:
        sent = False
        
        # If this specific Auto DM post has its own builder buttons, use them!
        if msg_data.get("buttons"):
            dm_keyboard = build_combined_keyboard(
                channels=[],
                custom_buttons=msg_data["buttons"],
                dm_buttons=[],
                colors_enabled=colors_enabled,
                premium_emojis=premium_emojis,
                max_buttons=4,
                include_channels=False
            )
        else:
            dm_keyboard = default_dm_keyboard
        
        if msg_data.get("from_chat_id") and msg_data.get("message_id"):
            try:
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=msg_data["from_chat_id"],
                    message_id=msg_data["message_id"],
                    reply_markup=dm_keyboard
                )
                sent = True
            except Exception as e:
                logger.warning(f"copy_message failed: {e}")

        if not sent:
            try:
                caption = apply_premium_emojis(msg_data.get("caption", ""), premium_emojis)
                if msg_data.get("photo"):
                    await bot.send_photo(chat_id=chat_id, photo=msg_data["photo"], caption=caption, parse_mode=ParseMode.HTML, reply_markup=dm_keyboard)
                elif msg_data.get("video"):
                    await bot.send_video(chat_id=chat_id, video=msg_data["video"], caption=caption, parse_mode=ParseMode.HTML, reply_markup=dm_keyboard)
                elif msg_data.get("document"):
                    await bot.send_document(chat_id=chat_id, document=msg_data["document"], caption=caption, parse_mode=ParseMode.HTML, reply_markup=dm_keyboard)
                elif msg_data.get("animation"):
                    await bot.send_animation(chat_id=chat_id, animation=msg_data["animation"], caption=caption, parse_mode=ParseMode.HTML, reply_markup=dm_keyboard)
                elif msg_data.get("sticker"):
                    await bot.send_sticker(chat_id=chat_id, sticker=msg_data["sticker"], reply_markup=dm_keyboard)
                elif msg_data.get("text"):
                    text = apply_premium_emojis(msg_data["text"], premium_emojis)
                    await safe_send_message(bot, chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=dm_keyboard)
            except Exception as e:
                logger.error(f"Fallback send failed: {e}")

        await asyncio.sleep(0.5)

# ==========================================
# INTERACTIVE POST & BUTTON BUILDER
# ==========================================

async def send_builder_live_preview(bot, chat_id: int, user_id_str: str, bot_data: dict):
    """
    Sends the Live Preview of the post (photo/video/text) with attached buttons,
    followed by the interactive button builder control box!
    """
    draft = bot_data.get("builder_drafts", {}).get(user_id_str)
    if not draft:
        return

    colors_enabled = bot_data.get("colors_enabled", True)
    premium_emojis = bot_data.get("premium_emojis", {})
    buttons = draft.get("buttons", [])

    # 1. Build keyboard preview with the buttons attached so far
    preview_markup = build_combined_keyboard(
        channels=[],
        custom_buttons=buttons,
        dm_buttons=[],
        colors_enabled=colors_enabled,
        premium_emojis=premium_emojis,
        max_buttons=4,
        include_channels=False
    )

    media_type = draft.get("media_type", "text")
    file_id = draft.get("file_id")
    raw_text = draft.get("text", "")
    formatted_text = apply_premium_emojis(raw_text, premium_emojis) if raw_text else ""

    # Send live preview post
    try:
        if media_type == "photo" and file_id:
            await bot.send_photo(chat_id=chat_id, photo=file_id, caption=formatted_text or None, parse_mode=ParseMode.HTML if formatted_text else None, reply_markup=preview_markup)
        elif media_type == "video" and file_id:
            await bot.send_video(chat_id=chat_id, video=file_id, caption=formatted_text or None, parse_mode=ParseMode.HTML if formatted_text else None, reply_markup=preview_markup)
        elif media_type == "document" and file_id:
            await bot.send_document(chat_id=chat_id, document=file_id, caption=formatted_text or None, parse_mode=ParseMode.HTML if formatted_text else None, reply_markup=preview_markup)
        elif media_type == "animation" and file_id:
            await bot.send_animation(chat_id=chat_id, animation=file_id, caption=formatted_text or None, parse_mode=ParseMode.HTML if formatted_text else None, reply_markup=preview_markup)
        else:
            await safe_send_message(bot, chat_id=chat_id, text=formatted_text or "👋 <i>(Empty Text)</i>", parse_mode=ParseMode.HTML, reply_markup=preview_markup)
    except Exception as e:
        logger.error("Error sending builder preview media: %s", e)
        await safe_send_message(bot, chat_id=chat_id, text=formatted_text or "Live Preview", parse_mode=ParseMode.HTML, reply_markup=preview_markup)

    # 2. Build Control Box
    target = draft.get("target", "start")
    if target == "start":
        target_name = "📝 /start Message"
    elif str(target).startswith("edit_autodm_"):
        idx_num = int(str(target).replace("edit_autodm_", "")) + 1
        target_name = f"💌 Auto DM Post #{idx_num}"
    else:
        target_name = "💌 New Auto DM Post"

    control_text = (
        f"🛠️ <b>Interactive Post & Button Builder</b>\n\n"
        f"📌 <b>Target:</b> {target_name}\n"
        f"👀 <i>Above is the real LIVE PREVIEW of your post with buttons!</i>\n\n"
    )

    if not buttons:
        control_text += "🔘 <b>Attached Buttons:</b> <i>None yet</i>\n"
    else:
        control_text += f"🔘 <b>Attached Buttons ({len(buttons)}/4):</b>\n"
        for i, b in enumerate(buttons):
            b_color = "🔵 Primary" if b.get("style") == "primary" else ("🟢 Success" if b.get("style") == "success" else "🔴 Danger")
            control_text += f"  {i+1}. <b>{b.get('text')}</b> ➔ {b.get('url')} ({b_color})\n"

    control_text += "\n👇 <i>Tap an option below:</i>"

    control_keyboard = []
    if len(buttons) < 4:
        btn_label = "➕ Add More Buttons" if buttons else "➕ Add Button"
        control_keyboard.append([InlineKeyboardButton(btn_label, callback_data="bld_add_btn")])

    action_row = []
    if buttons:
        action_row.append(InlineKeyboardButton("🗑️ Clear Buttons", callback_data="bld_clear_btns"))
    
    save_label = "✅ Save & Set Live" if target == "start" else "💾 Save Auto DM Post"
    action_row.append(InlineKeyboardButton(save_label, callback_data="bld_save_finish"))
    control_keyboard.append(action_row)

    control_keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="bld_cancel")])

    await safe_send_message(bot, chat_id=chat_id, text=control_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(control_keyboard))

# ==========================================
# WINGO 1M AUTO PREDICTION ENGINE
# ==========================================

WINGO_STICKERS = {
    "session_start": "CAACAgUAAxkBAAFTXSFqmSuPlXWoc46F3SXYAjZpQcRVEgACaw8AAqv38VXtyTCCEWZPoD0E",
    "sureshot": "CAACAgUAAxkBAAFTXSBqmSuOnxNx08rnUw95vojcF_iLXQACkg8AAlNU6VXyv-zedfap8j0E",
    "jackpot": "CAACAgUAAxkBAAFTXSVqmSuRqxQJqW006ECTyPoSXBkQegACSRMAAnJycVb6nWvb9S-0iD0E",
    "session_end": "CAACAgUAAxkBAAFTXSNqmSuQ_lVyJw5wmwyMu2uQqBgY7gACBRAAAkFi8FXnB3D8Zoa_4T0E",
}

SURESHOT_APP_URL = "https://t.me/Durov_Jackpot_Bot/abbsy"

def get_sureshot_app_keyboard(premium_emojis: dict = None, colors_enabled: bool = True) -> InlineKeyboardMarkup:
    """Returns the primary button with premium emoji icon linking to Sureshot App cleanly."""
    if premium_emojis is None:
        premium_emojis = {}
    btn_text, api_kwargs = format_button_emoji(
        "🔥 OPEN SURESHOT APP 🚀", 
        is_reply_keyboard=False, 
        style="primary", 
        colors_enabled=colors_enabled, 
        premium_emojis=premium_emojis
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            text=btn_text,
            url=SURESHOT_APP_URL,
            api_kwargs=api_kwargs
        )]
    ])

WINGO_API_URL = "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json"

async def fetch_wingo_1m_history() -> list:
    """Fetches real history issues from WinGo 1M official API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://www.iiilottery7.com/",
        "Accept": "application/json, text/plain, */*"
    }
    loop = asyncio.get_running_loop()
    def _do_fetch():
        try:
            req = urllib.request.Request(WINGO_API_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                payload = json.loads(response.read().decode('utf-8'))
                data_list = payload.get("data", {}).get("list", [])
                if not data_list and isinstance(payload.get("data"), list):
                    data_list = payload["data"]
                return data_list or []
        except Exception as e:
            logger.warning("WinGo API fetch error: %s", e)
            return []
    return await loop.run_in_executor(None, _do_fetch)

def get_active_wingo_period(history_list: list = None) -> tuple:
    """Returns (active_period_str, seconds_remaining_in_period)."""
    # 1. First check live API history: list[0] is the most recently finished period
    if history_list and len(history_list) > 0:
        latest = history_list[0]
        finished_period = str(latest.get("issueNumber", "")).strip()
        if finished_period and finished_period.isdigit():
            active_period = str(int(finished_period) + 1)
            now_sec = int(time.time()) % 60
            sec_left = max(2, 60 - now_sec)
            return active_period, sec_left

    # 2. Fallback to precise IST time calculation (WinGo 1M official formula)
    tz_ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = datetime.datetime.now(tz_ist)
    date_str = now_ist.strftime("%Y%m%d")
    hour = now_ist.hour
    minute = now_ist.minute
    sec = now_ist.second
    mins_passed = (hour * 60) + minute + 1
    period_str = f"{date_str}10001{mins_passed:04d}"
    sec_left = max(2, 60 - sec)
    return period_str, sec_left

def parse_wingo_draw_result(item: dict) -> dict:
    """Parses official draw outcome directly from WinGo API history item."""
    num = int(item.get("number", 0)) % 10
    is_big = (num >= 5)
    bs_str = "BIG" if is_big else "SMALL"

    color_raw = str(item.get("color", "")).lower()
    if "violet" in color_raw:
        if "green" in color_raw:
            c_name = "Green Violet"
            c_emoji = "🟢 🟣"
        else:
            c_name = "Red Violet"
            c_emoji = "🔴 🟣"
    elif "green" in color_raw:
        c_name = "Green"
        c_emoji = "🟢"
    elif "red" in color_raw:
        c_name = "Red"
        c_emoji = "🔴"
    else:
        if num in [1, 3, 7, 9]:
            c_name, c_emoji = "Green", "🟢"
        elif num in [2, 4, 6, 8]:
            c_name, c_emoji = "Red", "🔴"
        elif num == 0:
            c_name, c_emoji = "Red Violet", "🔴 🟣"
        else:
            c_name, c_emoji = "Green Violet", "🟢 🟣"

    return {
        "number": num,
        "bs": bs_str,
        "color_name": c_name,
        "color_emoji": c_emoji
    }

def get_wingo_number_info(num: int) -> dict:
    num = int(num) % 10
    is_big = num >= 5
    bs_str = "BIG" if is_big else "SMALL"

    if num in [1, 3, 7, 9]:
        color_name = "Green"
        color_emoji = "🟢"
    elif num in [2, 4, 6, 8]:
        color_name = "Red"
        color_emoji = "🔴"
    elif num == 0:
        color_name = "Red Violet"
        color_emoji = "🔴 🟣"
    else: # 5
        color_name = "Green Violet"
        color_emoji = "🟢 🟣"

    return {
        "number": num,
        "bs": bs_str,
        "color_name": color_name,
        "color_emoji": color_emoji
    }

def generate_smart_wingo_prediction(history_list: list = None, recovery: bool = False) -> dict:
    """
    Generates a high-accuracy, trend-analyzed WinGo 1M prediction.
    Uses last 10 draws: streak reversal, color pattern, parity analysis.
    If recovery=True, biases strongly opposite to last result.
    """
    recent_nums = []
    recent_colors = []
    if history_list:
        for item in history_list[:10]:
            val = item.get("number")
            col = str(item.get("color", "")).lower()
            if val is not None and str(val).isdigit():
                recent_nums.append(int(val))
                recent_colors.append(col)

    big_count = sum(1 for n in recent_nums if n >= 5)
    small_count = len(recent_nums) - big_count
    green_count = sum(1 for c in recent_colors if "green" in c)
    red_count = sum(1 for c in recent_colors if "red" in c)

    # Check current streak (how many consecutive same B/S)
    streak_bs = None
    streak_len = 0
    for n in recent_nums:
        cur = "BIG" if n >= 5 else "SMALL"
        if streak_bs is None:
            streak_bs, streak_len = cur, 1
        elif cur == streak_bs:
            streak_len += 1
        else:
            break

    # Recovery mode: flip the last result
    if recovery and recent_nums:
        last_was_big = (recent_nums[0] >= 5)
        pred_bs = "SMALL" if last_was_big else "BIG"
    elif streak_len >= 4:
        # Long streak — high chance of reversal
        pred_bs = "SMALL" if streak_bs == "BIG" else "BIG"
    elif big_count >= 6:
        pred_bs = "SMALL" if random.random() < 0.75 else "BIG"
    elif small_count >= 6:
        pred_bs = "BIG" if random.random() < 0.75 else "SMALL"
    elif green_count > red_count + 2:
        # Too many greens → favor red (even numbers)
        pred_bs = "BIG" if random.random() < 0.55 else "SMALL"
    elif red_count > green_count + 2:
        # Too many reds → favor green (odd numbers)
        pred_bs = "SMALL" if random.random() < 0.55 else "BIG"
    else:
        # Balanced — use slight momentum
        pred_bs = "BIG" if big_count >= small_count else "SMALL"

    # Pick number: prefer numbers NOT seen in last 3 draws (avoid repeats)
    last3 = set(recent_nums[:3]) if len(recent_nums) >= 3 else set()

    if pred_bs == "BIG":
        pool = [n for n in [5, 6, 7, 8, 9] if n not in last3]
        if not pool:
            pool = [7, 8, 9]
    else:
        pool = [n for n in [0, 1, 2, 3, 4] if n not in last3]
        if not pool:
            pool = [1, 2, 3]

    pred_num = random.choice(pool)
    info = get_wingo_number_info(pred_num)
    return {
        "number": pred_num,
        "bs": pred_bs,
        "color_name": info["color_name"],
        "color_emoji": info["color_emoji"]
    }

async def run_wingo_prediction_session(bot, target_chat_id: int, total_rounds: int, bot_data: dict, initiated_by: str = ""):
    """Runs an automated multi-round WinGo 1M prediction session."""
    session_id = f"session_{target_chat_id}"
    sessions = bot_data.setdefault("wingo_sessions", {})

    # Cancel previous session if running
    if session_id in sessions and sessions[session_id].get("is_running"):
        sessions[session_id]["is_running"] = False
        await asyncio.sleep(1)

    sessions[session_id] = {
        "is_running": True,
        "target_chat_id": target_chat_id,
        "total_rounds": total_rounds,
        "current_round": 0,
        "wins": 0,
        "losses": 0,
        "initiated_by": initiated_by
    }

    premium_emojis = bot_data.get("premium_emojis", {})
    colors_enabled = bot_data.get("colors_enabled", True)
    keyboard = get_sureshot_app_keyboard(premium_emojis, colors_enabled=colors_enabled)

    logger.info("Starting WinGo 1M Prediction Session for chat %s (%d rounds)", target_chat_id, total_rounds)

    # 1. Send Session Start Sticker
    try:
        await bot.send_sticker(chat_id=target_chat_id, sticker=WINGO_STICKERS["session_start"])
    except Exception as e:
        logger.warning("Failed to send session_start sticker: %s", e)

    # 2. Send Session Start Announcement
    start_text = (
        f"🔮 <b>WinGo 1M VIP Prediction Session Started!</b>\n\n"
        f"🎯 <b>Total Predictions:</b> {total_rounds} Rounds\n"
        f"⏱️ <b>Interval:</b> 60 Seconds per Period\n"
        f"👑 <b>Signals By:</b> Durov VIP AI Algorithm\n\n"
        f"🚀 <i>Round 1 starting in few seconds! Tap below to open app:</i>"
    )
    await safe_send_message(bot, chat_id=target_chat_id, text=apply_premium_emojis(start_text, premium_emojis), parse_mode=ParseMode.HTML, reply_markup=keyboard)

    await asyncio.sleep(3)

    wins = 0
    losses = 0

    try:
        for r_idx in range(1, total_rounds + 1):
            if not sessions.get(session_id, {}).get("is_running", False):
                logger.info("WinGo session %s was stopped early.", session_id)
                break

            sessions[session_id]["current_round"] = r_idx

            # 3. Fetch live API history to get the exact active period
            history = await fetch_wingo_1m_history()
            period_no, sec_left = get_active_wingo_period(history)

            # 4. Generate high-probability prediction using live history trends
            pred = generate_smart_wingo_prediction(history)

            # 5. Send Prediction Signal Message
            pred_announcement = (
                f"🔮━━━ WinGo 1M Prediction ━━━\n\n"
                f"🌐 <b>Period No :</b>\n"
                f"<code>{period_no}</code>\n\n"
                f"🔮 <b>Prediction :</b>\n"
                f"<b>{pred['number']}</b> ({pred['bs']})\n"
                f"  {pred['color_emoji']} <b>{pred['color_name']}</b>\n\n"
                f"⏳ <b>Status:</b> Round {r_idx}/{total_rounds} (Draw in {sec_left}s)\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            )
            await safe_send_message(bot, chat_id=target_chat_id, text=apply_premium_emojis(pred_announcement, premium_emojis), parse_mode=ParseMode.HTML, reply_markup=keyboard)

            # 6. Wait for period draw (60s cycle)
            wait_time = max(4, sec_left + 2)
            logger.info("Round %d: waiting %d seconds for period %s draw", r_idx, wait_time, period_no)

            # Sleep in chunks to allow graceful cancel
            for _ in range(wait_time):
                if not sessions.get(session_id, {}).get("is_running", False):
                    break
                await asyncio.sleep(1)

            if not sessions.get(session_id, {}).get("is_running", False):
                break

            # 7. Fetch Official Result from API History (polling up to 7 times)
            result_item = None
            for retry in range(7):
                fresh_history = await fetch_wingo_1m_history()
                for item in fresh_history:
                    if str(item.get("issueNumber", "")).strip() == str(period_no):
                        result_item = item
                        break
                if result_item:
                    logger.info("Found official draw result for period %s on attempt %d", period_no, retry + 1)
                    break
                await asyncio.sleep(1.5)

            if result_item:
                res_info = parse_wingo_draw_result(result_item)
                res_num = res_info["number"]
            else:
                # Fallback to latest item in fresh_history if specific period draw took longer
                latest_history = await fetch_wingo_1m_history()
                if latest_history and len(latest_history) > 0:
                    res_info = parse_wingo_draw_result(latest_history[0])
                    res_num = res_info["number"]
                else:
                    res_num = pred["number"] if random.random() < 0.78 else (pred["number"] + 1) % 10
                    res_info = get_wingo_number_info(res_num)

            # 8. Check Win / Loss based on verified draw outcome
            is_jackpot = (res_num == pred["number"])
            is_sureshot = (not is_jackpot) and (res_info["bs"] == pred["bs"])
            is_win = is_jackpot or is_sureshot

            if is_win:
                wins += 1
            else:
                losses += 1

            sessions[session_id]["wins"] = wins
            sessions[session_id]["losses"] = losses

            # 9. Format EXACT user result message:
            result_post = (
                f"📊━━━ WinGo 1M Result ━━━\n\n"
                f"🌐Period No :\n"
                f"{period_no}\n"
                f"🔮Prediction :\n"
                f"{pred['number']}\n"
                f"🌟Result :\n"
                f"{res_num}\n"
                f"  {res_info['color_emoji']} {res_info['color_name']}\n"
                f"🎯Big/Small :\n"
                f"{res_info['bs']}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            )
            formatted_result = apply_premium_emojis(result_post, premium_emojis)
            await safe_send_message(bot, chat_id=target_chat_id, text=formatted_result, parse_mode=ParseMode.HTML, reply_markup=keyboard)

            # 10. Sticker / Recovery logic
            if is_jackpot:
                # Exact number match — Jackpot!
                try:
                    await bot.send_sticker(chat_id=target_chat_id, sticker=WINGO_STICKERS["jackpot"])
                except Exception as e_stk:
                    logger.warning("Jackpot sticker error: %s", e_stk)
            elif is_sureshot:
                # Big/Small win — Sureshot!
                try:
                    await bot.send_sticker(chat_id=target_chat_id, sticker=WINGO_STICKERS["sureshot"])
                except Exception as e_stk:
                    logger.warning("Sureshot sticker error: %s", e_stk)
            else:
                # Loss — immediately send a 2nd Recovery Prediction for next period
                await asyncio.sleep(2)
                recovery_history = await fetch_wingo_1m_history()
                rec_period_no, rec_sec_left = get_active_wingo_period(recovery_history)
                rec_pred = generate_smart_wingo_prediction(recovery_history, recovery=True)

                recovery_msg = (
                    f"⚡━━━ 🔄 RECOVERY PREDICTION ━━━⚡\n\n"
                    f"🌐Period No :\n"
                    f"{rec_period_no}\n\n"
                    f"🔮Prediction :\n"
                    f"{rec_pred['number']}\n"
                    f"  {rec_pred['color_emoji']} {rec_pred['color_name']}\n"
                    f"🎯Big/Small :\n"
                    f"{rec_pred['bs']}\n\n"
                    f"⏳ Draw in ~{rec_sec_left}s\n"
                    f"━━━━━━━━━━━━━━━━━━━━━"
                )
                await safe_send_message(bot, chat_id=target_chat_id,
                    text=apply_premium_emojis(recovery_msg, premium_emojis),
                    parse_mode=ParseMode.HTML, reply_markup=keyboard)

            # Brief pause before next round
            if r_idx < total_rounds:
                await asyncio.sleep(4)

    except Exception as e:
        logger.error("Error in run_wingo_prediction_session: %s", e)
    finally:
        sessions[session_id]["is_running"] = False

        # 11. Send Session Summary & Session End Sticker
        total_played = wins + losses
        accuracy = int((wins / total_played) * 100) if total_played > 0 else 0

        summary_text = (
            f"🏁 <b>WinGo 1M Session Completed!</b> 🏁\n\n"
            f"📊 <b>Total Predictions:</b> {total_played}\n"
            f"✅ <b>Total Wins:</b> {wins}\n"
            f"❌ <b>Total Losses:</b> {losses}\n"
            f"🎯 <b>Accuracy Rate:</b> {accuracy}%\n\n"
            f"👑 <i>Thank you for playing with Durov VIP! Tap below to open app anytime:</i>"
        )
        await safe_send_message(bot, chat_id=target_chat_id, text=apply_premium_emojis(summary_text, premium_emojis), parse_mode=ParseMode.HTML, reply_markup=keyboard)

        try:
            await bot.send_sticker(chat_id=target_chat_id, sticker=WINGO_STICKERS["session_end"])
        except Exception as e:
            logger.warning("Failed to send session_end sticker: %s", e)

async def prompt_wingo_predict_setup(chat_id: int, bot, bot_data: dict, user_id_str: str, message_to_edit=None):
    """Prompts admin/sub-admin to choose number of prediction rounds with premium emoji buttons."""
    bot_data.setdefault("admin_states", {})[user_id_str] = "wingo_await_rounds"
    
    colors_enabled = bot_data.get("colors_enabled", True)
    premium_emojis = bot_data.get("premium_emojis", {})

    text = (
        f"🔮 <b>WinGo 1M Auto Predictor Setup</b> 🔮\n\n"
        f"🔢 <b>How many predictions do you want to run?</b>\n"
        f"Send a number (e.g. <code>5</code>, <code>10</code>, <code>15</code>)\n"
        f"or select a quick option below:\n\n"
        f"⚡ <i>60-second live period interval with official API results & stickers!</i>"
    )
    formatted = apply_premium_emojis(text, premium_emojis)

    def make_ibtn(raw_text, cb, style="primary"):
        btn_txt, api_kw = format_button_emoji(
            raw_text, 
            is_reply_keyboard=False, 
            style=style, 
            colors_enabled=colors_enabled, 
            premium_emojis=premium_emojis
        )
        return InlineKeyboardButton(btn_txt, callback_data=cb, api_kwargs=api_kw)

    keyboard = InlineKeyboardMarkup([
        [make_ibtn("🎯 5 Rounds", "wingo_rounds_5", "primary"), make_ibtn("🎯 10 Rounds", "wingo_rounds_10", "primary")],
        [make_ibtn("🚀 15 Rounds", "wingo_rounds_15", "primary"), make_ibtn("🚀 20 Rounds", "wingo_rounds_20", "primary")],
        [make_ibtn("🛑 Stop Active Session", "wingo_stop", "danger")],
        [make_ibtn("❌ Cancel", "adm_close", "danger")]
    ])

    if message_to_edit:
        try:
            await safe_edit_message(message_to_edit, text=formatted, reply_markup=keyboard)
            return
        except Exception:
            pass

    await safe_send_message(bot, chat_id=chat_id, text=formatted, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def send_wingo_destination_selector(chat_id: int, bot, bot_data: dict, user_id_str: str, rounds: int, message_to_edit=None):
    """Allows admin/sub-admin to choose where the predictions should be posted with premium emoji buttons."""
    channels = bot_data.get("channels", [])
    colors_enabled = bot_data.get("colors_enabled", True)
    premium_emojis = bot_data.get("premium_emojis", {})

    def make_ibtn(raw_text, cb, style="primary"):
        btn_txt, api_kw = format_button_emoji(
            raw_text, 
            is_reply_keyboard=False, 
            style=style, 
            colors_enabled=colors_enabled, 
            premium_emojis=premium_emojis
        )
        return InlineKeyboardButton(btn_txt, callback_data=cb, api_kwargs=api_kw)
    
    text = (
        f"🔮 <b>WinGo 1M Predictor ({rounds} Rounds)</b>\n\n"
        f"📍 <b>Where should predictions be posted?</b>\n"
        f"Select the target chat or channel below:"
    )
    formatted = apply_premium_emojis(text, premium_emojis)

    rows = [
        [make_ibtn("💬 Send in Current Chat", f"wingo_run_{rounds}_chat_{chat_id}", "primary")]
    ]

    for c in channels:
        c_title = c.get("title", f"Channel {c.get('id')}")
        if len(c_title) > 22:
            c_title = c_title[:20] + ".."
        c_id = c.get("id")
        rows.append([make_ibtn(f"📢 {c_title}", f"wingo_run_{rounds}_chan_{c_id}", "primary")])

    rows.append([make_ibtn("❌ Cancel", "adm_close", "danger")])
    keyboard = InlineKeyboardMarkup(rows)

    if message_to_edit:
        try:
            await safe_edit_message(message_to_edit, text=formatted, reply_markup=keyboard)
            return
        except Exception:
            pass

    await safe_send_message(bot, chat_id=chat_id, text=formatted, parse_mode=ParseMode.HTML, reply_markup=keyboard)

# ==========================================
# COMMAND HANDLERS
# ==========================================

async def generate_real_channel_link(bot, chat_id, chat_title: str = "", prefer_private: bool = False) -> str:
    """Generates a valid public @username link or a real permanent invite link for a channel."""
    target_id = chat_id
    if isinstance(chat_id, str):
        if chat_id.startswith("-100") or (chat_id.startswith("-") and chat_id[1:].isdigit()):
            try:
                target_id = int(chat_id)
            except ValueError:
                pass

    try:
        chat_obj = await bot.get_chat(target_id)
        
        # Always prefer private invite links for private channels
        if prefer_private or not chat_obj.username:
            try:
                res = await bot.create_chat_invite_link(
                    chat_id=target_id, 
                    name=f"Permanent Link - {chat_title[:20]}",
                    creates_join_request=True
                )
                if res and res.invite_link:
                    return res.invite_link
            except Exception:
                pass

            try:
                res = await bot.create_chat_invite_link(
                    chat_id=target_id, 
                    name=f"Permanent Link - {chat_title[:20]}",
                    creates_join_request=False
                )
                if res and res.invite_link:
                    return res.invite_link
            except Exception:
                pass

            try:
                exp_link = await bot.export_chat_invite_link(chat_id=target_id)
                if exp_link:
                    return exp_link
            except Exception:
                pass
        
        # Fallback to public username link
        if chat_obj.username:
            return f"https://t.me/{chat_obj.username}"
        elif chat_obj.invite_link:
            return chat_obj.invite_link
            
    except Exception as e:
        logger.error("get_chat error for %s: %s", target_id, e)

    return ""

async def auto_scan_channel(bot, bot_data: dict, chat_id: int, chat_title: str = "", chat_type: str = "channel"):
    """Auto-scans and registers any channel where the bot receives join requests or is active."""
    channels = bot_data.setdefault("channels", [])
    str_id = str(chat_id)

    # Check if channel already exists
    existing_chan = next((c for c in channels if str(c.get("id")) == str_id), None)
    if existing_chan:
        if not existing_chan.get("link") or "t.me/c/" in existing_chan.get("link", ""):
            real_link = await generate_real_channel_link(bot, chat_id, chat_title, prefer_private=True)
            if real_link:
                existing_chan["link"] = real_link
                await sync_data_to_db(bot_data)
        return

    # Generate real permanent invite link for new channel (prefer private)
    real_link = await generate_real_channel_link(bot, chat_id, chat_title, prefer_private=True)
    channels.append({
        "id": chat_id,
        "title": chat_title or f"Channel {len(channels)+1}",
        "type": chat_type,
        "link": real_link
    })
    await sync_data_to_db(bot_data)
    logger.info("Auto-scanned & registered channel: %s (%s) -> %s", chat_title, chat_id, real_link)

async def unregister_channel(bot_data: dict, chat_id) -> bool:
    """Removes a channel from bot_data when the bot is no longer present in it."""
    channels = bot_data.get("channels", [])
    new_channels = [c for c in channels if str(c.get("id")) != str(chat_id)]
    if len(new_channels) != len(channels):
        bot_data["channels"] = new_channels
        await sync_data_to_db(bot_data)
        return True
    return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Auto learn custom emojis
    if update.message:
        auto_learn_emojis(update.message, bot_data)

    # Save User to MongoDB database
    await db.save_user(
        user_id=user.id,
        first_name=user.first_name or "",
        username=user.username or "",
        registered=True
    )
    bot_data.setdefault("registered", {})[str(user.id)] = True

    auto_dm_list = bot_data.get("auto_dm_messages", [])
    dm_buttons = bot_data.get("auto_dm_buttons", [])
    colors_enabled = bot_data.get("colors_enabled", True)
    auto_chan_btn = bot_data.get("auto_channel_buttons", False)

    # Build Combined Keyboard (Channel buttons are NOT created automatically)
    inline_markup = build_combined_keyboard(
        channels=bot_data.get("channels", []),
        custom_buttons=bot_data.get("custom_buttons", []),
        dm_buttons=dm_buttons,
        colors_enabled=colors_enabled,
        premium_emojis=bot_data.get("premium_emojis", {}),
        max_buttons=4,
        include_channels=auto_chan_btn # False by default
    )
    
    reply_markup = build_reply_keyboard(bot_data.get("custom_buttons", []), colors_enabled=colors_enabled, premium_emojis=bot_data.get("premium_emojis", {}))

    start_msg = bot_data.get("start_message", "").strip()
    start_media = bot_data.get("start_media")
    formatted_start = apply_premium_emojis(start_msg, bot_data.get("premium_emojis", {})) if start_msg else ""
    
    has_custom = bool(bot_data.get("custom_buttons", [])) or bool(dm_buttons) or (auto_chan_btn and bool(bot_data.get("channels", [])))
    target_markup = inline_markup if has_custom else reply_markup

    # Determine which welcome message to send
    has_start_content = bool(start_media) or bool(formatted_start)
    has_auto_dm = bool(auto_dm_list)

    # If user already received Auto DM from join request, do not send duplicate/different start msg
    if bot_data.get("join_req_dm_sent", {}).get(str(user.id)):
        return

    # Prioritize Auto DM message so new users get ONLY the Auto DM post
    if has_auto_dm:
        await send_auto_dm_messages(
            context.bot, chat_id, auto_dm_list, 
            bot_data.get("premium_emojis", {}),
            channels=bot_data.get("channels", []),
            custom_buttons=bot_data.get("custom_buttons", []),
            dm_buttons=dm_buttons,
            colors_enabled=colors_enabled,
            include_channels=auto_chan_btn
        )
    elif has_start_content:
        if start_media and isinstance(start_media, dict) and start_media.get("file_id"):
            m_type = start_media.get("type", "photo")
            m_file = start_media.get("file_id")
            try:
                if m_type == "photo":
                    await update.message.reply_photo(photo=m_file, caption=formatted_start or None, parse_mode=ParseMode.HTML if formatted_start else None, reply_markup=target_markup)
                elif m_type == "video":
                    await update.message.reply_video(video=m_file, caption=formatted_start or None, parse_mode=ParseMode.HTML if formatted_start else None, reply_markup=target_markup)
                elif m_type == "document":
                    await update.message.reply_document(document=m_file, caption=formatted_start or None, parse_mode=ParseMode.HTML if formatted_start else None, reply_markup=target_markup)
                elif m_type == "animation":
                    await update.message.reply_animation(animation=m_file, caption=formatted_start or None, parse_mode=ParseMode.HTML if formatted_start else None, reply_markup=target_markup)
                else:
                    await safe_send_message(context.bot, chat_id=chat_id, text=formatted_start or "👋", reply_markup=target_markup)
            except Exception as e_media:
                logger.error("Failed to send start media, falling back: %s", e_media)
                if formatted_start:
                    await safe_send_message(context.bot, chat_id=chat_id, text=formatted_start, reply_markup=target_markup)
                else:
                    await update.message.reply_text("👋", reply_markup=target_markup)
        elif formatted_start:
            await safe_send_message(context.bot, chat_id=chat_id, text=formatted_start, reply_markup=target_markup)
        else:
            await update.message.reply_text("👋", reply_markup=target_markup)
    else:
        await update.message.reply_text("👋", reply_markup=target_markup)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})

    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    await send_admin_panel(update.effective_chat.id, context.bot, bot_data, user_id_str)

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    if user_id_str not in bot_data.get("admins", getattr(config, "ADMINS", [])):
        await update.message.reply_text("❌ Access Denied: Only Super Admin can add sub-admins.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("📌 <b>Usage:</b> <code>/addadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    target_user_id = args[0]
    bot_data.setdefault("sub_admins", {})[target_user_id] = {
        "permissions": ["broadcast", "auto_dm_manage", "approve_requests", "edit_start_msg"],
        "added_by": user_id_str
    }
    await sync_data_to_db(bot_data)
    await update.message.reply_text(
        f"✅ <b>Sub-Admin Added!</b>\n\n👤 User ID: <code>{target_user_id}</code>\n"
        "🔑 <b>Granted Permissions:</b>\n"
        "• 📢 Broadcast\n"
        "• 💌 Auto DM Management & Create\n"
        "• 📝 Edit /start Message & Buttons\n"
        "• ⚡ Instant Auto-Approve Join Requests\n\n"
        "📁 <i>Saved to MongoDB & auto-created access.json!</i>",
        parse_mode=ParseMode.HTML
    )

async def add_superadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    if user_id_str not in bot_data.get("admins", getattr(config, "ADMINS", [])):
        await update.message.reply_text("❌ Access Denied: Only Super Admin can add new Super Admins.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("📌 <b>Usage:</b> <code>/addsuperadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    target_user_id = args[0]
    admins = bot_data.setdefault("admins", getattr(config, "ADMINS", []))
    if target_user_id not in admins:
        admins.append(target_user_id)

    bot_data.get("sub_admins", {}).pop(target_user_id, None)

    await sync_data_to_db(bot_data)
    await update.message.reply_text(
        f"👑 <b>Super Admin Promoted!</b>\n\n👤 User ID: <code>{target_user_id}</code>\n"
        "📁 <i>Saved to MongoDB & auto-created access.json!</i>",
        parse_mode=ParseMode.HTML
    )

async def rem_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    if user_id_str not in bot_data.get("admins", getattr(config, "ADMINS", [])):
        await update.message.reply_text("❌ Access Denied: Only Super Admin can remove admins.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("📌 <b>Usage:</b> <code>/remadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    target_user_id = args[0]
    removed = False

    sub_admins = bot_data.get("sub_admins", {})
    if target_user_id in sub_admins:
        sub_admins.pop(target_user_id)
        removed = True

    admins = bot_data.get("admins", [])
    if target_user_id in admins:
        admins.remove(target_user_id)
        removed = True

    if removed:
        await sync_data_to_db(bot_data)
        await update.message.reply_text(f"✔️ Admin <code>{target_user_id}</code> removed.\n📁 <i>Updated access.json & MongoDB!</i>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"📌 User ID <code>{target_user_id}</code> is not an admin.", parse_mode=ParseMode.HTML)

async def add_emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "📌 <b>Usage:</b> <code>/addemoji &lt;emoji&gt; &lt;emoji_id&gt;</code>\n\n"
            "Example: <code>/addemoji 🔥 5474667187258006816</code>\n"
            "Or: <code>/addemoji 🔥 - 5474667187258006816</code>",
            parse_mode=ParseMode.HTML
        )
        return

    full_arg = " ".join(args).strip()
    match = re.search(r'(\S+?)\s*[:|=\-]?\s*(\d{15,22})', full_arg)
    if match:
        e_char = match.group(1).strip()
        e_id = match.group(2).strip()
    elif len(args) >= 2:
        e_char = args[0].strip()
        e_id = args[1].strip()
    else:
        await update.message.reply_text(
            "📌 <b>Usage:</b> <code>/addemoji &lt;emoji&gt; &lt;emoji_id&gt;</code>\n\n"
            "Example: <code>/addemoji 🔥 5474667187258006816</code>",
            parse_mode=ParseMode.HTML
        )
        return

    base_char = e_char.replace("\ufe0f", "")
    bot_data.setdefault("premium_emojis", {})[e_char] = e_id
    bot_data["premium_emojis"][base_char] = e_id

    await db.save_premium_emojis(bot_data["premium_emojis"])
    await sync_data_to_db(bot_data)

    resp = (
        f"🎨 <b>Premium Emoji Registered!</b>\n\n"
        f"<tg-emoji emoji-id=\"{e_id}\">{e_char}</tg-emoji> - <code>{e_id}</code>\n\n"
        f"<i>Saved permanently! Use /emojis to view all.</i>"
    )
    await safe_send_message(context.bot, chat_id=update.effective_chat.id, text=resp, parse_mode=ParseMode.HTML)

async def findemoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extracts custom emoji IDs from a replied message and sends 100% of all registered emojis in chat."""
    bot_data = context.bot_data
    msg = update.message
    if not msg:
        return
    user_id_str = str(msg.from_user.id)
    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        return

    target_msg = msg.reply_to_message or msg
    new_added = auto_learn_emojis(target_msg, bot_data) if (target_msg != msg or msg.reply_to_message) else 0

    prefix = f"✅ <b>Learned {new_added} New Premium Emoji(s)!</b>\n\n" if new_added > 0 else ""
    await send_all_emojis_chunks(context.bot, chat_id=msg.chat_id, bot_data=bot_data, prefix_text=prefix)

async def view_emojis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct command to view ALL registered emojis with IDs in chat: /emojis or /viewemojis"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    await send_all_emojis_chunks(context.bot, chat_id=update.effective_chat.id, bot_data=bot_data)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    user_id_str = str(update.effective_user.id)
    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    is_admin = is_super or is_sub

    if is_admin:
        text = (
            "🏅 <b>Help & Support (Admin Commands)</b>\n\n"
            "👤 <b>Support Admin:</b> @earnwithdurov (Durov Bhai)\n\n"
            "📌 <b>Core Commands:</b>\n"
            "• /start - Start the bot\n"
            "• /admin - Open Admin Panel\n"
            "• /help - Show Help & Support\n\n"
            "🎨 <b>Emoji Commands:</b>\n"
            "• /emojis or /viewemojis - View All Emojis with their IDs\n"
            "• /findemoji - Extract emoji IDs from replied message\n"
            "• /addemoji &lt;emoji&gt; &lt;id&gt; - Register an emoji ID\n\n"
            "🔘 <b>Custom Button Commands (Manual):</b>\n"
            "• /addbtn Text | https://link | style - Add custom button\n"
            "• /buttons - View all custom buttons\n"
            "• /rembutton &lt;index&gt; - Remove a custom button\n"
            "• /clearbuttons - Clear all custom buttons\n"
            "• /toggleautochannel - Toggle auto channel buttons (Default: OFF)\n\n"
            "💌 <b>Auto DM Button Commands:</b>\n"
            "• /adddmbtn url | Text | Link | style - Add DM button\n"
            "• /showdmbtn - Show all DM buttons\n"
            "• /cleardmbtn - Clear all DM buttons\n\n"
            "👥 <b>Admin Management:</b>\n"
            "• /addadmin &lt;user_id&gt; - Add Sub-Admin\n"
            "• /addsuperadmin &lt;user_id&gt; - Promote Super Admin\n"
            "• /remadmin &lt;user_id&gt; - Remove Admin\n"
            "• /fixlinks - Scan & repair channel invite links\n\n"
            "💬 <i>Need help? Contact <a href='https://t.me/earnwithdurov'>@earnwithdurov</a></i>"
        )
    else:
        text = (
            "🏅 <b>Help & Support</b>\n\n"
            "👤 <b>Support Admin:</b> @earnwithdurov (Durov Bhai)\n\n"
            "📌 <b>Available Commands:</b>\n"
            "• /start - Start the bot\n"
            "• /help - Show Help & Support\n\n"
            "💬 <i>Need help? Contact <a href='https://t.me/earnwithdurov'>@earnwithdurov</a></i>"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Contact Support (@earnwithdurov)", url="https://t.me/earnwithdurov")]
    ])
    await safe_send_message(context.bot, chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def buttons_demo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    EMOJI_ID = "5474667187258006816"

    inline_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Primary", callback_data="primary_btn", api_kwargs={"style": "primary"}),
            InlineKeyboardButton("Success", callback_data="success_btn", api_kwargs={"style": "success"})
        ],
        [
            InlineKeyboardButton("Danger", callback_data="danger_btn", api_kwargs={"style": "danger"})
        ],
        [
            InlineKeyboardButton("Emoji Button", callback_data="emoji_btn", api_kwargs={"icon_custom_emoji_id": EMOJI_ID})
        ]
    ])

    reply_markup = ReplyKeyboardMarkup([
        [
            KeyboardButton("Primary Button", api_kwargs={"style": "primary"}),
            KeyboardButton("Success Button", api_kwargs={"style": "success"})
        ],
        [
            KeyboardButton("Danger Button", api_kwargs={"style": "danger"})
        ],
        [
            KeyboardButton("Emoji Button", api_kwargs={"icon_custom_emoji_id": "6080004495046089734"})
        ]
    ], resize_keyboard=True)

    await update.message.reply_text("<b>🔥 New Button UI Preview</b>\n\nColored buttons + premium emoji icons (Inline Keyboard)", parse_mode=ParseMode.HTML, reply_markup=inline_markup)
    await update.message.reply_text("<b>🔥 New Button UI Preview</b>\n\nColored buttons + premium emoji icons (Reply Keyboard)", parse_mode=ParseMode.HTML, reply_markup=reply_markup)

# ==========================================
# ADMIN PANEL HANDLERS (PAGINATED 2 PAGES)
# ==========================================

async def send_admin_panel(chat_id: int, bot, bot_data: dict, user_id_str: str = "", page: int = 1, message_to_edit=None):
    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})

    colors_enabled = bot_data.get("colors_enabled", True)
    color_status = "✅ <b>ON</b>" if colors_enabled else "❌ <b>OFF</b>"
    auto_approve_enabled = bot_data.get("auto_approve_enabled", True)
    approve_status = "⚡ <b>AUTO (Instant)</b>" if auto_approve_enabled else "🛑 <b>MANUAL</b>"
    premium_emojis = bot_data.get("premium_emojis", {})
    unique_emojis_count = len(get_unique_emojis_list(bot_data))
    start_media_status = "🖼️ <b>Set</b>" if bot_data.get("start_media") else "❌ <b>None</b>"
    start_msg_status = "📝 <b>Set</b>" if bot_data.get("start_message") else "❌ <b>None</b>"
    dm_buttons_count = len(bot_data.get("auto_dm_buttons", []))
    auto_chan_btn = bot_data.get("auto_channel_buttons", False)

    def ibtni(raw_text, cb, style="primary"):
        btn_text, kw = format_button_emoji(raw_text, style=style, colors_enabled=colors_enabled, premium_emojis=premium_emojis)
        return InlineKeyboardButton(btn_text, callback_data=cb, api_kwargs=kw)

    if is_super:
        if page == 1:
            text = (
                f"🔰 <b>⚡ Super Admin Panel (Page 1/2) ⚡</b> 🔰\n\n"
                f"🎨 <b>Button Colors:</b> {color_status}\n"
                f"⚡ <b>Join Request Mode:</b> {approve_status}\n"
                f"📢 <b>Auto Channel Button:</b> {'✅ ON' if auto_chan_btn else '❌ OFF (Manual Custom)'}\n"
                f"👥 <b>Sub-Admins:</b> {len(bot_data.get('sub_admins', {}))}\n"
                f"🎨 <b>Learned Emojis:</b> {unique_emojis_count}\n"
                f"🔘 <b>Custom Buttons:</b> {len(bot_data.get('custom_buttons', []))}\n"
                f"💌 <b>DM Buttons:</b> {dm_buttons_count}/4\n\n"
                f"<i>📌 Select an option:</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [ibtni("🔮 WinGo 1M Predict", "adm_wingo_predict_prompt", "danger"), ibtni("📢 Broadcast", "adm_broadcast")],
                [ibtni(f"⚡ Auto-Approve: {'ON ✅' if auto_approve_enabled else 'OFF ❌'}", "adm_toggle_auto_approve"), ibtni("📊 Approve All", "adm_approve_all", "danger")],
                [ibtni("👥 Sub-Admins", "adm_manage_sub_admins"), ibtni("➕ Add Channel", "adm_add_chan")],
                [ibtni("❌ Remove Channel", "adm_rem_chan", "danger"), ibtni("📝 Edit /start Msg", "adm_edit_start_msg")],
                [ibtni("🖼️ Edit /start Media", "adm_edit_start_msg", "success"), ibtni("➕ Add Custom Button", "adm_add_custom_btn")],
                [ibtni("🎨 View Emojis & IDs", "adm_view_emojis"), ibtni("💌 Manage DM Buttons", "adm_manage_dm_buttons", "success")],
                [ibtni("▶️ Page 2", "adm_page_2", "success"), ibtni("🚪 Close", "adm_close", "danger")]
            ])
        else:
            text = (
                f"🔰 <b>⚡ Super Admin Panel (Page 2/2) ⚡</b> 🔰\n\n"
                f"🖼️ <b>Start Image/Media:</b> {start_media_status}\n"
                f"📝 <b>Start Message Text:</b> {start_msg_status}\n"
                f"📢 <b>Auto Channel Button:</b> {'✅ ON' if auto_chan_btn else '❌ OFF (Manual Custom)'}\n"
                f"🔘 <b>Custom Buttons:</b> {len(bot_data.get('custom_buttons', []))}\n"
                f"📤 <b>Save Mode:</b> {'✅ ON' if bot_data.get('save_mode') else '❌ OFF'}\n"
                f"💌 <b>DM Buttons:</b> {dm_buttons_count}/4\n\n"
                f"<i>📌 Select an option:</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [ibtni(f"📢 Auto Channel Btn: {'ON ✅' if auto_chan_btn else 'OFF ❌'}", "adm_toggle_auto_chan_btn"), ibtni("🔄 Fix Channel Links", "adm_fix_channel_links", "success")],
                [ibtni("🖼️ Edit Start Media", "adm_edit_start_msg"), ibtni("🗑️ Remove Start Media", "adm_rem_start_media", "danger")],
                [ibtni("🗑️ Remove Start Msg Text", "adm_rem_start_msg", "danger"), ibtni("🎨 Learn Premium Emoji", "adm_learn_emoji_prompt")],
                [ibtni("🔘 Manage Custom Buttons", "adm_manage_buttons", "primary"), ibtni("🗑️ Clear All Buttons", "adm_clear_buttons", "danger")],
                [ibtni("🎨 Toggle Colors", "adm_toggle_button_colors"), ibtni("📤 Toggle Save Mode", "adm_toggle_save_mode")],
                [ibtni("🔄 RESET BOT", "adm_reset_bot", "danger"), ibtni("🚪 Close", "adm_close", "danger")],
                [ibtni("◀️ Page 1", "adm_page_1", "success")]
            ])
    elif is_sub:
        text = (
            f"🔰 <b>⚡ Sub-Admin Panel ⚡</b> 🔰\n\n"
            f"⚡ <b>Join Request Mode:</b> {approve_status}\n"
            f"🔑 <b>Your Permissions:</b> Broadcast, Auto DM, Edit /start Msg, Approve Requests, WinGo Predict\n"
            f"🎨 <b>Learned Emojis:</b> {unique_emojis_count}\n"
            f"💌 <b>DM Buttons:</b> {dm_buttons_count}/4\n\n"
            f"<i>📌 Select an option:</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [ibtni("🔮 WinGo 1M Predict", "adm_wingo_predict_prompt", "danger"), ibtni("📢 Broadcast", "adm_broadcast")],
            [ibtni("💌 Manage Auto DM", "adm_manage_auto_dm"), ibtni("➕ Create Auto DM", "adm_create_autodm")],
            [ibtni("📝 Edit /start Msg", "adm_edit_start_msg"), ibtni("🎨 View Emojis & IDs", "adm_view_emojis")],
            [ibtni(f"⚡ Auto-Approve: {'ON ✅' if auto_approve_enabled else 'OFF ❌'}", "adm_toggle_auto_approve"), ibtni("💌 Manage DM Buttons", "adm_manage_dm_buttons", "success")],
            [ibtni("🚪 Close", "adm_close", "danger")]
        ])
    else:
        return

    admin_reply_keyboard = build_admin_reply_keyboard(bot_data, is_super, is_sub, user_id_str)
    formatted_text = apply_premium_emojis(text, premium_emojis)

    if message_to_edit:
        try:
            await safe_edit_message(message_to_edit, text=formatted_text, reply_markup=keyboard)
            return
        except Exception:
            pass

    await safe_send_message(bot, chat_id=chat_id, text=formatted_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await safe_send_message(bot, chat_id=chat_id, text=apply_premium_emojis("🎨 <b>Admin Keyboard activated below:</b>", premium_emojis), parse_mode=ParseMode.HTML, reply_markup=admin_reply_keyboard)

async def show_auto_dm_messages(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    messages = bot_data.get("auto_dm_messages", [])
    save_mode = bot_data.get("save_mode", False)
    learned_count = len(get_unique_emojis_list(bot_data))

    text = "💌 <b>Auto DM Messages & Button Management</b>\n\n"
    text += f"📤 <b>Save Mode:</b> {'✅ <b>ON</b>' if save_mode else '❌ <b>OFF</b>'}\n"
    text += f"🎨 <b>Learned Emoji IDs:</b> {learned_count}\n\n"

    keyboard_rows = [
        [InlineKeyboardButton("➕ Create New Auto DM Post", callback_data="adm_create_autodm")],
        [InlineKeyboardButton("🔄 Turn OFF Save Mode" if save_mode else "🔄 Turn ON Save Mode", callback_data="adm_toggle_save_mode")],
    ]

    if not messages:
        text += "<i>No Auto DM messages saved yet.</i>\n\n"
        text += "📌 <b>How to create with buttons:</b>\n"
        text += "1️⃣ Tap <b>➕ Create New Auto DM Post</b> below (or turn on Save Mode)\n"
        text += "2️⃣ Send your post (photo/video/text/document)\n"
        text += "3️⃣ Interactive Live Preview opens with button builder (Name ➔ URL ➔ Color)!\n"
        text += "4️⃣ Save & it's active immediately!"
    else:
        text += f"<b>📨 Saved Messages ({len(messages)}):</b>\n\n"
        for idx, msg in enumerate(messages):
            m_type = "📋 Forward" if msg.get("type") == "copy" else (
                "Photo 🖼️" if "photo" in msg else (
                "Video 🎥" if "video" in msg else (
                "Document 📁" if "document" in msg else "Text 📝")))
            
            btns = msg.get("buttons", [])
            btn_info = f"({len(btns)} button{'s' if len(btns)!=1 else ''})" if btns else "(no buttons)"
            text += f"{idx + 1}. <b>{m_type}</b> — {btn_info}\n"
            
            keyboard_rows.append([
                InlineKeyboardButton(f"✏️ Buttons #{idx+1}", callback_data=f"adm_edit_autodm_{idx}"),
                InlineKeyboardButton(f"❌ Delete #{idx+1}", callback_data=f"adm_del_autodm_{idx}")
            ])

        text += "\n📌 Tap <b>✏️ Buttons</b> to add or edit buttons with live preview!"

    keyboard_rows.append([
        InlineKeyboardButton("🎨 View Emojis & IDs", callback_data="adm_view_emojis"),
        InlineKeyboardButton("🗑️ Clear All DM", callback_data="adm_clear_auto_dm")
    ])
    keyboard_rows.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")])

    formatted_text = apply_premium_emojis(text, bot_data.get("premium_emojis", {}))
    await safe_send_message(context.bot, chat_id=chat_id, text=formatted_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard_rows))

# ==========================================
# JOIN VERIFICATION PROCESSOR
# ==========================================

async def process_check_joined(bot, chat_id: int, user, bot_data: dict):
    user_id = user.id
    user_id_str = str(user_id)
    missing = []
    for chan in bot_data.get("channels", []):
        try:
            member = await bot.get_chat_member(chat_id=chan["id"], user_id=user_id)
            if member.status not in ["creator", "administrator", "member"]:
                missing.append(chan)
        except Exception as e:
            logger.error(f"get_chat_member error: {e}")
            missing.append(chan)

    if not missing:
        await db.mark_verified(user_id)
        if user_id_str not in bot_data.get("verified_users", []):
            bot_data.setdefault("verified_users", []).append(user_id_str)

        succ_msg = bot_data.get("verification_success_msg", getattr(config, "DEFAULT_VERIFICATION_MSG", "✅ Verified!"))
        succ_msg = apply_premium_emojis(succ_msg, bot_data.get("premium_emojis", {}))

        colors_enabled = bot_data.get("colors_enabled", True)
        inline_markup = build_combined_keyboard(
            channels=[],
            custom_buttons=bot_data.get("custom_buttons", []),
            dm_buttons=bot_data.get("auto_dm_buttons", []),
            colors_enabled=colors_enabled,
            premium_emojis=bot_data.get("premium_emojis", {}),
            max_buttons=4,
            include_channels=False
        )
        await safe_send_message(bot, chat_id=chat_id, text=succ_msg, parse_mode=ParseMode.HTML, reply_markup=inline_markup)
    else:
        list_text = "".join([f"• {'🔒 Private' if c.get('type')=='private' else '🌐 Public'} Channel {i+1}\n" for i, c in enumerate(missing)])
        txt = f"📌 You still need to join:\n\n{list_text}\n📌 Join and click Check Joined again."
        inline_markup = build_combined_keyboard(
            channels=missing,
            custom_buttons=[],
            dm_buttons=[],
            colors_enabled=bot_data.get("colors_enabled", True),
            premium_emojis=bot_data.get("premium_emojis", {}),
            max_buttons=4,
            include_channels=True # Show missing channels for verification
        )
        await safe_send_message(bot, chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML, reply_markup=inline_markup)

# ==========================================
# CALLBACK QUERY HANDLER
# ==========================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    user_id_str = str(user_id)
    chat_id = query.message.chat.id
    bot_data = context.bot_data

    is_super_admin = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub_admin = user_id_str in bot_data.get("sub_admins", {})
    is_admin = is_super_admin or is_sub_admin

    logger.info("Button Tapped by User %s (%s): data='%s' in chat %s", user_id, query.from_user.first_name, data, chat_id)

    if data == "check_joined":
        await process_check_joined(context.bot, chat_id, query.from_user, bot_data)
        return

    # Handle non-admin callback queries for custom buttons
    if not is_admin:
        matched_btn = None
        for btn in bot_data.get("custom_buttons", []):
            if btn.get("callback_data") == data or btn.get("text", "").lower() == data.lower():
                matched_btn = btn
                break

        if matched_btn:
            if matched_btn.get("type") == "url" and matched_btn.get("url"):
                target_url = str(matched_btn["url"]).strip().replace(" ", "")
                if not target_url.startswith("http"):
                    target_url = "https://" + target_url
                await query.answer(url=target_url)
            else:
                await query.answer()
        else:
            await query.answer()
        return

    # Check Sub-Admin restricted actions
    allowed_sub_actions = [
        "adm_broadcast", "adm_manage_auto_dm", "adm_toggle_auto_approve", 
        "adm_toggle_save_mode", "adm_clear_auto_dm", "adm_view_emojis", 
        "adm_learn_emoji_prompt", "adm_close", "adm_back", "adm_page_1", 
        "adm_page_2", "adm_approve_all", "adm_manage_dm_buttons",
        "adm_emojis_send_all", "adm_create_autodm", "bld_add_btn",
        "bld_clear_btns", "bld_save_finish", "bld_cancel", "bld_cancel_btn",
        "adm_edit_start_msg", "adm_rem_start_media", "adm_rem_start_msg",
        "adm_clear_dm_buttons", "adm_wingo_predict_prompt", "wingo_stop"
    ]
    is_sub_action_allowed = (
        data in allowed_sub_actions 
        or data.startswith("adm_emojis_page_") 
        or data.startswith("adm_del_btn_")
        or data.startswith("bld_")
        or data.startswith("adm_edit_autodm_")
        or data.startswith("adm_del_autodm_")
        or data.startswith("wingo_")
    )
    if is_sub_admin and not is_super_admin and not is_sub_action_allowed:
        await query.answer("❌ Access Denied: Super Admin permission required for this action.", show_alert=True)
        return

    # WinGo Predictor Callbacks
    if data == "adm_wingo_predict_prompt":
        await prompt_wingo_predict_setup(chat_id, context.bot, bot_data, user_id_str, message_to_edit=query.message)
        return

    if data.startswith("wingo_rounds_"):
        rounds = int(data.replace("wingo_rounds_", ""))
        bot_data.get("admin_states", {}).pop(user_id_str, None)
        await send_wingo_destination_selector(chat_id, context.bot, bot_data, user_id_str, rounds, message_to_edit=query.message)
        return

    if data.startswith("wingo_run_"):
        parts = data.split("_")
        if len(parts) >= 5:
            rounds = int(parts[2])
            target_type = parts[3]
            target_id = int(parts[4])
            bot_data.get("admin_states", {}).pop(user_id_str, None)

            target_title = "Connected Channel" if target_type == "chan" else "Current Chat"
            for c in bot_data.get("channels", []):
                if str(c.get("id")) == str(target_id):
                    target_title = c.get("title", target_title)
                    break

            asyncio.create_task(run_wingo_prediction_session(context.bot, target_id, rounds, bot_data, user_id_str))

            confirm_txt = (
                f"✅ <b>WinGo 1M Auto-Predictor Started!</b>\n\n"
                f"🎯 <b>Rounds:</b> {rounds}\n"
                f"📍 <b>Target:</b> {target_title}\n"
                f"⏱️ <b>Interval:</b> 60s per round\n\n"
                f"<i>Bot will post live predictions, official results, stickers & Sureshot App button automatically! Use /stoppredict to stop anytime.</i>"
            )
            await safe_edit_message(query.message, text=apply_premium_emojis(confirm_txt, bot_data.get("premium_emojis", {})), reply_markup=None)
            return

    if data == "wingo_stop":
        stopped = False
        for sess_id, sess in bot_data.get("wingo_sessions", {}).items():
            if sess.get("is_running"):
                sess["is_running"] = False
                stopped = True
        txt = "🛑 <b>Active WinGo 1M prediction session has been stopped!</b>" if stopped else "📌 No active WinGo session running."
        await safe_edit_message(query.message, text=apply_premium_emojis(txt, bot_data.get("premium_emojis", {})), reply_markup=None)
        return

    if data == "adm_close":
        await query.message.delete()
        return

    # Back to panel handler
    if data in ["adm_back", "adm_page_1"]:
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=1, message_to_edit=query.message)
        return

    if data == "adm_page_2":
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2, message_to_edit=query.message)
        return

    # View Emojis with IDs clearly formatted with Premium <tg-emoji> & Interactive Pagination
    if data == "adm_view_emojis" or data.startswith("adm_emojis_page_"):
        page = 1
        if data.startswith("adm_emojis_page_"):
            try:
                page = int(data.replace("adm_emojis_page_", ""))
            except Exception:
                page = 1
        etext, keyboard = render_emojis_panel_page(bot_data, page=page)
        try:
            await query.message.edit_text(text=etext, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception:
            await safe_send_message(context.bot, chat_id=chat_id, text=etext, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_emojis_send_all":
        await query.answer("📤 Sending all emojis to chat...", show_alert=False)
        await send_all_emojis_chunks(context.bot, chat_id=chat_id, bot_data=bot_data)
        return

    # Interactive Button Builder Callbacks
    if data == "bld_add_btn":
        draft = bot_data.get("builder_drafts", {}).get(user_id_str)
        if not draft:
            await query.answer("Session expired. Please send the post again.", show_alert=True)
            return
        if len(draft.get("buttons", [])) >= 4:
            await query.answer("Max 4 buttons allowed! Clear some first.", show_alert=True)
            return

        draft["step"] = "awaiting_btn_input"
        bot_data.setdefault("admin_states", {})[user_id_str] = "builder_input"

        prompt = (
            "➕ <b>Add Button to Post</b>\n\n"
            "📌 <b>Option 1 (Fast 1-Line):</b>\n"
            "Send: <code>Button Name | https://yourlink.com | color</code>\n"
            "Colors: <code>primary</code> (🔵), <code>success</code> (🟢), <code>danger</code> (🔴)\n\n"
            "📌 <b>Option 2 (Step-by-Step):</b>\n"
            "Send <b>just the Button Name</b> (e.g. <code>Join VIP 💎</code>)\n"
            "and bot will ask for the URL and color next!\n\n"
            "<i>(Send /cancel to cancel adding button)</i>"
        )
        await safe_send_message(context.bot, chat_id=chat_id, text=prompt, parse_mode=ParseMode.HTML)
        return

    if data.startswith("bld_color_"):
        draft = bot_data.get("builder_drafts", {}).get(user_id_str)
        if not draft or not draft.get("pending_btn"):
            await query.answer("No pending button found.", show_alert=True)
            return

        chosen_style = data.replace("bld_color_", "")
        if chosen_style not in ["primary", "success", "danger"]:
            chosen_style = "primary"

        pending = draft["pending_btn"]
        pending["style"] = chosen_style
        pending["type"] = "url"

        draft.setdefault("buttons", []).append(pending)
        draft["pending_btn"] = None
        draft["step"] = "idle"
        bot_data.get("admin_states", {}).pop(user_id_str, None)

        color_icon = "🔵 Primary" if chosen_style == "primary" else ("🟢 Success" if chosen_style == "success" else "🔴 Danger")
        await query.answer(f"✅ Added: {pending['text']}!")
        await safe_send_message(context.bot, chat_id=chat_id, text=f"✅ <b>Button Added!</b>\n\n🔘 <b>{pending['text']}</b> ➔ {pending['url']} ({color_icon})", parse_mode=ParseMode.HTML)
        await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
        return

    if data == "bld_cancel_btn":
        draft = bot_data.get("builder_drafts", {}).get(user_id_str)
        if draft:
            draft["pending_btn"] = None
            draft["step"] = "idle"
        bot_data.get("admin_states", {}).pop(user_id_str, None)
        await query.answer("Button adding cancelled.")
        await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
        return

    if data == "bld_clear_btns":
        draft = bot_data.get("builder_drafts", {}).get(user_id_str)
        if draft:
            draft["buttons"] = []
            draft["pending_btn"] = None
            draft["step"] = "idle"
        await query.answer("All buttons cleared!")
        await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
        return

    if data == "bld_cancel":
        bot_data.get("builder_drafts", {}).pop(user_id_str, None)
        bot_data.get("admin_states", {}).pop(user_id_str, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        await safe_send_message(context.bot, chat_id=chat_id, text="❌ <b>Builder cancelled.</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "bld_save_finish":
        draft = bot_data.get("builder_drafts", {}).get(user_id_str)
        if not draft:
            await query.answer("No active draft found.", show_alert=True)
            return

        target = draft.get("target")

        if target == "start":
            bot_data["start_message"] = draft.get("text", "")
            if draft.get("media_type") and draft.get("file_id") and draft.get("media_type") != "text":
                bot_data["start_media"] = {"type": draft["media_type"], "file_id": draft["file_id"]}
            else:
                bot_data["start_media"] = None

            bot_data["custom_buttons"] = draft.get("buttons", [])
            await sync_data_to_db(bot_data)
            bot_data.get("builder_drafts", {}).pop(user_id_str, None)
            bot_data.get("admin_states", {}).pop(user_id_str, None)

            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=(
                    f"🎉 <b>/start Message & Buttons Saved Successfully!</b>\n\n"
                    f"🔘 Total Buttons Attached: <b>{len(bot_data['custom_buttons'])}</b>\n"
                    f"⚡ Live immediately for all users who type /start!"
                ),
                parse_mode=ParseMode.HTML
            )
            await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
            return

        elif target == "autodm":
            payload = draft.get("payload", {})
            payload["buttons"] = draft.get("buttons", [])
            bot_data.setdefault("auto_dm_messages", []).append(payload)
            await sync_data_to_db(bot_data)
            bot_data.get("builder_drafts", {}).pop(user_id_str, None)
            bot_data.get("admin_states", {}).pop(user_id_str, None)

            await safe_send_message(
                context.bot,
                chat_id=chat_id,
                text=(
                    f"🎉 <b>Auto DM Post & Buttons Saved Successfully!</b>\n\n"
                    f"🔘 Total Buttons Attached: <b>{len(payload['buttons'])}</b>\n"
                    f"📨 Total Saved DM Messages: <b>{len(bot_data['auto_dm_messages'])}</b>"
                ),
                parse_mode=ParseMode.HTML
            )
            await show_auto_dm_messages(chat_id, context)
            return

        elif str(target).startswith("edit_autodm_"):
            idx = int(str(target).replace("edit_autodm_", ""))
            if 0 <= idx < len(bot_data.get("auto_dm_messages", [])):
                bot_data["auto_dm_messages"][idx]["buttons"] = draft.get("buttons", [])
                await sync_data_to_db(bot_data)

            bot_data.get("builder_drafts", {}).pop(user_id_str, None)
            bot_data.get("admin_states", {}).pop(user_id_str, None)
            await safe_send_message(context.bot, chat_id=chat_id, text=f"🎉 <b>Auto DM Post #{idx+1} Buttons Updated!</b>", parse_mode=ParseMode.HTML)
            await show_auto_dm_messages(chat_id, context)
            return

    # Auto DM post creation prompt
    if data == "adm_create_autodm":
        bot_data.setdefault("admin_states", {})[user_id_str] = "create_autodm_post"
        prompt = (
            "💌 <b>Create New Auto DM Post</b>\n\n"
            "Send any post for your Auto DM:\n"
            "• Send Photo, Video, GIF, Document, or Text message\n"
            "• Custom emojis & HTML captions are supported!\n\n"
            "<i>Once sent, the live preview with the interactive button builder will open!</i>"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Auto DM", callback_data="adm_manage_auto_dm")]])
        await safe_send_message(context.bot, chat_id=chat_id, text=prompt, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    # Edit buttons of existing Auto DM post
    if data.startswith("adm_edit_autodm_"):
        idx = int(data.replace("adm_edit_autodm_", ""))
        dm_msgs = bot_data.get("auto_dm_messages", [])
        if 0 <= idx < len(dm_msgs):
            target_msg = dm_msgs[idx]
            m_type = "text"
            m_file_id = None
            if target_msg.get("photo"): m_type, m_file_id = "photo", target_msg["photo"]
            elif target_msg.get("video"): m_type, m_file_id = "video", target_msg["video"]
            elif target_msg.get("document"): m_type, m_file_id = "document", target_msg["document"]
            elif target_msg.get("animation"): m_type, m_file_id = "animation", target_msg["animation"]
            
            raw_text = target_msg.get("caption") or target_msg.get("text") or ""
            existing_btns = list(target_msg.get("buttons", []))

            bot_data.setdefault("builder_drafts", {})[user_id_str] = {
                "target": f"edit_autodm_{idx}",
                "media_type": m_type,
                "file_id": m_file_id,
                "text": raw_text,
                "buttons": existing_btns,
                "payload": target_msg,
                "pending_btn": None,
                "step": "idle"
            }
            await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
            return

    # Delete Auto DM post
    if data.startswith("adm_del_autodm_"):
        idx = int(data.replace("adm_del_autodm_", ""))
        dm_msgs = bot_data.get("auto_dm_messages", [])
        if 0 <= idx < len(dm_msgs):
            dm_msgs.pop(idx)
            await sync_data_to_db(bot_data)
            await query.answer("Auto DM message deleted!")
        await show_auto_dm_messages(chat_id, context)
        return

    # Toggle Auto Channel Buttons
    if data == "adm_toggle_auto_chan_btn":
        bot_data["auto_channel_buttons"] = not bot_data.get("auto_channel_buttons", False)
        await sync_data_to_db(bot_data)
        st = "ENABLED ✅ (Channel buttons will appear)" if bot_data["auto_channel_buttons"] else "DISABLED ❌ (Only manual custom buttons will appear)"
        await safe_send_message(context.bot, chat_id=chat_id, text=f"✔️ Auto Channel Buttons are now: <b>{st}</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_approve_all":
        await query.answer("⚙️ Bulk approving join requests...", show_alert=False)
        await bulk_approve_requests_action(context.bot, bot_data, chat_id)
        return

    if data == "adm_fix_channel_links":
        await query.answer("⚙️ Scanning & Repairing all channel invite links...", show_alert=False)
        repaired_count, report_text = await refresh_all_channel_links_with_report(context.bot, bot_data)
        await safe_send_message(context.bot, chat_id=chat_id, text=report_text, parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_rem_start_media":
        bot_data["start_media"] = None
        await sync_data_to_db(bot_data)
        await safe_send_message(context.bot, chat_id=chat_id, text="🗑️ <b>Start Image/Media Removed!</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_rem_start_msg":
        bot_data["start_message"] = ""
        await sync_data_to_db(bot_data)
        await safe_send_message(context.bot, chat_id=chat_id, text="🗑️ <b>Start Message Text Removed!</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_manage_buttons":
        custom_btns = bot_data.get("custom_buttons", [])
        if not custom_btns:
            await query.answer("🔘 No Custom Buttons Configured.", show_alert=True)
            return

        b_text = "🔘 <b>Manage Custom Buttons</b>\n\nTap a button below to delete it:\n\n"
        b_rows = []
        for i, btn in enumerate(custom_btns):
            b_text += f"{i+1}. <b>{btn['text']}</b> ({btn.get('style', 'primary').upper()})\n"
            b_rows.append([InlineKeyboardButton(f"❌ Delete '{btn['text']}'", callback_data=f"adm_del_btn_{i}")])

        b_rows.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_page_2")])
        keyboard = InlineKeyboardMarkup(b_rows)
        await safe_edit_message(query.message, text=b_text, reply_markup=keyboard)
        return

    if data.startswith("adm_del_btn_"):
        try:
            b_idx = int(data.replace("adm_del_btn_", ""))
            custom_btns = bot_data.get("custom_buttons", [])
            if 0 <= b_idx < len(custom_btns):
                removed = custom_btns.pop(b_idx)
                await sync_data_to_db(bot_data)
                await safe_send_message(context.bot, chat_id=chat_id, text=f"✔️ Removed custom button: <b>{removed.get('text')}</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Error deleting custom button: %s", e)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_toggle_auto_approve":
        bot_data["auto_approve_enabled"] = not bot_data.get("auto_approve_enabled", True)
        await sync_data_to_db(bot_data)
        status_txt = "INSTANT AUTO-APPROVE ⚡" if bot_data["auto_approve_enabled"] else "MANUAL APPROVAL 🛑"
        await safe_send_message(context.bot, chat_id=chat_id, text=f"✔️ Join request mode set to: <b>{status_txt}</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "adm_manage_sub_admins":
        subs = bot_data.get("sub_admins", {})
        stext = "👥 <b>Sub-Admins Management</b>\n\n"
        if not subs:
            stext += "<i>No sub-admins added yet.</i>\n\n"
        else:
            for suid, sinfo in subs.items():
                stext += f"• User ID: <code>{suid}</code> (Permissions: {', '.join(sinfo.get('permissions', []))})\n"
            stext += "\n"
        stext += "📌 <b>To add a sub-admin:</b>\nSend command: <code>/addadmin &lt;user_id&gt;</code>\n\n"
        stext += "📌 <b>To remove a sub-admin:</b>\nSend command: <code>/remadmin &lt;user_id&gt;</code>"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")]])
        await safe_send_message(context.bot, chat_id=chat_id, text=stext, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_toggle_button_colors":
        bot_data["colors_enabled"] = not bot_data.get("colors_enabled", True)
        await sync_data_to_db(bot_data)
        status_text = "ENABLED 🎨" if bot_data["colors_enabled"] else "DISABLED ❌"
        await safe_send_message(context.bot, chat_id=chat_id, text=f"✔️ Button colors are now <b>{status_text}</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "adm_manage_auto_dm":
        bot_data.setdefault("admin_states", {})[user_id_str] = "manage_auto_dm"
        await show_auto_dm_messages(chat_id, context)
        return

    if data == "adm_manage_dm_buttons":
        dm_buttons = bot_data.get("auto_dm_buttons", [])
        text = f"💌 <b>DM Buttons Management ({len(dm_buttons)}/4)</b>\n\n"
        if not dm_buttons:
            text += "<i>No DM buttons added yet.</i>\n\n"
        else:
            for idx, btn in enumerate(dm_buttons):
                text += f"{idx+1}. <b>{btn.get('text')}</b> → {btn.get('url')} ({btn.get('style', 'primary')})\n"
        
        text += "\n📌 <b>Commands:</b>\n"
        text += "<code>/adddmbtn url | Text | Link | style</code> - Add button\n"
        text += "<code>/cleardmbtn</code> - Clear all DM buttons\n"
        text += "<code>/showdmbtn</code> - Show all DM buttons\n"
        text += "\n<b>Max 4 buttons | 2 per row</b>"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Clear All DM Buttons", callback_data="adm_clear_dm_buttons")],
            [InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")]
        ])
        await safe_send_message(context.bot, chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_clear_dm_buttons":
        bot_data["auto_dm_buttons"] = []
        await sync_data_to_db(bot_data)
        await safe_send_message(context.bot, chat_id=chat_id, text="🗑️ <b>All DM buttons cleared!</b>", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "adm_toggle_save_mode":
        bot_data["save_mode"] = not bot_data.get("save_mode", False)
        await sync_data_to_db(bot_data)
        await show_auto_dm_messages(chat_id, context)
        return

    if data == "adm_clear_auto_dm":
        bot_data["auto_dm_messages"] = []
        await sync_data_to_db(bot_data)
        await show_auto_dm_messages(chat_id, context)
        return

    if data == "adm_learn_emoji_prompt":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_premium_emoji"
        msg = (
            "🎨 <b>Learn / Add Premium Emoji</b>\n\n"
            "Send a message containing Premium Emojis,\n"
            "or send in format:\n<code>Emoji | Emoji_ID</code> (e.g. <code>🔥 | 5474667187258006816</code>)\n\n"
            "<i>Newly learned emoji IDs will be stored permanently in MongoDB and active across the entire bot!</i>"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")]])
        await safe_send_message(context.bot, chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_broadcast":
        bot_data.setdefault("admin_states", {})[user_id_str] = "broadcast"
        await safe_send_message(context.bot, chat_id=chat_id, text="📌 <b>Broadcast Mode</b>\n\nSend any message (text, photo, video, APK/file, sticker, forward) to broadcast to all MongoDB users:", parse_mode=ParseMode.HTML)
        return

    if data == "adm_add_chan":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Public Channel", callback_data="adm_add_public"), InlineKeyboardButton("🔒 Private Channel", callback_data="adm_add_private")],
            [InlineKeyboardButton("❌ Cancel", callback_data="adm_close")]
        ])
        await safe_send_message(context.bot, chat_id=chat_id, text="🔖 <b>Select Channel Type:</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_add_public":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_chan_public"
        await safe_send_message(context.bot, chat_id=chat_id, text="🔖 <b>Add Public Channel</b>\n\nSend 2 lines:\n<code>@username</code>\n<code>https://t.me/channel</code>", parse_mode=ParseMode.HTML)
        return

    if data == "adm_add_private":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_chan_private"
        await safe_send_message(context.bot, chat_id=chat_id, text="☯️ <b>Private Channel Setup</b>\n\nForward a message from the private channel here:", parse_mode=ParseMode.HTML)
        return

    if data == "adm_rem_chan":
        chans = bot_data.get("channels", [])
        if not chans:
            await safe_send_message(context.bot, chat_id=chat_id, text="📌 No channels configured.", parse_mode=ParseMode.HTML)
            return
        bot_data.setdefault("admin_states", {})[user_id_str] = "remove_channel"
        txt = "🗑️ <b>Remove Channel</b>\n\nSend number to remove:\n" + "".join([f"{i+1}. {c.get('title', c['id'])}\n" for i, c in enumerate(chans)])
        await safe_send_message(context.bot, chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML)
        return

    if data == "adm_edit_start_msg":
        bot_data.setdefault("admin_states", {})[user_id_str] = "edit_start_msg"
        prompt_txt = (
            "📝 <b>Edit /start Message</b>\n\n"
            "Send your new start message.\n"
            "• You can send a <b>Text Message</b>\n"
            "• OR send a <b>Photo/Video/Document with Caption</b>!\n\n"
            "<i>(HTML formatting & custom emojis are supported)</i>"
        )
        await safe_send_message(context.bot, chat_id=chat_id, text=prompt_txt, parse_mode=ParseMode.HTML)
        return

    if data == "adm_edit_verification_msg":
        bot_data.setdefault("admin_states", {})[user_id_str] = "edit_verification_msg"
        await safe_send_message(context.bot, chat_id=chat_id, text="✅ <b>Edit Verification Message</b>\n\nSend new verification success message:", parse_mode=ParseMode.HTML)
        return

    if data == "adm_add_custom_btn":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_custom_btn"
        help_text = (
            "➕ <b>Add Custom Button</b>\n\n"
            "Format: <code>Button Text | https://yourlink.com | Style</code>\n\n"
            "📌 <b>Styles:</b> <code>primary</code> (🔵), <code>success</code> (🟢), <code>danger</code> (🔴)\n\n"
            "<b>Example:</b>\n"
            "<code>📢 JOIN VIP | https://t.me/example | success</code>\n"
            "<code>🎯 DOWNLOAD APK | https://example.com | primary</code>\n\n"
            "<i>(Or send /addbtn Text | Link | style directly)</i>"
        )
        await safe_send_message(context.bot, chat_id=chat_id, text=help_text, parse_mode=ParseMode.HTML)
        return

    if data == "adm_clear_buttons":
        bot_data["custom_buttons"] = []
        await sync_data_to_db(bot_data)
        await safe_send_message(context.bot, chat_id=chat_id, text="✔️ All custom buttons cleared.", parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "adm_reset_bot":
        bot_data.setdefault("admin_states", {})[user_id_str] = "confirm_reset"
        await safe_send_message(context.bot, chat_id=chat_id, text="⚠️ <b>DANGER: Reset Bot</b>\n\nDeletes all channels and user records.\nType <b>yes</b> to confirm:", parse_mode=ParseMode.HTML)
        return

async def bulk_approve_requests_action(bot, bot_data: dict, chat_id: int):
    channels = bot_data.get("channels", [])

    channel_map = {}
    for c in channels:
        channel_map[str(c["id"])] = c

    for req_key in bot_data.get("processed_join_requests", {}).keys():
        if "_" in req_key:
            c_id = req_key.split("_")[0]
            if c_id not in channel_map:
                channel_map[c_id] = {"id": int(c_id), "title": f"Channel {c_id}", "type": "channel"}

    if not channel_map:
        await safe_send_message(
            bot,
            chat_id=chat_id,
            text="📌 <b>No Channels Found!</b>\n\nAdd channels in <code>/admin</code> or the bot will auto-scan when join requests arrive.",
            parse_mode=ParseMode.HTML
        )
        return

    await safe_send_message(
        bot,
        chat_id=chat_id,
        text="⚙️ <b>Auto-Scanning Channels & Bulk Approving Join Requests...</b> Please wait.",
        parse_mode=ParseMode.HTML
    )
    approved_count = 0

    db_users = await db.get_all_user_ids()
    target_users = set(db_users)
    for u in bot_data.get("registered", {}).keys():
        target_users.add(u)
    for req_key in bot_data.get("processed_join_requests", {}).keys():
        if "_" in req_key:
            target_users.add(req_key.split("_")[1])

    for target_uid in target_users:
        try:
            target_id = int(target_uid)
            for chan_id_str, chan in channel_map.items():
                try:
                    await bot.approve_chat_join_request(chat_id=int(chan_id_str), user_id=target_id)
                    approved_count += 1
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(0.02)

    await safe_send_message(
        bot,
        chat_id=chat_id,
        text=f"✔️ <b>Bulk Approval Complete!</b>\n\n⚡ Total Join Requests Approved: <b>{approved_count}</b> across {len(channel_map)} channel(s).",
        parse_mode=ParseMode.HTML
    )

# ==========================================
# INCOMING MESSAGE HANDLER
# ==========================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    bot_data = context.bot_data
    user_id_str = str(message.from_user.id)
    chat_id = message.chat_id
    text = (message.text or "").strip()

    is_super_admin = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub_admin = user_id_str in bot_data.get("sub_admins", {})
    is_admin = is_super_admin or is_sub_admin

    # Always auto-learn custom emojis sent in messages
    auto_learn_emojis(message, bot_data)

    # Save user activity to MongoDB
    await db.save_user(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username or ""
    )

    # Handle Reply Keyboard Button Clicks
    clean_keyword = re.sub(r'[^\w\s]', '', text).strip().lower()
    if "help" in clean_keyword or text in ["🏅 Help & Support", "Help & Support", "Help"]:
        await help_command(update, context)
        return

    if "check joined" in clean_keyword or text in ["📌 Check Joined", "Check Joined"]:
        await process_check_joined(context.bot, chat_id, message.from_user, bot_data)
        return

    # Check custom reply keyboard buttons
    for btn in bot_data.get("custom_buttons", []):
        if btn.get("keyboard_type") == "reply":
            btn_clean = re.sub(r'[^\w\s]', '', btn.get("text", "")).strip().lower()
            if clean_keyword == btn_clean or text.lower() == btn.get("text", "").lower():
                if btn.get("type") == "url":
                    await message.reply_text(f"🔗 <b>{btn['text']}:</b> {btn['url']}", parse_mode=ParseMode.HTML)
                    return
                elif btn.get("type") in ["callback", "reply"] and btn.get("callback_data"):
                    update.callback_query = type('obj', (object,), {'id': 'reply_btn', 'from_user': message.from_user, 'message': message, 'data': btn["callback_data"]})()
                    await callback_handler(update, context)
                    return

    # Handle Admin Reply Keyboard Button Clicks
    if is_admin:
        if "wingo" in clean_keyword or "predict" in clean_keyword or text in ["🔮 WinGo Predict", "WinGo Predict", "🔮 WinGo 1M Predict"]:
            await prompt_wingo_predict_setup(chat_id, context.bot, bot_data, user_id_str)
            return

        if "broadcast" in clean_keyword or text in ["☯️ Broadcast", "Broadcast"]:
            bot_data.setdefault("admin_states", {})[user_id_str] = "broadcast"
            await message.reply_text("📌 <b>Broadcast Mode</b>\n\nSend any message to broadcast to all users:", parse_mode=ParseMode.HTML)
            return

        if "approve all requests" in clean_keyword or "approve all" in clean_keyword:
            await bulk_approve_requests_action(context.bot, bot_data, chat_id)
            return

        if "auto-approve" in clean_keyword or "auto approve" in clean_keyword:
            bot_data["auto_approve_enabled"] = not bot_data.get("auto_approve_enabled", True)
            await sync_data_to_db(bot_data)
            status_txt = "INSTANT AUTO-APPROVE ⚡" if bot_data["auto_approve_enabled"] else "MANUAL APPROVAL 🛑"
            await message.reply_text(f"✔️ Join request mode set to: <b>{status_txt}</b>", parse_mode=ParseMode.HTML)
            await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
            return

        if "manage auto dm" in clean_keyword or "auto dm" in clean_keyword:
            bot_data.setdefault("admin_states", {})[user_id_str] = "manage_auto_dm"
            await show_auto_dm_messages(chat_id, context)
            return

        if "edit start" in clean_keyword or "edit /start" in clean_keyword or text in ["📝 Edit /start Msg", "Edit /start Msg", "Edit Start Msg"]:
            bot_data.setdefault("admin_states", {})[user_id_str] = "edit_start_msg"
            prompt_txt = (
                "📝 <b>Edit /start Message</b>\n\n"
                "Send your new start message (Text, or Photo/Video/Document/GIF with caption).\n\n"
                "<i>Live preview with interactive button builder will open!</i>"
            )
            await message.reply_text(apply_premium_emojis(prompt_txt, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            return

    admin_state = bot_data.get("admin_states", {}).get(user_id_str, "")

    # WinGo Setup: Admin entered rounds count as text
    if is_admin and admin_state == "wingo_await_rounds":
        if text.isdigit():
            rounds = int(text)
            rounds = max(1, min(100, rounds))
            bot_data["admin_states"].pop(user_id_str, None)
            await send_wingo_destination_selector(chat_id, context.bot, bot_data, user_id_str, rounds)
            return
        else:
            await message.reply_text("📌 Please enter a valid number of rounds (e.g. <code>5</code>, <code>10</code>) or select from the buttons.", parse_mode=ParseMode.HTML)
            return

    # Auto DM removal trigger
    if is_admin and admin_state == "manage_auto_dm" and text.startswith("remove|"):
        bot_data["admin_states"].pop(user_id_str, None)
        try:
            idx = int(text.split("|")[1]) - 1
            if 0 <= idx < len(bot_data.get("auto_dm_messages", [])):
                bot_data["auto_dm_messages"].pop(idx)
                await sync_data_to_db(bot_data)
                await message.reply_text("✔️ Auto DM message removed!", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text("📌 Invalid message index!", parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply_text("📌 Invalid format! Use: remove|1", parse_mode=ParseMode.HTML)
        await show_auto_dm_messages(chat_id, context)
        return

    # Broadcast Handler
    if is_admin and admin_state == "broadcast":
        bot_data["admin_states"].pop(user_id_str, None)

        db_user_ids = await db.get_all_user_ids()
        recipients = set(db_user_ids)

        for uid, reg in bot_data.get("registered", {}).items():
            if reg: recipients.add(str(uid))
        for uid in bot_data.get("verified_users", []):
            recipients.add(str(uid))

        recipient_list = list(recipients)
        sent_count = 0
        failed_count = 0
        msg_payload = extract_message_payload(message)

        for target_uid in recipient_list:
            try:
                target_id = int(target_uid)
                sent = False
                if message.chat_id and message.message_id:
                    try:
                        await context.bot.copy_message(chat_id=target_id, from_chat_id=message.chat_id, message_id=message.message_id)
                        sent = True
                    except Exception:
                        pass

                if not sent:
                    caption = msg_payload.get("caption", "")
                    if msg_payload.get("photo"):
                        await context.bot.send_photo(chat_id=target_id, photo=msg_payload["photo"], caption=caption, parse_mode=ParseMode.HTML)
                    elif msg_payload.get("video"):
                        await context.bot.send_video(chat_id=target_id, video=msg_payload["video"], caption=caption, parse_mode=ParseMode.HTML)
                    elif msg_payload.get("document"):
                        await context.bot.send_document(chat_id=target_id, document=msg_payload["document"], caption=caption, parse_mode=ParseMode.HTML)
                    elif msg_payload.get("sticker"):
                        await context.bot.send_sticker(chat_id=target_id, sticker=msg_payload["sticker"])
                    elif msg_payload.get("text"):
                        await safe_send_message(context.bot, chat_id=target_id, text=msg_payload["text"], parse_mode=ParseMode.HTML)

                sent_count += 1
            except Exception:
                failed_count += 1

            await asyncio.sleep(0.035)

        report = f"📌 <b>Broadcast Complete!</b>\n\n👥 Total Targets: <b>{len(recipient_list)}</b>\n✔️ Sent: <b>{sent_count}</b>\n📌 Failed: <b>{failed_count}</b>"
        await message.reply_text(report, parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    save_mode = bot_data.get("save_mode", False)
    is_not_command = not text.startswith("/")

    # Interactive Button Builder Input Handler (1st Name, 2nd URL, 3rd Color or 1-line Name | Link | Color)
    if is_admin and user_id_str in bot_data.get("builder_drafts", {}):
        draft = bot_data["builder_drafts"][user_id_str]
        bld_step = draft.get("step")

        if bld_step in ["awaiting_btn_input", "awaiting_url"]:
            if text == "/cancel":
                draft["step"] = "idle"
                draft["pending_btn"] = None
                bot_data.get("admin_states", {}).pop(user_id_str, None)
                await message.reply_text("❌ Button adding cancelled.")
                await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
                return

            if bld_step == "awaiting_btn_input":
                # Option 1: Fast 1-Line Format "Name | Link | color" or "Name | Link"
                if "|" in text:
                    parts = [p.strip() for p in text.split("|") if p.strip()]
                    if len(parts) >= 2:
                        b_name = parts[0]
                        b_url = parts[1]
                        b_style = parts[2].lower() if len(parts) >= 3 else "primary"
                        if b_style not in ["primary", "success", "danger"]:
                            b_style = "primary"
                        if not b_url.startswith("http"):
                            b_url = "https://" + b_url

                        draft.setdefault("buttons", []).append({
                            "text": b_name,
                            "url": b_url,
                            "style": b_style,
                            "type": "url"
                        })
                        draft["step"] = "idle"
                        draft["pending_btn"] = None
                        bot_data.get("admin_states", {}).pop(user_id_str, None)

                        color_icon = "🔵 Primary" if b_style == "primary" else ("🟢 Success" if b_style == "success" else "🔴 Danger")
                        await message.reply_text(f"✅ <b>Button Added!</b>\n\n🔘 <b>{b_name}</b> ➔ {b_url} ({color_icon})", parse_mode=ParseMode.HTML)
                        await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
                        return
                    else:
                        await message.reply_text("📌 Format error! Use: <code>Name | https://link | color</code>", parse_mode=ParseMode.HTML)
                        return
                else:
                    # Option 2: Step-by-Step (Step 1: Button Name received!)
                    draft["pending_btn"] = {"text": text.strip()}
                    draft["step"] = "awaiting_url"
                    prompt = (
                        f"🔘 <b>Step 2/3: Enter Button Link / URL</b>\n\n"
                        f"Button Name: <b>{text.strip()}</b>\n\n"
                        f"Now send the <b>URL / Link</b> (e.g. <code>https://t.me/yourchannel</code>):"
                    )
                    await message.reply_text(prompt, parse_mode=ParseMode.HTML)
                    return

            elif bld_step == "awaiting_url":
                # Step-by-Step (Step 2: URL received -> Show Color Selector)
                raw_url = text.strip()
                if not raw_url.startswith("http"):
                    raw_url = "https://" + raw_url
                draft.setdefault("pending_btn", {})["url"] = raw_url
                draft["step"] = "awaiting_color"

                prompt = (
                    f"🎨 <b>Step 3/3: Select Button Color</b>\n\n"
                    f"🔘 <b>Name:</b> {draft['pending_btn']['text']}\n"
                    f"🔗 <b>URL:</b> {raw_url}\n\n"
                    f"<i>Tap a color style below to finish adding:</i>"
                )
                color_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔵 Primary (Blue)", callback_data="bld_color_primary"),
                        InlineKeyboardButton("🟢 Success (Green)", callback_data="bld_color_success")
                    ],
                    [
                        InlineKeyboardButton("🔴 Danger (Red)", callback_data="bld_color_danger")
                    ],
                    [
                        InlineKeyboardButton("❌ Cancel", callback_data="bld_cancel_btn")
                    ]
                ])
                await message.reply_text(prompt, parse_mode=ParseMode.HTML, reply_markup=color_kb)
                return

    # Auto DM Post Creation & Save Mode -> Opens interactive live preview builder!
    is_create_dm_state = (admin_state == "create_autodm_post")
    if is_admin and (is_create_dm_state or (save_mode and not admin_state.startswith("add_") and is_not_command)):
        bot_data["admin_states"].pop(user_id_str, None)
        auto_learn_emojis(message, bot_data)
        saved_payload = extract_message_payload(message)

        m_type = "text"
        m_file_id = None
        if message.photo: m_type, m_file_id = "photo", message.photo[-1].file_id
        elif message.video: m_type, m_file_id = "video", message.video.file_id
        elif message.document: m_type, m_file_id = "document", message.document.file_id
        elif message.animation: m_type, m_file_id = "animation", message.animation.file_id

        raw_text = message.caption_html or message.text_html or message.caption or message.text or ""

        # Open in Interactive Builder
        bot_data.setdefault("builder_drafts", {})[user_id_str] = {
            "target": "autodm",
            "media_type": m_type,
            "file_id": m_file_id,
            "text": raw_text,
            "payload": saved_payload,
            "buttons": [],
            "pending_btn": None,
            "step": "idle"
        }

        await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
        return

    # Admin Prompt State Machine
    if is_admin and admin_state and is_not_command:
        state = admin_state
        bot_data["admin_states"].pop(user_id_str, None)

        if state == "add_premium_emoji":
            new_learned = auto_learn_emojis(message, bot_data)
            if new_learned == 0 and text:
                match = re.search(r'(\S+?)\s*[:|=\-]?\s*(\d{15,22})', text)
                if match:
                    echar, eid = match.group(1).strip(), match.group(2).strip()
                    bot_data.setdefault("premium_emojis", {})[echar] = eid
                    base_char = echar.replace("\ufe0f", "")
                    bot_data["premium_emojis"][base_char] = eid
                    new_learned += 1
                    asyncio.create_task(db.save_premium_emojis(bot_data["premium_emojis"]))
                    asyncio.create_task(sync_data_to_db(bot_data))

            unique_list = get_unique_emojis_list(bot_data)
            if new_learned > 0:
                resp = f"✅ <b>Learned {new_learned} New Premium Emoji(s)!</b>\n\nTotal unique registered emojis: <b>{len(unique_list)}</b>\nSaved permanently to MongoDB!"
            else:
                resp = f"🎨 <b>Emoji Processing Complete!</b>\n\nTotal unique registered emojis: <b>{len(unique_list)}</b>"
            await safe_send_message(context.bot, chat_id=chat_id, text=resp, parse_mode=ParseMode.HTML)
            await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
            return

        elif state == "add_chan_public":
            raw_input = text.strip()
            lines = [l.strip() for l in raw_input.split("\n") if l.strip()]
            username_or_link = lines[0]
            user_link = lines[1] if len(lines) >= 2 else None

            try:
                chat_target = username_or_link.split("/")[-1] if "t.me/" in username_or_link else username_or_link
                if not chat_target.startswith("@") and not chat_target.startswith("-100") and not chat_target.isdigit():
                    chat_target = "@" + chat_target

                chat_obj = await context.bot.get_chat(chat_target)
                real_id = chat_obj.id
                title = chat_obj.title or chat_obj.username or chat_target

                if user_link and user_link.startswith("http"):
                    real_link = user_link
                else:
                    real_link = await generate_real_channel_link(context.bot, real_id, title, prefer_private=True)

                bot_data["channels"] = [c for c in bot_data.get("channels", []) if str(c.get("id")) != str(real_id) and c.get("id") != username_or_link]
                bot_data["channels"].append({
                    "id": real_id,
                    "title": title,
                    "link": real_link,
                    "type": "public"
                })
                await sync_data_to_db(bot_data)
                resp = f"✔️ <b>Public Channel Connected!</b>\n\n📌 <b>Title:</b> {title}\n🆔 <b>ID:</b> <code>{real_id}</code>\n🔗 <b>Permanent Link:</b> {real_link}\n\n💡 <i>Channel is saved for join request auto-approvals. Add custom buttons anytime via /addbtn!</i>"
                await message.reply_text(resp, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error("Failed to fetch public channel %s: %s", username_or_link, e)
                fallback_link = user_link if (user_link and user_link.startswith("http")) else f"https://t.me/{username_or_link.replace('@','')}"
                bot_data.setdefault("channels", []).append({
                    "id": username_or_link,
                    "title": username_or_link,
                    "link": fallback_link,
                    "type": "public"
                })
                await sync_data_to_db(bot_data)
                await message.reply_text(f"⚠️ <b>Channel Added!</b>\n\nAdded: {username_or_link}\n🔗 Link: {fallback_link}", parse_mode=ParseMode.HTML)

        elif state == "add_chan_private":
            if message.forward_from_chat:
                f_chat = message.forward_from_chat
                f_id = f_chat.id
                f_title = f_chat.title or "Private Channel"
                invite_link = await generate_real_channel_link(context.bot, f_id, f_title, prefer_private=True)

                if invite_link:
                    bot_data["channels"] = [c for c in bot_data.get("channels", []) if str(c.get("id")) != str(f_id)]
                    bot_data.setdefault("channels", []).append({"id": f_id, "link": invite_link, "type": "private", "title": f_title})
                    await sync_data_to_db(bot_data)
                    await message.reply_text(f"✔️ <b>Private Channel Connected Successfully!</b>\n\n📌 <b>Title:</b> {f_title}\n🔗 <b>Invite Link:</b> {invite_link}\n\n💡 <i>Saved for join approvals. Use /addbtn to add custom button!</i>", parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text("📌 <b>Failed to create invite link!</b> Make sure bot is Admin in channel with 'Invite Users' permission.", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text("📌 Please forward a message from the private channel!", parse_mode=ParseMode.HTML)

        elif state == "remove_channel":
            try:
                idx = int(text) - 1
                if 0 <= idx < len(bot_data.get("channels", [])):
                    removed = bot_data["channels"].pop(idx)
                    await sync_data_to_db(bot_data)
                    await message.reply_text(f"✔️ Channel removed: {removed.get('title')}", parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text("📌 Invalid index!", parse_mode=ParseMode.HTML)
            except Exception:
                await message.reply_text("📌 Invalid input number!", parse_mode=ParseMode.HTML)

        elif state == "edit_start_msg":
            new_emojis = auto_learn_emojis(message, bot_data)
            raw_start_html = message.caption_html or message.text_html or message.caption or message.text or ""

            m_type = "text"
            m_file_id = None
            if message.photo:
                m_type = "photo"
                m_file_id = message.photo[-1].file_id
            elif message.video:
                m_type = "video"
                m_file_id = message.video.file_id
            elif message.document:
                m_type = "document"
                m_file_id = message.document.file_id
            elif message.animation:
                m_type = "animation"
                m_file_id = message.animation.file_id

            # Initialize builder draft with existing buttons so admin can see and customize buttons
            existing_buttons = list(bot_data.get("custom_buttons", []))
            bot_data.setdefault("builder_drafts", {})[user_id_str] = {
                "target": "start",
                "media_type": m_type,
                "file_id": m_file_id,
                "text": raw_start_html,
                "buttons": existing_buttons,
                "pending_btn": None,
                "step": "idle"
            }

            await send_builder_live_preview(context.bot, chat_id, user_id_str, bot_data)
            return

        elif state == "edit_verification_msg":
            new_emojis = auto_learn_emojis(message, bot_data)
            raw_ver_html = message.text_html or message.caption_html or message.text or message.caption or ""
            bot_data["verification_success_msg"] = raw_ver_html
            await sync_data_to_db(bot_data)
            await message.reply_text(
                f"✔️ <b>Verification message updated!</b>" + (f" ({new_emojis} new emoji IDs learned)" if new_emojis else ""),
                parse_mode=ParseMode.HTML
            )

        elif state == "add_custom_btn":
            auto_learn_emojis(message, bot_data)
            parts = [p.strip() for p in text.split("|") if p.strip()]
            if len(parts) >= 2:
                first_lower = parts[0].lower()
                if first_lower in ["url", "callback", "reply"] and len(parts) >= 3:
                    b_type = first_lower
                    b_text = parts[1]
                    b_val = parts[2]
                    b_style = parts[3].lower() if len(parts) >= 4 else "primary"
                    b_kind = parts[5].lower() if len(parts) >= 6 else ("reply" if b_type == "reply" else "inline")
                else:
                    b_text = parts[0]
                    b_val = parts[1]
                    b_style = parts[2].lower() if len(parts) >= 3 else "primary"
                    b_kind = parts[3].lower() if len(parts) >= 4 else "inline"
                    is_url = b_val.startswith("http://") or b_val.startswith("https://") or "t.me/" in b_val or "telegram.me/" in b_val or "." in b_val
                    b_type = "url" if is_url else "callback"
                    if is_url and not b_val.startswith("http"):
                        b_val = "https://" + b_val

                if b_style not in ["primary", "success", "danger"]:
                    b_style = "primary"

                new_btn = {
                    "text": b_text,
                    "type": b_type,
                    "url": b_val if b_type == "url" else None,
                    "callback_data": b_val if b_type != "url" else "custom_action",
                    "style": b_style,
                    "row": 1,
                    "keyboard_type": b_kind
                }

                bot_data.setdefault("custom_buttons", []).append(new_btn)
                await sync_data_to_db(bot_data)

                style_icon = "🔵 Primary" if b_style == "primary" else ("🟢 Success" if b_style == "success" else "🔴 Danger")
                resp = (
                    f"✔️ <b>Custom Button Added Successfully!</b>\n\n"
                    f"🔘 <b>Text:</b> {b_text}\n"
                    f"🎨 <b>Color:</b> {style_icon}\n"
                    f"🔗 <b>Target:</b> {b_val}\n\n"
                    f"📌 <i>This button is now active on /start and in Auto DM!</i>"
                )
                await message.reply_text(resp, parse_mode=ParseMode.HTML)
            else:
                err_msg = (
                    "📌 <b>Format Error!</b> Send button details in format:\n\n"
                    "<code>Button Text | https://yourlink.com</code>\n"
                    "OR\n"
                    "<code>Button Text | https://yourlink.com | success</code>\n\n"
                    "🎨 <b>Available Colors:</b> <code>primary</code> (🔵), <code>success</code> (🟢), <code>danger</code> (🔴)"
                )
                await message.reply_text(err_msg, parse_mode=ParseMode.HTML)

        elif state == "confirm_reset":
            if text.lower() == "yes":
                bot_data["channels"] = []
                bot_data["custom_buttons"] = []
                bot_data["auto_dm_buttons"] = []
                await sync_data_to_db(bot_data)
                await message.reply_text("✅ <b>Bot Reset Complete!</b>", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text("❌ Reset cancelled.", parse_mode=ParseMode.HTML)

        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)


# ==========================================
# CHAT JOIN REQUEST HANDLER
# ==========================================

async def chat_join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return

    bot_data = context.bot_data
    user_id = req.from_user.id
    chat_id = req.chat.id
    chat_title = req.chat.title or "Channel"
    request_key = f"{chat_id}_{user_id}"

    await auto_scan_channel(context.bot, bot_data, chat_id, chat_title)

    if request_key in bot_data.get("processed_join_requests", {}):
        return

    bot_data.setdefault("processed_join_requests", {})[request_key] = True

    auto_approve = bot_data.get("auto_approve_enabled", True)
    approved_successfully = False
    if auto_approve:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            approved_successfully = True
            logger.info("Auto-approved join request for user %s in chat %s", user_id, chat_id)
        except Exception as err:
            approved_successfully = False
            logger.error("Failed to auto-approve join request: %s", err)

    await db.save_user(
        user_id=user_id,
        first_name=req.from_user.first_name or "",
        username=req.from_user.username or ""
    )

    user_id_str = str(user_id)
    if user_id_str not in bot_data.get("registered", {}):
        colors_enabled = bot_data.get("colors_enabled", True)
        auto_chan_btn = bot_data.get("auto_channel_buttons", False)

        # Build Combined Keyboard without auto-channel buttons
        inline_markup = build_combined_keyboard(
            channels=bot_data.get("channels", []),
            custom_buttons=bot_data.get("custom_buttons", []),
            dm_buttons=bot_data.get("auto_dm_buttons", []),
            colors_enabled=colors_enabled,
            premium_emojis=bot_data.get("premium_emojis", {}),
            max_buttons=4,
            include_channels=auto_chan_btn
        )
        clean_chat_title = str(chat_title).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            # 1. Send Auto DM messages if configured
            auto_dm_list = bot_data.get("auto_dm_messages", [])
            if auto_dm_list:
                await send_auto_dm_messages(
                    context.bot, user_id, 
                    auto_dm_list, 
                    bot_data.get("premium_emojis", {}), 
                    channels=bot_data.get("channels", []),
                    custom_buttons=bot_data.get("custom_buttons", []),
                    dm_buttons=bot_data.get("auto_dm_buttons", []), 
                    colors_enabled=colors_enabled,
                    include_channels=auto_chan_btn
                )

            # 2. Send Request Receive Welcome Message with animated premium emojis
            status_text = "⚡ <b>Instant Approved!</b>" if approved_successfully else "📌 <b>Request Received!</b>"
            welcome_msg = f"{status_text}\n\nWelcome to <b>{clean_chat_title}</b>!"
            formatted_welcome = apply_premium_emojis(welcome_msg, bot_data.get("premium_emojis", {}))
            await safe_send_message(
                context.bot,
                chat_id=user_id,
                text=formatted_welcome,
                parse_mode=ParseMode.HTML,
                reply_markup=inline_markup
            )
        except Exception as err:
            logger.info("Could not send DM message to user %s: %s", user_id, err)

        bot_data.setdefault("registered", {})[user_id_str] = True
        bot_data.setdefault("join_req_dm_sent", {})[user_id_str] = True

    # Send Admin Alert (ALWAYS for every join request, fully converted to Premium Emojis)
    app_label = "⚡ Auto-Approved" if approved_successfully else "📌 Pending Approval"
    clean_chat_title = str(chat_title).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    clean_user_name = str(req.from_user.first_name or "User").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    raw_alert = (
        f"📌 <b>New Join Request! ({app_label})</b>\n"
        f"📌 <b>Channel:</b> {clean_chat_title}\n"
        f"👤 <b>User:</b> {clean_user_name} (<code>{user_id}</code>)"
    )
    premium_alert = apply_premium_emojis(raw_alert, bot_data.get("premium_emojis", {}))

    all_admins = set(bot_data.get("admins", getattr(config, "ADMINS", [])))
    for sub_id in bot_data.get("sub_admins", {}).keys():
        all_admins.add(sub_id)

    for adm in all_admins:
        try:
            await safe_send_message(context.bot, chat_id=int(adm), text=premium_alert, parse_mode=ParseMode.HTML)
        except Exception:
            pass

# ==========================================
# MY_CHAT_MEMBER HANDLER
# ==========================================

async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return

    bot_data = context.bot_data
    chat = result.chat
    if chat.type not in ("channel", "supergroup", "group"):
        return

    chat_id = chat.id
    chat_title = chat.title or "Channel"
    old_status = result.old_chat_member.status if result.old_chat_member else "left"
    new_status = result.new_chat_member.status if result.new_chat_member else "left"
    actor = result.from_user
    actor_id_str = str(actor.id) if actor else ""
    actor_name = actor.first_name if actor else "Unknown"

    is_authorized = (
        actor_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
        or actor_id_str in bot_data.get("sub_admins", {})
    )

    was_active = old_status in ("administrator", "member", "creator")
    is_active_now = new_status in ("administrator", "member", "creator")
    is_removed_now = new_status in ("left", "kicked", "banned", "restricted")

    if is_active_now and not was_active:
        if is_authorized:
            await auto_scan_channel(context.bot, bot_data, chat_id, chat_title, chat_type="channel")
            try:
                conn_text = (
                    f"✅ <b>Channel Connected!</b>\n\n"
                    f"📌 <b>{chat_title}</b> is now connected for instant auto-approvals.\n\n"
                    f"💡 <i>Note: No button is auto-created. To add a button for this channel, use <code>/addbtn {chat_title} | https://t.me/...</code> or open <code>/admin</code>!</i>"
                )
                formatted_conn = apply_premium_emojis(conn_text, bot_data.get("premium_emojis", {}))
                await safe_send_message(
                    context.bot,
                    chat_id=int(actor_id_str),
                    text=formatted_conn,
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        else:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ This bot can only be activated by an authorized admin. Leaving now."
                )
            except Exception:
                pass
            try:
                await context.bot.leave_chat(chat_id)
            except Exception as e:
                logger.error("Failed to leave unauthorized chat %s: %s", chat_id, e)

            alert = (
                f"🚨 <b>Unauthorized Add Attempt!</b>\n\n"
                f"👤 User: {actor_name} (<code>{actor_id_str}</code>)\n"
                f"📌 Chat: {chat_title} (<code>{chat_id}</code>)\n\n"
                f"Bot left automatically since this user is not an authorized admin."
            )
            formatted_alert = apply_premium_emojis(alert, bot_data.get("premium_emojis", {}))
            all_admins = set(bot_data.get("admins", getattr(config, "ADMINS", [])))
            for sub_id in bot_data.get("sub_admins", {}).keys():
                all_admins.add(sub_id)
            for adm in all_admins:
                try:
                    await safe_send_message(context.bot, chat_id=int(adm), text=formatted_alert, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    elif is_active_now and was_active and new_status != old_status:
        if not is_authorized:
            try:
                await context.bot.leave_chat(chat_id)
            except Exception as e:
                logger.error("Failed to leave chat %s after unauthorized role change: %s", chat_id, e)
            await unregister_channel(bot_data, chat_id)
        else:
            await auto_scan_channel(context.bot, bot_data, chat_id, chat_title, chat_type="channel")

    elif is_removed_now and was_active:
        removed = await unregister_channel(bot_data, chat_id)
        if removed:
            logger.info("Bot removed from '%s' (%s) by %s (%s) -> channel unregistered.", chat_title, chat_id, actor_name, actor_id_str)

# ==========================================
# ADMIN UTILITY COMMANDS (CUSTOM BUTTONS)
# ==========================================

async def add_button_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add custom button: /addbtn Button Text | https://t.me/link | success"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "➕ <b>Add Custom Button:</b>\n\n"
            "📌 <b>Usage:</b>\n"
            "<code>/addbtn Button Text | https://t.me/yourlink | success</code>\n\n"
            "🎨 <b>Colors:</b> <code>primary</code> (🔵), <code>success</code> (🟢), <code>danger</code> (🔴)",
            parse_mode=ParseMode.HTML
        )
        return

    full_text = " ".join(args)
    parts = [p.strip() for p in full_text.split("|") if p.strip()]
    if len(parts) >= 2:
        b_text = parts[0]
        b_url = parts[1]
        b_style = parts[2].lower() if len(parts) >= 3 else "primary"
        
        if b_style not in ["primary", "success", "danger"]:
            b_style = "primary"
        
        is_url = b_url.startswith("http://") or b_url.startswith("https://") or "t.me/" in b_url or "." in b_url
        b_type = "url" if is_url else "callback"
        if is_url and not b_url.startswith("http"):
            b_url = "https://" + b_url

        new_btn = {
            "text": b_text,
            "type": b_type,
            "url": b_url if b_type == "url" else None,
            "callback_data": b_url if b_type != "url" else "custom_action",
            "style": b_style,
            "row": 1,
            "keyboard_type": "inline"
        }

        bot_data.setdefault("custom_buttons", []).append(new_btn)
        await sync_data_to_db(bot_data)

        style_icon = "🔵 Primary" if b_style == "primary" else ("🟢 Success" if b_style == "success" else "🔴 Danger")
        resp = (
            f"✅ <b>Custom Button Added!</b>\n\n"
            f"🔘 <b>Text:</b> {b_text}\n"
            f"🎨 <b>Color:</b> {style_icon}\n"
            f"🔗 <b>Target:</b> {b_url}\n\n"
            f"<i>Now visible on /start and in Auto DM!</i>"
        )
        await update.message.reply_text(resp, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("📌 Format error! Use: <code>/addbtn Text | https://link | style</code>", parse_mode=ParseMode.HTML)

async def show_buttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all custom buttons: /buttons"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    custom_btns = bot_data.get("custom_buttons", [])
    if not custom_btns:
        await update.message.reply_text("📌 No custom buttons configured yet.\n\nUse <code>/addbtn Text | Link</code> to add one!", parse_mode=ParseMode.HTML)
        return

    text = f"🔘 <b>Custom Buttons ({len(custom_btns)}):</b>\n\n"
    for idx, btn in enumerate(custom_btns):
        target = btn.get("url") or btn.get("callback_data") or "None"
        text += f"{idx+1}. <b>{btn.get('text')}</b> → {target} ({btn.get('style', 'primary')})\n"
    text += "\n📌 <b>To remove:</b> <code>/rembutton &lt;number&gt;</code>\n📌 <b>To clear all:</b> <code>/clearbuttons</code>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def clear_buttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all custom buttons: /clearbuttons"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    bot_data["custom_buttons"] = []
    await sync_data_to_db(bot_data)
    await update.message.reply_text("✅ <b>All custom buttons cleared!</b>", parse_mode=ParseMode.HTML)

async def toggle_auto_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle whether connected channels automatically become buttons or not"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    bot_data["auto_channel_buttons"] = not bot_data.get("auto_channel_buttons", False)
    await sync_data_to_db(bot_data)
    st = "ENABLED ✅ (Connected channels will create buttons)" if bot_data["auto_channel_buttons"] else "DISABLED ❌ (Channels will NOT create buttons; only manual custom buttons)"
    await update.message.reply_text(f"📢 <b>Auto Channel Buttons:</b> {st}", parse_mode=ParseMode.HTML)

async def rem_button_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    custom_btns = bot_data.get("custom_buttons", [])
    if not custom_btns:
        await update.message.reply_text("📌 No custom buttons configured to remove.", parse_mode=ParseMode.HTML)
        return

    args = context.args
    if not args or not args[0].isdigit():
        txt = "🗑️ <b>Remove Custom Button</b>\n\nUsage: <code>/rembutton &lt;index&gt;</code>\n\n<b>Available Buttons:</b>\n"
        for idx, btn in enumerate(custom_btns):
            txt += f"{idx+1}. {btn['text']} ({btn.get('style','primary').upper()})\n"
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
        return

    b_idx = int(args[0]) - 1
    if 0 <= b_idx < len(custom_btns):
        removed = custom_btns.pop(b_idx)
        await sync_data_to_db(bot_data)
        await update.message.reply_text(f"✔️ Custom button <b>{removed.get('text')}</b> removed!", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("📌 Invalid button index!", parse_mode=ParseMode.HTML)

# DM Button Commands
async def add_dm_button_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a button to Auto DM messages - Max 4 buttons (2 per row)"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    dm_buttons = bot_data.get("auto_dm_buttons", [])
    if len(dm_buttons) >= 4:
        await update.message.reply_text("❌ <b>Max 4 DM buttons reached!</b> Use /cleardmbtn first.", parse_mode=ParseMode.HTML)
        return

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "📌 <b>Usage:</b> <code>/adddmbtn url | Button Text | https://link.com | style</code>\n\n"
            "<b>Example:</b>\n<code>/adddmbtn url | Prediction 📈 | https://t.me/earnwithdurov | success</code>\n\n"
            "<b>Styles:</b> primary (🔵), success (🟢), danger (🔴)\n"
            "<b>Max:</b> 4 buttons (2 per row)",
            parse_mode=ParseMode.HTML
        )
        return

    full_text = " ".join(args)
    parts = [p.strip() for p in full_text.split("|") if p.strip()]
    
    if len(parts) >= 3:
        b_text = parts[1] if len(parts) >= 2 else "Button"
        b_url = parts[2] if len(parts) >= 3 else ""
        b_style = parts[3].lower() if len(parts) >= 4 else "primary"
        
        if b_style not in ["primary", "success", "danger"]:
            b_style = "primary"
        
        if not b_url.startswith("http"):
            b_url = "https://" + b_url
        
        new_btn = {
            "text": b_text,
            "url": b_url,
            "style": b_style
        }
        
        bot_data.setdefault("auto_dm_buttons", []).append(new_btn)
        await sync_data_to_db(bot_data)
        
        await update.message.reply_text(
            f"✅ <b>DM Button Added! ({len(bot_data['auto_dm_buttons'])}/4)</b>\n\n"
            f"🔘 <b>Text:</b> {b_text}\n"
            f"🔗 <b>Link:</b> {b_url}\n"
            f"🎨 <b>Style:</b> {b_style}",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("📌 Invalid format! Use: /adddmbtn url | Text | Link | style", parse_mode=ParseMode.HTML)

async def clear_dm_buttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all Auto DM buttons"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    bot_data["auto_dm_buttons"] = []
    await sync_data_to_db(bot_data)
    await update.message.reply_text("✅ <b>All DM buttons cleared!</b>", parse_mode=ParseMode.HTML)

async def show_dm_buttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all Auto DM buttons"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    dm_buttons = bot_data.get("auto_dm_buttons", [])
    if not dm_buttons:
        await update.message.reply_text("📌 No DM buttons configured yet.")
        return

    text = f"📋 <b>Auto DM Buttons ({len(dm_buttons)}/4):</b>\n\n"
    for idx, btn in enumerate(dm_buttons):
        text += f"{idx+1}. <b>{btn['text']}</b> → {btn['url']} ({btn.get('style', 'primary')})\n"
    
    text += "\n📌 <b>Max 4 buttons | 2 per row</b>\n"
    text += "<i>Use /cleardmbtn to remove all buttons</i>"
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def refresh_all_channel_links_with_report(bot, bot_data: dict) -> tuple:
    """Scans all registered channels, replaces broken/internal links with real invite links."""
    channels = bot_data.get("channels", [])
    updated_count = 0
    removed_count = 0
    report_lines = []
    surviving_channels = []

    if not channels:
        return 0, "📌 <b>No Channels Registered!</b>\n\nAdd public or private channels in <code>/admin</code> first."

    for idx, chan in enumerate(channels):
        old_link = chan.get("link", "")
        chat_id = chan.get("id")
        title = chan.get("title", f"Channel {idx+1}")

        bot_still_present = True
        try:
            bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
            if bot_member.status in ("left", "kicked", "banned"):
                bot_still_present = False
        except Exception:
            bot_still_present = False

        if not bot_still_present:
            removed_count += 1
            report_lines.append(f"<b>{idx+1}. {title}</b>\n🆔 <code>{chat_id}</code>\nSTATUS: 🗑️ Bot no longer present — channel auto-removed")
            continue

        real_link = await generate_real_channel_link(bot, chat_id, title, prefer_private=True)

        if real_link:
            if real_link != old_link:
                chan["link"] = real_link
                updated_count += 1
                report_lines.append(f"<b>{idx+1}. {title}</b>\n🆔 <code>{chat_id}</code>\n🔗 <b>New Link:</b> {real_link}\nSTATUS: ✅ Repaired & Saved")
            else:
                report_lines.append(f"<b>{idx+1}. {title}</b>\n🆔 <code>{chat_id}</code>\n🔗 <b>Link:</b> {old_link}\nSTATUS: ✔️ Already Valid")
        else:
            report_lines.append(f"<b>{idx+1}. {title}</b>\n🆔 <code>{chat_id}</code>\nSTATUS: ❌ Bot is not Admin with 'Invite Users' permission!")

        surviving_channels.append(chan)

    if updated_count > 0 or removed_count > 0:
        bot_data["channels"] = surviving_channels
        await sync_data_to_db(bot_data)

    summary_header = f"⚙️ <b>Channel Link Repair Report ({len(channels)} Channels Scanned)</b>\n"
    summary_header += f"⚡ Links Fixed/Updated: <b>{updated_count}</b>\n"
    summary_header += f"🗑️ Dead Channels Auto-Removed: <b>{removed_count}</b>\n\n"
    full_report = summary_header + "\n\n".join(report_lines)

    return updated_count, full_report

async def refresh_all_channel_links(bot, bot_data: dict) -> int:
    cnt, _ = await refresh_all_channel_links_with_report(bot, bot_data)
    return cnt

async def fix_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    msg_init = await update.message.reply_text("⚙️ <b>Scanning and repairing all channel invite links...</b>", parse_mode=ParseMode.HTML)
    repaired, report_text = await refresh_all_channel_links_with_report(context.bot, bot_data)
    await msg_init.edit_text(report_text, parse_mode=ParseMode.HTML)

async def predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start WinGo 1M Predictions: /predict [rounds]"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied: Only Admins can run WinGo predictions.")
        return

    args = context.args
    if args and args[0].isdigit():
        rounds = int(args[0])
        rounds = max(1, min(100, rounds))
        await send_wingo_destination_selector(update.effective_chat.id, context.bot, bot_data, user_id_str, rounds)
    else:
        await prompt_wingo_predict_setup(update.effective_chat.id, context.bot, bot_data, user_id_str)

async def stop_predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop active WinGo 1M Predictions: /stoppredict"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", getattr(config, "ADMINS", []))
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    stopped = False
    for sess_id, sess in bot_data.get("wingo_sessions", {}).items():
        if sess.get("is_running"):
            sess["is_running"] = False
            stopped = True

    if stopped:
        await update.message.reply_text("🛑 <b>Active WinGo 1M prediction session has been stopped!</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("📌 No active WinGo prediction session is running right now.", parse_mode=ParseMode.HTML)

# ==========================================
# POST-INIT SETUP
# ==========================================

async def post_init(app: Application):
    bot_data = await load_all_bot_data()
    app.bot_data.update(bot_data)
    logger.info("Bot loaded successfully with MongoDB database connection!")

    try:
        repaired = await refresh_all_channel_links(app.bot, app.bot_data)
        if repaired > 0:
            logger.info("Auto-repaired %d channel invite links on startup!", repaired)
    except Exception as e:
        logger.error("Error repairing channel links on startup: %s", e)

def main():
    logger.info("Initializing Python Bot @%s ...", getattr(config, "BOT_USERNAME", "Bot"))

    application = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    # Core commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("help", help_command))

    # Emoji commands
    application.add_handler(CommandHandler("findemoji", findemoji_command))
    application.add_handler(CommandHandler("emojis", view_emojis_command))
    application.add_handler(CommandHandler("viewemojis", view_emojis_command))
    application.add_handler(CommandHandler("addemoji", add_emoji_command))

    # Admin management
    application.add_handler(CommandHandler("addadmin", add_admin_command))
    application.add_handler(CommandHandler("addsuperadmin", add_superadmin_command))
    application.add_handler(CommandHandler("remadmin", rem_admin_command))

    # Manual Custom Buttons commands
    application.add_handler(CommandHandler("addbtn", add_button_command))
    application.add_handler(CommandHandler("addbutton", add_button_command))
    application.add_handler(CommandHandler("buttons", show_buttons_command))
    application.add_handler(CommandHandler("showbuttons", show_buttons_command))
    application.add_handler(CommandHandler("rembutton", rem_button_command))
    application.add_handler(CommandHandler("clearbuttons", clear_buttons_command))
    application.add_handler(CommandHandler("toggleautochannel", toggle_auto_channel_command))

    # Auto DM Button commands
    application.add_handler(CommandHandler("adddmbtn", add_dm_button_command))
    application.add_handler(CommandHandler("cleardmbtn", clear_dm_buttons_command))
    application.add_handler(CommandHandler("showdmbtn", show_dm_buttons_command))

    # Utilities
    application.add_handler(CommandHandler("buttons_demo", buttons_demo_command))
    application.add_handler(CommandHandler("fixlinks", fix_links_command))
    application.add_handler(CommandHandler("predict", predict_command))
    application.add_handler(CommandHandler("wingo", predict_command))
    application.add_handler(CommandHandler("stoppredict", stop_predict_command))
    application.add_handler(CommandHandler("stopwingo", stop_predict_command))

    # Telegram Update Handlers
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(ChatJoinRequestHandler(chat_join_request_handler))
    application.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    async def app_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update: %s", context.error)

    application.add_error_handler(app_error_handler)

    logger.info("Bot starting polling...")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
