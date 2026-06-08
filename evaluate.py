"""evaluate_v2.py — experimental duplicate of evaluate.py for score A/B.

Identical CLI and behaviour to evaluate.py, with two scoring changes so
you can run both side by side and compare the result lines by eye:

  1. Horizon consistency. `time_scaled_score` normalizes against the
     2-year longevity goal (LONGEVITY_TARGET_DAYS, 730) — the same
     horizon world/scoring.py treats as "complete" — instead of the
     full 3650-day game_days. A run that reaches the goal within budget
     keeps its full score; one cut off earlier is scaled by how far
     toward 2 years it got.

  2. Representativeness. Alongside the shipped `score`, the result line
     carries two alternative measures computed from the SAME component
     breakdown, so a run that is insolvent / depopulated most of the
     game scores like the bad run it is:
       - `score_reweight`: base-term weights shifted off longevity and
         renewable-share toward treasury / population / solvency.
       - `score_viability`: the baseline score gated by a
         solvency x population viability index (necessary conditions,
         not just weighted terms).
     Each gets its own `time_scaled_*` field using the 730-day horizon.

Original evaluate.py is untouched; compare its line to this one.

---

CLI driver for AFK evaluation and scoring.

Two modes:

    python evaluate.py --agent agents.scripted --seed 42
        Loads <module>.Agent (a class with __init__(api, *, seed) and a
        .play_game() method, e.g. agents.scripted.ScriptedAgent), plays
        a full game on the given seed, writes the final state alongside
        the action log at runs/{run_id}/final_state.json, and prints a
        JSON breakdown line.  Exit 0 on success, 1 on agent crash.

        Pass ``--scenario <dotted_path>`` (e.g. ``scenarios.grid_stress``)
        to attach a stress scenario before the agent runs.

    python evaluate.py --score runs/{run_id}
        Reads ``states.jsonl`` from the run folder (or a direct path to
        a ``states.jsonl``) and prints the score breakdown that
        ``compute_score`` produces — the same payload ``GET /score``
        returns. Pass ``--starting-cash`` to override the cash anchor
        when scoring a run played under a non-default config.

By default the agent runs against an in-process FastAPI TestClient — no
uvicorn boot, no port management — matching the pattern in
``agents.scripted``.  Pass ``--api-url`` (or set ``WORLD_API_URL``) to
point at a live world (used by ``docker compose --profile eval run
agent``, where the URL is ``http://world:8000``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load `.env` from the repo root before importing anything that reads
# env vars. This lets users keep LLM_PROVIDER and the per-provider
# settings (ANTHROPIC_API_KEY, NIM_BASE_URL, etc.) in `.env` instead
# of exporting them in every shell.
load_dotenv(Path(__file__).resolve().parent / ".env")

from agents.api_client import ApiClient, BudgetExpired  # noqa: E402
from agents.base import BaseAgent  # noqa: E402
from world.api import create_app  # noqa: E402
from world.config import load_config  # noqa: E402
from world.scoring import LONGEVITY_TARGET_DAYS, compute_score  # noqa: E402
from world.sim import World  # noqa: E402


def _load_agent_class(module_name: str) -> type[BaseAgent]:
    """Resolve the submitted agent class.

    Convention: the module exposes a top-level ``Agent`` symbol bound to
    a class with ``__init__(api, *, seed)`` and ``.play_game()``.  We
    also walk module attributes for any concrete ``BaseAgent`` subclass
    so a participant can re-export their class under any name.
    """
    mod = importlib.import_module(module_name)
    cls = getattr(mod, "Agent", None)
    if cls is None:
        for value in vars(mod).values():
            if isinstance(value, type) and issubclass(value, BaseAgent) and value is not BaseAgent:
                cls = value
                break
    if cls is None or not isinstance(cls, type):
        raise ValueError(f"{module_name} does not expose an `Agent` class or BaseAgent subclass")
    return cls


def _make_inprocess_client(game_days: int | None = None) -> tuple[ApiClient, World]:
    """Build an in-process API client + world.  No uvicorn, no socket.

    ``game_days`` overrides the world's game-day horizon (``--days``),
    so the agent's ``play_game`` loop — which runs until
    ``active_game_days`` — stops at the requested day.
    """
    from fastapi.testclient import TestClient

    config = replace(load_config(), game_days=game_days) if game_days is not None else None
    world = World(config=config, runs_root="runs", run_prefix="eval", seed_starter_grid=True)
    app = create_app(world=world)
    return ApiClient(transport=TestClient(app)), world


def _resolve_states_path(arg: Path) -> Path:
    if arg.is_dir():
        return arg / "states.jsonl"
    return arg


def _load_snapshots(path: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            state = json.loads(line).get("state", {})
            snapshots.append(
                {
                    "treasury": state.get("treasury", 0.0),
                    "population": state.get("population", 0.0),
                    "happiness": state.get("happiness", 0.0),
                    "cumulative_renewable_served_kwh": state.get(
                        "cumulative_renewable_served_kwh", 0.0
                    ),
                    "cumulative_total_served_kwh": state.get("cumulative_total_served_kwh", 0.0),
                }
            )
    return snapshots


# --- LLM metrics ------------------------------------------------------------


class _LLMMetricsRecorder:
    """Wraps an agent's ``llm`` so each ``chat()`` is timed and its token
    usage recorded, without the agent knowing.

    The agent calls ``self.llm.chat(...)`` once per planning turn; we
    intercept that one method, stamp the wall-clock latency around the
    underlying call, and pull ``input_tokens`` / ``output_tokens`` (plus
    Anthropic's cache-prefix counts) off the returned ``LLMResponse``.
    Every other attribute access proxies to the wrapped client, so the
    recorder is a transparent stand-in for any ``LLMClient`` adapter.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, float]] = []

    def chat(self, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        response = self._inner.chat(**kwargs)
        latency = time.monotonic() - t0
        usage = response.usage
        self.calls.append(
            {
                "latency_seconds": latency,
                "input_tokens": float(usage.input_tokens),
                "output_tokens": float(usage.output_tokens),
                "cache_creation_input_tokens": float(usage.cache_creation_input_tokens),
                "cache_read_input_tokens": float(usage.cache_read_input_tokens),
            }
        )
        return response

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not found on the recorder itself,
        # so the wrapped adapter's `model`, internal client, etc. stay
        # reachable for any agent that introspects its llm.
        return getattr(self._inner, name)

    def summary(self) -> dict[str, Any]:
        """Aggregate the recorded calls into a JSON-friendly breakdown."""
        n = len(self.calls)
        latencies = sorted(c["latency_seconds"] for c in self.calls)
        in_tokens = [c["input_tokens"] for c in self.calls]
        out_tokens = [c["output_tokens"] for c in self.calls]
        cache_create = sum(c["cache_creation_input_tokens"] for c in self.calls)
        cache_read = sum(c["cache_read_input_tokens"] for c in self.calls)
        total_in = sum(in_tokens)
        total_out = sum(out_tokens)
        return {
            "llm_calls": n,
            "latency_seconds": {
                "total": sum(latencies),
                "mean": (sum(latencies) / n) if n else 0.0,
                "min": latencies[0] if n else 0.0,
                "max": latencies[-1] if n else 0.0,
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
            },
            "input_tokens": {
                "total": int(total_in),
                "mean": (total_in / n) if n else 0.0,
                "cache_creation": int(cache_create),
                "cache_read": int(cache_read),
            },
            "output_tokens": {
                "total": int(total_out),
                "mean": (total_out / n) if n else 0.0,
            },
            "total_tokens": int(total_in + total_out + cache_create + cache_read),
        }


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list (0.0 if empty)."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


# --- Progress bar -----------------------------------------------------------


def _fmt_dur(seconds: float) -> str:
    """Compact duration: ``1h02m`` / ``3m05s`` / ``45s``."""
    s = int(max(0.0, seconds))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


class _ProgressBar:
    """Live progress bar rendered to stderr while the agent plays.

    Two modes, chosen by which bound the caller passed:

      - days mode (``--days``): fraction = current_day / total_days; the
        bar feeds off ``/state`` responses via ``note_day`` and shows an
        ETA extrapolated from the day rate.
      - time mode (``--time-budget``): fraction = elapsed / budget; a
        pure wall-clock countdown, independent of agent progress.

    A daemon thread re-renders every 0.5s so the "time remaining"
    estimate keeps ticking even while the agent blocks on a slow LLM
    call. No-op when stderr is not a TTY, so Docker/CI logs stay clean.
    """

    def __init__(
        self,
        *,
        started_at: float,
        total_days: int | None = None,
        time_budget: int | None = None,
    ) -> None:
        self._started_at = started_at
        self._total_days = total_days
        self._time_budget = time_budget
        self._current_day = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._active = sys.stderr.isatty()

    def note_day(self, day: int) -> None:
        self._current_day = day

    def start(self) -> None:
        if self._active:
            self._thread.start()

    def stop(self) -> None:
        if not self._active:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._render()
        sys.stderr.write("\n")
        sys.stderr.flush()

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self._render()

    def _render(self) -> None:
        elapsed = time.monotonic() - self._started_at
        if self._time_budget is not None:
            frac = min(1.0, elapsed / self._time_budget) if self._time_budget else 1.0
            tail = (
                f"{_fmt_dur(elapsed)} / {_fmt_dur(self._time_budget)}"
                f" · {_fmt_dur(self._time_budget - elapsed)} left"
            )
        else:
            total = self._total_days or 1
            day = min(self._current_day, total)
            frac = day / total
            if 0 < day < total:
                eta = elapsed * (total - day) / day
                tail = f"day {day}/{total} · ~{_fmt_dur(eta)} left"
            else:
                tail = f"day {day}/{total} · {_fmt_dur(elapsed)} elapsed"

        width = shutil.get_terminal_size((80, 20)).columns
        label = f" {int(frac * 100):3d}% {tail}"
        bar_width = max(10, width - len(label) - 3)
        filled = int(bar_width * frac)
        bar = "#" * filled + "-" * (bar_width - filled)
        # \033[K clears any trailing chars left by a longer prior line.
        sys.stderr.write(f"\r[{bar}]{label}\033[K")
        sys.stderr.flush()


# --- Alternative scoring measures (v2 only) ---------------------------------
#
# Both recombine the component breakdown `compute_score` already returns,
# so they stay in lockstep with the canonical per-axis math and differ
# only in how the components are AGGREGATED into a headline.

# `score_reweight` weights: shifted from the shipped
# (0.30/0.30/0.10/0.20/0.10 base, 0.15 longevity) to put fiscal +
# demographic health in the driver's seat. The five base weights sum to
# 1.0; longevity is cut to 0.05 so survival-days can't carry a broken run.
_RW_AXIS_TREASURY = 0.30
_RW_AXIS_POP = 0.30
_RW_AXIS_HAPPY = 0.05
_RW_R = 0.10
_RW_SOLVENCY = 0.25
_RW_LONGEVITY = 0.05

# `score_viability` gate: a run earns full credit only once it is solvent
# on _SOLVENCY_FULL of days AND sustains _POP_LEVEL_FULL of the population
# target on average. Each factor ramps to 1.0 at its threshold; the two
# multiply, so a run broke OR empty most of the game is pulled toward zero.
_SOLVENCY_FULL = 0.80
_POP_LEVEL_FULL = 0.50


def _score_reweight(components: dict[str, Any]) -> float:
    """Headline in [0, 100] from baseline components, fundamentals-first."""
    base = (
        _RW_AXIS_TREASURY * components["axis_treasury"]
        + _RW_AXIS_POP * components["axis_pop"]
        + _RW_AXIS_HAPPY * components["axis_happy"]
        + _RW_R * components["R"]
        + _RW_SOLVENCY * components["solvency"]
    )
    headline = 100.0 * ((1.0 - _RW_LONGEVITY) * base + _RW_LONGEVITY * components["longevity"])
    return max(0.0, min(100.0, headline))


def _score_viability(baseline_score: float, components: dict[str, Any]) -> float:
    """Baseline score gated by a solvency x population viability index."""
    solvency_factor = min(1.0, components["solvency"] / _SOLVENCY_FULL)
    pop_factor = min(1.0, components["level_pop"] / _POP_LEVEL_FULL)
    return max(0.0, min(100.0, baseline_score * solvency_factor * pop_factor))


# --- Commands ---------------------------------------------------------------


def cmd_eval(
    module_name: str,
    seed: int,
    api_url: str | None,
    scenario: str | None = None,
    time_budget: int | None = None,
    days: int | None = None,
    metrics: bool = False,
) -> int:
    AgentCls = _load_agent_class(module_name)
    world: World | None = None
    if api_url:
        api = ApiClient(base_url=api_url)
        # When running against a live world we don't own the action log
        # directory; final_state lands next to evaluate.py's cwd under a
        # synthetic run_id so the path printed in the result line points
        # somewhere meaningful.
        run_dir = Path("runs") / f"live-{seed}"
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        api, world = _make_inprocess_client(game_days=days)
        run_dir = world.recorder.dir if world.recorder is not None else Path("runs")

    # Attach the scenario BEFORE the agent's first reset so the scenario
    # survives into the post-reset recorder's metadata.json (the agent
    # always calls /reset without a scenario arg, and reset preserves
    # the currently-attached scenario when none is passed).
    if scenario is not None:
        api.attach_scenario(scenario)

    agent = AgentCls(api, seed=seed)

    # --metrics: intercept the agent's LLM so each chat() is timed and its
    # token usage tallied. Wrap after construction (the agent's __init__
    # has already built `self.llm`) and only when the agent actually has
    # an LLM client — scripted agents have none, so we warn and skip.
    recorder: _LLMMetricsRecorder | None = None
    if metrics:
        inner_llm = getattr(agent, "llm", None)
        if inner_llm is not None and callable(getattr(inner_llm, "chat", None)):
            recorder = _LLMMetricsRecorder(inner_llm)
            agent.llm = recorder  # type: ignore[attr-defined]
        else:
            print(
                "--metrics: agent exposes no `llm.chat`; no LLM metrics collected",
                file=sys.stderr,
            )

    # T2 clock: start counting once the agent class is in hand. Excludes
    # Python/import startup; includes agent __init__ (graph build for
    # langgraph) and the agent's own /reset inside play_game().
    started_at = time.monotonic()
    if time_budget is not None:
        api._deadline_monotonic = started_at + float(time_budget)

    # A progress bar is shown only when a bound is given. Time mode
    # (--time-budget) takes precedence over day mode (--days) when both
    # are present, since wall time is then the binding constraint.
    progress: _ProgressBar | None = None
    if time_budget is not None:
        progress = _ProgressBar(started_at=started_at, time_budget=time_budget)
    elif days is not None:
        progress = _ProgressBar(started_at=started_at, total_days=days)
        # Feed the bar the current day off each /state response.
        _orig_state = api.state

        def _state_with_progress() -> dict[str, Any]:
            state = _orig_state()
            assert progress is not None
            progress.note_day(int(state.get("day", 0)))
            return state

        api.state = _state_with_progress  # type: ignore[method-assign]

    final_state: dict[str, Any]
    try:
        if progress is not None:
            progress.start()
        final_state = agent.play_game()
    except BudgetExpired:
        # Stop the watchdog so the post-game state/score reads below
        # don't trip the deadline check on their own calls.
        api._deadline_monotonic = None
        final_state = api.state()
    finally:
        api._deadline_monotonic = None
        if progress is not None:
            progress.stop()
    wall_time_seconds = time.monotonic() - started_at

    if world is not None and world.recorder is not None:
        run_dir = world.recorder.dir

    # The recorder lazily materializes its directory on the first
    # /step. A budget that fires before /reset (or any other path
    # that never advances time) leaves the run folder uncreated, so
    # ensure it exists before writing the harness-side snapshot.
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "final_state.json").write_text(
        json.dumps(final_state, sort_keys=True, default=str) + "\n"
    )

    score_payload = api.score()
    raw_score = float(score_payload.get("score", 0.0))
    components = score_payload.get("components", {})
    # v2 alternative measures, recombined from the same components.
    score_reweight = _score_reweight(components) if components else 0.0
    score_viability = _score_viability(raw_score, components) if components else 0.0
    line: dict[str, Any] = {
        "agent": module_name,
        "seed": seed,
        "run_id": run_dir.name,
        "score": score_payload,
        "score_reweight": score_reweight,
        "score_viability": score_viability,
    }
    if time_budget is not None:
        days_advanced = int(final_state.get("day", 0))
        # v2 horizon fix: scale against the 2-year longevity goal
        # (LONGEVITY_TARGET_DAYS), capped at 1.0 — the same horizon
        # world/scoring.py treats as complete — not the full game_days.
        goal_fraction = min(1.0, days_advanced / LONGEVITY_TARGET_DAYS)
        line["time_budget_seconds"] = int(time_budget)
        line["wall_time_seconds"] = wall_time_seconds
        line["days_advanced"] = days_advanced
        line["longevity_target_days"] = LONGEVITY_TARGET_DAYS
        line["time_scaled_score"] = raw_score * goal_fraction
        line["time_scaled_reweight"] = score_reweight * goal_fraction
        line["time_scaled_viability"] = score_viability * goal_fraction
    if recorder is not None:
        line["llm_metrics"] = recorder.summary()
    print(json.dumps(line))
    return 0


def cmd_score(run: Path, starting_cash: float | None) -> int:
    states_path = _resolve_states_path(run)
    if not states_path.exists():
        print(f"not found: {states_path}", file=sys.stderr)
        return 1

    cash = float(starting_cash) if starting_cash is not None else float(load_config().starting_cash)
    snapshots = _load_snapshots(states_path)
    print(json.dumps(compute_score(snapshots, cash), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", help="Module path to the submitted agent (e.g. submit.agent).")
    parser.add_argument("--seed", type=int, default=42, help="Seed to play (default 42).")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("WORLD_API_URL"),
        help="Talk to a live world at this URL (default: in-process TestClient).",
    )
    parser.add_argument(
        "--score",
        type=Path,
        metavar="RUN",
        help="Score a recorded run: path to runs/{run_id} or to a states.jsonl.",
    )
    parser.add_argument(
        "--starting-cash",
        type=float,
        default=None,
        help="Override starting cash for --score. Defaults to Config.starting_cash.",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help=(
            "Dotted path to a Scenario subclass (e.g. scenarios.grid_stress). "
            "Attached to the world before the agent runs."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "Override the game-day horizon: the agent plays this many "
            "days instead of the configured game_days, and evaluate.py "
            "shows a day-by-day progress bar with an ETA. In-process "
            "only (incompatible with --api-url)."
        ),
    )
    parser.add_argument(
        "--time-budget",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Wall-clock budget (int seconds). When the budget elapses the "
            "next ApiClient call raises BudgetExpired; evaluate.py reads "
            "the world's current state and emits time_scaled_score = "
            "score * days_advanced / game_days alongside the regular "
            "score payload. Omitted = no cap."
        ),
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help=(
            "Record per-LLM-call latency and token usage. Wraps the "
            "agent's llm.chat() and emits an `llm_metrics` block "
            "(call count, latency total/mean/min/max/p50/p95, and "
            "input/output/cache token totals) in the result line. "
            "No-op for agents without an LLM (e.g. scripted)."
        ),
    )
    args = parser.parse_args(argv)

    if args.score is not None:
        return cmd_score(args.score, args.starting_cash)

    if not args.agent:
        parser.error("either --agent or --score is required")
    if args.days is not None and args.api_url:
        parser.error(
            "--days overrides the in-process world's horizon; it cannot apply to --api-url"
        )
    return cmd_eval(
        args.agent,
        int(args.seed),
        args.api_url,
        args.scenario,
        time_budget=args.time_budget,
        days=args.days,
        metrics=args.metrics,
    )


if __name__ == "__main__":
    raise SystemExit(main())
