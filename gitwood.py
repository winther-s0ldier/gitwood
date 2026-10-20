import argparse
import json
import math
import os
import random
import sys
import zlib
from collections import Counter, defaultdict
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
MAX_LEAVES = 420
MAX_PLANTS = 24
GRASS_ROWS = 2
TZ = ZoneInfo('Asia/Kolkata')
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

def now_local() -> datetime:
    return datetime.now(TZ)

def day_key(d) -> str:
    return d.strftime('%Y-%m-%d')

def get_season(tz=None) -> str:
    month = datetime.now(tz or TZ).month
    if month in (3, 4, 5):
        return 'spring'
    elif month in (6, 7, 8):
        return 'summer'
    elif month in (9, 10, 11):
        return 'autumn'
    return 'winter'

def get_time_of_day(tz=None) -> str:
    hour = datetime.now(tz or TZ).hour
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
    current_year = now_local().year
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
        date_str = day_key(dt.astimezone(TZ))
        weight = EVENT_WEIGHTS.get(event.get('type', ''), 0)
        if event.get('type') == 'PushEvent':
            payload = event.get('payload', {})
            commits = payload.get('size') or len(payload.get('commits', []))
            weight = max(weight, commits)
        scores[date_str] += weight
    return dict(scores)

def summarize_events(events: list) -> dict:
    """Merged PRs (30d) feed ripe fruit; merged PRs / closed issues (24h) feed rainbows."""
    now = datetime.now(timezone.utc)
    merged_30d = 0
    celebrate_24h = 0
    for event in events:
        try:
            dt = datetime.fromisoformat(event.get('created_at', '').replace('Z', '+00:00'))
        except ValueError:
            continue
        payload = event.get('payload', {})
        kind = event.get('type')
        merged = kind == 'PullRequestEvent' and payload.get('action') == 'closed' and payload.get('pull_request', {}).get('merged')
        closed_issue = kind == 'IssuesEvent' and payload.get('action') == 'closed'
        if merged and now - dt < timedelta(days=30):
            merged_30d += 1
        if (merged or closed_issue) and now - dt < timedelta(hours=24):
            celebrate_24h += 1
    return {'merged_prs': merged_30d, 'celebrate_24h': celebrate_24h}

def merge_days(event_scores: dict, gql_days: dict) -> dict:
    """Union of the GraphQL calendar (private repos, full history) and events (IST-accurate recent days)."""
    days = dict(gql_days)
    for k, v in event_scores.items():
        days[k] = max(days.get(k, 0), v)
    return days

def check_streak(days: dict) -> int:
    d = now_local().date()
    if not days.get(day_key(d)):
        d -= timedelta(days=1)
    streak = 0
    while days.get(day_key(d)):
        streak += 1
        d -= timedelta(days=1)
    return streak

def days_inactive(days: dict) -> int:
    d = now_local().date()
    n = 0
    while n < 365 and not days.get(day_key(d)):
        n += 1
        d -= timedelta(days=1)
    return n

def recent_score(days: dict) -> int:
    """Today's score; before noon IST yesterday also counts so a fresh day is not instantly idle."""
    today = now_local().date()
    score = days.get(day_key(today), 0)
    if now_local().hour < 12:
        score = max(score, days.get(day_key(today - timedelta(days=1)), 0))
    return score

def compute_rolling_avg(days: dict, n: int=7) -> float:
    today = now_local().date()
    return sum((days.get(day_key(today - timedelta(days=i)), 0) for i in range(n))) / n
STARS_QUERY = 'query($login: String!) { user(login: $login) { repositories(first: 100, ownerAffiliations: OWNER, isFork: false) { nodes { stargazerCount forkCount } } } }'

def fetch_languages(username: str, token: str, days: int=30) -> dict:
    """Commits per primary language over a sliding window ending now.

    GraphQL hides per-repo detail for private and org repos, so walk the REST API instead:
    list every repo the token can push to, then count the user's commits in the repos active in the window.
    """
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since = since_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    repos = []
    for page in range(1, 6):
        resp = requests.get(f'{REST_URL}/user/repos', headers=headers, params={'affiliation': 'owner,collaborator,organization_member', 'sort': 'pushed', 'direction': 'desc', 'per_page': 100, 'page': page}, timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        repos.extend(batch)
        if len(batch) < 100 or batch[-1]['pushed_at'] < since:
            break
    active = [r for r in repos if r.get('pushed_at') and r['pushed_at'] >= since]
    weights = Counter()
    owners = Counter()
    for repo in active:
        resp = requests.get(f"{REST_URL}/repos/{repo['full_name']}/commits", headers=headers, params={'author': username, 'since': since, 'per_page': 100}, timeout=30)
        if resp.status_code != 200:
            continue
        n = len(resp.json())
        if n:
            weights[repo.get('language') or 'Other'] += n
            owners[repo['owner']['login']] += 1
    owner_list = ', '.join(('%s=%d' % (o, n) for o, n in owners.items()))
    print('[gitwood] Language window %dd: %d/%d repos had commits (%s)' % (days, sum(owners.values()), len(active), owner_list))
    return dict(weights)

def fetch_stars(username: str, token: str) -> int:
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    data = _graphql_post({'query': STARS_QUERY, 'variables': {'login': username}}, headers)
    nodes = data['data']['user']['repositories']['nodes']
    return sum((n['stargazerCount'] + n['forkCount'] for n in nodes))

def empty_state(username: str='') -> dict:
    return {'username': username, 'version': 2, 'total_contributions': 0, 'last_update': datetime.now(timezone.utc).isoformat(), 'timezone': str(TZ), 'yearly_cache': {}, 'contribution_days': {}, 'languages': {}, 'stars': 0, 'tree': {'branches': [], 'leaves': [], 'plants': []}, 'activity': {'streak': 0, 'recent_score': 0, 'last_high_day': None, 'inactive_days': 0, 'merged_prs': 0, 'celebrate_24h': 0}}

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
        now = datetime.now(timezone.utc)
        for leaf in state.get('tree', {}).get('leaves', []):
            leaf.setdefault('attached_at', now.isoformat())
            leaf.setdefault('lifespan_days', random.randint(14, 28))
        migrate = state.get('version', 1) < 2
        for plant in state.get('tree', {}).get('plants', []):
            plant.setdefault('growth_stage', 'mature')
            plant.setdefault('kind', 'ground_plant')
            if migrate or 'born_at' not in plant:
                # pre-lifecycle plants: stagger ages so they do not all die on the same day
                plant['born_at'] = (now - timedelta(days=random.randint(0, 35))).isoformat()
        state['version'] = 2
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

def update_activity(state: dict, streak: int, today_score: int, last_high_day, **extra) -> dict:
    state['activity']['streak'] = streak
    state['activity']['recent_score'] = today_score
    state['activity'].update(extra)
    if last_high_day is not None:
        state['activity']['last_high_day'] = last_high_day
    return state
HEIGHT_CAP_CONTRIBUTIONS = 5000
BIRTH_BASE = 8
BIRTH_GROWTH = 1.9
BIRTH_SPREAD = 0.7
MAX_TREE_DEPTH = 11
LENGTH_DECAY = 0.74

def compute_scale_params(total: int) -> dict:
    t = max(total, 0)
    capped_t = min(t, HEIGHT_CAP_CONTRIBUTIONS)
    initial_length = int(min(80, max(20, math.log(capped_t + 1) * 9)) // PIXEL_SIZE * PIXEL_SIZE)
    trunk_thickness = min(6, max(1, int(math.log(t + 1, 10) * 1.6)))
    branch_thickness = {'trunk': trunk_thickness, 'mid': max(1, trunk_thickness - 3), 'tip': 1}
    leaf_scale = 1 if t < 1000 else 2
    return {'max_depth': MAX_TREE_DEPTH, 'initial_length': initial_length, 'trunk_thickness': trunk_thickness, 'branch_thickness': branch_thickness, 'leaf_scale': leaf_scale}

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

def node_rand(seed: int, path: str) -> float:
    return (zlib.crc32(f'{seed}:{path}'.encode()) & 4294967295) / 4294967296

def branch_birth_total(depth: int, u: float) -> float:
    """Contribution total at which a branch node appears. Parent threshold never exceeds its child's."""
    if depth == 0:
        return 0
    return BIRTH_BASE * BIRTH_GROWTH ** depth * (1 + BIRTH_SPREAD * u)

def generate_branches(x, y, angle, length, depth, total, branches, seed, path, canvas):
    """Each node's shape comes from its own path hash, so new growth only adds branches; existing ones never move."""
    if depth > MAX_TREE_DEPTH or length < PIXEL_SIZE:
        return
    if total < branch_birth_total(depth, node_rand(seed, path + 'b')):
        return
    x2 = x + math.cos(math.radians(angle)) * length
    y2 = y - math.sin(math.radians(angle)) * length
    branches.extend(rasterize_segment(x, y, x2, y2, depth, angle, canvas))
    spread = 25 + int(node_rand(seed, path + 's') * 21) - 10
    generate_branches(x2, y2, angle + spread, length * LENGTH_DECAY, depth + 1, total, branches, seed, path + 'L', canvas)
    generate_branches(x2, y2, angle - spread, length * LENGTH_DECAY, depth + 1, total, branches, seed, path + 'R', canvas)
    if depth < 2:
        wobble = int(node_rand(seed, path + 'm') * 11) - 5
        generate_branches(x2, y2, angle + wobble, length * 0.6, depth + 1, total, branches, seed, path + 'M', canvas)
    elif depth <= 5 and node_rand(seed, path + 'x') > 0.45:
        twig = int(node_rand(seed, path + 'y') * 36) - 18
        generate_branches(x2, y2, angle + twig, length * 0.62, depth + 1, total, branches, seed, path + 'M', canvas)

def build_branches(total: int, canvas: dict, seed: int=0) -> list:
    params = compute_scale_params(total)
    branches = []
    generate_branches(float(canvas['trunk_base_x']), float(canvas['trunk_base_y']), 90.0, float(params['initial_length']), 0, total, branches, seed, 'T', canvas)
    return branches

def collect_leaf_anchors(branches: list) -> list:
    if not branches:
        return []
    anchors = [(b['x'], b['y']) for b in branches if b['depth'] >= 2]
    return list(dict.fromkeys(anchors))

def compute_leaf_target(total: int, season: str) -> int:
    base = min(MAX_LEAVES, max(5, total // 5))
    if season == 'winter':
        return max(3, base // 4)
    elif season == 'autumn':
        return max(5, int(base * 0.7))
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

def evolve_leaves(existing, anchors, today_score, total, rng, season='summer', vitality=1.0):
    anchor_set = set(anchors)
    now = datetime.now(timezone.utc)
    target = max(2, int(compute_leaf_target(total, season) * vitality))
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
    excess = len([l for l in updated if l['state'] != 'falling']) - target
    for i, leaf in enumerate(updated):
        if excess <= 0:
            break
        if leaf['state'] == 'attached' and rng.random() < 0.3:
            updated[i] = dict(leaf, state='falling', fell_at=now.isoformat())
            excess -= 1
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
PLANT_KINDS = {'ground_plant': ['sprout', 'fern', 'mushroom', 'shrub', 'snowdrop'], 'flower': ['flower_purple', 'flower_pink', 'flower_blue', 'flower_lavender', 'flower_orange', 'flower_yellow'], 'tree_seedling': ['seedling']}
GROWTH_STAGES = {'ground_plant': ['sprout', 'growing', 'mature', 'withered'], 'flower': ['bud', 'blooming', 'full_bloom', 'wilting'], 'tree_seedling': ['seed', 'sprout', 'sapling', 'young_tree']}
STAGE_DURATIONS = {'ground_plant': {'sprout': 3, 'growing': 7, 'mature': 30, 'withered': 6}, 'flower': {'bud': 2, 'blooming': 5, 'full_bloom': 14, 'wilting': 3}, 'tree_seedling': {'seed': 3, 'sprout': 7, 'sapling': 21, 'young_tree': 999}}
LEAF_SPAWN_CHANCES = {'flower': 0.12, 'ground_plant': 0.05, 'tree_seedling': 0.02}
PLANT_PIXEL_SHAPES = {'snowdrop': [(0, 0), (0, -4), (-4, -8), (0, -8), (4, -8), (0, -12)], 'sprout': [(0, 0), (0, -4), (0, -8), (-4, -8), (4, -8)], 'flower_purple': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (0, -16), (-4, -8), (4, -8)], 'flower_pink': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (-8, -8), (8, -8), (0, -16)], 'flower_blue': [(0, 0), (0, -4), (-4, -8), (0, -8), (4, -8), (0, -12)], 'flower_lavender': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (-4, -16), (0, -16), (4, -16)], 'flower_yellow': [(0, 0), (0, -4), (0, -8), (-4, -8), (4, -8), (-4, -12), (0, -12), (4, -12)], 'flower_orange': [(0, 0), (0, -4), (0, -8), (-4, -12), (0, -12), (4, -12), (-4, -8), (4, -8), (0, -16)], 'shrub': [(-8, 0), (-4, 0), (0, 0), (4, 0), (8, 0), (-4, -4), (0, -4), (4, -4), (0, -8), (-8, -4), (8, -4)], 'mushroom': [(-4, -8), (0, -8), (4, -8), (-8, -4), (-4, -4), (0, -4), (4, -4), (8, -4), (-4, 0), (4, 0), (0, 4)], 'fern': [(0, 0), (0, -4), (0, -8), (0, -12), (-4, -4), (-8, -8), (4, -4), (8, -8), (4, -12), (-4, -12)], 'seedling_seed': [(0, 0)], 'seedling_sprout': [(0, 0), (0, -4), (-4, -4), (4, -4)], 'seedling_sapling': [(0, 0), (0, -4), (0, -8), (0, -12), (-4, -8), (4, -8), (-4, -4), (4, -4), (-4, -12), (4, -12)], 'seedling_young_tree': [(0, 4), (0, 0), (0, -4), (0, -8), (0, -12), (0, -16), (-4, -8), (4, -8), (-8, -8), (8, -8), (-4, -12), (4, -12), (-8, -12), (8, -12), (-4, -16), (4, -16), (0, -20), (-4, -20), (4, -20)]}
PLANT_COLORS = {'snowdrop': '#e8f0f8', 'sprout': '#6aad5e', 'shrub': '#4a7c59', 'mushroom': '#cc7755', 'fern': '#2d5a27', 'flower_purple': '#9b59b6', 'flower_pink': '#e91e8c', 'flower_blue': '#3498db', 'flower_lavender': '#b39ddb', 'flower_yellow': '#f1c40f', 'flower_orange': '#ff9966', 'seedling_seed': '#8B4513', 'seedling_sprout': '#6aad5e', 'seedling_sapling': '#5a8a4a', 'seedling_young_tree': '#4a7c39'}

LANG_FLOWERS = {'Python': 'flower_blue', 'JavaScript': 'flower_yellow', 'TypeScript': 'flower_lavender', 'Dart': 'flower_pink', 'Jupyter Notebook': 'flower_orange'}
SEASON_PLANTS = {'spring': [('flower', 0.6), ('sprout', 0.2), ('fern', 0.2)], 'summer': [('flower', 0.4), ('shrub', 0.25), ('fern', 0.2), ('sprout', 0.15)], 'autumn': [('mushroom', 0.28), ('flower', 0.32), ('fern', 0.2), ('shrub', 0.2)], 'winter': [('snowdrop', 0.45), ('shrub', 0.3), ('fern', 0.25)]}

def flower_for_language(rng, langs) -> str:
    """Flower colour follows the languages committed in the last 30 days; unknown languages get purple."""
    if not langs:
        return rng.choice(PLANT_KINDS['flower'])
    names = list(langs)
    pick = rng.choices(names, [max(1, langs[n]) for n in names])[0]
    return LANG_FLOWERS.get(pick, 'flower_purple')

def pick_plant(season: str, rng, langs) -> tuple:
    names, weights = zip(*SEASON_PLANTS[season])
    choice = rng.choices(names, weights)[0]
    if choice == 'flower':
        return (flower_for_language(rng, langs), 'flower')
    return (choice, 'ground_plant')

def make_plant(x: int, ptype: str, kind: str, canvas: dict, born_at=None) -> dict:
    born = born_at or datetime.now(timezone.utc).isoformat()
    return {'x': x, 'y': canvas['grass_y'], 'type': ptype, 'kind': kind, 'growth_stage': GROWTH_STAGES[kind][0], 'born_at': born, 'size': 1}

def free_x(plants: list, canvas: dict, rng, x=None):
    """A grass x position at least 9px from every plant, clustered around the meadow patches; None when full."""
    patches = canvas.get('patches') or []
    for _ in range(40):
        if x is not None:
            cx = x
        elif patches and rng.random() < 0.85:
            cx = int(rng.gauss(rng.choice(patches), 18))
        else:
            cx = rng.randint(20, canvas['w'] - 20)
        cx = max(20, min(canvas['w'] - 20, cx))
        if all((abs(cx - p['x']) >= 9 for p in plants)):
            return cx
        x = None
    return None

def check_spawn_triggers(state, today_score, rolling_avg):
    triggers = []
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

def maybe_spawn_from_leaf(leaf, plants, rng, canvas, season='summer', langs=None):
    if len(plants) >= MAX_PLANTS:
        return None
    kind = None
    if season == 'winter':
        if rng.random() < 0.1:
            ptype, kind = ('snowdrop', 'ground_plant')
    elif rng.random() < 0.12:
        ptype, kind = (flower_for_language(rng, langs), 'flower')
    elif rng.random() < (0.15 if season == 'autumn' else 0.05):
        # fallen leaves rot into mushrooms in autumn
        ptype = 'mushroom' if season == 'autumn' else rng.choice(['sprout', 'fern', 'shrub'])
        kind = 'ground_plant'
    elif rng.random() < 0.02:
        ptype, kind = ('seedling', 'tree_seedling')
    if kind is None:
        return None
    x = free_x(plants, canvas, rng, leaf['x'] + leaf.get('drift', 0) // 3)
    if x is None:
        return None
    return make_plant(x, ptype, kind, canvas)

def evolve_plant_growth(plants: list, rng) -> list:
    now = datetime.now(timezone.utc)
    updated = []
    for plant in plants:
        kind = plant.get('kind', 'ground_plant')
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
            cumulative_days += durations.get(s, 999)
            target_stage = s
            if age_days < cumulative_days:
                break
        if kind != 'tree_seedling' and age_days > cumulative_days:
            continue
        plant = dict(plant)
        plant['growth_stage'] = target_stage
        if kind == 'tree_seedling':
            plant['type'] = f'seedling_{target_stage}'
        updated.append(plant)
    return updated

def spawn_plant(x, plant_type, canvas):
    return make_plant(x, plant_type, 'ground_plant', canvas)

def evolve_plants(state, today_score, rolling_avg, completed_falling, rng, canvas, season='summer', langs=None):
    plants = evolve_plant_growth(list(state['tree']['plants']), rng)
    triggers = check_spawn_triggers(state, today_score, rolling_avg)
    if today_score > 0 and rng.random() < 0.25:
        triggers.append('ambient')
    for trigger in triggers:
        if len(plants) >= MAX_PLANTS:
            break
        x = free_x(plants, canvas, rng)
        if x is None:
            break
        ptype, kind = pick_plant(season, rng, langs)
        plants.append(make_plant(x, ptype, kind, canvas))
        if trigger.startswith('streak_'):
            state['activity']['last_high_day'] = trigger
    for leaf in completed_falling:
        new_plant = maybe_spawn_from_leaf(leaf, plants, rng, canvas, season, langs)
        if new_plant:
            plants.append(new_plant)
    return plants

def seed_initial_ecosystem(total: int, rng, canvas, langs=None) -> list:
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
    for i in range(target_plants):
        x = free_x(plants, canvas, eco_rng)
        if x is None:
            break
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
            plant_type = flower_for_language(eco_rng, langs)
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

def blend_hex(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join((f'{int(round(x + (y - x) * t)):02x}' for x, y in zip(a, b)))

def rot_attr(x: int, sag: float, pivot: tuple) -> str:
    """Droop: outer growth sags a few px straight down, so it stays attached to the branches below."""
    if sag <= 0:
        return ''
    return f' transform="translate(0 {round(sag, 1)})"'

def build_defs(leaf_colors: list) -> str:
    lines = ['<defs>']
    lines.append(f'  <rect id="px" width="{PIXEL_SIZE}" height="{PIXEL_SIZE}"/>')
    for i, color in enumerate(leaf_colors):
        light = blend_hex(color, '#ffffff', 0.28)
        dark = blend_hex(color, '#000000', 0.28)
        lines.append(f'  <g id="leaf-{i}">')
        for ox, oy, c in ((0, -PIXEL_SIZE, light), (-PIXEL_SIZE, 0, color), (0, 0, color), (PIXEL_SIZE, 0, color), (0, PIXEL_SIZE, dark)):
            lines.append(f'    {_rect(ox, oy, PIXEL_SIZE, PIXEL_SIZE, c)}')
        lines.append('  </g>')
    for fid, palette in FRUIT_PALETTES.items():
        lines.append(f'  <g id="{fid}">')
        for dx, dy, w, color in merge_runs(sprite_triples(FRUIT_ROWS, palette, anchor='center')):
            lines.append(f'    {_rect(dx, dy, w, PIXEL_SIZE, color)}')
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

def render_fireflies(tod: str, season: str, rng, canvas: dict, count: int) -> str:
    """Night meadow: glowing fireflies drifting low over the grass, with a soft halo."""
    if tod != 'night' or season == 'winter':
        return '<!-- No fireflies -->'
    lines = ['<!-- Fireflies -->']
    for _ in range(count):
        cx = rng.randint(16, canvas['w'] - 16)
        cy = rng.randint(canvas['grass_y'] - 90, canvas['grass_y'] - 8)
        delay = round(rng.uniform(0, 5), 2)
        dur = round(rng.uniform(2, 4), 2)
        drift = '%d %d' % (rng.randint(-18, 18), rng.randint(-12, 12))
        lines.append('<g>')
        lines.append(f'  <animateTransform attributeName="transform" type="translate" from="0 0" to="{drift}" dur="{dur * 2}s" begin="{delay}s" repeatCount="indefinite"/>')
        lines.append(f'  <circle cx="{cx}" cy="{cy}" r="4" fill="#d8ff66" opacity="0"><animate attributeName="opacity" values="0;0.25;0.3;0.1;0" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/></circle>')
        lines.append(f'  <circle cx="{cx}" cy="{cy}" r="1.5" fill="#ffff99" opacity="0"><animate attributeName="opacity" values="0;0.9;1;0.4;0" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/></circle>')
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
    today = now_local().date()
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

def render_rainbow(tod: str, canvas: dict) -> str:
    """A merged PR or closed issue in the last 24h clears the sky."""
    if tod != 'day':
        return '<!-- No rainbow -->'
    cx, gy = (int(canvas['w'] * 0.68), canvas['grass_y'])
    lines = ['<!-- Rainbow -->']
    for i, color in enumerate(['#e74c3c', '#ff9966', '#f1c40f', '#4a9c59', '#3498db', '#9b59b6']):
        r = 118 + i * 4
        lines.append(f'<path d="M{cx - r} {gy} A{r} {r} 0 0 1 {cx + r} {gy}" fill="none" stroke="{color}" stroke-width="4" opacity="0.28"/>')
    return '\n'.join(lines)

def render_frost(canvas: dict, rng) -> str:
    lines = ['<!-- Frost (long inactivity) -->']
    for x in range(0, canvas['w'], 8):
        if rng.random() < 0.6:
            lines.append(_rect(x + rng.randint(0, 4), canvas['grass_y'] - 2, 2, 2, '#e8f4ff', ' opacity="0.7"'))
    return '\n'.join(lines)

def render_moss(total: int, scale_params: dict, canvas: dict) -> str:
    if total < 100:
        return '<!-- No moss yet -->'
    width = PIXEL_SIZE * scale_params['branch_thickness']['trunk']
    x_left = canvas['trunk_base_x'] - (width - PIXEL_SIZE) // 2
    x_right = x_left + width - PIXEL_SIZE
    lines = ['<!-- Moss -->']
    for k in range(min(6, total // 300 + 2)):
        x = x_left if k % 2 == 0 else x_right
        lines.append(_rect(x, canvas['trunk_base_y'] - k * PIXEL_SIZE, PIXEL_SIZE, PIXEL_SIZE, '#3d6b35', ' opacity="0.85"'))
    return '\n'.join(lines)

GRASS_PALETTES = {'spring': ('#2f6b2a', '#4a9c3f', '#86d062'), 'summer': ('#2a5a24', '#3f8a35', '#6fb552'), 'autumn': ('#6b5a22', '#a08a35', '#d9bc5e'), 'winter': ('#5a5a48', '#8a8a72', '#cfcfbb')}
SEED_HEAD = '#d9c27a'
WEED_PALETTES = {'dandelion': {'y': '#f5d020', 'Y': '#ffec70'}, 'puff': {'w': '#dcdcdc', 'W': '#ffffff'}, 'clover': {'W': '#f4f4f4'}, 'thistle': {'P': '#b57edc', 'p': '#8a4fb5'}, 'daisy': {'y': '#f5d020', 'w': '#ffffff'}}
WEED_ROWS = {'dandelion': ['yYy', '.g.', '.g.', 'GgG'], 'puff': ['.w.', 'wWw', '.g.', '.g.', 'GgG'], 'clover': ['.W.', 'gGg', 'GgG'], 'thistle': ['.P.', 'pPp', '.g.', 'GgG'], 'daisy': ['wyw', '.g.', 'GgG']}
SEASON_WEEDS = {'spring': ['dandelion', 'clover', 'daisy'], 'summer': ['dandelion', 'clover', 'daisy', 'thistle', 'puff'], 'autumn': ['puff', 'thistle', 'clover'], 'winter': []}
SPECK_COLORS = ['#ffffff', '#f5d020', '#ff9ec7', '#c9a8ff', '#ffffff']
BUTTERFLY_COLORS = [('#ff8c1a', '#2a1a10'), ('#4a8cff', '#1d3f8a'), ('#f4f4f4', '#9a9a9a'), ('#f5d020', '#8a6a00'), ('#e91e8c', '#8a0f55')]

def patch_centers(seed: int, canvas: dict) -> list:
    """Meadow patches: flowers, weeds and tall grass cluster around these instead of spreading evenly."""
    rng = random.Random(seed ^ 24301)
    centers = []
    for _ in range(60):
        x = rng.randint(24, canvas['w'] - 24)
        if all((abs(x - c) >= 52 for c in centers)):
            centers.append(x)
        if len(centers) >= 6:
            break
    return centers

def scatter_x(rng, canvas: dict, centers: list, spread: float, clustered: float=0.65) -> int:
    if centers and rng.random() < clustered:
        x = int(rng.gauss(rng.choice(centers), spread))
    else:
        x = rng.randint(0, canvas['w'])
    return max(0, min(canvas['w'] - PIXEL_SIZE, x // PIXEL_SIZE * PIXEL_SIZE))

def blade_rects(x: int, gy: int, cells: int, lean: int, palette: tuple, seeded: bool) -> list:
    dark, mid, light = palette
    if cells == 1:
        return [_rect(x, gy, PIXEL_SIZE, PIXEL_SIZE, mid)]
    rects = [_rect(x, gy, PIXEL_SIZE, PIXEL_SIZE, dark)]
    if cells >= 3:
        rects.append(_rect(x, gy - PIXEL_SIZE * (cells - 2), PIXEL_SIZE, PIXEL_SIZE * (cells - 2), mid))
    tip_x = x + (lean * PIXEL_SIZE if cells >= 3 else 0)
    rects.append(_rect(tip_x, gy - PIXEL_SIZE * (cells - 1), PIXEL_SIZE, PIXEL_SIZE, light))
    if seeded:
        rects.append(_rect(tip_x + 1, gy - PIXEL_SIZE * cells + 1, 2, 3, SEED_HEAD))
    return rects

def render_grass(season: str, seed: int, canvas: dict, centers: list, total: int, wind: float, layer: str) -> str:
    """Tall blades behind the plants ('back') and short blades, weeds and specks in front ('front')."""
    rng = random.Random(zlib.crc32(f'{seed}:{season}:{layer}'.encode()))
    gy = canvas['grass_y']
    palette = GRASS_PALETTES[season]
    back = layer == 'back'
    clumps = (44 if back else 40) + min(24, total // 90)
    lo, hi = (4, 8) if back else (1, 3)
    if season == 'winter':
        lo, hi = (max(1, lo - 1), max(2, hi - 1))
    bands = defaultdict(list)
    band_w = canvas['w'] // 8
    for _ in range(clumps):
        x = scatter_x(rng, canvas, centers, 30)
        for k in range(rng.randint(2, 4)):
            bx = x + (k - 1) * PIXEL_SIZE
            if not 0 <= bx < canvas['w']:
                continue
            cells = rng.randint(lo, hi)
            if back and rng.random() < 0.12:
                cells = rng.randint(9, 13)
            seeded = back and cells >= 5 and season in ('summer', 'autumn') and rng.random() < 0.35
            bands[bx // band_w].extend(blade_rects(bx, gy, cells, rng.choice((-1, 0, 1)), palette, seeded))
    lines = [f'<!-- Grass ({layer}) -->']
    for idx in sorted(bands):
        cx = idx * band_w + band_w // 2
        lines.append('<g>')
        lines.append(_wind_animate(cx, gy, round((1.6 if back else 1.0) * wind, 2), round(3.4 + idx * 0.3, 1), round(idx * 0.2, 2)))
        lines.extend(bands[idx])
        lines.append('</g>')
    if back:
        return '\n'.join(lines)
    weeds = [rng.choice(SEASON_WEEDS[season]) for _ in range(7 + total // 500)] if SEASON_WEEDS[season] else []
    for name in weeds:
        x = scatter_x(rng, canvas, centers, 22, 0.8)
        palette_w = dict(GREENS, **WEED_PALETTES[name])
        pixels = sprite_triples(WEED_ROWS[name], palette_w, rng.random() < 0.5)
        lines.append('<g>')
        lines.append(_wind_animate(x, gy, round(1.6 * wind, 2), round(rng.uniform(3, 5), 1), round(rng.uniform(0, 2), 2)))
        for dx, dy, w, color in merge_runs(pixels):
            lines.append(_rect(x + dx, gy + dy, w, PIXEL_SIZE, color))
        lines.append('</g>')
    if season != 'winter':
        for _ in range(10 + total // 250):
            x = scatter_x(rng, canvas, centers, 26, 0.75)
            lines.append(_rect(x + rng.randint(0, 2), gy - rng.randint(4, 14), 2, 2, rng.choice(SPECK_COLORS)))
    for _ in range(5):
        x = scatter_x(rng, canvas, centers, 40, 0.3)
        size = rng.choice((4, 6))
        lines.append(_rect(x, gy + 4 - size // 2, size, size // 2 + 2, '#7d7f86'))
        lines.append(_rect(x, gy + 4 - size // 2, size - 2, 2, '#a9acb4'))
    if season == 'autumn':
        for _ in range(10):
            x = canvas['trunk_base_x'] + rng.randint(-56, 56)
            lines.append(_rect(x, gy + 2, 4, 2, rng.choice(SEASON_LEAF_COLORS['autumn'])))
    return '\n'.join(lines)

def render_butterflies(stars: int, tod: str, season: str, rng, canvas: dict) -> str:
    """A few always flutter by day outside winter; every star or fork adds one (max 10)."""
    if tod not in ('day', 'dawn') or season == 'winter':
        return '<!-- No butterflies -->'
    lines = ['<!-- Butterflies -->']
    for _ in range(min(10, 3 + stars)):
        x = rng.randint(24, canvas['w'] - 24)
        y = rng.randint(canvas['grass_y'] - 70, canvas['grass_y'] - 14)
        wing, edge = rng.choice(BUTTERFLY_COLORS)
        way = ';'.join(('%d %d' % (rng.randint(-34, 34), rng.randint(-16, 16)) for _ in range(4)))
        dur = rng.randint(9, 16)
        lines.append('<g>')
        lines.append(f'  <animateTransform attributeName="transform" type="translate" values="0 0;{way};0 0" dur="{dur}s" begin="{rng.randint(0, 6)}s" repeatCount="indefinite"/>')
        lines.append(f'  <g transform="translate({x} {y})"><g>')
        lines.append(f'    <animateTransform attributeName="transform" type="scale" values="1 1;0.3 1;1 1" dur="{round(rng.uniform(0.35, 0.6), 2)}s" repeatCount="indefinite"/>')
        for sx in (-1, 1):
            wx = 1 if sx == 1 else -5
            lines.append('    ' + _rect(wx, -3, 4, 3, wing))
            lines.append('    ' + _rect(wx + (0 if sx == 1 else 1), 0, 3, 2, wing))
            lines.append('    ' + _rect(wx + (3 if sx == 1 else 0), -3, 1, 1, edge))
        lines.append('    ' + _rect(0, -3, 1, 5, '#2a1a10'))
        lines.append('  </g></g>')
        lines.append('</g>')
    return '\n'.join(lines)

def render_bees(plants: list, tod: str, season: str, canvas: dict, rng) -> str:
    """Bees loop around flowers that are in bloom."""
    if tod != 'day' or season == 'winter':
        return '<!-- No bees -->'
    blooms = [p for p in plants if p.get('kind') == 'flower' and p.get('growth_stage') in ('blooming', 'full_bloom')]
    if not blooms:
        return '<!-- No bees -->'
    lines = ['<!-- Bees -->']
    for p in rng.sample(blooms, min(3, len(blooms))):
        hx, hy = (p['x'] - 4, canvas['grass_y'] - 30)
        a, b = (rng.randint(8, 14), rng.randint(4, 8))
        lines.append('<g>')
        lines.append(f'  <animateTransform attributeName="transform" type="translate" values="0 0;{a} -{b};{2 * a} 0;{a} {b};0 0" dur="{rng.randint(3, 5)}s" repeatCount="indefinite"/>')
        lines.append('  ' + _rect(hx, hy, 4, 2, '#f5c518'))
        lines.append('  ' + _rect(hx + 1, hy, 1, 2, '#2a1a10'))
        lines.append('  ' + _rect(hx + 3, hy, 1, 2, '#2a1a10'))
        lines.append('  ' + _rect(hx, hy - 2, 3, 2, '#ffffff', ' opacity="0.75"'))
        lines.append('</g>')
    return '\n'.join(lines)

def pixel_paths(pixels: list, thickness: int, color_of) -> list:
    """All pixels of a branch group as one <path> per colour (far smaller than a <use> per pixel)."""
    by_color = defaultdict(list)
    for b in pixels:
        w = b['w'] * thickness
        x = b['x'] - (w - b['w']) // 2
        by_color[color_of(b['angle'])].append(f"M{x} {b['y']}h{w}v{b['h']}h-{w}z")
    return [f'<path fill="{color}" d="{"".join(cells)}"/>' for color, cells in by_color.items()]

def render_branches(branches: list, scale_params: dict, palette: dict, canvas: dict, wind: float=1.0, droop_deg: float=0.0, pivot: tuple=(0, 0)) -> str:
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
    mid_pixels = [b for b in branches if 2 <= b['depth'] <= 4]
    tip_pixels = [b for b in branches if b['depth'] >= 5]
    lines = ['<!-- Branches -->']
    if trunk_pixels:
        lines.append('<g id="wind-trunk">')
        lines.append(_wind_animate(canvas['trunk_base_x'], canvas['trunk_base_y'], round(0.3 * wind, 2), 10, 0))
        lines.extend(pixel_paths(trunk_pixels, thickness['trunk'], branch_color))
        lines.append('</g>')
    if mid_pixels:
        cy = min((b['y'] for b in mid_pixels))
        lines.append('<g id="wind-mid">')
        lines.append(_wind_animate(canvas['trunk_base_x'], cy, round(1.0 * wind, 2), 7, 0.3))
        lines.extend(pixel_paths(mid_pixels, thickness['mid'], branch_color))
        lines.append('</g>')
    if tip_pixels:
        band_size = canvas['w'] // 4
        bands = defaultdict(list)
        for b in tip_pixels:
            bands[b['x'] // band_size].append(b)
        for band_idx in sorted(bands.keys()):
            bp = bands[band_idx]
            cx = int(sum((b['x'] for b in bp)) / len(bp))
            cy = int(sum((b['y'] for b in bp)) / len(bp))
            lines.append(f'<g{rot_attr(cx, droop_deg, pivot)}>')
            lines.append(f'<g id="wind-tip-{band_idx}">')
            lines.append(_wind_animate(cx, cy, round(3.0 * wind, 2), 3.5, band_idx * 0.2))
            lines.extend(pixel_paths(bp, thickness['tip'], branch_color))
            lines.append('</g>')
            lines.append('</g>')
    return '\n'.join(lines)

def render_fruit(branches: list, total: int, ripe: int=0, droop_deg: float=0.0, pivot: tuple=(0, 0)) -> str:
    """Red orbs are contribution milestones; orange ones are ripe fruit from recently merged PRs."""
    count = fruit_count(total)
    ripe = min(ripe, 6)
    if count + ripe == 0 or not branches:
        return '<!-- No fruit yet -->'
    tips = [b for b in branches if b['depth'] >= 4]
    if not tips:
        return '<!-- No tips for fruit -->'
    tips.sort(key=lambda b: (b['x'], b['y']))
    rng = random.Random(7)
    rng.shuffle(tips)
    lines = ['<!-- Fruit -->']
    for i, tip in enumerate(tips[:count + ripe]):
        href = 'fruit' if i < count else 'fruit_ripe'
        lines.append(f'<g{rot_attr(tip["x"], droop_deg, pivot)}>' + _use(href, tip['x'], tip['y'] + PIXEL_SIZE * 2) + '</g>')
    return '\n'.join(lines)

def render_leaves(leaves: list, canvas: dict, wind: float=1.0, droop_deg: float=0.0, pivot: tuple=(0, 0), depth_at: dict=None) -> str:
    if not leaves:
        return '<!-- No leaves -->'
    depth_at = depth_at or {}
    lines = ['<!-- Leaves -->']
    band_w = canvas['w'] // 6
    bands = defaultdict(list)
    for leaf in leaves:
        x, y = (leaf['x'], leaf['y'])
        color_idx = leaf.get('color_idx', 0)
        delay = leaf.get('delay', 0)
        duration = leaf.get('duration', 3.0)
        drift = leaf.get('drift', 0)
        state = leaf.get('state', 'attached')
        deep = depth_at.get((x, y), 0) >= 4
        if state == 'attached':
            bands[(x // band_w, deep)].append(_use(f'leaf-{color_idx}', x, y))
        elif state == 'falling':
            drop = max(0, canvas['grass_y'] - y)
            fade_begin = round(delay + duration * 0.8, 2)
            lines.append('<g>')
            lines.append(_fall_animate(x, y, drift, drop, duration, delay))
            lines.append(_opacity_animate(1, 0, round(duration * 0.2, 2), fade_begin))
            lines.append(_use(f'leaf-{color_idx}', 0, 0))
            lines.append('</g>')
        elif state == 'regrowing':
            lines.append(f'<g{rot_attr(x, droop_deg, pivot) if deep else ""}>')
            lines.append(f'<animate attributeName="opacity" from="0" to="1" dur="2s" begin="{delay}s" fill="freeze"/>')
            lines.append(_use(f'leaf-{color_idx}', x, y))
            lines.append('</g>')
    for (band, deep), uses in sorted(bands.items()):
        cx = band * band_w + band_w // 2
        lines.append(f'<g{rot_attr(cx, droop_deg, pivot) if deep else ""}><g>')
        lines.append(_wind_animate(cx, pivot[1], round(2.2 * wind, 2), round(3.2 + band * 0.3, 1), round(band * 0.25, 2)))
        lines.extend(uses)
        lines.append('</g></g>')
    return '\n'.join(lines)

WITHERED_COLOR = '#6b5b45'

GREENS = {'g': '#4f8f3f', 'G': '#2f6b2a'}
FLOWER_PALETTES = {'flower_blue': {'p': '#4a8cff', 'P': '#a9cdff', 'd': '#2f5fc4', 'c': '#f2e6a0', 'C': '#c9a227'}, 'flower_pink': {'p': '#e91e8c', 'P': '#ff9ec7', 'd': '#a8125f', 'c': '#ffd1e6', 'C': '#c9a227'}, 'flower_yellow': {'p': '#f5c518', 'P': '#ffe98a', 'd': '#c99a0e', 'c': '#7a4a1a', 'C': '#4a2a0e'}, 'flower_lavender': {'p': '#9b7fd4', 'P': '#d4c4f5', 'd': '#6a4fa8', 'c': '#e8dcff', 'C': '#6a4fa8'}, 'flower_orange': {'p': '#ff7a2b', 'P': '#ffb070', 'd': '#c24f0a', 'c': '#3a2a1a', 'C': '#1a120a'}, 'flower_purple': {'p': '#9b59b6', 'P': '#d7a8ea', 'd': '#6c3483', 'c': '#ffe066', 'C': '#c9a227'}}
WILT_PALETTE = {'p': '#8a7560', 'P': '#a08f78', 'd': '#6b5b45', 'c': '#5a4a38', 'C': '#4a3a2a', 'g': '#7a7a4a', 'G': '#6b6b3f'}
FLOWER_ROWS = {'flower_blue': ['.P.p.P.', '..pPp..', '.pPcPp.', '..pPp..', '...g...', '...g...', '..Gg...', '...gG..'], 'flower_pink': ['.p.P.p.', '.ppPpp.', '.pPPPp.', '..pPp..', '...g...', '...g...', '.GGg...', '...gGG.'], 'flower_yellow': ['..PpP..', '.PpcpP.', 'PpcCcpP', '.PpcpP.', '..PpP..', '...g...', '...g...', '..Gg...', '...gG..'], 'flower_lavender': ['...P...', '..pPp..', '...p...', '..pPp..', '...p...', '..pPp..', '...g...', '...g...', '..Gg...', '...gG..'], 'flower_orange': ['.pP.Pp.', 'pPPpPPp', 'pPpdpPp', '.pPdPp.', '..ppp..', '...g...', '...g...', '..Gg...', '...gG..'], 'flower_purple': ['.P...P.', '.pP.Pp.', '..pPp..', '.pPcPp.', '..pdp..', '...g...', '...g...', '..Gg...', '...gG..']}
BUD_ROWS = ['...p...', '..pPp..', '..GpG..', '...g...', '...g...', '..Gg...', '...gG..']
SNOWDROP_ROWS = ['...g...', '..wgw..', '.wWwWw.', '..wWw..', '...g...', '...g...', '..Gg...', '...gG..']
SNOWDROP_PALETTE = {'w': '#e8f0f8', 'W': '#ffffff', 'g': '#5a9a5a', 'G': '#3a7a4a'}
MUSHROOM_ROWS = ['..RwR..', '.RRRwR.', 'RwRRRRR', '.sssss.', '...S...', '...S...']
MUSHROOM_PALETTE = {'R': '#d32f2f', 'w': '#fff5e6', 's': '#c9b99a', 'S': '#efe3cf'}
FRUIT_ROWS = ['..sl.', '.rRRr', 'rwRRr', 'rRRRd', '.rdd.']
FRUIT_PALETTES = {'fruit': {'r': '#c62828', 'R': '#e53935', 'w': '#ff9a8f', 'd': '#8e1b1b', 's': '#5d3a1a', 'l': '#4a8a3a'}, 'fruit_ripe': {'r': '#e8731a', 'R': '#ff9a3c', 'w': '#ffd49a', 'd': '#b34d0c', 's': '#5d3a1a', 'l': '#4a8a3a'}}

def sprite_triples(rows: list, palette: dict, flip: bool=False, anchor: str='bottom') -> list:
    """ASCII sprite to (dx, dy, colour) pixels. 'bottom': last row on dy=0; 'center': middle on origin."""
    n = len(rows)
    centre_x = len(rows[0]) // 2
    pixels = []
    for i, row in enumerate(rows):
        dy = (i - n // 2) * PIXEL_SIZE if anchor == 'center' else -(n - 1 - i) * PIXEL_SIZE
        for j, ch in enumerate(row):
            if ch in palette:
                dx = ((centre_x - j) if flip else (j - centre_x)) * PIXEL_SIZE
                pixels.append((dx, dy, palette[ch]))
    return pixels

def merge_runs(pixels: list) -> list:
    """Horizontal runs of one colour become one rect: (dx, dy, width, colour)."""
    by_row = defaultdict(list)
    for dx, dy, color in pixels:
        by_row[(dy, color)].append(dx)
    rects = []
    for (dy, color), xs in by_row.items():
        xs.sort()
        start = prev = xs[0]
        for x in xs[1:]:
            if x != prev + PIXEL_SIZE:
                rects.append((start, dy, prev - start + PIXEL_SIZE, color))
                start = x
            prev = x
        rects.append((start, dy, prev - start + PIXEL_SIZE, color))
    return rects

def plant_pixels(plant: dict, night: bool) -> list:
    """Pixels for a plant's life stage as (dx, dy, colour). Flowers close at night, wilt and brown at the end."""
    ptype = plant.get('type', 'sprout')
    stage = plant.get('growth_stage', 'mature')
    kind = plant.get('kind', 'ground_plant')
    flip = (plant['x'] // PIXEL_SIZE) % 2 == 1
    extra = (plant['x'] // PIXEL_SIZE) % 3
    if kind == 'flower' and ptype in FLOWER_ROWS:
        palette = dict(GREENS, **FLOWER_PALETTES[ptype])
        rows = FLOWER_ROWS[ptype]
        if stage == 'bud' or (night and stage != 'wilting'):
            rows = BUD_ROWS
        elif stage == 'wilting':
            palette = dict(WILT_PALETTE)
        rows = rows[:-3] + [rows[-3]] * extra + rows[-3:]
        return sprite_triples(rows, palette, flip)
    if ptype == 'snowdrop':
        palette = dict(WILT_PALETTE, w=WILT_PALETTE['P'], W=WILT_PALETTE['P']) if stage == 'withered' else SNOWDROP_PALETTE
        return sprite_triples(SNOWDROP_ROWS, palette, flip)
    if ptype == 'mushroom':
        if stage == 'withered':
            palette = {'R': '#8a6a5a', 'w': '#b8a890', 's': '#8a7a62', 'S': '#a89878'}
        else:
            palette = MUSHROOM_PALETTE
        return sprite_triples(MUSHROOM_ROWS, palette, flip)
    if ptype not in PLANT_PIXEL_SHAPES:
        ptype = 'sprout'
    shape = PLANT_PIXEL_SHAPES[ptype]
    color = PLANT_COLORS.get(ptype, '#4a7c59')
    if kind == 'ground_plant':
        if stage == 'withered':
            color = WITHERED_COLOR
        elif stage == 'sprout':
            shape, color = (PLANT_PIXEL_SHAPES['sprout'][:2], PLANT_COLORS['sprout'])
        elif stage == 'growing':
            shape = [p for p in shape if p[1] >= -8]
    return [(ox, oy, color) for ox, oy in shape]

def render_plants(plants: list, canvas: dict, tod: str='day', wind: float=1.0) -> str:
    if not plants:
        return '<!-- No plants -->'
    lines = ['<!-- Plants -->']
    night = tod == 'night'
    for i, plant in enumerate(plants):
        px, gy = (plant['x'], canvas['grass_y'])
        dead = plant.get('growth_stage') in ('withered', 'wilting')
        lines.append('<g>')
        if not dead:
            lines.append(_wind_animate(px, gy, round(1.5 * wind, 2), 4 + i % 3, round(i * 0.3, 2)))
        lines.append(f'<ellipse cx="{px + 2}" cy="{gy + 3}" rx="7" ry="1.5" fill="#000000" opacity="0.25"/>')
        for dx, dy, w, color in merge_runs(plant_pixels(plant, night)):
            lines.append(_rect(px + dx, gy + dy, w, PIXEL_SIZE, color))
        lines.append('</g>')
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
    tips = [b for b in branches if b['depth'] >= 4]
    if not tips:
        return '<!-- No branches for creatures -->'
    lines = ['<!-- Creatures -->']
    for creature, req in CREATURE_UNLOCKS.items():
        if total < req['min_contributions']:
            continue
        if req['seasons'] and season not in req['seasons']:
            continue
        if req['time'] and tod not in req['time']:
            continue
        tip = tips[zlib.crc32(creature.encode()) % len(tips)]
        cx, cy = (tip['x'], tip['y'] - 8)
        color = CREATURE_COLORS[creature]
        lines.append(f'<g class="creature-{creature}">')
        if creature == 'butterfly':
            dx, dy = (rng.randint(-20, 20), rng.randint(-15, 15))
            lines.append(f'  <animateTransform attributeName="transform" type="translate" values="0 0;{dx} {dy};0 0" dur="{rng.randint(4, 8)}s" repeatCount="indefinite"/>')
        else:
            lines.append(f'  <animateTransform attributeName="transform" type="translate" values="0 0;0 -2;0 0;0 1;0 0" dur="{round(rng.uniform(2, 4), 1)}s" begin="{round(rng.uniform(0, 3), 2)}s" repeatCount="indefinite"/>')
        for ox, oy in CREATURE_PIXEL_SHAPES[creature]:
            lines.append('  ' + _rect(cx + ox, cy + oy, PIXEL_SIZE, PIXEL_SIZE, color))
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
    activity = state.get('activity', {})
    season = get_season()
    tod = get_time_of_day()
    inactive = activity.get('inactive_days', 0)
    score = activity.get('recent_score', 0)
    stars = state.get('stars', 0)
    droop = min(0.7, max(0, inactive - 2) * 0.1)
    droop_deg = min(3.0, max(0, inactive - 2) * 0.4)
    wind = 0.6 + min(score, 20) / 20 * 0.9
    leaf_colors = [blend_hex(c, '#8a8070', droop) for c in SEASON_LEAF_COLORS[season]]
    palette = TIME_PALETTES[tod]
    seed = state.get('seed', 0)
    centers = canvas.get('patches') or patch_centers(seed, canvas)
    star_rng = random.Random(total + 42)
    weather_rng = random.Random(total + int(datetime.now(timezone.utc).timestamp()) // 3600)
    scale_params = compute_scale_params(total)
    pivot = (canvas['trunk_base_x'], canvas['trunk_base_y'] - scale_params['initial_length'])
    depth_at = {}
    for b in branches:
        key = (b['x'], b['y'])
        depth_at[key] = max(depth_at.get(key, 0), b['depth'])
    parts = []
    cw, ch = (canvas['w'], canvas['h'])
    parts.append(f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {cw} {ch}" width="{cw}" height="{ch}" preserveAspectRatio="xMidYMid meet">')
    parts.append(build_defs(leaf_colors))
    parts.append(render_sky(tod, palette, canvas))
    parts.append(render_stars(star_rng, tod, canvas))
    parts.append(render_weather(tod, season, score, weather_rng, canvas))
    if activity.get('celebrate_24h', 0) > 0:
        parts.append(render_rainbow(tod, canvas))
    parts.append(render_ground(season, state.get('contribution_days', {}), canvas))
    if inactive >= 7:
        parts.append(render_frost(canvas, weather_rng))
    parts.append(render_roots(total, palette, canvas))
    parts.append(render_branches(branches, scale_params, palette, canvas, wind, droop_deg, pivot))
    parts.append(render_moss(total, scale_params, canvas))
    parts.append(render_leaves(leaves, canvas, wind, droop_deg, pivot, depth_at))
    parts.append(render_fruit(branches, total, activity.get('merged_prs', 0), droop_deg, pivot))
    parts.append(render_grass(season, seed, canvas, centers, total, wind, 'back'))
    parts.append(render_plants(plants, canvas, tod, wind))
    parts.append(render_grass(season, seed, canvas, centers, total, wind, 'front'))
    parts.append(render_bees(plants, tod, season, canvas, random.Random(seed + 13)))
    parts.append(render_creatures(branches, total, season, tod, random.Random(seed + 7)))
    parts.append(render_butterflies(stars, tod, season, random.Random(seed + 11), canvas))
    tint = {'night': ('#0b1236', 0.38), 'dusk': ('#3a1a30', 0.14), 'dawn': ('#2a2050', 0.12)}.get(tod)
    if tint:
        parts.append(_rect(0, 0, cw, ch, tint[0], f' opacity="{tint[1]}"'))
    parts.append(render_fireflies(tod, season, random.Random(seed + 17), canvas, 10 + min(stars, 12)))
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

def target_plants(total: int) -> int:
    for limit, count in ((50, 1), (200, 3), (500, 5), (1000, 8), (2000, 12)):
        if total < limit:
            return count
    return 15

def run(username: str, token: str, args, initial: bool=False) -> None:
    print(f"[gitwood] {('Initial' if initial else 'Update')} run for @{username} (tz={TZ})")
    state = load_state(args.state)
    fresh = initial or not state.get('username') or not state['tree'].get('branches')
    state['seed'] = zlib.crc32(username.encode())
    state['timezone'] = str(TZ)
    print('[gitwood] Fetching recent events via REST API...')
    try:
        events = fetch_rest_events(username, token)
    except Exception as e:
        print(f'[gitwood] REST fetch failed, using calendar only: {e}', file=sys.stderr)
        events = []
    print('[gitwood] Refreshing contributions via GraphQL...')
    try:
        gql_data = fetch_graphql(username, token, state)
        total = gql_data['total_contributions']
        yearly_cache = gql_data['yearly_cache']
        state['contribution_days'] = gql_data.get('days', {})
    except Exception as e:
        if fresh:
            print(f'[gitwood] GraphQL fetch failed: {e}', file=sys.stderr)
            sys.exit(1)
        print(f'[gitwood] GraphQL refresh failed (using cached): {e}')
        total = state.get('total_contributions', 0)
        yearly_cache = state.get('yearly_cache', {})
    try:
        weights = fetch_languages(username, token)
        if weights:
            state['languages'] = weights
    except Exception as e:
        print(f'[gitwood] Language fetch failed (using cached): {e}')
    try:
        state['stars'] = fetch_stars(username, token)
    except Exception as e:
        print(f'[gitwood] Star fetch failed (using cached): {e}')
    langs = state.get('languages', {})
    days = merge_days(compute_activity_score(events), state['contribution_days'])
    streak = check_streak(days)
    inactive = days_inactive(days)
    score = recent_score(days)
    rolling_avg = compute_rolling_avg(days)
    extra = summarize_events(events)
    print(f'[gitwood] Score: {score}, streak: {streak}d, inactive: {inactive}d, languages: {langs}')
    if streak < min(STREAK_MILESTONES):
        state['activity']['last_high_day'] = None
    state = update_activity(state, streak, score, last_high_day=None, inactive_days=inactive, **extra)
    old_canvas = state.get('canvas')
    canvas = compute_canvas_size(total, max(len(state['tree'].get('plants', [])), target_plants(total) if fresh else 0))
    if old_canvas and old_canvas['h'] != canvas['h']:
        dy = canvas['trunk_base_y'] - old_canvas['trunk_base_y']
        print(f"[gitwood] Canvas resized ({old_canvas['h']} -> {canvas['h']})! Shifting Y by {dy}px")
        for l in state['tree'].get('leaves', []):
            l['y'] += dy
    canvas['patches'] = patch_centers(state['seed'], canvas)
    state['canvas'] = canvas
    old_total = state.get('total_contributions', 0)
    state = update_metadata(state, total, username, yearly_cache)
    branches = build_branches(total, canvas, state['seed'])
    state['tree']['branches'] = branches
    print(f'[gitwood] Tree: {old_total} -> {total} contributions, {len(branches)} branch pixels')
    season = get_season()
    anchors = collect_leaf_anchors(branches)
    rng = random.Random(total + int(datetime.now(timezone.utc).timestamp()) % 10000)
    vitality = 1.0 - min(0.7, max(0, inactive - 2) * 0.1)
    leaves, completed_falling = evolve_leaves(state['tree'].get('leaves', []), anchors, score, total, rng, season, vitality)
    state['tree']['leaves'] = leaves
    print(f'[gitwood] Leaves: {len(leaves)} active, {len(completed_falling)} fell | season={season}')
    if fresh and not state['tree'].get('plants'):
        state['tree']['plants'] = seed_initial_ecosystem(total, rng, canvas, langs)
    if state.get('layout', 0) < 2:
        placed = []
        for plant in state['tree']['plants']:
            x = free_x(placed, canvas, rng)
            if x is None:
                continue
            if plant.get('kind') != 'tree_seedling':
                # refresh the pre-habitat plants so the meadow follows the season and language mix
                ptype, kind = pick_plant(season, rng, langs)
                age = rng.randint(3, 14) if kind == 'flower' else rng.randint(0, 25)
                plant = make_plant(x, ptype, kind, canvas, (datetime.now(timezone.utc) - timedelta(days=age)).isoformat())
            placed.append(dict(plant, x=x))
        state['tree']['plants'] = placed
        state['layout'] = 2
    state['tree']['plants'] = evolve_plants(state, score, rolling_avg, completed_falling, rng, canvas, season, langs)
    print(f"[gitwood] Plants: {len(state['tree']['plants'])}")
    save_state(state, args.state)
    generate_svg(state, canvas, args.output)

def run_initial(username: str, token: str, args) -> None:
    run(username, token, args, initial=True)

def run_update(username: str, token: str, args) -> None:
    run(username, token, args)

def run_demo(args):
    total = int(args.username) if args.username.isdigit() else 500
    demo_rng = random.Random(total)
    state = empty_state('demo-user')
    state['seed'] = zlib.crc32(b'demo-user')
    state['total_contributions'] = total
    state['languages'] = {'Python': 12, 'JavaScript': 6, 'TypeScript': 4, 'Dart': 3}
    state['stars'] = 5
    today = now_local().date()
    state['contribution_days'] = {day_key(today - timedelta(days=i)): demo_rng.randint(1, 10) for i in range(365) if demo_rng.random() > 0.3}
    streak = check_streak(state['contribution_days'])
    score = recent_score(state['contribution_days'])
    state = update_activity(state, streak, score, last_high_day=None, inactive_days=days_inactive(state['contribution_days']), merged_prs=2, celebrate_24h=1)
    canvas = compute_canvas_size(total, target_plants(total))
    canvas['patches'] = patch_centers(state['seed'], canvas)
    state['canvas'] = canvas
    branches = build_branches(total, canvas, state['seed'])
    state['tree']['branches'] = branches
    season = get_season()
    rng = random.Random(total + 1)
    leaves, _ = evolve_leaves([], collect_leaf_anchors(branches), score, total, rng, season)
    state['tree']['leaves'] = leaves
    state['tree']['plants'] = seed_initial_ecosystem(total, rng, canvas, state['languages'])
    save_state(state, args.state)
    generate_svg(state, canvas, args.output)
    print(f"[gitwood] Demo generated: {total} contributions, {len(branches)} branches, {len(leaves)} leaves, {len(state['tree']['plants'])} plants")

def main():
    global TZ
    args = parse_args()
    if args.timezone:
        TZ = ZoneInfo(args.timezone)
    token = args.token or os.environ.get('GH_TOKEN', '')
    if not token and not args.demo:
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
