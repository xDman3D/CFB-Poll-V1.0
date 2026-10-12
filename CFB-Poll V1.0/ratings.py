"""
ratings.py - the rating engine.

Command line (the UI uses the same functions):
    python ratings.py 2024            # whole season
    python ratings.py 2024 8          # only regular-season games through week 8

Every completed game becomes one equation:

    rating(home) - rating(away) + home_field  =  target_margin

target_margin is built in three steps:
  1. RESULT:      real margin, soft-capped, with turnover luck partly removed
  2. EFFICIENCY:  blended with the margin the game's stats say we should have seen
  3. CONTEXT:     rest, travel and altitude edges are taken out, so a team isn't
                  credited (or blamed) for circumstances

All teams are then solved together (opponent adjustment). Ridge "pulls" keep
things sensible when data is thin:
  - ratings are pulled toward a prior, which is mostly the team's CONFERENCE
    average (so conference strength is learned from the games, not assumed)
  - each team's home-field advantage is pulled toward the national average,
    so only teams with consistent evidence get their own number

Usage pattern:  data = load_season(2024)  ->  compute_ratings(data, through_week)
Loading is separate from solving so the backtest can solve hundreds of times fast.
"""
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import efficiency
from app_config import DB_PATH
from efficiency import soft_cap

# ---------------- Tunable settings (the backtest tunes the ones in backtest.GRIDS) ----------------
MARGIN_CAP = 28.0            # soft cap on margin, points

# Opponent adjustment / priors
RIDGE = 1.5                  # pull toward the prior (higher = trust priors longer)
PRIORS = {"fbs": 0.0, "fcs": -14.0}   # placeholder until real preseason priors exist
DEFAULT_PRIOR = -14.0
CONF_PULL = 0.5              # 0 = ignore conferences, 1 = prior is purely conference average
CONF_ITERATIONS = 3
MIN_CONF_SIZE = 4

# Home field
HFA_PRIOR = 2.5              # starting guess for national home-field edge, points
HFA_PRIOR_STRENGTH = 3.0
TEAM_HFA_RIDGE = 25.0        # high = team-specific home edges stay close to the average

# Luck and efficiency
LUCK_REGRESSION = 0.75       # fraction of measured turnover luck removed from each margin
EFF_WEIGHT_ADV = 0.50        # how much of the target comes from advanced stats (FBS)
EFF_WEIGHT_BASIC = 0.25      # ...and from box-score yards/play only (FCS, older games)

# Context
REST_VALUE = 0.15            # points per day of rest advantage
REST_CAP_DAYS = 7
TRAVEL_VALUE = 0.4           # points per 1,000 miles the visitor travels farther
TRAVEL_CAP_MILES = 2500
ALTITUDE_VALUE = 0.5         # points per 1,000 m of elevation gain vs. your home field
ALTITUDE_CAP = 2.0           # (in 1,000 m units). If your data is in feet, adjust this.

# Game tuple layout returned by load_season():
#  0 id, 1 start_date, 2 neutral, 3 home, 4 away, 5 home_pts, 6 away_pts,
#  7 home_class, 8 away_class, 9 raw_json, 10 week, 11 season_type


# ---------------- Helpers ----------------
def haversine_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_support(conn, season):
    """Optional data: luck, efficiency, locations, venues, conferences. Missing = skipped."""
    support = {"luck": {}, "eff": {}, "loc": {}, "venues": {}, "conf": {}}
    try:
        efficiency.fill_game_luck(conn)
        support["luck"] = dict(conn.execute("SELECT game_id, home_luck_pts FROM game_luck").fetchall())
        support["eff"] = efficiency.efficiency_margins(conn, season)
    except sqlite3.OperationalError:
        print("Note: no box score data found. Download data to enable efficiency and luck.")
    try:
        support["loc"] = {s: (la, lo, el) for s, la, lo, el in conn.execute(
            "SELECT school, latitude, longitude, elevation FROM team_locations WHERE season = ?", [season])}
        support["venues"] = {v: (la, lo, el) for v, la, lo, el in conn.execute(
            "SELECT venue_id, latitude, longitude, elevation FROM venues")}
    except sqlite3.OperationalError:
        print("Note: no location data found. Download data to enable travel and altitude.")
    support["conf"] = dict(conn.execute(
        "SELECT school, conference FROM teams WHERE season = ?", [season]).fetchall())
    return support


# ---------------- Context ----------------
def rest_days(games):
    """{game_id: (home_rest, away_rest)} in days since each team's previous game."""
    last, out = {}, {}
    dated = [(parse_date(g[1]), g) for g in games]
    for when, g in sorted((d for d in dated if d[0]), key=lambda d: d[0]):
        gid, home, away = g[0], g[3], g[4]
        out[gid] = tuple((when - last[t]).days if t in last else None for t in (home, away))
        last[home] = last[away] = when
    return out


def context_points(game, rest, loc, venues):
    """Points of edge the HOME team enjoyed from rest, travel and altitude."""
    gid, neutral, home, away, raw = game[0], game[2], game[3], game[4], game[9]
    adj = 0.0

    r = rest.get(gid)
    if r and None not in r:
        adj += REST_VALUE * max(-REST_CAP_DAYS, min(REST_CAP_DAYS, r[0] - r[1]))

    if neutral:
        try:
            rec = json.loads(raw)
            venue = venues.get(rec.get("venueId", rec.get("venue_id")))
        except (TypeError, ValueError):
            venue = None
    else:
        venue = loc.get(home)            # home team plays at its own place
    if venue is None:
        return adj

    def travel_and_climb(team):
        if team == home and not neutral:
            return 0.0, 0.0
        home_loc = loc.get(team)
        if home_loc is None:
            return 0.0, 0.0
        miles = min(haversine_miles(home_loc[0], home_loc[1], venue[0], venue[1]), TRAVEL_CAP_MILES)
        climb = 0.0
        if home_loc[2] is not None and venue[2] is not None:
            climb = min(max(0.0, (venue[2] - home_loc[2]) / 1000.0), ALTITUDE_CAP)
        return miles, climb

    h_miles, h_climb = travel_and_climb(home)
    a_miles, a_climb = travel_and_climb(away)
    adj += TRAVEL_VALUE * (a_miles - h_miles) / 1000.0
    adj += ALTITUDE_VALUE * (a_climb - h_climb)
    return adj


# ---------------- Loading and solving ----------------
def load_season(season):
    """Read one season from the database. Do this once, then solve as often as you like."""
    conn = sqlite3.connect(DB_PATH)
    try:
        games = conn.execute(
            """
            SELECT id, start_date, neutral_site, home_team, away_team,
                   home_points, away_points, home_classification, away_classification,
                   raw, week, season_type
            FROM games
            WHERE season = ? AND completed = 1
              AND home_points IS NOT NULL AND away_points IS NOT NULL
            """,
            [season],
        ).fetchall()
        if not games:
            raise ValueError(f"No completed games stored for {season}. Download that season first.")
        sup = load_support(conn, season)
    finally:
        conn.close()
    return {"season": season, "games": games, "sup": sup, "rest": rest_days(games)}


def compute_ratings(data, through_week=None):
    """
    Solve the ratings. through_week=None uses every game (postseason included);
    through_week=N uses only regular-season games in weeks <= N.
    Returns (rows, info). rows = [(team, rating, division, conference, home_edge)] best first.
    """
    sup = data["sup"]
    games = data["games"]
    if through_week is not None:
        games = [g for g in games if g[11] == "regular" and g[10] is not None and g[10] <= through_week]
    if not games:
        raise ValueError("No completed games in that range yet.")

    teams, division = {}, {}
    for g in games:
        for name, cls in ((g[3], g[7]), (g[4], g[8])):
            teams.setdefault(name, len(teams))
            if cls:
                division[name] = cls.lower()
    n = len(teams)
    names = sorted(teams, key=teams.get)

    G = len(games)
    n_cols = 2 * n + 1                       # ratings | national HFA | per-team HFA deviation
    A = np.zeros((G + n + 1 + n, n_cols))
    y = np.zeros(A.shape[0])
    used_adv = used_basic = 0
    for i, g in enumerate(games):
        gid, _, neutral, home, away, hp, ap = g[:7]
        target = float(soft_cap(hp - ap, MARGIN_CAP)) - LUCK_REGRESSION * sup["luck"].get(gid, 0.0)
        if gid in sup["eff"]:
            e_margin, kind = sup["eff"][gid]
            w = EFF_WEIGHT_ADV if kind == "adv" else EFF_WEIGHT_BASIC
            target = (1 - w) * target + w * e_margin
            used_adv += kind == "adv"
            used_basic += kind == "basic"
        target -= context_points(g, data["rest"], sup["loc"], sup["venues"])

        A[i, teams[home]], A[i, teams[away]] = 1.0, -1.0
        if not neutral:
            A[i, n] = 1.0
            A[i, n + 1 + teams[home]] = 1.0
        y[i] = target

    # Ridge rows (rating prior, national HFA prior, team HFA deviation).
    sr = math.sqrt(RIDGE)
    for idx in range(n):
        A[G + idx, idx] = sr
    A[G + n, n] = math.sqrt(HFA_PRIOR_STRENGTH)
    y[G + n] = math.sqrt(HFA_PRIOR_STRENGTH) * HFA_PRIOR
    for idx in range(n):
        A[G + n + 1 + idx, n + 1 + idx] = math.sqrt(TEAM_HFA_RIDGE)

    base_prior = np.array([PRIORS.get(division.get(t, ""), DEFAULT_PRIOR) for t in names])
    members = {}
    for idx, t in enumerate(names):
        c = sup["conf"].get(t)
        if c and "independent" not in c.lower():
            members.setdefault(c, []).append(idx)
    members = {c: ix for c, ix in members.items() if len(ix) >= MIN_CONF_SIZE}

    prior = base_prior.copy()
    for it in range(CONF_ITERATIONS + 1):
        y[G:G + n] = sr * prior
        sol, *_ = np.linalg.lstsq(A, y, rcond=None)
        if it < CONF_ITERATIONS:
            prior = base_prior.copy()
            for ix in members.values():
                mean = sol[ix].mean()
                for j in ix:
                    prior[j] = (1 - CONF_PULL) * base_prior[j] + CONF_PULL * mean

    rows = [
        (names[i], float(sol[i]), division.get(names[i], "?"),
         sup["conf"].get(names[i], ""), float(sol[n] + sol[n + 1 + i]))
        for i in range(n)
    ]
    rows.sort(key=lambda r: r[1], reverse=True)
    info = {
        "games": G, "adv": used_adv, "basic": used_basic,
        "context": bool(sup["loc"]), "hfa": float(sol[n]),
        "rating": {r[0]: r[1] for r in rows}, "team_hfa": {r[0]: r[4] for r in rows},
    }
    return rows, info


def predict(info, game, data):
    """Expected home-minus-away margin for a game, using ratings solved BEFORE it was played."""
    home, away = game[3], game[4]
    r_h = info["rating"].get(home, PRIORS.get((game[7] or "").lower(), DEFAULT_PRIOR))
    r_a = info["rating"].get(away, PRIORS.get((game[8] or "").lower(), DEFAULT_PRIOR))
    pred = r_h - r_a
    if not game[2]:
        pred += info["team_hfa"].get(home, info["hfa"])
    return pred + context_points(game, data["rest"], data["sup"]["loc"], data["sup"]["venues"])


def summary_line(info):
    return (f"{info['games']} games | efficiency data: {info['adv']} advanced, {info['basic']} basic"
            f" | context data: {'yes' if info['context'] else 'NO'} | home-field edge: {info['hfa']:.1f} pts")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python ratings.py <season> [through_week]")
    season = int(sys.argv[1])
    week = int(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        rows, info = compute_ratings(load_season(season), week)
    except ValueError as e:
        raise SystemExit(str(e))

    print(f"\nTop 25 - {season}" + (f" through week {week}" if week else ""))
    for rank, (team, rating, div, conf, _) in enumerate(rows[:25], 1):
        print(f"{rank:>3}. {team:<26} {rating:>6.1f}  {div.upper():<4} {conf}")
    print("\n" + summary_line(info))

    out = Path(f"rankings_{season}" + (f"_wk{week}" if week else "") + ".csv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("rank,team,rating,division,conference,home_field_edge\n")
        for rank, (team, rating, div, conf, hfa) in enumerate(rows, 1):
            f.write(f'{rank},"{team}",{rating:.2f},{div},"{conf}",{hfa:.2f}\n')
    print(f"Full ranking of {len(rows)} teams saved to {out.resolve()}")


if __name__ == "__main__":
    main()
