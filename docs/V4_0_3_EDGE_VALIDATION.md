# V4.0.3 Read-only Forward Edge Validation

V4.0.3의 목적은 **거래 횟수를 늘리기 위해 전략 임계값을 추측으로 낮추는 대신, 현재 JH-MicroFlow 가설이 실제 미래 가격에서 어떤 조건부 edge를 갖는지 측정할 수 있는 안전한 기반을 만드는 것**이다.

이 변경은 실거래 주문 경로를 수정하지 않는다. `scripts/validate_public_edge.py`는 API Key를 읽지 않고 Public REST/WebSocket만 사용하며 주문을 제출하지 않는다.

## 왜 필요한가

현재 `ExpectedMove`는 최근 30초 절대수익률 분포의 70% quantile 계열이다. 즉 현재 신호 이후 상승할 조건부 기대수익이 아니라 최근 시장의 움직임 크기 proxy다. 따라서 `ExpectedMove > 거래비용`이라는 논리만으로 수익성이 검증됐다고 볼 수 없다.

V4.0.3은 후보 평가 시점의 실제 top-of-book과 미래 top-of-book을 연결해 다음을 측정한다.

- 현재 BUY 신호의 forward net return
- 전체 후보 대비 BUY 신호의 차별성
- Hot 후보와 결과를 보지 않고 순환 선택한 비후보 대조군의 차이
- ExpectedMove와 실제 미래 절대 움직임의 calibration
- 시간순 train/holdout 구간의 차이
- label 누락률과 WebSocket 품질

## 수집 경로

1. `/v1/market/all`에서 현재 KRW 시장을 읽고 기존 경보 해석 규칙으로 위험/미확인 시장을 제외한다.
2. 전체 안전 KRW 시장의 public `trade` WebSocket을 수신해 실제 `MicroFlowStrategy`를 워밍업한다.
3. LIVE와 같은 fresh actionable 선택기로 고른 후보, 미래 결과와 무관하게 순환 선택한 대조군, 이미 만들어진 미완료 label 종목에 대해 public `orderbook`을 구독한다.
4. 후보·대조군 평가 시점에 `sample_role`, 특징값, regime, 진입판단, bid/ask를 기록한다.
5. 지정한 horizon 이후의 **future bid/ask**로 label을 만든다.

기본 horizon은 30/60/120/300초다.

## 실행 가능한 가격 기준 label

롱 진입을 가정할 때 label 순수익은 mid price가 아니라 다음 식을 사용한다.

```text
net_return = future_bid × (1 - sell_fee)
             -------------------------------- - 1
             entry_ask × (1 + buy_fee)
```

따라서 top-of-book spread와 양쪽 가정 수수료는 자동으로 비용에 포함된다.

단, 이 label은 **size-free**다. 실제 주문금액에 따른 여러 호가단계 depth walk, partial fill, queue position, 전송/서버 지연은 아직 포함하지 않는다. 그러므로 이 결과를 실체결 수익률로 부르지 않는다.

## 늦은 label 금지

네트워크 장애나 subscription cap 때문에 정확한 horizon 시점의 호가를 받지 못했을 때, 30초 label에 40초 뒤 가격을 넣는 식의 왜곡을 허용하지 않는다.

- 기본 허용 label 지연: 3초
- `due <= quote_time <= due + max_label_delay`일 때만 정상 label
- 허용창을 넘기면 해당 horizon은 `status=missed`로 기록
- summary는 labeled/missed/pending을 별도로 집계

label completion rate가 낮은 실행은 전략 판단보다 데이터 품질 문제를 먼저 해결해야 한다.

## live 엔진과 맞춘 데이터 안전장치

검증기가 실제 엔진보다 느슨해 false BUY를 만들지 않도록 다음을 맞춘다.

- trade/orderbook 중 하나라도 3초 이상 stale이면 BUY로 기록하지 않음
- LIVE와 동일하게 stale 상위 후보를 먼저 제거하고 top-N 밖 fresh 시장을 보충하며 deep residency/switch margin을 적용
- deep 분석에서 빠졌다 다시 들어온 종목은 이전 orderbook history와 cached quote를 폐기
- 구독 종목 합집합이 그대로여도 control↔candidate 역할이 바뀌면 이전 book, cached quote, sampling timer를 폐기해 대조군 관측으로 후보를 사전 워밍업하지 않음
- trade/orderbook WebSocket 재연결 시 영향을 받은 연속 history를 초기화
- 전략이 거부한 역순·중복 orderbook으로 label quote를 갱신하지 않음
- 이미 생성된 미완료 label 종목을 새 후보보다 orderbook subscription에서 우선 보호
- 현재 `TradingEngine._is_warning_market()` 규칙으로 시장 경보 해석

다만 계정 상태가 없으므로 다음 live gate는 의도적으로 모델링하지 않는다.

- 실제 계정별 수수료
- available KRW
- 이미 보유 중인 종목
- pending 주문
- Strategy Health 실거래 이력
- 주문금액별 liquidity capacity / depth slippage

따라서 JSON의 `BUY`는 **전략 1차 진입판단**이지 실제 주문이 제출됐다는 뜻이 아니다.

## 시간순 holdout과 purge

시계열에서 random shuffle split은 사용하지 않는다.

각 horizon마다 시간순 60/40 split을 만든 뒤, training sample의 미래 label 구간이 holdout 시작시각에 닿거나 넘어가면 해당 training sample을 제거한다.

예를 들어 holdout이 12:00:00에 시작하고 300초 label을 평가한다면, 11:55:00 이후 training sample은 미래 가격 구간이 holdout과 겹치므로 training 통계에서 purge한다.

이 조치는 forward-label overlap 누수를 막는다. 그러나 아직 학습 모델을 별도로 fit한 것이 아니므로 이를 완전한 OOS 성과라고 부르지 않는다.

## JSON 결과

출력에는 다음이 포함된다.

- JunhyunBank 버전과 StrategyConfig snapshot
- public trade/orderbook 메시지 수와 WebSocket 오류
- 후보/샘플/BUY 수
- label completion/miss 수
- horizon별 all / buy / purged train buy / holdout buy 통계
- horizon별 candidate / control 및 각각의 holdout 통계
- candidate BUY/reason headline과 분리된 control BUY/reason 통계
- mean/median net return, positive net rate
- ExpectedMove 평균, 실제 절대움직임 coverage, correlation, observed/expected ratio
- 개별 feature snapshot, entry bid/ask, future label
- `orders_submitted: 0`

## 실행

```bash
python scripts/validate_public_edge.py \
  --seconds 1800 \
  --horizons 30,60,120,300 \
  --sample-every 10 \
  --control-count 4 \
  --control-rotate-seconds 60 \
  --assumed-fee 0.0005 \
  --output edge-validation.json
```

기본 15분도 실행은 가능하지만 전략 워밍업 3분과 최대 300초 label을 고려하면 실제 sampling 구간은 더 짧다. 한 세션으로 결론을 내리지 말고 서로 다른 시간대·평일/주말·상승/하락/급변 구간을 여러 번 수집해야 한다.

정상 종료 실패, WebSocket 오류, 전략 입력검증에서 거부된 trade/orderbook, 해석 불가능한 quote, 표본 0건, `missed`/`pending`/`invalid` label, book 구독 상한 때문에 버린 신규 표본 중 하나라도 있으면 출력의 `data_integrity_ok`는 false이며 명령은 종료코드 2를 반환한다. `run.integrity_reasons`와 `run.integrity_counts`에 원인과 개수를 남기므로 파일은 조사에 사용할 수 있지만 정상 검증 세션으로 자동 승인하면 안 된다. 후보와 control 표본은 headline 통계부터 분리되며, 역할 전환 때 상태를 초기화하므로 대조군이 후보 결과를 미리 채우는 경로도 차단한다.

## 해석 원칙

V4.0.3 결과만으로 전략 threshold를 자동 변경하지 않는다. 특히 BUY 표본 수가 적을 때 positive rate가 높거나 낮아도 결론을 내리지 않는다.

다음 단계는 raw trade/L2 recorder + deterministic replay다. 충분한 기간의 데이터를 확보한 뒤 동일 이벤트를 재생해 parameter 변경을 같은 시장경로에서 비교하고, purged walk-forward 및 fee/delay/slippage stress를 통과한 변경만 실전 전략 후보로 승격한다.

## 실거래 안전 불변조건

이 작업으로 아래는 변경하지 않는다.

- LIVE 전용 제품 동작
- durable `order_intents`와 identifier reconciliation
- Private `myOrder`/`myAsset` 보조 reconciliation
- 사용자 기존 보유분과 `managed_quantity` 분리
- stale/API failure/경보 시장 신규매수 차단
- DRAINING / Emergency Stop 의미
- updater rollback
- 실제 진입/청산 threshold 및 자금배분 공식
