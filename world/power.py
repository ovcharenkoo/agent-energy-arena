"""Hourly demand + dispatch + balance-state model.

`total_demand_kw(state, h)` is the per-hour total electric load (brief §4.3
with the PRD's split-scope event multipliers).

`dispatch(plants, demand_kw, prev_outputs, weather, D, h)` runs the merit
order from brief §4.4: must-take renewables → coal must-run → coal ramp by
fuel cost → gas peakers ramp by fuel cost. Returns per-plant outputs,
total supply, and an aggregate by source.

`compute_balance_state(supply, demand)` returns one of "curtailment",
"balanced", "brownout", "blackout" along with served/excess kWh — the
thresholds match brief §4.4 with `R = supply / max(demand, 1)`.

Event multipliers (PRD's correction to the brief's bottom-line multipliers):

  * Heatwave (1.40) multiplies *residential demand only* — A/C drives it.
  * Demand surprise (1.30) multiplies *commercial + industrial only*.
  * Process loads are unaffected by either multiplier.

The per-event multiplier helpers live in ``world.event_effects`` so the
effect surface of each event is one grep target (CONTEXT.md — Event).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from world import workforce
from world.catalog import TILE_CATALOG
from world.demand_response import apply_demand_response
from world.event_effects import demand_surprise_ic_mult, heatwave_residential_mult
from world.snapshots import BalanceState, WeatherNow
from world.weather import P_solar_kw, turbine_kw

if TYPE_CHECKING:
    from world.state import Tile, WorldState

PER_CAPITA_KW: float = 0.333  # 8 kWh/day continuous; brief §4.3

# Dispatch ramp/min-run (brief §4.4).
COAL_RAMP_PER_HOUR: float = 0.10
GAS_RAMP_PER_HOUR: float = 0.50
COAL_MIN_RUN: float = 0.25

# Balance-state thresholds (brief §4.4).
R_CURTAILMENT: float = 1.15
R_BALANCED: float = 0.95
R_BROWNOUT: float = 0.70

# Per-hour happiness penalties from outages live in
# `world.population` as BLACKOUT_HAPPINESS_PER_HOUR /
# BROWNOUT_HAPPINESS_PER_HOUR. The hourly-decrement-on-state.happiness
# pattern was removed in issue 22 — `update_population` reassigns
# happiness end-of-day, so per-hour writes were silently clobbered.

# Plant types that participate in dispatch.
RENEWABLE_TYPES: frozenset[str] = frozenset({"solar_farm", "wind_turbine"})
FOSSIL_TYPES: frozenset[str] = frozenset({"coal_plant", "gas_peaker"})
PLANT_TYPES: frozenset[str] = RENEWABLE_TYPES | FOSSIL_TYPES


# Per-hour residential demand multiplier (revises brief §4.3). Shape: deep
# night minimum at h=1-3, smooth ramp through the morning, midday peak at
# h=12 (≈1.5), evening shoulder at h=17-19 for cooking / leisure, late
# evening drop. Calibrated so the 24-hour sum stays at 22.3 (≈22.3/24 ≈
# 0.93 mean — unchanged from the brief's stepped curve), so day-level
# residential consumption — and the city-wide economics tuned against it
# — are invariant. Only the intra-day mix changes: daytime gets more,
# night gets less. Peak/night ratio ≈ 4.3×.
_HOURLY_RESIDENTIAL_FACTOR: tuple[float, ...] = (
    0.40,
    0.35,
    0.35,
    0.35,
    0.40,  # 00-04 night trough
    0.55,
    0.75,
    0.95,
    1.10,  # 05-08 morning ramp
    1.25,
    1.35,
    1.45,
    1.50,  # 09-12 climb to midday peak
    1.45,
    1.35,
    1.25,
    1.20,  # 13-16 afternoon descent
    1.20,
    1.20,
    1.15,  # 17-19 evening shoulder
    0.95,
    0.75,
    0.60,
    0.45,  # 20-23 evening drop
)


def hourly_factor(h: int) -> float:
    return _HOURLY_RESIDENTIAL_FACTOR[h]


def residential_kw(h: int, pop: int) -> float:
    return pop * PER_CAPITA_KW * hourly_factor(h)


def commercial_factor(h: int) -> float:
    return 1.0 if 8 <= h < 20 else 0.2


def _industrial_kw(state: WorldState) -> float:
    return sum(t.demand_kw * workforce.efficiency(t) for t in state.tiles if t.type == "industrial")


def _commercial_peak_kw(state: WorldState) -> float:
    return sum(t.demand_kw * workforce.efficiency(t) for t in state.tiles if t.type == "commercial")


def _process_loads_kw(state: WorldState) -> float:
    # Process loads (injection wells, refineries) are added directly by the
    # sim loop alongside civilian demand — they need to be split out so power
    # revenue bills only the civilian portion. This stub stays at 0.0 so
    # `total_demand_kw` returns the civilian-only figure.
    return 0.0


def total_demand_kw(state: WorldState, h: int) -> float:
    res = residential_kw(h, int(state.population)) * heatwave_residential_mult(state)
    ic = (_industrial_kw(state) + _commercial_peak_kw(state) * commercial_factor(h)) * (
        demand_surprise_ic_mult(state)
    )
    process = _process_loads_kw(state)
    return float(apply_demand_response(state, h, res + ic) + process)


# -- Dispatch ----------------------------------------------------------------


def dispatch(
    plants: list[Tile],
    demand_kw: float,
    prev_outputs: dict[str, float],
    weather: WeatherNow,
    D: int,
    h: int,
    solar_derate: float = 1.0,
    fuel_cost_per_mwh: dict[str, float] | None = None,
    unsupplied_peaker_ids: frozenset[str] | None = None,
) -> tuple[dict[str, float], float, dict[str, float]]:
    """Run the merit-order dispatch for one hour.

    Returns (outputs_by_plant_id, supply_kw, by_source_kw). by_source_kw
    aggregates outputs into the four canonical keys: "solar", "wind",
    "coal", "gas". Non-operational plants are zeroed; they neither
    consume ramp room nor count toward must-run.

    `solar_derate` (default 1.0) multiplies the per-solar-plant output
    cap to model heatwave panel-temperature losses; wind unaffected.

    `fuel_cost_per_mwh` (default None) supplies the per-plant-type fuel
    costs that drive the merit-order key for coal/gas. When None the
    catalog defaults are used so unit tests calling `dispatch()` directly
    keep their semantics. Production callers pass `state.plant_fuel_cost_per_mwh`
    so a scenario can flip the merit order via a fuel-price shock.

    `unsupplied_peaker_ids` (default None) lists gas peakers that are not
    connected to an operational refinery via the pipeline network this
    hour. Filtered before merit-order ordering and treated identically to
    a `plant_failure` (zero output, no ramp credit) — downstream
    brownout/blackout accounting flows through the existing path. The
    day loop derives it from `world.pipelines.peaker_supplied_ids`
    once per day (the supplied set is day-stable).
    """
    outputs: dict[str, float] = {p.id: 0.0 for p in plants}

    cloud = weather.cloud_factor
    wind_v = weather.wind_speed_mps
    unsupplied = unsupplied_peaker_ids or frozenset()

    def _cost(plant_type: str) -> float:
        if fuel_cost_per_mwh is not None and plant_type in fuel_cost_per_mwh:
            return fuel_cost_per_mwh[plant_type]
        return TILE_CATALOG[plant_type].fuel_cost_per_mwh

    operational = [
        p for p in plants if p.operational and not (p.type == "gas_peaker" and p.id in unsupplied)
    ]
    solar = [p for p in operational if p.type == "solar_farm"]
    wind = [p for p in operational if p.type == "wind_turbine"]
    coal = sorted(
        (p for p in operational if p.type == "coal_plant"),
        key=lambda x: (_cost(x.type), x.id),
    )
    gas = sorted(
        (p for p in operational if p.type == "gas_peaker"),
        key=lambda x: (_cost(x.type), x.id),
    )

    # Per-PRD: an N%-staffed plant behaves like an N%-sized plant. Every
    # capacity-derived figure (ceiling, must-run floor, ramp room, intermittent
    # output cap) is multiplied by workforce.efficiency(p). Fuel burn and CO2
    # are linear in dispatched kWh and scale automatically.
    eff_cap: dict[str, float] = {
        p.id: TILE_CATALOG[p.type].capacity_kw * workforce.efficiency(p) for p in operational
    }

    # Step 1: must-take renewables (capped at effective capacity).
    # Solar cap is further scaled by `solar_derate` (heatwave panel-temp loss).
    for p in solar:
        outputs[p.id] = min(P_solar_kw(D, h, cloud), eff_cap[p.id] * solar_derate)
    for p in wind:
        outputs[p.id] = min(turbine_kw(wind_v), eff_cap[p.id])

    supply = sum(outputs.values())

    # Step 2: coal must-run minimum (25% of effective capacity).
    for p in coal:
        outputs[p.id] = eff_cap[p.id] * COAL_MIN_RUN
        supply += outputs[p.id]

    remaining = max(0.0, demand_kw - supply)

    # Step 3: ramp coal upward by cost (already sorted). Bound by ramp_room
    # measured from the previous hour's output, capped at effective capacity.
    for p in coal:
        if remaining <= 0:
            break
        cap = eff_cap[p.id]
        ramp_room = cap * COAL_RAMP_PER_HOUR
        # Newly-built coal: assume it warm-starts at must-run, no prior hour.
        prev_out = prev_outputs.get(p.id, cap * COAL_MIN_RUN)
        upper = min(cap, prev_out + ramp_room)
        headroom = upper - outputs[p.id]
        if headroom <= 0:
            continue
        inc = min(headroom, remaining)
        outputs[p.id] += inc
        supply += inc
        remaining -= inc

    # Step 4: gas peakers ramp by cost.
    for p in gas:
        if remaining <= 0:
            outputs[p.id] = 0.0
            continue
        cap = eff_cap[p.id]
        ramp_room = cap * GAS_RAMP_PER_HOUR
        prev_out = prev_outputs.get(p.id, 0.0)
        max_out = min(cap, prev_out + ramp_room)
        delivered = min(max_out, remaining)
        outputs[p.id] = delivered
        supply += delivered
        remaining -= delivered

    by_source = {
        "solar": sum(outputs[p.id] for p in solar),
        "wind": sum(outputs[p.id] for p in wind),
        "coal": sum(outputs[p.id] for p in coal),
        "gas": sum(outputs[p.id] for p in gas),
    }
    return outputs, supply, by_source


# -- Batteries ---------------------------------------------------------------
#
# Dispatch slots (PRD §"Battery dispatch"):
#   * step 1.5 — `battery_charge_step` absorbs renewable surplus into SoC,
#     consuming `sqrt(eta)` per kWh stored (the charging half of round-trip).
#   * step 5   — `battery_discharge_step` closes any residual demand after gas
#     ramps, draining `1/sqrt(eta)` kWh of SoC per kWh delivered.
#
# Setpoint sign convention (`Tile.charge_setpoint_kw`):
#   *  0  → auto: charge from surplus, discharge to close residual.
#   * >0  → manual charge mode: cap charging at the setpoint (still clamped
#           to renewable surplus + rated power + SoC room). Does NOT discharge.
#   * <0  → manual discharge mode: cap discharge at |setpoint| (still clamped
#           to rated power + available SoC). Does NOT charge.


def _battery_sqrt_eta(b: Tile) -> float:
    spec = TILE_CATALOG[b.type]
    if spec.round_trip_efficiency <= 0.0:
        return 0.0
    return math.sqrt(spec.round_trip_efficiency)


def battery_charge_step(
    batteries: list[Tile],
    renewable_supply_kw: float,
    demand_kw: float,
) -> tuple[dict[str, float], float, dict[str, float]]:
    """Absorb renewable surplus into batteries (dispatch step 1.5).

    Returns `(draw_kw_per_battery, total_draw_kw, soc_delta_kwh_per_battery)`.
    `draw_kw` is the kW each battery pulls from the bus this hour; SoC grows by
    `sqrt(eta) * draw_kw * 1h`. Batteries are iterated in id-ascending order so
    the order of operations is deterministic across runs.
    """
    charges: dict[str, float] = {b.id: 0.0 for b in batteries}
    soc_deltas: dict[str, float] = {b.id: 0.0 for b in batteries}
    surplus = max(0.0, renewable_supply_kw - demand_kw)
    if surplus <= 0:
        return charges, 0.0, soc_deltas

    total = 0.0
    for b in sorted(batteries, key=lambda x: x.id):
        if not b.operational:
            continue
        if b.charge_setpoint_kw < 0:
            continue  # manual discharge mode — no charging this hour
        spec = TILE_CATALOG[b.type]
        rated = spec.capacity_kw * workforce.efficiency(b)
        max_draw = rated if b.charge_setpoint_kw == 0 else min(b.charge_setpoint_kw, rated)
        sqrt_eta = _battery_sqrt_eta(b)
        if sqrt_eta <= 0:
            continue
        room_kwh = max(0.0, spec.storage_kwh - b.soc_kwh)
        max_room_draw = room_kwh / sqrt_eta
        draw = min(max_draw, max_room_draw, surplus)
        if draw <= 0:
            continue
        charges[b.id] = draw
        soc_deltas[b.id] = draw * sqrt_eta
        total += draw
        surplus -= draw
    return charges, total, soc_deltas


def battery_discharge_step(
    batteries: list[Tile],
    residual_demand_kw: float,
) -> tuple[dict[str, float], float, dict[str, float]]:
    """Close residual demand from battery SoC (dispatch step 5).

    Returns `(deliver_kw_per_battery, total_deliver_kw, soc_delta_kwh_per_battery)`.
    `soc_delta` is negative — delivering 1 kWh drains `1/sqrt(eta)` from SoC.
    Iteration is id-ascending for determinism.
    """
    discharges: dict[str, float] = {b.id: 0.0 for b in batteries}
    soc_deltas: dict[str, float] = {b.id: 0.0 for b in batteries}
    if residual_demand_kw <= 0:
        return discharges, 0.0, soc_deltas

    total = 0.0
    for b in sorted(batteries, key=lambda x: x.id):
        if not b.operational:
            continue
        if b.charge_setpoint_kw > 0:
            continue  # manual charge mode — no discharge this hour
        spec = TILE_CATALOG[b.type]
        rated = spec.capacity_kw * workforce.efficiency(b)
        rated_cap = min(rated, -b.charge_setpoint_kw) if b.charge_setpoint_kw < 0 else rated
        sqrt_eta = _battery_sqrt_eta(b)
        if sqrt_eta <= 0:
            continue
        deliverable_kwh = max(0.0, b.soc_kwh) * sqrt_eta
        deliver = min(rated_cap, deliverable_kwh, residual_demand_kw)
        if deliver <= 0:
            continue
        discharges[b.id] = deliver
        soc_deltas[b.id] = -(deliver / sqrt_eta)
        total += deliver
        residual_demand_kw -= deliver
    return discharges, total, soc_deltas


# -- Balance state -----------------------------------------------------------


def daily_met_demand_fraction(
    supply_kw_by_hour: list[float] | tuple[float, ...],
    demand_kw_by_hour: list[float] | tuple[float, ...],
) -> float:
    """Grid's daily met-demand fraction, averaged across the 24 hours.

    For each hour, the met-demand fraction is ``min(supply, demand) / demand``
    clamped to ``[0, 1]``. The day's value is the equal-weighted mean of the
    24 hourly fractions. Hours with zero demand contribute ``1.0`` (no unmet
    demand). When the demand trace is empty (no day has completed yet),
    returns ``1.0`` — the "no-evidence-of-shortage" default.

    Named for its widest scope: this is a property of the bus, not of any
    single load. Industrial revenue gating (issue 08) consumes it because
    industrial demand is hour-flat on a single-pool grid, so the "fraction
    of industrial demand actually served across the day" equals this
    aggregate by construction.
    """
    if not demand_kw_by_hour:
        return 1.0
    n = len(demand_kw_by_hour)
    total = 0.0
    for h in range(n):
        d = demand_kw_by_hour[h]
        s = supply_kw_by_hour[h] if h < len(supply_kw_by_hour) else 0.0
        if d <= 0.0:
            total += 1.0
        else:
            total += min(1.0, max(0.0, s) / d)
    return total / n


def compute_balance_state(
    supply_kw: float, demand_kw: float
) -> tuple[BalanceState, float, float, float]:
    """Map (supply, demand) onto the four balance states.

    Returns (state, served_kw, excess_kw, R). When demand is zero the grid
    is treated as balanced with served=excess=0 (no loads to serve, no
    export market either).
    """
    if demand_kw <= 0:
        return BalanceState.BALANCED, 0.0, 0.0, 0.0
    R = supply_kw / max(demand_kw, 1.0)
    if R >= R_CURTAILMENT:
        return BalanceState.CURTAILMENT, demand_kw, max(0.0, supply_kw - demand_kw), R
    if R >= R_BALANCED:
        return BalanceState.BALANCED, demand_kw, 0.0, R
    if R >= R_BROWNOUT:
        return BalanceState.BROWNOUT, supply_kw, 0.0, R
    return BalanceState.BLACKOUT, supply_kw, 0.0, R
