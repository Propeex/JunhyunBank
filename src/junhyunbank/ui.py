from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from collections import defaultdict, deque
from typing import Any

import pyqtgraph as pg
from PySide6.QtGui import QColor
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget

from . import __version__
from .engine import TradingEngine
from .models import EngineState
from .security import KeyStore
from .upbit import UpbitClient
from .updater import check_latest, is_update_available, stage_and_launch_update


def price_text(price: float) -> str:
    decimals = 0 if price >= 10000 else 2 if price >= 100 else 4 if price >= 1 else 8
    return f'{price:,.{decimals}f}'


class WonAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [price_text(value) for value in values]


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
        self._series: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=3601)); self._chart_market = "KRW-BTC"
        self._average_prices: dict[str, float] = {}
        self._trade_history: list[dict] = []
        self._activity_signature = None
        self._close_when_drained = False; self._update_in_progress = False; self._runtime: dict[str, Any] = {}
        self._entry_diagnostics = {}
        self._position_diagnostics = {}
        self.setWindowTitle(f"JunhyunBank V{__version__}"); self.resize(1440, 1000)
        root = QWidget(); layout = QVBoxLayout(root)
        controls = QHBoxLayout(); self.api_button = QPushButton("API 키 설정"); self.update_button = QPushButton("업데이트"); self.start_button = QPushButton("시작"); self.stop_button = QPushButton("종료"); self.emergency_button = QPushButton("긴급 정지")
        controls.addWidget(QLabel("LIVE 전용")); controls.addWidget(self.api_button); controls.addWidget(self.update_button); controls.addStretch(); controls.addWidget(self.start_button); controls.addWidget(self.stop_button); controls.addWidget(self.emergency_button); layout.addLayout(controls)
        summary = QHBoxLayout(); self.status = QLabel("대기"); self.equity = QLabel("총 평가: -"); self.cash = QLabel("KRW: -"); self.pnl = QLabel("세션 수익률: -"); self.health = QLabel("전략 건강도: -"); self.regime = QLabel("시장: -")
        summary.addWidget(self.status, 2); summary.addStretch(); summary.addWidget(self.equity); summary.addWidget(self.cash); summary.addWidget(self.pnl); summary.addWidget(self.health); summary.addWidget(self.regime); layout.addLayout(summary)
        self.data_health = QLabel("데이터: 자동매매 시작 후 WebSocket 상태와 워밍업 진행률을 표시합니다."); self.data_health.setWordWrap(True); layout.addWidget(self.data_health)
        self.buy_status = QLabel("매수 상태: 시작 후 연결 상태와 진입 조건을 확인합니다.")
        self.buy_status.setWordWrap(True); self.buy_status.setStyleSheet('padding: 10px; background: #eaf2fc; color: #16385b; border-radius: 6px;')
        layout.addWidget(self.buy_status)
        self.activity_summary = QLabel('실제 체결 기록 확인 대기 · 후보 신호는 체결 건수에 포함하지 않습니다.')
        self.activity_summary.setStyleSheet('font-size: 15px; font-weight: bold; padding: 8px; color: #16385b; background: #edf7f5;')
        self.activity_summary.setWordWrap(True); layout.addWidget(self.activity_summary)
        self.pending_status = QLabel('미확정 주문 확인 대기'); self.pending_status.setWordWrap(True); layout.addWidget(self.pending_status)
        self.entry_diagnostics = QTextEdit()
        self.entry_diagnostics.setReadOnly(True)
        self.entry_diagnostics.setMaximumHeight(150)
        self.entry_diagnostics.setPlaceholderText('매수 진단: 시작 후 종목별 대기 이유, 실제 진입 품질, 예상 변동폭과 비용을 표시합니다.')
        layout.addWidget(self.entry_diagnostics)
        body = QHBoxLayout(); left = QVBoxLayout()
        candidate_box = QGroupBox("실시간 후보 / 현재가 / HotScore (진입 품질과 다름)"); candidate_layout = QVBoxLayout(candidate_box); self.candidates = QLabel("-"); self.candidates.setWordWrap(True); candidate_layout.addWidget(self.candidates); left.addWidget(candidate_box, 1)
        log_box = QGroupBox("운영 로그"); log_layout = QVBoxLayout(log_box); self.log = QTextEdit(); self.log.setReadOnly(True); log_layout.addWidget(self.log); left.addWidget(log_box, 5); body.addLayout(left, 1)
        right = QVBoxLayout(); asset_box = QGroupBox("보유 자산"); asset_layout = QVBoxLayout(asset_box); self.assets = QTableWidget(0, 6); self.assets.setHorizontalHeaderLabels(["자산", "수량", "평균단가", "현재가", "평가금액", "자동관리"]); self.assets.horizontalHeader().setStretchLastSection(True); asset_layout.addWidget(self.assets); right.addWidget(asset_box, 2)
        self.assets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.assets.cellClicked.connect(self._asset_clicked)
        self.asset_status = QLabel('잔고 확인 대기'); self.asset_status.setWordWrap(True); asset_layout.addWidget(self.asset_status)
        self.assets.setMaximumHeight(140)
        trades_box = QGroupBox('최근 실제 체결 100건 · 확인 시각 기준 / 행을 누르면 차트 이동')
        trades_layout = QVBoxLayout(trades_box)
        self.trade_table = QTableWidget(0, 6)
        self.trade_table.setHorizontalHeaderLabels(['확인 시각', '종목', '매수/매도', '체결가', '체결금액', '판단 이유'])
        self.trade_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.trade_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.trade_table.horizontalHeader().setStretchLastSection(True)
        self.trade_table.setMaximumHeight(170)
        self.trade_table.setMinimumHeight(125)
        self.trade_table.cellClicked.connect(self._trade_clicked)
        trades_layout.addWidget(self.trade_table); right.addWidget(trades_box)
        chart_box = QGroupBox("실시간 가격 · 종목을 선택해 추적"); chart_layout = QVBoxLayout(chart_box)
        chart_controls = QHBoxLayout()
        self.market_selector = QComboBox(); self.market_selector.setMinimumWidth(150); self.market_selector.addItem('KRW-BTC')
        self.range_selector = QComboBox()
        for label, seconds in [('5분', 300), ('15분', 900), ('1시간', 3600)]: self.range_selector.addItem(label, seconds)
        chart_controls.addWidget(self.market_selector); chart_controls.addWidget(self.range_selector); chart_controls.addStretch()
        chart_layout.addLayout(chart_controls)
        self.chart_title = QLabel('KRW-BTC · 가격 수신 대기')
        self.chart = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem(), 'left': WonAxis(orientation='left')})
        self.chart.setMinimumHeight(250)
        self.chart.getAxis('left').enableAutoSIPrefix(False)
        self.chart.getAxis('left').setWidth(120)
        self.chart.setBackground('#101c2c'); self.chart.showGrid(x=True, y=True, alpha=.15)
        self.chart.setLabel('left', '가격', units='원'); self.chart.setLabel('bottom', '시간')
        self.chart.setMenuEnabled(False); self.chart.setMouseEnabled(x=False, y=False)
        self.curve = self.chart.plot([], pen=pg.mkPen('#54c9ff', width=2))
        self.buy_markers = pg.ScatterPlotItem(symbol='t1', size=13, brush='#f47282', pen='#ffffff')
        self.sell_markers = pg.ScatterPlotItem(symbol='t', size=13, brush='#63a8ff', pen='#ffffff')
        self.chart.addItem(self.buy_markers); self.chart.addItem(self.sell_markers)
        self.average_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('#ffbd69', width=1.5), label='보유 평균단가 {value:,.4f}원')
        self.chart.addItem(self.average_line); self.average_line.hide()
        chart_layout.addWidget(self.chart_title); chart_layout.addWidget(self.chart)
        self.chart_note = QLabel('수신 시세 · 10초 초과 공백은 선을 끊습니다. 분홍 ▲ 매수 / 파랑 ▼ 매도는 실제 체결 확인 시점입니다.')
        self.chart_note.setWordWrap(True); chart_layout.addWidget(self.chart_note)
        self.market_selector.currentTextChanged.connect(self._select_chart_market)
        self.range_selector.currentIndexChanged.connect(self._render_chart)
        right.addWidget(chart_box, 3); body.addLayout(right, 3); layout.addLayout(body, 1); self.setCentralWidget(root)
        self.api_button.clicked.connect(self.configure_api); self.update_button.clicked.connect(self.check_for_update); self.start_button.clicked.connect(self.start_trading); self.stop_button.clicked.connect(self.stop_trading); self.emergency_button.clicked.connect(self.emergency_stop)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll_events); self.timer.start(200); self._sync_buttons()
        self._activity_event(self.engine.storage.activity_snapshot())

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
            self._position_diagnostics.clear()
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
        self.data_health.setText(self.data_health.text() + f"\n최신 체결·호가 {event.get('fresh_deep_markets', 0)}/{deep_count}개 · 후보 계산 {float(event.get('scan_seconds', 0)):.2f}초 · 주문 판단 {float(event.get('evaluation_seconds', 0)):.2f}초")
        if not candidates and float(event.get("elapsed", 0.0)) < 180.0:
            self.candidates.setText(f"전략 워밍업 중 · 실시간 가격 추적 {tracked}개 시장")

    def poll_events(self) -> None:
        for event in self.engine.drain_events():
            event_type = event.get("type")
            if event_type == "price": self._price_event(event)
            elif event_type == 'activity': self._activity_event(event)
            elif event_type == "portfolio": self._portfolio_event(event)
            elif event_type == "candidates":
                markets, scores, prices = event.get("markets", []), event.get("scores", {}), event.get("prices", {})
                lines = []
                for market in markets:
                    price = prices.get(market)
                    formatted = f"{price_text(float(price))}원" if price else "가격 수신중"
                    lines.append(f"{market}  {formatted}  Hot {float(scores.get(market, 0.0)):.1f}")
                if lines:
                    self.candidates.setText("\n".join(lines))
                    for market in markets: self._ensure_chart_market(str(market))
                elif float(self._runtime.get("elapsed", 0.0)) >= 180.0:
                    self.candidates.setText("후보 없음 · 위 데이터 상태를 확인하세요.")
            elif event_type == "entry_diagnostic":
                self._entry_diagnostics[event['market']] = event
            elif event_type == "position_diagnostic":
                self._position_diagnostics[event['market']] = event
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
                    if event_type not in {'entry_wait', 'runtime_sample'}: self.status.setText(message)
                if event_type in {"stopped", "drain_complete", "emergency"}: self._sync_buttons()
                if event_type == "drain_complete" and self._close_when_drained: self._close_when_drained = False; QTimer.singleShot(250, self.close)
        lines = []
        managed = self.engine.storage.managed_markets()
        for market, detail in list(self._position_diagnostics.items()):
            if market not in managed:
                self._position_diagnostics.pop(market, None)
                continue
            lines.append(f"[보유 {detail['elapsed']:.0f}초] {market}: {detail['reason']}")
        for market, detail in list(self._entry_diagnostics.items()):
            if market != '전체' and market not in self.engine._deep_markets:
                self._entry_diagnostics.pop(market, None)
                continue
            metrics = ''
            if 'score' in detail:
                metrics += f" · 진입 Q {detail['score']:.1f}"
            if detail.get('expected_move_pct') or detail.get('cost_pct'):
                metrics += f" · 예상 {detail.get('expected_move_pct', 0):.3%} / 비용 {detail.get('cost_pct', 0):.3%}"
                metrics += f" / 필요 > {detail.get('cost_pct', 0) * 2:.3%}"
            if 'trade_age' in detail:
                metrics += f" · 체결 {self._age_text(detail['trade_age'])} / 호가 {self._age_text(detail.get('book_age'))}"
            lines.append(f"{market}: {detail['reason']}{metrics}")
        text = '\n'.join(lines)
        if self.entry_diagnostics.toPlainText() != text:
            self.entry_diagnostics.setPlainText(text)
        details = [d for m, d in self._entry_diagnostics.items() if m != '전체']
        global_reason = self._entry_diagnostics.get('전체', {}).get('reason', '연결 및 진입 조건 확인 중')
        if not self.engine.running:
            self.buy_status.setText('자동매매 중지 · 시작하면 시세와 진입 조건을 다시 확인합니다.')
        elif details and '진입 조건 평가 중' in global_reason:
            stale = sum('지연' in d['reason'] or '오래' in d['reason'] for d in details)
            cost = sum('비용' in d['reason'] for d in details)
            self.buy_status.setText(f'최근 후보 {len(details)}개 판단 · 시세 대기 {stale}개 · 비용 조건 미달 {cost}개 · 나머지는 아래 진입 조건 확인')
        else: self.buy_status.setText(f'매수 상태: {global_reason}')
        self._render_chart()
        self._sync_buttons()

    def _price_event(self, event: dict[str, Any]) -> None:
        market, price = str(event['market']), float(event['price'])
        timestamp = float(event.get('timestamp', time.time()))
        if not math.isfinite(price) or price <= 0 or not math.isfinite(timestamp): return
        samples = self._series[market]; second = float(int(timestamp))
        if samples and second < samples[-1][0]: return
        if samples and second == samples[-1][0]: samples[-1] = (second, price)
        else: samples.append((second, price))
        while samples and samples[0][0] < second - 3600: samples.popleft()
        self._ensure_chart_market(market)

    def _ensure_chart_market(self, market: str) -> None:
        if self.market_selector.findText(market) < 0: self.market_selector.addItem(market)

    def _select_chart_market(self, market: str) -> None:
        self._chart_market = market; self._render_chart()

    def _asset_clicked(self, row: int, column: int) -> None:
        item = self.assets.item(row, 0)
        if item and item.text().startswith('KRW-'):
            self._ensure_chart_market(item.text()); self.market_selector.setCurrentText(item.text())

    def _trade_clicked(self, row: int, column: int) -> None:
        item = self.trade_table.item(row, 1)
        if item:
            self._ensure_chart_market(item.text()); self.market_selector.setCurrentText(item.text())

    def _activity_event(self, event: dict) -> None:
        counts = event.get('counts', {})
        self.activity_summary.setText(f"{event.get('date', datetime.now().date().isoformat())} 실제 체결 · 매수 {counts.get('BUY', 0)}건   매도 {counts.get('SELL', 0)}건   | 이번 실행 판단 {event.get('evaluation_cycles', 0)}회")
        pending = event.get('pending', [])
        self.pending_status.setText('주문 확인 중: ' + ' · '.join(f"{p['market']} {'매수' if p['side']=='BUY' else '매도'} ({p['created_at']})" for p in pending) if pending else '현재 미확정 주문 없음')
        trades = event.get('trades', [])
        signature = tuple(t['id'] for t in trades)
        if signature == self._activity_signature: return
        self._activity_signature = signature; self._trade_history = trades
        self.trade_table.setRowCount(len(trades))
        for row, trade in enumerate(trades):
            buy = trade['side'] == 'BUY'
            values = [trade['created_at'][:19].replace('T', ' '), trade['market'], '매수 체결' if buy else '매도 체결',
                      price_text(float(trade['price'])), f"{float(trade['amount_krw']):,.0f}원", trade['reason']]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setToolTip(str(value))
                if col == 2: item.setForeground(QColor('#c4354f' if buy else '#2166b5'))
                self.trade_table.setItem(row, col, item)
        self._render_chart()

    def _render_chart(self, *_args) -> None:
        now = time.time(); window = int(self.range_selector.currentData() or 300)
        points = [(t, p) for t, p in self._series[self._chart_market] if now - window <= t <= now + 2]
        xs, ys = [], []
        for t, p in points:
            if xs and t - xs[-1] > 10: xs.append(t - .001); ys.append(float('nan'))
            xs.append(t); ys.append(p)
        self.curve.setData(xs, ys, connect='finite')
        markers = {'BUY': ([], []), 'SELL': ([], [])}
        for trade in self._trade_history:
            if trade['market'] != self._chart_market or trade['side'] not in markers: continue
            try: timestamp = datetime.fromisoformat(trade['created_at']).timestamp()
            except (ValueError, TypeError): continue
            if now - window <= timestamp <= now:
                markers[trade['side']][0].append(timestamp); markers[trade['side']][1].append(float(trade['price']))
        self.buy_markers.setData(*markers['BUY']); self.sell_markers.setData(*markers['SELL'])
        self.chart.setXRange(now - window, now, padding=0)
        self.chart.enableAutoRange(axis='y', enable=True)
        average = self._average_prices.get(self._chart_market, 0)
        self.average_line.setVisible(average > 0)
        if average > 0: self.average_line.setValue(average)
        if points:
            t, price = points[-1]; change = (price / points[0][1] - 1) * 100
            self.chart_title.setText(f'{self._chart_market}   {price_text(price)}원   표시 구간 {change:+.2f}%   · 마지막 체결 {max(0, now-t):.0f}초 전')
        else: self.chart_title.setText(f'{self._chart_market} · 선택 구간에 수신한 체결이 없습니다')

    def _portfolio_event(self, event: dict[str, Any]) -> None:
        self.equity.setText(f"총 평가: {float(event['equity']):,.0f}원"); self.cash.setText(f"KRW: {float(event['cash']):,.0f}원"); self.pnl.setText(f"세션 수익률: {float(event['pnl_pct']):+.2f}%")
        positions = event.get("positions", []); self.assets.setRowCount(len(positions))
        coins = [p for p in positions if p['market'] != 'KRW' and float(p['quantity']) > 0]
        self._average_prices = {str(p['market']): float(p['avg_price']) for p in coins}
        self.asset_status.setText(f'코인 {len(coins)}종 보유 · 자산 행을 누르면 차트를 표시합니다.' if coins else '현재 보유 코인 없음 · 원화 대기 중입니다. 위 매수 상태에서 대기 원인을 확인하세요.')
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
