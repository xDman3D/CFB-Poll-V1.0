"""
efficiency.py - efficiency-based margins and turnover-luck measurement.

Two jobs:

1. EFFICIENCY MARGIN. The scoreboard is a noisy summary of how a game went.
   Success rate, explosiveness and yards per play say more about who played
   better. We fit a regression on your own historical games that answers:
   "given these stat differences, what margin would we expect?"
   That expected margin then gets blended with the real margin in ratings.py.

     ADV model   (success rate, explosiveness, yards/play) - needs advanced stats (FBS)
     BASIC model (yards/play only)                         - works with box scores (FCS too)

   Turnovers are deliberately NOT in the efficiency model. They're the noisy,
   luck-heavy part, which is exactly what we want it to ignore.

2. LUCK. Fumble recoveries are close to a coin flip, and interceptions are
   partly luck (tipped balls, great catches). For each game we estimate how
   many points of margin came from turnover luck and store it in `game_luck`,
   which ratings.py then partially removes (LUCK_REGRESSION).

Run on its own to see how well the models fit:   python efficiency.py
"""
import sqlite3
from pathlib import Path

import numpy as np

from app_config import DB_PATH

MARGIN_CAP = 28.0
TURNOVER_VALUE = 4.0     # points a turnover swing is worth
INT_LUCK_SHARE = 0.3     # share of interception margin treated as luck (fumbles: 100%)
MIN_GAMES_TO_FIT = 150


def soft_cap(margin, cap=MARGIN_CAP):
    return cap * np.tanh(np.asarray(margin, dtype=float) / cap)


_FRAME_SQL = """
    SELECT g.id, g.season, g.home_points, g.away_points, g.neutral_site,
           h.total_yards, h.rush_att + h.pass_att,
           a.total_yards, a.rush_att + a.pass_att,
           ah.off_success, ah.off_explosiveness,
           aa.off_success, aa.off_explosiveness
    FROM games g
    JOIN team_game_stats h ON h.game_id = g.id AND h.team = g.home_team
    JOIN team_game_stats a ON a.game_id = g.id AND a.team = g.away_team
    LEFT JOIN game_adv ah ON ah.game_id = g.id AND ah.team = g.home_team
    LEFT JOIN game_adv aa ON aa.game_id = g.id AND aa.team = g.away_team
    WHERE g.completed = 1 AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
"""


def _frame(conn, season=None):
    """One dict per game with the stat differences (home minus away)."""
    sql = _FRAME_SQL + (" AND g.season = ?" if season is not None else "")
    rows = conn.execute(sql, [season] if season is not None else []).fetchall()
    out = []
    for gid, _, hp, ap, neutral, hy, hpl, ay, apl, hs, he, as_, ae in rows:
        if not (hy is not None and ay is not None and hpl and apl):
            continue
        rec = {
            "id": gid,
            "margin": float(soft_cap(hp - ap)),
            "home_flag": 0.0 if neutral else 1.0,
            "d_ypp": hy / hpl - ay / apl,
            "adv": None,
        }
        if None not in (hs, he, as_, ae):
            rec["adv"] = (hs - as_, he - ae)   # (success diff, explosiveness diff)
        out.append(rec)
    return out


def _fit(X, y):
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    r2 = 1 - (resid ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return beta, r2


def fit_models(conn):
    """Fit both models on every season in the database. Returns {name: (beta, r2, n)}."""
    frame = _frame(conn)
    models = {}
    adv = [r for r in frame if r["adv"] is not None]
    if len(adv) >= MIN_GAMES_TO_FIT:
        X = np.array([[r["adv"][0], r["adv"][1], r["d_ypp"], r["home_flag"]] for r in adv])
        y = np.array([r["margin"] for r in adv])
        beta, r2 = _fit(X, y)
        models["adv"] = (beta, r2, len(adv))
    if len(frame) >= MIN_GAMES_TO_FIT:
        X = np.array([[r["d_ypp"], r["home_flag"]] for r in frame])
        y = np.array([r["margin"] for r in frame])
        beta, r2 = _fit(X, y)
        models["basic"] = (beta, r2, len(frame))
    return models


def efficiency_margins(conn, season, models=None):
    """{game_id: (expected_margin, 'adv' or 'basic')} for one season."""
    models = models if models is not None else fit_models(conn)
    result = {}
    for r in _frame(conn, season):
        if r["adv"] is not None and "adv" in models:
            b = models["adv"][0]
            pred = b[0] * r["adv"][0] + b[1] * r["adv"][1] + b[2] * r["d_ypp"] + b[3] * r["home_flag"]
            result[r["id"]] = (float(pred), "adv")
        elif "basic" in models:
            b = models["basic"][0]
            pred = b[0] * r["d_ypp"] + b[1] * r["home_flag"]
            result[r["id"]] = (float(pred), "basic")
    return result


def fill_game_luck(conn):
    """(Re)build game_luck: points of home-team margin that came from turnover luck."""
    conn.execute("CREATE TABLE IF NOT EXISTS game_luck (game_id INTEGER PRIMARY KEY, home_luck_pts REAL)")
    rows = conn.execute(
        """
        SELECT g.id, h.turnovers, h.fumbles_lost, a.turnovers, a.fumbles_lost
        FROM games g
        JOIN team_game_stats h ON h.game_id = g.id AND h.team = g.home_team
        JOIN team_game_stats a ON a.game_id = g.id AND a.team = g.away_team
        WHERE h.turnovers IS NOT NULL AND a.turnovers IS NOT NULL
          AND h.fumbles_lost IS NOT NULL AND a.fumbles_lost IS NOT NULL
        """
    ).fetchall()
    out = []
    for gid, h_to, h_fl, a_to, a_fl in rows:
        h_int, a_int = h_to - h_fl, a_to - a_fl      # interceptions thrown
        fumble_swing = a_fl - h_fl                   # fumbles the home team recovered, net
        int_swing = a_int - h_int                    # interceptions the home team made, net
        out.append((gid, TURNOVER_VALUE * (fumble_swing + INT_LUCK_SHARE * int_swing)))
    conn.executemany("INSERT OR REPLACE INTO game_luck VALUES (?,?)", out)
    conn.commit()
    return len(out)


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    models = fit_models(conn)
    if not models:
        print("Not enough stat data yet. Run cfb_stats.py for a few seasons first.")
    for name, (beta, r2, n) in models.items():
        print(f"{name.upper():<6} model: {n} games, R^2 = {r2:.2f}, coefficients = {np.round(beta, 2)}")
    print(f"Luck estimated for {fill_game_luck(conn)} games")
