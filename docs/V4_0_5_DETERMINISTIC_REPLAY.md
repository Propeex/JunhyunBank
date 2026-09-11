# V4.0.5 — Deterministic Strategy Replay

## 목적

V4.0.4에서 저장한 Public raw trade/L2 recording을 네트워크 연결이나 실제 주문 없이 다시 JH-MicroFlow에 흘려보내 **같은 입력에서 같은 특징·진입판단을 재현**하는 첫 replay 계층이다.

이 단계의 목표는 백테스트 수익률을 만드는 것이 아니다. 먼저 전략 코드가 과거의 동일 시장경로를 반복해서 동일하게 해석하는지 보장해야 이후 execution simulation, purged walk-forward, stress test가 의미를 갖는다.

## 안전 범위

- `scripts/replay_strategy.py`는 `UpbitClient`를 생성하지 않는다.
- API Key/keyring/JWT를 읽지 않는다.
- 주문 API와 `execution.py`를 호출하지 않는다.
- 출력에는 항상 `orders_submitted: 0`을 기록한다.
- production `MicroFlowStrategy`의 진입/청산 threshold, capital sizing, managed quantity, durable order state machine을 변경하지 않는다.
- replay 전용 subclass에서 **local receive freshness clock만** recording timestamp에 바인딩한다.

## 입력

V4.0.4 `scripts/record_public_market.py`가 생성한 finalized 파일:

- `upbit-public-<session>-000001.jsonl`
- 또는 `.jsonl.gz`

활성 `.part` 파일은 자동 입력에서 제외한다. 디렉터리에 여러 recorder session이 있으면 기본적으로 filename 기준 최신 한 session만 선택한다. `--session`으로 특정 session id 또는 유일한 suffix를 선택할 수 있다.

`session_start` metadata에서 다음을 복원한다.

- 당시 안전 KRW market universe
- 당시 `StrategyConfig`
- recorder를 만든 JunhyunBank 버전
- `orders_submitted=0` Public-only 표식

한 입력에 여러 `session_start`가 섞이면 fail-closed 한다.

## Replay clock

실시간 전략의 `trade_age()`와 `book_age()`는 원래 process monotonic clock에 의존한다. 이를 그대로 offline replay하면 실행 PC 속도에 따라 결과가 달라진다.

V4.0.5는 production 전략을 수정하지 않고 `ReplayMicroFlowStrategy`에서 다음만 override한다.

- trade 수신시각
- orderbook 수신시각
- `trade_age()`
- `book_age()`

clock은 recorder의 `received_monotonic_ns`를 첫 record 기준 0초로 변환한다. 여러 WebSocket callback thread의 enqueue 경쟁 때문에 파일 순서에서 작은 timestamp 역전이 생기면 logical time을 뒤로 돌리지 않고 직전 값으로 clamp하며 `clock_retrograde_events`에 기록한다.

따라서 replay는 실제 시간만큼 `sleep()`하지 않고도 recorded freshness gate를 재현한다.

## Event reconstruction

Recorder record의 top-level `market`과 `exchange_timestamp_ms`를 `MicroFlowStrategy`가 기대하는 Public WebSocket event 형태로 복원한다.

### trade

- `code = market`
- `trade_price`
- `trade_volume`
- `ask_bid`
- `trade_timestamp`

### orderbook

- `code = market`
- `timestamp`
- `orderbook_units`

Recorder에 이미 exchange timestamp가 있으므로 replay에서 현재 wall clock을 가격/프레임 timestamp fallback으로 사용하지 않는다.

## 평가 방식

Replay는 file record 순서를 그대로 처리한다. orderbook event가 들어올 때 종목별 기본 1초 cadence로 현재 전략의 `evaluate_entry()`를 호출한다.

- `health=1.0`: 전략 자체의 entry hypothesis 측정용이며 실제 계정 Strategy Health를 재구성하는 것이 아니다.
- market regime은 replay 시점 전체 recorded safe KRW universe에서 계산한다.
- fee는 CLI 기본 각 방향 0.05%이며 `--bid-fee`, `--ask-fee`로 바꿀 수 있다.
- recorder가 Hot 후보에 대해서만 L2를 저장하므로 replay 평가도 실제 저장된 orderbook 종목에 한정된다.

각 평가에는 다음을 저장한다.

- effective/raw signal
- signal kind
- score/reason
- regime/regime factor
- ExpectedMove / decision cost
- capital fraction / initial risk / expected horizon
- replay 기준 trade/book age
- top-of-book bid/ask
- 당시 feature snapshot

비정상 `NaN`/`inf` 값은 JSON에 조용히 쓰지 않고 `null`로 정규화한다.

## Deterministic fingerprint

모든 decision row를 canonical JSON으로 직렬화해 순서대로 SHA-256에 넣는다.

```text
summary.decision_fingerprint_sha256
```

동일 recorder session + 동일 JunhyunBank 전략 코드 + 동일 StrategyConfig + 동일 fee/evaluation cadence이면 fingerprint가 동일해야 한다. 회귀테스트는 같은 synthetic recording을 두 번 replay해 decision list와 fingerprint가 정확히 같은지 확인한다.

이 fingerprint는 **수익성 지표가 아니다.** 전략 로직 변경으로 해석 결과가 달라졌는지 추적하는 재현성 지표다.

## 데이터 완전성 표시

출력 `input`에는 다음 진단을 남긴다.

- recorder schema version
- session_start/session_end 개수
- complete_session
- first/last receive timestamp
- seq regression 개수
- clock retrograde 개수
- nonzero order metadata 개수

`session_end`가 없는 비정상 종료 recording도 조사 목적으로 replay할 수 있지만 CLI exit code는 2로 반환해 자동화가 이를 완전한 연구 데이터로 승격하지 못하게 한다.

## 실행 예

```bash
python scripts/replay_strategy.py \
  --input ~/.junhyunbank/recordings \
  --session latest \
  --output replay-v405.json
```

이 명령은 네트워크를 사용하지 않으므로 recorder 파일만 있으면 반복 실행할 수 있다.

## 아직 하지 않는 것

V4.0.5 replay는 다음을 의도적으로 포함하지 않는다.

- 실제 주문 size에 따른 L2 depth walk
- IOC partial fill simulation
- 100/250/500ms execution latency stress
- portfolio cash/managed position state simulation
- exit/partial exit PnL engine
- parameter fitting
- conditional ExpectedMove 학습
- Kelly/correlation sizing 변경

다음 단계는 이 deterministic replay 위에 **execution simulator + purged walk-forward/stress harness**를 올리는 것이다. 실제 전략 threshold나 sizing 변경은 충분한 여러 시장상태의 recorder 데이터와 OOS 결과가 쌓인 뒤에만 검토한다.
