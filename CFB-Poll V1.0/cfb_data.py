"""
cfb_data.py - data layer for the computer poll.

Pulls teams and games (FBS + FCS) from the College Football Data API
and caches them in a local SQLite file so the poll can run offline
and you don't burn through your API call quota.

Setup:
  1. Get a free API key at https://collegefootballdata.com
  2. Set it as an environment variable:   setx CFBD_API_KEY "your_key_here"
     (or put it in a file named api_key.txt next to this script)
  3. pip install requests
  4. python cfb_data.py 2024 2025        (loads those seasons)
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://api.collegefootballdata.com"
import app_config

DB_PATH = app_config.DB_PATH
CLASSIFICATIONS = ["fbs", "fcs"]


def get_api_key() -> str:
    key = app_config.load_settings().get("api_key") or os.environ.get("CFBD_API_KEY")
    if not key:
        key_file = app_config.APP_DIR / "api_key.txt"
        if key_file.exists():
            key = key_file.read_text().strip()
    if not key:
        raise SystemExit("No API key found. Enter it on the Setup tab (or set CFBD_API_KEY).")
    return key


def api_get(path: str, params: dict | None = None):
    headers = {"Authorization": f"Bearer {get_api_key()}", "Accept": "application/json"}
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=60)
    if resp.status_code == 429:
        raise SystemExit("Rate/monthly call limit hit. Wait or check your CFBD tier.")
    resp.raise_for_status()
    time.sleep(0.2)  # be polite
    return resp.json()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS teams (
            season INTEGER, school TEXT, conference TEXT, classification TEXT,
            PRIMARY KEY (season, school)
        );
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY, season INTEGER, week INTEGER, season_type TEXT,
            start_date TEXT, neutral_site INTEGER, conference_game INTEGER,
            home_team TEXT, home_classification TEXT, home_points INTEGER,
            away_team TEXT, away_classification TEXT, away_points INTEGER,
            completed INTEGER, raw TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_games_season_week ON games (season, week);
        """
    )


def load_teams(conn, season: int) -> None:
    teams = api_get("/teams", {"year": season})
    rows = [
        (season, t["school"], t.get("conference"), t.get("classification"))
        for t in teams
    ]
    conn.executemany("INSERT OR REPLACE INTO teams VALUES (?,?,?,?)", rows)
    print(f"  {season}: {len(rows)} teams")


def load_games(conn, season: int) -> None:
    total = 0
    for season_type in ("regular", "postseason"):
        for cls in CLASSIFICATIONS:
            games = api_get(
                "/games",
                {"year": season, "seasonType": season_type, "classification": cls},
            )
            for g in games:
                # Field names have shifted between API versions, so accept both styles.
                def pick(*names):
                    for n in names:
                        if n in g and g[n] is not None:
                            return g[n]
                    return None

                conn.execute(
                    "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        g["id"],
                        season,
                        g.get("week"),
                        season_type,
                        pick("startDate", "start_date"),
                        int(bool(pick("neutralSite", "neutral_site"))),
                        int(bool(pick("conferenceGame", "conference_game"))),
                        pick("homeTeam", "home_team"),
                        pick("homeClassification", "home_classification"),
                        pick("homePoints", "home_points"),
                        pick("awayTeam", "away_team"),
                        pick("awayClassification", "away_classification"),
                        pick("awayPoints", "away_points"),
                        int(bool(g.get("completed"))),
                        json.dumps(g),
                    ),
                )
                total += 1
    conn.commit()
    print(f"  {season}: {total} games")


def main(seasons: list[int]) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    for season in seasons:
        print(f"Loading {season}...")
        load_teams(conn, season)
        load_games(conn, season)
    conn.commit()
    conn.close()
    print(f"Done. Data saved to {DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python cfb_data.py 2024 [2025 ...]")
    main([int(a) for a in sys.argv[1:]])
