"""Tests for the LangGraph reference agent's 5-node graph + rule critic.

LangGraph is an OPTIONAL dependency (declared under
`[project.optional-dependencies.llm]`). When it isn't installed, the
whole module skips — AFK CI without the extra installed still passes.

Coverage:
- One unit test per critic rule (2 tests) — pure functions, no graph.
- `_route_after_critique` returns the `plan` target on full rejection.
- `_route_after_critique` routes forward to `execute` on partial rejection.
- The 1-retry cap is honored (second full rejection proceeds to execute).
- Rejection reasons appear in the user message on the re-plan pass.
- `_execute` silently skips unknown tool names.
- Day 0 lays the `OPENING_BOOK` with no LLM call; the layout applies
  in full against a starter-grid world.
- Deterministic policy: renewable-drought throttle (sun AND wind low),
  coal-failure oil shutdown (production only, week-long, anchored).
- One MockLLM-driven end-to-end smoke test that reaches game_days.
- The CLI raises when the provider's API key is missing (same as ReAct).
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")  # noqa: E402  — skip whole module if missing.

from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.api_client import ApiClient
from agents.langgraph_agent import LangGraphAgent
from agents.langgraph_agent.agent import (
    OIL_SHUTDOWN_DAYS,
    OPENING_BOOK,
    OPENING_STEP_DAYS,
    _coal_plant_failed,
    _deterministic_policy,
    _renewables_low,
    out_of_bounds,
    tile_occupied,
)
from agents.llm import LLMResponse, MockLLM, ToolCall, Usage
from world.api import create_app
from world.sim import World


def _make_client(world: World | None = None) -> tuple[ApiClient, World]:
    w = world or World()
    return ApiClient(transport=TestClient(create_app(world=w))), w


def _resp(tool_calls: list[ToolCall], *, in_tok: int = 5, out_tok: int = 2) -> LLMResponse:
    return LLMResponse(tool_calls=tool_calls, text="", usage=Usage(in_tok, out_tok))


def _step_only_mock() -> MockLLM:
    return MockLLM(responses=[_resp([ToolCall("step", {"days": 7})])])


# ---------- Critic rules (pure functions) ---------------------------------


def test_rule_out_of_bounds_rejects_negative_or_oversize_coords() -> None:
    state_view = {"config": {"world_w": 16, "world_h": 16}}
    reason = out_of_bounds(ToolCall("build", {"tile_type": "road", "x": 20, "y": 5}), state_view)
    assert reason is not None and "out_of_bounds" in reason
    # In-bounds is None.
    assert (
        out_of_bounds(ToolCall("build", {"tile_type": "road", "x": 5, "y": 5}), state_view) is None
    )
    # Rule ignores non-coord-bearing tools.
    assert out_of_bounds(ToolCall("set_well_rate", {"well_id": "w-1"}), state_view) is None


def test_rule_tile_occupied_rejects_build_on_existing_tile() -> None:
    state_view = {
        "config": {"world_w": 16, "world_h": 16},
        "tiles": [{"x": 5, "y": 5, "type": "house"}],
    }
    reason = tile_occupied(ToolCall("build", {"tile_type": "road", "x": 5, "y": 5}), state_view)
    assert reason is not None and "tile_occupied" in reason
    assert (
        tile_occupied(ToolCall("build", {"tile_type": "road", "x": 6, "y": 5}), state_view) is None
    )
    # Non-build calls bypass the rule.
    assert tile_occupied(ToolCall("survey", {"x": 5, "y": 5}), state_view) is None


# ---------- Routing -------------------------------------------------------


def test_critique_back_edge_fires_on_full_rejection() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    obs = api.state()
    # Single mutator call that the critic will reject (out_of_bounds).
    out = agent._critique(
        {
            "pending_calls": [ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})],
            "obs": obs,
        }
    )
    assert out["survivors"] == []
    assert any("out_of_bounds" in r for r in out["rejections"])
    route = agent._route_after_critique(
        {
            "pending_calls": [ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})],
            "survivors": out["survivors"],
            "rejections": out["rejections"],
            "replan_retries": 0,
        }
    )
    assert route == "plan"


def test_critique_routes_forward_to_execute_on_partial_rejection() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    obs = api.state()
    th = next(t for t in obs["tiles"] if t["type"] == "town_hall")
    pending = [
        ToolCall("build", {"tile_type": "road", "x": th["x"] + 1, "y": th["y"]}),  # OK
        ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999}),  # rejected
    ]
    out = agent._critique({"pending_calls": pending, "obs": obs})
    assert len(out["survivors"]) == 1
    assert out["survivors"][0].arguments["x"] == th["x"] + 1
    assert len(out["rejections"]) == 1
    route = agent._route_after_critique(
        {
            "pending_calls": pending,
            "survivors": out["survivors"],
            "rejections": out["rejections"],
            "replan_retries": 0,
        }
    )
    assert route == "execute"


def test_replan_cap_of_one_is_honored() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    obs = api.state()
    pending = [ToolCall("build", {"tile_type": "road", "x": 9999, "y": 9999})]
    out = agent._critique({"pending_calls": pending, "obs": obs})
    # Already retried once — must route forward to execute even though
    # this critique was a full rejection.
    route = agent._route_after_critique(
        {
            "pending_calls": pending,
            "survivors": out["survivors"],
            "rejections": out["rejections"],
            "replan_retries": 1,
        }
    )
    assert route == "execute"


def test_rejection_reasons_appear_in_replan_user_message() -> None:
    api, _ = _make_client()
    api.reset(seed=42)
    obs = api.state()
    captured: dict[str, str] = {}

    class CapturingMock(MockLLM):
        def chat(
            self,
            *,
            system: str,
            user: str,
            tools: list[dict[str, Any]],
            max_tokens: int = 2048,
        ) -> LLMResponse:
            captured["user"] = user
            return super().chat(system=system, user=user, tools=tools, max_tokens=max_tokens)

    mock = CapturingMock(responses=[_resp([ToolCall("step", {"days": 1})])])
    agent = LangGraphAgent(api, seed=42, llm=mock)
    from agents.langgraph_agent.agent import GraphState

    state: GraphState = {
        "obs": obs,
        "forecast": None,
        "day": 5,  # day > 0 so _plan calls the LLM (day 0 is deterministic).
        "game_days": 14,
        "cumulative_tokens": 0,
        "turn": 0,
        "rejections": ["build(road,9999,9999) out_of_bounds (world 16x16)"],
        "replan_retries": 0,
        "oil_shutdown_until_day": 0,
    }
    agent._plan(state)
    assert "out_of_bounds" in captured["user"]
    assert "ALL rejected" in captured["user"]


def test_execute_silently_skips_unknown_tool_names() -> None:
    api, _ = _make_client()
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    api.reset(seed=42)
    pre_tile_count = len(api.state()["tiles"])
    agent._execute({"survivors": [ToolCall("hallucinate", {"foo": "bar"})]})
    # No crash, no state change.
    assert len(api.state()["tiles"]) == pre_tile_count


# ---------- Day-0 opening book --------------------------------------------


def test_day0_lays_opening_book_without_calling_the_llm() -> None:
    api, _ = _make_client()
    api.reset(seed=42)
    obs = api.state()
    mock = MockLLM(responses=[])  # .calls stays empty unless chat() is hit.
    agent = LangGraphAgent(api, seed=42, llm=mock)
    out = agent._plan(
        {
            "day": 0,
            "obs": obs,
            "forecast": None,
            "game_days": 30,
            "turn": 0,
            "replan_retries": 0,
            "oil_shutdown_until_day": 0,
        }
    )
    assert out["pending_calls"] == OPENING_BOOK
    assert out["step_days"] == OPENING_STEP_DAYS
    assert mock.calls == []  # day 0 is fully deterministic — no LLM call.


def test_opening_book_builds_apply_against_a_live_world() -> None:
    """Every OPENING_BOOK coordinate is in-bounds, unoccupied, and (for the
    road-dependent tiles) road-connected, so all of them apply server-side."""
    # Use the starter-grid world the real server seeds (world/api.py), so the
    # industrial tile at (9, 15) connects via the starter road at (9, 16).
    api = ApiClient(transport=TestClient(create_app(world=World(seed_starter_grid=True))))
    api.reset(seed=42)
    before = len(api.state()["tiles"])
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    # Run the opening book through the real critique + execute path.
    crit = agent._critique({"pending_calls": list(OPENING_BOOK), "obs": api.state()})
    assert crit["rejections"] == []  # critic passes the whole layout
    agent._execute({"survivors": crit["survivors"], "forced_calls": []})
    assert len(api.state()["tiles"]) == before + len(OPENING_BOOK)


def test_attach_act_lays_opening_book_on_day0() -> None:
    """Attach mode (UI `/step` → `act`) must lay the same opening as the
    graph — this is the path a running world actually drives."""
    api = ApiClient(transport=TestClient(create_app(world=World(seed_starter_grid=True))))
    api.reset(seed=42)
    before = len(api.state()["tiles"])
    agent = LangGraphAgent(api, seed=42, llm=_step_only_mock())
    skip = agent.act(api.state())  # day 0
    assert skip is None  # never steps in attach mode
    assert len(api.state()["tiles"]) == before + len(OPENING_BOOK)
    # Idempotent while still day 0: the second act runs the LLM turn (which
    # the step-only mock makes a no-op) and does not re-lay the opening.
    agent.act(api.state())
    assert len(api.state()["tiles"]) == before + len(OPENING_BOOK)


# ---------- Deterministic policy: renewable drought + coal failure --------


def _wells() -> list[dict[str, Any]]:
    return [
        {"id": "production-1", "type": "production", "setpoint_rate_bbl_day": 150.0},
        {"id": "injection-1", "type": "injection", "setpoint_rate_bbl_day": 100.0},
    ]


def _low_forecast() -> list[dict[str, Any]]:
    # Overcast (peak 0.2) and near-calm (2 m/s) for the next 24h.
    return [{"hour_offset": h, "solar_irradiance": 0.2, "wind_speed_mps": 2.0} for h in range(24)]


def _windy_forecast() -> list[dict[str, Any]]:
    return [{"hour_offset": h, "solar_irradiance": 0.2, "wind_speed_mps": 10.0} for h in range(24)]


def test_renewables_low_only_when_both_sun_and_wind_weak() -> None:
    assert _renewables_low(_low_forecast()) is True
    assert _renewables_low(_windy_forecast()) is False  # wind saves it
    # Sunny midday peak saves it even if the 24h mean solar is low.
    sunny = [
        {"solar_irradiance": 0.9 if h == 12 else 0.0, "wind_speed_mps": 2.0} for h in range(24)
    ]
    assert _renewables_low(sunny) is False
    assert _renewables_low(None) is False


def test_drought_throttles_all_wells_and_refineries() -> None:
    obs = {
        "wells": _wells(),
        "tiles": [{"id": "refinery-1", "type": "refinery", "setpoint_rate_bbl_day": 200.0}],
    }
    forced, shutdown_until = _deterministic_policy(obs, _low_forecast(), day=5, shutdown_until=0)
    parked = {
        (c.name, c.arguments["well_id"] if "well_id" in c.arguments else c.arguments["refinery_id"])
        for c in forced
    }
    assert ("set_well_rate", "production-1") in parked
    assert ("set_well_rate", "injection-1") in parked  # drought parks injectors too
    assert ("set_refinery_rate", "refinery-1") in parked
    assert all(c.arguments["rate_bbl_day"] == 0.0 for c in forced)
    assert shutdown_until == 0  # no coal failure → no shutdown window


def test_drought_is_idempotent_for_already_parked_loads() -> None:
    obs = {
        "wells": [{"id": "production-1", "type": "production", "setpoint_rate_bbl_day": 0.0}],
        "tiles": [{"id": "refinery-1", "type": "refinery", "setpoint_rate_bbl_day": 0.0}],
    }
    forced, _ = _deterministic_policy(obs, _low_forecast(), day=5, shutdown_until=0)
    assert forced == []  # nothing to do — already at 0


def _coal_failure_obs() -> dict[str, Any]:
    return {
        "wells": _wells(),
        "tiles": [{"id": "coal_plant-1", "type": "coal_plant"}],
        "active_events": [
            {"type": "plant_failure", "plant_id": "coal_plant-1", "started_day": 10, "ends_day": 15}
        ],
    }


def test_coal_failed_detects_only_coal_plant_failures() -> None:
    assert _coal_plant_failed(_coal_failure_obs()) is True
    # A gas-peaker failure must NOT trigger the coal rule.
    gas = {
        "tiles": [{"id": "gas_peaker-1", "type": "gas_peaker"}],
        "active_events": [{"type": "plant_failure", "plant_id": "gas_peaker-1"}],
    }
    assert _coal_plant_failed(gas) is False


def test_coal_failure_shuts_oil_extraction_for_a_week() -> None:
    # No forecast drought, so only the coal rule fires: production parked,
    # injection left alone, and a week-long window opened.
    forced, shutdown_until = _deterministic_policy(
        _coal_failure_obs(), None, day=10, shutdown_until=0
    )
    assert shutdown_until == 10 + OIL_SHUTDOWN_DAYS
    well_ids = {c.arguments["well_id"] for c in forced if c.name == "set_well_rate"}
    assert well_ids == {"production-1"}  # injection-1 keeps running


def test_shutdown_window_persists_and_is_not_re_armed_while_active() -> None:
    # Mid-window (day 12 < 17), coal still failing: production stays parked
    # but the deadline is NOT pushed out — the week is anchored to the start.
    forced, shutdown_until = _deterministic_policy(
        _coal_failure_obs(), None, day=12, shutdown_until=17
    )
    assert shutdown_until == 17
    assert {c.arguments["well_id"] for c in forced if c.name == "set_well_rate"} == {"production-1"}


# ---------- End-to-end smoke ----------------------------------------------


def test_short_game_runs_to_completion_with_mock_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAME_DAYS", "14")
    monkeypatch.setenv("MANUAL_GAME_DAYS", "14")
    api = ApiClient(transport=TestClient(create_app(world=World())))
    # (25, 25) is empty and outside the day-0 OPENING_BOOK footprint, so the
    # critic passes it through and no re-plan is triggered. Day 0 lays the
    # opening book with no LLM call, so the two responses below are consumed
    # by the day-1 and day-8 turns.
    plan: list[Any] = [
        _resp(
            [
                ToolCall("build", {"tile_type": "road", "x": 25, "y": 25}),
                ToolCall("step", {"days": 7}),
            ]
        ),
        _resp([ToolCall("step", {"days": 7})]),
    ]
    mock = MockLLM(responses=plan)
    agent = LangGraphAgent(api, seed=42, llm=mock)
    final = agent.play_game()
    assert final["day"] == 14
    assert agent.turns >= 1
    assert agent.cumulative_tokens > 0


_LLM_ENV_VARS = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "NVIDIA_API_KEY",
    "NVIDIA_BASE_URL",
    "NVIDIA_MODEL",
    "OLLAMA_BASE_URL",
    "OLLAMA_MODEL",
    "NIM_BASE_URL",
    "NIM_MODEL",
    "NIM_CHAT_TEMPLATE_KWARGS",
)


def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`test_nim_live.py` calls `load_dotenv` at import time, which leaks
    the user's local `.env` into `os.environ`. Tests that expect the
    openai branch (the default) must clear every var that would route
    the factory elsewhere — otherwise the assertion silently fails."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_agent_requires_llm_when_env_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    api, _ = _make_client()
    _isolate_llm_env(monkeypatch)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        LangGraphAgent(api, seed=42)


def test_cli_raises_without_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No MockLLM offline fallback — running without a key must raise."""
    from agents.langgraph_agent import agent as agent_module

    _isolate_llm_env(monkeypatch)
    # `main` mutates os.environ; monkeypatch restores it after the test
    # so the GAME_DAYS / MANUAL_GAME_DAYS knobs don't leak into the
    # scripted-agent smoke tests.
    monkeypatch.setenv("GAME_DAYS", "1")
    monkeypatch.setenv("MANUAL_GAME_DAYS", "1")
    # Patch in-process client construction so we don't accidentally hit a live URL.
    with (
        patch.object(agent_module, "_make_inprocess_client", _make_client_for_cli),
        pytest.raises(RuntimeError, match="OPENAI_API_KEY"),
    ):
        agent_module.main(["--seed", "42", "--days", "1"])


def _make_client_for_cli() -> ApiClient:
    api, _ = _make_client()
    return api
