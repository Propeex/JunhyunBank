# Changelog

## V1 / 1.0.0

- 업비트 API Key 최초 입력 및 OS keyring 보관
- PAPER / LIVE 모드 분리
- 시작 / 종료 / 긴급 정지 UI
- KRW 마켓 거래대금 기반 후보 자동 선정
- 5분봉 Trend + RSI 기본 전략
- WebSocket 실시간 가격 수신
- 시장가 매수/매도 및 고유 주문 identifier
- 손절/익절, 일일 최대 손실, 주문금액, 포지션 수 제한
- JunhyunBank가 직접 연 LIVE 포지션만 자동 매도하도록 격리
- 보유자산, 평가금액, 세션 수익률, 후보, 차트, 로그 UI
- SQLite 이벤트/거래/관리 포지션 기록
- pytest 및 GitHub Actions CI
- main 병합 시 Windows 실행파일 빌드 및 V1/V2/V3 자동 Release
