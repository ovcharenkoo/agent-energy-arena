"""Grand-finale scenario — a slow build to a stacked endgame.

Unlike `grand_collapse`, which spreads its pressures evenly across the
horizon, the grand finale is shaped like a crescendo. It deliberately
leaves the opening of the game quiet so the world has room to *develop* —
a fleet to build, a balance sheet to fatten — before any stress lands.
Then the pressure ramps, axis by axis, and converges into a brutal
final stretch.

Three time phases:

* **Development grace** (days 0..``DEVELOPMENT_GRACE_DAYS``): nothing
  fires. ``apply`` is a hard no-op. The agent gets an unmolested runway
  to stand up generation and accumulate cash.
* **Build-up** (grace..``CARBON_RATCHET_FIRST_DAY``): the *operational
  and market* axes start to bite — low-wind and solar-dark windows, a
  fuel-price shock, a heatwave or two, and the first short plant failures
  (deferred past ``FAILURE_FIRST_DAY``). CO2 is still cheap here; emitting
  to survive a wobble is a legitimate move.
* **Finale** (``CARBON_RATCHET_FIRST_DAY``..horizon): the carbon price
  steps up once, permanently, so every tonne emitted now costs more —
  emissions growth is *penalised* only once the agent has had 300 days to
  decarbonise. The failures densify, a second low-wind window and a deep
  solar-dark spell land, an oil-price collapse runs underneath, and the
  heaviest demand shocks hit. Everything the agent under-built earlier
  comes due at once.

The strategic point: an agent that treats the quiet opening as the whole
game — banking cash on a cheap fossil fleet — walks into a finale where
that fleet is both unreliable (failures) and ruinously taxed (carbon
ratchet). An agent that spends the grace period diversifying and
decarbonising pays the build-up's operational costs but sails through the
finale. CO2 is only priced after day 300 by design: the penalty rewards
*early* transition, not reflexes.

Everything is bounded inside the default 730-day submission horizon and
the scenario consumes no random numbers — given `(world, day)` the effect
is deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from world.scenario import Scenario, inject_display_marker

if TYPE_CHECKING:
    from world.sim import World
    from world.state import WorldState

# Mirrors `world.events.FOSSIL_PLANT_TYPES`; kept local so the scenario
# does not reach into simulation internals for two strings.
FOSSIL_PLANT_TYPES: frozenset[str] = frozenset({"coal_plant", "gas_peaker"})


class GrandFinale(Scenario):
    """A quiet opening, an escalating build-up, and a stacked endgame."""

    seed: int = 42

    GAME_HORIZON_DAYS: int = 730

    # Hard no-op floor. Nothing — no weather clip, no event, no price
    # move — fires before this day. The world develops untouched.
    DEVELOPMENT_GRACE_DAYS: int = 120

    # --- Finale: carbon ratchet (single permanent step) ---
    # CO2 is priced ONLY in the back half: one permanent carbon-price step
    # lands once the agent has had 300 days to decarbonise (25 -> 37.5 $/t).
    # Kept to a single step on purpose — a fossil-heavy fleet still feels it,
    # but the scenario does not climb the price a third time on top of any
    # stochastic `regulatory_tightening` the event sampler may already have
    # rolled (e.g. the day-250 roll on seed 42). The scenario cannot suppress
    # those random rolls without breaking the no-RNG determinism contract, so
    # it stays modest itself. Every step day is >= CARBON_RATCHET_FIRST_DAY
    # (asserted in the test).
    CARBON_RATCHET_FIRST_DAY: int = 300
    CARBON_STEPS: tuple[tuple[int, float], ...] = ((300, 1.5),)
    CARBON_MARKER_DURATION_DAYS: int = 60

    # --- Failures: recurring, densifying toward the finale ---
    # Deferred past FAILURE_FIRST_DAY so the agent has a long, failure-free
    # runway to stand up redundancy before anything breaks. Explicit days
    # rather than a fixed period so the cadence can tighten as the endgame
    # approaches (the gaps shrink from ~200d to ~20d). Each outage is short
    # (FAILURE_DURATION_DAYS) so a single failure is a wobble, not a wall.
    FAILURE_FIRST_DAY: int = 200
    FAILURE_DAYS: tuple[int, ...] = (360, 560, 660)
    FAILURE_DURATION_DAYS: int = 3

    # --- Renewable drought: a mild build-up window, a deep finale one ---
    LOW_WIND_WINDOWS: tuple[tuple[int, int, float], ...] = (
        (150, 185, 1.5),
        (540, 600, 0.5),
    )
    SOLAR_DARK_WINDOWS: tuple[tuple[int, int, float], ...] = (
        (150, 180, 0.20),
        (650, 710, 0.12),
    )

    # --- Market shocks: build-up fuel shock, finale fuel shock + oil collapse ---
    FUEL_SHOCK_WINDOWS: tuple[tuple[int, int, float, float], ...] = (
        (210, 270, 24.0, 60.0),
        (610, 690, 30.0, 80.0),
    )
    OIL_COLLAPSE_START_DAY: int = 330
    OIL_COLLAPSE_END_DAY: int = 560  # exclusive
    OIL_COLLAPSE_CRUDE_USD_PER_BBL: float = 15.0
    OIL_COLLAPSE_REFINED_USD_PER_BBL: float = 55.0

    # --- Demand shocks: light early, heaviest in the finale ---
    HEATWAVE_DAYS: tuple[int, ...] = (140, 320, 500, 660)
    HEATWAVE_DURATION_DAYS: int = 5
    HEATWAVE_SEVERITY: float = 1.4
    DEMAND_SURPRISE_DAYS: tuple[int, ...] = (230, 480, 680)
    DEMAND_SURPRISE_DURATION_DAYS: int = 10
    DEMAND_SURPRISE_SEVERITY: float = 1.3

    # Baselines restored on window-close days.
    _BASELINE_COAL_USD_PER_MWH: float = 12.0
    _BASELINE_GAS_USD_PER_MWH: float = 30.0
    _BASELINE_CRUDE_USD_PER_BBL: float = 40.0
    _BASELINE_REFINED_USD_PER_BBL: float = 90.0

    def apply(self, world: World, day: int) -> None:
        state = world.state
        # Development grace: the world is left to grow untouched. Every
        # scheduled lever below starts after this floor, so the guard is
        # the single source of truth for "nothing fires yet".
        if day < self.DEVELOPMENT_GRACE_DAYS:
            return
        self._apply_carbon_ratchet(state, day)
        self._inject_plant_failure(state, day)
        self._apply_low_wind(state, day)
        self._apply_solar_dark(state, day)
        self._apply_fuel_shock(state, day)
        self._apply_oil_collapse(state, day)
        self._inject_demand_events(state, day)

    def _apply_carbon_ratchet(self, state: WorldState, day: int) -> None:
        for step_day, mult in self.CARBON_STEPS:
            if day != step_day:
                continue
            already = any(
                e.get("type") == "regulatory_tightening" and e.get("started_day") == step_day
                for e in state.active_events
            ) or any(
                e.get("type") == "regulatory_tightening" and e.get("started_day") == step_day
                for e in state.historical_events
            )
            if already:
                continue
            carbon_before = state.carbon_price
            state.carbon_price *= mult
            state.active_events.append(
                {
                    "type": "regulatory_tightening",
                    "started_day": day,
                    "ends_day": day + self.CARBON_MARKER_DURATION_DAYS,
                    "severity": state.carbon_price,
                }
            )
            state.scenario_trace.append(
                {
                    "day": day,
                    "kind": "carbon_ratchet",
                    "multiplier": mult,
                    "carbon_price_before": carbon_before,
                    "carbon_price_after": state.carbon_price,
                }
            )

    def _inject_plant_failure(self, state: WorldState, day: int) -> None:
        if day not in self.FAILURE_DAYS:
            return
        candidates = sorted(
            (t for t in state.tiles if t.type in FOSSIL_PLANT_TYPES and t.operational),
            key=lambda t: t.id,
        )
        if not candidates:
            return
        target = candidates[0]
        target.operational = False
        state.active_events.append(
            {
                "type": "plant_failure",
                "plant_id": target.id,
                "started_day": day,
                "ends_day": day + self.FAILURE_DURATION_DAYS,
                "severity": 1.0,
            }
        )
        state.scenario_trace.append(
            {
                "day": day,
                "kind": "plant_failure_injected",
                "plant_id": target.id,
                "ends_day": day + self.FAILURE_DURATION_DAYS,
            }
        )

    def _apply_low_wind(self, state: WorldState, day: int) -> None:
        clip: float | None = None
        window_end = 0
        starting = False
        ending = False
        for start, end, mps in self.LOW_WIND_WINDOWS:
            if start <= day < end:
                clip = mps
                window_end = end
                starting = day == start
                break
            if day == end:
                ending = True
        if clip is not None:
            state.weather_overrides["wind_speed_mps"] = float(clip)
            if starting:
                state.scenario_trace.append(
                    {"day": day, "kind": "low_wind_start", "wind_mps": float(clip)}
                )
                inject_display_marker(
                    state,
                    marker_type="low_wind",
                    started_day=day,
                    ends_day=window_end,
                    wind_mps=float(clip),
                )
        else:
            state.weather_overrides.pop("wind_speed_mps", None)
            if ending:
                state.scenario_trace.append({"day": day, "kind": "low_wind_end"})

    def _apply_solar_dark(self, state: WorldState, day: int) -> None:
        clip: float | None = None
        window_end = 0
        starting = False
        ending = False
        for start, end, cloud in self.SOLAR_DARK_WINDOWS:
            if start <= day < end:
                clip = cloud
                window_end = end
                starting = day == start
                break
            if day == end:
                ending = True
        if clip is not None:
            state.weather_overrides["cloud_factor"] = float(clip)
            if starting:
                state.scenario_trace.append(
                    {"day": day, "kind": "solar_dark_start", "cloud_factor": float(clip)}
                )
                inject_display_marker(
                    state,
                    marker_type="solar_dark",
                    started_day=day,
                    ends_day=window_end,
                    cloud_factor=float(clip),
                )
        else:
            state.weather_overrides.pop("cloud_factor", None)
            if ending:
                state.scenario_trace.append({"day": day, "kind": "solar_dark_end"})

    def _apply_fuel_shock(self, state: WorldState, day: int) -> None:
        shock: tuple[int, float, float] | None = None  # (window_end, coal, gas)
        starting = False
        ending = False
        for start, end, coal, gas in self.FUEL_SHOCK_WINDOWS:
            if start <= day < end:
                shock = (end, coal, gas)
                starting = day == start
                break
            if day == end:
                ending = True
        if shock is not None:
            window_end, coal, gas = shock
            state.plant_fuel_cost_per_mwh["coal_plant"] = coal
            state.plant_fuel_cost_per_mwh["gas_peaker"] = gas
            if starting:
                state.scenario_trace.append(
                    {
                        "day": day,
                        "kind": "fuel_shock_start",
                        "coal_usd_per_mwh": coal,
                        "gas_usd_per_mwh": gas,
                    }
                )
                inject_display_marker(
                    state,
                    marker_type="fuel_cost_shock",
                    started_day=day,
                    ends_day=window_end,
                    coal_usd_per_mwh=coal,
                    gas_usd_per_mwh=gas,
                )
        elif ending:
            state.plant_fuel_cost_per_mwh["coal_plant"] = self._BASELINE_COAL_USD_PER_MWH
            state.plant_fuel_cost_per_mwh["gas_peaker"] = self._BASELINE_GAS_USD_PER_MWH
            state.scenario_trace.append({"day": day, "kind": "fuel_shock_end"})

    def _apply_oil_collapse(self, state: WorldState, day: int) -> None:
        if self.OIL_COLLAPSE_START_DAY <= day < self.OIL_COLLAPSE_END_DAY:
            state.crude_price_usd_per_bbl = self.OIL_COLLAPSE_CRUDE_USD_PER_BBL
            state.refined_price_usd_per_bbl = self.OIL_COLLAPSE_REFINED_USD_PER_BBL
            if day == self.OIL_COLLAPSE_START_DAY:
                state.scenario_trace.append(
                    {
                        "day": day,
                        "kind": "oil_collapse_start",
                        "crude_usd_per_bbl": self.OIL_COLLAPSE_CRUDE_USD_PER_BBL,
                        "refined_usd_per_bbl": self.OIL_COLLAPSE_REFINED_USD_PER_BBL,
                    }
                )
                inject_display_marker(
                    state,
                    marker_type="oil_collapse",
                    started_day=day,
                    ends_day=self.OIL_COLLAPSE_END_DAY,
                    crude_usd_per_bbl=self.OIL_COLLAPSE_CRUDE_USD_PER_BBL,
                    refined_usd_per_bbl=self.OIL_COLLAPSE_REFINED_USD_PER_BBL,
                )
        elif day == self.OIL_COLLAPSE_END_DAY:
            state.crude_price_usd_per_bbl = self._BASELINE_CRUDE_USD_PER_BBL
            state.refined_price_usd_per_bbl = self._BASELINE_REFINED_USD_PER_BBL
            state.scenario_trace.append({"day": day, "kind": "oil_collapse_end"})

    def _inject_demand_events(self, state: WorldState, day: int) -> None:
        if day in self.HEATWAVE_DAYS and not any(
            e.get("type") == "heatwave" for e in state.active_events
        ):
            state.active_events.append(
                {
                    "type": "heatwave",
                    "started_day": day,
                    "ends_day": day + self.HEATWAVE_DURATION_DAYS,
                    "severity": self.HEATWAVE_SEVERITY,
                }
            )
            state.scenario_trace.append(
                {
                    "day": day,
                    "kind": "heatwave_injected",
                    "ends_day": day + self.HEATWAVE_DURATION_DAYS,
                }
            )

        if day in self.DEMAND_SURPRISE_DAYS and not any(
            e.get("type") == "demand_surprise" for e in state.active_events
        ):
            state.active_events.append(
                {
                    "type": "demand_surprise",
                    "started_day": day,
                    "ends_day": day + self.DEMAND_SURPRISE_DURATION_DAYS,
                    "severity": self.DEMAND_SURPRISE_SEVERITY,
                }
            )
            state.scenario_trace.append(
                {
                    "day": day,
                    "kind": "demand_surprise_injected",
                    "ends_day": day + self.DEMAND_SURPRISE_DURATION_DAYS,
                }
            )
