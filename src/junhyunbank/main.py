from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import AppConfig
from .instance_guard import InstanceGuard
from .live_engine import TradingEngine
from .security import KeyStore
from .ui import ApiKeyDialog, MainWindow
from .upbit import UpbitClient
from .updater import verify_post_update_install


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--resume-trading", action="store_true")
    parser.add_argument("--post-update-verify")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return args


def main() -> int:
    args = _args()

    # The detached updater launches the newly installed binary once in this
    # non-trading verification mode before normal startup. No API request or
    # order can happen here. A token-bound marker is written only after current
    # Storage migrations and SQLite integrity checks succeed.
    if args.post_update_verify:
        try:
            verify_post_update_install(str(args.post_update_verify))
            return 0
        except Exception:
            return 3

    app = QApplication(sys.argv)
    app.setApplicationName("JunhyunBank")

    base = Path.home() / ".junhyunbank"
    guard = InstanceGuard(base / "junhyunbank.lock")
    if not guard.acquire():
        QMessageBox.warning(
            None,
            "이미 실행 중",
            "JunhyunBank가 이미 실행 중입니다. 실거래 중복 주문을 방지하기 위해 두 번째 실행을 차단했습니다.",
        )
        return 2

    client: UpbitClient | None = None
    engine: TradingEngine | None = None
    try:
        key_store = KeyStore()
        access, secret = key_store.load()
        if not (access and secret):
            first_run = ApiKeyDialog(key_store)
            if first_run.exec():
                access, secret = key_store.load()
            else:
                QMessageBox.information(None, "API 키 필요", "JunhyunBank는 LIVE 전용입니다. API 키를 저장해야 프로그램을 사용할 수 있습니다.")
                return 0
        client = UpbitClient(access, secret)
        try:
            client.test_credentials()
        except Exception as exc:
            QMessageBox.critical(None, "업비트 연결 실패", f"저장된 API 키로 업비트 계정을 확인할 수 없습니다.\n\n{exc}")
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
        return result
    finally:
        if engine is not None and engine.running:
            engine.shutdown_for_update()
            engine.wait(5.0)
        if client is not None:
            client.close()
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())