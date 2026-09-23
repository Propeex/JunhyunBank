# V4.0.4 Raw Market Data Recorder

V4.0.4는 전략 파라미터를 더 만지기 전에 **같은 실제 시장경로를 나중에 반복 재생할 수 있는 원시 데이터 기반**을 만든다. 이번 단계는 실거래 엔진에 recorder를 주입하지 않고, 독립적인 Public-only 연구 명령으로 먼저 검증한다.

## 안전 범위

`scripts/record_public_market.py`는 `UpbitClient()`를 자격증명 없이 생성하고 Public REST/WebSocket만 사용한다. API Key, Private WebSocket, 주문 API, SQLite 거래상태를 사용하지 않으며 출력에는 `orders_submitted: 0`을 명시한다.

따라서 이번 버전은 실제 진입/청산, `managed_quantity`, durable `order_intents`, updater, Strategy Health를 변경하지 않는다.

## 기록 데이터

전체 안전 KRW 시장에서는 raw trade를 기록한다. 결과를 보지 않고 순환 선택한 비후보 대조군 4개의 orderbook은 처음부터, 기본 3분 전략 워밍업 뒤에는 LIVE와 같은 fresh actionable 선택기로 고른 후보 24개의 orderbook도 함께 동적으로 구독하며 기본 15호가 레벨을 저장한다. 선택기는 stale 상위 후보 제거, top-N 밖 fresh 후보 보충, deep 최소 체류시간과 교체 margin을 LIVE와 동일하게 적용한다. 대조군은 기본 60초마다 교체되어 후보 선택 자체의 lift를 비교할 근거를 남긴다. 각 `orderbook_subscription` metadata에는 전체 `markets`뿐 아니라 서로 겹치지 않는 `hot_markets`와 `control_markets`를 기록한다. 구독 합집합이 우연히 같아도 역할이 바뀌면 새 metadata를 기록하므로 replay가 대조군과 production 후보를 추측하지 않는다.

각 레코드에는 다음 공통 필드가 있다.

```text
schema_version
seq
kind = trade | orderbook | meta
market
received_ns
received_monotonic_ns
exchange_timestamp_ms
data
```

`seq`는 프로세스 내부 총 순서를 보존한다. exchange timestamp와 local receive timestamp를 함께 저장하므로 이후 replay에서 거래소 시간 기준과 실제 수신 순서 기준을 비교할 수 있다.

trade는 가격, 수량, BID/ASK aggressor, trade timestamp, sequential id 등을 보존한다. orderbook은 선택한 depth의 bid/ask price와 size를 보존한다. 구독시장 또는 Hot/control 역할 변경, session start/end, WebSocket 오류도 meta event로 기록한다.

## 비차단 writer

WebSocket callback에서 파일을 직접 쓰지 않는다.

1. callback은 필요한 필드만 정규화한다.
2. bounded queue에 `put_nowait` 한다.
3. 별도 writer thread가 JSONL을 기록한다.
4. queue가 가득 차면 callback을 기다리게 하지 않고 해당 recorder event를 버리며 `dropped`를 증가시킨다.

연구 데이터 손실보다 실시간 시세/거래 thread 지연이 더 위험하므로 fail-open 대상은 **recorder 자체뿐**이다. `dropped > 0`인 세션은 완전한 replay dataset으로 자동 승격하면 안 된다.

## crash-safe rotation

활성 파일은 다음 형태다.

```text
upbit-public-<UTC session>-<segment>.jsonl.part
```

segment 종료 시 flush + fsync 후 atomic rename으로 `.jsonl`을 만든다. 압축은 별도 compressor thread가 `.jsonl.gz.part`에 작성하고 fsync/atomic rename 성공 후에만 원본 `.jsonl`을 삭제한다.

프로세스가 중간에 죽어도 이미 finalized된 `.jsonl`은 사라지지 않는다. 다음 시작 시 남은 `.jsonl.part`는 마지막 완전한 newline까지 잘라 torn trailing record를 버리고 finalized segment로 복구한다.

## 회전·보존 기본값

- queue: 50,000 records
- segment: 64 MiB 또는 15분
- gzip compression level: 5
- 총 저장 상한: 5 GiB
- 최대 finalized file: 512

보존 한도를 넘으면 가장 오래된 **완료 세션 전체**부터 삭제한다. 한 세션 안의 첫 segment만 지워 `session_start` 없는 꼬리를 만들지 않으며, 현재 기록 중인 세션은 segment 수가 파일 상한을 넘더라도 잘라내지 않는다.

## 실행

```bash
python scripts/record_public_market.py \
  --seconds 3600 \
  --output-dir ~/.junhyunbank/recordings
```

장기 수집은 예를 들어 다음처럼 실행할 수 있다.

```bash
python scripts/record_public_market.py \
  --seconds 0 \
  --orderbook-top 24 \
  --orderbook-controls 4 \
  --control-rotate-seconds 60 \
  --orderbook-depth 15 \
  --max-gb 20 \
  --output-dir ~/.junhyunbank/recordings
```

`--seconds 0`은 Ctrl+C까지 계속 기록한다.

## 데이터 품질 판정

종료 JSON에서 최소 다음을 확인한다.

- `clean_stop == true`
- `dropped == 0`
- `last_error == ""`
- WebSocket 오류가 0인지
- finalized/compressed segment가 존재하는지
- `orders_submitted == 0`

프로그램은 drop, recorder 내부 오류 또는 WebSocket 오류가 하나라도 있으면 데이터를 남기되 `data_integrity_ok=false`, exit code 2를 반환한다. 불완전 세션을 조용히 정상 dataset으로 취급하지 않기 위한 장치다.

## 현재 한계

이번 recorder는 baseline 연구용으로 orderbook을 **현재 LIVE 정합 후보와 작은 순환 대조군**에 대해서만 저장한다. 전체 KRW 시장의 모든 L2를 저장하지 않으므로, 미래에 후보 스캐너 자체를 크게 변경하면 당시 새 후보의 orderbook이 없을 수 있다. 순환 대조군도 완전한 무작위 전시장 표본은 아니며, 후보 밖 시장을 고정 규칙으로 순회하는 비교 자료다.

따라서 V4.0.4의 다음 단계는 deterministic replay를 먼저 만들고, replay coverage 보고서를 통해 필요한 orderbook coverage를 측정한다. 필요하면 recorder의 deep universe를 확대하되 데이터량/CPU/네트워크와 drop rate를 동시에 검증한다.

## 다음 단계

V4.0.5의 우선순위는 deterministic replay다.

- `.jsonl`/`.jsonl.gz` finalized segment만 읽는다.
- `seq` 또는 local receive timestamp 순서로 이벤트를 재생한다.
- recorded trade/orderbook을 `MicroFlowStrategy`에 동일하게 주입한다.
- `time.time()`/`time.monotonic()` 의존을 injectable clock으로 분리한다.
- 동일 recording + 동일 config가 동일 feature/decision을 생성하는지 회귀검증한다.
- 누락된 orderbook coverage와 recorder drop이 있으면 성과 계산에서 명시적으로 실패/제외한다.

그 뒤에야 purged walk-forward, fee/delay/slippage stress, Conditional ExpectedMove 후보 모델을 진행한다.
