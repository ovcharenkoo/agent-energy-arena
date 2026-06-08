# Rules

The full mechanics of the simulation. Every formula here has a 1:1 named-function counterpart in `world/` (see the file references). Numbers are configurable through `world/config.py` and the environment variables it reads; the defaults shown are what ships.

If you're after API shapes, see [API.md](API.md). If you're writing a stress scenario, see [SCENARIOS.md](SCENARIOS.md).

## Table of contents

- [Time and map](#time-and-map)
- [Starting conditions](#starting-conditions)
- [Build catalog](#build-catalog)
- [Weather (solar, wind, forecasts)](#weather-solar-wind-forecasts)
- [Demand](#demand)
- [Dispatch and grid balance](#dispatch-and-grid-balance)
- [Batteries](#batteries)
- [Subsurface and wells](#subsurface-and-wells)
- [Pipelines and crude routing](#pipelines-and-crude-routing)
- [Refinery](#refinery)
- [Carbon and emissions](#carbon-and-emissions)
- [Population and happiness](#population-and-happiness)
- [Revenue, taxes, and finance](#revenue-taxes-and-finance)
- [Events](#events)
- [Scoring](#scoring)
- [Determinism contract](#determinism-contract)

## Time and map

| Symbol | Default | Env var | Notes |
|---|---|---|---|
| Tick | 24/day | `TICKS_PER_DAY` | One simulated hour. |
| Game length | 3650 days | `GAME_DAYS` | Agent eval default. |
| Manual game length | 365 days | `MANUAL_GAME_DAYS` | UI default. |
| Surface width | 32 | `WORLD_W` | tiles |
| Surface height | 32 | `WORLD_H` | tiles |
| Subsurface depth | 16 | `WORLD_D` | voxels; z=0 is top |
| Step cadence | 1–7 days/call | argued | `POST /step` accepts `days ∈ [1,7]`. |

The agent decides at hour 0 of each day. Build/demolish/survey/drill actions submitted before `POST /step` apply at the start of the first stepped day. Well/refinery/plant/battery setpoints apply for the duration of subsequent days until changed.

## Starting conditions

| Field | Default | Env var |
|---|---|---|
| Treasury | $300,000 | `STARTING_CASH` |
| Population | 100 | `STARTING_POP` |
| Town hall | center, +100 housing, +30 jobs | hardcoded |

The town hall is immutable, counts as a road for adjacency, and provides housing + jobs at no cost. All other tiles start empty. The subsurface is hidden until surveyed.

## Build catalog

CAPEX is paid up-front at build time; OPEX is deducted daily as long as the tile exists. Demolition refunds 25% of the original CAPEX. The full machine-readable catalog ships through `GET /catalog`; the source of truth is `world/catalog.py`.

| Tile | CAPEX | OPEX/day | Spec |
|---|---:|---:|---|
| `road` | 500 | 0 | Connectivity for civilian tiles. |
| `house` | 3,000 | 20 | +8 housing capacity. Road-adjacent. |
| `commercial` | 8,000 | 50 | +12 jobs. 50 kW peak (8–20h), 20% otherwise. Earns commercial revenue per nearby resident. Road-adjacent. |
| `industrial` | 20,000 | 200 | +30 jobs. 300 kW continuous. Earns `industrial_revenue_per_day` per staffed slot. Emits CO₂. Road-adjacent. |
| `park` | 5,000 | 30 | Boosts happiness with radius-2 effect. |
| `solar_farm` | 25,000 | 50 | Up to 150 kW (sun + cloud-dependent). |
| `wind_turbine` | 40,000 | 80 | Up to 200 kW (wind-dependent). |
| `gas_peaker` | 80,000 | 150 | 0–500 kW. Ramp 50%/h. Fuel $30/MWh. 0.4 t CO₂/MWh. |
| `coal_plant` | 200,000 | 400 | 375–1500 kW. Min run 25%. Ramp 10%/h. Fuel $12/MWh. 0.9 t CO₂/MWh. |
| `battery` | 60,000 | 40 | 200 kW rated, 800 kWh storage, 85% round-trip. |
| `oil_well` | 50,000 base | 100 | Production well. Setpoint 0–200 bbl/day. Drilled via `/drill`; actual CAPEX scales quadratically with `target_z` (see Subsurface). |
| `injection_well` | 30,000 base | 50 | Injection well. Setpoint 0–200 bbl/day. Power 50 kWh/bbl (shed during brownout/blackout, 2× during curtailment). Drilled via `/drill`; quadratic depth-CAPEX. |
| `refinery` | 150,000 | 300 | +25 jobs. Up to 250 bbl/day. 200 kWh/bbl. 0.3 t CO₂/bbl. Road-adjacent. |
| `pipeline` | 2,000 | 5 | Crude transport. Routes producer→refinery on the 4-connected component. |
| `town_hall` | n/a | 0 | Placed at start. Immutable. +100 housing, +30 jobs. |

Adjacency rules:

- `house`, `commercial`, `industrial`, `refinery` must be orthogonally adjacent to a road tile or to another civilian tile that is itself road-connected via 4-connected flood-fill from any road. The town hall counts as a road. Coal plants are road-required for the same reason.
- Plants, batteries, and wells do not require road adjacency. Pipelines define their own 4-connected crude-transport network.
- `coal_plant`, `gas_peaker`, and `wind_turbine` each impose a one-cell no-build halo on the 8-neighborhood. Roads and batteries are admitted inside the halo (so plants can still be serviced and storage co-located); the town hall is admitted on the same grounds as a road. Existing tiles that already violate the rule at world-load time are grandfathered — the check only runs at build time.
- Wells are placed via `/drill`, not `/build`; the call specifies `target_z`. Two wells may share the same surface tile **only** if their `target_z` differs by ≥ 3 voxels (stacked completion); otherwise the call returns `completion_overlap`. A road or other built tile at (x, y) returns `tile_occupied` regardless of depth.

Validity errors returned by mutating endpoints: `insufficient_funds`, `tile_occupied`, `completion_overlap`, `out_of_bounds`, `no_road_adjacency`, `spacing_violation`, `voxel_out_of_bounds`, `unknown_tile_type`, and a handful of endpoint-specific cases (see [API.md](API.md)). `spacing_violation` carries the offending neighbor's `(x, y)` in the `result` field.

## Weather (solar, wind, forecasts)

Implementation: `world/weather.py`. Both processes consume the `sim_rng` stream; values can be clipped by `state.weather_overrides[...]` written by a scenario.

### Solar

```
sunrise(D)    = 6  - 2·sin(2π·D/365)        # 4..8
sunset(D)     = 18 + 2·sin(2π·D/365)        # 16..20
day_length(D) = sunset(D) - sunrise(D)

irradiance(D, h) =
    0                                            if h < sunrise(D) or h > sunset(D)
    sin(π·(h - sunrise(D))/day_length(D)) · cloud_factor(t)   otherwise

cloud_factor(t+1) = clip(0.7·cloud_factor(t) + 0.3·0.85 + N(0, 0.10), 0.1, 1.0)
P_solar_kw(t)     = SOLAR_PEAK_KW · irradiance(D, h)              # SOLAR_PEAK_KW = 150
```

### Wind

```
v_mean(D)  = 7 + 2·sin(2π·D/365 + φ_seed)        # m/s, seasonal
v(t+1)     = clip(0.85·v(t) + 0.15·v_mean(D) + N(0, 1.5), 0, 30)
θ(t+1)     = (θ(t) + N(0, 5°)) mod 360°

turbine_kw(v) =
    0                                       if v < 3.0 or v > 25.0
    WIND_RATED_KW                           if v ≥ 12.0          # 200
    WIND_RATED_KW · ((v - 3.0)/9.0)³        otherwise
```

Direction `θ` is reported in `state.weather_now` but does not affect output (turbines auto-yaw in v1).

### Forecasts

```
forecast(world, hours=24):
  for i in [0..hours):
    σ = 0.05 + 0.25·(i/hours)              # 0.05 → 0.30
    yield {
      hour_offset:     i,
      solar_irradiance: clip(true·(1 + N(0, σ)),     0, 1),
      wind_speed_mps:   max(0, true + N(0, σ·5)),
      demand_factor:    true·(1 + N(0, σ·0.3)),
    }
```

Forecasts are independently sampled per call from a dedicated `forecast_rng` stream — re-querying the same hours yields different noise. Averaging across calls reduces variance.

## Demand

Implementation: `world/power.py`.

```
PER_CAPITA_KW = 0.333                          # 8 kWh/day/person ≈ 0.333 kW continuous

residential_kw(h, pop) = pop · PER_CAPITA_KW · hourly_factor(h)

hourly_factor(h):
    h < 5:  0.6     # late night
    h < 9:  1.0     # morning
    h < 17: 0.8     # midday
    h < 22: 1.5     # evening peak
    else:   0.7
```

Industrial tiles draw their full `demand_kw` continuously. Commercial tiles draw full `demand_kw` between 08:00 and 20:00, 20% otherwise. Injection wells and refineries add their process load (computed from yesterday's throughput so dispatch is causal).

Automated Demand Response Hubs coordinate flexible civilian loads. Each
operational hub reduces residential + commercial + industrial demand by 15%
from 09:00 through 19:59, with a city-wide cap of 30%. Process loads from wells
and refineries are not reduced.

Multipliers (state-mutable, scenario-targetable):

- Heatwave multiplies residential demand by 1.40 while `heatwave` is active.
- Demand surprise multiplies commercial+industrial demand by 1.30 while `demand_surprise` is active.

## Dispatch and grid balance

Implementation: `world/power.py`.

Each hour, dispatch fires in order:

1. **Must-take renewables.** Solar and wind at their weather-modulated `available_kw`.
2. **Battery charge/discharge.** Auto: charge when `R = supply/demand > 1.05`, discharge when `R < 0.95`; manual override via `charge_setpoint_kw` clamps the band. Round-trip efficiency 85%.
3. **Coal must-run.** Each coal plant runs at ≥25% of capacity.
4. **Coal ramp-up by merit order.** Cheapest fuel cost first; per-hour ramp room is `capacity · COAL_RAMP_PER_HOUR` (0.10).
5. **Gas peakers.** Cheapest fuel cost first; ramp room `capacity · GAS_RAMP_PER_HOUR` (0.50).
6. **Per-plant overrides.** `POST /control/plant` setpoints (when wired) clamp Step 4/5 outcomes.

With `R = supply / max(demand, 1)`:

```
R ≥ 1.15:   state = "curtailment"  served = demand   surplus exported at grid_price_export
R ≥ 0.95:   state = "balanced"     served = demand
R ≥ 0.70:   state = "brownout"     served = supply   happiness -= 0.05·(1-R)
R <  0.70:  state = "blackout"     served = supply   happiness -= 0.20
                                                     treasury  -= state.blackout_penalty_hour
```

Curtailed kWh sold to the external grid (`grid_price_export`, default $0.04/kWh) does **not** contribute to the renewable share denominator — only kWh actually served to local load count toward `R` in the score formula.

Per-source dispatch totals land in `state.power_now.by_source_kw` and the hourly arrays `last_day_supply_kw_by_hour`, `last_day_demand_kw_by_hour`, and `last_day_balance_state_by_hour`.

## Batteries

`battery` tile fields: `soc_kwh` (state of charge, kWh), `charge_setpoint_kw` (>0 charge, <0 discharge, 0 = auto).

```
rated     = TILE_CATALOG["battery"].capacity_kw          # 200
store_cap = TILE_CATALOG["battery"].storage_kwh          # 800
η         = TILE_CATALOG["battery"].round_trip_efficiency # 0.85

cmd = clip(charge_setpoint_kw, -rated, +rated)    if override
    = +rated  if auto and R > 1.05
    = -rated  if auto and R < 0.95
    = 0       otherwise

if cmd > 0:  soc_kwh += cmd · √η         # charging stores √η of input
if cmd < 0:  soc_kwh += cmd / √η         # discharging burns √η of output
```

API knob: `POST /control/battery {tile_id, charge_kw}`. `charge_kw = 0` returns to auto.

## Subsurface and wells

Implementation: `world/subsurface.py`. Voxel coordinates are `(x, y, z)` with `z=0` at the top, `z=WORLD_D-1` at the bottom.

### Reservoir generation per seed

At reset, the world generates 3–7 reservoir blobs (`reservoir_rng` stream):

1. Center voxel `(x, y, z)` with `z ∈ [4, WORLD_D-2]`.
2. Radius `r ∈ [3, 6]`.
3. Within Manhattan distance `r` of center, mark a voxel hydrocarbon-bearing with probability `0.6·(1 - dist/r)`. Each accepted voxel is tagged with the blob's 1-indexed `reservoir_id`; voxels sharing an id form a single 26-connected component.
4. Per HC voxel: porosity `φ ~ U(0.10, 0.30)`, permeability `k ~ LogU(10, 1000)` mD, oil saturation `S_o ~ U(0.55, 0.80)`, `oil_in_place_bbl = φ · S_o · VOXEL_VOLUME_BBL`. `VOXEL_VOLUME_BBL = 56,000` (per-voxel OIP ranges ~3k–13k bbl, mean ~7.5k).
5. Total OOIP varies by seed; a typical 36-voxel reservoir holds ~270k bbl — exhaustible by an injection-supported producer within the 10-year game horizon.

### Surveys

`SEISMIC_BASE_COST = 15_000`; cost scales quadratically with column size: `cost = 15_000 · (size/4)²`. Default size 4 ($15k, the cheapest legal column); `size ∈ [4, 16]`. Each surveyed voxel returns a noisy estimate of `oil_in_place` and `permeability` (σ ≈ 0.25 / 0.30). Re-surveying is allowed and reduces variance via averaging.

### Drilling

```
drill_capex(base_capex, target_z, world_d) =
    base_capex · (1 + (target_z / world_d)²)
```

`base_capex` is the catalog value (50,000 for `oil_well`, 30,000 for `injection_well`). At `target_z = 0` you pay base; at the deepest legal voxel (`world_d - 1`) the capex is ~2× base.

A new completion at `(x, y, target_z)` is rejected with:
- `tile_occupied` if any built tile already sits at `(x, y)`.
- `completion_overlap` if any existing well shares `(x, y)` and its `target_z` is within 2 voxels of the new target (the 3×3×3 drainage cubes would overlap on the z-axis). Stacked completions are legal when `|Δtarget_z| ≥ 3`.

### Production

```
def well_production_bbl_day(w, world):
    pool = voxels_in_3x3x3(w.x, w.y, w.target_z)
    V_init   = sum(v.oil_in_place_bbl    for v in pool)
    V_remain = sum(v.oil_remaining_bbl   for v in pool)
    if V_init == 0 or V_remain == 0: return 0

    fraction = V_remain / V_init
    k_eff    = mean_perm(pool) / 500.0

    # Rate-based injection support. An injector "qualifies" iff it
    # shares the producer's reservoir_id AND its (x, y, target_z) sits
    # at 3D Chebyshev distance > 1 from the producer's target voxel
    # (adjacent injectors are rejected — breakthrough).
    qualifying_inj_rate = Σ injector.yesterday_rate_bbl_day  # qualifiers only
    pressure_boost      = min(0.5, qualifying_inj_rate
                                   / max(producer.yesterday_rate_bbl_day, 1))
    effective           = min(1.0, fraction + pressure_boost)

    q_potential = Q_MAX_WELL_BBL_DAY · efficiency · k_eff · effective    # Q_MAX = 200
    q_actual    = min(w.setpoint_rate_bbl_day, q_potential)

    drain pool by weights (k · v.oil_remaining)
    return q_actual
```

`efficiency ∈ [0, 1]` is the well's staffing ratio (idle wells produce 0 regardless of setpoint). On the day a well is drilled both yesterday rates are 0, so `pressure_boost = 0` that day — support kicks in the *next* day.

### Injection

```
def well_injection(iw, prev_balance):
    baseline_kw = iw.setpoint_rate_bbl_day · INJECTION_KWH_PER_BBL / 24 · efficiency
    cap_kw      = Q_MAX_WELL_BBL_DAY · INJECTION_KWH_PER_BBL / 24 · efficiency

    if prev_balance in ("brownout", "blackout"):
        power_kw = 0                              # demand-response shed
    elif prev_balance == "curtailment":
        power_kw = min(2 · baseline_kw, cap_kw)   # absorb surplus renewables
    else:
        power_kw = baseline_kw

    q_bbl = power_kw / INJECTION_KWH_PER_BBL
    iw.cumulative_injected_bbl += q_bbl
    iw.power_kw = power_kw                         # 50 kWh/bbl baseline
    return q_bbl
```

Notes:

- The 3×3×3 pool is clipped at grid boundaries.
- Multiple wells targeting overlapping pools share the resource; order is deterministic by `well.id`.
- A well targeting non-HC rock (`V_init = 0`, `reservoir_id is None`) is wasted CAPEX — it sits silent forever and never qualifies as an injector for any producer.

## Pipelines and crude routing

Pipelines are a 4-connected network. At end-of-day routing (`world/pipelines.py`):

- Each connected component aggregates the crude produced by wells touching it.
- Refineries on the same component pull crude in priority of setpoint, capped by their throughput.
- Orphan producers (no pipeline component) sell at `state.crude_price_usd_per_bbl`.
- Orphan refineries (component with no wells) starve and produce nothing.

`state.pipeline_networks`, `state.orphan_well_ids`, `state.orphan_refinery_ids` surface the routing decisions in `/state`.

## Refinery

```
def refine(refinery, available_crude_bbl_day):
    actual = min(refinery.setpoint_rate, available_crude_bbl_day, REFINERY_MAX_BBL_DAY)
    refined_bbl       = actual · REFINERY_YIELD            # 0.85
    refinery.power_kw = actual · REFINERY_KWH_PER_BBL / 24 # 200 kWh/bbl
    refinery.co2_t_day= actual · REFINERY_CO2_PER_BBL      # 0.30 t/bbl
    return refined_bbl, actual
```

Daily oil revenue:

```
total_crude  = Σ well_production_bbl_day
crude_to_ref = routed crude consumed by refineries
crude_direct = total_crude - crude_to_ref

revenue_oil = crude_direct · state.crude_price_usd_per_bbl   # default 40
            + refined     · state.refined_price_usd_per_bbl  # default 90
```

## Carbon and emissions

```
state.carbon_price (default 25 $/t, mutable; regulatory_tightening events scale it)

COAL_CO2_T_PER_MWH       = 0.90
GAS_CO2_T_PER_MWH        = 0.40
INDUSTRIAL_CO2_T_PER_MWH = 0.30      # tied to industrial tiles' consumed power
REFINERY_CO2_PER_BBL     = 0.30

daily_emissions_t  = Σ coal_mwh_today · 0.90
                   + Σ gas_mwh_today  · 0.40
                   + Σ industrial_mwh_consumed · 0.30
                   + Σ refined_bbl_today · 0.30

daily_carbon_cost = daily_emissions_t · state.carbon_price
```

`regulatory_tightenings_applied` increments each time a regulatory tightening event fires; the carbon price multiplies by 1.5 per increment (cumulative, permanent).

## Population and happiness

Implementation: `world/population.py`. Each day:

```
capacity  = Σ housing_capacity over housing + town hall
jobs      = Σ jobs over job-providing tiles

happiness  = 1.0
            + 0.05 · park_count
            + 0.10 · parks_within_radius_2_of_houses / max(1, house_count)
            - 0.10 · yesterday_blackout_hours / 24
            - 0.03 · industrial_or_refinery_within_radius_2_of_house / max(1, house_count)
            (park-between halves the noise penalty)
happiness  = clip(happiness, 0.0, 1.5)

growth_multiplier = max(0.0, (happiness - 0.3) / 1.2)
                       # 0.3→0%, 0.5→17%, 1.0→58%, 1.5→100%

pop = world.population

if jobs ≥ pop and capacity > pop:
    growth = BASE_GROWTH_RATE · pop · growth_multiplier        # 0.025
    growth = min(growth, capacity - pop, jobs - pop)
    pop   += growth

elif capacity < pop:
    pop = max(capacity, pop - 5)               # housing exodus
elif pop > jobs:
    pop = max(jobs, pop · 0.997)               # idle out-migration (0.3%/day)
elif happiness < 0.3:
    pop = pop · 0.99                            # unhappy decline

world.population = max(0, pop)                  # population is a float
world.happiness  = happiness
```

Population is stored as a float so sub-1/day deltas accumulate; `/state` reports `int(population)`.

## Revenue, taxes, and finance

State-mutable rates (`world/state.py`):

| Field | Default | Effect |
|---|---|---|
| `daily_tax_per_capita` | 4.0 | `tax_revenue = population · rate` |
| `industrial_revenue_per_day` | 500.0 | per staffed industrial slot |
| `commercial_revenue_per_resident_per_day` | 2.0 | per resident in 5×5 area × occupancy × staffing |
| `crude_price_usd_per_bbl` | 40.0 | unrouted crude sale price |
| `refined_price_usd_per_bbl` | 90.0 | refined product sale price |
| `grid_price_retail` | 0.08 $/kWh | local power served price |
| `grid_price_export` | 0.04 $/kWh | curtailment export price |
| `blackout_penalty_hour` | 5000 | $/hour deducted while balance_state = blackout |
| `plant_fuel_cost_per_mwh` | `{coal:12, gas:30}` | fuel cost paid against served MWh |

Daily P&L (see `state.today_summary_so_far` and the `/step` summary):

```
tax_revenue        = pop · daily_tax_per_capita
power_revenue      = served_local_kwh · grid_price_retail
                   + curtailed_exported_kwh · grid_price_export
oil_revenue        = crude_direct·crude_price + refined·refined_price
industrial_revenue = staffed_industrial_slots · industrial_revenue_per_day
commercial_revenue = Σ over commercial tiles
opex               = Σ tile.opex_per_day
fuel_cost          = Σ plant.kwh_served_yesterday · fuel_cost_per_mwh[type] / 1000
carbon_cost        = daily_emissions_t · carbon_price
blackout_penalty   = blackout_hours · blackout_penalty_hour

delta              = tax + power + oil + industrial + commercial
                   - opex - fuel - carbon - blackout - capex_today
```

## Events

Implementation: `world/events.py`. Probabilities are checked once per day, before the daily simulation runs.

| Event | Daily P | Duration | Effect |
|---|---:|---|---|
| `heatwave` | 0.006 | 5 days | residential demand × 1.40 |
| `plant_failure` | 0.0028 per gas peaker, 0.0012 per coal plant | 3–7 days | affected plant outputs 0 |
| `fuel_price_shock` | 0.004 | 30 days | gas fuel × 2.5, coal fuel × 1.3 |
| `demand_surprise` | 0.006 | 10 days | industrial+commercial demand × 1.30 |
| `regulatory_tightening` | 0.002 (capped at 3 occurrences) | permanent | carbon price × 1.5 (cumulative) |

Active events surface in `state.active_events`. Expired events move to `state.historical_events`. Scenarios can inject events directly into `active_events` using the same shape (see [SCENARIOS.md](SCENARIOS.md)); the day-loop's "is this event type already active?" guard prevents double-firing.

## Scoring

Implementation: `world/scoring.py`. The score is an absolute number in `[0, 100]` computed from the full per-day trace of `(treasury, population, happiness, cumulative_renewable_served_kwh, cumulative_total_served_kwh)` — no reference agent, no relative comparison.

Three axes (treasury, population, happiness) each decompose into a level / trend / trough triple. Three extra terms (renewable share, solvency, longevity) round out the headline.

```
# Per-day utility maps in [0, 1]
u_t(day) = 0.5 · (1 + tanh((treasury - starting_cash) / TREASURY_SCALE))   # 1_000_000
u_p(day) = min(1, population / POP_TARGET)                                  # 400 (completion target)
u_h(day) = clip(happiness, 0, HAPPINESS_CEIL) / HAPPINESS_CEIL              # 1.2

# Per-axis: level = mean over the run.
level_X = mean(u_X over all days)

# Per-axis: trend. The max() lifts a signal parked at its ceiling out of
# the zero-slope/zero-CAGR fixed point. Treasury uses the RECENT-window
# slope (last n//4 days), so a late collapse is not hidden by an early climb.
trend_treasury = max( 0.5 + 0.5·tanh(linear_slope(last n//4 of (treasury - starting_cash)) / 2_740),
                      mean(u_t over last n//4 days) )
trend_pop      = max( 0.5 + 0.5·tanh(daily_CAGR(population) / 0.003),
                      mean(u_p over last n//4 days) )
trend_happy    = max( 0.5 + 0.5·tanh(daily_CAGR(happiness)  / 0.001),
                      mean(u_h over last n//4 days) )

# Per-axis: trough = utility of the CVaR_5% (mean of the worst 5% of days).
trough_X = u_X( mean of lowest ceil(0.05·n) values of the raw signal )

# Per-axis composite.
axis_X = 0.4·level_X + 0.4·trend_X + 0.2·trough_X

# Renewable: linear ramp to RENEWABLE_TARGET = 0.5, clamped at 1.0 above.
R = min(1.0, renewable_share / 0.5)

# Solvency: fraction of days with treasury > 0.
solvency = (# days with treasury > 0) / n

# Longevity: linear ramp to full credit at LONGEVITY_TARGET_DAYS = 730
# (2 years), flat at 1.0 above.
longevity = min(1.0, n_days / 730)

# Headline. The five base terms (summing to 1.0) are scaled by
# (1 - 0.15) to make room for the additive longevity term.
base  = 0.30·axis_treasury + 0.30·axis_pop + 0.10·axis_happy + 0.20·R + 0.10·solvency
score = clip(100 · ( 0.85·base + 0.15·longevity ), 0, 100)
```

Properties:

- Bankruptcy is punished twice: the treasury level term collapses toward 0, and `solvency` drops.
- Cash-hoarding saturates: the treasury level term plateaus near 1 well below the tanh anchor.
- Trough terms (CVaR over the worst 5% of days) make late-game survival mandatory — one catastrophic stretch erodes the score even if the level looks fine on average.
- A signal pinned at its utility ceiling for the whole run still scores 1.0 on the trend term (the trailing-window fallback handles the zero-CAGR case).
- The renewable share contributes up to 20%; `happiness` and `solvency` each up to 10%; `longevity` (survival to the 2-year horizon) adds up to 15% on top.

`GET /score` reads the on-disk `runs/{run_id}/states.jsonl` trace and computes the score from it directly — there is no reference agent and no baseline lookup.

## Determinism contract

A run is fully determined by `(seed, action log)`. The world threads three `numpy.random.Generator` streams from a single seed sequence:

- `sim_rng` — weather AR(1), per-hour noise, reservoir generation at reset, and seismic survey noise.
- `event_rng` — daily event sampling.
- `forecast_rng` — `GET /forecast` noise.

Reservoir generation consumes its `sim_rng` draws once, before any `/step` is taken, so the per-day weather sequence within a run is unaffected by world dimensions. Scenarios consume zero RNG draws in v1; introducing the scenario hook does not change the byte trace of a baseline-seed run.

Every API call is appended to `runs/{run_id}/actions.jsonl`; every end-of-day state is appended to `runs/{run_id}/states.jsonl`. Score a recorded run offline with `python evaluate.py --score runs/{run_id}`. `world/tests/test_determinism.py` pins the same-seed reproduction contract.

If your agent introduces non-determinism (e.g., wall-clock time, threading), be aware that the arena's regression baselines will not be reproducible against it. Use the RNG you control and pass the seed through.
