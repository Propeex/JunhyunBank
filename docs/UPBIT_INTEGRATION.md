# Upbit 연동 규칙과 전제

기준 재확인일: **2026-09-11 / V4.0.6**

Upbit API는 변경될 수 있으므로 주문·인증·Rate Limit·WebSocket 코드를 수정하기 전에는 최신 공식 문서를 다시 확인한다. 코드와 이 문서가 충돌하면 최신 공식 Upbit 사양을 확인한 뒤 둘을 함께 수정한다.

## 1. 연결 endpoint

```text
REST              https://api.upbit.com
Public WebSocket  wss://api.upbit.com/websocket/v1
Private WebSocket wss://api.upbit.com/websocket/v1/private
```

V4는 Public trade/orderbook WebSocket과 authenticated Private `myOrder`/`myAsset` WebSocket을 모두 사용한다.

## 2. API Key / 인증 보안

- 출금 API를 구현하지 않는다.
- 거래/잔고조회에 필요한 권한만 사용한다.
- Access Key / Secret Key / JWT를 source, SQLite, 로그에 저장하지 않는다.
- API Key는 OS keyring에 저장한다.
- 실제 사용 PC의 허용 공인 IP 정책을 확인한다.
- Private WS JWT는 연결마다 새로 만든다.

REST private JWT 기본 payload:

```text
access_key
nonce = fresh UUID
```

query/body가 있으면 직렬화한 query string의 SHA-512를 넣는다.

```text
query_hash
query_hash_alg = SHA512
```

Secret Key 문자열은 임의로 base64 decode하지 않는다.

## 3. 주요 REST API

### 계정

```text
GET /v1/accounts
```

KRW available/locked, coin balance/locked/avg_buy_price, portfolio reconciliation에 사용한다.

### 시장 목록

```text
GET /v1/market/all?is_details=true
```

KRW universe와 `market_event` warning/caution을 확인한다. 상세 필드가 없는 호환 응답에는 legacy `isDetails=true`를 한 번 재시도할 수 있다.

### ticker fallback

```text
GET /v1/ticker?markets=KRW-BTC,...
```

portfolio 조회 시 아직 Public WS current price가 없는 보유자산의 가격 fallback에 사용한다.

### 주문 가능 정보

```text
GET /v1/orders/chance?market=KRW-XXX
```

- 실제 계정 `bid_fee`
- `ask_fee`
- bid/ask minimum total

을 읽고 cache한다.

### 개별 주문 조회

```text
GET /v1/order?uuid=...
GET /v1/order?identifier=...
```

V4 durable reconciliation의 회계 정본이다. accepted/ambiguous 주문의 state, executed volume, trades, fee를 조회한다.

## 4. 일반 주문: Best + IOC

현재 공식 주문 문서:

- https://docs.upbit.com/kr/reference/new-order
- https://docs.upbit.com/kr/v1.5.9/reference/%EC%A3%BC%EB%AC%B8%ED%95%98%EA%B8%B0

### 매수

```text
POST /v1/orders
market        = KRW-XXX
side          = bid
ord_type      = best
time_in_force = ioc
price         = <KRW quote amount>
identifier    = junhyunbank-<uuid>
```

### 매도

```text
POST /v1/orders
market        = KRW-XXX
side          = ask
ord_type      = best
time_in_force = ioc
volume        = <coin quantity>
identifier    = junhyunbank-<uuid>
```

### 매우 중요한 Best + IOC 의미

Upbit의 `best`(최유리 지정가)는 **주문 접수 시점 상대 방향의 최우선 호가와 같은 가격을 주문 가격으로 사용하는 방식**이다.

`ioc`는 해당 가격조건에서 **즉시 체결 가능한 수량만 체결하고 나머지를 취소**한다.

따라서 현재 snapshot 기준:

```text
BUY immediate capacity  = best ask price × best ask size
SELL immediate capacity = best bid price × best bid size
```

Best+IOC는 현재 최우선호가보다 더 불리한 2호가, 3호가를 시장가처럼 순서대로 걷지 않는다. 요청량이 최우선호가 잔량보다 크면 그 초과분은 worse-price slippage로 채우는 것이 아니라 **미체결 후 취소될 수 있는 remainder**다.

V4.0.6부터 live pre-trade model도 이 의미를 따른다. 현재 신규 포지션의 보수적 유동성 capacity는 최우선 ask와 bid 양쪽의 즉시 체결 가능 notional 중 작은 값으로 제한한다. 이는 임의 고정 KRW cap이 아니라 현재 호가에서 직접 계산되는 동적 liquidity cap이다.

호가는 주문 전송/접수 사이에도 변할 수 있으므로 실제 partial/no fill 가능성은 사라지지 않는다. 실제 결과는 identifier REST reconciliation과 partial-fill accounting으로 확정한다.

## 5. Emergency Stop 매도 fallback

일반 청산은 Best+IOC다. Emergency Stop 청산에서 Best+IOC가 **terminal이며 executed volume이 0**으로 확인된 경우에만 시장가 매도를 fallback으로 사용할 수 있다.

```text
side     = ask
ord_type = market
volume   = <remaining managed quantity>
```

부분체결 Best+IOC를 임의로 전량체결로 간주하지 않는다. durable managed quantity가 남아 있으면 다음 reconciliation/evaluation에서 계속 관리한다.

## 6. Order identifier / durable state

모든 JunhyunBank 실주문은 unique identifier를 가진다.

```text
junhyunbank-<uuid>
```

V4는 POST 전 identifier와 주문 의도를 SQLite `order_intents`에 먼저 저장한다.

- POST timeout/5xx/응답 유실에서 같은 주문을 재전송하지 않는다.
- identifier로 결과를 조회한다.
- terminal 확인 전 같은 시장 중복 주문을 막는다.
- 재시작 뒤 pending intent를 다시 reconciliation한다.
- partial fill은 실제 trades/fee만 원자적으로 반영한다.

## 7. Public WebSocket — trade

구독 type:

```text
trade
```

전략 핵심 필드:

- `code`
- `trade_price`
- `trade_volume`
- `ask_bid`
- `trade_timestamp` / `timestamp`
- `sequential_id` 가능 시 recorder 저장

`ask_bid == BID`는 공격적 매수 체결, `ASK`는 공격적 매도 체결로 집계한다. 역순 exchange-second event가 현재 frame을 훼손하지 않게 방어한다.

## 8. Public WebSocket — orderbook

구독 type:

```text
orderbook
```

live runtime은 Hot 후보와 managed positions를 deep subscription에 포함한다. V3 이후 후보 변경 때 socket 전체를 매번 끊지 않고 subscription set을 갱신한다.

중요 필드:

- `orderbook_units[].bid_price`
- `bid_size`
- `ask_price`
- `ask_size`
- `timestamp`

전략은 여러 L2 level을 imbalance/microprice/liquidity 연구에 사용한다. 그러나 **현재 live Best+IOC의 즉시 체결 capacity는 최우선 level만 사용한다.** Deeper L2는 Best+IOC가 worse price로 walk할 수 있다는 뜻이 아니다.

V4.0.4 recorder는 향후 execution research를 위해 더 깊은 L2를 보존할 수 있다.

## 9. Private WebSocket — myOrder / myAsset

V4.0.2부터:

```text
wss://api.upbit.com/websocket/v1/private
Authorization: Bearer <fresh JWT>
```

한 authenticated 연결에서 `myOrder` + `myAsset`을 구독한다.

### myOrder

- JunhyunBank identifier 주문 상태 변화를 빠르게 감지.
- pending REST reconciliation을 즉시 깨우는 신호.
- WebSocket event 자체로 최종 체결회계를 확정하지 않음.

### myAsset

- balance/locked 변화 감지.
- 계정 변화가 있음을 알리는 보조 신호.
- 계정 총잔고를 JunhyunBank managed quantity로 추정하는 데 사용하지 않음.

원칙:

```text
Private WS = 빠른 wake-up/event signal
identifier REST = accounting source of truth
```

Private stream은 장시간 이벤트가 없어도 정상일 수 있으므로 ping/reconnect를 유지한다.

## 10. Market warning/caution

시장 universe에서는 현재 `market_event.warning`과 `market_event.caution` 구조를 명시적인 bool/string 값으로 해석한다. 해석 불가능한 alert schema는 신규진입에서 fail-closed 한다.

경보가 생긴 종목은 신규진입에서 제외하지만 이미 JunhyunBank가 관리 중인 포지션은 감시/전략 청산을 계속한다.

## 11. Minimum order / dust

KRW market minimum은 `/v1/orders/chance` 응답을 우선하고 `SafetyConfig.min_order_krw`를 fallback으로 사용한다.

관리 잔여수량 가치가 minimum 아래면 전체 계정잔고로 보충하거나 사용자 수량을 섞지 않는다. dust 처리 UX는 별도 개선 대상이다.

## 12. Rate Limit

Rate Limit은 전략상의 임의 거래횟수 제한과 다르다. 거래소 운영 제약으로 항상 준수한다.

endpoint group별 허용량과 정책은 변경될 수 있으므로 숫자를 코드 변경의 근거로 삼기 전에 최신 공식 문서를 확인한다. 현재 client는 private REST를 직렬화/보수적 간격으로 제한하고 429/backoff 경로를 가진다.

- `429`: rate-limit response, retry/backoff 대상.
- `418`: temporary block 성격의 오류로 취급.

향후 endpoint group별 circuit breaker/token bucket으로 분리할 수 있다.

## 13. Recorder / replay와 Upbit 연동

### V4.0.3 Forward Edge

Public REST/WS만 사용하고 주문하지 않는다. 후보 시점 entry ask와 미래 bid를 연결한다.

### V4.0.4 Raw Recorder

Public trade + 후보 L2를 raw JSONL/GZIP으로 저장한다. API Key를 읽지 않으며 `orders_submitted=0` metadata를 남긴다.

### V4.0.5 Deterministic Replay

network를 사용하지 않는다. recorder의 exchange/local receive timestamp로 현재 전략을 offline 재생한다.

### V4.0.6 이후 Execution Simulation 원칙

현재 live order semantics를 연구할 때 Best+IOC를 generic depth-walk market order로 시뮬레이션하면 안 된다.

latency 후 주문 접수 시점 book을 선택하고 그 시점의 best opposing price/available size 안에서 full/partial/no-fill을 결정하며 remainder는 IOC cancel로 처리해야 한다.

## 14. API 변경 시 체크리스트

1. endpoint/path와 version 문서.
2. request body field semantics.
3. `best`의 가격 결정 방식.
4. IOC/FOK/Post Only 조건.
5. 매수 `price`와 매도 `volume` 의미.
6. minimum order.
7. fee schema.
8. order state / executed volume / trades / paid fee.
9. identifier 조회 지원.
10. Private `myOrder`/`myAsset` schema.
11. Public WebSocket field/format/depth syntax.
12. Rate Limit group.
13. market warning/caution schema.
14. JWT query hash 직렬화.
15. API Key IP/권한 정책.
16. 최근 Upbit changelog.

특히 주문 타입을 바꾸거나 execution simulator를 수정할 때는 **실제 live REST payload와 simulation semantics를 한 테스트에서 같이 고정**해야 한다.
