"""Fixed-risk sizing and broker-aware protective prices; no position scaling."""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import math
from numbers import Real

from .models import Side, SymbolSpec, Tick


def _decimal(value: float) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError("Expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Expected a finite number") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite number")
    return result


def _precision(values: tuple[Decimal, ...]) -> int:
    # Keep subtraction/products exact even for disparate float magnitudes.
    return max(64, 2 * sum(
        len(value.as_tuple().digits) + abs(value.as_tuple().exponent)
        for value in values
    ) + 16)


def size_volume(
    *, equity: float, risk_fraction: float, remaining_budget: float,
    loss_per_lot: float, volume_min: float, volume_max: float, volume_step: float,
) -> float:
    """Floor fixed-risk volume on the grid volume_min + n * volume_step.

    Equity, remaining budget and loss per lot must use the same currency
    units (including for cent accounts). Risk fraction must be in (0, 1].
    Invalid inputs or an unaffordable minimum return zero, never a rounded-up
    minimum. Both the risk budget and broker maximum are hard caps.
    """
    try:
        values = tuple(_decimal(value) for value in (
            equity, risk_fraction, remaining_budget, loss_per_lot,
            volume_min, volume_max, volume_step,
        ))
    except ValueError:
        return 0.0
    equity_d, fraction, remaining, loss, minimum, maximum, step = values
    if any(value <= 0 for value in values) or fraction > 1 or maximum < minimum:
        return 0.0

    with localcontext() as context:
        context.prec = _precision(values)
        budget = min(equity_d * fraction, remaining)
        if minimum * loss > budget:
            return 0.0
        risk_steps = ((budget - minimum * loss) / (step * loss)).to_integral_value(
            rounding=ROUND_FLOOR
        )
        max_steps = ((maximum - minimum) / step).to_integral_value(rounding=ROUND_FLOOR)
        volume = minimum + min(risk_steps, max_steps) * step
        # A division at the precision boundary must not round a cap upward.
        if volume * loss > budget or volume > maximum:
            volume -= step
        result = float(volume)
        if not math.isfinite(result) or result <= 0:
            return 0.0
        represented = Decimal(str(result))
        if represented * loss > budget or represented > maximum:
            # Conversion to float can itself round upward. Re-floor the grid
            # below that representable float instead of returning an over-cap lot.
            ceiling = Decimal(str(math.nextafter(result, 0.0)))
            steps = ((ceiling - minimum) / step).to_integral_value(rounding=ROUND_FLOOR)
            if steps < 0:
                return 0.0
            result = float(minimum + steps * step)
            represented = Decimal(str(result))
        if represented < minimum or represented > maximum or represented * loss > budget:
            return 0.0
        return result


def price_levels(
    side: Side, tick: Tick, spec: SymbolSpec, atr: float,
    stop_atr: float = 1.5, reward_ratio: float = 1.5,
) -> tuple[float, float, float]:
    """Return market entry and outward tick-rounded (SL, TP).

    Stop distance is max(ATR * stop_atr, stops_level * point + spread +
    tick_size). Reward distance uses the actual rounded entry-to-SL risk.
    TP also respects the broker's minimum distance if reward_ratio is small.
    Entry is the unrounded executable quote, not a synthetic tick-grid price.
    """
    if side not in ("buy", "sell"):
        raise ValueError("Side must be buy or sell")
    values = tuple(_decimal(value) for value in (
        tick.bid, tick.ask, spec.point, spec.tick_size, atr, stop_atr, reward_ratio,
    ))
    bid, ask, point, grid, atr_d, multiple, reward = values
    if any(value <= 0 for value in values) or ask < bid:
        raise ValueError("Prices and distances must be positive; ask must be >= bid")
    if (
        not isinstance(spec.stops_level, int) or isinstance(spec.stops_level, bool)
        or spec.stops_level < 0
        or not isinstance(spec.digits, int) or isinstance(spec.digits, bool)
        or spec.digits < 0
        or not isinstance(tick.time, int) or isinstance(tick.time, bool) or tick.time < 0
    ):
        raise ValueError("Invalid broker metadata or quote timestamp")

    with localcontext() as context:
        context.prec = _precision(values + (Decimal(spec.stops_level),))
        entry = ask if side == "buy" else bid
        broker_distance = Decimal(spec.stops_level) * point + ask - bid + grid
        distance = max(atr_d * multiple, broker_distance)
        if side == "buy":
            sl = ((entry - distance) / grid).to_integral_value(rounding=ROUND_FLOOR) * grid
            target_distance = max((entry - sl) * reward, broker_distance)
            tp = ((entry + target_distance) / grid).to_integral_value(rounding=ROUND_CEILING) * grid
        else:
            sl = ((entry + distance) / grid).to_integral_value(rounding=ROUND_CEILING) * grid
            target_distance = max((sl - entry) * reward, broker_distance)
            tp = ((entry - target_distance) / grid).to_integral_value(rounding=ROUND_FLOOR) * grid
        result = tuple(float(value) for value in (entry, sl, tp))
        if not all(math.isfinite(value) and value > 0 for value in result):
            raise ValueError("Protective prices must be positive and representable")
        if not (result[1] < result[0] < result[2] if side == "buy"
                else result[2] < result[0] < result[1]):
            raise ValueError("Protective distances are not representable")
        return result

