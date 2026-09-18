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


def level_size_weights(count: int, multiplier: Decimal) -> list[Decimal]:
    """Martingale weights across a grid: 1 in the middle, growing to the edges.

    A per-fill martingale doubles after a loss. A grid has no losses to count,
    but it does have a middle: cells near the centre of the corridor trade the
    most often for the least edge, while the cells at the bottom and the top
    are the ones worth committing size to. So the weight is a function of the
    distance from the middle cell, not of what happened before -- the same
    shape, indexed by price instead of by streak.

    Symmetric on purpose: a cell's SELL sells what its own BUY bought, so a
    heavy cell at the top is exactly what "sell size into the top" means here.
    """
    if count <= 0:
        return []
    if multiplier <= 0:
        raise ValueError("level size multiplier must be positive")
    # An even number of cells has no single middle cell; the half-step
    # distance that falls out of this keeps the shape symmetric anyway.
    centre = Decimal(count - 1) / Decimal(2)
    return [multiplier ** abs(Decimal(index) - centre) for index in range(count)]


def grid_exposure(quote_per_level: Decimal, count: int, multiplier: Decimal) -> Decimal:
    """Quote committed when every cell of the grid is long at once.

    The number that matters for funding, and not the same as
    ``quote_per_level * count`` once a multiplier is in play: cells above the
    market only arm after price has risen through them, but nothing brings
    their money back before price falls again, so every cell can hold a lot
    at the same time. Sizing against the flat product is how a grid runs out
    of quote halfway down its own ladder.
    """
    return quote_per_level * sum(level_size_weights(count, multiplier), Decimal("0"))


def quote_per_level_for_budget(
    budget: Decimal, count: int, multiplier: Decimal
) -> Decimal:
    """Invert :func:`grid_exposure`: the middle cell's size a budget affords.

    ``budget / sum(weights)`` -- the whole ladder then fits the budget exactly,
    which is the only sizing rule that survives contact with a real wallet.
    """
    if budget <= 0:
        raise ValueError("budget must be positive")
    weights = sum(level_size_weights(count, multiplier), Decimal("0"))
    if weights <= 0:
        raise ValueError("grid has no levels to size")
    return budget / weights


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
