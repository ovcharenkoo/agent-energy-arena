"""Regression tests for the demand-response hub world component."""

from __future__ import annotations

import pytest

from world.catalog import TILE_CATALOG, build_catalog
from world.demand_response import DEMAND_RESPONSE_REDUCTION_PER_HUB
from world.power import total_demand_kw
from world.sim import World
from world.state import Tile


def _world_with_hub() -> tuple[World, Tile]:
    world = World()
    world.reset(seed=42)
    result = world.build("demand_response_hub", 1, 1)
    assert result["ok"] is True
    hub = next(tile for tile in world.state.tiles if tile.type == "demand_response_hub")
    return world, hub


def test_demand_response_hub_is_exposed_in_catalog() -> None:
    spec = TILE_CATALOG["demand_response_hub"]
    assert spec.requires_road is False
    assert spec.jobs == 0
    assert any(tile["tile_type"] == "demand_response_hub" for tile in build_catalog()["tiles"])


def test_operational_hub_reduces_peak_civilian_demand() -> None:
    world, hub = _world_with_hub()
    with_hub = total_demand_kw(world.state, 12)
    hub.operational = False
    without_hub = total_demand_kw(world.state, 12)

    assert with_hub == pytest.approx(without_hub * (1.0 - DEMAND_RESPONSE_REDUCTION_PER_HUB))


def test_hub_does_not_reduce_off_peak_demand() -> None:
    world, hub = _world_with_hub()
    with_hub = total_demand_kw(world.state, 2)
    hub.operational = False

    assert with_hub == pytest.approx(total_demand_kw(world.state, 2))


def test_two_hubs_stack_their_effect() -> None:
    world, hub = _world_with_hub()
    one_hub = total_demand_kw(world.state, 12)
    result = world.build("demand_response_hub", 2, 1)
    assert result["ok"] is True

    assert total_demand_kw(world.state, 12) == pytest.approx(
        one_hub
        * (1.0 - 2.0 * DEMAND_RESPONSE_REDUCTION_PER_HUB)
        / (1.0 - DEMAND_RESPONSE_REDUCTION_PER_HUB)
    )
