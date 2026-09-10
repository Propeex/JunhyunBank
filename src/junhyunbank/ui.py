from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget

from . import __version__
from .engine import TradingEngine
from .models import EngineState
from .security import KeyStore
from .upbit import UpbitClient
from .updater import check_latest, is_update_available, stage_and_launch_update


class ApiKeyDialog(QDialog):
    def __init__(self, key_store: KeyStore, parent=None) -> None:
        super().__init__(parent); self.key_store = key_store; self.setWindowTitle("업비트 API 키 설정")
        self.access = QLineEdit(); self.secret = QLineEdit(); self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel("LIVE 전용 프로그램입니다. 출금 권한은 부여하지 마세요. 거래/잔고조회 권한만 사용합니다."); self.status.setWordWrap(True)
        form = QFormLayout(); form.addRow("Access Key", self.access); form.addRow("Secret Key", self.secret); form.addRow(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject); form.addRow(buttons); self.setLayout(form)

    def _save(self) -> None:
        access, secret = self.access.text().strip(), self.secret.text().strip()
        try:
            tester = UpbitClient(access, secret)
            try: tester.test_credentials()
            finally: tester.close()
            self.key_store.save(access, secret)
        except Exception as exc:
            QMessageBox.critical(self, "API 연결 실패", str(exc)); return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, engine: TradingEngine, key_store: KeyStore) -> None:
        super().__init__(); self.engine = engine; self.key_store = key_store
        self._series: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=360)); self._chart_market: str | None = "KRW-BTC"
        self._close_when_drained = False; self._update_in_progress = False; self._runtime: dict[str, Any] = {}
        self._entry_diagnostics = {}
        self.setWindowTitle(f"JunhyunBank V{__version__}"); self.resize(1320, 920)
        root = QWidget(); layout = QVBoxLayout(root)
        controls = QHBoxLayout(); self.api_button = QPushButton("API 키 설정"); self.update_button = QPushButton("업데이트"); self.start_button = QPushButton("시작"); self.stop_button = QPushButton("종료"); self.emergency_button = QPushButton("긴급 정지")
        controls.addWidget(QLabel("LIVE 전용")); controls.addWidget(self.api_button); controls.addWidget(self.update_button); controls.addStretch(); controls.addWidget(self.start_button); controls.addWidget(self.stop_button); controls.addWidget(self.emergency_button); layout.addLayout(controls)
        summary = QHBoxLayout(); self.status = QLabel("대기"); self.equity = QLabel("총 평가: -"); self.cash = QLabel("KRW: -"); self.pnl = QLabel("세션 수익률: -"); self.health = QLabel("전략 건강도: -"); self.regime = QLabel("시장: -")
        summary.addWidget(self.status, 2); summary.addStretch(); summary.addWidget(self.equity); summary.addWidget(self.cash); summary.addWidget(self.pnl); summary.addWidget(self.health); summary.addWidget(self.regime); layout.addLayout(summary)
        self.data_health = QLabel("데이터: 자동매매 시작 후 WebSocket 상태와 워밍업 진행률을 표시합니다."); self.data_health.setWordWrap(True); layout.addWidget(self.data_health)
        self.entry_diagnostics = QTextEdit()
        self.entry_diagnostics.setReadOnly(True)
        self.entry_diagnostics.setMaximumHeight(150)
        self.entry_diagnostics.setPlaceholderText('매수 진단: 시작 후 종목별 대기 이유, 실제 진입 품질, 예상 변동폭과 비용을 표시합니다.')
        layout.addWidget(self.entry_diagnostics)
        body = QHBoxLayout(); left = QVBoxLayout()
        candidate_box = QGroupBox("실시간 후보 / 현재가 / HotScore (진입 품질과 다름)"); candidate_layout = QVBoxLayout(candidate_box); self.candidates = QLabel("-"); self.candidates.setWordWrap(True); candidate_layout.addWidget(self.candidates); left.addWidget(candidate_box, 1)
        log_box = QGroupBox("운영 로그"); log_layout = QVBoxLayout(log_box); self.log = QTextEdit(); self.log.setReadOnly(True); log_layout.addWidget(self.log); left.addWidget(log_box, 5); body.addLayout(left, 1)
        right = QVBoxLayout(); asset_box = QGroupBox("보유 자산"); asset_layout = QVBoxLayout(asset_box); self.assets = QTableWidget(0, 6); self.assets.setHorizontalHeaderLabels(["자산", "수량", "평균단가", "현재가", "평가금액", "자동관리"]); self.assets.horizontalHeader().setStretchLastSection(True); asset_layout.addWidget(self.assets); right.addWidget(asset_box, 2)
        chart_box = QGroupBox("실시간 가격"); chart_layout = QVBoxLayout(chart_box); self.chart_title = QLabel("KRW-BTC 실시간 가격 수신 대기"); self.chart = pg.PlotWidget(); self.curve = self.chart.plot([]); chart_layout.addWidget(self.chart_title); chart_layout.addWidget(self.chart); right.addWidget(chart_box, 3); body.addLayout(right, 3); layout.addLayout(body, 1); self.setCentralWidget(root)
        self.api_button.clicked.connect(self.configure_api); self.update_button.clicked.connect(self.check_for_update); self.start_button.clicked.connect(self.start_trading); self.stop_button.clicked.connect(self.stop_trading); self.emergency_button.clicked.connect(self.emergency_stop)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll_events); self.timer.start(200); self._sync_buttons()

    def _sync_buttons(self) -> None:
        running = self.engine.running; draining = self.engine.state == EngineState.DRAINING
        self.start_button.setEnabled(not running and not self._update_in_progress); self.stop_button.setEnabled(running and not draining and not self._update_in_progress); self.api_button.setEnabled(not running and not self._update_in_progress); self.update_button.setEnabled(not draining and not self._update_in_progress)

    def configure_api(self) -> None:
        if ApiKeyDialog(self.key_store, self).exec():
            access, secret = self.key_store.load(); self.engine.client.access_key = access; self.engine.client.secret_key = secret; self.log.append("[security] 새 API 키를 현재 세션에 적용했습니다.")

    def start_trading(self) -> None:
        if not self.key_store.exists(): QMessageBox.warning(self, "API 키 필요", "JunhyunBank는 LIVE 전용이므로 업비트 API 키가 필요합니다."); return
        result = QMessageBox.warning(self, "실거래 시작", "JunhyunBank는 실제 원화로 자동 주문합니다. LIVE 자동매매를 시작하시겠습니까?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if result == QMessageBox.StandardButton.Yes: self.start_trading_silent()

    def start_trading_silent(self) -> None:
        try:
            self._entry_diagnostics.clear()
            self.entry_diagnostics.clear()
            self.data_health.setText("데이터: WebSocket 연결 중 · 전략 워밍업은 기본 약 3분입니다.")
            self.engine.start(); self._sync_buttons()
        except Exception as exc: QMessageBox.critical(self, "시작 실패", str(exc))

    def stop_trading(self) -> None:
        if not self.engine.running: return
        managed = len(self.engine.storage.managed_markets())
        result = QMessageBox.question(self, "종료 대기 모드", f"신규 매수를 즉시 중단합니다.\n\n현재 JunhyunBank 관리 포지션: {managed}개\n관리 중인 코인은 MicroFlow 청산 규칙에 따라 매도될 때까지 계속 감시합니다.\n포지션이 모두 정리되면 자동매매 엔진이 종료됩니다.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if result == QMessageBox.StandardButton.Yes: self.engine.request_stop(); self._sync_buttons()

    def emergency_stop(self) -> None:
        result = QMessageBox.warning(self, "긴급 정지 확인", "전략과 신규 주문을 즉시 중단합니다. 현재 보유 포지션을 강제 매도하지 않습니다.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if result != QMessageBox.StandardButton.Yes: return
        self.engine.emergency_stop(); self._sync_buttons(); QMessageBox.critical(self, "긴급 정지", "자동 주문을 즉시 중단했습니다. 보유 포지션은 그대로 유지됩니다.")

    def check_for_update(self) -> None:
        if self.engine.state == EngineState.DRAINING: QMessageBox.information(self, "업데이트 대기", "현재 종료 대기 중입니다. 관리 포지션 청산이 끝난 뒤 업데이트해 주세요."); return
        if self._update_in_progress: return
        self._update_in_progress = True; self._sync_buttons(); self.status.setText("최신 GitHub Release 확인 중...")
        def worker() -> None:
            try:
                info = check_latest(); self.engine.events.put({"type": "update_check", "available": is_update_available(info), "info": info})
            except Exception as exc: self.engine.events.put({"type": "update_error", "message": f"업데이트 확인 실패: {exc}"})
        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _confirm_update(self, info: Any) -> None:
        if not is_update_available(info):
            self._update_in_progress = False; self._sync_buttons(); QMessageBox.information(self, "최신 버전", f"현재 V{__version__.split('.')[0]}가 최신 Release입니다."); return
        resume = self.engine.state == EngineState.RUNNING; self.status.setText(f"{info.tag} 다운로드 및 SHA-256 검증 중...")
        def worker() -> None:
            try:
                stage_and_launch_update(info, resume_trading=resume); self.engine.events.put({"type": "update_ready", "resume_trading": resume})
            except Exception as exc: self.engine.events.put({"type": "update_error", "message": f"업데이트 실패: {exc}"})
        threading.Thread(target=worker, name="update-download", daemon=True).start()

    @staticmethod
    def _age_text(value: Any) -> str:
        if value is None:
            return "수신없음"
        try:
            age = float(value)
        except (TypeError, ValueError):
            return "수신없음"
        if not math.isfinite(age):
            return "수신없음"
        return f"{age:.1f}초 전"

    def _runtime_event(self, event: dict[str, Any]) -> None:
        self._runtime = event
        global_connected = int(event.get("global_connected", 0)); global_total = int(event.get("global_total", 0))
        deep_connected = bool(event.get("deep_connected")); deep_count = int(event.get("deep_markets", 0))
        tracked = int(event.get("tracked_markets", 0)); warmed = int(event.get("warmed_markets", 0)); allowed = int(event.get("allowed_markets", 0)); candidates = int(event.get("candidate_count", 0))
        trade_age = self._age_text(event.get("trade_age")); deep_age = self._age_text(event.get("deep_age")) if deep_count else "후보 대기"
        deep_state = "연결" if deep_connected else ("대기" if deep_count == 0 else "재연결")
        self.data_health.setText(f"데이터: Trade WS {global_connected}/{global_total} · 마지막 체결 {trade_age} · 추적 {tracked}/{allowed} · 워밍업 완료 {warmed} · 후보 {candidates} · Orderbook {deep_state}({deep_count}) / {deep_age}")
        if not candidates and float(event.get("elapsed", 0.0)) < 180.0:
            self.candidates.setText(f"전략 워밍업 중 · 실시간 가격 추적 {tracked}개 시장")

    def poll_events(self) -> None:
        for event in self.engine.drain_events():
            event_type = event.get("type")
            if event_type == "price": self._price_event(event)
            elif event_type == "portfolio": self._portfolio_event(event)
            elif event_type == "candidates":
                markets, scores, prices = event.get("markets", []), event.get("scores", {}), event.get("prices", {})
                lines = []
                for market in markets:
                    price = prices.get(market)
                    price_text = f"{float(price):,.4f}원" if price else "가격 수신중"
                    lines.append(f"{market}  {price_text}  Hot {float(scores.get(market, 0.0)):.1f}")
                if lines:
                    self.candidates.setText("\n".join(lines))
                    if self._chart_market not in markets:
                        self._chart_market = str(markets[0]); self.curve.setData(list(self._series[self._chart_market]))
                elif float(self._runtime.get("elapsed", 0.0)) >= 180.0:
                    self.candidates.setText("후보 없음 · 위 데이터 상태를 확인하세요.")
            elif event_type == "entry_diagnostic":
                self._entry_diagnostics[event['market']] = event
            elif event_type == "runtime_health": self._runtime_event(event)
            elif event_type == "stream_status":
                if event.get("state") == "reconnecting":
                    self.log.append(f"[websocket] {event.get('name')}: 재연결 · {event.get('message', '')}")
            elif event_type == "deep_set":
                pass
            elif event_type == "strategy_health": self.health.setText(f"전략 건강도: {float(event.get('health', 0.0)):.2f}"); self.regime.setText(f"시장: {event.get('regime', '-')}")
            elif event_type == "update_check": self._confirm_update(event.get("info"))
            elif event_type == "update_error":
                self._update_in_progress = False; self._sync_buttons(); message = str(event.get("message", "업데이트 실패")); self.log.append(f"[update] {message}"); QMessageBox.critical(self, "업데이트", message)
            elif event_type == "update_ready":
                self.status.setText("업데이트 파일 검증 완료. 재시작합니다."); self.engine.shutdown_for_update(); self.engine.wait(3.0); QApplication.quit()
            else:
                message = str(event.get("message", ""))
                if message:
                    self.log.append(f"[{event_type}] {message}")
                    if event_type != 'entry_wait': self.status.setText(message)
                if event_type in {"stopped", "drain_complete", "emergency"}: self._sync_buttons()
                if event_type == "drain_complete" and self._close_when_drained: self._close_when_drained = False; QTimer.singleShot(250, self.close)
        lines = []
        for market, detail in list(self._entry_diagnostics.items()):
            if market != '전체' and market not in self.engine._deep_markets:
                self._entry_diagnostics.pop(market, None)
                continue
            metrics = ''
            if 'score' in detail:
                metrics += f" · 진입 Q {detail['score']:.1f}"
            if detail.get('expected_move_pct') or detail.get('cost_pct'):
                metrics += f" · 예상 {detail.get('expected_move_pct', 0):.3%} / 비용 {detail.get('cost_pct', 0):.3%}"
            lines.append(f"{market}: {detail['reason']}{metrics}")
        text = '\n'.join(lines)
        if self.entry_diagnostics.toPlainText() != text:
            self.entry_diagnostics.setPlainText(text)
        self._sync_buttons()

    def _price_event(self, event: dict[str, Any]) -> None:
        market, price = str(event["market"]), float(event["price"]); self._series[market].append(price)
        if self._chart_market == market: self.curve.setData(list(self._series[market])); self.chart_title.setText(f"{market}  {price:,.4f}")

    def _portfolio_event(self, event: dict[str, Any]) -> None:
        self.equity.setText(f"총 평가: {float(event['equity']):,.0f}원"); self.cash.setText(f"KRW: {float(event['cash']):,.0f}원"); self.pnl.setText(f"세션 수익률: {float(event['pnl_pct']):+.2f}%")
        positions = event.get("positions", []); self.assets.setRowCount(len(positions))
        for row_index, row in enumerate(positions):
            market = str(row["market"])
            values = ["KRW (원화)", f"{float(row['quantity']):,.0f}", "-", "-", f"{float(row['value']):,.0f}원", "-"] if market == "KRW" else [market, f"{float(row['quantity']):.8f}", f"{float(row['avg_price']):,.4f}", f"{float(row['current_price']):,.4f}", f"{float(row['value']):,.0f}원", "JunhyunBank" if row.get("managed") else "사용자 보유"]
            for col, value in enumerate(values): self.assets.setItem(row_index, col, QTableWidgetItem(str(value)))

    def closeEvent(self, event) -> None:
        if self._update_in_progress: event.ignore(); return
        if self.engine.running:
            if self.engine.state == EngineState.RUNNING:
                result = QMessageBox.question(self, "자동매매 종료", "창을 닫으면 신규 매수를 중단하고 관리 포지션이 전략에 따라 모두 청산된 뒤 종료합니다.\n계속하시겠습니까?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
                if result != QMessageBox.StandardButton.Yes: event.ignore(); return
                self._close_when_drained = True; self.engine.request_stop(); event.ignore(); return
            if self.engine.state == EngineState.DRAINING:
                self._close_when_drained = True; QMessageBox.information(self, "종료 대기 중", "관리 포지션을 청산할 때까지 창을 유지합니다. 즉시 중단하려면 긴급 정지를 사용하세요."); event.ignore(); return
        super().closeEvent(event)
