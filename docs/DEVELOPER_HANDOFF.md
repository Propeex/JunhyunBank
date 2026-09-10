# 현재 기준: V4.0.0

먼저 [V4 감사 및 구현 변경](V4_AUDIT.md)을 읽으세요. 아래 V2 문서는 역사적 설계 기록입니다. 주문 복구·관리 수량·재시작·릴리즈 태그 설명은 V4 문서가 우선합니다. 현재 릴리즈 태그는 전체 버전 V4.0.0 형태입니다.

# JunhyunBank 개발 인수인계 문서

작성 기준: **V2 / 2.0.0**, `main` 기준 릴리즈 V2 이후.

이 문서는 이전 대화가 모두 사라져도 새 개발자 또는 새 ChatGPT가 JunhyunBank의 목적, 구현 의도, 안전 원칙, 현재 상태와 다음 작업을 이해하고 바로 개발을 이어갈 수 있도록 작성했습니다.

---

## 1. 프로젝트 한 줄 정의

**JunhyunBank는 Upbit KRW 마켓을 24시간 실시간 감시하고, 체결/호가 기반 단기 모멘텀을 찾아 실제 원화로 자동 매수·매도하는 Windows 데스크톱 프로그램입니다.**

V2부터는 모의매매(PAPER) 모드를 제거하고 LIVE 실전매매 전용으로 운영합니다.

---

## 2. 사용자가 원하는 제품 방향

사용자는 비개발자이며, 세부 기술 선택은 개발자가 판단해 완성도 높은 형태로 구현하는 것을 선호합니다. 기능 요구를 받으면 불필요한 기술 질문을 반복하기보다 제품 의도를 해석해 적절한 구조를 설계하고 구현합니다.

### 반드시 유지할 사용자 요구

1. **V2 이후 오직 실전(LIVE) 모드만 사용**
2. 프로그램이 Upbit 시장을 24시간 지속 감시하고 스스로 종목/진입/청산을 판단
3. 단타 전략이 핵심
4. `시간당 N회`, `1회 최대 N원`, `최대 N종목` 같은 임의의 고정 거래한도를 전략에 두지 않음
5. 대신 신호 품질, 시장상태, 거래비용, 유동성, 전략 건강도를 이용해 거래 여부와 주문금액을 동적으로 결정
6. **업데이트 버튼**으로 최신 GitHub Release의 `JunhyunBank.exe`를 내려받아 현재 파일을 교체하고 자동 재시작
7. 업데이트 후 API Key를 다시 입력하지 않아야 함
8. 보유자산 목록에 KRW 원화도 표시
9. UI에서 보유자산은 넓게, 운영로그는 좁게 배치
10. 일반 `종료`를 누르면 신규매수만 즉시 중단하고, JunhyunBank 관리 포지션은 전략 청산이 끝날 때까지 계속 관리한 뒤 종료
11. `긴급 정지`는 일반 종료와 별개이며 즉시 전략/신규주문을 멈추되 보유자산을 강제 시장가 청산하지 않음
12. 사용자가 프로그램 실행 전부터 갖고 있던 코인을 JunhyunBank가 임의로 매도하지 않음
13. 완성 버전은 **V1, V2, V3 ...** 형태로 GitHub Release 발행

---

## 3. 절대 변경하면 안 되는 안전 원칙

아래 원칙은 전략 파라미터가 아니라 시스템 안전장치입니다. 사용자가 명시적으로 다른 동작을 요구하지 않는 한 유지합니다.

- **출금 API를 구현하지 않는다.**
- Access Key / Secret Key를 GitHub, SQLite, 로그에 저장하지 않는다.
- API Key는 OS 자격증명 저장소(`keyring`)에 보관한다.
- 기존 사용자 보유자산과 JunhyunBank 자동매매 포지션을 구분한다.
- 자동매도는 JunhyunBank가 직접 연 **관리 수량(managed quantity)** 에만 적용한다.
- 실시간 trade/orderbook 데이터가 오래되면 신규매수하지 않는다.
- 반복 API 오류 시 신규매수를 차단한다.
- 거래소 최소 주문금액을 지킨다.
- Upbit API Rate Limit은 거래전략상의 한도가 아니라 반드시 지켜야 할 운영 제약이다.
- 주문 요청 직전에 RUNNING / emergency 상태를 다시 검사해 종료 또는 긴급정지와 경쟁조건이 생겨도 신규매수가 나가지 않게 한다.
- 주문마다 고유 `identifier`를 사용한다.
- 긴급정지는 강제청산 버튼이 아니다.
- 업데이트 중 관리 포지션 DB를 삭제하거나 API Key를 초기화하지 않는다.

---

## 4. 버전 역사

### V1

초기 MVP. PAPER/LIVE 모드, 5분봉 이동평균 + RSI 전략, 고정 주문금액/손절/익절/세션 손실제한, ticker WebSocket, 기본 UI와 SQLite 관리상태를 제공했습니다.

V1 Release는 GitHub `V1` 태그로 발행되었습니다.

### V2

V1 전략과 실행구조를 크게 개편한 현재 버전입니다.

- 버전: `2.0.0`
- Release tag: `V2`
- main 병합 commit: `12b7e50148e9e4567603d3928fc16c8c4d984787`
- V2 PR: `#2` — `V2: LIVE MicroFlow 전략·자동 업데이트·UI 개선`
- Release: `https://github.com/Propeex/JunhyunBank/releases/tag/V2`
- Windows asset: `JunhyunBank.exe`
- V2 asset SHA-256: `10d2e5fdcccb92a2332685e70db6a7b69ec6cd76f1dc03b5bccb93b894645121`

V2 주요 변경:

- PAPER 제거, LIVE 전용
- JH-MicroFlow 실시간 단타 전략
- 전체 KRW `trade` WebSocket 감시
- Hot 후보만 `orderbook` 정밀 분석
- 상대 percentile 기반 Activity/Aggression/Book/Momentum
- IGNITION / PULLBACK 진입
- 실제 계정 수수료 + spread + 호가 slippage 비용 필터
- 고정 주문금액/횟수/포지션 수/익절/손절 제거
- 동적 자금배분
- Adaptive Trailing / Emergency Stop / 기대시간 실패 청산
- Strategy Health Governor
- DRAINING 종료
- KRW 보유자산 표시 및 UI 재배치
- GitHub Release 자동 업데이트 + SHA-256 검증 + rollback
- V1 SQLite schema 자동 확장

---

## 5. 현재 소스 구조

핵심 모듈은 `src/junhyunbank/` 아래에 있습니다.

| 파일 | 역할 |
|---|---|
| `main.py` | 프로그램 시작, API Key 확인, `--resume-trading` 처리 |
| `ui.py` | PySide6 UI, 시작/종료/긴급정지/업데이트 |
| `engine.py` | 시장 감시, 포트폴리오, 전략 호출, 주문 실행, DRAINING |
| `strategy.py` | JH-MicroFlow 실시간 신호 계산 및 진입/청산 판단 |
| `health.py` | 최근 실전 성과 기반 Strategy Health 0~1 계산 |
| `market_stream.py` | Upbit public WebSocket trade/orderbook 수신 |
| `upbit.py` | REST/JWT 인증, 계좌/주문/주문조회, private rate guard |
| `risk.py` | 고정 투자한도가 아닌 시스템 안전조건 검사 |
| `storage.py` | SQLite 이벤트/거래/관리 포지션/전략결과 저장 |
| `security.py` | OS keyring API Key 저장 |
| `updater.py` | GitHub latest Release 확인, digest 검증, EXE 교체/restart |
| `config.py` | SafetyConfig / StrategyConfig |
| `models.py` | Signal, EngineState, Position 등 데이터 구조 |

테스트는 `tests/`에 있고 GitHub Actions가 PR과 main에서 `pytest`를 실행합니다.

---

## 6. 런타임 상태기계

### EngineState.STOPPED

자동매매 정지 상태.

### EngineState.RUNNING

시장 감시, 신규진입, 포지션 청산 모두 허용.

### EngineState.DRAINING

사용자가 일반 `종료`를 눌렀을 때 진입.

- 즉시 신규매수 금지
- 이미 관리 중인 포지션은 MicroFlow 청산 규칙을 계속 적용
- `managed_positions`가 비면 `drain_complete` 이벤트 발생 후 엔진 종료

### Emergency

`RiskManager.emergency = True` + hard stop.

- 전략 및 신규주문 즉시 중단
- 기존 포지션 강제청산하지 않음

### Update shutdown

업데이트는 일반 DRAINING이 아닙니다.

- 관리 포지션을 SQLite에 그대로 유지
- 엔진을 일시 hard stop
- 새 EXE로 교체
- 업데이트 직전 거래 중이었다면 `--resume-trading`으로 재실행
- 재실행 후 저장된 API Key와 managed position을 사용해 관리 재개

---

## 7. JH-MicroFlow 전략의 제품 의도

전략의 핵심 철학은 다음과 같습니다.

> 사람이 모든 코인을 24시간 동시에 보고 호가/체결 변화를 수초 단위로 계산하는 것은 어렵지만 프로그램은 가능하다. 따라서 장기 예측보다 **실시간 수급 변화가 시작되는 짧은 구간**을 찾아 비용을 넘을 가능성이 있는 움직임만 거래한다.

중요한 원칙:

- 단순 상승률 상위 종목 추격매수 금지
- RSI 하나, 이동평균 하나, OFI 하나만으로 매수하지 않음
- 각 코인을 절대값이 아니라 **자기 자신의 최근 평소 상태 대비 상대적 이상현상**으로 비교
- 거래량 증가 + 공격적 매수체결 + 호가상승 압력 + 가격 모멘텀이 동시에 살아 있어야 함
- 좋은 신호라도 수수료/spread/slippage를 넘을 기대 움직임이 없으면 매수하지 않음
- 산 뒤에는 고정 +N% 익절을 기다리는 것이 아니라 **왜 샀는지의 근거가 유지되는지** 계속 검사

상세 전략은 `STRATEGY_JH_MICROFLOW.md`를 참조합니다.

---

## 8. 중요한 설계 논의와 실제 V2 구현의 차이

**매우 중요:** 과거 전략 설계 과정에서 논의한 모든 아이디어가 V2에 구현된 것은 아닙니다. 아래를 혼동하지 마세요.

### 실제 V2에 구현됨

- 초 단위 trade frame
- 체결 없는 초를 0거래/보합 프레임으로 채워 실제 wall-clock 시간창 유지
- 오래 거래가 없는 종목의 오래된 baseline 제거
- Activity percentile
- Aggression(BID vs ASK 체결대금) percentile
- Orderbook imbalance / microprice bias / imbalance 변화 percentile
- Momentum percentile
- 4개 factor의 geometric mean = quality
- HotScore
- Market regime: PANIC / RISK_OFF / NEUTRAL / RISK_ON / EUPHORIA
- IGNITION / PULLBACK 분류
- expected move = 최근 30초 절대수익 분포의 70% quantile 계열
- 실제 수수료 조회
- spread 및 orderbook 기반 slippage simulation
- 동적 capital fraction
- Strategy Health multiplier
- Adaptive trailing
- 진입 시 emergency risk 저장 후 불리한 방향으로 확대하지 않음
- signal reset 후 재진입

### 설계에는 있었지만 V2에 완전히 구현되지 않음

1. **Bootstrap Damped Kelly**
   - 설계상 유사 신호의 승률/손익분포를 bootstrap해 Kelly 하단값으로 자금배분하려 했음.
   - 현재 V2는 `edge × quality × health × regime_factor`의 단순 곱으로 `capital_fraction`을 계산함.

2. **Correlation Penalty / 포트폴리오 중복위험**
   - 여러 알트가 사실상 동일 BTC 베타일 때 중복 노출을 줄이려는 설계였음.
   - 현재 V2에는 명시적 상관관계 패널티가 없음.

3. **Shadow/Virtual Trading 기반 Health 자동 복귀**
   - 설계상 StrategyHealth=0일 때 실제 매수는 멈추되 가상 체결을 계속 기록해 회복 여부를 판단하려 했음.
   - 현재 V2는 health가 0이면 신규진입이 정지되며 별도 shadow outcome을 생성하지 않음. 포지션도 없다면 자동 회복 경로가 사실상 부족함. **우선 개선 대상.**

4. **장기 L2 orderbook recorder + replay backtester**
   - 전략의 진짜 수익성 검증에 필요하지만 아직 구현되지 않음.

5. **Private WebSocket `myOrder` / `myAsset` 중심 주문상태 머신**
   - 현재는 주문 제출 후 REST `GET /v1/order` polling으로 체결을 확인함.
   - production hardening 대상.

6. **엄밀한 OFI(Order Flow Imbalance)**
   - 현재 Book factor는 weighted depth imbalance, microprice bias, imbalance 변화로 구성됨.
   - 학술적/이벤트 레벨 OFI를 완전히 재현하지는 않음.

따라서 다음 버전에서 “설계대로 이미 되어 있을 것”이라고 가정하지 말고 코드 상태를 확인합니다.

---

## 9. 주문 실행 의도

### 진입

일반 진입은 Upbit `best + IOC` 사용.

- 매수: KRW 주문총액(`price`)
- 매도: 코인 수량(`volume`)
- 잔량은 IOC 특성상 취소
- accepted order의 uuid를 이용해 REST 주문상태 확인
- 실제 체결량/평균체결가를 기록

시장가 매수는 V2 일반 진입에서 기본 방식이 아닙니다.

### 청산

일반 청산도 `best + IOC` 우선.

Emergency Stop 청산에서 IOC가 전혀 체결되지 않으면 시장가 매도를 fallback으로 허용합니다.

### 관리수량

매수 체결 후 실제 executed volume을 `managed_quantity`에 저장합니다.

사용자 계정에 같은 코인의 별도 보유분이 있어도 JunhyunBank가 매도하려는 수량은 관리수량을 넘지 않아야 합니다.

---

## 10. API Key와 업데이트 의도

`security.py`의 keyring 저장은 실행파일과 분리되어 있습니다. 따라서 `JunhyunBank.exe`를 교체해도 API Key는 유지됩니다.

업데이트 흐름:

1. GitHub `releases/latest` 조회
2. `JunhyunBank.exe` asset 탐색
3. asset의 GitHub `digest`가 `sha256:<hash>`인지 확인
4. `~/.junhyunbank/update/JunhyunBank.new.exe`로 다운로드
5. SHA-256 일치 확인
6. 별도 PowerShell updater 실행
7. 부모 프로세스 종료 대기
8. 기존 EXE를 `.old`로 이동
9. 새 EXE 교체
10. 교체 실패 시 가능한 경우 `.old` 복구
11. 성공 시 새 EXE 실행
12. 거래 중 업데이트였으면 `--resume-trading`
13. `.old` 삭제

자동 업데이트는 PyInstaller Windows frozen executable에서만 동작하도록 제한되어 있습니다.

---

## 11. SQLite 영속 상태

기본 경로: `~/.junhyunbank/junhyunbank.db`

핵심 테이블:

- `events`
- `trades`
- `managed_positions`
- `strategy_outcomes`

V2의 `managed_positions`에는 최소 다음 상태가 추가됩니다.

- `entry_price`
- `entry_amount_krw`
- `entry_fee_rate`
- `initial_risk_pct`
- `entry_score`
- `signal_kind`
- `peak_price`
- `expected_horizon_seconds`
- `managed_quantity`

V1 DB를 발견하면 컬럼을 `ALTER TABLE`로 추가해 마이그레이션합니다.

주의: V1에는 managed quantity가 없었기 때문에 V1 관리표시를 V2가 복구할 때 현재 계정의 해당 종목 전체 수량을 관리수량으로 간주하는 fallback이 있습니다. 사용자가 V1 이후 같은 코인을 별도로 추가 매수했다면 이 migration 방식은 완벽하지 않을 수 있습니다. 향후 migration/reconciliation 기능을 개선해야 합니다.

---

## 12. 현재 검증 완료 상태

V2 PR과 main에서 GitHub Actions pytest가 통과했고, Windows release workflow에서도 다음 단계가 모두 성공했습니다.

- Python 설치
- package install
- pytest
- PyInstaller one-file Windows build
- V2 tag resolve
- GitHub Release 생성
- `JunhyunBank.exe` asset upload

그러나 이것은 **코드/패키징 검증**이지 전략 수익성 검증이 아닙니다.

실제 기대수익, MDD, slippage 내성, regime별 성능 등은 아직 충분한 실시간 L2 데이터셋이 없어 검증되었다고 말하면 안 됩니다.

---

## 13. 현재 알려진 중요 리스크 / 다음 개발 우선순위

상세 내용은 `VALIDATION_AND_ROADMAP.md`를 참조하되, 새 개발자가 가장 먼저 알아야 할 순서는 다음과 같습니다.

### P0/P1 수준으로 우선 검토

1. **실제 Upbit 계정에서 소액 주문 전 end-to-end 체결 상태 머신 검증**
2. Partial fill / IOC / REST polling timeout / network ambiguity에 대한 reconciliation 강화
3. Private `myOrder` / `myAsset` WebSocket 도입 검토
4. Strategy Health가 0이 된 뒤 shadow simulation 없이 영구정지될 가능성 해결
5. 실시간 trade + orderbook raw recorder와 replay/backtest 엔진 구현
6. 주문금액 동적 계산이 너무 공격적으로 계좌 대부분을 한 신호에 배분할 수 있는지 검증
7. 다중 포지션 간 correlation / shared market risk 반영
8. V1→V2 managed quantity migration의 사용자 보유분 혼합 위험 개선

### P2

- UI chart 종목 선택
- strategy decision/expected cost/entry reason 상세 표시
- 거래 리포트 및 분석 화면
- parameter versioning
- 진단 export 기능

---

## 14. 개발 및 릴리즈 규칙

권장 작업 흐름:

1. `main` 최신 상태 확인
2. 새 버전용 개발 branch 생성 (예: `v3-development`)
3. Upbit API 변경을 다루면 공식 문서 최신 사양 재확인
4. 코드 + 테스트 + 관련 문서 동시에 수정
5. PR 생성
6. CI 통과
7. 코드 diff 및 실거래 edge-case 검토
8. `main` 병합
9. 메이저 버전 완성 시 `__version__`/`pyproject.toml`을 `3.0.0`, `4.0.0` 등으로 올림
10. `main` push 시 release workflow가 `V<major>` 생성
11. Windows Release asset과 digest 확인

현재 release workflow는 해당 메이저 태그가 이미 존재하면 release 생성을 skip합니다. 같은 `2.x.x`에서 EXE만 다시 만들고 V2 asset을 자동 교체하는 구조가 아니므로, 배포 정책을 바꿀 경우 workflow부터 수정해야 합니다.

---

## 15. 다음 ChatGPT/개발자에게 주는 시작 지침

대화 내용이 없고 이 저장소만 받았다면 다음 순서로 시작하세요.

1. 이 문서를 끝까지 읽는다.
2. `ARCHITECTURE.md`와 `STRATEGY_JH_MICROFLOW.md`를 읽는다.
3. `VALIDATION_AND_ROADMAP.md`의 P0/P1을 확인한다.
4. 반드시 현재 `main`의 `engine.py`, `strategy.py`, `upbit.py`, `storage.py`, `updater.py`를 실제로 읽어 문서와 차이가 없는지 확인한다.
5. 사용자의 새 요구사항이 기존 안전 원칙과 충돌하는지 먼저 판단한다.
6. 전략 변경 시 “수익 보장”을 주장하지 않는다. 테스트 통과와 수익성 검증을 분리한다.
7. 완성된 새 메이저 버전은 반드시 V3, V4 식으로 GitHub Release까지 확인한다.
8. 개발 후 이 문서를 최신 상태로 갱신한다.

이 문서의 목적은 단순 설명이 아니라 **프로젝트의 설계 기억을 GitHub에 보존하는 것**입니다.
