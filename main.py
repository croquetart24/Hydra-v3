import os
import json
import logging
import time
import requests
import asyncio
import aiohttp
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CREATOR_ID = int(os.getenv("CREATOR_ID"))
HYDRAX_API_ID = os.getenv("HYDRAX_API_ID")

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
user_ads_state = {}  # user_id: dict con estado del anuncio
known_users = set([CREATOR_ID])  # todos los que han iniciado el bot

# --- Nueva estructura para la cola de subida de videos por usuario ---
user_video_queue = {}  # user_id: [ (message, video_info/url) ]
user_uploading = {}    # user_id: bool

app = Client("auu_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def get_user_lang(user_id):
    return user_langs.get(user_id, DEFAULT_LANG)

def t(user_id, key):
    lang = get_user_lang(user_id)
    return LANGS.get(lang, LANGS[DEFAULT_LANG]).get(key, key)

def is_direct_video_url(text):
    # Detecta enlaces directos a archivos de video
    if isinstance(text, str):
        return text.lower().endswith(('.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv')) and (text.startswith("http://") or text.startswith("https://"))
    return False

def make_progress_bar(percent, length=20):
    filled = int(percent / 100 * length)
    bar = '█' * filled + '-' * (length - filled)
    return f"[{bar}] {percent:.1f}%"

async def upload_to_hydrax(api_id, file_path, file_name, file_type, progress_callback):
    """
    Sube el archivo a Hydrax mostrando el progreso mediante el callback.
    """
    url = f"http://up.hydrax.net/{api_id}"
    file_size = os.path.getsize(file_path)
    chunk_size = 1024 * 1024  # 1 MB

    # Hydrax no soporta streaming puro, así que simulamos el progreso enviando por chunks.
    with open(file_path, 'rb') as f:
        sent = 0
        chunks = []
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
            sent += len(chunk)
            percent = sent * 100 / file_size
            await progress_callback(percent)
        # Enviar todo en una sola petición (Hydrax requiere el archivo completo)
        files = {'file': (file_name, open(file_path, 'rb'), file_type)}
        try:
            r = requests.post(url, files=files)
            return r.text
        except Exception as e:
            return None

async def process_video_queue(user_id):
    """
    Procesa la cola de videos para el usuario, uno a uno.
    """
    user_uploading[user_id] = True
    while user_video_queue.get(user_id):
        item = user_video_queue[user_id].pop(0)
        message, video_info = item
        # Obtener datos del video
        lang = get_user_lang(user_id)
        hydrax_api = user_hydrax_api.get(user_id, HYDRAX_API_ID)
        await message.reply(t(user_id, "video_upload_start"))
        # Descargar el video si es de Telegram
        local_path = None
        file_name = None
        file_type = None
        temp_msg = await message.reply(t(user_id, "video_preparing"))
        try:
            if isinstance(video_info, dict):  # Es video Telegram
                file_id = video_info["file_id"]
                file_name = video_info.get("file_name", "video.mp4")
                file_type = video_info.get("mime_type", "video/mp4")
                local_path = await app.download_media(file_id, file_name=file_name)
            elif isinstance(video_info, str):  # Es URL directa
                file_name = video_info.split("/")[-1]
                file_type = "video/mp4" if file_name.endswith(".mp4") else "application/octet-stream"
                local_path = f"temp_{user_id}_{int(time.time())}_{file_name}"
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(video_info) as resp:
                        with open(local_path, "wb") as f:
                            downloaded = 0
                            total = int(resp.headers.get('content-length', 0))
                            async for chunk in resp.content.iter_chunked(1024*1024):
                                f.write(chunk)
                                downloaded += len(chunk)
                                percent = downloaded * 100 / total if total else 0
                                await temp_msg.edit_text(f"{t(user_id, 'video_downloading')}\n{make_progress_bar(percent)}")
            else:
                await temp_msg.edit_text(t(user_id, "video_error"))
                continue

            # Subida a Hydrax
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
            # Borra archivos temporales
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass
        # Espera un momento antes del siguiente
        await asyncio.sleep(1)
    user_uploading[user_id] = False

@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    known_users.add(user_id)
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
    # Si está esperando API de Hydrax:
    if user_id in user_pending_hapi and user_pending_hapi[user_id] is None:
        api_candidate = message.text.strip()
        user_pending_hapi[user_id] = api_candidate
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅Si", callback_data="hapi_ok"),
             InlineKeyboardButton("🚫No", callback_data="hapi_cancel")]
        ])
        await message.reply(t(user_id, "confirm_hapi").format(api=api_candidate), reply_markup=kb)
        log_event(f"Usuario {user_id} envió api para /hapi (pendiente confirmación)")
        return

    # Si está en proceso de anuncio (solo CREATOR_ID)
    if user_id == CREATOR_ID and user_id in user_ads_state:
        state = user_ads_state[user_id]
        if state["step"] == "collecting":
            state["messages"].append(message.text)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "ads_yes"), callback_data="ads_more"),
                 InlineKeyboardButton(t(user_id, "ads_no"), callback_data="ads_no_more")]
            ])
            await message.reply(t(user_id, "ads_add_more"), reply_markup=kb)
            state["step"] = "add_more"
            log_event(f"Anuncio: Añadido mensaje por {user_id}")
            return

    # --- VIDEO/URL ENTRANTE ---
    # Procesa solo si está permitido y tiene server Hydrax activo
    if user_server.get(user_id, "telegram") == "hydrax":
        # Video de Telegram
        if message.video:
            video_info = {
                "file_id": message.video.file_id,
                "file_name": message.video.file_name,
                "mime_type": message.video.mime_type
            }
            user_video_queue.setdefault(user_id, []).append((message, video_info))
            log_event(f"Video recibido de {user_id} (Telegram): {video_info['file_name']}")
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
    user_pending_cancel.add(user_id)
    # Cancela anuncios en curso
    if user_id in user_ads_state:
        del user_ads_state[user_id]
        await message.reply(t(user_id, "cancel_ok"))
        log_event(f"Usuario {user_id} canceló anuncio en curso.")
        return
    # Vacía la cola de videos
    if user_video_queue.get(user_id):
        user_video_queue[user_id] = []
        await message.reply(t(user_id, "video_cancelled"))
        log_event(f"Usuario {user_id} vació la cola de videos.")
        return
    await message.reply(t(user_id, "cancel_ok"))
    log_event(f"Usuario {user_id} usó /cancel.")

# ----------------------- ANUNCIOS /ads ------------------------------

@app.on_message(filters.command("ads"))
async def ads_command(client, message):
    user_id = message.from_user.id
    if user_id != CREATOR_ID:
        await message.reply(t(user_id, "not_allowed"))
        log_event(f"Usuario {user_id} intentó usar /ads sin permiso.")
        return
    user_ads_state[user_id] = {"step": "collecting", "messages": []}
    await message.reply(t(user_id, "ads_first"))
    log_event("Comenzando proceso de anuncio (/ads)")

@app.on_callback_query(filters.regex("^ads_"))
async def ads_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id != CREATOR_ID or user_id not in user_ads_state:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    state = user_ads_state[user_id]
    if callback_query.data == "ads_more":
        state["step"] = "collecting"
        await callback_query.message.reply(t(user_id, "ads_next"))
        log_event("Solicitando siguiente mensaje para anuncio (/ads)")
        return
    if callback_query.data == "ads_no_more":
        preview = "\n\n".join(state["messages"])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(user_id, "ads_send"), callback_data="ads_send"),
             InlineKeyboardButton(t(user_id, "ads_cancel"), callback_data="ads_cancel")]
        ])
        await callback_query.message.reply(t(user_id, "ads_preview").format(preview=preview), reply_markup=kb)
        state["step"] = "preview"
        log_event("Vista previa de anuncio generada (/ads)")
        return
    if callback_query.data == "ads_cancel":
        del user_ads_state[user_id]
        await callback_query.message.edit_text(t(user_id, "ads_cancelled"))
        log_event("Anuncio cancelado (/ads)")
        return
    if callback_query.data == "ads_send":
        state["step"] = "sending"
        preview = "\n\n".join(state["messages"])
        await callback_query.message.edit_text(t(user_id, "ads_sending"))
        users_to_send = [u for u in known_users if u in allowed_users and u != CREATOR_ID]
        sent = 0
        blocked = 0
        total = len(users_to_send)
        progress_msg = await app.send_message(CREATOR_ID, t(user_id, "ads_progress").format(sent=sent, total=total, blocked=blocked))
        for u in users_to_send:
            try:
                for msg in state["messages"]:
                    await app.send_message(u, msg)
                    time.sleep(0.5)
                sent += 1
            except Exception as e:
                log_event(f"Fallo al enviar anuncio a {u}: {e}")
                blocked += 1
            await progress_msg.edit_text(t(user_id, "ads_progress").format(sent=sent, total=total, blocked=blocked))
        await app.send_message(CREATOR_ID, t(user_id, "ads_summary").format(sent=sent, total=total, blocked=blocked))
        log_event(f"Anuncio enviado: {sent} usuarios, {blocked} bloqueados.")
        del user_ads_state[user_id]
        return

if __name__ == "__main__":
    log_event("Bot iniciado.")
    app.run()
