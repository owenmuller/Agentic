"""MOMENTUM SANITY BACKTEST (human ruling 2026-10-13, item 5): free,
informational, NOT decisive. v1 and every declared shadow on Alpaca daily
data 2016-2026, delisted names included, reused tickers split by company.

    python ops/experiments/momentum_sanity.py OUT_DIR [--rebuild] [--universe-version 1|2]

Writes OUT_DIR/report.txt (and report.json). Every stage caches under
OUT_DIR/cache, so a restart resumes where it stopped. A research process:
the data guard confines it to scratch and research directories.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from momentum import engine, rules  # noqa: E402 - installs the data guard
from momentum.store import Store, build  # noqa: E402

COST = rules.V1.cost_per_side


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def stats(navs: list, calendar: list) -> dict:
    values = [v for _, v in navs]
    if len(values) < 2 or values[0] <= 0:
        return {}
    years = (len(values) - 1) / 252
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if years > 0 else 0.0
    peak, dd = values[0], 0.0
    rets = []
    for a, b in zip(values, values[1:]):
        rets.append(b / a - 1 if a > 0 else 0.0)
    for v in values:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    vol = rules.stdev(rets) * math.sqrt(252)
    by_year: dict = {}
    for (t, v) in navs:
        y = calendar[t][:4]
        by_year.setdefault(y, [v, v])
        by_year[y][1] = v
    prev = None
    yearly = {}
    for y in sorted(by_year):
        start = prev if prev is not None else by_year[y][0]
        yearly[y] = by_year[y][1] / start - 1
        prev = by_year[y][1]
    return dict(cagr=cagr, max_dd=dd, vol=vol, sharpe=(rules.mean(rets) * 252) / vol if vol else None,
                total=values[-1] / values[0] - 1, years=years, yearly=yearly)


def series(prices, start: int, end: int) -> list:
    out, last = [], None
    for t in range(start, end + 1):
        p = prices[t]
        if engine.ok(p):
            last = p
        if last is not None:
            out.append((t, last))
    return out


def best3_share(yearly: dict) -> float:
    """Share of the total log gain that came from the best 3 calendar years."""
    logs = {y: math.log(1 + r) for y, r in yearly.items() if r > -1}
    total = sum(logs.values())
    best = sum(sorted(logs.values(), reverse=True)[:3])
    return best / total if total > 0 else float("nan")


def main() -> int:
    out = Path(sys.argv[1]).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    store_dir = out / "store"
    if (store_dir / "meta.json").exists() and "--rebuild" not in sys.argv:
        store = Store.load(store_dir)
        coverage = json.loads((out / "coverage.json").read_text())
        log("store loaded")
    else:
        # the live plan's own builder and universe rules (version 2 from the
        # 2026-11-02 rebalance; --universe-version 1 = as traded in October)
        version = int(sys.argv[sys.argv.index("--universe-version") + 1]) if "--universe-version" in sys.argv else 2
        store, coverage = build(out / "cache", log=log, universe_version=version)
        store.save(store_dir)
        (out / "coverage.json").write_text(json.dumps(coverage, indent=2))
        log("store saved")
    cal = store.calendar
    end = len(cal) - 1
    starts = engine.month_starts(cal)
    # the first month with 12 monthly returns behind it (data begins 2016-01)
    first = starts[13]
    log(f"backtest {cal[first]} .. {cal[end]}")

    results = {}
    specs = {"v1.1 (live spec, no AI veto in a backtest)": rules.LIVE, "v1 (top 1,000; a shadow since 2026-10-14)": rules.V1,
             **{k: v for k, v in rules.SHADOWS.items() if k not in ("v1_1_no_veto", "v1_top_1000")}}
    for name, spec in specs.items():
        t0 = time.time()
        run = engine.run(store, spec, first, end)
        s = stats(run.nav, cal)
        blocked = sum(1 for d in run.decisions if not d.buys_allowed)
        s.update(turnover_per_year=run.turnover / s["years"], months=len(run.decisions),
                 months_market_filter_on=blocked,
                 mean_exposure=rules.mean([d.exposure for d in run.decisions]),
                 mean_holdings=rules.mean([len(d.holdings) for d in run.decisions]),
                 delisted_exits=len(run.delisted))
        results[name] = s
        with open(out / f"decisions_{spec.name}.jsonl", "w", encoding="utf-8") as fh:
            for d in run.decisions:
                fh.write(json.dumps(dict(day=cal[d.t], exposure=d.exposure, buys_allowed=d.buys_allowed,
                                         holdings={k: round(w, 4) for k, w in d.holdings.items()},
                                         bought=d.selection.bought, sold=d.selection.sold,
                                         skipped=d.selection.skipped[:20], universe=d.details["universe"],
                                         scored=d.details["scored"], current_vol=d.details["current_vol"],
                                         target_vol=d.details["target_vol"])) + "\n")
        log(f"{name}: CAGR {s['cagr']:.2%} maxDD {s['max_dd']:.2%} ({time.time() - t0:.0f}s)")

    xnav, picks = engine.x_thread(store, starts[12], end)
    xs = stats(xnav, cal)
    xs["best3_share_of_gain"] = best3_share(xs["yearly"])
    xs["picks"] = picks
    results["X thread: single top prior-year gainer (top-500 proxy)"] = xs

    bench = {"SPY": series(store.spy, first, end)}
    bench.update({k: series(v, first, end) for k, v in store.benchmarks.items()})
    bstats = {k: stats(v, cal) for k, v in bench.items() if v}

    lines = [f"MOMENTUM SANITY BACKTEST - informational, not decisive ({cal[first]} to {cal[end]})",
             f"costs {COST:.2%} of traded notional per side; fills at the rebalance day's close; "
             "no delisting return beyond the last trade; cash earns nothing", "",
             f"Coverage: {json.dumps(coverage)}", "",
             f"{'variant':52} {'CAGR':>7} {'maxDD':>7} {'vol':>6} {'Sharpe':>6} {'turn/yr':>7} {'expo':>5}"]
    for name, s in list(results.items()) + [(f"benchmark {k}", v) for k, v in bstats.items()]:
        if not s:
            continue
        lines.append(f"{name[:52]:52} {s['cagr']:>7.2%} {s['max_dd']:>7.2%} {s['vol']:>6.1%} "
                     f"{(s['sharpe'] or 0):>6.2f} {s.get('turnover_per_year', 0):>7.1f} "
                     f"{s.get('mean_exposure', 1):>5.2f}")
    lines += ["", "Calendar-year returns:"]
    years = sorted({y for s in results.values() for y in s.get("yearly", {})})
    lines.append(f"{'':52} " + " ".join(f"{y:>7}" for y in years))
    for name, s in list(results.items()) + [(f"benchmark {k}", v) for k, v in bstats.items()]:
        lines.append(f"{name[:52]:52} " + " ".join(f"{s.get('yearly', {}).get(y, float('nan')):>7.1%}" for y in years))
    lines += ["", f"X thread: share of log gain from the best 3 years {xs['best3_share_of_gain']:.0%}; picks:"]
    lines += [f"  {d}: {k} (prior-year return {r:+.0%})" for d, k, r in picks]
    lines += ["", "FLAGS:",
              "- Data begins 2016-01, so the first decision is 2017-02 and the 36-month residual regression "
              "runs on 12-35 months until 2019-02.",
              "- The window holds the 2020 crash and the 2022 bear market, but no prolonged crash (2000-02, "
              "2008) and no momentum crash like 2009: it cannot show how v1 behaves in one.",
              "- The X thread's universe is the top 500 by market cap, a proxy: this data has no S&P 500 "
              "membership history.",
              "- 'US stocks' = companies filing 10-K/10-Q; foreign filers (20-F/6-K) are excluded.",
              "- A name with no 8-K 2.02 release in 12 months scores 0 on the earnings leg (neutral).",
              "- Exclusions gate buys only; holdings sell at rank > 30.",
              "- Vol scaling's target is the UNSCALED book's own trailing vol (756 sessions, minimum 126), "
              "so scaling cannot ratchet itself down."]
    text = "\n".join(lines)
    (out / "report.txt").write_text(text, encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"results": results, "benchmarks": bstats,
                                                 "coverage": coverage}, default=str, indent=1))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
