# JunhyunBank 개발 문서

이 디렉터리는 **대화 기록이 전혀 없는 새 개발자 또는 새 ChatGPT 세션이 JunhyunBank를 정확히 이해하고 개발을 이어가기 위한 영구 인수인계 자료**입니다.

> 가장 먼저 `DEVELOPER_HANDOFF.md`를 읽으세요. 코드와 문서가 충돌하면 **현재 `main`의 코드가 최종 사실(source of truth)** 입니다. 전략 문서에는 의도와 현재 구현의 차이를 별도로 표시합니다.

## 읽는 순서

1. [`DEVELOPER_HANDOFF.md`](DEVELOPER_HANDOFF.md) — 제품 의도, 절대 지켜야 할 원칙, 현재 버전/상태, 다음 작업
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — 모듈 구조, 엔진 상태, 데이터/주문/업데이트 흐름
3. [`STRATEGY_JH_MICROFLOW.md`](STRATEGY_JH_MICROFLOW.md) — 전략 철학, 수식, 실제 구현 규칙, 설계상 미구현 기능
4. [`VALIDATION_AND_ROADMAP.md`](VALIDATION_AND_ROADMAP.md) — 현재 검증 수준, 알려진 결함/리스크, 실전 검증 계획, 우선순위
5. [`UPBIT_INTEGRATION.md`](UPBIT_INTEGRATION.md) — Upbit API 사용법, 주문/인증/Rate Limit/보안 전제
6. [`V4_AUDIT.md`](V4_AUDIT.md) — V4 런타임·매수 경로·주문 복구 감사와 남은 과제
7. [`V4_0_1_UPDATE_RECOVERY.md`](V4_0_1_UPDATE_RECOVERY.md) — 새 EXE 사전검증, DB snapshot, 실패 시 EXE+DB 원자 롤백
8. [`V4_0_2_PRIVATE_RECONCILIATION.md`](V4_0_2_PRIVATE_RECONCILIATION.md) — authenticated myOrder/myAsset 보조 감시, event-driven REST reconciliation, 잔고 보호 불변조건
9. [`V4_0_3_EDGE_VALIDATION.md`](V4_0_3_EDGE_VALIDATION.md) — 주문 없는 public forward-edge 수집, label 품질, purged chronological holdout
10. [`V4_0_4_MARKET_RECORDER.md`](V4_0_4_MARKET_RECORDER.md) — 비차단 raw trade/L2 recorder, crash-safe rotation, compression/retention, replay 데이터 형식
11. [`V3_RUNTIME_HARDENING.md`](V3_RUNTIME_HARDENING.md) — V3 Public WebSocket 안정화와 런타임 진단
12. [`V3_0_1_MARKET_DISCOVERY_FIX.md`](V3_0_1_MARKET_DISCOVERY_FIX.md) — `Trade WS 0/0` / KRW 유니버스 0개 증상, 원인 경로, V3.0.1 보완과 검증 기준

## 문서 유지 규칙

기능 또는 전략을 바꿀 때 코드만 수정하지 말고 관련 문서를 함께 수정합니다.

- 제품 동작/UX 변경 → `DEVELOPER_HANDOFF.md`, `ARCHITECTURE.md`
- 전략 수식/판단 변경 → `STRATEGY_JH_MICROFLOW.md`
- 새 검증 결과/알려진 문제 → `VALIDATION_AND_ROADMAP.md`
- Upbit API 사용 변경 → `UPBIT_INTEGRATION.md`
- 런타임 데이터/배포 안정화 → 해당 버전의 runtime/fix 문서 및 `CHANGELOG.md`

사용자가 보는 제품 이름은 **V1, V2, V3 ... 메이저 버전**을 유지하되, 같은 메이저의 수정 배포는 `V4.0.4`처럼 semantic patch tag를 사용할 수 있습니다. 이는 업데이트 버튼이 기존 설치본과 새 패치 배포를 구분할 수 있게 하기 위한 것입니다.
