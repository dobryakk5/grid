from decimal import Decimal, ROUND_DOWN


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def buy_level(anchor: Decimal, step_pct: Decimal, level: int) -> Decimal:
    if level < 1:
        raise ValueError("level must be >= 1")
    return anchor * (Decimal("1") - step_pct * level)


def one_step_up(price: Decimal, step_pct: Decimal) -> Decimal:
    return price * (Decimal("1") + step_pct)


def one_step_down(price: Decimal, step_pct: Decimal) -> Decimal:
    return price / (Decimal("1") + step_pct)
