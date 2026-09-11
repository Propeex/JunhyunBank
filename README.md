# JunhyunBank V4.1.0

## V4.1.0 — 매수 직후 불필요한 청산 방지

신규 진입용 점수를 보유 판단에 재사용하던 구조를 수정했습니다. 상승이 잠깐 쉬는 것과 실제 수급 반전을 구분하며, 일반 수급 청산은 여러 새 관측으로 반전을 확인합니다. 급락 손절과 이익 보호용 추적 청산은 별도로 작동합니다. 주문 직전에는 매수 신호를 다시 확인합니다.

화면에 보유 경과시간과 청산 판단 근거를 표시합니다. 진입 품질·거래비용·데이터 안전 기준은 유지합니다. 상세 검증과 한계는 [V4.1 보유 전략 보완](docs/V4_1_POSITION_POLICY.md)을 확인하세요.

Upbit KRW 마켓을 24시간 감시하고 체결·호가 기반 단기 수급 신호로 실제 원화 주문을 수행하는 **Windows LIVE 전용** 자동매매 프로그램입니다.

> `시작` 버튼은 실제 주문을 허용합니다. PAPER 모드는 V2부터 제거됐습니다. API Key에는 출금 권한을 부여하지 마세요.

## V4.0.7 — 신선 후보 우선 진입 파이프라인

V4.0.7은 V4.0.0 실사용에서 보였던 **후보는 많이 잡히는데 종목별 진입 진단이 `체결/호가 수신 대기 또는 3초 이상 지연`으로 반복되는 현상**을 재발 방지합니다.

기존 scanner의 HotScore 신선도 허용폭은 실제 LIVE 신규매수 stale gate(기본 3초)보다 넓었습니다. 그래서 몇 초 전에 강한 수급이 있었던 시장이 높은 HotScore로 상위 후보와 deep orderbook 슬롯을 차지하면서도, 실제 진입평가에서는 이미 stale로 탈락할 수 있었습니다. 30초 minimum deep residency까지 겹치면 더 아래 순위의 방금 체결된 fresh 시장이 정밀분석 기회를 얻지 못하는 candidate starvation이 생길 수 있었습니다.

V4.0.7은 stale 비관리 후보를 deep 선정 전에 제거하고, top-N 밖의 fresh/warmed 시장을 HotScore 순으로 보충합니다. stale 후보는 30초 residency보다 신선도 조건을 우선해 퇴출하지만, **이미 JunhyunBank가 관리하는 포지션은 청산 감시를 위해 예외적으로 deep monitoring에 계속 유지**합니다. 화면 후보와 runtime candidate count도 실제 fresh actionable 후보에 맞춥니다.

이 변경은 아무 코인이나 사게 만드는 것이 아닙니다. IGNITION/PULLBACK 품질, ExpectedMove 대비 비용 gate, Strategy Health, Market Regime, stale 주문차단은 그대로입니다. 즉 **매수 심사 자체에 도달할 수 없는 stale 후보가 슬롯을 독점하는 문제만 제거**합니다.

상세: **[V4.0.7 Fresh Candidate Pipeline](docs/V4_0_7_FRESH_CANDIDATE_PIPELINE.md)**

## V4.0.6 — Best+IOC 실행 모델 정합화

V4.0.6은 execution simulator 설계 과정에서 발견한 **실제 Upbit 주문 의미와 기존 pre-trade 유동성 모델의 불일치**를 수정합니다.

JunhyunBank의 일반 주문은 `ord_type=best` + `time_in_force=ioc`입니다. Upbit 최유리 지정가는 주문 접수 시 상대 방향 최우선호가를 주문 가격으로 사용하고, IOC는 그 가격조건에서 즉시 체결 가능한 수량만 체결한 뒤 remainder를 취소합니다. 따라서 현재 snapshot에서 worse 2·3호가까지 시장가처럼 depth-walk한다고 가정하면 안 됩니다.

V4.0.6부터 production `live_engine.TradingEngine`은 BUY는 best ask, SELL은 best bid의 current top-level capacity만 즉시 체결 가능으로 계산합니다. 신규 position의 동적 liquidity cap은 current best ask/bid 양방향 notional 중 작은 값입니다. 이는 임의 고정 KRW 제한이 아니라 현재 호가에서 직접 계산되는 유동성 제한입니다.

실제 REST 주문 타입, JH-MicroFlow threshold, ExpectedMove, durable order state, managed quantity는 바꾸지 않습니다. 주문 전송 사이에 호가가 변할 수 있으므로 실제 partial/no fill은 계속 identifier REST reconciliation으로 확정합니다.

상세: **[V4.0.6 Best+IOC Execution Model](docs/V4_0_6_BEST_IOC_EXECUTION_MODEL.md)**

## V4.0.5 — Deterministic strategy replay

V4.0.5는 V4.0.4에서 저장한 raw trade/L2 recording을 **네트워크와 실제 주문 없이** 현재 JH-MicroFlow에 재생하는 offline 검증 계층입니다.

`scripts/replay_strategy.py`는 recorder의 `received_monotonic_ns`를 logical clock으로 사용해 실행 PC 속도와 현재 시각에 영향을 받지 않게 freshness를 재현합니다. 당시 `StrategyConfig`와 안전 KRW universe를 복원하고, orderbook 시점의 feature·entry decision·regime·top-of-book을 기록합니다. 동일 recording, 동일 전략 코드/config/fee이면 canonical decision SHA-256 fingerprint가 동일해야 합니다.

상세: **[V4.0.5 Deterministic Strategy Replay](docs/V4_0_5_DETERMINISTIC_REPLAY.md)**

## V4.0.4 — Raw market data recorder

`scripts/record_public_market.py`는 API Key 없이 Public REST/WebSocket만 사용해 전체 안전 KRW trade와 현재 Hot 후보의 L2 orderbook을 JSONL로 기록합니다.

WebSocket callback에서는 bounded queue에 비차단 enqueue만 하고 파일 쓰기·fsync·rotation·gzip은 background worker가 담당합니다. queue 포화는 시세 callback을 막지 않고 drop 통계와 exit code로 드러내며, `.jsonl.part`는 crash 후 마지막 완전한 line까지 복구합니다.

상세: **[V4.0.4 Raw Market Data Recorder](docs/V4_0_4_MARKET_RECORDER.md)**

## V4.0.3 — Read-only forward-edge validator

`scripts/validate_public_edge.py`는 실제 Public trade/orderbook으로 현재 전략을 워밍업하고 후보 시점 entry ask → 미래 bid의 forward net return을 측정합니다. 양쪽 가정 수수료와 spread를 반영하고, 늦은 label은 `missed` 처리하며 시간순 holdout 앞의 overlapping forward horizon을 purge합니다.

API Key와 주문 API를 사용하지 않으며 `orders_submitted: 0`을 명시합니다.

상세: **[V4.0.3 Forward Edge Validation](docs/V4_0_3_EDGE_VALIDATION.md)**

## V4.0.2 — Private 주문·자산 보조 reconciliation

Authenticated Private WebSocket에서 `myOrder`와 `myAsset`을 구독합니다. Private 이벤트는 빠른 wake-up 신호로만 사용하고 실제 체결량·가격·수수료·terminal 상태는 identifier REST 조회를 회계 정본으로 유지합니다. `myAsset`의 계정 전체 잔고를 JunhyunBank 관리수량으로 추정하지 않습니다.

상세: **[V4.0.2 Private Reconciliation](docs/V4_0_2_PRIVATE_RECONCILIATION.md)**

## V4.0.1 — 업데이트 검증과 롤백

새 EXE 적용 전 현재 EXE와 SQLite DB/WAL/SHM을 snapshot으로 보존합니다. 새 버전은 먼저 `--post-update-verify` 비거래 모드에서 DB migration, `PRAGMA quick_check`, 필수 테이블 검사를 통과하고 health marker를 남겨야 합니다. 실패하면 이전 EXE와 업데이트 직전 DB를 함께 복원하며 자동매매를 자동 재개하지 않습니다.

상세: **[V4.0.1 업데이트 복구](docs/V4_0_1_UPDATE_RECOVERY.md)**

## V4 핵심 안전성

V4.0.0부터 주문 전에 unique `identifier`를 SQLite에 영속화하고, POST timeout/5xx처럼 결과가 애매해도 같은 주문을 재전송하지 않고 identifier로 결과를 조회합니다. partial fill은 실제 체결량·금액·수수료만 원자적으로 반영하며 terminal 확인 전 같은 시장의 중복 주문을 차단합니다.

JunhyunBank가 직접 체결해 확보한 **managed quantity만 자동매도**합니다. 프로그램 실행 전 사용자가 보유하던 코인이나 별도로 추가한 수량을 계정 총잔고로 추정해 자동관리하지 않습니다.

## JH-MicroFlow 전략

전략은 각 코인의 절대값보다 자기 자신의 최근 상태 대비 상대적 이상현상을 봅니다.

- 전체 KRW `trade` WebSocket 감시, Hot 후보만 deep `orderbook` 분석
- Activity / Aggression / Book Pressure / Momentum rolling percentile
- 네 요소 geometric mean 기반 Quality
- IGNITION / PULLBACK CONTINUATION 진입
- 실제 계정 수수료 + spread + 실행 유동성 비용 gate
- 시장 Regime과 Strategy Health를 반영한 동적 자금배분
- 고정 `1회 최대 N원`, `시간당 N회`, `최대 N종목` 같은 임의 전략 cap 없음
- 수급 약화, Adaptive Trailing, entry 시 고정한 Emergency Risk, 기대시간 실패 기반 청산
- stale trade/orderbook, 반복 API 오류, 경보/해석불가 시장에서는 신규매수 차단

현재 `ExpectedMove`는 조건부 미래 상승 기대수익 모델이 아니라 최근 `|30초 수익률|` 분포 기반의 **절대 변동폭 proxy**입니다. recorder/OOS 근거 없이 threshold를 완화하거나 Kelly류 sizing을 추가하지 않습니다.

## Best+IOC 유동성 원칙

현재 일반 실주문은 Best+IOC입니다.

```text
BUY immediate capacity  = best ask price × best ask size
SELL immediate capacity = best bid price × best bid size
```

더 깊은 L2는 orderbook imbalance와 향후 연구에는 사용하지만, 현재 Best+IOC가 더 불리한 가격을 자동으로 걸어간다는 뜻이 아닙니다. 실제 partial fill은 주문 결과에서 확인하며 계정 수량을 임의 추정하지 않습니다.

## 종료 동작

일반 `종료`는 즉시 신규매수를 막고 `DRAINING`으로 전환합니다. JunhyunBank 관리 포지션은 기존 전략 청산 규칙으로 계속 관리하고 모두 종료된 뒤 엔진을 멈춥니다.

`긴급 정지`는 전략과 신규주문을 즉시 정지하지만 보유자산을 강제 시장가 청산하는 기능이 아닙니다.

## 업데이트

상단 `업데이트`는 GitHub 최신 Release의 `JunhyunBank.exe`를 내려받고 Release SHA-256 digest를 검증한 뒤 staged update를 수행합니다. V4.0.1 이후 새 바이너리 health 검증이 성공한 경우에만 정상 실행/자동재개합니다.

API Key는 실행파일이 아니라 OS keyring에 저장됩니다. 관리 포지션, 주문 의도, 거래상태는 `~/.junhyunbank/junhyunbank.db`에 유지됩니다.

## UI

- 좌측: 실시간 후보 및 운영 로그
- 우측 상단: 보유자산 목록, KRW 포함
- 우측 하단: 실시간 가격 차트
- 후보별 현재가 + HotScore + 매수 대기 사유/진입 품질
- Trade WS, 마지막 체결, 워밍업, 후보, Orderbook 상태
- 전략 건강도와 Market Regime

## 개발·검증 명령

```bash
python -m pip install -e ".[dev]"
pytest -q
python scripts/smoke_upbit_public_ws.py --timeout 20 --attempts 3
python scripts/validate_public_edge.py --seconds 1800 --output edge-validation.json
python scripts/record_public_market.py --seconds 3600 --output-dir ~/.junhyunbank/recordings
python scripts/replay_strategy.py --input ~/.junhyunbank/recordings --session latest --output replay.json
python launcher.py
```

Public smoke, forward-edge validator, raw recorder는 실제 Upbit Public API만 사용하고 주문하지 않습니다. deterministic replay는 네트워크 자체를 사용하지 않습니다. Private account WebSocket은 CI에 실계정 API Key를 두지 않으므로 protocol/auth/reconciliation을 mock 회귀테스트로 검증합니다.

`main` 병합 시 GitHub Actions가 Windows/Ubuntu 단위·회귀 테스트와 실제 Upbit Public REST/WebSocket smoke를 통과합니다. Release workflow는 Windows에서 다시 테스트/smoke 후 PyInstaller EXE를 빌드해 package version과 같은 `V4.0.x` Release를 생성합니다.

## 검증 상태와 다음 단계

코드/실서버 연결 테스트 성공은 수익성 검증을 뜻하지 않습니다. V4.0.3 forward label, V4.0.4 raw recorder, V4.0.5 deterministic replay, V4.0.6 live Best+IOC semantics 정합화, V4.0.7 fresh candidate pipeline으로 **측정 → 저장 → 동일 경로 재현 → 실제 주문 의미 정렬 → 실제 심사 가능한 후보 공급**까지 진행했습니다.

V4.0.7 이후에도 fresh actionable 후보가 꾸준히 존재하지만 오랜 시간 모든 후보가 `예상 움직임이 거래비용 대비 부족` 또는 품질 기준에서만 탈락한다면, 그때는 pipeline 문제가 아니라 전략 threshold/ExpectedMove의 실효성 문제입니다. 다음 execution simulator와 purged walk-forward/stress에서 이를 검증하고, 충분한 OOS 근거가 있을 때만 조건을 조정합니다.

다음 execution simulator는 generic multi-level market depth walk가 아니라 **latency 후의 실제 Best+IOC top-level full/partial/no-fill + remainder cancel**을 재현해야 합니다. 그 위에 purged walk-forward/stress를 구축하고, 충분한 여러 시장상태 OOS가 쌓인 뒤에만 Conditional ExpectedMove와 uncertainty-aware sizing을 현재 proxy와 비교합니다.

## 개발자 / 다음 ChatGPT 인수인계

새 세션에서는 **[`docs/DEVELOPER_HANDOFF.md`](docs/DEVELOPER_HANDOFF.md)** 를 먼저 읽으세요. 전체 문서 색인은 [`docs/README.md`](docs/README.md)입니다.

핵심 문서:

- [`docs/V4_AUDIT.md`](docs/V4_AUDIT.md)
- [`docs/V4_0_1_UPDATE_RECOVERY.md`](docs/V4_0_1_UPDATE_RECOVERY.md)
- [`docs/V4_0_2_PRIVATE_RECONCILIATION.md`](docs/V4_0_2_PRIVATE_RECONCILIATION.md)
- [`docs/V4_0_3_EDGE_VALIDATION.md`](docs/V4_0_3_EDGE_VALIDATION.md)
- [`docs/V4_0_4_MARKET_RECORDER.md`](docs/V4_0_4_MARKET_RECORDER.md)
- [`docs/V4_0_5_DETERMINISTIC_REPLAY.md`](docs/V4_0_5_DETERMINISTIC_REPLAY.md)
- [`docs/V4_0_6_BEST_IOC_EXECUTION_MODEL.md`](docs/V4_0_6_BEST_IOC_EXECUTION_MODEL.md)
- [`docs/V4_0_7_FRESH_CANDIDATE_PIPELINE.md`](docs/V4_0_7_FRESH_CANDIDATE_PIPELINE.md)
- [`docs/STRATEGY_JH_MICROFLOW.md`](docs/STRATEGY_JH_MICROFLOW.md)
- [`docs/VALIDATION_AND_ROADMAP.md`](docs/VALIDATION_AND_ROADMAP.md)
- [`docs/UPBIT_INTEGRATION.md`](docs/UPBIT_INTEGRATION.md)

코드와 문서가 충돌하면 현재 `main` 코드가 source of truth입니다.
