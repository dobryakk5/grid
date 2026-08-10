from decimal import Decimal, ROUND_DOWN


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def grid_buy_levels(lower: Decimal, upper: Decimal, step: Decimal) -> list[Decimal]:
    """Return BUY levels. The upper boundary is reserved as the final SELL level."""
    if lower <= 0 or upper <= 0:
        raise ValueError("grid prices must be positive")
    if upper <= lower:
        raise ValueError("upper_price must be greater than lower_price")
    if step <= 0:
        raise ValueError("step_price must be positive")

    levels: list[Decimal] = []
    price = lower
    while price + step <= upper:
        levels.append(price)
        price += step
    if not levels:
        raise ValueError("range must contain at least one complete grid step")
    return levels


def grid_lines(lower: Decimal, upper: Decimal, step: Decimal) -> list[Decimal]:
    buys = grid_buy_levels(lower, upper, step)
    return buys + [buys[-1] + step]


def strategy_grid_lines(
    lower: Decimal,
    upper: Decimal,
    step: Decimal,
    *,
    mode: str = "arithmetic",
    step_percent: Decimal | None = None,
) -> list[Decimal]:
    if mode == "arithmetic":
        return grid_lines(lower, upper, step)
    if mode != "geometric":
        raise ValueError("grid_mode must be arithmetic or geometric")
    if lower <= 0 or upper <= lower:
        raise ValueError("upper_price must be greater than lower_price")
    if step_percent is None or step_percent <= 0:
        raise ValueError("step_percent must be positive for geometric grid")

    factor = Decimal("1") + step_percent / Decimal("100")
    lines = [lower]
    while lines[-1] < upper:
        next_price = lines[-1] * factor
        if next_price >= upper:
            lines.append(upper)
            break
        lines.append(next_price)
        if len(lines) > 1000:
            raise ValueError("geometric grid has too many levels")
    return lines


def strategy_grid_cells(
    lower: Decimal,
    upper: Decimal,
    step: Decimal,
    *,
    mode: str = "arithmetic",
    step_percent: Decimal | None = None,
) -> list[tuple[Decimal, Decimal]]:
    lines = strategy_grid_lines(
        lower, upper, step, mode=mode, step_percent=step_percent
    )
    return list(zip(lines, lines[1:]))
