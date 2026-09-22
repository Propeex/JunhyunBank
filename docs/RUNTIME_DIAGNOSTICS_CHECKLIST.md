# Runtime Diagnostics Checklist

## 정상 시작 기대 흐름

1. pending 주문, 특히 이전 실행의 SELL을 identifier REST로 먼저 reconciliation
2. 계정 조회 후 관리전략 실현·평가손익 PnL baseline 구성
3. 계정 또는 baseline 확인 실패 시 “청산 유지 / 해당 세션 신규매수 영구 잠금” 경고 확인
4. 안전 market discovery를 마친 뒤 Public trade WebSocket 연결
5. 종목별 완료 frame 180개, 유효 실체결 초 60개, 유효 orderbook 관측 10회 조건 충족
6. fresh actionable 후보에 HotScore와 BookQ가 형성되고 candidate event가 UI에 표시
7. 비용·시장상태·품질·Best+IOC 최우선호가 capacity 조건을 모두 통과한 경우에만 실제 매수

## 비정상 판단 기준

- 시작 60초 이후에도 trade message를 한 건도 못 받음
- 시작 5분 이후 candidate가 계속 0개
- orderbook age가 계속 무한/3초 초과
- WebSocket reconnect 에러가 반복됨
- 후보는 있는데 차트/현재가가 전혀 갱신되지 않음
- 시작 계정/전략 PnL baseline 경고 뒤에도 신규매수가 열림

위 경우 전략이 보수적인 것이 아니라 데이터 파이프라인 문제로 간주한다.

## 현재 UI/로그에서 확인할 진단값

- Trade WS: CONNECTED / STALE / RECONNECTING
- Orderbook WS: CONNECTED / STALE / RECONNECTING
- 감시 market 수
- warmup 진행률
- 마지막 trade 수신 경과시간
- 마지막 orderbook 수신 경과시간
- candidate 수
- 최근 WebSocket 에러
- 신규 진입 차단 이유
- pending 주문 reconciliation 상태
- market discovery 안전검사와 세션 신규매수 영구 잠금 경고

## Release 실연결 체크

CI 단위테스트 외에 Windows Release candidate에서 실제 Upbit Public WebSocket을 연결하여 다음을 최소 확인한다.

- KRW-BTC trade event 수신
- KRW-BTC.5 orderbook event 수신
- 30초 이상 지속 수신
- reconnect 후 복구
- Origin rate-limit을 유발하지 않음

이 smoke test는 주문 API를 호출하지 않아야 한다.
