import os

# ==========================================
# BOT & MONGODB CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8748612606:AAHUD7QxQvVpZFMc5p56CPqBvm7itVDYGzc")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@Aman_Sureshortbot")
ADMINS = [a for a in ["8692549519", ""] if a]

# MongoDB Connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://monjitbora161_db_user:tonny%40123@tonny.rc1s0tu.mongodb.net/tonny?retryWrites=true&w=majority&appName=tonny")
DB_NAME = os.getenv("DB_NAME", "aman_db")

DEFAULT_IMAGE = "https://t.me/NEXm2m/824"
DEFAULT_VERIFICATION_MSG = "✅ <b>Verification Successful!</b>\n\nYou have successfully joined all channels.\n\n<b>Access Granted!</b>"

DATA_FILE = os.path.join(os.path.dirname(__file__), "data_aman.json")
BACKUP_FILE = os.path.join(os.path.dirname(__file__), "data_backup_aman.json")
ACCESS_FILE = os.path.join(os.path.dirname(__file__), "access_aman.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "bot_errors_aman.log")

# Button Style Definitions
STYLE_EMOJIS = {
    "primary": "🔵",
    "success": "🟢",
    "danger": "🔴"
}

# Pre-populated Premium Emoji Mappings (Only explicitly listed premium emoji IDs)
DEFAULT_PREMIUM_EMOJIS = {
    "🔗": "5902449142575141204",
    "📹": "5208735005701869592",
    "🟣": "6033058509635982119",
    "👑": "6181357919276114076",
    "🔙": "5352759161945867747",
    "👤": "5766915217552315762",
    "✅": "6088893844693195262",
    "📅": "6154676319712973937",
    "📢": "6154301953183587055",
    "❌": "6181467651395558500",
    "🔒": "6154387470277414903",
    "📣": "6165579510805696230",
    "📊": "6105134176896293236",
    "✍️": "5197269100878907942",
    "🗑": "6278268268557897540",
    "✉️": "5456457058498913849",
    "🏷": "6323215848634847159",
    "👁": "6147523214890244143",
    "🥰": "6073371665381724173",
    "❤️": "6071272766403776978",
    "🤩": "6073456529640525999",
    "⭐️": "6154712281474146033",
    "🚀": "6136480579094325175",
    "🥳": "6210514579642916617",
    "🗿": "6111702792504612925",
    "🐶": "6111656466987358867",
    "❄️": "6125257520312229044",
    "🎁": "6089047892285200811",
    "🎄": "6181742443403156898",
    "🥂": "6181642091492285536",
    "🗣️": "6181439802827611525",
    "⚡️": "6181421841274379029",
    "⬇️": "6089298151439604414",
    "❓": "6181487163431984652",
    "💋": "6181240782633049582",
    "⭐": "6181535395914718008",
    "🔥": "6179353385024626225",
    "🚗": "6181732393179683700",
    "📌": "6181743753368182130",
    "💬": "6181322172263308706",
    "🟢": "6179295235462406768",
    "🌟": "6181445996170452501",
    "💕": "6181356978678275248",
    "🎆": "6181644724307238586",
    "💖": "6181465104479952715",
    "🧸": "6181718060873816980",
    "🔝": "6181351206242231136"
}
