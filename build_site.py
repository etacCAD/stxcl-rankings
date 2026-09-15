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
from zoneinfo import ZoneInfo

import rank

EVENT_ID = 4260
FLIGHT_ID = 41143
ROOT = pathlib.Path(__file__).resolve().parent
TZ = ZoneInfo("America/Chicago")
PUBLIC_URL = f"https://app.athleteone.com/public/event/{EVENT_ID}/schedules-standings/schedules/{FLIGHT_ID}"

e = html.escape


def nice_date(d, weekday=True):
    x = dt.date.fromisoformat(d)
    return x.strftime("%a %b %-d" if weekday else "%b %-d")


def signed(v):
    return f"{v:+d}" if v else "0"


# --- analysis ----------------------------------------------------------------

def analyse(meta, sched, standings, event, simulate_until=None):
    teams, played, upcoming = rank.parse(sched)
    ids = set(teams)

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

    rec, k = rank.build(ids, played)
    comps = rank.components(ids, played)
    connected = len(comps) == 1
    name = lambda t: teams[t]["name"]

    pods = rank.seed_pods(ids, played) or []
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
    hist = ROOT / "history"
    if last_date and hist.exists() and not simulate_until:
        older = sorted(p for p in hist.glob("*.json") if p.stem < last_date)
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

    next_date = upcoming[0]["date"] if upcoming else None
    return {
        "meta": meta, "event": event, "teams": teams, "rec": rec, "knobs": k,
        "played": played, "upcoming": upcoming, "scheduled": len(played) + len(upcoming),
        "connected": connected, "components": len(comps), "pods": pods, "pod_of": pod_of,
        "pod_tables": pod_tables, "tiers": tiers, "links": links,
        "connects_on": rank.connects_on(ids, played, upcoming),
        "bridges": rank.bridges(ids, played) if connected else [],
        "overall": overall, "pts_rank": pts_rank, "flags": flags,
        "prev": prev, "last_date": last_date, "next_date": next_date,
        "cross_check": rank.cross_check(standings, rec) if not simulate_until else [],
        "big_clubs": big_clubs, "simulated": simulate_until,
    }


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
--accent:#0f5bd8;--win:#127a45;--loss:#b3261e;--draw:#8a6d00;--warnbg:#fff4d6;--warnline:#e7c35a;
--okbg:#e4f4ea;--okline:#7cc49a;--infobg:#e6eefc;--infoline:#8fb0ec}
@media (prefers-color-scheme:dark){:root{--bg:#111317;--card:#1a1d23;--ink:#eceef2;--muted:#9aa1ad;
--line:#2b2f37;--soft:#22262d;--accent:#7aa8ff;--win:#4cc584;--loss:#ff7b72;--draw:#e0c050;
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
.fx{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}
.fx:first-child{border-top:0}
.fx .h{text-align:right}.fx .res{font-weight:700;text-align:center;min-width:48px}
.fx .meta{grid-column:1/-1;text-align:center;font-size:12px;color:var(--muted);margin-top:-4px}
.win{font-weight:700}
ul.flags{margin:0;padding-left:18px}ul.flags li{margin:6px 0}
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


def render_fixtures(a):
    out = []
    if a["next_date"]:
        nxt = [g for g in a["upcoming"] if g["date"] == a["next_date"]]
        more = sorted({g["date"] for g in a["upcoming"] if g["date"] != a["next_date"]})
        nd = [d for d in more if (dt.date.fromisoformat(d) - dt.date.fromisoformat(a["next_date"])).days <= 1]
        nxt += [g for g in a["upcoming"] if g["date"] in nd]
        label = nice_date(a["next_date"]) + (f" – {nice_date(nd[-1])}" if nd else "")
        out.append(f'<h2>Next up · {label}</h2><div class="card">'
                   + "".join(fixture(a, g, show_link=not a["connected"] or any(l["played"] == 0 for l in a["links"])) for g in nxt)
                   + "</div>")
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


def render_teams(a):
    name = lambda t: a["teams"][t]["name"]
    out = ['<h2>Teams</h2>']
    for t in sorted(a["teams"], key=name):
        x = a["rec"][t]
        rows = []
        for g in a["played"] + a["upcoming"]:
            if t not in (g["home"], g["away"]):
                continue
            home = g["home"] == t
            opp = g["away"] if home else g["home"]
            vs = ("v " if home else "@ ") + e(name(opp))
            if g in a["played"]:
                gf, ga = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
                res = "W" if gf > ga else "D" if gf == ga else "L"
                right = f'<span class="{res}">{res}</span> {gf}–{ga}'
            else:
                right = f'<span class="muted">{e(g["time"])}</span>'
            rows.append(f'<tr><td class="l muted">{nice_date(g["date"])}</td><td class="l">{vs}</td><td>{right}</td></tr>')
        pod = f' · Pod {a["pod_of"][t]}' if a["pod_of"] and not a["connected"] else ""
        out.append(f'<details><summary><span class="tm">{team_cell(a, t)}</span>'
                   f'<span class="muted small">{x["w"]}-{x["l"]}-{x["d"]} · {x["pts"]} pts{pod}</span></summary>'
                   f'<div class="body scroll"><p class="muted small" style="margin:0 0 6px">{e(a["teams"][t]["club"])}</p>'
                   f'<table><tbody>{"".join(rows)}</tbody></table></div></details>')
    return "".join(out)


def render_method(a):
    k = a["knobs"]
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
<li>League games only. Tournaments and friendlies aren't in AthleteOne's data, so they aren't counted.</li>
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
<header>
<div class="eyebrow">{logo}{e(ev.get('name', 'ECNL RL Girls STXCL'))}</div>
<h1>{e(title)}</h1>
<p class="sub">Ranked on every completed league game — who each team played, and by how much.</p>
<div class="stats">{''.join(f'<div class="stat"><b>{e(v)}</b>{e(l)}</div>' for v, l in stats)}</div>
</header>
{render_banner(a)}
{render_ranking(a) if a['played'] else ''}
{render_flags(a)}
{render_fixtures(a)}
{render_teams(a)}
{render_method(a)}
<footer>Data: <a href="{PUBLIC_URL}">AthleteOne schedules &amp; standings</a>. Updates every Monday morning.
Unofficial ranking; not affiliated with ECNL.</footer>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--simulate-until")
    args = ap.parse_args()

    meta, sched, standings, event = rank.fetch(EVENT_ID, FLIGHT_ID)
    a = analyse(meta, sched, standings, event, args.simulate_until)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(a))
    (out / "data.json").write_text(json.dumps({
        "updated": dt.datetime.now(TZ).isoformat(timespec="minutes"),
        "connected": a["connected"],
        "components": a["components"],
        "teams": {a["teams"][t]["name"]: {**a["rec"][t], "rank": r} for r, t in a["overall"]},
        "cross_check_mismatch": [a["teams"][t]["name"] for t in (a["cross_check"] or [])],
    }, indent=1))

    if a["last_date"] and not args.simulate_until:
        hist = ROOT / "history"
        hist.mkdir(exist_ok=True)
        snap = json.dumps(snapshot(a), indent=1, sort_keys=True)
        path = hist / f"{a['last_date']}.json"
        if not path.exists() or path.read_text() != snap:
            path.write_text(snap)

    print(f"played {len(a['played'])}/{a['scheduled']}  components {a['components']}  "
          f"cross-check {'MATCH' if not a['cross_check'] else a['cross_check']}  -> {out / 'index.html'}")


if __name__ == "__main__":
    main()
