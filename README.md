# JunhyunBank V2

업비트 KRW 마켓을 24시간 감시하는 개인용 LIVE 자동매매 데스크톱 프로그램입니다.

> **주의:** V2는 모의매매 모드가 없습니다. `시작` 버튼은 실제 업비트 계정에서 실제 원화 주문을 실행합니다. API Key에는 출금 권한을 부여하지 마세요.

## V2 핵심 변경

- LIVE 전용: PAPER/LIVE 선택 제거
- JH-MicroFlow V2: 전체 KRW 체결 스트림 감시 + Hot 후보의 호가 정밀 분석
- Activity / Aggression / Book Pressure / Momentum을 종목별 rolling percentile로 정규화
- IGNITION / PULLBACK CONTINUATION 진입
- 실제 계정 수수료, 스프레드, 호가 기반 예상 슬리피지를 반영한 거래비용 필터
- 고정 1회 주문금액, 시간당 거래횟수, 최대 보유종목 수 제한 제거
- 신호 품질·시장상태·최근 전략성과·현재 호가 유동성으로 주문금액을 동적 계산
- 고정 익절/손절 대신 수급 약화, Adaptive Trailing, 동적 Emergency Stop, 기대시간 실패 청산
- Strategy Health Governor: 최근 위험 정규화 성과가 검증 분포에서 악화되면 노출을 축소하며 심각한 악화 시 신규 진입을 0으로 만듦
- 업비트 시장 경보 종목 신규 진입 차단
- JunhyunBank가 직접 연 포지션 수량만 자동매도

## 종료 동작

`종료`를 누르면 즉시 신규 매수를 차단하고 엔진 상태가 `DRAINING`으로 바뀝니다. JunhyunBank가 관리 중인 포지션은 MicroFlow의 정상 청산 규칙에 따라 계속 관리하며, 모두 청산된 뒤 자동매매 엔진이 종료됩니다.

`긴급 정지`는 다릅니다. 전략/주문을 즉시 중지하며 현재 보유 포지션을 강제로 매도하지 않습니다.

## 업데이트

V2부터 상단 `업데이트` 버튼을 지원합니다.

1. GitHub의 최신 Release를 확인합니다.
2. 새 `JunhyunBank.exe`를 다운로드합니다.
3. GitHub Release가 제공하는 SHA-256 digest와 다운로드 파일을 비교합니다.
4. 검증에 성공한 경우 현재 실행파일을 교체합니다.
5. 프로그램을 자동 재시작합니다.
6. 업데이트 직전 자동매매가 실행 중이었다면 재시작 후 LIVE 감시를 자동 재개합니다.

API Key는 실행파일 내부가 아니라 운영체제의 keyring에 저장되므로 업데이트 후 다시 입력할 필요가 없습니다. 거래/포지션 상태는 `~/.junhyunbank/junhyunbank.db`에 유지됩니다.

V1에는 업데이트 버튼이 없으므로 **V1 → V2 전환만 GitHub V2 Release에서 실행파일을 한 번 직접 내려받아 교체**해야 합니다. 이후 V2 → V3부터는 업데이트 버튼을 사용할 수 있습니다.

## UI

- 좌측: 실시간 후보 및 운영 로그
- 우측 상단: 넓은 보유자산 목록
- 우측 하단: 실시간 가격 차트
- 보유자산 첫 행에 KRW 원화 잔고 표시
- 각 코인에 `JunhyunBank` 자동관리 여부 표시
- 전략 건강도와 시장 Regime 표시

## 데이터/실행 구조

전체 KRW 마켓의 `trade` WebSocket을 여러 연결로 분할해 실시간 수집합니다. 1초 단위 프레임으로 거래대금과 BID/ASK 체결을 집계하며, 상대적으로 뜨거워진 후보에 대해서만 `orderbook`을 추가 구독해 5개 호가쌍의 OBI·Microprice·호가 변화 및 실제 체결 가능 금액을 분석합니다.

주문은 일반 진입/청산에 Upbit `best + IOC`를 사용합니다. Emergency Stop 청산에서 IOC가 체결되지 않을 경우 시장가 매도를 보조 수단으로 사용할 수 있습니다. 거래소 자체 API Rate Limit은 전략상의 거래횟수 제한과 별개로 항상 준수합니다.

## 보안

- Access Key / Secret Key는 GitHub, SQLite, 로그에 저장하지 않습니다.
- Python `keyring`을 통해 OS 자격증명 저장소를 사용합니다.
- 출금 API는 구현하지 않습니다.
- API 오류 반복, 오래된 trade/orderbook 데이터, 최소 주문금액 미달에서는 신규 주문을 차단합니다.

## 개발/테스트

```bash
python -m pip install -e ".[dev]"
pytest -q
python launcher.py
```

`main` 병합 시 GitHub Actions가 Windows에서 테스트 후 `JunhyunBank.exe`를 빌드하고 패키지의 메이저 버전에 맞는 Release(`V2`, `V3`, ...)를 생성합니다.

## 전략 검증에 대한 원칙

코드 테스트 통과는 수익성 검증을 뜻하지 않습니다. V2는 실시간 수급 규칙을 구현하지만, 실제 기대수익은 축적된 업비트 체결/호가 데이터에 수수료·슬리피지·주문지연을 반영한 워크포워드 검증으로 별도로 평가해야 합니다. 모든 실거래 손익의 책임은 사용자에게 있습니다.
