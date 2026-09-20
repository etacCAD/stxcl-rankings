#!/usr/bin/env python3
"""
rank.py — pull an AthleteOne flight's schedule/results and rank teams.

Evolved from athleteone_rank.py (see CLAUDE.md for the full method and the
connectivity rule). Differences from the original:
  - teams keyed by AthleteOne teamID, and built from the full schedule, so a
    team with zero completed games is still listed
  - friendlies excluded
  - CAP / LAM loosen automatically as games-per-team grows (CLAUDE.md §3)
  - ties detected and shared, never broken by noise
  - "seed pods", bridge games, and the date the graph connects are computed
    from the schedule instead of hard-coded

Usage (text report, same spirit as the original script):
  python3 rank.py 4260 41143
"""

import argparse
import collections
import json
import math
import re
import urllib.request

import numpy as np

API = "https://api.athleteone.com/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# --- model knobs -------------------------------------------------------------
HFA = 0.25     # home-field advantage, in goals (Massey)
BT_L2 = 0.35   # Bradley-Terry regularization
BT_HFA = 0.15  # Bradley-Terry home edge (logit)
SOS_W = 0.45   # weight on strength-of-schedule in adjusted PPG
BLEND = (0.45, 0.30, 0.25)  # adjPPG, Massey, BT
TIE_EPS = 0.05       # composite scores this close are a tie outright
TIE_EPS_SAME = 0.25  # ...or this close when W-L-D and GD are identical


def knobs(games_per_team):
    """Margin cap and ridge strength by sample size (CLAUDE.md §3 tuning schedule)."""
    if games_per_team < 6:
        return {"cap": 3.0, "lam": 1.0}
    if games_per_team < 9:
        return {"cap": 5.0, "lam": 0.5}
    return {"cap": 6.0, "lam": 0.25}
# -----------------------------------------------------------------------------


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


SUFFIX_RE = re.compile(r"\s*\bECNL\s+RL\s+STXCL\b\s*|\s*\bG\d{4}(?:/\d{2,4})?\b\s*")


def shorten(name):
    """Strip the repeated league/age suffix ("ECNL RL STXCL", "G2012/13") so output is readable.
    Works for any age group: 'AHFC CENTRAL ECNL RL STXCL G2012/13 I' -> 'AHFC CENTRAL I'."""
    out = SUFFIX_RE.sub(" ", name).replace("Soccer Club - ", "")
    return re.sub(r"\s{2,}", " ", out).strip()


def fetch(event_id, flight_id):
    meta = get(f"{API}/Event/get-flight-division-by-flightID/{flight_id}")["data"]
    sched = get(f"{API}/Event/get-schedules-by-flight/{event_id}/{flight_id}/0")["data"]
    try:
        standings = get(
            f"{API}/Event/get-standings-by-div-and-flight/"
            f"{meta['divisionID']}/{flight_id}/{event_id}"
        )["data"]
    except Exception:
        standings = None
    try:
        event = get(f"{API}/team/get-event-details-by-eventID/{event_id}")["data"]
    except Exception:
        event = {}
    return meta, sched, standings, event


def parse(sched):
    """-> (teams by id, played games, upcoming games). Completed iff flagText == 'Box Score'."""
    teams, played, upcoming = {}, [], []
    for m in sched:
        for side in ("home", "away"):
            tid = m[f"{side}teamID"]
            teams.setdefault(tid, {
                "id": tid,
                "name": shorten(m[f"{side}Team"]),
                "club": m.get(f"{side}TeamClub") or "",
                "logo": m.get(f"{side}ClubLogo") or "",
            })
        if m.get("friendly"):
            continue
        g = {
            "id": m["matchID"],
            "dt": m["gameDate"],
            "date": m["gameDate"][:10],
            "time": m.get("gameTimeText") or "",
            "venue": " · ".join(x for x in (m.get("complex"), m.get("venue")) if x),
            "home": m["hometeamID"],
            "away": m["awayteamID"],
            "hs": m.get("hometeamscore"),
            "as": m.get("awayteamscore"),
        }
        if m.get("flagText") == "Box Score":
            # scores of 0 on unplayed games are why we never filter on score
            if g["hs"] is not None and g["as"] is not None:
                played.append(g)
        else:
            upcoming.append(g)
    played.sort(key=lambda g: g["dt"])
    upcoming.sort(key=lambda g: g["dt"])
    return teams, played, upcoming


# --- results graph -----------------------------------------------------------

def components(team_ids, games):
    """Groups of league teams linked by results. Games may include outside teams
    ('gs:<id>' tournament opponents); they can link league teams but aren't returned."""
    parent = {t: t for t in team_ids}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for g in games:
        ra, rb = find(g["home"]), find(g["away"])
        if ra != rb:
            parent[ra] = rb
    groups = collections.defaultdict(set)
    for t in team_ids:
        groups[find(t)].add(t)
    return list(groups.values())


def bridges(team_ids, games):
    """Single games whose removal would split the results graph."""
    pairs = collections.Counter(frozenset((g["home"], g["away"])) for g in games)
    base = len(components(team_ids, games))
    out = []
    for pair, count in pairs.items():
        if count > 1:
            continue
        rest = [g for g in games if frozenset((g["home"], g["away"])) != pair]
        if len(components(team_ids, rest)) > base:
            out.append(tuple(pair))
    return out


def seed_pods(team_ids, played):
    """Components as of the last matchday on which the graph was still disconnected."""
    dates = sorted({g["date"] for g in played})
    last = None
    for d in dates:
        comps = components(team_ids, [g for g in played if g["date"] <= d])
        if len(comps) > 1:
            last = comps
    if len(components(team_ids, played)) > 1:
        last = components(team_ids, played)
    return last


def connects_on(team_ids, played, upcoming):
    """Date of the scheduled game that first joins the whole graph (None if already joined)."""
    games = list(played)
    if len(components(team_ids, games)) == 1:
        return None
    for g in upcoming:
        games.append(g)
        if len(components(team_ids, games)) == 1:
            return g["date"]
    return "not on the schedule"


# --- ratings -----------------------------------------------------------------

def result_pts(gf, ga):
    return 3 if gf > ga else (1 if gf == ga else 0)


def build(team_ids, games, extra=()):
    """games: league games (records, SOS, ratings). extra: weighted neutral-site games
    (counted tournament games) that feed Massey/BT only and may involve outside teams."""
    team_ids = sorted(team_ids)
    idx = {t: i for i, t in enumerate(team_ids)}
    n = len(team_ids)

    log = collections.defaultdict(list)  # team -> [(opp, gf, ga)]
    for g in games:
        log[g["home"]].append((g["away"], g["hs"], g["as"]))
        log[g["away"]].append((g["home"], g["as"], g["hs"]))

    rec = {}
    for t in team_ids:
        r = {"w": 0, "l": 0, "d": 0, "gf": 0, "ga": 0}
        for _, gf, ga in log[t]:
            r["gf"] += gf
            r["ga"] += ga
            r["w" if gf > ga else ("d" if gf == ga else "l")] += 1
        r["gp"] = len(log[t])
        r["gd"] = r["gf"] - r["ga"]
        r["pts"] = 3 * r["w"] + r["d"]
        r["ppg"] = r["pts"] / r["gp"] if r["gp"] else 0.0
        rec[t] = r

    # strength of schedule: mean opponent PPG, excluding games against this team
    for t in team_ids:
        vals = []
        for o, _, _ in log[t]:
            other = [result_pts(gf, ga) for x, gf, ga in log[o] if x != t]
            if other:
                vals.append(sum(other) / len(other))
        rec[t]["sos"] = sum(vals) / len(vals) if vals else 1.0
        rec[t]["adj"] = rec[t]["ppg"] + SOS_W * (rec[t]["sos"] - 1.0)

    gpt = 2 * len(games) / n if n else 0
    k = knobs(gpt)

    # rating nodes: league teams, then outside teams from counted tournament games
    all_games = [dict(g, w=1.0, neutral=False) for g in games] + list(extra)
    outside = sorted({g[s] for g in extra for s in ("home", "away")} - set(team_ids), key=str)
    nidx = {t: i for i, t in enumerate(team_ids + outside)}
    N = len(nidx)

    if all_games:
        rows = np.arange(len(all_games))
        hi = np.array([nidx[g["home"]] for g in all_games])
        ai = np.array([nidx[g["away"]] for g in all_games])
        margin = np.array([g["hs"] - g["as"] for g in all_games], dtype=float)
        wt = np.array([g.get("w", 1.0) for g in all_games])
        home = np.array([0.0 if g.get("neutral") else 1.0 for g in all_games])

        # Massey (weighted ridge, capped margin)
        sw = np.sqrt(wt)
        X = np.zeros((len(all_games), N))
        X[rows, hi] = sw
        X[rows, ai] = -sw
        y = (np.clip(margin, -k["cap"], k["cap"]) - HFA * home) * sw
        massey = np.linalg.solve(X.T @ X + k["lam"] * np.eye(N), X.T @ y)
        massey -= massey[:n].mean()

        # Bradley-Terry (weighted regularized logistic, gradient ascent), draws = 0.5
        res = np.where(margin > 0, 1.0, np.where(margin == 0, 0.5, 0.0))
        w = np.zeros(N)
        for _ in range(3000):
            p = 1 / (1 + np.exp(-(w[hi] - w[ai] + BT_HFA * home)))
            resid = (res - p) * wt
            grad = np.bincount(hi, resid, N) - np.bincount(ai, resid, N) - BT_L2 * w
            w += 0.1 * grad
        w -= w[:n].mean()
    else:
        massey = np.zeros(N)
        w = np.zeros(N)

    for t in team_ids:
        rec[t]["massey"] = float(massey[nidx[t]])
        rec[t]["bt"] = float(w[nidx[t]])

    def z(key):
        v = np.array([rec[t][key] for t in team_ids], dtype=float)
        return (v - v.mean()) / (v.std() + 1e-9)

    comp = BLEND[0] * z("adj") + BLEND[1] * z("massey") + BLEND[2] * z("bt")
    for t in team_ids:
        rec[t]["score"] = float(comp[idx[t]])
    return rec, {"games_per_team": gpt, **k}


def predict(rec, home, away, goals_per_team, neutral=False):
    """Score estimate: expected goals = league scoring level +/- half the Massey margin
    (plus home edge), independent Poisson for win/draw/loss. The score shown is the
    likeliest scoreline within the likeliest result, so a favorite never shows a draw."""
    margin = rec[home]["massey"] - rec[away]["massey"] + (0.0 if neutral else HFA)
    lh = max(0.15, goals_per_team + margin / 2)
    la = max(0.15, goals_per_team - margin / 2)
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(12)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(12)]
    grid = {(i, j): ph[i] * pa[j] for i in range(12) for j in range(12)}
    total = sum(grid.values())
    p_home = sum(p for (i, j), p in grid.items() if i > j) / total
    p_draw = sum(p for (i, j), p in grid.items() if i == j) / total
    p_away = max(0.0, 1 - p_home - p_draw)
    pick = max((p_home, "home"), (p_draw, "draw"), (p_away, "away"))[1]
    fits = {"home": lambda i, j: i > j, "draw": lambda i, j: i == j, "away": lambda i, j: i < j}[pick]
    score = max((s for s in grid if fits(*s)), key=grid.get)
    return {"home": p_home, "draw": p_draw, "away": p_away, "xg": (lh, la), "score": score, "pick": pick}


def is_tie(a, b):
    diff = abs(a["score"] - b["score"])
    if diff < TIE_EPS:
        return True
    same = (a["w"], a["l"], a["d"], a["gd"]) == (b["w"], b["l"], b["d"], b["gd"])
    return same and diff < TIE_EPS_SAME


def ranked(ids, rec, key=None, tie=is_tie):
    """-> [(rank, team_id)] with shared ranks for ties."""
    key = key or (lambda t: (-rec[t]["score"], -rec[t]["pts"], -rec[t]["gd"]))
    order = sorted(ids, key=key)
    out = []
    anchor = None  # first team of the current tie group; compare to it so ties don't chain
    for i, t in enumerate(order):
        if anchor is not None and tie(rec[anchor], rec[t]):
            out.append((out[-1][0], t))
        else:
            anchor = t
            out.append((i + 1, t))
    return out


def points_ranked(ids, rec):
    same = lambda a, b: (a["pts"], a["gd"], a["gf"]) == (b["pts"], b["gd"], b["gf"])
    return ranked(ids, rec, key=lambda t: (-rec[t]["pts"], -rec[t]["gd"], -rec[t]["gf"]), tie=same)


def cross_check(standings, rec):
    """-> list of team ids whose computed points differ from the official table."""
    if not standings:
        return None
    off = {}
    for grp in standings:
        for row in grp.get("teamStandings", []):
            off[row["teamID"]] = row["standingpoints"]
    return [t for t in rec if t in off and off[t] != rec[t]["pts"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event_id")
    ap.add_argument("flight_id")
    args = ap.parse_args()

    meta, sched, standings, _ = fetch(args.event_id, args.flight_id)
    teams, played, upcoming = parse(sched)
    name = lambda t: teams[t]["name"]
    print(f"{meta['divisionName']} / {meta['flightName']}   teams {len(teams)}   "
          f"scheduled {len(sched)}   played {len(played)}")
    rec, k = build(teams, played)
    comps = components(teams, played)
    print(f"CONNECTIVITY: {len(comps)} component(s)   knobs {k}")
    if len(comps) > 1:
        print("  *** DISCONNECTED — only within-pod order is evidence.")
        for i, c in enumerate(sorted(comps, key=lambda c: min(map(name, c))), 1):
            print(f"  pod {i}: " + ", ".join(name(t) for t, in
                  [(u,) for _, u in ranked(c, rec)]))
    for r, t in ranked(teams, rec):
        x = rec[t]
        print(f"{r:<3} {name(t):<26} {x['w']}-{x['l']}-{x['d']}  {x['pts']:>2} pts  "
              f"GD {x['gd']:+d}  SOS {x['sos']:.2f}  score {x['score']:+.2f}")
    bad = cross_check(standings, rec)
    print("OFFICIAL STANDINGS CROSS-CHECK: " + (
        "unavailable" if bad is None else "MATCH" if not bad else f"MISMATCH {[name(t) for t in bad]}"))


if __name__ == "__main__":
    main()
