# JunhyunBank 개발 인수인계 — V4.3.0 기준

V4.3의 입력 검증, 위험예산, 영속 추적손절, managed state 격리와 검증 한계는 [V4.3 실전 운용 강화](V4_3_REAL_WORLD_HARDENING.md)를 먼저 읽으세요. V4.1의 진입·보유 판단 분리와 V4.2의 지속 수급 진입·차트 보완도 계속 적용됩니다.

이 문서는 이전 대화가 없어도 새 개발자 또는 새 ChatGPT가 제품 의도, 안전 불변조건, 현재 구현과 다음 작업을 바로 이어가기 위한 최우선 인수인계 문서다.

코드와 문서가 충돌하면 **현재 `main` 코드가 source of truth**다. 주문/복구는 `V4_AUDIT.md`, 업데이트는 `V4_0_1_UPDATE_RECOVERY.md`, Private reconciliation은 `V4_0_2_PRIVATE_RECONCILIATION.md`, 전략 검증 계보는 V4.0.3~V4.0.7 문서를 함께 읽는다.

## 1. 제품 정의

JunhyunBank는 Upbit KRW 마켓을 24시간 실시간 감시하고 체결·호가 기반 단기 수급 신호를 찾아 **실제 원화로 자동 매수·매도하는 Windows LIVE 전용 데스크톱 프로그램**이다.

PAPER 모드는 V2부터 제거됐다. 앱 `시작`은 실제 주문을 허용한다. `validate_public_edge.py`, `record_public_market.py`, `replay_strategy.py` 등 연구 도구는 실거래 경로와 분리하며 주문을 제출하지 않는다.

## 2. 반드시 유지할 사용자 요구

- LIVE 전용 자동매매.
- 시장 전체를 지속 감시하고 종목·진입·청산을 프로그램이 스스로 결정.
- 핵심은 짧은 수급/호가 기반 단타.
- `시간당 N회`, `1회 최대 N원`, `최대 N종목` 같은 임의의 고정 KRW/횟수 cap을 추가하지 않음.
- 신호 품질, market regime, 거래비용, 손절거리, 계정 노출, 호가 유동성, Strategy Health로 거래 여부와 자금배분을 동적으로 결정. 계정 대비 비율 안전상한은 허용함.
- 프로그램 실행 전부터 사용자가 보유한 코인을 임의로 매도하지 않음.
- 일반 `종료`는 신규매수를 막고 관리 포지션을 전략대로 청산한 후 멈추는 DRAINING.
- `긴급 정지`는 즉시 전략/신규주문을 멈추지만 강제 시장가 청산 기능이 아님.
- 보유자산 UI에 KRW 표시.
- GitHub Release 기반 업데이트와 자동 재시작.
- 업데이트 후 API Key와 SQLite 관리상태 유지.

## 3. 절대 깨면 안 되는 안전 불변조건

1. 출금 API를 구현하지 않는다.
2. Access Key / Secret Key / JWT를 GitHub, SQLite, 로그에 저장하지 않는다.
3. API Key는 OS keyring에만 저장한다.
4. 자동매도는 JunhyunBank가 실제 체결로 확보한 `managed_quantity` 범위에만 적용한다.
5. `managed_quantity`가 0/NULL이면 계정 총잔고로 대체해 추정 관리하지 않는다.
6. 주문 전 unique `identifier`를 SQLite `order_intents`에 먼저 영속화한다.
7. timeout/5xx/응답 유실 같은 ambiguous POST 결과에서 동일 주문을 재전송하지 않고 identifier로 reconciliation한다.
8. terminal 확인 전 동일 시장 중복주문을 막고 pending 주문이 있으면 신규매수를 차단한다.
9. partial fill은 실제 체결의 수량·금액·수수료만 원자적으로 반영한다.
10. Private `myOrder`/`myAsset`은 빠른 변화 신호일 뿐 회계 정본이 아니다. identifier REST 조회가 정본이다.
11. stale trade/orderbook, 반복 API 오류, 비정상 market universe, 경보/해석불가 시장에서는 신규매수하지 않는다.
12. 이미 관리 중인 포지션은 시장 경보가 생겨도 감시/청산 경로를 유지한다.
13. NaN/inf, 최소주문 미달, 비정상 API 응답에서 신규주문을 fail-closed 한다.
14. 업데이트는 새 EXE/DB 사전검증 성공 뒤에만 정상 재개하고 실패하면 EXE+DB snapshot을 함께 rollback한다.
15. 거래가 적다는 이유로 전략 threshold나 비용 gate를 임의 완화하지 않는다. recorder/OOS 근거가 먼저다.
16. 현재 일반 주문은 **Upbit Best+IOC**이며, 이를 generic market depth-walk 주문처럼 모델링하지 않는다.
17. scanner 후보가 실제 entry freshness gate를 이미 통과할 수 없다면 scarce deep-entry 슬롯을 장시간 점유시키지 않는다. 단, managed position의 exit monitoring은 freshness 때문에 제거하지 않는다.
18. 입력 검증을 통과하지 않은 체결·호가는 최신가격, feature, local freshness를 갱신하지 않는다.
19. universe 크기와 무관하게 BTC/regime 표본이 부족하면 `NEUTRAL`로 추정하지 않고 신규진입을 막는다.
20. 신호 기반 주문금액은 현금·단일/총 노출·초기 손절위험·포트폴리오 위험·표시 유동성 예산을 모두 통과해야 한다.
21. 활성화된 trailing stop은 SQLite에 저장하고 보유 중 또는 재시작 후 낮추지 않는다.
22. 계정 잔고가 한 번 누락됐다고 managed marker를 삭제하지 않는다. 반복 누락은 `QUARANTINED`, 최소주문 미만 잔량은 `DUST`로 보존한다.
23. 조회 GET의 제한 재시도와 달리 주문 POST는 자동 재시도하지 않는다. 애매한 결과는 identifier reconciliation으로 확인한다.
24. 세션 손실은 계정 전체 변동이 아니라 JunhyunBank 관리전략의 누적 실현손익과 fresh bid 평가손익만으로 계산한다. 계산할 수 없어도 신규진입을 막는다.
25. 정지 요청과 주문 POST를 공통 submission gate로 직렬화하고, BUY POST 직전 전체·종목별 데이터 신뢰성을 다시 확인한다.
26. LIVE·recorder·validator의 후보 선택은 같은 fresh actionable 선택기를 사용한다.
27. UI 이벤트는 하드 상한과 최신값 병합을 유지해 느린 화면 때문에 메모리가 무한 증가하지 않게 한다.
28. 시작 시 계정·세션 baseline보다 pending SELL reconciliation을 먼저 수행한다.
29. 시작 account/baseline을 안전하게 구성하지 못해도 관리 포지션 청산 루프는 시작하되 그 세션 신규매수는 영구 잠금하고 자동 rebase하지 않는다.
30. 청산 판단의 bid·spread·depth·freshness는 한 orderbook generation에서 읽고, POST 직전 정책평가 중 generation이 바뀌면 주문하지 않는다.
31. Best+IOC 세션 PnL 평가는 bid 1호가가 전체 관리수량을 받을 때만 허용한다. 부족하면 하위 호가를 추정하지 않고 신규진입을 막는다.
32. 자동 SELL 전 핵심 managed 회계 필드 손상은 격리한다. 이미 체결된 SELL에서 outcome 기준만 손상된 경우 실제 KRW PnL은 UUID-idempotent adjustment 원장에 보존한다.
33. market 중복행은 최악 상태로 합치고 `caution`/`warning` null·미확인 형식을 정상으로 추정하지 않는다. BTC와 전체·안전 허용목록 최소 floor를 모두 지킨다.
34. recorder/replay의 Hot/control 역할은 metadata·book 상태·결정·fingerprint까지 분리하며 control이 production 후보를 사전 워밍업하거나 headline 통계에 섞이지 않게 한다.
35. 사용자는 JunhyunBank가 관리 중인 종목을 업비트 앱이나 다른 프로그램에서 수동 매매하지 않는다. 수량 소유권이 모호해지면 자동매도 대신 격리한다.

## 4. V4 버전 계보

### V4.0.0

- 런타임 재시작/시세 진단 보강.
- 동일 5초 수급 비교 수정.
- 후보별 매수 대기 사유와 진입 Q/비용 표시.
- durable `order_intents`, identifier 복구, partial fill atomic accounting.
- 중복주문/사용자 보유분 보호.

### V4.0.1

- updater health verification.
- 새 EXE의 DB migration/`PRAGMA quick_check`/필수 테이블 확인.
- 실패 시 기존 EXE와 업데이트 직전 DB/WAL/SHM snapshot 복원.
- rollback된 이전 버전 자동매매 자동재개 금지.

### V4.0.2

- authenticated Private WebSocket `myOrder` + `myAsset`.
- fresh JWT, ping/reconnect, credential redaction.
- JunhyunBank identifier event가 pending REST reconciliation을 즉시 깨움.
- Private WS 장애 시 REST fallback 유지.
- `myAsset`으로 managed quantity를 추정하지 않음.

### V4.0.3

- Public-only forward-edge validator.
- entry ask → future bid, 양쪽 가정 수수료 포함 label.
- 늦은 horizon quote `missed` 처리.
- BUY/전체 후보 분리, ExpectedMove calibration, label completion.
- chronological holdout의 forward-label overlap purge.

### V4.0.4

- Public-only raw market recorder.
- 전체 안전 KRW trade + LIVE 정합 fresh actionable 후보 L2 orderbook 저장.
- exchange timestamp + local wall/monotonic receive timestamp + process sequence.
- bounded nonblocking queue, crash-safe `.part`, atomic finalize, gzip, retention.

### V4.0.5

- V4.0.4 recording의 deterministic offline replay.
- recorded `received_monotonic_ns` logical clock.
- production 전략은 그대로 두고 replay freshness만 logical clock으로 대체.
- recorded safe KRW universe와 당시 `StrategyConfig` 복원.
- 동일 input/code/config/fee의 canonical decision SHA-256 fingerprint.

### V4.0.6

- Upbit Best+IOC 실제 사양과 live pre-trade liquidity model 정합화.
- `best`는 접수 시 상대 최우선호가를 지정가격으로 사용하고 IOC remainder는 취소된다는 의미를 코드/테스트에 고정.
- BUY current immediate capacity = best ask price × best ask size.
- SELL current immediate capacity = best bid price × best bid size.
- 신규 position의 보수적 dynamic liquidity cap = 두 current top-level notional의 min.
- deeper L2를 current Best+IOC가 worse price로 체결할 수 있는 capacity로 간주하지 않음.
- `execution_model.py`를 current order semantics의 pure source로 추가.
- `live_engine.TradingEngine`이 runtime engine을 상속하고 pre-trade execution helper만 override.
- `main.py`는 `live_engine.TradingEngine`을 실제 production engine으로 사용.
- 주문 타입 자체와 JH-MicroFlow threshold/ExpectedMove/managed quantity/durable reconciliation은 변경하지 않음.

### V4.0.7

- V4.0.0 실사용 캡처에서 확인된 `후보는 있으나 대부분 체결/호가 3초 이상 지연` 형태의 candidate starvation을 재현·보완.
- scanner HotScore freshness 허용폭과 실제 LIVE entry stale gate(기본 3초)의 불일치를 production 후보선정에서 정렬.
- scanner 상위 목록의 stale 비관리 후보를 deep 선정 전에 제외.
- 빈 자리는 top-N 밖의 fresh/warmed 시장을 HotScore 순으로 보충.
- stale 비관리 후보는 30초 deep minimum residency보다 freshness를 우선하여 즉시 퇴출.
- managed position은 stale이어도 exit monitoring을 위해 deep set에 유지.
- GUI 후보 목록과 runtime candidate count를 실제 fresh actionable ranking과 정렬.
- 5분 이상 fresh actionable 후보가 없으면 stale 제외 수를 포함한 별도 진단 경고.
- `ignition_quality`, `pullback_quality`, ExpectedMove 비용 gate, Strategy Health, Market Regime은 완화하지 않음.

### V4.1.0

- 신규진입용 percentile 점수와 보유 중 수급 반전 판단을 분리.
- 복수 방향성 증거와 새 관측을 확인한 뒤 일반 수급 청산.
- 주문 직전 진입조건 재평가.

### V4.2.0

- 증가율이 평평해져도 양의 호가 우위가 지속되는 경우를 진입 근거로 보완.
- 차트 종목 고정, 실제 체결 시간축, 조회 구간, 평균단가와 수신 공백 표시.

### V4.3.0

- 시간·sequence·숫자·호가구조 검증을 통과한 이벤트만 전략과 최신가격에 반영.
- 실제 체결 관측시간과 최소 book 관측수를 워밍업에 포함; reconnect history reset.
- regime coverage 부족 fail-closed, zero-evidence HotScore와 pullback chronology 수정.
- market universe 대표시장/최소크기/급감/경보 schema sanity와 공용 후보 선택기.
- 중복 market의 최악 상태 집계, `caution`/`warning` null unknown 처리, BTC·전체/안전 허용목록 최소 floor.
- ExpectedMove signal 비중 상한과 손절거리·노출·유동성 기반 동적 위험예산.
- 입출금·비관리 자산을 제외한 관리전략 전용 세션 손익 중단조건.
- pending SELL 선조정, 시작 baseline 실패 세션의 신규매수 영구 잠금과 청산 루프 유지.
- 단일 호가 generation의 bid/spread/depth 판단, POST 직전 generation 재확인, bid 1호가 depth 부족 시 전략 PnL 계산 불가.
- 영속 단방향 trailing stop, 최대 보유시간, fresh executable bid 청산가격.
- accounts 반복 누락 격리, dust 보존, order submit/accept 시각 영속화.
- 핵심 회계 손상 격리와 outcome basis 손상의 durable KRW PnL adjustment 원장.
- Best+IOC SMP `cancel_taker`, GET 제한 재시도와 POST no-retry 원칙.
- 정지/POST submission gate, 청산 직전 fresh 정책 재평가, bounded/coalesced UI events.
- recorder/replay의 두 receive clock·exchange lag·거부 이벤트·WS 오류 무결성 진단과 Hot/control 역할 격리 강화. forward validator는 거부 이벤트·잘못된 quote·0 sample·미완료 label·book cap drop도 fail-closed. 단, replay/validator는 아직 체결·청산·PnL simulator가 아님.

## 5. 핵심 소스 구조

| 파일 | 역할 |
|---|---|
| `main.py` | 앱 시작, 실제 `live_engine.TradingEngine` 생성, update verify/resume |
| `ui.py` | PySide6 UI와 사용자 동작 |
| `engine.py` | 기본 거래 lifecycle, portfolio/주문 호출; 과거 generic helper도 남아 있으므로 production semantics는 `live_engine`/`execution_model` 확인 |
| `runtime_engine.py` | market discovery, persistent deep stream, Private stream lifecycle |
| `live_engine.py` | V4.0.6 Best+IOC pre-trade semantics + V4.0.7 fresh actionable candidate/deep-slot selection |
| `execution_model.py` | Best+IOC top-level capacity/fill pure model; 이후 simulator도 재사용 |
| `execution.py` | durable order intent, submit/reconcile/fill 적용 |
| `strategy.py` | JH-MicroFlow feature/HotScore/entry/exit |
| `health.py` | 실제 strategy outcomes 기반 Strategy Health |
| `risk.py` | stale/API/emergency/min-order gate + 계정/손절/노출/유동성 기반 entry budget |
| `storage.py` | SQLite events/trades/managed positions/order intents/outcomes/PnL adjustments + trailing/격리/submit 상태 |
| `market_stream.py` | Public trade/orderbook WebSocket + exchange timestamp 신선도 차단 |
| `private_stream.py` | authenticated myOrder/myAsset WebSocket |
| `upbit.py` | REST/JWT/order API/rate guard; 일반 주문은 Best+IOC |
| `updater.py` | Release 확인, digest, staged verify/rollback |
| `recorder.py` | V4.0.4 bounded crash-safe Public market recorder |
| `replay.py` | V4.0.5 deterministic strategy replay |
| `validation.py` | forward label, Hot/control 역할·통계, 무결성 판정, purged chronological split |
| `security.py` | OS keyring |
| `config.py` | SafetyConfig / StrategyConfig |

## 6. 실제 주문 의미 — 매우 중요

일반 매수:

```text
side=bid, ord_type=best, time_in_force=ioc, price=<KRW amount>
```

일반 매도:

```text
side=ask, ord_type=best, time_in_force=ioc, volume=<coin quantity>
```

Upbit `best`는 주문 접수 시 상대 방향 최우선호가와 같은 가격의 지정가다. IOC는 그 가격조건으로 즉시 체결 가능한 수량만 체결하고 나머지를 취소한다.

따라서 current snapshot에서:

```text
BUY capacity  = best ask price × ask size
SELL capacity = best bid price × bid size
```

더 깊은 L2는 orderbook imbalance, liquidity research, 다른 hypothetical order type 연구에는 유용하지만 **현재 live Best+IOC가 자동으로 worse levels를 걷는다는 의미가 아니다.**

## 7. JH-MicroFlow 현재 전략

- 1초 trade frame.
- Activity: rolling 체결대금 percentile.
- Aggression: BID/ASK 체결대금 불균형 percentile.
- Book: imbalance, microprice bias, imbalance 변화 percentile.
- Momentum: short/long positive-return percentile.
- Quality: 네 factor geometric mean.
- HotScore는 scanner score이고 실제 entry Quality와 다름.
- IGNITION / PULLBACK.
- 비용 gate: bid/ask fee + spread + current execution liquidity.
- 자금배분: signal 상한 뒤 현금·손절거리·단일/총 노출·포트폴리오 위험·current book capacity 예산의 최솟값.
- 청산: 수급 약화, persisted adaptive trailing, entry 시 고정 emergency risk, 기대시간 실패, 최대 보유시간.

현재 가장 중요한 전략 한계:

```text
ExpectedMove ≈ 최근 |30초 수익률| 분포의 70% quantile
```

이는 **조건부 미래 상승 기대수익이 아니라 최근 절대 변동폭 proxy**다. 이를 검증된 edge로 가정해 Kelly/ML sizing을 먼저 붙이면 안 된다.

## 8. 연구/검증 계층

```bash
python scripts/smoke_upbit_public_ws.py --timeout 20 --attempts 3
python scripts/diagnose_public.py
python scripts/validate_public_edge.py --seconds 1800 --output edge-validation.json
python scripts/record_public_market.py --seconds 3600 --output-dir ~/.junhyunbank/recordings
python scripts/replay_strategy.py --input ~/.junhyunbank/recordings --session latest --output replay.json
```

- Public smoke/forward validator/recorder는 API Key와 주문을 사용하지 않는다.
- replay는 네트워크도 사용하지 않는다.
- Private WS 실제 연결은 사용자 계정/IP 권한에 의존하므로 CI에는 실계정 secret을 넣지 않는다.

## 9. 현재 검증 수준

### 코드/배포

PR과 `main`에서 Windows + Ubuntu pytest와 실제 Upbit Public REST/WebSocket read-only smoke를 수행한다. Release workflow는 Windows에서 다시 test/smoke/PyInstaller를 수행하고 package와 같은 semantic version Release를 만든다.

### 실거래 안전성

mock/synthetic exchange에서 timeout, 5xx, 429, partial IOC, nonterminal order, restart recovery, manual holdings 보호, Private event wake-up을 회귀검증한다. 자동 CI는 실제 자금 주문을 하지 않는다.

### 전략 연구/파이프라인

- V4.0.3: forward top-of-book label.
- V4.0.4: raw Public trade/L2 capture.
- V4.0.5: deterministic decision replay.
- V4.0.6: current live Best+IOC execution semantics 정합화.
- V4.0.7: scanner → deep entry 경로를 actual entry freshness와 정렬해 stale-slot starvation 방지.
- V4.3.0: 입력·위험예산·청산상태·replay 무결성을 fail-closed로 강화.

아직 수익성 확정이 아니다. latency-aware fill simulation과 충분한 장기간 purged OOS가 남아 있다.

## 10. 다음 개발 우선순위

1. **Best+IOC Execution Simulator**
   - deterministic replay decision timestamp에서 configurable latency 적용.
   - latency 후 첫 유효 orderbook을 주문 접수시점 book으로 선택.
   - current best opposing price/size만 사용해 full/partial/no-fill.
   - remainder는 IOC cancel.
   - fee/spread/fill ratio/latency/data gap diagnostics.
   - 실제 `execution.py`/Upbit 주문 API를 호출하지 않는 pure research layer.
2. **Purged Walk-forward / Stress Harness**
   - TRAIN → VALIDATE → untouched OOS.
   - forward horizon overlap purge/embargo.
   - fee stress, +100/+250/+500ms latency, liquidity shrink/no-fill stress.
   - 특정 코인/최고 수익일 제거 sensitivity.
3. **Recorder 데이터 축적/품질 점검**
   - 평일/주말, 상승/하락, 저/고변동, 급변 구간.
   - drop/WS gap/session completeness.
4. **Conditional ExpectedMove 후보 모델**
   - 충분한 recorder/OOS 이후에만.
   - current proxy와 동일 OOS에서 비교.
5. **Uncertainty-aware sizing / correlation control**
   - V4.3 safety budget은 유지하고, conditional edge가 OOS에서 확인된 뒤 그 안에서만 비교.

## 11. 이미 해결된 결함을 되돌리지 말 것

- Public WS Origin/연결 재시도.
- deep candidate 변경 socket churn.
- deep 재진입 stale book history.
- `market_event` false caution 때문에 `Trade WS 0/0`이 되던 문제.
- ambiguous POST 재전송/부분체결 추정/계정 총수량 managed 처리.
- 새 EXE health 확인 전 old executable 삭제.
- Private account event 미사용.
- **Best+IOC를 multi-level depth-walk로 모델링하던 오류.**
- 거부된 과거 이벤트가 최신 가격/freshness를 덮는 오류.
- 희박한 체결 사이 0 frame만으로 워밍업이 끝나는 오류.
- 추적 손절가가 다시 낮아지거나 일시적 잔고 누락으로 managed state가 사라지는 오류.
- **stale high-HotScore 후보를 deep minimum residency로 붙잡아 fresh lower-rank 후보를 굶기던 오류.**

관련 회귀테스트를 삭제하거나 단순화하지 않는다.

## 12. 하지 말아야 할 것

- 거래가 적다는 이유로 entry quality/cost threshold를 바로 낮추지 않는다.
- ExpectedMove를 검증된 future return prediction이라고 부르지 않는다.
- 한 데이터 구간에서 threshold를 최적화하고 같은 구간 결과를 OOS라고 하지 않는다.
- random shuffle 시계열 split을 하지 않는다.
- Best+IOC current execution을 generic market depth walk로 시뮬레이션하지 않는다.
- stale scanner 후보를 실제 entry candidate와 같은 것으로 표시하거나 scarce deep slot에 오래 유지하지 않는다.
- Private WS만 보고 체결 회계를 확정하지 않는다.
- balance 변화만으로 managed quantity를 재구성하지 않는다.
- pending order를 자동 삭제/재제출하지 않는다.
- 회귀테스트/public smoke가 실패한 상태에서 merge/release하지 않는다.

## 13. 완료 정의

후속 PR은 코드 작성만으로 완료가 아니다.

- 관련 unit/regression test.
- Windows + Ubuntu CI.
- 실제 Public REST/WebSocket read-only smoke.
- 거래 경로 변경이면 payload/order semantics/restart/partial fill/managed quantity 회귀검증.
- 문서/CHANGELOG 동시 갱신.
- `main` 병합 후 Windows Release workflow와 실제 `JunhyunBank.exe` asset 확인.

전략 개선을 live에 반영하려면 여기에 **충분한 recorder 데이터 + purged OOS + realistic Best+IOC latency/fill stress + 특정 코인/날짜 의존성 점검**이 추가로 필요하다.
