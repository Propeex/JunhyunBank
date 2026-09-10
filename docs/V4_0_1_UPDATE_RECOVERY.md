# V4.0.1 업데이트 검증·롤백

V4.0.0 이후의 첫 후속 안정화 작업은 **실행파일 업데이트 자체를 하나의 복구 가능한 트랜잭션으로 만드는 것**이다.

## 문제

V4.0.0까지의 updater는 새 `JunhyunBank.exe`를 SHA-256으로 검증한 뒤 현재 EXE와 교체하고 새 프로그램을 실행했다. 그러나 새 EXE를 실행한 뒤 약 2초만 기다리고 이전 EXE를 삭제했다. 따라서 새 바이너리가 실제로 시작 가능한지, 현재 SQLite DB를 정상적으로 열고 마이그레이션할 수 있는지 확인하는 health handshake가 없었다.

자동매매 프로그램에서는 단순 파일 교체 성공보다 **재시작 후 상태 복구 가능성**이 중요하다. 특히 `managed_positions`와 `order_intents`에는 실제 자동관리 수량과 미확정 주문 의도가 들어 있으므로, 업데이트 실패로 실행파일 또는 DB가 반쯤 바뀐 상태가 남아서는 안 된다.

## V4.0.1 동작

새 updater가 생성하는 PowerShell 적용 스크립트는 다음 순서를 따른다.

1. 기존 JunhyunBank 프로세스가 완전히 종료될 때까지 기다린다.
2. 현재 SQLite DB와 존재할 경우 WAL/SHM 파일을 업데이트 작업 디렉터리에 백업한다.
3. 현재 EXE를 토큰별 backup 이름으로 보존한 뒤 새 EXE로 교체한다.
4. 새 EXE를 `--post-update-verify <token>` 모드로 **자동매매 없이 1회 실행**한다.
5. 검증 모드는 현재 버전의 `Storage`를 통해 DB를 열어 스키마 마이그레이션을 수행하고 `PRAGMA quick_check`와 필수 테이블 존재 여부를 확인한다.
6. 성공한 새 EXE만 토큰과 버전이 들어 있는 health marker를 원자적으로 기록한다.
7. 적용 스크립트는 검증 프로세스 종료코드, marker 토큰, marker 버전을 모두 확인한다.
8. 검증에 실패하면 새 EXE와 검증 중 변경된 DB를 버리고 **이전 EXE + 업데이트 직전 DB snapshot**을 함께 복원한다. 복원된 이전 버전은 자동매매를 자동 재개하지 않고 일반 모드로 실행한다.
9. 검증에 성공한 경우에만 이전 EXE와 DB backup을 삭제하고 새 버전을 정상 실행한다. 업데이트 직전에 자동매매가 실행 중이었다면 이 시점 이후에만 `--resume-trading`을 전달한다.

## 안전 불변조건

- SHA-256 검증 전에는 실행파일을 교체하지 않는다.
- 새 바이너리 검증 전에는 LIVE 자동매매를 자동 재개하지 않는다.
- 새 바이너리 검증은 Upbit 주문 API를 호출하지 않는다.
- 검증 실패 시 EXE만 되돌리고 DB는 새 스키마로 남겨두는 혼합 상태를 만들지 않는다.
- rollback으로 복구된 이전 버전은 자동 주문을 스스로 재개하지 않는다.
- health marker는 무작위 UUID 토큰에 묶여 있어 과거 marker를 성공으로 오인하지 않는다.

## 검증 범위

단위 테스트는 다음을 확인한다.

- 새 EXE 검증 모드가 임시 SQLite DB를 만들고 필수 테이블 및 무결성을 확인한 후 올바른 token/version marker를 기록한다.
- 생성되는 PowerShell 스크립트에서 `--post-update-verify`가 `--resume-trading`보다 먼저 실행된다.
- 검증 프로세스 종료코드, health token, target version을 모두 검사한다.
- EXE rollback과 DB/WAL/SHM snapshot 복원 경로가 스크립트에 존재한다.
- 업데이트 시작 당시 자동매매가 꺼져 있었다면 최종 실행에 `--resume-trading`이 포함되지 않는다.

Windows Release workflow의 기존 전체 pytest 및 실제 Upbit Public REST/WebSocket smoke gate는 그대로 유지한다.

## 중요한 전환점

V4.0.0에서 V4.0.1로 처음 업데이트할 때 적용 스크립트 자체는 **현재 실행 중인 V4.0.0 updater가 생성**한다. 따라서 transactional rollback은 V4.0.1 바이너리에 탑재되어 그 이후 업데이트부터 완전히 적용된다. V4.0.1 릴리즈 자체는 기존 SHA-256 교체 경로로 설치된다.

이 제한 때문에 V4.0.1에서는 거래 로직 변경을 함께 섞지 않는다. updater 안정화만 별도 패치로 배포하고, 이후 private `myOrder`/`myAsset` reconciliation과 장기 전략 검증 작업을 별도 PR로 진행한다.
