"""
backtest.py - historic training.

BACKTEST: for every week of every chosen season, build the ratings using only
games BEFORE that week, predict that week's games, and score the predictions.
That's the honest test of whether the model works - it never sees the games
it's predicting.

  MAE        average miss on the margin, in points (lower is better)
  Accuracy   how often it picked the winner
  Baseline   the MAE of just guessing "home team wins by the home-field edge"
             - the model has to beat this to be worth anything

AUTO-TUNE: tries alternative values for each setting in GRIDS, one at a time,
keeping any change that lowers MAE. The result is saved to settings.json and
used from then on.

Caveats: the efficiency models are fit on every season in the database (just a
few coefficients, so leakage is tiny), and early-season weeks are hard because
the preseason priors are still placeholders.
"""
import numpy as np

import ratings
from app_config import CANCEL, load_settings, save_settings

GRIDS = {
    "RIDGE": [0.75, 1.5, 3.0, 6.0],
    "CONF_PULL": [0.0, 0.5, 0.8],
    "TEAM_HFA_RIDGE": [10.0, 25.0, 100.0],
    "LUCK_REGRESSION": [0.0, 0.5, 0.75, 1.0],
    "EFF_WEIGHT_ADV": [0.0, 0.25, 0.5, 0.75],
    "EFF_WEIGHT_BASIC": [0.0, 0.25, 0.5],
    "REST_VALUE": [0.0, 0.15, 0.3],
    "TRAVEL_VALUE": [0.0, 0.4, 0.8],
    "ALTITUDE_VALUE": [0.0, 0.5, 1.0],
}
DEFAULTS = {name: getattr(ratings, name) for name in GRIDS}   # captured at import time


class Cancelled(Exception):
    pass


def load_datasets(seasons):
    out = {}
    for s in seasons:
        try:
            out[s] = ratings.load_season(s)
            print(f"  {s}: {len(out[s]['games'])} games loaded")
        except ValueError as e:
            print(f"  {s}: skipped ({e})")
    if not out:
        raise ValueError("None of those seasons have data. Download them on the Setup tab first.")
    return out


def evaluate(datasets, weeks):
    abs_err, base_err, correct, decided = [], [], 0, 0
    for season, data in datasets.items():
        by_week = {}
        for g in data["games"]:
            if g[11] == "regular" and g[10] in weeks:
                by_week.setdefault(g[10], []).append(g)
        for w in sorted(by_week):
            if CANCEL.is_set():
                raise Cancelled()
            try:
                _, info = ratings.compute_ratings(data, through_week=w - 1)
            except ValueError:
                continue
            for g in by_week[w]:
                actual = g[5] - g[6]
                pred = ratings.predict(info, g, data)
                abs_err.append(abs(actual - pred))
                base_err.append(abs(actual - (0.0 if g[2] else ratings.HFA_PRIOR)))
                if actual != 0:
                    decided += 1
                    correct += (actual > 0) == (pred > 0)
    if not abs_err:
        raise ValueError("No games to evaluate for those seasons/weeks.")
    return {
        "mae": float(np.mean(abs_err)),
        "baseline_mae": float(np.mean(base_err)),
        "accuracy": correct / max(decided, 1),
        "games": len(abs_err),
    }


def describe(score):
    return (f"{score['games']} games predicted | average miss {score['mae']:.2f} pts "
            f"(home-field-only baseline {score['baseline_mae']:.2f}) | "
            f"picked the winner {score['accuracy'] * 100:.1f}% of the time")


def tune(datasets, weeks, rounds=1):
    current = {name: getattr(ratings, name) for name in GRIDS}
    print("Scoring current settings...")
    best = evaluate(datasets, weeks)
    print("  start: " + describe(best))
    total = rounds * sum(len([v for v in opts if v != current[n]]) for n, opts in GRIDS.items())
    done = 0
    try:
        for r in range(rounds):
            for name, options in GRIDS.items():
                start = current[name]
                for value in options:
                    if value == start:
                        continue
                    done += 1
                    setattr(ratings, name, value)
                    score = evaluate(datasets, weeks)
                    kept = score["mae"] < best["mae"] - 0.005
                    print(f"  [{done}/{total}] {name} = {value}: miss {score['mae']:.3f}"
                          + ("  <- better, keeping" if kept else ""))
                    if kept:
                        best, current[name] = score, value
                    setattr(ratings, name, current[name])
    except Cancelled:
        print("Stopped early - keeping the best settings found so far.")
    for name, value in current.items():
        setattr(ratings, name, value)
    return current, best


def save_tuned(params):
    settings = load_settings()
    settings["params"] = params
    save_settings(settings)


def reset_defaults():
    for name, value in DEFAULTS.items():
        setattr(ratings, name, value)
    settings = load_settings()
    settings.pop("params", None)
    save_settings(settings)
