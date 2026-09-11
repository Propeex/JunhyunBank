# V4.0.7 — 신선 후보 우선 진입 파이프라인

- V4.0.0 실사용에서 반복됐던 `후보는 존재하지만 체결/호가 수신 대기 또는 3초 이상 지연` 상태의 구조적 원인을 보완.
- scanner HotScore 상위 목록과 실제 신규매수 stale gate(`market_data_stale_seconds`, 기본 3초)를 정렬해, 이미 진입 불가능한 stale 후보가 deep orderbook 슬롯을 차지하지 않도록 수정.
- top-N 안의 stale 후보를 제거한 뒤 top-N 밖의 fresh/warmed 시장을 HotScore 순으로 보충해 실제 심사 가능한 후보 수를 유지.
- 비관리 stale 후보는 기존 deep minimum residency보다 신선도 안전조건을 우선해 즉시 deep set에서 제외.
- JunhyunBank managed position은 stale 여부와 관계없이 deep monitoring에 남겨 청산 안전성을 유지.
- UI 후보 목록과 runtime candidate count를 실제 fresh actionable 후보 기준으로 맞춤.
- 5분 이상 actionable fresh 후보가 0개면 stale 후보 제거 수를 포함한 별도 진단 경고를 출력.
- IGNITION/PULLBACK 품질, ExpectedMove 비용 gate, Strategy Health, Market Regime, Best+IOC 주문 방식은 완화하지 않음.
- 회귀테스트로 stale 고득점 후보가 fresh lower-rank 후보를 굶기지 않는지, stale managed position이 유지되는지, UI 후보 이벤트가 actionable set과 일치하는지 검증.

# V4.0.6 — Upbit Best+IOC 실행 모델 정합화

- Upbit 공식 `best` 주문이 접수 시점 상대 최우선호가를 가격으로 쓰는 지정가이며 IOC remainder는 취소된다는 사양에 live pre-trade model을 맞춤.
- 기존 multi-level depth-walk 가정 대신 BUY는 best ask, SELL은 best bid의 current top-level capacity만 즉시 체결 가능으로 계산.
- 신규 position의 동적 liquidity cap을 current best ask/bid 양방향 즉시 체결 notional 중 작은 값으로 제한.
- 요청량이 top-level capacity를 넘을 때 더 불리한 L2에 체결된다고 가정하지 않고 unfilled/cancel 가능량으로 처리.
- `execution_model.py`에 Best+IOC 순수 실행모델을 분리해 다음 execution simulator도 같은 의미를 재사용할 수 있게 함.
- production `main.py`는 `live_engine.TradingEngine`을 사용하며 기존 runtime/주문상태/managed quantity 경로는 그대로 상속.
- 실제 REST 주문은 계속 `ord_type=best`, `time_in_force=ioc`; 주문 타입이나 JH-MicroFlow threshold는 변경하지 않음.
- deeper L2/큰 ExpectedMove가 Best+IOC capacity를 인위적으로 늘리지 못하는 회귀테스트와 실제 order payload 회귀테스트 추가.
- 상세 설계: [V4.0.6 Best+IOC Execution Model](docs/V4_0_6_BEST_IOC_EXECUTION_MODEL.md).

# V4.0.5 — Deterministic strategy replay

- V4.0.4 recorder session을 네트워크와 실제 주문 없이 재생하는 `scripts/replay_strategy.py` 추가.
- recorder의 `received_monotonic_ns`를 기준으로 logical replay clock을 구성해 실행 PC 속도와 wall clock에 영향을 받지 않는 freshness 판정을 지원.
- 여러 WebSocket callback의 수신시각이 파일 순서에서 소폭 역전되면 시간을 뒤로 돌리지 않고 clamp하고 `clock_retrograde_events`로 진단.
- production `MicroFlowStrategy`의 threshold/자금배분 코드는 변경하지 않고 replay 전용 subclass에서 trade/orderbook local receive freshness만 대체.
- `session_start`에 저장된 당시 안전 KRW universe와 `StrategyConfig`를 복원하고 한 입력에 여러 session이 섞이면 fail-closed.
- recorded orderbook 이벤트마다 종목별 cadence로 현재 entry decision/feature/regime/top-of-book 상태를 재생.
- 동일 recording + 동일 전략 코드/config/fee에서 동일해야 하는 canonical decision SHA-256 fingerprint 추가.
- 비정상 종료 recording은 조사할 수 있으나 complete session이 아니면 CLI가 nonzero exit code를 반환.
- depth execution/partial fill/latency/portfolio PnL simulation은 아직 포함하지 않으며 수익성 검증으로 간주하지 않음.
- 실거래 engine/execution/managed quantity/전략 threshold는 변경하지 않음.
- 상세 설계: [V4.0.5 Deterministic Strategy Replay](docs/V4_0_5_DETERMINISTIC_REPLAY.md).

# V4.0.4 — Raw market data recorder

- API Key와 주문 API를 사용하지 않는 `scripts/record_public_market.py` 추가.
- 전체 안전 KRW 시장의 raw trade와 현재 Hot 후보의 L2 orderbook을 replay용 JSONL로 기록.
- exchange timestamp, local receive timestamp/monotonic timestamp, process sequence를 함께 저장해 이후 수신순서 기반 deterministic replay를 지원.
- WebSocket callback은 bounded queue에 `put_nowait`만 수행하고 파일 I/O·fsync·rotation·gzip은 background thread로 분리.
- queue 포화 시 거래/시세 callback을 block하지 않고 recorder event를 drop하며 `dropped` 통계와 nonzero exit code로 데이터 불완전성을 노출.
- 활성 `.jsonl.part` → fsync → atomic `.jsonl` finalize, 별도 `.jsonl.gz.part` → atomic gzip finalize 순서로 crash-safe 보존.
- 비정상 종료 후 `.jsonl.part`의 torn trailing line을 제거하고 완전한 레코드까지 자동 복구.
- segment size/time rotation과 총 bytes/file-count retention 추가.
- finalized `.jsonl`/`.jsonl.gz`를 읽는 `iter_records()`를 추가해 다음 deterministic replay 단계의 입력 계약을 고정.
- 실거래 engine/execution/managed quantity/전략 threshold는 변경하지 않음.
- 상세 설계: [V4.0.4 Raw Market Data Recorder](docs/V4_0_4_MARKET_RECORDER.md).

# V4.0.3 — Read-only forward-edge 검증 기반

- API Key와 주문 API를 전혀 사용하지 않는 `scripts/validate_public_edge.py` 추가.
- 실제 Public trade/orderbook으로 현재 JH-MicroFlow 특징·진입판단을 워밍업하고 후보 평가 시점의 executable top-of-book을 기록.
- 미래 label은 entry ask → future bid 기준으로 양쪽 가정 수수료와 spread를 지불한 순수익을 계산.
- 30/60/120/300초 등 여러 forward horizon을 동시에 기록하고 BUY 표본과 전체 후보 표본을 분리 집계.
- label 시점 quote가 허용 지연창을 벗어나면 늦은 가격으로 왜곡하지 않고 `missed`로 기록.
- live 엔진과 동일하게 3초 stale gate를 적용하고 deep 재진입 종목의 오래된 orderbook state/quote를 폐기.
- 시간순 holdout 앞에서 horizon만큼 training label을 purge해 forward-label overlap 데이터 누수를 차단.
- label completion/miss, ExpectedMove calibration, 순수익률, positive rate, WebSocket 오류와 재현용 전략 설정을 JSON에 저장.
- 이 검증은 size-free top-of-book shadow 관측으로, depth slippage·실제 주문 지연·계정 상태를 아직 모델링하지 않음.
- 상세 설계: [V4.0.3 Forward Edge Validation](docs/V4_0_3_EDGE_VALIDATION.md).

# V4.0.2 — Private 주문·자산 보조 reconciliation

- authenticated Private WebSocket 한 연결에서 `myOrder` + `myAsset` 동시 구독.
- 장시간 무이벤트가 정상인 private stream에 ping/reconnect 및 fresh JWT 적용.
- Authorization/JWT가 오류/상태 로그에 노출되지 않도록 redaction.
- `junhyunbank-` identifier 주문 이벤트만 durable intent REST reconciliation을 즉시 깨우도록 제한.
- WebSocket 이벤트를 회계 정본으로 사용하지 않고 실제 체결/수수료/terminal 상태는 identifier REST 조회 후 기존 atomic accounting 적용.
- Private WS 장애 시 기존 REST reconciliation fallback 유지.
- pending 주문 REST 조회 실패에 2→4→8→15초 backoff를 적용하고 myOrder 이벤트는 backoff를 우회.
- `myAsset`은 자산 변동 신호로만 사용하며 계정 전체 잔고를 자동관리 수량으로 추정하지 않음.
- 상세 설계: [V4.0.2 Private Reconciliation](docs/V4_0_2_PRIVATE_RECONCILIATION.md).

# V4.0.1 — 업데이트 사전검증·원자 롤백

- 새 EXE를 정상 실행하기 전에 `--post-update-verify` 비거래 검증 모드로 현재 DB 스키마와 SQLite 무결성을 확인.
- 검증 전 현재 EXE + SQLite DB/WAL/SHM을 snapshot으로 보존.
- 새 바이너리가 token/version health marker를 만들지 못하면 이전 EXE와 업데이트 직전 DB를 함께 복원.
- 새 버전 검증이 완료되기 전에는 LIVE 자동매매 자동 재개 금지.
- rollback으로 복구된 이전 버전은 자동매매를 자동 재개하지 않도록 fail-safe 처리.
- 상세 설계: [V4.0.1 업데이트 복구](docs/V4_0_1_UPDATE_RECOVERY.md).

# V4.0.0 — 런타임·주문 복구·매수 진단

- 정지 후 재시작 구독 복구 및 일시 네트워크 오류 복구.
- 동일 시간창 수급 비교, 신호 초기화 및 동시 접근 수정.
- 종목별 매수 대기 이유와 진입 품질/비용 표시.
- 주문 의도 영속 저장과 identifier 기반 복구, 부분 체결 원자적 기록.
- 미확정 주문 중복 방지 및 사용자 보유분 보호.
- 상세 분석: [V4 감사](docs/V4_AUDIT.md).

# Changelog

## V3.0.1

- `Trade WS 0/0`, `추적 0/0`, 후보 0 상태로 조용히 멈추는 KRW 마켓 유니버스 실패 경로 보강
- Upbit `market_event` 경보 필드를 Python truthiness가 아닌 명시적 bool/string 값으로 안전하게 해석
- `is_details` 응답에 경보 상세가 없을 때 legacy `isDetails` 표기로 1회 호환 재조회
- KRW 마켓은 존재하지만 안전하게 해석 가능한 종목이 0개면 명확한 오류/진단 로그 출력
- 마켓 탐색 실패 시 10초 backoff를 적용해 빈 유니버스 상태에서 REST API를 과도하게 재호출하지 않도록 수정
- Release smoke test가 실제 `/v1/market/all` → 안전 KRW 필터 → Public WebSocket 경로 전체를 검증하도록 확대
- 패치 릴리즈가 기존 `V3` 태그에 막히지 않도록 semantic release tag(`V3.0.1` 등) 지원

## V3

- Public WebSocket Origin suppression 적용
- deep orderbook 연결을 후보 변경마다 재생성하지 않고 live subscription 갱신 방식으로 안정화
- Trade WS/마지막 체결/워밍업/후보/Orderbook 상태 진단 UI 추가
- 후보별 현재가 표시와 KRW-BTC 기본 차트 추가
- 단일 인스턴스 실행 보호 및 실제 Upbit read-only WebSocket smoke test 추가

## V2

- 모의매매 제거, LIVE 전용 실행
- JH-MicroFlow 실시간 단타 엔진 도입
- 전체 KRW trade WebSocket 감시와 후보 orderbook 정밀분석
- 종목별 rolling percentile 기반 Activity/Aggression/Book/Momentum 신호
- IGNITION / PULLBACK CONTINUATION 진입
- 실제 수수료·스프레드·예상 슬리피지 비용 게이트
- 고정 주문금액/거래횟수/포지션 수/익절·손절 제거
- 동적 자금배분, 유동성 용량 계산, Adaptive Trailing 및 Emergency Stop
- Strategy Health Governor 추가
- 종료 버튼의 DRAINING 동작 추가
- 보유자산에 KRW 표시, 보유자산/운영로그 UI 위치 교체
- 최신 GitHub Release 자동 업데이트 및 재시작 기능
- 업데이트 후 OS keyring API Key 및 SQLite 포지션 상태 유지
- V1 SQLite 스키마 자동 마이그레이션

## V1

- 최초 업비트 자동매매 MVP
- PAPER/LIVE 모드
- 5분봉 Trend + RSI 전략
- API Key keyring 저장
- 기본 리스크 제한 및 거래 기록