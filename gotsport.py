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

def refresh():
    CACHE.mkdir(parents=True, exist_ok=True)
    mapping = load_mapping()
    status = load_status()
    status["last_attempt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    today = dt.date.today()
    fetched = 0
    try:
        for league_id, m in mapping.items():
            for gs_id in m["gotsport"]:
                j = _get(f"team_ranking_data?team_id={gs_id}")
                fetched += 1
                tr = j.get("team_ranking_data") or {}
                team = {
                    "team_id": gs_id,
                    "team_name": tr.get("team_name"),
                    "events": [{
                        "id": e["id"], "name": e["name"],
                        "start": _date((e.get("start_date_formatted") or {}).get("long")),
                        "end": _date((e.get("end_date_formatted") or {}).get("long")),
                    } for e in j.get("events") or []],
                    "flights": {ev: [x.get("schedule_group_id") for x in v.values()]
                                for ev, v in (tr.get("event_results_json") or {}).items()},
                }
                (CACHE / f"team_{gs_id}.json").write_text(json.dumps(team, indent=1))

                for e in team["events"]:
                    if not e["start"] or e["start"] < SINCE:
                        continue
                    for fl in team["flights"].get(str(e["id"]), []):
                        path = CACHE / f"flight_{e['id']}_{fl}.json"
                        end = dt.date.fromisoformat(e["end"] or e["start"])
                        if path.exists() and (today - end).days > RECHECK_DAYS:
                            continue
                        games = _get(f"event_ranking_data/flight_matches?flight_id={fl}&event_id={e['id']}")
                        fetched += 1
                        path.write_text(json.dumps([{
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


def split_counted(games, league_ids):
    """Count a tournament game only if its component of the tournament graph touches
    2+ league teams — i.e. it helps compare league teams to each other."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for g in games:
        a, b = find(g["home"]), find(g["away"])
        if a != b:
            parent[a] = b
    league_in = collections.defaultdict(set)
    for g in games:
        for side in ("home", "away"):
            if g[side] in league_ids:
                league_in[find(g[side])].add(g[side])
    counted = [g for g in games if len(league_in[find(g["home"])]) >= 2]
    return counted, [g for g in games if g not in counted]


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
