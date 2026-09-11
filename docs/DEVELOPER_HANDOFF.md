# JunhyunBank 개발 인수인계 — V4.0.5 기준

이 문서는 이전 대화가 없어도 새 개발자 또는 새 ChatGPT가 제품 의도, 안전 불변조건, 현재 구현과 다음 작업을 바로 이어가기 위한 최우선 인수인계 문서다.

코드와 문서가 충돌하면 **현재 `main` 코드가 source of truth**다. 주문/복구는 `V4_AUDIT.md`, 업데이트는 `V4_0_1_UPDATE_RECOVERY.md`, Private reconciliation은 `V4_0_2_PRIVATE_RECONCILIATION.md`, 전략 검증 계보는 V4.0.3~V4.0.5 문서를 함께 읽는다.

## 1. 제품 정의

JunhyunBank는 Upbit KRW 마켓을 24시간 실시간 감시하고 체결·호가 기반 단기 수급 신호를 찾아 **실제 원화로 자동 매수·매도하는 Windows LIVE 전용 데스크톱 프로그램**이다.

PAPER 모드는 V2부터 제거됐다. 앱 `시작`은 실제 주문을 허용한다. 반대로 `scripts/validate_public_edge.py`, `record_public_market.py`, `replay_strategy.py`는 연구/검증 도구이며 실거래 경로와 분리한다.

## 2. 반드시 유지할 사용자 요구

- LIVE 전용 자동매매.
- 시장 전체를 지속 감시하고 종목·진입·청산을 프로그램이 스스로 결정.
- 핵심은 짧은 수급/호가 기반 단타.
- `시간당 N회`, `1회 최대 N원`, `최대 N종목` 같은 임의의 고정 전략 cap을 추가하지 않음.
- 신호 품질, market regime, 실제 거래비용, 호가 유동성, Strategy Health로 거래 여부와 자금배분을 동적으로 결정.
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
5. `managed_quantity`가 0/NULL이면 계정 총잔고를 대신 넣어 추정 관리하지 않는다.
6. 주문 전 unique `identifier`를 SQLite `order_intents`에 먼저 영속화한다.
7. timeout/5xx/응답 유실 같은 ambiguous POST 결과에서 동일 주문을 재전송하지 않고 identifier로 reconciliation한다.
8. terminal 확인 전 동일 시장의 중복 주문을 막고 pending 주문이 있으면 신규매수를 차단한다.
9. partial fill은 실제 체결의 수량·금액·수수료만 원자적으로 반영한다.
10. Private `myOrder`/`myAsset`은 빠른 변화 신호일 뿐 회계 정본이 아니다. identifier REST 조회가 정본이다.
11. stale trade/orderbook, 반복 API 오류, 경보/해석불가 시장에서는 신규매수하지 않는다.
12. 이미 관리 중인 포지션은 시장 경보가 생겨도 감시/청산 경로를 유지한다.
13. NaN/inf, 최소주문 미달, 비정상 API 응답에서 신규주문을 fail-closed 한다.
14. 업데이트는 새 EXE/DB 사전검증 성공 뒤에만 정상 재개하고 실패하면 EXE+DB snapshot을 함께 rollback한다.
15. 거래가 적다는 이유로 전략 threshold나 비용 gate를 임의 완화하지 않는다. recorder/OOS 근거가 먼저다.

## 4. V4 버전 계보

### V4.0.0

- 런타임 재시작/시세 진단 보강.
- 동일 5초 수급 비교 수정.
- 후보별 매수 대기 사유와 진입 Q/비용 표시.
- durable `order_intents`, identifier 복구, partial fill atomic accounting.
- 중복주문/사용자 보유분 보호.

### V4.0.1

- updater를 health-verified transaction으로 변경.
- 새 EXE의 DB migration/`PRAGMA quick_check`/필수 테이블 확인.
- 실패 시 기존 EXE와 업데이트 직전 DB/WAL/SHM snapshot 복원.
- rollback된 이전 버전은 자동매매 자동재개 금지.

### V4.0.2

- authenticated Private WebSocket `myOrder` + `myAsset` 추가.
- fresh JWT, ping/reconnect, credential redaction.
- `junhyunbank-` identifier 이벤트만 REST reconciliation을 즉시 깨움.
- Private WS 장애 시 REST fallback 유지.
- pending REST 조회에 backoff, myOrder wake-up은 backoff 우회.
- `myAsset`으로 managed quantity를 추정하지 않음.

### V4.0.3

- Public-only forward-edge validator.
- entry ask → future bid, 양쪽 가정 수수료 포함 label.
- 늦은 horizon quote는 `missed` 처리.
- BUY/전체 후보 분리, ExpectedMove calibration, label completion 기록.
- 시간순 holdout의 forward-label overlap purge.
- 실제 주문 size/depth/latency는 아직 미포함.

### V4.0.4

- Public-only raw market recorder.
- 전체 안전 KRW trade + 현재 Hot 후보 L2 orderbook 저장.
- exchange timestamp + local wall/monotonic receive timestamp + process sequence 저장.
- WebSocket callback은 bounded queue에 nonblocking enqueue만 수행.
- disk I/O/fsync/rotation/gzip은 background worker.
- `.jsonl.part` crash recovery, atomic finalize, retention, gzip reader.
- live engine에는 아직 recorder를 주입하지 않고 별도 연구 명령으로 분리.

### V4.0.5

- V4.0.4 recording을 network/order 없이 재생하는 deterministic replay.
- recorded `received_monotonic_ns` 기반 logical freshness clock.
- 작은 cross-thread receive-time 역전은 clock을 뒤로 돌리지 않고 clamp/diagnostic.
- production `MicroFlowStrategy`는 그대로 두고 replay subclass가 receive freshness만 대체.
- recorded safe KRW universe와 당시 `StrategyConfig` 복원.
- orderbook 시점 feature/entry decision/regime/top-of-book snapshot 생성.
- 동일 recording + 동일 코드/config/fee의 canonical decision SHA-256 fingerprint.
- interrupted session은 조사 가능하지만 완전한 dataset으로 자동 승격되지 않게 nonzero CLI exit.
- execution simulation이나 수익성 확정 기능은 아님.

## 5. 핵심 소스 구조

| 파일 | 역할 |
|---|---|
| `main.py` | 앱 시작, single instance, update verification/resume |
| `ui.py` | PySide6 UI와 사용자 동작 |
| `engine.py` | 기본 거래 lifecycle, portfolio/주문 호출 |
| `runtime_engine.py` | market discovery, persistent deep stream, Private stream lifecycle |
| `execution.py` | durable order intent, submit/reconcile/fill 적용 |
| `strategy.py` | JH-MicroFlow feature/HotScore/entry/exit |
| `health.py` | 실제 strategy outcomes 기반 Strategy Health |
| `risk.py` | stale/API/emergency/min-order 안전조건 |
| `storage.py` | SQLite events/trades/managed positions/order intents/outcomes |
| `market_stream.py` | Public trade/orderbook WebSocket |
| `private_stream.py` | authenticated myOrder/myAsset WebSocket |
| `upbit.py` | Public/private REST, JWT, order API, rate guard |
| `updater.py` | Release 확인, digest, staged verify/rollback |
| `recorder.py` | V4.0.4 bounded crash-safe Public market recorder |
| `replay.py` | V4.0.5 recorder session/logical clock/deterministic decision replay |
| `validation.py` | forward label, 통계, purged chronological split |
| `security.py` | OS keyring |
| `config.py` | SafetyConfig / StrategyConfig |

연구/검증 명령:

```bash
python scripts/smoke_upbit_public_ws.py --timeout 20 --attempts 3
python scripts/diagnose_public.py
python scripts/validate_public_edge.py --seconds 1800 --output edge-validation.json
python scripts/record_public_market.py --seconds 3600 --output-dir ~/.junhyunbank/recordings
python scripts/replay_strategy.py --input ~/.junhyunbank/recordings --session latest --output replay.json
```

Private WebSocket smoke는 로컬 계정 권한/IP 설정에 의존하므로 CI secret을 두지 않는다.

## 6. JH-MicroFlow 현재 전략

- 1초 trade frame.
- Activity: rolling 체결대금 percentile.
- Aggression: BID/ASK 체결대금 불균형 percentile.
- Book: imbalance, microprice bias, imbalance 변화 percentile.
- Momentum: short/long positive-return percentile.
- Quality: 네 factor geometric mean.
- HotScore는 deep 후보를 고르는 scanner score이며 실제 entry Quality와 다름.
- IGNITION / PULLBACK 분류.
- 비용 gate: bid/ask fee + spread + 예상 slippage.
- 자금배분: edge × quality × health × regime factor + 실제 호가 capacity.
- 청산: 수급 약화, adaptive trailing, entry 시 고정 emergency risk, 기대시간 실패.

가장 중요한 전략 한계:

```text
ExpectedMove ≈ 최근 |30초 수익률| 분포의 70% quantile
```

이는 **조건부 미래 상승 기대수익이 아니라 최근 절대 변동폭 proxy**다. 이를 진짜 edge로 가정해 Kelly/ML sizing을 먼저 붙이면 과최적화 위험이 크다.

## 7. 이미 해결된 결함을 되돌리지 말 것

- Public WS Origin/연결 재시도 문제.
- deep 후보 변경 때마다 socket 전체 재생성하던 churn.
- deep 재진입 종목의 stale book history 재사용.
- Upbit `market_event` false caution을 warning으로 오판해 `Trade WS 0/0`이 되던 문제.
- ambiguous POST 재전송/부분체결 추정/계정 총수량 managed 처리.
- 새 EXE health 확인 전 old 실행파일을 삭제하던 updater.
- Private account 이벤트 미사용.

관련 회귀테스트를 삭제하거나 단순화하지 않는다.

## 8. 현재 검증 수준

### 코드/배포

PR과 `main`에서 Windows + Ubuntu pytest, 실제 Upbit Public REST/WebSocket read-only smoke를 수행한다. Release workflow는 Windows에서 다시 테스트/smoke 후 PyInstaller EXE와 semantic `V4.0.x` Release를 만든다.

### 실거래 안전성

mock/synthetic exchange에서 timeout, 5xx, 429, partial IOC, nonterminal order, restart recovery, manual holdings 보호, Private event wake-up 등을 회귀검증한다. CI는 실제 자금 주문을 하지 않는다.

### 전략 연구

- V4.0.3: 현재 신호의 미래 top-of-book forward label.
- V4.0.4: 실제 Public raw trade/L2 저장.
- V4.0.5: 동일 raw path의 deterministic decision replay.

여기까지는 수익성 확정이 아니다. execution realism과 장기간 OOS가 아직 남아 있다.

## 9. 다음 개발 우선순위

### P0 — 계속 회귀 방어

모든 후속 PR에서 durable order state, managed quantity, Private→REST reconciliation, stale gates, updater rollback을 보존한다. 거래 경로를 바꾸는 변경은 별도 작은 PR과 failure/restart 테스트가 먼저다.

### P1 — 다음 실제 작업

1. **Execution Simulator**
   - recorder 당시 L2에서 요청 size별 ask/bid depth walk.
   - spread, fee, price impact.
   - configurable execution delay +100/+250/+500ms.
   - IOC partial/no fill 모델과 fill diagnostics.
   - 실제 주문 코드를 호출하지 않는 pure research layer.
2. **Purged Walk-forward / Stress Harness**
   - TRAIN → VALIDATE → OOS 시간순 분리.
   - forward horizon overlap purge/embargo.
   - fee 1.0/1.25/1.5×, delay/slippage stress.
   - 특정 코인/최고 수익일 제거 sensitivity.
3. **Recorder 데이터 축적/품질 점검**
   - 평일/주말, 저변동/고변동, 상승/하락/급변 구간 확보.
   - drop/WS gap/session completeness를 먼저 점검.
4. **Conditional ExpectedMove 후보 모델**
   - 충분한 recorder/OOS 이후에만 구현.
   - 현 proxy와 같은 OOS 구간에서 비교.
5. **Uncertainty-aware sizing / correlation control**
   - 4의 OOS edge가 확인된 뒤 bootstrap lower bound/damped Kelly류 검토.
   - 임의 고정 KRW cap이 아니라 데이터 기반 risk budget이어야 함.

## 10. Replay 해석 규칙

V4.0.5 replay의 `decision_fingerprint_sha256`는 전략 재현성 지표지 수익성 지표가 아니다. code/config/fee/evaluation cadence를 바꾸면 fingerprint가 달라질 수 있다.

`complete_session=false`, recorder drop, websocket gap 또는 부족한 L2 coverage가 있는 데이터는 연구 결과를 채택하는 근거로 사용하지 않는다. recorder는 당시 Hot 후보에 대해서만 L2를 저장하므로 모든 시장의 execution을 완벽히 재구성한다고 가정하지 않는다.

## 11. 하지 말아야 할 것

- 거래가 적다는 이유로 `ignition_quality`, `pullback_quality`, 비용 배수를 바로 낮추지 않는다.
- ExpectedMove를 검증된 미래수익 예측값처럼 부르지 않는다.
- 한 구간에서 threshold를 최적화하고 같은 구간 성과를 OOS라고 하지 않는다.
- 시장 시계열을 random shuffle split하지 않는다.
- Private WS만 보고 체결 회계를 확정하지 않는다.
- balance 변화만으로 managed quantity를 재구성하지 않는다.
- pending order를 자동 삭제/재제출하지 않는다.
- recorder/replay 데이터를 실제 주문 경로로 무심코 연결하지 않는다.
- 회귀테스트/공개 smoke가 실패한 상태에서 merge/release하지 않는다.

## 12. 완료 정의

후속 PR은 코드 작성만으로 완료가 아니다.

- 관련 unit/regression test.
- Windows + Ubuntu CI.
- 실제 Public REST/WebSocket smoke.
- 거래 경로 변경이면 restart/ambiguous response/partial fill/managed quantity 회귀검증.
- 문서/CHANGELOG 동시 갱신.
- `main` 병합 후 Windows Release workflow와 실제 `JunhyunBank.exe` asset 확인.

전략 개선을 실제 live에 반영하려면 여기에 **충분한 recorder 데이터 + purged OOS + 비용/지연/slippage stress + 특정 코인/날짜 의존성 점검**이 추가로 필요하다.