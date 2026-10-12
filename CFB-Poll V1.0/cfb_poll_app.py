"""
cfb_poll_app.py - the whole thing in one window.

  Tab 1  Setup & Data   enter your API key, download historic seasons, update the current one
  Tab 2  Train          check the efficiency models, backtest on past seasons, auto-tune settings
  Tab 3  Rankings       generate the poll for any season/week, filter FBS/FCS, export to CSV

Run it directly with `python cfb_poll_app.py`, or package it as an .exe with build_exe.bat.
Long jobs run in the background so the window never freezes; progress shows in the log.
"""
import contextlib
import csv
import datetime
import queue
import sqlite3
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, scrolledtext, ttk

import app_config
import backtest          # imported before apply_tuned_params so defaults are captured first
import cfb_data
import cfb_stats
import efficiency
import ratings
import updater
from app_config import CANCEL, DB_PATH

app_config.apply_tuned_params()


def current_season():
    today = datetime.date.today()
    return today.year if today.month >= 8 else today.year - 1


def loaded_seasons():
    """{season: completed_game_count} for everything stored so far."""
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT season, COUNT(*) FROM games WHERE completed = 1 GROUP BY season ORDER BY season"
            ).fetchall()
        finally:
            conn.close()
        return dict(rows)
    except sqlite3.OperationalError:
        return {}


# ---------------- Background tasks (run in a worker thread) ----------------
def task_download(seasons, force):
    conn = sqlite3.connect(DB_PATH)
    try:
        cfb_data.init_db(conn)
        cfb_stats.init_tables(conn)
        print("Loading stadium data...")
        cfb_stats.load_venues(conn)
        for s in seasons:
            if CANCEL.is_set():
                print("Cancelled.")
                break
            done = conn.execute(
                "SELECT COUNT(*) FROM games g WHERE g.season = ? AND g.completed = 1 "
                "AND EXISTS (SELECT 1 FROM team_game_stats t WHERE t.game_id = g.id)", (s,)
            ).fetchone()[0]
            if done and not force:
                print(f"{s}: already downloaded, skipping")
                continue
            print(f"Downloading {s}...")
            cfb_data.load_teams(conn, s)
            cfb_data.load_games(conn, s)
            cfb_stats.load_locations(conn, s)
            cfb_stats.load_game_stats(conn, s)
            cfb_stats.load_advanced(conn, s)
        conn.commit()
    finally:
        conn.close()
    print("Download finished.")
    return True


def task_fit():
    conn = sqlite3.connect(DB_PATH)
    try:
        models = efficiency.fit_models(conn)
        if not models:
            print("Not enough box score data yet. Download a few seasons first.")
            return None
        for name, (beta, r2, n) in models.items():
            print(f"{name.upper():<6} model: {n} games, R^2 = {r2:.2f}")
        print(f"Luck estimated for {efficiency.fill_game_luck(conn)} games")
        print("(R^2 near 1 = the stats explain margin very well. These refit automatically.)")
        return True
    finally:
        conn.close()


def task_backtest(seasons):
    print("Loading seasons...")
    datasets = backtest.load_datasets(seasons)
    print("Predicting every game from weeks 3-14 using only earlier results...")
    score = backtest.evaluate(datasets, range(3, 15))
    print("\nRESULT: " + backtest.describe(score))
    return score


def task_tune(seasons):
    print("Loading seasons...")
    datasets = backtest.load_datasets(seasons)
    params, best = backtest.tune(datasets, range(4, 15, 2))
    backtest.save_tuned(params)
    print("\nBest settings found and saved:")
    for k, v in params.items():
        print(f"  {k} = {v}")
    print("RESULT: " + backtest.describe(best))
    return best


def task_rank(season, week):
    data = ratings.load_season(season)
    return ratings.compute_ratings(data, week)


def task_check_update(url):
    print("Checking for updates...")
    try:
        manifest = updater.check_online(url)
    except updater.UpdateError as e:
        raise SystemExit(str(e))
    if manifest is None:
        print("You're on the latest version.")
        return {"up_to_date": True}
    print(f"Version {manifest['version']} is available.")
    return manifest


def task_install_update(manifest):
    try:
        return updater.download_and_install(manifest)
    except updater.UpdateError as e:
        raise SystemExit(str(e))


# ---------------- UI plumbing ----------------
class QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"CFB Computer Poll  v{updater.running_version()}")
        root.geometry("1000x760")
        self.q = queue.Queue()
        self.busy = False
        self.rows, self.rank_info, self.rank_label = [], {}, ""
        self.buttons = []

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tab_setup, self.tab_train, self.tab_rank, self.tab_updates = (ttk.Frame(nb, padding=10) for _ in range(4))
        nb.add(self.tab_setup, text="1. Setup & Data")
        nb.add(self.tab_train, text="2. Train")
        nb.add(self.tab_rank, text="3. Rankings")
        nb.add(self.tab_updates, text="4. Updates")
        self._build_setup()
        self._build_train()
        self._build_rank()
        self._build_updates()

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.log = scrolledtext.ScrolledText(root, height=11, font=("Consolas", 9))
        self.log.pack(fill="both", padx=8, pady=(2, 8))

        self.refresh_summary()
        self.refresh_version()
        self.show_startup_notes()
        root.after(100, self._poll)

    # ----- helpers -----
    def _button(self, parent, text, command):
        b = ttk.Button(parent, text=text, command=command)
        self.buttons.append(b)
        return b

    def _spin(self, parent, var, width=7):
        return ttk.Spinbox(parent, from_=2000, to=2100, textvariable=var, width=width)

    def write_log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def cancel(self):
        CANCEL.set()
        self.status.set("Cancelling...")

    def run(self, label, func, on_done=None):
        if self.busy:
            return
        self.busy = True
        CANCEL.clear()
        self.status.set(label + "...")
        self.cancel_btn.config(state="normal")
        for b in self.buttons:
            b.config(state="disabled")
        self.write_log(f"\n--- {label} ---\n")

        def worker():
            try:
                with contextlib.redirect_stdout(QueueWriter(self.q)):
                    result = func()
                self.q.put(("done", result, on_done))
            except backtest.Cancelled:
                self.q.put(("log", "\nCancelled.\n"))
                self.q.put(("done", None, None))
            except SystemExit as e:
                self.q.put(("log", f"\nERROR: {e}\n"))
                self.q.put(("done", None, None))
            except Exception as e:
                self.q.put(("log", f"\nERROR: {e}\n{traceback.format_exc()}\n"))
                self.q.put(("done", None, None))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self.write_log(msg[1])
                else:
                    _, result, on_done = msg
                    self.busy = False
                    self.status.set("Ready")
                    self.cancel_btn.config(state="disabled")
                    for b in self.buttons:
                        b.config(state="normal")
                    self.refresh_summary()
                    if on_done and result is not None:
                        on_done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ----- tab 1: setup -----
    def _build_setup(self):
        t, cur = self.tab_setup, current_season()
        ttk.Label(t, text="Free API key from collegefootballdata.com:").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar(value=app_config.load_settings().get("api_key", ""))
        ttk.Entry(t, textvariable=self.key_var, show="*", width=48).grid(row=0, column=1, columnspan=3, sticky="w", padx=6)
        self._button(t, "Save key", self.save_key).grid(row=0, column=4, padx=6)

        ttk.Label(t, text="Historic seasons:").grid(row=1, column=0, sticky="w", pady=(18, 0))
        self.hist_from, self.hist_to = tk.IntVar(value=cur - 5), tk.IntVar(value=cur - 1)
        self._spin(t, self.hist_from).grid(row=1, column=1, sticky="w", padx=6, pady=(18, 0))
        ttk.Label(t, text="to").grid(row=1, column=2, pady=(18, 0))
        self._spin(t, self.hist_to).grid(row=1, column=3, sticky="w", padx=6, pady=(18, 0))
        self._button(t, "Download missing seasons", self.download_hist).grid(row=1, column=4, padx=6, pady=(18, 0))

        ttk.Label(t, text="Current season:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.cur_var = tk.IntVar(value=cur)
        self._spin(t, self.cur_var).grid(row=2, column=1, sticky="w", padx=6, pady=(10, 0))
        self._button(t, "Update current season", self.download_current).grid(row=2, column=4, padx=6, pady=(10, 0))

        self.summary = tk.StringVar()
        ttk.Label(t, textvariable=self.summary, wraplength=900, justify="left").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(24, 0))
        ttk.Label(
            t, foreground="gray", wraplength=900, justify="left",
            text=("Each season downloaded uses roughly 60 API calls, so check your monthly limit. "
                  "Historic seasons only download once; 'Update current season' re-downloads the "
                  f"season in progress. Your data and settings are stored in: {app_config.APP_DIR}"),
        ).grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))

    def save_key(self):
        s = app_config.load_settings()
        s["api_key"] = self.key_var.get().strip()
        app_config.save_settings(s)
        self.write_log("API key saved.\n")

    def _download(self, label, seasons, force):
        if not self.key_var.get().strip():
            messagebox.showinfo("API key needed", "Enter your API key and press 'Save key' first.")
            return
        self.save_key()
        self.run(label, lambda: task_download(seasons, force))

    def download_hist(self):
        a, b = sorted((self.hist_from.get(), self.hist_to.get()))
        self._download("Downloading historic seasons", list(range(a, b + 1)), force=False)

    def download_current(self):
        self._download("Updating current season", [self.cur_var.get()], force=True)

    def refresh_summary(self):
        seasons = loaded_seasons()
        if seasons:
            text = "Stored so far: " + ", ".join(f"{s} ({n} games)" for s, n in seasons.items())
        else:
            text = "No data downloaded yet."
        self.summary.set(text)
        if hasattr(self, "season_box"):
            self.season_box["values"] = list(seasons) or [current_season()]
            if not self.season_var.get() or int(self.season_var.get()) not in seasons:
                self.season_var.set(str(max(seasons)) if seasons else "")

    # ----- tab 2: train -----
    def _build_train(self):
        t = self.tab_train
        ttk.Label(
            t, wraplength=900, justify="left",
            text=("Historic training tests the model on past seasons: for every week it uses only earlier "
                  "games to predict that week, then scores itself. Auto-tune then tries different settings "
                  "(how much to trust stats, rest, travel, luck...) and keeps what predicts best. "
                  "Do this after downloading a few seasons."),
        ).grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Label(t, text="Train on seasons:").grid(row=1, column=0, sticky="w", pady=(16, 0))
        cur = current_season()
        self.tr_from, self.tr_to = tk.IntVar(value=cur - 5), tk.IntVar(value=cur - 1)
        self._spin(t, self.tr_from).grid(row=1, column=1, padx=6, pady=(16, 0))
        ttk.Label(t, text="to").grid(row=1, column=2, pady=(16, 0))
        self._spin(t, self.tr_to).grid(row=1, column=3, padx=6, pady=(16, 0))

        row = ttk.Frame(t)
        row.grid(row=2, column=0, columnspan=6, sticky="w", pady=(16, 0))
        self._button(row, "Check efficiency models", lambda: self.run("Checking efficiency models", task_fit)).pack(side="left", padx=(0, 8))
        self._button(row, "Run backtest", self.run_backtest).pack(side="left", padx=(0, 8))
        self._button(row, "Auto-tune settings (slow)", self.run_tune).pack(side="left", padx=(0, 8))
        self._button(row, "Reset to defaults", self.reset).pack(side="left")
        self.train_result = tk.StringVar()
        ttk.Label(t, textvariable=self.train_result, wraplength=900, justify="left").grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(20, 0))

    def _train_seasons(self):
        a, b = sorted((self.tr_from.get(), self.tr_to.get()))
        return list(range(a, b + 1))

    def run_backtest(self):
        seasons = self._train_seasons()
        self.run("Backtesting", lambda: task_backtest(seasons),
                 lambda s: self.train_result.set("Backtest: " + backtest.describe(s)))

    def run_tune(self):
        seasons = self._train_seasons()
        self.run("Auto-tuning", lambda: task_tune(seasons),
                 lambda s: self.train_result.set("Tuned settings saved. " + backtest.describe(s)))

    def reset(self):
        backtest.reset_defaults()
        self.train_result.set("Settings reset to defaults.")
        self.write_log("Settings reset to defaults.\n")

    # ----- tab 3: rankings -----
    def _build_rank(self):
        t = self.tab_rank
        top = ttk.Frame(t)
        top.pack(fill="x")
        ttk.Label(top, text="Season:").pack(side="left")
        self.season_var = tk.StringVar()
        self.season_box = ttk.Combobox(top, textvariable=self.season_var, width=7, state="readonly")
        self.season_box.pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Through week:").pack(side="left")
        self.week_var = tk.StringVar(value="Latest")
        ttk.Combobox(top, textvariable=self.week_var, width=8, state="readonly",
                     values=["Latest"] + [str(w) for w in range(1, 16)]).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Show:").pack(side="left")
        self.div_var = tk.StringVar(value="All")
        div_box = ttk.Combobox(top, textvariable=self.div_var, width=6, state="readonly", values=["All", "FBS", "FCS"])
        div_box.pack(side="left", padx=(4, 12))
        div_box.bind("<<ComboboxSelected>>", lambda e: self.show_table())
        self._button(top, "Generate rankings", self.generate).pack(side="left", padx=(0, 8))
        self._button(top, "Export CSV", self.export).pack(side="left")

        cols = ("rank", "team", "rating", "division", "conference", "home")
        heads = ("#", "Team", "Rating", "Div", "Conference", "Home edge")
        widths = (50, 230, 80, 60, 200, 90)
        frame = ttk.Frame(t)
        frame.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=16)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("team", "conference") else "center")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.rank_note = tk.StringVar()
        ttk.Label(t, textvariable=self.rank_note, foreground="gray").pack(anchor="w", pady=(6, 0))

    def generate(self):
        if not self.season_var.get():
            messagebox.showinfo("No data", "Download at least one season on the Setup tab first.")
            return
        season = int(self.season_var.get())
        week = None if self.week_var.get() == "Latest" else int(self.week_var.get())
        self.rank_label = f"{season}" + (f"_wk{week}" if week else "")
        self.run("Computing rankings", lambda: task_rank(season, week), self.on_ranked)

    def on_ranked(self, result):
        self.rows, self.rank_info = result
        self.show_table()
        self.write_log(ratings.summary_line(self.rank_info) + "\n")

    def visible_rows(self):
        d = self.div_var.get()
        rows = [r for r in self.rows if d == "All" or r[2].upper() == d]
        return rows

    def show_table(self):
        self.tree.delete(*self.tree.get_children())
        for rank, (team, rating, div, conf, hfa) in enumerate(self.visible_rows(), 1):
            self.tree.insert("", "end", values=(rank, team, f"{rating:.1f}", div.upper(), conf, f"{hfa:.1f}"))
        if self.rank_info:
            self.rank_note.set(ratings.summary_line(self.rank_info))

    def export(self):
        rows = self.visible_rows()
        if not rows:
            messagebox.showinfo("Nothing to export", "Generate rankings first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=f"rankings_{self.rank_label or 'poll'}.csv",
            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rank", "team", "rating", "division", "conference", "home_field_edge"])
            for rank, (team, rating, div, conf, hfa) in enumerate(rows, 1):
                w.writerow([rank, team, f"{rating:.2f}", div, conf, f"{hfa:.2f}"])
        self.write_log(f"Exported {len(rows)} teams to {path}\n")

    # ----- tab 4: updates -----
    def _build_updates(self):
        t = self.tab_updates
        self.ver_var = tk.StringVar()
        ttk.Label(t, textvariable=self.ver_var, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            t, wraplength=900, justify="left",
            text=("Updates change the program without rebuilding the .exe. When you get an update package "
                  "(a .zip), install it here and restart. Updates can change how the poll works, but they "
                  "can't add new libraries or change the launcher - those need a one-time rebuild with "
                  "build_exe.bat. If an update ever fails to start, the program rolls back to the last "
                  "working version by itself."),
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._button(t, "Install update from file...", self.install_from_file).grid(
            row=2, column=0, sticky="w", pady=(18, 0))

        ttk.Label(t, text="Online update address (https):").grid(row=3, column=0, sticky="w", pady=(24, 0))
        self.url_var = tk.StringVar(value=app_config.load_settings().get("update_url", ""))
        ttk.Entry(t, textvariable=self.url_var, width=70).grid(
            row=3, column=1, columnspan=3, sticky="w", padx=6, pady=(24, 0))
        self._button(t, "Check online for updates", self.check_online).grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            t, foreground="gray", wraplength=900, justify="left",
            text=("Optional. This is the address of the manifest.json file you host (for example on GitHub). "
                  "An update is code that runs on your computer, so only use an address you control."),
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        row = ttk.Frame(t)
        row.grid(row=6, column=0, columnspan=4, sticky="w", pady=(26, 0))
        self._button(row, "Undo last update", self.undo_update).pack(side="left", padx=(0, 8))
        self._button(row, "Revert to built-in version", self.revert).pack(side="left")

    def refresh_version(self):
        running, installed = updater.running_version(), updater.installed_version()
        text = f"Running version {running}"
        if installed != running:
            text += f"   (version {installed} is installed - restart to use it)"
        self.ver_var.set(text)

    def show_startup_notes(self):
        err = updater.pop_startup_error()
        if err:
            self.write_log("The last update could not start and was rolled back to a working version:\n"
                           + err + "\n")

    def restart_prompt(self, manifest):
        self.refresh_version()
        self.write_log(f"Version {manifest['version']} installed.\n")
        if messagebox.askyesno("Update installed",
                               f"Version {manifest['version']} is installed. Restart now to use it?"):
            updater.relaunch()
            self.root.destroy()

    def install_from_file(self):
        path = filedialog.askopenfilename(title="Choose an update package",
                                          filetypes=[("Update package", "*.zip")])
        if not path:
            return
        try:
            manifest = updater.install_file(path)
        except updater.UpdateError as e:
            messagebox.showerror("Update not installed", str(e))
            return
        self.restart_prompt(manifest)

    def check_online(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("Update address needed", "Paste the https:// address of your manifest.json first.")
            return
        s = app_config.load_settings()
        s["update_url"] = url
        app_config.save_settings(s)
        self.run("Checking for updates", lambda: task_check_update(url), self.offer_update)

    def offer_update(self, manifest):
        if manifest.get("up_to_date"):
            return
        notes = manifest.get("notes", "")
        if messagebox.askyesno("Update available",
                               f"Version {manifest['version']} is available.\n\n{notes}\n\nDownload and install it now?"):
            self.run("Installing update", lambda: task_install_update(manifest), self.restart_prompt)

    def undo_update(self):
        if not updater.undo_last_update():
            messagebox.showinfo("Nothing to undo", "There is no previous update to go back to.")
            return
        self.refresh_version()
        if messagebox.askyesno("Update undone",
                               f"Switched to version {updater.installed_version()}. Restart now to use it?"):
            updater.relaunch()
            self.root.destroy()

    def revert(self):
        if not messagebox.askyesno("Revert", "Remove all installed updates and go back to the version "
                                             "built into the .exe?"):
            return
        updater.revert_to_builtin()
        self.refresh_version()
        if messagebox.askyesno("Reverted", "Restart now to use the built-in version?"):
            updater.relaunch()
            self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
