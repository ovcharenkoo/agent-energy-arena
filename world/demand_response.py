"""Demand-response world component.

An automated demand-response hub coordinates flexible civilian loads during
the city's high-demand daytime and evening window. Each operational hub
reduces civilian demand by 15%, capped at 30% across the city.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from world.state import WorldState

DEMAND_RESPONSE_TILE_TYPE = "demand_response_hub"
DEMAND_RESPONSE_PEAK_HOURS = frozenset(range(9, 20))
DEMAND_RESPONSE_REDUCTION_PER_HUB = 0.15
DEMAND_RESPONSE_MAX_REDUCTION = 0.30


def demand_response_reduction(state: WorldState, hour: int) -> float:
    """Return the fraction of civilian demand shed this hour."""
    if hour not in DEMAND_RESPONSE_PEAK_HOURS:
        return 0.0
    reduction = sum(
        DEMAND_RESPONSE_REDUCTION_PER_HUB
        for tile in state.tiles
        if tile.type == DEMAND_RESPONSE_TILE_TYPE and tile.operational
    )
    return min(DEMAND_RESPONSE_MAX_REDUCTION, reduction)


def apply_demand_response(state: WorldState, hour: int, civilian_kw: float) -> float:
    """Apply the active hubs' peak-hour reduction to civilian demand."""
    return civilian_kw * (1.0 - demand_response_reduction(state, hour))
