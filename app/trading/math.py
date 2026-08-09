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
