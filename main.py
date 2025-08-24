import os
import json
import logging
import time
import asyncio
import aiohttp
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Variables de entorno
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CREATOR_ID = int(os.getenv("CREATOR_ID"))
HYDRAX_API_ID = os.getenv("HYDRAX_API_ID")
SESSION = os.getenv("SESSION")

# Logging
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_event(event):
    logging.info(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} — {event}")

# Persistencia de configuración
def load_json(filename, default):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f)

allowed_users = set(load_json("allowed_users.json", [CREATOR_ID]))
user_langs = load_json("user_langs.json", {})
user_server = load_json("user_server.json", {})
user_hydrax_api = load_json("user_hydrax_api.json", {})
user_session = load_json("user_session.json", {"session": SESSION})

def load_lang(lang_code):
    with open(f'lang/{lang_code}.json', 'r', encoding="utf-8") as f:
        return json.load(f)

LANGS = {
    "es": load_lang("es"),
    "en": load_lang("en"),
}
DEFAULT_LANG = "en"

user_video_queue = {}  # user_id: [ (message, video_info/url) ]
user_uploading = {}    # user_id: bool
user_pending_hapi = {}  # user_id: api_key (temporal hasta confirmación)
user_pending_session = {}  # user_id: session string (temporal hasta confirmación)
user_ads_state = {}  # user_id: dict con estado del anuncio
known_users = set(load_json("allowed_users.json", [CREATOR_ID]))

TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

bot_app = Client("AUUBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def get_user_lang(user_id):
    return user_langs.get(str(user_id), DEFAULT_LANG)

def t(user_id, key):
    lang = get_user_lang(user_id)
    return LANGS.get(lang, LANGS[DEFAULT_LANG]).get(key, key)

def is_direct_video_url(text):
    if isinstance(text, str):
        return text.lower().endswith(('.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv')) and (text.startswith("http://") or text.startswith("https://"))
    return False

def make_progress_bar(percent, length=20):
    filled = int(percent / 100 * length)
    bar = '█' * filled + '-' * (length - filled)
    return f"[{bar}] {percent:.1f}%"

async def download_url(url, dest, message, user_id):
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            with open(dest, "wb") as f:
                downloaded = 0
                total = int(resp.headers.get('content-length', 0))
                async for chunk in resp.content.iter_chunked(1024*1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    percent = downloaded * 100 / total if total else 0
                    await message.edit_text(f"{t(user_id, 'video_downloading')}\n{make_progress_bar(percent)}")
    return dest

async def download_with_session(file_id, file_name, message, user_id):
    session_string = user_session.get("session", SESSION)
    if not session_string:
        await message.edit_text(t(user_id, "session_missing"))
        return None
    # Cliente usuario temporal para la descarga
    temp_client = Client("AUUTempUser", api_id=API_ID, api_hash=API_HASH, session_string=session_string)
    await temp_client.start()
    local_path = os.path.join(TEMP_DIR, file_name)
    await temp_client.download_media(file_id, file_name=local_path, progress=lambda cur, tot: asyncio.create_task(message.edit_text(f"{t(user_id, 'video_downloading')}\n{make_progress_bar(cur * 100 / tot if tot > 0 else 0)}")))
    await temp_client.stop()
    return local_path

async def upload_to_hydrax(api_id, file_path, file_name, file_type, progress_callback):
    import requests
    file_size = os.path.getsize(file_path)
    try:
        with open(file_path, 'rb') as f:
            files = {'file': (file_name, f, file_type)}
            await progress_callback(0)
            r = requests.post(f"http://up.hydrax.net/{api_id}", files=files)
            await progress_callback(100)
            return r.text
    except Exception as e:
        return None

async def process_video_queue(user_id):
    user_uploading[user_id] = True
    while user_video_queue.get(user_id):
        item = user_video_queue[user_id].pop(0)
        message, video_info = item
        lang = get_user_lang(user_id)
        hydrax_api = user_hydrax_api.get(str(user_id), HYDRAX_API_ID)
        await message.reply(t(user_id, "video_upload_start"))
        local_path = None
        file_name = None
        file_type = None
        temp_msg = await message.reply(t(user_id, "video_preparing"))
        try:
            # DESCARGA
            if isinstance(video_info, dict):  # Video Telegram
                file_id = video_info["file_id"]
                file_name = video_info.get("file_name", "video.mp4")
                file_type = video_info.get("mime_type", "video/mp4")
                # Descarga usando SESSION
                local_path = await download_with_session(file_id, file_name, temp_msg, user_id)
                if not local_path:
                    continue
            elif isinstance(video_info, str):  # URL directa
                file_name = video_info.split("/")[-1]
                file_type = "video/mp4" if file_name.endswith(".mp4") else "application/octet-stream"
                local_path = os.path.join(TEMP_DIR, f"{user_id}_{int(time.time())}_{file_name}")
                await download_url(video_info, local_path, temp_msg, user_id)
            else:
                await temp_msg.edit_text(t(user_id, "video_error"))
                continue

            # SUBIDA
            async def progress_callback(percent):
                await temp_msg.edit_text(f"{t(user_id, 'video_uploading')}\n{make_progress_bar(percent)}")

            result = await upload_to_hydrax(hydrax_api, local_path, file_name, file_type, progress_callback)
            if result:
                await temp_msg.edit_text(f"{t(user_id, 'video_done')}\n{result}")
                log_event(f"Video subido a Hydrax por {user_id}: {file_name}")
            else:
                await temp_msg.edit_text(t(user_id, "video_error"))
                log_event(f"Error subiendo a Hydrax para {user_id}: {file_name}")
        except Exception as e:
            await temp_msg.edit_text(t(user_id, "video_error"))
            log_event(f"Excepción en subida para {user_id}: {e}")
        finally:
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass
        await asyncio.sleep(1)
    user_uploading[user_id] = False

@bot_app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    known_users.add(user_id)
    allowed_users.add(user_id)
    save_json("allowed_users.json", list(allowed_users))
    user_server[str(user_id)] = "hydrax"
    save_json("user_server.json", user_server)
    user_hydrax_api[str(user_id)] = HYDRAX_API_ID
    save_json("user_hydrax_api.json", user_hydrax_api)
    await message.reply(t(user_id, "welcome"))
    log_event(f"Usuario {user_id} inició el bot.")

@bot_app.on_message(filters.command("setlang"))
async def setlang(client, message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
         InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")]
    ])
    await message.reply(t(user_id, "choose_lang"), reply_markup=kb)
    log_event(f"Usuario {user_id} solicitó cambio de idioma.")

@bot_app.on_callback_query(filters.regex("^lang_"))
async def lang_callback(client, callback_query):
    user_id = callback_query.from_user.id
    lang_code = callback_query.data.split("_")[1]
    user_langs[str(user_id)] = lang_code
    save_json("user_langs.json", user_langs)
    await callback_query.message.edit_text(LANGS[lang_code]["lang_set"])
    log_event(f"Usuario {user_id} cambió idioma a {lang_code}.")

@bot_app.on_message(filters.command("ayuda"))
async def ayuda(client, message):
    user_id = message.from_user.id
    ayuda_text = (
        "✨ <b>Comandos disponibles:</b>\n"
        "• <b>/add</b> — Añade un usuario a la lista de permitidos.\n"
        "• <b>/ads</b> — Crea y envía un anuncio masivo.\n"
        "• <b>/ayuda</b> — Muestra esta ayuda detallada 🆘.\n"
        "• <b>/cancel</b> — Cancela la operación en curso ⏹️.\n"
        "• <b>/hapi</b> — Cambia la API Key de Hydrax 🔑.\n"
        "• <b>/session</b> — Cambia la SESSION string para descargas avanzadas.\n"
        "• <b>/log</b> — Recupera el registro de actividad 📄.\n"
        "• <b>/ping</b> — Mide la latencia del bot 📶.\n"
        "• <b>/remove</b> — Elimina un usuario de la lista de permitidos 🚫.\n"
        "• <b>/server</b> — Selecciona el destino de subida 🌐.\n"
        "• <b>/setlang</b> — Cambia el idioma del bot 🇪🇸🇺🇸.\n\n"
        "👉 <i>Envía un video o enlace directo para subirlo a Hydrax.</i>"
    )
    await message.reply(ayuda_text, parse_mode="html")
    log_event(f"Usuario {user_id} solicitó ayuda.")

@bot_app.on_message(filters.command("session"))
async def session_command(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        return
    await message.reply(t(user_id, "send_session"))
    user_pending_session[user_id] = None

@bot_app.on_message(filters.text & filters.user([CREATOR_ID]))
async def session_receive(client, message):
    user_id = message.from_user.id
    # Configuración de SESSION
    if user_id in user_pending_session and user_pending_session[user_id] is None:
        session_candidate = message.text.strip()
        user_pending_session[user_id] = session_candidate
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅Si", callback_data="session_ok"),
             InlineKeyboardButton("🚫No", callback_data="session_cancel")]
        ])
        await message.reply(t(user_id, "confirm_session").format(session=session_candidate), reply_markup=kb)
        return

@bot_app.on_callback_query(filters.regex("^session_"))
async def session_confirm_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in user_pending_session:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    if callback_query.data == "session_ok":
        user_session["session"] = user_pending_session[user_id]
        save_json("user_session.json", user_session)
        await callback_query.message.edit_text(t(user_id, "session_set_ok"))
        del user_pending_session[user_id]
    else:
        await callback_query.message.edit_text(t(user_id, "session_set_cancel"))
        del user_pending_session[user_id]

@bot_app.on_message(filters.command("add"))
async def add_user(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        return
    try:
        new_id = int(message.text.strip().split(" ")[1])
        allowed_users.add(new_id)
        save_json("allowed_users.json", list(allowed_users))
        await message.reply(f"Usuario {new_id} añadido a la whitelist.")
        log_event(f"Usuario {new_id} añadido por {user_id}.")
    except Exception as e:
        await message.reply("Error en el formato. Usa /add <user_id>")
        log_event(f"Error añadiendo usuario: {e}")

@bot_app.on_message(filters.command("remove"))
async def remove_user(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        return
    try:
        rem_id = int(message.text.strip().split(" ")[1])
        allowed_users.discard(rem_id)
        save_json("allowed_users.json", list(allowed_users))
        await message.reply(f"Usuario {rem_id} eliminado de la whitelist.")
        log_event(f"Usuario {rem_id} eliminado por {user_id}.")
    except Exception as e:
        await message.reply("Error en el formato. Usa /remove <user_id>")
        log_event(f"Error eliminando usuario: {e}")

@bot_app.on_message(filters.command("ping"))
async def ping_command(client, message):
    user_id = message.from_user.id
    start = time.time()
    sent = await message.reply(t(user_id, "pinging"))
    end = time.time()
    ms = int((end - start) * 1000)
    await sent.edit_text(t(user_id, "pong").format(ms=ms))
    log_event(f"Usuario {user_id} usó /ping: {ms}ms")

@bot_app.on_message(filters.command("server"))
async def server_command(client, message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀Telegram", callback_data="server_telegram"),
         InlineKeyboardButton("🦎Hydrax", callback_data="server_hydrax")]
    ])
    await message.reply(t(user_id, "choose_server"), reply_markup=kb)
    log_event(f"Usuario {user_id} solicitó /server.")

@bot_app.on_callback_query(filters.regex("^server_"))
async def server_callback(client, callback_query):
    user_id = callback_query.from_user.id
    srv = callback_query.data.split("_")[1]
    user_server[str(user_id)] = srv
    save_json("user_server.json", user_server)
    await callback_query.message.edit_text(t(user_id, f"server_set_{srv}"))
    log_event(f"Usuario {user_id} cambió server a {srv}.")

@bot_app.on_message(filters.command("hapi"))
async def hapi_command(client, message):
    user_id = message.from_user.id
    await message.reply(t(user_id, "send_hapi"))
    user_pending_hapi[user_id] = None

@bot_app.on_message(filters.text & filters.user(list(allowed_users)))
async def text_receive(client, message):
    user_id = message.from_user.id
    # Configuración de HAPI
    if user_id in user_pending_hapi and user_pending_hapi[user_id] is None:
        api_candidate = message.text.strip()
        user_pending_hapi[user_id] = api_candidate
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅Si", callback_data="hapi_ok"),
             InlineKeyboardButton("🚫No", callback_data="hapi_cancel")]
        ])
        await message.reply(t(user_id, "confirm_hapi").format(api=api_candidate), reply_markup=kb)
        return

    # Procesamiento de videos y enlaces
    if user_server.get(str(user_id), "hydrax") == "hydrax":
        if message.video or message.document and (message.document.mime_type or "").startswith("video/"):
            file_id = message.video.file_id if message.video else message.document.file_id
            file_name = (message.video.file_name if message.video else message.document.file_name) or "video.mp4"
            file_type = (message.video.mime_type if message.video else message.document.mime_type) or "video/mp4"
            video_info = {
                "file_id": file_id,
                "file_name": file_name,
                "mime_type": file_type
            }
            user_video_queue.setdefault(user_id, []).append((message, video_info))
            log_event(f"Video recibido de {user_id} (Telegram): {file_name}")
            if not user_uploading.get(user_id, False):
                asyncio.create_task(process_video_queue(user_id))
            else:
                await message.reply(t(user_id, "video_queued"))
            return
        # URL directa
        if is_direct_video_url(message.text):
            user_video_queue.setdefault(user_id, []).append((message, message.text.strip()))
            log_event(f"Video recibido de {user_id} (URL): {message.text.strip()}")
            if not user_uploading.get(user_id, False):
                asyncio.create_task(process_video_queue(user_id))
            else:
                await message.reply(t(user_id, "video_queued"))
            return

    await message.reply(t(user_id, "main_instruction"))

@bot_app.on_callback_query(filters.regex("^hapi_"))
async def hapi_confirm_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in user_pending_hapi:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    if callback_query.data == "hapi_ok":
        user_hydrax_api[str(user_id)] = user_pending_hapi[user_id]
        save_json("user_hydrax_api.json", user_hydrax_api)
        await callback_query.message.edit_text(t(user_id, "hapi_set_ok"))
        del user_pending_hapi[user_id]
    else:
        await callback_query.message.edit_text(t(user_id, "hapi_set_cancel"))
        del user_pending_hapi[user_id]

@bot_app.on_callback_query(filters.regex("^session_"))
async def session_confirm_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in user_pending_session:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    if callback_query.data == "session_ok":
        user_session["session"] = user_pending_session[user_id]
        save_json("user_session.json", user_session)
        await callback_query.message.edit_text(t(user_id, "session_set_ok"))
        del user_pending_session[user_id]
    else:
        await callback_query.message.edit_text(t(user_id, "session_set_cancel"))
        del user_pending_session[user_id]

@bot_app.on_message(filters.command("cancel"))
async def cancel_command(client, message):
    user_id = message.from_user.id
    if user_video_queue.get(user_id):
        user_video_queue[user_id] = []
        await message.reply(t(user_id, "video_cancelled"))
        log_event(f"Usuario {user_id} vació la cola de videos.")
        return
    await message.reply(t(user_id, "cancel_ok"))
    log_event(f"Usuario {user_id} usó /cancel.")

if __name__ == "__main__":
    log_event("Bot iniciado.")
    print("Bot iniciado correctamente. Esperando mensajes...")
    bot_app.run()
