#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SAM Messenger — десктопное приложение на Python.

Дизайн НЕ переписывается: это тот же HTML (login.html / chat.html),
который рендерится в нативном окне PyQt5 (QWebEngineView = Chrome внутри).
Бэкенд — ваш aiohttp-сервер (server.py): локально или удалённый (Render).

Запуск:
    pip install -r requirements-desktop.txt
    python desktop.py                       # локальный сервер + окно
    python desktop.py --url https://sam-dc1v.onrender.com   # только окно, без сервера
    python desktop.py --no-server --url http://localhost:8080  # внешний/свой сервер

Отличие от app_launcher.py: здесь есть автозапуск локального бэкенда,
выбор адреса через аргументы и корректное завершение работы.
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
    parser.add_argument("--host", default="127.0.0.1", help="Хост локального сервера")
    parser.add_argument("--port", type=int, default=8080, help="Порт локального сервера")
    parser.add_argument("--no-server", action="store_true",
                        help="Не запускать бэкенд, только окно (для --url)")
    args = parser.parse_args()

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
