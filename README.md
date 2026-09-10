# JunhyunBank V4.0.1

## V4.0.1 — 업데이트 사전검증과 원자 롤백

V4.0.1은 거래 전략을 바꾸지 않는 배포 안정화 패치입니다. 새 EXE를 교체한 뒤 곧바로 LIVE 자동매매를 재개하지 않고, 먼저 비거래 검증 모드로 현재 SQLite DB의 스키마/무결성을 확인합니다. 검증 실패 시 이전 EXE와 업데이트 직전 DB snapshot을 함께 복원합니다.

상세 설계와 전환 주의점: **[V4.0.1 업데이트 복구](docs/V4_0_1_UPDATE_RECOVERY.md)**.

## V4 — 매수 진단과 주문 복구

- 같은 창에서 정지 후 다시 시작해도 시세 연결을 정상 복구합니다.
- 일시적인 잔고 조회 오류로 엔진 전체가 종료되지 않도록 수정했습니다.
- 후보 HotScore와 실제 진입 품질을 구분하고, 종목별 **매수 대기 이유·예상 변동폭·거래비용**을 표시합니다.
- 5초 수급의 비교 기준을 과거 5초 수급으로 바로잡았습니다.
- 주문 전에 식별자를 저장해 응답 유실과 재시작 후에도 주문 결과를 조회합니다. 미확정 주문을 중복 제출하지 않습니다.
- 부분 체결은 실제 체결량·금액으로 기록하며, 관리 수량을 전체 계정 잔고로 추정하지 않습니다.

상세 원인, 수정 내용, 테스트 및 남은 한계: **[V4 감사 보고서](docs/V4_AUDIT.md)**.

업데이트 후 `시작`을 누르면 약 3분의 기본 워밍업을 거칩니다. 거래가 적은 종목은 더 오래 걸릴 수 있습니다. 높은 HotScore만으로 매수하지 않으며 비용과 진입 품질 조건까지 충족해야 합니다. V4는 매수 횟수를 늘리기 위해 이 조건을 낮추지 않았습니다.

아래 V3 설명은 계승된 기능과 변경 이력입니다. 주문·진단의 최신 동작은 위 V4 보고서를 기준으로 확인하세요.

업비트 KRW 마켓을 24시간 감시하는 개인용 **LIVE 전용** 자동매매 Windows 데스크톱 프로그램입니다.

> **주의:** 모의매매(PAPER) 모드는 없습니다. `시작` 버튼은 실제 업비트 계정에서 실제 원화 주문을 실행합니다. API Key에는 출금 권한을 부여하지 마세요.

## 개발자 / 다음 ChatGPT 인수인계

이전 대화가 없어도 개발을 이어갈 수 있도록 제품 의도, 구현 구조, 전략 명세, 검증 상태, 알려진 위험과 다음 우선순위를 GitHub에 영구 문서화했습니다.

**새 개발자 또는 새 ChatGPT 세션은 [`docs/DEVELOPER_HANDOFF.md`](docs/DEVELOPER_HANDOFF.md)부터 읽으세요.**

전체 문서 색인: [`docs/README.md`](docs/README.md)

- [`docs/V4_AUDIT.md`](docs/V4_AUDIT.md) — V4 런타임·주문·매수 경로 감사와 남은 과제
- [`docs/V4_0_1_UPDATE_RECOVERY.md`](docs/V4_0_1_UPDATE_RECOVERY.md) — 업데이트 health handshake와 EXE+DB rollback
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 런타임/모듈/주문/업데이트 구조
- [`docs/STRATEGY_JH_MICROFLOW.md`](docs/STRATEGY_JH_MICROFLOW.md) — 전략 의도, 수식, 실제 구현과 미구현 설계
- [`docs/VALIDATION_AND_ROADMAP.md`](docs/VALIDATION_AND_ROADMAP.md) — 현재 검증 수준, 리스크, P0/P1/P2 로드맵
- [`docs/UPBIT_INTEGRATION.md`](docs/UPBIT_INTEGRATION.md) — Upbit 인증/주문/WebSocket/Rate Limit 전제
- [`docs/CURRENT_V2_RUNTIME_FINDINGS.md`](docs/CURRENT_V2_RUNTIME_FINDINGS.md) — V2 실사용 중 발견된 런타임 문제
- [`docs/RUNTIME_DIAGNOSTICS_CHECKLIST.md`](docs/RUNTIME_DIAGNOSTICS_CHECKLIST.md) — 런타임 진단 체크리스트

코드와 문서가 충돌하면 현재 `main` 코드가 최종 사실이며, 기능/전략을 변경할 때 관련 인수인계 문서도 함께 갱신합니다.

## V3 핵심 변경 — 데이터 파이프라인 안정화

V3는 **JH-MicroFlow의 진입/청산 품질 기준, 비용 배수, 자금배분 공식 자체를 완화하지 않습니다.** V2 실사용에서 발견된 WebSocket/관측성 문제를 우선 수정한 런타임 안정화 릴리즈입니다.

- `websocket-client`가 자동으로 넣는 `Origin` 헤더를 Public WebSocket 연결에서 명시적으로 제거
- 모든 WebSocket 연결 시도를 프로세스 전체에서 직렬화해 Upbit 연결 rate limit을 넘지 않도록 보호
- 후보 종목이 바뀌어도 deep orderbook WebSocket을 끊지 않고 **동일 연결에 새 구독 메시지를 전송**
- deep 후보에 다시 들어오는 종목은 오래된 orderbook delta 이력을 폐기한 뒤 새 호가로 다시 워밍업
- WebSocket 서버 오류 응답을 정상 시세로 오인하지 않고 재연결/로그 처리
- 스트림 종료 시 활성 socket을 닫아 blocking `recv()`를 즉시 깨우도록 개선
- 화면에 Trade WS 연결 수, 마지막 체결 수신 시각, 추적 종목 수, 워밍업 완료 종목 수, 후보 수, Orderbook 상태 표시
- 후보 목록에 **현재가 + HotScore** 직접 표시
- 후보가 아직 없어도 기본 `KRW-BTC` 실시간 가격 차트를 표시해 데이터 수신 여부를 즉시 확인 가능
- 5분 이상 후보가 없으면 단순 전략 대기로 숨기지 않고 실시간 데이터 상태 확인 경고
- CI 및 Windows Release 전에 **실제 Upbit Public WebSocket read-only smoke test** 수행

## JH-MicroFlow 전략

V2에서 도입한 단기 수급/호가 기반 전략 구조를 V3에서도 유지합니다.

- 전체 KRW 체결 스트림 감시 + Hot 후보 orderbook 정밀 분석
- Activity / Aggression / Book Pressure / Momentum을 종목별 rolling percentile로 정규화
- IGNITION / PULLBACK CONTINUATION 진입
- 실제 계정 수수료, 스프레드, 호가 기반 예상 슬리피지를 반영한 거래비용 필터
- 고정 1회 주문금액, 시간당 거래횟수, 최대 보유종목 수 같은 임의의 전략 cap 없음
- 신호 품질·시장상태·최근 전략성과·현재 호가 유동성으로 주문금액을 동적 계산
- 고정 익절/손절 대신 수급 약화, Adaptive Trailing, 동적 Emergency Stop, 기대시간 실패 청산
- Strategy Health Governor로 최근 위험 정규화 성과 악화 시 신규 노출 축소/중단
- 업비트 시장 경보 종목 신규 진입 차단
- JunhyunBank가 직접 연 관리 수량만 자동매도

## 종료 동작

`종료`를 누르면 즉시 신규 매수를 차단하고 엔진 상태가 `DRAINING`으로 바뀝니다. JunhyunBank가 관리 중인 포지션은 MicroFlow의 정상 청산 규칙에 따라 계속 관리하며, 모두 청산된 뒤 자동매매 엔진이 종료됩니다.

`긴급 정지`는 다릅니다. 전략/신규 주문을 즉시 중지하며 현재 보유 포지션을 강제로 매도하지 않습니다.

## 업데이트

V2부터 상단 `업데이트` 버튼을 지원합니다. V4.0.1부터 새 updater가 생성하는 적용 스크립트는 다음 순서를 사용합니다.

1. GitHub 최신 Release의 `JunhyunBank.exe`를 다운로드하고 Release SHA-256 digest를 검증합니다.
2. 현재 프로그램이 완전히 종료된 뒤 기존 EXE와 SQLite DB/WAL/SHM을 snapshot으로 보존합니다.
3. 새 EXE로 교체한 뒤 `--post-update-verify` 비거래 모드로 실행합니다.
4. 새 버전이 DB migration, `PRAGMA quick_check`, 필수 테이블 확인을 통과하고 token/version health marker를 남겼는지 검증합니다.
5. 실패하면 이전 EXE와 업데이트 직전 DB snapshot을 함께 복원하고, 이전 버전을 **자동매매 자동재개 없이** 실행합니다.
6. 성공한 경우에만 backup을 제거하고 새 버전을 정상 실행합니다. 업데이트 직전 자동매매가 실행 중이었다면 이 단계 이후 LIVE 감시를 자동 재개합니다.

API Key는 실행파일 내부가 아니라 운영체제 keyring에 저장되므로 업데이트 후 다시 입력할 필요가 없습니다. 거래/포지션/미확정 주문 상태는 `~/.junhyunbank/junhyunbank.db`에 유지됩니다.

주의: V4.0.0 → V4.0.1 최초 업데이트는 적용 스크립트를 V4.0.0 코드가 생성하므로 새 transactional rollback은 **V4.0.1 설치 이후 다음 업데이트부터** 완전히 적용됩니다.

## UI

- 좌측: 실시간 후보 및 운영 로그
- 우측 상단: 넓은 보유자산 목록
- 우측 하단: 실시간 가격 차트
- 보유자산 첫 행에 KRW 원화 잔고 표시
- 각 코인에 `JunhyunBank` 자동관리 여부 표시
- 전략 건강도와 시장 Regime 표시
- 데이터 상태 줄에서 WebSocket/워밍업/후보 상태를 실시간 확인

## 데이터/실행 구조

전체 KRW 마켓의 `trade` WebSocket을 여러 persistent 연결로 분할해 실시간 수집합니다. 1초 단위 프레임으로 거래대금과 BID/ASK 체결을 집계하고, 상대적으로 뜨거워진 후보에 대해서만 `orderbook`을 구독해 5개 호가쌍의 OBI·Microprice·호가 변화 및 실제 체결 가능 금액을 분석합니다.

V4 런타임은 `runtime_engine.TradingEngine`이 핵심 `engine.TradingEngine`을 상속합니다. V4에서는 이 구조 위에 주문 의도 영속화와 재시작/체결 복구가 추가돼 있습니다.

주문은 일반 진입/청산에 Upbit `best + IOC`를 사용합니다. Emergency Stop 청산에서 IOC가 명시적으로 종료되고 0체결임이 확인된 경우에만 시장가 매도를 보조 수단으로 사용할 수 있습니다. 거래소 API Rate Limit은 전략상의 거래횟수 제한과 별개로 항상 준수합니다.

## 보안

- Access Key / Secret Key는 GitHub, SQLite, 로그에 저장하지 않습니다.
- Python `keyring`을 통해 OS 자격증명 저장소를 사용합니다.
- 출금 API는 구현하지 않습니다.
- API 오류 반복, 오래된 trade/orderbook 데이터, 최소 주문금액 미달에서는 신규 주문을 차단합니다.

## 개발/테스트

```bash
python -m pip install -e ".[dev]"
pytest -q
python scripts/smoke_upbit_public_ws.py --timeout 20 --attempts 3
python launcher.py
```

Public WebSocket smoke test는 실제 Upbit 시장 탐색 후 선택한 KRW 페어의 체결과 호가를 받는지를 검증하며 **API Key와 주문 API를 사용하지 않습니다.**

`main` 병합 시 GitHub Actions가 Windows에서 단위/회귀 테스트와 Public REST/WebSocket smoke test를 통과한 뒤 `JunhyunBank.exe`를 빌드하고 전체 패키지 버전에 맞는 Release(`V4.0.1` 등)를 생성합니다.

## 전략 검증에 대한 원칙

코드/실서버 연결 테스트 통과는 수익성 검증을 뜻하지 않습니다. 현재 JH-MicroFlow의 실제 기대수익은 축적된 업비트 체결/호가 데이터에 수수료·슬리피지·주문지연을 반영한 워크포워드 검증으로 별도로 평가해야 합니다. 특히 현재 `ExpectedMove`는 아직 진정한 조건부 미래수익 모델이 아니라 최근 절대 변동폭 proxy라는 한계가 있습니다.
