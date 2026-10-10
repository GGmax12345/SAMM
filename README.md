# SAM Messenger

Веб-мессенджер на Python (aiohttp) с WebSocket-чатами, авторизацией по email и десктопным клиентом.

> ⚠️ **Статус проекта:** заморожен / забыт / потерян / удалён по причине бесполезности и отсутствия нормальных бесплатных хостингов.
> The project has been frozen, forgotten, lost, or deleted due to uselessness, and there are no decent free hosting services available.

## Возможности

- Регистрация и вход по email (одноразовые коды подтверждения через mail.ru SMTP)
- Хэширование паролей (argon2), шифрование сообщений (AES-GCM)
- Личные и групповые чаты в реальном времени через WebSocket
- Загрузка файлов и изображений (Pillow)
- Поиск пользователей, профиль, раздел приложений
- Роли пользователей, включая администратора
- Десктопный клиент на PyQt5 (QtWebEngine), собираемый PyInstaller

## Структура репозитория

| Файл | Описание |
|---|---|
| `server.py` | Сервер aiohttp: HTTP-роуты, WebSocket, БД, криптография |
| `app_launcher.py` | Десктопный клиент (PyQt5 + QWebEngineView) |
| `login.html`, `chat.html`, `profile.html`, `apps.html` | Клиентские страницы |
| `requirements.txt` | Зависимости Python |
| `SAM.spec`, `app_launcher.spec`, `server.spec` | Конфиги сборки PyInstaller |
| `icon.ico` | Иконка приложения |

## Требования

- Python 3.9+
- PostgreSQL (через `asyncpg`)

## Установка

```bash
git clone <repo-url>
cd SAM
pip install -r requirements.txt
```

## Запуск сервера

Переменные окружения:

| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | Строка подключения к PostgreSQL |
| `EMAIL_USER` / `EMAIL_PASSWORD` | Почта mail.ru для отправки кодов подтверждения |
| `ADMIN_PASSWORD` | Пароль администратора |
| `APP_SECRET` | Секрет для шифрования и хэшей (обязательно задать постоянно!) |
| `PORT` | Порт сервера (по умолчанию `8080`) |

```bash
python server.py
```

Сервер доступен на `http://0.0.0.0:8080` (порт задаётся переменной `PORT`).

## Десктопный клиент

```bash
pip install pyqt5
python app_launcher.py
```

По умолчанию клиент открывает URL развёрнутого сервера — измените адрес в `app_launcher.py` при необходимости.

## Сборка exe (PyInstaller)

```bash
pip install pyinstaller
pyinstaller SAM.spec
```

## Лицензия

Не указана.
