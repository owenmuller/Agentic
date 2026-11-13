"""``config/lab.yaml``, validated (PAPER PUSH, human ruling 2026-10-09 late)."""
from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_gate.limits import RiskLimits, default_limits_path

ZERO = Decimal("0")
ONE = Decimal("1")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccountConfig(_Strict):
    enabled: bool
    label: str
    sleeve: str
    live_eligible: bool = False
    status: Optional[str] = None
    limits_overrides: dict[str, Any] = Field(default_factory=dict)
    #: The account's drawdown kill switch (12% from its own high-water mark,
    #: the main risk_limits value). OFF only for the leverage ladder (ruling
    #: 2026-10-10, item 3a: the benchmark must keep rebalancing through
    #: drawdowns); every other lab account keeps it - LabConfig enforces that.
    kill_switch: bool = True
    #: Drawdown from peak that raises an urgent alert (once per peak).
    drawdown_alert: Optional[Decimal] = Field(default=None, gt=ZERO, lt=ONE)


class Rung(_Strict):
    name: str
    label: str
    weights: dict[str, Decimal]

    @model_validator(mode="after")
    def _weights(self) -> "Rung":
        if not self.weights or any(w <= ZERO for w in self.weights.values()):
            raise ValueError(f"rung {self.name}: weights must be positive")
        if sum(self.weights.values()) != ONE:
            raise ValueError(f"rung {self.name}: weights must sum to 1")
        return self


class LadderConfig(_Strict):
    rungs: tuple[Rung, ...]
    cash_reserve_fraction: Decimal = Field(ge=ZERO, lt=Decimal("0.1"))
    min_trade_usd: Decimal = Field(gt=ZERO)
    limit_buffer: Decimal = Field(ge=ZERO, lt=Decimal("0.02"))
    reprice_seconds: int = Field(gt=0)
    fill_wait_seconds: int = Field(gt=0)
    trade_after: str
    trade_before: str

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({s for rung in self.rungs for s in rung.weights}))


class AiOptionsConfig(_Strict):
    """How the AI trader's rules engine picks a contract for a call/put idea:
    deterministic, never the model's choice of strike."""

    #: Days to expiry allowed by horizon: [min, max]. intraday may be 0DTE;
    #: "days" and "weeks" need at least 7 (human ruling 2026-11-12, item 3).
    dte: dict[str, tuple[int, int]]
    #: The window each horizon used BEFORE that ruling: the contract it would
    #: have picked is priced beside every entry, logged only (same ruling).
    counterfactual_dte: dict[str, tuple[int, int]] = Field(default_factory=dict)
    delta_min: Decimal = Field(gt=ZERO, lt=ONE)
    delta_max: Decimal = Field(gt=ZERO, le=ONE)
    target_delta: Decimal = Field(gt=ZERO, lt=ONE)
    min_open_interest: int = Field(ge=0)
    max_spread_pct_of_mid: Decimal = Field(gt=ZERO, le=ONE)
    max_iv_percentile: Decimal = Field(gt=ZERO, le=ONE)
    #: Close time (ET) on a contract's expiry day; a multi-day contract is
    #: also closed at this time on the session BEFORE expiry.
    expiry_close: str

    @model_validator(mode="after")
    def _bands(self) -> "AiOptionsConfig":
        if not self.delta_min <= self.target_delta <= self.delta_max:
            raise ValueError("target_delta must sit inside [delta_min, delta_max]")
        for horizon in ("intraday", "days", "weeks"):
            low, high = self.dte.get(horizon, (-1, -1))
            if low < 0 or high < low:
                raise ValueError(f"options dte for {horizon!r} must be [min, max] with 0 <= min <= max")
        for horizon in ("days", "weeks"):
            if self.dte[horizon][0] < MIN_MULTI_DAY_DTE:
                raise ValueError(f"options dte for {horizon!r} must start at {MIN_MULTI_DAY_DTE} or more "
                                 f"(human ruling 2026-11-12, item 3)")
        return self


#: Human ruling 2026-11-12, item 3: options on "days" or "weeks" horizons
#: carry at least 7 days to expiry (the 11-11 calls had 2).
MIN_MULTI_DAY_DTE = 7


class ConcentrationConfig(_Strict):
    """One bet per sector or theme (human ruling 2026-11-12, item 2, a live
    rule): no new position whose sector or theme is already held or already
    proposed earlier in the same pass, and at most this many new options per
    scan. The sector comes from the gate's map (``config/sectors.yaml``; an
    unmapped name is its own sector); the theme is the model's tag, one of
    these names. Origin: three AI-chip calls from one scan lost $4,622 at one
    open (2026-11-12).

    Readings (human ruling 2026-11-12, later): "already proposed" counts only
    earlier ideas in the pass whose entry order was placed (filled, working
    or unfilled); a refused idea never blocks a later one. A theme in
    ``exempt_themes`` ("other") never collides by theme; its sector still does."""

    #: name -> what belongs in it; rendered into the prompt and the tool's enum.
    themes: dict[str, str]
    max_new_options_per_scan: int = Field(ge=0)
    #: themes that never collide by theme (a catch-all is not a shared bet)
    exempt_themes: tuple[str, ...] = ("other",)

    @model_validator(mode="after")
    def _names(self) -> "ConcentrationConfig":
        if "other" not in self.themes:
            raise ValueError("the theme list needs 'other' for an idea that fits none")
        for name in self.themes:
            if not re.fullmatch(r"[a-z][a-z_]*", name):
                raise ValueError(f"theme {name!r}: lowercase letters and underscores only")
        unknown = set(self.exempt_themes) - set(self.themes)
        if unknown:
            raise ValueError(f"exempt_themes not in the theme list: {sorted(unknown)}")
        return self


class MoveSinceConfig(_Strict):
    """Move since public (human ruling 2026-11-12, item 4): LOGGED ONLY. At
    proposal time the catalyst's first public time is looked up in the news
    feed and the underlying's move since then is measured against its
    ATR(14); a shadow records "skip if the move exceeds ``skip_above_atr`` x
    ATR". Nothing here can stop or size a trade."""

    enabled: bool
    #: How far back the news feed is searched for the catalyst's first headline.
    news_lookback_hours: int = Field(gt=0, le=168)
    #: Headline words (4+ letters, stopwords out) it must share with the catalyst.
    min_word_overlap: int = Field(ge=1)
    skip_above_atr: Decimal = Field(gt=ZERO)


class AiTraderConfig(_Strict):
    """2b, the AUTONOMOUS AI TRADER (PAPER PUSH 2026-10-09; built per ruling
    2026-10-10, item 5). The LLM decides what and why; these rules decide
    how much, when out, and whether at all."""

    # -- the model (must be in research.yaml pinned_models) -------------------
    model: str
    effort: str
    max_searches: int = Field(ge=1, le=5)
    #: The search tool version (see research.config.WebSearchConfig.tool_type).
    search_tool: str
    #: Per-request timeout; one retry. A pass that cannot finish is a fault
    #: (no trade), never a frozen session.
    request_timeout_seconds: int = Field(gt=0, le=600)
    # -- spend: the daily cap is hard; a pass starts only with reserve left ---
    daily_spend_cap_usd: Decimal = Field(gt=ZERO)
    pass_cost_reserve_usd: Decimal = Field(gt=ZERO)
    search_fee_usd: Decimal = Field(ge=ZERO)
    # -- cadence ---------------------------------------------------------------
    scan_times: tuple[str, ...]
    max_ideas_per_scan: int = Field(gt=0, le=5)
    tick_seconds: int = Field(gt=0)
    # -- the rules engine --------------------------------------------------------
    risk_fraction: Decimal = Field(gt=ZERO, le=Decimal("0.05"))
    max_positions: int = Field(gt=0)
    #: Explicit gross cap (human ruling 2026-10-14, audit item 4): open
    #: exposure plus the new order may not pass this fraction of NAV. Per
    #: position the gate's caps already bind tighter than 25%: stocks
    #: 18.75% of NAV (aggressive 0.25 x 0.75), option premium 2.5%.
    max_gross_fraction: Decimal = Field(default=Decimal("1.00"), gt=ZERO, le=ONE)
    #: Which gross the cap enforces (human ruling 2026-10-14, second, item 2):
    #: "face" (notional) until the human decides; "leverage_adjusted" counts
    #: notional x |leverage factor|. Both are always measured and reported.
    gross_basis: Literal["face", "leverage_adjusted"] = "face"
    #: Leverage watch (human ruling 2026-10-14, third): an URGENT alert when
    #: leverage-adjusted gross passes this fraction of NAV, once per crossing,
    #: re-armed when it falls below the second. Information only: nothing is
    #: blocked on the leverage-adjusted figure (the cap stays on face value).
    leverage_alert_above: Decimal = Field(default=Decimal("1.50"), gt=ZERO)
    leverage_alert_rearm_below: Decimal = Field(default=Decimal("1.30"), gt=ZERO)
    #: Signed leverage factors by symbol (SOXS -3, SSO 2, ...). A fund not
    #: listed is read from its name at the broker; anything else is 1.
    leverage_factors: dict[str, Decimal] = Field(default_factory=dict)
    #: The research brief (human rulings 2026-10-15; ``lab.brief``): optional
    #: reference data appended to a scan's prompt. "off": never sent; "ab":
    #: an A/B test - each pass draws its arm at random, half with the brief
    #: and half without, the arm logged on the pass; "always": every pass
    #: (the golden replay). A pass without the brief sends a prompt and a
    #: report tool byte-identical to the brief-less ones.
    brief_mode: Literal["off", "ab", "always"] = "off"
    #: The brief arm's share of passes under "ab".
    brief_ab_share: float = Field(default=0.5, gt=0, lt=1)
    brief_evidence_path: str = "config/evidence_library.yaml"
    min_confidence: int = Field(ge=0, le=100)
    min_reward_risk: Decimal = Field(ge=ZERO)
    min_stop_fraction: Decimal = Field(gt=ZERO, lt=ONE)
    max_stop_fraction: Decimal = Field(gt=ZERO, lt=ONE)
    no_entries_after: str
    no_intraday_entries_after: str
    intraday_close: str
    horizon_sessions: dict[str, int]
    limit_buffer: Decimal = Field(ge=ZERO, lt=Decimal("0.05"))
    fill_wait_seconds: int = Field(gt=0)
    options: AiOptionsConfig
    concentration: ConcentrationConfig
    move_since_public: MoveSinceConfig
    #: The "would exit" shadow (human ruling 2026-11-12, item 1): through this
    #: date every tick also computes the OLD exit rule (the mid of any quote,
    #: however wide) and logs each disagreement with the new one. None: off.
    exit_shadow_until: Optional[date] = None

    @model_validator(mode="after")
    def _sane(self) -> "AiTraderConfig":
        if self.leverage_alert_rearm_below >= self.leverage_alert_above:
            raise ValueError("leverage_alert_rearm_below must be below leverage_alert_above")
        if self.min_stop_fraction >= self.max_stop_fraction:
            raise ValueError("min_stop_fraction must be below max_stop_fraction")
        if self.pass_cost_reserve_usd > self.daily_spend_cap_usd:
            raise ValueError("pass_cost_reserve_usd cannot exceed the daily cap")
        if set(self.horizon_sessions) != {"days", "weeks"}:
            raise ValueError("horizon_sessions needs exactly 'days' and 'weeks'")
        return self


class LiveMirrorConfig(_Strict):
    """The AI trader's LIVE mirror on the Robinhood Agentic account (GO LIVE,
    human ruling 2026-11-04; ``lab.live_mirror``). Real money: every bound
    here is the ruling's and only tightens."""

    enabled: bool = False
    #: The Robinhood Agentic account, pinned (refused in code otherwise).
    account_number: str
    #: Settled cash the mirror waits for before its round trip (human ruling
    #: 2026-11-04, second: $500; deposits after it are cash flows).
    min_funding_usd: Decimal = Field(gt=ZERO)
    #: An unexplained cash change at least this large is a deposit or withdrawal.
    flow_threshold_usd: Decimal = Field(default=Decimal("1"), gt=ZERO)
    paper_account: str = "ai-trader"
    token_store: str = "~/.config/agentic/rh_oauth.json"
    risk_fraction: Decimal = Field(default=Decimal("0.02"), gt=ZERO, le=Decimal("0.02"))
    max_position_fraction: Decimal = Field(default=Decimal("0.1875"), gt=ZERO, le=Decimal("0.1875"))
    #: Of STARTING equity (the ruling); the gate's own 12%-from-peak applies too.
    kill_fraction: Decimal = Field(default=Decimal("0.12"), gt=ZERO, le=Decimal("0.12"))
    daily_loss_fraction: Decimal = Field(default=Decimal("0.04"), gt=ZERO, le=Decimal("0.04"))
    round_trip_symbol: str = "SPY"
    round_trip_shares: Decimal = Field(default=Decimal("0.01"), gt=ZERO, le=Decimal("0.05"))
    #: Cards go to #urgent for this many days of mirroring, then #reports.
    urgent_days: int = Field(default=7, ge=7)
    tick_seconds: int = Field(default=15, ge=5, le=120)
    session_start: str = "09:30"
    session_end: str = "16:05"
    fill_wait_seconds: int = Field(default=60, ge=10, le=600)
    #: A place call that failed mid-request is watched by its reference this long
    #: before it is called never placed (2026-11-05: an HTTP 502 on the MCP).
    unknown_order_seconds: int = Field(default=120, ge=30, le=900)
    limit_buffer: Decimal = Field(default=Decimal("0.002"), ge=ZERO, lt=Decimal("0.02"))
    #: The gate's dust floor (risk_limits equity_sleeve.min_order_notional_usd).
    min_notional_usd: Decimal = Field(default=Decimal("5"), ge=Decimal("1"))
    #: A paper entry copied later than this is skipped, not chased.
    max_entry_delay_seconds: int = Field(default=300, ge=30, le=1800)
    #: TEMPORARY option ruling 2026-11-09 (while the account is too small for
    #: typical contracts): a paper CALL whose contract does not fit the premium
    #: cap is copied as its underlying stock. Off until the human approves the
    #: proxy method; a put that does not fit is always skipped.
    option_proxy_enabled: bool = False
    #: The one live option round trip (same ruling, item 3): buy and sell one
    #: cheap, liquid call through the gate. Native option copies wait for it.
    option_round_trip_enabled: bool = False
    option_round_trip_symbol: str = "SPY"
    option_round_trip_window: tuple[str, str] = ("09:45", "15:00")
    #: A round-trip contract's spread may be at most this wide (dollars a share).
    option_round_trip_max_spread: Decimal = Field(default=Decimal("0.02"), gt=ZERO, le=Decimal("0.05"))
    option_round_trip_attempts: int = Field(default=3, ge=1, le=5)
    #: The one-time live path tests (human ruling 2026-11-10, sweep item 1):
    #: T1/T4 a far-from-market limit buy (stock, option) placed, found and
    #: cancelled; T2 a whole share out on its stop with a cancel and re-price;
    #: T3 a fraction out on its target. Entries and the round trip wait for them.
    path_tests_enabled: bool = False
    path_tests_start: str = "09:31"
    #: configured for a 16:00 close (moves with an early one)
    path_tests_end: str = "11:00"
    path_test_symbol: str = "F"
    path_test_fraction_symbol: str = "SPY"
    path_test_fraction: Decimal = Field(default=Decimal("0.05"), gt=ZERO, le=Decimal("0.1"))
    #: a T2/T3 position not closed by then fails the test (its exit keeps running)
    path_test_exit_minutes: int = Field(default=10, ge=3, le=60)
    #: The live account's own cap table on top of risk_limits.yaml.
    limits_overrides: dict[str, Any] = Field(default_factory=dict)


class LabConfig(_Strict):
    version: int
    accounts: dict[str, AccountConfig]
    ladder: LadderConfig
    ai_trader: Optional[AiTraderConfig] = None
    live_mirror: Optional[LiveMirrorConfig] = None

    @model_validator(mode="after")
    def _pinned_model(self) -> "LabConfig":
        """The AI trader's model obeys the main model-pinning rule (ruling
        2026-09-02): it must be on research.yaml's closed pinned_models list,
        and never a moving ``-latest`` alias."""
        if self.ai_trader is None:
            return self
        model = self.ai_trader.model
        research = Path(__file__).resolve().parents[2] / "config" / "research.yaml"
        with open(research, "r", encoding="utf-8") as handle:
            pinned = (yaml.safe_load(handle) or {}).get("pinned_models") or []
        if model.endswith("-latest") or model not in pinned:
            raise ValueError(f"ai_trader.model {model!r} is not on research.yaml pinned_models {pinned}")
        return self

    @model_validator(mode="after")
    def _kill_switch_off_only_for_the_ladder(self) -> "LabConfig":
        for name, account in self.accounts.items():
            if not account.kill_switch and account.sleeve != "leverage_ladder":
                raise ValueError(
                    f"lab account {name!r}: only the leverage ladder may run without its kill switch "
                    f"(ruling 2026-10-10, item 3a); the 12% switch stays for every other lab account"
                )
        return self

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "LabConfig":
        path = path or Path(__file__).resolve().parents[2] / "config" / "lab.yaml"
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))

    def account(self, name: str) -> AccountConfig:
        if name not in self.accounts:
            raise KeyError(f"no lab account named {name!r} (config/lab.yaml has {sorted(self.accounts)})")
        return self.accounts[name]


def _merge(base: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def account_limits(account: AccountConfig, base_path: Optional[Path] = None) -> RiskLimits:
    """The account's cap table: the main ``risk_limits.yaml`` with the
    account's overrides on top, validated by the SAME ``RiskLimits`` model -
    so an override that switches margin on or permits shorting fails to load
    (Constraints #1 and #2 are not configuration)."""
    with open(base_path or default_limits_path(), "r", encoding="utf-8") as handle:
        base = yaml.safe_load(handle)
    merged = _merge(base, account.limits_overrides)
    if not account.kill_switch:
        # The drawdown trip at 100% of the high-water mark: unreachable in a
        # cash account (NAV cannot go below zero). The operator halt marker
        # still freezes the account by hand.
        merged["kill_switch"]["drawdown_from_high_water_mark"] = "1"
    return RiskLimits.model_validate(merged)
