import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

def _load_dotenv(path: str='.env') -> None:
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
_load_dotenv()
PIXEL_SIZE = 4
MAX_LEAVES = 300
MAX_PLANTS = 20
GRASS_ROWS = 2
STATE_FILE = 'gitwood.json'
SVG_FILE = 'gitwood.svg'

def compute_canvas_size(total: int, num_plants: int) -> dict:
    base_h = 300
    if total > 2000:
        base_h += 40
    elif total > 500:
        base_h += 20
    if num_plants > 10:
        base_h += 30
    elif num_plants > 5:
        base_h += 15
    height = min(base_h, 450)
    width = 400
    grass_y = height - 24
    trunk_base_y = grass_y - 4
    return {'w': width, 'h': height, 'trunk_base_x': width // 2, 'trunk_base_y': trunk_base_y, 'grass_y': grass_y}
COLORS = {'fruit': '#cc3333', 'fruit_ripe': '#ff6633', 'sun': '#f5d060', 'moon': '#d4cfb0'}
TIME_PALETTES = {'dawn': {'sky': '#1a1035', 'trunk': '#7a4a2a', 'branch_mid': '#5a3418', 'branch_tip': '#4a2d15', 'root': '#5a3418', 'grass_dark': '#3a5a30', 'grass_light': '#5a7e50', 'star': '#ffffff'}, 'day': {'sky': '#87CEEB', 'trunk': '#8B4513', 'branch_mid': '#6B3410', 'branch_tip': '#5a2d0c', 'root': '#6B3410', 'grass_dark': '#2d8a27', 'grass_light': '#4a9c59', 'star': '#ffffff'}, 'dusk': {'sky': '#2d1b4e', 'trunk': '#6a3a1a', 'branch_mid': '#5a2e10', 'branch_tip': '#4a250a', 'root': '#5a2e10', 'grass_dark': '#4a5a30', 'grass_light': '#6a7a40', 'star': '#ffffff'}, 'night': {'sky': '#0d1117', 'trunk': '#5a3010', 'branch_mid': '#4a2a0e', 'branch_tip': '#3a200a', 'root': '#4a2a0e', 'grass_dark': '#1d3a17', 'grass_light': '#2a4c39', 'star': '#ffffff'}}
SEASON_LEAF_COLORS = {'spring': ['#6aad5e', '#8bc34a', '#a5d6a7', '#c8e6c9'], 'summer': ['#2d5a27', '#4a7c59', '#6aad5e', '#8bc34a'], 'autumn': ['#c84b11', '#d4620a', '#e8941a', '#f0c040'], 'winter': ['#7a6652', '#8d7b6a', '#5a7a65', '#4a7c59']}
GRASS_SEASON_COLORS = {'spring': ('#3a7a30', '#5a9e50'), 'summer': ('#2d5a27', '#4a7c59'), 'autumn': ('#6b7a30', '#8a9a40'), 'winter': ('#4a5a48', '#5a6a58')}
EVENT_WEIGHTS = {'PushEvent': 2, 'PullRequestEvent': 5, 'IssuesEvent': 3, 'CreateEvent': 2, 'WatchEvent': 1, 'ForkEvent': 1, 'ReleaseEvent': 4, 'PullRequestReviewEvent': 3, 'IssueCommentEvent': 1, 'CommitCommentEvent': 1}
STREAK_MILESTONES = {7, 30, 100, 365}
FRUIT_MILESTONES = [100, 500, 1000, 2000, 5000]

def get_season(tz=None) -> str:
    month = datetime.now(tz or timezone.utc).month
    if month in (3, 4, 5):
        return 'spring'
    elif month in (6, 7, 8):
        return 'summer'
    elif month in (9, 10, 11):
        return 'autumn'
    return 'winter'

def get_time_of_day(tz=None) -> str:
    hour = datetime.now(tz or timezone.utc).hour
    if 6 <= hour < 8:
        return 'dawn'
    elif 8 <= hour < 18:
        return 'day'
    elif 18 <= hour < 20:
        return 'dusk'
    return 'night'

def is_nighttime(tz=None) -> bool:
    return get_time_of_day(tz) == 'night'

def fruit_count(total: int) -> int:
    return sum((1 for m in FRUIT_MILESTONES if total >= m))
GRAPHQL_URL = 'https://api.github.com/graphql'
REST_URL = 'https://api.github.com'
ACCOUNT_QUERY = '\nquery($login: String!) {\n  user(login: $login) {\n    createdAt\n  }\n}\n'
YEARLY_QUERY = '\nquery($login: String!, $from: DateTime!, $to: DateTime!) {\n  user(login: $login) {\n    contributionsCollection(from: $from, to: $to) {\n      contributionCalendar {\n        totalContributions\n        weeks {\n          contributionDays {\n            contributionCount\n            date\n          }\n        }\n      }\n    }\n  }\n}\n'

def _graphql_post(payload: dict, headers: dict) -> dict:
    resp = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if 'errors' in data:
        raise ValueError(f"GraphQL errors: {data['errors']}")
    return data

def fetch_graphql(username: str, token: str, state: dict) -> dict:
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    yearly_cache = state.get('yearly_cache', {})
    current_year = datetime.now(timezone.utc).year
    data = _graphql_post({'query': ACCOUNT_QUERY, 'variables': {'login': username}}, headers)
    created_at = data['data']['user']['createdAt']
    created_year = int(created_at[:4])
    print(f'[gitwood] Account created {created_year}, scanning {created_year}–{current_year}...')
    total = 0
    all_days = {}
    for year in range(created_year, current_year + 1):
        year_str = str(year)
        if year < current_year and year_str in yearly_cache:
            year_total = yearly_cache[year_str]
            print(f'[gitwood]   {year}: {year_total} contributions (cached)')
            total += year_total
            continue
        from_dt = f'{year}-01-01T00:00:00Z'
        to_dt = f'{year}-12-31T23:59:59Z'
        data = _graphql_post({'query': YEARLY_QUERY, 'variables': {'login': username, 'from': from_dt, 'to': to_dt}}, headers)
        calendar = data['data']['user']['contributionsCollection']['contributionCalendar']
        year_total = calendar['totalContributions']
        total += year_total
        print(f'[gitwood]   {year}: {year_total} contributions')
        if year < current_year:
            yearly_cache[year_str] = year_total
        for week in calendar['weeks']:
            for day in week['contributionDays']:
                if day['contributionCount'] > 0:
                    all_days[day['date']] = day['contributionCount']
    print(f'[gitwood] Lifetime total: {total} contributions')
    return {'total_contributions': total, 'days': all_days, 'yearly_cache': yearly_cache}

def fetch_rest_events(username: str, token: str, pages: int=3) -> list:
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    events = []
    for page in range(1, pages + 1):
        url = f'{REST_URL}/users/{username}/events'
        resp = requests.get(url, headers=headers, params={'per_page': 100, 'page': page}, timeout=30)
        if resp.status_code in (401, 403):
            print('Error: GH_TOKEN not set or invalid. Set it as a repository secret named GH_TOKEN.', file=sys.stderr)
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
    scores = defaultdict(int)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for event in events:
        created = event.get('created_at', '')
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
        except ValueError:
            continue
        if dt < cutoff:
            continue
        date_str = dt.strftime('%Y-%m-%d')
        weight = EVENT_WEIGHTS.get(event.get('type', ''), 0)
        if event.get('type') == 'PushEvent':
            commits = len(event.get('payload', {}).get('commits', []))
            weight = max(weight, commits)
        scores[date_str] += weight
    return dict(scores)

def get_today_score(events: list) -> int:
    scores = compute_activity_score(events)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return scores.get(today, 0)

def check_streak(activity: dict) -> int:
    streak = 0
    day = datetime.now(timezone.utc).date()
    while True:
        if activity.get(day.strftime('%Y-%m-%d'), 0) > 0:
            streak += 1
            day -= timedelta(days=1)
        else:
            break
    return streak

def empty_state(username: str='') -> dict:
    return {'username': username, 'total_contributions': 0, 'last_update': datetime.now(timezone.utc).isoformat(), 'timezone': 'UTC', 'yearly_cache': {}, 'contribution_days': {}, 'tree': {'branches': [], 'leaves': [], 'plants': []}, 'activity': {'streak': 0, 'recent_score': 0, 'last_high_day': None}}

def load_state(path: str=STATE_FILE) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        base = empty_state(state.get('username', ''))
        for k, v in base.items():
            if k not in state:
                state[k] = v
        for k, v in base['tree'].items():
            if k not in state['tree']:
                state['tree'][k] = v
        for k, v in base['activity'].items():
            if k not in state['activity']:
                state['activity'][k] = v
        now_iso = datetime.now(timezone.utc).isoformat()
        for leaf in state.get('tree', {}).get('leaves', []):
            leaf.setdefault('attached_at', now_iso)
            leaf.setdefault('lifespan_days', random.randint(14, 28))
        for plant in state.get('tree', {}).get('plants', []):
            plant.setdefault('born_at', now_iso)
            plant.setdefault('growth_stage', 'mature')
            plant.setdefault('kind', 'ground_plant')
        return state
    except (json.JSONDecodeError, KeyError):
        return empty_state()

def save_state(state: dict, path: str=STATE_FILE) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)

def update_metadata(state: dict, total: int, username: str, yearly_cache: dict) -> dict:
    state['total_contributions'] = total
    state['last_update'] = datetime.now(timezone.utc).isoformat()
    state['username'] = username
    state['yearly_cache'] = yearly_cache
    return state

def update_activity(state: dict, streak: int, today_score: int, last_high_day) -> dict:
    state['activity']['streak'] = streak
    state['activity']['recent_score'] = today_score
    if last_high_day is not None:
        state['activity']['last_high_day'] = last_high_day
    return state
HEIGHT_CAP_CONTRIBUTIONS = 2000

def compute_scale_params(total: int) -> dict:
    t = max(total, 0)
    capped_t = min(t, HEIGHT_CAP_CONTRIBUTIONS)
    max_depth = min(8, max(2, int(math.log(capped_t + 1, 1.8))))
    initial_length = min(90, max(20, int(math.log(capped_t + 1) * 9)))
    trunk_thickness = min(4, max(1, int(math.log(t + 1, 10) * 1.2)))
    branch_thickness = {'trunk': trunk_thickness, 'mid': max(1, trunk_thickness - 1), 'tip': 1}
    leaf_scale = 1 if t < 1000 else 2
    return {'max_depth': max_depth, 'initial_length': initial_length, 'trunk_thickness': trunk_thickness, 'branch_thickness': branch_thickness, 'leaf_scale': leaf_scale}

def snap(value: float) -> int:
    return int(round(value / PIXEL_SIZE) * PIXEL_SIZE)

def rasterize_segment(x1, y1, x2, y2, depth, angle, canvas) -> list:
    x1s, y1s = (snap(x1), snap(y1))
    x2s, y2s = (snap(x2), snap(y2))
    dx = x2s - x1s
    dy = y2s - y1s
    steps = max(abs(dx), abs(dy)) // PIXEL_SIZE
    if steps == 0:
        if 0 <= x1s < canvas['w'] and 0 <= y1s < canvas['h']:
            return [{'x': x1s, 'y': y1s, 'w': PIXEL_SIZE, 'h': PIXEL_SIZE, 'depth': depth, 'angle': angle}]
        return []
    pixels = []
    seen = set()
    for i in range(steps + 1):
        t = i / steps
        px = snap(x1s + t * dx)
        py = snap(y1s + t * dy)
        if not (0 <= px < canvas['w'] and 0 <= py < canvas['h']):
            continue
        key = (px, py)
        if key not in seen:
            seen.add(key)
            pixels.append({'x': px, 'y': py, 'w': PIXEL_SIZE, 'h': PIXEL_SIZE, 'depth': depth, 'angle': angle})
    return pixels

def generate_branches(x, y, angle, length, depth, max_depth, branches, rng, canvas):
    if depth > max_depth or length < PIXEL_SIZE:
        return
    x2 = x + math.cos(math.radians(angle)) * length
    y2 = y - math.sin(math.radians(angle)) * length
    branches.extend(rasterize_segment(x, y, x2, y2, depth, angle, canvas))
    spread = 25 + rng.randint(-10, 10)
    generate_branches(x2, y2, angle + spread, length * 0.7, depth + 1, max_depth, branches, rng, canvas)
    generate_branches(x2, y2, angle - spread, length * 0.7, depth + 1, max_depth, branches, rng, canvas)
    if depth < 2:
        generate_branches(x2, y2, angle + rng.randint(-5, 5), length * 0.6, depth + 1, max_depth, branches, rng, canvas)

def build_branches(total: int, canvas: dict) -> list:
    params = compute_scale_params(total)
    rng = random.Random(total)
    branches = []
    generate_branches(float(canvas['trunk_base_x']), float(canvas['trunk_base_y']), 90.0, float(params['initial_length']), 0, params['max_depth'], branches, rng, canvas)
    return branches

def collect_leaf_anchors(branches: list) -> list:
    if not branches:
        return []
    anchors = [(b['x'], b['y']) for b in branches if b['depth'] >= 2]
    return list(dict.fromkeys(anchors))

def compute_leaf_target(total: int, season: str) -> int:
    base = min(MAX_LEAVES, max(5, total // 10))
    if season == 'winter':
        return max(3, base // 3)
    elif season == 'autumn':
        return max(5, base // 2)
    return base

def make_leaf(x: int, y: int, state: str, rng: random.Random, season: str='summer') -> dict:
    now = datetime.now(timezone.utc)
    if season == 'autumn':
        lifespan = rng.randint(7, 14)
    elif season == 'winter':
        lifespan = rng.randint(5, 10)
    elif season == 'spring':
        lifespan = rng.randint(18, 30)
    else:
        lifespan = rng.randint(14, 28)
    return {'x': x, 'y': y, 'state': state, 'color_idx': rng.randint(0, 3), 'delay': round(rng.uniform(0.0, 5.0), 2), 'duration': round(rng.uniform(2.5, 4.5), 2), 'drift': rng.randint(-30, 30), 'fell_at': None, 'season': season, 'attached_at': now.isoformat(), 'lifespan_days': lifespan}

def evolve_leaves(existing, anchors, today_score, total, rng, season='summer'):
    anchor_set = set(anchors)
    now = datetime.now(timezone.utc)
    target = compute_leaf_target(total, season)
    completed_falling = []
    updated = []
    for leaf in existing:
        pos = (leaf['x'], leaf['y'])
        if pos not in anchor_set and leaf['state'] == 'attached':
            continue
        if leaf['state'] == 'falling':
            fell_at = leaf.get('fell_at')
            if fell_at:
                try:
                    fell_dt = datetime.fromisoformat(fell_at)
                    if fell_dt.tzinfo is None:
                        fell_dt = fell_dt.replace(tzinfo=timezone.utc)
                    if (now - fell_dt).total_seconds() > 3 * leaf['duration']:
                        completed_falling.append(leaf)
                        continue
                except ValueError:
                    pass
        if leaf['state'] == 'attached':
            attached_at = leaf.get('attached_at')
            if attached_at:
                try:
                    att_dt = datetime.fromisoformat(attached_at)
                    if att_dt.tzinfo is None:
                        att_dt = att_dt.replace(tzinfo=timezone.utc)
                    age_days = (now - att_dt).total_seconds() / 86400
                    lifespan = leaf.get('lifespan_days', 21)
                    if age_days >= lifespan:
                        leaf = dict(leaf)
                        leaf['state'] = 'falling'
                        leaf['fell_at'] = now.isoformat()
                        updated.append(leaf)
                        continue
                    elif age_days >= lifespan * 0.8:
                        if rng.random() < 0.15:
                            leaf = dict(leaf)
                            leaf['state'] = 'falling'
                            leaf['fell_at'] = now.isoformat()
                            updated.append(leaf)
                            continue
                except ValueError:
                    pass
            fall_chance = 0.08 if season == 'autumn' else 0.15 if season == 'winter' else 0.05
            if today_score == 0 and rng.random() < fall_chance:
                leaf = dict(leaf)
                leaf['state'] = 'falling'
                leaf['fell_at'] = now.isoformat()
            elif today_score < 3 and rng.random() < 0.02:
                leaf = dict(leaf)
                leaf['state'] = 'falling'
                leaf['fell_at'] = now.isoformat()
        if leaf['state'] == 'regrowing':
            leaf = dict(leaf)
            leaf['state'] = 'attached'
        updated.append(leaf)
    occupied = {(l['x'], l['y']) for l in updated}
    free_anchors = [a for a in anchors if a not in occupied]
    rng.shuffle(free_anchors)
    deficit = target - len(updated)
    for i in range(min(deficit, len(free_anchors))):
        ax, ay = free_anchors[i]
        updated.append(make_leaf(ax, ay, 'regrowing', rng, season))
    if len(updated) > MAX_LEAVES:
        updated = updated[:MAX_LEAVES]
    return (updated, completed_falling)
PLANT_KINDS = {'ground_plant': ['sprout', 'fern', 'mushroom', 'shrub'], 'flower': ['flower_purple', 'flower_pink', 'flower_blue', 'flower_lavender', 'flower_orange', 'flower_yellow'], 'tree_seedling': ['seedling']}
GROWTH_STAGES = {'ground_plant': ['sprout', 'growing', 'mature'], 'flower': ['bud', 'blooming', 'full_bloom', 'wilting'], 'tree_seedling': ['seed', 'sprout', 'sapling', 'young_tree']}
STAGE_DURATIONS = {'ground_plant': {'sprout': 3, 'growing': 7, 'mature': 999}, 'flower': {'bud': 2, 'blooming': 5, 'full_bloom': 14, 'wilting': 3}, 'tree_seedling': {'seed': 3, 'sprout': 7, 'sapling': 21, 'young_tree': 999}}
LEAF_SPAWN_CHANCES = {'flower': 0.12, 'ground_plant': 0.05, 'tree_seedling': 0.02}
PLANT_PIXEL_SHAPES = {'sprout': [(0, 0), (0, -4), (0, -8), (-4, -8), (4, -8)], 'flower_purple': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (0, -16), (-4, -8), (4, -8)], 'flower_pink': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (-8, -8), (8, -8), (0, -16)], 'flower_blue': [(0, 0), (0, -4), (-4, -8), (0, -8), (4, -8), (0, -12)], 'flower_lavender': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (-4, -16), (0, -16), (4, -16)], 'flower_yellow': [(0, 0), (0, -4), (0, -8), (-4, -8), (4, -8), (-4, -12), (0, -12), (4, -12)], 'flower_orange': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (-4, -8), (4, -8), (0, -16)], 'shrub': [(-8, 0), (-4, 0), (0, 0), (4, 0), (8, 0), (-4, -4), (0, -4), (4, -4), (0, -8), (-8, -4), (8, -4)], 'mushroom': [(-4, -8), (0, -8), (4, -8), (-8, -4), (-4, -4), (0, -4), (4, -4), (8, -4), (-4, 0), (4, 0), (0, 4)], 'fern': [(0, 0), (0, -4), (0, -8), (0, -12), (-4, -4), (-8, -8), (4, -4), (8, -8), (4, -12), (-4, -12)], 'seedling_seed': [(0, 0)], 'seedling_sprout': [(0, 0), (0, -4), (-4, -4), (4, -4)], 'seedling_sapling': [(0, 0), (0, -4), (0, -8), (0, -12), (-4, -8), (4, -8), (-4, -4), (4, -4), (-4, -12), (4, -12)], 'seedling_young_tree': [(0, 4), (0, 0), (0, -4), (0, -8), (0, -12), (0, -16), (-4, -8), (4, -8), (-8, -8), (8, -8), (-4, -12), (4, -12), (-8, -12), (8, -12), (-4, -16), (4, -16), (0, -20), (-4, -20), (4, -20)]}
PLANT_COLORS = {'sprout': '#6aad5e', 'shrub': '#4a7c59', 'mushroom': '#cc7755', 'fern': '#2d5a27', 'flower_purple': '#9b59b6', 'flower_pink': '#e91e8c', 'flower_blue': '#3498db', 'flower_lavender': '#b39ddb', 'flower_yellow': '#f1c40f', 'flower_orange': '#ff9966', 'seedling_seed': '#8B4513', 'seedling_sprout': '#6aad5e', 'seedling_sapling': '#5a8a4a', 'seedling_young_tree': '#4a7c39'}

def check_spawn_triggers(state, today_score, rolling_avg):
    triggers = []
    if len(state['tree']['plants']) >= MAX_PLANTS:
        return triggers
    if today_score > 15:
        triggers.append('high_day')
    streak = state['activity'].get('streak', 0)
    for milestone in STREAK_MILESTONES:
        if streak == milestone:
            trigger_name = f'streak_{milestone}'
            if state['activity'].get('last_high_day') != trigger_name:
                triggers.append(trigger_name)
    if rolling_avg > 2 and today_score > 1.5 * rolling_avg:
        triggers.append('spike')
    return triggers

def maybe_spawn_from_leaf(leaf, plants, rng, canvas):
    if len(plants) >= MAX_PLANTS:
        return None
    for kind, chance in LEAF_SPAWN_CHANCES.items():
        if rng.random() < chance:
            if kind == 'flower':
                plant_type = rng.choice(PLANT_KINDS['flower'])
            elif kind == 'ground_plant':
                plant_type = rng.choice(PLANT_KINDS['ground_plant'])
            else:
                plant_type = 'seedling'
            leaf_x = leaf['x'] + leaf.get('drift', 0) // 3
            spawn_x = max(20, min(canvas['w'] - 20, leaf_x))
            if any((abs(spawn_x - p['x']) < 16 for p in plants)):
                return None
            return {'x': spawn_x, 'y': canvas['grass_y'], 'type': plant_type, 'kind': kind, 'growth_stage': GROWTH_STAGES[kind][0], 'born_at': datetime.now(timezone.utc).isoformat(), 'size': 1}
    return None

def evolve_plant_growth(plants: list, rng) -> list:
    now = datetime.now(timezone.utc)
    updated = []
    for plant in plants:
        kind = plant.get('kind', 'ground_plant')
        stage = plant.get('growth_stage', 'mature')
        born_at = plant.get('born_at')
        if not born_at:
            updated.append(plant)
            continue
        try:
            born_dt = datetime.fromisoformat(born_at)
            if born_dt.tzinfo is None:
                born_dt = born_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            updated.append(plant)
            continue
        age_days = (now - born_dt).total_seconds() / 86400
        stages = GROWTH_STAGES.get(kind, ['mature'])
        durations = STAGE_DURATIONS.get(kind, {})
        cumulative_days = 0
        target_stage = stages[0]
        for s in stages:
            dur = durations.get(s, 999)
            cumulative_days += dur
            if age_days < cumulative_days:
                target_stage = s
                break
            target_stage = s
        if kind == 'flower' and target_stage == 'wilting':
            total_flower_life = sum((durations.get(s, 0) for s in stages))
            if age_days > total_flower_life:
                continue
        plant = dict(plant)
        plant['growth_stage'] = target_stage
        if kind == 'tree_seedling':
            plant['type'] = f'seedling_{target_stage}'
        elif kind == 'flower':
            pass
        updated.append(plant)
    return updated

def spawn_plant(x, plant_type, canvas):
    return {'x': x, 'y': canvas['grass_y'], 'type': plant_type, 'kind': 'ground_plant', 'growth_stage': GROWTH_STAGES['ground_plant'][0], 'born_at': datetime.now(timezone.utc).isoformat(), 'size': 1}

def evolve_plants(state, today_score, rolling_avg, completed_falling, rng, canvas):
    plants = list(state['tree']['plants'])
    plants = evolve_plant_growth(plants, rng)
    triggers = check_spawn_triggers(state, today_score, rolling_avg)
    for trigger in triggers:
        if len(plants) >= MAX_PLANTS:
            break
        plant_type = rng.choice(list(PLANT_KINDS['ground_plant']))
        existing_xs = {p['x'] for p in plants}
        for _ in range(20):
            x = rng.randint(20, canvas['w'] - 20)
            if all((abs(x - ex) > 16 for ex in existing_xs)):
                break
        plants.append(spawn_plant(x, plant_type, canvas))
        if trigger.startswith('streak_'):
            state['activity']['last_high_day'] = trigger
    for leaf in completed_falling:
        if len(plants) >= MAX_PLANTS:
            break
        new_plant = maybe_spawn_from_leaf(leaf, plants, rng, canvas)
        if new_plant:
            plants.append(new_plant)
    return plants

def seed_initial_ecosystem(total: int, rng, canvas) -> list:
    plants = []
    if total < 50:
        target_plants = 1
        flower_ratio = 0.5
        tree_chance = 0.0
    elif total < 200:
        target_plants = 3
        flower_ratio = 0.5
        tree_chance = 0.0
    elif total < 500:
        target_plants = 5
        flower_ratio = 0.4
        tree_chance = 0.05
    elif total < 1000:
        target_plants = 8
        flower_ratio = 0.4
        tree_chance = 0.1
    elif total < 2000:
        target_plants = 12
        flower_ratio = 0.35
        tree_chance = 0.15
    else:
        target_plants = 15
        flower_ratio = 0.3
        tree_chance = 0.2
    eco_rng = random.Random(total * 31 + 7)
    now = datetime.now(timezone.utc)
    occupied_xs = set()
    for i in range(target_plants):
        for _ in range(30):
            x = eco_rng.randint(20, canvas['w'] - 20)
            if all((abs(x - ox) > 16 for ox in occupied_xs)):
                break
        occupied_xs.add(x)
        roll = eco_rng.random()
        if roll < tree_chance and total >= 500:
            if total >= 2000:
                stage = 'young_tree'
                plant_type = 'seedling_young_tree'
            elif total >= 1000:
                stage = 'sapling'
                plant_type = 'seedling_sapling'
            else:
                stage = 'sprout'
                plant_type = 'seedling_sprout'
            kind = 'tree_seedling'
            age_days = eco_rng.randint(30, 120)
            born_at = (now - timedelta(days=age_days)).isoformat()
        elif roll < tree_chance + flower_ratio:
            flower_types = ['flower_purple', 'flower_pink', 'flower_blue', 'flower_lavender', 'flower_orange', 'flower_yellow']
            plant_type = eco_rng.choice(flower_types)
            kind = 'flower'
            stage = 'full_bloom'
            age_days = eco_rng.randint(5, 15)
            born_at = (now - timedelta(days=age_days)).isoformat()
        else:
            ground_types = ['sprout', 'fern', 'mushroom', 'shrub']
            plant_type = eco_rng.choice(ground_types)
            kind = 'ground_plant'
            stage = 'mature'
            age_days = eco_rng.randint(10, 60)
            born_at = (now - timedelta(days=age_days)).isoformat()
        plants.append({'x': x, 'y': canvas['grass_y'], 'type': plant_type, 'kind': kind, 'growth_stage': stage, 'born_at': born_at, 'size': 1})
    return plants

def _rect(x, y, w, h, fill, extra=''):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"{extra}/>'

def _use(href, x, y, extra=''):
    return f'<use href="#{href}" x="{x}" y="{y}"{extra}/>'

def _wind_animate(cx, cy, amplitude, duration, begin):
    a = amplitude
    vals = f'0 {cx} {cy};{-a} {cx} {cy};{a} {cx} {cy};{-a} {cx} {cy};0 {cx} {cy}'
    spline = '0.45 0 0.55 1'
    splines = ';'.join([spline] * 4)
    return f'<animateTransform attributeName="transform" type="rotate" values="{vals}" keyTimes="0;0.25;0.5;0.75;1" keySplines="{splines}" calcMode="spline" dur="{duration}s" begin="{begin}s" repeatCount="indefinite"/>'

def _fall_animate(from_x, from_y, drift, drop, duration, begin):
    return f'<animateTransform attributeName="transform" type="translate" from="{from_x} {from_y}" to="{from_x + drift} {from_y + drop}" dur="{duration}s" begin="{begin}s" repeatCount="indefinite" calcMode="spline" keySplines="0.25 0 0.75 1"/>'

def _opacity_animate(from_val, to_val, duration, begin, fill='remove'):
    return f'<animate attributeName="opacity" from="{from_val}" to="{to_val}" dur="{duration}s" begin="{begin}s" fill="{fill}" repeatCount="indefinite"/>'

def build_defs(leaf_colors: list) -> str:
    lines = ['<defs>']
    lines.append(f'  <rect id="px" width="{PIXEL_SIZE}" height="{PIXEL_SIZE}"/>')
    offsets = [(0, -PIXEL_SIZE), (-PIXEL_SIZE, 0), (0, 0), (PIXEL_SIZE, 0), (0, PIXEL_SIZE)]
    for i, color in enumerate(leaf_colors):
        lines.append(f'  <g id="leaf-{i}">')
        for ox, oy in offsets:
            lines.append(f'    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, color)}')
        lines.append('  </g>')
    for ptype, shape in PLANT_PIXEL_SHAPES.items():
        color = PLANT_COLORS.get(ptype, '#4a7c59')
        lines.append(f'  <g id="plant-{ptype}">')
        for ox, oy in shape:
            lines.append(f'    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, color)}')
        lines.append('  </g>')
    fruit_pixels = [(-4, -4), (0, -4), (4, -4), (-4, 0), (0, 0), (4, 0), (0, 4)]
    lines.append(f'  <g id="fruit">')
    for ox, oy in fruit_pixels:
        lines.append(f"    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, COLORS['fruit'])}")
    lines.append('  </g>')
    lines.append('</defs>')
    return '\n'.join(lines)

def render_sky(tod: str, palette: dict, canvas: dict) -> str:
    bg = palette['sky']
    lines = [f'<!-- Sky ({tod}) -->', _rect(0, 0, canvas['w'], canvas['h'], bg)]
    if tod == 'night':
        mx, my = (340, 30)
        moon_pixels = [(0, 0), (4, 0), (8, 0), (0, 4), (0, 8), (4, 8)]
        for ox, oy in moon_pixels:
            lines.append(_rect(mx + ox, my + oy, PIXEL_SIZE, PIXEL_SIZE, COLORS['moon']))
    elif tod == 'day':
        sx, sy = (28, 20)
        sun_pixels = [(0, -8), (0, -4), (0, 0), (0, 4), (0, 8), (-8, 0), (-4, 0), (4, 0), (8, 0), (-4, -4), (4, -4), (-4, 4), (4, 4)]
        for ox, oy in sun_pixels:
            lines.append(_rect(sx + ox, sy + oy, PIXEL_SIZE, PIXEL_SIZE, COLORS['sun']))
    elif tod == 'dawn':
        for x in range(0, canvas['w'], PIXEL_SIZE):
            lines.append(_rect(x, canvas['grass_y'] - PIXEL_SIZE * 3, PIXEL_SIZE, PIXEL_SIZE, '#3a1f5c', f' opacity="0.4"'))
    elif tod == 'dusk':
        for x in range(0, canvas['w'], PIXEL_SIZE):
            lines.append(_rect(x, canvas['grass_y'] - PIXEL_SIZE * 3, PIXEL_SIZE, PIXEL_SIZE, '#8b2500', f' opacity="0.4"'))
    return '\n'.join(lines)

def render_weather(tod: str, season: str, today_score: int, rng, canvas: dict) -> str:
    lines = ['<!-- Weather -->']
    if today_score == 0 and season != 'winter':
        for i in range(15):
            x = rng.randint(0, canvas['w'])
            y = rng.randint(0, canvas['h'] - 40)
            delay = round(rng.uniform(0, 2), 2)
            dur = round(rng.uniform(0.4, 0.8), 2)
            lines.append(f'<line x1="{x}" y1="{y}" x2="{x - 2}" y2="{y + 8}" stroke="#6688aa" stroke-width="1" opacity="0.4">')
            ch = canvas['h']
            lines.append(f'  <animateTransform attributeName="transform" type="translate" from="0 0" to="4 {ch}" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append(f'  <animate attributeName="opacity" values="0.4;0.2;0.4" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append('</line>')
    if season == 'winter':
        count = 12 if today_score == 0 else 6
        for i in range(count):
            x = rng.randint(0, canvas['w'])
            y = rng.randint(-20, 0)
            delay = round(rng.uniform(0, 4), 2)
            dur = round(rng.uniform(3, 6), 2)
            drift = rng.randint(-20, 20)
            lines.append(f'<rect x="{x}" y="{y}" width="2" height="2" fill="#ffffff" opacity="0.6">')
            ch = canvas['h']
            lines.append(f'  <animateTransform attributeName="transform" type="translate" from="0 0" to="{drift} {ch + 20}" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append(f'  <animate attributeName="opacity" values="0.6;0.3;0.6;0" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append('</rect>')
    if tod == 'night' and season in ('spring', 'summer'):
        for i in range(8):
            cx = rng.randint(40, canvas['w'] - 40)
            cy = rng.randint(100, canvas['grass_y'] - 20)
            delay = round(rng.uniform(0, 5), 2)
            dur = round(rng.uniform(2, 4), 2)
            drift_x = rng.randint(-15, 15)
            drift_y = rng.randint(-10, 10)
            lines.append(f'<circle cx="{cx}" cy="{cy}" r="1.5" fill="#ffff66" opacity="0">')
            lines.append(f'  <animate attributeName="opacity" values="0;0.8;0.9;0.3;0" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append(f'  <animateTransform attributeName="transform" type="translate" from="0 0" to="{drift_x} {drift_y}" dur="{dur * 2}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append('</circle>')
    if tod in ('day', 'dawn', 'dusk'):
        cloud_count = 2 if tod == 'day' else 1
        for i in range(cloud_count):
            cy = rng.randint(15, 50)
            delay = round(rng.uniform(0, 10), 2)
            dur = round(rng.uniform(30, 60), 1)
            cloud_color = '#ffffff' if tod == 'day' else '#8888aa'
            opacity = '0.3' if tod == 'day' else '0.2'
            lines.append(f'<g opacity="{opacity}">')
            lines.append(f'  <rect x="0" y="{cy}" width="12" height="4" fill="{cloud_color}"/>')
            lines.append(f'  <rect x="4" y="{cy - 4}" width="8" height="4" fill="{cloud_color}"/>')
            lines.append(f'  <rect x="-4" y="{cy}" width="4" height="4" fill="{cloud_color}"/>')
            cw = canvas['w']
            lines.append(f'  <animateTransform attributeName="transform" type="translate" from="-20 0" to="{cw + 20} 0" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>')
            lines.append('</g>')
    return '\n'.join(lines)

def render_stars(rng: random.Random, tod: str, canvas: dict) -> str:
    if tod not in ('night', 'dawn'):
        return '<!-- No stars (daytime) -->'
    lines = ['<!-- Stars -->']
    count = 20 if tod == 'night' else 8
    lines.append('<g>')
    lines.append('<animateTransform attributeName="transform" type="translate" values="0 0;3 0;0 0;-3 0;0 0" dur="60s" repeatCount="indefinite"/>')
    for _ in range(count):
        x = rng.randint(4, canvas['w'] - 4)
        y = rng.randint(4, 60)
        opacity = round(rng.uniform(0.3, 0.8), 1)
        lines.append(_rect(x, y, 2, 2, '#ffffff', f' opacity="{opacity}"'))
    lines.append('</g>')
    return '\n'.join(lines)

def render_ground(season: str, contribution_days: dict, canvas: dict) -> str:
    lines = [f'<!-- Ground ({season}) -->']
    today = datetime.now(timezone.utc).date()
    heatmap_data = []
    for i in range(364):
        day = today - timedelta(days=363 - i)
        count = contribution_days.get(day.strftime('%Y-%m-%d'), 0)
        heatmap_data.append(count)
    max_count = max(heatmap_data) if heatmap_data and max(heatmap_data) > 0 else 1

    def heatmap_color(count, season):
        if count == 0:
            if season == 'winter':
                return '#1a2a1a'
            return '#1a2a17'
        intensity = min(count / max_count, 1.0)
        if season == 'autumn':
            r = int(100 + 155 * intensity)
            g = int(80 + 80 * intensity)
            b = int(20 + 10 * intensity)
        elif season == 'winter':
            r = int(40 + 100 * intensity)
            g = int(60 + 120 * intensity)
            b = int(80 + 140 * intensity)
        else:
            r = int(20 + 30 * intensity)
            g = int(60 + 140 * intensity)
            b = int(20 + 30 * intensity)
        return f'#{r:02x}{g:02x}{b:02x}'
    cols = canvas['w'] // PIXEL_SIZE
    days_per_col = len(heatmap_data) / cols
    for col in range(cols):
        x = col * PIXEL_SIZE
        start_idx = int(col * days_per_col)
        end_idx = int((col + 1) * days_per_col)
        chunk = heatmap_data[start_idx:end_idx]
        avg_count = sum(chunk) / len(chunk) if chunk else 0
        color = heatmap_color(avg_count, season)
        lines.append(_rect(x, canvas['grass_y'], PIXEL_SIZE, PIXEL_SIZE, color))
    dark, light = GRASS_SEASON_COLORS.get(season, ('#2d5a27', '#4a7c59'))
    for col in range(cols):
        x = col * PIXEL_SIZE
        color = dark if col % 2 == 0 else light
        lines.append(_rect(x, canvas['grass_y'] + PIXEL_SIZE, PIXEL_SIZE, PIXEL_SIZE, color))
    soil_y = canvas['grass_y'] + PIXEL_SIZE * 2
    soil_h = canvas['h'] - soil_y
    if soil_h > 0:
        soil_color = '#2e1b12' if season == 'winter' else '#3e2723'
        lines.append(_rect(0, soil_y, canvas['w'], soil_h, soil_color))
    return '\n'.join(lines)

def render_roots(total: int, palette: dict, canvas: dict) -> str:
    if total < 10:
        return '<!-- No roots yet -->'
    lines = ['<!-- Roots -->']
    root_depth = min(4, max(1, int(math.log(total + 1, 10) * 2)))
    color = palette['root']
    for i in range(root_depth):
        y = canvas['trunk_base_y'] + GRASS_ROWS * PIXEL_SIZE + i * PIXEL_SIZE
        if y >= canvas['h']:
            break
        lines.append(_rect(canvas['trunk_base_x'], y, PIXEL_SIZE, PIXEL_SIZE, color))
    if total >= 30:
        for i in range(min(root_depth, 3)):
            x = canvas['trunk_base_x'] - (i + 1) * PIXEL_SIZE
            y = canvas['trunk_base_y'] + GRASS_ROWS * PIXEL_SIZE + i * PIXEL_SIZE
            if 0 <= x < canvas['w'] and y < canvas['h']:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
    if total >= 30:
        for i in range(min(root_depth, 3)):
            x = canvas['trunk_base_x'] + (i + 2) * PIXEL_SIZE
            y = canvas['trunk_base_y'] + GRASS_ROWS * PIXEL_SIZE + i * PIXEL_SIZE
            if 0 <= x < canvas['w'] and y < canvas['h']:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
    if total >= 200:
        for i in range(2):
            x = canvas['trunk_base_x'] - (root_depth + i) * PIXEL_SIZE
            y = canvas['trunk_base_y'] + GRASS_ROWS * PIXEL_SIZE + root_depth * PIXEL_SIZE
            if 0 <= x < canvas['w'] and y < canvas['h']:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
            x = canvas['trunk_base_x'] + (root_depth + i + 1) * PIXEL_SIZE
            if 0 <= x < canvas['w'] and y < canvas['h']:
                lines.append(_rect(x, y, PIXEL_SIZE, PIXEL_SIZE, color))
    return '\n'.join(lines)

def render_branches(branches: list, scale_params: dict, palette: dict, canvas: dict) -> str:
    if not branches:
        return '<!-- No branches -->'
    thickness = (scale_params or {}).get('branch_thickness', {'trunk': 1, 'mid': 1, 'tip': 1})

    def branch_color(angle):
        if angle > 100:
            return palette.get('branch_light', palette['trunk'])
        elif angle < 80:
            return palette.get('branch_dark', palette['branch_tip'])
        return palette.get('branch_base', palette['branch_mid'])
    trunk_pixels = [b for b in branches if b['depth'] <= 1]
    mid_pixels = [b for b in branches if 2 <= b['depth'] <= 3]
    tip_pixels = [b for b in branches if b['depth'] >= 4]
    lines = ['<!-- Branches -->']
    lines.append('<defs>')
    lines.append(f'''  <rect id="px_trunk" width="{PIXEL_SIZE * thickness['trunk']}" height="{PIXEL_SIZE}"/>''')
    lines.append(f'''  <rect id="px_mid" width="{PIXEL_SIZE * thickness['mid']}" height="{PIXEL_SIZE}"/>''')
    lines.append(f'''  <rect id="px_tip" width="{PIXEL_SIZE * thickness['tip']}" height="{PIXEL_SIZE}"/>''')
    lines.append('</defs>')
    if trunk_pixels:
        lines.append('<g id="wind-trunk">')
        lines.append(_wind_animate(canvas['trunk_base_x'], canvas['trunk_base_y'], 0.3, 10, 0))
        for b in trunk_pixels:
            w = b['w'] * thickness['trunk']
            x_offset = (w - b['w']) // 2
            lines.append(f'''<use href="#px_trunk" x="{b['x'] - x_offset}" y="{b['y']}" fill="{branch_color(b['angle'])}"/>''')
        lines.append('</g>')
    if mid_pixels:
        cy = min((b['y'] for b in mid_pixels))
        lines.append('<g id="wind-mid">')
        lines.append(_wind_animate(canvas['trunk_base_x'], cy, 1.0, 7, 0.3))
        for b in mid_pixels:
            w = b['w'] * thickness['mid']
            x_offset = (w - b['w']) // 2
            lines.append(f'''<use href="#px_mid" x="{b['x'] - x_offset}" y="{b['y']}" fill="{branch_color(b['angle'])}"/>''')
        lines.append('</g>')
    if tip_pixels:
        band_size = canvas['w'] // 4
        bands = __import__('collections').defaultdict(list)
        for b in tip_pixels:
            bands[b['x'] // band_size].append(b)
        for band_idx in sorted(bands.keys()):
            bp = bands[band_idx]
            cx = int(sum((b['x'] for b in bp)) / len(bp))
            cy = int(sum((b['y'] for b in bp)) / len(bp))
            lines.append(f'<g id="wind-tip-{band_idx}">')
            lines.append(_wind_animate(cx, cy, 3.0, 3.5, band_idx * 0.2))
            for b in bp:
                w = b['w'] * thickness['tip']
                x_offset = (w - b['w']) // 2
                lines.append(f'''<use href="#px_tip" x="{b['x'] - x_offset}" y="{b['y']}" fill="{branch_color(b['angle'])}"/>''')
            lines.append('</g>')
    return '\n'.join(lines)

def render_fruit(branches: list, total: int) -> str:
    count = fruit_count(total)
    if count == 0 or not branches:
        return '<!-- No fruit yet -->'
    tips = [b for b in branches if b['depth'] >= 4]
    if not tips:
        return '<!-- No tips for fruit -->'
    rng = random.Random(total * 7)
    rng.shuffle(tips)
    chosen = tips[:count]
    lines = ['<!-- Fruit (milestone orbs) -->']
    for tip in chosen:
        lines.append(_use('fruit', tip['x'], tip['y'] - PIXEL_SIZE * 2))
    return '\n'.join(lines)

def render_leaves(leaves: list, canvas: dict) -> str:
    if not leaves:
        return '<!-- No leaves -->'
    lines = ['<!-- Leaves -->']
    for leaf in leaves:
        x, y = (leaf['x'], leaf['y'])
        color_idx = leaf.get('color_idx', 0)
        delay = leaf.get('delay', 0)
        duration = leaf.get('duration', 3.0)
        drift = leaf.get('drift', 0)
        state = leaf.get('state', 'attached')
        if state == 'attached':
            lines.append('<g>')
            lines.append(_wind_animate(x, y, 4.0, duration * 0.8, delay))
            lines.append(_use(f'leaf-{color_idx}', x, y))
            lines.append('</g>')
        elif state == 'falling':
            drop = max(0, canvas['grass_y'] - y)
            fade_begin = round(delay + duration * 0.8, 2)
            lines.append('<g>')
            lines.append(_fall_animate(x, y, drift, drop, duration, delay))
            lines.append(_opacity_animate(1, 0, round(duration * 0.2, 2), fade_begin))
            lines.append(_use(f'leaf-{color_idx}', 0, 0))
            lines.append('</g>')
        elif state == 'regrowing':
            lines.append('<g>')
            lines.append(f'<animate attributeName="opacity" from="0" to="1" dur="2s" begin="{delay}s" fill="freeze"/>')
            lines.append(_wind_animate(x, y, 4.0, duration * 0.8, delay + 2))
            lines.append(_use(f'leaf-{color_idx}', x, y))
            lines.append('</g>')
    return '\n'.join(lines)

def render_plants(plants: list, canvas: dict) -> str:
    if not plants:
        return '<!-- No plants -->'
    lines = ['<!-- Plants -->']
    for plant in plants:
        ptype = plant.get('type', 'sprout')
        if ptype not in PLANT_PIXEL_SHAPES:
            ptype = 'sprout'
        lines.append(_use(f'plant-{ptype}', plant['x'], canvas['grass_y']))
    return '\n'.join(lines)
CREATURE_UNLOCKS = {'butterfly': {'min_contributions': 100, 'seasons': ['spring', 'summer'], 'time': ['day', 'dawn']}, 'bird': {'min_contributions': 500, 'seasons': None, 'time': ['day', 'dawn', 'dusk']}, 'owl': {'min_contributions': 500, 'seasons': None, 'time': ['night']}, 'squirrel': {'min_contributions': 1000, 'seasons': None, 'time': None}, 'beehive': {'min_contributions': 2000, 'seasons': ['spring', 'summer'], 'time': None}}
CREATURE_PIXEL_SHAPES = {'butterfly': [(-4, -4), (4, -4), (-4, 0), (0, 0), (4, 0), (0, -4)], 'bird': [(-8, 0), (-4, -4), (0, -4), (4, -4), (8, 0), (0, 0)], 'owl': [(-4, -8), (0, -8), (4, -8), (-4, -4), (0, -4), (4, -4), (-8, -4), (8, -4), (-4, 0), (4, 0)], 'squirrel': [(0, 0), (0, -4), (0, -8), (4, -8), (4, -12), (-4, -4)], 'beehive': [(-4, -4), (0, -4), (4, -4), (-8, 0), (-4, 0), (0, 0), (4, 0), (8, 0), (-4, 4), (0, 4), (4, 4), (0, -8)]}
CREATURE_COLORS = {'butterfly': '#e91e8c', 'bird': '#4a6a8a', 'owl': '#8a7a6a', 'squirrel': '#b5651d', 'beehive': '#daa520'}
PIXEL_FONT = {'0': [(0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (2, 2), (0, 3), (2, 3), (0, 4), (1, 4), (2, 4)], '1': [(1, 0), (1, 1), (1, 2), (1, 3), (1, 4)], '2': [(0, 0), (1, 0), (2, 0), (2, 1), (0, 2), (1, 2), (2, 2), (0, 3), (0, 4), (1, 4), (2, 4)], '3': [(0, 0), (1, 0), (2, 0), (2, 1), (0, 2), (1, 2), (2, 2), (2, 3), (0, 4), (1, 4), (2, 4)], '4': [(0, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2), (2, 3), (2, 4)], '5': [(0, 0), (1, 0), (2, 0), (0, 1), (0, 2), (1, 2), (2, 2), (2, 3), (0, 4), (1, 4), (2, 4)], '6': [(0, 0), (1, 0), (2, 0), (0, 1), (0, 2), (1, 2), (2, 2), (0, 3), (2, 3), (0, 4), (1, 4), (2, 4)], '7': [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (2, 3), (2, 4)], '8': [(0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2), (0, 3), (2, 3), (0, 4), (1, 4), (2, 4)], '9': [(0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2), (2, 3), (0, 4), (1, 4), (2, 4)], 'K': [(0, 0), (2, 0), (0, 1), (1, 1), (0, 2), (0, 3), (1, 3), (0, 4), (2, 4)], '.': [(1, 4)], ' ': []}
RANK_THRESHOLDS = [(0, 'Seed'), (50, 'Sprout'), (200, 'Sapling'), (500, 'Tree'), (1000, 'Oak'), (2000, 'Ancient'), (5000, 'Forest'), (10000, 'Legend')]

def get_rank(total: int) -> str:
    rank = 'Seed'
    for threshold, name in RANK_THRESHOLDS:
        if total >= threshold:
            rank = name
    return rank

def render_creatures(branches: list, total: int, season: str, tod: str, rng) -> str:
    lines = ['<!-- Creatures -->']
    tips = [b for b in branches if b['depth'] >= 4]
    if not tips:
        return '<!-- No branches for creatures -->'
    for creature, req in CREATURE_UNLOCKS.items():
        if total < req['min_contributions']:
            continue
        if req['seasons'] and season not in req['seasons']:
            continue
        if req['time'] and tod not in req['time']:
            continue
        creature_rng = __import__('random').Random(total + hash(creature))
        tip = creature_rng.choice(tips)
        cx, cy = (tip['x'], tip['y'] - 8)
        color = CREATURE_COLORS[creature]
        shape = CREATURE_PIXEL_SHAPES[creature]
        lines.append(f'<g class="creature-{creature}">')
        bob_dur = round(rng.uniform(2, 4), 1)
        bob_delay = round(rng.uniform(0, 3), 2)
        lines.append(f'  <animateTransform attributeName="transform" type="translate" values="0 0;0 -2;0 0;0 1;0 0" dur="{bob_dur}s" begin="{bob_delay}s" repeatCount="indefinite"/>')
        for ox, oy in shape:
            lines.append(f'  {{_rect(cx + ox, cy + oy, PIXEL_SIZE, PIXEL_SIZE, color)}}')
        lines.append('</g>')
        if creature == 'butterfly':
            lines.append(f'<g class="butterfly-wings">')
            flap_dur = round(rng.uniform(0.3, 0.6), 2)
            lines.append(f'  <animateTransform attributeName="transform" type="translate" from="{cx} {cy}" to="{{cx + rng.randint(-20, 20)}} {{cy + rng.randint(-15, 15)}}" dur="{{rng.randint(4, 8)}}s" begin="0s" repeatCount="indefinite"/>')
            lines.append('</g>')
    return '\n'.join(lines)

def render_pixel_text(text: str, start_x: int, start_y: int, color: str, scale: int=1) -> str:
    lines = []
    cursor_x = start_x
    for char in text.upper():
        pixels = PIXEL_FONT.get(char, [])
        for px, py in pixels:
            x = cursor_x + px * (scale + 1)
            y = start_y + py * (scale + 1)
            lines.append(_rect(x, y, scale, scale, color))
        cursor_x += 4 * (scale + 1)
    return '\n'.join(lines)

def render_hud(total: int, streak: int, season: str, tod: str, canvas: dict) -> str:
    lines = ['<!-- HUD -->']
    panel_x = canvas['w'] - 85
    panel_y = 8
    lines.append(f'<rect x="{panel_x}" y="{panel_y}" width="78" height="40" rx="2" fill="#000000" opacity="0.35"/>')
    total_str = f'{total}' if total < 10000 else f'{total / 1000:.1f}K'
    text_color = '#e0e0e0'
    lines.append(render_pixel_text(total_str, panel_x + 4, panel_y + 4, text_color, 1))
    if streak > 0:
        fx = panel_x + 4
        fy = panel_y + 16
        lines.append(_rect(fx, fy, 2, 2, '#ff6633'))
        lines.append(_rect(fx, fy - 2, 2, 2, '#ffaa33'))
        lines.append(_rect(fx + 2, fy, 2, 2, '#ff4411'))
        lines.append(render_pixel_text(str(streak), fx + 8, fy - 2, text_color, 1))
    rank = get_rank(total)
    rank_color = '#8bc34a' if total < 1000 else '#f0c040' if total < 5000 else '#ff6633'
    lines.append(render_pixel_text(rank, panel_x + 4, panel_y + 28, rank_color, 1))
    return '\n'.join(lines)

def generate_svg(state: dict, canvas: dict, path: str=SVG_FILE) -> None:
    total = state.get('total_contributions', 0)
    branches = state['tree'].get('branches', [])
    leaves = state['tree'].get('leaves', [])
    plants = state['tree'].get('plants', [])
    tz_name = state.get('timezone', 'UTC')
    tz = ZoneInfo(tz_name) if tz_name != 'UTC' else timezone.utc
    season = get_season(tz)
    tod = get_time_of_day(tz)
    leaf_colors = SEASON_LEAF_COLORS[season]
    palette = TIME_PALETTES[tod]
    star_rng = random.Random(total + 42)
    weather_rng = random.Random(total + int(datetime.now(timezone.utc).timestamp()) // 3600)
    scale_params = compute_scale_params(total)
    parts = []
    cw, ch = (canvas['w'], canvas['h'])
    parts.append(f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {cw} {ch}" width="{cw}" height="{ch}" preserveAspectRatio="xMidYMid meet">')
    parts.append(build_defs(leaf_colors))
    parts.append(render_sky(tod, palette, canvas))
    parts.append(render_stars(star_rng, tod, canvas))
    parts.append(render_weather(tod, season, state.get('activity', {}).get('recent_score', 0), weather_rng, canvas))
    parts.append(render_ground(season, state.get('contribution_days', {}), canvas))
    parts.append(render_roots(total, palette, canvas))
    parts.append(render_branches(branches, scale_params, palette, canvas))
    parts.append(render_fruit(branches, total))
    parts.append(render_leaves(leaves, canvas))
    parts.append(render_plants(plants, canvas))
    parts.append('</svg>')
    content = '\n'.join(parts)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    size_kb = os.path.getsize(path) / 1024
    print(f'[gitwood] SVG written to {path} ({size_kb:.1f} KB) | season={season} tod={tod}')

def parse_args():
    parser = argparse.ArgumentParser(description='Gitwood — Animated GitHub ecosystem tree SVG generator')
    parser.add_argument('--username', required=True)
    parser.add_argument('--token', default=None)
    parser.add_argument('--mode', choices=['initial', 'update'], default='update')
    parser.add_argument('--output', default=SVG_FILE)
    parser.add_argument('--state', default=STATE_FILE)
    parser.add_argument('--demo', action='store_true', help='Generate synthetic data')
    parser.add_argument('--timezone', default=None, help='IANA timezone e.g. Asia/Kolkata')
    return parser.parse_args()

def compute_rolling_avg(activity_scores: dict, days: int=7) -> float:
    today = datetime.now(timezone.utc).date()
    vals = [activity_scores.get((today - timedelta(days=i)).strftime('%Y-%m-%d'), 0) for i in range(days)]
    return sum(vals) / len(vals)

def run_initial(username: str, token: str, args) -> None:
    print(f'[gitwood] Initial run for @{username}')
    state = load_state(args.state)
    tz_name = args.timezone or state.get('timezone', 'UTC')
    tz = ZoneInfo(tz_name) if tz_name != 'UTC' else timezone.utc
    state['timezone'] = tz_name
    print('[gitwood] Fetching lifetime contributions via GraphQL...')
    try:
        gql_data = fetch_graphql(username, token, state)
    except Exception as e:
        print(f'[gitwood] GraphQL fetch failed: {e}', file=sys.stderr)
        sys.exit(1)
    try:
        events = fetch_rest_events(username, token)
        activity_scores = compute_activity_score(events)
        today_score = get_today_score(events)
        streak = check_streak(activity_scores)
        rolling_avg = compute_rolling_avg(activity_scores)
    except Exception:
        today_score = 0
        streak = 0
        rolling_avg = 0
    state = update_activity(state, streak, today_score, last_high_day=None)
    total = gql_data['total_contributions']
    yearly_cache = gql_data['yearly_cache']
    state['contribution_days'] = gql_data.get('days', {})
    print(f'[gitwood] Total contributions: {total}')
    state = update_metadata(state, total, username, yearly_cache)
    if total < 50:
        target_plants = 1
    elif total < 200:
        target_plants = 3
    elif total < 500:
        target_plants = 5
    elif total < 1000:
        target_plants = 8
    elif total < 2000:
        target_plants = 12
    else:
        target_plants = 15
    canvas = compute_canvas_size(total, target_plants)
    state['canvas'] = canvas
    print('[gitwood] Building fractal branch structure...')
    branches = build_branches(total, canvas)
    state['tree']['branches'] = branches
    print(f'[gitwood] Generated {len(branches)} branch pixels')
    season = get_season(tz)
    anchors = collect_leaf_anchors(branches)
    rng = random.Random(total + 1)
    leaves, _ = evolve_leaves([], anchors, today_score, total, rng, season)
    state['tree']['leaves'] = leaves
    state['tree']['plants'] = seed_initial_ecosystem(total, rng, canvas)
    print(f"[gitwood] Seeded {len(state['tree']['plants'])} initial plants/flowers")
    print(f'[gitwood] Placed {len(leaves)} leaves (season: {season})')
    save_state(state, args.state)
    generate_svg(state, canvas, args.output)

def run_update(username: str, token: str, args) -> None:
    print(f'[gitwood] Update run for @{username}')
    state = load_state(args.state)
    if not state.get('username'):
        print('[gitwood] No existing state, falling back to initial run.')
        run_initial(username, token, args)
        return
    tz_name = args.timezone or state.get('timezone', 'UTC')
    tz = ZoneInfo(tz_name) if tz_name != 'UTC' else timezone.utc
    state['timezone'] = tz_name
    print('[gitwood] Fetching recent events via REST API...')
    try:
        events = fetch_rest_events(username, token)
    except Exception as e:
        print(f'[gitwood] REST fetch failed: {e}', file=sys.stderr)
        sys.exit(1)
    activity_scores = compute_activity_score(events)
    today_score = get_today_score(events)
    streak = check_streak(activity_scores)
    rolling_avg = compute_rolling_avg(activity_scores)
    print(f"[gitwood] Today's score: {today_score}, Streak: {streak} days")
    print('[gitwood] Refreshing total contributions via GraphQL...')
    try:
        gql_data = fetch_graphql(username, token, state)
        total = gql_data['total_contributions']
        yearly_cache = gql_data['yearly_cache']
        state['contribution_days'] = gql_data.get('days', {})
    except Exception as e:
        print(f'[gitwood] GraphQL refresh failed (using cached): {e}')
        total = state.get('total_contributions', 0)
        yearly_cache = state.get('yearly_cache', {})
    old_canvas = state.get('canvas')
    canvas = compute_canvas_size(total, len(state['tree'].get('plants', [])))
    if old_canvas and old_canvas['h'] != canvas['h']:
        dy = canvas['trunk_base_y'] - old_canvas['trunk_base_y']
        print(f"[gitwood] Canvas resized ({old_canvas['h']} -> {canvas['h']})! Shifting Y by {dy}px")
        for b in state['tree'].get('branches', []):
            b['y'] += dy
        for l in state['tree'].get('leaves', []):
            l['y'] += dy
        for p in state['tree'].get('plants', []):
            p['y'] += dy
    state['canvas'] = canvas
    old_total = state.get('total_contributions', 0)
    state = update_metadata(state, total, username, yearly_cache)
    if abs(total - old_total) > 3 or not state['tree'].get('branches'):
        print(f'[gitwood] Rebuilding branches ({old_total} → {total})...')
        branches = build_branches(total, canvas)
        state['tree']['branches'] = branches
        print(f'[gitwood] Generated {len(branches)} branch pixels')
    else:
        branches = state['tree']['branches']
        print(f'[gitwood] Keeping existing {len(branches)} branch pixels')
    season = get_season(tz)
    anchors = collect_leaf_anchors(branches)
    rng = random.Random(total + int(datetime.now(timezone.utc).timestamp()) % 10000)
    leaves, completed_falling = evolve_leaves(state['tree'].get('leaves', []), anchors, today_score, total, rng, season)
    state['tree']['leaves'] = leaves
    print(f'[gitwood] Leaves: {len(leaves)} active, {len(completed_falling)} fell | season={season}')
    plants = evolve_plants(state, today_score, rolling_avg, completed_falling, rng, canvas)
    state['tree']['plants'] = plants
    state = update_activity(state, streak, today_score, last_high_day=state['activity'].get('last_high_day'))
    save_state(state, args.state)
    generate_svg(state, canvas, args.output)

def run_demo(args):
    total = int(args.username) if args.username.isdigit() else 500
    state = empty_state('demo-user')
    state['total_contributions'] = total
    demo_rng = __import__('random').Random(total)
    state['contribution_days'] = {(__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - __import__('datetime').timedelta(days=i)).strftime('%Y-%m-%d'): demo_rng.randint(0, 10) if demo_rng.random() > 0.3 else 0 for i in range(365)}
    streak = min(total // 50, 30)
    today_score = demo_rng.randint(1, 10) if total > 50 else 0
    state = update_activity(state, streak, today_score, last_high_day=None)
    if total < 50:
        target_plants = 1
    elif total < 200:
        target_plants = 3
    elif total < 500:
        target_plants = 5
    elif total < 1000:
        target_plants = 8
    elif total < 2000:
        target_plants = 12
    else:
        target_plants = 15
    canvas = compute_canvas_size(total, target_plants)
    state['canvas'] = canvas
    branches = build_branches(total, canvas)
    state['tree']['branches'] = branches
    season = get_season(__import__('zoneinfo').ZoneInfo('UTC'))
    anchors = collect_leaf_anchors(branches)
    rng = __import__('random').Random(total + 1)
    leaves, _ = evolve_leaves([], anchors, today_score, total, rng, season, canvas=canvas) if 'canvas' in evolve_leaves.__code__.co_varnames else evolve_leaves([], anchors, today_score, total, rng, season)
    state['tree']['leaves'] = leaves
    state['tree']['plants'] = seed_initial_ecosystem(total, rng, canvas)
    save_state(state, args.state)
    generate_svg(state, canvas, args.output)
    print(f"[gitwood] Demo generated: {total} contributions, {len(branches)} branches, {len(leaves)} leaves, {len(state['tree']['plants'])} plants")

def main():
    args = parse_args()
    token = args.token or os.environ.get('GH_TOKEN', '')
    if not token:
        print('Error: No GitHub token. Use --token or set GH_TOKEN.', file=sys.stderr)
        sys.exit(1)
    if args.demo:
        run_demo(args)
    elif args.mode == 'initial':
        run_initial(args.username, token, args)
    else:
        run_update(args.username, token, args)
if __name__ == '__main__':
    main()