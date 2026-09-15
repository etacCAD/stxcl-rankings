#!/usr/bin/env python3
"""
gotsport.py — tournament games for league teams, from GotSport's rankings API.

  python3 gotsport.py            # refresh the cache (slow on purpose: PAUSE seconds per request)

Only games on or after SINCE are used. Team mapping lives in gotsport/teams.json.
Responses are cached under gotsport/cache/ and committed, so if GotSport blocks a
run (Cloudflare 403s bursts and some datacenter IPs) the site uses the last good data.

Endpoints (undocumented; they power rankings.gotsport.com):
  team_ranking_data?team_id=<team_id>                      -> events + flight ids
  event_ranking_data/flight_matches?flight_id=&event_id=   -> every game in that flight
"""

import collections
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import rank

API = "https://system.gotsport.com/api/v1"
SINCE = "2026-08-01"
PAUSE = 8          # seconds before every request
RECHECK_DAYS = 10  # re-pull a flight until this long after its event ends (late score entry)
TOURN_W = 0.5      # model weight of a tournament game relative to a league game
MAX_CHAIN = 3      # count a tournament game only if it sits on a chain of <= this many games
                   # between two different league teams (1 = they met, 2 = shared opponent,
                   # 3 = their opponents played each other)
OUTSIDE_FRESH_DAYS = 6  # re-pull an outside opponent's events at most this often

DIR = pathlib.Path(__file__).resolve().parent / "gotsport"
CACHE = DIR / "cache"
STATUS = DIR / "status.json"
HEADERS = {
    "User-Agent": rank.UA,
    "Accept": "application/json",
    "Origin": "https://rankings.gotsport.com",
    "Referer": "https://rankings.gotsport.com/",
}


class Blocked(Exception):
    pass


def _get(path):
    time.sleep(PAUSE)
    req = urllib.request.Request(f"{API}/{path}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise Blocked(f"HTTP {e.code} on {path}")
        raise


def _date(long_text):
    """'Friday, August 14, 2026' -> '2026-08-14'."""
    try:
        return dt.datetime.strptime(long_text, "%A, %B %d, %Y").date().isoformat()
    except (TypeError, ValueError):
        return None


def load_mapping():
    raw = json.loads((DIR / "teams.json").read_text())
    return {int(k): v for k, v in raw.items() if not k.startswith("_")}


def load_status():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


# --- refresh -----------------------------------------------------------------

def _pull_team(gs_id, today, fresh_days=None):
    """Cache one team's events and every flight it played since SINCE. Returns requests made."""
    path = CACHE / f"team_{gs_id}.json"
    if fresh_days is not None and path.exists():
        pulled = json.loads(path.read_text()).get("pulled")
        if pulled and (today - dt.date.fromisoformat(pulled)).days < fresh_days:
            return 0
    j = _get(f"team_ranking_data?team_id={gs_id}")
    fetched = 1
    tr = j.get("team_ranking_data") or {}
    team = {
        "team_id": gs_id,
        "team_name": tr.get("team_name"),
        "pulled": today.isoformat(),
        "events": [{
            "id": e["id"], "name": e["name"],
            "start": _date((e.get("start_date_formatted") or {}).get("long")),
            "end": _date((e.get("end_date_formatted") or {}).get("long")),
        } for e in j.get("events") or []],
        "flights": {ev: [x.get("schedule_group_id") for x in v.values()]
                    for ev, v in (tr.get("event_results_json") or {}).items()},
    }
    path.write_text(json.dumps(team, indent=1))

    for e in team["events"]:
        if not e["start"] or e["start"] < SINCE:
            continue
        for fl in team["flights"].get(str(e["id"]), []):
            fpath = CACHE / f"flight_{e['id']}_{fl}.json"
            end = dt.date.fromisoformat(e["end"] or e["start"])
            if fpath.exists() and (today - end).days > RECHECK_DAYS:
                continue
            games = _get(f"event_ranking_data/flight_matches?flight_id={fl}&event_id={e['id']}")
            fetched += 1
            fpath.write_text(json.dumps([{
                "id": g["id"],
                "date": g.get("match_date"),
                "event_id": e["id"],
                "event": g.get("event_name") or e["name"],
                "division": g.get("division_name"),
                "home_id": (g.get("homeTeam") or {}).get("team_id"),
                "home_name": (g.get("homeTeam") or {}).get("full_name"),
                "away_id": (g.get("awayTeam") or {}).get("team_id"),
                "away_name": (g.get("awayTeam") or {}).get("full_name"),
                "hs": g.get("home_score"),
                "as": g.get("away_score"),
            } for g in games], indent=1))
    return fetched


def outside_opponents(games, league_ids):
    """GotSport team_ids of outside teams that played a league team."""
    out = set()
    for g in games:
        for a, b in (("home", "away"), ("away", "home")):
            if g[a] in league_ids and isinstance(g[b], str) and g[b].startswith("gs:"):
                out.add(int(g[b][3:]))
    return out


def refresh():
    CACHE.mkdir(parents=True, exist_ok=True)
    mapping = load_mapping()
    status = load_status()
    status["last_attempt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    today = dt.date.today()
    fetched = 0
    try:
        for m in mapping.values():
            for gs_id in m["gotsport"]:
                fetched += _pull_team(gs_id, today)
        # one step out: opponents' own games are what reveal league–X–Y–league chains
        for gs_id in sorted(outside_opponents(load_games(mapping), set(mapping))):
            fetched += _pull_team(gs_id, today, fresh_days=OUTSIDE_FRESH_DAYS)
        status.update(blocked=False, error=None,
                      last_success=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"))
    except Blocked as e:
        status.update(blocked=True, error=str(e))
    except Exception as e:  # network hiccup etc.: keep the cache, report it
        status.update(blocked=False, error=f"{type(e).__name__}: {e}")
    STATUS.write_text(json.dumps(status, indent=1))
    return status, fetched


# --- load for the model ------------------------------------------------------

def load_games(mapping=None):
    """Cached tournament games since SINCE, with league teams translated to AthleteOne ids.
    Non-league teams become 'gs:<team_id>' nodes."""
    mapping = mapping or load_mapping()
    to_league = {gs: lid for lid, m in mapping.items() for gs in m["gotsport"]}
    seen, games = set(), []
    for path in sorted(CACHE.glob("flight_*.json")):
        for g in json.loads(path.read_text()):
            if g["id"] in seen or g["hs"] is None or g["as"] is None or not g["date"] or g["date"] < SINCE:
                continue
            if g["home_id"] is None or g["away_id"] is None:
                continue
            seen.add(g["id"])
            node = lambda gid: to_league.get(gid, f"gs:{gid}")
            games.append({
                "id": f"gs{g['id']}", "dt": g["date"], "date": g["date"], "time": "",
                "venue": g["event"], "event": g["event"], "division": g["division"],
                "home": node(g["home_id"]), "away": node(g["away_id"]),
                "home_name": g["home_name"], "away_name": g["away_name"],
                "hs": g["hs"], "as": g["as"], "w": TOURN_W, "neutral": True,
            })
    games.sort(key=lambda g: g["dt"])
    return games


def split_counted(games, league_ids, max_chain=MAX_CHAIN):
    """Count a tournament game only if it lies on a chain of <= max_chain games between two
    different league teams, so it helps compare league teams without getting too removed."""
    games = list(games)
    inc = collections.defaultdict(list)  # node -> [(game index, other node)]
    for i, g in enumerate(games):
        inc[g["home"]].append((i, g["away"]))
        inc[g["away"]].append((i, g["home"]))

    keep = set()

    def walk(path, used):
        # simple paths only (no revisiting a team), stopping at the first other league team
        u = path[-1]
        if used and u in league_ids:
            keep.update(used)
            return
        if len(used) == max_chain:
            return
        for i, v in inc[u]:
            if v not in path:
                walk(path + [v], used + [i])

    for t in league_ids:
        if t in inc:
            walk([t], [])
    counted = [g for i, g in enumerate(games) if i in keep]
    return counted, [g for i, g in enumerate(games) if i not in keep]


def main():
    status, fetched = refresh()
    games = load_games()
    league = set(load_mapping())
    counted, _ = split_counted(games, league)
    teams = {g[s] for g in games for s in ("home", "away") if g[s] in league}
    print(f"gotsport: {fetched} requests  blocked={status.get('blocked')}  error={status.get('error')}")
    print(f"  {len(games)} tournament games since {SINCE}, {len(teams)} league teams, {len(counted)} counted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
