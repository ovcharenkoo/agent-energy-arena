(() => {
  const canvas = document.getElementById("grid");
  const ctx = canvas.getContext("2d");
  const buildList = document.getElementById("buildlist");
  const toastEl = document.getElementById("toast");
  const nextDayBtn = document.getElementById("next-day");
  const prevDayBtn = document.getElementById("prev-day");
  const buildHint = document.getElementById("buildhint");
  const subSurveyBtn = document.getElementById("mode-survey");
  const subDrillBtn = document.getElementById("mode-drill");
  const surveySizeInput = document.getElementById("survey-size");
  const surveyCostEl = document.getElementById("survey-cost-preview");
  const drillCostEl = document.getElementById("drill-cost-preview");
  const modal = document.getElementById("modal");
  const modalBody = document.getElementById("modal-body");
  const modalCancel = document.getElementById("modal-cancel");
  const modalConfirm = document.getElementById("modal-confirm");
  const subTargetEl = document.getElementById("sub-target");
  const drillTypeRadios = () =>
    document.querySelectorAll('input[name="drill-type"]');

  const els = {
    day: document.getElementById("day"),
    treasury: document.getElementById("treasury"),
    population: document.getElementById("population"),
    jobs: document.getElementById("jobs"),
    happiness: document.getElementById("happiness"),
    balance: document.getElementById("balance"),
  };

  const hoverPopupEl = document.getElementById("hover-popup");
  const canvasRowEl = document.getElementById("canvasrow");

  // Replay mode (issue 09). Live mode polls `/state` via `tick()`; replay
  // mode renders end-of-day snapshots out of an in-memory `states` array
  // loaded from a recorded run folder (`metadata.json` + `states.jsonl`).
  // The two modes share the same render helpers — only the data source
  // and the mutating-action gates differ.
  const replay = {
    mode: "live",   // "live" | "replay"
    states: [],     // parsed states.jsonl entries
    actions: [],    // parsed actions.jsonl entries (replay mode only)
    metadata: null, // parsed metadata.json (or null if missing)
    cursor: 0,
  };
  const loadRunBtn = document.getElementById("load-run");
  const replayFilesInput = document.getElementById("replay-files");
  const replayBarEl = document.getElementById("replaybar");
  const replayBadgeEl = document.getElementById("replay-badge");
  const replaySliderEl = document.getElementById("replay-slider");
  const replayDayLabelEl = document.getElementById("replay-day-label");
  const replayBackBtn = document.getElementById("replay-back");
  const replayForwardBtn = document.getElementById("replay-forward");
  const replayCloseBtn = document.getElementById("replay-close");

  function isReplay() { return replay.mode === "replay"; }

  // Agent Play attach state (agent-play slice 03). The mutation lock is
  // load-bearing: while an agent is attached, the human owns the clock and
  // the agent owns world mutations. `isMutationLocked()` is the helper that
  // expresses that invariant — replay mode and an attached agent both lock
  // out world mutations, while clock controls (Play, Fast, Next Day, Prev
  // Day) keep running on `isReplay()` so the human can advance turns.
  // Hoisted here so mutation-guard sites earlier in the file (canvas
  // click/right-click, refinery/well slider rendering) can reference it.
  let attachedAgentFolder = null;
  function isAgentAttached() { return attachedAgentFolder !== null; }
  function isMutationLocked() { return isReplay() || isAgentAttached(); }

  // Live-mode "peek backward" (chess-style review). The recorder writes
  // `runs/<run-id>/states.jsonl` for every live session; `GET
  // /state/history?day=N` returns the entry for day N. While `peekDay`
  // is non-null the UI renders the historical snapshot instead of the
  // live `/state`. Clicking "Next Day" clears the peek and fast-forwards
  // by stepping the server, which is the documented contract: "the
  // world jumps to day N+1 fast-forwarding UI".
  let peekDay = null;
  let lastLiveDay = 0;
  function isPeeking() { return peekDay !== null; }

  // Refinery process-load is 200 kWh/bbl (slice 09). Per-day kWh for the
  // hover popup = throughput × kWh/bbl. CO2 intensity is 0.3 t/bbl (slice 10).
  const REFINERY_KWH_PER_BBL = 200;
  const REFINERY_CO2_T_PER_BBL = 0.3;

  const TILE_COLORS = {
    town_hall: "#d4a72c",
    road: "#6e7177",
    house: "#4ea3ff",
    commercial: "#9d6cff",
    industrial: "#ff7a59",
    demand_response_hub: "#42d6c7",
    park: "#3fbf7f",
    pipeline: "#9ec6e8",
    solar_farm: "#f5d76e",
    wind_turbine: "#6dd5ed",
    coal_plant: "#c97676",
    gas_peaker: "#d09bff",
    battery: "#7be0a3",
    refinery: "#e07a4d",
  };

  const PLANT_TYPES = ["solar_farm", "wind_turbine", "coal_plant", "gas_peaker"];
  const STORAGE_TYPES = ["battery"];

  // Workforce slice 08: staffing badge colours. Bands defined by the PRD:
  // full = 100%, partial = 50–99%, low = 1–49%, idle = 0%. Background +
  // foreground pairs mirror the .balance-badge palette in style.css so the
  // badges blend with the existing UI chrome.
  const STAFFING_BAND_BG = {
    full: "#1d3a25",
    partial: "#3a3119",
    low: "#3a1d1d",
    idle: "#1a1c22",
  };
  const STAFFING_BAND_FG = {
    full: "#b0e8c8",
    partial: "#f0d28e",
    low: "#ffb0b0",
    idle: "#8b8f9a",
  };

  function staffingBand(staffed, jobs) {
    if (jobs <= 0) return null;
    const eff = staffed / jobs;
    if (eff <= 0) return "idle";
    if (eff < 0.5) return "low";
    if (eff < 1.0) return "partial";
    return "full";
  }

  let cols = 32;
  let rows = 32;
  let tiles = [];
  let wells = [];
  // oilfield-v2 slice 09: pipeline-routing orphan sets, populated from
  // /state.{orphan_well_ids, orphan_refinery_ids} every tick. Producers in
  // `orphanWellIds` are selling raw at $40/bbl; refineries in
  // `orphanRefineryIds` have no crude. Empty sets = everyone is connected.
  let orphanWellIds = new Set();
  let orphanRefineryIds = new Set();
  // wells-reservoir-rollup #01: per-reservoir rollup array from /state.
  // One entry per reservoir with ≥1 revealed voxel; drives the Wells-tab
  // grouped renderer (#03). Unsurveyed reservoirs are absent.
  let reservoirsSummary = [];
  let activeEvents = [];
  let historicalEvents = [];
  let summary = {};
  let treasury = 0;
  let catalog = null;       // map of tile_type -> tile spec (buildable only)
  let catalogRaw = null;    // full /catalog payload (incl. subsurface block)
  let selectedType = null;
  let hoverCell = null;

  // Mode state: "build" (legacy selectedType drives detail), "survey",
  // "drill", or null. Only one mode is active at a time; selecting a build
  // tile, the Survey button, or the Drill button deactivates the others.
  let mode = null;
  let surveySize = 4;
  let drillWellType = "production";
  // Locked drill anchor (picked via voxel-click in the Subsurface tab).
  // null until the user picks a voxel. Survives slice-selector changes.
  let drillAnchor = null;
  // Pending modal action — populated when the dry-hole modal is open.
  let pendingDrill = null;

  const REFINERY_YIELD = 0.85;
  // Refinery setpoint cap — read from /catalog so the slider tracks the
  // backend clamp in world.economy.REFINERY_MAX_BBL_DAY (250). Falls back
  // to 250 until /catalog has loaded.
  function refineryMaxBblDay() {
    return (catalogRaw && catalogRaw.economics && catalogRaw.economics.refinery_max_bbl_day) || 250;
  }

  function showToast(msg, kind = "error") {
    toastEl.textContent = msg;
    toastEl.className = `toast show ${kind}`;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toastEl.className = "toast";
    }, 1800);
  }

  function cellSize() {
    return { cw: canvas.width / cols, ch: canvas.height / rows };
  }

  function drawGrid() {
    const w = canvas.width;
    const h = canvas.height;
    const { cw, ch } = cellSize();
    ctx.clearRect(0, 0, w, h);

    for (const t of tiles) {
      ctx.fillStyle = TILE_COLORS[t.type] || "#888";
      ctx.fillRect(t.x * cw, t.y * ch, cw, ch);
      if (t.type === "town_hall") {
        ctx.fillStyle = "#1a1c22";
        ctx.font = `${Math.floor(ch * 0.6)}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("⌂", t.x * cw + cw / 2, t.y * ch + ch / 2);
      }
    }

    // Wells occupy a surface cell (sim.drill rejects co-located builds /
    // re-drills), so render them as tiles in their own right. Production
    // wells get a dark-rust fill + ▼; injection wells get a dark-teal
    // fill + ▲. Matches the symbol convention used in the subsurface
    // cross-section.
    for (const w of wells) {
      ctx.fillStyle = w.type === "production" ? "#5a2f1a" : "#1a3a4d";
      ctx.fillRect(w.x * cw, w.y * ch, cw, ch);
      ctx.fillStyle = w.type === "production" ? "#3fbf7f" : "#a8d8ff";
      ctx.font = `${Math.floor(ch * 0.55)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const symbol = w.type === "production" ? "▼" : "▲";
      ctx.fillText(symbol, w.x * cw + cw / 2, w.y * ch + ch / 2);
    }

    // oilfield-v2 slice 09: orphan badges. Red border on refineries with no
    // crude (pipeline-disconnected from any producer). Yellow border on
    // producers selling raw at $40/bbl (no pipeline path to a refinery).
    // The border sits on top of the tile/well fill but underneath the grid
    // lines so it reads as a property of the cell, not as a selection cue.
    for (const t of tiles) {
      if (t.type === "refinery" && orphanRefineryIds.has(t.id)) {
        ctx.save();
        ctx.strokeStyle = "#ff5050";
        ctx.lineWidth = 2;
        ctx.strokeRect(t.x * cw + 1, t.y * ch + 1, cw - 2, ch - 2);
        ctx.restore();
      }
    }
    for (const w of wells) {
      if (w.type !== "production") continue;
      if (!orphanWellIds.has(w.id)) continue;
      ctx.save();
      ctx.strokeStyle = "#f5d76e";
      ctx.lineWidth = 2;
      ctx.strokeRect(w.x * cw + 1, w.y * ch + 1, cw - 2, ch - 2);
      ctx.restore();
    }

    ctx.strokeStyle = "#2a2d34";
    ctx.lineWidth = 1;
    for (let i = 0; i <= cols; i++) {
      const x = Math.round(i * cw) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    for (let j = 0; j <= rows; j++) {
      const y = Math.round(j * ch) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    if (selectedType && mode === "build" && hoverCell) {
      const valid = isPlacementValid(hoverCell.x, hoverCell.y, selectedType);
      ctx.fillStyle = valid ? "rgba(63,191,127,0.35)" : "rgba(255,80,80,0.35)";
      ctx.fillRect(hoverCell.x * cw, hoverCell.y * ch, cw, ch);
    }

    // Issue 21: explored-columns hatching (visible in all modes).
    const explored = exploredColumnsSet();
    if (explored.size > 0) {
      ctx.save();
      ctx.strokeStyle = "rgba(245,215,110,0.18)";
      ctx.lineWidth = 1;
      for (const key of explored) {
        const [ex, ey] = key.split(",").map(Number);
        if (tileAt(ex, ey)) continue; // tiles already render their own swatch
        // Diagonal hatch: two thin lines per cell.
        const x0 = ex * cw;
        const y0 = ey * ch;
        ctx.beginPath();
        ctx.moveTo(x0, y0 + ch * 0.3);
        ctx.lineTo(x0 + cw * 0.7, y0 + ch);
        ctx.moveTo(x0 + cw * 0.3, y0);
        ctx.lineTo(x0 + cw, y0 + ch * 0.7);
        ctx.stroke();
      }
      ctx.restore();
    }

    // Survey mode: clipped N×N footprint + affordability tint.
    if (mode === "survey" && hoverCell) {
      const [x0, y0, x1, y1] = columnBounds(hoverCell.x, hoverCell.y, surveySize, cols, rows);
      const cost = surveyCost(surveySize) || 0;
      const affordable = treasury >= cost;
      ctx.save();
      ctx.fillStyle = affordable ? "rgba(245,215,110,0.22)" : "rgba(255,80,80,0.28)";
      ctx.fillRect(x0 * cw, y0 * ch, (x1 - x0) * cw, (y1 - y0) * ch);
      ctx.strokeStyle = affordable ? "rgba(245,215,110,0.7)" : "rgba(255,80,80,0.85)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x0 * cw, y0 * ch, (x1 - x0) * cw, (y1 - y0) * ch);
      // Resurvey overlay: cells already in explored set get a darker hatch.
      ctx.strokeStyle = "rgba(245,215,110,0.55)";
      ctx.lineWidth = 1;
      for (let yy = y0; yy < y1; yy++) {
        for (let xx = x0; xx < x1; xx++) {
          if (!explored.has(`${xx},${yy}`)) continue;
          const px = xx * cw;
          const py = yy * ch;
          ctx.beginPath();
          ctx.moveTo(px, py);
          ctx.lineTo(px + cw, py + ch);
          ctx.moveTo(px + cw, py);
          ctx.lineTo(px, py + ch);
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    // Drill mode: crosshair on the locked anchor; color encodes guard state.
    // Three rejection states mirror `drill_collision`:
    //   tile_occupied      → red    (#ff5050)
    //   completion_overlap → gray   (#9aa0a6) — same (x,y), |Δz| < 3
    //   dry hole           → yellow (#f5d76e)
    // Legal target: orange (#ff7a59). A same-(x,y) well at |Δz| ≥ 3 is legal
    // under §4.12 and lands on the orange path.
    if (mode === "drill" && drillAnchor) {
      const collision = drillCollision(drillAnchor.x, drillAnchor.y, drillAnchor.target_z);
      const dryHole = !poolHasHc(drillAnchor.x, drillAnchor.y, drillAnchor.target_z);
      let color;
      if (collision === "tile_occupied") color = "#ff5050";
      else if (collision === "completion_overlap") color = "#9aa0a6";
      else if (dryHole) color = "#f5d76e";
      else color = "#ff7a59";
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      const cx = drillAnchor.x * cw + cw / 2;
      const cy = drillAnchor.y * ch + ch / 2;
      const r = Math.min(cw, ch) * 0.4;
      ctx.beginPath();
      ctx.moveTo(cx - r, cy);
      ctx.lineTo(cx + r, cy);
      ctx.moveTo(cx, cy - r);
      ctx.lineTo(cx, cy + r);
      ctx.stroke();
      ctx.strokeRect(drillAnchor.x * cw + 0.5, drillAnchor.y * ch + 0.5, cw - 1, ch - 1);
      ctx.restore();
    }

    drawStaffingBadges(cw, ch);
  }

  // Per-well jobs counts are not in /state — wells only carry staffed_jobs.
  // Pull the per-type denominator from the full /catalog payload, which
  // puts well specs ("oil_well" / "injection_well") under `.wells`, not
  // `.tiles`. Falling back to `.tiles` keeps older payload shapes working.
  function wellJobsByType() {
    if (!catalogRaw) return { production: 0, injection: 0 };
    const byType = {};
    const wellEntries = Array.isArray(catalogRaw.wells) ? catalogRaw.wells : [];
    const tileEntries = Array.isArray(catalogRaw.tiles) ? catalogRaw.tiles : [];
    for (const entry of [...wellEntries, ...tileEntries]) {
      byType[entry.tile_type] = entry.jobs || 0;
    }
    return {
      production: byType.oil_well || 0,
      injection: byType.injection_well || 0,
    };
  }

  function drawStaffingBadges(cw, ch) {
    const fontPx = Math.max(7, Math.floor(ch * 0.32));
    ctx.save();
    ctx.font = `${fontPx}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const t of tiles) {
      if (!(t.jobs > 0)) continue;
      drawStaffingBadge(t.x, t.y, t.staffed_jobs, t.jobs, cw, ch, fontPx);
    }
    const wellJobs = wellJobsByType();
    for (const w of wells) {
      const jobs = wellJobs[w.type] || 0;
      if (jobs <= 0) continue;
      drawStaffingBadge(w.x, w.y, w.staffed_jobs, jobs, cw, ch, fontPx);
    }
    ctx.restore();
  }

  function drawStaffingBadge(x, y, staffed, jobs, cw, ch, fontPx) {
    const band = staffingBand(staffed, jobs);
    if (!band) return;
    const text = `${staffed}/${jobs}`;
    const padX = 2;
    const padY = 1;
    const bw = Math.min(cw - 1, ctx.measureText(text).width + padX * 2);
    const bh = fontPx + padY * 2;
    const bx = x * cw + cw - bw - 1;
    const by = y * ch + 1;
    ctx.fillStyle = STAFFING_BAND_BG[band];
    ctx.fillRect(bx, by, bw, bh);
    ctx.fillStyle = STAFFING_BAND_FG[band];
    ctx.fillText(text, bx + bw / 2, by + bh / 2);
  }

  function tileAt(x, y) {
    return tiles.find((t) => t.x === x && t.y === y) || null;
  }

  function wellAt(x, y) {
    return wells.find((w) => w.x === x && w.y === y) || null;
  }

  function roadNetwork() {
    const start = tiles.find((t) => t.type === "town_hall");
    if (!start) return new Set();
    const isRoad = new Map();
    for (const t of tiles) {
      if (t.type === "road" || t.type === "town_hall") {
        isRoad.set(`${t.x},${t.y}`, t);
      }
    }
    const seen = new Set([`${start.x},${start.y}`]);
    const stack = [[start.x, start.y]];
    while (stack.length) {
      const [x, y] = stack.pop();
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const nx = x + dx;
        const ny = y + dy;
        const key = `${nx},${ny}`;
        if (seen.has(key)) continue;
        if (!isRoad.has(key)) continue;
        seen.add(key);
        stack.push([nx, ny]);
      }
    }
    return seen;
  }

  function isPlacementValid(x, y, tileType) {
    if (!catalog) return false;
    if (x < 0 || y < 0 || x >= cols || y >= rows) return false;
    if (tileAt(x, y)) return false;
    const spec = catalog[tileType];
    if (!spec) return false;
    if (treasury < spec.capex) return false;
    if (spec.requires_road) {
      const net = roadNetwork();
      const adj = [[x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]];
      if (!adj.some(([ax, ay]) => net.has(`${ax},${ay}`))) return false;
    }
    return true;
  }

  async function loadCatalog() {
    try {
      const res = await fetch("/catalog");
      const data = await res.json();
      catalogRaw = data;
      catalog = {};
      for (const entry of data.tiles) {
        if (entry.buildable) catalog[entry.tile_type] = entry;
      }
      renderBuildMenu();
      updateSurveyCostPreview();
      updateDrillCostPreview();
    } catch (err) {
      console.error("catalog load failed", err);
    }
  }

  // Mirrors world.subsurface._column_bounds(x, y, size, world_w, world_h).
  // Returns inclusive-exclusive [x0, x1) × [y0, y1) range clipped to grid.
  function columnBounds(x, y, size, w, h) {
    const half = Math.floor(size / 2);
    return [
      Math.max(0, x - half),
      Math.max(0, y - half),
      Math.min(w, x - half + size),
      Math.min(h, y - half + size),
    ];
  }

  // base_cost * (size / base_size)**2 — matches world.subsurface.survey_cost.
  // `base_size` is the SEISMIC_DEFAULT_SIZE constant (4 under oilfield-v2),
  // so the formula is equivalent to the catalog-advertised `base * (size/4)**2`.
  // Returns null until /catalog has loaded.
  function surveyCost(size) {
    if (!catalogRaw || !catalogRaw.subsurface) return null;
    const s = catalogRaw.subsurface.survey;
    return s.base_cost * Math.pow(size / s.base_size, 2);
  }

  function drillCapex(wellType, targetZ) {
    if (!catalogRaw || !catalogRaw.subsurface) return 0;
    const d = catalogRaw.subsurface.drill[wellType];
    const base = d.capex;
    const worldD = d.world_depth;
    if (typeof targetZ !== "number" || !worldD) return base;
    return base * (1 + Math.pow(targetZ / worldD, 2));
  }

  function clampSurveySize(raw) {
    let n = parseInt(raw, 10);
    if (isNaN(n)) return surveySize;
    const s = catalogRaw && catalogRaw.subsurface ? catalogRaw.subsurface.survey : { min_size: 4, max_size: 16 };
    if (n < s.min_size) n = s.min_size;
    if (n > s.max_size) n = s.max_size;
    return n;
  }

  function updateSurveyCostPreview() {
    if (!surveyCostEl) return;
    const c = surveyCost(surveySize);
    surveyCostEl.textContent = c == null ? "—" : `$${Math.round(c).toLocaleString()}`;
  }

  function updateDrillCostPreview() {
    if (!drillCostEl) return;
    if (!catalogRaw || !catalogRaw.subsurface) {
      drillCostEl.textContent = "—";
      return;
    }
    if (!drillAnchor) {
      const base = drillCapex(drillWellType, 0);
      drillCostEl.textContent = `from $${Math.round(base).toLocaleString()} · pick voxel`;
      return;
    }
    const c = drillCapex(drillWellType, drillAnchor.target_z);
    drillCostEl.textContent = `$${Math.round(c).toLocaleString()} @ z=${drillAnchor.target_z}`;
  }

  // Set of "x,y" keys for columns that have at least one revealed voxel.
  // Mirrors `subsurface.explored_columns` for HC-bearing columns; truly
  // empty surveyed columns are not in /reservoirs so the overlay won't mark
  // them — acceptable for a hover hint (server is the source of truth).
  function exploredColumnsSet() {
    const s = new Set();
    for (const v of revealedVoxels) s.add(`${v.x},${v.y}`);
    return s;
  }

  // Mirrors `world.subsurface.drill_collision`: returns "tile_occupied" if a
  // non-well tile (road, refinery, ...) blocks the surface; "completion_overlap"
  // if another well sits at the same (x, y) with |Δz| < 3 (the relaxed §4.12
  // rule); null otherwise. Tile check runs first so the more fundamental
  // build-side rule wins when both apply.
  function drillCollision(x, y, targetZ) {
    if (tileAt(x, y)) return "tile_occupied";
    for (const w of wells) {
      if (w.x === x && w.y === y && Math.abs(w.target_z - targetZ) < 3) {
        return "completion_overlap";
      }
    }
    return null;
  }

  // Dry-hole guard: returns true iff no known HC voxel sits within ±1 of
  // (x, y, target_z). Uses the /reservoirs?top_k=4096 payload that the
  // Subsurface tab already polls.
  function poolHasHc(x, y, z) {
    for (const v of revealedVoxels) {
      if (Math.abs(v.x - x) <= 1 && Math.abs(v.y - y) <= 1 && Math.abs(v.z - z) <= 1) {
        if (v.oil_estimate_bbl > 0) return true;
      }
    }
    return false;
  }

  function setMode(next) {
    mode = next;
    if (next !== "build") {
      selectedType = null;
      for (const node of buildList.children) node.classList.remove("selected");
    }
    if (next !== "drill") {
      drillAnchor = null;
    }
    updateDrillCostPreview();
    if (subSurveyBtn) subSurveyBtn.classList.toggle("selected", next === "survey");
    if (subDrillBtn) subDrillBtn.classList.toggle("selected", next === "drill");
    canvas.classList.toggle("crosshair", next === "survey" || next === "drill");
    refreshBuildHint();
    drawGrid();
    // Subsurface widget is always on screen now (lives under the canvas in
    // the Map tab); voxel-pickability + anchor highlight depend on `mode`
    // and `drillAnchor`, so re-render unconditionally.
    renderSubsurface();
  }

  function refreshBuildHint() {
    if (!buildHint) return;
    if (mode === "survey") {
      buildHint.textContent =
        "Survey mode — click canvas to anchor. Right-click or Esc to cancel.";
    } else if (mode === "drill") {
      const target = drillAnchor
        ? ` Locked at (${drillAnchor.x}, ${drillAnchor.y}, z=${drillAnchor.target_z}).`
        : "";
      buildHint.textContent =
        `Drill mode — pick a voxel in the Subsurface tab, then click the canvas.${target} Right-click or Esc to cancel.`;
    } else if (mode === "build" && selectedType) {
      buildHint.textContent = `Build mode — click a grid cell to place ${selectedType}. Right-click to demolish.`;
    } else {
      buildHint.textContent = "Click a tile type, then click the grid. Right-click a tile to demolish.";
    }
  }

  function renderBuildMenu() {
    if (!catalog) return;
    buildList.innerHTML = "";
    const order = [
      "road",
      "house",
      "commercial",
      "industrial",
      "demand_response_hub",
      "park",
      "pipeline",
      "refinery",
      "solar_farm",
      "wind_turbine",
      "gas_peaker",
      "coal_plant",
      "battery",
    ];
    for (const tt of order) {
      const spec = catalog[tt];
      if (!spec) continue;
      const li = document.createElement("li");
      li.dataset.type = tt;
      li.className = "buildItem";
      li.innerHTML = `
        <span class="swatch" style="background:${TILE_COLORS[tt] || "#888"}"></span>
        <div class="bi-text">
          <div class="bi-name">${tt}</div>
          <div class="bi-desc">${spec.description}</div>
          <div class="bi-cost">$${spec.capex.toLocaleString()} · $${spec.opex_per_day}/day</div>
        </div>
      `;
      li.addEventListener("click", () => {
        const turningOff = selectedType === tt;
        selectedType = turningOff ? null : tt;
        for (const node of buildList.children) node.classList.remove("selected");
        if (selectedType) li.classList.add("selected");
        setMode(selectedType ? "build" : null);
      });
      buildList.appendChild(li);
    }
  }

  function gridCellFromEvent(ev) {
    const rect = canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    return {
      x: Math.floor((px / rect.width) * cols),
      y: Math.floor((py / rect.height) * rows),
    };
  }

  // Subsurface tool palette wiring — event delegation on the parent <ul> so
  // clicks on any nested element (swatch, text, cost-preview span) route to
  // the right mode toggle. The size input and well-type radios sit inside the
  // <li>s; we ignore clicks on form controls so they keep their native
  // behaviour (editing the number, picking a radio).
  const subList = document.getElementById("sublist");
  if (subList) {
    subList.addEventListener("click", (ev) => {
      const tag = ev.target.tagName;
      if (tag === "INPUT" || tag === "LABEL") return;
      const li = ev.target.closest("li.modeItem");
      if (!li) return;
      const target = li.dataset.mode;
      if (target !== "survey" && target !== "drill") return;
      setMode(mode === target ? null : target);
    });
  }
  if (surveySizeInput) {
    surveySizeInput.addEventListener("input", () => {
      surveySize = clampSurveySize(surveySizeInput.value);
      // Don't snap the input while typing; only refresh the preview text.
      updateSurveyCostPreview();
      if (mode === "survey") drawGrid();
    });
    surveySizeInput.addEventListener("blur", () => {
      surveySize = clampSurveySize(surveySizeInput.value);
      surveySizeInput.value = String(surveySize);
      updateSurveyCostPreview();
      drawGrid();
    });
  }
  for (const radio of drillTypeRadios()) {
    radio.addEventListener("change", () => {
      drillWellType = radio.value;
      updateDrillCostPreview();
      drawGrid();
    });
  }

  // Modal wiring. Default state in CSS is `display: none` on the bare
  // `.modal-backdrop`; JS toggles a `.show` class to reveal. This survives
  // any cache mix because the modal is hidden until explicitly opened.
  function hideModal() {
    if (modal) modal.classList.remove("show");
    pendingDrill = null;
  }
  function showModal(bodyText, onConfirm) {
    if (!modal) return;
    modalBody.textContent = bodyText;
    modal.classList.add("show");
    pendingDrill = onConfirm;
  }
  if (modalCancel) modalCancel.addEventListener("click", () => hideModal());
  if (modalConfirm) {
    modalConfirm.addEventListener("click", async () => {
      const fn = pendingDrill;
      hideModal();
      if (fn) await fn();
    });
  }

  window.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (modal && modal.classList.contains("show")) {
      hideModal();
      return;
    }
    if (mode) setMode(null);
  });

  function fmtMoney(v) {
    const sign = v < 0 ? "-" : "";
    return `${sign}$${Math.abs(Math.round(v)).toLocaleString()}`;
  }
  function fmtNum(v, digits = 0) {
    if (v == null) return "—";
    return Number(v).toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function catalogSpec(tileType) {
    if (!catalogRaw || !Array.isArray(catalogRaw.tiles)) return null;
    return catalogRaw.tiles.find((e) => e.tile_type === tileType) || null;
  }

  function row(label, val, cls = "") {
    return `<div class="hp-row"><span class="hp-label">${label}</span><span class="hp-val ${cls}">${val}</span></div>`;
  }
  const sep = () => `<div class="hp-sep"></div>`;

  function buildTilePopup(t) {
    const spec = catalogSpec(t.type);
    const title = `<div class="hp-title"><span>${t.type.replace(/_/g, " ")}</span><span class="hp-coord">(${t.x}, ${t.y}) · day ${t.built_day}</span></div>`;
    const rows = [];
    rows.push(row("CAPEX paid", fmtMoney(t.capex_paid || 0)));
    rows.push(row("OPEX / day", fmtMoney(-(t.opex_per_day || 0)), "neg"));
    if (t.housing_capacity > 0) {
      rows.push(row("Housing capacity", `${t.housing_capacity}`, "pos"));
    }
    if (t.jobs > 0) {
      const band = staffingBand(t.staffed_jobs || 0, t.jobs);
      const cls = band === "full" ? "pos" : band === "idle" || band === "low" ? "neg" : "warn";
      rows.push(row("Jobs (staffed / total)", `${t.staffed_jobs || 0} / ${t.jobs}`, cls));
    }
    // Demand-tile (commercial/industrial).
    if ((t.demand_kw || 0) > 0 && !(spec && spec.capacity_kw > 0)) {
      const note = t.type === "commercial" ? " peak (8–20h)" : " continuous";
      rows.push(row("Demand", `${fmtNum(t.demand_kw)} kW${note}`, "warn"));
    }
    // Battery — SoC + manual setpoint. Skip the generator "current output"
    // row since batteries don't stamp `current_output_kw` per hour.
    if (t.type === "battery") {
      const maxKwh = (spec && spec.storage_kwh) || 0;
      const ratedKw = (spec && spec.capacity_kw) || 0;
      const eta = (spec && spec.round_trip_efficiency) || 0;
      const soc = t.soc_kwh || 0;
      const socPct = maxKwh > 0 ? (100 * soc / maxKwh).toFixed(0) : "—";
      const sp = t.charge_setpoint_kw || 0;
      let spLbl = "auto (charge surplus / discharge residual)";
      let spCls = "";
      if (sp > 0) { spLbl = `charge ${fmtNum(sp, 1)} kW (manual)`; spCls = "warn"; }
      else if (sp < 0) { spLbl = `discharge ${fmtNum(-sp, 1)} kW (manual)`; spCls = "warn"; }
      rows.push(row("Rated power", `${fmtNum(ratedKw)} kW (charge/discharge)`));
      rows.push(row("Storage", `${fmtNum(maxKwh)} kWh · ${(eta * 100).toFixed(0)}% round-trip`));
      rows.push(row("State of charge", `${fmtNum(soc, 1)} / ${fmtNum(maxKwh)} kWh (${socPct}%)`, "pos"));
      rows.push(row("Setpoint", spLbl, spCls));
    }
    // Generators (excluding batteries — they have their own block above).
    if (spec && spec.capacity_kw > 0 && t.type !== "battery") {
      const cap = spec.capacity_kw;
      const out = t.current_output_kw || 0;
      const pct = cap > 0 ? (100 * out / cap).toFixed(0) : "—";
      rows.push(row("Capacity", `${fmtNum(cap)} kW`));
      rows.push(row("Current output", `${fmtNum(out, 1)} kW (${pct}%)`, "pos"));
      const kwhYesterday = t.kwh_served_yesterday || 0;
      rows.push(row("Served (yest.)", `${fmtNum(kwhYesterday, 0)} kWh`, "pos"));
      // Per-facility economics (facility-economics-popup slice 04). All
      // dollar and tonnage figures come from server-stamped /state fields
      // and reconcile with Net by eye. Renewables show $0 explicitly so the
      // contrast with fossils is visible.
      const co2 = t.estimated_co2_per_day || 0;
      const fuelCost = t.estimated_fuel_cost_per_day || 0;
      const carbonCost = t.estimated_carbon_cost_per_day || 0;
      const revenue = t.estimated_revenue_per_day || 0;
      const net = t.estimated_net_per_day || 0;
      const isFossil = (spec.fuel_cost_per_mwh || 0) > 0 || (spec.co2_t_per_mwh || 0) > 0;
      const co2Cls = co2 > 0 ? "neg" : "pos";
      rows.push(row("CO₂ / day", `${fmtNum(co2, 2)} t`, co2Cls));
      rows.push(row("Fuel cost / day", fmtMoney(-fuelCost), fuelCost > 0 ? "neg" : "pos"));
      rows.push(row("Carbon cost / day", fmtMoney(-carbonCost), carbonCost > 0 ? "neg" : "pos"));
      if (!isFossil) {
        rows.push(row("Emissions", "0 (renewable)", "pos"));
      }
      rows.push(row("Revenue / day (est.)", fmtMoney(revenue), "pos"));
      rows.push(row("Net / day", fmtMoney(net), net >= 0 ? "pos" : "neg"));
    }
    // Refinery process-load + product economics. Slice-05 of the
    // facility-economics-popup PRD: CO2 / Carbon cost / Revenue / Net rows
    // are server-stamped from /state so the client never re-derives them.
    if (t.type === "refinery") {
      const throughput = t.current_throughput_bbl_day || 0;
      const setpoint = t.setpoint_rate_bbl_day || 0;
      const procKw = (throughput * REFINERY_KWH_PER_BBL) / 24;
      const refined = throughput * REFINERY_YIELD;
      const co2 = t.estimated_co2_per_day || 0;
      const carbonCost = t.estimated_carbon_cost_per_day || 0;
      const revenue = t.estimated_revenue_per_day || 0;
      const net = t.estimated_net_per_day || 0;
      rows.push(row("Setpoint", `${fmtNum(setpoint)} bbl/d`));
      rows.push(row("Throughput (yest.)", `${fmtNum(throughput, 1)} bbl/d`, "pos"));
      rows.push(row("Refined yield", `${fmtNum(refined, 1)} bbl (85%)`, "pos"));
      rows.push(row("Process load", `${fmtNum(procKw, 1)} kW avg`, "warn"));
      rows.push(row("CO₂ / day", `${fmtNum(co2, 2)} t`, "neg"));
      rows.push(row("Carbon cost / day", fmtMoney(-carbonCost), "neg"));
      rows.push(row("Revenue / day", fmtMoney(revenue), "pos"));
      rows.push(row("Net / day", fmtMoney(net), net >= 0 ? "pos" : "neg"));
    }
    // Industrial economics (slice 01 of facility-economics-popup PRD). All
    // four rows come from server-stamped /state fields; the Net row is
    // server-computed so the UI never re-derives it client-side.
    if (t.type === "commercial") {
      const residents = t.residents_in_radius || 0;
      const revenue = t.estimated_revenue_per_day || 0;
      const net = t.estimated_net_per_day || 0;
      rows.push(row("Residents served", fmtNum(residents, 1), "pos"));
      rows.push(row("Revenue / day", fmtMoney(revenue), "pos"));
      rows.push(row("Net / day", fmtMoney(net), net >= 0 ? "pos" : "neg"));
    }
    if (t.type === "industrial") {
      const co2 = t.estimated_co2_per_day || 0;
      const carbonCost = t.estimated_carbon_cost_per_day || 0;
      const revenue = t.estimated_revenue_per_day || 0;
      const net = t.estimated_net_per_day || 0;
      rows.push(row("CO₂ / day", `${fmtNum(co2, 2)} t`, "neg"));
      rows.push(row("Carbon cost / day", fmtMoney(-carbonCost), "neg"));
      rows.push(row("Revenue / day", fmtMoney(revenue), "pos"));
      rows.push(row("Net / day", fmtMoney(net), net >= 0 ? "pos" : "neg"));
    }
    if (t.type === "town_hall") {
      rows.push(row("Civic center", "counts as road", "pos"));
    }
    if (t.type === "park") {
      rows.push(row("Effect", "+0.05 happiness / extra park", "pos"));
    }
    if (!t.operational) {
      rows.push(row("Status", "non-operational", "neg"));
    }
    let note = "";
    if (spec && spec.description) {
      note = `<div class="hp-note">${spec.description}</div>`;
    }
    return title + sep() + rows.join("") + note;
  }

  function buildWellPopup(w) {
    const wellJobs = wellJobsByType();
    const jobs = wellJobs[w.type] || 0;
    const staffed = w.staffed_jobs || 0;
    const band = staffingBand(staffed, jobs);
    const jcls = band === "full" ? "pos" : band === "idle" || band === "low" ? "neg" : "warn";
    const title = `<div class="hp-title"><span>${w.type} well</span><span class="hp-coord">(${w.x}, ${w.y}, z=${w.target_z}) · day ${w.drilled_day}</span></div>`;
    const rows = [];
    rows.push(row("ID", w.id));
    rows.push(row("CAPEX paid", fmtMoney(w.capex_paid || 0)));
    rows.push(row("OPEX / day", fmtMoney(-(w.opex_per_day || 0)), "neg"));
    if (jobs > 0) {
      rows.push(row("Jobs (staffed / total)", `${staffed} / ${jobs}`, jcls));
    }
    const setpoint = w.setpoint_rate_bbl_day || 0;
    const rate = w.current_rate_bbl_day || 0;
    rows.push(row("Setpoint", `${fmtNum(setpoint)} bbl/d`));
    rows.push(row("Actual rate", `${fmtNum(rate, 1)} bbl/d`, "pos"));
    const revenue = w.estimated_revenue_per_day || 0;
    const net = w.estimated_net_per_day || 0;
    const ncls = net > 0 ? "pos" : net < 0 ? "neg" : "";
    const reservoirLabel =
      w.reservoir_id === null || w.reservoir_id === undefined ? "—" : `R${w.reservoir_id}`;
    rows.push(row("Reservoir", reservoirLabel));
    if (w.type === "production") {
      const yProd = w.yesterday_rate_bbl_day || 0;
      const yInj = w.yesterday_inj_rate_bbl_day || 0;
      const boost = w.pressure_boost || 0;
      const bcls = boost > 0 ? "pos" : "";
      rows.push(row("Pressure boost", `${(boost * 100).toFixed(1)}%`, bcls));
      rows.push(row("Yesterday prod rate", `${fmtNum(yProd, 1)} bbl/d`));
      rows.push(row("Yesterday inj rate (qualifying)", `${fmtNum(yInj, 1)} bbl/d`));
      rows.push(row("Cumulative produced", `${fmtNum(w.cumulative_produced_bbl || 0)} bbl`, "pos"));
      rows.push(row("Gross crude value (est.) / day", fmtMoney(revenue), "pos"));
      rows.push(row("Net / day", fmtMoney(net), ncls));
    } else {
      const yInj = w.yesterday_rate_bbl_day || 0;
      rows.push(row("Yesterday inj rate", `${fmtNum(yInj, 1)} bbl/d`));
      const injKwh = w.injection_power_kwh_per_day || 0;
      rows.push(row("Injection load", `${fmtNum(injKwh / 24, 1)} kW avg`, "warn"));
      rows.push(row("Power consumed / day", `${fmtNum(injKwh)} kWh`, "warn"));
      rows.push(row("Cumulative injected", `${fmtNum(w.cumulative_injected_bbl || 0)} bbl`));
      rows.push(row("Net / day", fmtMoney(net), ncls));
    }
    return title + sep() + rows.join("");
  }

  function updateHoverPopup(ev) {
    if (!hoverPopupEl || !canvasRowEl) return;
    if (!hoverCell) {
      hoverPopupEl.classList.add("hidden");
      return;
    }
    const tile = tileAt(hoverCell.x, hoverCell.y);
    const well = wellAt(hoverCell.x, hoverCell.y);
    if (!tile && !well) {
      hoverPopupEl.classList.add("hidden");
      return;
    }
    hoverPopupEl.innerHTML = tile ? buildTilePopup(tile) : buildWellPopup(well);
    hoverPopupEl.classList.remove("hidden");
    // Position relative to #canvasrow (canvasrow is position:relative).
    const rect = canvasRowEl.getBoundingClientRect();
    const offset = 14;
    let px = ev.clientX - rect.left + offset;
    let py = ev.clientY - rect.top + offset;
    const popupRect = hoverPopupEl.getBoundingClientRect();
    if (px + popupRect.width > rect.width) {
      px = ev.clientX - rect.left - popupRect.width - offset;
    }
    if (py + popupRect.height > rect.height) {
      py = rect.height - popupRect.height - 4;
    }
    hoverPopupEl.style.left = `${Math.max(0, px)}px`;
    hoverPopupEl.style.top = `${Math.max(0, py)}px`;
  }

  canvas.addEventListener("mousemove", (ev) => {
    const cell = gridCellFromEvent(ev);
    if (!hoverCell || hoverCell.x !== cell.x || hoverCell.y !== cell.y) {
      hoverCell = cell;
      drawGrid();
    }
    updateHoverTooltip();
    updateHoverPopup(ev);
  });
  canvas.addEventListener("mouseleave", () => {
    hoverCell = null;
    canvas.title = "";
    if (hoverPopupEl) hoverPopupEl.classList.add("hidden");
    drawGrid();
  });

  function updateHoverTooltip() {
    if (!hoverCell) {
      canvas.title = "";
      return;
    }
    if (mode === "survey") {
      const [x0, y0, x1, y1] = columnBounds(hoverCell.x, hoverCell.y, surveySize, cols, rows);
      const cells = (x1 - x0) * (y1 - y0);
      const cost = surveyCost(surveySize) || 0;
      const explored = exploredColumnsSet();
      let prior = 0;
      for (let yy = y0; yy < y1; yy++) {
        for (let xx = x0; xx < x1; xx++) {
          if (explored.has(`${xx},${yy}`)) prior += 1;
        }
      }
      const label = prior > 0 ? "resurvey" : `survey @ (${hoverCell.x}, ${hoverCell.y}) size=${surveySize}`;
      const priorNote = prior > 0 ? ` (${prior} previously surveyed)` : "";
      canvas.title = `${label} · cost $${Math.round(cost).toLocaleString()} · ${cells} cells${priorNote}`;
    } else if (mode === "drill") {
      if (drillAnchor) {
        const collision = drillCollision(drillAnchor.x, drillAnchor.y, drillAnchor.target_z);
        const base = `drill @ (${drillAnchor.x}, ${drillAnchor.y}) target_z=${drillAnchor.target_z} type=${drillWellType}`;
        if (collision === "tile_occupied") {
          canvas.title = `${base} — occupied (surface tile)`;
        } else if (collision === "completion_overlap") {
          canvas.title = `${base} — occupied — Δz too small`;
        } else {
          canvas.title = base;
        }
      } else {
        canvas.title = "Pick a voxel in the Subsurface tab first to lock a drill target.";
      }
    } else {
      canvas.title = "";
    }
  }

  canvas.addEventListener("click", async (ev) => {
    if (isMutationLocked()) return;
    const { x, y } = gridCellFromEvent(ev);
    if (mode === "survey") {
      await handleSurveyClick(x, y);
      return;
    }
    if (mode === "drill") {
      await handleDrillClick(x, y);
      return;
    }
    if (!selectedType) return;
    try {
      const res = await fetch("/build", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ tile_type: selectedType, x, y }),
      });
      const body = await res.json();
      if (body.ok) showToast(`built ${selectedType} at (${x}, ${y})`, "ok");
      else showToast(`build rejected: ${body.error}`, "error");
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  });

  canvas.addEventListener("contextmenu", async (ev) => {
    ev.preventDefault();
    if (isMutationLocked()) return;
    // While a subsurface mode is active, right-click cancels the mode and
    // does NOT fall through to /demolish.
    if (mode === "survey" || mode === "drill") {
      setMode(null);
      return;
    }
    const { x, y } = gridCellFromEvent(ev);
    try {
      const res = await fetch("/demolish", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ x, y }),
      });
      const body = await res.json();
      if (body.ok) showToast(`demolished at (${x}, ${y})`, "ok");
      else showToast(`demolish rejected: ${body.error}`, "error");
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  });

  async function handleSurveyClick(x, y) {
    const cost = surveyCost(surveySize) || 0;
    if (treasury < cost) {
      showToast(`survey rejected: insufficient_funds`, "error");
      return;
    }
    try {
      const res = await fetch("/survey", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ x, y, size: surveySize }),
      });
      const body = await res.json();
      if (body.ok) {
        const voxList = (body.result && body.result.voxels) || [];
        const nHc = voxList.filter((v) => (v.oil_estimate_bbl || 0) > 0).length;
        showToast(`survey done — ${nHc} HC voxels revealed · click to view`, "ok-bridge");
        // Toast click scrolls the subsurface widget into view + jumps to
        // slice=anchor_y. The widget lives under the canvas in the Map tab
        // so there's no tab to switch — just focus it.
        toastEl.onclick = () => {
          if (subAxisEl) subAxisEl.value = "y";
          if (subSliceEl) {
            subSliceEl.value = String(y);
            renderSubsurface();
          }
          const subPanel = document.getElementById("subsurfacepanel");
          if (subPanel) subPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
          toastEl.className = "toast";
          toastEl.onclick = null;
        };
      } else {
        showToast(`survey rejected: ${body.error}`, "error");
      }
      await refreshRevealed();
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  async function handleDrillClick(_surfaceX, _surfaceY) {
    if (!drillAnchor) {
      showToast("pick a voxel in the Subsurface tab first", "error");
      return;
    }
    const { x: ax, y: ay, target_z: az } = drillAnchor;
    const collision = drillCollision(ax, ay, az);
    if (collision === "tile_occupied") {
      showToast("drill rejected: occupied", "error");
      return;
    }
    if (collision === "completion_overlap") {
      showToast("drill rejected: occupied — Δz too small", "error");
      return;
    }
    const fire = () => fireDrill(ax, ay);
    if (!poolHasHc(ax, ay, az)) {
      const capex = drillCapex(drillWellType, az);
      showModal(
        `No surveyed HC voxels in the 3×3×3 drainage pool around (${ax}, ${ay}, ${az}). The well may produce 0 bbl/day — $${capex.toLocaleString()} CAPEX at risk.`,
        fire,
      );
      return;
    }
    await fire();
  }

  async function fireDrill(surfaceX, surfaceY) {
    try {
      const res = await fetch("/drill", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          x: surfaceX,
          y: surfaceY,
          target_z: drillAnchor.target_z,
          well_type: drillWellType,
        }),
      });
      const body = await res.json();
      if (body.ok) {
        const wellId = body.result && (body.result.id || body.result.well_id) || "";
        showToast(
          `drilled ${drillWellType} well at (${surfaceX}, ${surfaceY}, ${drillAnchor.target_z})${wellId ? ` — id=${wellId}` : ""}`,
          "ok",
        );
        drillAnchor = null;
        updateDrillCostPreview();
        renderSubsurface();
        refreshBuildHint();
      } else {
        showToast(`drill rejected: ${body.error}`, "error");
      }
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  nextDayBtn.addEventListener("click", async () => {
    if (isReplay()) return;
    // Clear peek FIRST so tick() is not suppressed by the peeking guard.
    // From the user's seat: peek at day N-3 → click Next Day → world steps
    // N → N+1 and the UI snaps forward to N+1 (live).
    clearPeek();
    nextDayBtn.disabled = true;
    try {
      await fetch("/step", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ days: 1 }),
      });
      tick();
    } finally {
      nextDayBtn.disabled = false;
    }
  });

  if (prevDayBtn) prevDayBtn.addEventListener("click", () => peekStep(-1));

  const resetBtn = document.getElementById("reset-btn");
  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      if (isReplay()) return;
      resetBtn.disabled = true;
      try {
        const res = await fetch("/reset", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!res.ok) {
          let detail = "";
          try {
            const body = await res.json();
            detail = body.detail || "";
          } catch (err) {
            // ignore
          }
          showToast(`reset rejected: ${detail || res.status}`, "error");
          return;
        }
        clearPeek();
        await tick();
      } catch (err) {
        showToast(`network error: ${err}`, "error");
      } finally {
        resetBtn.disabled = false;
      }
    });
  }

  // Tab switching ---------------------------------------------------------
  const tabButtons = document.querySelectorAll(".tab");
  const tabPanels = document.querySelectorAll(".tabpanel");
  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      for (const b of tabButtons) b.classList.toggle("active", b === btn);
      for (const p of tabPanels) p.classList.toggle("active", p.id === `tab-${target}`);
    });
  }

  // Power tab rendering ---------------------------------------------------
  const chartEl = document.getElementById("powerchart");
  const plantListEl = document.getElementById("plantlist");
  const marginEl = document.getElementById("power-margin");

  function renderPowerChart(preview, yesterday) {
    if (!chartEl) return;
    chartEl.innerHTML = "";
    const pSupply = (preview && preview.supply_kw_by_hour) || [];
    const pDemand = (preview && preview.demand_kw_by_hour) || [];
    const ySupply = (yesterday && yesterday.supply) || [];
    const yDemand = (yesterday && yesterday.demand) || [];
    const haveProjection = pSupply.length > 0 && pDemand.length > 0;
    const haveYesterday = ySupply.length > 0 && yDemand.length > 0;
    if (!haveProjection && !haveYesterday) {
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", "50%");
      t.setAttribute("y", "50%");
      t.setAttribute("fill", "#5a5d65");
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("font-size", "12");
      t.textContent = "no data";
      chartEl.appendChild(t);
      return;
    }
    const W = 480;
    const H = 200;
    const padX = 28;
    const padY = 8;
    const allVals = [...pSupply, ...pDemand, ...ySupply, ...yDemand, 1];
    const maxY = Math.max(...allVals) * 1.1;
    const path = (series, color, opts = {}) => {
      if (series.length === 0) return;
      const pts = series.map((v, i) => {
        const x = padX + (i / Math.max(1, series.length - 1)) * (W - padX * 2);
        const y = H - padY - (v / maxY) * (H - padY * 2);
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const w = opts.width || 2;
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", pts.join(" "));
      p.setAttribute("stroke", color);
      p.setAttribute("stroke-width", String(w));
      p.setAttribute("fill", "none");
      if (opts.opacity != null) p.setAttribute("stroke-opacity", String(opts.opacity));
      if (opts.dashed) {
        // Scale the dash pattern with stroke width so a wide supply band and
        // a thin demand line both read as visibly dashed. A flat "4 3" looks
        // dashed at width 2 but reads as near-continuous at width 4 because
        // the gap is smaller than the stroke is tall.
        const dash = (w * 2.5).toFixed(1);
        const gap = (w * 1.75).toFixed(1);
        p.setAttribute("stroke-dasharray", `${dash} ${gap}`);
      }
      chartEl.appendChild(p);
    };
    // Y-axis label (max).
    const lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
    lbl.setAttribute("x", "4");
    lbl.setAttribute("y", "14");
    lbl.setAttribute("fill", "#5a5d65");
    lbl.setAttribute("font-size", "10");
    lbl.textContent = `${Math.round(maxY)} kW`;
    chartEl.appendChild(lbl);
    // Layering: all supply (wider, semi-transparent envelopes) underneath,
    // then both demand lines on top. Otherwise the projected-supply band
    // paints over the yesterday-demand dashed line wherever they overlap,
    // making the dashed line look intermittent. Within each layer:
    // yesterday (dashed, dimmer) below projection.
    path(ySupply, "#4ea3ff", { dashed: true, width: 4, opacity: 0.35 });
    path(pSupply, "#4ea3ff", { width: 4, opacity: 0.45 });
    path(yDemand, "#ff7a59", { dashed: true, width: 2, opacity: 0.7 });
    path(pDemand, "#ff7a59", { width: 2 });
  }

  function renderPowerMargin(preview) {
    if (!marginEl) return;
    if (!preview || !preview.demand_kw_by_hour || preview.demand_kw_by_hour.length === 0) {
      marginEl.textContent = "";
      marginEl.className = "power-margin";
      return;
    }
    const peakD = preview.peak_demand_kw || 0;
    const peakS = preview.peak_supply_kw || 0;
    const margin = preview.min_reserve_margin || 0;
    const pct = (margin * 100).toFixed(0);
    let tone = "ok";
    if (margin < 0) tone = "blackout";
    else if (margin < 0.15) tone = "brownout";
    else if (margin > 0.5) tone = "curtailment";
    marginEl.className = `power-margin ${tone}`;
    marginEl.innerHTML =
      `<span class="pm-label">Worst-hour reserve</span>` +
      `<span class="pm-val">${pct >= 0 ? "+" : ""}${pct}%</span>` +
      `<span class="pm-sub">peak ${Math.round(peakD)} kW demand · ${Math.round(peakS)} kW supply</span>`;
  }

  function renderPlantList(allTiles) {
    if (!plantListEl) return;
    plantListEl.innerHTML = "";
    const plants = allTiles.filter((t) => PLANT_TYPES.includes(t.type));
    const batteries = allTiles.filter((t) => STORAGE_TYPES.includes(t.type));
    if (plants.length === 0 && batteries.length === 0) {
      const li = document.createElement("li");
      li.style.color = "#5a5d65";
      li.style.fontSize = "0.8rem";
      li.textContent = "no plants built";
      plantListEl.appendChild(li);
      return;
    }
    const caps = (catalog && Object.fromEntries(Object.entries(catalog).map(([k, v]) => [k, v.capacity_kw || 1]))) || {};
    const storageMax = (catalog && Object.fromEntries(Object.entries(catalog).map(([k, v]) => [k, v.storage_kwh || 0]))) || {};
    for (const p of plants) {
      const cap = caps[p.type] || 1;
      const out = p.current_output_kw || 0;
      const pct = Math.min(100, (out / cap) * 100);
      const li = document.createElement("li");
      li.className = `plantrow ${p.type}`;
      li.innerHTML = `
        <span class="pl-name">${p.type} (${p.x},${p.y})</span>
        <div class="pl-bar"><div class="pl-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
        <span class="pl-val">${Math.round(out)}/${cap} kW</span>
      `;
      plantListEl.appendChild(li);
    }
    // Batteries: show SoC as a fill bar (kWh stored / max kWh) and the manual
    // charge setpoint when set. current_output_kw isn't stamped per-hour on
    // batteries so we deliberately don't show a kW rate here.
    for (const b of batteries) {
      const maxKwh = storageMax[b.type] || 1;
      const soc = b.soc_kwh || 0;
      const pct = Math.min(100, (soc / maxKwh) * 100);
      const sp = b.charge_setpoint_kw || 0;
      let spLbl = "auto";
      if (sp > 0) spLbl = `charge ${Math.round(sp)} kW`;
      else if (sp < 0) spLbl = `discharge ${Math.round(-sp)} kW`;
      const li = document.createElement("li");
      li.className = `plantrow battery`;
      li.innerHTML = `
        <span class="pl-name">battery (${b.x},${b.y}) · ${spLbl}</span>
        <div class="pl-bar"><div class="pl-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
        <span class="pl-val">${Math.round(soc)}/${maxKwh} kWh</span>
      `;
      plantListEl.appendChild(li);
    }
  }

  // Subsurface tab rendering -------------------------------------------
  const subAxisEl = document.getElementById("sub-axis");
  const subSliceEl = document.getElementById("sub-slice");
  const subChartEl = document.getElementById("subchart");
  const subStatsEl = document.getElementById("sub-stats");

  let worldDims = { w: 32, h: 32, d: 16 };
  let revealedVoxels = [];

  function maxOilForColorScale() {
    let m = 0;
    for (const v of revealedVoxels) {
      if (v.oil_estimate_bbl > m) m = v.oil_estimate_bbl;
    }
    return m || 1;
  }

  // oilfield-v2 slice 02: voxels are coloured by reservoir_id so that a
  // single connected HC blob reads as one visual region in the cross-section.
  // The exact oil estimate is still surfaced via per-voxel <title> hover and
  // the well popup. 8-colour rotation: reservoir_id % 8. id=0 / null means
  // "non-HC" (shouldn't appear in revealedVoxels) — fall back to a neutral
  // grey so a stray non-HC row would not crash the render.
  const RESERVOIR_PALETTE = [
    "#e76f51", // 1 — coral
    "#f4a261", // 2 — amber
    "#e9c46a", // 3 — sand
    "#8ab17d", // 4 — sage
    "#2a9d8f", // 5 — teal
    "#4f8fc0", // 6 — sky
    "#9b6dd7", // 7 — violet
    "#d36cb3", // 8 — magenta
  ];
  function reservoirColor(reservoirId) {
    if (reservoirId === null || reservoirId === undefined || reservoirId <= 0) {
      return "#5a5e68";
    }
    return RESERVOIR_PALETTE[(reservoirId - 1) % RESERVOIR_PALETTE.length];
  }

  async function refreshRevealed() {
    try {
      const res = await fetch("/reservoirs?min_oil=0&top_k=4096");
      if (!res.ok) return;
      const body = await res.json();
      revealedVoxels = body.voxels || [];
    } catch (err) {
      // best-effort; subsurface tab keeps prior data
    }
  }

  function renderSubsurface() {
    if (!subChartEl) return;
    subChartEl.innerHTML = "";
    const axis = subAxisEl.value || "y";
    let slice = parseInt(subSliceEl.value, 10);
    if (isNaN(slice)) slice = 16;
    const W = 640;
    const H = 320;
    // Cross-section dims: (lateral × depth)
    const lateralN = axis === "y" ? worldDims.w : worldDims.h;
    const depthN = worldDims.d;
    const cw = W / lateralN;
    const ch = H / depthN;

    // Outline grid for unrevealed voxels.
    const gridG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gridG.setAttribute("stroke", "#2f323a");
    gridG.setAttribute("fill", "transparent");
    gridG.setAttribute("stroke-width", "0.5");
    for (let i = 0; i < lateralN; i++) {
      for (let z = 0; z < depthN; z++) {
        const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        r.setAttribute("x", (i * cw).toFixed(2));
        r.setAttribute("y", (z * ch).toFixed(2));
        r.setAttribute("width", cw.toFixed(2));
        r.setAttribute("height", ch.toFixed(2));
        gridG.appendChild(r);
      }
    }
    subChartEl.appendChild(gridG);

    const maxOil = maxOilForColorScale();
    const onSlice = revealedVoxels.filter((v) =>
      axis === "y" ? v.y === slice : v.x === slice
    );
    const filledG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const v of onSlice) {
      const lateral = axis === "y" ? v.x : v.y;
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", (lateral * cw).toFixed(2));
      r.setAttribute("y", (v.z * ch).toFixed(2));
      r.setAttribute("width", cw.toFixed(2));
      r.setAttribute("height", ch.toFixed(2));
      r.setAttribute("fill", reservoirColor(v.reservoir_id));
      r.setAttribute("stroke", "#1a1c22");
      r.setAttribute("stroke-width", "0.5");
      const resTag =
        v.reservoir_id === null || v.reservoir_id === undefined || v.reservoir_id <= 0
          ? "—"
          : `R${v.reservoir_id}`;
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `(${v.x}, ${v.y}, ${v.z}) ${resTag} — ${Math.round(v.oil_estimate_bbl).toLocaleString()} bbl, ${Math.round(v.perm_estimate_md)} mD`;
      r.appendChild(title);
      if (mode === "drill") {
        r.classList.add("drill-pickable");
        r.addEventListener("click", () => {
          drillAnchor = { x: v.x, y: v.y, target_z: v.z };
          updateDrillCostPreview();
          if (subTargetEl) {
            subTargetEl.classList.remove("hidden");
            subTargetEl.textContent = `selected target: (${v.x}, ${v.y}, ${v.z}) — click surface on the Map tab to drill`;
          }
          refreshBuildHint();
          drawGrid();
          renderSubsurface();
        });
      }
      if (
        drillAnchor
        && drillAnchor.target_z === v.z
        && drillAnchor.x === v.x
        && drillAnchor.y === v.y
      ) {
        r.classList.add("drill-picked");
      }
      filledG.appendChild(r);
    }
    subChartEl.appendChild(filledG);

    // Wellhead markers ▼/▲ for wells on this slice. The vertical bore
    // line runs from the surface (z=0) down to the centre of the target
    // voxel — visually anchors the surface tile to its subsurface target.
    const wellG = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const w of wells) {
      const offAxis = axis === "y" ? w.y : w.x;
      if (offAxis !== slice) continue;
      const lateral = axis === "y" ? w.x : w.y;
      const boreX = lateral * cw + cw / 2;
      const boreY1 = (w.target_z + 0.5) * ch;
      const bore = document.createElementNS("http://www.w3.org/2000/svg", "line");
      bore.setAttribute("x1", boreX.toFixed(2));
      bore.setAttribute("y1", "0");
      bore.setAttribute("x2", boreX.toFixed(2));
      bore.setAttribute("y2", boreY1.toFixed(2));
      bore.setAttribute("stroke", "#ff5050");
      bore.setAttribute("stroke-width", "1.5");
      bore.setAttribute("pointer-events", "none");
      wellG.appendChild(bore);
      const symbol = w.type === "production" ? "▼" : "▲";
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", (lateral * cw + cw / 2).toFixed(2));
      t.setAttribute("y", (w.target_z * ch + ch * 0.78).toFixed(2));
      t.setAttribute("fill", w.type === "production" ? "#3fbf7f" : "#a8d8ff");
      t.setAttribute("font-size", Math.max(8, Math.floor(ch * 0.7)).toString());
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("pointer-events", "none");
      t.textContent = symbol;
      const setpoint = w.setpoint_rate_bbl_day || 0;
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${w.id} · ${w.type} · (${w.x}, ${w.y}, ${w.target_z}) · setpoint ${Math.round(setpoint)} bbl/d`;
      t.appendChild(title);
      wellG.appendChild(t);
    }
    subChartEl.appendChild(wellG);

    // Axes labels.
    const axisLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
    axisLabel.setAttribute("x", "4");
    axisLabel.setAttribute("y", "14");
    axisLabel.setAttribute("fill", "#8b8f9a");
    axisLabel.setAttribute("font-size", "11");
    axisLabel.textContent = `axis=${axis}, slice=${slice}, depth↓ ${axis === "y" ? "x→" : "y→"}`;
    subChartEl.appendChild(axisLabel);

    subStatsEl.textContent = `${revealedVoxels.length} revealed voxels · max est. ${Math.round(maxOil).toLocaleString()} bbl · ${onSlice.length} on this slice`;

    if (subTargetEl) {
      if (drillAnchor) {
        subTargetEl.classList.remove("hidden");
        subTargetEl.textContent = `selected target: (${drillAnchor.x}, ${drillAnchor.y}, ${drillAnchor.target_z}) — click surface on the Map tab to drill`;
      } else {
        subTargetEl.classList.add("hidden");
        subTargetEl.textContent = "";
      }
    }
  }

  if (subAxisEl) subAxisEl.addEventListener("change", renderSubsurface);
  if (subSliceEl) subSliceEl.addEventListener("input", renderSubsurface);

  // Wells tab rendering ----------------------------------------------------
  const wellsTableBody = document.getElementById("wellstable-body");
  const wellsStatsEl = document.getElementById("wells-stats");
  const refineriesTableBody = document.getElementById("refineriestable-body");
  const refineriesStatsEl = document.getElementById("refineries-stats");
  const financeListEl = document.getElementById("financelist");

  async function setRefineryRate(refineryId, rate) {
    if (isMutationLocked()) return;
    try {
      await fetch("/control/refinery", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ refinery_id: refineryId, rate_bbl_day: rate }),
      });
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  function renderRefineries() {
    if (!refineriesTableBody) return;
    refineriesTableBody.innerHTML = "";
    const refineries = tiles.filter((t) => t.type === "refinery");
    if (refineries.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.style.color = "#5a5d65";
      td.style.fontSize = "0.8rem";
      td.style.textAlign = "center";
      td.textContent = "no refineries built — POST /build { tile_type: 'refinery' }";
      tr.appendChild(td);
      refineriesTableBody.appendChild(tr);
    } else {
      for (const r of refineries) {
        const setpoint = r.setpoint_rate_bbl_day || 0;
        const throughput = r.current_throughput_bbl_day || 0;
        const refined = throughput * REFINERY_YIELD;
        const tr = document.createElement("tr");
        const orphan = orphanRefineryIds.has(r.id);
        if (orphan) tr.classList.add("orphan-refinery");
        const idCell = orphan
          ? `${r.id} <span class="orphan-badge orphan-refinery-badge" title="Not connected to any producer's pipeline network — zero throughput today.">no crude</span>`
          : `${r.id}`;
        tr.innerHTML = `
          <td>${idCell}</td>
          <td>(${r.x}, ${r.y})</td>
          <td>
            <input type="range" min="0" max="${refineryMaxBblDay()}" step="10" value="${setpoint}" data-id="${r.id}" ${isMutationLocked() ? "disabled" : ""} />
            <span class="setpoint-val">${Math.round(setpoint)}</span>
          </td>
          <td class="actual">${throughput.toFixed(1)}</td>
          <td class="cumulative">${refined.toFixed(1)}</td>
        `;
        refineriesTableBody.appendChild(tr);
        const slider = tr.querySelector("input[type=range]");
        const valEl = tr.querySelector(".setpoint-val");
        slider.addEventListener("input", () => {
          valEl.textContent = String(slider.value);
        });
        slider.addEventListener("change", () => {
          setRefineryRate(r.id, parseFloat(slider.value));
        });
      }
    }
    if (refineriesStatsEl) {
      const totalSetpoint = refineries.reduce((a, r) => a + (r.setpoint_rate_bbl_day || 0), 0);
      const totalThroughput = refineries.reduce((a, r) => a + (r.current_throughput_bbl_day || 0), 0);
      refineriesStatsEl.textContent = `${refineries.length} refineries · ${totalSetpoint.toFixed(0)} bbl/d setpoint · ${totalThroughput.toFixed(1)} bbl/d throughput`;
    }
  }

  function renderFinance() {
    if (!financeListEl) return;
    financeListEl.innerHTML = "";
    const rows = [
      ["Tax revenue", summary.tax_revenue || 0, "+"],
      ["Commercial revenue", summary.commercial_revenue || 0, "+"],
      ["Industrial revenue", summary.industrial_revenue || 0, "+"],
      ["Power revenue", summary.power_revenue || 0, "+"],
      ["Crude (direct sale)", summary.crude_revenue || 0, "+"],
      ["Refined oil", summary.refined_revenue || 0, "+"],
      ["OPEX", -(summary.opex || 0), "-"],
      ["Fuel cost", -(summary.fuel_cost || 0), "-"],
      ["Carbon cost", -(summary.carbon_cost || 0), "-"],
      ["Outage penalty", -(summary.outage_penalty || 0), "-"],
    ];
    for (const [label, value, sign] of rows) {
      const li = document.createElement("li");
      const cls = value > 0 ? "positive" : value < 0 ? "negative" : "neutral";
      li.className = `finance-row ${cls}`;
      li.innerHTML = `<span class="finance-label">${label}</span><span class="finance-value">${sign === "-" ? "-" : ""}$${Math.abs(Math.round(value)).toLocaleString()}</span>`;
      financeListEl.appendChild(li);
    }
  }

  async function setWellRate(wellId, rate) {
    if (isMutationLocked()) return;
    try {
      await fetch("/control/well", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ well_id: wellId, rate_bbl_day: rate }),
      });
      tick();
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  function fmtBblCompact(v) {
    const n = Number(v) || 0;
    const abs = Math.abs(n);
    const sign = n < 0 ? "-" : "";
    if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`;
    return `${sign}${Math.round(abs).toLocaleString()}`;
  }

  function renderWells() {
    if (!wellsTableBody) return;
    wellsTableBody.innerHTML = "";
    const COLSPAN = 8;
    if (wells.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = COLSPAN;
      td.style.color = "#5a5d65";
      td.style.fontSize = "0.8rem";
      td.style.textAlign = "center";
      td.textContent = "no wells drilled — POST /drill { x, y, target_z, well_type }";
      tr.appendChild(td);
      wellsTableBody.appendChild(tr);
    } else {
      // wells-reservoir-rollup #03+#04: group wells by reservoir, surface
      // per-row boost (producers) and supports (injectors) columns.
      const wellsById = new Map(wells.map((w) => [w.id, w]));
      const groupedIds = new Set();

      const emitWellRow = (w) => {
        const tr = document.createElement("tr");
        const setpoint = w.setpoint_rate_bbl_day || 0;
        const cum = w.type === "injection"
          ? (w.cumulative_injected_bbl || 0)
          : (w.cumulative_produced_bbl || 0);
        const orphan = w.type === "production" && orphanWellIds.has(w.id);
        if (orphan) tr.classList.add("orphan-well");
        const idCell = orphan
          ? `${w.id} <span class="orphan-badge orphan-well-badge" title="No pipeline path to a refinery — selling raw crude at $40/bbl.">selling raw</span>`
          : `${w.id}`;
        let boostCell = "";
        let supportsCell = "";
        if (w.type === "production") {
          const boost = w.pressure_boost || 0;
          boostCell = boost > 0 ? boost.toFixed(3) : "—";
        } else if (w.type === "injection") {
          const supports = w.supports_producer_ids || [];
          supportsCell = supports.length === 0
            ? `<span title="no qualifying producers in this reservoir at Chebyshev distance > 1.">—</span>`
            : supports.join(", ");
        }
        tr.innerHTML = `
          <td>${idCell}</td>
          <td>${w.type}</td>
          <td>(${w.x}, ${w.y}, ${w.target_z})</td>
          <td>
            <input type="range" min="0" max="200" step="5" value="${setpoint}" data-id="${w.id}" ${isMutationLocked() ? "disabled" : ""} />
            <span class="setpoint-val">${Math.round(setpoint)}</span>
          </td>
          <td class="actual">${(w.current_rate_bbl_day || 0).toFixed(1)}</td>
          <td class="cumulative">${Math.round(cum).toLocaleString()}</td>
          <td class="boost">${boostCell}</td>
          <td class="supports">${supportsCell}</td>
        `;
        wellsTableBody.appendChild(tr);
        const slider = tr.querySelector("input[type=range]");
        const valEl = tr.querySelector(".setpoint-val");
        slider.addEventListener("input", () => {
          valEl.textContent = String(slider.value);
        });
        slider.addEventListener("change", () => {
          setWellRate(w.id, parseFloat(slider.value));
        });
      };

      const emitGroupHeader = (text) => {
        const tr = document.createElement("tr");
        tr.classList.add("reservoir-group-header");
        const td = document.createElement("td");
        td.colSpan = COLSPAN;
        td.textContent = text;
        tr.appendChild(td);
        wellsTableBody.appendChild(tr);
      };

      const emitEmptyPlaceholder = () => {
        const tr = document.createElement("tr");
        tr.classList.add("reservoir-group-empty");
        const td = document.createElement("td");
        td.colSpan = COLSPAN;
        td.textContent = "no wells — drill here?";
        tr.appendChild(td);
        wellsTableBody.appendChild(tr);
      };

      for (const r of reservoirsSummary) {
        const producerIds = r.producer_ids || [];
        const injectorIds = r.injector_ids || [];
        const head = `Reservoir R${r.reservoir_id} — est ${fmtBblCompact(r.estimated_bbl || 0)} bbl · engaged ${fmtBblCompact(r.engaged_bbl || 0)} · remaining ${fmtBblCompact(r.remaining_bbl || 0)} · ${r.n_revealed_voxels || 0} revealed vox · ${producerIds.length}P + ${injectorIds.length}I`;
        emitGroupHeader(head);
        if (producerIds.length === 0 && injectorIds.length === 0) {
          emitEmptyPlaceholder();
          continue;
        }
        for (const id of producerIds) {
          const w = wellsById.get(id);
          if (w) { emitWellRow(w); groupedIds.add(id); }
        }
        for (const id of injectorIds) {
          const w = wellsById.get(id);
          if (w) { emitWellRow(w); groupedIds.add(id); }
        }
      }
      const unaffiliated = wells.filter((w) => !groupedIds.has(w.id));
      if (unaffiliated.length > 0) {
        emitGroupHeader("Unaffiliated (drilled into rock)");
        const sorted = [...unaffiliated].sort((a, b) => {
          if (a.type !== b.type) return a.type === "production" ? -1 : 1;
          if (a.id < b.id) return -1;
          if (a.id > b.id) return 1;
          return 0;
        });
        for (const w of sorted) emitWellRow(w);
      }
    }
    if (wellsStatsEl) {
      const prodWells = wells.filter((w) => w.type === "production");
      const injWells = wells.filter((w) => w.type === "injection");
      const totalProd = prodWells.reduce((a, w) => a + (w.cumulative_produced_bbl || 0), 0);
      const totalInj = injWells.reduce((a, w) => a + (w.cumulative_injected_bbl || 0), 0);
      const prodRate = prodWells.reduce((a, w) => a + (w.current_rate_bbl_day || 0), 0);
      const injRate = injWells.reduce((a, w) => a + (w.current_rate_bbl_day || 0), 0);
      wellsStatsEl.textContent = `${prodWells.length} prod · ${prodRate.toFixed(1)} bbl/d · ${Math.round(totalProd).toLocaleString()} cum bbl  ·  ${injWells.length} inj · ${injRate.toFixed(1)} bbl/d · ${Math.round(totalInj).toLocaleString()} cum bbl`;
    }
  }

  // Events tab rendering ----------------------------------------------------
  const eventsTableBody = document.getElementById("eventstable-body");
  const eventsHistoryBody = document.getElementById("eventshistorytable-body");

  function eventDetail(e) {
    if (e.type === "plant_failure") return `plant_id=${e.plant_id || "?"}`;
    if (e.type === "regulatory_tightening") {
      const occ = e.occurrences_after != null ? ` (#${e.occurrences_after})` : "";
      return `carbon_price → $${(e.severity || 0).toFixed(2)}${occ}`;
    }
    // Scenario display-only markers — silent levers (price-field / weather
    // mutations) surfaced so they can be tracked here and in History. See
    // world.scenario.inject_display_marker.
    if (e.type === "fuel_cost_shock")
      return `coal → $${(e.coal_usd_per_mwh || 0).toFixed(0)}/MWh, gas → $${(e.gas_usd_per_mwh || 0).toFixed(0)}/MWh`;
    if (e.type === "crude_collapse")
      return `crude → $${(e.crude_usd_per_bbl || 0).toFixed(0)}/bbl`;
    if (e.type === "oil_collapse")
      return `crude → $${(e.crude_usd_per_bbl || 0).toFixed(0)}/bbl, refined → $${(e.refined_usd_per_bbl || 0).toFixed(0)}/bbl`;
    if (e.type === "low_wind")
      return `wind pinned ${(e.wind_mps || 0).toFixed(1)} m/s (below 3 m/s cut-in)`;
    if (e.type === "solar_dark")
      return `cloud_factor → ${(e.cloud_factor || 0).toFixed(2)}`;
    return `×${(e.severity || 1).toFixed(2)}`;
  }

  function renderEvents(today) {
    if (!eventsTableBody) return;
    eventsTableBody.innerHTML = "";
    if (activeEvents.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.style.color = "#5a5d65";
      td.style.fontSize = "0.8rem";
      td.style.textAlign = "center";
      td.textContent = "no active events";
      tr.appendChild(td);
      eventsTableBody.appendChild(tr);
    } else {
      for (const e of activeEvents) {
        const tr = document.createElement("tr");
        const ends = e.ends_day == null ? "—" : e.ends_day;
        const left = e.ends_day == null ? "permanent" : Math.max(0, e.ends_day - today);
        tr.innerHTML = `
          <td>${e.type}</td>
          <td>${e.started_day}</td>
          <td>${ends}</td>
          <td>${left}</td>
          <td>${eventDetail(e)}</td>
        `;
        eventsTableBody.appendChild(tr);
      }
    }
    if (eventsHistoryBody) {
      eventsHistoryBody.innerHTML = "";
      const recent = historicalEvents.slice(-50).reverse();
      if (recent.length === 0) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 4;
        td.style.color = "#5a5d65";
        td.style.fontSize = "0.8rem";
        td.style.textAlign = "center";
        td.textContent = "no past events";
        tr.appendChild(td);
        eventsHistoryBody.appendChild(tr);
      } else {
        for (const e of recent) {
          const tr = document.createElement("tr");
          const ends = e.ends_day == null ? "—" : e.ends_day;
          tr.innerHTML = `
            <td>${e.type}</td>
            <td>${e.started_day}</td>
            <td>${ends}</td>
            <td>${eventDetail(e)}</td>
          `;
          eventsHistoryBody.appendChild(tr);
        }
      }
    }
  }

  function applyStateSnapshot(s, { fromReplay = false } = {}) {
    cols = s.config.world_w;
    rows = s.config.world_h;
    worldDims = { w: s.config.world_w, h: s.config.world_h, d: s.config.world_d };
    if (Number.isFinite(s.config.ui_play_ms)) playCadenceMs = s.config.ui_play_ms;
    if (Number.isFinite(s.config.ui_fast_play_ms)) fastCadenceMs = s.config.ui_fast_play_ms;
    updateTimerButtons();
    if (subSliceEl && !subSliceEl.dataset.bounded) {
      subSliceEl.max = String(Math.max(0, worldDims.w - 1));
      subSliceEl.dataset.bounded = "1";
    }
    tiles = s.tiles || [];
    wells = s.wells || [];
    orphanWellIds = new Set(s.orphan_well_ids || []);
    orphanRefineryIds = new Set(s.orphan_refinery_ids || []);
    reservoirsSummary = s.reservoirs_summary || [];
    activeEvents = s.active_events || [];
    historicalEvents = s.historical_events || [];
    summary = s.today || {};
    treasury = s.treasury;
    els.day.textContent = s.day;
    els.treasury.textContent = Math.round(s.treasury).toLocaleString();
    // Population: pop / housing capacity, with unemployed as a prefix so the
    // jobs headroom story (the throttle on growth) reads at a glance.
    const housingCap = s.housing_capacity ?? 0;
    els.population.textContent = `${s.unemployed} idle · ${s.population}/${housingCap}`;
    // Jobs: staffed/total with the open headroom inline. Empty headroom is
    // the immediate-cause for stalled population growth.
    const jobsTotal = s.jobs_total ?? 0;
    const jobsVacant = s.jobs_vacant ?? 0;
    if (els.jobs) {
      els.jobs.textContent = `${s.employed}/${jobsTotal} (${jobsVacant} open)`;
    }
    els.happiness.textContent = s.happiness.toFixed(2);
    const balanceState = (s.power_now && s.power_now.balance_state) || "—";
    els.balance.textContent = balanceState;
    els.balance.className = `balance-badge ${balanceState}`;
    renderPowerChart(
      s.next_24h_preview,
      {
        supply: s.last_day_supply_kw_by_hour,
        demand: s.last_day_demand_kw_by_hour,
      }
    );
    renderPowerMargin(s.next_24h_preview);
    renderPlantList(tiles);
    renderWells();
    renderRefineries();
    renderFinance();
    renderEvents(s.day);
    drawGrid();
    // Subsurface rendering. In live mode we re-fetch /reservoirs only when
    // the material inputs changed (avoids tearing down in-flight voxel
    // picks). In replay mode there is no server to fetch from, so we use
    // the recorded snapshot's embedded `reservoirs_revealed.voxels` (top
    // 10 by oil estimate — same shape /reservoirs returns).
    const rr = s.reservoirs_revealed || {};
    const revealedCount = rr.n_revealed_voxels || 0;
    const wellsCount = wells.length;
    const subsurfaceDirty =
      revealedCount !== _lastRevealedCount
      || wellsCount !== _lastWellsCount
      || fromReplay;
    if (subsurfaceDirty) {
      if (fromReplay) {
        revealedVoxels = rr.voxels || [];
        renderSubsurface();
      } else {
        // Live mode: refresh from /reservoirs (covers top_k=4096, not just
        // the embedded top-10 summary).
        refreshRevealed().then(() => renderSubsurface());
      }
      _lastRevealedCount = revealedCount;
      _lastWellsCount = wellsCount;
    }
    // Actions panel: re-pull on every snapshot so live/peek/replay all
    // render the slice matching the rendered day.
    refreshActions(s.day);
  }

  // Actions widget. Mirror of `world/api._slice_actions_for_day`; the
  // fixture at `world/tests/fixtures/actions_slicing.jsonl` is the
  // shared ground truth — keep this in sync with the server helper.
  const actionsListEl = document.getElementById("actionslist");
  const actionsDayRangeEl = document.getElementById("actions-day-range");

  function sliceActionsForDay(entries, day) {
    const slices = [];
    let currentDay = 0;
    let buffer = [];
    for (const entry of entries) {
      if (!entry || !entry.ok) continue;
      buffer.push(entry);
      const ep = entry.endpoint;
      if (ep === "/step") {
        const days = Number((entry.params || {}).days ?? 7);
        slices.push({ day_start: currentDay, day_end: currentDay + days - 1, entries: buffer });
        currentDay += days;
        buffer = [];
      } else if (ep === "/reset") {
        slices.push({ day_start: currentDay, day_end: currentDay, entries: buffer });
        currentDay = 0;
        buffer = [];
      }
    }
    slices.push({ day_start: currentDay, day_end: currentDay, entries: buffer });
    for (let i = slices.length - 1; i >= 0; i -= 1) {
      const s = slices[i];
      if (s.day_start <= day && day <= s.day_end) return s;
    }
    return { day_start: day, day_end: day, entries: [] };
  }

  function renderActions(slice) {
    if (!actionsListEl || !actionsDayRangeEl) return;
    const a = slice.day_start;
    const b = slice.day_end;
    actionsDayRangeEl.textContent = a === b ? `day ${a}` : `days ${a}–${b}`;
    actionsListEl.innerHTML = "";
    const entries = slice.entries || [];
    if (entries.length === 0) {
      const li = document.createElement("li");
      li.className = "ar-empty";
      li.textContent = "no actions submitted yet";
      actionsListEl.appendChild(li);
      return;
    }
    for (const entry of entries) {
      const li = document.createElement("li");
      const isTerminator = entry.endpoint === "/step" || entry.endpoint === "/reset";
      li.className = `actionrow ${isTerminator ? "terminator" : "ok"}`;
      const ep = document.createElement("span");
      ep.className = "ar-ep";
      ep.textContent = entry.endpoint;
      const params = document.createElement("span");
      params.className = "ar-params";
      params.textContent = compactParams(entry.params);
      li.appendChild(ep);
      li.appendChild(params);
      actionsListEl.appendChild(li);
    }
    // Pin to bottom so the latest action is visible.
    actionsListEl.scrollTop = actionsListEl.scrollHeight;
  }

  function compactParams(params) {
    if (!params || typeof params !== "object") return "";
    const parts = [];
    for (const [k, v] of Object.entries(params)) {
      const sv = typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);
      parts.push(`${k}=${sv}`);
    }
    return parts.join(" ");
  }

  async function refreshActions(day) {
    if (!actionsListEl) return;
    if (isReplay()) {
      renderActions(sliceActionsForDay(replay.actions, day));
      return;
    }
    try {
      const res = await fetch(`/actions?day=${day}`);
      if (!res.ok) return;
      const slice = await res.json();
      renderActions(slice);
    } catch (err) {
      // tolerant: panel keeps its last render
    }
  }

  async function tick() {
    if (isReplay()) return;
    if (isPeeking()) return;  // peek mode renders a historical snapshot; live polls are suppressed
    try {
      const res = await fetch("/state");
      if (!res.ok) return;
      const s = await res.json();
      lastLiveDay = Number.isFinite(s.day) ? s.day : lastLiveDay;
      applyStateSnapshot(s);
      updatePeekButtons();
    } catch (err) {
      // Server may not be up yet during boot; the next user action will retry.
    }
  }

  function updatePeekButtons() {
    if (!prevDayBtn) return;
    // In replay mode, the replay bar has its own back button; hide this one.
    prevDayBtn.disabled = isReplay() || (isPeeking() ? peekDay <= 0 : lastLiveDay <= 0);
    prevDayBtn.textContent = isPeeking() ? `◀ Prev Day (peeking ${peekDay})` : "◀ Prev Day";
    const resetBtnEl = document.getElementById("reset-btn");
    if (resetBtnEl) resetBtnEl.disabled = isReplay();
  }

  async function peekStep(delta) {
    if (isReplay()) return;
    const base = isPeeking() ? peekDay : lastLiveDay;
    const target = base + delta;
    if (target < 0) {
      showToast("no recorded day before day 0", "info");
      return;
    }
    try {
      const res = await fetch(`/state/history?day=${target}`);
      if (!res.ok) {
        let detail = "";
        try {
          const body = await res.json();
          detail = body.detail || "";
        } catch (err) {
          // ignore
        }
        showToast(`peek failed: ${detail || res.status}`, "error");
        return;
      }
      const entry = await res.json();
      const state = entry && entry.state;
      if (!state) return;
      // Pause the play timer if it was running — peeking and auto-advance
      // are mutually exclusive.
      pauseTimer();
      peekDay = target;
      document.body.classList.add("peeking");
      applyStateSnapshot(state, { fromReplay: true });
      updatePeekButtons();
    } catch (err) {
      showToast(`peek failed: ${err}`, "error");
    }
  }

  function clearPeek() {
    if (!isPeeking()) return;
    peekDay = null;
    document.body.classList.remove("peeking");
    updatePeekButtons();
  }

  function renderReplayFrame(index) {
    if (!replay.states.length) return;
    const clamped = Math.max(0, Math.min(replay.states.length - 1, index));
    replay.cursor = clamped;
    const entry = replay.states[clamped];
    const state = entry && entry.state;
    if (!state) return;
    applyStateSnapshot(state, { fromReplay: true });
    updateReplayUi();
  }

  function updateReplayUi() {
    if (!replayBarEl) return;
    const total = replay.states.length;
    const visible = isReplay();
    replayBarEl.classList.toggle("hidden", !visible);
    if (!visible) return;
    if (total === 0) {
      replayDayLabelEl.textContent = "no recorded days";
      replaySliderEl.hidden = true;
      replayBackBtn.hidden = true;
      replayForwardBtn.hidden = true;
    } else {
      replaySliderEl.hidden = false;
      replayBackBtn.hidden = false;
      replayForwardBtn.hidden = false;
      replaySliderEl.disabled = false;
      replaySliderEl.min = "0";
      replaySliderEl.max = String(total - 1);
      replaySliderEl.value = String(replay.cursor);
      const entry = replay.states[replay.cursor];
      const dayNum = entry && entry.day != null ? entry.day : replay.cursor + 1;
      replayDayLabelEl.textContent = `Day ${dayNum} / ${replay.states[total - 1].day || total}`;
      replayBackBtn.disabled = replay.cursor <= 0;
      replayForwardBtn.disabled = replay.cursor >= total - 1;
    }
    const md = replay.metadata || {};
    const scenario = md.scenario == null ? "(none)" : md.scenario;
    const seed = md.seed != null ? md.seed : "—";
    const session = md.session || "—";
    const runId = md.run_id || "(metadata unavailable)";
    replayBadgeEl.textContent = `scenario: ${scenario} · seed: ${seed} · session: ${session} · run_id: ${runId}`;
  }

  function setReplayMode(next) {
    const changed = replay.mode !== next;
    replay.mode = next;
    if (next === "replay") {
      document.body.classList.add("replay-mode");
      // Cancel any active build/survey/drill interaction mode.
      if (mode) setMode(null);
      // Replay mode owns its own back button; clear any live-mode peek.
      clearPeek();
    } else {
      document.body.classList.remove("replay-mode");
    }
    // Pause the auto-advance timer on any mode switch so cadence and
    // dispatch target don't desync (live timer firing into replay or
    // vice versa).
    if (changed && typeof pauseTimer === "function") pauseTimer();
    updateReplayUi();
    updatePeekButtons();
    if (typeof refreshScenarioReadout === "function") refreshScenarioReadout();
  }

  function closeReplay() {
    replay.states = [];
    replay.actions = [];
    replay.metadata = null;
    replay.cursor = 0;
    setReplayMode("live");
    // Force a re-fetch so the live view's voxel cache repopulates.
    _lastRevealedCount = -1;
    _lastWellsCount = -1;
    tick();
  }

  function replayStepBy(delta) {
    if (!isReplay() || !replay.states.length) return;
    renderReplayFrame(replay.cursor + delta);
  }

  async function loadReplayFromFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;
    const findByName = (name) => files.find((f) => {
      const rel = f.webkitRelativePath || f.name;
      return rel === name || rel.endsWith("/" + name);
    });
    const statesFile = findByName("states.jsonl");
    const metadataFile = findByName("metadata.json");
    const actionsFile = findByName("actions.jsonl");
    if (!statesFile) {
      showToast("no states.jsonl in selected folder", "error");
      return;
    }
    let metadata = null;
    if (metadataFile) {
      try {
        const text = await metadataFile.text();
        metadata = JSON.parse(text);
      } catch (err) {
        showToast("metadata.json parse failed — continuing without it", "error");
        metadata = null;
      }
    }
    let statesText;
    try {
      statesText = await statesFile.text();
    } catch (err) {
      showToast("could not read states.jsonl", "error");
      return;
    }
    const lines = statesText.split("\n");
    const parsed = [];
    let badCount = 0;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        parsed.push(JSON.parse(trimmed));
      } catch (err) {
        badCount += 1;
      }
    }
    const actionsParsed = [];
    if (actionsFile) {
      try {
        const text = await actionsFile.text();
        for (const line of text.split("\n")) {
          const t = line.trim();
          if (!t) continue;
          try { actionsParsed.push(JSON.parse(t)); } catch (err) { /* skip */ }
        }
      } catch (err) {
        showToast("could not read actions.jsonl — actions panel will be empty", "error");
      }
    }
    replay.states = parsed;
    replay.actions = actionsParsed;
    replay.metadata = metadata;
    replay.cursor = parsed.length > 0 ? parsed.length - 1 : 0;
    setReplayMode("replay");
    if (badCount > 0) {
      showToast(`skipped ${badCount} unparseable state line(s)`, "error");
    } else if (parsed.length === 0) {
      showToast("no recorded days in states.jsonl", "error");
    } else {
      showToast(`loaded ${parsed.length} day(s)`, "ok");
    }
    if (parsed.length > 0) renderReplayFrame(replay.cursor);
  }

  if (loadRunBtn && replayFilesInput) {
    loadRunBtn.addEventListener("click", () => replayFilesInput.click());
    replayFilesInput.addEventListener("change", async (ev) => {
      await loadReplayFromFiles(ev.target.files);
      // Reset the input so re-picking the same folder fires the change event.
      ev.target.value = "";
    });
  }

  // Play / fast / pause (issue 10). A single `playTimer` setInterval
  // handle plus `playSpeed` (500 | 250 | null) describes the auto-advance
  // state. The timer dispatches to `/step` (live) or `renderReplayFrame`
  // (replay) based on `replay.mode`. `stepInFlight` guards live mode
  // against overlapping `/step` requests on slow hardware.
  //
  // Each speed has ONE button that toggles between starting that speed
  // and pausing (#play-btn for 500 ms, #fast-btn for 250 ms). The button
  // label/icon flips: ▶ Play / ⏩ Fast when that speed is idle, ⏸ Pause
  // when that speed is currently running. Clicking either button while
  // the OTHER speed is running switches speeds (cancels the running
  // interval and starts at the clicked button's cadence).
  let playTimer = null;
  let playSpeed = null;
  let stepInFlight = false;
  // Cadences come from `/state.config.{ui_play_ms, ui_fast_play_ms}` so
  // they can be tuned via env vars (UI_PLAY_MS, UI_FAST_PLAY_MS) without
  // editing this file. Defaults match the in-flight values used before
  // `tick()` populates them — clicking Play in the brief window between
  // page load and the first `/state` response uses these.
  let playCadenceMs = 500;
  let fastCadenceMs = 250;
  const playBtn = document.getElementById("play-btn");
  const fastBtn = document.getElementById("fast-btn");

  function updateTimerButtons() {
    if (playBtn) {
      const running = playSpeed === playCadenceMs;
      playBtn.classList.toggle("active", running);
      playBtn.textContent = running ? "⏸ Pause" : "▶ Play";
      playBtn.title = running
        ? "Pause (Space)"
        : `Play (Space) — auto-advance at ${playCadenceMs} ms/day`;
    }
    if (fastBtn) {
      const running = playSpeed === fastCadenceMs;
      fastBtn.classList.toggle("active", running);
      fastBtn.textContent = running ? "⏸ Pause" : "⏩ Fast";
      fastBtn.title = running
        ? "Pause (Shift+Space)"
        : `Fast (Shift+Space) — auto-advance at ${fastCadenceMs} ms/day`;
    }
  }

  function pauseTimer() {
    if (playTimer !== null) {
      clearInterval(playTimer);
      playTimer = null;
    }
    playSpeed = null;
    updateTimerButtons();
  }

  async function timerTickLive() {
    if (stepInFlight) return;
    stepInFlight = true;
    try {
      const res = await fetch("/step", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ days: 1 }),
      });
      if (!res.ok) {
        pauseTimer();
        let detail = "";
        try {
          const body = await res.json();
          detail = body.detail || "";
        } catch (err) {
          // ignore
        }
        showToast(`step rejected: ${detail || res.status}`, "error");
        return;
      }
      await tick();
    } catch (err) {
      pauseTimer();
      showToast(`network error: ${err}`, "error");
    } finally {
      stepInFlight = false;
    }
  }

  function timerTickReplay() {
    if (!replay.states.length) {
      pauseTimer();
      return;
    }
    const next = replay.cursor + 1;
    if (next >= replay.states.length) {
      renderReplayFrame(replay.states.length - 1);
      pauseTimer();
      return;
    }
    renderReplayFrame(next);
  }

  function startTimer(ms) {
    if (playSpeed === ms && playTimer !== null) return;  // no-op: same speed already running
    pauseTimer();
    // Auto-advance is a live-mode action; pressing Play while peeking
    // snaps the UI back to live before the first interval fires.
    clearPeek();
    playSpeed = ms;
    playTimer = setInterval(() => {
      if (isReplay()) timerTickReplay();
      else timerTickLive();
    }, ms);
    updateTimerButtons();
  }

  function toggleTimer(ms) {
    if (playSpeed === ms) pauseTimer();
    else startTimer(ms);
  }

  if (playBtn) playBtn.addEventListener("click", () => toggleTimer(playCadenceMs));
  if (fastBtn) fastBtn.addEventListener("click", () => toggleTimer(fastCadenceMs));

  window.addEventListener("beforeunload", () => pauseTimer());

  // Scenario attach control (issue 10). Calls GET /scenario to populate
  // the readout on app boot, after every successful POST /scenario, and
  // when entering live mode from replay. POST /scenario carries the
  // dotted path in `{dotted_path: <value>}`. "Detach" re-attaches
  // `scenarios.baseline` — a NullScenario subclass, so the GET response
  // flips back to `{dotted_path: null}` and the readout shows `(none)`.
  // The loader explicitly excludes NullScenario itself, so we cannot
  // post `world.scenario.NullScenario` directly.
  const scenarioCurrentEl = document.getElementById("scenario-current");
  const scenarioDescriptionEl = document.getElementById("scenario-description");
  const scenarioSourceEl = document.getElementById("scenario-source");
  const scenarioChooseBtn = document.getElementById("scenario-choose");
  const scenarioDetachBtn = document.getElementById("scenario-detach");
  const scenarioModal = document.getElementById("scenario-attach-modal");
  const scenarioSelectEl = document.getElementById("scenario-select");
  const scenarioConfirmBtn = document.getElementById("scenario-attach-confirm");
  const scenarioCancelBtn = document.getElementById("scenario-attach-cancel");
  const DETACH_DOTTED_PATH = "scenarios.baseline";

  function renderScenarioDescription(description) {
    if (!scenarioDescriptionEl) return;
    const text = typeof description === "string" ? description.trim() : "";
    if (text) {
      scenarioDescriptionEl.textContent = text;
      scenarioDescriptionEl.classList.remove("hidden");
    } else {
      scenarioDescriptionEl.textContent = "";
      scenarioDescriptionEl.classList.add("hidden");
    }
  }

  function renderScenarioSource(source) {
    if (!scenarioSourceEl) return;
    const text = typeof source === "string" ? source : "";
    if (text) {
      // textContent (not innerHTML) is the XSS-safe path — the source
      // ends up as a literal text node, never parsed as HTML.
      scenarioSourceEl.textContent = text;
      scenarioSourceEl.classList.remove("hidden");
    } else {
      scenarioSourceEl.textContent = "";
      scenarioSourceEl.classList.add("hidden");
    }
  }

  async function refreshScenarioReadout() {
    if (!scenarioCurrentEl) return;
    if (isReplay()) {
      const md = replay.metadata || {};
      const scenario = md.scenario == null ? "(none)" : md.scenario;
      scenarioCurrentEl.textContent = scenario;
      // Replay metadata.json doesn't carry the live class docstring or
      // module source; hide both rather than render stale snapshots.
      renderScenarioDescription(null);
      renderScenarioSource(null);
      return;
    }
    try {
      const res = await fetch("/scenario");
      if (!res.ok) return;
      const body = await res.json();
      const path = body.dotted_path;
      scenarioCurrentEl.textContent = path == null ? "(none)" : path;
      renderScenarioDescription(body.description);
      renderScenarioSource(body.source);
    } catch (err) {
      // ignore — boot race
    }
  }

  async function attachScenario(path) {
    try {
      const res = await fetch("/scenario", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ dotted_path: path }),
      });
      if (!res.ok) {
        let detail = String(res.status);
        try {
          const body = await res.json();
          detail = body.detail || detail;
        } catch (err) {
          // ignore
        }
        showToast(`scenario rejected: ${detail}`, "error");
        return false;
      }
      await refreshScenarioReadout();
      tick();
      return true;
    } catch (err) {
      showToast(`network error: ${err}`, "error");
      return false;
    }
  }

  async function getCurrentScenarioPath() {
    try {
      const res = await fetch("/scenario");
      if (!res.ok) return null;
      const body = await res.json();
      return body.dotted_path == null ? null : body.dotted_path;
    } catch (err) {
      return null;
    }
  }

  async function populateScenarioSelect(currentPath) {
    if (!scenarioSelectEl) return;
    scenarioSelectEl.innerHTML = "";
    const loading = document.createElement("option");
    loading.textContent = "Loading…";
    loading.disabled = true;
    scenarioSelectEl.appendChild(loading);
    scenarioSelectEl.disabled = true;
    if (scenarioConfirmBtn) scenarioConfirmBtn.disabled = true;
    try {
      const res = await fetch("/scenarios");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const scenarios = Array.isArray(body.scenarios) ? body.scenarios : [];
      scenarioSelectEl.innerHTML = "";
      if (scenarios.length === 0) {
        const opt = document.createElement("option");
        opt.textContent = "no scenarios discovered";
        opt.disabled = true;
        scenarioSelectEl.appendChild(opt);
        scenarioSelectEl.disabled = true;
        return;
      }
      for (const path of scenarios) {
        const opt = document.createElement("option");
        opt.value = path;
        opt.textContent = path;
        scenarioSelectEl.appendChild(opt);
      }
      if (currentPath && scenarios.includes(currentPath)) {
        scenarioSelectEl.value = currentPath;
      } else {
        scenarioSelectEl.value = scenarios[0];
      }
      scenarioSelectEl.disabled = false;
      if (scenarioConfirmBtn) scenarioConfirmBtn.disabled = false;
    } catch (err) {
      scenarioSelectEl.innerHTML = "";
      const opt = document.createElement("option");
      opt.textContent = "failed to load scenarios";
      opt.disabled = true;
      scenarioSelectEl.appendChild(opt);
      scenarioSelectEl.disabled = true;
      showToast(`could not list scenarios: ${err}`, "error");
    }
  }

  function openScenarioModal() {
    if (!scenarioModal) return;
    scenarioModal.classList.add("show");
    getCurrentScenarioPath().then((current) =>
      populateScenarioSelect(current).then(() => {
        if (scenarioSelectEl && !scenarioSelectEl.disabled) scenarioSelectEl.focus();
      }),
    );
  }
  function closeScenarioModal() {
    if (scenarioModal) scenarioModal.classList.remove("show");
  }

  if (scenarioChooseBtn) {
    scenarioChooseBtn.addEventListener("click", () => {
      if (isMutationLocked()) return;
      openScenarioModal();
    });
  }
  if (scenarioCancelBtn) {
    scenarioCancelBtn.addEventListener("click", () => closeScenarioModal());
  }
  if (scenarioConfirmBtn && scenarioSelectEl) {
    scenarioConfirmBtn.addEventListener("click", async () => {
      const val = (scenarioSelectEl.value || "").trim();
      if (!val) {
        showToast("pick a scenario first", "error");
        return;
      }
      const ok = await attachScenario(val);
      if (ok) {
        closeScenarioModal();
        showToast(`attached ${val}`, "ok");
      }
    });
  }
  if (scenarioDetachBtn) {
    scenarioDetachBtn.addEventListener("click", async () => {
      if (isMutationLocked()) return;
      const ok = await attachScenario(DETACH_DOTTED_PATH);
      if (ok) showToast("scenario detached", "ok");
    });
  }

  if (replaySliderEl) {
    replaySliderEl.addEventListener("input", () => {
      const idx = parseInt(replaySliderEl.value, 10);
      if (!isNaN(idx)) renderReplayFrame(idx);
    });
  }
  if (replayBackBtn) replayBackBtn.addEventListener("click", () => replayStepBy(-1));
  if (replayForwardBtn) replayForwardBtn.addEventListener("click", () => replayStepBy(1));
  if (replayCloseBtn) replayCloseBtn.addEventListener("click", () => closeReplay());

  window.addEventListener("keydown", (ev) => {
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (isReplay()) {
      if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        replayStepBy(-1);
        return;
      }
      if (ev.key === "ArrowRight") {
        ev.preventDefault();
        replayStepBy(1);
        return;
      }
    }
    // Space toggles Play / Pause; Shift+Space toggles Fast / Pause.
    // Works in both live and replay mode (the timer dispatcher branches
    // on `replay.mode` per tick).
    if (ev.key === " " || ev.code === "Space") {
      ev.preventDefault();
      const targetSpeed = ev.shiftKey ? fastCadenceMs : playCadenceMs;
      if (playSpeed === targetSpeed) pauseTimer();
      else startTimer(targetSpeed);
    }
  });

  let _lastRevealedCount = -1;
  let _lastWellsCount = -1;

  // No periodic `setInterval(tick)` for /state — every mutating action
  // (build, demolish, survey, drill, control, step) calls `tick()`
  // itself, so the UI stays current without polling continuously. The
  // play/fast/pause timer (issue 10) is opt-in and dispatches to /step
  // (live) or `renderReplayFrame` (replay) on a setInterval.
  // Agent Play attach/detach (agent-play slice 01). The Agent Play button
  // opens a modal with one text input; clicking Attach POSTs to
  // /agent/attach. Success surfaces a red dot + folder slug on the button
  // and reveals Detach. While attached the button itself is a no-op —
  // detach is the only way back to human play. This slice ships the
  // tracer-bullet UX; mutation lock + crash safety land in slices #3 / #4.
  const agentPlayBtn = document.getElementById("agent-play-btn");
  const agentDetachBtn = document.getElementById("agent-detach-btn");
  const agentStatusDot = document.getElementById("agent-status-dot");
  const agentFolderLabel = document.getElementById("agent-folder-label");
  const agentModal = document.getElementById("agent-modal");
  const agentFolderSelect = document.getElementById("agent-folder-select");
  const agentAttachConfirm = document.getElementById("agent-attach-confirm");
  const agentAttachCancel = document.getElementById("agent-attach-cancel");
  // `attachedAgentFolder` and `isAgentAttached()` are hoisted to the top of
  // this IIFE alongside `isMutationLocked()` so mutation-guard sites earlier
  // in the file (canvas handlers, slider rendering) can reference them.

  function renderAgentButton() {
    if (!agentPlayBtn) return;
    if (isAgentAttached()) {
      agentPlayBtn.classList.add("attached");
      if (agentStatusDot) agentStatusDot.classList.remove("hidden");
      if (agentFolderLabel) {
        agentFolderLabel.textContent = attachedAgentFolder;
        agentFolderLabel.classList.remove("hidden");
      }
      if (agentDetachBtn) agentDetachBtn.classList.remove("hidden");
    } else {
      agentPlayBtn.classList.remove("attached");
      if (agentStatusDot) agentStatusDot.classList.add("hidden");
      if (agentFolderLabel) {
        agentFolderLabel.textContent = "";
        agentFolderLabel.classList.add("hidden");
      }
      if (agentDetachBtn) agentDetachBtn.classList.add("hidden");
    }
  }

  async function refreshAgentStatus() {
    try {
      const res = await fetch("/agent");
      if (!res.ok) return;
      const body = await res.json();
      const wasAttached = isAgentAttached();
      attachedAgentFolder = body.folder == null ? null : body.folder;
      renderAgentButton();
      // If the lock state flipped (e.g. boot after a reload found a still-
      // attached agent), re-tick so slider rows reflect the new lock.
      if (wasAttached !== isAgentAttached()) tick();
    } catch (err) {
      // ignore — boot race
    }
  }

  async function populateAgentFolderSelect() {
    if (!agentFolderSelect) return;
    // Clear stale options before fetching so a slow request doesn't
    // leave the previous list visible alongside a "loading" hint.
    agentFolderSelect.innerHTML = "";
    const loading = document.createElement("option");
    loading.textContent = "Loading…";
    loading.disabled = true;
    agentFolderSelect.appendChild(loading);
    agentFolderSelect.disabled = true;
    if (agentAttachConfirm) agentAttachConfirm.disabled = true;
    try {
      const res = await fetch("/agent/folders");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const folders = Array.isArray(body.folders) ? body.folders : [];
      agentFolderSelect.innerHTML = "";
      if (folders.length === 0) {
        const opt = document.createElement("option");
        opt.textContent = "no folders with agent.py found";
        opt.disabled = true;
        agentFolderSelect.appendChild(opt);
        agentFolderSelect.disabled = true;
        return;
      }
      for (const folder of folders) {
        const opt = document.createElement("option");
        opt.value = folder;
        opt.textContent = folder;
        agentFolderSelect.appendChild(opt);
      }
      // Prefer the canonical participant submission folder when present;
      // otherwise fall back to the first listed folder.
      const preferred = folders.includes("submit") ? "submit" : folders[0];
      agentFolderSelect.value = preferred;
      agentFolderSelect.disabled = false;
      if (agentAttachConfirm) agentAttachConfirm.disabled = false;
    } catch (err) {
      agentFolderSelect.innerHTML = "";
      const opt = document.createElement("option");
      opt.textContent = "failed to load folders";
      opt.disabled = true;
      agentFolderSelect.appendChild(opt);
      agentFolderSelect.disabled = true;
      showToast(`could not list agent folders: ${err}`, "error");
    }
  }

  function openAgentModal() {
    if (!agentModal) return;
    agentModal.classList.add("show");
    populateAgentFolderSelect().then(() => {
      if (agentFolderSelect && !agentFolderSelect.disabled) {
        agentFolderSelect.focus();
      }
    });
  }
  function closeAgentModal() {
    if (agentModal) agentModal.classList.remove("show");
  }

  async function postAgentAttach(folder) {
    try {
      const res = await fetch("/agent/attach", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ folder }),
      });
      if (!res.ok) {
        let detail = String(res.status);
        try {
          const body = await res.json();
          detail = body.detail || detail;
        } catch (err) {
          // ignore
        }
        showToast(`agent attach failed: ${detail}`, "error");
        return false;
      }
      const body = await res.json();
      attachedAgentFolder = body.folder;
      renderAgentButton();
      // Refresh well/refinery tables so their slider `disabled` flags pick
      // up the mutation lock without waiting for the next /step.
      tick();
      showToast(`agent attached: ${body.folder}`, "ok");
      return true;
    } catch (err) {
      showToast(`network error: ${err}`, "error");
      return false;
    }
  }

  async function postAgentDetach() {
    try {
      const res = await fetch("/agent/detach", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      if (!res.ok) {
        showToast(`agent detach failed: HTTP ${res.status}`, "error");
        return;
      }
      attachedAgentFolder = null;
      renderAgentButton();
      // Re-render slider rows so they re-enable now that the lock is gone.
      tick();
      showToast("agent detached", "ok");
    } catch (err) {
      showToast(`network error: ${err}`, "error");
    }
  }

  if (agentPlayBtn) {
    agentPlayBtn.addEventListener("click", () => {
      // No-op while attached — the human re-takes control via Detach.
      if (isAgentAttached()) return;
      openAgentModal();
    });
  }
  if (agentAttachCancel) {
    agentAttachCancel.addEventListener("click", () => closeAgentModal());
  }
  if (agentAttachConfirm && agentFolderSelect) {
    agentAttachConfirm.addEventListener("click", async () => {
      const val = (agentFolderSelect.value || "").trim();
      if (!val) {
        showToast("folder is required", "error");
        return;
      }
      const ok = await postAgentAttach(val);
      if (ok) closeAgentModal();
    });
  }
  if (agentDetachBtn) {
    agentDetachBtn.addEventListener("click", () => postAgentDetach());
  }

  loadCatalog();
  drawGrid();
  tick();
  refreshScenarioReadout();
  refreshAgentStatus();
  updatePeekButtons();
})();
