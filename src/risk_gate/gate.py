"""The enforcing risk gate.

CONSTRAINT #3 (CLAUDE.md): deterministic Python, no LLM calls, no bypass path. Every
order passes through ``RiskGate.submit`` before it can reach a broker.

The no-bypass property is carried by ``ApprovedOrder``: the execution layer accepts
only an ``ApprovedOrder``, and the only code that can construct one is
``RiskGate._approve``. An unchecked ``Order`` is not type-compatible with the broker
interface, so "forgot to call the gate" is a construction error rather than a silent
live trade.

Check ordering
--------------
Checks run most-fundamental first, and the first failure wins — so a rejection code
names the most basic reason an order failed, not an incidental one. Kill switch
precedes everything; cash-securing precedes percentage caps, because a cap breach on
an order you cannot afford anyway is the less useful thing to log.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Final, Mapping, Optional, Union, final

from risk_gate.limits import RiskLimits
from risk_gate.rejections import Rejection, RejectionCode
from risk_gate.sectors import SectorMap
from risk_gate.schema import Order
from risk_gate.state import (
    AccountState,
    AccountType,
    PositionKey,
    Sleeve,
    is_option,
    position_key,
    sleeve_of,
    unit_multiplier,
    units_of,
)

ZERO = Decimal("0")

#: Held by this module alone. ``ApprovedOrder.__init__`` demands it, which makes
#: constructing one outside the gate an error rather than a possibility.
#:
#: This is a guardrail against accident and a marker for code review, not a security
#: boundary — Python has no true privacy, and anything in-process can reach this name
#: if it sets out to. It stops the failure mode that actually happens: an execution
#: path added later that forgets to call the gate.
_APPROVAL_TOKEN: Final = object()


class BuyingPowerBreached(RuntimeError):
    """A settled fill drove buying power negative — Constraint #1 violated in reality.

    Reachable only when a broker fills an order above the price the gate reserved
    against (a market order printing worse than its ``max_price``). The gate cannot
    prevent that; it records the fill faithfully, trips the kill switch, and raises so
    a human sees it immediately rather than finding it in a reconciliation later.
    """


@final
class ApprovedOrder:
    """An order that has passed the gate. Constructible only by ``RiskGate``."""

    __slots__ = ("_order", "_max_loss", "_approved_at", "_sequence")

    def __init__(
        self,
        token: object,
        order: Order,
        max_loss: Decimal,
        approved_at: datetime,
        sequence: int,
    ) -> None:
        if token is not _APPROVAL_TOKEN:
            raise PermissionError(
                "ApprovedOrder may only be constructed by RiskGate.submit(); "
                "an order that has not passed the risk gate must never reach a broker"
            )
        object.__setattr__(self, "_order", order)
        object.__setattr__(self, "_max_loss", max_loss)
        object.__setattr__(self, "_approved_at", approved_at)
        object.__setattr__(self, "_sequence", sequence)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ApprovedOrder is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ApprovedOrder is immutable")

    @property
    def order(self) -> Order:
        return self._order

    @property
    def max_loss(self) -> Decimal:
        """Cash reserved for this order at approval time."""
        return self._max_loss

    @property
    def approved_at(self) -> datetime:
        return self._approved_at

    @property
    def sequence(self) -> int:
        """Monotonic per-gate approval number, for the audit record."""
        return self._sequence

    @property
    def is_approved(self) -> bool:
        return True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ApprovedOrder(seq={self._sequence}, kind={self._order.kind!r}, "
            f"max_loss={self._max_loss})"
        )


#: What ``submit`` returns. Callers branch on ``.is_approved``.
GateDecision = Union[ApprovedOrder, Rejection]


class RiskGate:
    """Validates every order against the limits in ``config/risk_limits.yaml``."""

    def __init__(
        self,
        limits: RiskLimits,
        state: AccountState,
        clock: Optional[Callable[[], datetime]] = None,
        sectors: Optional[SectorMap] = None,
    ) -> None:
        self._limits = limits
        self._state = state
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sectors = sectors if sectors is not None else SectorMap.load()
        self._sequence = 0
        self._evaluate_kill_switch()

    # -- introspection ---------------------------------------------------------

    @property
    def state(self) -> AccountState:
        return self._state

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def buying_power(self) -> Decimal:
        return self._state.buying_power

    @property
    def nav(self) -> Decimal:
        return self._state.nav

    @property
    def kill_switch_tripped(self) -> bool:
        return self._state.kill_switch_tripped

    def sleeve_nav(self, sleeve: Sleeve) -> Decimal:
        """Capital notionally allotted to a sleeve: target weight x NAV.

        Caps in CLAUDE.md are quoted as percentages "of sleeve NAV". Sleeve NAV is
        taken as the sleeve's *target allocation* rather than its currently-deployed
        value, so an empty sleeve still has a well-defined cap. The separate 90/10
        drift check is what constrains actual deployment.
        """
        weight = (
            self._limits.portfolio.sleeves.equity
            if sleeve is Sleeve.EQUITY
            else self._limits.portfolio.sleeves.prediction
        )
        return self._state.nav * weight

    # -- the gate ---------------------------------------------------------------

    def submit(self, order: Order) -> GateDecision:
        """Validate an order. Returns an ``ApprovedOrder`` or a typed ``Rejection``.

        State is mutated only on approval, and only to reserve — see
        ``risk_gate.state`` for the reserve-then-settle model.
        """
        now = self._clock()
        today = now.date()
        state = self._state

        # A halt stops exposure growing; it does not trap the account in its
        # positions. Closes still run the full close-validation path below, so a
        # halted account can reduce risk but can never oversell into a short.
        if state.kill_switch_tripped and order.is_opening:
            return Rejection(
                code=RejectionCode.KILL_SWITCH_ACTIVE,
                message=(
                    "kill switch is tripped; opening orders are halted until a human "
                    "resets it (risk-reducing closes are still accepted)"
                ),
                limit=self._limits.kill_switch.drawdown_from_high_water_mark,
                observed=state.drawdown(),
            )

        state.roll_deployment_window(today)

        if order.is_opening:
            return self._submit_opening(order, now, today)
        return self._submit_closing(order, now, today)

    # -- closing orders ---------------------------------------------------------

    def _submit_closing(self, order: Order, now: datetime, today: date) -> GateDecision:
        """Close-only orders. The synthetic-short gap lives here.

        The schema cannot see positions, so it cannot tell a legitimate exit from a
        close of units the account never held. This is the check that closes it.
        """
        state = self._state
        key = position_key(order)
        units = units_of(order)
        position = state.position(key)

        if position is None or position.quantity <= 0:
            return Rejection(
                code=RejectionCode.POSITION_NOT_HELD,
                message=f"no held position for {key} to close",
                limit=ZERO,
                observed=units,
            )

        if units > position.available_to_close:
            return Rejection(
                code=RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY,
                message=(
                    f"close of {units} units exceeds {position.available_to_close} "
                    f"available for {key}; approving it would create a net short"
                ),
                limit=position.available_to_close,
                observed=units,
            )

        completes_day_trade = position.last_open_date == today
        if completes_day_trade:
            pdt_rejection = self._check_pdt(today)
            if pdt_rejection is not None:
                return pdt_rejection

        position.reserved_close += units
        if completes_day_trade:
            state.day_trades.append(today)
        return self._approve(order, ZERO, now)

    def _check_pdt(self, today: date) -> Optional[Rejection]:
        """FINRA day-trade counting, applicable only to sub-threshold margin accounts.

        A cash account cannot be a pattern day trader, which is part of why the config
        prefers one — it also structurally prevents a negative balance.
        """
        pdt = self._limits.pdt
        state = self._state
        if not pdt.enforce_day_trade_count_in_margin_account:
            return None
        if state.account_type is not AccountType.MARGIN:
            return None
        if state.nav >= pdt.equity_threshold_usd:
            return None

        used = state.day_trades_in_window(today, pdt.window_business_days)
        if used >= pdt.max_day_trades_per_window:
            return Rejection(
                code=RejectionCode.PDT_LIMIT_REACHED,
                message=(
                    f"{used} day trades in the last {pdt.window_business_days} "
                    f"business days; account equity is below "
                    f"${pdt.equity_threshold_usd}"
                ),
                limit=Decimal(pdt.max_day_trades_per_window),
                observed=Decimal(used),
            )
        return None

    # -- opening orders ---------------------------------------------------------

    def _submit_opening(self, order: Order, now: datetime, today: date) -> GateDecision:
        state = self._state
        limits = self._limits
        cost = order.max_loss()
        sleeve = sleeve_of(order)
        key = position_key(order)

        # Constraint #1: cash-secured. Nothing is spendable twice.
        if cost > state.buying_power:
            return Rejection(
                code=RejectionCode.INSUFFICIENT_BUYING_POWER,
                message=(
                    f"order reserves {cost} against {state.buying_power} available; "
                    "the account is cash-secured and may never go negative"
                ),
                limit=state.buying_power,
                observed=cost,
            )

        # Dust floor (fractional shares, 2026-08-20). Opening equity orders only:
        # closes are risk-reducing and are never trapped by a floor, options carry
        # whole contracts, and the prediction sleeve's arb strategy is micro-unit
        # by design. "Below the floor" is the explicit rule, so exactly at the
        # floor passes.
        if sleeve is Sleeve.EQUITY and not is_option(order):
            floor = limits.equity_sleeve.min_order_notional_usd
            if cost < floor:
                return Rejection(
                    code=RejectionCode.BELOW_MIN_NOTIONAL,
                    message=(
                        f"opening order for {key} reserves {cost}, below the "
                        f"{floor} minimum notional — dust, not a position"
                    ),
                    limit=floor,
                    observed=cost,
                )

        sleeve_nav = self.sleeve_nav(sleeve)

        # Max single position.
        position = state.position(key)
        current_exposure = position.exposure if position is not None else ZERO
        resulting = current_exposure + cost
        single_cap_fraction = (
            limits.equity_sleeve.max_single_position
            if sleeve is Sleeve.EQUITY
            else self._prediction_cap_fraction(order)
        )
        single_cap = sleeve_nav * single_cap_fraction
        if resulting > single_cap:
            return Rejection(
                code=RejectionCode.MAX_SINGLE_POSITION_EXCEEDED,
                message=(
                    f"position in {key} would reach {resulting}, above the "
                    f"{single_cap_fraction} cap on {sleeve} sleeve NAV"
                ),
                limit=single_cap,
                observed=resulting,
            )

        # Sector concentration. Equity positions only: options carry their own
        # aggregate-premium cap, and mapping option symbols back to underlyings
        # would smuggle parsing into the gate. Membership is the static table in
        # config/sectors.yaml; an unmapped name is its own singleton sector, so
        # this check can only ever be TIGHTER for unknown tickers, never looser.
        if sleeve is Sleeve.EQUITY and not is_option(order):
            sector = self._sectors.sector_of(key[1])
            sector_exposure = cost + sum(
                (
                    p.exposure
                    for p in state.positions.values()
                    if p.sleeve is Sleeve.EQUITY
                    and not p.is_option
                    and self._sectors.sector_of(p.key[1]) == sector
                ),
                ZERO,
            )
            sector_cap = sleeve_nav * limits.equity_sleeve.max_sector_exposure
            if sector_exposure > sector_cap:
                return Rejection(
                    code=RejectionCode.SECTOR_CONCENTRATION,
                    message=(
                        f"equity exposure in sector {sector!r} would reach "
                        f"{sector_exposure}, above the "
                        f"{limits.equity_sleeve.max_sector_exposure} cap on "
                        f"equity sleeve NAV (membership: config/sectors.yaml)"
                    ),
                    limit=sector_cap,
                    observed=sector_exposure,
                )

        # Daily deployment — an equity-sleeve cap in CLAUDE.md § Position Caps.
        if sleeve is Sleeve.EQUITY:
            deployed = state.deployed_today + cost
            daily_cap = sleeve_nav * limits.equity_sleeve.max_daily_deployment
            if deployed > daily_cap:
                return Rejection(
                    code=RejectionCode.MAX_DAILY_DEPLOYMENT_EXCEEDED,
                    message=(
                        f"deploying {cost} would put today's total at {deployed}, "
                        f"above the daily cap"
                    ),
                    limit=daily_cap,
                    observed=deployed,
                )

        # Aggregate long-option premium at risk.
        if is_option(order):
            premium = state.options_premium_at_risk + cost
            premium_cap = (
                self.sleeve_nav(Sleeve.EQUITY)
                * limits.equity_sleeve.max_options_premium_at_risk
            )
            if premium > premium_cap:
                return Rejection(
                    code=RejectionCode.MAX_OPTIONS_PREMIUM_EXCEEDED,
                    message=(
                        f"aggregate option premium at risk would reach {premium}, "
                        f"above the cap on the equity sleeve"
                    ),
                    limit=premium_cap,
                    observed=premium,
                )

        # 90/10 split, upper side only. Being under-weight is a rebalance concern,
        # not a risk breach, so it does not block an order.
        nav = state.nav
        if nav > ZERO:
            target = (
                limits.portfolio.sleeves.equity
                if sleeve is Sleeve.EQUITY
                else limits.portfolio.sleeves.prediction
            )
            ceiling_fraction = target + limits.portfolio.drift_tolerance
            resulting_fraction = (state.sleeve_exposure(sleeve) + cost) / nav
            if resulting_fraction > ceiling_fraction:
                return Rejection(
                    code=RejectionCode.SLEEVE_ALLOCATION_EXCEEDED,
                    message=(
                        f"{sleeve} sleeve would reach {resulting_fraction} of NAV, "
                        f"above target {target} plus drift "
                        f"{limits.portfolio.drift_tolerance}"
                    ),
                    limit=ceiling_fraction,
                    observed=resulting_fraction,
                )

        # Approved: reserve cash and exposure.
        target_position = state.ensure_position(
            key, sleeve, unit_multiplier(order), is_option(order)
        )
        state.reserved_cash += cost
        target_position.pending_open_units += units_of(order)
        target_position.pending_open_cost += cost
        target_position.last_open_date = today
        if sleeve is Sleeve.EQUITY:
            state.deployed_today += cost
        return self._approve(order, cost, now)

    def _prediction_cap_fraction(self, order: Order) -> Decimal:
        """Per-position cap for the prediction sleeve, by strategy.

        Arbitrage is micro-unit and high-turnover (0.5%); directional divergence
        positions are larger (2%). The order carries its own strategy tag, so an arb
        order can never be sized like a directional one.
        """
        prediction = self._limits.prediction_sleeve
        if getattr(order, "strategy", None) == "arb":
            return prediction.arbitrage.max_position
        return prediction.directional.max_position

    def _approve(self, order: Order, reserved: Decimal, now: datetime) -> ApprovedOrder:
        self._sequence += 1
        return ApprovedOrder(_APPROVAL_TOKEN, order, reserved, now, self._sequence)

    # -- settlement --------------------------------------------------------------

    def record_fill(
        self,
        approved: ApprovedOrder,
        fill_price: Decimal,
        filled_units: Optional[Decimal] = None,
    ) -> None:
        """Settle an approved order at its actual fill price.

        Buys release the reservation and take the real cash; sells release the held
        units and credit proceeds. Approving reserved the worst case, so a fill at a
        better price returns the difference to buying power.

        ``filled_units`` handles the partial fill: an order that filled 30 of 100 shares
        and was then cancelled or expired. Settlement is written for a *terminal* order,
        so the whole reservation is released either way — nothing more is going to
        happen to the remaining 70 — while only the units that actually filled become a
        position. Because the released cash is the full worst case and the cash taken is
        the filled portion, a partial fill can only move buying power in the safe
        direction.

        Omitting it settles the full quantity, which is the ordinary case and the
        previous behaviour.

        The day's deployment budget is deliberately *not* credited back for the unfilled
        remainder. It was charged the worst case at approval; leaving it charged means
        the daily cap counts capital the account committed rather than capital that
        happened to print, which is the tighter reading (Constraint #6).
        """
        order = approved.order
        key = position_key(order)
        position = self._state.position(key)
        if position is None:
            raise KeyError(f"no position to settle for {key}")

        ordered = units_of(order)
        filled = ordered if filled_units is None else filled_units
        if not 0 <= filled <= ordered:
            raise ValueError(
                f"filled_units must be between 0 and the {ordered} units ordered, "
                f"got {filled}"
            )
        multiplier = unit_multiplier(order)
        value = fill_price * filled * multiplier

        if order.is_opening:
            self._state.reserved_cash -= approved.max_loss
            self._state.cash -= value
            position.pending_open_units -= ordered
            position.pending_open_cost -= approved.max_loss
            position.quantity += filled
            position.cost_basis += value
            position.market_value += value
        else:
            share = (
                position.cost_basis * Decimal(filled) / Decimal(position.quantity)
                if position.quantity
                else ZERO
            )
            mark_share = (
                position.market_value * Decimal(filled) / Decimal(position.quantity)
                if position.quantity
                else ZERO
            )
            position.reserved_close -= ordered
            position.quantity -= filled
            position.cost_basis -= share
            position.market_value -= mark_share
            self._state.cash += value

        self._state.drop_if_empty(key)
        self._state.refresh_high_water_mark()
        self._evaluate_kill_switch()

        if self._state.buying_power < ZERO:
            self._state.kill_switch_tripped = True
            raise BuyingPowerBreached(
                f"fill at {fill_price} left buying power at "
                f"{self._state.buying_power}; the fill exceeded the reserved worst "
                f"case of {approved.max_loss}. Trading halted."
            )

    def cancel(self, approved: ApprovedOrder) -> None:
        """Release the reservations an approval took, without filling.

        Does not un-count a day trade: whether a cancelled close would have completed
        one is not recoverable here, and over-counting day trades is the safer error.
        """
        order = approved.order
        key = position_key(order)
        position = self._state.position(key)
        if position is None:
            return
        if order.is_opening:
            self._state.reserved_cash -= approved.max_loss
            position.pending_open_units -= units_of(order)
            position.pending_open_cost -= approved.max_loss
            # Only refund today's deployment budget if the approval was today. The
            # counter resets at the day boundary, so crediting a stale approval back
            # would hand tomorrow extra headroom.
            same_day = self._state.deployment_date == approved.approved_at.date()
            if sleeve_of(order) is Sleeve.EQUITY and same_day:
                self._state.deployed_today = max(
                    ZERO, self._state.deployed_today - approved.max_loss
                )
        else:
            position.reserved_close -= units_of(order)
        self._state.drop_if_empty(key)

    def mark_to_market(self, marks: Mapping[PositionKey, Decimal]) -> None:
        """Apply per-unit prices, then re-check drawdown."""
        self._state.apply_marks(marks)
        self._state.refresh_high_water_mark()
        self._evaluate_kill_switch()

    # -- kill switch ---------------------------------------------------------------

    def _evaluate_kill_switch(self) -> None:
        """Trip on breach. Never untrips itself — only ``reset_kill_switch`` clears it.

        Sticky by design: a drawdown that recovers on the next mark must not quietly
        resume trading, because the halt is there to force a human to look.
        """
        if self._state.kill_switch_tripped:
            return
        threshold = self._limits.kill_switch.drawdown_from_high_water_mark
        if self._state.drawdown() >= threshold:
            self._state.kill_switch_tripped = True

    def reset_kill_switch(self, operator_acknowledgement: str) -> None:
        """Clear the halt. FOR A HUMAN OPERATOR ONLY.

        CLAUDE.md § Portfolio Structure: "Resume requires manual human reset." The
        agent must never call this, and must never write code that calls it on the
        agent's behalf. The acknowledgement string is recorded in the audit trail so a
        reset always has a name attached.
        """
        if not operator_acknowledgement.strip():
            raise ValueError(
                "a kill-switch reset requires a non-empty operator acknowledgement"
            )
        self._state.kill_switch_tripped = False
        # Re-baseline the high-water mark to current NAV. Without this the reset is
        # inert: NAV is still 12% below the old mark, so the next mark-to-market would
        # immediately re-trip and no human reset could ever resume trading. The cost is
        # that the drawdown clock restarts from the post-loss level, which is the
        # decision the human is making by resetting at all.
        self._state.high_water_mark = self._state.nav
