"""FastAPI surface for the simulation.

Endpoints wired through slice 02:
  /state, /step, /reset, /seed, /catalog, /forecast, /build, /demolish.
All mutating calls (success or failure) are appended to
runs/{run_id}/actions.jsonl.
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.types import Scope

from agents.api_client import UiAgentApiClient
from agents.base import BaseAgent
from world.action_log import ActionLog
from world.catalog import build_catalog
from world.scenario import NullScenario, discover_scenarios, load_scenario
from world.scoring import compute_score
from world.sim import World
from world.subsurface import SEISMIC_DEFAULT_SIZE

load_dotenv()  # silent no-op if .env is absent

# Default repo root for `POST /agent/attach`. Tests can override via
# `create_app(agent_repo_root=...)` so a `tmp_path` scratch dir counts as
# the trust boundary; the production server uses the on-disk repo.
DEFAULT_AGENT_REPO_ROOT: Path = Path(__file__).resolve().parent.parent


class _NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


class ResetBody(BaseModel):
    seed: int | None = None
    # open-source-arena slice 04: optional scenario dotted path. When
    # present, the reset attaches the resolved scenario to the new
    # world; absent, the world's existing scenario is preserved.
    scenario: str | None = None


class ScenarioBody(BaseModel):
    dotted_path: str


class StepBody(BaseModel):
    days: int = Field(default=7, ge=1, le=7)


class BuildBody(BaseModel):
    tile_type: str
    x: int
    y: int


class DemolishBody(BaseModel):
    x: int
    y: int


class SurveyBody(BaseModel):
    x: int
    y: int
    size: int = Field(default=SEISMIC_DEFAULT_SIZE)


class DrillBody(BaseModel):
    x: int
    y: int
    target_z: int
    well_type: str = Field(default="production")


class WellControlBody(BaseModel):
    well_id: str
    rate_bbl_day: float


class RefineryControlBody(BaseModel):
    refinery_id: str
    rate_bbl_day: float


class BatteryControlBody(BaseModel):
    tile_id: str
    charge_kw: float


class AgentAttachBody(BaseModel):
    folder: str


def _load_agent_class_from_module(mod: Any) -> type[BaseAgent] | None:
    """Mirror `evaluate._load_agent_class`'s lookup convention: prefer a
    top-level `Agent` symbol bound to a class; otherwise walk module
    attributes for a concrete `BaseAgent` subclass. Slice #1 ships the
    same convention so a participant's submission works under both
    `python evaluate.py` and the UI's Agent Play attach."""
    cls = getattr(mod, "Agent", None)
    if isinstance(cls, type) and issubclass(cls, BaseAgent) and cls is not BaseAgent:
        return cls
    for value in vars(mod).values():
        if isinstance(value, type) and issubclass(value, BaseAgent) and value is not BaseAgent:
            return value
    return None


# Directories that never contain attachable agents and would otherwise
# bloat `GET /agent/folders` or slow the walk (notably `runs/` after a
# few sessions, and the third-party tree under `node_modules/`). Hidden
# dirs are excluded by the leading-dot check at the walk site.
_AGENT_FOLDER_WALK_SKIP: frozenset[str] = frozenset({"__pycache__", "node_modules", "runs"})


def _list_attachable_agent_folders(repo_root: Path) -> list[str]:
    """Return repo-relative folder paths that contain an `agent.py`.

    Powers `GET /agent/folders`, which the UI uses to populate the
    Agent Play dropdown. Sorted alphabetically so the UI renders in a
    stable order across requests. Skips hidden dirs (leading `.`) and
    the entries in `_AGENT_FOLDER_WALK_SKIP`; does not follow symlinks
    (`Path.iterdir` is non-recursive — the recursion here uses the
    same boundary rules as `_resolve_agent_folder`).
    """
    root = repo_root.resolve()
    found: list[str] = []

    def walk(current: Path) -> None:
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            return
        if any(e.name == "agent.py" and e.is_file() for e in entries):
            rel = current.relative_to(root).as_posix()
            if rel != ".":
                found.append(rel)
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name.startswith(".") or entry.name in _AGENT_FOLDER_WALK_SKIP:
                continue
            walk(entry)

    walk(root)
    found.sort()
    return found


def _resolve_agent_folder(repo_root: Path, folder: str) -> Path:
    """Resolve `folder` against `repo_root` and assert it stays inside.

    One `is_relative_to(repo_root.resolve())` boundary check on the
    resolved candidate subsumes absolute paths and symlink-escape hops
    (resolve() walks symlinks). Pre-filesystem dot-rejection in the
    handler catches Python dotted paths (`submit.agent`), `..`, and
    hidden dirs before we touch the filesystem.
    """
    candidate = (repo_root / folder).resolve()
    root = repo_root.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"folder {folder!r} resolves outside the repo root")
    if not candidate.is_dir():
        raise ValueError(f"folder {folder!r} does not exist or is not a directory")
    if not (candidate / "agent.py").is_file():
        raise ValueError(f"folder {folder!r} has no agent.py")
    return candidate


def _purge_modules_under(folder: Path) -> None:
    """Drop every entry in `sys.modules` whose `__file__` lives under
    `folder`. Lets re-attach see edits to `agent.py` *and* its sibling
    helpers (`helpers.py`, `strategy.py`, ...). Without this walk,
    popping only `sys.modules["agent"]` would silently re-use the stale
    helper modules cached from the previous attach.

    Containment is checked with `Path.is_relative_to` (not string
    prefix), so a folder `/tmp/foo` does not accidentally match modules
    under `/tmp/foobar`.
    """
    for name in list(sys.modules):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        modfile = getattr(mod, "__file__", None)
        if not modfile:
            continue
        try:
            if Path(modfile).is_relative_to(folder):
                del sys.modules[name]
        except ValueError:
            continue


def _detach_agent(app: FastAPI) -> None:
    """Drop the attached agent + remove the matching `sys.path` entry.

    Idempotent: no-op when nothing is attached. Used by `POST /agent/detach`
    and by `POST /reset` (auto-detach)."""
    folder: Path | None = getattr(app.state, "attached_agent_folder_path", None)
    if folder is not None:
        folder_str = str(folder)
        if folder_str in sys.path:
            sys.path.remove(folder_str)
        _purge_modules_under(folder)
    app.state.attached_agent = None
    app.state.attached_agent_folder = None
    app.state.attached_agent_folder_path = None
    app.state.agent_skip_remaining = 0


def _slice_actions_for_day(entries: list[dict[str, Any]], day: int) -> dict[str, Any]:
    """Slice `actions.jsonl` entries by successful `/step` / `/reset` boundaries.

    Walks forward maintaining `current_day` and a buffer. A successful
    `/step` terminates a slice spanning `[current_day, current_day + days - 1]`
    and advances `current_day += days`. A successful `/reset` terminates a
    slice and resets `current_day` to 0. Anything past the last terminator
    is the in-flight slice at the final `current_day`.

    Failed entries (`ok=false`) are dropped entirely — the widget only
    surfaces actions that mutated world state. A failed `/step` therefore
    also does not terminate the slice.

    Returns the latest slice whose day-range contains `day`. If no slice
    matches, returns an empty in-flight slice at `day`. Mirrored in
    `world/ui/app.js` for replay mode; the shared fixture in
    `world/tests/test_actions_endpoint.py` asserts parity.
    """
    slices: list[dict[str, Any]] = []
    current_day = 0
    buffer: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.get("ok"):
            continue
        buffer.append(entry)
        ep = entry.get("endpoint")
        if ep == "/step":
            days = int(entry.get("params", {}).get("days", 7))
            slices.append(
                {
                    "day_start": current_day,
                    "day_end": current_day + days - 1,
                    "entries": buffer,
                }
            )
            current_day += days
            buffer = []
        elif ep == "/reset":
            slices.append({"day_start": current_day, "day_end": current_day, "entries": buffer})
            current_day = 0
            buffer = []
    slices.append({"day_start": current_day, "day_end": current_day, "entries": buffer})
    for s in reversed(slices):
        if s["day_start"] <= day <= s["day_end"]:
            return s
    return {"day_start": day, "day_end": day, "entries": []}


def create_app(
    world: World | None = None,
    action_log: ActionLog | None = None,
    *,
    runs_root: str = "runs",
    agent_repo_root: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Agent Energy Arena", version="0.1.0")

    # When no World is provided, allocate one wired to the canonical
    # `runs/` root so the recorder (slice 03) writes metadata + per-day
    # state + final.json. The default ActionLog then lands its
    # `actions.jsonl` inside the recorder's run folder so a single run
    # directory holds every artifact for the session.
    if world is None:
        world = World(runs_root=runs_root, run_prefix="play", seed_starter_grid=True)
    if action_log is None:
        if world.recorder is not None:
            # Co-locate actions.jsonl with the recorder so a single run
            # directory holds every artifact for the session, sharing the
            # recorder's readable `<prefix>-<timestamp>` folder name.
            action_log = ActionLog(root=runs_root, run_id=world.recorder.run_id)
        else:
            # The world was built with `runs_root=None` — it deliberately
            # keeps no on-disk run folder (tests, library use). An action
            # log with no recorder to anchor it must NOT materialize into
            # the canonical `runs/` root, or every mutating-endpoint test
            # would litter it with stray `<epoch>-<uuid>` directories.
            # Route it to a throwaway temp root that mirrors the world's
            # "no persistence" intent.
            action_log = ActionLog(root=Path(tempfile.gettempdir()) / "aea-ephemeral-runs")
    app.state.world = world
    app.state.action_log = action_log
    # When the world owns a recorder, /reset reallocates the run folder
    # under it — actions.jsonl must follow so a single run dir holds
    # every artifact for the post-reset session. This flag is set only
    # for callers that took the default `action_log` path; callers who
    # passed their own ActionLog have explicit lifecycle control.
    app.state._action_log_follows_recorder = action_log is not None and (
        world.recorder is not None and action_log.dir == world.recorder.dir
    )
    app.state._runs_root = runs_root
    # Agent Play (agent-play slice 01) — three pieces of attach state:
    # the instantiated BaseAgent subclass, the folder slug a `GET /agent`
    # reports, and the resolved Path used by detach to scrub `sys.path`.
    app.state.attached_agent = None
    app.state.attached_agent_folder = None
    app.state.attached_agent_folder_path = None
    # Agent Play skip cooldown: an attached `act()` may return `N` to say
    # "act now, skip the next N-1 /step calls." The /step handler decrements
    # this by `body.days` each tick and only re-invokes `act` when it hits 0.
    # `None` return → counter stays 0 (act every step, today's behavior).
    app.state.agent_skip_remaining = 0
    app.state._agent_repo_root = (
        agent_repo_root if agent_repo_root is not None else DEFAULT_AGENT_REPO_ROOT
    )

    @app.get("/seed")
    def get_seed() -> dict[str, int]:
        return {"seed": app.state.world.state.seed}

    @app.get("/catalog")
    def get_catalog() -> dict[str, Any]:
        return build_catalog()

    @app.get("/state")
    def get_state() -> dict[str, Any]:
        return app.state.world.state_dict()

    @app.get("/state/history")
    def get_state_history(day: int) -> dict[str, Any]:
        # Reads one entry from the in-progress run's `states.jsonl`. Lets
        # the UI render a previous live day (read-only "peek backward")
        # without server simulation state moving. The recorder writes one
        # line per just-completed day with `day` reflecting `state.day`
        # before its post-step increment — so live `state.day == N` means
        # the most recent recorded entry has `day == N - 1`.
        recorder = app.state.world.recorder
        if recorder is None or not recorder.states_path.exists():
            raise HTTPException(status_code=404, detail="no recorded history")
        with recorder.states_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                entry = json.loads(stripped)
                if entry.get("day") == day:
                    return entry
        raise HTTPException(status_code=404, detail=f"day {day} not in recorded history")

    @app.get("/actions")
    def get_actions(day: int) -> dict[str, Any]:
        # Line-scans the in-progress run's `actions.jsonl` and slices it
        # at every successful `/step` or `/reset` entry. Returns the slice
        # whose day-range contains `day` (latest sequence wins after a
        # mid-run /reset). The UI's Actions panel calls this with
        # `day=state.day` to render "actions submitted since the last
        # step/reset". Replay mode runs the same algorithm client-side
        # against the loaded `actions.jsonl` (see `world/ui/app.js`).
        path = app.state.action_log.path
        if not path.exists():
            return {"day_start": day, "day_end": day, "entries": []}
        with path.open("r", encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        slice_ = _slice_actions_for_day(entries, day)
        return slice_

    @app.get("/scenario")
    def get_scenario() -> dict[str, Any]:
        world = app.state.world
        # NullScenario surfaces as None for downstream consumers (UI,
        # evaluate.py replay) so they can distinguish "no scenario
        # attached" from a real dotted path. `description` mirrors that:
        # null when nothing is attached, otherwise the scenario class's
        # docstring (the "plan" the UI prints once a scenario is loaded).
        # `source` is the full module file the UI shows in a read-only
        # code box so a player can inspect what the scenario actually
        # does.
        if isinstance(world.scenario, NullScenario):
            return {"dotted_path": None, "description": None, "source": None}
        doc = (type(world.scenario).__doc__ or "").strip() or None
        source: str | None = None
        try:
            src_file = inspect.getsourcefile(type(world.scenario))
            if src_file:
                source = Path(src_file).read_text(encoding="utf-8")
        except (OSError, TypeError):
            source = None
        return {
            "dotted_path": world.scenario_dotted_path,
            "description": doc,
            "source": source,
        }

    @app.post("/scenario")
    def post_scenario(body: ScenarioBody) -> dict[str, Any]:
        params = body.model_dump()
        try:
            scenario = load_scenario(body.dotted_path)
        except (ImportError, ValueError) as exc:
            app.state.action_log.append("/scenario", params, ok=False, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        world = app.state.world
        world.scenario = scenario
        world.scenario_dotted_path = body.dotted_path
        result: dict[str, Any] = {"ok": True, "dotted_path": body.dotted_path}
        app.state.action_log.append("/scenario", params, ok=True, result=result)
        return result

    @app.get("/scenarios")
    def get_scenarios() -> dict[str, Any]:
        repo_root: Path = app.state._agent_repo_root
        return {"scenarios": discover_scenarios(repo_root / "scenarios")}

    @app.get("/agent")
    def get_agent() -> dict[str, Any]:
        folder = getattr(app.state, "attached_agent_folder", None)
        return {"folder": folder}

    @app.get("/agent/folders")
    def get_agent_folders() -> dict[str, Any]:
        repo_root: Path = app.state._agent_repo_root
        return {"folders": _list_attachable_agent_folders(repo_root)}

    @app.post("/agent/attach")
    def post_agent_attach(body: AgentAttachBody) -> dict[str, Any]:
        # Detach any previously-attached agent first so re-attach is
        # idempotent and the previous `sys.path` entry is cleaned up.
        _detach_agent(app)
        folder = body.folder
        repo_root: Path = app.state._agent_repo_root

        # Bad-input phase: reject anything containing '.' before any
        # filesystem access. Catches Python dotted paths ("submit.agent"),
        # parent escapes (".."), and hidden dirs (".hidden"). Path-safety
        # boundary + existence checks happen next.
        if "." in folder:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"failed to load agent from {folder!r}: bad input: "
                    "folder must not contain '.' "
                    "(pass a plain folder name, not a Python dotted path)"
                ),
            )

        # Bad-input phase: filesystem boundary + existence. One
        # `is_relative_to(repo_root.resolve())` check subsumes absolute
        # paths and symlink escapes.
        try:
            folder_path = _resolve_agent_folder(repo_root, folder)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"failed to load agent from {folder!r}: bad input: {exc}",
            ) from exc

        # Hot-reload loop (slice #5): `_detach_agent` above already
        # walked `sys.modules` for the previously-attached folder and
        # removed its `sys.path` entry — so on re-attach, edits to
        # `agent.py` *and* its sibling helpers (`helpers.py`,
        # `strategy.py`, ...) are picked up from disk.
        #
        # Pop `"agent"` defensively: the walk above covers the
        # *currently-tracked* attach state on this app, but `sys.modules`
        # is process-global. A prior attach against a different `app`
        # instance (e.g. across test cases) can leave `sys.modules["agent"]`
        # pointing at a folder this app has never heard of, which would
        # otherwise short-circuit our fresh `import_module("agent")`.
        # `invalidate_caches` before `sys.path.insert` keeps importlib's
        # finder cache honest in case a prior import poisoned a "missing"
        # entry for a path we now want resolved.
        #
        # Loading phases (import → lookup → validate → construct) share
        # one try/except so the developer gets the same detail shape for
        # every loading failure: `failed to load agent from <input>:
        # <ExceptionType>: <message>`. Surfaces ImportError, missing
        # class, and `__init__` exceptions verbatim — enough to debug
        # without grepping server logs.
        sys.modules.pop("agent", None)
        importlib.invalidate_caches()
        folder_str = str(folder_path)
        sys.path.insert(0, folder_str)
        try:
            mod = importlib.import_module("agent")
            cls = _load_agent_class_from_module(mod)
            if cls is None:
                raise ValueError(f"folder {folder!r} has no `Agent` class or BaseAgent subclass")
            api_for_agent = UiAgentApiClient(transport=TestClient(app))
            instance = cls(api_for_agent)
        except Exception as exc:
            if folder_str in sys.path:
                sys.path.remove(folder_str)
            raise HTTPException(
                status_code=400,
                detail=f"failed to load agent from {folder!r}: {type(exc).__name__}: {exc}",
            ) from exc

        app.state.attached_agent = instance
        app.state.attached_agent_folder = body.folder
        app.state.attached_agent_folder_path = folder_path
        # Fresh attach: clear any stale cooldown from a prior agent.
        app.state.agent_skip_remaining = 0
        return {"ok": True, "folder": body.folder}

    @app.post("/agent/detach")
    def post_agent_detach() -> dict[str, Any]:
        _detach_agent(app)
        return {"ok": True, "folder": None}

    @app.get("/run")
    def get_run() -> dict[str, Any]:
        recorder = app.state.world.recorder
        if recorder is None:
            return {"run_id": None, "dir": None}
        return {"run_id": recorder.run_id, "dir": str(recorder.dir)}

    @app.get("/events")
    def get_events() -> dict[str, Any]:
        s = app.state.world.state
        return {
            "active": list(s.active_events),
            "historical": list(s.historical_events),
            "regulatory_tightenings_applied": s.regulatory_tightenings_applied,
        }

    @app.get("/score")
    def get_score() -> dict[str, Any]:
        # Trend-aware absolute score in [0, 100], computed from the
        # active recorder's per-day `states.jsonl` log on disk. Reads
        # `treasury`, `population`, `happiness`, and the renewable
        # cumulative kWh accumulators from each line's embedded `state`
        # dict; the in-memory `WorldState` is deliberately not
        # consulted. Mid-game queries work by construction — the
        # recorder writes one line per just-completed day. Missing
        # recorder / missing file / empty file all return the
        # empty-response payload with HTTP 200; never 404. See
        # `world/scoring.py` for the formula and scale anchors.
        world = app.state.world
        recorder = world.recorder
        if recorder is None or not recorder.states_path.exists():
            return {"n_days": 0, "score": 0.0, "components": {}}
        snapshots: list[dict[str, Any]] = []
        with recorder.states_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                entry = json.loads(stripped)
                state = entry.get("state", {})
                snapshots.append(
                    {
                        "treasury": state.get("treasury", 0.0),
                        "population": state.get("population", 0.0),
                        "happiness": state.get("happiness", 0.0),
                        "cumulative_renewable_served_kwh": state.get(
                            "cumulative_renewable_served_kwh", 0.0
                        ),
                        "cumulative_total_served_kwh": state.get(
                            "cumulative_total_served_kwh", 0.0
                        ),
                    }
                )
        return compute_score(snapshots, float(world.config.starting_cash))

    @app.get("/forecast")
    def get_forecast(hours: int = 24) -> list[dict[str, Any]]:
        if hours < 1 or hours > 168:
            raise HTTPException(status_code=400, detail="hours must be in [1, 168]")
        return app.state.world.forecast(hours=hours)

    @app.post("/reset")
    def post_reset(body: ResetBody) -> dict[str, Any]:
        params = body.model_dump()
        # Agent Play (slice 01): /reset wipes the world; the attached
        # agent goes with it. Scenario state survives via the existing
        # scenario-preservation branch below.
        _detach_agent(app)
        scenario_instance = None
        if body.scenario is not None:
            try:
                scenario_instance = load_scenario(body.scenario)
            except (ImportError, ValueError) as exc:
                app.state.action_log.append("/reset", params, ok=False, error=str(exc))
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # An empty body (no `seed` field) preserves the currently active
        # seed rather than reverting to the world's configured default,
        # so the UI's top-bar Reset button is a "wipe but keep seed" action.
        seed_to_use = body.seed if body.seed is not None else app.state.world.state.seed
        try:
            app.state.world.reset(
                seed=seed_to_use,
                scenario=scenario_instance,
                scenario_dotted_path=body.scenario if scenario_instance else None,
            )
            result = {
                "ok": True,
                "treasury_after": app.state.world.state.treasury,
                "result": {"seed": app.state.world.state.seed, "day": 0},
            }
            # If the action log was co-located with the (now-finalized)
            # recorder, rebind it to the fresh recorder's folder so the
            # post-reset session keeps actions.jsonl alongside the new
            # states.jsonl / metadata.json / final.json. Callers who
            # passed their own ActionLog stay where they put it.
            if (
                getattr(app.state, "_action_log_follows_recorder", False)
                and app.state.world.recorder is not None
            ):
                app.state.action_log = ActionLog(
                    root=app.state._runs_root,
                    run_id=app.state.world.recorder.run_id,
                )
            app.state.action_log.append("/reset", params, ok=True, result=result["result"])
            return result
        except Exception as exc:  # pragma: no cover - defensive
            app.state.action_log.append("/reset", params, ok=False, error=str(exc))
            raise

    @app.post("/step")
    def post_step(body: StepBody) -> dict[str, Any]:
        params = body.model_dump()
        # Agent Play (slice 01 + 04): when an agent is attached, give it the
        # chance to mutate the world before the day(s) advance. Any exception
        # raised by `act()` surfaces as a 500 whose detail names the cause
        # verbatim — the developer reads the proximate error in the UI toast
        # without grepping server logs. The agent stays attached and the day
        # does not advance, so the edit-fix-retry loop stays cheap. This is
        # also the path that catches `RuntimeError` from `UiAgentApiClient`'s
        # clock-method guards (agent calling `self.api.step()` from `act()`).
        #
        # Skip cooldown: if the previous `act()` returned N>1, we owe
        # the agent (N-1) silent steps before calling it again. The
        # play timer can keep ticking the world clock at native speed
        # while a slow LLM model only fires once every N days.
        attached_agent = getattr(app.state, "attached_agent", None)
        if attached_agent is not None:
            skip_remaining = int(getattr(app.state, "agent_skip_remaining", 0))
            if skip_remaining > 0:
                app.state.agent_skip_remaining = max(0, skip_remaining - body.days)
                print(
                    f"[agent day={app.state.world.state.day}] (skipped, "
                    f"{app.state.agent_skip_remaining}d cooldown left)",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                try:
                    requested = attached_agent.act(app.state.world.state_dict())
                except Exception as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=f"agent.act raised: {exc!r}",
                    ) from exc
                # `None` → wake me every step (today's default). An int
                # `N` is clamped 1..7 by `drive_one_turn`; we owe N-1
                # silent steps after this one. We've already consumed
                # `body.days` worth of this step's "budget" by acting
                # now, so subtract that from the upcoming cooldown.
                if requested is not None:
                    app.state.agent_skip_remaining = max(0, int(requested) - body.days)
        try:
            summary = app.state.world.step(days=body.days)
            result = {
                "ok": True,
                "day_completed": summary.day_completed,
                "summary": summary.summary,
                "treasury_after": summary.treasury_after,
            }
            app.state.action_log.append(
                "/step", params, ok=True, result={"day_completed": summary.day_completed}
            )
            return result
        except ValueError as exc:
            app.state.action_log.append("/step", params, ok=False, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/build")
    def post_build(body: BuildBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.build(body.tile_type, body.x, body.y)
        app.state.action_log.append(
            "/build",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/survey")
    def post_survey(body: SurveyBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.survey(body.x, body.y, body.size)
        # Strip the bulky voxel array out of the action log entry; agents that
        # care can re-read /reservoirs. Keep cost/size/x/y for forensics.
        log_result: Any = None
        if result.get("result"):
            log_result = {k: v for k, v in result["result"].items() if k != "voxels"}
            log_result["n_voxels"] = len(result["result"].get("voxels", []))
        app.state.action_log.append(
            "/survey",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=log_result,
        )
        return result

    @app.get("/reservoirs")
    def get_reservoirs(min_oil: float = 0.0, top_k: int = 100) -> dict[str, Any]:
        if top_k < 1 or top_k > 4096:
            raise HTTPException(status_code=400, detail="top_k must be in [1, 4096]")
        return app.state.world.reservoirs(min_oil=min_oil, top_k=top_k)

    @app.post("/drill")
    def post_drill(body: DrillBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.drill(body.x, body.y, body.target_z, body.well_type)
        app.state.action_log.append(
            "/drill",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/control/well")
    def post_control_well(body: WellControlBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.control_well(body.well_id, body.rate_bbl_day)
        app.state.action_log.append(
            "/control/well",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/control/battery")
    def post_control_battery(body: BatteryControlBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.control_battery(body.tile_id, body.charge_kw)
        app.state.action_log.append(
            "/control/battery",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/control/refinery")
    def post_control_refinery(body: RefineryControlBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.control_refinery(body.refinery_id, body.rate_bbl_day)
        app.state.action_log.append(
            "/control/refinery",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    @app.post("/demolish")
    def post_demolish(body: DemolishBody) -> dict[str, Any]:
        params = body.model_dump()
        result = app.state.world.demolish(body.x, body.y)
        app.state.action_log.append(
            "/demolish",
            params,
            ok=result["ok"],
            error=result.get("error"),
            result=result.get("result"),
        )
        return result

    # Static UI -----------------------------------------------------------
    # `no-store` keeps the hot-reload loop honest during dev: every page load
    # re-fetches app.js / style.css / index.html so working-tree edits show
    # up without a hard-reload. The UI is tiny and local; no caching needed.
    ui_dir = Path(__file__).parent / "ui"
    if ui_dir.exists():
        app.mount("/ui", _NoStoreStaticFiles(directory=ui_dir), name="ui")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(
                ui_dir / "index.html",
                headers={"Cache-Control": "no-store"},
            )

    return app


app = create_app()
