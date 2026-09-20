# STXCL GU14 Regional — Power Ranking

Auto-updating ranking for ECNL RL Girls STXCL 2026-27, GU14 "Regional" flight,
built from every completed league game (who played whom, and by what score).

- `rank.py` — fetch from the AthleteOne API + ranking model (text report: `python3 rank.py 4260 41143`)
- `build_site.py` — renders `site/index.html` and `site/data.json`; snapshots each matchday into `history/`
- `.github/workflows/update.yml` — runs every Monday 8 AM CT (or manually via "Run workflow"), deploys to GitHub Pages, commits new snapshots
- `gotsport.py` — pulls tournament games since Aug 1, 2026 from GotSport's rankings API (8s between requests), caching them in `gotsport/cache/`. A tournament game counts in the ranking at half weight only when it sits on a chain of 3 or fewer games between two league teams (head-to-head, shared opponent, or opponents who played each other). Outside opponents' games are pulled so those chains can be found. All other tournament games are shown on team pages only. Team matching lives in `gotsport/teams.json`, and the Lonestar teams still need confirming. If GotSport blocks a run, the saved cache is used.
- `CLAUDE.md` — full method, the connectivity rule, and dead ends. Read before changing the model.

Local preview:

```bash
pip install numpy
python3 build_site.py --out /tmp/stxcl && open /tmp/stxcl/index.html
```

`--simulate-until YYYY-MM-DD` fills unplayed games with random scores to preview later-season states.

## Leagues

`leagues.json` lists every league the site builds. The first entry is served at the site root and
the rest at `/<slug>/`; the page header shows a toggle between them.

```bash
python3 add_league.py --list 41120 41180    # browse flights and their ids
python3 add_league.py 41141                 # add one (confirms teams first)
python3 build_site.py                       # builds every league
python3 build_site.py --slug gu13-western   # or just one
```

Each league keeps its own `history/<slug>/` (matchday snapshots + saved estimates) and optional
`gotsport/<slug>/teams.json` (tournament games; leave it out and the league is league-games-only).
Flight and event ids come straight from the public AthleteOne URL.

Note: GitHub pauses scheduled workflows after 60 days without a commit. Matchday
snapshots keep it alive in season; after a long break, re-enable from the Actions tab.
