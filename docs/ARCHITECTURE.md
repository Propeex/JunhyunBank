# JunhyunBank V4.3 아키텍처

이 문서는 V4.3의 실제 구현 구조와 각 모듈의 책임을 설명합니다. 설계 의도보다 **현재 코드 동작**을 우선해 기록합니다.

---

## 1. 전체 구조

```text
Upbit Public WebSocket
  ├─ trade (전체 KRW)
  └─ orderbook (fresh actionable 후보 + managed positions)
          │
          ▼
  MarketStream
          │
          ▼
  MicroFlowStrategy
  ├─ 1초 frame
  ├─ rolling percentile
  ├─ HotScore
  ├─ Market Regime
  ├─ Entry decision
  └─ Exit decision
          │
          ▼
  TradingEngine
  ├─ portfolio reconciliation
  ├─ fee / minimum order lookup
  ├─ Best+IOC top-level capacity / fee+spread cost check
  ├─ Strategy Health
  ├─ dynamic risk budgeting
  ├─ order execution
  └─ DRAINING state
          │
          ├──────────► Upbit REST /v1/orders, /v1/order, /v1/accounts
          │
          ├──────────► SQLite Storage
          │
          └──────────► bounded/coalesced UI EventBuffer
```

별도 축:

```text
GitHub latest Release
      │
      ▼
 updater.py
      │ SHA-256 verify
      ▼
 PowerShell helper
      │
      ├─ old EXE backup
      ├─ new EXE replace
      ├─ 새 EXE/DB health verify
      ├─ 실패 시 EXE+DB snapshot rollback
      └─ 검증 성공 시 restart (--resume-trading optional)
```

---

## 2. 모듈 책임

### `main.py`

- QApplication 생성
- OS keyring에서 API Key 로드
- 없으면 `ApiKeyDialog`
- API Key 없이 PAPER로 진입하지 않고 종료
- 저장된 키로 `get_accounts()`를 호출해 유효성 확인
- `TradingEngine`, `MainWindow` 생성
- `--resume-trading`이면 UI 로드 후 자동 `engine.start()`

### `ui.py`

UI thread만 담당합니다.

주요 컨트롤:

- API 키 설정
- 업데이트
- 시작
- 종료
- 긴급 정지

레이아웃:

- 좌측: 후보 + 운영 로그
- 우측 상단: 보유자산
- 우측 하단: 가격 차트

포트폴리오 테이블 첫 행에 KRW를 표시합니다.

엔진과 직접 복잡한 연산을 공유하지 않고 `engine.events`를 polling합니다. 이 버퍼는 가격·후보·진단 같은 고빈도 상태를 유형/시장별 최신값으로 병합하고 로그성 이벤트는 FIFO로 유지하며, 전체 5,000건 기본 하드 상한을 적용합니다.

### `engine.py` / `runtime_engine.py` / `live_engine.py`

거래 lifecycle의 orchestration 계층입니다. `runtime_engine.py`가 market discovery와 stream lifecycle을, production `live_engine.py`가 Best+IOC pre-trade 의미를 확정합니다.

책임:

- 대표시장·크기·직전 대비 유지율·경보 schema를 검사하는 market universe 갱신
- public stream 생성/재시작
- managed positions를 deep orderbook 구독에 강제 포함
- 주기적 portfolio refresh
- 전략 후보 랭킹
- entry/exit 평가
- fee/minimum lookup cache
- Best+IOC 최우선 한 가격단계 capacity와 수수료·spread 기반 비용 계산
- 주문 제출 및 주문 결과 polling
- managed quantity 영속화
- Strategy Health 적용
- 관리전략 전용 실현·평가손익 기반 세션 손실 gate
- 정지와 주문 POST를 직렬화하는 submission gate 및 최종 데이터 재확인
- RUNNING / DRAINING / STOPPED lifecycle

### `strategy.py`

주문을 직접 보내지 않습니다. 시장 데이터 상태와 순수 전략 판단만 담당하는 것이 원칙입니다.

내부 상태:

- `_frames[market]`: 완료된 1초 frame deque
- `_current[market]`: 현재 초 frame
- `_books[market]`: 최신 orderbook snapshot
- `_book_history[market]`: 초별 imbalance/micro-bias
- `_weak_counts`: 연속 약화 카운트
- `_blocked_after_exit`: 동일 신호 재진입 방지

### `health.py`

최근 `normalized_return` 결과를 받아 0~1 health multiplier를 반환합니다.

현재 구현은 rolling weighted mean + win rate 기반 heuristic이며 통계 검정/베이지안 모델은 아닙니다.

### `risk.py`

V4.3의 RiskManager는 **시스템 안전 게이트와 손실위험 기반 신규주문 예산**을 담당합니다.

차단 조건:

- emergency
- 연속 API failure 한도
- stale market data
- 동적 주문금액이 exchange minimum 미만
- available cash 부족
- 관리전략 전용 세션 손실 중단조건
- 현금 예비금, 단일·총 암호자산 노출
- 초기 손절거리 기반 거래당·포트폴리오 계획위험
- 최우선호가 표시 유동성 참여율

`1회 N원`, `시간당 N회`, `최대 N종목` 같은 임의의 고정 전략 cap은 없습니다. 대신 계정 평가액과 현재 시장상태에 비례하는 안전상한을 사용합니다. 계획위험은 갭·API 장애 때 실제 손실을 보장하지 않습니다.

### `storage.py`

SQLite 영속성 담당.

동기화는 process 내부 `threading.Lock`을 사용합니다.

영속화 대상:

- events
- trades
- managed positions
- normalized health와 별도 net KRW 손익을 가진 strategy outcomes
- outcome 정규화 기준은 손상됐지만 실현손익은 계산 가능한 SELL을 위한 `strategy_pnl_adjustments` 원장과 exchange order UUID 기반 중복 방지
- order submit/accept 시각, persisted trailing stop, managed status/absence state

실시간 raw trade/orderbook은 현재 영속화하지 않습니다.

### `upbit.py`

REST + JWT client.

- JWT HS512
- nonce UUID
- query/body가 있으면 SHA512 query hash
- private request serialization lock
- 약 11 req/s 내부 rate guard
- GET timeout/429/5xx 제한 재시도, POST no-retry, 418 error
- accounts, tickers, market list, order chance, order status
- SMP `cancel_taker`를 포함한 best IOC buy/sell
- market buy/sell fallback method

### `market_stream.py`

public WebSocket client.

- `trade`
- `orderbook`
- timeout 시 ping
- disconnect 시 exponential backoff reconnect
- 거래소 timestamp stale/future event 차단

Private WebSocket은 `private_stream.py`에서 `myOrder`/`myAsset` 보조 신호로 사용하며 회계 정본은 identifier REST입니다.

### `updater.py`

배포 EXE만 대상으로 self-update orchestration.

API Key / DB는 실행파일 밖에 있으므로 교체 대상이 아닙니다.

---

## 3. 시장 데이터 흐름

### 전체시장 trade

`engine._refresh_markets()`가 KRW market list를 가져와 warning/caution 종목을 제외합니다.

허용 market을 최대 100개 단위로 나누어 여러 `MarketStream`을 만들고 모든 종목의 `trade` realtime event를 수신합니다.

`strategy.on_trade()`에서 1초 단위로 집계합니다.

각 frame에는:

- OHLC
- BID aggressor 체결대금
- ASK aggressor 체결대금
- 총 체결대금
- 체결 건수

가 들어갑니다.

체결 없는 초는 마지막 가격의 보합 frame + 거래대금 0으로 채웁니다. 이것은 `10초 수익률`이 실제 wall-clock 10초가 되게 하기 위한 중요한 구현입니다.

오래 거래가 없는 gap이 baseline 길이보다 길면 과거 frame/book history를 버립니다.

### Fresh actionable 후보 orderbook

전체 trade에서 HotScore를 계산한 뒤 live stale 기준으로 비관리 후보를 거르고, 잘린 scanner 목록 밖의 fresh 시장을 보충합니다. 신선한 기존 deep 후보에는 최소 체류시간과 교체 margin을 적용하며, 이 선택기는 LIVE·recorder·validator가 공유합니다. 최종 후보 중 `deep_candidate_count`를 orderbook 정밀분석 대상으로 사용합니다.

단, JunhyunBank managed position은 Hot 후보가 아니어도 deep stream에 반드시 포함합니다.

현재 기본 분석 depth는 5개 호가쌍입니다.

---

## 4. 엔진 loop

시작 순서는 다음과 같습니다.

```text
pending 주문(특히 SELL) identifier REST reconciliation
  ↓
계정 조회
  ↓
관리전략 실현·평가손익 baseline 구성
  ↓
engine thread 시작
```

계정 조회나 손익 baseline 구성이 실패해도 관리 포지션 청산 엔진은 시작합니다. 다만 해당 실행 세션의 신규매수는 영구 잠금하며, 원인을 확인하고 프로그램을 재시작하기 전에 자동 rebase하여 매수를 다시 열지 않습니다.

엔진 thread는 대략 다음 cadence로 동작합니다.

```text
market universe refresh  : 300s 기본
candidate refresh        : 3s 기본
strategy evaluation      : 1s 기본
portfolio publish        : 1s 기본
loop sleep               : 0.1s
```

각 loop에서:

1. market list 필요 시 갱신
2. 후보 랭킹/호가 스트림 갱신
3. 포지션 청산 및 신규진입 평가
4. UI용 portfolio publish
5. DRAINING이면 managed_positions가 0인지 확인

---

## 5. 신규진입 흐름

```text
portfolio 조회
  ↓
managed positions exit 우선 처리
  ↓
state == RUNNING 확인
  ↓
Strategy Health 계산
  ↓
Market Regime 계산
  ↓
ranked deep markets
  ↓
이미 보유/managed 종목 제외
  ↓
fee / min order 조회
  ↓
strategy.evaluate_entry()
  ↓
BUY 후보 score 순 정렬
  ↓
desired KRW = remaining cash × capital_fraction
  ↓
Best+IOC ask1·bid1 즉시 체결 capacity와 위험예산 계산
  ↓
실제 계정 수수료 + spread 비용 확인
  ↓
expected move > actual cost × 2 확인
  ↓
RiskManager system safety gate
  ↓
state == RUNNING 재확인
  ↓
best + IOC BUY
  ↓
managed marker 즉시 저장
  ↓
GET /v1/order polling
  ↓
actual executed volume / avg fill price 저장
```

### 중요한 경쟁조건 방지

사용자가 `종료`를 누르는 동안 이미 BUY 계산이 진행 중일 수 있습니다. 그래서 engine은 주문 루프와 `_buy_managed()` 직전 모두 `state == RUNNING`을 다시 확인합니다.

---

## 6. 포지션 관리 흐름

계정 전체 잔고를 조회하지만 매도대상은 `storage.managed_markets()`로 제한합니다.

각 managed market:

1. account position 존재 여부와 반복 누락 상태 확인
2. persisted `managed_quantity` 읽기
3. 한 orderbook generation의 fresh bid·spread·depth·수신시각으로 실행호가와 current/entry/peak 가격 구성
4. fee + spread 기반 round trip cost 재계산
5. 진입 시 저장된 `initial_risk_pct` 사용
6. `strategy.evaluate_position()`
7. 새 trailing stop을 SQLite에 ratchet 저장
8. SELL이면 submission gate 안에서 한 generation의 실행호가로 보유정책 전체를 최종 재평가
9. 재평가 중 generation token이 바뀌지 않았고 최종 판단도 SELL일 때만 `_sell_managed()`

### managed quantity

계정 총수량이 영속 `managed_quantity`보다 작으면 소유권을 안전하게 판별할 수 없으므로 `QUARANTINED`로 격리하고 주문하지 않습니다. 총수량이 관리수량 이상일 때만 매도 주문수량을 다음과 같이 제한합니다.

```text
min(persisted managed_quantity, currently available balance)
```

이것이 사용자의 기존 보유분 보호의 핵심입니다.

### dust

관리 잔여수량의 원화가치가 최소 주문금액 아래면 `DUST`로 보존합니다. 단, 계정 총수량은 충분하고 잠금 때문에 현재 주문 가능 수량의 가치만 최소주문 미만이면 `DUST`로 오인하지 않고 `ACTIVE`로 유지합니다. 한 번의 accounts 누락으로 관리 해제하지 않으며 반복 누락은 `QUARANTINED`로 격리합니다. 두 상태 모두 계정 총잔고를 managed quantity로 추정하지 않고 DRAINING의 자동처리 대기대상에서는 제외합니다.

---

## 7. 일반 종료 / 긴급정지 / 창 닫기

### 일반 종료

`request_stop()`:

```text
RUNNING → DRAINING
```

- 신규매수 금지
- managed position 매도 규칙 계속 실행
- 모두 정리되면 stopped

### 창 X 버튼

엔진 RUNNING이면 사용자 확인 후 DRAINING으로 전환하고 창을 유지합니다.

DRAINING 중 다시 닫으려 하면 즉시 종료하지 않고 대기 안내를 표시합니다.

### 긴급정지

`emergency_stop()`:

- emergency flag set
- hard stop event set
- engine state STOPPED
- 기존 코인 강제청산 안 함

---

## 8. 업데이트 흐름

UI의 업데이트 버튼은 별도 worker thread에서 GitHub Release 조회/다운로드를 수행합니다.

거래 중이면 `resume_trading=True`를 updater에 넘깁니다.

새 파일 download + digest 검증이 끝난 다음에만 engine을 shutdown합니다. 즉 네트워크 다운로드 실패 때문에 자동매매를 먼저 멈추지는 않습니다.

그 후 PowerShell helper가 부모 EXE 종료를 기다리고 파일을 교체합니다.

새 EXE는 별도 검증 모드에서 DB migration, 무결성, 필수 테이블과 health marker를 확인합니다. 실패하면 EXE와 업데이트 직전 DB snapshot을 함께 복원하며 자동매매를 자동 재개하지 않습니다.

---

## 9. Threading 모델

- Qt main/UI thread
- TradingEngine daemon thread
- public MarketStream별 daemon thread
- update check/download worker thread
- update 적용 PowerShell child process

SQLite method는 내부 lock으로 보호합니다.

`MicroFlowStrategy`는 trade/orderbook stream thread에서 쓰고 engine thread에서 읽는 상태 갱신과 주요 평가를 내부 `RLock`과 동기화 wrapper로 보호합니다. `book()`은 lock 안에서 한 번 게시된 `BookSnapshot`을 반환하며, 청산 경로는 이 한 snapshot에서 bid·depth·spread·수신시각을 함께 만듭니다. 안전에 중요한 판단에서는 `book()`과 별도 age getter를 조합하지 않고 하나의 `ExecutableQuote`와 generation token을 사용합니다.

---

## 10. 장애/복구 모델

### Public WebSocket 끊김

각 stream이 reconnect를 시도합니다. 신규진입은 trade/orderbook age가 stale threshold를 넘으면 Safety gate에서 막힙니다.

### Private REST 오류

연속 failure counter가 올라가며 한도 도달 시 신규진입을 막습니다. 성공 요청이 들어오면 counter는 0으로 reset됩니다.

### 앱 재시작

- API Key: keyring에서 복구
- managed positions: SQLite에서 복구
- pending 주문은 계정·세션 기준을 만들기 전 identifier REST로 먼저 reconciliation
- 계정 또는 관리전략 PnL baseline 확인 실패 시 청산은 유지하되 해당 세션 신규매수는 재시작 전까지 영구 잠금
- 전략 rolling market state: **복구하지 않음**, 다시 warmup
- managed position emergency stop: persisted initial risk를 활용할 수 있지만 current market price가 들어와야 판단 가능

### 업데이트 재시작

`--resume-trading`이 있으면 약간의 UI 로드 지연 뒤 LIVE engine을 자동 시작합니다.

---

## 11. 설계상 다음 개선 포인트

- Best+IOC latency/partial/no-fill execution simulator
- recorder 장기간 데이터와 purged walk-forward/OOS
- conditional directional ExpectedMove
- portfolio correlation risk
- strategy state snapshot/persistence
- structured observability/metrics
- quarantined/dust operator reconciliation UI

상세 우선순위는 `VALIDATION_AND_ROADMAP.md`를 참조합니다.
