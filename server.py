import asyncio
import os
import json
import datetime
import uuid
import aiohttp
from aiohttp import web
import aiosqlite

routes = web.RouteTableDef()
DB_PATH = 'sam_database.db'
UPLOAD_DIR = 'uploads'

# Создаем папку для загрузок, если её нет
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

active_connections = {}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                is_banned INTEGER DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                username TEXT NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT DEFAULT 'text',
                timestamp TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY,
                type TEXT DEFAULT 'group'
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS room_members (
                room_name TEXT,
                username TEXT,
                UNIQUE(room_name, username)
            )
        ''')
        async with db.execute("SELECT * FROM users WHERE username = 'Grom'") as cursor:
            if not await cursor.fetchone():
                await db.execute(
                    "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                    ('Grom', '12344321', 'admin')
                )
        await db.commit()

def load_html(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

@routes.get('/')
async def index(request):
    return web.Response(text=load_html('login.html'), content_type='text/html')

@routes.get('/chat')
async def chat_page(request):
    return web.Response(text=load_html('chat.html'), content_type='text/html')

@routes.get('/profile')
async def profile_page(request):
    return web.Response(text=load_html('profile.html'), content_type='text/html')

# МАРШРУТ ДЛЯ ЗАГРУЗКИ ФАЙЛОВ (HTTP POST)
@routes.post('/upload')
async def upload_file(request):
    reader = await request.multipart()
    field = await reader.next()
    
    if field.name == 'file':
        filename = field.filename
        # Делаем имя уникальным, чтобы файлы не перезаписывали друг друга
        ext = os.path.splitext(filename)[1]
        unique_filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(UPLOAD_DIR, unique_filename)
        
        size = 0
        with open(filepath, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
                
        # Возвращаем клиенту имя файла и его тип
        is_image = field.headers.get('Content-Type', '').startswith('image/')
        msg_type = 'image' if is_image else 'file'
        
        return web.json_response({
            "success": True, 
            "url": f"/uploads/{unique_filename}", 
            "name": filename,
            "msg_type": msg_type
        })
        
    return web.json_response({"success": False, "error": "Файл не найден"})

@routes.get('/ws')
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    active_connections[ws] = {"username": None, "role": 'user', "room": None}
    
    try:
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
                        
                    async with aiosqlite.connect(DB_PATH) as db:
                        if action == 'register':
                            try:
                                await db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
                                await db.commit()
                                await ws.send_json({"type": "auth_result", "success": True, "username": username, "role": "user"})
                                active_connections[ws]["username"] = username
                            except aiosqlite.IntegrityError:
                                await ws.send_json({"type": "auth_result", "success": False, "error": "Имя занято!"})
                        
                        elif action == 'login':
                            async with db.execute("SELECT role, is_banned, password FROM users WHERE username = ?", (username,)) as cursor:
                                row = await cursor.fetchone()
                                if row:
                                    db_role, is_banned, db_password = row
                                    if is_banned:
                                        await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены."})
                                    elif db_password == password:
                                        active_connections[ws]["username"] = username
                                        active_connections[ws]["role"] = db_role
                                        await ws.send_json({"type": "auth_result", "success": True, "username": username, "role": db_role})
                                        
                                        async with db.execute("SELECT room_name FROM room_members WHERE username = ?", (username,)) as c:
                                            groups = [r[0] for r in await c.fetchall()]
                                            
                                        private_rooms = []
                                        async with db.execute("SELECT DISTINCT room FROM messages WHERE room LIKE 'PRIVATE|%'") as c:
                                            for r in await c.fetchall():
                                                parts = r[0].split('|')
                                                if len(parts) == 3 and username in (parts[1], parts[2]):
                                                    private_rooms.append(r[0])
                                                    
                                        await ws.send_json({"type": "init_data", "groups": groups, "private_rooms": private_rooms})
                                    else:
                                        await ws.send_json({"type": "auth_result", "success": False, "error": "Неверный пароль!"})
                                else:
                                    await ws.send_json({"type": "auth_result", "success": False, "error": "Пользователь не найден!"})

                elif action == 'get_users':
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT username FROM users WHERE username != ?", (active_connections[ws]["username"],)) as c:
                            users = [r[0] for r in await c.fetchall()]
                    await ws.send_json({"type": "user_list", "users": users})

                elif action == 'create_group':
                    group_name = data.get('name', '').strip()
                    if group_name:
                        async with aiosqlite.connect(DB_PATH) as db:
                            try:
                                await db.execute("INSERT INTO rooms (name, type) VALUES (?, 'group')", (group_name,))
                                await db.execute("INSERT INTO room_members (room_name, username) VALUES (?, ?)", (group_name, active_connections[ws]["username"]))
                                await db.commit()
                                await ws.send_json({"type": "new_group", "name": group_name})
                            except aiosqlite.IntegrityError:
                                await ws.send_json({"type": "error_msg", "text": "Группа с таким именем уже существует!"})

                elif action == 'add_to_group':
                    target_user = data.get('username')
                    room = data.get('room')
                    if target_user and room:
                        async with aiosqlite.connect(DB_PATH) as db:
                            try:
                                await db.execute("INSERT INTO room_members (room_name, username) VALUES (?, ?)", (room, target_user))
                                await db.commit()
                                for client, info in active_connections.items():
                                    if info["username"] == target_user:
                                        await client.send_json({"type": "new_group", "name": room})
                                await ws.send_json({"type": "success_msg", "text": f"Пользователь {target_user} добавлен в группу!"})
                            except aiosqlite.IntegrityError:
                                await ws.send_json({"type": "error_msg", "text": "Этот пользователь уже в группе!"})

                elif action == 'change_nickname':
                    old_username = active_connections[ws]["username"]
                    new_username = data.get('new_nickname', '').strip()
                    
                    if not old_username: continue
                    if not new_username or new_username == old_username: continue
                    
                    async with aiosqlite.connect(DB_PATH) as db:
                        try:
                            await db.execute("UPDATE users SET username = ? WHERE username = ?", (new_username, old_username))
                            await db.execute("UPDATE messages SET username = ? WHERE username = ?", (new_username, old_username))
                            await db.execute("UPDATE room_members SET username = ? WHERE username = ?", (new_username, old_username))
                            await db.commit()
                            
                            active_connections[ws]["username"] = new_username
                            await ws.send_json({"type": "nickname_changed", "new_name": new_username})
                        except aiosqlite.IntegrityError:
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
                    
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT is_banned FROM users WHERE username = ?", (user_info["username"],)) as c:
                            res = await c.fetchone()
                            if res and res[0] == 1:
                                await ws.send_json({"type": "auth_result", "success": False, "error": "Вы забанены!"})
                                continue
                        
                        cursor = await db.execute("INSERT INTO messages (room, username, content, msg_type, timestamp) VALUES (?, ?, ?, ?, ?)", 
                                                 (room, user_info["username"], content, msg_type, time_str))
                        msg_id = cursor.lastrowid
                        await db.commit()
                    
                    for client, info in active_connections.items():
                        if info["username"]:
                            if room.startswith("PRIVATE|"):
                                parts = room.split('|')
                                if info["username"] not in (parts[1], parts[2]): continue
                            else:
                                async with aiosqlite.connect(DB_PATH) as db:
                                    async with db.execute("SELECT 1 FROM room_members WHERE room_name = ? AND username = ?", (room, info["username"])) as c:
                                        if not await c.fetchone(): continue
                            
                            await client.send_json({
                                "type": "msg", "id": msg_id, "room": room, 
                                "username": user_info["username"], "content": content, "msg_type": msg_type, "time": time_str
                            })

                elif action == 'join_room':
                    room = data.get('room')
                    active_connections[ws]["room"] = room
                    await ws.send_json({"type": "clear_chat"})
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT id, username, content, msg_type, timestamp FROM messages WHERE room = ? ORDER BY id ASC LIMIT 100", (room,)) as msg_cursor:
                            history = await msg_cursor.fetchall()
                            for msg_id, u, c, t, time_str in history:
                                await ws.send_json({"type": "msg", "id": msg_id, "room": room, "username": u, "content": c, "msg_type": t, "time": time_str, "history": True})

                elif action == 'delete_msg':
                    if active_connections[ws]["role"] == 'admin':
                        msg_id = data.get('msg_id')
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE messages SET content = 'Сообщение удалено администратором', msg_type = 'system' WHERE id = ?", (msg_id,))
                            await db.commit()
                        for client in active_connections:
                            await client.send_json({"type": "delete_evt", "id": msg_id})
                            
                elif action == 'ban_user':
                    if active_connections[ws]["role"] == 'admin':
                        target_user = data.get('username')
                        if target_user == 'Grom': continue
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE users SET is_banned = 1 WHERE username = ?", (target_user,))
                            await db.commit()
                        for client, info in list(active_connections.items()):
                            if info["username"] == target_user:
                                await client.send_json({"type": "banned"})
                                await client.close()
    finally:
        active_connections.pop(ws, None)
    return ws

app = web.Application()
app.add_routes(routes)

# Раздача статических загруженных файлов из папки uploads
app.router.add_static('/uploads/', path=UPLOAD_DIR, name='uploads')

app.on_startup.append(lambda a: init_db())

if __name__ == '__main__':
    web.run_app(app, port=8080)