# V4.0.3 검증 상태와 개발 로드맵

이 문서의 목적은 **무엇이 구현됐는가보다 무엇을 아직 믿으면 안 되는가**를 명확히 유지하는 것이다.

## 1. 검증을 네 층으로 구분한다

### A. 코드 검증

- import/문법
- unit/regression test
- 주문/관리수량/업데이트 불변조건
- Windows + Ubuntu GitHub CI

### B. 배포 검증

- Windows runner 설치
- pytest
- 실제 Upbit Public REST/WebSocket smoke
- PyInstaller build
- GitHub Release / asset digest

### C. 실거래 안전성 검증

- durable order intent
- ambiguous POST 복구
- partial fill accounting
- restart recovery
- 사용자 보유분 보호
- Private account event + REST reconciliation
- updater health verification/rollback

### D. 전략 수익성 검증

- 실제 시장의 미래 label
- 실제 수수료/spread/slippage
- 주문 지연
- purged train/validate/OOS
- regime별 성과
- MDD/tail loss
- parameter sensitivity

V4.0.3 기준 A/B와 C의 주요 구조는 크게 보강됐다. **D는 아직 초기 단계다.** 코드와 릴리즈가 성공했다는 사실을 수익성이 검증됐다는 의미로 사용하지 않는다.

## 2. V4까지 완료한 핵심 안전성 작업

### 주문 상태 머신

V4.0.0부터:

- 주문 제출 전에 unique identifier와 주문 의도를 SQLite에 저장.
- timeout/5xx/응답 유실에서 동일 주문 자동 재전송 금지.
- identifier 기반 REST reconciliation.
- terminal 여부와 실제 trades 확인 후에만 체결 반영.
- partial fill의 실제 수량·금액·수수료 원자 기록.
- pending 주문이 있으면 신규매수 차단.
- 재시작 후 pending 복구.
- POST 429 자동 재시도 금지.

### managed quantity 보호

- JunhyunBank가 직접 체결한 수량만 자동관리.
- 0/NULL managed quantity를 계정 총잔고로 추정하지 않음.
- 부분매도 후 남은 managed quantity만 계속 관리.
- 사용자 별도 보유분이 있어도 관리수량 외 매도를 금지하는 회귀테스트 유지.

### 런타임 데이터

- Public WebSocket Origin 제거와 reconnect.
- 시장 discovery 실패 backoff 및 fail-closed alert parsing.
- deep orderbook subscription을 후보 변경마다 재연결하지 않고 live update.
- deep 재진입 종목의 stale book state 제거.
- 시세/워밍업/후보/orderbook 상태 UI 진단.
- stale trade/orderbook 신규진입 차단.

### updater

V4.0.1부터:

- 새 EXE digest 확인.
- 기존 EXE + SQLite DB/WAL/SHM snapshot.
- `--post-update-verify` 비거래 부팅으로 DB migration/quick_check 검증.
- health marker 실패 시 EXE+DB 함께 rollback.
- 검증 성공 전 LIVE 자동재개 금지.

### Private account reconciliation

V4.0.2부터:

- authenticated `myOrder` + `myAsset` 단일 WebSocket.
- fresh JWT/reconnect/ping 및 credential redaction.
- JunhyunBank identifier 주문 이벤트만 pending reconciliation을 즉시 깨움.
- WebSocket을 회계 정본으로 쓰지 않고 REST identifier 조회를 정본으로 유지.
- Private WS 장애 시 REST fallback.
- pending REST 조회 failure backoff.
- `myAsset`은 balance-change signal일 뿐 managed quantity 추정에 사용하지 않음.

## 3. 현재 가장 중요한 미검증 사실

### `ExpectedMove`는 아직 조건부 기대수익 모델이 아니다

현재 구현은 대략 다음 성격이다.

```text
ExpectedMove = recent |30s return| distribution의 70% quantile 계열
```

즉 “현재 BUY 신호 뒤에 앞으로 얼마나 오를 것인가”가 아니라 최근의 **절대 움직임 크기 proxy**다.

현재 entry logic은 이 값을 거래비용과 비교하고 capital sizing에도 사용한다. 방향성 성공확률과 조건부 net expectancy가 충분히 검증되기 전에는 이를 진정한 edge라고 부르면 안 된다.

## 4. V4.0.3 Forward Edge Validator

V4.0.3은 이 문제를 해결하기 위한 첫 연구 계측 기반이다.

`python scripts/validate_public_edge.py --seconds 1800 --output edge-validation.json`

특징:

- API Key를 읽지 않는다.
- 주문 API를 호출하지 않는다.
- 실제 Public trade/orderbook으로 현재 `MicroFlowStrategy`를 워밍업한다.
- 후보 평가 시점의 entry bid/ask와 특징/decision을 기록한다.
- 미래 horizon의 bid/ask로 label한다.
- long shadow net return은 **entry ask → future bid**, 양쪽 assumed fee를 포함한다.
- label quote가 허용 지연창을 넘기면 늦은 가격을 대신 쓰지 않고 `missed`로 기록한다.
- live와 동일한 3초 stale gate를 적용한다.
- deep 재진입 시 old orderbook history/quote를 제거한다.
- BUY와 전체 후보 통계를 분리한다.
- ExpectedMove calibration을 측정한다.
- 각 horizon의 train/holdout 사이에서 forward-label overlap 구간을 purge한다.

이 validator도 아직 다음을 모델링하지 않는다.

- 주문 크기별 L2 depth walk/slippage
- queue position
- 실제 REST/WebSocket 주문 latency
- 실제 계정별 fee
- available KRW / held market / pending order
- 실제 Strategy Health history

따라서 V4.0.3의 BUY는 **전략 1차 BUY 판단 표본**이다. 실제 주문 체결 성과가 아니다.

## 5. V4.0.3 결과를 볼 때 최소 조건

한 세션의 평균수익만 보고 판단하지 않는다. 최소한 다음을 함께 본다.

- `orders_submitted == 0` 확인.
- WebSocket 오류가 과도하지 않은지.
- horizon별 label completion rate.
- missed label 비율.
- BUY 표본 수.
- BUY mean/median net return과 positive rate.
- 전체 후보와 BUY의 차이.
- train BUY와 holdout BUY의 차이.
- purged training 표본 수.
- ExpectedMove coverage/correlation/observed-to-expected ratio.
- 특정 종목/짧은 시간대에 결과가 몰리는지.

BUY 수가 몇 건뿐이면 방향성 결론을 내리지 않는다. label completion이 낮으면 전략보다 데이터 파이프라인을 먼저 고친다.

## 6. 다음 P1 — Raw Market Data Recorder

다음 우선순위는 **장기 recorder**다. V4.0.3 validator의 online sample만으로는 같은 시장경로에서 여러 전략 후보를 반복 비교할 수 없다.

최소 저장 데이터:

### trade

- market
- exchange timestamp
- local receive timestamp
- price
- volume
- aggressor side
- sequential id가 제공되면 저장

### orderbook

- market
- exchange timestamp
- local receive timestamp
- 최소 상위 5 level, 가능하면 15~30 level
- bid/ask price and size

### strategy/decision

- candidate rank/HotScore
- ActivityQ/AggressionQ/BookQ/MomentumQ
- Quality
- spread
- ExpectedMove
- realized volatility
- regime
- decision/reason

### execution linkage

실거래 이벤트를 recorder 데이터와 연결할 수 있게 identifier/uuid, request time, accepted time, fills/fee도 별도 ledger와 연결한다. 단, raw market recording이 주문 thread를 block해서는 안 된다.

Recorder 설계 요구:

- bounded queue
- writer thread/process 분리
- drop count/queue lag 진단
- crash-safe file rotation
- 압축
- 디스크 용량 상한/retention
- format schema version

## 7. 다음 P1 — Deterministic Replay

Recorder 다음에는 동일 raw events를 전략에 재생하는 replay가 필요하다.

핵심 요구:

- 이벤트의 원래 시간순서 보존.
- exchange/local receive timestamp 선택 가능.
- `time.monotonic()`/`time.time()`에 직접 묶인 전략 의존성을 injectable clock으로 분리.
- 동일 fixture를 여러 번 재생하면 동일 feature/decision 결과.
- current strategy와 candidate strategy를 **같은 입력 경로**에서 비교.
- 실제 당시 orderbook으로 size별 execution simulation.

Replay가 없으면 threshold 변경 전후를 서로 다른 시장 구간에서 비교하는 오류를 피하기 어렵다.

## 8. Walk-forward / OOS 계획

시간순서를 유지한다.

```text
TRAIN → VALIDATE → OOS
```

random shuffle split은 사용하지 않는다.

권장 절차:

1. 여러 시장상태를 포함한 recorder 데이터 축적.
2. event labeling과 current baseline 측정.
3. TRAIN에서만 후보 모델/parameter 제안.
4. VALIDATE에서 후보를 줄이고 parameter plateau 확인.
5. untouched OOS에서 최종 비교.
6. rolling/purged walk-forward 반복.

forward horizon label이 다음 fold와 겹치지 않도록 purge/embargo를 둔다.

## 9. Stress 요구

전략 변경 후보는 최소 다음 stress에서 쉽게 붕괴하지 않아야 한다.

- fee × 1.0 / 1.25 / 1.5
- execution delay +100 / +250 / +500ms
- depth slippage 악화
- partial fill
- threshold 주변 ±20~30% sensitivity
- 특정 최고 수익 코인 제거
- 최고 수익일/이벤트 제거
- 상승/하락/저변동/고변동 regime 분리

가장 높은 backtest 수익 하나가 아니라 넓은 parameter 영역의 안정성을 본다.

## 10. Conditional ExpectedMove와 sizing

충분한 recorder/replay/OOS 이후에만 진행한다.

후보 방향:

- 현재 features를 조건으로 한 forward-return distribution.
- direction probability + expected gain/loss 분리.
- calibration과 uncertainty interval.
- bootstrap lower confidence edge.

새 estimator가 current volatility proxy보다 OOS에서 일관되게 개선될 때만 live candidate로 고려한다.

그 다음 자금배분은 고정 KRW cap 대신 다음을 사용해 보수화할 수 있다.

- bootstrap lower-bound edge
- damped Kelly류
- liquidity capacity
- recent drawdown/tail loss
- portfolio/BTC beta correlation

데이터 없이 Kelly/correlation 모델부터 구현하지 않는다.

## 11. Strategy Health 후속

현재 Strategy Health는 실제 strategy outcomes에 기반한다. health=0이고 신규 거래가 없으면 실제 outcome만으로 자연 회복하기 어렵다.

장기 recorder/replay 기반이 생긴 뒤 shadow decision 성과를 별도 상태로 축적해 **실거래 health와 섞지 않고** recovery evidence로 사용하는 설계를 검토한다.

shadow 결과를 실제 PnL처럼 DB에 섞거나 health를 자동으로 강제 해제하면 안 된다.

## 12. 아직 실제 환경에서 추가 확인할 것

- Private authenticated WS의 장시간 운영 안정성. CI에는 실계정 secret을 주입하지 않는다.
- 사용자가 직접 같은 코인을 추가 매수/매도한 복잡한 운영 시나리오.
- locked balance가 큰 경우 managed sell 가능량 처리.
- Windows 업데이트 verify/rollback을 실제 사용자 경로에서 반복 검증.
- 장시간 24/7 운전 시 메모리/스레드/DB/로그 성장.
- 최소주문 미만 dust managed position 때문에 DRAINING이 오래 유지되는 UX.

## 13. 채택/기각 기준의 철학

채택하려면:

- OOS net expectancy가 양수.
- 충분한 표본과 label quality.
- 비용/지연 stress에서 쉽게 음수로 붕괴하지 않음.
- 특정 한두 코인/날짜에 의존하지 않음.
- 주변 parameter에서도 유사한 성능.
- drawdown/tail loss가 통제 가능.
- simulated fill과 실제 fill 오차가 장기적으로 측정/보정 가능.

기각해야 할 패턴:

- training에서만 좋음.
- threshold를 조금만 바꾸면 붕괴.
- fee/spread를 빼야만 플러스.
- 몇 번의 급등이 전체 수익의 대부분.
- execution delay를 넣으면 edge 소멸.
- label completion이 낮은데 누락을 무시한 채 성과를 계산.

## 14. 현재 우선순위

1. **V4.0.3 forward-edge validator 안정화/여러 세션 수집**
2. **Raw Market Data Recorder**
3. **Deterministic Replay Engine**
4. **Purged Walk-forward + execution stress harness**
5. **Conditional ExpectedMove 후보 모델**
6. **Uncertainty-aware dynamic sizing**
7. **Correlation-aware portfolio allocation**
8. **Shadow Health recovery evidence**
9. 운영 리포트/진단 bundle/장기 관측성 개선

중요: 5~8은 2~4가 충분히 준비된 뒤 진행한다. 데이터 없이 모델을 복잡하게 만드는 것은 정교한 과최적화가 될 가능성이 높다.

## 15. 현재 평가

### 구현 완성도

V4.0.0~V4.0.2에서 실거래 자동매매의 주문 복구, 부분체결, managed quantity 보호, updater rollback, Private account 보조 reconciliation이 크게 강화됐다.

### 실전 운영 완성도

초기 V2/V3보다 높지만 24/7 장기 실제 사용자 환경 검증과 복잡한 manual balance interaction은 더 필요하다.

### 전략 검증 완성도

**초기 계측 단계.** V4.0.3은 기존 “논리적으로 그럴듯한 MicroFlow 가설”을 실제 미래 bid/ask로 측정하기 시작한 단계다. 충분한 recorder/replay/purged OOS가 쌓이기 전에는 “검증된 수익전략”이라고 표현하지 않는다.
