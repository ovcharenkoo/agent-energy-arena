"""Challenge scenarios — the harder stress profiles the arena grades against.

Each module here is a single, self-contained `Scenario` subclass tuned to
hammer one strategic axis (or, for `grand_collapse`, all of them at once).
Every schedule is bounded inside the default 730-day submission horizon and
spaced with recovery gaps, so a competent agent can still post a positive
score — the scenarios are *hard*, not *unwinnable*.

Discovered the same way as the sibling top-level scenarios: a file at
``scenarios/challenge/strict_co2.py`` resolves to the dotted path
``scenarios.challenge.strict_co2``. See ``scenarios/SCENARIOS.md``.
"""
