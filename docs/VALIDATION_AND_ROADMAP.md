# V4.0.5 검증 상태와 개발 로드맵

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

- 실제 시장 forward label
- raw trade/L2 보존
- deterministic replay
- 실제 수수료/spread/size별 slippage
- 주문 지연/partial fill
- purged TRAIN/VALIDATE/OOS
- regime별 성과
- MDD/tail loss
- parameter sensitivity

V4.0.5 기준 A/B와 C의 주요 구조는 크게 보강됐고, D는 **측정 → 저장 → 동일 경로 재현**까지 왔다. 그러나 execution realism과 충분한 장기간 OOS가 아직 없으므로 “릴리즈가 성공했다”와 “수익성이 검증됐다”를 같은 뜻으로 사용하지 않는다.

## 2. V4 실거래 안전성 기반

### 주문 상태 머신

V4.0.0부터 주문 전에 unique identifier와 주문 의도를 SQLite에 저장한다. timeout/5xx/응답 유실에서는 동일 주문을 재전송하지 않고 identifier REST reconciliation으로 terminal 상태와 실제 trades를 확인한다. partial fill은 실제 수량·금액·수수료만 반영하며 pending 주문이 있으면 동일 시장 신규주문을 차단하고 재시작 후에도 복구한다.

### managed quantity

JunhyunBank가 직접 체결한 수량만 자동관리한다. 0/NULL managed quantity를 계정 총잔고로 대체하지 않으며, 부분청산 뒤에는 남은 managed quantity만 관리한다.

### 런타임 데이터

Public WebSocket reconnect, 시장 discovery fail-closed/backoff, persistent deep orderbook subscription, deep 재진입 stale state 제거, 데이터 상태 UI, stale trade/orderbook 신규진입 차단을 유지한다.

### updater

V4.0.1부터 새 EXE digest, 기존 EXE+DB snapshot, `--post-update-verify`, DB migration/quick_check/필수 테이블 health marker를 검증한다. 실패하면 EXE+DB를 함께 rollback하며 자동매매를 자동 재개하지 않는다.

### Private reconciliation

V4.0.2부터 authenticated `myOrder`/`myAsset`은 pending 상태 변화의 빠른 신호로만 사용한다. 실제 체결 회계는 identifier REST가 정본이며 Private WS 장애 시 REST fallback을 유지한다. `myAsset`으로 managed quantity를 추정하지 않는다.

## 3. 가장 중요한 전략 한계

현재 `ExpectedMove`는 대략 다음 성격이다.

```text
ExpectedMove = recent |30s return| distribution의 70% quantile 계열
```

이는 “현재 BUY 뒤 앞으로 얼마나 오를 것인가”가 아니라 최근의 **절대 움직임 크기 proxy**다. 현재 entry cost gate와 capital sizing에 사용되지만 방향성 성공확률/조건부 net expectancy가 충분히 검증되지 않았다. 이를 이미 검증된 edge라고 부르면 안 된다.

## 4. V4.0.3 — Forward Edge Validator

`validate_public_edge.py`는 API Key/주문 없이 실제 Public trade/orderbook으로 현재 전략을 워밍업하고 후보 시점 entry ask → 미래 bid를 양쪽 가정 수수료까지 포함해 label한다.

중요한 품질 제어:

- 늦은 horizon quote를 임의로 사용하지 않고 `missed` 처리.
- live와 동일한 stale gate.
- deep 재진입 old book/quote 제거.
- BUY와 전체 후보 분리.
- ExpectedMove calibration 측정.
- 시간순 holdout 경계에서 forward horizon overlap purge.

한계: size별 depth walk, 실제 주문 latency/partial fill, 실제 계정 fee/잔고/pending/Strategy Health를 재현하지 않는다.

## 5. V4.0.4 — Raw Market Data Recorder

`record_public_market.py`는 자격증명 없이 실제 Public market data를 저장한다.

- 전체 안전 KRW raw trade.
- 현재 Hot 후보 L2 orderbook, 기본 15 level.
- exchange timestamp.
- local receive wall timestamp.
- local receive monotonic timestamp.
- process sequence.
- session/config/subscription/error metadata.

WebSocket callback은 bounded queue에 `put_nowait`만 수행한다. disk I/O/fsync/rotation/gzip은 background worker로 분리한다. queue full은 callback을 block하지 않고 `dropped`로 드러내며 nonzero exit로 불완전 capture를 표시한다.

활성 `.jsonl.part`는 crash 후 마지막 완전한 line까지 복구하고 atomic finalize한다. gzip도 `.gz.part`를 거쳐 durable finalize한 뒤 plain source를 삭제한다. bytes/file-count retention을 적용한다.

현재 recorder는 실거래 live engine에 직접 주입하지 않고 별도 Public-only 연구 명령으로 유지한다.

## 6. V4.0.5 — Deterministic Replay

`replay_strategy.py`는 V4.0.4 recorder session 하나를 네트워크와 주문 없이 replay한다.

핵심:

- `received_monotonic_ns` 첫 record를 logical time 0으로 사용.
- file record 순서를 그대로 처리.
- cross-thread enqueue 때문에 receive timestamp가 약간 뒤로 가면 clock을 뒤로 돌리지 않고 clamp/diagnostic.
- production `MicroFlowStrategy`를 수정하지 않고 replay subclass에서 local trade/book freshness만 recorded logical clock으로 대체.
- session_start의 당시 `StrategyConfig`와 안전 KRW universe 복원.
- recorder record를 원래 WebSocket event shape으로 재구성.
- 저장된 orderbook 시점마다 종목별 cadence로 feature/entry decision/regime/top-of-book snapshot 생성.
- 동일 recording + 동일 코드/config/fee의 decision rows를 canonical JSON으로 SHA-256 fingerprint.
- incomplete session은 조사 가능하지만 CLI nonzero exit.
- replay 경로는 API Key, UpbitClient, execution/order API를 사용하지 않음.

`decision_fingerprint_sha256`는 **재현성 지표이지 수익성 지표가 아니다.**

## 7. 연구 데이터 최소 품질 기준

전략 결과를 해석하기 전에 최소한 다음을 확인한다.

- capture/replay의 `orders_submitted == 0`.
- recorder drop == 0.
- session start/end가 정상적으로 존재.
- WebSocket 오류/gap이 과도하지 않음.
- 필요한 horizon의 label completion이 충분함.
- L2가 실제 평가 대상 후보에 존재함.
- seq/clock retrograde diagnostics가 비정상적으로 많지 않음.
- BUY 표본 수가 통계 해석에 충분함.
- 특정 한 코인/짧은 시간대에 표본과 성과가 몰리지 않음.

몇 건의 BUY나 짧은 한 세션을 근거로 threshold를 바꾸지 않는다.

## 8. 다음 P1 — Execution Simulator

이제 가장 중요한 다음 구현은 deterministic replay 위에 **실행 현실성(execution realism)** 을 추가하는 것이다.

최소 요구:

1. 당시 L2 snapshot에서 요청 KRW/수량별 depth walk.
2. 매수는 ask side, 매도는 bid side로 실행 가능 가격 계산.
3. fee + spread + depth impact 분리 기록.
4. configurable latency: 0 / +100 / +250 / +500ms 등.
5. 지연 후 최초 사용 가능한 book을 사용하되 데이터 gap/stale이면 fill 금지.
6. IOC를 full/partial/no fill로 구분.
7. book capacity가 부족한 부분을 억지로 마지막 가격에 체결시키지 않음.
8. simulator는 pure research layer이며 `execution.py`/실주문 API를 호출하지 않음.
9. 같은 replay input + simulator config에서 동일 결과 fingerprint.

실제 live 주문과 simulator fill의 오차는 장기간 데이터를 통해 별도로 측정해야 한다.

## 9. Purged Walk-forward / OOS

시간순서를 유지한다.

```text
TRAIN → VALIDATE → untouched OOS
```

random shuffle split을 사용하지 않는다. forward label/holding horizon이 다음 fold와 겹치지 않도록 purge/embargo를 둔다.

권장 절차:

1. 평일/주말, 상승/하락, 저변동/고변동, 급변 event를 포함한 recorder 데이터 축적.
2. current strategy baseline을 고정.
3. TRAIN에서만 candidate model/parameter 제안.
4. VALIDATE에서 candidate 수를 줄이고 주변 parameter plateau 확인.
5. untouched OOS에서 최종 비교.
6. rolling/purged walk-forward 반복.
7. 결과는 market/regime/date별로 분해.

## 10. Stress 요구

후보 전략은 최소 다음 stress에서 쉽게 붕괴하지 않아야 한다.

- fee × 1.0 / 1.25 / 1.5.
- execution delay +100 / +250 / +500ms.
- depth slippage 악화.
- partial/no fill.
- threshold 주변 ±20~30% sensitivity.
- 특정 최고 수익 코인 제거.
- 최고 수익일/급등 이벤트 제거.
- 상승/하락/저변동/고변동 regime 분리.

최고 백테스트 수익 하나보다 넓은 parameter 영역의 안정성을 본다.

## 11. Conditional ExpectedMove와 sizing

Execution simulator와 충분한 recorder/OOS가 준비된 뒤에만 진행한다.

ExpectedMove 후보는 현재 features를 조건으로 한 **forward executable return distribution**을 예측해야 한다. direction probability, gain/loss distribution, calibration, uncertainty interval을 분리해 평가한다.

새 estimator가 current volatility proxy보다 여러 OOS fold에서 일관되게 개선될 때만 live candidate가 된다.

그 이후 sizing은 임의 고정 KRW cap 대신 다음 같은 데이터 기반 risk budget을 검토할 수 있다.

- bootstrap lower-confidence edge.
- damped Kelly류.
- liquidity capacity.
- recent drawdown/tail loss.
- portfolio/BTC beta correlation.

데이터 없이 Kelly/correlation 모델부터 구현하지 않는다.

## 12. Strategy Health 후속

현재 Strategy Health는 실제 strategy outcomes를 사용한다. health=0이고 실거래가 없으면 자연 회복 근거가 부족할 수 있다.

장기간 replay/OOS가 준비된 뒤 shadow decision 결과를 **실제 PnL과 분리된 상태**로 축적해 health recovery evidence로 사용할 수 있다. shadow 결과를 실제 PnL ledger와 섞거나 health를 자동 강제 해제하면 안 된다.

## 13. 실제 환경에서 추가 확인할 운영 문제

- Private authenticated WS 장시간 안정성.
- 사용자가 같은 코인을 직접 추가 매수/매도한 복잡한 시나리오.
- locked balance가 큰 경우 managed sell 가능량.
- updater verify/rollback의 반복 실제 Windows 경로.
- 24/7 운전 시 메모리/스레드/DB/로그 성장.
- recorder 장기 실행의 disk/queue/compression 부하.
- 최소주문 미만 dust managed position의 DRAINING UX.

## 14. 채택/기각 철학

채택하려면 OOS executable net expectancy가 양수이고, 충분한 표본/데이터 품질이 있으며, 비용·지연·slippage stress에서 쉽게 붕괴하지 않고, 특정 코인/날짜에 과도하게 의존하지 않으며, 주변 parameter에서도 성능이 유사하고, drawdown/tail loss가 통제 가능해야 한다.

기각해야 할 패턴은 training에서만 좋거나, threshold를 조금만 바꾸면 붕괴하거나, fee/spread를 빼야만 플러스이거나, 몇 번의 급등이 전체 성과 대부분이거나, realistic delay/depth를 넣으면 edge가 사라지거나, 누락된 label/fill을 무시했을 때만 좋아 보이는 경우다.

## 15. 다음 릴리즈 순서

현재 권장 순서:

1. **V4.0.5 deterministic replay 안정화/회귀검증.**
2. **Execution simulator + fill diagnostics.**
3. **Purged walk-forward/stress harness.**
4. 여러 시장상태 recorder 데이터 축적 및 baseline report.
5. Conditional ExpectedMove 후보 비교.
6. 충분한 OOS 근거가 생긴 경우에만 live threshold/sizing 후보 검토.

중요: 5~6을 데이터보다 먼저 구현해 live에 연결하지 않는다.