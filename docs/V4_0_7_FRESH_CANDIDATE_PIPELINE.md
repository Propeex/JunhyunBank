# V4.0.7 — Fresh Candidate Pipeline

## 증상

V4.0.0 실사용 캡처에서는 시장 유니버스와 WebSocket 자체는 살아 있었습니다.

- KRW 안전시장 약 250개
- Trade WS 3/3
- 추적 시장 221/250
- 후보 24개
- Orderbook 연결(24), 최근 수신 0초대

그런데 종목별 진입 진단에는 `체결/호가 수신 대기 또는 3초 이상 지연`이 반복됐고, 일부 후보만 `예상 움직임이 거래비용 대비 부족`으로 내려갔습니다.

첫 번째 상태는 전략이 신중해서 매수를 안 한 것과 다릅니다. scanner가 후보로 올린 종목이 실제 주문 직전 stale gate를 이미 통과할 수 없는 상태였다는 뜻입니다.

## 원인

`MicroFlowStrategy.hot_score()`의 scanner freshness 허용폭은 entry safety gate보다 넓습니다. HotScore는 짧은 순간의 수급 이상을 몇 초간 유지해 scanner가 과도하게 흔들리지 않게 설계됐지만, LIVE 신규매수는 `SafetyConfig.market_data_stale_seconds`(기본 3초)보다 오래된 trade/orderbook에서 금지됩니다.

기존 파이프라인은 다음 순서였습니다.

1. 전체 시장 HotScore 계산
2. 상위 `scanner_candidate_count`만 자름
3. 그 목록에서 deep orderbook 후보 선정
4. deep 후보는 anti-churn 목적의 minimum residency(기본 30초) 유지
5. 실제 진입평가 시 3초 stale gate 적용

따라서 3~10초 전에 마지막 체결이 있었던 시장이 높은 HotScore로 상위 목록을 차지할 수 있었습니다. 이 시장은 deep slot을 차지하지만 실제 진입평가에서는 매번 stale로 탈락합니다. 더 아래 순위에 있는, 방금 체결이 들어온 fresh 시장은 top-N cutoff 때문에 deep orderbook 분석 기회를 얻지 못할 수 있었습니다.

특히 minimum residency가 stale 후보에도 적용되면 이 상태가 수십 초 지속될 수 있어 실제로는 시장을 감시하고 있으면서도 '매수 심사 가능한 후보'가 굶는 starvation이 발생합니다.

## V4.0.7 수정

production `live_engine.TradingEngine`에서 scanner 후보와 실제 entry freshness gate를 정렬합니다.

### 1. stale 후보 선제 제거

scanner 상위 후보 중 다음 조건을 넘은 비관리 시장은 deep 후보로 보내지 않습니다.

```text
trade_age > SafetyConfig.market_data_stale_seconds
```

기본값은 3초입니다.

### 2. top-N 밖 fresh 후보 보충

stale 후보를 제거한 자리만큼 전체 안전 KRW 시장 중:

- scanner 상위 목록에 아직 없고
- 마지막 trade가 entry stale limit 이내이며
- HotScore가 0보다 큰

시장을 추가 계산해 HotScore 순으로 보충합니다.

따라서 상위 30개 중 절반이 stale이라고 해서 deep 후보가 15개로 줄어들거나, fresh 31~45위 시장이 영원히 분석되지 않는 문제가 줄어듭니다.

### 3. stale은 deep residency보다 우선

기존 30초 minimum residency는 후보 churn을 줄이기 위한 장치입니다. 하지만 신규매수에 사용할 수 없는 stale 시장을 30초 유지할 이유는 없습니다.

V4.0.7에서는 비관리 후보가 stale이 되는 즉시 다음 candidate refresh에서 deep set에서 제거합니다.

### 4. managed position은 예외

JunhyunBank가 이미 보유·관리하는 포지션은 신규진입 후보가 아니므로 stale entry filter로 제거하지 않습니다. 관리 포지션은 기존과 같이 deep monitoring에 남아 청산 판단을 계속 받을 수 있습니다.

### 5. 화면 후보와 실제 심사 후보 일치

기존에는 좌측 후보 목록에 보이는 시장과 실제 3초 stale gate를 통과할 수 있는 시장이 다를 수 있었습니다.

V4.0.7은 GUI `candidates` event를 fresh actionable ranking으로 다시 써서 화면의 후보 수/시장과 실제 deep 진입 심사 대상이 일치하도록 합니다. runtime candidate count도 actionable set 기준으로 표시합니다.

### 6. 5분 무후보 진단

5분 이상 fresh actionable 후보가 하나도 없으면 단순히 `후보 없음`으로 끝내지 않고, 상위 HotScore 후보에서 stale로 제외된 개수를 포함해 로그를 남깁니다.

이 경우에는 전략 threshold보다 먼저 다음을 확인해야 합니다.

- Trade WS 수신 안정성
- 시장 전체 실제 체결 빈도
- 워밍업 상태
- candidate freshness starvation

## 바꾸지 않은 것

이번 패치는 매수 빈도를 억지로 늘리기 위해 신호 기준을 낮추는 변경이 아닙니다.

다음은 그대로입니다.

- `ignition_quality`
- `pullback_quality`
- ExpectedMove 계산
- `expected_move > actual round-trip cost × 2` 비용 gate
- Strategy Health
- Market Regime
- stale orderbook/trade 신규매수 차단
- V4.0.6 Best+IOC 주문 의미
- durable order intent / ambiguous POST recovery
- partial fill accounting
- managed quantity 보호
- Private myOrder/myAsset → REST reconciliation
- updater verify/rollback

즉 '아무거나 사게 만들기'가 아니라 **실제로 매수 심사까지 갈 수 없는 후보가 분석 슬롯을 독점하는 문제를 제거**한 것입니다.

## 검증

회귀테스트는 다음을 고정합니다.

1. stale 고득점 시장이 scanner top-N을 차지해도 fresh lower-rank 시장이 deep 후보로 보충됩니다.
2. 30초 residency 안에 있더라도 stale 비관리 후보는 deep set에서 제거됩니다.
3. stale managed position은 청산 안전성을 위해 deep monitoring에 남습니다.
4. UI candidate event는 stale raw scanner 결과가 아니라 actionable ranking을 표시합니다.
5. 기존 전체 unit/regression suite와 실제 Upbit Public REST/WebSocket read-only smoke를 함께 통과해야 합니다.

## 남은 문제와 다음 판단

V4.0.7 이후에도 충분히 긴 실사용에서 fresh 후보는 꾸준히 존재하지만 모든 후보가 다음 이유로만 탈락한다면, 그때는 candidate starvation이 아니라 실제 전략 gate의 문제입니다.

- `예상 움직임이 거래비용 대비 부족`
- `IGNITION/PULLBACK 품질 대기`
- `EXTENDED/EXHAUSTED`
- Strategy Health 0
- Market PANIC/RISK_OFF 영향

이 경우에도 거래가 적다는 이유만으로 threshold를 낮추지 않습니다. V4.0.3~V4.0.6에서 마련한 forward label, recorder, deterministic replay, Best+IOC execution semantics를 사용해 장기간 purged OOS에서 '현재 기준이 너무 엄격한지'를 검증한 뒤 조정합니다.
