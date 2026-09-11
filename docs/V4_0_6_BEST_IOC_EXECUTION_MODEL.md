# V4.0.6 — Upbit Best+IOC 실행 모델 정합화

## 배경

V4.0.5 이후 deterministic replay의 다음 단계인 execution simulator를 설계하면서 현재 Upbit 주문 사양과 live pre-trade 유동성 모델을 다시 대조했다.

JunhyunBank의 일반 매수/매도는 다음 주문을 실제로 제출한다.

```text
ord_type = best
time_in_force = ioc
```

Upbit 공식 주문 문서에서 `best`(최유리 지정가)는 **주문 접수 시점의 상대 방향 최우선 호가와 같은 가격을 지정가로 사용하는 주문**이다. `IOC`는 그 가격조건으로 즉시 체결 가능한 수량만 체결하고 나머지를 취소한다.

공식 문서:

- https://docs.upbit.com/kr/reference/new-order
- https://docs.upbit.com/kr/v1.5.9/reference/%EC%A3%BC%EB%AC%B8%ED%95%98%EA%B8%B0

따라서 현재 스냅샷을 기준으로 한 `best + IOC` 주문은 더 불리한 2호가, 3호가로 **시장가처럼 depth walk하지 않는다.**

## 발견한 불일치

기존 `engine.TradingEngine`의 pre-trade 모델은 매수/매도 예상 체결을 여러 호가 레벨에 걸쳐 순차적으로 소진하는 방식으로 계산했다.

그 결과:

1. 실제 best+IOC가 접근할 수 없는 더 불리한 가격 레벨을 체결 가능 유동성으로 포함할 수 있었다.
2. 실제 주문에서는 발생하지 않는 multi-level price slippage를 비용에 더했다.
3. 실제 주문은 최우선호가 잔량이 부족하면 remainder가 취소되는데, 기존 sizing은 deeper L2까지 채워질 것으로 예상할 수 있었다.
4. 따라서 의도한 position size와 실제 partial fill 가능성, 비용 gate의 의미가 어긋날 수 있었다.

이 문제는 거래소가 주문금액보다 더 많이 체결하는 종류의 과다매수 위험은 아니지만, **실제 체결비율과 포지션 크기, 진입/청산 유동성 가정이 틀린 문제**이므로 execution/OOS 연구 전에 바로잡아야 한다.

## V4.0.6 변경

### 1. 실제 실행 모델을 별도 순수 모듈로 고정

`src/junhyunbank/execution_model.py`에 current snapshot 기준 Best+IOC 모델을 둔다.

매수 즉시 체결 가능 KRW:

```text
best_ask_price × best_ask_size
```

매도 즉시 체결 가능 KRW:

```text
best_bid_price × best_bid_size
```

신규 포지션 sizing에 쓰는 보수적 round-trip capacity:

```text
min(best-ask capacity, best-bid capacity)
```

이는 임의의 고정 KRW cap이 아니다. **현재 관측된 최우선호가 양방향 유동성으로부터 직접 계산되는 동적 cap**이다.

### 2. 초과 주문금액은 slippage가 아니라 unfilled capacity

스냅샷에서 best+IOC의 worse-level price slippage는 0으로 모델링한다. 요청량이 최우선호가 잔량보다 크면 그 초과분을 더 나쁜 가격에 체결시킨다고 가정하지 않고 즉시 체결 가능 금액에서 제외한다.

실제 주문 접수까지 호가가 변할 수 있으므로 실제 IOC partial/no fill은 여전히 발생할 수 있다. 이 부분은 V4 durable order intent + identifier reconciliation + partial-fill accounting이 처리한다.

### 3. production entry path만 명시적으로 정합화

`main.py`는 `live_engine.TradingEngine`을 사용한다. 이 클래스는 기존 `runtime_engine.TradingEngine`의 market discovery, WebSocket, Private reconciliation, 주문 state machine을 그대로 상속하고 pre-trade liquidity helper만 Best+IOC 모델로 override한다.

이번 패치에서 다음은 바꾸지 않는다.

- Upbit 주문 타입 자체 (`best + IOC` 유지)
- JH-MicroFlow entry/exit threshold
- ExpectedMove 계산
- Strategy Health
- durable order intent
- managed quantity
- Private WS → REST reconciliation
- updater

## 비용 해석

`evaluate_entry()`의 1차 비용 gate는 기존대로 계정 수수료와 보수적 spread factor를 사용한다. V4.0.6은 실제 best+IOC가 갈 수 없는 deeper level price impact를 추가 비용으로 만들어내지 않는다.

따라서 일부 경우 기존의 **가상 depth-walk slippage 때문에만** 2차 단계에서 차단되던 거래가 이제 차단되지 않을 수 있다. 이는 threshold 완화가 아니라 주문 사양과 맞지 않던 체결 모델을 제거한 결과다. 동시에 주문금액은 실제 current top-level 양방향 capacity를 넘지 않도록 더 보수적으로 제한된다.

## 테스트 불변조건

회귀테스트는 다음을 고정한다.

- BUY 요청이 best ask capacity보다 커도 deeper ask는 사용하지 않는다.
- SELL 요청이 best bid capacity보다 커도 deeper bid는 사용하지 않는다.
- ExpectedMove가 매우 커져도 best+IOC capacity가 deeper L2 때문에 늘어나지 않는다.
- deeper level size를 임의로 크게 늘려도 current Best+IOC capacity는 변하지 않는다.
- 실제 REST 주문 payload는 여전히 `ord_type=best`, `time_in_force=ioc`이다.
- BUY는 KRW `price`, SELL은 coin `volume` 형식을 유지한다.

## 다음 단계

V4.0.7 execution simulator는 이 모듈을 current live execution semantics의 정본으로 사용한다.

현재 live Best+IOC를 시뮬레이션할 때 generic depth walk를 사용하지 않는다. 대신:

1. signal/decision 시점 기록
2. configurable order latency 적용
3. latency 후 첫 유효 orderbook에서 best opposing price/size 확인
4. 해당 best-price capacity까지 full/partial/no fill 결정
5. IOC remainder cancel
6. fee/spread/fill ratio 기록

을 재현한다.

여러 호가를 걷는 generic depth-walk simulator는 향후 시장가 또는 다른 limit 가격 전략을 연구할 때 별도 가정으로만 사용할 수 있으며, 현재 JunhyunBank live Best+IOC의 체결모델로 사용하면 안 된다.
