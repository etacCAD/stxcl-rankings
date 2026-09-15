# STXCL GU14 Ranking Project — Agent Instructions

Context doc for an agent picking this up cold. Read fully before running anything.

---

## 1. Goal

Produce a defensible strength ranking of the 16 teams in **ECNL RL Girls STXCL
2026-27, GU14 "Regional" flight**, based on actual results (who played whom, and
by what score) rather than raw standings position.

Ties in the output are acceptable and often correct. Do not manufacture
separation the data does not support.

**Primary identifiers**

| Thing | Value |
|---|---|
| Event ID | `4260` |
| Flight ID | `41143` (GU14 / "Regional") |
| Division ID | `22227` |
| Public URL | `app.athleteone.com/public/event/4260/schedules-standings/schedules/41143` |
| Teams | 16 |
| Format | Full single round-robin, 120 games, 15 per team |
| Season window | 2026-08-01 → 2027-07-31 |

---

## 2. Data source: the AthleteOne API

The public site is an Angular SPA and `app.athleteone.com` is **robots-disallowed**,
so ordinary fetch tools will refuse it. Do not fight this. The backing API at
`api.athleteone.com` is open, unauthenticated, returns clean JSON, and needs no
browser. Use it directly.

```
BASE = https://api.athleteone.com/api

GET {BASE}/Event/get-flight-division-by-flightID/{flightID}
    -> divisionID, divisionName, flightName, teamCount, hideStandings

GET {BASE}/Event/get-schedules-by-flight/{eventID}/{flightID}/0
    -> full fixture list (played + future). THE PRIMARY ENDPOINT.

GET {BASE}/Event/get-standings-by-div-and-flight/{divisionID}/{flightID}/{eventID}
    -> official standings, grouped by flightGroup. Use ONLY to cross-check.

GET {BASE}/Event/get-individual-event-team-info/{eventID}/{teamID}
    -> one team's schedule + record, incl. a separate `overallWins/Draws/Losses`

GET {BASE}/team/get-event-details-by-eventID/{eventID}
    -> event metadata (name, location, logo)
```

Send a normal desktop browser `User-Agent`. No auth, no cookies, no rate limit
observed. A full pull is ~140KB and takes about a second.

### Reading the schedule payload

Response shape is `{"result": "success", "data": [ ...games... ]}`. Per-game
fields that matter: `gameDate`, `homeTeam` / `awayTeam`, `hometeamscore` /
`awayteamscore`, `homeTeamClub` / `awayTeamClub`, `flightGroupID`, `type`,
`flagText`.

**A game is completed iff `flagText == "Box Score"`.** Future games carry
`flagText == "Game Preview"` and `hometeamscore`/`awayteamscore` of `0`, not
`null` — so filtering on score being non-null will silently count unplayed games
as 0-0 draws. Filter on `flagText`. This is the single easiest bug to introduce
here.

Team names all carry the suffix ` ECNL RL STXCL G2012/13`. Strip it for display.

---

## 3. The method

Implemented in `athleteone_rank.py`. Run as:

```
python3 athleteone_rank.py 4260 41143 [--json out.json]     # requires numpy
```

Steps, in order:

1. **Records.** W-L-D, points (3/1/0), GF, GA, GD from completed games only.
2. **Cross-check** computed points against the official standings endpoint. The
   script prints `MATCH` or `MISMATCH`. A mismatch means a point deduction, a
   forfeit, or a parsing bug — investigate before trusting any output.
3. **Connectivity check** (union-find over the results graph). See §4 — this
   gates everything downstream.
4. **Three ratings**, blended by z-score at `0.45 / 0.30 / 0.25`:
   - *Adjusted PPG* — `ppg + 0.45 * (opponent_ppg - 1.0)`, where opponent PPG is
     computed **excluding** that opponent's games against the team being rated
     (otherwise a team's own result inflates its own strength of schedule).
   - *Massey* — ridge-regularized least squares on goal margin, margin capped at
     ±3, home-field advantage 0.25 goals.
   - *Bradley-Terry* — regularized logistic win-probability rating, draws = 0.5,
     home edge 0.15 logits.

Caps and regularization exist because the sample is tiny. Knobs live at the top
of the script (`CAP`, `HFA`, `LAM`, `BT_L2`, `SOS_W`, `BLEND`).

**Tuning schedule:** the current values are set for a 2-game sample. Around 6–8
games per team, raise or remove `CAP` and lower `LAM` — by then blowouts carry
real signal and the ridge prior is just dampening it.

---

## 4. The connectivity rule — most important thing in this doc

With few games played, the results graph is **disconnected**. As of 2026-09-14
(32 team-games, 2 per team) it split into four isolated pods of four teams with
zero games between them:

```
Pod A  Lonestar SAT · AHFC Central I · AHFC Central II · ATX STH Gold
Pod B  Boerne Red   · AHFC East      · AHFC NW         · Sting CC
Pod C  LTFC         · AHFC West      · FC Westlake     · Challenge
Pod D  Imperial SC  · ATX White      · SG1             · ATX NTH Gold
```

Massey and Bradley-Terry ratings are **mathematically undefined across
disconnected components**. The ridge/L2 term forces a number out anyway, but the
cross-component ordering is an artifact of the prior, not evidence.

**Rules for the agent:**

- Always run the connectivity check and always report the result.
- If `components > 1`: rank confidently **within** each component. Present the
  global table as tiers with an explicit low-confidence label. Never present a
  clean 1–16 list as if the seams were earned.
- Once `components == 1`, the global ranking is legitimate — but note which pod
  pairs are still connected only through long paths.

**Pod interlock schedule.** First cross-pod meetings:

| Pod pair | First meeting |
|---|---|
| A–B, A–D, B–C, C–D | 2026-09-19 |
| A–C | 2026-09-26 |
| **B–D** | **2026-10-18** |

So after 9/19–20 the graph connects, but B-vs-D remains the weakest link until
mid-October. Call that out rather than implying it is resolved.

---

## 5. Dead ends — do not re-litigate these

Each was tested and failed. Re-running them wastes time.

| Attempt | Outcome |
|---|---|
| `web_fetch` on `app.athleteone.com` | `ROBOTS_DISALLOWED`. Use the API. |
| Looking for cross-pod games hidden inside STXCL | None exist. 120 games = full round-robin; pods are a matchday-1-2 artifact that dissolves on its own. |
| AthleteOne team pages for outside-league games | Checked all 16. `overallWins/Draws/Losses` equals the event record for every team, and every `teamScheduleList` contains only `eventID: 4260`. AthleteOne knows nothing outside this league. |
| `gotsport.com`, `system.gotsport.com` root | 403 / 202, Cloudflare-challenged. |
| GotSport team-name search | ~~No public endpoint.~~ **Resolved 2026-09-15:** flat params are ignored, but nested `search[team_or_club_name]`, `search[gender]`, `search[age]`, `search[team_country]`, `search[team_association]` work. See §6 and §9. |
| `youthsoccerrankings.us` | 503. |
| Web search for these teams' tournament results | Only registration pages and noise. |
| PlayMetrics | Public schedules are club-scoped and login-gated. |

---

## 6. The one open thread: GotSport

GotSport event pages **are** server-rendered, public, and scrapable — but only if
you already have the numeric IDs:

```
https://system.gotsport.com/org_event/events/{eventID}/schedules?team={teamID}
```

And `system.gotsport.com/api/v1/team_ranking_data` returns, per team, an
`event_results_json` mapping **every event that team has played**, with placement
and points. That is exactly the cross-pod bridge we want — via shared tournament
brackets.

~~The blocker is narrow and specific: **no public name → team ID lookup.**~~

**Update 2026-09-15: the lookup exists.** The rankings SPA sends *nested* params, so
`team_ranking_data?search[team_or_club_name]=LTFC&search[gender]=f&search[age]=14&search[team_country]=USA`
filters properly (`search[team_association]=TXS` = Texas South). Flat `search=` etc. are
ignored, which is why it looked dead. Confirmed match: LTFC → ranking id 78929144,
"LTFC ECNL RL STXCL G2012/13". Rows carry both a ranking `id` and the real `team_id`.
**Rate limit:** ~20 rapid requests got the IP 403'd by Cloudflare. Pace requests
several seconds apart and cache responses to disk.

If the user supplies even one GotSport team ID for any of these 16 teams,
the unlock path is:

1. Pull that team's `event_results_json` → list of event IDs.
2. Fetch each event's public schedule pages → full brackets and scores.
3. Harvest the other STXCL teams' GotSport team IDs from those same events.
4. Repeat until the set is covered.
5. Feed any cross-pod tournament results in as extra edges in the results graph —
   they may connect pods before the league does.

If weighting tournament results alongside league results, down-weight them:
different rosters, different stakes, often different formats.

---

## 7. Output conventions

- Lead with the ranking. Method notes after, not before.
- Strip the ` ECNL RL STXCL G2012/13` suffix from team names.
- Show W-L-D, points, GF-GA, GD alongside any model score, so the reader can
  sanity-check the model against the plain record.
- Flag teams whose model position diverges sharply from their points position,
  and say which input is driving it (usually strength of schedule).
- State ties plainly. Do not break a tie the data cannot break.
- Note that Albion Hurricanes (AHFC) has **five** teams in this 16-team flight,
  so a meaningful share of results are intra-club.

---

## 8. Files

- `athleteone_rank.py` — the scraper + ranking model. Self-contained, stdlib +
  numpy. Works for any AthleteOne flight; event and flight IDs come straight out
  of the public URL, so it covers other age groups and divisions unchanged.
- `CLAUDE.md` — this file.

---

## 9. Tournament games (built 2026-09-15)

`gotsport.py` runs before `build_site.py` in the Monday workflow.

- **Scope:** games on or after **2026-08-01** only (Evan's rule).
- **Mapping:** `gotsport/teams.json` maps AthleteOne teamID to GotSport `team_id`s. The four Lonestar teams are unmatched (candidates listed) until someone confirms them. Don't guess.
- **Fetch:** `team_ranking_data?team_id=` gives events + flight ids, and `event_ranking_data/flight_matches` gives scores. It sleeps 8s between requests. A flight is re-pulled until 10 days after its event ends. The cache is committed, so a blocked run falls back to saved data (`gotsport/status.json` records it).
- **Also pulled:** every outside team that played a league team gets its own events and flights since Aug 1 pulled (at most once per 6 days), because that's where league–X–Y–league chains show up.
- **Counting rule:** a tournament game enters the model only if it lies on a chain of **at most 3 games** between two *different* league teams (`MAX_CHAIN`): 1 = they met, 2 = shared opponent, 3 = their opponents played each other. Longer chains are too removed (Evan's call, 2026-09-15). Those games are neutral-site, weight 0.5, and outside opponents become extra Massey/BT nodes. They don't touch W-L-D, points, SOS or adjusted PPG. Every other tournament game is shown on team pages only.
- **Why:** a league team beating outside teams no other league team played says nothing about how it compares to league teams, and the ridge prior would otherwise treat those opponents as league-average.

## 10. Estimated power ranking (forced 1–16), added 2026-09-15

Evan explicitly asked for a forced order at the **top** of the page, overriding §4's
"never present a clean 1–16" rule. Keep it, but keep it honest:
- order = composite rating; teams the model calls tied (`rank.is_tie`) are listed **alphabetically**
- every team gets a one-line "why above the next team": head-to-head, a common opponent it did
  better against, points per game, goal difference, schedule strength, plus an "even though…"
  counterpoint when the lower team has the better record or won head-to-head
- label per gap: `clear` / `lean` / `coin flip` (gap < 0.15) / `educated guess` (no results link
  the two teams yet). Pod standings, tiers and full tables stay below it.

## 11. Score estimates for upcoming games (added 2026-09-15)

`rank.predict()`: expected goals = league goals-per-team-per-game ± half the Massey margin
(+ `HFA` for the home side), floored at 0.15; independent Poisson gives win/draw/loss.
The displayed score is the likeliest scoreline **within the likeliest result**, so a 40% favorite
shows e.g. 1–0 and not the raw-mode 1–1. Upcoming games are grouped Mon–Sun in collapsible
`<details>` panels (next week open). Matchups between teams not linked by results are guesses and
labeled as such (once per panel if all are unlinked). Massey is ridge-shrunk early season, so
estimates lean toward close scores. That's intended.

## 12. Tracking how the estimates did (added 2026-09-15)

`history/predictions.json` (keyed by AthleteOne matchID) holds the latest estimate for each game.
Every non-simulated build overwrites estimates only for games whose date is **today or later**, so an
estimate freezes once its game day arrives and grading always uses a pre-kickoff estimate.
Grading (`grade()`) runs on played games that have a saved estimate: right result (W/D/L pick),
exact score, and 3-way Brier score (pure guessing = 0.67; lower is better), split by linked
matchups vs guesses. The first 16 games (Sep 12–13) predate tracking and aren't graded.
`--simulate-until` grades against saved estimates but never writes them. Don't commit a
predictions.json produced during a simulation.
