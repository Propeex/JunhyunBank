# V4.3.3 — 실전 운용 안전성 전면 보강

V4.3.3의 목적은 거래 횟수를 늘리거나 백테스트 수익률을 높이는 것이 아니다. 실제 계정에서 치명적일 수 있는 **잘못된 시장 데이터, 과도한 주문 크기, 느슨해지는 청산 기준, 잔고·주문 상태의 애매함**을 보수적으로 처리하는 것이 목적이다.

이 문서는 변경의 의도와 운용 한계를 설명한다. 최종 동작은 항상 현재 코드와 `SafetyConfig` / `StrategyConfig`가 정본이다.

## 1. 감사에서 확인한 핵심 위험

| 영역 | 이전 위험 | V4.3 원칙 |
|---|---|---|
| 시장 데이터 | 늦게 도착한 이벤트가 현재 book/가격을 덮거나 freshness가 새로워질 수 있음 | 검증을 통과한 이벤트만 상태와 freshness를 갱신 |
| 시장 목록 | 잘린·변형된 market 응답을 정상 전체 목록으로 받아 구독과 regime 근거가 축소될 수 있음 | 대표시장·최소 크기·이전 대비 유지율·경보 schema 비율을 검사하고 이상 시 신규진입 차단 |
| 워밍업 | 드문 체결 사이의 0 채움 frame만으로 충분히 학습했다고 볼 수 있음 | frame 수와 실제 체결 관측시간을 함께 요구 |
| 시장 regime | BTC나 시장 표본이 부족해도 `NEUTRAL`로 열릴 수 있음 | universe 크기와 무관하게 coverage가 부족하면 신규진입 차단 |
| 연구 후보 | LIVE와 recorder/validator의 후보 선별 세부 규칙이 달라 선택 편향을 잘못 측정할 수 있음 | 동일 fresh actionable 선택기를 공용으로 사용 |
| 후보 점수 | 수급 또는 모멘텀이 0이어도 수치 epsilon으로 미세한 양수 점수가 생김 | 필수 요소가 0이면 HotScore도 정확히 0 |
| pullback | 저점과 고점의 시간 순서를 무시해 급락 후 반등을 pullback으로 오인 가능 | `저점 → 고점 → 고점 이후 눌림 → 현재 회복` 순서 확인 |
| 자금배분 | 신호 비율이 가용 KRW 대부분까지 커질 수 있음 | 손절거리·노출·현금·유동성 기반 위험예산이 최종 상한 |
| 추적 청산 | 매 평가 때 계산한 stop이 낮아져 이미 확보한 이익을 다시 내줄 수 있음 | SQLite에 저장한 stop은 올라가기만 하는 ratchet |
| 보유시간 | 과거 한 번 큰 MFE가 있으면 기대시간 청산에서 영구 제외될 수 있음 | 현재 손익 기준과 별도 최대 보유시간으로 사각지대 제거 |
| 잔고 불일치 | 한 번의 계정 조회 누락으로 관리 포지션이 해제될 수 있음 | 반복 확인 후 격리하고 자동 삭제·자동 수량추정 금지 |
| 시작 복구 | 이전 실행의 pending SELL보다 계정/baseline을 먼저 읽으면 정상 종결된 포지션을 불일치로 오인할 수 있음 | pending 주문을 먼저 조정하고, 기준 구성 실패 시에도 청산 엔진은 시작하되 그 세션 신규매수 영구 잠금 |
| 청산 호가 | bid·spread·depth를 서로 다른 WebSocket snapshot에서 읽거나 정책평가 중 바뀐 호가로 POST 가능 | 한 generation의 호가를 원자적으로 사용하고 POST 직전 generation 재확인 |
| 손익 회계 | outcome 정규화 기준 손상 때문에 이미 실현된 KRW 손익이 세션 원장에서 빠질 수 있음 | 계산 가능한 KRW 손익은 별도 durable adjustment 원장에 보존하고 핵심 회계 손상은 격리 |
| 주문 상태 | 준비 상태와 거래소 접수 상태의 경계가 충분히 드러나지 않음 | submit/accept 시각을 별도 영속화하고 identifier 복구 유지 |
| 정지·주문 경합 | 마지막 조건 검사 뒤 정지 또는 전체 WS 단절이 발생해도 POST가 시작될 수 있음 | 공통 submission gate 안에서 마지막 조건 검사와 POST를 직렬화 |
| REST 조회 지연 | 후보별 수수료 조회가 몰리거나 보유 판단 앞에서 지연될 수 있음 | 청산은 영속 수수료/검증 캐시를 사용하고 신규 조회는 주기당 1건만 수행 |
| 재현 데이터 | sequence gap·불완전 session·drop을 정상 replay처럼 해석할 수 있음 | 입력 무결성 진단을 결과의 일부로 만들고 fail-closed 판정 |
| 화면 이벤트 | UI가 느리거나 멈췄을 때 고빈도 이벤트가 메모리에 계속 쌓일 수 있음 | 최신값 병합과 하드 상한을 가진 비차단 이벤트 버퍼 사용 |

## 2. 시장 데이터 입력과 워밍업

### 이벤트 수용 조건

체결과 호가는 다음을 만족할 때만 전략에 반영한다.

- 시장코드, 가격, 수량, 호가단계가 해석 가능하고 숫자가 유한함.
- 거래소 timestamp가 존재하며 허용 가능한 현재 범위에 있음.
- 같은 시장에서 이미 반영한 이벤트보다 시간 또는 sequence가 역행하지 않음.
- 같은 timestamp와 핵심 내용의 중복 이벤트가 아님.
- 호가의 bid/ask 가격 정렬과 양수가 유지됨.

거부된 이벤트는 마지막 가격, orderbook, feature history, local receive freshness를 갱신하지 않는다. 따라서 “낡은 데이터를 받았지만 방금 받은 것처럼 보이는” 상태를 피한다. WebSocket 재연결에서는 해당 stream의 연속성에 의존하는 history를 초기화해 연결 전후 이벤트를 한 구간으로 이어 붙이지 않는다.

거래소 시계와 실행 PC 시계가 크게 어긋나면 정상 이벤트도 거부될 수 있다. 시스템 시각 동기화가 운용 전제이며, 이 상황에서 자동으로 fail-open하지 않는다.

### 시장 universe 응답도 입력 데이터다

`/v1/market/all` 응답이 JSON 형식상 성공했다고 해서 완전한 시장 목록이라고 가정하지 않는다. 같은 market이 중복되면 정상 행 하나를 고르는 대신 warning > unknown > normal 순서의 **가장 위험한 상태**로 합친다. `market_event.warning`이나 `caution` 및 그 내부 값의 `null`, 누락, 미확인 문자열/구조는 정상 `false`로 추정하지 않고 unknown으로 제외한다. 기본 `SafetyConfig`는 다음을 이상으로 본다.

- 대표시장 `KRW-BTC`가 원본 또는 안전 허용목록에서 누락.
- 전체 KRW 시장 또는 경보 해석을 통과한 안전 허용시장이 각각 50개 미만.
- 새 안전 허용목록이 직전 정상 허용목록의 70% 미만으로 급감.
- 해석할 수 없는 경보 형식이 전체 KRW 시장의 5%를 초과.

이상 응답은 정상 목록으로 교체하지 않는다. 기존 WebSocket 구독은 관리 포지션의 청산 감시를 위해 유지하지만 `_market_discovery_ready`를 닫고, 독립적인 정상 discovery가 들어올 때까지 모든 신규매수를 막는다. 이 하한은 거래소 상장 수 변화에 맞춰 검토할 수 있는 운영 안전값이지 매매 신호 파라미터가 아니다.

### 워밍업 조건

기본 워밍업은 다음을 모두 본다.

- 초별 frame 수: `min_warmup_seconds`.
- 유효 체결이 실제 있었던 서로 다른 초의 수: `min_warmup_trade_seconds`.
- book factor 사용 전 유효 호가 관측수: `min_book_observations`.

체결 두 건 사이의 빈 초를 0으로 채운 것만으로 학습 완료가 되지 않는다. 재연결 또는 subscription 변경으로 연속성이 끊기면 관련 상태를 다시 쌓는다.

## 3. 후보·regime·진입 판단

### 시장 근거 부족은 매수 근거가 아니다

BTC 데이터가 오래됐거나 신선한 시장 표본 비율이 `regime_min_coverage`보다 작으면 universe 크기와 관계없이 `DATA_INSUFFICIENT`로 판단해 신규매수를 막는다. 작은 universe를 `NEUTRAL`로 여는 별도 예외는 없다. 테스트나 연구 도구도 필요한 표본을 명시적으로 제공해야 한다.

LIVE 엔진, Public recorder, forward validator는 `MicroFlowStrategy.select_actionable_deep_markets()`를 공용으로 사용한다. 이 선택기는 같은 live stale 기준으로 비관리 stale 후보를 제거하고, 잘린 scanner 상위 목록 밖의 fresh·양수 HotScore 시장을 보충하며, deep 최소 체류시간과 교체 margin을 적용한다. 관리 포지션은 청산 감시를 위해 LIVE deep set에 별도로 유지하고, 연구 도구는 후보군 뒤에 미래 결과와 무관한 순환 대조군과 미완료 label 시장을 추가한다. 따라서 연구 후보가 LIVE라면 이미 탈락했을 stale 상위 종목으로 편향되는 경로를 줄인다.

HotScore는 필수 수급·모멘텀 근거가 0이면 정확히 0이다. 이는 후보 순위를 위한 점수이며 실제 entry Quality와 동일하지 않다.

PULLBACK은 시간 순서를 확인한다. 과거 저점이 있었다는 사실만으로 충분하지 않으며, 저점 이후 고점을 형성하고 그 고점 이후 눌림이 나온 다음 현재 회복하는 구조여야 한다.

### ExpectedMove의 제한된 역할

현재 `ExpectedMove`는 최근 `|30초 수익률|` 분포에서 만든 절대 변동폭 proxy다. 상승 방향의 성공확률이나 조건부 순기대값이 아니다.

V4.3은 다음처럼 보수적으로 취급한다.

- 왕복비용의 2배를 넘는 변동폭 여유가 없으면 진입하지 않음.
- signal 기반 자금비중에 별도 상한을 적용.
- 최종 주문금액은 독립적인 위험예산과 표시 유동성 상한을 넘지 않음.

따라서 높은 ExpectedMove를 “오를 확률이 높다” 또는 “Kelly edge가 크다”로 해석하면 안 된다.

## 4. 동적 위험예산

V4.3의 기본 안전예산은 고정 KRW 금액이나 시간당 횟수 제한이 아니다. 매 평가 시 계정 평가액, 가용 현금, 현재 노출, entry 시 고정한 초기 손절거리, 최우선호가 표시 유동성으로 다시 계산한다.

기본값은 다음과 같다.

| 항목 | 기본값 | 의미 |
|---|---:|---|
| 현금 예비금 | 평가액의 15% | 신규주문 뒤에도 남겨 둘 현금 |
| 단일 포지션 노출 | 평가액의 15% | 한 종목에 집중될 수 있는 최대 비율 |
| 총 암호자산 노출 | 평가액의 60% | 전체 보유 노출 상한 |
| 거래당 계획 손실위험 | 평가액의 0.25% | 주문금액 × 초기 손절거리 기준 |
| 포트폴리오 계획 손실위험 | 평가액의 1% | 열린 관리 포지션과 신규주문의 합계 |
| 표시 유동성 참여 | 최우선호가 capacity의 10% | 현재 보이는 한 가격단계에 대한 참여 상한 |
| 세션 신규매수 중단 | 관리전략 손익 변화 / 시작 평가액 -2% | 도달 시 해당 세션 신규매수 중지 |
| signal 자금비중 | 최대 25% | ExpectedMove 기반 비중의 상한 |

최종 주문예산은 위 조건별 허용금액 중 가장 작은 값이다. 최소 주문금액에 못 미치면 주문하지 않는다.

시작 순서는 복구에도 중요하다. 이전 실행에서 거래소에 도달한 pending SELL을 identifier REST로 먼저 reconciliation한 뒤 계정잔고와 세션 baseline을 만든다. SELL이 이미 종결됐는데 오래된 managed row부터 비교해 정상 빈 잔고를 불일치로 오인하는 일을 막기 위해서다. 시작 시 계정 조회 또는 전략 손익 baseline 구성이 불가능해도 엔진 자체를 중단하지 않고 관리 포지션 청산 루프를 구동한다. 대신 그 실행 세션의 신규매수는 **영구 잠금**하며, 나중에 데이터가 우연히 들어왔다고 baseline을 다시 잡아 매수를 열지 않는다. 운영자는 원인을 확인하고 프로그램을 종료·재시작해야 한다.

세션 손익의 분자는 계정 전체 평가액 변화가 아니다. 영속 `strategy_outcomes.net_pnl_krw`와 `strategy_pnl_adjustments.net_pnl_krw`의 누적 실현손익에 각 열린 관리 포지션의 부분 실현손익과 잔여 관리수량을 fresh best bid로 전량 매도한다고 본 수수료 차감 평가손익을 더하고, 시작 시점의 같은 전략 snapshot과 비교한다. 시작 baseline과 이후 신규진입 판단 모두 같은 fresh executable bid를 요구한다. 실제 주문이 Best+IOC이므로 bid 1호가 표시수량이 관리수량 전부를 받을 수 없을 때는 더 나쁜 하위 호가까지 임의로 평가하지 않고 전략 PnL을 계산 불가로 처리해 신규매수를 막는다. 분모만 세션 시작 계정 평가액을 사용한다. 따라서 외부 입금이나 사용자의 비관리 코인 상승이 JunhyunBank 손실을 가리지 못하며, 반대로 출금이나 비관리 자산 하락이 봇 손실로 오인되지 않는다. 필요한 관리상태·계정수량·fresh bid·bid 1호가 depth를 신뢰할 수 없으면 손실률을 추정하지 않고 신규매수를 막는다. -2%에 도달한 중단 상태는 해당 실행 세션 동안 유지하되 기존 관리 포지션의 청산은 계속 허용한다.

이 수치는 손실을 보장된 범위 안에 가두지 않는다. 급격한 갭, 호가 소진, 거래소·네트워크 장애, 프로세스 중단에서는 실제 손실이 계획값보다 커질 수 있다. 기본값은 보수적인 초기 운영값이지 장기간 OOS로 최적화된 값이 아니다.

## 5. 보유·청산 상태

### 단방향 추적 손절

Adaptive Trailing이 활성화되면 계산된 stop을 `managed_positions.trailing_stop_price`에 저장한다. 이후 평가에서 변동성이 커져 새 계산값이 낮아지더라도 저장된 stop은 낮아지지 않는다. 재시작 후에도 같은 기준을 이어간다.

Emergency Stop은 entry 시 저장한 `initial_risk_pct`를 계속 사용한다. 최대 보유시간 기본값은 900초이며, 과거 MFE가 컸다는 이유만으로 무기한 보유하지 않는다. 청산 판단에는 가능한 한 현재 실행 가능한 fresh best bid를 사용하고, live book이 오래되면 신선도가 확인된 Public REST orderbook만 보조값으로 사용한다. REST 보조호가의 수신시각은 요청 직전이 아니라 응답을 실제로 받은 뒤 기록해 느린 요청을 새 호가처럼 보이게 하지 않는다. 어느 쪽도 신선하지 않으면 오래된 마지막 체결가로 청산을 추정하지 않고 진단을 남긴다.

WebSocket 실행호가는 잠금 아래서 얻은 한 `BookSnapshot` generation으로 bid 가격·bid depth·ask·spread·수신시각을 함께 구성한다. bid와 spread를 서로 다른 갱신에서 섞지 않는다. SELL 결정을 얻은 뒤에도 submission gate의 전송 경계에서 이 원자 snapshot으로 보유정책 전체를 다시 평가하고 trailing ratchet을 저장한다. 정책평가가 끝난 직후 같은 generation token인지 한 번 더 확인하며, 그 사이 호가가 바뀌었거나 만료됐거나 가격·수급이 회복돼 최종 판단이 SELL이 아니면 POST하지 않고 다음 주기에 다시 평가한다. 이미 최소 매도금액보다 작은 관리잔량은 매도 시도 전부터 `DUST`로 표시해 DRAINING과 운영 화면이 그 상태를 정확히 반영한다.

영속 관리수량보다 계정 총수량이 작아진 경우에는 `min(관리수량, 계정수량)`을 매도하지 않는다. 외부 매도·출금 뒤 남은 수량의 소유권을 증명할 수 없기 때문에 즉시 `QUARANTINED`로 격리한다. 잠긴 수량은 총수량에는 포함하되 주문 가능 수량에서는 제외하므로 잠금 상태를 경제적 `DUST`로 오인하지 않는다. 관리 중인 종목을 업비트 앱이나 다른 프로그램에서 수동 매매하면 동일 자산 lot의 소유권을 완전히 식별할 수 없으므로 금지한다. 부득이하게 수동 거래했다면 자동매매를 중지하고 해당 격리 상태와 계정 주문내역을 먼저 확인한다.

이는 거래소에 미리 등록된 stop 주문이 아니다. 앱이 실행 중이고 시세·주문 API를 사용할 수 있어야 동작한다.

### 잔고 불일치와 dust

JunhyunBank가 체결해 확보한 `managed_quantity`만 자동매도한다는 원칙은 유지한다.

- 한 번의 accounts 응답에서 수량이 보이지 않아도 즉시 관리 해제하지 않음.
- 영속 진입 원가가 없거나 손상됐으면 계정 평균단가로 대체 추정하지 않고 자동매도 금지.
- 연속 누락이 설정된 확인 횟수에 도달하면 `QUARANTINED`로 격리.
- 격리 상태에서는 계정 총잔고로 수량을 새로 추정하거나 자동매도하지 않음.
- 최소 매도금액보다 작은 잔량은 `DUST`로 보존해 감사 가능한 cost basis를 유지.
- DRAINING은 자동으로 처리할 수 없는 `DUST` / `QUARANTINED` 때문에 영구 대기하지 않음.

격리는 사용자가 계정 거래내역·수량·pending order를 확인해야 하는 운영 경보다. 자동으로 원상복구하는 기능으로 보면 안 된다.

자동 SELL을 만들기 전에는 관리수량·진입가격·진입금액·진입수수료·부분실현손익 등 핵심 회계 필드가 유한하고 의미 있는지 확인한다. 이 값으로 실제 SELL 손익을 안전하게 계산할 수 없으면 포지션을 `QUARANTINED`로 격리하고 주문하지 않는다. 다만 이미 거래소에서 SELL이 체결되어 수량·진입가격·수수료·부분실현손익으로 실제 KRW 손익은 확정할 수 있지만 `entry_amount_krw` 또는 `initial_risk_pct` 같은 outcome 정규화 기준만 손상된 경우에는, 체결을 되돌리거나 손익을 버리지 않는다. 정상 Strategy Health 표본에는 섞지 않고 주문 UUID unique key를 가진 `strategy_pnl_adjustments` 원장에 실현 KRW 손익을 한 번만 기록한다. 세션 누적손익은 정상 outcomes와 이 조정 원장을 모두 합산한다.

## 6. 주문 전송과 거래소 보호

- 주문 전 unique `identifier`와 의도를 SQLite에 기록하는 기존 불변조건을 유지.
- POST 직전 `submitted_at`, 거래소 UUID 수신 직후 `accepted_at`을 별도 기록.
- 주문 전송은 정지 요청과 공유하는 reentrant submission gate에서 직렬화. 정지가 gate를 먼저 얻으면 POST를 막고, POST가 먼저 얻으면 해당 주문은 명시적인 in-flight intent로 남겨 reconciliation 대상이 됨.
- BUY는 gate 안에서 POST 직전에 실행상태, market 허용·discovery 상태, 전체 trade shard 연결/신선도, 해당 시장 trade/book 신선도를 다시 확인. 조건이 깨지면 durable intent를 명시적으로 rejected 처리.
- Best+IOC 매수·매도와 terminal zero-fill 뒤의 시장가 매도 fallback에 SMP `cancel_taker` 요청.
- 조회 GET은 timeout, 429, 일시적 5xx에 짧고 제한적인 재시도를 적용.
- 보유 포지션 판단은 진입 때 저장한 실제 수수료 또는 이미 검증된 캐시를 사용해 신규 REST 조회를 기다리지 않음.
- 신규진입 후보의 미캐시 수수료·최소주문 조회는 기본 한 주기 1건만 수행하고, 나머지는 다음 평가 주기로 이월해 긴 직렬 조회가 청산 루프를 오래 점유하지 않게 함.
- 주문 POST는 결과가 애매할 때 중복 주문이 될 수 있으므로 자동 재시도하지 않음.
- POST timeout/5xx는 동일 주문을 다시 보내는 대신 identifier REST reconciliation으로 확인.

SMP는 자기 주문끼리의 의도치 않은 체결을 줄이는 거래소 기능이다. 다른 참여자의 체결, 가격 변동, partial/no-fill 위험까지 없애지는 않는다.

## 7. recorder·replay·forward validator 무결성

deterministic replay는 의사결정 재현 도구이지 체결·손익 backtest가 아니다. V4.3은 결과에 다음 데이터 품질 신호를 포함한다.

- process sequence gap·중복·역행.
- session 시작 전 또는 종료 후 event.
- 기록된 universe 밖의 market event.
- `received_ns` 또는 `received_monotonic_ns`가 없는 record. 전략 이벤트는 두 시계가 모두 없으면 재생하지 않음.
- 전략 이벤트의 exchange timestamp 누락·오류 및 recorded wall receive 시각 대비 허용범위를 벗어난 과거·미래 timestamp.
- production 입력검증에서 거부된 trade/orderbook 이벤트.
- recorder queue drop, writer 통계와 WebSocket 오류.
- subscription 변경에 따른 orderbook state reset.
- 동일 segment의 `.jsonl` / `.jsonl.gz` 중복 입력 방지.
- Hot 후보 외 순환 대조군 orderbook을 함께 수집해 후보 선택의 lift를 비교할 기반.
- 각 orderbook subscription metadata의 `hot_markets`와 `control_markets`를 완전하고 서로 겹치지 않는 역할 partition으로 검증.
- WebSocket 구독 합집합이 같더라도 Hot/control 역할이 바뀐 시장의 book history와 평가시계를 초기화해 대조군이 후보를 사전 워밍업하지 못하게 함.
- production 후보 decision/fingerprint와 control decision/fingerprint를 각각 `decisions`/`summary`, `control_decisions`/`control_diagnostics`로 분리.

필수 무결성 조건이 충족되지 않은 replay는 조사 자료로는 남길 수 있지만 complete/정상 세션으로 승인하지 않는다. 현재 recorder가 대조군을 선언한 세션에서는 역할 metadata 누락·부분기록·중복 역할·구독 합집합 불일치도 무결성 실패다. 과거 controls 없는 legacy recording만 `markets` 전체를 Hot으로 해석하는 호환경로를 유지한다. Public recorder도 queue drop이나 writer 오류뿐 아니라 WebSocket 오류가 하나라도 있으면 `data_integrity_ok=false`, 종료코드 2로 끝낸다.

forward validator 역시 정상 종료 여부와 WebSocket 오류만 보지 않는다. 전략이 거부한 trade/orderbook, quote 구조 오류, 표본 0건, `missed`/`pending`/`invalid` label, book 구독 상한 때문에 버린 신규 표본 중 하나라도 있으면 `integrity_reasons`와 개별 count를 남기고 `data_integrity_ok=false`, 종료코드 2로 끝낸다. control↔candidate 역할 전환은 구독 합집합이 같아도 book·cached quote·sampling timer를 초기화한다. headline BUY·reason·candidate 성과에는 control을 섞지 않고 대조군 통계를 별도로 낸다. 이는 데이터가 완전할 때 수익성을 보장한다는 뜻이 아니라, 명백히 불완전한 run이 정상 검증으로 승격되는 것을 막는 최소 조건이다. replay와 validator는 실제 주문지연, 체결, partial/no-fill, exit, portfolio PnL을 아직 재현하지 않는다.

## 8. UI 이벤트 메모리 상한

시세 callback과 엔진은 화면 소비 속도를 기다리지 않는다. `price`, 후보, 진단, runtime health, portfolio 같은 고빈도 상태 이벤트는 유형·시장별 최신값으로 병합하고, 로그·수명주기 이벤트는 FIFO 순서를 유지한다. 두 저장영역의 합계는 기본 5,000건(`event_buffer_max_items`)을 넘지 않으며, 포화 시 오래된 병합 이벤트를 우선 제거하고 제거 건수를 경고로 노출한다. 이 정책은 화면에 모든 중간 tick을 보존하는 것보다 거래 thread의 비차단성과 메모리 상한을 우선한다.

## 9. 실전 적용 전 확인

1. Upbit API Key에서 출금 권한을 제거하고 필요한 조회·주문 권한과 허용 IP만 설정한다.
2. Windows 시스템 시각 동기화를 확인한다.
3. Public smoke와 전체 회귀테스트가 현재 배포본에서 통과했는지 확인한다.
4. 첫 운용은 감당 가능한 제한된 계정 자금으로 시작하고 로그·주문내역·관리수량을 함께 대조한다.
5. `DATA_INSUFFICIENT`, stale, `QUARANTINED`, pending reconciliation 경고를 무시하고 강제로 거래를 늘리지 않는다.
6. 긴급 정지는 강제청산 기능이 아니며, 일반 종료는 관리 포지션의 전략 청산을 기다리는 DRAINING임을 이해한다.
7. DB와 업데이트 snapshot을 보존한다. pending 주문이나 관리 포지션이 있을 때 오래된 버전으로 임의 롤백하지 않는다.

## 10. 검증 범위와 남은 한계

자동 테스트와 Public smoke는 다음을 확인하는 수단이다.

- 입력 검증과 신선도 판정.
- risk budget 계산과 최소주문 차단.
- trailing stop ratchet, 최대 보유시간, managed state migration.
- 주문 payload, 상태 기록, 재시작 reconciliation.
- recorder/replay 데이터 무결성 진단.
- market universe 이상 응답, 정지/POST 경합, UI 이벤트 버퍼 상한.

Public smoke는 실제 주문을 하지 않는다. mock 주문 테스트도 거래소 실계정에서의 체결품질이나 수익성을 증명하지 않는다.

여전히 필요한 검증은 다음과 같다.

- 다양한 장세·요일·유동성 구간을 포함한 장기간 raw recording.
- 후보군과 순환 대조군의 forward net return 차이가 기간·regime별로 유지되는지 검증.
- 주문 접수 지연 뒤의 Best+IOC full/partial/no-fill simulator.
- fee·지연·유동성 축소·가격갭 stress.
- forward horizon overlap을 제거한 purged walk-forward와 untouched OOS.
- 시장·날짜·regime별 성과, MDD와 tail loss.
- 현재 절대 변동폭 proxy보다 나은 조건부 방향성 ExpectedMove 비교.
- Strategy Health가 실거래가 없는 동안 회복 근거를 얻지 못하는 문제.

따라서 V4.3.3은 **실전 실패 형태를 더 안전하게 처리하는 릴리즈**이지, 자동매매가 수익을 낸다는 보증 또는 투자 권유가 아니다.
