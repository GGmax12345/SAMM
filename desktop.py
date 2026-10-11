#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SAM Messenger — десктопное приложение на Python.

Дизайн НЕ переписывается: это тот же HTML (login.html / chat.html),
который рендерится в нативном окне PyQt5 (QWebEngineView = Chrome внутри).

Режимы:
    python desktop.py                       # локальный сервер + окно (всё на ПК)
    python desktop.py --remote https://sam-dc1v.onrender.com
                                            # дизайн берётся с удалённого сервера,
                                            # данные — из его PostgreSQL; локальный
                                            # бэкенд НЕ запускается
    python desktop.py --url http://localhost:8080   # просто открыть адрес
    python desktop.py --no-server --url <адрес>     # внешний сервер, без своего бэкенда

Окно ведёт себя как «локальный браузер»: интерфейс открывается в своём окне,
а WebSocket/fetch ходят на указанный сервер. E2EE-ключи и сессия сохраняются
в localStorage между запусками (профиль WebEngine на диске).
"""

import argparse
import atexit
import os
import socket
import subprocess
import sys
import time

from PyQt5.QtCore import QUrl, QTimer
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtWebEngineWidgets import QWebEngineView

HERE = os.path.dirname(os.path.abspath(__file__))


def is_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """Проверяет, слушает ли кто-то указанный порт (сервер уже запущен?)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_local_server(host: str, port: int):
    """Поднимает бэкенд (server.py) отдельным процессом `python server.py`.

    Отдельный процесс, а не поток: у PyQt5 своё event loop в основном потоке,
    а asyncio-сервер требует свой. Управление портами через переменную окружения PORT.
    """
    env = os.environ.copy()
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py")],
        cwd=HERE, env=env,
    )
    return proc


class MainWindow(QMainWindow):
    def __init__(self, url: str):
        super().__init__()
        self.browser = QWebEngineView(self)
        self.browser.setUrl(QUrl(url))
        self.setCentralWidget(self.browser)
        self.setWindowTitle("SAM Messenger Desktop")
        self.resize(600, 800)
        # E2EE-ключи и сессия живут в localStorage — включаем хранилище
        profile = self.browser.page().profile()
        profile.setPersistentStoragePath(os.path.join(HERE, ".webengine-profile"))
        profile.setHttpCacheType(profile.HttpCacheDisk)

    def wait_and_load(self, url: str):
        self.browser.setUrl(QUrl(url))

    def closeEvent(self, event):
        # При закрытии окна гасим и локальный бэкенд
        proc = getattr(self, "server_proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="SAM Messenger desktop client")
    parser.add_argument("--url", default=None,
                        help="Адрес фронтенда (по умолчанию локальный сервер)")
    parser.add_argument("--remote", default=None,
                        help="URL удалённого сервера (например https://sam-dc1v.onrender.com): "
                             "локальный бэкенд не запускается, дизайн и данные берутся с сервера")
    parser.add_argument("--host", default="127.0.0.1", help="Хост локального сервера")
    parser.add_argument("--port", type=int, default=8080, help="Порт локального сервера")
    parser.add_argument("--no-server", action="store_true",
                        help="Не запускать бэкенд, только окно (для --url)")
    args = parser.parse_args()

    if args.remote:
        # Режим «приложение + серверный бэкенд/PostgreSQL»: страница открывается
        # с удалённого сервера, WebSocket/fetch идут туда же (window.location.host).
        args.url = args.remote.rstrip("/") + "/"
        args.no_server = True

    remote_mode = args.no_server or args.url is not None
    url = args.url or f"http://{args.host}:{args.port}"

    server_proc = None
    if not remote_mode and not is_port_open(args.host, args.port):
        # Бэкенд в отдельном процессе; убьём его при закрытии приложения
        server_proc = start_local_server(args.host, args.port)
        atexit.register(lambda: server_proc.terminate())

    app = QApplication(sys.argv)
    window = MainWindow("about:blank")
    window.server_proc = server_proc
    window.show()

    deadline = time.time() + 20

    def check():
        # Ждём, пока локальный сервер начнёт принимать соединения, затем грузим страницу
        if is_port_open(args.host, args.port) or time.time() > deadline:
            timer.stop()
            window.wait_and_load(url)

    if not remote_mode:
        timer = QTimer(window)
        timer.setInterval(300)
        timer.timeout.connect(check)
        timer.start()
    else:
        window.wait_and_load(url)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
