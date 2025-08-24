import os
import json
import logging
import time
import requests
import asyncio
import aiohttp
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- CONFIGURACIÓN Y PERSISTENCIA ---

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

def load_json(filename, default):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f)

# --- Idiomas ---
LANGS = {
    "es": load_lang("es"),
    "en": load_lang("en"),
}
DEFAULT_LANG = "en"
user_langs = load_json("user_langs.json", {})

# --- Usuarios y configuración persistente ---
allowed_users = set(load_json("allowed_users.json", [CREATOR_ID]))
user_server = load_json("user_server.json", {})
user_hydrax_api = load_json("user_hydrax_api.json", {})

user_pending_hapi = {}  # user_id: api_key (temporal hasta confirmación)
user_ads_state = {}     # user_id: dict con estado del anuncio

# --- Cola y estado de subida por usuario ---
user_video_queue = {}   # user_id: [ (message, video_info/url) ]
user_uploading = {}     # user_id: bool

# --- Carpeta temporal para descargas ---
TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

app = Client("auu_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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

async def upload_to_hydrax(api_id, file_path, file_name, file_type, progress_callback):
    url = f"http://up.hydrax.net/{api_id}"
    file_size = os.path.getsize(file_path)
    try:
        with open(file_path, 'rb') as f:
            files = {'file': (file_name, f, file_type)}
            await progress_callback(0)
            r = requests.post(url, files=files)
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
            # --- DESCARGA ---
            if isinstance(video_info, dict):  # Telegram video/documento de tipo video
                file_id = video_info["file_id"]
                file_name = video_info.get("file_name", "video.mp4")
                file_type = video_info.get("mime_type", "video/mp4")
                local_path = os.path.join(TEMP_DIR, f"{int(time.time())}_{file_name}")
                await app.download_media(file_id, file_name=local_path, progress=lambda cur, tot: asyncio.create_task(temp_msg.edit_text(f"{t(user_id, 'video_downloading')}\n{make_progress_bar(cur * 100 / tot if tot > 0 else 0)}")))
            elif isinstance(video_info, str):  # URL directa
                file_name = video_info.split("/")[-1]
                file_type = "video/mp4" if file_name.endswith(".mp4") else "application/octet-stream"
                local_path = os.path.join(TEMP_DIR, f"{user_id}_{int(time.time())}_{file_name}")
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

            # --- SUBIDA A HYDRAX ---
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

# --- COMANDOS Y MANEJO DE MENSAJES ---

@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id)
    allowed_users.add(user_id)
    save_json("allowed_users.json", list(allowed_users))
    await message.reply(LANGS[lang]["welcome"])
    user_server[str(user_id)] = "hydrax"
    user_hydrax_api[str(user_id)] = HYDRAX_API_ID
    log_event(f"Usuario {user_id} inició el bot.")

@app.on_message(filters.command("setlang"))
async def setlang(client, message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
         InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")]
    ])
    await message.reply(t(user_id, "choose_lang"), reply_markup=kb)
    log_event(f"Usuario {user_id} solicitó cambio de idioma.")

@app.on_callback_query(filters.regex("^lang_"))
async def lang_callback(client, callback_query):
    user_id = callback_query.from_user.id
    lang_code = callback_query.data.split("_")[1]
    user_langs[str(user_id)] = lang_code
    save_json("user_langs.json", user_langs)
    await callback_query.message.edit_text(LANGS[lang_code]["lang_set"])
    log_event(f"Usuario {user_id} cambió idioma a {lang_code}.")

@app.on_message(filters.command("ayuda"))
async def ayuda(client, message):
    user_id = message.from_user.id
    ayuda_text = (
        "✨ <b>Comandos disponibles:</b>\n"
        "• <b>/add</b> — Añade un usuario a la lista de permitidos.\n"
        "• <b>/ads</b> — Crea y envía un anuncio masivo.\n"
        "• <b>/ayuda</b> — Muestra esta ayuda detallada 🆘.\n"
        "• <b>/cancel</b> — Cancela la operación en curso ⏹️.\n"
        "• <b>/hapi</b> — Cambia la API Key de Hydrax 🔑.\n"
        "• <b>/log</b> — Recupera el registro de actividad 📄.\n"
        "• <b>/ping</b> — Mide la latencia del bot 📶.\n"
        "• <b>/remove</b> — Elimina un usuario de la lista de permitidos 🚫.\n"
        "• <b>/server</b> — Selecciona el destino de subida 🌐.\n"
        "• <b>/setlang</b> — Cambia el idioma del bot 🇪🇸🇺🇸.\n\n"
        "👉 <i>Envía un video, documento de tipo video o enlace directo para subirlo a Hydrax.</i>"
    )
    await message.reply(ayuda_text, parse_mode="HTML")
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
        save_json("allowed_users.json", list(allowed_users))
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
        save_json("allowed_users.json", list(allowed_users))
        await message.reply(f"Usuario {rem_id} eliminado de la whitelist.")
        log_event(f"Usuario {rem_id} eliminado por {user_id}.")
    except Exception as e:
        await message.reply("Error en el formato. Usa /remove <user_id>")
        log_event(f"Error eliminando usuario: {e}")

@app.on_message(filters.command("ping"))
async def ping_command(client, message):
    user_id = message.from_user.id
    start = time.time()
    sent = await message.reply(t(user_id, "pinging"))
    end = time.time()
    ms = int((end - start) * 1000)
    await sent.edit_text(t(user_id, "pong").format(ms=ms))
    log_event(f"Usuario {user_id} usó /ping: {ms}ms")

@app.on_message(filters.command("server"))
async def server_command(client, message):
    user_id = message.from_user.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀Telegram", callback_data="server_telegram"),
         InlineKeyboardButton("🦎Hydrax", callback_data="server_hydrax")]
    ])
    await message.reply(t(user_id, "choose_server"), reply_markup=kb)
    log_event(f"Usuario {user_id} solicitó /server.")

@app.on_callback_query(filters.regex("^server_"))
async def server_callback(client, callback_query):
    user_id = callback_query.from_user.id
    srv = callback_query.data.split("_")[1]
    user_server[str(user_id)] = srv
    save_json("user_server.json", user_server)
    await callback_query.message.edit_text(t(user_id, f"server_set_{srv}"))
    log_event(f"Usuario {user_id} cambió server a {srv}.")

@app.on_message(filters.command("hapi"))
async def hapi_command(client, message):
    user_id = message.from_user.id
    await message.reply(t(user_id, "send_hapi"))
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
    if user_server.get(str(user_id), "hydrax") == "hydrax":
        # Video de Telegram
        if message.video or (message.document and (message.document.mime_type or "").startswith("video/")):
            # Soporta videos y documentos de tipo video
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

    # RESPUESTA PARA MENSAJES NO COMANDO NI VIDEO
    await message.reply(t(user_id, "main_instruction"))

@app.on_callback_query(filters.regex("^hapi_"))
async def hapi_confirm_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id not in allowed_users or user_id not in user_pending_hapi:
        await callback_query.answer(t(user_id, "not_allowed"), show_alert=True)
        return
    if callback_query.data == "hapi_ok":
        user_hydrax_api[str(user_id)] = user_pending_hapi[user_id]
        save_json("user_hydrax_api.json", user_hydrax_api)
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
        users_to_send = [u for u in allowed_users if u != CREATOR_ID]
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
    print("Si aparece el mensaje de TgCrypto, instala con: pip install tgcrypto para mejorar la velocidad de descarga de videos de Telegram.")
    app.run()
