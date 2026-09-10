# V4.0.2 Private Account Reconciliation

V4.0.2의 목적은 V4.0.0에서 도입한 **durable order intent + identifier REST 복구**를 Upbit의 authenticated Private WebSocket(`myOrder`, `myAsset`)으로 보조해, 체결/주문 상태 변화에 더 빠르게 반응하면서도 회계 정합성과 재시작 복구 성질을 유지하는 것이다.

## 1. 왜 WebSocket을 정본으로 쓰지 않는가

Upbit `myOrder`는 주문 생성/체결/취소 등 이벤트가 있을 때만 전송되고, `myAsset` 역시 자산 변동이 있을 때만 전송된다. 연결 직후 아무 이벤트도 오지 않는 것이 정상이며 `myAsset`은 최초 이용 시 수분간 수신이 지연될 수 있다.

따라서 V4.0.2는 Private WebSocket을 **wake-up / anomaly signal**로만 사용한다.

- `myOrder`에서 JunhyunBank identifier를 발견하면 해당 durable intent의 REST reconciliation을 즉시 재요청한다.
- 실제 terminal state, 체결량, 체결가, 체결금액, 수수료는 기존 `GET /v1/order?identifier=...` 응답을 정본으로 사용한다.
- `myAsset`은 자산 변동이 있었다는 사실만 알려준다. WebSocket 잔고를 `managed_quantity`로 복사하지 않는다.
- Private WebSocket이 끊겨도 기존 REST polling 복구가 계속 작동한다.

이 구조는 이벤트 누락/재연결/프로그램 재시작 후에도 `order_intents` SQLite journal이 최종 복구 기준으로 남게 한다.

## 2. 연결 구조

프로세스당 Private Account WebSocket은 하나만 사용한다.

구독 메시지:

```json
[
  {"ticket": "<uuid>"},
  {"type": "myOrder"},
  {"type": "myAsset"},
  {"format": "DEFAULT"}
]
```

`myOrder`의 `codes`는 생략해 전체 주문을 받는다. `myAsset`에는 `codes`를 넣지 않는다. Private endpoint는 `wss://api.upbit.com/websocket/v1/private`이며 매 연결마다 새로운 nonce의 JWT를 생성하여 `Authorization: Bearer ...` 헤더에 넣는다.

JWT/Authorization 문자열은 상태 이벤트와 오류 로그에서 redaction한다.

## 3. 무이벤트 연결 유지

Public trade stream과 달리 Private stream에는 장시간 메시지가 없는 것이 정상이다. 따라서 "30초 동안 메시지가 없음"을 stale로 판정하지 않는다.

- recv timeout 시 ping frame을 전송한다.
- 소켓 자체가 실패한 경우에만 reconnect한다.
- reconnect는 1초에서 시작해 최대 30초까지 exponential backoff한다.
- reconnect마다 새 JWT를 생성한다.

Private WS 실패는 주문 엔진을 중단시키지 않는다. REST identifier reconciliation이 fallback이다.

## 4. myOrder 처리 불변조건

JunhyunBank 주문 identifier는 `junhyunbank-` prefix를 가진다.

Private stream에서 다른 수동 주문 또는 제3 프로그램 주문을 보더라도 JunhyunBank 내부 상태를 변경하지 않는다. `junhyunbank-` identifier만 빠른 reconciliation 요청 집합에 넣는다.

중요하게, WebSocket 이벤트 자체로 `complete_order_intent()`를 호출하지 않는다. 다음 reconciliation에서 REST canonical order를 조회한 후 기존 atomic accounting path를 사용한다.

Upbit는 `myOrder.state`에 `wait`, `watch`, `trade`, `done`, `cancel` 외에 SMP 관련 `prevented`도 전송할 수 있다. V4.0.2는 이 이벤트 역시 단지 REST 재확인의 계기로 취급하며 자체적으로 terminal이라고 추정하지 않는다.

## 5. REST polling backoff

V4.0.0은 매 evaluation cycle마다 모든 pending intent를 조회할 수 있었다. pending 주문이 여러 개이거나 Upbit 조회 오류가 반복되면 Exchange REST quota와 평가 thread를 불필요하게 점유할 수 있다.

V4.0.2는 다음 정책을 사용한다.

- 정상적인 non-terminal 주문: 약 1초 뒤 재확인
- 조회 실패: 2초 → 4초 → 8초 → 15초 상한 backoff
- `myOrder` event: 현재 backoff를 무시하고 해당 identifier를 즉시 재확인
- 재시작 직후: in-memory backoff가 없으므로 durable pending intent를 즉시 한 번 조회

pending intent가 존재하는 동안 신규 BUY를 막는 기존 정책은 유지한다.

## 6. myAsset 처리 불변조건

`myAsset.assets[].balance/locked` 값은 계정 전체 잔고다. 이 값은 JunhyunBank가 직접 연 포지션 수량과 같지 않을 수 있다.

따라서 V4.0.2는 myAsset을 자산 변동 신호로만 노출한다. 실제 UI portfolio는 기존 authenticated REST accounts 조회로 갱신하며, `managed_positions.managed_quantity`는 오직 JunhyunBank 주문의 canonical fill accounting을 통해서만 변한다.

이 원칙은 사용자가 원래 보유하던 코인 또는 수동으로 추가 매수한 수량을 자동매도하지 않기 위한 핵심 불변조건이다.

## 7. 검증 범위

CI에서 API Key를 저장하지 않기 때문에 authenticated Private WebSocket 실제 연결 자체는 GitHub Actions에서 강제로 수행하지 않는다. 대신 다음을 자동 검증한다.

- 한 연결에서 `myOrder` + `myAsset`을 올바른 payload로 구독하고 `myAsset`에 `codes`를 넣지 않음
- reconnect마다 새로운 Bearer authorization을 요청
- Authorization/JWT가 오류 문자열에서 redaction됨
- 수동 주문 event가 JunhyunBank pending intent를 깨우거나 변경하지 않음
- JunhyunBank myOrder event가 기존 REST backoff를 무시하고 canonical reconciliation을 촉발
- myAsset이 managed quantity를 변경하지 않음
- 반복 REST 실패가 backoff되어 즉시 반복 호출되지 않음
- 기존 전체 unit/regression + 실제 Public REST/WebSocket smoke가 계속 통과

실계정 Private WS의 마지막 검증은 V4.0.2 설치 후 운영 로그의 `Private WS 연결 · myOrder/myAsset 보조 감시 시작`을 확인하는 방식으로 수행한다. 주문/자산 이벤트가 없을 때 데이터가 오지 않는 것은 정상이다.

## 8. 다음 작업

Private stream 안정화 이후 우선순위는 다음과 같다.

- private event/REST 결과 불일치를 사용자에게 명확히 보여주는 pending reconciliation UI
- 장시간 L2/체결 recorder와 deterministic replay
- ExpectedMove를 실제 conditional forward edge로 교체하기 위한 OOS/work-forward 검증
- 최소주문금액 이하 managed dust가 DRAINING을 영구 고착시키는 문제의 명시적 정책
- strategy health 0의 장기 운영/복구 정책 검증
