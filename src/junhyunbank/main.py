from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import AppConfig
from .runtime_engine import TradingEngine
from .security import KeyStore
from .ui import ApiKeyDialog, MainWindow
from .upbit import UpbitClient


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--resume-trading", action="store_true")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return args


def main() -> int:
    args = _args()
    app = QApplication(sys.argv)
    app.setApplicationName("JunhyunBank")
    key_store = KeyStore()
    access, secret = key_store.load()
    if not (access and secret):
        first_run = ApiKeyDialog(key_store)
        if first_run.exec():
            access, secret = key_store.load()
        else:
            QMessageBox.information(None, "API 키 필요", "JunhyunBank V3는 LIVE 전용입니다. API 키를 저장해야 프로그램을 사용할 수 있습니다.")
            return 0
    client = UpbitClient(access, secret)
    try:
        client.test_credentials()
    except Exception as exc:
        QMessageBox.critical(None, "업비트 연결 실패", f"저장된 API 키로 업비트 계정을 확인할 수 없습니다.\n\n{exc}")
        client.close()
        return 1
    engine = TradingEngine(client, AppConfig())
    window = MainWindow(engine, key_store)
    window.show()
    if args.resume_trading:
        QTimer.singleShot(750, window.start_trading_silent)
    result = app.exec()
    if engine.running:
        engine.shutdown_for_update()
    engine.wait(5.0)
    client.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
