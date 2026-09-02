import asyncio
import datetime
import logging
import os
import sys
from motor.motor_asyncio import AsyncIOMotorClient
import config

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.client = None
        self.db = None
        self.is_connected = False

    async def connect(self):
        try:
            self.client = AsyncIOMotorClient(config.MONGO_URI, serverSelectionTimeoutMS=2000)
            # Ping database to check connection
            await self.client.admin.command('ping')
            self.db = self.client[config.DB_NAME]
            self.is_connected = True
            logger.info("Successfully connected to MongoDB (%s)", config.DB_NAME)
        except Exception as e:
            self.is_connected = False
            logger.warning("MongoDB connection failed (%s). Operating in local fallback mode.", e)

    async def log_event(self, level: str, message: str):
        log_entry = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "level": level,
            "message": message
        }
        if self.is_connected and self.db is not None:
            try:
                await self.db.logs.insert_one(log_entry)
            except Exception as e:
                logger.error("Failed to write log to MongoDB: %s", e)

    # ==================== USERS COLLECTION ====================
    async def save_user(self, user_id: int, first_name: str = "", username: str = "", registered: bool = True, verified: bool = False):
        user_doc = {
            "_id": str(user_id),
            "user_id": user_id,
            "first_name": first_name,
            "username": username,
            "registered": registered,
            "verified": verified,
            "updated_at": datetime.datetime.utcnow().isoformat()
        }
        if self.is_connected and self.db is not None:
            try:
                await self.db.users.update_one({"_id": str(user_id)}, {"$set": user_doc}, upsert=True)
            except Exception as e:
                logger.error("MongoDB save_user error: %s", e)

    async def mark_verified(self, user_id: int):
        if self.is_connected and self.db is not None:
            try:
                await self.db.users.update_one({"_id": str(user_id)}, {"$set": {"verified": True}})
            except Exception as e:
                logger.error("MongoDB mark_verified error: %s", e)

    async def get_all_user_ids(self) -> list:
        if self.is_connected and self.db is not None:
            try:
                cursor = self.db.users.find({}, {"_id": 1})
                docs = await cursor.to_list(length=100000)
                return [d["_id"] for d in docs]
            except Exception as e:
                logger.error("MongoDB get_all_user_ids error: %s", e)
        return []

    # ==================== CONFIG & STATE COLLECTION ====================
    async def get_bot_config(self) -> dict:
        default_cfg = {
            "_id": "bot_config",
            "admins": config.ADMINS,
            "imageUrl": config.DEFAULT_IMAGE,
            "verification_success_msg": config.DEFAULT_VERIFICATION_MSG,
            "save_mode": False,
            "colors_enabled": True,  # Admin Button Colors Toggle (ON/OFF)
            "start_message": ""
        }
        if self.is_connected and self.db is not None:
            try:
                doc = await self.db.config.find_one({"_id": "bot_config"})
                if doc:
                    default_cfg.update(doc)
                    return default_cfg
            except Exception as e:
                logger.error("MongoDB get_bot_config error: %s", e)
        return default_cfg

    async def save_bot_config(self, cfg: dict):
        if self.is_connected and self.db is not None:
            try:
                cfg["_id"] = "bot_config"
                await self.db.config.update_one({"_id": "bot_config"}, {"$set": cfg}, upsert=True)
            except Exception as e:
                logger.error("MongoDB save_bot_config error: %s", e)

    # ==================== CHANNELS ====================
    async def get_channels(self) -> list:
        if self.is_connected and self.db is not None:
            try:
                doc = await self.db.channels.find_one({"_id": "channels_list"})
                if doc and "channels" in doc:
                    return doc["channels"]
            except Exception as e:
                logger.error("MongoDB get_channels error: %s", e)
        return []

    async def save_channels(self, channels: list):
        if self.is_connected and self.db is not None:
            try:
                await self.db.channels.update_one({"_id": "channels_list"}, {"$set": {"channels": channels}}, upsert=True)
            except Exception as e:
                logger.error("MongoDB save_channels error: %s", e)

    # ==================== AUTO DM MESSAGES ====================
    async def get_auto_dm_messages(self) -> list:
        if self.is_connected and self.db is not None:
            try:
                doc = await self.db.auto_dm.find_one({"_id": "auto_dm_list"})
                if doc and "messages" in doc:
                    return doc["messages"]
            except Exception as e:
                logger.error("MongoDB get_auto_dm_messages error: %s", e)
        return []

    async def save_auto_dm_messages(self, messages: list):
        if self.is_connected and self.db is not None:
            try:
                await self.db.auto_dm.update_one({"_id": "auto_dm_list"}, {"$set": {"messages": messages}}, upsert=True)
            except Exception as e:
                logger.error("MongoDB save_auto_dm_messages error: %s", e)

    # ==================== CUSTOM BUTTONS (INLINE & REPLY) ====================
    async def get_custom_buttons(self) -> list:
        if self.is_connected and self.db is not None:
            try:
                doc = await self.db.buttons.find_one({"_id": "buttons_list"})
                if doc and "buttons" in doc:
                    return doc["buttons"]
            except Exception as e:
                logger.error("MongoDB get_custom_buttons error: %s", e)
        return []

    async def save_custom_buttons(self, buttons: list):
        if self.is_connected and self.db is not None:
            try:
                await self.db.buttons.update_one({"_id": "buttons_list"}, {"$set": {"buttons": buttons}}, upsert=True)
            except Exception as e:
                logger.error("MongoDB save_custom_buttons error: %s", e)

    # ==================== PREMIUM EMOJIS ====================
    async def get_premium_emojis(self) -> dict:
        if self.is_connected and self.db is not None:
            try:
                doc = await self.db.emojis.find_one({"_id": "emojis_map"})
                if doc and "emojis" in doc:
                    return doc["emojis"]
            except Exception as e:
                logger.error("MongoDB get_premium_emojis error: %s", e)
        return {}

    async def save_premium_emojis(self, emojis: dict):
        if self.is_connected and self.db is not None:
            try:
                await self.db.emojis.update_one({"_id": "emojis_map"}, {"$set": {"emojis": emojis}}, upsert=True)
            except Exception as e:
                logger.error("MongoDB save_premium_emojis error: %s", e)

# Singleton Instance
db = Database()
