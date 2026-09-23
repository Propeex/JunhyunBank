from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator
import uuid


@dataclass(slots=True)
class RecorderConfig:
    directory: Path
    prefix: str = "market"
    queue_max: int = 50_000
    rotate_bytes: int = 64 * 1024 * 1024
    rotate_seconds: float = 900.0
    flush_seconds: float = 1.0
    fsync_seconds: float = 10.0
    compress: bool = True
    compression_level: int = 5
    max_total_bytes: int = 5 * 1024 * 1024 * 1024
    max_files: int = 512


@dataclass(slots=True)
class RecorderStats:
    accepting: bool
    enqueued: int
    written: int
    trade_events: int
    orderbook_events: int
    dropped: int
    bytes_written: int
    queue_depth: int
    segments_finalized: int
    compressed_segments: int
    recovered_parts: int
    current_segment: str | None
    writer_alive: bool
    compressor_alive: bool
    last_error: str


def recording_integrity_ok(
    *,
    stopped_cleanly: bool,
    dropped: int,
    last_error: str,
    websocket_errors: dict[str, int] | None = None,
    trade_events: int = 0,
    orderbook_events: int = 0,
    orderbook_requested: bool = False,
) -> bool:
    """Return whether automation may promote this recording as complete."""
    return not recording_integrity_reasons(
        stopped_cleanly=stopped_cleanly,
        dropped=dropped,
        last_error=last_error,
        websocket_errors=websocket_errors,
        trade_events=trade_events,
        orderbook_events=orderbook_events,
        orderbook_requested=orderbook_requested,
    )


def recording_integrity_reasons(
    *,
    stopped_cleanly: bool,
    dropped: int,
    last_error: str,
    websocket_errors: dict[str, int] | None = None,
    trade_events: int = 0,
    orderbook_events: int = 0,
    orderbook_requested: bool = False,
) -> list[str]:
    """Return stable fail-closed reason codes for recorder promotion."""
    reasons: list[str] = []
    if not stopped_cleanly:
        reasons.append("unclean_stop")
    if int(dropped) != 0:
        reasons.append("dropped_records")
    if str(last_error or "").strip():
        reasons.append("recorder_error")
    if websocket_errors:
        reasons.append("websocket_errors")
    if int(trade_events) <= 0:
        reasons.append("zero_trade_events")
    if orderbook_requested and int(orderbook_events) <= 0:
        reasons.append("zero_orderbook_events")
    return reasons


def subscription_metadata_changed(
    *,
    current_markets: Iterable[str],
    current_hot: Iterable[str],
    current_controls: Iterable[str],
    next_markets: Iterable[str],
    next_hot: Iterable[str],
    next_controls: Iterable[str],
) -> bool:
    """Return whether replay-visible subscription metadata must be emitted.

    The WebSocket market union can remain unchanged while a control market and
    a production candidate exchange roles. That role-only transition is still
    material to replay and must receive its own sequence-stamped metadata row.
    """
    return bool(
        list(current_markets) != list(next_markets)
        or set(current_hot) != set(next_hot)
        or set(current_controls) != set(next_controls)
    )


class MarketDataRecorder:
    """Bounded, asynchronous JSONL recorder for public market events.

    WebSocket callbacks only normalize an event and call ``put_nowait``. Disk
    I/O, fsync, rotation and gzip compression happen on background threads. If
    the bounded queue fills, recording is dropped and counted instead of ever
    blocking the market-data/trading callback.

    Active segments use ``.jsonl.part``. A finalized segment is fsynced and
    atomically renamed to ``.jsonl`` before optional compression. Compression
    writes a second ``.gz.part`` and only deletes the source JSONL after the
    gzip file is fsynced and atomically renamed. A crash therefore cannot
    destroy the last finalized plain segment. On the next start, complete lines
    from abandoned ``.jsonl.part`` files are recovered; a torn trailing line is
    truncated.
    """

    SCHEMA_VERSION = 1

    def __init__(self, config: RecorderConfig) -> None:
        self.config = config
        self.directory = Path(config.directory).expanduser()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(1, int(config.queue_max))
        )
        self._compression_queue: queue.Queue[Path] = queue.Queue()
        self._stop = threading.Event()
        self._compression_stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._compressor: threading.Thread | None = None
        self._lock = threading.RLock()
        self._accepting = False
        self._sequence = 0
        self._enqueued = 0
        self._written = 0
        self._trade_events = 0
        self._orderbook_events = 0
        self._dropped = 0
        self._bytes_written = 0
        self._segments_finalized = 0
        self._compressed_segments = 0
        self._recovered_parts = 0
        self._current_segment: str | None = None
        self._last_error = ""
        self._session = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        self._segment_index = 0

    def start(self) -> None:
        with self._lock:
            if self._writer and self._writer.is_alive():
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            self._compression_stop.clear()
            self._accepting = True

        self._compressor = threading.Thread(
            target=self._compression_loop,
            name="market-recorder-compressor",
            daemon=True,
        )
        self._compressor.start()
        self._recover_parts()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="market-recorder-writer",
            daemon=True,
        )
        self._writer.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop accepting records and drain queued data within ``timeout``.

        Returns True only when both writer and compressor have stopped. A false
        result is safe for trading callbacks: the threads are daemonized and an
        unfinished active segment remains a recoverable ``.part`` file.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._accepting = False
        self._stop.set()
        writer = self._writer
        if writer and writer.is_alive():
            writer.join(timeout=max(0.0, deadline - time.monotonic()))
        writer_done = not writer or not writer.is_alive()
        if writer_done:
            self._compression_stop.set()
        compressor = self._compressor
        if compressor and compressor.is_alive():
            compressor.join(timeout=max(0.0, deadline - time.monotonic()))
        return writer_done and (not compressor or not compressor.is_alive())

    def stats(self) -> RecorderStats:
        with self._lock:
            return RecorderStats(
                accepting=self._accepting,
                enqueued=self._enqueued,
                written=self._written,
                trade_events=self._trade_events,
                orderbook_events=self._orderbook_events,
                dropped=self._dropped,
                bytes_written=self._bytes_written,
                queue_depth=self._queue.qsize(),
                segments_finalized=self._segments_finalized,
                compressed_segments=self._compressed_segments,
                recovered_parts=self._recovered_parts,
                current_segment=self._current_segment,
                writer_alive=bool(self._writer and self._writer.is_alive()),
                compressor_alive=bool(
                    self._compressor and self._compressor.is_alive()
                ),
                last_error=self._last_error,
            )

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def record(
        self,
        kind: str,
        *,
        market: str | None = None,
        exchange_timestamp_ms: int | float | None = None,
        data: dict[str, Any] | None = None,
        received_ns: int | None = None,
        received_monotonic_ns: int | None = None,
    ) -> bool:
        with self._lock:
            if not self._accepting:
                return False
            # Allocate the sequence and enqueue under the same lock. Multiple
            # WebSocket callback threads otherwise can obtain seq N/N+1 and
            # put them into the queue in the opposite order. A full queue still
            # consumes a sequence on purpose, making the loss visible as a gap
            # in the finalized recording.
            self._sequence += 1
            record = {
                "schema_version": self.SCHEMA_VERSION,
                "seq": self._sequence,
                "kind": str(kind),
                "market": str(market or ""),
                "received_ns": int(received_ns or time.time_ns()),
                "received_monotonic_ns": int(
                    received_monotonic_ns or time.monotonic_ns()
                ),
                "exchange_timestamp_ms": (
                    int(exchange_timestamp_ms)
                    if exchange_timestamp_ms is not None
                    else None
                ),
                "data": dict(data or {}),
            }
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped += 1
                return False
            self._enqueued += 1
            if record["kind"] == "trade":
                self._trade_events += 1
            elif record["kind"] == "orderbook":
                self._orderbook_events += 1
        return True

    def record_trade(
        self,
        event: dict[str, Any],
        *,
        received_ns: int | None = None,
        received_monotonic_ns: int | None = None,
    ) -> bool:
        market = str(event.get("code") or "")
        exchange_ts = event.get("trade_timestamp") or event.get("timestamp")
        data = {
            key: event.get(key)
            for key in (
                "trade_price",
                "trade_volume",
                "ask_bid",
                "trade_timestamp",
                "timestamp",
                "sequential_id",
                "stream_type",
            )
            if key in event
        }
        return self.record(
            "trade",
            market=market,
            exchange_timestamp_ms=exchange_ts,
            data=data,
            received_ns=received_ns,
            received_monotonic_ns=received_monotonic_ns,
        )

    def record_orderbook(
        self,
        event: dict[str, Any],
        *,
        received_ns: int | None = None,
        received_monotonic_ns: int | None = None,
    ) -> bool:
        market = str(event.get("code") or "")
        units: list[dict[str, Any]] = []
        for row in event.get("orderbook_units") or []:
            if isinstance(row, dict):
                units.append(
                    {
                        key: row.get(key)
                        for key in (
                            "ask_price",
                            "bid_price",
                            "ask_size",
                            "bid_size",
                        )
                        if key in row
                    }
                )
        data = {
            "timestamp": event.get("timestamp"),
            "total_ask_size": event.get("total_ask_size"),
            "total_bid_size": event.get("total_bid_size"),
            "orderbook_units": units,
        }
        if "level" in event:
            data["level"] = event.get("level")
        if "stream_type" in event:
            data["stream_type"] = event.get("stream_type")
        return self.record(
            "orderbook",
            market=market,
            exchange_timestamp_ms=event.get("timestamp"),
            data=data,
            received_ns=received_ns,
            received_monotonic_ns=received_monotonic_ns,
        )

    def record_meta(self, event: str, **data: Any) -> bool:
        return self.record("meta", data={"event": str(event), **data})

    def record_session_end(self, **data: Any) -> bool:
        """Record the final capture-quality snapshot with ``session_end``.

        Callers must stop market streams before invoking this method so no
        later market event can appear after the session boundary. The snapshot
        is taken before the session_end record itself is enqueued; replay uses
        loss/error fields as integrity gates and treats queue counters as
        diagnostics rather than requiring them to be equal at this instant.
        """
        stats = self.stats()
        payload = dict(data)
        payload["recorder_stats"] = {
            "enqueued_before_session_end": stats.enqueued,
            "written_before_session_end": stats.written,
            "trade_events_before_session_end": stats.trade_events,
            "orderbook_events_before_session_end": stats.orderbook_events,
            "dropped": stats.dropped,
            "bytes_written_before_session_end": stats.bytes_written,
            "queue_depth_before_session_end": stats.queue_depth,
            "segments_finalized_before_session_end": stats.segments_finalized,
            "last_error": stats.last_error,
        }
        return self.record_meta("session_end", **payload)

    def _segment_paths(self) -> tuple[Path, Path]:
        self._segment_index += 1
        stem = (
            f"{self.config.prefix}-{self._session}-"
            f"{self._segment_index:06d}.jsonl"
        )
        final = self.directory / stem
        return final.with_suffix(final.suffix + ".part"), final

    def _writer_loop(self) -> None:
        handle = None
        part_path: Path | None = None
        final_path: Path | None = None
        opened_at = 0.0
        segment_bytes = 0
        last_flush = time.monotonic()
        last_fsync = last_flush

        def open_segment() -> None:
            nonlocal handle, part_path, final_path, opened_at
            nonlocal segment_bytes, last_flush, last_fsync
            part_path, final_path = self._segment_paths()
            handle = part_path.open("ab", buffering=1024 * 1024)
            opened_at = time.monotonic()
            segment_bytes = int(part_path.stat().st_size)
            last_flush = opened_at
            last_fsync = opened_at
            with self._lock:
                self._current_segment = part_path.name

        def flush(sync: bool = False) -> None:
            nonlocal last_flush, last_fsync
            if handle is None:
                return
            handle.flush()
            last_flush = time.monotonic()
            if sync:
                os.fsync(handle.fileno())
                last_fsync = last_flush

        def finalize() -> None:
            nonlocal handle, part_path, final_path, segment_bytes
            if handle is None or part_path is None or final_path is None:
                return
            try:
                flush(sync=True)
                handle.close()
                handle = None
                if segment_bytes <= 0:
                    part_path.unlink(missing_ok=True)
                else:
                    os.replace(part_path, final_path)
                    with self._lock:
                        self._segments_finalized += 1
                    if self.config.compress:
                        self._compression_queue.put(final_path)
                    else:
                        self._apply_retention()
            except Exception as exc:
                self._set_error(f"segment finalize failed: {exc}")
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass
                handle = None
            finally:
                with self._lock:
                    self._current_segment = None
                part_path = None
                final_path = None
                segment_bytes = 0

        try:
            while not self._stop.is_set() or not self._queue.empty():
                now = time.monotonic()
                try:
                    item = self._queue.get(timeout=0.10)
                except queue.Empty:
                    if handle is not None:
                        if now - last_flush >= max(
                            0.05, float(self.config.flush_seconds)
                        ):
                            flush(sync=False)
                        if now - last_fsync >= max(
                            0.25, float(self.config.fsync_seconds)
                        ):
                            flush(sync=True)
                        if (
                            self.config.rotate_seconds > 0
                            and now - opened_at
                            >= float(self.config.rotate_seconds)
                        ):
                            finalize()
                    continue

                try:
                    if handle is None:
                        open_segment()
                    payload = (
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    handle.write(payload)
                    segment_bytes += len(payload)
                    with self._lock:
                        self._written += 1
                        self._bytes_written += len(payload)
                    now = time.monotonic()
                    if now - last_flush >= max(
                        0.05, float(self.config.flush_seconds)
                    ):
                        flush(sync=False)
                    if now - last_fsync >= max(
                        0.25, float(self.config.fsync_seconds)
                    ):
                        flush(sync=True)
                    if (
                        self.config.rotate_bytes > 0
                        and segment_bytes >= int(self.config.rotate_bytes)
                    ):
                        finalize()
                    elif (
                        self.config.rotate_seconds > 0
                        and now - opened_at
                        >= float(self.config.rotate_seconds)
                    ):
                        finalize()
                except (TypeError, ValueError) as exc:
                    # A malformed research event must not kill the writer or
                    # affect live market-data callbacks. Count it as dropped.
                    with self._lock:
                        self._dropped += 1
                    self._set_error(f"record serialization failed: {exc}")
                except Exception as exc:
                    with self._lock:
                        self._dropped += 1
                    self._set_error(f"record write failed: {exc}")
                finally:
                    self._queue.task_done()
        finally:
            finalize()

    def _compression_loop(self) -> None:
        while (
            not self._compression_stop.is_set()
            or not self._compression_queue.empty()
        ):
            try:
                source = self._compression_queue.get(timeout=0.10)
            except queue.Empty:
                continue
            try:
                self._compress_file(source)
            finally:
                self._compression_queue.task_done()

    def _compress_file(self, source: Path) -> None:
        if not source.exists():
            return
        final = source.with_suffix(source.suffix + ".gz")
        part = final.with_suffix(final.suffix + ".part")
        try:
            with source.open("rb") as src, gzip.open(
                part,
                "wb",
                compresslevel=max(
                    1, min(9, int(self.config.compression_level))
                ),
            ) as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            with part.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, final)
            source.unlink(missing_ok=True)
            with self._lock:
                self._compressed_segments += 1
            self._apply_retention()
        except Exception as exc:
            self._set_error(f"segment compression failed: {exc}")
            part.unlink(missing_ok=True)
            # Keep the already-finalized source JSONL as the durable fallback.
            self._apply_retention()

    def _recover_parts(self) -> None:
        pattern = f"{self.config.prefix}-*.jsonl.part"
        for part in sorted(self.directory.glob(pattern)):
            try:
                size = self._truncate_to_last_newline(part)
                if size <= 0:
                    part.unlink(missing_ok=True)
                    continue
                final = part.with_suffix("")
                candidate = final
                suffix = 1
                while candidate.exists():
                    candidate = final.with_name(
                        f"{final.stem}-recovered-{suffix:03d}{final.suffix}"
                    )
                    suffix += 1
                os.replace(part, candidate)
                with self._lock:
                    self._recovered_parts += 1
                    self._segments_finalized += 1
                if self.config.compress:
                    self._compression_queue.put(candidate)
            except Exception as exc:
                self._set_error(f"part recovery failed for {part.name}: {exc}")
        self._apply_retention()

    @staticmethod
    def _truncate_to_last_newline(path: Path) -> int:
        size = path.stat().st_size
        if size <= 0:
            return 0
        chunk = 64 * 1024
        with path.open("r+b") as handle:
            position = size
            last_newline = -1
            while position > 0:
                read_size = min(chunk, position)
                position -= read_size
                handle.seek(position)
                data = handle.read(read_size)
                found = data.rfind(b"\n")
                if found >= 0:
                    last_newline = position + found + 1
                    break
            if last_newline < 0:
                handle.truncate(0)
                handle.flush()
                os.fsync(handle.fileno())
                return 0
            if last_newline < size:
                handle.truncate(last_newline)
            handle.flush()
            os.fsync(handle.fileno())
            return last_newline

    def _apply_retention(self) -> None:
        try:
            finalized = [
                path
                for path in self.directory.glob(f"{self.config.prefix}-*")
                if path.is_file()
                and not path.name.endswith(".part")
                and (
                    path.name.endswith(".jsonl")
                    or path.name.endswith(".jsonl.gz")
                )
            ]
            pattern = re.compile(
                rf"^{re.escape(self.config.prefix)}-"
                r"(?P<session>.+)-(?P<segment>\d{6})\.jsonl(?:\.gz)?$"
            )
            grouped: dict[str, list[Path]] = {}
            for path in finalized:
                match = pattern.match(path.name)
                # Unknown legacy names are isolated so retention never treats
                # them as part of a valid multi-segment recorder session.
                key = match.group("session") if match else f"legacy:{path.name}"
                grouped.setdefault(key, []).append(path)

            sessions: list[tuple[str, list[Path], int, bool]] = []
            for key, paths in grouped.items():
                newest_mtime = max(path.stat().st_mtime_ns for path in paths)
                sessions.append(
                    (key, paths, newest_mtime, key == self._session)
                )
            # The active recorder session is always retained. Other sessions
            # are expired oldest-first as complete units, never segment-by-
            # segment, because removing segment 1 also removes session_start,
            # config and universe metadata required for deterministic replay.
            sessions.sort(key=lambda item: (item[3], item[2], item[0]))

            max_files = max(1, int(self.config.max_files))
            max_bytes = max(0, int(self.config.max_total_bytes))

            def totals() -> tuple[int, int]:
                paths = [path for _, items, _, _ in sessions for path in items]
                total_bytes = 0
                for path in paths:
                    try:
                        total_bytes += path.stat().st_size
                    except FileNotFoundError:
                        pass
                return len(paths), total_bytes

            file_count, total_bytes = totals()
            while len(sessions) > 1 and (
                file_count > max_files
                or (max_bytes > 0 and total_bytes > max_bytes)
            ):
                _, oldest_paths, _, _ = sessions.pop(0)
                for path in oldest_paths:
                    path.unlink(missing_ok=True)
                file_count, total_bytes = totals()
        except Exception as exc:
            self._set_error(f"retention failed: {exc}")

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)


def _deduplicate_segments(paths: Iterable[Path]) -> list[Path]:
    """Return one durable file per logical JSONL segment.

    Compression is deliberately crash safe: the gzip is atomically finalized
    before the source JSONL is removed.  A process crash in that small window
    can therefore leave both files behind.  Reading both would duplicate every
    event in the segment, so prefer the finalized gzip for that exact path.
    """
    selected: dict[str, Path] = {}
    for raw_path in paths:
        path = Path(raw_path)
        logical = path.with_suffix("") if path.name.endswith(".jsonl.gz") else path
        key = str(logical)
        previous = selected.get(key)
        if previous is None or path.name.endswith(".jsonl.gz"):
            selected[key] = path
    return [selected[key] for key in sorted(selected)]


def recording_files(directory: Path, prefix: str = "market") -> list[Path]:
    """Return one finalized file per segment, ignoring active parts."""
    directory = Path(directory).expanduser()
    files = [
        path
        for path in directory.glob(f"{prefix}-*")
        if path.is_file()
        and (
            path.name.endswith(".jsonl")
            or path.name.endswith(".jsonl.gz")
        )
    ]
    return _deduplicate_segments(files)


def iter_records(
    files_or_directory: Path | Iterable[Path],
    *,
    prefix: str = "market",
) -> Iterator[dict[str, Any]]:
    """Iterate finalized JSONL/JSONL.GZ records for future replay tooling."""
    if isinstance(files_or_directory, (str, Path)):
        source = Path(files_or_directory)
        files = recording_files(source, prefix) if source.is_dir() else [source]
    else:
        files = _deduplicate_segments(Path(path) for path in files_or_directory)
    for path in files:
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value
