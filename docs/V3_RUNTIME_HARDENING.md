# V3 Runtime Hardening

기준 버전: **V3 / 3.0.0**

이 문서는 V2 실사용 중 관찰된 “10분 이상 매수 없음 + 후보/가격 표시 부재” 문제를 분석한 뒤 V3에서 무엇을 고쳤고 무엇을 의도적으로 건드리지 않았는지 기록합니다.

## 1. 발견된 핵심 문제

### Public WebSocket Origin

`websocket-client`는 기본 handshake에 Origin 헤더를 넣을 수 있습니다. Upbit Public WebSocket/Quotation 요청은 Origin이 포함되면 일반 연결 한도와 별개로 **10초당 1회**의 강화된 제한을 받습니다.

V2의 `MarketStream`은 `suppress_origin=True`를 지정하지 않았고 여러 trade stream 및 deep orderbook stream을 사용하므로 실사용 환경에서 rate-limit/reconnect 불안정이 발생할 가능성이 있었습니다.

### Deep orderbook reconnect churn

V2는 Hot 후보 집합이 바뀌면 deep orderbook WebSocket을 stop하고 새 연결을 만들었습니다. 후보 갱신은 기본 3초이므로 후보 순위가 흔들리는 시장에서 연결 churn과 짧은 데이터 gap이 생길 수 있었습니다.

Upbit 공식 WebSocket Best Practice는 새로운 데이터를 구독할 때 연결을 새로 만들 필요 없이 **기존 연결에 새로운 구독 메시지를 전송하면 이전 구독을 중단하고 새 스트림을 시작할 수 있음**을 안내합니다.

### 관측성 부족

V2 UI에서는 후보 영역에 가격이 표시되지 않았고 후보가 생기기 전에는 차트 대상도 없었습니다. 따라서 “전략 워밍업/신호 없음”과 “시장 데이터 수신 장애”를 사용자가 구분하기 어려웠습니다.

## 2. V3 변경사항

### `market_stream.py`

- Public WebSocket 연결에 `suppress_origin=True`
- 프로세스 전체 연결 시도를 lock으로 직렬화하고 최소 0.25초 간격 적용
- 연결 상태/메시지 수/마지막 오류를 진단할 수 있도록 상태 정보 제공
- `update_markets()` 추가: 연결된 소켓에 새 구독 메시지를 보내 market set 교체
- Upbit WebSocket `error` payload를 정상 시세로 처리하지 않고 오류로 승격
- `stop()` 시 활성 socket을 닫아 blocking `recv()`를 즉시 깨움

### `runtime_engine.py`

V2의 핵심 `engine.TradingEngine`을 상속한 안정화 계층입니다.

- 주문/리스크/자금배분/청산/DRAINING 로직은 V2 base engine 그대로 사용
- deep orderbook 후보 변경 시 WebSocket 객체를 재생성하지 않고 `update_markets()` 사용
- market이 deep set에서 빠졌다가 다시 들어올 때 그 market의 **orderbook snapshot/history만 초기화**
- trade/momentum baseline은 그대로 유지

이 분리는 런타임 안정화가 실제 매수/매도 판단 코드에 불필요하게 영향을 주지 않기 위한 것입니다.

### UI

- Trade WS 연결 수
- 마지막 trade message age
- 실시간 가격 추적 시장 수
- 전략 워밍업 완료 시장 수
- 후보 수
- deep Orderbook 연결상태/age
- 후보별 현재가 + HotScore
- 후보가 없어도 KRW-BTC 가격 차트 기본 표시
- 5분 이상 후보 0개면 데이터 상태 확인 경고

## 3. 의도적으로 변경하지 않은 것

V3 runtime hardening에서는 아래 JH-MicroFlow 거래 규칙을 완화하거나 공격적으로 변경하지 않습니다.

- `ignition_quality`
- `pullback_quality`
- cost gate 배수
- capital fraction 계산식
- Strategy Health 계산
- Market Regime 기준
- Adaptive Trailing
- Emergency Stop 계산
- expected horizon 청산
- managed quantity 보호
- DRAINING 동작
- 기존 사용자 보유자산 보호

즉 V3의 목표는 **더 자주 매수하게 만드는 것**이 아니라 “정상 데이터를 안정적으로 보고 있는지 확실하게 만들고 그 상태를 사용자에게 보여주는 것”입니다.

## 4. 자동 검증

### 단위/회귀 테스트

`tests/test_market_stream.py`

- Origin suppression 확인
- `.5` orderbook subscription 확인
- 기존 socket에서 market subscription 갱신 확인
- stop 시 active socket close 확인
- Upbit error payload 처리 확인

`tests/test_runtime_engine.py`

- deep 후보 변경 시 같은 WebSocket 객체를 재사용하는지 확인
- re-entry market의 오래된 orderbook state 제거 확인
- deep set empty 시 stream 종료 확인

기존 auth/risk/strategy/storage/updater/engine 테스트도 그대로 실행하여 자동매매 핵심 회귀를 확인합니다.

### 실서버 read-only smoke test

`scripts/smoke_upbit_public_ws.py`

- API Key 사용 안 함
- 주문 API 사용 안 함
- KRW-BTC `trade` + `orderbook.5` 동시 구독
- 실제 Upbit Public WebSocket에서 두 타입 모두 일정 시간 안에 수신해야 성공
- CI와 Windows Release workflow에서 실행

이 테스트를 통과하기 전에는 Windows Release를 생성하지 않습니다.

## 5. 여전히 남은 주요 위험

V3가 해결하지 않는 문제도 명확히 유지합니다.

1. `ExpectedMove`는 아직 진정한 conditional forward edge 모델이 아니라 최근 절대 변동폭 proxy입니다.
2. private `myOrder` 기반 durable order state machine이 아직 없습니다.
3. ambiguous POST timeout/restart recovery가 완전하지 않습니다.
4. partial exit 전체 가중평균 체결손익이 Strategy Health에 완벽히 반영되지 않습니다.
5. Strategy Health=0 이후 shadow trade 기반 자동복귀가 없습니다.
6. raw L2 recorder/replay backtester가 없습니다.
7. 실제 전략 수익성은 아직 장기 OOS 검증되지 않았습니다.

따라서 V3 runtime hardening 성공은 **시장데이터 연결 신뢰도 개선**을 의미하며, 전략의 수익성을 보증하는 의미가 아닙니다.

## 6. 실사용자가 정상 상태를 확인하는 기준

자동매매 시작 후:

- 수초 내 `Trade WS n/n`이 연결 상태가 되어야 함
- `마지막 체결`은 활발한 시장에서 계속 낮은 초 단위로 갱신되어야 함
- `추적 x/y`의 x가 빠르게 증가해야 함
- 기본 약 3분 동안 `워밍업 완료` 종목 수가 증가해야 함
- 후보가 생기면 후보명 옆에 현재가와 Q 점수가 표시되어야 함
- 후보가 아직 없어도 KRW-BTC 차트가 움직여야 함

5분 이상 후보가 없으면서 추적/워밍업 수가 비정상적으로 낮거나 Trade WS가 연결되지 않았다면 전략 대기가 아니라 런타임 데이터 문제로 판단합니다.
