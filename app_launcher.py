import sys
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from PyQt5.QtWebEngineWidgets import *

class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.browser = QWebEngineView()
        
        # Указываешь адрес запущенного сервера (локальный или на Render)
        self.browser.setUrl(QUrl("https://sam-dc1v.onrender.com"))
        self.setCentralWidget(self.browser)
        
        self.setWindowTitle('SAM Messenger Desktop')
        self.resize(1024, 768)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())