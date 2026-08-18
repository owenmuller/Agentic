"""Signal scanners — three latency classes, cadence per ``config/signals.yaml``.

CONSTRAINT #5 (CLAUDE.md): signals are data, not commands. Content fetched here is
untrusted external input. It is extracted, classified and queued; it is never
interpreted as an instruction to the agent, whatever it says.

This package deliberately imports nothing from ``risk_gate``, ``sizing`` or
``execution``. A scanner's entire vocabulary is "a Signal was observed" — it cannot
size, price, approve or send anything, so no amount of persuasive content in a post
can produce a trade from here.
"""

from signals.classification import (
    ClassificationResult,
    Segment,
    classify_post,
    extract_tickers,
)
from signals.config import ClassConfig, SignalsConfig, SourceConfig, default_signals_path
from signals.records import (
    UNTRUSTED_CONTENT_PREAMBLE,
    Classification,
    CredibilityLog,
    CredibilityRecord,
    Priority,
    Signal,
    SignalClass,
    SignalQueue,
    as_data_block,
    signal_id_for,
)
from signals.scanners import (
    Class1RealtimeScanner,
    Class2CongressionalScanner,
    Class3Form13FScanner,
    Fetcher,
    RawItem,
    Scanner,
    build_scanners,
    is_market_hours,
)

__all__ = [
    "UNTRUSTED_CONTENT_PREAMBLE",
    "ClassConfig",
    "Class1RealtimeScanner",
    "Class2CongressionalScanner",
    "Class3Form13FScanner",
    "Classification",
    "ClassificationResult",
    "CredibilityLog",
    "CredibilityRecord",
    "Fetcher",
    "Priority",
    "RawItem",
    "Scanner",
    "Segment",
    "Signal",
    "SignalClass",
    "SignalQueue",
    "SignalsConfig",
    "SourceConfig",
    "as_data_block",
    "build_scanners",
    "classify_post",
    "default_signals_path",
    "extract_tickers",
    "is_market_hours",
    "signal_id_for",
]
