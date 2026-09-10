# Runtime Diagnostics Checklist

## 정상 시작 기대 흐름

1. LIVE 시작 직후 계정/시장 목록 조회 성공
2. Public trade WebSocket 연결
3. 약 180초 동안 전략 baseline warmup
4. 3분 전후부터 일부 종목에 HotScore 형성
5. candidate event가 UI에 표시
6. 후보 대상 orderbook 수신 후 BookQ 계산
7. 비용/시장상태/품질 조건을 통과한 경우에만 실제 매수

## 비정상 판단 기준

- 시작 60초 이후에도 trade message를 한 건도 못 받음
- 시작 5분 이후 candidate가 계속 0개
- orderbook age가 계속 무한/3초 초과
- WebSocket reconnect 에러가 반복됨
- 후보는 있는데 차트/현재가가 전혀 갱신되지 않음

위 경우 전략이 보수적인 것이 아니라 데이터 파이프라인 문제로 간주한다.

## 차기 구현에서 UI에 보여줄 진단값

- Trade WS: CONNECTED / STALE / RECONNECTING
- Orderbook WS: CONNECTED / STALE / RECONNECTING
- 감시 market 수
- warmup 진행률
- 마지막 trade 수신 경과시간
- 마지막 orderbook 수신 경과시간
- candidate 수
- 최근 WebSocket 에러
- 신규 진입 차단 이유

## Release 실연결 체크

CI 단위테스트 외에 Windows Release candidate에서 실제 Upbit Public WebSocket을 연결하여 다음을 최소 확인한다.

- KRW-BTC trade event 수신
- KRW-BTC.5 orderbook event 수신
- 30초 이상 지속 수신
- reconnect 후 복구
- Origin rate-limit을 유발하지 않음

이 smoke test는 주문 API를 호출하지 않아야 한다.
