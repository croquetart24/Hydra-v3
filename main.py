import os
import json
import logging
import time
import requests
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CREATOR_ID = int(os.getenv("CREATOR_ID"))
HYDRAX_API_ID = os.getenv("HYDRAX_API_ID")

# Logging con hora UTC
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_event(event):
    logging.info(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} — {event}")

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

allowed_users = set([CREATOR_ID])
user_server = {}  # user_id: "telegram" or "hydrax"
user_hydrax_api = {}  # user_id: api_key
user_pending_hapi = {}  # user_id: api_key (temporal hasta confirmación)
user_pending_cancel = set()  # usuarios con proceso cancelable en curso

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
    user_server[user_id] = "telegram"
    user_hydrax_api[user_id] = HYDRAX_API_ID
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

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        await message.reply(t(user_id, "not_allowed"))
        return
    start = time.time()
    sent = await message.reply(t(user_id, "pinging"))
    end = time.time()
    ms = int((end - start) * 1000)
    await sent.edit_text(t(user_id, "pong").format(ms=ms))
    log_event(f"Usuario {user_id} usó /ping: {ms}ms")

@app.on_message(filters.command("server"))
async def server_command(client, message):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        await message.reply(t(user_id, "not_allowed"))
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀Telegram", callback_data="server_telegram"),
         InlineKeyboardButton("🦎Hydrax", callback_data="server_hydrax")]
    ])
    await message.reply(t(user_id, "choose_server"), reply_markup=kb)
    log_event(f"Usuario {user_id} solicitó /server.")

@app.on_callback_query(filters.regex("^server_"))
async def server_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in allowed_users:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    srv = callback_query.data.split("_")[1]
    user_server[user_id] = srv
    await callback_query.message.edit_text(t(user_id, f"server_set_{srv}"))
    log_event(f"Usuario {user_id} cambió server a {srv}.")

@app.on_message(filters.command("hapi"))
async def hapi_command(client, message):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        await message.reply(t(user_id, "not_allowed"))
        return
    await message.reply(t(user_id, "send_hapi"))
    log_event(f"Usuario {user_id} inició proceso /hapi.")
    user_pending_hapi[user_id] = None

@app.on_message(filters.text & filters.user(list(allowed_users)))
async def hapi_receive(client, message):
    user_id = message.from_user.id
    if user_id in user_pending_hapi and user_pending_hapi[user_id] is None:
        # Recibe api, pide confirmación
        api_candidate = message.text.strip()
        user_pending_hapi[user_id] = api_candidate
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅Si", callback_data="hapi_ok"),
             InlineKeyboardButton("🚫No", callback_data="hapi_cancel")]
        ])
        await message.reply(t(user_id, "confirm_hapi").format(api=api_candidate), reply_markup=kb)
        log_event(f"Usuario {user_id} envió api para /hapi (pendiente confirmación)")

@app.on_callback_query(filters.regex("^hapi_"))
async def hapi_confirm_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in allowed_users or user_id not in user_pending_hapi:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    if callback_query.data == "hapi_ok":
        user_hydrax_api[user_id] = user_pending_hapi[user_id]
        await callback_query.message.edit_text(t(user_id, "hapi_set_ok"))
        log_event(f"Usuario {user_id} confirmó nueva api Hydrax.")
        del user_pending_hapi[user_id]
    else:
        await callback_query.message.edit_text(t(user_id, "hapi_set_cancel"))
        log_event(f"Usuario {user_id} canceló cambio api Hydrax.")
        del user_pending_hapi[user_id]

@app.on_message(filters.command("cancel"))
async def cancel_command(client, message):
    user_id = message.from_user.id
    if user_id not in allowed_users:
        await message.reply(t(user_id, "not_allowed"))
        return
    # Marca usuario como cancelando; integración real debe abortar procesos en curso
    user_pending_cancel.add(user_id)
    await message.reply(t(user_id, "cancel_ok"))
    log_event(f"Usuario {user_id} usó /cancel.")

if __name__ == "__main__":
    log_event("Bot iniciado.")
    app.run()
