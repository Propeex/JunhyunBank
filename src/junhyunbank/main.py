from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .config import AppConfig
from .engine import TradingEngine
from .security import KeyStore
from .ui import ApiKeyDialog, MainWindow
from .upbit import UpbitClient


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("JunhyunBank")

    key_store = KeyStore()
    access, secret = key_store.load()

    if not (access and secret):
        first_run = ApiKeyDialog(key_store)
        if first_run.exec():
            access, secret = key_store.load()
        else:
            QMessageBox.information(
                None,
                "PAPER 모드",
                "API 키를 저장하지 않았습니다. PAPER 모드는 사용할 수 있으며 LIVE 모드는 사용할 수 없습니다.",
            )

    client = UpbitClient(access, secret)
    engine = TradingEngine(client, AppConfig())
    window = MainWindow(engine, key_store)
    window.show()
    result = app.exec()
    engine.request_stop()
    client.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
