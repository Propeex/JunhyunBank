# V4.3.3 — 실전 운용 안전성 전면 보강

- 비정상·비유한·중복·시간 역행 체결/호가를 전략 상태와 최신가격에 반영하지 않도록 fail-closed 입력 검증 강화.
- 이벤트의 거래소 시각이 지나치게 오래됐거나 미래이면 Public stream freshness를 갱신하지 않도록 차단.
- 0 채움 frame 수가 아니라 실제 체결 관측시간과 최소 호가 관측수를 워밍업 조건에 포함.
- BTC 기준과 시장 표본 coverage가 부족한 production universe에서 regime을 중립으로 추정하지 않고 신규진입 차단.
- 중복 market 행은 warning/unknown 중 최악 상태로 합치고 `caution`/`warning`의 `null`도 unknown으로 격리. KRW-BTC가 안전 허용목록에 없거나 전체·안전 허용 KRW 시장이 각각 50개 미만, 이전 허용목록의 70% 미만으로 급감, 경보형식 미확인 비율 5% 초과이면 discovery 이상으로 처리. 기존 구독은 관리 포지션 청산 감시용으로 유지하되 정상 목록을 다시 받을 때까지 신규매수 차단.
- LIVE·Public recorder·forward validator가 fresh filter, top-N 밖 보충, 최소 residency와 switch margin을 포함한 동일 후보 선택기를 사용하도록 통합.
- 0 aggression/momentum에 epsilon이 더해져 후보점수가 생기던 경로와 pullback 저점/고점 순서 판정 수정.
- ExpectedMove의 signal 자금배분 영향에 상한을 두고, 계정 평가액·현금 예비금·단일/총 노출·거래/포트폴리오 손실위험·표시 유동성 참여율을 함께 쓰는 동적 위험예산 추가.
- 시작 시 pending SELL을 먼저 reconciliation한 뒤 계정·전략손익 baseline을 구성. baseline/account 확인이 실패해도 청산 엔진은 구동하되 해당 세션 신규매수는 영구 잠금.
- 세션 손실은 계정 전체 평가액 증감이 아니라 누적 관리전략 실현손익과 열린 관리수량의 신선한 실행가능 bid 기준 손익으로 분리. 실제 Best+IOC가 관리수량 전부를 받을 bid 1호가 depth가 부족하면 계산 불가로 처리. 입금·수동 보유자산이 손실을 가리지 못하며 계산 불가 또는 -2% 한도 도달 시 신규매수 중단; 기존 관리 포지션 청산은 계속 허용.
- Adaptive Trailing stop을 SQLite에 단방향 ratchet으로 저장하고 최대 보유시간과 기대시간 청산 사각지대 보완.
- 관리수량이 계정 응답에서 한 번 사라졌다고 관리 해제하지 않고 반복 확인 후 `QUARANTINED`; 최소주문 미만 잔량은 `DUST`로 보존.
- 영속 진입 원가·수량·수수료·부분실현손익 같은 핵심 회계값이 손상된 관리 포지션은 계정 평균단가로 추정하지 않고 자동매도를 차단·격리. 이미 체결된 SELL의 실현 KRW 손익은 계산 가능하지만 outcome 분모/위험 기준만 손상된 경우 별도 `strategy_pnl_adjustments`에 주문 UUID 기준 한 번만 기록해 세션 손익 누락을 방지.
- 주문 의도에 submit/accept 시각을 별도 기록하고, Best+IOC 주문에 Upbit SMP `cancel_taker` 요청.
- 정지 요청과 주문 POST 사이를 공통 submission gate로 직렬화하고 POST 직전에 중지/시장 목록/전체 스트림/해당 체결·호가 신선도를 재확인해 TOCTOU 주문 경로 차단. 청산은 단일 orderbook generation에서 bid·spread·depth를 원자적으로 읽고 최종 정책평가 중 generation이 바뀌면 주문 보류.
- 읽기 REST는 timeout/429/5xx에 제한 재시도하되, 중복주문 위험이 있는 POST는 자동 재시도하지 않음.
- 보유 판단은 영속 수수료/검증 캐시로 즉시 진행하고, 신규진입 후보의 미캐시 수수료 조회는 기본 주기당 1건으로 제한해 다음 청산 평가 지연을 축소.
- recorder/replay의 sequence·session·두 local receive clock·exchange timestamp lag·전략 이벤트 수용·drop/WebSocket 오류 무결성 진단을 강화해 불완전 기록을 정상 재현으로 오인하지 않도록 보완.
- Public recorder와 forward validator에 미래 결과와 무관한 순환 대조군을 추가. recorder/replay는 Hot/control 역할 metadata·상태·decision·fingerprint를 분리하고, validator는 candidate/control 표본·판단·성과 통계를 분리해 후보 사전 워밍업과 headline 통계 혼입을 방지.
- forward validator는 역할 전환 때 기존 book/quote/timer를 초기화하고, WS 오류·전략 거부 이벤트·잘못된 quote·0 sample·미완료 label·book cap sample drop 중 하나라도 있으면 무결성 실패와 종료코드 2로 처리.
- 고빈도 UI 상태 이벤트는 종목·유형별 최신값으로 병합하고 전체 이벤트를 기본 5,000건으로 제한해 느리거나 중단된 화면에서 메모리가 무한 증가하지 않도록 보완.
- 상세 변경, 기본 위험값, 운영 한계: [V4.3 실전 운용 강화](docs/V4_3_REAL_WORLD_HARDENING.md).

# V4.3.2 — 후보 연속성

- 실거래 후보의 교체 점수 차이 조건이 적용되기 전에 기존 후보가 사라지던 문제 수정.
- 빈 호가 감시 자리는 짧은 체결 공백의 기존 종목에 활용하고, 신선 후보는 항상 우선.
- 기준 이력 확보와 최근 체결 활동을 별도 표시. 주문 신선도·비용·청산 조건 유지.
- 상세: [V4.3.2 후보 연속성](docs/V4_3_2_CANDIDATE_CONTINUITY.md).

# V4.3.1 — 최종 체결·진단 갱신

- 종료 직전 마지막 매도의 체결표·건수·미확정 주문 표시 갱신 보장.
- 최종 매수 차단 시 진단을 차단 사유와 실제 계산/최소 주문금액으로 갱신.
- 활동 화면 조회 실패가 주문 감시·종료를 중단하지 않도록 보호.
- 상세: [V4.3.1 최종 활동 갱신](docs/V4_3_1_FINAL_ACTIVITY.md).

# V4.3.0 — 비용 정렬 관측구간과 활동 화면

- 30초 비용 조건 미달 시 충분한 표본을 가진 60/120초 관측 구간도 평가하고 보유 기대시간 하한을 맞춤.
- 실제 체결 표, 날짜별 매수/매도 건수, 미확정 주문과 판단 주기 수, 차트 체결 마커 추가.
- 신호·미체결 취소는 체결 건수에서 제외. 상세: [V4.3 활동 화면](docs/V4_3_ACTIVITY.md).

# V4.2.1 — 스캐너·런타임 진단

- 후보 순위에서 미사용 호가·비용·변동성 계산을 제거하고 거래 프레임 복사 후 잠금 밖에서 계산.
- 시작 시 버전과 주기적 처리시간/시세 신선도 진단 기록 및 화면 표시.
- 진입·청산 기준 유지. 상세: [V4.2.1 스캐너](docs/V4_2_1_SCANNER.md).

# V4.2.0 — 지속 수급 진입과 시간축 차트

- 지속 매수 우위 호가의 진입 확인을 추가하고 기존 비용·위험 제한 유지.
- 실시간 차트에 종목 고정 선택, 체결 시간축, 조회 구간, 평균단가 및 수신 공백 표시 추가.
- 원화 대기 설명과 매수 대기 조건 요약, 체결/호가 개별 수신 지연 표시 추가.
- 127개 테스트 및 공개 기록 재생 검증. 상세: [V4.2 진입·차트 보완](docs/V4_2_ENTRY_AND_CHART.md).

# V4.1.0 — 진입과 보유 판단 분리

- 진입 percentile 급락만으로 매수 직후 매도하던 수급 청산 규칙 교체.
- 방향성 있는 체결·호가·가격 중 복수 반전, 5초/3회 새 관측 확인. 반복 폴링과 데이터 지연은 반전 근거에서 제외.
- 즉시 Emergency Stop, Adaptive Trailing, 기대시간 실패 청산 유지.
- 주문 직전 진입 신호 재확인, 보유 경과시간과 청산 근거 표시·기록.
- 122개 테스트, 동일 공개 기록의 진입 결정 지문 일치 검증.
- 상세: [V4.1 전략 보완](docs/V4_1_POSITION_POLICY.md).

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
