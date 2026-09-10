# Upbit 연동 규칙과 전제

기준 확인일: **2026-09-10**

Upbit API는 변경될 수 있으므로 주문/인증/Rate Limit 관련 코드를 수정하기 전에는 반드시 최신 공식 문서를 다시 확인합니다.

---

## 1. 기본 원칙

- REST base: `https://api.upbit.com`
- Public WebSocket: `wss://api.upbit.com/websocket/v1`
- Private WebSocket: `wss://api.upbit.com/websocket/v1/private`
- 현재 V2는 public trade/orderbook WebSocket만 사용
- private 주문/자산 stream은 아직 미사용

---

## 2. API Key 보안

Upbit API Key는 거래/잔고 조회에 필요한 권한만 사용합니다.

**출금 권한은 부여하지 않는 것이 프로젝트 원칙입니다.**

Upbit API Key는 등록한 허용 공인 IP에서 사용해야 하므로 실제 실행 환경의 public IP 설정을 확인합니다. 안정 운영에는 고정 public IP가 유리합니다.

JunhyunBank:

- Access Key / Secret Key를 source/database/log에 저장하지 않음
- Python `keyring`을 통해 OS credential storage 사용
- updater가 EXE를 교체해도 keyring 데이터는 유지

---

## 3. JWT 인증

현재 `upbit.py` 구현 원칙:

JWT algorithm:

```text
HS512
```

payload 기본:

```text
access_key
nonce = fresh UUID
```

query/body가 있으면 요청 파라미터를 query string으로 직렬화하고 SHA-512 hash:

```text
query_hash
query_hash_alg = SHA512
```

를 JWT payload에 포함합니다.

Secret Key는 base64 decode하지 않고 제공받은 문자열 그대로 signing key로 사용합니다.

private retry마다 **새 nonce/JWT**를 생성해야 합니다.

Authorization header:

```text
Bearer <JWT>
```

---

## 4. 현재 사용하는 REST API

### 계정

```text
GET /v1/accounts
```

용도:

- API Key 검증
- KRW available/locked
- coin balance/locked/avg_buy_price
- portfolio reconciliation

### market list

```text
GET /v1/market/all?is_details=true
```

용도:

- KRW market universe
- warning/caution market 제외

### selected ticker

```text
GET /v1/ticker?markets=KRW-BTC,...
```

용도:

- 앱 시작/portfolio 조회에서 WebSocket current price가 아직 없는 보유자산 가격 fallback

### order chance

```text
GET /v1/orders/chance?market=KRW-XXX
```

용도:

- 실제 계정 `bid_fee`
- `ask_fee`
- market minimum total

V2는 이 값을 cache합니다.

### order status

```text
GET /v1/order?uuid=...
```

용도:

- accepted order의 executed volume
- terminal state
- trades/fill price
- paid fee

향후 identifier 조회와 private myOrder stream을 포함한 durable reconciliation을 권장합니다.

---

## 5. 주문 방식

### 일반 매수

V2:

```text
POST /v1/orders
market = KRW-XXX
side = bid
ord_type = best
time_in_force = ioc
price = <KRW total>
identifier = junhyunbank-v2-<uuid>
```

`best + IOC` 매수에서는 KRW 주문총액을 `price`로 보냅니다.

### 일반 매도

```text
POST /v1/orders
market = KRW-XXX
side = ask
ord_type = best
time_in_force = ioc
volume = <coin quantity>
identifier = junhyunbank-v2-<uuid>
```

### Emergency exit fallback

`best + IOC` 매도가 전혀 체결되지 않았고 전략의 exit reason이 Emergency Stop인 경우 V2는:

```text
side = ask
ord_type = market
volume = ...
```

시장가 매도를 fallback으로 사용할 수 있습니다.

### 과거 V1 호환 method

`place_market_buy`, `place_market_sell` method가 `upbit.py`에 남아 있을 수 있지만 V2 일반 entry는 best+IOC가 주 방식입니다.

---

## 6. Order identifier

모든 주문에 고유 identifier를 부여합니다.

현재 prefix:

```text
junhyunbank-v2-
```

이 identifier는 duplicate prevention/reconciliation 확장에 중요합니다.

향후 주문 상태 머신에서는 **REST POST 전에 identifier를 SQLite에 먼저 저장**하는 방식을 권장합니다.

---

## 7. Public WebSocket trade

V2 전체시장 scanner의 핵심입니다.

구독 type:

```text
trade
```

`is_only_realtime = true`

전략에서 중요하게 쓰는 필드:

- `code`
- `trade_price`
- `trade_volume`
- `ask_bid`
- `timestamp`

`ask_bid == BID`를 공격적 매수 체결, `ASK`를 공격적 매도 체결로 집계합니다.

WebSocket event는 수신 순서가 항상 완벽하다고 가정하지 않으며 V2 strategy는 현재 frame보다 과거 second event를 버립니다.

---

## 8. Public WebSocket orderbook

구독 type:

```text
orderbook
```

V2는 Hot 후보 + managed positions에 대해 사용합니다.

호가레벨 지정 예:

```text
KRW-BTC.5
```

여기서 `.5`는 요청 orderbook depth이며 응답 `code`는 원래 market code `KRW-BTC`로 처리합니다.

전략이 사용하는 필드:

- `orderbook_units[].bid_price`
- `bid_size`
- `ask_price`
- `ask_size`
- `timestamp`

현재 default depth는 5입니다.

---

## 9. Private WebSocket — 향후 권장

현재 미구현이지만 다음 버전에서 높은 우선순위를 가집니다.

### `myOrder`

목적:

- order accepted/fill/cancel event
- partial fill
- fee / trade state
- polling latency 감소

### `myAsset`

목적:

- balance/locked 변화
- 수동 주문/외부 변화 감지
- managed position reconciliation

권장 구조:

```text
private WS = 빠른 이벤트 채널
REST = 최종 reconciliation / reconnect recovery
```

둘 중 하나만 절대적인 유일 truth로 두기보다 event + REST 확인 구조가 안전합니다.

---

## 10. Rate Limit

2026-09-10 기준 공식 사양을 다시 확인하고 구현해야 합니다.

최근 공식 변경으로 order group은 초당 최대 **12회** 수준입니다. quotation 계열은 일반적으로 그룹별 초당 10회, Exchange API 기본 그룹은 더 높은 한도가 있지만 endpoint group별 정책이 다를 수 있습니다.

현재 V2 `upbit.py`는 모든 private REST를 하나의 lock으로 serialize하고 약:

```text
1 / 11 second
```

최소 간격을 둡니다.

즉 주문 group 12/s보다 보수적으로 private 전체를 제한합니다.

장점:

- 단순하고 안전

단점:

- endpoint group별 실제 허용량을 활용하지 못함
- balance/order chance/order status polling이 주문과 같은 global queue를 공유

향후 `Remaining-Req`를 parsing하여 endpoint group별 token bucket/circuit breaker를 구현할 수 있습니다.

HTTP:

- `429`: retry/backoff
- `418`: temporary IP block으로 간주해 error

---

## 11. Market warning/caution

시장 universe를 만들 때 warning/caution 종목은 신규진입에서 제외합니다.

V2는 `market_event.warning`, `market_event.caution` 구조와 legacy `market_warning` fallback을 확인합니다.

관리 중인 기존 포지션은 신규 후보 ranking과 별개로 orderbook deep monitoring에 강제로 포함합니다.

시장경보가 새로 생겼다고 기존 managed position을 즉시 시장가 강제청산하는 규칙은 현재 없습니다. 청산정책을 변경할 경우 별도 전략/안전정책으로 명시해야 합니다.

---

## 12. Minimum order

KRW market의 거래소 최소주문 기준을 주문 가능정보에서 읽고 `SafetyConfig.min_order_krw` fallback과 함께 사용합니다.

관리 포지션의 잔여가치가 minimum 아래가 되면 현재 V2는 dust로 보고 자동관리 marker를 해제할 수 있습니다.

이 경우 코인 잔량은 계정에 남을 수 있습니다.

---

## 13. Upbit API 변경 시 체크리스트

API 관련 코드를 수정할 때 다음을 확인합니다.

1. 공식 문서의 endpoint/path
2. request body field semantics
3. `best` + IOC/FOK/Post Only 지원 상태
4. minimum order
5. fee response schema
6. order response state names
7. partial fill fields
8. WebSocket field names
9. orderbook depth syntax
10. Rate Limit group
11. market warning schema
12. JWT query hash 직렬화 규칙
13. API Key IP/권한 정책
14. CHANGELOG의 최근 API 변경

문서와 코드가 다르면 최신 공식 Upbit 문서를 기준으로 코드/이 문서를 함께 수정합니다.
