"""Build catalog for civilian and (later) energy tiles.

Costs and per-tile attributes from §4.12 of the design brief. Slice 02 lights
up only the civilian tiles plus the immutable town hall; energy plants and
wells land in slices 05 and 07 respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TileSpec:
    tile_type: str
    capex: float
    opex_per_day: float
    requires_road: bool
    description: str
    housing_capacity: int = 0
    jobs: int = 0
    # Peak electric demand in kW for this tile type. Commercial scales by an
    # hourly factor (full 8-20h, 20% otherwise); industrial draws this value
    # continuously. See world.power.total_demand_kw.
    demand_kw: float = 0.0
    # Generation capacity for plants. Solar/wind use weather-modulated output
    # capped at this value; coal/gas dispatch up to this with ramp limits.
    capacity_kw: float = 0.0
    # Fuel cost in $/MWh for fossil plants (used as merit-order key and for
    # daily fuel-cost accrual). Renewables are 0.
    fuel_cost_per_mwh: float = 0.0
    # CO2 intensity in tonnes/MWh (used by slice 10 carbon accounting; defined
    # here so the catalog has the full plant spec).
    co2_t_per_mwh: float = 0.0
    # Battery-only: round-trip energy capacity (kWh) and AC-to-AC efficiency.
    # capacity_kw is the rated charge/discharge power for batteries (same field
    # as plants); these two extend it. Non-battery tiles leave at 0.
    storage_kwh: float = 0.0
    round_trip_efficiency: float = 0.0
    buildable: bool = True  # False = not placeable via /build (town_hall, wells)


TILE_CATALOG: dict[str, TileSpec] = {
    "road": TileSpec(
        tile_type="road",
        capex=500,
        opex_per_day=0,
        requires_road=False,
        description="Enables connectivity for civilian tiles.",
    ),
    "house": TileSpec(
        tile_type="house",
        capex=3_000,
        opex_per_day=20,
        requires_road=True,
        description="+8 housing capacity. Requires road adjacency.",
        housing_capacity=8,
    ),
    "commercial": TileSpec(
        tile_type="commercial",
        capex=8_000,
        opex_per_day=50,
        requires_road=True,
        description=(
            "+12 jobs. 50 kW peak demand (8-20h, 20% otherwise). "
            "Earns ~$2/resident/day from houses within 5×5 × occupancy × staffing. "
            "Requires road adjacency."
        ),
        jobs=12,
        demand_kw=50,
    ),
    "industrial": TileSpec(
        tile_type="industrial",
        capex=20_000,
        opex_per_day=200,
        requires_road=True,
        description=(
            "+30 jobs. 300 kW continuous demand. Earns $500/day × staffing, "
            "emits 2 t CO2/day × staffing. Requires road adjacency."
        ),
        jobs=30,
        demand_kw=300,
    ),
    "park": TileSpec(
        tile_type="park",
        capex=5_000,
        opex_per_day=30,
        requires_road=False,
        description="Boosts happiness; no road requirement.",
    ),
    "demand_response_hub": TileSpec(
        tile_type="demand_response_hub",
        capex=30_000,
        opex_per_day=60,
        requires_road=False,
        description=(
            "Automated smart-grid control. Reduces civilian electricity "
            "demand by 15% from 09:00-20:00; city-wide reduction capped "
            "at 30%. No road requirement."
        ),
    ),
    "pipeline": TileSpec(
        tile_type="pipeline",
        capex=2_000,
        opex_per_day=5,
        requires_road=False,
        description="Crude transport. 4-connected networks route producer crude to refineries on the same network; orphan producers sell raw at $40/bbl; orphan refineries starve.",
    ),
    "solar_farm": TileSpec(
        tile_type="solar_farm",
        capex=25_000,
        opex_per_day=50,
        requires_road=False,
        description="Up to 150 kW (sun-dependent). No road requirement.",
        jobs=2,
        capacity_kw=150,
    ),
    "wind_turbine": TileSpec(
        tile_type="wind_turbine",
        capex=40_000,
        opex_per_day=80,
        requires_road=False,
        description=(
            "Up to 200 kW (wind-dependent). No road requirement. "
            "One-cell no-build halo; roads and batteries admitted inside it."
        ),
        jobs=2,
        capacity_kw=200,
    ),
    "gas_peaker": TileSpec(
        tile_type="gas_peaker",
        capex=80_000,
        opex_per_day=150,
        requires_road=False,
        description=(
            "0-500 kW. Ramp 50%/h. Fuel $30/MWh. "
            "Dispatches only when sharing a pipeline network with an "
            "operational refinery. One-cell no-build halo; roads and "
            "batteries admitted inside it."
        ),
        jobs=4,
        capacity_kw=500,
        fuel_cost_per_mwh=30.0,
        co2_t_per_mwh=0.4,
    ),
    "coal_plant": TileSpec(
        tile_type="coal_plant",
        capex=200_000,
        opex_per_day=400,
        requires_road=True,
        description=(
            "375-1500 kW. Min run 25%, ramp 10%/h. Fuel $12/MWh. "
            "Needs 30 workers and a road-adjacent site. One-cell "
            "no-build halo; roads and batteries admitted inside it."
        ),
        jobs=30,
        capacity_kw=1500,
        fuel_cost_per_mwh=12.0,
        co2_t_per_mwh=0.9,
    ),
    "battery": TileSpec(
        tile_type="battery",
        capex=60_000,
        opex_per_day=40,
        requires_road=False,
        description=(
            "Grid-scale battery. 200 kW rated charge/discharge, 800 kWh storage, "
            "85% round-trip. Auto-charges from renewable surplus and discharges "
            "to cover residual demand; manual override via /control/battery."
        ),
        jobs=0,
        capacity_kw=200,
        storage_kwh=800,
        round_trip_efficiency=0.85,
    ),
    "refinery": TileSpec(
        tile_type="refinery",
        capex=150_000,
        opex_per_day=300,
        requires_road=True,
        description="+25 jobs. Up to 250 bbl/day. 200 kWh/bbl. 0.3 t CO2/bbl. Requires road.",
        jobs=25,
    ),
    "oil_well": TileSpec(
        tile_type="oil_well",
        capex=50_000,
        opex_per_day=100,
        requires_road=False,
        description="Production well. Setpoint 0-200 bbl/day. Drilled via /drill.",
        jobs=3,
        buildable=False,
    ),
    "injection_well": TileSpec(
        tile_type="injection_well",
        capex=30_000,
        opex_per_day=50,
        requires_road=False,
        description="Injection well. Setpoint 0-200 bbl/day. 50 kWh/bbl. Drilled via /drill.",
        jobs=2,
        buildable=False,
    ),
    "town_hall": TileSpec(
        tile_type="town_hall",
        capex=0,
        opex_per_day=0,
        requires_road=False,
        description="Civic center; counts as road for adjacency. Immutable.",
        housing_capacity=100,
        jobs=30,
        buildable=False,
    ),
}


WELL_TYPES: frozenset[str] = frozenset({"oil_well", "injection_well"})


def _spec_to_dict(spec: TileSpec) -> dict[str, Any]:
    return {
        "tile_type": spec.tile_type,
        "capex": spec.capex,
        "opex_per_day": spec.opex_per_day,
        "requires_road": spec.requires_road,
        "description": spec.description,
        "housing_capacity": spec.housing_capacity,
        "jobs": spec.jobs,
        "demand_kw": spec.demand_kw,
        "capacity_kw": spec.capacity_kw,
        "fuel_cost_per_mwh": spec.fuel_cost_per_mwh,
        "co2_t_per_mwh": spec.co2_t_per_mwh,
        "storage_kwh": spec.storage_kwh,
        "round_trip_efficiency": spec.round_trip_efficiency,
        "buildable": spec.buildable,
    }


def build_catalog() -> dict[str, Any]:
    from world.config import load_config
    from world.economy import (
        CARBON_PRICE_USD_PER_TON,
        COMMERCIAL_RADIUS,
        COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
        INDUSTRIAL_REVENUE_PER_DAY,
        REFINED_PRICE_USD_PER_BBL,
        REFINERY_CO2_PER_BBL,
        REFINERY_MAX_BBL_DAY,
        REFINERY_YIELD,
    )
    from world.subsurface import (
        CRUDE_PRICE_USD_PER_BBL,
        INJECTION_KWH_PER_BBL,
        Q_MAX_WELL_BBL_DAY,
        SEISMIC_BASE_COST,
        SEISMIC_DEFAULT_SIZE,
        SEISMIC_MAX_SIZE,
        SEISMIC_MIN_SIZE,
    )

    tiles: list[dict[str, Any]] = []
    wells: list[dict[str, Any]] = []
    for spec in TILE_CATALOG.values():
        entry = _spec_to_dict(spec)
        if spec.tile_type in WELL_TYPES:
            wells.append(entry)
        else:
            tiles.append(entry)
    oil_well = TILE_CATALOG["oil_well"]
    injection_well = TILE_CATALOG["injection_well"]
    cfg = load_config()
    subsurface = {
        "survey": {
            "base_cost": SEISMIC_BASE_COST,
            "base_size": SEISMIC_DEFAULT_SIZE,
            "min_size": SEISMIC_MIN_SIZE,
            "max_size": SEISMIC_MAX_SIZE,
            "cost_formula": "base * (size/4)**2",
            "default_size": SEISMIC_DEFAULT_SIZE,
        },
        "drill": {
            "production": {
                "capex": oil_well.capex,
                "opex_per_day": oil_well.opex_per_day,
                "max_rate_bbl_day": Q_MAX_WELL_BBL_DAY,
                "crude_price_usd_per_bbl": CRUDE_PRICE_USD_PER_BBL,
                "cost_formula": "base * (1 + (target_z / world_depth)**2)",
                "world_depth": cfg.world_d,
            },
            "injection": {
                "capex": injection_well.capex,
                "opex_per_day": injection_well.opex_per_day,
                "max_rate_bbl_day": Q_MAX_WELL_BBL_DAY,
                "kwh_per_bbl": INJECTION_KWH_PER_BBL,
                "cost_formula": "base * (1 + (target_z / world_depth)**2)",
                "world_depth": cfg.world_d,
            },
        },
    }
    economics = {
        "industrial_revenue_per_day": INDUSTRIAL_REVENUE_PER_DAY,
        "commercial_revenue_per_resident_per_day": COMMERCIAL_REVENUE_PER_RESIDENT_PER_DAY,
        "commercial_radius": COMMERCIAL_RADIUS,
        "carbon_price": CARBON_PRICE_USD_PER_TON,
        "grid_price_retail": cfg.grid_price_retail,
        "grid_price_export": cfg.grid_price_export,
        "refined_price_usd_per_bbl": REFINED_PRICE_USD_PER_BBL,
        "refinery_yield": REFINERY_YIELD,
        "refinery_co2_t_per_bbl": REFINERY_CO2_PER_BBL,
        "refinery_max_bbl_day": REFINERY_MAX_BBL_DAY,
        "crude_price_usd_per_bbl": CRUDE_PRICE_USD_PER_BBL,
        "injection_kwh_per_bbl": INJECTION_KWH_PER_BBL,
    }
    return {
        "tiles": tiles,
        "wells": wells,
        "subsurface": subsurface,
        "economics": economics,
    }


def is_buildable(tile_type: str) -> bool:
    spec = TILE_CATALOG.get(tile_type)
    return bool(spec and spec.buildable)
