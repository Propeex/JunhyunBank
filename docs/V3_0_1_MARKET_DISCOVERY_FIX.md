# V3.0.1 Market Discovery Fix

기준 증상: JunhyunBank V3 실사용 화면에서 자동매매 시작 후 5분 이상 `Trade WS 0/0`, `추적 0/0`, `워밍업 완료 0`, `후보 0`, `Orderbook 대기(0)`가 지속됨.

## 1. 이 증상이 의미하는 것

이 화면은 단순히 전략 진입조건이 까다로워 매수가 없다는 뜻이 아니다. `Trade WS 0/0`과 `추적 0/0`은 전략 평가 이전 단계인 **KRW 마켓 유니버스 자체가 0개**라는 뜻이다.

정상이라면 자동매매 시작 직후 `/v1/market/all`에서 KRW 페어 목록을 얻고, 경보 종목을 제외한 뒤 100개 단위의 Public trade WebSocket 연결을 생성한다. 이후 수초 안에 `Trade WS n/n`과 `추적 x/y`가 0이 아닌 값으로 올라가야 한다. 후보는 180초 warmup 이후에 형성될 수 있으므로 후보 0 자체는 정상일 수 있지만, `Trade WS 0/0`은 정상 대기가 아니다.

## 2. 확인된 취약점

### 2.1 빈 유니버스가 정상 결과처럼 조용히 통과

기존 base engine은 필터 결과가 빈 리스트이고 초기 `_allowed_markets`도 빈 리스트이면 변경이 없다고 판단하여 `_restart_global_streams()`도, `market_universe` 이벤트도 발생시키지 않았다. 결과적으로 UI는 0/0인 채 전략 대기처럼 보였다.

### 2.2 경보 필드에 Python truthiness 사용

기존 `_is_warning_market()`은 `bool(value)` 계열 판단을 사용했다. JSON Boolean이면 문제가 없지만, 호환 계층/프록시/스키마 변화로 `"false"`, `"0"` 같은 문자열이 들어오면 Python에서는 모두 True가 된다. 이런 응답 변형이 생기면 정상 종목까지 전부 경보 종목으로 오인하여 유니버스가 0개가 될 수 있다.

V3.0.1은 명시적으로 알려진 true/false 표현만 해석한다. 알 수 없는 형식은 임의로 거래 가능하다고 추정하지 않고 해당 종목을 제외하며, 모든 KRW 종목을 안전하게 해석하지 못하면 명확한 오류를 기록한다.

### 2.3 상세 조회 파라미터 호환성

현재 Upbit 문서는 `is_details=true`를 사용한다. 다만 과거 API/클라이언트 예제에는 `isDetails=true` 표기가 널리 사용되어 왔다. V3.0.1은 첫 상세 조회 응답에 `market_event`가 전혀 없을 때만 legacy 표기로 1회 재조회한다. 이 재조회는 경보정보 없는 상태에서 LIVE 거래를 계속하는 것보다 안전한 호환 조치다.

### 2.4 빈 유니버스 상태의 과도한 REST 재호출 가능성

base loop는 `_allowed_markets`가 비어 있으면 다음 정기 갱신 시각과 무관하게 마켓 갱신을 다시 시도한다. 빈 결과가 예외 없이 반복될 경우 매우 빠른 반복호출이 가능했다. V3.0.1은 실패한 마켓 탐색에 10초 backoff를 둔다.

## 3. V3.0.1 변경 범위

변경한 것은 시장 데이터 진입 경로와 릴리즈 검증이다.

- Upbit market rows 응답 형식 검증
- current `is_details` → 필요 시 legacy `isDetails` 1회 fallback
- 명시적 market alert Boolean/string parser
- KRW 전체 수, 경보 제외 수, 형식 미확인 제외 수를 로그에 표시
- 안전 유니버스 0개를 오류로 승격
- 실패 시 10초 market-discovery retry backoff
- 실제 `/v1/market/all` 결과를 같은 parser로 통과시킨 뒤 WebSocket trade/orderbook을 받는 read-only smoke test
- Windows/Ubuntu PR CI에서 위 smoke test 실행
- 패치 버전이 업데이트 버튼에 노출될 수 있도록 full semantic version Release tag 사용

## 4. 의도적으로 건드리지 않은 것

이번 수정은 매수 빈도를 높이는 패치가 아니다. 아래 거래 규칙은 변경하지 않는다.

- IGNITION / PULLBACK 품질 임계값
- ExpectedMove 및 cost gate
- capital fraction 및 liquidity capacity
- stale data gate
- Strategy Health / Market Regime
- managed quantity 보호
- Adaptive Trailing / Emergency Stop
- DRAINING / 긴급 정지 동작
- 기존 사용자 보유자산 보호

따라서 V3.0.1에서 후보가 정상적으로 표시되어도 매수 신호가 없으면 주문하지 않는 것이 정상이다.

## 5. 정상 화면 판정 기준

업데이트 후 LIVE 자동매매를 시작했을 때 다음 순서가 정상이다.

1. 운영 로그에 `KRW 실시간 감시 종목 N개 (전체 KRW ... )`가 표시된다.
2. 수초 내 `Trade WS n/n`에서 n이 1 이상이 된다.
3. `추적 x/y`에서 y가 0이 아니고 x가 빠르게 증가한다.
4. KRW-BTC 기본 차트가 움직인다.
5. 약 3분 동안 `워밍업 완료` 종목 수가 증가한다.
6. 시장상태에 따라 후보가 생기면 후보별 현재가와 Q가 표시된다.

반대로 `Trade WS 0/0`이면 전략 대기가 아니라 마켓 탐색 실패다. V3.0.1에서는 이 경우 운영 로그에 원인을 숨기지 않고 KRW 수신 건수/경보 필터 수/스키마 미확인 수를 남긴다.

## 6. Release gate

V3.0.1 이후 Release는 단순히 고정 `KRW-BTC` WebSocket만 확인하지 않는다. CI가 실제 market discovery를 먼저 실행하고, 그 응답에서 안전 KRW market이 1개 이상 만들어지는지 확인한 뒤 선택한 market의 `trade`와 `orderbook.5`를 실제 Upbit Public WebSocket에서 받아야 성공한다.

이 검증은 API Key와 주문 API를 사용하지 않는 read-only smoke test다.

## 7. 여전히 남는 별도 P0/P1 과제

이번 증상과 직접 관련 없는 기존 위험은 그대로 남는다.

- ambiguous POST timeout/restart recovery를 위한 durable pending-order state machine
- private `myOrder`/`myAsset` 기반 체결/잔고 reconciliation
- partial exit 전체 가중평균 손익 기록
- ExpectedMove를 실제 conditional forward edge로 대체하기 위한 recorder/replay/OOS 검증
- update 성공 health handshake와 자동 rollback

이 항목은 시장 데이터 수신 복구와 분리하여 후속 단계에서 진행한다.
