import asyncio
import json
import logging
import os
import re

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
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Custom Log Handler for MongoDB
class MongoLogHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        if db.is_connected:
            asyncio.create_task(db.log_event(record.levelname, log_entry))

logger.addHandler(MongoLogHandler())

# ==========================================
# DATA LOADING & PERSISTENCE
# ==========================================

async def load_all_bot_data() -> dict:
    await db.connect()

    cfg = await db.get_bot_config()
    channels = await db.get_channels()
    auto_dm = await db.get_auto_dm_messages()
    buttons = await db.get_custom_buttons()
    emojis = await db.get_premium_emojis()

    # Load access.json for local admin persistence
    access_admins = config.ADMINS
    access_sub_admins = {}
    if os.path.exists(config.ACCESS_FILE):
        try:
            with open(config.ACCESS_FILE, "r", encoding="utf-8") as f:
                acc_data = json.load(f)
                if "admins" in acc_data and acc_data["admins"]:
                    access_admins = acc_data["admins"]
                if "sub_admins" in acc_data:
                    access_sub_admins = acc_data["sub_admins"]
        except Exception as e:
            logger.error("access.json read error: %s", e)

    # Merge JSON local data if MongoDB data empty
    if os.path.exists(config.DATA_FILE):
        try:
            with open(config.DATA_FILE, "r", encoding="utf-8") as f:
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

    merged_admins = list(set(cfg.get("admins", config.ADMINS) + access_admins))
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
        "imageUrl": cfg.get("imageUrl", config.DEFAULT_IMAGE),
        "verification_success_msg": cfg.get("verification_success_msg", config.DEFAULT_VERIFICATION_MSG),
        "save_mode": cfg.get("save_mode", False),
        "colors_enabled": cfg.get("colors_enabled", True),
        "auto_approve_enabled": cfg.get("auto_approve_enabled", True),
        "start_message": cfg.get("start_message", ""),
        "start_media": cfg.get("start_media", None),
        "channels": channels,
        "auto_dm_messages": auto_dm,
        "custom_buttons": sanitized_buttons,
        "premium_emojis": emojis,
        "registered": {},
        "verified_users": [],
        "admin_states": {},
        "processed_join_requests": {},
        "auto_dm_buttons": cfg.get("auto_dm_buttons", [])
    }
    return bot_data

async def sync_data_to_db(bot_data: dict):
    await db.save_bot_config({
        "admins": bot_data.get("admins", config.ADMINS),
        "sub_admins": bot_data.get("sub_admins", {}),
        "imageUrl": bot_data.get("imageUrl", config.DEFAULT_IMAGE),
        "verification_success_msg": bot_data.get("verification_success_msg", config.DEFAULT_VERIFICATION_MSG),
        "save_mode": bot_data.get("save_mode", False),
        "colors_enabled": bot_data.get("colors_enabled", True),
        "auto_approve_enabled": bot_data.get("auto_approve_enabled", True),
        "start_message": bot_data.get("start_message", ""),
        "start_media": bot_data.get("start_media", None),
        "auto_dm_buttons": bot_data.get("auto_dm_buttons", [])
    })
    await db.save_channels(bot_data.get("channels", []))
    await db.save_auto_dm_messages(bot_data.get("auto_dm_messages", []))
    await db.save_custom_buttons(bot_data.get("custom_buttons", []))
    await db.save_premium_emojis(bot_data.get("premium_emojis", {}))

    # Save access.json for local admin persistence
    try:
        access_payload = {
            "admins": bot_data.get("admins", config.ADMINS),
            "sub_admins": bot_data.get("sub_admins", {})
        }
        with open(config.ACCESS_FILE, "w", encoding="utf-8") as f:
            json.dump(access_payload, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to write access.json: %s", e)

    # Backup to local JSON file
    try:
        with open(config.DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(bot_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to write local JSON backup: %s", e)

# ==========================================
# EMOJI UTILITIES & AUTO-LEARNING SYSTEM
# ==========================================

def get_all_premium_emojis(bot_data_or_emojis=None) -> dict:
    """Consolidates DEFAULT_PREMIUM_EMOJIS and learned emojis into a unified dictionary."""
    combined = getattr(config, "DEFAULT_PREMIUM_EMOJIS", {}).copy()
    if isinstance(bot_data_or_emojis, dict):
        if "premium_emojis" in bot_data_or_emojis:
            combined.update(bot_data_or_emojis.get("premium_emojis", {}))
        else:
            combined.update(bot_data_or_emojis)

    normalized = {}
    for char, eid in combined.items():
        if not char or not eid:
            continue
        eid_str = str(eid).strip()
        if not eid_str:
            continue
        base_char = char.replace("\ufe0f", "")
        variant_char = base_char + "\ufe0f"
        normalized[char] = eid_str
        normalized[base_char] = eid_str
        normalized[variant_char] = eid_str

    return normalized

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
        char_clean = char.strip()
        if not char_clean or not eid_str:
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
                    emoji_char = utf16_bytes[start:end].decode("utf-16le", errors="ignore").strip()
                    add_learned_emoji(emoji_char, entity.custom_emoji_id)
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
        pair_matches = re.findall(r'(\S)\s*[:|=]?\s*(\d{15,20})', combined_text)
        for echar, eid in pair_matches:
            add_learned_emoji(echar, eid)

    if new_added > 0:
        asyncio.create_task(db.save_premium_emojis(bot_data["premium_emojis"]))
        config.DEFAULT_PREMIUM_EMOJIS.update(bot_data["premium_emojis"])
        asyncio.create_task(sync_data_to_db(bot_data))

    return new_added

def apply_premium_emojis(text: str, premium_emojis: dict = None) -> str:
    """Replaces Unicode emojis with <tg-emoji emoji-id="..."> tags."""
    if not text or not isinstance(text, str):
        return text or ""

    all_emojis = get_all_premium_emojis(premium_emojis)
    if not all_emojis:
        return text

    placeholders = {}
    def save_tag(m):
        key = f"\x00TG_EMOJI_{len(placeholders)}\x00"
        placeholders[key] = m.group(0)
        return key

    protected = re.sub(r'<tg-emoji[^>]*>.*?</tg-emoji>', save_tag, text, flags=re.DOTALL)
    sorted_emojis = sorted(all_emojis.items(), key=lambda x: len(x[0]), reverse=True)

    for emoji_char, emoji_id in sorted_emojis:
        if not emoji_char or not emoji_id:
            continue
        if emoji_char not in protected:
            continue
        pattern = re.escape(emoji_char)
        replacement = f'<tg-emoji emoji-id="{emoji_id}">{emoji_char}</tg-emoji>'
        protected = re.sub(pattern, replacement, protected)

    for key, val in placeholders.items():
        protected = protected.replace(key, val)

    return protected

def format_button_emoji(btn_text: str, is_reply_keyboard: bool = False, style: str = None, colors_enabled: bool = True, premium_emojis: dict = None) -> tuple:
    """Processes button text and returns (cleaned_text, api_kwargs)."""
    all_emojis = get_all_premium_emojis(premium_emojis)
    api_kwargs = {}

    if colors_enabled and style:
        api_kwargs["style"] = style

    sorted_emojis = sorted(all_emojis.items(), key=lambda x: len(x[0]), reverse=True)
    found_id = None
    found_char = None

    for char, eid in sorted_emojis:
        if char in btn_text:
            found_id = eid
            found_char = char
            break

    cleaned_text = btn_text
    if found_id:
        api_kwargs["icon_custom_emoji_id"] = str(found_id)
        base_char = found_char.replace("\ufe0f", "")
        cleaned_text = re.sub(re.escape(found_char) + r'\s*', '', cleaned_text)
        cleaned_text = re.sub(re.escape(base_char) + r'\s*', '', cleaned_text)
        cleaned_text = cleaned_text.strip()
        if not cleaned_text:
            cleaned_text = btn_text

    return cleaned_text, (api_kwargs if api_kwargs else None)


# ==========================================
# INLINE & REPLY KEYBOARD BUILDERS
# ==========================================

def build_combined_keyboard(channels: list, custom_buttons: list, dm_buttons: list = None, colors_enabled: bool = True, premium_emojis: dict = None, max_buttons: int = 4) -> InlineKeyboardMarkup:
    """
    Build inline keyboard with MAX 4 BUTTONS TOTAL (2 per row x 2 rows).
    Shows all buttons combined: channel + custom + DM buttons.
    """
    all_buttons = []
    
    # 1. Collect all channel buttons
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
    
    # 2. Collect all custom inline buttons
    for btn in custom_buttons:
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
    
    # 3. Collect DM buttons
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
        
        # 2 buttons per row
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


def build_inline_keyboard(channels: list, custom_buttons: list, colors_enabled: bool = True, show_check_joined: bool = True, premium_emojis: dict = None, dm_buttons: list = None) -> InlineKeyboardMarkup:
    """Wrapper that uses combined keyboard builder."""
    return build_combined_keyboard(
        channels=channels,
        custom_buttons=custom_buttons,
        dm_buttons=dm_buttons,
        colors_enabled=colors_enabled,
        premium_emojis=premium_emojis
    )


def build_reply_keyboard(custom_buttons: list, colors_enabled: bool = True, premium_emojis: dict = None) -> ReplyKeyboardMarkup:
    reply_btns = [b for b in custom_buttons if b.get("keyboard_type") == "reply"]

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
            [make_btn("☯️ Broadcast", "primary"), make_btn("📊 Approve All Requests", "danger")],
            [make_btn(approve_btn_raw, approve_style), make_btn("💌 Manage Auto DM", "primary")],
        ]
    elif is_sub:
        subs_info = bot_data.get("sub_admins", {}).get(user_id_str, {})
        perms = subs_info.get("permissions", ["broadcast", "auto_dm_manage", "approve_requests"])
        rows = []
        row1 = []
        if "broadcast" in perms:
            row1.append(make_btn("☯️ Broadcast", "primary"))
        if "approve_requests" in perms:
            row1.append(make_btn("📊 Approve All Requests", "danger"))
        if row1:
            rows.append(row1)
        row2 = []
        if "approve_requests" in perms:
            row2.append(make_btn(approve_btn_raw, approve_style))
        if "auto_dm_manage" in perms:
            row2.append(make_btn("💌 Manage Auto DM", "primary"))
        if row2:
            rows.append(row2)
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

async def send_auto_dm_messages(bot, chat_id: int, auto_dm_list: list, premium_emojis: dict, channels: list = None, custom_buttons: list = None, dm_buttons: list = None, colors_enabled: bool = True):
    """Send auto DM messages with combined inline buttons (max 4 buttons, 2 per row)."""
    if not auto_dm_list:
        return

    # Build Combined Keyboard instead of just DM buttons
    dm_keyboard = None
    if channels or custom_buttons or dm_buttons:
        dm_keyboard = build_combined_keyboard(
            channels=channels or [],
            custom_buttons=custom_buttons or [],
            dm_buttons=dm_buttons,
            colors_enabled=colors_enabled,
            premium_emojis=premium_emojis,
            max_buttons=4
        )

    for msg_data in auto_dm_list:
        sent = False
        
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
                elif msg_data.get("sticker"):
                    await bot.send_sticker(chat_id=chat_id, sticker=msg_data["sticker"], reply_markup=dm_keyboard)
                elif msg_data.get("text"):
                    text = apply_premium_emojis(msg_data["text"], premium_emojis)
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=dm_keyboard)
            except Exception as e:
                logger.error(f"Fallback send failed: {e}")

        await asyncio.sleep(0.5)

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
        # Always try to generate private link for existing channels
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
    logger.info("Auto-scanned & generated permanent invite link for channel: %s (%s) -> %s", chat_title, chat_id, real_link)

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

    # Build Combined Keyboard (Max 4 buttons total: 2 per row x 2 rows)
    colors_enabled = bot_data.get("colors_enabled", True)
    
    inline_markup = build_combined_keyboard(
        channels=bot_data.get("channels", []),
        custom_buttons=bot_data.get("custom_buttons", []),
        dm_buttons=dm_buttons,
        colors_enabled=colors_enabled,
        premium_emojis=bot_data.get("premium_emojis", {}),
        max_buttons=4  # MAX 4 BUTTONS ONLY
    )
    
    reply_markup = build_reply_keyboard(bot_data.get("custom_buttons", []), colors_enabled=colors_enabled, premium_emojis=bot_data.get("premium_emojis", {}))

    start_msg = bot_data.get("start_message", "").strip()
    start_media = bot_data.get("start_media")
    formatted_start = apply_premium_emojis(start_msg, bot_data.get("premium_emojis", {})) if start_msg else ""
    
    has_content = bool(bot_data.get("channels", [])) or bool(bot_data.get("custom_buttons", [])) or bool(dm_buttons)
    target_markup = inline_markup if has_content else reply_markup

    # Determine which welcome message to send (Mutually Exclusive Logic)
    has_start_content = bool(start_media) or bool(formatted_start)
    has_auto_dm = bool(auto_dm_list)

    if has_start_content:
        # Send Start Media or Text
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
                    await update.message.reply_text(formatted_start or "👋", parse_mode=ParseMode.HTML, reply_markup=target_markup)
            except Exception as e_media:
                logger.error("Failed to send start media, falling back: %s", e_media)
                if formatted_start:
                    await update.message.reply_text(formatted_start, parse_mode=ParseMode.HTML, reply_markup=target_markup)
                else:
                    await update.message.reply_text("👋", reply_markup=target_markup)
        elif formatted_start:
            await update.message.reply_text(formatted_start, parse_mode=ParseMode.HTML, reply_markup=target_markup)
        else:
            await update.message.reply_text("👋", reply_markup=target_markup)

    elif has_auto_dm:
        # Send Auto DM messages with COMBINED buttons (Channels + Custom + DM)
        await send_auto_dm_messages(
            context.bot, chat_id, auto_dm_list, 
            bot_data.get("premium_emojis", {}),
            channels=bot_data.get("channels", []),
            custom_buttons=bot_data.get("custom_buttons", []),
            dm_buttons=dm_buttons,
            colors_enabled=colors_enabled
        )
    else:
        # Default fallback if neither is set
        await update.message.reply_text("👋", reply_markup=target_markup)

    # Notify Super Admins and Sub-Admins
    admin_alert = apply_premium_emojis(
        f"🎯 <b>New User Alert!</b>\n\n👤 {user.first_name}\n🆔 <code>{user.id}</code>",
        bot_data.get("premium_emojis", {})
    )
    all_admins = set(bot_data.get("admins", config.ADMINS))
    for sub_id in bot_data.get("sub_admins", {}).keys():
        all_admins.add(sub_id)

    for adm in all_admins:
        try:
            await context.bot.send_message(chat_id=int(adm), text=admin_alert, parse_mode=ParseMode.HTML)
        except Exception:
            pass

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})

    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    await send_admin_panel(update.effective_chat.id, context.bot, bot_data, user_id_str)

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    if user_id_str not in bot_data.get("admins", config.ADMINS):
        await update.message.reply_text("❌ Access Denied: Only Super Admin can add sub-admins.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("📌 <b>Usage:</b> <code>/addadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    target_user_id = args[0]
    bot_data.setdefault("sub_admins", {})[target_user_id] = {
        "permissions": ["broadcast", "auto_dm_manage", "approve_requests"],
        "added_by": user_id_str
    }
    await sync_data_to_db(bot_data)
    await update.message.reply_text(
        f"✅ <b>Sub-Admin Added!</b>\n\n👤 User ID: <code>{target_user_id}</code>\n"
        "🔑 <b>Granted Permissions:</b>\n"
        "• 📢 Broadcast\n"
        "• 💌 Auto DM Management\n"
        "• ⚡ Instant Auto-Approve Join Requests\n\n"
        "📁 <i>Saved to MongoDB & auto-created access.json!</i>",
        parse_mode=ParseMode.HTML
    )

async def add_superadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    if user_id_str not in bot_data.get("admins", config.ADMINS):
        await update.message.reply_text("❌ Access Denied: Only Super Admin can add new Super Admins.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("📌 <b>Usage:</b> <code>/addsuperadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return

    target_user_id = args[0]
    admins = bot_data.setdefault("admins", config.ADMINS)
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

    if user_id_str not in bot_data.get("admins", config.ADMINS):
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

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("📌 <b>Usage:</b> <code>/addemoji &lt;emoji&gt; &lt;emoji_id&gt;</code>\n\nExample: <code>/addemoji 🔥 5474667187258006816</code>", parse_mode=ParseMode.HTML)
        return

    e_char = args[0].strip()
    e_id = args[1].strip()

    bot_data.setdefault("premium_emojis", {})[e_char] = e_id
    await db.save_premium_emojis(bot_data["premium_emojis"])
    await sync_data_to_db(bot_data)

    await update.message.reply_text(
        f"🎨 <b>Premium Emoji Registered!</b>\n\n"
        f"Emoji: {e_char}\n"
        f"ID: <code>{e_id}</code>",
        parse_mode=ParseMode.HTML
    )

async def findemoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    msg = update.message
    if not msg:
        return
    user_id_str = str(msg.from_user.id)
    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        return

    target_msg = msg.reply_to_message or msg
    new_added = auto_learn_emojis(target_msg, bot_data)

    learned = bot_data.get("premium_emojis", {})
    if not learned:
        await msg.reply_text("🎨 <b>No custom emoji IDs found in message.</b>", parse_mode=ParseMode.HTML)
    else:
        out = f"🎨 <b>Learned Premium Emoji IDs (New: {new_added}):</b>\n\n"
        for char, eid in list(learned.items())[:50]:
            out += f"{char} → <code>{eid}</code>\n"
        await msg.reply_text(out, parse_mode=ParseMode.HTML)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    user_id_str = str(update.effective_user.id)
    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    is_admin = is_super or is_sub

    if is_admin:
        text = (
            "🏅 <b>Help & Support (Admin View)</b>\n\n"
            "👤 <b>Support Admin:</b> @earnwithdurov (Durov Bhai)\n\n"
            "📌 <b>Available Admin Commands:</b>\n"
            "• /start - Start the bot\n"
            "• /admin - Open Admin Panel\n"
            "• /help - Show Help & Support\n"
            "• /findemoji - Extract Premium Custom Emoji IDs\n"
            "• /addemoji &lt;emoji&gt; &lt;emoji_id&gt; - Register Premium Emoji\n"
            "• /addadmin &lt;user_id&gt; - Add Sub-Admin\n"
            "• /addsuperadmin &lt;user_id&gt; - Promote Super Admin\n"
            "• /remadmin &lt;user_id&gt; - Remove Admin\n"
            "• /rembutton &lt;index&gt; - Remove Custom Button\n"
            "• /clearbuttons - Clear All Custom Buttons\n"
            "• /adddmbtn url | Text | Link | style - Add DM Button\n"
            "• /cleardmbtn - Clear All DM Buttons\n"
            "• /showdmbtn - Show DM Buttons\n\n"
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

    formatted = apply_premium_emojis(text, bot_data.get("premium_emojis", {}))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Contact Support (@earnwithdurov)", url="https://t.me/earnwithdurov")]
    ])
    await update.message.reply_text(formatted, parse_mode=ParseMode.HTML, reply_markup=keyboard)

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
    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})

    colors_enabled = bot_data.get("colors_enabled", True)
    color_status = "✅ <b>ON</b>" if colors_enabled else "❌ <b>OFF</b>"
    auto_approve_enabled = bot_data.get("auto_approve_enabled", True)
    approve_status = "⚡ <b>AUTO (Instant)</b>" if auto_approve_enabled else "🛑 <b>MANUAL</b>"
    premium_emojis = bot_data.get("premium_emojis", {})
    start_media_status = "🖼️ <b>Set</b>" if bot_data.get("start_media") else "❌ <b>None</b>"
    start_msg_status = "📝 <b>Set</b>" if bot_data.get("start_message") else "❌ <b>None</b>"
    dm_buttons_count = len(bot_data.get("auto_dm_buttons", []))

    def ibtni(raw_text, cb, style="primary"):
        btn_text, kw = format_button_emoji(raw_text, style=style, colors_enabled=colors_enabled, premium_emojis=premium_emojis)
        return InlineKeyboardButton(btn_text, callback_data=cb, api_kwargs=kw)

    if is_super:
        if page == 1:
            text = (
                f"🔰 <b>⚡ Super Admin Panel (Page 1/2) ⚡</b> 🔰\n\n"
                f"🎨 <b>Button Colors:</b> {color_status}\n"
                f"⚡ <b>Join Request Mode:</b> {approve_status}\n"
                f"👥 <b>Sub-Admins:</b> {len(bot_data.get('sub_admins', {}))}\n"
                f"🎨 <b>Learned Emojis:</b> {len(premium_emojis)}\n"
                f"💌 <b>DM Buttons:</b> {dm_buttons_count}/4\n\n"
                f"<i>📌 Select an option:</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [ibtni("📢 Broadcast", "adm_broadcast"), ibtni("📊 Approve All Requests", "adm_approve_all", "danger")],
                [ibtni(f"⚡ Auto-Approve: {'ON ✅' if auto_approve_enabled else 'OFF ❌'}", "adm_toggle_auto_approve"), ibtni("👥 Sub-Admins", "adm_manage_sub_admins")],
                [ibtni("➕ Add Channel", "adm_add_chan"), ibtni("❌ Remove Channel", "adm_rem_chan", "danger")],
                [ibtni("📝 Edit /start Msg", "adm_edit_start_msg"), ibtni("🖼️ Edit /start Media", "adm_edit_start_msg", "success")],
                [ibtni("➕ Add Custom Button", "adm_add_custom_btn"), ibtni("🎨 Learn Premium Emoji", "adm_learn_emoji_prompt")],
                [ibtni("💌 Manage DM Buttons", "adm_manage_dm_buttons", "success"), ibtni("▶️ Page 2", "adm_page_2", "success")],
                [ibtni("🚪 Close", "adm_close", "danger")]
            ])
        else:
            text = (
                f"🔰 <b>⚡ Super Admin Panel (Page 2/2) ⚡</b> 🔰\n\n"
                f"🖼️ <b>Start Image/Media:</b> {start_media_status}\n"
                f"📝 <b>Start Message Text:</b> {start_msg_status}\n"
                f"🔘 <b>Custom Buttons:</b> {len(bot_data.get('custom_buttons', []))}\n"
                f"📤 <b>Save Mode:</b> {'✅ ON' if bot_data.get('save_mode') else '❌ OFF'}\n"
                f"💌 <b>DM Buttons:</b> {dm_buttons_count}/4\n\n"
                f"<i>📌 Select an option:</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [ibtni("🖼️ Edit Start Media", "adm_edit_start_msg"), ibtni("🗑️ Remove Start Media", "adm_rem_start_media", "danger")],
                [ibtni("🗑️ Remove Start Msg Text", "adm_rem_start_msg", "danger"), ibtni("🔄 Fix Channel Links", "adm_fix_channel_links", "success")],
                [ibtni("🗑️ Remove Custom Buttons", "adm_manage_buttons", "danger"), ibtni("🗑️ Clear All Buttons", "adm_clear_buttons", "danger")],
                [ibtni("🎨 View Emojis", "adm_view_emojis"), ibtni("🎨 Toggle Colors", "adm_toggle_button_colors")],
                [ibtni("📤 Toggle Save Mode", "adm_toggle_save_mode"), ibtni("🔄 RESET BOT", "adm_reset_bot", "danger")],
                [ibtni("◀️ Page 1", "adm_page_1", "success"), ibtni("🚪 Close", "adm_close", "danger")]
            ])
    elif is_sub:
        text = (
            f"🔰 <b>⚡ Sub-Admin Panel ⚡</b> 🔰\n\n"
            f"⚡ <b>Join Request Mode:</b> {approve_status}\n"
            f"🔑 <b>Your Permissions:</b> Broadcast, Auto DM, Approve Requests\n"
            f"💌 <b>DM Buttons:</b> {dm_buttons_count}/4\n\n"
            f"<i>📌 Select an option:</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [ibtni("📢 Broadcast", "adm_broadcast"), ibtni("💌 Manage Auto DM", "adm_manage_auto_dm")],
            [ibtni(f"⚡ Auto-Approve: {'ON ✅' if auto_approve_enabled else 'OFF ❌'}", "adm_toggle_auto_approve")],
            [ibtni("💌 Manage DM Buttons", "adm_manage_dm_buttons", "success")],
            [ibtni("🚪 Close", "adm_close", "danger")]
        ])
    else:
        return

    text = apply_premium_emojis(text, premium_emojis)
    admin_reply_keyboard = build_admin_reply_keyboard(bot_data, is_super, is_sub, user_id_str)

    if message_to_edit:
        try:
            await message_to_edit.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return
        except Exception:
            pass

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🎨 <b>Admin Keyboard activated below:</b>", premium_emojis), parse_mode=ParseMode.HTML, reply_markup=admin_reply_keyboard)

async def show_auto_dm_messages(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    messages = bot_data.get("auto_dm_messages", [])
    save_mode = bot_data.get("save_mode", False)
    learned_count = len(bot_data.get("premium_emojis", {}))
    dm_buttons = bot_data.get("auto_dm_buttons", [])

    text = "💌 <b>Auto DM Messages Management</b>\n\n"
    text += f"📤 <b>Save Mode:</b> {'✅ <b>ON</b>' if save_mode else '❌ <b>OFF</b>'}\n"
    text += f"🎨 <b>Learned Emoji IDs:</b> {learned_count}\n"
    text += f"💌 <b>DM Buttons:</b> {len(dm_buttons)}/4\n\n"

    if not messages:
        text += "<i>No Auto DM messages saved yet.</i>\n\n"
        text += "📌 <b>How to save:</b>\n1️⃣ Turn ON Save Mode\n2️⃣ Send any message (text/photo/video/APK/sticker/forward)\n3️⃣ Saved to MongoDB & Auto-DM active!"
    else:
        text += f"<b>📨 Saved Messages:</b> {len(messages)}\n\n"
        for idx, msg in enumerate(messages):
            m_type = "📋 Forwarded" if msg.get("type") == "copy" else (
                "Text" if "text" in msg else (
                "Photo" if "photo" in msg else (
                "Video" if "video" in msg else (
                "Document/APK" if "document" in msg else "Media"))))
            text += f"{idx + 1}. 📎 {m_type}\n"
        text += "\n📌 To remove a message, send: <code>remove|1</code>"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Turn OFF Save Mode" if save_mode else "🔄 Turn ON Save Mode", callback_data="adm_toggle_save_mode")],
        [InlineKeyboardButton("🎨 View Learned Emojis", callback_data="adm_view_emojis"), InlineKeyboardButton("🗑️ Clear All DM", callback_data="adm_clear_auto_dm")],
        [InlineKeyboardButton("💌 Manage DM Buttons", callback_data="adm_manage_dm_buttons")],
        [InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")]
    ])

    await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(text, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)

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

        succ_msg = bot_data.get("verification_success_msg", config.DEFAULT_VERIFICATION_MSG)
        succ_msg = apply_premium_emojis(succ_msg, bot_data.get("premium_emojis", {}))

        colors_enabled = bot_data.get("colors_enabled", True)
        inline_markup = build_combined_keyboard(
            channels=[],
            custom_buttons=bot_data.get("custom_buttons", []),
            dm_buttons=bot_data.get("auto_dm_buttons", []),
            colors_enabled=colors_enabled,
            premium_emojis=bot_data.get("premium_emojis", {}),
            max_buttons=4
        )
        await bot.send_message(chat_id=chat_id, text=succ_msg, parse_mode=ParseMode.HTML, reply_markup=inline_markup)
    else:
        list_text = "".join([f"• {'🔒 Private' if c.get('type')=='private' else '🌐 Public'} Channel {i+1}\n" for i, c in enumerate(missing)])
        txt = f"📌 You still need to join:\n\n{list_text}\n📌 Join and click Check Joined again."
        inline_markup = build_combined_keyboard(
            channels=missing,
            custom_buttons=[],
            dm_buttons=[],
            colors_enabled=bot_data.get("colors_enabled", True),
            premium_emojis=bot_data.get("premium_emojis", {}),
            max_buttons=4
        )
        await bot.send_message(chat_id=chat_id, text=apply_premium_emojis(txt, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=inline_markup)

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

    is_super_admin = user_id_str in bot_data.get("admins", config.ADMINS)
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
                await query.answer(f"🔘 Button clicked: {matched_btn['text']}", show_alert=True)
        else:
            await query.answer()
        return

    # Check Sub-Admin restricted actions
    allowed_sub_actions = ["adm_broadcast", "adm_manage_auto_dm", "adm_toggle_auto_approve", "adm_toggle_save_mode", "adm_clear_auto_dm", "adm_view_emojis", "adm_learn_emoji_prompt", "adm_close", "adm_back", "adm_approve_all", "adm_manage_dm_buttons"]
    if is_sub_admin and not is_super_admin and data not in allowed_sub_actions:
        await query.answer("❌ Access Denied: Super Admin permission required for this action.", show_alert=True)
        return

    if data == "adm_close":
        await query.message.delete()
        return

    if data == "adm_approve_all":
        await query.answer("⚙️ Bulk approving join requests...", show_alert=False)
        await bulk_approve_requests_action(context.bot, bot_data, chat_id)
        return

    if data == "adm_fix_channel_links":
        await query.answer("⚙️ Scanning & Repairing all channel invite links...", show_alert=False)
        repaired_count, report_text = await refresh_all_channel_links_with_report(context.bot, bot_data)
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(report_text, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_page_1":
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=1, message_to_edit=query.message)
        return

    if data == "adm_page_2":
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2, message_to_edit=query.message)
        return

    if data == "adm_rem_start_media":
        bot_data["start_media"] = None
        await sync_data_to_db(bot_data)
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🗑️ <b>Start Image/Media Removed!</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_rem_start_msg":
        bot_data["start_message"] = ""
        await sync_data_to_db(bot_data)
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🗑️ <b>Start Message Text Removed!</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_manage_buttons":
        custom_btns = bot_data.get("custom_buttons", [])
        if not custom_btns:
            await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🔘 <b>No Custom Buttons Configured.</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            return

        b_text = "🔘 <b>Manage Custom Buttons</b>\n\nTap a button below to delete it:\n\n"
        b_rows = []
        for i, btn in enumerate(custom_btns):
            b_text += f"{i+1}. <b>{btn['text']}</b> ({btn.get('style', 'primary').upper()}, {btn.get('keyboard_type', 'inline').upper()})\n"
            b_rows.append([InlineKeyboardButton(f"❌ Delete '{btn['text']}'", callback_data=f"adm_del_btn_{i}")])

        b_rows.append([InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_page_2")])
        keyboard = InlineKeyboardMarkup(b_rows)
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(b_text, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data.startswith("adm_del_btn_"):
        try:
            b_idx = int(data.replace("adm_del_btn_", ""))
            custom_btns = bot_data.get("custom_buttons", [])
            if 0 <= b_idx < len(custom_btns):
                removed = custom_btns.pop(b_idx)
                await sync_data_to_db(bot_data)
                await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(f"✔️ Removed custom button: <b>{removed.get('text')}</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Error deleting custom button: %s", e)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str, page=2)
        return

    if data == "adm_toggle_auto_approve":
        bot_data["auto_approve_enabled"] = not bot_data.get("auto_approve_enabled", True)
        await sync_data_to_db(bot_data)
        status_txt = "INSTANT AUTO-APPROVE ⚡" if bot_data["auto_approve_enabled"] else "MANUAL APPROVAL 🛑"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(f"✔️ Join request mode set to: <b>{status_txt}</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
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
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(stext, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_toggle_button_colors":
        bot_data["colors_enabled"] = not bot_data.get("colors_enabled", True)
        await sync_data_to_db(bot_data)
        status_text = "ENABLED 🎨" if bot_data["colors_enabled"] else "DISABLED ❌"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(f"✔️ Button colors are now <b>{status_text}</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "adm_manage_auto_dm":
        bot_data.setdefault("admin_states", {})[user_id_str] = "manage_auto_dm"
        await show_auto_dm_messages(chat_id, context)
        return

    # NEW: DM Buttons Management
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
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(text, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_clear_dm_buttons":
        bot_data["auto_dm_buttons"] = []
        await sync_data_to_db(bot_data)
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🗑️ <b>All DM buttons cleared!</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
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

    if data == "adm_view_emojis":
        learned = bot_data.get("premium_emojis", {})
        if not learned:
            etext = "🎨 <b>No learned custom emoji IDs yet.</b>"
        else:
            etext = f"🎨 <b>Auto-Learned Premium Emoji IDs ({len(learned)})</b>\n\n"
            for char, eid in list(learned.items())[:50]:
                etext += f"{char} → <code>{eid}</code>\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")]])
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(etext, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_learn_emoji_prompt":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_premium_emoji"
        msg = (
            "🎨 <b>Learn / Add Premium Emoji</b>\n\n"
            "Send a message containing Premium Emojis (from Telegram Custom Emoji picker),\n"
            "or send in format:\n<code>Emoji | Emoji_ID</code> (e.g. <code>🔥 | 5474667187258006816</code>)\n\n"
            "<i>Newly learned emoji IDs will be stored permanently in MongoDB and automatically used across the entire bot!</i>"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Panel", callback_data="adm_back")]])
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(msg, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_broadcast":
        bot_data.setdefault("admin_states", {})[user_id_str] = "broadcast"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("📌 <b>Broadcast Mode</b>\n\nSend any message (text, photo, video, APK/file, sticker, forward) to broadcast to all MongoDB users:", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_add_chan":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Public Channel", callback_data="adm_add_public"), InlineKeyboardButton("🔒 Private Channel", callback_data="adm_add_private")],
            [InlineKeyboardButton("❌ Cancel", callback_data="adm_close")]
        ])
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🔖 <b>Select Channel Type:</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "adm_add_public":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_chan_public"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("🔖 <b>Add Public Channel</b>\n\nSend 2 lines:\n<code>@username</code>\n<code>https://t.me/channel</code>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_add_private":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_chan_private"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("☯️ <b>Private Channel Setup</b>\n\nForward a message from the private channel here:", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_rem_chan":
        chans = bot_data.get("channels", [])
        if not chans:
            await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("📌 No channels configured.", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            return
        bot_data.setdefault("admin_states", {})[user_id_str] = "remove_channel"
        txt = "🗑️ <b>Remove Channel</b>\n\nSend number to remove:\n" + "".join([f"{i+1}. {c.get('title', c['id'])}\n" for i, c in enumerate(chans)])
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(txt, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_edit_start_msg":
        bot_data.setdefault("admin_states", {})[user_id_str] = "edit_start_msg"
        prompt_txt = (
            "📝 <b>Edit /start Message</b>\n\n"
            "Send your new start message.\n"
            "• You can send a <b>Text Message</b>\n"
            "• OR send a <b>Photo/Video/Document with Caption</b>!\n\n"
            "<i>(HTML formatting & custom premium emojis are fully supported)</i>"
        )
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(prompt_txt, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_edit_verification_msg":
        bot_data.setdefault("admin_states", {})[user_id_str] = "edit_verification_msg"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("✅ <b>Edit Verification Message</b>\n\nSend new verification success message:", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_add_custom_btn":
        bot_data.setdefault("admin_states", {})[user_id_str] = "add_custom_btn"
        help_text = (
            "➕ <b>Add Custom Button</b>\n\n"
            "Format: <code>Type | Button Text | Action_Or_URL | Style | Row | KeyboardKind</code>\n\n"
            "📌 <b>Type:</b> <code>url</code> or <code>callback</code> or <code>reply</code>\n"
            "📌 <b>Styles:</b> <code>primary</code> (🔵), <code>success</code> (🟢), <code>danger</code> (🔴)\n"
            "📌 <b>KeyboardKind:</b> <code>inline</code> (Message) or <code>reply</code> (Bottom menu)\n\n"
            "<b>Inline Examples:</b>\n"
            "<code>url | 📢 JOIN GROUP | https://t.me/example | primary | 1 | inline</code>\n"
            "<code>url | 🎯 GET APK | https://t.me/apk | success | 1 | inline</code>\n\n"
            "<b>Reply Bottom Menu Examples:</b>\n"
            "<code>reply | 📌 Check Joined | check_joined | success | 1 | reply</code>\n"
            "<code>reply | 💬 Support Chat | support | danger | 2 | reply</code>"
        )
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis(help_text, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    if data == "adm_clear_buttons":
        bot_data["custom_buttons"] = []
        await sync_data_to_db(bot_data)
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("✔️ All custom buttons cleared.", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    if data == "adm_reset_bot":
        bot_data.setdefault("admin_states", {})[user_id_str] = "confirm_reset"
        await context.bot.send_message(chat_id=chat_id, text=apply_premium_emojis("⚠️ <b>DANGER: Reset Bot</b>\n\nDeletes all channels and user records.\nType <b>yes</b> to confirm:", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
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
        await bot.send_message(
            chat_id=chat_id,
            text=apply_premium_emojis("📌 <b>No Channels Found!</b>\n\nAdd channels in <code>/admin</code> or the bot will auto-scan when join requests arrive.", bot_data.get("premium_emojis", {})),
            parse_mode=ParseMode.HTML
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=apply_premium_emojis("⚙️ <b>Auto-Scanning Channels & Bulk Approving Join Requests...</b> Please wait.", bot_data.get("premium_emojis", {})),
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

    await bot.send_message(
        chat_id=chat_id,
        text=apply_premium_emojis(f"✔️ <b>Bulk Approval Complete!</b>\n\n⚡ Total Join Requests Approved: <b>{approved_count}</b> across {len(channel_map)} channel(s).", bot_data.get("premium_emojis", {})),
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

    is_super_admin = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub_admin = user_id_str in bot_data.get("sub_admins", {})
    is_admin = is_super_admin or is_sub_admin

    # Always auto-learn any custom emojis sent in messages by admins/users
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
                    await message.reply_text(apply_premium_emojis(f"🔗 <b>{btn['text']}:</b> {btn['url']}", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
                    return
                elif btn.get("type") in ["callback", "reply"] and btn.get("callback_data"):
                    update.callback_query = type('obj', (object,), {'id': 'reply_btn', 'from_user': message.from_user, 'message': message, 'data': btn["callback_data"]})()
                    await callback_handler(update, context)
                    return

    # Handle Admin Reply Keyboard Button Clicks
    if is_admin:
        if "broadcast" in clean_keyword or text in ["☯️ Broadcast", "Broadcast"]:
            bot_data.setdefault("admin_states", {})[user_id_str] = "broadcast"
            await message.reply_text(apply_premium_emojis("📌 <b>Broadcast Mode</b>\n\nSend any message to broadcast to all users:", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            return

        if "approve all requests" in clean_keyword or "approve all" in clean_keyword:
            await bulk_approve_requests_action(context.bot, bot_data, chat_id)
            return

        if "auto-approve" in clean_keyword or "auto approve" in clean_keyword:
            bot_data["auto_approve_enabled"] = not bot_data.get("auto_approve_enabled", True)
            await sync_data_to_db(bot_data)
            status_txt = "INSTANT AUTO-APPROVE ⚡" if bot_data["auto_approve_enabled"] else "MANUAL APPROVAL 🛑"
            await message.reply_text(apply_premium_emojis(f"✔️ Join request mode set to: <b>{status_txt}</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
            return

        if "manage auto dm" in clean_keyword or "auto dm" in clean_keyword:
            bot_data.setdefault("admin_states", {})[user_id_str] = "manage_auto_dm"
            await show_auto_dm_messages(chat_id, context)
            return

    admin_state = bot_data.get("admin_states", {}).get(user_id_str, "")

    # Auto DM removal trigger
    if is_admin and admin_state == "manage_auto_dm" and text.startswith("remove|"):
        bot_data["admin_states"].pop(user_id_str, None)
        try:
            idx = int(text.split("|")[1]) - 1
            if 0 <= idx < len(bot_data.get("auto_dm_messages", [])):
                bot_data["auto_dm_messages"].pop(idx)
                await sync_data_to_db(bot_data)
                await message.reply_text(apply_premium_emojis("✔️ Auto DM message removed!", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(apply_premium_emojis("📌 Invalid message index!", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply_text(apply_premium_emojis("📌 Invalid format! Use: remove|1", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
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
                    caption = apply_premium_emojis(msg_payload.get("caption", ""), bot_data.get("premium_emojis", {}))
                    if msg_payload.get("photo"):
                        await context.bot.send_photo(chat_id=target_id, photo=msg_payload["photo"], caption=caption, parse_mode=ParseMode.HTML)
                    elif msg_payload.get("video"):
                        await context.bot.send_video(chat_id=target_id, video=msg_payload["video"], caption=caption, parse_mode=ParseMode.HTML)
                    elif msg_payload.get("document"):
                        await context.bot.send_document(chat_id=target_id, document=msg_payload["document"], caption=caption, parse_mode=ParseMode.HTML)
                    elif msg_payload.get("sticker"):
                        await context.bot.send_sticker(chat_id=target_id, sticker=msg_payload["sticker"])
                    elif msg_payload.get("text"):
                        txt = apply_premium_emojis(msg_payload["text"], bot_data.get("premium_emojis", {}))
                        await context.bot.send_message(chat_id=target_id, text=txt, parse_mode=ParseMode.HTML)

                sent_count += 1
            except Exception:
                failed_count += 1

            await asyncio.sleep(0.035)

        report = f"📌 <b>Broadcast Complete!</b>\n\n👥 Total MongoDB Targets: <b>{len(recipient_list)}</b>\n✔️ Sent: <b>{sent_count}</b>\n📌 Failed: <b>{failed_count}</b>"
        await message.reply_text(apply_premium_emojis(report, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await send_admin_panel(chat_id, context.bot, bot_data, user_id_str)
        return

    # Auto DM Save Mode
    save_mode = bot_data.get("save_mode", False)
    is_auto_dm_state = (admin_state == "manage_auto_dm")
    is_not_command = not text.startswith("/")

    if is_admin and (save_mode or is_auto_dm_state) and not admin_state.startswith("add_") and is_not_command:
        saved_data = extract_message_payload(message)
        bot_data.setdefault("auto_dm_messages", []).append(saved_data)
        await sync_data_to_db(bot_data)

        reply = f"✅ <b>Auto DM Message Saved!</b>\n\n📌 Total messages: {len(bot_data['auto_dm_messages'])}"
        await message.reply_text(apply_premium_emojis(reply, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        await show_auto_dm_messages(chat_id, context)
        return

    # Admin Prompt State Machine
    if is_admin and admin_state and is_not_command:
        state = admin_state
        bot_data["admin_states"].pop(user_id_str, None)

        if state == "add_premium_emoji":
            new_learned = auto_learn_emojis(message, bot_data)
            learned_dict = bot_data.get("premium_emojis", {})
            if new_learned > 0:
                resp = f"✅ <b>Learned {new_learned} New Premium Emoji(s)!</b>\n\nSaved permanently to MongoDB and active everywhere across text and buttons."
            else:
                resp = f"🎨 <b>Emoji Processing Complete!</b>\n\nTotal learned emojis currently stored: <b>{len(learned_dict)}</b>"
            await message.reply_text(apply_premium_emojis(resp, learned_dict), parse_mode=ParseMode.HTML)
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
                resp = f"✔️ <b>Public Channel Fetched & Added!</b>\n\n📌 <b>Title:</b> {title}\n🆔 <b>ID:</b> <code>{real_id}</code>\n🔗 <b>Permanent Link:</b> {real_link}"
                await message.reply_text(apply_premium_emojis(resp, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
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
                await message.reply_text(apply_premium_emojis(f"⚠️ <b>Channel Added!</b>\n\nAdded: {username_or_link}\n🔗 Link: {fallback_link}", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)

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
                    await message.reply_text(apply_premium_emojis(f"✔️ <b>Private Channel Added Successfully!</b>\n\n📌 <b>Title:</b> {f_title}\n🔗 <b>Invite Link:</b> {invite_link}", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text(apply_premium_emojis("📌 <b>Failed to create invite link!</b> Make sure bot is Admin in channel.", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(apply_premium_emojis("📌 Please forward a message from the private channel!", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)

        elif state == "remove_channel":
            try:
                idx = int(text) - 1
                if 0 <= idx < len(bot_data.get("channels", [])):
                    removed = bot_data["channels"].pop(idx)
                    await sync_data_to_db(bot_data)
                    await message.reply_text(apply_premium_emojis(f"✔️ Channel removed: {removed.get('title')}", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text(apply_premium_emojis("📌 Invalid index!", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            except Exception:
                await message.reply_text(apply_premium_emojis("📌 Invalid input number!", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)

        elif state == "edit_start_msg":
            new_emojis = auto_learn_emojis(message, bot_data)
            raw_start_html = message.caption_html or message.text_html or message.caption or message.text or ""
            bot_data["start_message"] = raw_start_html

            if message.photo:
                bot_data["start_media"] = {"type": "photo", "file_id": message.photo[-1].file_id}
            elif message.video:
                bot_data["start_media"] = {"type": "video", "file_id": message.video.file_id}
            elif message.document:
                bot_data["start_media"] = {"type": "document", "file_id": message.document.file_id}
            elif message.animation:
                bot_data["start_media"] = {"type": "animation", "file_id": message.animation.file_id}
            else:
                bot_data["start_media"] = None

            await sync_data_to_db(bot_data)
            learned_lbl = f" ({new_emojis} new custom emoji IDs learned!)" if new_emojis > 0 else ""
            media_lbl = f" with {bot_data['start_media']['type'].title()}" if bot_data.get("start_media") else ""
            await message.reply_text(
                apply_premium_emojis(f"✔️ <b>/start message updated successfully{media_lbl}!</b>{learned_lbl}\n\nSaved to MongoDB & active immediately.", bot_data.get("premium_emojis", {})),
                parse_mode=ParseMode.HTML
            )

        elif state == "edit_verification_msg":
            new_emojis = auto_learn_emojis(message, bot_data)
            raw_ver_html = message.text_html or message.caption_html or message.text or message.caption or ""
            bot_data["verification_success_msg"] = raw_ver_html
            await sync_data_to_db(bot_data)
            await message.reply_text(
                apply_premium_emojis(f"✔️ <b>Verification message updated!</b>" + (f" ({new_emojis} new emoji IDs learned)" if new_emojis else ""), bot_data.get("premium_emojis", {})),
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
                    f"📌 <i>This button will be attached directly to your /start message!</i>"
                )
                await message.reply_text(apply_premium_emojis(resp, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            else:
                err_msg = (
                    "📌 <b>Format Error!</b> Send button details in format:\n\n"
                    "<code>Button Text | https://yourlink.com</code>\n"
                    "OR\n"
                    "<code>Button Text | https://yourlink.com | success</code>\n\n"
                    "🎨 <b>Available Colors:</b> <code>primary</code> (🔵), <code>success</code> (🟢), <code>danger</code> (🔴)"
                )
                await message.reply_text(apply_premium_emojis(err_msg, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)

        elif state == "confirm_reset":
            if text.lower() == "yes":
                bot_data["channels"] = []
                bot_data["custom_buttons"] = []
                bot_data["auto_dm_buttons"] = []
                await sync_data_to_db(bot_data)
                await message.reply_text(apply_premium_emojis("✅ <b>Bot Reset Complete!</b>", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(apply_premium_emojis("❌ Reset cancelled.", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)

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
        inline_markup = build_combined_keyboard(
            channels=bot_data.get("channels", []),
            custom_buttons=bot_data.get("custom_buttons", []),
            dm_buttons=bot_data.get("auto_dm_buttons", []),
            colors_enabled=colors_enabled,
            premium_emojis=bot_data.get("premium_emojis", {}),
            max_buttons=4
        )
        try:
            await send_auto_dm_messages(
                context.bot, user_id, 
                bot_data.get("auto_dm_messages", []), 
                bot_data.get("premium_emojis", {}), 
                channels=bot_data.get("channels", []),
                custom_buttons=bot_data.get("custom_buttons", []),
                dm_buttons=bot_data.get("auto_dm_buttons", []), 
                colors_enabled=colors_enabled
            )
            status_text = "⚡ <b>Instant Approved!</b>" if approved_successfully else "📌 <b>Request Received!</b>"
            msg = f"{status_text}\n\nWelcome to <b>{chat_title}</b>!"
            await context.bot.send_message(
                chat_id=user_id,
                text=apply_premium_emojis(msg, bot_data.get("premium_emojis", {})),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_markup
            )
        except Exception as err:
            logger.info("Could not send DM message to user %s: %s", user_id, err)

        app_label = "⚡ Auto-Approved" if approved_successfully else "📌 Pending Approval"
        alert = f"📌 <b>New Join Request! ({app_label})</b>\n📌 <b>Channel:</b> {chat_title}\n👤 <b>User:</b> {req.from_user.first_name} (<code>{user_id}</code>)"
        all_admins = set(bot_data.get("admins", config.ADMINS))
        for sub_id in bot_data.get("sub_admins", {}).keys():
            all_admins.add(sub_id)

        for adm in all_admins:
            try:
                await context.bot.send_message(chat_id=int(adm), text=apply_premium_emojis(alert, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
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
        actor_id_str in bot_data.get("admins", config.ADMINS)
        or actor_id_str in bot_data.get("sub_admins", {})
    )

    was_active = old_status in ("administrator", "member", "creator")
    is_active_now = new_status in ("administrator", "member", "creator")
    is_removed_now = new_status in ("left", "kicked", "banned", "restricted")

    if is_active_now and not was_active:
        if is_authorized:
            await auto_scan_channel(context.bot, bot_data, chat_id, chat_title, chat_type="channel")
            try:
                await context.bot.send_message(
                    chat_id=int(actor_id_str),
                    text=apply_premium_emojis(
                        f"✅ <b>New Channel Registered!</b>\n\n📌 <b>{chat_title}</b> has been added and its join button is now active.",
                        bot_data.get("premium_emojis", {})
                    ),
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
            all_admins = set(bot_data.get("admins", config.ADMINS))
            for sub_id in bot_data.get("sub_admins", {}).keys():
                all_admins.add(sub_id)
            for adm in all_admins:
                try:
                    await context.bot.send_message(chat_id=int(adm), text=alert, parse_mode=ParseMode.HTML)
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
# ADMIN UTILITY COMMANDS
# ==========================================

async def rem_button_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
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
        await update.message.reply_text(apply_premium_emojis(txt, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
        return

    b_idx = int(args[0]) - 1
    if 0 <= b_idx < len(custom_btns):
        removed = custom_btns.pop(b_idx)
        await sync_data_to_db(bot_data)
        await update.message.reply_text(apply_premium_emojis(f"✔️ Custom button <b>{removed.get('text')}</b> removed!", bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("📌 Invalid button index!", parse_mode=ParseMode.HTML)

# NEW: DM Button Commands
async def add_dm_button_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a button to Auto DM messages - Max 4 buttons (2 per row)"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
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
            "<b>Example:</b>\n<code>/adddmbtn url | Predection 📈 | https://t.me/earnwithdurov | success</code>\n\n"
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
            apply_premium_emojis(
                f"✅ <b>DM Button Added! ({len(bot_data['auto_dm_buttons'])}/4)</b>\n\n"
                f"🔘 <b>Text:</b> {b_text}\n"
                f"🔗 <b>Link:</b> {b_url}\n"
                f"🎨 <b>Style:</b> {b_style}",
                bot_data.get("premium_emojis", {})
            ),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("📌 Invalid format! Use: /adddmbtn url | Text | Link | style", parse_mode=ParseMode.HTML)

async def clear_dm_buttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all Auto DM buttons"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    bot_data["auto_dm_buttons"] = []
    await sync_data_to_db(bot_data)
    await update.message.reply_text(
        apply_premium_emojis("✅ <b>All DM buttons cleared!</b>", bot_data.get("premium_emojis", {})),
        parse_mode=ParseMode.HTML
    )

async def show_dm_buttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all Auto DM buttons"""
    user_id_str = str(update.effective_user.id)
    bot_data = context.bot_data

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
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
    
    await update.message.reply_text(
        apply_premium_emojis(text, bot_data.get("premium_emojis", {})),
        parse_mode=ParseMode.HTML
    )

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

    is_super = user_id_str in bot_data.get("admins", config.ADMINS)
    is_sub = user_id_str in bot_data.get("sub_admins", {})
    if not (is_super or is_sub):
        await update.message.reply_text("❌ Access Denied.")
        return

    msg_init = await update.message.reply_text("⚙️ <b>Scanning and repairing all channel invite links...</b>", parse_mode=ParseMode.HTML)
    repaired, report_text = await refresh_all_channel_links_with_report(context.bot, bot_data)
    await msg_init.edit_text(apply_premium_emojis(report_text, bot_data.get("premium_emojis", {})), parse_mode=ParseMode.HTML)

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
    logger.info("Initializing Python Bot @%s ...", config.BOT_USERNAME)

    application = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("findemoji", findemoji_command))
    application.add_handler(CommandHandler("emojis", findemoji_command))
    application.add_handler(CommandHandler("buttons_demo", buttons_demo_command))
    application.add_handler(CommandHandler("addemoji", add_emoji_command))
    application.add_handler(CommandHandler("addadmin", add_admin_command))
    application.add_handler(CommandHandler("addsuperadmin", add_superadmin_command))
    application.add_handler(CommandHandler("remadmin", rem_admin_command))
    application.add_handler(CommandHandler("rembutton", rem_button_command))
    application.add_handler(CommandHandler("clearbuttons", rem_button_command))
    application.add_handler(CommandHandler("fixlinks", fix_links_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # NEW: DM Button commands
    application.add_handler(CommandHandler("adddmbtn", add_dm_button_command))
    application.add_handler(CommandHandler("cleardmbtn", clear_dm_buttons_command))
    application.add_handler(CommandHandler("showdmbtn", show_dm_buttons_command))

    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(ChatJoinRequestHandler(chat_join_request_handler))
    application.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("Bot starting polling...")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
