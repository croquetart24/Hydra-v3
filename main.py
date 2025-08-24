import os
import json
import logging
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Leer variables de entorno
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CREATOR_ID = int(os.getenv("CREATOR_ID"))  # ID numérico
HYDRAX_API_ID = os.getenv("HYDRAX_API_ID")

# Configuración de logging
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_event(event):
    # Log con hora UTC
    logging.info(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} — {event}")

# Cargar traducciones
def load_lang(lang_code):
    try:
        with open(f'lang/{lang_code}.json', 'r', encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_event(f"Error cargando idioma {lang_code}: {e}")
        return {}

LANGS = {
    "es": load_lang("es"),
    "en": load_lang("en"),
}
DEFAULT_LANG = "en"
user_langs = {}

# Gestión de usuarios permitidos
allowed_users = set([CREATOR_ID])

app = Client("auu_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def get_user_lang(user_id):
    return user_langs.get(user_id, DEFAULT_LANG)

def t(user_id, key):
    lang = get_user_lang(user_id)
    return LANGS.get(lang, LANGS[DEFAULT_LANG]).get(key, key)

@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    if user_id not in allowed_users:
        await message.reply(LANGS[lang]["not_allowed"])
        log_event(f"Intento de acceso denegado: {user_id}")
        return
    await message.reply(LANGS[lang]["welcome"])
    log_event(f"Usuario {user_id} inició el bot.")

@app.on_message(filters.command("setlang"))
async def setlang(client, message):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        await message.reply(t(user_id, "not_allowed"))
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
         InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")]
    ])
    await message.reply(t(user_id, "choose_lang"), reply_markup=kb)
    log_event(f"Usuario {user_id} solicitó cambio de idioma.")

@app.on_callback_query(filters.regex("^lang_"))
async def lang_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in allowed_users:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    lang_code = callback_query.data.split("_")[1]
    user_langs[user_id] = lang_code
    await callback_query.message.edit_text(LANGS[lang_code]["lang_set"])
    log_event(f"Usuario {user_id} cambió idioma a {lang_code}.")

@app.on_message(filters.command("ayuda"))
async def ayuda(client, message):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        await message.reply(t(user_id, "not_allowed"))
        return
    await message.reply(t(user_id, "help"))
    log_event(f"Usuario {user_id} solicitó ayuda.")

@app.on_message(filters.command("log"))
async def send_log(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        return
    if os.path.exists("bot.log"):
        await message.reply_document("bot.log", caption="Registro de actividad del bot")
        log_event(f"Usuario {user_id} solicitó el log.")
    else:
        await message.reply("No existe el archivo de log.")

@app.on_message(filters.command("add"))
async def add_user(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        return
    try:
        new_id = int(message.text.strip().split(" ")[1])
        allowed_users.add(new_id)
        await message.reply(f"Usuario {new_id} añadido a la whitelist.")
        log_event(f"Usuario {new_id} añadido por {user_id}.")
    except Exception as e:
        await message.reply("Error en el formato. Usa /add <user_id>")
        log_event(f"Error añadiendo usuario: {e}")

@app.on_message(filters.command("remove"))
async def remove_user(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        return
    try:
        rem_id = int(message.text.strip().split(" ")[1])
        allowed_users.discard(rem_id)
        await message.reply(f"Usuario {rem_id} eliminado de la whitelist.")
        log_event(f"Usuario {rem_id} eliminado por {user_id}.")
    except Exception as e:
        await message.reply("Error en el formato. Usa /remove <user_id>")
        log_event(f"Error eliminando usuario: {e}")

# El resto de comandos y lógica se irá añadiendo en los siguientes cambios

if __name__ == "__main__":
    log_event("Bot iniciado.")
    app.run()
