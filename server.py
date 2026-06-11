import asyncio
import os
import json
import datetime
import uuid
import hashlib
import aiohttp
from aiohttp import web
import asyncpg

routes = web.RouteTableDef()
# На Render ссылка на базу данных автоматически передаётся в переменную DATABASE_URL
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://sam_rf61_user:yn0Vv1yOgyQYhHCCyfJejFjVlIQFyeSH@dpg-d8li4purnols73evc1mg-a/sam_rf61')
UPLOAD_DIR = 'uploads'

# Создаем папку для загрузок, если её нет (учти, на бесплатном Render файлы тут временные)
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

active_connections = {}

# Функция для создания SHA-256 хеша пароля
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

# Инициализация базы данных PostgreSQL
# Инициализация базы данных PostgreSQL
async def init_db(app):
    global DATABASE_URL
    
    # Защита: если Render почему-то не передал переменную, а мы на сервере,
    # мы принудительно заставим его выдать ошибку конфигурации, а не стучаться в localhost
    if 'RENDER' in os.environ and ( "localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL ):
        print("КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL не задана в настройках Render Environment!")
    
    # Если мы подключаемся к внешней базе данных, принудительно запрашиваем SSL-соединение
    if "localhost" not in DATABASE_URL and "127.0.0.1" not in DATABASE_URL:
        pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")
    else:
        pool = await asyncpg.create_pool(DATABASE_URL)
        
    app['db_pool'] = pool
    
    async with pool.acquire() as conn:
        # Таблица пользователей
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                is_banned INTEGER DEFAULT 0
            )
        ''')
        # Таблица сообщений
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
        # Таблица комнат
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY,
                type TEXT DEFAULT 'group'
            )
        ''')
        # Таблица участников комнат
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS room_members (
                room_name TEXT,
                username TEXT,
                UNIQUE(room_name, username)
            )
        ''')
        
        # Создаем админа Grom с захешированным паролем, если его нет
        row = await conn.fetchrow("SELECT * FROM users WHERE username = 'Grom'")
        if not row:
            hashed_admin_pass = hash_password('12344321')
            await conn.execute(
                "INSERT INTO users (username, password, role) VALUES ($1, $2, $3)",
                'Grom', hashed_admin_pass, 'admin'
            )

# Функция для закрытия пула соединений при остановке сервера
async def close_db(app):
    await app['db_pool'].close()

# Функция для чтения HTML-файлов из папки проекта
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

# --- ЗАГРУЗКА МЕДИАФАЙЛОВ И ПЛЕЕРОВ ---

@routes.post('/upload')
async def upload_file(request):
    reader = await request.multipart()
    field = await reader.next()
    
    if field.name == 'file':
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
        async_pg_conn = await pool.acquire()
        async with ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    action = data.get('action')
                    
                    if action in ['login', 'register']:
                        username = data.get('username', '').strip()
                        password = data.get('password', '').strip()
                        if not username or not password:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Заполните поля!"})
                            continue
                        
                        hashed_pass = hash_password(password)
                            
                        if action == 'register':
                            try:
                                await async_pg_conn.execute("INSERT INTO users (username, password) VALUES ($1, $2)", username, hashed_pass)
                                await ws.send_json({"type": "auth_result", "success": True, "username": username, "role": "user"})
                                active_connections[ws]["username"] = username
                            except asyncpg.UniqueViolationError:
                                await ws.send_json({"type": "auth_result", "success": False, "error": "Имя занято!"})
                        
                        elif action == 'login':
                            row = await async_pg_conn.fetchrow("SELECT role, is_banned, password FROM users WHERE username = $1", username)
                            if row:
                                db_role, is_banned, db_password = row['role'], row['is_banned'], row['password']
                                if is_banned:
                                    await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                                elif db_password == hashed_pass:
                                    active_connections[ws]["username"] = username
                                    active_connections[ws]["role"] = db_role
                                    await ws.send_json({"type": "auth_result", "success": True, "username": username, "role": db_role})
                                    
                                    groups_rows = await async_pg_conn.fetch("SELECT room_name FROM room_members WHERE username = $1", username)
                                    groups = [r['room_name'] for r in groups_rows]
                                        
                                    private_rooms = []
                                    private_rows = await async_pg_conn.fetch("SELECT DISTINCT room FROM messages WHERE room LIKE 'PRIVATE|%'")
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
                        users_rows = await async_pg_conn.fetch("SELECT username FROM users WHERE username != $1", active_connections[ws]["username"])
                        users = [r['username'] for r in users_rows]
                        await ws.send_json({"type": "user_list", "users": users})

                    elif action == 'create_group':
                        group_name = data.get('name', '').strip()
                        if group_name:
                            try:
                                await async_pg_conn.execute("INSERT INTO rooms (name, type) VALUES ($1, 'group')", group_name)
                                await async_pg_conn.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", group_name, active_connections[ws]["username"])
                                await ws.send_json({"type": "new_group", "name": group_name})
                            except asyncpg.UniqueViolationError:
                                await ws.send_json({"type": "error_msg", "text": "Группа с таким именем уже существует!"})

                    elif action == 'add_to_group':
                        target_user = data.get('username')
                        room = data.get('room')
                        if target_user and room:
                            try:
                                await async_pg_conn.execute("INSERT INTO room_members (room_name, username) VALUES ($1, $2)", room, target_user)
                                for client, info in active_connections.items():
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
                            # В PostgreSQL обновляем всё в одной транзакции
                            async with async_pg_conn.transaction():
                                await async_pg_conn.execute("UPDATE users SET username = $1 WHERE username = $2", new_username, old_username)
                                await async_pg_conn.execute("UPDATE messages SET username = $1 WHERE username = $2", new_username, old_username)
                                await async_pg_conn.execute("UPDATE room_members SET username = $1 WHERE username = $2", new_username, old_username)
                            
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
                        
                        res = await async_pg_conn.fetchrow("SELECT is_banned FROM users WHERE username = $1", user_info["username"])
                        if res and res['is_banned'] == 1:
                            await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены!"})
                            continue
                        
                        msg_id = await async_pg_conn.fetchval(
                            "INSERT INTO messages (room, username, content, msg_type, timestamp) VALUES ($1, $2, $3, $4, $5) RETURNING id", 
                            room, user_info["username"], content, msg_type, time_str
                        )
                        
                        for client, info in active_connections.items():
                            if info["username"]:
                                if room.startswith("PRIVATE|"):
                                    parts = room.split('|')
                                    if info["username"] not in (parts[1], parts[2]): continue
                                else:
                                    member_check = await async_pg_conn.fetchrow("SELECT 1 FROM room_members WHERE room_name = $1 AND username = $2", room, info["username"])
                                    if not member_check: continue
                                
                                await client.send_json({
                                    "type": "msg", "id": msg_id, "room": room, 
                                    "username": user_info["username"], "content": content, "msg_type": msg_type, "time": time_str
                                })

                    elif action == 'join_room':
                        room = data.get('room')
                        active_connections[ws]["room"] = room
                        await ws.send_json({"type": "clear_chat"})
                        
                        history_rows = await async_pg_conn.fetch("SELECT id, username, content, msg_type, timestamp FROM messages WHERE room = $1 ORDER BY id ASC LIMIT 100", room)
                        for r in history_rows:
                            await ws.send_json({"type": "msg", "id": r['id'], "room": room, "username": r['username'], "content": r['content'], "msg_type": r['msg_type'], "time": r['timestamp'], "history": True})

                    elif action == 'delete_msg':
                        if active_connections[ws]["role"] == 'admin':
                            msg_id = data.get('msg_id')
                            await async_pg_conn.execute("UPDATE messages SET content = 'Сообщение удалено администратором', msg_type = 'system' WHERE id = $1", msg_id)
                            for client in active_connections:
                                await client.send_json({"type": "delete_evt", "id": msg_id})
                                
                    elif action == 'ban_user':
                        if active_connections[ws]["role"] == 'admin':
                            target_user = data.get('username')
                            if target_user == 'Grom': continue
                            await async_pg_conn.execute("UPDATE users SET is_banned = 1 WHERE username = $1", target_user)
                            for client, info in list(active_connections.items()):
                                if info["username"] == target_user:
                                    await client.send_json({"type": "banned"})
                                    await client.close()
    finally:
        active_connections.pop(ws, None)
        await pool.release(async_pg_conn)
    return ws

app = web.Application()
app.add_routes(routes)
app.router.add_static('/uploads/', path=UPLOAD_DIR, name='uploads')

# Жизненный цикл пула соединений в aiohttp
app.on_startup.append(init_db)
app.on_cleanup.append(close_db)

if __name__ == '__main__':
    web.run_app(app, port=int(os.environ.get('PORT', 8080)))