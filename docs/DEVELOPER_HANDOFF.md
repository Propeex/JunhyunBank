# JunhyunBank 개발 인수인계 — V4.0.3 기준

이 문서는 이전 대화가 모두 사라져도 새 개발자 또는 새 ChatGPT가 제품의 의도, 안전 불변조건, 현재 구현과 다음 작업을 바로 이어갈 수 있도록 유지하는 최우선 인수인계 문서다.

코드와 문서가 충돌하면 **현재 `main` 코드가 source of truth**다. V4의 주문/복구는 `V4_AUDIT.md`, 업데이트는 `V4_0_1_UPDATE_RECOVERY.md`, Private account reconciliation은 `V4_0_2_PRIVATE_RECONCILIATION.md`, 전략 검증은 `V4_0_3_EDGE_VALIDATION.md`를 함께 읽는다.

## 1. 제품 한 줄 정의

JunhyunBank는 Upbit KRW 마켓을 24시간 실시간 감시하고 체결/호가 기반 단기 수급 신호를 찾아 **실제 원화로 자동 매수·매도하는 Windows LIVE 전용 데스크톱 프로그램**이다.

모의매매(PAPER) 모드는 V2부터 제거됐다. 앱의 `시작`은 실제 주문을 허용하므로 테스트/진단 도구와 실거래 경로를 명확히 분리해야 한다.

## 2. 반드시 유지할 사용자 요구

- LIVE 전용 자동매매.
- 시장 전체를 지속 감시하고 종목·진입·청산을 프로그램이 스스로 결정.
- 핵심은 짧은 수급/호가 기반 단타.
- `시간당 N회`, `1회 최대 N원`, `최대 N종목` 같은 임의의 고정 전략 cap을 추가하지 않음.
- 대신 신호 품질, 시장 regime, 실제 거래비용, 호가 유동성, Strategy Health를 이용해 동적으로 거래 여부와 자금배분을 결정.
- 사용자가 프로그램 실행 전부터 보유한 코인을 JunhyunBank가 임의로 매도하지 않음.
- 일반 `종료`는 신규매수만 막고 관리 포지션을 전략대로 청산한 후 종료하는 DRAINING.
- `긴급 정지`는 즉시 전략/신규주문을 멈추지만 보유자산 강제 시장가 청산 기능이 아님.
- 보유자산 UI에 KRW 표시.
- GitHub Release 기반 `업데이트` 버튼과 자동 재시작.
- 업데이트 후 API Key와 SQLite 관리상태 유지.

## 3. 절대 깨면 안 되는 안전 불변조건

1. 출금 API를 구현하지 않는다.
2. Access Key / Secret Key / JWT를 GitHub, SQLite, 로그에 저장하지 않는다.
3. API Key는 OS keyring에만 저장한다.
4. 자동매도는 JunhyunBank가 실제 체결로 확보한 `managed_quantity` 범위에만 적용한다.
5. `managed_quantity`가 0/NULL이면 계정 총잔고를 대신 넣어 추정 매도하지 않는다.
6. 주문 전 unique `identifier`를 SQLite `order_intents`에 먼저 영속화한다.
7. timeout/5xx/응답 유실 같은 ambiguous POST 결과에서 같은 주문을 재전송하지 않는다. identifier로 reconciliation한다.
8. terminal 체결 확인 전 동일 시장의 중복 주문을 막고 pending이 있으면 신규매수를 차단한다.
9. partial fill은 실제 trades의 수량·금액·수수료만 원자적으로 반영한다.
10. Private `myOrder`/`myAsset`은 빠른 변화 신호일 뿐 회계 정본이 아니다. 체결 정본은 identifier REST 조회다.
11. public/private WebSocket이 끊겨도 무조건 거래를 계속하지 않는다. stale trade/orderbook에서는 신규매수 금지.
12. 경보 또는 해석 불가능한 시장은 신규진입에서 fail-closed 한다. 이미 관리 중인 포지션의 감시/청산은 유지한다.
13. 반복 API 오류, 최소 주문금액 미달, 비정상 NaN/inf 입력에서 신규주문을 막는다.
14. 업데이트는 새 EXE/DB 사전검증이 성공한 뒤에만 정상 재개하고 실패하면 EXE+DB snapshot을 함께 rollback한다.
15. 검증을 이유로 실제 전략 threshold를 임의 완화하지 않는다. 데이터와 OOS 근거가 먼저다.

## 4. 현재 버전 계보

### V4.0.0

- 런타임 재시작/시세 진단 보강.
- 동일 5초 수급 비교 수정.
- 종목별 매수 대기 사유와 진입 Q/비용 표시.
- durable `order_intents`, identifier 복구, partial fill atomic accounting.
- 중복주문/사용자 보유분 보호.

### V4.0.1

- updater를 복구 가능한 트랜잭션으로 변경.
- 새 EXE를 `--post-update-verify` 비거래 모드로 실행해 DB migration/`PRAGMA quick_check`/필수 테이블 확인.
- 실패 시 기존 EXE와 업데이트 직전 DB/WAL/SHM snapshot 복구.
- rollback된 이전 버전의 자동매매 자동재개 금지.

### V4.0.2

- authenticated Private WebSocket `myOrder` + `myAsset` 한 연결 추가.
- 매 연결 fresh JWT, 장시간 무이벤트 ping/reconnect, credential redaction.
- `junhyunbank-` identifier 이벤트만 REST reconciliation을 즉시 깨움.
- Private WS 장애 시 기존 REST fallback 유지.
- 반복 REST pending 조회에 exponential backoff.
- `myAsset`으로 managed quantity를 추정하지 않음.

### V4.0.3

- 실거래 threshold를 건드리지 않는 read-only forward-edge validator 추가.
- Public trade/orderbook으로 실제 `MicroFlowStrategy` 워밍업.
- 후보 시점 entry ask → horizon 후 future bid 기준, 양쪽 가정 수수료 포함 net return label.
- label 지연창을 넘긴 호가는 `missed` 처리해 잘못된 horizon 가격 사용 금지.
- stale 3초 gate 및 deep 재진입 orderbook reset을 live runtime과 맞춤.
- BUY/전체 후보 분리, ExpectedMove calibration, label completion 측정.
- 시간순 holdout에서 forward horizon과 겹치는 training label purge.
- **아직 depth slippage/실제 주문 latency를 포함한 완전한 OOS 수익성 검증은 아님.**

## 5. 현재 소스 구조

핵심은 `src/junhyunbank/`다.

| 파일 | 역할 |
|---|---|
| `main.py` | 앱 시작, single instance, update verification/resume 처리 |
| `ui.py` | PySide6 UI, 시작/종료/긴급정지/업데이트/진단 표시 |
| `engine.py` | 기본 거래 lifecycle, 시장/포트폴리오 평가, 주문 호출 |
| `runtime_engine.py` | V3/V4 runtime hardening, market discovery, deep stream 재사용, Private stream lifecycle |
| `execution.py` | durable order intent, 주문 제출, identifier reconciliation, 체결 적용 |
| `strategy.py` | JH-MicroFlow 특징, HotScore, entry/exit 판단 |
| `health.py` | 실제 strategy outcomes 기반 Strategy Health |
| `risk.py` | stale/API/emergency/min-order 시스템 안전조건 |
| `storage.py` | SQLite events/trades/managed positions/order intents/outcomes |
| `market_stream.py` | Public trade/orderbook WebSocket |
| `private_stream.py` | authenticated `myOrder`/`myAsset` WebSocket |
| `upbit.py` | Public/private REST, JWT, order API, rate guard |
| `updater.py` | Release 확인, digest, staged update, verify/rollback |
| `security.py` | OS keyring |
| `validation.py` | forward label/통계/purged chronological split |
| `config.py` | SafetyConfig / StrategyConfig |

연구/검증 스크립트:

- `scripts/smoke_upbit_public_ws.py`: 주문 없는 Public REST/WS smoke.
- `scripts/diagnose_public.py`: 주문 없는 기존 전략 대기 이유 관측.
- `scripts/smoke_private_ws.py`: 로컬 API Key가 있는 환경에서 주문 없이 private subscription 확인용. CI secret을 요구하지 않는다.
- `scripts/validate_public_edge.py`: V4.0.3 forward-edge shadow dataset 생성. API Key를 읽지 않는다.

## 6. JH-MicroFlow 현재 전략

전략은 각 코인의 절대 수치가 아니라 자기 최근 상태 대비 상대적 이상현상을 사용한다.

- 1초 trade frame.
- Activity: rolling 체결대금 percentile.
- Aggression: BID/ASK 체결대금 불균형의 rolling percentile.
- Book: orderbook imbalance, microprice bias, imbalance 변화 percentile.
- Momentum: short/long return percentile.
- Quality: 네 factor의 geometric mean.
- HotScore: deep 분석 후보를 고르는 스캐너 점수이며 **실제 진입 Q와 다름**.
- IGNITION / PULLBACK 분류.
- 비용 gate: 실제 계정 bid/ask fee + spread + 호가 기반 예상 slippage.
- 자금배분: edge × quality × health × regime factor에 실제 호가 capacity를 추가 적용.
- 청산: 수급 약화, adaptive trailing, entry 시 고정한 emergency risk, 기대시간 실패.

현재 가장 중요한 전략 한계는 `ExpectedMove`다.

```text
ExpectedMove ≈ 최근 |30초 수익률| 분포의 70% quantile
```

이는 **조건부 미래 상승 기대수익이 아니라 최근 절대 변동폭 proxy**다. 따라서 복잡한 Kelly/ML 모델을 붙이기 전에 실제 forward label과 replay 데이터를 먼저 확보해야 한다.

## 7. 이미 해결된 과거 결함을 다시 구현하지 말 것

- Public WS `Origin` 문제와 연결 재시도.
- deep 후보 변경마다 socket을 새로 여는 churn: live runtime은 기존 socket subscription을 갱신한다.
- 신규 deep 종목 stale book history 재사용.
- `market_event` 문자열 `"false"`를 truthy로 오판하는 문제.
- `Trade WS 0/0` 상태의 조용한 실패.
- timeout 주문 재전송/부분체결 추정/전체 계정수량 managed 처리.
- updater가 새 EXE health 확인 없이 `.old`를 삭제하던 문제.
- Private account 이벤트 미사용.

관련 회귀테스트를 삭제하거나 단순화하지 않는다.

## 8. 검증 수준

### 코드/배포

PR과 `main`에서 Windows + Ubuntu pytest를 실행하고, 실제 Upbit Public REST/WebSocket read-only smoke를 수행한다. `main` release workflow는 Windows EXE를 빌드한 뒤 package version에 맞는 `V4.0.x` Release를 만든다.

### 실거래 안전성

합성/mocked 거래소에서 timeout, 5xx, 429, partial IOC, nonterminal order, restart recovery, manual holdings 보호, private event wake-up 등을 회귀검증한다. 실제 자금을 사용하는 자동 CI 주문은 하지 않는다.

### 전략 수익성

아직 완료되지 않았다. V4.0.3은 첫 forward-edge 계측 기반일 뿐이다. 한두 번의 BUY 또는 짧은 세션을 근거로 수익성을 선언하거나 threshold를 변경하지 않는다.

## 9. 다음 개발 우선순위

### P0 — 유지/회귀 방어

현재 durable 주문/managed quantity/updater/private reconciliation 불변조건을 모든 후속 PR에서 보존한다. 새 기능이 이 경로를 침범하면 작은 PR로 분리하고 failure/restart 테스트를 먼저 작성한다.

### P1 — 다음 실제 작업

1. **Raw Market Data Recorder**
   - 전체/선별 trade event, L2 snapshot, exchange timestamp, local receive timestamp 저장.
   - 파일 회전, 압축, 용량 상한, crash-safe write.
   - 실거래 엔진을 지연시키지 않도록 bounded queue/drop diagnostics 필요.
2. **Deterministic Replay Engine**
   - recorder 데이터를 동일 시간순으로 `MicroFlowStrategy`에 재생.
   - wall-clock과 monotonic 의존을 injectable clock으로 분리.
   - 동일 입력 → 동일 특징/decision을 보장하는 regression fixture.
3. **Purged Walk-forward / Stress Harness**
   - TRAIN → VALIDATE → OOS 시간순 분리.
   - fee 1.0/1.25/1.5×, delay +100/+250/+500ms, slippage 악화.
   - 특정 코인/최고 수익일 제거 sensitivity.
4. **Conditional ExpectedMove 후보 모델**
   - 충분한 데이터 이후에만 구현.
   - 현재 proxy와 새 estimator를 같은 OOS 구간에서 비교.
5. **Uncertainty-aware sizing / correlation control**
   - bootstrap lower bound 또는 damped Kelly류는 4의 OOS edge가 확인된 뒤 진행.
   - 임의 고정 KRW cap을 대체하는 데이터 기반 risk budget이어야 함.

### 별도 운영 검증

Private authenticated WebSocket은 CI에 실계정 secret을 넣지 않는다. 실제 설치 환경에서 **주문 없이** 연결/구독만 확인할 수 있으나, API Key 권한과 IP 등록 상태는 사용자 환경에 의존한다.

## 10. V4.0.3 validator 해석 규칙

실행 예:

```bash
python scripts/validate_public_edge.py \
  --seconds 1800 \
  --horizons 30,60,120,300 \
  --sample-every 10 \
  --output edge-validation.json
```

반드시 확인할 필드:

- `run.orders_submitted == 0`
- `run.websocket_errors`
- horizon별 `label_completion_rate` / `missed_label_count`
- `buy_sample_count`
- `buy.mean_net_return`, `buy.positive_net_rate`
- `train_buy`와 `holdout_buy` 차이
- `purged_from_train_count`
- ExpectedMove coverage/correlation/observed-to-expected ratio

표본이 적거나 label completion이 낮으면 성과 해석보다 수집 품질을 먼저 해결한다.

## 11. 후속 개발자가 하지 말아야 할 것

- 거래가 적다는 이유만으로 `ignition_quality`, `pullback_quality`, 비용 배수를 바로 낮추지 않는다.
- `ExpectedMove`를 이미 검증된 미래수익 예측값처럼 사용하지 않는다.
- backtest 전체기간 하나에서 threshold를 최적화한 뒤 같은 기간 성과를 OOS라고 부르지 않는다.
- random shuffle로 시장시계열 train/test를 나누지 않는다.
- Private WS 이벤트만 보고 체결 회계를 확정하지 않는다.
- 계정 balance 변화만으로 JunhyunBank managed quantity를 재구성하지 않는다.
- pending order를 자동 삭제하거나 같은 주문을 재제출하지 않는다.
- 회귀테스트/공개 smoke가 실패한 상태에서 merge/release하지 않는다.

## 12. 완료 정의

후속 PR의 완료는 코드 작성으로 끝나지 않는다.

- 관련 unit/regression test 통과.
- Windows + Ubuntu CI 통과.
- 실제 Public REST/WebSocket smoke 통과.
- 거래 경로 변경이라면 restart/ambiguous response/partial fill/managed quantity 회귀검증.
- 문서와 CHANGELOG 동시 갱신.
- `main` 병합 후 Windows Release workflow와 실제 `JunhyunBank.exe` asset 생성 확인.

전략 개선은 여기에 더해 충분한 recorder 데이터, purged OOS, 비용/지연/slippage stress를 통과해야 한다.
