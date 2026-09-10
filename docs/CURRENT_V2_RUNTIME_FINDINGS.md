# V2 Runtime Findings — 2026-09-10

## 사용자 실전 관찰

V2 자동매매 시작 후 10분 이상:

- 매수 주문 없음
- 실시간 후보/후보 가격 표시가 사실상 비어 있음

## 결론

매수 없음 자체는 전략 조건 미충족일 수 있으므로 정상일 수 있다. 그러나 `min_warmup_seconds=180`이므로 WebSocket trade 수신이 정상이라면 약 3분 이후에는 적어도 일부 KRW 종목의 후보 점수가 형성되어 UI에 후보가 나타나는 것이 정상이다. 10분 이상 후보가 계속 비어 있다면 정상 대기만으로 설명하기 어렵다.

## P0 의심 원인: Public WebSocket Origin rate limit

현재 `MarketStream`은 `websocket.create_connection(self.URL, timeout=10)`을 호출한다. `websocket-client`는 기본 handshake에 Origin 헤더를 보낼 수 있으며, `suppress_origin=True`를 지정하지 않았다.

Upbit 공식 Rate Limits 문서에 따르면 Origin 헤더가 포함된 Quotation REST / Public WebSocket 요청에는 별도 제한이 적용되어 10초당 1회만 허용된다.

V2는:

- 전체 KRW trade stream을 최대 100 markets 단위로 여러 연결 생성
- Hot 후보 set이 바뀌면 deep orderbook stream을 stop/recreate
- candidate refresh 기본값 3초

구조이므로 Origin 제한과 충돌할 가능성이 높다. 특히 deep stream churn이 반복되면 호가 수신이 실패/불안정해지고 entry는 `book_age <= 3s` 조건을 만족하지 못해 차단될 수 있다.

### 우선 수정안

1. Public websocket 연결 시 `suppress_origin=True` 적용.
2. 후보가 바뀔 때 3초마다 deep websocket 전체를 재연결하지 않도록 persistent stream + hysteresis/residency 구조로 변경.
3. WebSocket 연결 상태/마지막 수신시각/재연결 횟수/최근 에러를 UI에 명확히 표시.
4. 시작 후 180초 warmup progress를 UI에 표시.
5. 5분 이상 candidate=0이면 사용자에게 "시장 데이터 수신 이상" 경고.
6. 네트워크 실연결 smoke test 또는 별도 수동 release checklist 추가.

## UI 관련 확인

현재 후보 라벨은 `market + hot score`만 표시한다. 가격은 후보 목록 자체에 표시하지 않는다. 차트는 최초 non-empty candidate event가 들어온 때 `_chart_market`을 지정하고, 해당 market의 `price` event가 들어와야 그려진다.

따라서 후보가 계속 비어 있으면 차트도 계속 비어 있을 수 있다. 또한 사용자가 "후보 가격"을 기대한다면 후보 라벨에 현재가를 함께 표시하는 개선이 필요하다.

## 전략 수익성 상태

현재 V2의 코드/CI/Windows build 성공은 전략 수익성을 의미하지 않는다. 특히 `expected_move`는 신호 조건부 미래수익이 아니라 최근 절대 30초 수익률의 70 percentile proxy다. 실제 자금배분에 쓰기에는 검증이 부족하다.

## 현재 운용 권고

이 runtime issue가 수정되고 실제 Upbit public trade/orderbook stream smoke test가 통과하기 전까지 V2를 unattended live trading용으로 계속 운용하지 않는 것을 권장한다.
