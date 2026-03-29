"""
Gitwood — A pixel-art, animated, infinitely evolving GitHub ecosystem SVG.
Visualizes a developer's journey as a living tree that grows with lifetime
contributions and gains/loses leaves based on recent activity.

Structure:
  Module 1 — GitHub Activity Fetcher
  Module 2 — Tree State Manager
  Module 3 — Fractal Branch Generator
  Module 4 — Leaf System
  Module 5 — Ecosystem (Plants)
  Module 6 — SVG Generator
  Module 7 — Main Orchestrator
"""

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# .ENV LOADER (no external dependency)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    """Parse a simple KEY=VALUE .env file into os.environ (if not already set)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

CANVAS_W = 400
CANVAS_H = 300
PIXEL_SIZE = 4

MAX_LEAVES = 300
MAX_PLANTS = 5

TRUNK_BASE_X = 200
TRUNK_BASE_Y = 280
GRASS_Y = 284
GRASS_ROWS = 2

STATE_FILE = "gitwood.json"
SVG_FILE = "gitwood.svg"

COLORS = {
    "bg": "#0d1117",
    "trunk": "#8B4513",
    "branch_mid": "#6B3410",
    "branch_tip": "#5a2d0c",
    "leaf": ["#2d5a27", "#4a7c59", "#6aad5e", "#8bc34a"],
    "grass_dark": "#2d5a27",
    "grass_light": "#4a7c59",
}

EVENT_WEIGHTS = {
    "PushEvent": 2,
    "PullRequestEvent": 5,
    "IssuesEvent": 3,
    "CreateEvent": 2,
    "WatchEvent": 1,
    "ForkEvent": 1,
    "ReleaseEvent": 4,
    "PullRequestReviewEvent": 3,
    "IssueCommentEvent": 1,
    "CommitCommentEvent": 1,
}

STREAK_MILESTONES = {7, 30, 100, 365}


# ---------------------------------------------------------------------------
# MODULE 1 — GITHUB ACTIVITY FETCHER
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"

# Fetches account creation year
ACCOUNT_QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
  }
}
"""

# Fetches contributions for a specific year range
YEARLY_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
          }
        }
      }
    }
  }
}
"""


def _graphql_post(payload: dict, headers: dict) -> dict:
    """Execute a GraphQL request and return parsed JSON."""
    resp = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")
    return data


def fetch_graphql(username: str, token: str) -> dict:
    """Fetch LIFETIME total contributions by querying every year since account creation.

    GitHub's contributionsCollection defaults to the current year only.
    This loops from account creation year → now to get the true all-time total.

    Returns:
        {"total_contributions": int, "days": {"YYYY-MM-DD": int}}
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Step 1: get account creation year
    data = _graphql_post(
        {"query": ACCOUNT_QUERY, "variables": {"login": username}},
        headers,
    )
    created_at = data["data"]["user"]["createdAt"]  # e.g. "2018-04-12T10:22:00Z"
    created_year = int(created_at[:4])
    current_year = datetime.now(timezone.utc).year

    print(f"[gitwood] Account created {created_year}, scanning {created_year}–{current_year}...")

    # Step 2: query each year and accumulate
    total = 0
    all_days = {}

    for year in range(created_year, current_year + 1):
        from_dt = f"{year}-01-01T00:00:00Z"
        to_dt   = f"{year}-12-31T23:59:59Z"

        data = _graphql_post(
            {
                "query": YEARLY_QUERY,
                "variables": {"login": username, "from": from_dt, "to": to_dt},
            },
            headers,
        )
        calendar = (
            data["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        )
        year_total = calendar["totalContributions"]
        total += year_total
        print(f"[gitwood]   {year}: {year_total} contributions")

        for week in calendar["weeks"]:
            for day in week["contributionDays"]:
                if day["contributionCount"] > 0:
                    all_days[day["date"]] = day["contributionCount"]

    print(f"[gitwood] Lifetime total: {total} contributions")
    return {"total_contributions": total, "days": all_days}


def fetch_rest_events(username: str, token: str, pages: int = 3) -> list:
    """Fetch recent GitHub events via REST API.

    Returns flat list of raw event dicts (up to pages*100 events).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    events = []
    for page in range(1, pages + 1):
        url = f"{REST_URL}/users/{username}/events"
        resp = requests.get(
            url,
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        if resp.status_code in (401, 403):
            print(
                "Error: GH_TOKEN not set or invalid. "
                "Set it as a repository secret named GH_TOKEN.",
                file=sys.stderr,
            )
            sys.exit(1)
        if resp.status_code == 404:
            print(f"Error: User '{username}' not found.", file=sys.stderr)
            sys.exit(1)
        resp.raise_for_status()
        page_data = resp.json()
        if not page_data:
            break
        events.extend(page_data)
    return events


def compute_activity_score(events: list) -> dict:
    """Convert raw events to daily activity scores.

    Returns {"YYYY-MM-DD": int} for last 30 days.
    """
    scores = defaultdict(int)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for event in events:
        created = event.get("created_at", "")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt < cutoff:
            continue
        date_str = dt.strftime("%Y-%m-%d")
        weight = EVENT_WEIGHTS.get(event.get("type", ""), 0)
        # For PushEvent count commits
        if event.get("type") == "PushEvent":
            commits = len(event.get("payload", {}).get("commits", []))
            weight = max(weight, commits * 1)
        scores[date_str] += weight
    return dict(scores)


def get_today_score(events: list) -> int:
    """Return today's activity score from raw events."""
    scores = compute_activity_score(events)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return scores.get(today, 0)


def check_streak(activity: dict) -> int:
    """Count consecutive active days ending today."""
    streak = 0
    day = datetime.now(timezone.utc).date()
    while True:
        day_str = day.strftime("%Y-%m-%d")
        if activity.get(day_str, 0) > 0:
            streak += 1
            day -= timedelta(days=1)
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# MODULE 2 — TREE STATE MANAGER
# ---------------------------------------------------------------------------


def empty_state(username: str = "") -> dict:
    """Return a fresh, valid state dict."""
    return {
        "version": 1,
        "username": username,
        "total_contributions": 0,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "tree": {
            "branches": [],
            "leaves": [],
            "plants": [],
        },
        "activity": {
            "streak": 0,
            "recent_score": 0,
            "last_high_day": None,
        },
    }


def load_state(path: str = STATE_FILE) -> dict:
    """Load gitwood.json; return empty_state on missing or corrupt file."""
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Fill missing keys for forward compatibility
        base = empty_state(state.get("username", ""))
        for k, v in base.items():
            if k not in state:
                state[k] = v
        if "tree" not in state:
            state["tree"] = base["tree"]
        for k, v in base["tree"].items():
            if k not in state["tree"]:
                state["tree"][k] = v
        if "activity" not in state:
            state["activity"] = base["activity"]
        for k, v in base["activity"].items():
            if k not in state["activity"]:
                state["activity"][k] = v
        return state
    except (json.JSONDecodeError, KeyError):
        return empty_state()


def save_state(state: dict, path: str = STATE_FILE) -> None:
    """Atomically write state to path via a temp file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def update_metadata(state: dict, total: int, username: str) -> dict:
    """Update total_contributions, last_update, username in state."""
    state["total_contributions"] = total
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    state["username"] = username
    return state


def update_activity(
    state: dict, streak: int, today_score: int, last_high_day
) -> dict:
    """Update activity section of state."""
    state["activity"]["streak"] = streak
    state["activity"]["recent_score"] = today_score
    if last_high_day is not None:
        state["activity"]["last_high_day"] = last_high_day
    return state


# ---------------------------------------------------------------------------
# MODULE 3 — FRACTAL BRANCH GENERATOR
# ---------------------------------------------------------------------------


def compute_scale_params(total: int) -> dict:
    """Compute fractal parameters scaled to total contributions.

    Uses logarithmic scaling so small users get small trees,
    large users get large trees, without unbounded SVG growth.
    """
    t = max(total, 0)
    max_depth = min(6, max(2, int(math.log(t + 1, 2))))
    initial_length = min(80, max(20, int(math.log(t + 1) * 8)))
    trunk_thickness = min(6, max(1, int(math.log(t + 1, 10) * 2)))
    return {
        "max_depth": max_depth,
        "initial_length": initial_length,
        "trunk_thickness": trunk_thickness,
    }


def snap(value: float) -> int:
    """Snap a float coordinate to the nearest PIXEL_SIZE grid point."""
    return int(round(value / PIXEL_SIZE) * PIXEL_SIZE)


def rasterize_segment(
    x1: float, y1: float, x2: float, y2: float, depth: int, angle: float
) -> list:
    """Walk from (x1,y1) to (x2,y2) emitting PIXEL_SIZE pixel dicts.

    Uses linear interpolation (Bresenham-style) for correct pixel-art
    diagonal rendering. Deduplicates overlapping pixels. Clamps to canvas.
    """
    x1s, y1s = snap(x1), snap(y1)
    x2s, y2s = snap(x2), snap(y2)
    dx = x2s - x1s
    dy = y2s - y1s
    steps = max(abs(dx), abs(dy)) // PIXEL_SIZE
    if steps == 0:
        px, py = x1s, y1s
        if 0 <= px < CANVAS_W and 0 <= py < CANVAS_H:
            return [{"x": px, "y": py, "w": PIXEL_SIZE, "h": PIXEL_SIZE,
                     "depth": depth, "angle": angle}]
        return []

    pixels = []
    seen = set()
    for i in range(steps + 1):
        t = i / steps
        px = snap(x1s + t * dx)
        py = snap(y1s + t * dy)
        # Clamp to canvas bounds
        if not (0 <= px < CANVAS_W and 0 <= py < CANVAS_H):
            continue
        key = (px, py)
        if key not in seen:
            seen.add(key)
            pixels.append({
                "x": px, "y": py,
                "w": PIXEL_SIZE, "h": PIXEL_SIZE,
                "depth": depth, "angle": angle,
            })
    return pixels


def generate_branches(
    x: float, y: float, angle: float, length: float,
    depth: int, max_depth: int, branches: list, rng: random.Random
) -> None:
    """Recursive fractal branch generator. Mutates `branches` list.

    Each call draws a segment from (x,y) in direction `angle`,
    then spawns 2-3 child branches at reduced length.
    """
    if depth > max_depth or length < PIXEL_SIZE:
        return

    # Compute endpoint (SVG y-axis is flipped, so subtract for upward)
    x2 = x + math.cos(math.radians(angle)) * length
    y2 = y - math.sin(math.radians(angle)) * length

    # Rasterize this segment
    pixels = rasterize_segment(x, y, x2, y2, depth, angle)
    branches.extend(pixels)

    # Angle spread with controlled randomness
    spread = 25 + rng.randint(-10, 10)

    # Left child
    generate_branches(
        x2, y2, angle + spread, length * 0.7,
        depth + 1, max_depth, branches, rng
    )
    # Right child
    generate_branches(
        x2, y2, angle - spread, length * 0.7,
        depth + 1, max_depth, branches, rng
    )
    # Middle child only for early depths (creates fuller canopy)
    if depth < 2:
        generate_branches(
            x2, y2, angle + rng.randint(-5, 5), length * 0.6,
            depth + 1, max_depth, branches, rng
        )


def build_branches(total: int) -> list:
    """Build the full pixel list for the tree structure.

    Seeds RNG with total_contributions for determinism — same
    contribution count always produces the same tree shape.
    """
    params = compute_scale_params(total)
    rng = random.Random(total)  # deterministic seeding

    branches = []
    generate_branches(
        float(TRUNK_BASE_X), float(TRUNK_BASE_Y),
        90.0,  # straight up
        float(params["initial_length"]),
        0,
        params["max_depth"],
        branches,
        rng,
    )
    return branches


# ---------------------------------------------------------------------------
# MODULE 4 — LEAF SYSTEM
# ---------------------------------------------------------------------------


def collect_leaf_anchors(branches: list) -> list:
    """Extract tip positions from the deepest branch pixels.

    Returns list of (x, y) tuples sorted by depth descending.
    These are candidate positions for leaf attachment.
    """
    if not branches:
        return []
    max_depth = max(b["depth"] for b in branches)
    tip_depth = max(max_depth - 1, 0)
    anchors = [
        (b["x"], b["y"])
        for b in branches
        if b["depth"] >= tip_depth
    ]
    # Deduplicate
    return list(dict.fromkeys(anchors))


def compute_leaf_target(total: int) -> int:
    """Target leaf count proportional to contributions."""
    return min(MAX_LEAVES, max(5, total // 10))


def make_leaf(x: int, y: int, state: str, rng: random.Random) -> dict:
    """Construct a new leaf dict with randomized animation parameters."""
    return {
        "x": x,
        "y": y,
        "state": state,  # "attached" | "falling" | "regrowing"
        "color": rng.choice(COLORS["leaf"]),
        "color_idx": rng.randint(0, 3),
        "delay": round(rng.uniform(0.0, 5.0), 2),
        "duration": round(rng.uniform(2.5, 4.5), 2),
        "drift": rng.randint(-30, 30),
        "fell_at": None,
    }


def evolve_leaves(
    existing: list, anchors: list, today_score: int,
    total: int, rng: random.Random
) -> tuple:
    """Manage full leaf lifecycle. Returns (updated_leaves, completed_falling).

    Steps:
    1. Remove leaves whose anchor no longer exists in branch set
    2. Transition attached->falling based on inactivity
    3. Identify completed falling leaves (for plant spawning)
    4. Add regrowing leaves up to target
    """
    anchor_set = set(anchors)
    now = datetime.now(timezone.utc)
    target = compute_leaf_target(total)

    completed_falling = []
    updated = []

    for leaf in existing:
        pos = (leaf["x"], leaf["y"])

        # Skip leaves whose tree position no longer exists
        if pos not in anchor_set and leaf["state"] == "attached":
            continue

        if leaf["state"] == "falling":
            # Check if this leaf has "landed" (3x duration has elapsed)
            fell_at = leaf.get("fell_at")
            if fell_at:
                try:
                    fell_dt = datetime.fromisoformat(fell_at)
                    if fell_dt.tzinfo is None:
                        fell_dt = fell_dt.replace(tzinfo=timezone.utc)
                    elapsed = (now - fell_dt).total_seconds()
                    if elapsed > 3 * leaf["duration"]:
                        completed_falling.append(leaf)
                        continue  # Remove from active leaves
                except ValueError:
                    pass

        if leaf["state"] == "attached":
            # Transition to falling based on inactivity
            if today_score == 0 and rng.random() < 0.05:
                leaf = dict(leaf)
                leaf["state"] = "falling"
                leaf["fell_at"] = now.isoformat()
            elif today_score < 3 and rng.random() < 0.02:
                leaf = dict(leaf)
                leaf["state"] = "falling"
                leaf["fell_at"] = now.isoformat()

        if leaf["state"] == "regrowing":
            # Transition regrowing->attached after 1 cycle
            leaf = dict(leaf)
            leaf["state"] = "attached"

        updated.append(leaf)

    # Add regrowing leaves up to target from free anchors
    occupied = {(l["x"], l["y"]) for l in updated}
    free_anchors = [a for a in anchors if a not in occupied]
    rng.shuffle(free_anchors)

    deficit = target - len(updated)
    for i in range(min(deficit, len(free_anchors), 20)):  # max 20 new per run
        ax, ay = free_anchors[i]
        updated.append(make_leaf(ax, ay, "regrowing", rng))

    # Enforce MAX_LEAVES budget
    if len(updated) > MAX_LEAVES:
        updated = updated[:MAX_LEAVES]

    return updated, completed_falling


# ---------------------------------------------------------------------------
# MODULE 5 — ECOSYSTEM (PLANTS)
# ---------------------------------------------------------------------------

PLANT_PIXEL_SHAPES = {
    "sprout": [
        (0, 0), (0, -4), (0, -8), (-4, -8), (4, -8),
    ],
    "flower": [
        (0, 0), (0, -4), (0, -8),
        (-4, -12), (0, -12), (4, -12),
        (-4, -8), (4, -8), (0, -16),
    ],
    "shrub": [
        (-8, 0), (-4, 0), (0, 0), (4, 0), (8, 0),
        (-4, -4), (0, -4), (4, -4),
        (0, -8), (-8, -4), (8, -4),
    ],
    "mushroom": [
        (-4, -8), (0, -8), (4, -8),
        (-8, -4), (-4, -4), (0, -4), (4, -4), (8, -4),
        (-4, 0), (4, 0), (0, 4),
    ],
    "fern": [
        (0, 0), (0, -4), (0, -8), (0, -12),
        (-4, -4), (-8, -8),
        (4, -4), (8, -8), (4, -12), (-4, -12),
    ],
}

PLANT_COLORS = {
    "sprout": "#6aad5e",
    "flower": "#ff9966",
    "shrub": "#4a7c59",
    "mushroom": "#cc7755",
    "fern": "#2d5a27",
}

PLANT_STEM_COLOR = "#4a7c59"


def check_spawn_triggers(state: dict, today_score: int, rolling_avg: float) -> list:
    """Return list of trigger reasons that fired for plant spawning."""
    triggers = []
    plants = state["tree"]["plants"]
    if len(plants) >= MAX_PLANTS:
        return triggers

    if today_score > 15:
        triggers.append("high_day")

    streak = state["activity"].get("streak", 0)
    for milestone in STREAK_MILESTONES:
        if streak == milestone:
            # Check we haven't already logged this milestone
            trigger_name = f"streak_{milestone}"
            last_high = state["activity"].get("last_high_day", "")
            if last_high != trigger_name:
                triggers.append(trigger_name)

    if rolling_avg > 2 and today_score > 1.5 * rolling_avg:
        triggers.append("spike")

    return triggers


def maybe_spawn_from_leaf(leaf: dict, plants: list, rng: random.Random):
    """2% chance a completed falling leaf spawns a new plant."""
    if len(plants) >= MAX_PLANTS:
        return None
    if rng.random() > 0.02:
        return None
    plant_type = rng.choice(list(PLANT_PIXEL_SHAPES.keys()))
    # Spread plants across the canvas base
    x = rng.randint(20, CANVAS_W - 20)
    return {"x": x, "y": GRASS_Y, "size": 1, "type": plant_type}


def spawn_plant(x: int, plant_type: str) -> dict:
    """Construct a plant dict."""
    return {"x": x, "y": GRASS_Y, "size": 1, "type": plant_type}


def evolve_plants(
    state: dict, today_score: int, rolling_avg: float,
    completed_falling: list, rng: random.Random
) -> list:
    """Orchestrate plant spawning. Plants never die once added."""
    plants = list(state["tree"]["plants"])

    # Trigger-based spawning
    triggers = check_spawn_triggers(state, today_score, rolling_avg)
    for trigger in triggers:
        if len(plants) >= MAX_PLANTS:
            break
        plant_type = rng.choice(list(PLANT_PIXEL_SHAPES.keys()))
        # Spread new plants away from existing ones
        existing_xs = {p["x"] for p in plants}
        for _ in range(20):
            x = rng.randint(20, CANVAS_W - 20)
            if all(abs(x - ex) > 20 for ex in existing_xs):
                break
        plants.append(spawn_plant(x, plant_type))
        if trigger.startswith("streak_"):
            state["activity"]["last_high_day"] = trigger

    # Fallen leaf spawning (2% chance each)
    for leaf in completed_falling:
        if len(plants) >= MAX_PLANTS:
            break
        new_plant = maybe_spawn_from_leaf(leaf, plants, rng)
        if new_plant:
            plants.append(new_plant)

    return plants


# ---------------------------------------------------------------------------
# MODULE 6 — SVG GENERATOR
# ---------------------------------------------------------------------------


def _rect(x, y, w, h, fill, extra=""):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"{extra}/>'


def _use(href, x, y, extra=""):
    return f'<use href="#{href}" x="{x}" y="{y}"{extra}/>'


def _wind_animate(cx, cy, amplitude, duration, begin):
    """Emit animateTransform for wind sway (rotate type)."""
    a = amplitude
    vals = f"0 {cx} {cy};{-a} {cx} {cy};{a} {cx} {cy};{-a} {cx} {cy};0 {cx} {cy}"
    spline = "0.45 0 0.55 1"
    splines = ";".join([spline] * 4)
    return (
        f'<animateTransform attributeName="transform" type="rotate" '
        f'values="{vals}" keyTimes="0;0.25;0.5;0.75;1" '
        f'keySplines="{splines}" calcMode="spline" '
        f'dur="{duration}s" begin="{begin}s" repeatCount="indefinite"/>'
    )


def _fall_animate(from_x, from_y, drift, drop, duration, begin):
    """Emit animateTransform for falling leaf (translate type)."""
    to_x = from_x + drift
    to_y = from_y + drop
    return (
        f'<animateTransform attributeName="transform" type="translate" '
        f'from="{from_x} {from_y}" to="{to_x} {to_y}" '
        f'dur="{duration}s" begin="{begin}s" repeatCount="indefinite" '
        f'calcMode="spline" keySplines="0.25 0 0.75 1"/>'
    )


def _opacity_animate(from_val, to_val, duration, begin, fill="remove"):
    return (
        f'<animate attributeName="opacity" from="{from_val}" to="{to_val}" '
        f'dur="{duration}s" begin="{begin}s" fill="{fill}" repeatCount="indefinite"/>'
    )


def build_defs() -> str:
    """Build <defs> block with reusable pixel and leaf/plant shapes."""
    lines = ["<defs>"]
    lines.append(f'  <rect id="px" width="{PIXEL_SIZE}" height="{PIXEL_SIZE}"/>')

    # Leaf cross shapes (5 pixels each), 4 color variants
    offsets = [(0, -PIXEL_SIZE), (-PIXEL_SIZE, 0), (0, 0), (PIXEL_SIZE, 0), (0, PIXEL_SIZE)]
    for i, color in enumerate(COLORS["leaf"]):
        lines.append(f'  <g id="leaf-{i}">')
        for ox, oy in offsets:
            lines.append(f'    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, color)}')
        lines.append("  </g>")

    # Plant shapes
    for ptype, shape in PLANT_PIXEL_SHAPES.items():
        color = PLANT_COLORS.get(ptype, "#4a7c59")
        lines.append(f'  <g id="plant-{ptype}">')
        for ox, oy in shape:
            lines.append(f'    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, color)}')
        lines.append("  </g>")

    lines.append("</defs>")
    return "\n".join(lines)


def render_grass() -> str:
    """Render alternating dark/light pixel-art grass at the canvas bottom."""
    lines = ['<!-- Grass -->']
    for row in range(GRASS_ROWS):
        y = GRASS_Y + row * PIXEL_SIZE
        for col in range(CANVAS_W // PIXEL_SIZE):
            x = col * PIXEL_SIZE
            color = COLORS["grass_dark"] if (col + row) % 2 == 0 else COLORS["grass_light"]
            lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
    return "\n".join(lines)


def render_branches(branches: list) -> str:
    """Render all branch pixels in 3-tier wind groups.

    Tier 1 (depth 0-1): trunk, ±0.5°, 8s
    Tier 2 (depth 2-3): mid-branches, ±1.5°, 6s
    Tier 3 (depth 4+): tips, ±2.0°, 4-5s staggered left→right
    """
    if not branches:
        return "<!-- No branches -->"

    def branch_color(depth):
        if depth <= 1:
            return COLORS["trunk"]
        elif depth <= 3:
            return COLORS["branch_mid"]
        return COLORS["branch_tip"]

    # Separate by tier
    trunk_pixels = [b for b in branches if b["depth"] <= 1]
    mid_pixels = [b for b in branches if 2 <= b["depth"] <= 3]
    tip_pixels = [b for b in branches if b["depth"] >= 4]

    lines = ['<!-- Branches -->']

    # Trunk group
    if trunk_pixels:
        cx = TRUNK_BASE_X
        cy = TRUNK_BASE_Y
        lines.append(f'<g id="wind-trunk">')
        lines.append(_wind_animate(cx, cy, 0.5, 8, 0))
        for b in trunk_pixels:
            lines.append(_rect(b["x"], b["y"], b["w"], b["h"], branch_color(b["depth"])))
        lines.append("</g>")

    # Mid-branch group
    if mid_pixels:
        # Pivot at trunk top
        cx = TRUNK_BASE_X
        cy = min(b["y"] for b in mid_pixels) if mid_pixels else TRUNK_BASE_Y - 40
        lines.append(f'<g id="wind-mid">')
        lines.append(_wind_animate(cx, cy, 1.5, 6, 0.3))
        for b in mid_pixels:
            lines.append(_rect(b["x"], b["y"], b["w"], b["h"], branch_color(b["depth"])))
        lines.append("</g>")

    # Tip clusters — group by x-band for staggered wave effect
    if tip_pixels:
        band_size = CANVAS_W // 4  # 4 horizontal bands
        bands = defaultdict(list)
        for b in tip_pixels:
            band = b["x"] // band_size
            bands[band].append(b)

        for band_idx in sorted(bands.keys()):
            band_pixels = bands[band_idx]
            cx = int(sum(b["x"] for b in band_pixels) / len(band_pixels))
            cy = int(sum(b["y"] for b in band_pixels) / len(band_pixels))
            begin = round((cx / CANVAS_W) * 2.0, 2)
            dur = round(4.0 + (band_idx % 3) * 0.5, 1)
            lines.append(f'<g class="wind-tip">')
            lines.append(_wind_animate(cx, cy, 2.0, dur, begin))
            for b in band_pixels:
                lines.append(_rect(b["x"], b["y"], b["w"], b["h"], branch_color(b["depth"])))
            lines.append("</g>")

    return "\n".join(lines)


def render_leaves(leaves: list) -> str:
    """Render all leaves with appropriate animations per state."""
    if not leaves:
        return "<!-- No leaves -->"

    lines = ['<!-- Leaves -->']
    for leaf in leaves:
        x = leaf["x"]
        y = leaf["y"]
        color_idx = leaf.get("color_idx", 0)
        delay = leaf.get("delay", 0)
        duration = leaf.get("duration", 3.0)
        drift = leaf.get("drift", 0)
        state = leaf.get("state", "attached")

        if state == "attached":
            # Wind sway in place
            lines.append(f'<g>')
            lines.append(_wind_animate(x, y, 3.0, duration, delay))
            lines.append(_use(f"leaf-{color_idx}", x, y))
            lines.append("</g>")

        elif state == "falling":
            # Translate downward with lateral drift + fade out near ground
            drop = max(0, GRASS_Y - y)
            fade_begin = round(delay + duration * 0.8, 2)
            lines.append(f'<g>')
            lines.append(_fall_animate(x, y, drift, drop, duration, delay))
            lines.append(_opacity_animate(1, 0, round(duration * 0.2, 2), fade_begin))
            lines.append(_use(f"leaf-{color_idx}", 0, 0))
            lines.append("</g>")

        elif state == "regrowing":
            # Fade in from opacity 0 → 1
            lines.append(f'<g>')
            lines.append(
                f'<animate attributeName="opacity" from="0" to="1" '
                f'dur="2s" begin="{delay}s" fill="freeze"/>'
            )
            lines.append(_wind_animate(x, y, 3.0, duration, delay + 2))
            lines.append(_use(f"leaf-{color_idx}", x, y))
            lines.append("</g>")

    return "\n".join(lines)


def render_plants(plants: list) -> str:
    """Render plants as static pixel shapes at ground level."""
    if not plants:
        return "<!-- No plants -->"
    lines = ['<!-- Plants -->']
    for plant in plants:
        ptype = plant.get("type", "sprout")
        if ptype not in PLANT_PIXEL_SHAPES:
            ptype = "sprout"
        lines.append(_use(f"plant-{ptype}", plant["x"], plant["y"]))
    return "\n".join(lines)


def render_stars(rng: random.Random) -> str:
    """Render a static field of background stars."""
    lines = ['<!-- Stars -->']
    for _ in range(20):
        x = rng.randint(4, CANVAS_W - 4)
        y = rng.randint(4, 60)
        opacity = round(rng.uniform(0.3, 0.8), 1)
        lines.append(_rect(x, y, 2, 2, "#ffffff", f' opacity="{opacity}"'))
    return "\n".join(lines)


def generate_svg(state: dict, path: str = SVG_FILE) -> None:
    """Assemble the complete SVG from current state and write to path."""
    total = state.get("total_contributions", 0)
    branches = state["tree"].get("branches", [])
    leaves = state["tree"].get("leaves", [])
    plants = state["tree"].get("plants", [])

    # Seeded RNG for stable decorative elements (stars)
    star_rng = random.Random(total + 42)

    parts = []

    # Header
    parts.append(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'width="{CANVAS_W}" height="{CANVAS_H}" '
        'preserveAspectRatio="xMidYMid meet">'
    )

    # Defs
    parts.append(build_defs())

    # Background
    parts.append(f'<!-- Background -->')
    parts.append(_rect(0, 0, CANVAS_W, CANVAS_H, COLORS["bg"]))

    # Stars
    parts.append(render_stars(star_rng))

    # Grass
    parts.append(render_grass())

    # Tree layers (bottom to top)
    parts.append(render_branches(branches))
    parts.append(render_leaves(leaves))
    parts.append(render_plants(plants))

    # Close SVG
    parts.append("</svg>")

    content = "\n".join(parts)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    size_kb = os.path.getsize(path) / 1024
    print(f"[gitwood] SVG written to {path} ({size_kb:.1f} KB)")


# ---------------------------------------------------------------------------
# MODULE 7 — MAIN ORCHESTRATOR
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Gitwood — Animated GitHub ecosystem tree SVG generator"
    )
    parser.add_argument("--username", required=True, help="GitHub username")
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub personal access token (falls back to GH_TOKEN env var)",
    )
    parser.add_argument(
        "--mode",
        choices=["initial", "update"],
        default="update",
        help="'initial' for first run, 'update' for incremental (default: update)",
    )
    parser.add_argument(
        "--output", default=SVG_FILE, help=f"SVG output path (default: {SVG_FILE})"
    )
    parser.add_argument(
        "--state", default=STATE_FILE, help=f"State JSON path (default: {STATE_FILE})"
    )

    return parser.parse_args()


def compute_rolling_avg(activity_scores: dict, days: int = 7) -> float:
    """Return average activity score over the last N days."""
    if not activity_scores:
        return 0.0
    today = datetime.now(timezone.utc).date()
    total = 0
    count = 0
    for i in range(days):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        total += activity_scores.get(day, 0)
        count += 1
    return total / count if count > 0 else 0.0


def run_initial(username: str, token: str, args) -> None:
    """First-run mode: fetch all data, build tree from scratch."""
    print(f"[gitwood] Initial run for @{username}")

    print("[gitwood] Fetching lifetime contributions via GraphQL...")
    try:
        gql_data = fetch_graphql(username, token)
    except Exception as e:
        print(f"[gitwood] GraphQL fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    total = gql_data["total_contributions"]
    print(f"[gitwood] Total contributions: {total}")

    state = load_state(args.state)
    state = update_metadata(state, total, username)

    print("[gitwood] Building fractal branch structure...")
    branches = build_branches(total)
    state["tree"]["branches"] = branches
    print(f"[gitwood] Generated {len(branches)} branch pixels")

    # Initial leaves
    anchors = collect_leaf_anchors(branches)
    rng = random.Random(total + 1)
    leaves, _ = evolve_leaves([], anchors, 0, total, rng)
    state["tree"]["leaves"] = leaves
    print(f"[gitwood] Placed {len(leaves)} leaves")

    # No plants on first run
    state["tree"]["plants"] = []

    save_state(state, args.state)
    print(f"[gitwood] State saved to {args.state}")

    generate_svg(state, args.output)


def run_update(username: str, token: str, args) -> None:
    """Incremental update: fetch recent activity, evolve leaves and plants."""
    print(f"[gitwood] Update run for @{username}")

    state = load_state(args.state)
    if not state.get("username"):
        print("[gitwood] No existing state found, falling back to initial run.")
        run_initial(username, token, args)
        return

    print("[gitwood] Fetching recent events via REST API...")
    try:
        events = fetch_rest_events(username, token)
    except Exception as e:
        print(f"[gitwood] REST fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    activity_scores = compute_activity_score(events)
    today_score = get_today_score(events)
    streak = check_streak(activity_scores)
    rolling_avg = compute_rolling_avg(activity_scores)

    print(f"[gitwood] Today's score: {today_score}, Streak: {streak} days")

    # Refresh total contributions
    print("[gitwood] Refreshing total contributions via GraphQL...")
    try:
        gql_data = fetch_graphql(username, token)
        total = gql_data["total_contributions"]
    except Exception as e:
        print(f"[gitwood] GraphQL refresh failed (using cached): {e}")
        total = state.get("total_contributions", 0)

    old_total = state.get("total_contributions", 0)
    state = update_metadata(state, total, username)

    # Rebuild branches only if contribution count changed significantly
    if abs(total - old_total) > 10 or not state["tree"]["branches"]:
        print(f"[gitwood] Rebuilding branches ({old_total} → {total} contributions)...")
        branches = build_branches(total)
        state["tree"]["branches"] = branches
        print(f"[gitwood] Generated {len(branches)} branch pixels")
    else:
        branches = state["tree"]["branches"]
        print(f"[gitwood] Keeping existing {len(branches)} branch pixels")

    # Evolve leaves
    anchors = collect_leaf_anchors(branches)
    rng = random.Random(total + int(datetime.now(timezone.utc).timestamp()) % 10000)
    existing_leaves = state["tree"].get("leaves", [])
    leaves, completed_falling = evolve_leaves(
        existing_leaves, anchors, today_score, total, rng
    )
    state["tree"]["leaves"] = leaves
    print(f"[gitwood] Leaves: {len(leaves)} active, {len(completed_falling)} fell")

    # Evolve plants
    plants = evolve_plants(state, today_score, rolling_avg, completed_falling, rng)
    state["tree"]["plants"] = plants

    state = update_activity(
        state, streak, today_score,
        last_high_day=state["activity"].get("last_high_day")
    )

    save_state(state, args.state)
    print(f"[gitwood] State saved to {args.state}")

    generate_svg(state, args.output)


def main():
    args = parse_args()

    # Resolve token
    token = args.token or os.environ.get("GH_TOKEN", "")
    if not token:
        print(
            "Error: No GitHub token provided. "
            "Use --token or set the GH_TOKEN environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.mode == "initial":
        run_initial(args.username, token, args)
    else:
        run_update(args.username, token, args)


if __name__ == "__main__":
    main()
