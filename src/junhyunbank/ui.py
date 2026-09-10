from __future__ import annotations

from collections import defaultdict, deque

import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import TradingMode
from .engine import TradingEngine
from .security import KeyStore
from .upbit import UpbitClient


class ApiKeyDialog(QDialog):
    def __init__(self, key_store: KeyStore, parent=None) -> None:
        super().__init__(parent)
        self.key_store = key_store
        self.setWindowTitle("업비트 API 키 설정")
        self.access = QLineEdit()
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel("출금 권한은 부여하지 마세요. 거래/잔고조회 권한만 사용합니다.")

        form = QFormLayout()
        form.addRow("Access Key", self.access)
        form.addRow("Secret Key", self.secret)
        form.addRow(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setLayout(form)

    def _save(self) -> None:
        access = self.access.text().strip()
        secret = self.secret.text().strip()
        try:
            tester = UpbitClient(access, secret)
            try:
                tester.test_credentials()
            finally:
                tester.close()
            self.key_store.save(access, secret)
        except Exception as exc:
            QMessageBox.critical(self, "API 연결 실패", str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, engine: TradingEngine, key_store: KeyStore) -> None:
        super().__init__()
        self.engine = engine
        self.key_store = key_store
        self._series: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=180))
        self._chart_market: str | None = None
        self.setWindowTitle("JunhyunBank V1")
        self.resize(1100, 760)

        root = QWidget()
        layout = QVBoxLayout(root)

        controls = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems([TradingMode.PAPER.value, TradingMode.LIVE.value])
        self.api_button = QPushButton("API 키 설정")
        self.start_button = QPushButton("시작")
        self.stop_button = QPushButton("종료")
        self.emergency_button = QPushButton("긴급 정지")
        controls.addWidget(QLabel("모드"))
        controls.addWidget(self.mode)
        controls.addWidget(self.api_button)
        controls.addStretch()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.emergency_button)
        layout.addLayout(controls)

        summary = QHBoxLayout()
        self.status = QLabel("대기")
        self.equity = QLabel("총 평가: -")
        self.cash = QLabel("KRW: -")
        self.pnl = QLabel("세션 수익률: -")
        summary.addWidget(self.status)
        summary.addStretch()
        summary.addWidget(self.equity)
        summary.addWidget(self.cash)
        summary.addWidget(self.pnl)
        layout.addLayout(summary)

        body = QHBoxLayout()
        left = QVBoxLayout()

        asset_box = QGroupBox("보유 자산")
        asset_layout = QVBoxLayout(asset_box)
        self.assets = QTableWidget(0, 5)
        self.assets.setHorizontalHeaderLabels(["마켓", "수량", "평균단가", "현재가", "평가금액"])
        self.assets.horizontalHeader().setStretchLastSection(True)
        asset_layout.addWidget(self.assets)
        left.addWidget(asset_box)

        candidate_box = QGroupBox("자동 선정 후보")
        candidate_layout = QVBoxLayout(candidate_box)
        self.candidates = QLabel("-")
        self.candidates.setWordWrap(True)
        candidate_layout.addWidget(self.candidates)
        left.addWidget(candidate_box)

        body.addLayout(left, 1)

        right = QVBoxLayout()
        chart_box = QGroupBox("실시간 가격")
        chart_layout = QVBoxLayout(chart_box)
        self.chart_title = QLabel("후보 종목의 실시간 가격을 표시합니다.")
        self.chart = pg.PlotWidget()
        self.curve = self.chart.plot([])
        chart_layout.addWidget(self.chart_title)
        chart_layout.addWidget(self.chart)
        right.addWidget(chart_box, 2)

        log_box = QGroupBox("운영 로그")
        log_layout = QVBoxLayout(log_box)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        right.addWidget(log_box, 1)

        body.addLayout(right, 2)
        layout.addLayout(body, 1)
        self.setCentralWidget(root)

        self.api_button.clicked.connect(self.configure_api)
        self.start_button.clicked.connect(self.start_trading)
        self.stop_button.clicked.connect(self.stop_trading)
        self.emergency_button.clicked.connect(self.emergency_stop)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_events)
        self.timer.start(250)

    def configure_api(self) -> None:
        if ApiKeyDialog(self.key_store, self).exec():
            access, secret = self.key_store.load()
            self.engine.client.access_key = access
            self.engine.client.secret_key = secret
            self.log.append("[security] 새 API 키를 현재 세션에 적용했습니다.")

    def start_trading(self) -> None:
        mode = TradingMode(self.mode.currentText())
        if mode == TradingMode.LIVE:
            if not self.key_store.exists():
                QMessageBox.warning(self, "API 키 필요", "LIVE 모드에는 업비트 API 키가 필요합니다.")
                return
            result = QMessageBox.warning(
                self,
                "실거래 확인",
                "LIVE 모드는 실제 원화로 주문합니다. 계속하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return
        try:
            self.engine.start(mode)
        except Exception as exc:
            QMessageBox.critical(self, "시작 실패", str(exc))

    def stop_trading(self) -> None:
        self.engine.request_stop()

    def emergency_stop(self) -> None:
        self.engine.emergency_stop()
        QMessageBox.critical(
            self,
            "긴급 정지",
            "신규 주문과 전략 실행을 즉시 중단했습니다. 기존 보유 자산은 자동으로 매도하지 않습니다.",
        )

    def poll_events(self) -> None:
        for event in self.engine.drain_events():
            event_type = event.get("type")
            if event_type == "price":
                self._price_event(event)
            elif event_type == "portfolio":
                self._portfolio_event(event)
            elif event_type == "candidates":
                markets = event.get("markets", [])
                self.candidates.setText(", ".join(markets) if markets else "-")
                if markets and not self._chart_market:
                    self._chart_market = markets[0]
            else:
                message = str(event.get("message", ""))
                if message:
                    self.log.append(f"[{event_type}] {message}")
                    self.status.setText(message)

    def _price_event(self, event: dict) -> None:
        market = str(event["market"])
        price = float(event["price"])
        self._series[market].append(price)
        if self._chart_market == market:
            values = list(self._series[market])
            self.curve.setData(values)
            self.chart_title.setText(f"{market}  {price:,.4f}")

    def _portfolio_event(self, event: dict) -> None:
        self.equity.setText(f"총 평가: {float(event['equity']):,.0f}원")
        self.cash.setText(f"KRW: {float(event['cash']):,.0f}원")
        self.pnl.setText(f"세션 수익률: {float(event['pnl_pct']):+.2f}%")
        positions = event.get("positions", [])
        self.assets.setRowCount(len(positions))
        for row_index, row in enumerate(positions):
            values = [
                row["market"],
                f"{float(row['quantity']):.8f}",
                f"{float(row['avg_price']):,.4f}",
                f"{float(row['current_price']):,.4f}",
                f"{float(row['value']):,.0f}",
            ]
            for col, value in enumerate(values):
                self.assets.setItem(row_index, col, QTableWidgetItem(str(value)))

    def closeEvent(self, event) -> None:
        self.engine.request_stop()
        super().closeEvent(event)
