"""
cfb_stats.py - loads the extra data the context and efficiency factors need.

    python cfb_stats.py 2024 [2025 ...]     load these seasons (run cfb_data.py first)
    python cfb_stats.py --inspect 2024      print one raw record from each endpoint,
                                            so field names are easy to check/fix

Loads into cfb.db:
  team_locations   home coordinates + elevation of every team   (travel, altitude)
  venues           coordinates + elevation of every stadium     (neutral-site travel)
  team_game_stats  box score numbers per team per game          (yards/play, turnovers)
  game_adv         advanced stats per team per game             (success rate, explosiveness)

Advanced stats are pulled with garbage time excluded, which keeps blowout
junk-time from distorting the efficiency numbers. They're FBS-only in practice;
FCS games fall back to the simpler box score numbers.

API usage: roughly 50-60 calls per season. Check your monthly call limit.
"""
import json
import sqlite3
import sys

import requests

from cfb_data import CLASSIFICATIONS, DB_PATH, api_get

SEASON_PARTS = (("regular", range(0, 16)), ("postseason", [1]))


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def safe_get(path, params=None):
    """api_get, but a bad/empty week just returns [] instead of stopping everything."""
    try:
        return api_get(path, params) or []
    except requests.HTTPError as e:
        print(f"    skipped {path} {params}: {e}")
        return []


def init_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS team_locations (
            season INTEGER, school TEXT, latitude REAL, longitude REAL, elevation REAL,
            PRIMARY KEY (season, school)
        );
        CREATE TABLE IF NOT EXISTS venues (
            venue_id INTEGER PRIMARY KEY, name TEXT,
            latitude REAL, longitude REAL, elevation REAL
        );
        CREATE TABLE IF NOT EXISTS team_game_stats (
            game_id INTEGER, team TEXT, home_away TEXT, points REAL,
            rush_att REAL, pass_att REAL, total_yards REAL,
            turnovers REAL, fumbles_lost REAL,
            PRIMARY KEY (game_id, team)
        );
        CREATE TABLE IF NOT EXISTS game_adv (
            game_id INTEGER, team TEXT,
            off_success REAL, off_explosiveness REAL, off_ppa REAL, off_plays REAL,
            PRIMARY KEY (game_id, team)
        );
        """
    )


def coords(obj):
    """Pull (lat, lon, elevation) out of a team or venue record, whatever its shape."""
    loc = obj.get("location") if isinstance(obj.get("location"), dict) else obj
    lat = to_num(loc.get("latitude", loc.get("y")))
    lon = to_num(loc.get("longitude", loc.get("x")))
    elev = to_num(loc.get("elevation", obj.get("elevation")))
    return lat, lon, elev


def load_locations(conn, season):
    rows = []
    for t in api_get("/teams", {"year": season}):
        lat, lon, elev = coords(t)
        if lat is not None and lon is not None:
            rows.append((season, t["school"], lat, lon, elev))
    conn.executemany("INSERT OR REPLACE INTO team_locations VALUES (?,?,?,?,?)", rows)
    print(f"  {season}: {len(rows)} team locations")


def load_venues(conn):
    rows = []
    for v in api_get("/venues"):
        lat, lon, elev = coords(v)
        if lat is not None and lon is not None:
            rows.append((v["id"], v.get("name"), lat, lon, elev))
    conn.executemany("INSERT OR REPLACE INTO venues VALUES (?,?,?,?,?)", rows)
    print(f"  {len(rows)} venues")


def parse_game_team_stats(game):
    gid = game.get("id", game.get("gameId"))
    rows = []
    for t in game.get("teams", []):
        stats = {s.get("category"): s.get("stat") for s in t.get("stats", [])}
        pass_att = None
        comp_att = stats.get("completionAttempts")  # looks like "17-28"
        if isinstance(comp_att, str) and "-" in comp_att:
            pass_att = to_num(comp_att.split("-")[1])
        turnovers = to_num(stats.get("turnovers"))
        fumbles_lost = to_num(stats.get("fumblesLost"))
        if fumbles_lost is None and turnovers is not None:
            fumbles_lost = 0.0  # stat is often omitted when it's zero
        rows.append(
            (
                gid,
                t.get("school") or t.get("team"),
                t.get("homeAway") or t.get("home_away"),
                to_num(t.get("points")),
                to_num(stats.get("rushingAttempts")),
                pass_att,
                to_num(stats.get("totalYards")),
                turnovers,
                fumbles_lost,
            )
        )
    return rows


def load_game_stats(conn, season):
    total = 0
    for season_type, weeks in SEASON_PARTS:
        for week in weeks:
            for cls in CLASSIFICATIONS:
                data = safe_get(
                    "/games/teams",
                    {"year": season, "week": week, "seasonType": season_type, "classification": cls},
                )
                for game in data:
                    rows = parse_game_team_stats(game)
                    conn.executemany(
                        "INSERT OR REPLACE INTO team_game_stats VALUES (?,?,?,?,?,?,?,?,?)", rows
                    )
                    total += len(rows)
        conn.commit()
    print(f"  {season}: {total} team box scores")


def load_advanced(conn, season):
    total = 0
    for season_type, weeks in SEASON_PARTS:
        for week in weeks:
            data = safe_get(
                "/stats/game/advanced",
                {"year": season, "week": week, "seasonType": season_type, "excludeGarbageTime": "true"},
            )
            rows = []
            for r in data:
                off = r.get("offense") or {}
                rows.append(
                    (
                        r.get("gameId"),
                        r.get("team"),
                        to_num(off.get("successRate")),
                        to_num(off.get("explosiveness")),
                        to_num(off.get("ppa")),
                        to_num(off.get("plays")),
                    )
                )
            conn.executemany("INSERT OR REPLACE INTO game_adv VALUES (?,?,?,?,?,?)", rows)
            total += len(rows)
        conn.commit()
    print(f"  {season}: {total} advanced team-game lines")


def inspect(season):
    """Print one raw record from each endpoint so you can compare against the loaders."""
    def show(label, data):
        print(f"\n=== {label} ({len(data)} records) ===")
        if data:
            print(json.dumps(data[0], indent=1)[:1800])

    show("/teams", api_get("/teams", {"year": season}))
    show("/venues", api_get("/venues"))
    show("/games/teams", api_get("/games/teams", {"year": season, "week": 5, "classification": "fbs"}))
    show("/stats/game/advanced", api_get("/stats/game/advanced", {"year": season, "week": 5, "excludeGarbageTime": "true"}))


def main(args):
    if args and args[0] == "--inspect":
        inspect(int(args[1]))
        return
    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    print("Loading venues...")
    load_venues(conn)
    for season in [int(a) for a in args]:
        print(f"Loading {season}...")
        load_locations(conn, season)
        load_game_stats(conn, season)
        load_advanced(conn, season)
    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python cfb_stats.py 2024 [2025 ...]   or   --inspect 2024")
    main(sys.argv[1:])
