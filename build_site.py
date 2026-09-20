#!/usr/bin/env python3
"""
build_site.py — fetch the latest results, rank, and write a static site.

  python3 build_site.py                      # writes site/index.html + site/data.json
  python3 build_site.py --out /tmp/preview   # somewhere else
  python3 build_site.py --simulate-until 2026-10-31
        # dev only: fills unplayed games before that date with random scores,
        # to preview how the page looks once the pods connect

Snapshots of each matchday's ranking go in history/ so the page can show movement.
"""

import argparse
import collections
import datetime as dt
import html
import json
import pathlib
import random
import re
from zoneinfo import ZoneInfo

import gotsport
import rank

ROOT = pathlib.Path(__file__).resolve().parent
LEAGUES = json.loads((ROOT / "leagues.json").read_text())

# Set per league by use_league(). The first league keeps the original paths and URL,
# so the GU14 page and its saved history stay where they were.
EVENT_ID, FLIGHT_ID, SLUG, LEAGUE = 4260, 41143, "", {}
HIST = ROOT / "history"
TZ = ZoneInfo("America/Chicago")
PUBLIC_URL = ""


def use_league(cfg):
    """Point this module (and gotsport) at one league: ids, output slug, own data folders."""
    global EVENT_ID, FLIGHT_ID, SLUG, LEAGUE, HIST, PRED_FILE, PUBLIC_URL
    LEAGUE = cfg
    EVENT_ID, FLIGHT_ID, SLUG = cfg["event"], cfg["flight"], cfg.get("slug", "")
    HIST = (ROOT / "history" / SLUG) if SLUG else (ROOT / "history")
    PRED_FILE = HIST / "predictions.json"
    PUBLIC_URL = f"https://app.athleteone.com/public/event/{EVENT_ID}/schedules-standings/standings/{FLIGHT_ID}"
    gotsport.use_league(SLUG)


def render_nav():
    if len(LEAGUES) < 2:
        return ""
    links = []
    for cfg in LEAGUES:
        slug = cfg.get("slug", "")
        href = ("../" if SLUG else "") + (slug + "/" if slug else "")
        cur = ' class="cur"' if slug == SLUG else ""
        links.append(f'<a href="{e(href or "./")}"{cur}>{e(cfg["name"])}</a>')
    return '<nav class="leagues">' + "".join(links) + "</nav>"

e = html.escape


def nice_date(d, weekday=True):
    x = dt.date.fromisoformat(d)
    return x.strftime("%a %b %-d" if weekday else "%b %-d")


def signed(v):
    return f"{v:+d}" if v else "0"


# --- analysis ----------------------------------------------------------------

def analyse(meta, sched, standings, event, simulate_until=None, gs_games=()):
    teams, played, upcoming = rank.parse(sched)
    ids = set(teams)
    gs_games = list(gs_games)

    if simulate_until:
        rng = random.Random(7)
        keep = []
        for g in upcoming:
            if g["date"] <= simulate_until:
                played.append({**g, "hs": rng.choice([0, 0, 1, 1, 2, 3, 4]),
                               "as": rng.choice([0, 0, 1, 1, 2, 3])})
            else:
                keep.append(g)
        upcoming = keep
        played.sort(key=lambda g: g["dt"])

    # tournament games only count when they help compare league teams to each other
    counted, _ = gotsport.split_counted(gs_games, ids)
    linked = sorted(played + counted, key=lambda g: g["dt"])

    rec, k = rank.build(ids, played, counted)
    comps = rank.components(ids, linked)
    connected = len(comps) == 1
    name = lambda t: teams[t]["name"]

    pods = rank.seed_pods(ids, linked) or []
    pods = sorted(pods, key=lambda c: min(map(name, c)))
    pod_of = {t: i + 1 for i, c in enumerate(pods) for t in c}

    overall = rank.ranked(ids, rec)
    pts_rank = dict((t, r) for r, t in rank.points_ranked(ids, rec))
    model_rank = dict((t, r) for r, t in overall)

    pod_tables = []
    for i, c in enumerate(pods if not connected else [], 1):
        pod_tables.append({"pod": i, "rows": rank.ranked(c, rec)})

    # provisional tiers while disconnected: tier = finishing position within pod
    tiers = collections.defaultdict(list)
    for p in pod_tables:
        for r, t in p["rows"]:
            tiers[r].append(t)
    tiers = [(tier, sorted(ts, key=lambda t: -rec[t]["score"])) for tier, ts in sorted(tiers.items())]

    # pod-pair links (who has / hasn't met directly yet)
    links = []
    if len(pods) > 1:
        for a in range(1, len(pods) + 1):
            for b in range(a + 1, len(pods) + 1):
                between = lambda g: {pod_of[g["home"]], pod_of[g["away"]]} == {a, b}
                n_played = sum(1 for g in played if between(g))
                nxt = next((g["date"] for g in upcoming if between(g)), None)
                links.append({"a": a, "b": b, "played": n_played, "next": nxt})

    # model vs points table divergences
    flags = []
    mean_sos = sum(rec[t]["sos"] for t in ids) / len(ids)
    if connected:
        pairs = [(t, model_rank[t], pts_rank[t]) for t in ids]
        threshold = 4
    else:
        pairs = []
        for p in pod_tables:
            pr = dict((t, r) for r, t in rank.points_ranked([t for _, t in p["rows"]], rec))
            pairs += [(t, r, pr[t]) for r, t in p["rows"]]
        threshold = 2
    for t, mr, pr in pairs:
        if abs(mr - pr) < threshold or rec[t]["gp"] == 0:
            continue
        x = rec[t]
        up = mr < pr
        if up and x["sos"] - mean_sos >= 0.3:
            why = f"a tougher schedule — opponents average {x['sos']:.2f} pts/game elsewhere vs {mean_sos:.2f} league-wide"
        elif not up and mean_sos - x["sos"] >= 0.3:
            why = f"a softer schedule — opponents average {x['sos']:.2f} pts/game elsewhere vs {mean_sos:.2f} league-wide"
        elif up:
            why = f"winning margins (GD {signed(x['gd'])}) the points table doesn't reward"
        else:
            why = f"narrow results (GD {signed(x['gd'])}) behind the points"
        flags.append({"team": t, "model": mr, "points": pr, "why": why})
    flags.sort(key=lambda f: -abs(f["model"] - f["points"]))

    # movement vs previous matchday snapshot
    last_date = played[-1]["date"] if played else None
    prev = None
    hist = HIST
    if last_date and hist.exists() and not simulate_until:
        older = sorted(p for p in hist.glob("20*.json") if p.stem < last_date)
        if older:
            prev = json.loads(older[-1].read_text())

    # clubs with several teams
    clubs = collections.Counter(teams[t]["club"] for t in ids)
    big_clubs = []
    for club, n in clubs.most_common():
        if n < 3:
            break
        intra = sum(1 for g in played if teams[g["home"]]["club"] == teams[g["away"]]["club"] == club)
        big_clubs.append({"club": club, "teams": n, "intra": intra})

    today = dt.datetime.now(TZ).date().isoformat()
    ahead = [g for g in upcoming if g["date"] >= today]
    next_date = ahead[0]["date"] if ahead else None
    gpg = sum(g["hs"] + g["as"] for g in played) / (2 * len(played)) if played else 1.5
    return {
        "meta": meta, "event": event, "teams": teams, "rec": rec, "knobs": k,
        "played": played, "upcoming": upcoming, "scheduled": len(played) + len(upcoming),
        "connected": connected, "components": len(comps), "comps": comps, "pods": pods, "pod_of": pod_of,
        "pod_tables": pod_tables, "tiers": tiers, "links": links,
        "connects_on": rank.connects_on(ids, linked, upcoming),
        "bridges": rank.bridges(ids, linked) if connected else [],
        "gs_games": gs_games, "gs_counted": counted,
        "gs_status": gotsport.load_status(), "gs_mapping": gotsport.load_mapping(),
        "overall": overall, "pts_rank": pts_rank, "flags": flags,
        "prev": prev, "last_date": last_date, "next_date": next_date, "gpg": gpg,
        "cross_check": rank.cross_check(standings, rec) if not simulate_until else [],
        "big_clubs": big_clubs, "simulated": simulate_until,
    }


PRED_FILE = HIST / "predictions.json"  # replaced per league by use_league()


def load_predictions():
    try:
        return json.loads(PRED_FILE.read_text())
    except (OSError, ValueError):
        return {}


def update_predictions(a, store):
    """Save the latest estimate for every game that hasn't happened yet. Once a game's date
    arrives its estimate is frozen, so grading always uses what was predicted before kickoff."""
    store = dict(store)
    today = dt.datetime.now(TZ).date().isoformat()
    for g in a["upcoming"]:
        if g["date"] < today:
            continue
        p = rank.predict(a["rec"], g["home"], g["away"], a["gpg"])
        store[str(g["id"])] = {
            "made": today, "date": g["date"], "home": g["home"], "away": g["away"],
            "hs": p["score"][0], "as": p["score"][1], "pick": p["pick"],
            "p": [round(p["home"], 3), round(p["draw"], 3), round(p["away"], 3)],
            "linked": is_linked(a, g),
        }
    return store


def grade(a, store):
    rows = []
    for g in a["played"]:
        s = store.get(str(g["id"]))
        if not s:
            continue
        actual = "home" if g["hs"] > g["as"] else "draw" if g["hs"] == g["as"] else "away"
        hit = [actual == k for k in ("home", "draw", "away")]
        rows.append({
            "game": g, "est": s, "actual": actual,
            "right": s["pick"] == actual,
            "exact": (s["hs"], s["as"]) == (g["hs"], g["as"]),
            "brier": sum((p - o) ** 2 for p, o in zip(s["p"], hit)),
            "linked": s["linked"],
        })
    return rows


def snapshot(a):
    return {
        "date": a["last_date"],
        "connected": a["connected"],
        "rank": {str(t): r for r, t in a["overall"]},
        "score": {str(t): round(a["rec"][t]["score"], 3) for t in a["rec"]},
    }


# --- rendering ---------------------------------------------------------------

CSS = """
:root{--bg:#f6f5f1;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#e4e2dc;--soft:#efede7;
--accent:#0f5bd8;--away:#c96a12;--win:#127a45;--loss:#b3261e;--draw:#8a6d00;--warnbg:#fff4d6;--warnline:#e7c35a;
--okbg:#e4f4ea;--okline:#7cc49a;--infobg:#e6eefc;--infoline:#8fb0ec}
@media (prefers-color-scheme:dark){:root{--bg:#111317;--card:#1a1d23;--ink:#eceef2;--muted:#9aa1ad;
--line:#2b2f37;--soft:#22262d;--accent:#7aa8ff;--away:#f0a35e;--win:#4cc584;--loss:#ff7b72;--draw:#e0c050;
--warnbg:#2e2714;--warnline:#7a6420;--okbg:#15291e;--okline:#2f6b47;--infobg:#172238;--infoline:#34528a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 60px}
header .eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);display:flex;gap:8px;align-items:center}
header .eyebrow img{width:22px;height:22px;object-fit:contain}
h1{font-size:30px;line-height:1.15;margin:6px 0 4px;letter-spacing:-.01em}
h2{font-size:19px;margin:36px 0 12px}
h3{font-size:15px;margin:0 0 8px}
.sub{color:var(--muted);margin:0 0 16px}
.stats{display:flex;flex-wrap:wrap;gap:8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13px;color:var(--muted)}
.stat b{color:var(--ink);font-size:15px;display:block}
.banner{border:1px solid;border-radius:12px;padding:14px 16px;margin:22px 0 0}
.banner p{margin:4px 0 0}
.warn{background:var(--warnbg);border-color:var(--warnline)}
.ok{background:var(--okbg);border-color:var(--okline)}
.info{background:var(--infobg);border-color:var(--infoline)}
.sim{background:var(--loss);color:#fff;padding:6px 12px;border-radius:8px;font-weight:600;display:inline-block;margin-bottom:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,440px),1fr));gap:12px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:0}
th.l,td.l{text-align:left}
td.rk{font-weight:700;width:36px;text-align:left}
td.team{text-align:left;font-weight:600;white-space:normal;min-width:120px}
.tm{display:inline-flex;align-items:center;gap:8px;line-height:1.25}
.tm img{width:20px;height:20px;object-fit:contain;flex:none}
.muted{color:var(--muted)}
.small{font-size:13px}
.score{font-weight:700}
.up{color:var(--win);font-size:12px}.down{color:var(--loss);font-size:12px}.same{color:var(--muted);font-size:12px}
.tier td{background:var(--soft);font-size:12px;font-weight:600;color:var(--muted);text-align:left;text-transform:uppercase;letter-spacing:.05em}
.chip{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:99px;background:var(--soft);color:var(--muted);border:1px solid var(--line)}
.chip.link{background:var(--infobg);border-color:var(--infoline);color:var(--ink)}
.W{color:var(--win);font-weight:700}.L{color:var(--loss);font-weight:700}.D{color:var(--draw);font-weight:700}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:0 0 8px}
summary{cursor:pointer;padding:12px 14px;font-weight:600;list-style:none;display:flex;justify-content:space-between;gap:12px}
summary::-webkit-details-marker{display:none}
summary:after{content:"+";color:var(--muted)}
details[open] summary:after{content:"–"}
details .body{padding:0 14px 12px}
details .body td.l{white-space:normal}
details .body td.l.muted{white-space:nowrap}
.fx{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}
.fx:first-child{border-top:0}
.fx .h{text-align:right}.fx .res{font-weight:700;text-align:center;min-width:48px}
.fx .meta{grid-column:1/-1;text-align:center;font-size:12px;color:var(--muted);margin-top:-4px}
.win{font-weight:700}
ul.flags{margin:0;padding-left:18px}ul.flags li{margin:6px 0}
.orank{font-weight:700;color:var(--muted);font-variant-numeric:tabular-nums}
.chip.ok{background:var(--okbg);border-color:var(--okline);color:var(--ink)}
.chip.miss{color:var(--loss)}
.fx.pred{padding:10px 0;row-gap:4px}
.res.est{border:1px dashed var(--muted);border-radius:8px;padding:1px 8px;font-variant-numeric:tabular-nums}
.pct{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.pct.c{text-align:center}.pct.home-c{color:var(--accent)}.pct.away-c{color:var(--away)}
.probs{grid-column:1/-1;display:flex;height:6px;border-radius:99px;overflow:hidden;background:var(--soft)}
.probs span{display:block;height:100%}
.probs .ph{background:var(--accent)}.probs .pd{background:var(--line)}.probs .pa{background:var(--away)}
.fx.pred .meta{margin-top:2px}
nav.leagues{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
nav.leagues a{font-size:13px;color:var(--muted);text-decoration:none;border:1px solid var(--line);background:var(--card);padding:3px 11px;border-radius:99px}
nav.leagues a.cur{color:var(--ink);border-color:var(--muted);font-weight:600}
ol.est{list-style:none;margin:0;padding:0;display:grid;gap:8px}
ol.est li{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.est-rank{font-size:22px;font-weight:800;min-width:30px;line-height:1.2;font-variant-numeric:tabular-nums}
.est-body{flex:1;min-width:0}
.est-top{display:flex;flex-wrap:wrap;gap:2px 12px;align-items:baseline;font-weight:600}
.why{font-size:13.5px;margin-top:6px;line-height:1.45}
.chip.conf{margin-right:4px;color:var(--ink)}
.chip.clear{background:var(--okbg);border-color:var(--okline)}
.chip.lean{background:var(--infobg);border-color:var(--infoline)}
.chip.coin-flip,.chip.educated-guess{background:var(--warnbg);border-color:var(--warnline)}
.method{color:var(--muted);font-size:14px}.method li{margin:5px 0}
footer{margin-top:40px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:16px}
a{color:var(--accent)}
@media (max-width:560px){h1{font-size:24px}.hide-sm{display:none}td,th{padding:7px 5px}}
"""


def team_cell(a, t):
    tm = a["teams"][t]
    logo = f'<img src="{e(tm["logo"])}" alt="" loading="lazy">' if tm["logo"] else ""
    return f'<span class="tm">{logo}{e(tm["name"])}</span>'


def move_cell(a, t, r):
    prev = a["prev"]
    if not prev or str(t) not in prev["rank"]:
        return '<td class="hide-sm"></td>'
    d = prev["rank"][str(t)] - r
    if d > 0:
        return f'<td class="hide-sm"><span class="up">▲{d}</span></td>'
    if d < 0:
        return f'<td class="hide-sm"><span class="down">▼{-d}</span></td>'
    return '<td class="hide-sm"><span class="same">–</span></td>'


def rank_label(rows, r):
    return f"T{r}" if sum(1 for x, _ in rows if x == r) > 1 else str(r)


def full_row(a, t, label, show_pod=False, move=True):
    x = a["rec"][t]
    pod = f'<td class="hide-sm"><span class="chip">Pod {a["pod_of"][t]}</span></td>' if show_pod else ""
    return (
        f'<tr><td class="rk">{label}</td><td class="team">{team_cell(a, t)}</td>{pod}'
        f'<td>{x["w"]}-{x["l"]}-{x["d"]}</td><td class="score">{x["pts"]}</td>'
        f'<td class="hide-sm">{x["gf"]}-{x["ga"]}</td><td>{signed(x["gd"])}</td>'
        f'<td class="hide-sm">{x["sos"]:.2f}</td><td class="hide-sm">{x["massey"]:+.2f}</td>'
        f'<td class="hide-sm">{x["bt"]:+.2f}</td><td class="score">{x["score"]:+.2f}</td>'
        + (move_cell(a, t, a_rank(a, t)) if move else "") + "</tr>"
    )


def a_rank(a, t):
    return dict((u, r) for r, u in a["overall"])[t]


HEAD = ('<th class="l">#</th><th class="l">Team</th>{pod}<th>W-L-D</th><th>Pts</th>'
        '<th class="hide-sm">GF-GA</th><th>GD</th><th class="hide-sm" title="Opponents\' points per game in their other games">SOS</th>'
        '<th class="hide-sm">Massey</th><th class="hide-sm">BT</th><th>Rating</th>{move}')


def render_ranking(a):
    out = []
    if a["connected"]:
        rows = a["overall"]
        out.append('<h2>Power ranking</h2><div class="card scroll"><table><thead><tr>'
                   + HEAD.format(pod="", move='<th class="hide-sm">Δ</th>') + "</tr></thead><tbody>")
        for r, t in rows:
            out.append(full_row(a, t, rank_label(rows, r)))
        out.append("</tbody></table></div>")
        return "".join(out)

    out.append('<h2>Pod standings <span class="chip">reliable</span></h2>'
               '<p class="sub small">Every team has only played inside its pod so far, so these orders are backed by head-to-head evidence.</p>'
               '<div class="grid">')
    for p in a["pod_tables"]:
        rows = p["rows"]
        out.append(f'<div class="card scroll"><h3>Pod {p["pod"]}</h3><table><thead><tr>'
                   '<th class="l">#</th><th class="l">Team</th><th>W-L-D</th><th>Pts</th><th>GD</th><th>Rating</th>'
                   '</tr></thead><tbody>')
        for r, t in rows:
            x = a["rec"][t]
            out.append(f'<tr><td class="rk">{rank_label(rows, r)}</td><td class="team">{team_cell(a, t)}</td>'
                       f'<td>{x["w"]}-{x["l"]}-{x["d"]}</td><td class="score">{x["pts"]}</td>'
                       f'<td>{signed(x["gd"])}</td><td>{x["score"]:+.2f}</td></tr>')
        out.append("</tbody></table></div>")
    out.append("</div>")

    out.append('<h2>Overall tiers <span class="chip">low confidence</span></h2>'
               '<p class="sub small">Teams grouped by where they finished in their own pod. Order <em>inside</em> a tier compares teams '
               "that haven't played anyone in common — it comes from the model's prior, not results. Treat it as a guess until the pods connect.</p>"
               '<div class="card scroll"><table><thead><tr>'
               + HEAD.format(pod='<th class="l hide-sm">Pod</th>', move="") + "</tr></thead><tbody>")
    names = {1: "Pod leaders", 2: "Second in pod", 3: "Third in pod", 4: "Fourth in pod"}
    for tier, ts in a["tiers"]:
        out.append(f'<tr class="tier"><td colspan="11">Tier {tier} · {names.get(tier, f"Position {tier}")}</td></tr>')
        for t in ts:
            out.append(full_row(a, t, "", show_pod=True, move=False))
    out.append("</tbody></table></div>")
    return "".join(out)


def render_banner(a):
    name = lambda t: a["teams"][t]["name"]
    if not a["played"]:
        return '<div class="banner info"><b>No completed games yet.</b><p>The ranking appears after the first matchday.</p></div>'
    if not a["connected"]:
        firsts = sorted({l["next"] for l in a["links"] if l["next"]})
        joins = a["connects_on"] if a["connects_on"] and a["connects_on"][0].isdigit() else None
        if firsts and joins == firsts[0]:
            first = f" Cross-pod games start <b>{nice_date(joins)}</b> and should link every team that weekend."
            joins = ""
        else:
            first = f" First cross-pod games: <b>{nice_date(firsts[0])}</b>." if firsts else ""
            joins = f" Every team becomes linked through results on <b>{nice_date(joins)}</b>, if games go as scheduled." if joins else ""
        return (f'<div class="banner warn"><b>Early season — {a["components"]} separate pods.</b>'
                f"<p>So far each team has only played teams in its own pod, and there are no results connecting one pod to another. "
                f"Ranking inside a pod is solid; any order <em>between</em> pods is a guess.{first}{joins}</p></div>")
    missing = [l for l in a["links"] if l["played"] == 0]
    notes = []
    for l in missing:
        when = f"first meeting {nice_date(l['next'])}" if l["next"] else "no meeting scheduled"
        notes.append(f"Pod {l['a']} and Pod {l['b']} haven't played each other directly yet ({when}) — they compare only through common opponents.")
    if a["bridges"]:
        games = ", ".join(f"{e(name(x))}–{e(name(y))}" for x, y in a["bridges"][:4])
        notes.append(f"{len(a['bridges'])} single result(s) are the only link between parts of the league ({games}); one upset there moves a lot.")
    if notes:
        return ('<div class="banner info"><b>All teams are now connected through results.</b>'
                + "".join(f"<p>{n}</p>" for n in notes) + "</div>")
    return ('<div class="banner ok"><b>Well connected.</b><p>Every team is linked to every other through multiple results, '
            "so the overall ranking is backed by evidence across the league.</p></div>")


def estimated_order(a):
    """Forced 1..N: rating order; teams the model calls tied are listed alphabetically."""
    groups = {}
    for r, t in a["overall"]:
        groups.setdefault(r, []).append(t)
    return [t for r in sorted(groups) for t in sorted(groups[r], key=lambda t: a["teams"][t]["name"].lower())]


def _did(gf, ga):
    return "beat" if gf > ga else "drew with" if gf == ga else "lost to"


def why_above(a, t, u):
    """(confidence label, html sentence) for why t sits directly above u."""
    rec, name = a["rec"], lambda x: e(a["teams"][x]["name"])
    x, y = rec[t], rec[u]
    rank_of = dict((v, r) for r, v in a["overall"])
    if rank_of[t] == rank_of[u]:
        return "coin flip", f"Level with {name(u)} on the model, so they're listed alphabetically."

    log = collections.defaultdict(list)
    for g in a["played"]:
        log[g["home"]].append((g["away"], g["hs"], g["as"]))
        log[g["away"]].append((g["home"], g["as"], g["hs"]))

    parts, against = [], []
    for o, gf, ga in log[t]:
        if o == u:
            (against if gf < ga else parts).append(
                f"they lost to {name(u)} {gf}–{ga} head-to-head" if gf < ga else f"{_did(gf, ga)} them {gf}–{ga} head-to-head")
    for o in sorted({o for o, _, _ in log[t]} & {o for o, _, _ in log[u]} - {t, u}, key=lambda o: a["teams"][o]["name"]):
        rt = next((gf, ga) for oo, gf, ga in log[t] if oo == o)
        ru = next((gf, ga) for oo, gf, ga in log[u] if oo == o)
        if rt[0] - rt[1] > ru[0] - ru[1]:
            parts.append(f"{_did(*rt)} {name(o)} {rt[0]}–{rt[1]}, while {name(u)} {_did(*ru)} them {ru[0]}–{ru[1]}")
            break
    if x["ppg"] - y["ppg"] >= 0.5:
        parts.append(f"more points per game ({x['ppg']:.1f} vs {y['ppg']:.1f})")
    if x["gd"] - y["gd"] >= 2:
        parts.append(f"better goal difference ({signed(x['gd'])} vs {signed(y['gd'])})")
    if x["sos"] - y["sos"] >= 0.5:
        parts.append(f"tougher opponents ({x['sos']:.1f} vs {y['sos']:.1f} pts/game in their other games)")

    if y["ppg"] - x["ppg"] >= 0.5:
        against.append(f"{name(u)} has more points per game ({y['ppg']:.1f} vs {x['ppg']:.1f})")
    elif y["gd"] - x["gd"] >= 2:
        against.append(f"{name(u)} has the better goal difference ({signed(y['gd'])} vs {signed(x['gd'])})")

    gap = x["score"] - y["score"]
    if not parts:
        parts.append("slightly better on the combined rating" if gap < 0.5 else "better on the combined rating")
    linked = any(t in c and u in c for c in a["comps"])
    conf = ("coin flip" if gap < 0.15 else "educated guess" if not linked else "lean" if gap < 0.5 else "clear")
    text = "; ".join(parts)
    if against:
        text += ", even though " + " and ".join(against)
    text = text[0].upper() + text[1:]
    if not linked:
        text += ". No results link them yet, since they're in different pods"
    return conf, f"Above {name(u)}: {text}."


def render_estimated(a):
    order = estimated_order(a)
    items = []
    for i, t in enumerate(order):
        x = a["rec"][t]
        if i + 1 < len(order):
            conf, why = why_above(a, t, order[i + 1])
            why_html = f'<div class="why"><span class="chip conf {conf.replace(" ", "-")}">{conf}</span> {why}</div>'
        else:
            why_html = '<div class="why muted">Last in the current order.</div>'
        items.append(f'<li><span class="est-rank">{i + 1}</span><div class="est-body"><div class="est-top">{team_cell(a, t)}'
                     f'<span class="muted small">{x["w"]}-{x["l"]}-{x["d"]} · {x["pts"]} pts · GD {signed(x["gd"])} · rating {x["score"]:+.2f}</span>'
                     f'</div>{why_html}</div></li>')
    note = ("" if a["connected"] else
            " Right now the pods haven't played each other, so the order between teams from different pods is an educated guess.")
    return ('<h2>Estimated power ranking</h2>'
            f'<p class="sub small">A forced 1–{len(a["teams"])} order from every result so far. Each line says why a team sits above the next one. '
            '<b>Coin flip</b> means too close to call; exact ties are listed alphabetically.' + note + '</p>'
            f'<ol class="est">{"".join(items)}</ol>')


def gs_name(n):
    """GotSport flight names carry a ranking suffix like '- 57' or '- 8-c'."""
    return re.sub(r"\s*-\s*\d+(-c)?\s*$", "", (n or "").strip())


def render_tourney_note(a):
    c = a["gs_counted"]
    if not c:
        return ""
    league = {g[s] for g in c for s in ("home", "away") if g[s] in a["teams"]}
    return (f'<div class="banner info"><b>Tournament results are helping.</b><p>{len(c)} tournament games since Aug 1 '
            f"connect {len(league)} league teams: head-to-head, through a shared opponent, or through opponents who played each other. "
            "They count at half weight.</p></div>")


def render_flags(a):
    if not a["flags"]:
        return ""
    name = lambda t: a["teams"][t]["name"]
    scope = "overall" if a["connected"] else "in its pod"
    items = "".join(
        f'<li><b>{e(name(f["team"]))}</b> — #{f["model"]} by rating but #{f["points"]} on points {scope}. Driven by {e(f["why"])}.</li>'
        for f in a["flags"])
    return f'<h2>Rating vs. points table</h2><div class="card"><ul class="flags">{items}</ul></div>'


def fixture(a, g, show_link=False):
    name = lambda t: a["teams"][t]["name"]
    h, w = g["home"], g["away"]
    if g in a["played"]:
        hc = ' class="h win"' if g["hs"] > g["as"] else ' class="h"'
        ac = ' class="win"' if g["as"] > g["hs"] else ""
        mid = f'{g["hs"]}–{g["as"]}'
        meta = ""
    else:
        hc, ac, mid = ' class="h"', "", "v"
        link = ""
        if show_link and a["pod_of"] and a["pod_of"].get(h) != a["pod_of"].get(w):
            link = f' <span class="chip link">links Pod {a["pod_of"][h]} ↔ Pod {a["pod_of"][w]}</span>'
        meta = f'<div class="meta">{e(g["time"])} · {e(g["venue"])}{link}</div>'
    return (f'<div class="fx"><div{hc}>{e(name(h))}</div><div class="res">{mid}</div>'
            f'<div{ac}>{e(name(w))}</div>{meta}</div>')


def is_linked(a, g):
    return any(g["home"] in c and g["away"] in c for c in a["comps"])


def prediction(a, g, label_guess=True):
    name = lambda t: a["teams"][t]["name"]
    h, w = g["home"], g["away"]
    p = rank.predict(a["rec"], h, w, a["gpg"])
    hc = ' class="h win"' if p["pick"] == "home" else ' class="h"'
    ac = ' class="win"' if p["pick"] == "away" else ""
    chip = ' <span class="chip">guess · no results link these teams yet</span>' if label_guess and not is_linked(a, g) else ""
    pct = lambda v: f"{v * 100:.0f}%"
    return (
        f'<div class="fx pred"><div{hc}>{e(name(h))}</div><div class="res est">{p["score"][0]}–{p["score"][1]}</div><div{ac}>{e(name(w))}</div>'
        f'<div class="pct h home-c">{pct(p["home"])}</div><div class="pct c">draw {pct(p["draw"])}</div><div class="pct away-c">{pct(p["away"])}</div>'
        f'<div class="probs" role="img" aria-label="{e(name(h))} {pct(p["home"])}, draw {pct(p["draw"])}, {e(name(w))} {pct(p["away"])}">'
        f'<span class="ph" style="width:{p["home"] * 100:.1f}%"></span><span class="pd" style="width:{p["draw"] * 100:.1f}%"></span>'
        f'<span class="pa" style="width:{p["away"] * 100:.1f}%"></span></div>'
        f'<div class="meta">{nice_date(g["date"])} · {e(g["time"])} · {e(g["venue"])}{chip}</div></div>'
    )


def week_start(date):
    d = dt.date.fromisoformat(date)
    return d - dt.timedelta(days=d.weekday())


def render_grades(a):
    rows = a.get("graded") or []
    out = ["<h2>How the estimates did</h2>"]
    if not rows:
        out.append('<div class="card small">Tracking starts with the next games. Each week\'s estimates are saved before kickoff '
                   "and graded here once the results are posted.</div>")
        return "".join(out)

    n = len(rows)
    right = sum(r["right"] for r in rows)
    exact = sum(r["exact"] for r in rows)
    brier = sum(r["brier"] for r in rows) / n
    tiles = [(f"{right} of {n}", f"right result ({right / n:.0%})"),
             (f"{exact} of {n}", "exact score"),
             (f"{brier:.2f}", "accuracy score · pure guessing = 0.67 · lower is better")]
    out.append('<div class="stats">' + "".join(f'<div class="stat"><b>{e(v)}</b>{e(l)}</div>' for v, l in tiles) + "</div>")
    frac = lambda xs: f"{sum(r['right'] for r in xs)} of {len(xs)} right" if xs else "none yet"
    out.append(f'<p class="sub small" style="margin-top:10px">Linked matchups: {frac([r for r in rows if r["linked"]])} · '
               f'Guesses (teams not yet linked by results): {frac([r for r in rows if not r["linked"]])}.</p>')

    name = lambda t: e(a["teams"][t]["name"])
    weeks = {}
    for r in rows:
        weeks.setdefault(week_start(r["game"]["date"]), []).append(r)
    for i, wk in enumerate(sorted(weeks, reverse=True)):
        rs = weeks[wk]
        first = min(r["game"]["date"] for r in rs)
        last = max(r["game"]["date"] for r in rs)
        label = nice_date(first) + (f" – {nice_date(last)}" if last != first else "")
        body = []
        for r in rs:
            g, s = r["game"], r["est"]
            hc = ' class="h win"' if g["hs"] > g["as"] else ' class="h"'
            ac = ' class="win"' if g["as"] > g["hs"] else ""
            marks = ('<span class="chip ok">✓ right result</span>' if r["right"] else '<span class="chip miss">✗ missed</span>')
            if r["exact"]:
                marks += ' <span class="chip ok">exact score</span>'
            if not r["linked"]:
                marks += ' <span class="chip">guess</span>'
            body.append(f'<div class="fx"><div{hc}>{name(g["home"])}</div><div class="res">{g["hs"]}–{g["as"]}</div>'
                        f'<div{ac}>{name(g["away"])}</div>'
                        f'<div class="meta">Estimate {s["hs"]}–{s["as"]} · {marks}</div></div>')
        wr = sum(r["right"] for r in rs)
        op = " open" if i == 0 else ""
        out.append(f'<details{op}><summary><span>{label}</span><span class="muted small">{wr} of {len(rs)} right</span></summary>'
                   f'<div class="body">{"".join(body)}</div></details>')
    return "".join(out)


def render_fixtures(a):
    out = []
    if a["upcoming"]:
        today = dt.datetime.now(TZ).date().isoformat()
        stale = [g for g in a["upcoming"] if g["date"] < today]  # date has passed, no score posted
        weekends = {}
        for g in a["upcoming"]:
            if g["date"] < today:
                continue
            d = dt.date.fromisoformat(g["date"])
            weekends.setdefault(d - dt.timedelta(days=d.weekday()), []).append(g)  # Mon–Sun week, so Fri joins its Sat/Sun
        out.append('<h2>Upcoming games · score estimates</h2>'
                   '<p class="sub small">Estimates come from the ratings: each side starts at the league scoring average '
                   f'({a["gpg"]:.1f} goals per team per game), shifted by the rating gap plus a small home edge. '
                   "The score shown is the likeliest scoreline for the likeliest result; the bar shows win / draw / win chances. "
                   "With only a few games played these are rough, and games between pods that haven't met yet are guesses.</p>")
        for i, key in enumerate(sorted(weekends)):
            games = weekends[key]
            first, last = min(g["date"] for g in games), max(g["date"] for g in games)
            label = nice_date(first) + (f" – {nice_date(last)}" if last != first else "")
            op = " open" if i == 0 else ""
            none_linked = not any(is_linked(a, g) for g in games)
            note = ('<p class="muted small" style="margin:0 0 4px">None of these matchups are linked by results yet, '
                    "so every estimate here is a guess.</p>") if none_linked else ""
            out.append(f'<details{op}><summary><span>{label}</span><span class="muted small">{len(games)} games</span></summary>'
                       f'<div class="body">{note}{"".join(prediction(a, g, label_guess=not none_linked) for g in games)}</div></details>')
        if stale:
            body = "".join(prediction(a, g) for g in stale)
            out.append(f'<details><summary><span>Postponed or not yet posted</span>'
                       f'<span class="muted small">{len(stale)} games</span></summary>'
                       '<div class="body"><p class="muted small" style="margin:0 0 4px">These dates have passed with no '
                       'score posted — usually postponed, sometimes just a late entry.</p>'
                       f'{body}</div></details>')
    out.append(render_grades(a))
    if a["played"]:
        out.append("<h2>Results</h2>")
        by_date = collections.defaultdict(list)
        for g in a["played"]:
            by_date[g["date"]].append(g)
        for i, d in enumerate(sorted(by_date, reverse=True)):
            op = " open" if i == 0 else ""
            out.append(f'<details{op}><summary><span>{nice_date(d)}</span><span class="muted small">{len(by_date[d])} games</span></summary>'
                       f'<div class="body">{"".join(fixture(a, g) for g in by_date[d])}</div></details>')
    return "".join(out)


def opp_rank(a, t):
    """Current standing of a team in the estimated ranking: '#3', or 'T5' when shared."""
    r = dict((u, rr) for rr, u in a["overall"]).get(t)
    if r is None:
        return ""
    return ("T" if sum(1 for rr, _ in a["overall"] if rr == r) > 1 else "#") + str(r)


def render_teams(a):
    name = lambda t: a["teams"][t]["name"]
    out = ['<h2>Teams</h2>']
    for t in sorted(a["teams"], key=name):
        x = a["rec"][t]
        rows = []
        pct = lambda v: f"{v * 100:.0f}%"
        for g in a["played"] + a["upcoming"]:
            if t not in (g["home"], g["away"]):
                continue
            home = g["home"] == t
            opp = g["away"] if home else g["home"]
            vs = ("v " if home else "@ ") + f'<span class="orank">{opp_rank(a, opp)}</span> ' + e(name(opp))
            if g in a["played"]:
                gf, ga = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
                res = "W" if gf > ga else "D" if gf == ga else "L"
                right = f'<span class="{res}">{res}</span> {gf}–{ga}'
                sub = ""
            else:
                p = rank.predict(a["rec"], g["home"], g["away"], a["gpg"])
                gf, ga = p["score"] if home else p["score"][::-1]
                win, loss = (p["home"], p["away"]) if home else (p["away"], p["home"])
                right = f'<span class="res est">{gf}–{ga}</span>'
                sub = (f'<br><span class="muted small">{e(g["time"])} · est · win {pct(win)} · '
                       f'draw {pct(p["draw"])} · loss {pct(loss)}</span>')
            rows.append(f'<tr><td class="l muted">{nice_date(g["date"])}</td>'
                        f'<td class="l">{vs}{sub}</td><td>{right}</td></tr>')
        mine = [g for g in a["gs_games"] if t in (g["home"], g["away"])]
        if mine:
            rows.append('<tr class="tier"><td colspan="3">Tournaments since Aug 1 · GotSport</td></tr>')
        for g in mine:
            home = g["home"] == t
            gf, ga = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
            res = "W" if gf > ga else "D" if gf == ga else "L"
            opp = gs_name(g["away_name"] if home else g["home_name"])
            tag = "counted at ½ weight" if g in a["gs_counted"] else "not counted"
            rows.append(f'<tr><td class="l muted">{nice_date(g["date"])}</td>'
                        f'<td class="l">v {e(opp)}<br><span class="muted small">{e(g["event"])} · {tag}</span></td>'
                        f'<td><span class="{res}">{res}</span> {gf}–{ga}</td></tr>')
        if t in a["gs_mapping"] and not a["gs_mapping"][t]["gotsport"]:
            rows.append('<tr><td class="l small" colspan="3"><span class="muted">Not matched to a GotSport team yet, so tournament games aren\'t shown.</span></td></tr>')
        pod = f' · Pod {a["pod_of"][t]}' if a["pod_of"] and not a["connected"] else ""
        if mine:
            pod += f" · {len(mine)} tournament"
        out.append(f'<details><summary><span class="tm">{team_cell(a, t)}</span>'
                   f'<span class="muted small">{opp_rank(a, t)} · {x["w"]}-{x["l"]}-{x["d"]} · {x["pts"]} pts{pod}</span></summary>'
                   f'<div class="body scroll"><p class="muted small" style="margin:0 0 6px">{e(a["teams"][t]["club"])} · '
                   "ranks are current; upcoming games show the estimated score and this team's chances</p>"
                   f'<table><tbody>{"".join(rows)}</tbody></table></div></details>')
    return "".join(out)


def render_method(a):
    k = a["knobs"]
    st, gsg = a["gs_status"], a["gs_games"]
    gsg = [g for g in gsg if g["home"] in a["teams"] or g["away"] in a["teams"]]  # skip outside-only bracket games
    gteams = {g[s] for g in gsg for s in ("home", "away") if g[s] in a["teams"]}
    unmatched = [m["name"] for m in a["gs_mapping"].values() if not m["gotsport"]]
    checked = (dt.datetime.fromisoformat(st["last_success"]).astimezone(TZ).strftime("%b %-d")
               if st.get("last_success") else "never")
    if not a["gs_mapping"]:
        gs_txt = ("<li><b>Tournaments:</b> not tracked for this league yet, so the ranking uses league games only. "
                  f"They can be added by mapping these teams to GotSport in <code>gotsport/{e(SLUG)}/teams.json</code>.</li>")
    else:
        gs_txt = (f"<li><b>Tournaments:</b> {len(gsg)} games since Aug 1 found on GotSport for {len(gteams)} league teams. "
                      "A tournament game counts, at half weight, only when it helps compare league teams "
                      f"(they met, share an opponent, or their opponents played each other; no longer chains); so far {len(a['gs_counted'])} do. The rest are listed on team pages. "
                      f"Last checked {checked}."
                      + (" ⚠️ GotSport blocked the latest check, so this uses saved data." if st.get("blocked") else "")
                      + (f" Not yet matched on GotSport: {e(', '.join(unmatched))}." if unmatched else "")
                      + " Tournaments run on other platforms aren't included.</li>")
    cc = a["cross_check"]
    cc_txt = ("Official standings unavailable for cross-check." if cc is None else
              "Computed points match the official AthleteOne standings." if not cc else
              "⚠️ Computed points DON'T match the official standings for: "
              + ", ".join(e(a["teams"][t]["name"]) for t in cc) + " — possible deduction or forfeit.")
    clubs = "".join(
        f"<li>{e(c['club'])} has {c['teams']} of the {len(a['teams'])} teams in this flight; "
        f"{c['intra']} of {len(a['played'])} results so far were club-vs-club.</li>" for c in a["big_clubs"])
    return f"""
<h2>How the rating works</h2>
<div class="card method"><ul>
<li><b>Rating</b> blends three models, each scaled so they're comparable: 45% points per game adjusted for opponent strength,
30% <b>Massey</b> (goal margin, capped at ±{k['cap']:g}, with a {rank.HFA} goal home edge), 25% <b>Bradley-Terry</b> (win/draw/loss probability).
0 is league average.</li>
<li><b>SOS</b> is the average points per game your opponents earned <em>in their other games</em>, so your own result doesn't pad your schedule.</li>
<li>With few games, big wins are capped and every team is pulled toward average. That loosens automatically as the season fills in
(now: {k['games_per_team']:.1f} games per team, cap ±{k['cap']:g}, shrinkage {k['lam']:g}).</li>
<li>Teams too close to separate share a rank (shown as T4, etc.).</li>
{gs_txt}
<li>{cc_txt}</li>
{clubs}
</ul></div>"""


def render(a):
    ev = a["event"] or {}
    now = dt.datetime.now(TZ)
    title = f"{a['meta'].get('divisionName', '')} {a['meta'].get('flightName', '')} Power Ranking".strip()
    logo = f'<img src="{e(ev["eventLogo"])}" alt="">' if ev.get("eventLogo") else ""
    games_pt = a["knobs"]["games_per_team"]
    stats = [
        (f"{len(a['played'])} / {a['scheduled']}", "games played"),
        (f"{games_pt:.1f}", "games per team"),
        (nice_date(a["next_date"]) if a["next_date"] else "—", "next matchday"),
        (now.strftime("%b %-d, %-I:%M %p CT"), "last updated"),
    ]
    sim = f'<div class="sim">SIMULATION — fake scores through {a["simulated"]}</div>' if a["simulated"] else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{e(title)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
{sim}
{render_nav()}
<header>
<div class="eyebrow">{logo}{e(ev.get('name', 'ECNL RL Girls STXCL'))}</div>
<h1>{e(title)}</h1>
<p class="sub">Ranked on every completed league game — who each team played, and by how much.</p>
<div class="stats">{''.join(f'<div class="stat"><b>{e(v)}</b>{e(l)}</div>' for v, l in stats)}</div>
</header>
{render_estimated(a) if a['played'] else ''}
{render_banner(a)}
{render_tourney_note(a)}
{render_ranking(a) if a['played'] else ''}
{render_flags(a)}
{render_fixtures(a)}
{render_teams(a)}
{render_method(a)}
<footer>Data: <a href="{PUBLIC_URL}">AthleteOne schedules &amp; standings</a>. Updates every Monday morning.
Unofficial ranking; not affiliated with ECNL.</footer>
</div></body></html>"""


def build_one(out, simulate_until):
    meta, sched, standings, event = rank.fetch(EVENT_ID, FLIGHT_ID)
    a = analyse(meta, sched, standings, event, simulate_until, gotsport.load_games())
    store = load_predictions()
    if not simulate_until:  # simulations grade against saved estimates but never save new ones
        store = update_predictions(a, store)
        PRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        PRED_FILE.write_text(json.dumps(store, indent=1, sort_keys=True))
    a["graded"] = grade(a, store)

    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(a))
    (out / "data.json").write_text(json.dumps({
        "updated": dt.datetime.now(TZ).isoformat(timespec="minutes"),
        "connected": a["connected"],
        "components": a["components"],
        "teams": {a["teams"][t]["name"]: {**a["rec"][t], "rank": r} for r, t in a["overall"]},
        "estimated_order": [a["teams"][t]["name"] for t in estimated_order(a)] if a["played"] else [],
        "cross_check_mismatch":[a["teams"][t]["name"] for t in (a["cross_check"] or [])],
    }, indent=1))

    if a["last_date"] and not simulate_until:
        HIST.mkdir(parents=True, exist_ok=True)
        snap = json.dumps(snapshot(a), indent=1, sort_keys=True)
        path = HIST / f"{a['last_date']}.json"
        if not path.exists() or path.read_text() != snap:
            path.write_text(snap)

    print(f"{LEAGUE.get('name', '?')}: played {len(a['played'])}/{a['scheduled']}  "
          f"components {a['components']}  "
          f"cross-check {'MATCH' if not a['cross_check'] else a['cross_check']}  -> {out / 'index.html'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--simulate-until")
    ap.add_argument("--slug", help='build only this league ("" for the root one)')
    args = ap.parse_args()
    for cfg in LEAGUES:
        if args.slug is not None and cfg.get("slug", "") != args.slug:
            continue
        use_league(cfg)
        build_one(pathlib.Path(args.out) / cfg.get("slug", ""), args.simulate_until)


if __name__ == "__main__":
    main()
