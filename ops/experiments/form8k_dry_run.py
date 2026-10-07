"""Live dry-run of the changed 8-K path (human ruling 2026-10-07, item 2 and
the overnight deploy gate): the production lister against live EDGAR with
pagination, through the production Class 1 scanner into Signal objects, each
written as a record into a TEMPORARY audit log and read back through the
forward funnel. No LLM, no broker, no production file touched.

    python ops/experiments/form8k_dry_run.py [--lookback-days N]
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PAPER_MODE", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import httpx  # noqa: E402

from audit.log import AuditLog  # noqa: E402
from audit.records import RejectedStage  # noqa: E402
from forward.funnel import funnel_entries  # noqa: E402
from signals import Class1RealtimeScanner, Form8KFetcher, SignalQueue, SignalsConfig  # noqa: E402

lookback = int(sys.argv[sys.argv.index("--lookback-days") + 1]) if "--lookback-days" in sys.argv else 1
config = SignalsConfig.load()
source = config.source("class_1", "form_8k")
requests: list[str] = []


def _record(request: httpx.Request) -> None:
    requests.append(str(request.url))


client = httpx.Client(event_hooks={"request": [_record]})
fetcher = Form8KFetcher(client, lookback_days=lookback, clock=lambda: datetime.now(timezone.utc))
queue = SignalQueue()


def fetch(cfg):
    return list(fetcher(cfg)) if cfg.id == "form_8k" else []


scanner = Class1RealtimeScanner(config.klass("class_1"), fetch, queue, clock=lambda: datetime.now(timezone.utc))
signals = [s for s in scanner.poll(force=True) if s.source_id == "form_8k"]
listing = [u for u in requests if "efts.sec.gov" in u]
pages_by_item: dict[str, list[int]] = {}
for u in listing:
    q = httpx.URL(u).params
    pages_by_item.setdefault(q.get("q", "?"), []).append(int(q.get("from", "0") or 0))
print(f"lookback {lookback} day(s); tally {fetcher.last_tally}")
print("listing pages read per item:", {k: sorted(v) for k, v in pages_by_item.items()})
print(f"signals emitted: {len(signals)}")

with tempfile.TemporaryDirectory() as tmp:
    audit = AuditLog(path=Path(tmp) / "audit.jsonl")
    n = 0
    for signal in signals:
        audit.record_stage_rejection(
            f"dryrun-{n:04d}",
            RejectedStage.PRE_FILTER,
            "dry_run",
            "8-K lister dry run (ruling 2026-10-07): listed live, recorded, not researched",
            signal,
        )
        n += 1
    entries = [e for e in funnel_entries(audit.records()) if e.source_id == "form_8k"]
    print(f"records written: {n}; funnel entries read back: {len(entries)}; with 8-K items parsed: {sum(1 for e in entries if e.form8k_items)}")
    for e in entries[:12]:
        print(f"  {e.observed_at.date()} {e.primary_ticker:6} items={','.join(e.form8k_items or ())} code={e.code}")
    vst = [e for e in entries if e.primary_ticker == "VST"]
    print(f"Vistra entries in the window: {len(vst)}")
client.close()
print("DRY RUN OK" if n == len(signals) and len(entries) == n else "DRY RUN MISMATCH")
