# World Contribution: Demand Response Hub

## Motivation

The original world primarily solves grid stress by adding generation. Real
power systems also manage the demand side: flexible consumers reduce or defer
load during peak periods, avoiding expensive emergency generation and outages.

## Mechanic

The Demand Response Hub is a new distributed smart-grid facility:

- CAPEX: $30,000
- OPEX: $60/day
- Automated facility with no workforce requirement.
- No road requirement.
- Each operational hub reduces civilian demand by 15% from 09:00-20:00.
- The city-wide reduction is capped at 30%.
- Residential, commercial, and industrial loads participate.
- Oil-well, injection-well, and refinery process loads do not participate.

The mechanic is deterministic and is used by both the real simulation and the
24-hour preview because it is integrated into `world.power.total_demand_kw`.

## Agent Integration

The conservative submission builds at most one Demand Response Hub after the
population reaches 180 and treasury exceeds the renewable reserve. This makes
demand flexibility a mature-city investment without risking early solvency.

## Seed 42 Results, 730 Days

| Scenario | Original conservative | With Demand Response Hub |
|---|---:|---:|
| Baseline | 71.23 | 71.49 |
| Grid stress | 71.17 | 71.55 |
| Economy stress | 69.44 | 69.44 |

The contribution improves baseline and grid-stress performance while preserving
the strong conservative agent's economy-stress score and full solvency. In the
economy-stress run, population never reaches the agent's mature-city threshold,
so it correctly avoids the discretionary investment.

## Implementation

- `world/demand_response.py`: isolated demand-response policy and constants.
- `world/catalog.py`: buildable facility specification.
- `world/power.py`: shared simulation/preview demand hook.
- `world/ui/app.js`: map color and build-menu visibility.
- `world/tests/test_demand_response.py`: catalog, peak/off-peak, and stacking
  regression tests.
