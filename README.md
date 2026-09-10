# JunhyunBank

업비트(Upbit) Open API를 이용하는 개인용 자동매매 데스크톱 프로그램입니다.

> ⚠️ 기본값은 `PAPER`(모의매매)입니다. `LIVE`는 실제 원화 주문을 발생시키며 사용자가 시작 시 다시 확인해야 합니다. 이 프로젝트는 출금 API를 구현하지 않습니다.

## V1 기능

- 최초 실행 시 업비트 Access Key / Secret Key 입력 및 운영체제 보안 저장소(keyring) 보관
- PAPER / LIVE 모드 분리
- 시작 / 정상 종료 / 긴급 정지
- KRW 마켓을 독립적으로 탐색하고 24시간 거래대금 기준 후보 선정
- 5분봉 이동평균 + RSI 기본 전략으로 후보별 매수/매도 판단
- WebSocket으로 후보 종목 실시간 현재가 수신
- 주문 전 리스크 관리자 강제 검증
- 보유 자산, 총 평가금액, 세션 수익률, 후보 종목, 실시간 차트, 운영 로그 표시
- SQLite에 이벤트, 거래내역, JunhyunBank 관리 포지션 저장
- 프로그램이 직접 매수한 LIVE 포지션만 자동 매도하여 기존 보유 코인 보호
- GitHub Actions 자동 테스트
- `main` 병합 시 Windows 실행파일을 만들고 `V1`, `V2`, `V3` 방식으로 GitHub Release 자동 생성

## V1 기본 리스크 설정

| 항목 | 기본값 |
| --- | ---: |
| 1회 매수금액 | 10,000 KRW |
| 최대 동시 관리 포지션 | 1 |
| 손절 | -2% |
| 익절 | +4% |
| 세션 최대 손실 | -3% |
| PAPER 시작 자금 | 1,000,000 KRW |
| 후보 종목 수 | 8 |
| 분석 봉 | 5분봉 |

이 값들은 `src/junhyunbank/config.py`에 있으며 향후 UI 설정 화면으로 이동할 예정입니다.

## 긴급 정지 동작

긴급 정지는 **신규 주문 및 전략 실행을 즉시 차단**합니다. 이미 보유한 자산을 시장가로 강제 청산하지는 않습니다. 급락 중 강제 청산이 더 큰 손실을 만들 수 있기 때문에 V1에서는 정지와 청산을 분리했습니다.

## 업비트 API Key 주의사항

- 잔고 조회와 주문에 필요한 권한만 부여하고 **출금 권한은 부여하지 마세요.**
- 업비트에 등록한 허용 공인 IP에서 실행해야 합니다. 가능하면 고정 IP를 사용하세요.
- Access/Secret Key를 GitHub, 메신저, 스크린샷, 로그에 올리지 마세요.

## 개발 실행

Python 3.11 이상에서:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e .
junhyunbank
```

비전문 사용자는 GitHub의 Releases에서 `JunhyunBank.exe`를 받아 실행하는 방식을 권장합니다. 서명되지 않은 개인 빌드이므로 Windows SmartScreen 경고가 나타날 수 있습니다.

## V1 기본 전략

1. KRW 마켓 중 유의/경고 종목을 제외합니다.
2. 24시간 거래대금이 일정 수준 이상인 종목을 정렬해 상위 후보를 선정합니다.
3. 후보별 5분봉을 분석합니다.
4. 단기 이동평균이 장기 이동평균 위에 있고 상승 중이며 RSI가 설정 구간에 있을 때 매수 후보가 됩니다.
5. 여러 매수 후보가 있으면 추세 점수가 높은 종목을 우선합니다.
6. 전략 매도 신호 또는 손절/익절 조건이 발생하면 관리 포지션을 매도합니다.

이 전략은 V1의 동작 검증용 기본 전략이며 수익을 보장하지 않습니다. 이후 버전에서 백테스트 결과를 근거로 반복 개선합니다.

## 코드 구조

```text
src/junhyunbank/
├── upbit.py          # REST/JWT/주문
├── market_stream.py  # WebSocket 실시간 시세
├── strategy.py       # 교체 가능한 매매 전략
├── risk.py           # 손실/포지션/주문 제한
├── engine.py         # 자동매매 실행 엔진
├── storage.py        # SQLite 기록 및 상태 복구
├── security.py       # API Key 보안 저장
├── ui.py             # 데스크톱 UI
└── main.py           # 프로그램 시작점
```

## 버전 정책

- `1.x.x` → GitHub Release `V1`
- `2.x.x` → GitHub Release `V2`
- `3.x.x` → GitHub Release `V3`

전략 변경은 기존 거래소/리스크/UI 코드를 최대한 건드리지 않고 `strategy.py`와 설정을 중심으로 진행합니다.
