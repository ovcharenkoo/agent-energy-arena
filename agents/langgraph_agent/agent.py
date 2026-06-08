"""LangGraph reference agent — 5-node graph with a rule-based critic.

```
START → observe → plan(LLM) → critique(rules) → execute → step → {observe | END}
                                     ↑                                  │
                                     └──── re-plan once if all dropped ─┘
```

The five nodes do five different kinds of cognitive work, and the
conditional edge from `critique` back to `plan` gates on a real
decision: did the local critic veto every proposed mutation? If so,
re-prompt the model once with the rejection reasons; otherwise advance
to `execute`. The re-plan retry is capped at 1 per turn.

Three deterministic overlays wrap the LLM (CLI / `play_game` only — the
attach path in `act()` is unchanged):

  - `OPENING_BOOK` — a fixed city layout built on day 0 with no LLM call.
  - Renewable-drought throttle — when the next-day `/forecast` shows both
    weak sun and weak wind, every well and refinery is parked at 0 for the
    turn to keep the day off fossil backup.
  - Coal-failure oil shutdown — when a coal plant fails, all production
    wells are parked at 0 for a week.

The last two live in `_deterministic_policy`; their forced setpoints
bypass the critic and are applied in `_execute` after the LLM's surviving
calls. Throttling is one-way (loads only go *down*); restoring rates is
left to the LLM, which sees the parked setpoints in the state summary.

Extension surfaces documented for hackathon participants:

  1. The module-level `RULES = [...]` list of critic functions.
     Append a new pure function `rule(call, state_view)` to add a check.
  2. `_OPENING_LAYOUT` / `_deterministic_policy` plus the thresholds
     (`SOLAR_PEAK_LOW`, `WIND_MEAN_LOW`, `OIL_SHUTDOWN_DAYS`) — tune the
     deterministic policy.
  3. The rejection-reason prompt construction inside `_plan`. Tune the
     framing the model receives on the re-plan pass.

The critic is a fast local pre-flight check, not a second source of
truth: the `World` still validates and rejects every mutation
server-side (`_execute` swallows those rejections). So the shipped
rules stay cheap and stable — they read the `/state` payload only and
never re-implement `World` economics or topology. That keeps this
file something a student can read top-to-bottom without learning
`World` internals.

CLI:
  python -m agents.langgraph_agent.agent --seed 42 --days 30   # short demo
  python -m agents.langgraph_agent.agent --seed 42 --full      # full game

Requires the active provider's API key (e.g. `ANTHROPIC_API_KEY`) —
same contract as the ReAct CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from agents.api_client import ApiClient
from agents.attach_runtime import drive_one_turn
from agents.base import BaseAgent
from agents.llm import LLMClient, ToolCall, make_llm_from_env
from agents.prompts import ACTION_TOOLS, SYSTEM_PROMPT
from agents.state_summary import summarize_state
from agents.tool_dispatch import dispatch_tool_call

DEFAULT_STEP_DAYS_FALLBACK: int = 7
MAX_TOKENS_PER_TURN: int = 2048
FORECAST_HOURS: int = 24
MAX_REPLAN_RETRIES: int = 1

# Deterministic-policy knobs (see `_deterministic_policy` + `OPENING_BOOK`).
OPENING_STEP_DAYS: int = (
    1  # step after laying the day-0 opening, so the LLM re-plans the morning after.
)
OIL_SHUTDOWN_DAYS: int = 7  # length of the post-coal-failure oil-extraction shutdown ("a week").
THROTTLED_RATE_BBL_DAY: float = 0.0  # setpoint a throttled well/refinery is parked at.
SOLAR_PEAK_LOW: float = 0.5  # next-day peak irradiance below this reads as an overcast day.
WIND_MEAN_LOW: float = 4.0  # next-day mean wind (m/s) below this is near the ~3 m/s turbine cut-in.

MUTATOR_TOOLS: frozenset[str] = frozenset(
    {"build", "demolish", "survey", "drill", "set_well_rate", "set_refinery_rate"}
)

# ---------- Day-0 opening book ---------------------------------------------
#
# A fixed city layout laid down deterministically on day 0 (no LLM call).
# Edit `_OPENING_LAYOUT` to change the opening; roads are listed before the
# tiles that need road adjacency, and `_execute` dispatches in order, so the
# network exists by the time the houses/commercial/industrial land. Every
# coordinate is validated against the live world by `_critique` (bounds +
# occupancy) and by `World.build` server-side, so a bad entry is dropped, not
# fatal.
_OPENING_LAYOUT: tuple[tuple[str, int, int], ...] = (
    ("road", 16, 15),
    ("road", 16, 14),
    ("road", 16, 13),
    ("road", 16, 12),
    ("road", 17, 16),
    ("road", 18, 16),
    ("road", 19, 16),
    ("road", 19, 14),
    ("road", 19, 15),
    ("road", 19, 13),
    ("road", 19, 12),
    ("house", 17, 15),
    ("house", 18, 15),
    ("house", 18, 14),
    ("house", 18, 13),
    ("house", 17, 13),
    ("park", 17, 12),
    ("park", 18, 12),
    ("commercial", 17, 14),
    ("wind_turbine", 1, 30),
    ("wind_turbine", 3, 30),
    ("industrial", 9, 15),
    ("road", 19, 11),
    ("road", 19, 10),
)
OPENING_BOOK: list[ToolCall] = [
    ToolCall("build", {"tile_type": t, "x": x, "y": y}) for (t, x, y) in _OPENING_LAYOUT
]


class GraphState(TypedDict, total=False):
    """Per-turn state that flows through the LangGraph nodes."""

    day: int
    game_days: int
    obs: dict[str, Any]
    forecast: list[dict[str, Any]] | None
    pending_calls: list[ToolCall]
    survivors: list[ToolCall]
    forced_calls: list[ToolCall]
    rejections: list[str]
    step_days: int
    cumulative_tokens: int
    turn: int
    replan_retries: int
    oil_shutdown_until_day: int


# ---------- Critic rules ---------------------------------------------------
#
# Each rule is a pure function: given the proposed `ToolCall` and the
# world `state_view` (the parsed `/state` payload it would mutate),
# return a rejection reason string or `None` to let the call through.
# The `RULES = [...]` list below is the documented extension surface —
# append your own rule to add a check.

RuleFn = Callable[[ToolCall, dict[str, Any]], "str | None"]


def out_of_bounds(call: ToolCall, state_view: dict[str, Any]) -> str | None:
    """Reject build/demolish/survey/drill calls with an (x, y) outside the world."""
    if call.name not in {"build", "demolish", "survey", "drill"}:
        return None
    cfg = state_view.get("config") or {}
    w = int(cfg.get("world_w", 0))
    h = int(cfg.get("world_h", 0))
    try:
        x = int(call.arguments["x"])
        y = int(call.arguments["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if 0 <= x < w and 0 <= y < h:
        return None
    return f"{call.name}({x},{y}) out_of_bounds (world {w}x{h})"


def tile_occupied(call: ToolCall, state_view: dict[str, Any]) -> str | None:
    """Reject `build` calls onto an already-occupied (x, y) surface tile."""
    if call.name != "build":
        return None
    try:
        x = int(call.arguments["x"])
        y = int(call.arguments["y"])
    except (KeyError, TypeError, ValueError):
        return None
    for t in state_view.get("tiles") or []:
        if t.get("x") == x and t.get("y") == y:
            tile_type = call.arguments.get("tile_type")
            return f"build({tile_type},{x},{y}) tile_occupied by {t.get('type')}"
    return None


RULES: list[RuleFn] = [out_of_bounds, tile_occupied]


# ---------- Deterministic policy -------------------------------------------
#
# Two rule-based overrides that bypass the LLM and the critic: they read the
# `/state` + `/forecast` snapshot and force well/refinery setpoints. The LLM
# still proposes builds and step size; these only ever turn power-hungry oil
# loads *down*. See `_deterministic_policy` for how they compose.


def _renewables_low(forecast: list[dict[str, Any]] | None) -> bool:
    """True when the next-day forecast shows BOTH weak sun and weak wind.

    `solar_irradiance` is 0 overnight, so we gate on the daytime *peak*
    (max over the horizon) — a peak below `SOLAR_PEAK_LOW` means an
    overcast day. Wind gates on the horizon *mean*; below `WIND_MEAN_LOW`
    (~the 3 m/s turbine cut-in) the turbines barely turn. Both must be low
    to throttle: a windy-but-cloudy day still has renewable headroom."""
    if not forecast:
        return False
    solar = [float(f.get("solar_irradiance", 0.0)) for f in forecast]
    wind = [float(f.get("wind_speed_mps", 0.0)) for f in forecast]
    if not solar or not wind:
        return False
    return max(solar) < SOLAR_PEAK_LOW and (sum(wind) / len(wind)) < WIND_MEAN_LOW


def _coal_plant_failed(obs: dict[str, Any]) -> bool:
    """True when an active `plant_failure` event names a coal-plant tile.

    The event payload carries only `plant_id` (not the plant type), so we
    resolve it against `/state.tiles`. Detection is by *active failure*,
    not `started_day == today`: the agent may advance several days per
    turn and would otherwise miss a failure that began mid-window."""
    tiles = obs.get("tiles") or []
    coal_ids = {t.get("id") for t in tiles if t.get("type") == "coal_plant"}
    if not coal_ids:
        return False
    return any(
        e.get("type") == "plant_failure" and e.get("plant_id") in coal_ids
        for e in obs.get("active_events") or []
    )


def _deterministic_policy(
    obs: dict[str, Any],
    forecast: list[dict[str, Any]] | None,
    day: int,
    shutdown_until: int,
) -> tuple[list[ToolCall], int]:
    """Forced well/refinery setpoints from two rules, plus the (possibly
    extended) oil-shutdown deadline.

    Rule C (coal failure → shut oil for a week): when a coal plant is down
    and we're not already inside a shutdown window, park every *production*
    well at 0 for `OIL_SHUTDOWN_DAYS`. The window is re-armed every turn it
    stays active, so it survives multi-day steps; it auto-expires once
    `day >= shutdown_until`.

    Rule B (renewable drought → cut discretionary load): when BOTH sun and
    wind are forecast low, park every well *and* refinery at 0 for the
    turn. These are the grid's biggest controllable loads (injection 50,
    production 15, refinery 200 kWh/bbl), so parking them keeps a
    low-renewable day off fossil backup.

    Throttling is one-way — the policy only turns loads *down*. Restoring
    rates after the sky clears or the week ends is left to the LLM, which
    sees the parked setpoints in the state summary. Setpoints already at
    target are skipped, so a long shutdown doesn't spam the action log."""
    wells = obs.get("wells") or []
    refineries = [t for t in (obs.get("tiles") or []) if t.get("type") == "refinery"]

    if _coal_plant_failed(obs) and day >= shutdown_until:
        shutdown_until = day + OIL_SHUTDOWN_DAYS
    oil_shut = day < shutdown_until
    renewables_low = _renewables_low(forecast)

    well_targets: dict[str, float] = {}
    if oil_shut:
        for w in wells:
            if w.get("type") == "production":
                well_targets[str(w.get("id"))] = THROTTLED_RATE_BBL_DAY
    if renewables_low:
        for w in wells:
            well_targets[str(w.get("id"))] = THROTTLED_RATE_BBL_DAY  # incl. injection

    refinery_targets: dict[str, float] = {}
    if renewables_low:
        for r in refineries:
            refinery_targets[str(r.get("id"))] = THROTTLED_RATE_BBL_DAY

    forced: list[ToolCall] = []
    cur_well = {str(w.get("id")): w.get("setpoint_rate_bbl_day") for w in wells}
    for wid, rate in well_targets.items():
        if cur_well.get(wid) != rate:
            forced.append(ToolCall("set_well_rate", {"well_id": wid, "rate_bbl_day": rate}))
    cur_ref = {str(r.get("id")): r.get("setpoint_rate_bbl_day") for r in refineries}
    for rid, rate in refinery_targets.items():
        if cur_ref.get(rid) != rate:
            forced.append(ToolCall("set_refinery_rate", {"refinery_id": rid, "rate_bbl_day": rate}))

    return forced, shutdown_until


# ---------- Agent ----------------------------------------------------------


class LangGraphAgent(BaseAgent):
    """Graph-based reference agent. Implements the `Agent` protocol
    (`__init__(api, *, seed=None)` + `play_game() -> dict`).

    Subclasses `BaseAgent` so the Agent Play attach handler accepts it.
    The compiled graph only runs in CLI mode (`play_game()`); attach mode
    (`act(state)`, driven by the UI's `/step`) delegates the LLM turn to
    the shared attach runtime. The three deterministic overlays — the
    day-0 `OPENING_BOOK`, the renewable-drought throttle, and the
    coal-failure oil shutdown — run in BOTH modes, so an attached agent
    lays the same opening and applies the same policy as the CLI run.
    """

    def __init__(
        self,
        api: ApiClient,
        *,
        seed: int | None = None,
        llm: LLMClient | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        action_tools: list[dict[str, Any]] | None = None,
        max_tokens_per_turn: int = MAX_TOKENS_PER_TURN,
    ) -> None:
        self.api = api
        self._seed = seed
        self.llm: LLMClient = llm if llm is not None else make_llm_from_env()
        self.system_prompt: str = system_prompt
        self.action_tools: list[dict[str, Any]] = action_tools or ACTION_TOOLS
        self.max_tokens_per_turn: int = max_tokens_per_turn
        self.cumulative_tokens: int = 0
        self.turns: int = 0
        self.final_score: dict[str, Any] | None = None
        # Attach-mode policy state (the graph threads its own copies through
        # GraphState; attach mode keeps them on the instance instead).
        self._opening_done: bool = False
        self._oil_shutdown_until_day: int = 0
        self.graph = self._build_graph()

    # -- Attach hook ------------------------------------------------------

    def act(self, state: dict[str, Any]) -> int | None:
        """Per-`/step` attach hook. Runs the same deterministic overlays as
        the graph: lay the day-0 opening once (no LLM call), then on later
        days take the LLM turn and apply the forced well/refinery throttles
        on top. `api.step` is forbidden in attach mode, so the day-0 path
        returns `None` ("wake me next step") rather than advancing time."""
        day = int(state.get("day", 0))
        forecast = _safe_forecast(self.api)

        # Day 0: lay the opening book deterministically, exactly once.
        if day == 0 and not self._opening_done:
            self._opening_done = True
            self._dispatch_all(OPENING_BOOK)
            return None

        usage, skip_days = drive_one_turn(
            self.api,
            state,
            self.llm,
            system_prompt=self.system_prompt,
            action_tools=self.action_tools,
            max_tokens=self.max_tokens_per_turn,
        )
        self.cumulative_tokens += usage.total

        # Apply deterministic throttles AFTER the LLM turn so they override
        # any well/refinery rate the model just set.
        forced, self._oil_shutdown_until_day = _deterministic_policy(
            state, forecast, day, self._oil_shutdown_until_day
        )
        self._dispatch_all(forced)
        return skip_days

    def _dispatch_all(self, calls: list[ToolCall]) -> None:
        """Dispatch each call, swallowing unknown names, world-side
        rejections, and malformed args so one bad call doesn't crash the
        turn. Shared by `act` (attach) and `_execute` (graph)."""
        for call in calls:
            try:
                dispatch_tool_call(self.api, call)
            except (RuntimeError, KeyError, TypeError, ValueError):
                continue

    # -- Graph construction ----------------------------------------------

    def _build_graph(self) -> Any:
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:
            raise RuntimeError(
                "langgraph is not installed — install the optional 'llm' extra: "
                'pip install -e ".[llm]"'
            ) from exc

        g = StateGraph(GraphState)
        g.add_node("observe", self._observe)
        g.add_node("plan", self._plan)
        g.add_node("critique", self._critique)
        g.add_node("execute", self._execute)
        g.add_node("step", self._step)

        g.add_edge(START, "observe")
        g.add_edge("observe", "plan")
        g.add_edge("plan", "critique")
        g.add_conditional_edges(
            "critique",
            self._route_after_critique,
            {"plan": "plan", "execute": "execute"},
        )
        g.add_edge("execute", "step")
        g.add_conditional_edges(
            "step",
            self._route_after_step,
            {"observe": "observe", "end": END},
        )
        return g.compile()

    # -- Public entry ----------------------------------------------------

    def play_game(self) -> dict[str, Any]:
        """Reset, invoke the graph, then fetch /score for the CLI summary."""
        self.api.reset(seed=self._seed)
        initial_state = self.api.state()
        game_days = int(
            initial_state["config"].get("active_game_days", initial_state["config"]["game_days"])
        )

        # Recursion limit: roughly (turns × nodes_per_turn) + slack.
        # nodes_per_turn ≈ 6 (observe / plan / critique / execute / step
        # / loop), with one extra plan visit per re-plan retry.
        recursion_limit = max(50, (game_days + 7) * 10)

        final: GraphState = self.graph.invoke(
            {
                "day": int(initial_state.get("day", 0)),
                "game_days": game_days,
                "cumulative_tokens": 0,
                "turn": 0,
                "replan_retries": 0,
                "oil_shutdown_until_day": 0,
            },
            config={"recursion_limit": recursion_limit},
        )

        self.cumulative_tokens = int(final.get("cumulative_tokens", 0))
        self.turns = int(final.get("turn", 0))

        try:
            self.final_score = self.api.score()
        except RuntimeError:
            self.final_score = None

        end_state: dict[str, Any] = final.get("obs") or self.api.state()
        return end_state

    # -- Nodes -----------------------------------------------------------

    def _observe(self, state: GraphState) -> GraphState:
        """Snapshot `/state` + `/forecast`. Resets per-turn rejection state."""
        obs = self.api.state()
        forecast = _safe_forecast(self.api)
        return {
            "obs": obs,
            "forecast": forecast,
            "day": int(obs.get("day", state.get("day", 0))),
            "rejections": [],
            "replan_retries": 0,
        }

    def _plan(self, state: GraphState) -> GraphState:
        """Propose this turn's calls. Two deterministic overlays wrap the
        LLM here:

          - Day 0 lays the fixed `OPENING_BOOK` with no LLM call at all.
          - Every turn, `_deterministic_policy` computes `forced_calls`
            (well/refinery throttles) that bypass the critic in `_execute`.

        On a re-plan pass, the rejection reasons from `critique` are
        prepended so the model sees what the local critic vetoed."""
        day = int(state.get("day", 0))
        obs = state.get("obs") or {}
        forecast = state.get("forecast")
        game_days = int(state.get("game_days", 0))
        retries = int(state.get("replan_retries", 0))

        forced, shutdown_until = _deterministic_policy(
            obs, forecast, day, int(state.get("oil_shutdown_until_day", 0))
        )

        # Day 0: lay the opening book deterministically — skip the LLM.
        if day == 0:
            return {
                "pending_calls": list(OPENING_BOOK),
                "forced_calls": forced,
                "step_days": min(OPENING_STEP_DAYS, max(1, game_days - day)),
                "turn": int(state.get("turn", 0)) + 1,
                "rejections": [],
                "replan_retries": retries,
                "oil_shutdown_until_day": shutdown_until,
            }

        user_msg = summarize_state(obs, forecast)
        rejections = state.get("rejections") or []
        if rejections:
            bullets = "\n".join(f"- {r}" for r in rejections)
            user_msg = (
                "Your previous tool calls were ALL rejected by the local critic:\n"
                f"{bullets}\n\nRevise the plan to avoid these failure modes.\n\n" + user_msg
            )

        response = self.llm.chat(
            system=self.system_prompt,
            user=user_msg,
            tools=self.action_tools,
            max_tokens=self.max_tokens_per_turn,
        )

        pending: list[ToolCall] = []
        step_days = DEFAULT_STEP_DAYS_FALLBACK
        for call in response.tool_calls:
            if call.name == "step":
                step_days = _clamp_days(call.arguments.get("days", DEFAULT_STEP_DAYS_FALLBACK))
                break  # step terminates the turn — ignore anything after it
            pending.append(call)

        remaining = max(1, game_days - day)
        step_days = min(step_days, remaining)

        if rejections:
            retries += 1

        return {
            "pending_calls": pending,
            "forced_calls": forced,
            "step_days": step_days,
            "cumulative_tokens": int(state.get("cumulative_tokens", 0)) + response.usage.total,
            "turn": int(state.get("turn", 0)) + 1,
            "rejections": [],
            "replan_retries": retries,
            "oil_shutdown_until_day": shutdown_until,
        }

    def _critique(self, state: GraphState) -> GraphState:
        """Per-call gate. Walks each proposed mutator through `RULES`.
        Calls with non-mutator names are passed through to `execute`,
        which drops them via `dispatch_tool_call` returning `None`
        (defensive against LLM hallucination)."""
        pending = state.get("pending_calls") or []
        state_view = state.get("obs") or {}
        survivors: list[ToolCall] = []
        rejections: list[str] = []
        for call in pending:
            if call.name not in MUTATOR_TOOLS:
                survivors.append(call)
                continue
            reason: str | None = None
            for rule in RULES:
                r = rule(call, state_view)
                if r is not None:
                    reason = r
                    break
            if reason is not None:
                rejections.append(reason)
                continue
            survivors.append(call)
        return {"survivors": survivors, "rejections": rejections}

    def _route_after_critique(self, state: GraphState) -> str:
        """Back-edge to `plan` if every mutator was rejected and we
        haven't already retried this turn; forward to `execute`
        otherwise."""
        pending = state.get("pending_calls") or []
        survivors = state.get("survivors") or []
        rejections = state.get("rejections") or []
        retries = int(state.get("replan_retries", 0))
        full_reject = bool(pending) and not survivors and bool(rejections)
        if full_reject and retries < MAX_REPLAN_RETRIES:
            return "plan"
        return "execute"

    def _execute(self, state: GraphState) -> GraphState:
        """Dispatch each survivor, then the deterministic `forced_calls`,
        through the shared `dispatch_tool_call`. Survivors run first so an
        LLM `set_well_rate` is overridden by a policy throttle on the same
        well. Unknown tool names return `None` from the dispatcher and are
        silently skipped; world-side rejections (`RuntimeError` from the
        4xx envelope) and malformed args are swallowed so a single bad
        call doesn't crash the turn."""
        survivors = state.get("survivors") or []
        forced = state.get("forced_calls") or []
        self._dispatch_all([*survivors, *forced])
        return {"survivors": [], "forced_calls": []}

    def _step(self, state: GraphState) -> GraphState:
        """Advance the world by `step_days` and refresh `day`."""
        days = max(1, int(state.get("step_days", DEFAULT_STEP_DAYS_FALLBACK)))
        remaining = max(1, state.get("game_days", 0) - state.get("day", 0))
        days = min(days, remaining)
        with contextlib.suppress(RuntimeError):
            self.api.step(days=days)
        new_state = self.api.state()
        return {"obs": new_state, "day": int(new_state.get("day", 0))}

    def _route_after_step(self, state: GraphState) -> str:
        return "observe" if state.get("day", 0) < state.get("game_days", 0) else "end"


# ---------- Helpers --------------------------------------------------------


def _clamp_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STEP_DAYS_FALLBACK
    return max(1, min(7, days))


def _safe_forecast(api: ApiClient) -> list[dict[str, Any]] | None:
    try:
        return api.forecast(hours=FORECAST_HOURS)
    except RuntimeError:
        return None


# ---------- CLI driver -----------------------------------------------------


def _make_inprocess_client() -> ApiClient:
    from fastapi.testclient import TestClient

    from world.api import create_app

    return ApiClient(transport=TestClient(create_app()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LangGraph reference agent (5-node graph).")
    parser.add_argument("--seed", type=int, default=42, help="World seed (default 42).")
    parser.add_argument("--days", type=int, default=30, help="Cap game length (default 30).")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the full configured game length (overrides --days).",
    )
    parser.add_argument("--api-url", type=str, default=None, help="Live world URL (else in-proc).")
    parser.add_argument("--output", type=Path, default=None, help="Write summary JSON here.")
    args = parser.parse_args(argv)

    if not args.full:
        os.environ["GAME_DAYS"] = str(args.days)
        os.environ["MANUAL_GAME_DAYS"] = str(args.days)

    api = ApiClient(base_url=args.api_url) if args.api_url else _make_inprocess_client()

    # No offline fallback — same contract as the ReAct CLI. Without an
    # LLM key, the construction below raises RuntimeError so a
    # degenerate "step-only" run can't be mistaken for a real one.
    agent = LangGraphAgent(api, seed=args.seed)
    final = agent.play_game()

    payload = {
        "seed": args.seed,
        "day": int(final.get("day", 0)),
        "population": int(final.get("population", 0)),
        "treasury": float(final.get("treasury", 0.0)),
        "turns": agent.turns,
        "cumulative_tokens": agent.cumulative_tokens,
        "score": agent.final_score,
    }
    print(json.dumps(payload, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


# Agent Play attach contract: the handler prefers a top-level `Agent`
# symbol that is a BaseAgent subclass (`world.api.post_agent_attach`).
Agent = LangGraphAgent


if __name__ == "__main__":
    raise SystemExit(main())
