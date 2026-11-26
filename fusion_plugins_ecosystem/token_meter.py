# Unified token metering and breakdown.
#
# Addresses pain points called out in the PRD:
# - Subagents running 40 minutes with zero token usage and no logs when stuck
# - Chaotic token accounting across multiple plugins
#
# Distinguishes two kinds of consumption:
# - TokenKind.CLAUDE_MODEL: Claude model inference usage (metered per the
#   Claude API)
# - TokenKind.PLUGIN_LOCAL: plugin local compute overhead (metered by
#   wall-clock + MLX tokens)

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TokenKind(StrEnum):
    # Token consumption categories.

    CLAUDE_MODEL = "claude_model"  # Claude model inference
    PLUGIN_LOCAL = "plugin_local"  # plugin local compute
    MLX_INFERENCE = "mlx_inference"  # fusion-mlx local inference
    MCP_RELAY = "mcp_relay"  # MCP protocol relay overhead


@dataclass
class TokenRecord:
    # A single token consumption record.

    plugin_id: str
    kind: TokenKind
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    wall_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    # Extra metadata (e.g. Claude model name, MLX model id)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens


class TokenMeter:
    # Unified token metering and breakdown.
    #
    # Usage:
    #     meter = TokenMeter(desk)
    #     with meter.measure("caveman_compress", TokenKind.PLUGIN_LOCAL):
    #         ...  # plugin execution

    def __init__(
        self,
        desk: Any | None = None,
        max_records: int = 10000,
        persist_path: str | None = None,
        save_batch: int = 50,
        save_interval_seconds: float = 2.0,
    ) -> None:
        self.desk = desk
        self._max_records = max_records
        self._persist_path = persist_path
        self._records: list[TokenRecord] = []
        self._by_plugin: dict[str, list[TokenRecord]] = {}
        # Persistence lock: record() may be called concurrently by multiple
        # coroutines, so file writes must be serialized (P1-7)
        self._save_lock = threading.Lock()
        # R4: throttled persistence to avoid full rewrites saturating IO on
        # every record
        self._save_batch = max(1, save_batch)
        self._save_interval_seconds = save_interval_seconds
        self._last_save_ts = time.time()
        # P3-4: append-write + periodic compaction. _dirty_records holds
        # records pending append to the wal; _next_seq is a monotonic
        # sequence (snapshot.last_seq + wal replay dedup guards against
        # duplicates on crash recovery).
        self._dirty_records: list[TokenRecord] = []
        self._next_seq = 1
        self._wal_line_count = 0
        self._compact_threshold = max(1000, self._max_records * 2)
        if persist_path:
            self._load(persist_path)

    def record(self, rec: TokenRecord) -> None:
        # Record a single token consumption event.
        self._records.append(rec)
        self._by_plugin.setdefault(rec.plugin_id, []).append(rec)
        self._prune()
        # P3-4: push the record into the dirty buffer, append to the wal
        # on a throttle. No longer rewrites the full snapshot per batch.
        self._dirty_records.append(rec)
        if self._should_save():
            self._save()
        # Anomaly detection: PLUGIN_LOCAL with zero input/output tokens and
        # a very long wall_seconds, matching the "subagent runs 40 minutes
        # with no token usage" pain point
        if rec.kind == TokenKind.PLUGIN_LOCAL and rec.total_tokens == 0 and rec.wall_seconds > 60:
            logger.warning(
                "token_meter: plugin %s ran %.1f seconds with no token usage, possibly stuck",
                rec.plugin_id,
                rec.wall_seconds,
            )
            if self.desk is not None:
                self.desk.log(
                    rec.plugin_id,
                    "WARN",
                    "long wall time with no token usage, possibly stuck",
                    wall_seconds=rec.wall_seconds,
                )

    def _should_save(self) -> bool:
        if self._persist_path is None:
            return False
        if len(self._dirty_records) >= self._save_batch:
            return True
        if time.time() - self._last_save_ts >= self._save_interval_seconds:
            return True
        return False

    def flush(self) -> None:
        # Force-persist all current records (shutdown / explicit flush call).
        #
        # P3-4: flush triggers a forced compaction — writes all _records into
        # a single snapshot and clears the wal, so a single file restores
        # state after shutdown with no replay needed. Normal _save appends to
        # the wal (O(batch)); flush compaction (O(N)) happens only on
        # shutdown / explicit calls, keeping amortized cost incremental.
        if self._persist_path is None:
            return
        self._save(force_compact=True)

    def measure(
        self,
        plugin_id: str,
        kind: TokenKind = TokenKind.PLUGIN_LOCAL,
        input_tokens: int = 0,
        output_tokens: int = 0,
        metadata: dict[str, Any] | None = None,
    ):
        # Context manager that measures wall_seconds and records
        # automatically.
        #
        # Usage:
        #     with meter.measure("caveman", TokenKind.PLUGIN_LOCAL):
        #         result = plugin.run(...)
        return _MeasureContext(
            self,
            plugin_id,
            kind,
            input_tokens,
            output_tokens,
            metadata or {},
        )

    def summary(self, since: float | None = None) -> dict[str, dict[str, int]]:
        # Per-plugin aggregated stats: {plugin_id: {kind: total_tokens}}.
        records = self._records_since(since) if since else self._records
        summary: dict[str, dict[str, int]] = {}
        for rec in records:
            pid = rec.plugin_id
            summary.setdefault(pid, {})
            key = rec.kind.value
            summary[pid][key] = summary[pid].get(key, 0) + rec.total_tokens
        return summary

    def records_for(self, plugin_id: str) -> list[TokenRecord]:
        # Return all records for the given plugin.
        return self._by_plugin.get(plugin_id, [])

    def all_records(self) -> list[TokenRecord]:
        # Return all records (in chronological order).
        return list(self._records)

    def _prune(self) -> None:
        # Evict old records beyond max_records.
        #
        # Rebuilds each affected plugin's _by_plugin list in one pass instead
        # of scanning everything with an identity filter per removed record
        # (P3-3: the old implementation was O(N×M)).
        if len(self._records) <= self._max_records:
            return
        excess = len(self._records) - self._max_records
        removed = self._records[:excess]
        self._records = self._records[excess:]
        self._rebuild_by_plugin(removed)

    def _records_since(self, since: float) -> list[TokenRecord]:
        # Return records after the given timestamp.
        #
        # Reverse scan + early termination: records are appended in time
        # order, so the reverse traversal stops at the first record older
        # than since (P3-2).
        out: list[TokenRecord] = []
        for rec in reversed(self._records):
            if rec.timestamp < since:
                break
            out.append(rec)
        out.reverse()
        return out

    def prune(self, max_age_seconds: float | None = None) -> None:
        # Manually evict old records.
        #
        # - When max_age_seconds is given: evict records older than that
        #   many seconds (age-based eviction)
        # - Regardless: also evict records beyond max_records
        #   (count-based eviction)
        if max_age_seconds is not None:
            self._prune_by_age(max_age_seconds)
        self._prune()

    def _prune_by_age(self, max_age_seconds: float) -> None:
        # Evict records older than max_age_seconds.
        cutoff = time.time() - max_age_seconds
        kept = [r for r in self._records if r.timestamp >= cutoff]
        if len(kept) == len(self._records):
            return
        removed = [r for r in self._records if r.timestamp < cutoff]
        self._records = kept
        self._rebuild_by_plugin(removed)
        logger.info("token_meter: evicted %d records by age (>%ss)", len(removed), max_age_seconds)

    def _rebuild_by_plugin(self, removed: list[TokenRecord]) -> None:
        # Rebuild the affected plugins' _by_plugin lists in one pass given
        # the removed set.
        #
        # Avoids per-record identity filtering over the whole table (P3-3).
        # Largest benefit when removed records belong to few plugins.
        if not removed:
            return
        removed_ids = {id(r) for r in removed}
        affected = {r.plugin_id for r in removed}
        for pid in affected:
            bucket = self._by_plugin.get(pid)
            if not bucket:
                continue
            self._by_plugin[pid] = [r for r in bucket if id(r) not in removed_ids]
            if not self._by_plugin[pid]:
                del self._by_plugin[pid]

    def _save(self, force_compact: bool = False) -> None:
        # Persist dirty records (P3-4: append-write + periodic compaction).
        #
        # - Normal: append the dirty batch to the .wal file (O(batch), no
        #   full snapshot rewrite).
        # - Periodic: when wal line count exceeds _compact_threshold, rewrite
        #   the full snapshot and clear the wal (O(N) but only triggered
        #   periodically; amortized cost stays O(batch) incremental).
        # - force_compact (flush/shutdown): compact immediately so a single
        #   file restores state after shutdown.
        # - Snapshot is written atomically (temp + os.replace); the wal is
        #   appended (append mode, fsync for ordering).
        # Concurrency safety: _save_lock serializes file operations (P1-7).
        if not self._persist_path:
            return
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            wal_path = path.with_suffix(path.suffix + ".wal")
            with self._save_lock:
                # Snapshot the pending records, then clear the dirty buffer
                # (copy inside the lock avoids misalignment with concurrent
                # record() calls)
                batch = self._dirty_records
                self._dirty_records = []
                if batch:
                    self._append_wal(wal_path, batch)
                # Periodic or forced compaction: rewrite the full snapshot
                # and clear the wal
                if force_compact or self._wal_line_count >= self._compact_threshold:
                    self._compact(path, wal_path)
            self._last_save_ts = time.time()
        except Exception as e:
            logger.warning("token_meter: persist failed: %s", e)

    def _record_to_dict(self, r: TokenRecord) -> dict[str, Any]:
        return {
            "plugin_id": r.plugin_id,
            "kind": r.kind.value,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "total_tokens": r.total_tokens,
            "wall_seconds": r.wall_seconds,
            "timestamp": r.timestamp,
            "metadata": r.metadata,
        }

    def _append_wal(self, wal_path: Path, batch: list[TokenRecord]) -> None:
        # Append a batch of records to the wal (JSON Lines, one per line).
        lines = []
        for r in batch:
            entry = self._record_to_dict(r)
            entry["seq"] = self._next_seq
            self._next_seq += 1
            lines.append(json.dumps(entry, ensure_ascii=False))
        # Open in append mode, one JSON per line, fsync for crash consistency
        with open(wal_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._wal_line_count += len(lines)

    def _compact(self, path: Path, wal_path: Path) -> None:
        # Rewrite the full snapshot (current _records) and clear the wal.
        #
        # Crash-recovery semantics: the snapshot carries last_seq; on load
        # only wal lines after snapshot.last_seq are replayed. Compaction
        # writes the new snapshot first (with last_seq = current max) and
        # clears the wal only after success — if a crash happens between the
        # snapshot write and the wal clear, replayed wal lines with
        # seq > snapshot.last_seq are still deduplicated (already included),
        # so nothing is applied twice.
        data = [self._record_to_dict(r) for r in self._records]
        max_seq = self._next_seq - 1
        snapshot = {"last_seq": max_seq, "records": data}
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".token_meter_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # Snapshot written successfully, clear the wal
        try:
            wal_path.write_text("")
        except OSError as e:
            logger.warning("token_meter: failed to clear wal: %s", e)
        self._wal_line_count = 0
        logger.info(
            "token_meter: compacted snapshot with %d records, cleared wal (last_seq=%d)",
            len(data),
            max_seq,
        )

    def _load(self, path_str: str) -> None:
        # Load snapshot + replay wal (P3-4).
        #
        # 1. Read the snapshot (dict {last_seq, records} or legacy list)
        #    into _records.
        # 2. Read the wal JSON Lines and replay only lines with
        #    seq > snapshot.last_seq (deduplication).
        # 3. Set _next_seq to the highest seen seq + 1.
        # Legacy list format (no last_seq) stays compatible: loaded in full
        # as if last_seq=0.
        try:
            path = Path(path_str)
            wal_path = path.with_suffix(path.suffix + ".wal")
            last_seq = 0
            if path.exists():
                raw = json.loads(path.read_text())
                records_data: list[dict[str, Any]] = []
                if isinstance(raw, list):
                    # Legacy full-list format
                    records_data = raw
                elif isinstance(raw, dict):
                    records_data = raw.get("records", [])
                    last_seq = int(raw.get("last_seq", 0))
                for item in records_data:
                    rec = self._item_to_record(item)
                    self._records.append(rec)
                    self._by_plugin.setdefault(rec.plugin_id, []).append(rec)
            # replay the wal
            if wal_path.exists():
                for line in wal_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = int(item.get("seq", 0))
                    if seq <= last_seq:
                        # Already included in the snapshot, skip (dedup)
                        continue
                    last_seq = max(last_seq, seq)
                    rec = self._item_to_record(item)
                    self._records.append(rec)
                    self._by_plugin.setdefault(rec.plugin_id, []).append(rec)
                self._wal_line_count = 0  # wal assumed replayed on startup; next _save compacts
            self._next_seq = last_seq + 1
            self._prune()
            logger.info(
                "token_meter: loaded %d records (last_seq=%d) from %s",
                len(self._records),
                last_seq,
                path_str,
            )
        except Exception as e:
            logger.warning("token_meter: load failed: %s", e)

    def _item_to_record(self, item: dict[str, Any]) -> TokenRecord:
        # dict → TokenRecord (shared by snapshot/wal).
        kind_str = item.get("kind", "plugin_local")
        try:
            kind = TokenKind(kind_str)
        except ValueError:
            kind = TokenKind.PLUGIN_LOCAL
        return TokenRecord(
            plugin_id=item.get("plugin_id", "unknown"),
            kind=kind,
            input_tokens=item.get("input_tokens", 0),
            output_tokens=item.get("output_tokens", 0),
            total_tokens=item.get("total_tokens", 0),
            wall_seconds=item.get("wall_seconds", 0.0),
            timestamp=item.get("timestamp", time.time()),
            metadata=item.get("metadata", {}),
        )


class _MeasureContext:
    # Context-manager implementation of measure().

    def __init__(
        self,
        meter: TokenMeter,
        plugin_id: str,
        kind: TokenKind,
        input_tokens: int,
        output_tokens: int,
        metadata: dict[str, Any],
    ) -> None:
        self.meter = meter
        self.plugin_id = plugin_id
        self.kind = kind
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.metadata = metadata
        self._start: float | None = None

    def __enter__(self) -> _MeasureContext:
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._start is None:
            return
        wall = time.time() - self._start
        rec = TokenRecord(
            plugin_id=self.plugin_id,
            kind=self.kind,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            wall_seconds=wall,
            metadata=self.metadata,
        )
        self.meter.record(rec)


class AsyncMeasureContext:
    # Async context-manager implementation of measure().

    def __init__(
        self,
        meter: TokenMeter,
        plugin_id: str,
        kind: TokenKind,
        input_tokens: int = 0,
        output_tokens: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.meter = meter
        self.plugin_id = plugin_id
        self.kind = kind
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.metadata = metadata or {}
        self._start: float | None = None

    async def __aenter__(self) -> AsyncMeasureContext:
        self._start = time.time()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._start is None:
            return
        wall = time.time() - self._start
        rec = TokenRecord(
            plugin_id=self.plugin_id,
            kind=self.kind,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            wall_seconds=wall,
            metadata=self.metadata,
        )
        self.meter.record(rec)
