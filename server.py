import asyncio
import os
import json
import datetime
import uuid
import hashlib
import sys
import ssl
import re
import secrets
import string
import base64
import aiohttp
from aiohttp import web
import asyncpg
from PIL import Image as PILImage
import random
from argon2 import PasswordHasher

# ─── КОНФИГ ──────────────────────────────────────────────────────────
USERNAME_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_]{3,31}$')

MAILRU_USER = os.environ.get('EMAIL_USER', '')
MAILRU_PASS = os.environ.get('EMAIL_PASSWORD', '')

routes = web.RouteTableDef()

DATABASE_URL = os.environ.get('DATABASE_URL', '')
UPLOAD_DIR = 'uploads'

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'change_me_now')

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

active_connections = {}

ph = PasswordHasher()

# ─── КРИПТО-УТИЛИТЫ ──────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return ph.hash(password)

def verify_password(password: str, hash_str: str) -> bool:
    try:
        ph.verify(hash_str, password)
        return True
    except Exception:
        return False

def hash_email(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode('utf-8')).hexdigest()

# ─── E2EE (СКВОЗНОЕ ШИФРОВАНИЕ) ──────────────────────────────────────
# Сервер НЕ имеет доступа к ключам шифрования и не может читать сообщения.
# Содержимое сообщений приходит с клиента уже зашифрованным (AES-GCM 256,
# WebCrypto), ключи хранятся только на устройствах пользователей.
# Для приватных чатов сервер выступает «почтовым ящиком»: хранит публичные
# identity-ключи (Ed25519) и зашифрованные симметричные ключи чатов
# (sealed box: X25519 + AES-GCM) как непрозрачные blob'ы и раздаёт их
# только участникам соответствующего чата.
E2EE_KEY_TTL_DAYS = 30

async def e2ee_cleanup(app):
    """Периодически чистим устаревшие E2EE-ключи."""
    pool = app['db_pool']
    while True:
        try:
            await pool.execute(
                "DELETE FROM e2ee_keys WHERE updated_at < NOW() - ($1 || ' days')::interval",
                str(E2EE_KEY_TTL_DAYS)
            )
        except Exception as e:
            print(f"E2EE cleanup error: {e}")
        await asyncio.sleep(24 * 3600)

async def generate_user_id(pool, length=10):
    alphabet = string.ascii_letters + string.digits
    while True:
        uid = ''.join(secrets.choice(alphabet) for _ in range(length))
        exists = await pool.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id = $1)", uid)
        if not exists:
            return uid

def get_ws_by_username(username):
    for ws, info in list(active_connections.items()):
        if info.get('username') == username:
            return ws
    return None

async def get_or_create_private_room(pool, room_str: str) -> str:
    parts = room_str.split('|')
    if len(parts) != 3:
        return room_str
    user1, user2 = sorted([parts[1], parts[2]])
    row = await pool.fetchrow('''
        SELECT r.name FROM rooms r
        JOIN room_members rm1 ON r.name = rm1.room_name
        JOIN room_members rm2 ON r.name = rm2.room_name
        WHERE r.type = 'private' AND rm1.username = $1 AND rm2.username = $2
    ''', user1, user2)
    if row:
        return row['name']
    new_uuid = str(uuid.uuid4())
    await pool.execute("INSERT INTO rooms (name, type) VALUES ($1, 'private')", new_uuid)
    await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", new_uuid, user1)
    await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", new_uuid, user2)
    return new_uuid

async def send_mailru_code(user_email, code):
    proxy_url = "https://resend-u5gj.vercel.app/api"
    payload = {"to": user_email, "code": code}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(proxy_url, json=payload) as response:
                if response.status == 200:
                    print("Письмо успешно улетело через связку Render + Vercel + Resend!")
                    return True
                else:
                    print(f"Vercel ответил статусом: {response.status}")
                    return False
    except Exception as e:
        print(f"Ошибка подключения к Vercel: {e}")
        return False

# ─── БАЗА ДАННЫХ ─────────────────────────────────────────────────────
async def init_db(app):
    global DATABASE_URL
    if 'RENDER' in os.environ and ("localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL or not DATABASE_URL):
        print("КРИТИЧЕСКАЯ ОШИБКА: DATABASE_URL не задана в Environment!")

    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    if "localhost" not in DATABASE_URL and "127.0.0.1" not in DATABASE_URL:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        pool = await asyncpg.create_pool(DATABASE_URL, ssl=ssl_context, statement_cache_size=0)
    else:
        pool = await asyncpg.create_pool(DATABASE_URL, statement_cache_size=0)

    app['db_pool'] = pool

    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS verification_codes (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                code TEXT NOT NULL
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                email TEXT UNIQUE NOT NULL,
                role TEXT DEFAULT 'user',
                is_banned INTEGER DEFAULT 0,
                session_token TEXT,
                password_hash TEXT
            )
        ''')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                room TEXT NOT NULL,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT DEFAULT 'text',
                timestamp TEXT NOT NULL
            )
        ''')
        await conn.execute('ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY,
                type TEXT DEFAULT 'group'
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS room_members (
                room_name TEXT,
                username TEXT,
                UNIQUE(room_name, username)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS contacts (
                owner_username TEXT NOT NULL,
                contact_username TEXT NOT NULL,
                UNIQUE(owner_username, contact_username)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS trusted_devices (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                device_token TEXT NOT NULL,
                device_name TEXT,
                last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, device_token)
            )
        ''')
        # E2EE: публичные ключи и запечатанные симметричные ключи чатов.
        # Сервер хранит их как непрозрачные base64 blob'ы и не может расшифровать.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS e2ee_keys (
                room TEXT NOT NULL,
                username TEXT NOT NULL,
                identity_pub TEXT NOT NULL,
                sealed_chat_key TEXT NOT NULL,
                dh_pub TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (room, username)
            )
        ''')
        await conn.execute('ALTER TABLE e2ee_keys ADD COLUMN IF NOT EXISTS dh_pub TEXT DEFAULT \'\'')
        # Тип комнаты: 'group' — групповой чат со сквозным шифрованием,
        # 'channel' — открытый канал без E2EE (сообщения хранятся в открытом виде).
        await conn.execute("ALTER TABLE rooms ADD COLUMN IF NOT EXISTS is_channel INTEGER DEFAULT 0")

        if not os.environ.get('ADMIN_PASSWORD'):
            print("ВНИМАНИЕ: ADMIN_PASSWORD не задан — используется пароль по умолчанию.")

        admin_password_hash = hash_password(ADMIN_PASSWORD)
        row = await conn.fetchrow("SELECT * FROM users WHERE username = 'Grom'")
        if not row:
            await conn.execute(
                "INSERT INTO users (id, username, display_name, email, role, password_hash) VALUES ($1, $2, $3, $4, $5, $6)",
                'AdminGrom1', 'Grom', 'Grom', hash_email('admin@sam.messenger'), 'admin', admin_password_hash
            )
            print("Создан аккаунт admin 'Grom'.")
        else:
            await conn.execute("UPDATE users SET password_hash = $1 WHERE username = 'Grom'", admin_password_hash)

async def finish_login(ws, pool, db_id, db_username, db_display_name, db_role, token):
    active_connections[ws]["username"] = db_username
    active_connections[ws]["display_name"] = db_display_name or db_username
    active_connections[ws]["user_id"] = db_id
    active_connections[ws]["role"] = db_role

    await ws.send_json({
        "type": "auth_result",
        "success": True,
        "user_id": db_id,
        "username": db_username,
        "display_name": db_display_name or db_username,
        "role": db_role,
        "token": token
    })

    groups_rows = await pool.fetch(
        "SELECT room_name FROM room_members WHERE username = $1 AND room_name NOT IN (SELECT name FROM rooms WHERE type='private')",
        db_username
    )
    groups = [r['room_name'] for r in groups_rows]
    # Каналы (is_channel=1) работают без E2EE — клиент должен знать, какие комнаты каналы.
    channel_rows = await pool.fetch(
        "SELECT name FROM rooms WHERE is_channel = 1"
    )
    channels = [r['name'] for r in channel_rows]

    private_rooms = []
    my_private_rooms = await pool.fetch('''
        SELECT r.name FROM rooms r
        JOIN room_members rm ON r.name = rm.room_name
        WHERE r.type = 'private' AND rm.username = $1
    ''', db_username)
    for r in my_private_rooms:
        room_uuid = r['name']
        members = await pool.fetch("SELECT username FROM room_members WHERE room_name = $1", room_uuid)
        if len(members) == 2:
            m_names = sorted([members[0]['username'], members[1]['username']])
            private_rooms.append(f"PRIVATE|{m_names[0]}|{m_names[1]}")

    saved_room = f"SAVED|{db_id}"

    await ws.send_json({
        "type": "init_data",
        "groups": groups,
        "channels": channels,
        "private_rooms": private_rooms,
        "saved_room": saved_room
    })

async def close_db(app):
    if 'db_pool' in app:
        await app['db_pool'].close()

def load_html(filename, request=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()
    # Десктоп-режим: страница грузится локально (file://), а WebSocket/fetch
    # идут на удалённый сервер. Адрес берётся из ?remote=... или SAM_SERVER_URL.
    remote = ''
    if request is not None:
        remote = request.query.get('remote', '')
    if not remote:
        remote = os.environ.get('SAM_SERVER_URL', '')
    safe_remote = _html_escape_url(remote.strip()) if remote else ''
    cfg = (f'window.SAM_SERVER_URL = "{safe_remote}";' if safe_remote
           else '/* local mode */')
    return html.replace('/*SAM_CFG*/', cfg, 1)

def _html_escape_url(url):
    # защита от выхода из JS-строки / разрыва тега <script>
    return (url.replace('\\', '\\\\').replace('"', '\\"')
               .replace('<', '\\u003c').replace('\n', '').replace('\r', ''))

# ─── WEB ROUTES ──────────────────────────────────────────────────────
@routes.get('/')
async def index(request):
    return web.Response(text=load_html('login.html', request), content_type='text/html')

@routes.get('/chat')
async def chat_page(request):
    return web.Response(text=load_html('chat.html', request), content_type='text/html')

@routes.get('/profile')
async def profile_page(request):
    return web.Response(text=load_html('profile.html', request), content_type='text/html')

@routes.get('/apps')
async def apps_page(request):
    return web.Response(text=load_html('apps.html', request), content_type='text/html')

@routes.get('/api/search')
async def search_handler(request):
    query = request.query.get('q', '').strip()
    if not query:
        return web.json_response([])
    pool = request.app['db_pool']
    search_pattern = f"{query}%"
    try:
        users_rows = await pool.fetch(
            "SELECT DISTINCT username AS name, display_name, 'user' AS type FROM users WHERE username ILIKE $1 LIMIT 10",
            search_pattern
        )
        rooms_rows = await pool.fetch(
            "SELECT DISTINCT name AS name, 'group' AS type FROM rooms WHERE name ILIKE $1 AND type = 'group' LIMIT 10",
            search_pattern
        )
        results = []
        for r in users_rows:
            results.append({"name": r['name'], "display_name": r['display_name'] or r['name'], "type": r['type']})
        for r in rooms_rows:
            results.append({"name": r['name'], "type": r['type']})
        return web.json_response(results)
    except Exception as e:
        print(f"Ошибка HTTP поиска: {e}")
        return web.json_response({"error": "Ошибка сервера при поиске"}, status=500)

@routes.post('/upload')
async def upload_file(request):
    reader = await request.multipart()
    field = await reader.next()
    if field and field.name == 'file':
        filename = field.filename
        ext = os.path.splitext(filename)[1].lower()
        unique_filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(UPLOAD_DIR, unique_filename)

        with open(filepath, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)

        mime_type = field.headers.get('Content-Type', '')
        if mime_type.startswith('image/'):
            msg_type = 'image'
            if ext != '.gif':
                try:
                    with PILImage.open(filepath) as img:
                        if img.mode in ("RGBA", "P") and ext in ['.jpg', '.jpeg']:
                            img = img.convert("RGB")
                        img.save(filepath, optimize=True, quality=75)
                except Exception as e:
                    print(f"Ошибка при сжатии изображения: {e}")
        elif mime_type.startswith('audio/') or ext in ['.mp3', '.wav', '.ogg', '.m4a']:
            msg_type = 'audio'
        elif mime_type.startswith('video/') or ext in ['.mp4', '.webm', '.mov']:
            msg_type = 'video'
        else:
            msg_type = 'file'

        return web.json_response({
            "success": True,
            "url": f"/uploads/{unique_filename}",
            "name": filename,
            "msg_type": msg_type
        })

    return web.json_response({"success": False, "error": "Файл не найден"})

# ─── WEBSOCKET ───────────────────────────────────────────────────────
@routes.get('/ws')
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    active_connections[ws] = {
        "username": None,
        "display_name": None,
        "user_id": None,
        "role": 'user',
        "room": None
    }
    pool = request.app['db_pool']

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get('action')
                sender_name = active_connections[ws]["username"]

                if action == 'request_code':
                    email = data.get('email', '').strip()
                    if not email:
                        await ws.send_json({"type": "auth_error", "text": "Укажите адрес электронной почты!"})
                        continue

                    code = str(random.randint(100000, 999999))
                    await pool.execute('''
                        INSERT INTO verification_codes (email, code)
                        VALUES ($1, $2)
                        ON CONFLICT (email) DO UPDATE SET code = $2
                    ''', email, code)

                    is_sent = await send_mailru_code(email, code)
                    if is_sent:
                        await ws.send_json({"type": "code_sent", "text": "Код отправлен на почту!"})
                    else:
                        await ws.send_json({"type": "auth_error", "text": "Ошибка при отправке письма."})
                    continue

                elif action == 'register':
                    email = data.get('email', '').strip()
                    code = data.get('code', '').strip()
                    username = data.get('username', '').strip()
                    password = data.get('password', '')

                    if not email or not code or not username or not password:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Заполните все поля!"})
                        continue
                    if len(password) < 6:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Пароль от 6 символов!"})
                        continue
                    if not USERNAME_PATTERN.match(username):
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Неверный формат username!"})
                        continue

                    is_code_valid = await pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM verification_codes WHERE email = $1 AND code = $2)",
                        email, code
                    )
                    if not is_code_valid:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Неверный код!"})
                        continue

                    try:
                        token = uuid.uuid4().hex
                        pw_hash = hash_password(password)
                        uid = await generate_user_id(pool)
                        await pool.execute(
                            "INSERT INTO users (id, username, display_name, email, session_token, password_hash) VALUES ($1, $2, $3, $4, $5, $6)",
                            uid, username, username, hash_email(email), token, pw_hash
                        )
                        await pool.execute("DELETE FROM verification_codes WHERE email = $1", email)
                        await finish_login(ws, pool, uid, username, username, "user", token)
                    except asyncpg.UniqueViolationError:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Ник или email заняты!"})

                elif action == 'login':
                    email = data.get('email', '').strip()
                    password = data.get('password', '')

                    if not email or not password:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Заполните поля!"})
                        continue

                    row = await pool.fetchrow(
                        "SELECT id, username, display_name, role, is_banned, password_hash FROM users WHERE email = $1",
                        hash_email(email)
                    )
                    if not row:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Пользователь не найден!"})
                        continue

                    db_id, db_username, db_display, db_role, is_banned, db_pw_hash = (
                        row['id'], row['username'], row['display_name'], row['role'], row['is_banned'], row['password_hash']
                    )

                    if is_banned:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                        continue
                    if not verify_password(password, db_pw_hash):
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Неверный пароль!"})
                        continue

                    token = uuid.uuid4().hex
                    await pool.execute("UPDATE users SET session_token = $1 WHERE id = $2", token, db_id)
                    await finish_login(ws, pool, db_id, db_username, db_display, db_role, token)

                elif action == 'session_login':
                    username = data.get('username', '').strip()
                    token = data.get('token', '').strip()
                    if not username or not token:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Сессия истекла.", "need_relogin": True})
                        continue

                    row = await pool.fetchrow(
                        "SELECT id, username, display_name, role, is_banned, session_token FROM users WHERE username = $1",
                        username
                    )
                    if not row or row['session_token'] != token:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Сессия истекла.", "need_relogin": True})
                    elif row['is_banned']:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                    else:
                        await finish_login(ws, pool, row['id'], row['username'], row['display_name'], row['role'], token)

                # === НОВЫЕ ЭКШЕНЫ: запоминание устройства ===
                elif action == 'check_trusted_device':
                    user_id = data.get('user_id', '').strip()
                    device_token = data.get('device_token', '').strip()
                    if not user_id or not device_token:
                        await ws.send_json({"type": "device_check_result", "trusted": False})
                        continue
                    row = await pool.fetchrow(
                        "SELECT 1 FROM trusted_devices WHERE user_id = $1 AND device_token = $2",
                        user_id, device_token
                    )
                    if row:
                        user_row = await pool.fetchrow(
                            "SELECT id, username, display_name, role, is_banned FROM users WHERE id = $1",
                            user_id
                        )
                        if user_row and not user_row['is_banned']:
                            token = uuid.uuid4().hex
                            await pool.execute("UPDATE users SET session_token = $1 WHERE id = $2", token, user_id)
                            await finish_login(ws, pool, user_row['id'], user_row['username'],
                                              user_row['display_name'], user_row['role'], token)
                            await ws.send_json({"type": "device_check_result", "trusted": True, "token": token})
                        else:
                            await ws.send_json({"type": "device_check_result", "trusted": False})
                    else:
                        await ws.send_json({"type": "device_check_result", "trusted": False})

                elif action == 'trust_device':
                    if not sender_name:
                        continue
                    device_token = data.get('device_token', '').strip()
                    device_name = data.get('device_name', 'Unknown Device')[:64]
                    user_id = active_connections[ws]["user_id"]
                    if device_token and user_id:
                        await pool.execute('''
                            INSERT INTO trusted_devices (user_id, device_token, device_name)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (user_id, device_token) DO UPDATE SET last_used = CURRENT_TIMESTAMP
                        ''', user_id, device_token, device_name)
                        await ws.send_json({"type": "device_trusted", "text": "Устройство запомнено"})

                elif action == 'untrust_device':
                    if not sender_name:
                        continue
                    device_token = data.get('device_token', '').strip()
                    user_id = active_connections[ws]["user_id"]
                    if device_token and user_id:
                        await pool.execute(
                            "DELETE FROM trusted_devices WHERE user_id = $1 AND device_token = $2",
                            user_id, device_token
                        )
                        await ws.send_json({"type": "device_untrusted", "text": "Устройство удалено из доверенных"})
                # === КОНЕЦ НОВЫХ ЭКШЕНОВ ===

                # === E2EE: РЕГИСТРАЦИЯ И ОБМЕН КЛЮЧАМИ ===
                elif action == 'e2ee_register':
                    if not sender_name:
                        continue
                    room = data.get('room', '')
                    identity_pub = data.get('identity_pub', '')
                    sealed_chat_key = data.get('sealed_chat_key', '')
                    dh_pub = data.get('dh_pub', '')
                    if not room or not identity_pub or not sealed_chat_key:
                        await ws.send_json({"type": "error_msg", "text": "E2EE: некорректные данные ключей"})
                        continue
                    if len(identity_pub) > 512 or len(sealed_chat_key) > 8192 or len(dh_pub) > 512:
                        await ws.send_json({"type": "error_msg", "text": "E2EE: ключи слишком длинные"})
                        continue
                    # Каналы работают без E2EE — ключи там не нужны.
                    is_channel_room = await pool.fetchval(
                        "SELECT COALESCE(is_channel, 0) FROM rooms WHERE name = $1", room
                    )
                    if is_channel_room:
                        await ws.send_json({"type": "e2ee_skipped", "room": room, "reason": "channel"})
                        continue
                    # Разрешаем публиковать ключи только участникам чата
                    can_post = False
                    if room.startswith("SAVED|"):
                        can_post = (room == f"SAVED|{active_connections[ws]['user_id']}")
                    elif room.startswith("PRIVATE|"):
                        parts = room.split('|')
                        can_post = len(parts) == 3 and sender_name in (parts[1], parts[2])
                    else:
                        member = await pool.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM room_members WHERE room_name = $1 AND username = $2)",
                            room, sender_name
                        )
                        can_post = bool(member)
                    if not can_post:
                        await ws.send_json({"type": "error_msg", "text": "E2EE: вы не состоите в этом чате"})
                        continue
                    await pool.execute('''
                        INSERT INTO e2ee_keys (room, username, identity_pub, sealed_chat_key, dh_pub)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (room, username)
                        DO UPDATE SET identity_pub = $3, sealed_chat_key = $4, dh_pub = $5, updated_at = CURRENT_TIMESTAMP
                    ''', room, sender_name, identity_pub, sealed_chat_key, dh_pub)
                    await ws.send_json({"type": "e2ee_registered", "room": room})

                elif action == 'e2ee_get_keys':
                    if not sender_name:
                        continue
                    room = data.get('room', '')
                    if not room:
                        continue
                    # Раздаём ключи только участникам чата
                    can_read = False
                    peer_names = []
                    if room.startswith("SAVED|"):
                        can_read = (room == f"SAVED|{active_connections[ws]['user_id']}")
                        peer_names = [sender_name]
                    elif room.startswith("PRIVATE|"):
                        parts = room.split('|')
                        can_read = len(parts) == 3 and sender_name in (parts[1], parts[2])
                        peer_names = [p for p in parts[1:3]] if can_read else []
                    else:
                        member = await pool.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM room_members WHERE room_name = $1 AND username = $2)",
                            room, sender_name
                        )
                        can_read = bool(member)
                        if can_read:
                            mrows = await pool.fetch(
                                "SELECT username FROM room_members WHERE room_name = $1", room
                            )
                            peer_names = [r['username'] for r in mrows]
                    if not can_read or not peer_names:
                        await ws.send_json({"type": "e2ee_keys", "room": room, "keys": {}})
                        continue
                    rows = await pool.fetch(
                        "SELECT username, identity_pub, sealed_chat_key, dh_pub FROM e2ee_keys "
                        "WHERE room = $1 AND username = ANY($2::text[])",
                        room, peer_names
                    )
                    keys = {r['username']: {
                        "identity_pub": r['identity_pub'],
                        "sealed_chat_key": r['sealed_chat_key'],
                        "dh_pub": r['dh_pub'] or ''
                    } for r in rows}
                    await ws.send_json({"type": "e2ee_keys", "room": room, "keys": keys})

                elif action == 'update_profile':
                    if not sender_name:
                        continue
                    display_name = data.get('display_name', '').strip()
                    if len(display_name) > 32:
                        display_name = display_name[:32]
                    await pool.execute(
                        "UPDATE users SET display_name = $1 WHERE username = $2",
                        display_name or None, sender_name
                    )
                    active_connections[ws]["display_name"] = display_name or sender_name
                    await ws.send_json({"type": "profile_updated", "display_name": display_name or sender_name})

                elif action == 'get_user_profile':
                    target = data.get('username', sender_name).strip()
                    row = await pool.fetchrow(
                        "SELECT username, display_name, role FROM users WHERE username = $1", target
                    )
                    if row:
                        await ws.send_json({
                            "type": "user_profile",
                            "username": row['username'],
                            "display_name": row['display_name'] or row['username'],
                            "role": row['role']
                        })

                elif action == 'get_contacts':
                    if not sender_name:
                        continue
                    rows = await pool.fetch(
                        "SELECT contact_username FROM contacts WHERE owner_username = $1 ORDER BY contact_username",
                        sender_name
                    )
                    contacts = [r['contact_username'] for r in rows]
                    await ws.send_json({"type": "contacts_list", "contacts": contacts})

                elif action == 'add_contact':
                    if not sender_name:
                        continue
                    target = data.get('username', '').strip()
                    if not target or target == sender_name:
                        await ws.send_json({"type": "error_msg", "text": "Нельзя добавить себя!"})
                        continue
                    user_exists = await pool.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE username = $1)", target)
                    if not user_exists:
                        await ws.send_json({"type": "error_msg", "text": "Пользователь не найден!"})
                        continue
                    try:
                        await pool.execute(
                            "INSERT INTO contacts (owner_username, contact_username) VALUES ($1, $2)",
                            sender_name, target
                        )
                        await ws.send_json({"type": "contact_added", "username": target})
                    except asyncpg.UniqueViolationError:
                        await ws.send_json({"type": "error_msg", "text": "Уже в контактах!"})

                elif action == 'remove_contact':
                    if not sender_name:
                        continue
                    target = data.get('username', '').strip()
                    await pool.execute(
                        "DELETE FROM contacts WHERE owner_username = $1 AND contact_username = $2",
                        sender_name, target
                    )
                    await ws.send_json({"type": "contact_removed", "username": target})

                elif action == 'search_users':
                    if not sender_name:
                        continue
                    query = data.get('query', '').strip()
                    if query:
                        search_pattern = f"{query}%"
                        rows = await pool.fetch(
                            "SELECT username, display_name FROM users WHERE username ILIKE $1 AND username != $2 LIMIT 10",
                            search_pattern, sender_name
                        )
                        results = [r['username'] for r in rows]
                        await ws.send_json({"type": "contact_search_results", "results": results})

                elif action == 'search':
                    query = data.get('query', '').strip()
                    if query:
                        search_pattern = f"{query}%"
                        try:
                            users_rows = await pool.fetch(
                                "SELECT DISTINCT username AS name, display_name, 'user' AS type FROM users WHERE username ILIKE $1 LIMIT 10",
                                search_pattern
                            )
                            rooms_rows = await pool.fetch(
                                "SELECT DISTINCT name AS name, 'group' AS type FROM rooms WHERE name ILIKE $1 AND type = 'group' LIMIT 10",
                                search_pattern
                            )
                            results = []
                            for r in users_rows:
                                results.append({"name": r['name'], "display_name": r['display_name'] or r['name'], "type": r['type']})
                            for r in rooms_rows:
                                results.append({"name": r['name'], "type": r['type']})
                            await ws.send_json({"type": "search_results", "results": results})
                        except Exception as e:
                            print(f"Ошибка поиска: {e}")
                            await ws.send_json({"type": "error_msg", "text": "Ошибка при поиске"})

                elif action == 'create_group':
                    if not sender_name:
                        continue
                    group_name = data.get('name', '').strip()
                    if group_name:
                        try:
                            await pool.execute("INSERT INTO rooms (name, type) VALUES ($1, 'group')", group_name)
                            await pool.execute(
                                "INSERT INTO room_members (room_name, username) VALUES ($1, $2)",
                                group_name, sender_name
                            )
                            await ws.send_json({"type": "new_group", "name": group_name})
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "error_msg", "text": "Группа уже существует!"})

                elif action == 'create_channel':
                    # Канал — открытая комната без сквозного шифрования (как в Bitrix24).
                    if not sender_name or active_connections.get(ws, {}).get("role") != 'admin':
                        await ws.send_json({"type": "error_msg", "text": "Каналы могут создавать только администраторы."})
                        continue
                    channel_name = data.get('name', '').strip()
                    if channel_name and len(channel_name) <= 64:
                        try:
                            await pool.execute(
                                "INSERT INTO rooms (name, type, is_channel) VALUES ($1, 'group', 1)", channel_name
                            )
                            await pool.execute(
                                "INSERT INTO room_members (room_name, username) VALUES ($1, $2)",
                                channel_name, sender_name
                            )
                            for client in list(active_connections.keys()):
                                try:
                                    await client.send_json({"type": "new_channel", "name": channel_name})
                                except Exception:
                                    pass
                            await ws.send_json({"type": "success_msg", "text": f"Канал «{channel_name}» создан (E2EE выключен)."})
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "error_msg", "text": "Канал с таким названием уже существует!"})
                    elif channel_name:
                        await ws.send_json({"type": "error_msg", "text": "Слишком длинное название канала."})

                elif action == 'toggle_channel':
                    # Включить/выключить режим канала для комнаты.
                    if not sender_name or active_connections.get(ws, {}).get("role") != 'admin':
                        await ws.send_json({"type": "error_msg", "text": "Назначать каналы может только администратор."})
                        continue
                    room = data.get('room', '')
                    target_val = int(bool(data.get('is_channel')))
                    if room and not room.startswith(("PRIVATE|", "SAVED|")):
                        changed = await pool.execute(
                            "UPDATE rooms SET is_channel = $1 WHERE name = $2 AND type != 'private'",
                            target_val, room
                        )
                        if changed and changed.endswith("1"):
                            # При переходе в канал старые зашифрованные ключи не нужны.
                            await pool.execute("DELETE FROM e2ee_keys WHERE room = $1", room)
                            await ws.send_json({
                                "type": "channel_toggled", "room": room, "is_channel": bool(target_val),
                                "text": f"«{room}»: {'E2EE выключен (канал)' if target_val else 'E2EE включён (группа)'}"
                            })
                        else:
                            await ws.send_json({"type": "error_msg", "text": "Комната не найдена."})

                elif action == 'add_to_group':
                    if not sender_name:
                        continue
                    target_user = data.get('username')
                    room = data.get('room')
                    if target_user and room:
                        try:
                            await pool.execute(
                                "INSERT INTO room_members (room_name, username) VALUES ($1, $2)",
                                room, target_user
                            )
                            for client, info in list(active_connections.items()):
                                if info["username"] == target_user:
                                    await client.send_json({"type": "new_group", "name": room})
                            await ws.send_json({"type": "success_msg", "text": f"{target_user} добавлен!"})
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "error_msg", "text": "Уже в группе!"})

                elif action == 'change_nickname':
                    if not sender_name:
                        continue
                    old_username = active_connections[ws]["username"]
                    new_username = data.get('new_nickname', '').strip()
                    if not old_username or not new_username or new_username == old_username:
                        continue
                    if not USERNAME_PATTERN.match(new_username):
                        await ws.send_json({"type": "error_msg", "text": "Неверный формат username!"})
                        continue
                    try:
                        async with pool.acquire() as transaction_conn:
                            async with transaction_conn.transaction():
                                await transaction_conn.execute(
                                    "UPDATE users SET username = $1 WHERE username = $2",
                                    new_username, old_username
                                )
                                await transaction_conn.execute(
                                    "UPDATE room_members SET username = $1 WHERE username = $2",
                                    new_username, old_username
                                )
                        active_connections[ws]["username"] = new_username
                        await ws.send_json({"type": "nickname_changed", "new_name": new_username})
                    except asyncpg.UniqueViolationError:
                        await ws.send_json({"type": "error_msg", "text": "Ник занят!"})

                elif action == 'send_msg':
                    user_info = active_connections.get(ws)
                    if not user_info or not user_info["username"] or not user_info["user_id"]:
                        continue
                    room = data.get('room')
                    if not room:
                        continue
                    content = data.get('content', '')
                    msg_type = data.get('msg_type', 'text')
                    if msg_type == 'text' and len(content) > 1000:
                        await ws.send_json({"type": "error_msg", "text": "Слишком длинное сообщение!"})
                        continue

                    time_str = datetime.datetime.now().strftime("%H:%M")

                    res = await pool.fetchrow(
                        "SELECT is_banned FROM users WHERE username = $1", user_info["username"]
                    )
                    if res and res['is_banned'] == 1:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены!"})
                        continue

                    if room.startswith("SAVED|"):
                        db_room = room
                    elif room.startswith("PRIVATE|"):
                        db_room = await get_or_create_private_room(pool, room)
                    else:
                        db_room = room

                    # E2EE: content приходит с клиента уже зашифрованным (AES-GCM).
                    # Сервер хранит и пересылает blob как есть, не имея ключей.
                    if not isinstance(content, str) or len(content) > 100_000:
                        await ws.send_json({"type": "error_msg", "text": "Некорректное содержимое сообщения!"})
                        continue
                    msg_id = await pool.fetchval(
                        "INSERT INTO messages (room, user_id, content, msg_type, timestamp) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                        db_room, user_info["user_id"], content, msg_type, time_str
                    )

                    allowed_users = set()
                    is_private = False
                    if room.startswith("SAVED|"):
                        is_private = True
                        allowed_users = {user_info["username"]}
                    elif room.startswith("PRIVATE|"):
                        is_private = True
                        parts = room.split('|')
                        if len(parts) == 3:
                            allowed_users = {parts[1], parts[2]}
                    else:
                        members_rows = await pool.fetch(
                            "SELECT username FROM room_members WHERE room_name = $1", room
                        )
                        allowed_users = {r['username'] for r in members_rows}

                    sender_display = user_info.get("display_name") or user_info["username"]

                    for client, info in list(active_connections.items()):
                        if not info["username"]:
                            continue
                        if is_private:
                            if info["username"] not in allowed_users:
                                continue
                        else:
                            if info["username"] not in allowed_users and info.get("room") != room:
                                continue
                        try:
                            await client.send_json({
                                "type": "msg",
                                "id": msg_id,
                                "room": room,
                                "username": user_info["username"],
                                "nickname": sender_display,
                                "display_name": sender_display,
                                "content": content,
                                "msg_type": msg_type,
                                "time": time_str
                            })
                        except Exception:
                            pass

                elif action == 'join_room':
                    room = data.get('room')
                    active_connections[ws]["room"] = room
                    await ws.send_json({"type": "clear_chat"})

                    if room.startswith("SAVED|"):
                        db_room = room
                    elif room.startswith("PRIVATE|"):
                        db_room = await get_or_create_private_room(pool, room)
                    else:
                        db_room = room

                    history_rows = await pool.fetch('''
                        SELECT id, username, display_name, content, msg_type, timestamp FROM (
                            SELECT m.id, u.username, u.display_name, m.content, m.msg_type, m.timestamp
                            FROM messages m
                            JOIN users u ON m.user_id = u.id
                            WHERE m.room = $1
                            ORDER BY m.id DESC
                            LIMIT 100
                        ) sub
                        ORDER BY id ASC
                    ''', db_room)
                    for r in history_rows:
                        # E2EE: сервер отдаёт зашифрованный blob как есть —
                        # расшифровка только на устройствах участников чата.
                        await ws.send_json({
                            "type": "msg",
                            "id": r['id'],
                            "room": room,
                            "username": r['username'],
                            "display_name": r['display_name'] or r['username'],
                            "content": r['content'],
                            "msg_type": r['msg_type'],
                            "time": r['timestamp'],
                            "history": True
                        })

                elif action == 'delete_msg':
                    if active_connections[ws]["role"] == 'admin':
                        msg_id = data.get('msg_id')
                        # E2EE: сервер не может подменить содержимое осмысленным
                        # текстом — помечаем сообщение удалённым флагом is_deleted.
                        await pool.execute(
                            "UPDATE messages SET is_deleted = 1 WHERE id = $2",
                            msg_id
                        )
                        for client in list(active_connections.keys()):
                            try:
                                await client.send_json({"type": "delete_evt", "id": msg_id})
                            except Exception:
                                pass

                elif action == 'ban_user':
                    if active_connections[ws]["role"] == 'admin':
                        target_user = data.get('username')
                        if target_user == 'Grom':
                            continue
                        await pool.execute(
                            "UPDATE users SET is_banned = 1 WHERE username = $1", target_user
                        )
                        for client, info in list(active_connections.items()):
                            if info["username"] == target_user:
                                await client.send_json({"type": "banned"})
                                await client.close()

                elif action == 'call_user':
                    if not sender_name:
                        continue
                    target = data.get('target')
                    target_ws = get_ws_by_username(target)
                    if target_ws:
                        await target_ws.send_json({"type": "incoming_call", "from": sender_name})
                    else:
                        await ws.send_json({"type": "error_msg", "text": "Оффлайн"})

                elif action == 'call_response':
                    if not sender_name:
                        continue
                    target = data.get('target')
                    accepted = data.get('accepted')
                    target_ws = get_ws_by_username(target)
                    if target_ws:
                        await target_ws.send_json({
                            "type": "call_response",
                            "from": sender_name,
                            "accepted": accepted
                        })

                elif action == 'webrtc_signal':
                    if not sender_name:
                        continue
                    target = data.get('target')
                    target_ws = get_ws_by_username(target)
                    if target_ws:
                        await target_ws.send_json({
                            "type": "webrtc_signal",
                            "from": sender_name,
                            "signal": data.get('signal')
                        })

    finally:
        active_connections.pop(ws, None)

    return ws

app = web.Application()
app.add_routes(routes)
app.router.add_static('/uploads/', path=UPLOAD_DIR, name='uploads')
app.on_startup.append(init_db)
app.on_cleanup.append(close_db)
app.on_startup.append(lambda app: asyncio.ensure_future(e2ee_cleanup(app)))

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))