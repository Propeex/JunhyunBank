# Changelog

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
