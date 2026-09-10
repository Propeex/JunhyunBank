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
