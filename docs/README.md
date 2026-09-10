# JunhyunBank 개발 문서

이 디렉터리는 **대화 기록이 전혀 없는 새 개발자 또는 새 ChatGPT 세션이 JunhyunBank를 정확히 이해하고 개발을 이어가기 위한 영구 인수인계 자료**입니다.

> 가장 먼저 `DEVELOPER_HANDOFF.md`를 읽으세요. 코드와 문서가 충돌하면 **현재 `main`의 코드가 최종 사실(source of truth)** 입니다. 전략 문서에는 의도와 현재 구현의 차이를 별도로 표시합니다.

## 읽는 순서

1. [`DEVELOPER_HANDOFF.md`](DEVELOPER_HANDOFF.md) — 제품 의도, 절대 지켜야 할 원칙, 현재 버전/상태, 다음 작업
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — 모듈 구조, 엔진 상태, 데이터/주문/업데이트 흐름
3. [`STRATEGY_JH_MICROFLOW.md`](STRATEGY_JH_MICROFLOW.md) — 전략 철학, 수식, 실제 V2 구현 규칙, 설계상 미구현 기능
4. [`VALIDATION_AND_ROADMAP.md`](VALIDATION_AND_ROADMAP.md) — 현재 검증 수준, 알려진 결함/리스크, 실전 검증 계획, 우선순위
5. [`UPBIT_INTEGRATION.md`](UPBIT_INTEGRATION.md) — Upbit API 사용법, 주문/인증/Rate Limit/보안 전제

## 문서 유지 규칙

기능 또는 전략을 바꿀 때 코드만 수정하지 말고 관련 문서를 함께 수정합니다.

- 제품 동작/UX 변경 → `DEVELOPER_HANDOFF.md`, `ARCHITECTURE.md`
- 전략 수식/판단 변경 → `STRATEGY_JH_MICROFLOW.md`
- 새 검증 결과/알려진 문제 → `VALIDATION_AND_ROADMAP.md`
- Upbit API 사용 변경 → `UPBIT_INTEGRATION.md`
- 새 메이저 버전 릴리즈 → 위 문서의 `현재 상태`와 `CHANGELOG.md` 갱신

완성된 프로그램 버전은 사용자의 요구에 따라 **V1, V2, V3 ... 메이저 릴리즈 방식**을 유지합니다.
