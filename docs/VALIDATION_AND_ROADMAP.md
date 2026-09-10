# V2 검증 상태와 개발 로드맵

기준 버전: **V2 / 2.0.0**

이 문서의 목적은 “무엇이 완료됐는가”보다 **무엇을 아직 믿으면 안 되는가**를 명확하게 남기는 것입니다.

---

## 1. 검증을 세 종류로 구분한다

### A. 코드 검증

- import/문법
- 단위테스트
- 핵심 함수 규칙
- GitHub CI

### B. 배포 검증

- Windows runner 설치
- pytest
- PyInstaller build
- GitHub Release
- asset digest

### C. 전략 수익성 검증

- 실제 체결/호가 데이터
- 수수료
- 슬리피지
- 주문 지연
- train/OOS 분리
- regime별 성과
- MDD / tail loss

**현재 V2는 A/B는 통과했지만 C는 아직 충분히 완료되지 않았습니다.**

따라서 “V2 빌드가 성공했다”와 “V2가 수익성이 있다”를 절대 같은 뜻으로 사용하지 않습니다.

---

## 2. 현재 완료된 검증

V2 개발 과정에서:

- PR CI pytest 통과
- 보강 수정 후 PR CI 재통과
- main push pytest 통과
- Windows release workflow pytest 통과
- Windows PyInstaller `JunhyunBank.exe` build 성공
- V2 GitHub Release 생성 성공
- Release asset SHA-256 digest 존재 확인

V2 Release:

- tag: `V2`
- target commit: `12b7e50148e9e4567603d3928fc16c8c4d984787`
- asset: `JunhyunBank.exe`
- size: `71,485,066 bytes`
- SHA-256: `10d2e5fdcccb92a2332685e70db6a7b69ec6cd76f1dc03b5bccb93b894645121`

테스트가 확인하는 대표 규칙:

- 동적 주문금액에 임의 고정 KRW cap이 없음
- stale market data 신규진입 차단
- exchange minimum 미달 차단
- emergency/API failures 차단
- warning market 분류
- orderbook slippage/capacity 계산 기본 동작
- Strategy Health negative sequence stop
- managed position state persistence
- strategy outcome persistence
- cost gate
- high quality entry
- emergency stop distance 고정 의도
- 체결 없는 초를 wall-clock frame으로 보정
- updater release tag parsing

---

## 3. 현재 가장 중요한 미검증 사실

### 전략의 `ExpectedMove`는 아직 진짜 기대수익 모델이 아니다

현재:

```text
expected_move = recent |30s returns| 70th percentile
```

입니다.

이 값은 “현재 신호가 앞으로 상승할 조건부 기대수익”이 아니라 최근 시장의 **절대 움직임 크기**입니다.

그런데 현재 capital sizing의 `edge`는 이 값을 거래비용과 비교합니다.

```text
edge = (expected_move - cost) / expected_move
```

따라서 current signal의 실제 방향성 성공확률이 충분히 검증되지 않은 상태에서 **변동성 용량을 기대수익처럼 사용**하는 근본적 한계가 있습니다.

이것은 V2 수익성 검증에서 가장 먼저 다뤄야 할 전략 이슈입니다.

---

## 4. P0 — 실전 안전성 관점에서 최우선 점검

### P0-1. End-to-end 주문 상태 머신

현재 주문 제출 후 REST `GET /v1/order`를 약 8초 grace 동안 polling합니다.

점검해야 할 상황:

- POST 주문은 거래소에 도달했지만 client timeout
- accepted response는 받았지만 subsequent GET 실패
- IOC partial fill
- cancel state와 executed volume 조합
- 앱 crash 직후 pending order
- updater/Windows 종료와 order settlement가 겹침

현재 `identifier`는 고유하지만, ambiguous POST 결과에서 identifier로 자동 reconciliation하는 완전한 durable state machine은 없습니다.

**개선 권장:**

- pending_orders 테이블
- 주문 제출 전 identifier 영속화
- uuid/identifier 양쪽 조회 reconciliation
- terminal state 확인 전 동일 market 신규주문 차단
- restart recovery
- private myOrder WebSocket 병행

### P0-2. 관리수량 보호의 실제 계정 테스트

의도는 기존 보유자산을 건드리지 않는 것이지만 실제 계정에서 아래를 검증해야 합니다.

- 기존에 KRW-XYZ 10개 보유
- JunhyunBank가 2개 추가 매수
- 관리수량 2개만 매도하는지
- 사용자가 중간에 직접 추가매수/매도한 경우 reconciliation
- locked balance가 있는 경우

### P0-3. 지나치게 큰 자금배분 가능성

고정 주문한도가 없다는 사용자 요구는 유지해야 하지만, 현재 capital fraction은 최대 1입니다.

조건이 강하면 가용 KRW의 상당 부분을 한 market에 배분할 수 있습니다.

특히 현재 `edge`가 진정한 conditional expected return이 아니라 volatility proxy 기반이라는 점과 결합하면 위험합니다.

**사용자 요구를 훼손하지 않는 개선 방향:**

고정 “최대 N원”이 아니라 bootstrap uncertainty, observed edge confidence, portfolio correlation, liquidity와 drawdown distribution을 이용해 **데이터 기반 risk budget**을 계산합니다.

### P0-4. Update 후 실제 실행 실패 rollback

현재 updater는 파일 교체 실패에는 rollback을 시도합니다.

하지만 새 EXE를 `Start-Process`한 뒤 2초 후 `.old`를 삭제합니다.

새 EXE가 시작되자마자 crash하거나 API migration 오류가 나도 old executable을 자동 복원하는 health handshake는 없습니다.

**개선 권장:**

- 새 버전이 `update-success` marker를 쓰기 전까지 old 보존
- timeout 내 marker 없으면 updater가 new 종료 + old 복구
- DB migration backward compatibility 고려

---

## 5. P1 — 전략/운영 품질상 중요한 문제

### P1-1. Deep orderbook stream churn

현재 후보 목록이 바뀌면 `_restart_deep_stream()`이 **전체 deep WebSocket을 stop 후 새로 생성**합니다.

candidate refresh 기본값은 3초입니다.

Hot 순위가 자주 바뀌면:

- WebSocket reconnect 빈도 증가
- orderbook message gap
- `_book_history` continuity 약화
- BookQ delta percentile 품질 저하
- stale gate 증가

가 발생할 수 있습니다.

**개선 권장:**

- 후보 변경 hysteresis
- 최소 residency time
- subscription set 변경을 덜 자주 수행
- stable core + rotating candidates 분리
- 또는 여러 persistent shard 사용

### P1-2. Strategy Health = 0의 자동 복귀 문제

현재 health=0이면 신규진입하지 않습니다.

열린 포지션도 없으면 새로운 outcome이 생기지 않으므로 health가 스스로 회복될 근거가 없습니다.

**개선 권장:**

- shadow/virtual trade engine
- health=0에서 동일 신호를 실제 주문 없이 기록
- 충분한 shadow sample의 conditional expectancy 회복 시 단계적 exposure 복원

### P1-3. Strategy outcome이 partial exit를 정확히 반영하지 않음

현재 포지션이 여러 IOC에 걸쳐 부분청산되더라도 마지막 잔여수량이 종료될 때 마지막 `avg_price`를 이용해 전체 `net_return`을 계산합니다.

따라서 여러 partial sell의 가중평균 실제 exit price와 전체 paid fee가 Strategy Health outcome에 완전히 반영되지 않습니다.

**개선 권장:**

- position ledger에 cumulative sold quantity
- cumulative sell proceeds
- cumulative fees
- realized weighted exit price
- 실제 net PnL / actual initial risk

### P1-4. Private WebSocket 미사용

현재 public trade/orderbook WebSocket + private REST입니다.

Upbit private `myOrder`, `myAsset`를 사용하면:

- fill event latency 감소
- polling 감소
- partial fill state 명확화
- external/manual balance change 감지

에 도움이 됩니다.

REST reconciliation은 fallback/source-of-truth 검증으로 남기는 편이 좋습니다.

### P1-5. Raw market data recorder 부재

현재 전략 rolling data는 memory-only입니다.

앱 종료 시 사라지고 수익성 연구에 필요한 과거 L2 상태를 재생할 수 없습니다.

**최우선 연구 인프라:**

- raw trade events
- orderbook snapshots/deltas
- receive timestamp + exchange timestamp
- selected candidate status
- strategy features
- decisions
- order submissions/fills

을 저장합니다.

### P1-6. V1→V2 managed quantity migration

V1 DB에는 managed quantity가 없었습니다.

V2가 V1 marker를 발견하면 현재 해당 market의 **계정 총수량**을 관리수량으로 채우는 fallback이 있습니다.

사용자가 V1 관리포지션과 같은 코인을 별도 추가매수한 이력이 있다면 보호경계가 불명확할 수 있습니다.

migration UI/reconciliation을 추가하거나 legacy marker를 자동매도 대신 사용자 확인 대상으로 처리하는 방안을 검토합니다.

### P1-7. API failure counter가 endpoint별 상태를 구분하지 않음

현재 성공하는 private API 호출 하나가 전역 `api_failures`를 0으로 reset합니다.

특정 중요 endpoint가 반복 실패해도 다른 endpoint가 성공하면 연속 실패 guard가 잘 동작하지 않을 수 있습니다.

**개선:** order/account/market-data별 circuit breaker 분리.

---

## 6. P2 — 품질/분석성 개선

- 모든 entry/hold/exit feature snapshot 구조화 저장
- UI에서 현재 선택 종목 chart 변경
- 현재 Entry Quality / expected cost / regime / health 표시
- 거래별 이유와 실제 체결비용 리포트
- 일/주/월 성과 summary
- signal kind별 통계
- regime별 통계
- live vs simulated slippage 오차
- updater progress bar
- automatic diagnostic bundle export
- code signing / SmartScreen 개선

---

## 7. 전략 수익성 검증을 위해 필요한 데이터

### 최소 저장 데이터

#### trade

- market
- exchange timestamp
- local receive timestamp
- price
- volume
- aggressor side
- sequential id 가능 시 저장

#### orderbook

- market
- exchange timestamp
- local receive timestamp
- 최소 상위 5 level, 가능하면 15~30 level
- bid/ask price and size

#### features

평가시점마다:

- ActivityQ
- AggressionQ
- BookQ
- MomentumQ
- Quality
- spread
- expected_move
- realized volatility
- regime
- health

#### decision/order

- HOLD/BUY/SELL
- reason
- capital fraction
- requested KRW
- liquidity capacity
- predicted buy/sell slippage
- identifier/uuid
- actual fills
- actual fee

---

## 8. 권장 검증 절차

### Phase 1 — Recorder 안정화

적어도 여러 시장상태가 포함되게 데이터를 수집합니다.

초기 목표로 최소 14일 이상을 제안하지만, 날짜 자체보다:

- 평일/주말
- 저변동/고변동
- 시장 상승/하락
- 급등/급락 event

가 포함되는 것이 중요합니다.

### Phase 2 — Event labeling

각 candidate signal 시점 이후:

- +5s
- +10s
- +30s
- +60s
- +180s

forward return, MFE, MAE를 계산합니다.

그 다음 현재 Quality가 실제로 조건부 미래수익/성공확률을 높이는지 검증합니다.

### Phase 3 — Execution simulation

당시 orderbook을 사용해:

- requested size
- partial fill
- spread
- depth walk
- fee

를 재현합니다.

### Phase 4 — Walk-forward

시간순서 유지:

```text
TRAIN → VALIDATE → OOS
```

random shuffle split은 시장시계열 검증에 부적합합니다.

rolling/purged walk-forward를 사용합니다.

### Phase 5 — Stress

최소:

- fee × 1.0 / 1.25 / 1.5
- execution delay +100 / +250 / +500 ms
- slippage model 악화
- feature threshold ±20~30%
- 특정 상위 수익 코인 제거
- 최고 수익일 제거

### Phase 6 — 선택 기준

가장 높은 백테스트 수익 하나가 아니라 **넓은 parameter 영역에서 안정적인 설정**을 선택합니다.

---

## 9. 채택/기각 기준의 예

정확한 숫자는 데이터가 쌓인 뒤 정하되 기본 철학은 다음과 같습니다.

채택하려면:

- out-of-sample net expectancy > 0
- 비용 증가 stress에서도 쉽게 음수로 붕괴하지 않음
- delay stress 내성
- 특정 한 코인/하루 의존도가 과도하지 않음
- parameter 주변 영역에서도 성능이 유사
- drawdown/tail loss가 감당 가능한 구조
- 실제 fill과 simulated fill 차이가 통제됨

기각해야 할 패턴:

- training에서만 좋음
- threshold를 아주 조금 바꾸면 붕괴
- 거래비용 미적용 시에만 플러스
- 한두 번의 급등 거래가 전체 수익 대부분
- 실체결 지연을 넣으면 기대값 소멸

---

## 10. 권장 V3 우선순위

현재 기준으로 V3를 만든다면 아래 순서를 권장합니다.

1. **Market Data Recorder + Replay Engine**
2. **Durable Order/Fill State Machine + private myOrder/myAsset**
3. partial fill 기반 정확한 position/PnL ledger
4. deep stream churn 개선
5. shadow Strategy Health recovery
6. conditional edge estimator
7. bootstrap/damped Kelly 또는 이에 준하는 uncertainty-aware sizing
8. correlation-aware portfolio allocation
9. UI에 전략 근거/실제비용/건강도 상세 표시
10. update health handshake rollback

중요: 6~8은 1의 데이터가 충분히 쌓인 뒤 해야 합니다. 데이터 없이 복잡한 모델을 먼저 넣으면 정교해 보이는 과최적화가 될 가능성이 큽니다.

---

## 11. 현재 평가

### 구현 완성도

V1 대비 큰 발전이 있으며 기본 lifecycle, updater, managed position 보호, 실시간 MicroFlow pipeline의 골격은 갖춰져 있습니다.

### 실전 운영 완성도

실거래 프로그램이라는 기준에서는 주문상태 reconciliation과 관측가능성(observability)을 더 강화할 필요가 있습니다.

### 전략 검증 완성도

**낮음~초기 단계.**

현재 규칙은 논리적으로 구성된 초기 MicroFlow hypothesis이며 장기간의 실제 Upbit L2 기반 수익성 검증이 아직 없습니다.

다음 개발자는 이 사실을 숨기거나 “검증된 수익전략”이라고 표현하지 마세요.
