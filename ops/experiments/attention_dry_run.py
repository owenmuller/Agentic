"""Live dry-run of the attention-momentum path (risk-on redirect, 2026-10-08;
the overnight deploy gate): the production screen over the PRODUCTION audit
log and live SIP bars, through the production Class 2 scanner into Signal
objects, each written as a record into a TEMPORARY audit log and read back
through the forward funnel. No LLM, no broker, no production file written.

    python ops/experiments/attention_dry_run.py [--json OUT]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PAPER_MODE", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from audit.log import AuditLog  # noqa: E402
from audit.records import RejectedStage  # noqa: E402
from execution.market_data import AlpacaDailyBars  # noqa: E402
from forward.funnel import funnel_entries  # noqa: E402
from orchestrator.attention import AttentionMomentumFetcher  # noqa: E402
from orchestrator.config import OrchestratorConfig  # noqa: E402
from signals import SignalQueue, SignalsConfig  # noqa: E402
from signals.scanners import Class2CongressionalScanner  # noqa: E402

data = Path(os.path.expanduser("~/Agentic/data"))
production = AuditLog(path=data / "audit.jsonl")
config = OrchestratorConfig.load().aggressive_sleeve.attention
bars = AlpacaDailyBars(feed="sip")
now = datetime.now(timezone.utc)
# --replay N: also run the screen as of each of the last N weekday mornings,
# so real confirmations exercise the record path end to end.
mornings = [now]
if "--replay" in sys.argv:
    from datetime import timedelta

    back = int(sys.argv[sys.argv.index("--replay") + 1])
    day = now
    while len(mornings) < back + 1:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            mornings.append(day.replace(hour=14, minute=0, second=0, microsecond=0))
items = []
for moment in mornings:
    fetcher = AttentionMomentumFetcher(production.records, bars.bars_many, config, clock=lambda m=moment: m)
    found = fetcher.screen(moment)  # bypass the once-a-morning gate: this is a dry run
    print(moment.date(), "tally:", fetcher.last_tally)
    items.extend(found)
bars.close()
for item in items:
    f = item.fields
    print(f"  {item.external_id}: {f['event_source']} t0 {f['t0']} confirm {f['confirm_day']} (t0+{f['confirm_offset']}) vol x{f['volume_ratio']} excess {float(f['excess_since_event']):+.2%} atr {f['atr_fraction']}")

signals_config = SignalsConfig.load()
queue = SignalQueue()
scanner = Class2CongressionalScanner(
    signals_config.klass("class_2"),
    lambda source: items if source.id == "attention_momentum" else [],
    queue,
    clock=lambda: now,
)
signals = [s for s in scanner.poll(force=True) if s.source_id == "attention_momentum"]
print(f"signals emitted by the production scanner: {len(signals)}")
with tempfile.TemporaryDirectory() as tmp:
    audit = AuditLog(path=Path(tmp) / "audit.jsonl")
    for n, signal in enumerate(signals):
        audit.record_stage_rejection(f"dryrun-{n:04d}", RejectedStage.PRE_FILTER, "dry_run", "attention dry run: recorded, not researched", signal)
    entries = [e for e in funnel_entries(audit.records()) if e.source_id == "attention_momentum"]
    print(f"records written {len(signals)}; funnel entries read back {len(entries)}; tickers {[e.primary_ticker for e in entries][:12]}")
    print("metadata of the first signal:", {k: signals[0].metadata.get(k) for k in ("tickers", "atr_fraction", "priced_in_analysis_required", "event_source")} if signals else "-")
if "--json" in sys.argv and items:
    out = Path(sys.argv[sys.argv.index("--json") + 1])
    out.write_text(json.dumps([{"external_id": i.external_id, "content": i.content, "fields": i.fields} for i in items], indent=1), encoding="utf-8")
print("DRY RUN OK" if len(signals) == len(items) and (not items or len(entries) == len(items)) else "DRY RUN MISMATCH")
