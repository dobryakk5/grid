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


def configured_grid_cells(profile) -> list[tuple[Decimal, Decimal]]:
    """Return the main grid plus optional arithmetic accumulation cells below LOW."""
    lower = Decimal(profile.lower_price)
    upper = Decimal(profile.upper_price)
    step = Decimal(profile.step_price)
    main = strategy_grid_cells(
        lower, upper, step,
        mode=getattr(profile, "grid_mode", "arithmetic"),
        step_percent=(
            Decimal(profile.step_percent)
            if getattr(profile, "step_percent", None) is not None else None
        ),
    )
    extension = getattr(profile, "below_grid_lower_price", None)
    if not getattr(profile, "buy_below_grid", True) or extension is None:
        return main
    extension = Decimal(extension)
    if extension >= lower:
        raise ValueError("below_grid_lower_price must be below lower_price")
    return strategy_grid_cells(extension, lower, step, mode="arithmetic") + main


def ladder_allocations(
    total: Decimal, count: int, *, mode: str, multiplier: Decimal = Decimal("1.5")
) -> list[Decimal]:
    """Split a total into increasingly large ladder portions."""
    if count <= 0:
        return []
    if total <= 0:
        raise ValueError("ladder total must be positive")
    if mode == "linear":
        weights = [Decimal(i) for i in range(1, count + 1)]
    elif mode == "geometric":
        if multiplier <= 1:
            raise ValueError("geometric ladder multiplier must be greater than 1")
        weights = [multiplier ** i for i in range(count)]
    else:
        raise ValueError("ladder mode must be linear or geometric")
    weight_sum = sum(weights, Decimal("0"))
    result = [total * weight / weight_sum for weight in weights]
    # Preserve the exact total despite Decimal division tails.
    result[-1] += total - sum(result, Decimal("0"))
    return result


def dca_initial_percent(
    market_price: Decimal, lower: Decimal, upper: Decimal, above_mid_percent: Decimal
) -> Decimal:
    """Use a cautious share above the midpoint and its complement below it."""
    if not Decimal("0") < above_mid_percent < Decimal("50"):
        raise ValueError("initial_buy_percent must be between 0 and 50")
    midpoint = (lower + upper) / Decimal("2")
    return above_mid_percent if market_price > midpoint else Decimal("100") - above_mid_percent
