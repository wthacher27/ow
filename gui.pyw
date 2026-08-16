#!/usr/bin/env pythonw
"""OW Tracker - one-click GUI with a screen-scan button."""
import asyncio
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("OW_DB_PATH", str(PROJECT_DIR / "data" / "ow_stats.db"))

from database import init_db, get_players, save_game_log
import display as disp
import screen_reader as sr
from tracker import track_all_players

DEFAULT_PLAYER = "CptEnfuego-1359"

STAT_OPTIONS = {
    "Overview": disp.show_overview,
    "Top Heroes": disp.show_heroes,
    "Rank History": disp.show_ranks,
    "Map Stats": disp.show_map_stats,
    "Teammate Stats": disp.show_teammate_stats,
    "Recent Games": disp.show_recent_games,
}


class QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text.strip():
            self.q.put(text)

    def flush(self):
        pass


async def _resolve(reader, battletag):
    players = await get_players()
    row = next((p for p in players if p["battletag"] == battletag), None)
    if not row:
        raise RuntimeError(f"Player {battletag!r} not found. Add them first with the 'add' command.")
    reader.player_id = row["id"]
    print(f"[OK] Watching for {battletag} (id={reader.player_id})")


class App:
    def __init__(self, root):
        self.root = root
        root.title("OW Tracker")
        root.geometry("1080x480")
        root.configure(bg="#1e1e1e")

        frame = tk.Frame(root, padx=12, pady=12, bg="#1e1e1e")
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Player (BattleTag):", bg="#1e1e1e", fg="white").grid(row=0, column=0, sticky="w")
        self.player_var = tk.StringVar(value=DEFAULT_PLAYER)
        tk.Entry(frame, textvariable=self.player_var, width=28).grid(row=0, column=1, sticky="w", padx=(6, 0))

        self.scan_btn = tk.Button(
            frame, text="Scan Screen", width=14, command=self.toggle_scan,
            bg="#f39c12", fg="black", activebackground="#e67e22",
        )
        self.scan_btn.grid(row=0, column=2, padx=(10, 0))

        self.history_btn = tk.Button(
            frame, text="Scan History", width=14, command=self.scan_history_now,
            bg="#8e44ad", fg="white", activebackground="#9b59b6",
        )
        self.history_btn.grid(row=0, column=3, padx=(6, 0))

        self.stats_btn = tk.Button(
            frame, text="View Stats", width=14, command=self.open_stats_dialog,
            bg="#2980b9", fg="white", activebackground="#3498db",
        )
        self.stats_btn.grid(row=0, column=4, padx=(6, 0))

        self.snapshot_btn = tk.Button(
            frame, text="Retake Snapshot", width=16, command=self.retake_snapshot,
            bg="#27ae60", fg="white", activebackground="#2ecc71",
        )
        self.snapshot_btn.grid(row=0, column=5, padx=(6, 0))

        self.status_var = tk.StringVar(value="Idle")
        tk.Label(frame, textvariable=self.status_var, bg="#1e1e1e", fg="#aaaaaa").grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(8, 0)
        )

        self.log = scrolledtext.ScrolledText(
            frame, height=22, state="disabled", bg="#111111", fg="#33dd33",
            font=("Consolas", 9), wrap="none",
        )
        self.log.grid(row=2, column=0, columnspan=6, sticky="nsew", pady=(10, 0))
        h_scroll = tk.Scrollbar(frame, orient="horizontal", command=self.log.xview)
        h_scroll.grid(row=3, column=0, columnspan=6, sticky="ew")
        self.log.configure(xscrollcommand=h_scroll.set)
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(1, weight=1)

        self.q = queue.Queue()
        self.scanning = False
        self.stop_event = None
        self.thread = None
        self._orig_stdout = sys.stdout
        self._stdout_lock = threading.Lock()

        self.root.after(150, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_queue(self):
        try:
            while True:
                self._log(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(150, self._drain_queue)

    def toggle_scan(self):
        self.stop_scan() if self.scanning else self.start_scan()

    def start_scan(self):
        player = self.player_var.get().strip()
        if not player:
            messagebox.showwarning("OW Tracker", "Enter a BattleTag first.")
            return
        self.scanning = True
        self.scan_btn.configure(text="Stop Scan", bg="#c0392b", fg="white")
        self.status_var.set(f"Scanning for {player}...")
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run_scan, args=(player, self.stop_event), daemon=True)
        self.thread.start()

    def stop_scan(self):
        if self.stop_event:
            self.stop_event.set()
        self.scanning = False
        self.scan_btn.configure(text="Scan Screen", bg="#f39c12", fg="black")
        self.status_var.set("Stopped")

    def _run_scan(self, player, stop_event):
        with self._stdout_lock:
            sys.stdout = QueueWriter(self.q)
            try:
                asyncio.run(self._scan_loop(player, stop_event))
            except Exception as e:
                self.q.put(f"[ERROR] {e}")
            finally:
                sys.stdout = self._orig_stdout
        self.root.after(0, self._on_scan_ended)

    async def _scan_loop(self, player, stop_event):
        await init_db()
        reader = sr.ScreenReader(player)
        await _resolve(reader, player)
        print(f"[screen_reader] Watching screen every {sr.POLL_INTERVAL}s. Click 'Stop Scan' to stop.")
        prev_map, prev_result, prev_logged = None, None, reader._last_logged
        while not stop_event.is_set():
            try:
                await reader.tick()

                if reader._map_candidate and reader._map_candidate != prev_map:
                    print(f"[scan] map detected: {reader._map_candidate}")
                prev_map = reader._map_candidate

                if reader._result_candidate and reader._result_candidate != prev_result:
                    print(f"[scan] result detected: {reader._result_candidate.upper()}")
                prev_result = reader._result_candidate

                if reader._last_logged != prev_logged:
                    prev_logged = reader._last_logged
                    self._notify("Match logged", f"{player}: match saved to game log.")
            except Exception as e:
                print(f"[WARN] tick error: {e}")
            await asyncio.sleep(sr.POLL_INTERVAL)
        print("[screen_reader] Stopped.")

    def _notify(self, title, message):
        self.root.after(0, lambda: messagebox.showinfo(title, message))

    def _on_scan_ended(self):
        if self.scanning:
            self.stop_scan()

    def scan_history_now(self):
        player = self.player_var.get().strip()
        if not player:
            messagebox.showwarning("OW Tracker", "Enter a BattleTag first.")
            return
        self.history_btn.configure(state="disabled")
        self.status_var.set("Scanning History/Replays screen...")
        self._log(f">>> Scan History — {player}")
        threading.Thread(target=self._run_history_scan, args=(player,), daemon=True).start()

    def _run_history_scan(self, player):
        rows = None
        try:
            rows = asyncio.run(self._do_history_scan(player))
        except Exception as e:
            self.q.put(f"[ERROR] {e}")
        self.root.after(0, self._on_history_scanned, player, rows)

    async def _do_history_scan(self, player):
        await init_db()
        return await sr.scan_history_screen(player)

    def _on_history_scanned(self, player, rows):
        self.history_btn.configure(state="normal")
        self.status_var.set("Idle")
        if rows is None:
            return
        if not rows:
            messagebox.showinfo(
                "OW Tracker",
                "No matches recognized on screen.\n"
                "Make sure the game's History / Replays list is visible and try again.",
            )
            return
        self._log(f"[history] Found {len(rows)} match row(s) on screen.")
        self._open_history_review(player, rows)

    def _open_history_review(self, player, rows):
        dialog = tk.Toplevel(self.root)
        dialog.title("Scan History Results")
        dialog.configure(bg="#1e1e1e")
        dialog.resizable(False, True)
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(
            dialog, text=f"Found {len(rows)} match(es) for {player}. Uncheck any already logged, then Log Selected.",
            bg="#1e1e1e", fg="white", wraplength=420, justify="left",
        ).pack(padx=10, pady=(10, 4), anchor="w")

        mode_row = tk.Frame(dialog, bg="#1e1e1e")
        mode_row.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(mode_row, text="Mode for all selected:", bg="#1e1e1e", fg="white").pack(side="left")
        mode_var = tk.StringVar(value="competitive")
        ttk.Combobox(
            mode_row, textvariable=mode_var, values=["competitive", "quickplay"],
            state="readonly", width=14,
        ).pack(side="left", padx=(6, 0))

        list_frame = tk.Frame(dialog, bg="#1e1e1e")
        list_frame.pack(fill="both", expand=True, padx=10)
        canvas = tk.Canvas(list_frame, bg="#111111", height=260, width=420, highlightthickness=0)
        v_scroll = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        rows_frame = tk.Frame(canvas, bg="#111111")
        rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=v_scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        v_scroll.pack(side="right", fill="y")

        check_vars = []
        for row in rows:
            var = tk.BooleanVar(value=not row.get("likely_duplicate"))
            check_vars.append(var)
            label = f"{row['map']} — {row['result'].upper()}"
            if row.get("likely_duplicate"):
                label += "  (looks already logged)"
            tk.Checkbutton(
                rows_frame, text=label, variable=var, anchor="w", bg="#111111", fg="#33dd33",
                selectcolor="#222222", activebackground="#111111", activeforeground="#33dd33",
            ).pack(fill="x")

        btn_row = tk.Frame(dialog, bg="#1e1e1e")
        btn_row.pack(fill="x", padx=10, pady=10)

        def on_log_selected():
            selected = [r for r, v in zip(rows, check_vars) if v.get()]
            if not selected:
                messagebox.showwarning("OW Tracker", "No rows selected.")
                return
            mode = mode_var.get()
            dialog.destroy()
            self._log(f">>> Logging {len(selected)} match(es) from history for {player} ({mode})")
            threading.Thread(target=self._run_log_history, args=(player, mode, selected), daemon=True).start()

        tk.Button(btn_row, text="Log Selected", command=on_log_selected, bg="#8e44ad", fg="white").pack(side="left")
        tk.Button(btn_row, text="Cancel", command=dialog.destroy).pack(side="left", padx=(8, 0))

    def _run_log_history(self, player, mode, selected):
        try:
            count = asyncio.run(self._log_history_rows(player, mode, selected))
        except Exception as e:
            self.q.put(f"[ERROR] {e}")
            return
        self.q.put(f"[history] Logged {count} match(es) for {player}.")
        self._notify("OW Tracker", f"Logged {count} match(es) from history for {player}.")

    async def _log_history_rows(self, player, mode, selected):
        await init_db()
        players = await get_players()
        match = next((p for p in players if p["battletag"] == player), None)
        if not match:
            raise RuntimeError(f"Player {player!r} not found.")
        count = 0
        for row in selected:
            await save_game_log(match["id"], row["map"], row["result"], None, mode, [], None)
            count += 1
        return count

    def retake_snapshot(self):
        player = self.player_var.get().strip()
        if not player:
            messagebox.showwarning("OW Tracker", "Enter a BattleTag first.")
            return
        self.snapshot_btn.configure(state="disabled")
        self.status_var.set(f"Taking snapshot for {player}...")
        self._log(f">>> Retake Snapshot — {player}")
        threading.Thread(target=self._run_snapshot, args=(player,), daemon=True).start()

    def _run_snapshot(self, player):
        with self._stdout_lock:
            sys.stdout = QueueWriter(self.q)
            try:
                asyncio.run(self._do_snapshot(player))
            except Exception as e:
                self.q.put(f"[ERROR] {e}")
            finally:
                sys.stdout = self._orig_stdout
        self.root.after(0, self._on_snapshot_ended)

    async def _do_snapshot(self, player):
        await init_db()
        await track_all_players([player])

    def _on_snapshot_ended(self):
        self.snapshot_btn.configure(state="normal")
        self.status_var.set("Idle")

    def open_stats_dialog(self):
        try:
            players = asyncio.run(get_players())
        except Exception as e:
            messagebox.showerror("OW Tracker", f"Could not load players: {e}")
            return
        if not players:
            messagebox.showinfo("OW Tracker", "No players tracked yet. Use the CLI 'add' command first.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("View Stats")
        dialog.configure(bg="#1e1e1e")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        pad = {"padx": 10, "pady": 6}
        tags = [p["battletag"] for p in players]
        stat_names = list(STAT_OPTIONS.keys())

        tk.Label(dialog, text="Player:", bg="#1e1e1e", fg="white").grid(row=0, column=0, sticky="w", **pad)
        player_var = tk.StringVar(value=tags[0])
        ttk.Combobox(dialog, textvariable=player_var, values=tags, state="readonly", width=28).grid(
            row=0, column=1, **pad
        )

        tk.Label(dialog, text="Stat:", bg="#1e1e1e", fg="white").grid(row=1, column=0, sticky="w", **pad)
        stat_var = tk.StringVar(value=stat_names[0])
        ttk.Combobox(dialog, textvariable=stat_var, values=stat_names, state="readonly", width=28).grid(
            row=1, column=1, **pad
        )

        def on_show():
            battletag = player_var.get()
            stat_label = stat_var.get()
            dialog.destroy()
            self._log(f">>> {stat_label} — {battletag}")
            threading.Thread(target=self._run_stat, args=(battletag, stat_label), daemon=True).start()

        tk.Button(dialog, text="Show", command=on_show, bg="#2980b9", fg="white").grid(
            row=2, column=0, columnspan=2, pady=(4, 10)
        )

    def _run_stat(self, battletag, stat_label):
        with self._stdout_lock:
            sys.stdout = QueueWriter(self.q)
            try:
                asyncio.run(self._show_stat(battletag, stat_label))
            except Exception as e:
                self.q.put(f"[ERROR] {e}")
            finally:
                sys.stdout = self._orig_stdout

    async def _show_stat(self, battletag, stat_label):
        await init_db()
        await STAT_OPTIONS[stat_label](battletag)

    def on_close(self):
        if self.scanning:
            self.stop_scan()
        self.root.after(300, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
