# JH-MicroFlow 전략 명세

대상 버전: **V2 / 2.0.0**

이 문서는 두 가지를 분리해 설명합니다.

1. **전략 의도** — 왜 이런 구조를 택했는가
2. **V2 실제 구현** — 현재 코드가 실제로 어떤 규칙을 실행하는가

수익성이 검증되었다는 의미가 아닙니다. 전략 수익성 검증 계획은 `VALIDATION_AND_ROADMAP.md`를 봅니다.

---

## 1. 전략 철학

사용자가 원하는 핵심은 다음과 같습니다.

- 사람이 하기 어려운 24시간 전 종목 실시간 감시를 프로그램의 장점으로 활용
- 단타를 중심으로 함
- 거래횟수 제한 없음
- 1회 고정 주문금액 상한 없음
- 프로그램이 시장상태와 신호를 보고 스스로 거래규모 결정

따라서 JH-MicroFlow는 전통적인 5분봉 RSI/이동평균 교차보다 **체결 흐름과 호가 구조의 단기 변화**를 중심으로 설계했습니다.

목표는 단순히 많이 오른 코인을 따라 사는 것이 아니라:

```text
평소보다 거래활동 증가
        +
공격적 매수체결 증가
        +
호가 구조가 상승방향으로 개선
        +
가격 모멘텀 확인
        +
수수료/스프레드/슬리피지를 넘을 여유
        ↓
초기 모멘텀 진입 후보
```

를 찾는 것입니다.

---

## 2. 왜 절대값 대신 상대값인가

`BuyPressure > 0.18`, `30초 거래대금 > 10억원` 같은 모든 코인 공통 절대 임계값은 사용하지 않는 방향입니다.

BTC와 작은 알트코인은 정상적인 체결량, spread, volatility가 크게 다릅니다.

V2는 대부분의 feature를 **해당 종목 자신의 rolling history percentile**로 정규화합니다.

예:

```text
현재 5초 거래대금이
그 종목 최근 기록의 97 percentile
→ ActivityQ ≈ 0.97
```

이 방식은 서로 가격대/유동성이 다른 시장을 같은 0~1 스케일로 비교할 수 있게 합니다.

---

## 3. 시간축

### SecondFrame

trade event는 1초 frame으로 집계합니다.

```text
second
open / high / low / close
bid_value
ask_value
trade_value
trades
```

### 체결 없는 초

V2에서 매우 중요한 보정입니다.

예를 들어 12:00:01 다음 체결이 12:00:05에 들어왔다면 02/03/04초를 누락시키지 않고 마지막 가격의 0거래 frame으로 채웁니다.

따라서:

```text
10초 window = 체결이 있었던 10개 초
```

가 아니라

```text
10초 window = 실제 벽시계 10초
```

입니다.

오래 거래가 없어서 gap이 `baseline_seconds`보다 크면 기존 baseline과 book history를 초기화합니다.

역순/과거 trade event는 현재 frame을 훼손하지 않게 버립니다.

---

## 4. V2 기본 StrategyConfig

현재 `config.py` 기본값의 의미:

| 설정 | 기본값 | 의미 |
|---|---:|---|
| `baseline_seconds` | 1800 | rolling history 목표 길이 |
| `min_warmup_seconds` | 180 | 최소 전략 warmup |
| `deep_candidate_count` | 24 | orderbook 정밀분석 후보 수 |
| `candidate_refresh_seconds` | 3 | Hot 후보 재선정 |
| `market_refresh_seconds` | 300 | market universe 재조회 |
| `evaluation_seconds` | 1 | 전략 평가 cadence |
| `orderbook_depth` | 5 | 분석 호가쌍 수 |
| `activity_window_seconds` | 5 | Activity 현재창 |
| `aggression_window_seconds` | 5 | Aggression 현재창 |
| `momentum_window_seconds` | 10 | short momentum |
| `long_momentum_window_seconds` | 30 | long momentum |
| `expected_move_window_seconds` | 30 | expected move 기준 horizon |
| `ignition_quality` | 0.82 | IGNITION quality 기준 |
| `pullback_quality` | 0.76 | PULLBACK quality 기준 |
| `health_lookback` | 50 | Strategy Health 결과 lookback |

이 숫자는 현재 V2 구현 초기값이지 장기 실증 최적값으로 검증된 수치가 아닙니다.

---

## 5. Activity factor

현재 5초 거래대금 합계와 과거 rolling 5초 거래대금 합계들을 비교합니다.

개념:

```text
ActivityQ = percentile_rank(
    historical rolling 5s trade value,
    current rolling 5s trade value
)
```

값 범위는 0~1입니다.

높을수록 “이 종목 자체의 평소에 비해 지금 거래활동이 이례적으로 강함”을 의미합니다.

---

## 6. Aggression factor

Upbit trade event의 `ask_bid`를 사용해 공격적 매수/매도를 구분합니다.

현재창:

```text
Aggression =
(BID 체결대금 - ASK 체결대금)
--------------------------------
(BID 체결대금 + ASK 체결대금)
```

현재 구현은 최근 `aggression_window_seconds` 합산값으로 계산합니다.

과거 1초 frame의 aggression 분포에 대한 percentile을 `AggressionQ`로 사용합니다.

Aggression이 0 이하이면 AggressionQ를 0으로 둡니다. 즉 매도우세 체결이 다른 강한 factor에 의해 쉽게 상쇄되지 않게 설계했습니다.

---

## 7. Book Pressure factor

현재 orderbook의 상위 `orderbook_depth` 레벨을 사용합니다.

### Weighted depth imbalance

가까운 호가일수록 높은 weight를 둡니다.

V2 weight:

```text
weight(level i) = 1 / sqrt(i)
```

1-based 개념이며 코드에서는 index+1입니다.

각 측 depth:

```text
BidDepth = Σ bid_price × bid_size × weight
AskDepth = Σ ask_price × ask_size × weight
```

imbalance:

```text
OBI = (BidDepth - AskDepth) / (BidDepth + AskDepth)
```

### Spread

```text
mid = (bid1 + ask1) / 2
spread_pct = (ask1 - bid1) / mid
```

### Microprice bias

```text
microprice =
(ask1 × bid_size1 + bid1 × ask_size1)
-------------------------------------
(bid_size1 + ask_size1)

micro_bias = microprice / mid - 1
```

매수호가 잔량이 상대적으로 강하면 microprice가 ask 쪽으로 이동합니다.

### BookQ

현재 구현은 다음 3개 percentile의 기하평균입니다.

- 현재 imbalance percentile
- 현재 micro_bias percentile
- 약 3초간 imbalance 변화(delta) percentile

```text
BookQ = (IQ × MQ × DQ)^(1/3)
```

각 값이 양수 방향일 때만 의미를 주며 음수면 0 취급합니다.

> 주의: 이것은 “정교한 이벤트 레벨 OFI”의 완전 구현이 아닙니다. 현재는 depth/microprice/delta 기반 Book Pressure proxy입니다.

---

## 8. Momentum factor

현재 short window와 long window 수익률이 모두 양수일 때만 momentum을 인정합니다.

```text
short_return = price_now / price_10s_ago - 1
long_return  = price_now / price_30s_ago - 1
```

현재 short return이 과거 short return 분포에서 어느 percentile인지 계산합니다.

```text
MomentumQ = percentile_rank(history short returns, current short return)
```

short 또는 long momentum이 0 이하이면 MomentumQ=0입니다.

---

## 9. Signal Quality

네 factor를 단순 가중합하지 않고 기하평균합니다.

```text
Quality =
(ActivityQ × AggressionQ × BookQ × MomentumQ)^(1/4)
```

이유:

단순 평균은 하나의 극단적으로 높은 feature가 나머지 나쁜 상태를 덮을 수 있습니다.

기하평균은 네 축 중 하나가 거의 0이면 전체 quality도 크게 떨어집니다.

---

## 10. HotScore

전체 KRW market을 빠르게 순위화하기 위해 orderbook factor를 제외한 세 요소를 사용합니다.

```text
HotScore =
(ActivityQ × AggressionQ × MomentumQ)^(1/3) × 100
```

trade가 최근 freshness limit보다 오래된 market은 HotScore=0입니다.

HotScore 상위 market을 orderbook deep analysis로 승격합니다.

---

## 11. Market Regime

KRW 시장 전체의 60초 수익률 breadth와 median, BTC 60초 수익률을 이용합니다.

현재 반환 상태와 multiplier:

| 상태 | multiplier |
|---|---:|
| `PANIC` | 0.00 |
| `RISK_OFF` | 0.40 |
| `NEUTRAL` | 0.75 |
| `RISK_ON` | 1.00 |
| `EUPHORIA` | 0.65 |

데이터가 충분하지 않으면 `NEUTRAL, 0.70` fallback을 사용합니다.

핵심 의도:

- PANIC에서는 신규매수 완전 중지
- RISK_OFF에서는 노출 축소
- 정상 상승환경은 최대 multiplier
- 지나친 시장 광기(EUPHORIA)에서도 추격위험 때문에 노출을 다시 낮춤

현재 regime 수식은 heuristic이며 학습된 regime classifier가 아닙니다.

---

## 12. Expected Move

현재 구현은 해당 market의 최근 30초 절대수익률 분포를 이용합니다.

```text
expected_move = 70th percentile(
    recent |30-second returns|
)
```

최근 최대 약 900개 값을 사용합니다.

이 값은 “다음 30초 수익률 예측모델”이 아니라 **최근 시장에서 통상 발생 가능한 움직임 용량(capacity)의 proxy**입니다.

향후 실제 label 기반 conditional expected return 모델로 발전시킬 수 있습니다.

---

## 13. 거래비용 gate

전략은 신호가 좋아도 비용이 지나치면 진입하지 않습니다.

strategy 단계의 초기 cost proxy:

```text
cost = bid_fee + ask_fee + spread × 1.5
```

진입 필수조건:

```text
expected_move > cost × 2
```

engine 단계에서는 실제 orderbook을 이용해 buy/sell slippage까지 다시 계산합니다.

```text
actual_cost =
bid_fee + ask_fee + spread + buy_slippage + sell_slippage
```

다시:

```text
expected_move > actual_cost × 2
```

를 확인합니다.

즉 전략과 실행 두 단계에서 비용 gate가 있습니다.

---

## 14. IGNITION

PULLBACK 조건이 아니면 기본 entry kind는 IGNITION입니다.

의도:

- 아직 크게 확장되기 전
- 거래활동/매수체결/호가압력/가격이 동시에 활성화되는 초기 수급 burst 포착

현재 최소 quality 기본값:

```text
0.82
```

---

## 15. PULLBACK CONTINUATION

최근 약 60초에서 선행 상승 후 일부 되돌림이 발생하고 다시 단기 수익률이 양수로 돌아오는 상태를 PULLBACK으로 분류합니다.

현재 개념적 조건:

```text
advance >= expected_move × 1.5

expected_move × 0.15
    <= drawdown from peak
    <= expected_move × 0.85

3초 return > 0
```

PULLBACK 기본 quality threshold는 0.76입니다.

의도는 최초 돌파를 놓친 뒤 무작정 추격하지 않고 건전한 되돌림 후 재가속을 노리는 것입니다.

---

## 16. 재진입 reset

포지션 청산 후 같은 market을 `_blocked_after_exit`에 넣습니다.

기존 신호 quality가:

```text
< 0.45
```

까지 떨어져야 block이 해제됩니다.

따라서 단순히 30초/5분 cooldown을 두지 않고 **기존 수급 이벤트가 끝났다가 새로운 이벤트가 생겨야 재진입**하도록 설계했습니다.

---

## 17. 추격매수 방지

현재 30초 return이 자신의 과거 분포 99 percentile 이상인데 BookQ가 0.80 미만이면:

```text
EXTENDED/EXHAUSTED
```

로 신규진입을 차단합니다.

의도:

가격만 이미 크게 뛰었는데 호가 품질은 약해진 종목을 뒤늦게 따라가지 않기 위함입니다.

---

## 18. 현재 자금배분 규칙

이 부분은 **설계안과 현재 구현을 꼭 구분**해야 합니다.

현재 V2:

```text
edge = (expected_move - cost) / expected_move

capital_fraction = clamp(
    edge × quality × health × regime_factor,
    0,
    1
)
```

engine:

```text
desired_amount = remaining_cash × capital_fraction
```

그리고 orderbook으로 계산한 liquidity capacity 및 실제 available cash와 min()합니다.

```text
actual_amount = min(
    desired_amount,
    liquidity_capacity,
    available_cash / (1 + bid_fee)
)
```

### 중요한 사실

- 고정 KRW 주문상한은 없음
- 하지만 `capital_fraction`이 최대 1이므로 신호/환경에 따라 가용자금의 매우 큰 비중을 단일 신호가 원할 수 있음
- 명시적인 portfolio VaR / correlation / Kelly uncertainty cap은 현재 없음

### 원래 발전 방향

이후 버전에서 유사 신호의 실현손익 분포를 이용한 **bootstrap damped Kelly**와 correlation penalty를 넣는 것이 계획된 개선 방향입니다.

---

## 19. Liquidity Capacity

현재 ask orderbook을 따라가며 expected edge의 일부 범위 안에서만 체결 가능한 호가를 합산합니다.

```text
edge_room = expected_move - bid_fee - ask_fee - spread
allowed_slippage = edge_room × 0.30
```

best ask 대비 허용 slippage를 넘어가기 전까지 ask depth의 원화가치를 합산합니다.

즉 “1회 최대 50만원” 같은 고정 숫자 대신:

> 현재 이 시장이 우리 주문을 얼마까지 받아줄 수 있는가

를 동적 주문용량으로 사용합니다.

engine은 별도로 buy/sell 평균체결가를 호가에서 시뮬레이션해 slippage를 재검증합니다.

---

## 20. Initial Emergency Risk

진입 순간 위험폭 proxy:

```text
initial_risk = max(
    round_trip_cost × 1.8,
    realized_30s_volatility × 1.25,
    spread × 3
)
```

중요한 구현 원칙:

**매수 후 변동성이 커져도 stop distance를 불리한 방향으로 다시 넓히지 않습니다.**

진입 후 `initial_risk_pct`를 SQLite에 저장해 포지션 생애 동안 기준으로 사용합니다.

청산:

```text
pnl <= -initial_risk_pct
→ SELL: Emergency Stop
```

이 Emergency Stop은 사용자 UI의 `긴급 정지` 버튼과 다른 개념입니다.

---

## 21. Hold Quality

보유 중에도 같은 네 factor 기하평균을 계산합니다.

```text
HoldQ =
(ActivityQ × AggressionQ × BookQ × MomentumQ)^(1/4)
```

약화 조건:

```text
HoldQ < 0.36
OR
(Aggression < 0 AND OBI < 0)
```

가 연속 2회 평가되면 SELL합니다.

평가 cadence 기본값이 1초이므로 대략 2회 연속 확인 개념입니다.

---

## 22. Adaptive Trailing

최고 유리움직임:

```text
MFE = peak_price / entry_price - 1
```

MFE가 왕복비용의 2.5배 이상이면 trailing을 활성화합니다.

현재 trailing distance:

```text
max(
    realized_30s_volatility × 0.45,
    round_trip_cost × 1.5,
    spread × 2
)
```

현재가가 peak에서 이 거리 이상 되돌아오면 SELL합니다.

고정 `+4% 익절`을 사용하지 않는 이유는 강한 추세의 상승 여지를 인위적으로 잘라내지 않기 위해서입니다.

---

## 23. Expected Horizon Exit

entry 당시 expected move와 최근 1초 typical move로 expected horizon을 추정합니다.

현재 제한:

```text
20s <= expected_horizon <= 600s
```

보유시간이:

```text
elapsed > expected_horizon × 1.5
```

이고 동시에:

```text
MFE < round_trip_cost × 1.5
AND HoldQ < 0.60
```

이면 “기대 시간 내 모멘텀 미발생”으로 청산합니다.

의도는 단타 신호가 실패했는데 단순히 본전이 올 때까지 장기 보유하는 것을 막기 위함입니다.

---

## 24. Strategy Health Governor

`strategy_outcomes`의 최근 normalized return을 사용합니다.

normalized return:

```text
net_return / initial_risk
```

현재 health는:

- 최근 결과 가중평균
- 승률
- 최근 20개 tail 성과

를 조합하는 heuristic입니다.

데이터가 없으면:

```text
health = 0.65
```

심각한 최근 악화 조건을 만족하면:

```text
health = 0
```

이며 신규진입은 중단됩니다.

### 현재 중요한 결함/한계

health=0이고 열린 관리 포지션도 없으면 새로운 실전 outcome이 생성되지 않습니다. 현재 shadow simulation이 없으므로 전략이 실제로 다시 좋아졌더라도 **자동으로 health가 회복될 데이터 경로가 부족합니다.**

향후 반드시 개선할 가치가 높은 부분입니다.

---

## 25. V2에 없는 것

다음은 전략 논의 중 언급됐지만 **현재 V2에 존재한다고 가정하면 안 됩니다.**

- 머신러닝 매수모델
- bootstrap Kelly
- Bayesian edge posterior
- correlation-aware portfolio optimizer
- raw L2 orderbook 장기저장
- historical orderbook replay
- shadow trading health recovery
- 엄밀한 queue-position execution simulator
- private WebSocket 기반 fill state machine
- true multi-level event OFI

---

## 26. 전략 변경 시 개발 규칙

전략을 개선할 때:

1. 단일 파라미터 최적수익을 찾는 방식 금지
2. 수수료와 slippage를 항상 포함
3. 미래정보를 참조하는 look-ahead 금지
4. 같은 시간구간이 train/test에 섞이는 leakage 방지
5. 여러 코인/시장상태에서 검증
6. 지연을 추가한 stress test
7. 파라미터 ±20~30%에서도 결과가 급붕괴하지 않는지 확인
8. 특정 하루/특정 코인 하나가 전체 수익을 만든 것은 아닌지 확인
9. 새 전략이 기존 사용자 보유자산 보호와 DRAINING semantics를 깨지 않는지 테스트
10. 이 문서를 실제 코드와 함께 갱신

---

## 27. 전략의 최종 목적

JH-MicroFlow의 목적은 “항상 거래”가 아닙니다.

```text
기회 없음 → 주문금액 0
좋은 기회 → 시장이 허용하는 수준으로 동적 투자
근거 소멸 → 빠르게 청산
강한 근거 지속 → 고정 익절 없이 추세 추종
전략 자체가 망가짐 → 노출 축소/중단
```

이 철학을 유지하면서 실제 데이터로 규칙을 반복 개선하는 것이 프로젝트의 장기 방향입니다.
