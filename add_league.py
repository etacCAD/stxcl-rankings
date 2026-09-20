#!/usr/bin/env python3
"""
add_league.py — add a league (an AthleteOne flight) to the site.

  python3 add_league.py 41141                 # look it up, add it, pick a slug
  python3 add_league.py 41141 --event 4260 --slug gu13-western --name "GU13 Western"
  python3 add_league.py --list 41120 41180    # browse flights in an id range

The flight and event ids come straight out of the public URL:
  app.athleteone.com/public/event/<event>/schedules-standings/standings/<flight>

After adding, `python3 build_site.py` builds every league in leagues.json, and the page
nav gets a toggle between them. Tournament games are optional per league: create
gotsport/<slug>/teams.json mapping AthleteOne team ids to GotSport team ids.
"""

import argparse
import json
import pathlib
import re

import rank

ROOT = pathlib.Path(__file__).resolve().parent
LEAGUES = ROOT / "leagues.json"


def flight_info(flight_id):
    return rank.get(f"{rank.API}/Event/get-flight-division-by-flightID/{flight_id}")["data"]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("flight", nargs="?", type=int)
    ap.add_argument("--event", type=int, default=4260)
    ap.add_argument("--slug")
    ap.add_argument("--name")
    ap.add_argument("--list", nargs=2, type=int, metavar=("FROM", "TO"),
                    help="print the flights in an id range instead of adding one")
    args = ap.parse_args()

    if args.list:
        for fid in range(args.list[0], args.list[1] + 1):
            try:
                d = flight_info(fid)
            except Exception:
                continue
            if d:
                print(f"{fid}  {d.get('divisionName', ''):10} {d.get('flightName', ''):26} "
                      f"{d.get('teamCount', '?')} teams")
        return

    if not args.flight:
        ap.error("give a flight id, or --list FROM TO")

    info = flight_info(args.flight)
    name = args.name or f"{info.get('divisionName', '')} {info.get('flightName', '')}".strip()
    slug = args.slug or slugify(name)
    leagues = json.loads(LEAGUES.read_text())
    if any(l["flight"] == args.flight and l["event"] == args.event for l in leagues):
        print(f"already there: {name}")
        return
    if any(l.get("slug", "") == slug for l in leagues):
        ap.error(f"slug {slug!r} is taken; pass --slug")

    # confirm the flight really has a schedule before wiring it in
    sched = rank.get(f"{rank.API}/Event/get-schedules-by-flight/{args.event}/{args.flight}/0")["data"]
    teams, played = rank.parse(sched)[0], rank.parse(sched)[1]
    print(f"{name}: {len(teams)} teams, {len(sched)} games scheduled, {len(played)} played")
    for t in sorted(teams.values(), key=lambda t: t["name"]):
        print("   ", t["name"])

    leagues.append({"slug": slug, "name": name, "event": args.event, "flight": args.flight})
    LEAGUES.write_text(json.dumps(leagues, indent=1) + "\n")
    print(f"\nadded to leagues.json as /{slug}/ — run: python3 build_site.py")


if __name__ == "__main__":
    main()
