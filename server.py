import asyncio
import os
import json
import datetime
import uuid
import hashlib
import sys
import ssl
import aiohttp
from aiohttp import web
import asyncpg
from PIL import Image as PILImage
import random

routes = web.RouteTableDef()

@routes.post('/register')
async def register_handler(request):
    data = await request.json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return web.json_response({'error': 'Заполните все поля'}, status=400)

    pool = request.app['pool']
    async with pool.acquire() as conn:
        existing = await conn.fetchrow('SELECT id FROM users WHERE username = $1', username)
        if existing:
            return web.json_response({'error': 'Никнейм занят'}, status=409)

        hashed_pw = hashlib.sha256(password.encode()).hexdigest()
        token = str(uuid.uuid4())

        await conn.execute(
            'INSERT INTO users (username, password_hash, token) VALUES ($1, $2, $3)',
            username, hashed_pw, token
        )

    return web.json_response({'success': True, 'username': username, 'token': token})




# Настройки Mail.ru для отправки писем
MAILRU_USER = os.environ.get('EMAIL_USER', 'sam_official@inbox.ru')
MAILRU_PASS = os.environ.get('EMAIL_PASSWORD', 'tk7l6KKRnqjmW1f9Oxkp') # 16-значный пароль приложения

routes = web.RouteTableDef()
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres.btwkssbcdaltjrceufqz:mYOgVhNRNGMMXe9o@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?pgbouncer=true')
UPLOAD_DIR = 'uploads'

# Исправление багов asyncio с сетью на Windows при локальном тестировании
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

active_connections = {}

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def get_ws_by_username(username):
    """Поиск активного веб-сокета пользователя по его нику для звонков"""
    for ws, info in list(active_connections.items()):
        if info.get('username') == username:
            return ws
    return None

async def get_or_create_private_room(pool, room_str: str) -> str:
    """
    Принимает строку вида 'PRIVATE|User1|User2', находит или создает 
    для них комнату со случайным UUID и возвращает этот UUID.
    """
    parts = room_str.split('|')
    if len(parts) != 3:
        return room_str
    
    # Сортируем пользователей по алфавиту для консистентности
    user1, user2 = sorted([parts[1], parts[2]])
    
    # Ищем, существует ли уже UUID комната для этой пары пользователей
    row = await pool.fetchrow('''
        SELECT r.name FROM rooms r
        JOIN room_members rm1 ON r.name = rm1.room_name
        JOIN room_members rm2 ON r.name = rm2.room_name
        WHERE r.type = 'private' AND rm1.username = $1 AND rm2.username = $2
    ''', user1, user2)
    
    if row:
        return row['name']
    
    # Если комнаты нет, создаем новую с UUID
    new_uuid = str(uuid.uuid4())
    await pool.execute("INSERT INTO rooms (name, type) VALUES ($1, 'private')", new_uuid)
    await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", new_uuid, user1)
    await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", new_uuid, user2)
    return new_uuid


# Асинхронная функция отправки кода на Mail.ru
async def send_mailru_code(user_email, code):
    proxy_url = "https://resend-u5gj.vercel.app/api" 
    
    payload = {
        "to": user_email,
        "code": code
    }

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

# Инициализация базы данных PostgreSQL
async def init_db(app):
    global DATABASE_URL
    
    if 'RENDER' in os.environ and ("localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL):
        print("КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL не задана в настройках Render Environment!")
    
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    # Настройка безопасного SSL-соединения для облака
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
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                role TEXT DEFAULT 'user',
                is_banned INTEGER DEFAULT 0,
                session_token TEXT
            )
        ''')

        # Миграция для БД, созданных до появления колонки session_token
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                room TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT DEFAULT 'text',
                timestamp TEXT NOT NULL
            )
        ''')
        
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
        
        # Создание админа Grom
        row = await conn.fetchrow("SELECT * FROM users WHERE username = 'Grom'")
        if not row:
            await conn.execute(
                "INSERT INTO users (username, email, role) VALUES ($1, $2, $3)",
                'Grom', 'admin@sam.messenger', 'admin'
            )

async def finish_login(ws, pool, db_id, db_username, db_role, token):
    """
    Общая логика после успешной проверки личности (по коду с почты ИЛИ по токену устройства):
    поднимает сессию на сокете и отправляет клиенту его комнаты/чаты.
    """
    active_connections[ws]["username"] = db_username
    active_connections[ws]["user_id"] = db_id
    active_connections[ws]["role"] = db_role

    await ws.send_json({"type": "auth_result", "success": True, "username": db_username, "role": db_role, "token": token})

    groups_rows = await pool.fetch(
        "SELECT room_name FROM room_members WHERE username = $1 AND room_name NOT IN (SELECT name FROM rooms WHERE type='private')",
        db_username
    )
    groups = [r['room_name'] for r in groups_rows]

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

    await ws.send_json({"type": "init_data", "groups": groups, "private_rooms": private_rooms})


async def close_db(app):
    if 'db_pool' in app:
        await app['db_pool'].close()

def load_html(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

# --- МАРШРУТЫ ДЛЯ СТРАНИЦ (WEB ROUTES) ---

@routes.get('/')
async def index(request):
    return web.Response(text=load_html('login.html'), content_type='text/html')

@routes.get('/chat')
async def chat_page(request):
    return web.Response(text=load_html('chat.html'), content_type='text/html')

@routes.get('/profile')
async def profile_page(request):
    return web.Response(text=load_html('profile.html'), content_type='text/html')

@routes.get('/apps')
async def apps_page(request):
    return web.Response(text=load_html('apps.html'), content_type='text/html')

# --- ПОИСК ЧЕРЕЗ HTTP API (FETCH) ---
@routes.get('/api/search')
async def search_handler(request):
    query = request.query.get('q', '').strip()
    if not query:
        return web.json_response([])

    pool = request.app['db_pool']
    search_pattern = f"{query}%"

    try:
        users_rows = await pool.fetch("SELECT DISTINCT username AS name, 'user' AS type FROM users WHERE username ILIKE $1 LIMIT 10", search_pattern)
        rooms_rows = await pool.fetch("SELECT DISTINCT name AS name, 'group' AS type FROM rooms WHERE name ILIKE $1 AND type = 'group' LIMIT 10", search_pattern)
        
        results = []
        for r in users_rows:
            results.append({"name": r['name'], "type": r['type']})
        for r in rooms_rows:
            results.append({"name": r['name'], "type": r['type']})
            
        return web.json_response(results)
    except Exception as e:
        print(f"Ошибка HTTP поиска: {e}")
        return web.json_response({"error": "Ошибка сервера при поиске"}, status=500)

# --- ЗАГРУЗКА МЕДИАФАЙЛОВ ---

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

# --- ОБРАБОТКА WEBSOCKET ---

@routes.get('/ws')
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    active_connections[ws] = {"username": None, "user_id": None, "role": 'user', "room": None}
    
    pool = request.app['db_pool']
    
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get('action')
                sender_name = active_connections[ws]["username"]
                
                # --- Обработка запроса одноразового кода ---
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
                        await ws.send_json({"type": "auth_error", "text": "Ошибка при отправке письма. Проверьте настройки SMTP."})
                    continue

                elif action in ['login', 'register']:
                    email = data.get('email', '').strip()
                    code = data.get('code', '').strip()
                    
                    if not email or not code:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Заполните поля электронной почты и кода!"})
                        continue
                    
                    # ПРОВЕРКА КОДА в таблице verification_codes
                    is_code_valid = await pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM verification_codes WHERE email = $1 AND code = $2)", 
                        email, code
                    )
                    
                    if not is_code_valid:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Неверный код подтверждения!"})
                        continue
                        
                    if action == 'register':
                        username = data.get('username', '').strip()
                        if not username:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Укажите имя пользователя для регистрации!"})
                            continue
                        
                        try:
                            token = uuid.uuid4().hex
                            user_id = await pool.fetchval(
                                "INSERT INTO users (username, email, session_token) VALUES ($1, $2, $3) RETURNING id",
                                username, email, token
                            )
                            
                            await pool.execute("DELETE FROM verification_codes WHERE email = $1", email)
                            
                            await finish_login(ws, pool, user_id, username, "user", token)
                            
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Этот никнейм или email уже заняты!"})
                    
                    elif action == 'login':
                        row = await pool.fetchrow("SELECT id, username, role, is_banned FROM users WHERE email = $1", email)
                        
                        if row:
                            db_id, db_username, db_role, is_banned = row['id'], row['username'], row['role'], row['is_banned']
                            
                            if is_banned:
                                await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                            else:
                                # Новый токен устройства выдаётся при каждом входе по почте,
                                # чтобы дальше это устройство могло входить без повторного запроса кода
                                token = uuid.uuid4().hex
                                await pool.execute("UPDATE users SET session_token = $1 WHERE id = $2", token, db_id)
                                await pool.execute("DELETE FROM verification_codes WHERE email = $1", email)
                                
                                await finish_login(ws, pool, db_id, db_username, db_role, token)
                        else:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Пользователь не найден! Зарегистрируйтесь."})

                elif action == 'session_login':
                    # Тихий повторный вход по сохранённому на устройстве токену — без почты и кода
                    username = data.get('username', '').strip()
                    token = data.get('token', '').strip()
                    
                    if not username or not token:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Сессия недействительна, войдите заново.", "need_relogin": True})
                        continue
                    
                    row = await pool.fetchrow("SELECT id, username, role, is_banned, session_token FROM users WHERE username = $1", username)
                    
                    if not row or row['session_token'] != token:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Сессия недействительна, войдите заново.", "need_relogin": True})
                    elif row['is_banned']:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                    else:
                        await finish_login(ws, pool, row['id'], row['username'], row['role'], token)

                elif action == 'get_users':
                    if not sender_name: continue
                    users_rows = await pool.fetch("SELECT username FROM users WHERE username != $1", active_connections[ws]["username"])
                    users = [r['username'] for r in users_rows]
                    await ws.send_json({"type": "user_list", "users": users})

                elif action == 'search':
                    query = data.get('query', '').strip()
                    if query:
                        search_pattern = f"{query}%"
                        try:
                            users_rows = await pool.fetch("SELECT DISTINCT username AS name, 'user' AS type FROM users WHERE username ILIKE $1 LIMIT 10", search_pattern)
                            rooms_rows = await pool.fetch("SELECT DISTINCT name AS name, 'group' AS type FROM rooms WHERE name ILIKE $1 AND type = 'group' LIMIT 10", search_pattern)
                            
                            results = []
                            for r in users_rows:
                                results.append({"name": r['name'], "type": r['type']})
                            for r in rooms_rows:
                                results.append({"name": r['name'], "type": r['type']})
                                
                            await ws.send_json({"type": "search_results", "results": results})
                        except Exception as e:
                            print(f"Ошибка WebSocket поиска: {e}")
                            await ws.send_json({"type": "error_msg", "text": "Ошибка при поиске"})

                elif action == 'create_group':
                    if not sender_name: continue
                    group_name = data.get('name', '').strip()
                    if group_name:
                        try:
                            await pool.execute("INSERT INTO rooms (name, type) VALUES ($1, 'group')", group_name)
                            await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", group_name, active_connections[ws]["username"])
                            await ws.send_json({"type": "new_group", "name": group_name})
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "error_msg", "text": "Группа с таким именем уже существует!"})

                elif action == 'add_to_group':
                    if not sender_name: continue
                    target_user = data.get('username')
                    room = data.get('room')
                    if target_user and room:
                        try:
                            await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", room, target_user)
                            for client, info in list(active_connections.items()):
                                if info["username"] == target_user:
                                    await client.send_json({"type": "new_group", "name": room})
                            await ws.send_json({"type": "success_msg", "text": f"Пользователь {target_user} добавлен в группу!"})
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "error_msg", "text": "Этот пользователь уже в группе!"})

                elif action == 'change_nickname':
                    if not sender_name: continue
                    old_username = active_connections[ws]["username"]
                    new_username = data.get('new_nickname', '').strip()
                    
                    if not old_username: continue
                    if not new_username or new_username == old_username: continue
                    
                    try:
                        async with pool.acquire() as transaction_conn:
                            async with transaction_conn.transaction():
                                await transaction_conn.execute("UPDATE users SET username = $1 WHERE username = $2", new_username, old_username)
                                await transaction_conn.execute("UPDATE room_members SET username = $1 WHERE username = $2", new_username, old_username)
                        
                        active_connections[ws]["username"] = new_username
                        await ws.send_json({"type": "nickname_changed", "new_name": new_username})
                    except asyncpg.UniqueViolationError:
                        await ws.send_json({"type": "error_msg", "text": "Этот ник уже занят!"})

                elif action == 'send_msg':
                    user_info = active_connections.get(ws)
                    if not user_info or not user_info["username"] or not user_info["user_id"]: continue
                    
                    room = data.get('room')
                    if not room: continue
                    content = data.get('content', '')
                    msg_type = data.get('msg_type', 'text')
                    
                    if msg_type == 'text' and len(content) > 1000:
                        await ws.send_json({"type": "error_msg", "text": "Сообщение слишком длинное (лимит 1000 символов)!"})
                        continue
                        
                    time_str = datetime.datetime.now().strftime("%H:%M")
                    
                    res = await pool.fetchrow("SELECT is_banned FROM users WHERE username = $1", user_info["username"])
                    if res and res['is_banned'] == 1:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены!"})
                        continue

                    db_room = room
                    if room.startswith("PRIVATE|"):
                        db_room = await get_or_create_private_room(pool, room)
                    
                    msg_id = await pool.fetchval(
                        "INSERT INTO messages (room, user_id, content, msg_type, timestamp) VALUES ($1, $2, $3, $4, $5) RETURNING id", 
                        db_room, user_info["user_id"], content, msg_type, time_str
                    )
                    
                    allowed_users = set()
                    is_private = False
                    
                    if room.startswith("PRIVATE|"):
                        is_private = True
                        parts = room.split('|')
                        if len(parts) == 3:
                            allowed_users = {parts[1], parts[2]}
                    else:
                        members_rows = await pool.fetch("SELECT username FROM room_members WHERE room_name = $1", room)
                        allowed_users = {r['username'] for r in members_rows}
                    
                    for client, info in list(active_connections.items()):
                        if info["username"]:
                            if is_private:
                                if info["username"] not in allowed_users: continue
                            else:
                                if info["username"] not in allowed_users and info.get("room") != room:
                                    continue
                            
                            try:
                                await client.send_json({
                                    "type": "msg", "id": msg_id, "room": room, 
                                    "username": user_info["username"], "content": content, "msg_type": msg_type, "time": time_str
                                })
                            except Exception:
                                pass

                elif action == 'join_room':
                    room = data.get('room')
                    active_connections[ws]["room"] = room
                    await ws.send_json({"type": "clear_chat"})
                    
                    db_room = room
                    if room.startswith("PRIVATE|"):
                        db_room = await get_or_create_private_room(pool, room)
                    
                    history_rows = await pool.fetch('''
                        SELECT m.id, u.username, m.content, m.msg_type, m.timestamp 
                        FROM messages m
                        JOIN users u ON m.user_id = u.id
                        WHERE m.room = $1 
                        ORDER BY m.id ASC 
                        LIMIT 100
                    ''', db_room)
                    
                    for r in history_rows:
                        await ws.send_json({"type": "msg", "id": r['id'], "room": room, "username": r['username'], "content": r['content'], "msg_type": r['msg_type'], "time": r['timestamp'], "history": True})

                elif action == 'delete_msg':
                    if active_connections[ws]["role"] == 'admin':
                        msg_id = data.get('msg_id')
                        await pool.execute("UPDATE messages SET content = 'Сообщение удалено администратором', msg_type = 'system' WHERE id = $1", msg_id)
                        for client in list(active_connections.keys()):
                            try:
                                await client.send_json({"type": "delete_evt", "id": msg_id})
                            except Exception:
                                pass
                            
                elif action == 'ban_user':
                    if active_connections[ws]["role"] == 'admin':
                        target_user = data.get('username')
                        if target_user == 'Grom': continue
                        await pool.execute("UPDATE users SET is_banned = 1 WHERE username = $1", target_user)
                        for client, info in list(active_connections.items()):
                            if info["username"] == target_user:
                                await client.send_json({"type": "banned"})
                                await client.close()

                elif action == 'call_user':
                    if not sender_name: continue
                    target = data.get('target')
                    target_ws = get_ws_by_username(target)
                    if target_ws:
                        await target_ws.send_json({
                            "type": "incoming_call",
                            "from": sender_name
                        })
                    else:
                        await ws.send_json({"type": "error_msg", "text": "Пользователь сейчас оффлайн"})

                elif action == 'call_response':
                    if not sender_name: continue
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
                    if not sender_name: continue
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

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
