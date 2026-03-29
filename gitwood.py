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

Improvements over v1:
  - Seasons: leaf colors change with the calendar (spring/summer/autumn/winter)
  - Day/Night: sky background and celestial body shift with the current hour
  - Year cache: past years stored in state, skipped on subsequent fetches
  - Roots: pixel root system grows below the trunk
  - Fruit: milestone orbs appear on branches at 100/500/1000 contributions
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
TRUNK_BASE_Y = 272
GRASS_Y = 276
GRASS_ROWS = 2

STATE_FILE = "gitwood.json"
SVG_FILE = "gitwood.svg"

# Base colors (overridden per season/time in generate_svg)
COLORS = {
    "trunk": "#8B4513",
    "branch_mid": "#6B3410",
    "branch_tip": "#5a2d0c",
    "root": "#6B3410",
    "grass_dark": "#2d5a27",
    "grass_light": "#4a7c59",
    "fruit": "#cc3333",
    "fruit_ripe": "#ff6633",
    "star": "#ffffff",
    "sun": "#f5d060",
    "moon": "#d4cfb0",
}

# Seasonal leaf color palettes
SEASON_LEAF_COLORS = {
    "spring":  ["#6aad5e", "#8bc34a", "#a5d6a7", "#c8e6c9"],
    "summer":  ["#2d5a27", "#4a7c59", "#6aad5e", "#8bc34a"],
    "autumn":  ["#c84b11", "#d4620a", "#e8941a", "#f0c040"],
    "winter":  ["#7a6652", "#8d7b6a", "#5a7a65", "#4a7c59"],
}

# Sky color palettes keyed by time-of-day bucket
SKY_COLORS = {
    "night":   "#0d1117",
    "dawn":    "#1a1035",
    "day":     "#0f2744",
    "dusk":    "#1c0f2e",
}

GRASS_SEASON_COLORS = {
    "spring":  ("#3a7a30", "#5a9e50"),
    "summer":  ("#2d5a27", "#4a7c59"),
    "autumn":  ("#6b7a30", "#8a9a40"),
    "winter":  ("#4a5a48", "#5a6a58"),
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
FRUIT_MILESTONES  = [100, 500, 1000, 2000, 5000]


# ---------------------------------------------------------------------------
# SEASON / TIME HELPERS
# ---------------------------------------------------------------------------

def get_season() -> str:
    """Return current season based on calendar month."""
    month = datetime.now(timezone.utc).month
    if month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    elif month in (9, 10, 11):
        return "autumn"
    return "winter"


def get_time_of_day() -> str:
    """Return time-of-day bucket based on UTC hour."""
    hour = datetime.now(timezone.utc).hour
    if 6 <= hour < 8:
        return "dawn"
    elif 8 <= hour < 18:
        return "day"
    elif 18 <= hour < 20:
        return "dusk"
    return "night"


def is_nighttime() -> bool:
    return get_time_of_day() == "night"


def fruit_count(total: int) -> int:
    """Number of fruit orbs to render based on contribution milestones reached."""
    return sum(1 for m in FRUIT_MILESTONES if total >= m)


# ---------------------------------------------------------------------------
# MODULE 1 — GITHUB ACTIVITY FETCHER
# ---------------------------------------------------------------------------

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"

ACCOUNT_QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
  }
}
"""

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


def fetch_graphql(username: str, token: str, state: dict) -> dict:
    """Fetch LIFETIME contributions, using cached past-year data to save API calls.

    Past years (< current year) are stored in state["yearly_cache"] and skipped
    on subsequent runs — their counts can never change. Only the current year
    is always re-fetched.

    Returns:
        {"total_contributions": int, "days": {"YYYY-MM-DD": int}}
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    yearly_cache = state.get("yearly_cache", {})
    current_year = datetime.now(timezone.utc).year

    # Fetch account creation year (needed to know scan range)
    data = _graphql_post(
        {"query": ACCOUNT_QUERY, "variables": {"login": username}},
        headers,
    )
    created_at = data["data"]["user"]["createdAt"]
    created_year = int(created_at[:4])

    print(f"[gitwood] Account created {created_year}, scanning {created_year}–{current_year}...")

    total = 0
    all_days = {}

    for year in range(created_year, current_year + 1):
        year_str = str(year)

        # Use cached value for past years — they can't change
        if year < current_year and year_str in yearly_cache:
            year_total = yearly_cache[year_str]
            print(f"[gitwood]   {year}: {year_total} contributions (cached)")
            total += year_total
            continue

        # Fetch from API
        from_dt = f"{year}-01-01T00:00:00Z"
        to_dt   = f"{year}-12-31T23:59:59Z"
        data = _graphql_post(
            {
                "query": YEARLY_QUERY,
                "variables": {"login": username, "from": from_dt, "to": to_dt},
            },
            headers,
        )
        calendar = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        year_total = calendar["totalContributions"]
        total += year_total
        print(f"[gitwood]   {year}: {year_total} contributions")

        # Cache completed past years
        if year < current_year:
            yearly_cache[year_str] = year_total

        for week in calendar["weeks"]:
            for day in week["contributionDays"]:
                if day["contributionCount"] > 0:
                    all_days[day["date"]] = day["contributionCount"]

    print(f"[gitwood] Lifetime total: {total} contributions")
    return {"total_contributions": total, "days": all_days, "yearly_cache": yearly_cache}


def fetch_rest_events(username: str, token: str, pages: int = 3) -> list:
    """Fetch recent GitHub events via REST API."""
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
    """Convert raw events to daily activity scores for last 30 days."""
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
        if event.get("type") == "PushEvent":
            commits = len(event.get("payload", {}).get("commits", []))
            weight = max(weight, commits)
        scores[date_str] += weight
    return dict(scores)


def get_today_score(events: list) -> int:
    scores = compute_activity_score(events)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return scores.get(today, 0)


def check_streak(activity: dict) -> int:
    streak = 0
    day = datetime.now(timezone.utc).date()
    while True:
        if activity.get(day.strftime("%Y-%m-%d"), 0) > 0:
            streak += 1
            day -= timedelta(days=1)
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# MODULE 2 — TREE STATE MANAGER
# ---------------------------------------------------------------------------


def empty_state(username: str = "") -> dict:
    return {
        "version": 2,
        "username": username,
        "total_contributions": 0,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "yearly_cache": {},
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
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        base = empty_state(state.get("username", ""))
        for k, v in base.items():
            if k not in state:
                state[k] = v
        for k, v in base["tree"].items():
            if k not in state["tree"]:
                state["tree"][k] = v
        for k, v in base["activity"].items():
            if k not in state["activity"]:
                state["activity"][k] = v
        return state
    except (json.JSONDecodeError, KeyError):
        return empty_state()


def save_state(state: dict, path: str = STATE_FILE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def update_metadata(state: dict, total: int, username: str, yearly_cache: dict) -> dict:
    state["total_contributions"] = total
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    state["username"] = username
    state["yearly_cache"] = yearly_cache
    return state


def update_activity(state: dict, streak: int, today_score: int, last_high_day) -> dict:
    state["activity"]["streak"] = streak
    state["activity"]["recent_score"] = today_score
    if last_high_day is not None:
        state["activity"]["last_high_day"] = last_high_day
    return state


# ---------------------------------------------------------------------------
# MODULE 3 — FRACTAL BRANCH GENERATOR
# ---------------------------------------------------------------------------


def compute_scale_params(total: int) -> dict:
    t = max(total, 0)
    max_depth      = min(6, max(2, int(math.log(t + 1, 2))))
    initial_length = min(80, max(20, int(math.log(t + 1) * 8)))
    trunk_thickness = min(6, max(1, int(math.log(t + 1, 10) * 2)))
    return {
        "max_depth": max_depth,
        "initial_length": initial_length,
        "trunk_thickness": trunk_thickness,
    }


def snap(value: float) -> int:
    return int(round(value / PIXEL_SIZE) * PIXEL_SIZE)


def rasterize_segment(x1, y1, x2, y2, depth, angle) -> list:
    x1s, y1s = snap(x1), snap(y1)
    x2s, y2s = snap(x2), snap(y2)
    dx = x2s - x1s
    dy = y2s - y1s
    steps = max(abs(dx), abs(dy)) // PIXEL_SIZE
    if steps == 0:
        if 0 <= x1s < CANVAS_W and 0 <= y1s < CANVAS_H:
            return [{"x": x1s, "y": y1s, "w": PIXEL_SIZE, "h": PIXEL_SIZE,
                     "depth": depth, "angle": angle}]
        return []
    pixels = []
    seen = set()
    for i in range(steps + 1):
        t = i / steps
        px = snap(x1s + t * dx)
        py = snap(y1s + t * dy)
        if not (0 <= px < CANVAS_W and 0 <= py < CANVAS_H):
            continue
        key = (px, py)
        if key not in seen:
            seen.add(key)
            pixels.append({"x": px, "y": py, "w": PIXEL_SIZE, "h": PIXEL_SIZE,
                           "depth": depth, "angle": angle})
    return pixels


def generate_branches(x, y, angle, length, depth, max_depth, branches, rng):
    if depth > max_depth or length < PIXEL_SIZE:
        return
    x2 = x + math.cos(math.radians(angle)) * length
    y2 = y - math.sin(math.radians(angle)) * length
    branches.extend(rasterize_segment(x, y, x2, y2, depth, angle))
    spread = 25 + rng.randint(-10, 10)
    generate_branches(x2, y2, angle + spread, length * 0.7, depth + 1, max_depth, branches, rng)
    generate_branches(x2, y2, angle - spread, length * 0.7, depth + 1, max_depth, branches, rng)
    if depth < 2:
        generate_branches(x2, y2, angle + rng.randint(-5, 5), length * 0.6,
                          depth + 1, max_depth, branches, rng)


def build_branches(total: int) -> list:
    params = compute_scale_params(total)
    rng = random.Random(total)
    branches = []
    generate_branches(
        float(TRUNK_BASE_X), float(TRUNK_BASE_Y),
        90.0, float(params["initial_length"]),
        0, params["max_depth"], branches, rng,
    )
    return branches


# ---------------------------------------------------------------------------
# MODULE 4 — LEAF SYSTEM
# ---------------------------------------------------------------------------


def collect_leaf_anchors(branches: list) -> list:
    if not branches:
        return []
    max_depth = max(b["depth"] for b in branches)
    tip_depth = max(max_depth - 1, 0)
    anchors = [(b["x"], b["y"]) for b in branches if b["depth"] >= tip_depth]
    return list(dict.fromkeys(anchors))


def compute_leaf_target(total: int, season: str) -> int:
    base = min(MAX_LEAVES, max(5, total // 10))
    # Winter has fewer leaves
    if season == "winter":
        return max(3, base // 3)
    elif season == "autumn":
        return max(5, base // 2)
    return base


def make_leaf(x: int, y: int, state: str, rng: random.Random, season: str = "summer") -> dict:
    return {
        "x": x,
        "y": y,
        "state": state,
        "color_idx": rng.randint(0, 3),
        "delay":    round(rng.uniform(0.0, 5.0), 2),
        "duration": round(rng.uniform(2.5, 4.5), 2),
        "drift":    rng.randint(-30, 30),
        "fell_at":  None,
        "season":   season,
    }


def evolve_leaves(existing, anchors, today_score, total, rng, season="summer"):
    anchor_set = set(anchors)
    now = datetime.now(timezone.utc)
    target = compute_leaf_target(total, season)
    completed_falling = []
    updated = []

    for leaf in existing:
        pos = (leaf["x"], leaf["y"])
        if pos not in anchor_set and leaf["state"] == "attached":
            continue

        if leaf["state"] == "falling":
            fell_at = leaf.get("fell_at")
            if fell_at:
                try:
                    fell_dt = datetime.fromisoformat(fell_at)
                    if fell_dt.tzinfo is None:
                        fell_dt = fell_dt.replace(tzinfo=timezone.utc)
                    if (now - fell_dt).total_seconds() > 3 * leaf["duration"]:
                        completed_falling.append(leaf)
                        continue
                except ValueError:
                    pass

        if leaf["state"] == "attached":
            fall_chance = 0.08 if season == "autumn" else (0.15 if season == "winter" else 0.05)
            if today_score == 0 and rng.random() < fall_chance:
                leaf = dict(leaf)
                leaf["state"] = "falling"
                leaf["fell_at"] = now.isoformat()
            elif today_score < 3 and rng.random() < 0.02:
                leaf = dict(leaf)
                leaf["state"] = "falling"
                leaf["fell_at"] = now.isoformat()

        if leaf["state"] == "regrowing":
            leaf = dict(leaf)
            leaf["state"] = "attached"

        updated.append(leaf)

    occupied = {(l["x"], l["y"]) for l in updated}
    free_anchors = [a for a in anchors if a not in occupied]
    rng.shuffle(free_anchors)

    deficit = target - len(updated)
    for i in range(min(deficit, len(free_anchors), 20)):
        ax, ay = free_anchors[i]
        updated.append(make_leaf(ax, ay, "regrowing", rng, season))

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
    "sprout":   "#6aad5e",
    "flower":   "#ff9966",
    "shrub":    "#4a7c59",
    "mushroom": "#cc7755",
    "fern":     "#2d5a27",
}


def check_spawn_triggers(state, today_score, rolling_avg):
    triggers = []
    if len(state["tree"]["plants"]) >= MAX_PLANTS:
        return triggers
    if today_score > 15:
        triggers.append("high_day")
    streak = state["activity"].get("streak", 0)
    for milestone in STREAK_MILESTONES:
        if streak == milestone:
            trigger_name = f"streak_{milestone}"
            if state["activity"].get("last_high_day") != trigger_name:
                triggers.append(trigger_name)
    if rolling_avg > 2 and today_score > 1.5 * rolling_avg:
        triggers.append("spike")
    return triggers


def maybe_spawn_from_leaf(leaf, plants, rng):
    if len(plants) >= MAX_PLANTS or rng.random() > 0.02:
        return None
    plant_type = rng.choice(list(PLANT_PIXEL_SHAPES.keys()))
    x = rng.randint(20, CANVAS_W - 20)
    return {"x": x, "y": GRASS_Y, "size": 1, "type": plant_type}


def spawn_plant(x, plant_type):
    return {"x": x, "y": GRASS_Y, "size": 1, "type": plant_type}


def evolve_plants(state, today_score, rolling_avg, completed_falling, rng):
    plants = list(state["tree"]["plants"])
    triggers = check_spawn_triggers(state, today_score, rolling_avg)
    for trigger in triggers:
        if len(plants) >= MAX_PLANTS:
            break
        plant_type = rng.choice(list(PLANT_PIXEL_SHAPES.keys()))
        existing_xs = {p["x"] for p in plants}
        for _ in range(20):
            x = rng.randint(20, CANVAS_W - 20)
            if all(abs(x - ex) > 20 for ex in existing_xs):
                break
        plants.append(spawn_plant(x, plant_type))
        if trigger.startswith("streak_"):
            state["activity"]["last_high_day"] = trigger
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
    return (
        f'<animateTransform attributeName="transform" type="translate" '
        f'from="{from_x} {from_y}" to="{from_x + drift} {from_y + drop}" '
        f'dur="{duration}s" begin="{begin}s" repeatCount="indefinite" '
        f'calcMode="spline" keySplines="0.25 0 0.75 1"/>'
    )


def _opacity_animate(from_val, to_val, duration, begin, fill="remove"):
    return (
        f'<animate attributeName="opacity" from="{from_val}" to="{to_val}" '
        f'dur="{duration}s" begin="{begin}s" fill="{fill}" repeatCount="indefinite"/>'
    )


def build_defs(leaf_colors: list) -> str:
    """Build <defs> with season-aware leaf colors and all plant/fruit shapes."""
    lines = ["<defs>"]
    lines.append(f'  <rect id="px" width="{PIXEL_SIZE}" height="{PIXEL_SIZE}"/>')

    # Leaf cross shapes (5 pixels), 4 seasonal color variants
    offsets = [(0, -PIXEL_SIZE), (-PIXEL_SIZE, 0), (0, 0), (PIXEL_SIZE, 0), (0, PIXEL_SIZE)]
    for i, color in enumerate(leaf_colors):
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

    # Fruit orb shape (small 3x3 cluster of pixels)
    fruit_pixels = [(-4, -4), (0, -4), (4, -4), (-4, 0), (0, 0), (4, 0), (0, 4)]
    lines.append(f'  <g id="fruit">')
    for ox, oy in fruit_pixels:
        lines.append(f'    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, COLORS["fruit"])}')
    lines.append("  </g>")

    lines.append("</defs>")
    return "\n".join(lines)


def render_sky(tod: str) -> str:
    """Render background and celestial body (sun or moon) based on time of day."""
    bg = SKY_COLORS[tod]
    lines = [f'<!-- Sky ({tod}) -->', _rect(0, 0, CANVAS_W, CANVAS_H, bg)]

    if tod == "night":
        # Moon: top-right corner, crescent-style
        mx, my = 340, 30
        moon_pixels = [(0,0),(4,0),(8,0),(0,4),(0,8),(4,8)]
        for ox, oy in moon_pixels:
            lines.append(_rect(mx + ox, my + oy, PIXEL_SIZE, PIXEL_SIZE, COLORS["moon"]))

    elif tod == "day":
        # Sun: top-left corner, cross shape with rays
        sx, sy = 28, 20
        sun_pixels = [
            (0,-8),(0,-4),(0,0),(0,4),(0,8),
            (-8,0),(-4,0),(4,0),(8,0),
            (-4,-4),(4,-4),(-4,4),(4,4),
        ]
        for ox, oy in sun_pixels:
            lines.append(_rect(sx + ox, sy + oy, PIXEL_SIZE, PIXEL_SIZE, COLORS["sun"]))

    elif tod == "dawn":
        # Horizon glow: thin strip at ground level
        for x in range(0, CANVAS_W, PIXEL_SIZE):
            lines.append(_rect(x, GRASS_Y - PIXEL_SIZE * 3, PIXEL_SIZE, PIXEL_SIZE,
                               "#3a1f5c", f' opacity="0.4"'))

    elif tod == "dusk":
        # Orange horizon glow
        for x in range(0, CANVAS_W, PIXEL_SIZE):
            lines.append(_rect(x, GRASS_Y - PIXEL_SIZE * 3, PIXEL_SIZE, PIXEL_SIZE,
                               "#8b2500", f' opacity="0.4"'))

    return "\n".join(lines)


def render_stars(rng: random.Random, tod: str) -> str:
    """Render stars — visible at night and dawn only."""
    if tod not in ("night", "dawn"):
        return "<!-- No stars (daytime) -->"
    lines = ["<!-- Stars -->"]
    count = 20 if tod == "night" else 8
    for _ in range(count):
        x = rng.randint(4, CANVAS_W - 4)
        y = rng.randint(4, 60)
        opacity = round(rng.uniform(0.3, 0.8), 1)
        lines.append(_rect(x, y, 2, 2, COLORS["star"], f' opacity="{opacity}"'))
    return "\n".join(lines)


def render_grass(season: str) -> str:
    """Render alternating pixel-art grass with season-appropriate colors."""
    dark, light = GRASS_SEASON_COLORS.get(season, ("#2d5a27", "#4a7c59"))
    lines = [f'<!-- Grass ({season}) -->']
    for row in range(GRASS_ROWS):
        y = GRASS_Y + row * PIXEL_SIZE
        for col in range(CANVAS_W // PIXEL_SIZE):
            x = col * PIXEL_SIZE
            color = dark if (col + row) % 2 == 0 else light
            lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
    return "\n".join(lines)


def render_roots(total: int) -> str:
    """Render pixel root system below the trunk. Grows with contributions."""
    if total < 10:
        return "<!-- No roots yet -->"

    lines = ["<!-- Roots -->"]
    root_depth = min(4, max(1, int(math.log(total + 1, 10) * 2)))
    color = COLORS["root"]

    # Central tap root (straight down)
    for i in range(root_depth):
        y = TRUNK_BASE_Y + GRASS_ROWS * PIXEL_SIZE + i * PIXEL_SIZE
        if y >= CANVAS_H:
            break
        lines.append(_rect(TRUNK_BASE_X, y, PIXEL_SIZE, PIXEL_SIZE, color))

    # Left root
    if total >= 30:
        for i in range(min(root_depth, 3)):
            x = TRUNK_BASE_X - (i + 1) * PIXEL_SIZE
            y = TRUNK_BASE_Y + GRASS_ROWS * PIXEL_SIZE + i * PIXEL_SIZE
            if 0 <= x < CANVAS_W and y < CANVAS_H:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))

    # Right root
    if total >= 30:
        for i in range(min(root_depth, 3)):
            x = TRUNK_BASE_X + (i + 2) * PIXEL_SIZE
            y = TRUNK_BASE_Y + GRASS_ROWS * PIXEL_SIZE + i * PIXEL_SIZE
            if 0 <= x < CANVAS_W and y < CANVAS_H:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))

    # Deeper side roots for veteran contributors
    if total >= 200:
        for i in range(2):
            x = TRUNK_BASE_X - (root_depth + i) * PIXEL_SIZE
            y = TRUNK_BASE_Y + GRASS_ROWS * PIXEL_SIZE + root_depth * PIXEL_SIZE
            if 0 <= x < CANVAS_W and y < CANVAS_H:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
            x = TRUNK_BASE_X + (root_depth + i + 1) * PIXEL_SIZE
            if 0 <= x < CANVAS_W and y < CANVAS_H:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))

    return "\n".join(lines)


def render_branches(branches: list) -> str:
    """Render branches in 3-tier wind groups."""
    if not branches:
        return "<!-- No branches -->"

    def branch_color(depth):
        if depth <= 1:   return COLORS["trunk"]
        elif depth <= 3: return COLORS["branch_mid"]
        return COLORS["branch_tip"]

    trunk_pixels = [b for b in branches if b["depth"] <= 1]
    mid_pixels   = [b for b in branches if 2 <= b["depth"] <= 3]
    tip_pixels   = [b for b in branches if b["depth"] >= 4]

    lines = ["<!-- Branches -->"]

    if trunk_pixels:
        lines.append('<g id="wind-trunk">')
        lines.append(_wind_animate(TRUNK_BASE_X, TRUNK_BASE_Y, 0.5, 8, 0))
        for b in trunk_pixels:
            lines.append(_rect(b["x"], b["y"], b["w"], b["h"], branch_color(b["depth"])))
        lines.append("</g>")

    if mid_pixels:
        cy = min(b["y"] for b in mid_pixels)
        lines.append('<g id="wind-mid">')
        lines.append(_wind_animate(TRUNK_BASE_X, cy, 1.5, 6, 0.3))
        for b in mid_pixels:
            lines.append(_rect(b["x"], b["y"], b["w"], b["h"], branch_color(b["depth"])))
        lines.append("</g>")

    if tip_pixels:
        band_size = CANVAS_W // 4
        bands = defaultdict(list)
        for b in tip_pixels:
            bands[b["x"] // band_size].append(b)
        for band_idx in sorted(bands.keys()):
            bp = bands[band_idx]
            cx = int(sum(b["x"] for b in bp) / len(bp))
            cy = int(sum(b["y"] for b in bp) / len(bp))
            begin = round((cx / CANVAS_W) * 2.0, 2)
            dur   = round(4.0 + (band_idx % 3) * 0.5, 1)
            lines.append('<g class="wind-tip">')
            lines.append(_wind_animate(cx, cy, 2.0, dur, begin))
            for b in bp:
                lines.append(_rect(b["x"], b["y"], b["w"], b["h"], branch_color(b["depth"])))
            lines.append("</g>")

    return "\n".join(lines)


def render_fruit(branches: list, total: int) -> str:
    """Render milestone fruit orbs on branch tips."""
    count = fruit_count(total)
    if count == 0 or not branches:
        return "<!-- No fruit yet -->"

    # Pick stable tip positions seeded by milestone count
    tips = [b for b in branches if b["depth"] >= 4]
    if not tips:
        return "<!-- No tips for fruit -->"

    rng = random.Random(total * 7)
    rng.shuffle(tips)
    chosen = tips[:count]

    lines = ["<!-- Fruit (milestone orbs) -->"]
    for tip in chosen:
        lines.append(_use("fruit", tip["x"], tip["y"] - PIXEL_SIZE * 2))
    return "\n".join(lines)


def render_leaves(leaves: list) -> str:
    """Render all leaves with per-state animations."""
    if not leaves:
        return "<!-- No leaves -->"
    lines = ["<!-- Leaves -->"]
    for leaf in leaves:
        x, y = leaf["x"], leaf["y"]
        color_idx = leaf.get("color_idx", 0)
        delay     = leaf.get("delay", 0)
        duration  = leaf.get("duration", 3.0)
        drift     = leaf.get("drift", 0)
        state     = leaf.get("state", "attached")

        if state == "attached":
            lines.append("<g>")
            lines.append(_wind_animate(x, y, 3.0, duration, delay))
            lines.append(_use(f"leaf-{color_idx}", x, y))
            lines.append("</g>")

        elif state == "falling":
            drop = max(0, GRASS_Y - y)
            fade_begin = round(delay + duration * 0.8, 2)
            lines.append("<g>")
            lines.append(_fall_animate(x, y, drift, drop, duration, delay))
            lines.append(_opacity_animate(1, 0, round(duration * 0.2, 2), fade_begin))
            lines.append(_use(f"leaf-{color_idx}", 0, 0))
            lines.append("</g>")

        elif state == "regrowing":
            lines.append("<g>")
            lines.append(
                f'<animate attributeName="opacity" from="0" to="1" '
                f'dur="2s" begin="{delay}s" fill="freeze"/>'
            )
            lines.append(_wind_animate(x, y, 3.0, duration, delay + 2))
            lines.append(_use(f"leaf-{color_idx}", x, y))
            lines.append("</g>")

    return "\n".join(lines)


def render_plants(plants: list) -> str:
    if not plants:
        return "<!-- No plants -->"
    lines = ["<!-- Plants -->"]
    for plant in plants:
        ptype = plant.get("type", "sprout")
        if ptype not in PLANT_PIXEL_SHAPES:
            ptype = "sprout"
        lines.append(_use(f"plant-{ptype}", plant["x"], plant["y"]))
    return "\n".join(lines)


def generate_svg(state: dict, path: str = SVG_FILE) -> None:
    """Assemble the complete SVG with all layers and write to path."""
    total    = state.get("total_contributions", 0)
    branches = state["tree"].get("branches", [])
    leaves   = state["tree"].get("leaves", [])
    plants   = state["tree"].get("plants", [])

    season      = get_season()
    tod         = get_time_of_day()
    leaf_colors = SEASON_LEAF_COLORS[season]

    star_rng = random.Random(total + 42)

    parts = []
    parts.append(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'width="{CANVAS_W}" height="{CANVAS_H}" '
        'preserveAspectRatio="xMidYMid meet">'
    )
    parts.append(build_defs(leaf_colors))
    parts.append(render_sky(tod))
    parts.append(render_stars(star_rng, tod))
    parts.append(render_grass(season))
    parts.append(render_roots(total))
    parts.append(render_branches(branches))
    parts.append(render_fruit(branches, total))
    parts.append(render_leaves(leaves))
    parts.append(render_plants(plants))
    parts.append("</svg>")

    content = "\n".join(parts)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    size_kb = os.path.getsize(path) / 1024
    print(f"[gitwood] SVG written to {path} ({size_kb:.1f} KB) | season={season} tod={tod}")


# ---------------------------------------------------------------------------
# MODULE 7 — MAIN ORCHESTRATOR
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Gitwood — Animated GitHub ecosystem tree SVG generator"
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--token", default=None)
    parser.add_argument("--mode", choices=["initial", "update"], default="update")
    parser.add_argument("--output", default=SVG_FILE)
    parser.add_argument("--state",  default=STATE_FILE)
    return parser.parse_args()


def compute_rolling_avg(activity_scores: dict, days: int = 7) -> float:
    today = datetime.now(timezone.utc).date()
    vals = [activity_scores.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), 0)
            for i in range(days)]
    return sum(vals) / len(vals)


def run_initial(username: str, token: str, args) -> None:
    print(f"[gitwood] Initial run for @{username}")
    state = load_state(args.state)

    print("[gitwood] Fetching lifetime contributions via GraphQL...")
    try:
        gql_data = fetch_graphql(username, token, state)
    except Exception as e:
        print(f"[gitwood] GraphQL fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    total        = gql_data["total_contributions"]
    yearly_cache = gql_data["yearly_cache"]
    print(f"[gitwood] Total contributions: {total}")

    state = update_metadata(state, total, username, yearly_cache)

    print("[gitwood] Building fractal branch structure...")
    branches = build_branches(total)
    state["tree"]["branches"] = branches
    print(f"[gitwood] Generated {len(branches)} branch pixels")

    season  = get_season()
    anchors = collect_leaf_anchors(branches)
    rng     = random.Random(total + 1)
    leaves, _ = evolve_leaves([], anchors, 0, total, rng, season)
    state["tree"]["leaves"] = leaves
    state["tree"]["plants"] = []
    print(f"[gitwood] Placed {len(leaves)} leaves (season: {season})")

    save_state(state, args.state)
    generate_svg(state, args.output)


def run_update(username: str, token: str, args) -> None:
    print(f"[gitwood] Update run for @{username}")
    state = load_state(args.state)
    if not state.get("username"):
        print("[gitwood] No existing state, falling back to initial run.")
        run_initial(username, token, args)
        return

    print("[gitwood] Fetching recent events via REST API...")
    try:
        events = fetch_rest_events(username, token)
    except Exception as e:
        print(f"[gitwood] REST fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    activity_scores = compute_activity_score(events)
    today_score     = get_today_score(events)
    streak          = check_streak(activity_scores)
    rolling_avg     = compute_rolling_avg(activity_scores)
    print(f"[gitwood] Today's score: {today_score}, Streak: {streak} days")

    print("[gitwood] Refreshing total contributions via GraphQL...")
    try:
        gql_data     = fetch_graphql(username, token, state)
        total        = gql_data["total_contributions"]
        yearly_cache = gql_data["yearly_cache"]
    except Exception as e:
        print(f"[gitwood] GraphQL refresh failed (using cached): {e}")
        total        = state.get("total_contributions", 0)
        yearly_cache = state.get("yearly_cache", {})

    old_total = state.get("total_contributions", 0)
    state = update_metadata(state, total, username, yearly_cache)

    if abs(total - old_total) > 10 or not state["tree"]["branches"]:
        print(f"[gitwood] Rebuilding branches ({old_total} → {total})...")
        branches = build_branches(total)
        state["tree"]["branches"] = branches
        print(f"[gitwood] Generated {len(branches)} branch pixels")
    else:
        branches = state["tree"]["branches"]
        print(f"[gitwood] Keeping existing {len(branches)} branch pixels")

    season  = get_season()
    anchors = collect_leaf_anchors(branches)
    rng     = random.Random(total + int(datetime.now(timezone.utc).timestamp()) % 10000)
    leaves, completed_falling = evolve_leaves(
        state["tree"].get("leaves", []), anchors, today_score, total, rng, season
    )
    state["tree"]["leaves"] = leaves
    print(f"[gitwood] Leaves: {len(leaves)} active, {len(completed_falling)} fell | season={season}")

    plants = evolve_plants(state, today_score, rolling_avg, completed_falling, rng)
    state["tree"]["plants"] = plants
    state = update_activity(state, streak, today_score,
                            last_high_day=state["activity"].get("last_high_day"))

    save_state(state, args.state)
    generate_svg(state, args.output)


def main():
    args  = parse_args()
    token = args.token or os.environ.get("GH_TOKEN", "")
    if not token:
        print("Error: No GitHub token. Use --token or set GH_TOKEN.", file=sys.stderr)
        sys.exit(1)

    if args.mode == "initial":
        run_initial(args.username, token, args)
    else:
        run_update(args.username, token, args)


if __name__ == "__main__":
    main()
