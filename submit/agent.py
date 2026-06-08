"""Cash-first deterministic agent for the EAGE Energy Arena.

The supplied reference agent tries to exercise nearly every mechanic. This
agent is deliberately narrower: establish a compact profitable city, preserve
an emergency reserve, grow only when housing/jobs require it, and avoid
speculative oil infrastructure.
"""

from __future__ import annotations

from typing import Any

from agents.api_client import ApiClient
from agents.base import BaseAgent

EMERGENCY_RESERVE = 150_000.0
GROWTH_RESERVE = 190_000.0
RENEWABLE_RESERVE = 220_000.0
MAX_SOLAR = 4
MAX_PARKS = 3
MAX_DEMAND_RESPONSE_HUBS = 1
HOUSING_HEADROOM = 8
JOBS_HEADROOM = 12


class ConservativeAgent(BaseAgent):
    """A compact-city policy that treats solvency as a hard constraint."""

    def __init__(self, api: ApiClient, *, seed: int | None = None) -> None:
        super().__init__(api, seed=seed)
        self._bootstrapped = False
        self._emergency_coal_id: str | None = None
        self._backup_is_permanent = False

    def next_step_days(self, state: dict[str, Any]) -> int:
        return 1 if state.get("active_events") else 7

    def act(self, state: dict[str, Any]) -> None:
        if not self._bootstrapped:
            self._bootstrap(state)
            self._bootstrapped = True
            return

        failure_active = any(
            event.get("type") == "plant_failure" for event in state.get("active_events") or []
        )
        operational_coal = [
            tile for tile in state["tiles"] if tile["type"] == "coal_plant" and tile["operational"]
        ]
        if failure_active and not operational_coal:
            self._build_emergency_coal(state)
            return
        if (
            not failure_active
            and self._emergency_coal_id is not None
            and not self._backup_is_permanent
        ):
            self._remove_emergency_coal(state)
            return
        had_grid_stress = any(
            balance in ("brownout", "blackout")
            for balance in state.get("last_day_balance_state_by_hour") or []
        )
        if had_grid_stress and int(state["population"]) >= 200 and self._emergency_coal_id is None:
            self._build_emergency_coal(state, permanent=True)
            return

        treasury = float(state["treasury"])
        if treasury < EMERGENCY_RESERVE or self._daily_net(state) < -500.0:
            return

        tiles = state["tiles"]
        population = int(state["population"])
        capacity = int(state["housing_capacity"])
        jobs = int(state["jobs_total"])
        n_solar = _count(tiles, "solar_farm")
        n_parks = _count(tiles, "park")
        n_demand_response_hubs = _count(tiles, "demand_response_hub")
        occupied = {(int(t["x"]), int(t["y"])) for t in tiles}
        occupied.update((int(w["x"]), int(w["y"])) for w in state["wells"])
        cx = int(state["config"]["world_w"]) // 2
        cy = int(state["config"]["world_h"]) // 2
        width = int(state["config"]["world_w"])
        height = int(state["config"]["world_h"])

        # Grow the renewable share without spending the emergency reserve.
        if (
            n_solar < MAX_SOLAR
            and treasury >= RENEWABLE_RESERVE
            and self._build_plant("solar_farm", cx, cy, width, height, occupied)
        ):
            return

        # Once the city is mature, coordinate flexible load instead of
        # immediately adding more fossil backup for ordinary peak demand.
        if (
            n_demand_response_hubs < MAX_DEMAND_RESPONSE_HUBS
            and population >= 180
            and treasury >= RENEWABLE_RESERVE
            and self._build_plant("demand_response_hub", cx, cy, width, height, occupied)
        ):
            return

        # Population can grow only when both structural constraints have room.
        if (
            capacity <= population + HOUSING_HEADROOM
            and treasury >= GROWTH_RESERVE
            and self._build_civilian("house", cx, cy, width, height, occupied, tiles)
        ):
            return

        if (
            jobs <= population + JOBS_HEADROOM
            and treasury >= GROWTH_RESERVE
            and self._build_civilian("commercial", cx, cy, width, height, occupied, tiles)
        ):
            return

        # A nearby park is cheap insurance against happiness and population loss.
        if (
            n_parks < MAX_PARKS
            and treasury >= GROWTH_RESERVE
            and self._build_park(cx, cy, width, height, occupied)
        ):
            return

        # Extend the road only when growth is blocked by a lack of build sites.
        if treasury >= GROWTH_RESERVE and (
            capacity <= population + HOUSING_HEADROOM or jobs <= population + JOBS_HEADROOM
        ):
            self._extend_road(cx, cy, width, height, occupied, tiles)

    def _bootstrap(self, state: dict[str, Any]) -> None:
        """Build the smallest city that can retain and slowly grow population."""
        cx = int(state["config"]["world_w"]) // 2
        cy = int(state["config"]["world_h"]) // 2
        plan = [
            ("park", cx + 1, cy - 1),
            ("park", cx + 1, cy + 1),
            ("commercial", cx, cy - 1),
            ("commercial", cx, cy + 1),
            ("commercial", cx - 1, cy - 1),
            ("commercial", cx - 1, cy + 1),
            ("house", cx - 2, cy - 1),
            ("solar_farm", cx + 6, cy - 2),
            ("solar_farm", cx + 6, cy + 2),
        ]
        for tile_type, x, y in plan:
            result = self.api.build(tile_type, x, y)
            if result.get("error") == "insufficient_funds":
                return

    def _build_civilian(
        self,
        tile_type: str,
        cx: int,
        cy: int,
        width: int,
        height: int,
        occupied: set[tuple[int, int]],
        tiles: list[dict[str, Any]],
    ) -> bool:
        road_tiles = {
            (int(t["x"]), int(t["y"])) for t in tiles if t["type"] in ("road", "town_hall")
        }
        for x, y in _spiral(cx, cy, width, height):
            if (x, y) in occupied:
                continue
            if not _adjacent_to_any(x, y, road_tiles):
                continue
            result = self.api.build(tile_type, x, y)
            if result.get("ok"):
                return True
            if result.get("error") == "insufficient_funds":
                return False
        return False

    def _build_plant(
        self,
        tile_type: str,
        cx: int,
        cy: int,
        width: int,
        height: int,
        occupied: set[tuple[int, int]],
    ) -> bool:
        for x, y in _perimeter_spiral(cx, cy, width, height):
            if (x, y) in occupied:
                continue
            result = self.api.build(tile_type, x, y)
            if result.get("ok"):
                return True
            if result.get("error") == "insufficient_funds":
                return False
        return False

    def _build_park(
        self,
        cx: int,
        cy: int,
        width: int,
        height: int,
        occupied: set[tuple[int, int]],
    ) -> bool:
        for x, y in _spiral(cx, cy, width, height):
            if (x, y) in occupied:
                continue
            result = self.api.build("park", x, y)
            if result.get("ok"):
                return True
            if result.get("error") == "insufficient_funds":
                return False
        return False

    def _extend_road(
        self,
        cx: int,
        cy: int,
        width: int,
        height: int,
        occupied: set[tuple[int, int]],
        tiles: list[dict[str, Any]],
    ) -> bool:
        road_tiles = {
            (int(t["x"]), int(t["y"])) for t in tiles if t["type"] in ("road", "town_hall")
        }
        for x, y in _spiral(cx, cy, width, height):
            if (x, y) in occupied or not _adjacent_to_any(x, y, road_tiles):
                continue
            result = self.api.build("road", x, y)
            if result.get("ok"):
                return True
            if result.get("error") == "insufficient_funds":
                return False

        # A mature compact city can surround every road edge. Convert the
        # easternmost road-adjacent house into a connected growth corridor.
        houses = sorted(
            (
                t
                for t in tiles
                if t["type"] == "house" and _adjacent_to_any(int(t["x"]), int(t["y"]), road_tiles)
            ),
            key=lambda t: (int(t["x"]), int(t["y"])),
            reverse=True,
        )
        for house in houses:
            x, y = int(house["x"]), int(house["y"])
            demolished = self.api.demolish(x, y)
            if not demolished.get("ok"):
                continue
            return bool(self.api.build("road", x, y).get("ok"))
        return False

    def _build_emergency_coal(self, state: dict[str, Any], *, permanent: bool = False) -> None:
        if self._emergency_coal_id is not None or float(state["treasury"]) < 205_000.0:
            return

        cx = int(state["config"]["world_w"]) // 2
        cy = int(state["config"]["world_h"]) // 2
        plant_x = cx + 8

        # Create a connected eastern service road for the temporary plant.
        for x in range(cx + 1, plant_x):
            live = self.api.state()
            occupant = next(
                (tile for tile in live["tiles"] if int(tile["x"]) == x and int(tile["y"]) == cy),
                None,
            )
            if occupant is not None and occupant["type"] != "road":
                self.api.demolish(x, cy)
            live = self.api.state()
            if not any(
                tile["type"] == "road" and int(tile["x"]) == x and int(tile["y"]) == cy
                for tile in live["tiles"]
            ):
                self.api.build("road", x, cy)

        # Fossil plants require a one-cell clear safety halo. Roads are
        # permitted inside it, but other facilities must be removed.
        for x in range(plant_x - 1, plant_x + 2):
            for y in range(cy - 1, cy + 2):
                if x == plant_x - 1 and y == cy:
                    continue
                live = self.api.state()
                occupant = next(
                    (tile for tile in live["tiles"] if int(tile["x"]) == x and int(tile["y"]) == y),
                    None,
                )
                if occupant is not None and (
                    (x == plant_x and y == cy) or occupant["type"] not in ("road", "battery")
                ):
                    self.api.demolish(x, y)

        # A coal plant needs 30 workers. Temporarily remove the newest
        # commercial facilities until enough people are available to staff it.
        live = self.api.state()
        while int(live["unemployed"]) < 30:
            staffed_commercial = sorted(
                (
                    tile
                    for tile in live["tiles"]
                    if tile["type"] == "commercial" and int(tile["staffed_jobs"]) > 0
                ),
                key=lambda tile: (int(tile["built_day"]), str(tile["id"])),
                reverse=True,
            )
            if not staffed_commercial:
                return
            tile = staffed_commercial[0]
            self.api.demolish(int(tile["x"]), int(tile["y"]))
            live = self.api.state()

        result = self.api.build("coal_plant", plant_x, cy)
        if result.get("ok"):
            self._emergency_coal_id = str(result["result"]["id"])
            self._backup_is_permanent = permanent

    def _remove_emergency_coal(self, state: dict[str, Any]) -> None:
        tile = next(
            (tile for tile in state["tiles"] if tile["id"] == self._emergency_coal_id),
            None,
        )
        if tile is not None:
            self.api.demolish(int(tile["x"]), int(tile["y"]))
        self._emergency_coal_id = None
        self._backup_is_permanent = False

    @staticmethod
    def _daily_net(state: dict[str, Any]) -> float:
        today = state.get("today") or {}
        revenue = sum(
            float(today.get(key, 0.0))
            for key in (
                "tax_revenue",
                "power_revenue",
                "oil_revenue",
                "industrial_revenue",
                "commercial_revenue",
            )
        )
        costs = sum(
            float(today.get(key, 0.0))
            for key in ("opex", "fuel_cost", "carbon_cost", "outage_penalty")
        )
        return revenue - costs


def _count(tiles: list[dict[str, Any]], tile_type: str) -> int:
    return sum(1 for tile in tiles if tile["type"] == tile_type)


def _adjacent_to_any(x: int, y: int, points: set[tuple[int, int]]) -> bool:
    return any((x + dx, y + dy) in points for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))


def _in_bounds(x: int, y: int, width: int, height: int) -> bool:
    return 0 <= x < width and 0 <= y < height


def _spiral(cx: int, cy: int, width: int, height: int) -> list[tuple[int, int]]:
    points = [(cx, cy)]
    for radius in range(1, max(width, height)):
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue
                x, y = cx + dx, cy + dy
                if _in_bounds(x, y, width, height):
                    points.append((x, y))
    return points


def _perimeter_spiral(cx: int, cy: int, width: int, height: int) -> list[tuple[int, int]]:
    return [
        point
        for point in _spiral(cx, cy, width, height)
        if max(abs(point[0] - cx), abs(point[1] - cy)) >= 4
    ]


Agent = ConservativeAgent
