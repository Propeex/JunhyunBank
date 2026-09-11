"""Replay one JunhyunBank public recorder session without network or orders.

The command reads V4.0.4+ finalized JSONL/JSONL.GZ recorder segments, drives the
current JH-MicroFlow strategy using recorded receive time, and writes a stable
decision fingerprint plus feature/decision snapshots. It never creates an
UpbitClient, never reads API credentials, and never submits an order.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from junhyunbank.replay import (
    ReplayInputError,
    ReplayOptions,
    replay_files,
    select_recording_session,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prefix", default="upbit-public")
    parser.add_argument(
        "--session",
        default="latest",
        help="directory 입력일 때 latest 또는 session id/suffix",
    )
    parser.add_argument("--evaluate-every", type=float, default=1.0)
    parser.add_argument("--bid-fee", type=float, default=0.0005)
    parser.add_argument("--ask-fee", type=float, default=0.0005)
    args = parser.parse_args()

    try:
        session_id, files = select_recording_session(
            args.input,
            prefix=str(args.prefix),
            session=str(args.session),
        )
        result = replay_files(
            files,
            options=ReplayOptions(
                evaluate_every_seconds=float(args.evaluate_every),
                bid_fee=float(args.bid_fee),
                ask_fee=float(args.ask_fee),
            ),
        )
    except ReplayInputError as exc:
        print(json.dumps({"error": str(exc), "orders_submitted": 0}, ensure_ascii=False))
        return 2

    result["source"]["session"] = session_id
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    summary = result["summary"]
    input_state = result["input"]
    print(
        json.dumps(
            {
                "session": session_id,
                "files": len(files),
                "evaluations": summary["evaluations"],
                "buy_decisions": summary["buy_decisions"],
                "fingerprint": summary["decision_fingerprint_sha256"],
                "complete_session": input_state["complete_session"],
                "clock_retrograde_events": input_state["clock_retrograde_events"],
                "output": str(output),
                "orders_submitted": 0,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    # An interrupted recorder session can still be inspected, but automation
    # must not silently treat it as a complete research dataset.
    return 0 if input_state["complete_session"] and input_state["nonzero_orders_meta"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
