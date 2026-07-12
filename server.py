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

routes = web.RouteTableDef()
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:mYOgVhNRNGMMXe9o@db.btwkssbcdaltjrceufqz.supabase.co:5432/postgres')
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

# Инициализация базы данных PostgreSQL
async def init_db(app):
    global DATABASE_URL
    
    if 'RENDER' in os.environ and ("localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL):
        print("КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL не задана в настройках Render Environment!")
    
    # Решаем проблему Render: asyncpg требует префикс postgresql:// вместо postgres://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    # Настройка безопасного SSL-соединения для облака
    if "localhost" not in DATABASE_URL and "127.0.0.1" not in DATABASE_URL:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')
    else:
        pool = await asyncpg.create_pool(DATABASE_URL)
        
    app['db_pool'] = pool
    
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                is_banned INTEGER DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                room TEXT NOT NULL,
                username TEXT NOT NULL,
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
        
        row = await conn.fetchrow("SELECT * FROM users WHERE username = 'Grom'")
        if not row:
            hashed_admin_pass = hash_password('12344321')
            await conn.execute(
                "INSERT INTO users (username, password, role) VALUES ($1, $2, $3)",
                'Grom', hashed_admin_pass, 'admin'
            )

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
        rooms_rows = await pool.fetch("SELECT DISTINCT name AS name, 'group' AS type FROM rooms WHERE name ILIKE $1 LIMIT 10", search_pattern)
        
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
    active_connections[ws] = {"username": None, "role": 'user', "room": None}
    
    pool = request.app['db_pool']
    
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get('action')
                sender_name = active_connections[ws]["username"]
                
                if action in ['login', 'register']:
                    username = data.get('username', '').strip()
                    password = data.get('password', '').strip()
                    if not username or not password:
                        await ws.send_json({"type": "auth_result", "success": False, "error": "Заполните поля!"})
                        continue
                    
                    hashed_pass = hash_password(password)
                        
                    if action == 'register':
                        try:
                            await pool.execute("INSERT INTO users (username, password) VALUES ($1, $2)", username, hashed_pass)
                            await ws.send_json({"type": "auth_result", "success": True, "username": username, "role": "user"})
                            active_connections[ws]["username"] = username
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Имя занято!"})
                    
                    elif action == 'login':
                        row = await pool.fetchrow("SELECT role, is_banned, password FROM users WHERE username = $1", username)
                        if row:
                            db_role, is_banned, db_password = row['role'], row['is_banned'], row['password']
                            if is_banned:
                                await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                            elif db_password == hashed_pass:
                                active_connections[ws]["username"] = username
                                active_connections[ws]["role"] = db_role
                                await ws.send_json({"type": "auth_result", "success": True, "username": username, "role": db_role})
                                
                                groups_rows = await pool.fetch("SELECT room_name FROM room_members WHERE username = $1", username)
                                groups = [r['room_name'] for r in groups_rows]
                                    
                                private_rooms = []
                                private_rows = await pool.fetch("SELECT DISTINCT room FROM messages WHERE room LIKE 'PRIVATE|%'")
                                for r in private_rows:
                                    parts = r['room'].split('|')
                                    if len(parts) == 3 and username in (parts[1], parts[2]):
                                        private_rooms.append(r['room'])
                                            
                                await ws.send_json({"type": "init_data", "groups": groups, "private_rooms": private_rooms})
                            else:
                                await ws.send_json({"type": "auth_result", "success": False, "error": "Неверный пароль!"})
                        else:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Пользователь не найден!"})

                elif action == 'get_users':
                    users_rows = await pool.fetch("SELECT username FROM users WHERE username != $1", active_connections[ws]["username"])
                    users = [r['username'] for r in users_rows]
                    await ws.send_json({"type": "user_list", "users": users})

                # --- ПОИСК ЧЕРЕЗ WEBSOCKET ---
                elif action == 'search':
                    query = data.get('query', '').strip()
                    if query:
                        search_pattern = f"{query}%"
                        try:
                            users_rows = await pool.fetch("SELECT DISTINCT username AS name, 'user' AS type FROM users WHERE username ILIKE $1 LIMIT 10", search_pattern)
                            rooms_rows = await pool.fetch("SELECT DISTINCT name AS name, 'group' AS type FROM rooms WHERE name ILIKE $1 LIMIT 10", search_pattern)
                            
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
                    group_name = data.get('name', '').strip()
                    if group_name:
                        try:
                            await pool.execute("INSERT INTO rooms (name, type) VALUES ($1, 'group')", group_name)
                            await pool.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", group_name, active_connections[ws]["username"])
                            await ws.send_json({"type": "new_group", "name": group_name})
                        except asyncpg.UniqueViolationError:
                            await ws.send_json({"type": "error_msg", "text": "Группа с таким именем уже существует!"})

                elif action == 'add_to_group':
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
                    old_username = active_connections[ws]["username"]
                    new_username = data.get('new_nickname', '').strip()
                    
                    if not old_username: continue
                    if not new_username or new_username == old_username: continue
                    
                    try:
                        async with pool.acquire() as transaction_conn:
                            async with transaction_conn.transaction():
                                await transaction_conn.execute("UPDATE users SET username = $1 WHERE username = $2", new_username, old_username)
                                await transaction_conn.execute("UPDATE messages SET username = $1 WHERE username = $2", new_username, old_username)
                                await transaction_conn.execute("UPDATE room_members SET username = $1 WHERE username = $2", new_username, old_username)
                        
                        active_connections[ws]["username"] = new_username
                        await ws.send_json({"type": "nickname_changed", "new_name": new_username})
                    except asyncpg.UniqueViolationError:
                        await ws.send_json({"type": "error_msg", "text": "Этот ник уже занят!"})

                elif action == 'send_msg':
                    user_info = active_connections.get(ws)
                    if not user_info or not user_info["username"]: continue
                    
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
                    
                    msg_id = await pool.fetchval(
                        "INSERT INTO messages (room, username, content, msg_type, timestamp) VALUES ($1, $2, $3, $4, $5) RETURNING id", 
                        room, user_info["username"], content, msg_type, time_str
                    )
                    
                    # ОПТИМИЗАЦИЯ: Получаем список разрешенных пользователей ОДИН раз перед циклом рассылки
                    allowed_users = set()
                    is_private = False
                    
                    if room.startswith("PRIVATE|"):
                        is_private = True
                        parts = room.split('|')
                        if len(parts) == 3:
                            allowed_users = {parts[1], parts[2]}
                    else:
                        # Получаем всех участников группы
                        members_rows = await pool.fetch("SELECT username FROM room_members WHERE room_name = $1", room)
                        allowed_users = {r['username'] for r in members_rows}
                    
                    # Рассылка сообщений по защищенной копии списка подключений
                    for client, info in list(active_connections.items()):
                        if info["username"]:
                            if is_private:
                                if info["username"] not in allowed_users: continue
                            else:
                                # Если это групповой чат, сообщение доставляется тем, кто в списке участников,
                                # ИЛИ тем, у кого прямо сейчас открыта эта комната (для работы публичных комнат)
                                if info["username"] not in allowed_users and info.get("room") != room:
                                    continue
                            
                            try:
                                await client.send_json({
                                    "type": "msg", "id": msg_id, "room": room, 
                                    "username": user_info["username"], "content": content, "msg_type": msg_type, "time": time_str
                                })
                            except Exception:
                                pass # Игнорируем ошибки отправки на мертвые сокеты

                elif action == 'join_room':
                    room = data.get('room')
                    active_connections[ws]["room"] = room
                    await ws.send_json({"type": "clear_chat"})
                    
                    history_rows = await pool.fetch("SELECT id, username, content, msg_type, timestamp FROM messages WHERE room = $1 ORDER BY id ASC LIMIT 100", room)
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

                # --- СИГНАЛИНГ ДЛЯ ЗВОНКОВ (WebRTC) ---
                elif action == 'call_user':
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
    web.run_app(app, port=int(os.environ.get('PORT', 8080)))