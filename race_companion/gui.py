"""Tk desktop dashboard for live HH timing and saved recordings."""

from pathlib import Path
from queue import Empty
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .dashboard_session import DashboardSession, PROJECT_ROOT
from .estimator import EXCLUDED_STATUSES, PIT_STATUSES
from .forecasting import event_file, read_event
from .physical_field import field_state as measured_field_state
from .pits import apply_pit_overrides, load_scenarios
from .state import number


BG = "#101722"
PANEL = "#192535"
TEXT = "#edf3f9"
MUTED = "#a8b7c9"
GREEN = "#54d6a0"
AMBER = "#ffd06d"
RED = "#ff7b85"
BLUE = "#74c9f1"


def _number(value, digits=1):
    return "-" if value is None else f"{value:.{digits}f}"


def _lap_time(value):
    if value is None:
        return "-"
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes)}:{seconds:06.3f}"


def _race_gap(row):
    gap = row.get("race_gap_to_leader_s")
    if gap is None:
        return "-"
    laps = row.get("race_laps_back") or 0
    return f"{laps}L / {_number(gap)}s" if laps else f"{_number(gap)}s"


def _physical_neighbour(row, direction):
    if not row or row.get("physical_car_" + direction) is None:
        return "-"
    car = row["physical_car_" + direction]
    gap = row.get("physical_gap_" + direction + "_s")
    if gap is None:
        return f"#{car} / awaiting gap"
    age_key = "physical_gap_age_s" if direction == "ahead" else "physical_gap_behind_age_s"
    age = row.get(age_key)
    suffix = f" ({age:.0f}s old)" if age is not None else ""
    if row.get("ready") is False:
        suffix += " [CHECK]"
    return f"#{car} / {_number(gap)}s{suffix}"


def _position(horizon):
    if not horizon:
        return "-"
    overall = horizon.get("position")
    category = horizon.get("class_position")
    return f"P{overall}" + (f" / C{category}" if category is not None else "")


def _ahead(horizon):
    if not horizon:
        return "-"
    car = horizon.get("car_ahead")
    if car is None:
        return "No car ahead"
    return f"#{car}  {_number(horizon.get('gap_ahead_s'))}s"


def scenario_values(scenario):
    """Readable comparison row without inventing unconfigured pit results."""
    if scenario.get("status") != "model_projection":
        return (scenario.get("label", "?"), "PIT INPUTS", "-", "-", "-", "-", "-", "-", "-")
    horizons = {item["model_laps_ahead"]: item for item in scenario["horizons"]}
    first, second = horizons.get(1), horizons.get(2)
    loss = _number(scenario.get("net_pit_loss_s"))
    if scenario.get("pit_loss_source") == "engineer":
        loss += " *"
    return (scenario.get("label", "?"), "READY", loss,
            _position(first), _ahead(first),
            "Close" if first and first.get("traffic_pack") else "Clear",
            _position(second), _ahead(second),
            "Close" if second and second.get("traffic_pack") else "Clear")


class PitTimesDialog(tk.Toplevel):
    def __init__(self, parent, scenarios, overrides, on_apply):
        super().__init__(parent)
        self.title("Adjust pit times")
        self.configure(bg=BG)
        self.geometry("740x620")
        self.minsize(650, 420)
        self.transient(parent)
        self.scenarios = scenarios
        self.on_apply = on_apply
        self.pit_vars = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ttk.Label(self, text=("Total pit loss (seconds) compared with staying on track.\n"
                              "Includes pit lane and service; this is not just stationary time.\n"
                              "Adjustments apply to this event in the current app session."),
                  wraplength=680).grid(row=0, column=0, sticky="w", padx=16, pady=14)
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=16)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        rows = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        rows.grid_columnconfigure(0, weight=1)
        for col, title in enumerate(("Scenario", "File default (s)", "Engineer value (s)")):
            ttk.Label(rows, text=title, foreground=BLUE).grid(
                row=0, column=col, sticky="w", padx=6, pady=(0, 8))
        for row, scenario in enumerate(scenarios, 1):
            identifier = scenario["id"]
            default = scenario.get("net_pit_loss_s")
            ttk.Label(rows, text=scenario["label"]).grid(
                row=row, column=0, sticky="w", padx=6, pady=5)
            ttk.Label(rows, text=_number(default) if default is not None else "Unset").grid(
                row=row, column=1, sticky="w", padx=6)
            if identifier == "stay_out":
                ttk.Label(rows, text="0.0 (stay out)").grid(row=row, column=2, sticky="w", padx=6)
                continue
            value = overrides.get(identifier, default)
            var = tk.StringVar(value="" if value is None else str(value))
            self.pit_vars[identifier] = var
            ttk.Spinbox(rows, textvariable=var, from_=0, to=1000000, increment=0.5,
                        width=17).grid(row=row, column=2, sticky="w", padx=6)
        ttk.Label(self, text="Blank uses the file default. Unset defaults remain unavailable until entered.",
                  foreground=MUTED, wraplength=680).grid(
                      row=2, column=0, sticky="w", padx=16, pady=(12, 0))
        buttons = ttk.Frame(self, padding=16)
        buttons.grid(row=3, column=0, sticky="ew")
        ttk.Button(buttons, text="Reset to file defaults", command=self._reset).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Apply and recalculate", command=self._apply).pack(side="right", padx=8)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _reset(self):
        for scenario in self.scenarios:
            if scenario["id"] in self.pit_vars:
                value = scenario.get("net_pit_loss_s")
                self.pit_vars[scenario["id"]].set("" if value is None else str(value))

    def _apply(self):
        overrides = {}
        for scenario in self.scenarios:
            var = self.pit_vars.get(scenario["id"])
            if var is None or not var.get().strip():
                continue
            try:
                value = number(var.get().strip())
            except ValueError:
                messagebox.showerror("Pit time", f"{scenario['label']}: enter a finite number of seconds, zero or greater.",
                                     parent=self)
                return
            if value != scenario.get("net_pit_loss_s"):
                overrides[scenario["id"]] = value
        try:
            self.on_apply(overrides)
        except ValueError as exc:
            messagebox.showerror("Pit times", str(exc), parent=self)
            return
        self.destroy()


class DashboardApp:
    def __init__(self, root, event_path, selected_car):
        self.root = root
        self.root.title("Race Engineer Companion - Public Edition")
        self.root.geometry("1500x860")
        self.root.minsize(1120, 680)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.session = None
        self.last_revision = 0
        self.closing = False
        self.pit_overrides = {}
        self.pit_event_path = None
        self.pit_dialog = None
        self.pit_summary_var = tk.StringVar(value="Pit times: file defaults")
        self.event_var = tk.StringVar(value=str(event_path))
        self.car_var = tk.StringVar(value=selected_car)
        recordings = sorted((PROJECT_ROOT / "recordings").glob("*.jsonl"),
                            key=lambda path: path.stat().st_mtime, reverse=True)
        self.recording_var = tk.StringVar(value=str(recordings[0]) if recordings else str(PROJECT_ROOT / "examples" / "synthetic_race.jsonl"))
        self.speed_var = tk.StringVar(value="Instant")
        self.activity_var = tk.StringVar(value="Run the synthetic demo, or choose Live / Replay")
        self.ready_var = tk.StringVar(value="0 / 0 cars ready")
        self.source_var = tk.StringVar(value="Source time: -")
        self.selected_var = tk.StringVar(value="Selected car: awaiting timing")
        self.timing_note_var = tk.StringVar(
            value="HH P is the latest rank received from HH. Click any heading to sort.")
        self.timing_sort_column = "pos"
        self.timing_sort_reverse = False
        self.current_snapshot = None
        self.forecast_var = tk.StringVar(value="FORECAST WAITING")
        self.forecast_info_var = tk.StringVar(value="No timing snapshot yet")
        self._build()
        self._load_car_options()
        self.root.after(200, self._poll)

    def _build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("TLabelframe", background=BG, foreground=TEXT)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT)
        style.configure("TButton", padding=(11, 7))
        style.configure("TEntry", fieldbackground="#f5f8fb", foreground="#17212d")
        style.configure("TCombobox", fieldbackground="#f5f8fb", foreground="#17212d")
        style.configure("TSpinbox", fieldbackground="#f5f8fb", foreground="#17212d")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=25, borderwidth=0)
        style.configure("Treeview.Heading", background="#2a3a4d", foreground=TEXT,
                        relief="flat", padding=(6, 7))
        style.map("Treeview", background=[("selected", "#315776")])

        main = ttk.Frame(self.root, padding=14)
        main.grid(sticky="nsew")
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(4, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(header, text="RACE ENGINEER COMPANION | PUBLIC EDITION", bg=BG, fg=TEXT,
                 font=("Segoe UI", 19, "bold")).pack(anchor="w")
        tk.Label(header, text="Live timing and constant-pace baseline projection",
                 bg=BG, fg=MUTED).pack(anchor="w")

        setup = ttk.LabelFrame(main, text="Session", padding=10)
        setup.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        setup.grid_columnconfigure(1, weight=1)
        setup.grid_columnconfigure(4, weight=1)
        ttk.Label(setup, text="Event profile").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.event_entry = ttk.Entry(setup, textvariable=self.event_var)
        self.event_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        self.event_button = ttk.Button(setup, text="Browse", command=self._choose_event)
        self.event_button.grid(row=0, column=2, padx=(0, 18))
        ttk.Label(setup, text="Car").grid(row=0, column=3, padx=(0, 8))
        self.car_box = ttk.Combobox(setup, textvariable=self.car_var, width=9)
        self.car_box.grid(row=0, column=4, sticky="w")
        self.car_box.bind("<<ComboboxSelected>>", self._change_car)
        self.car_box.bind("<Return>", self._change_car)
        self.car_box.bind("<FocusOut>", self._change_car)
        ttk.Label(setup, text="Recording").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.recording_entry = ttk.Entry(setup, textvariable=self.recording_var)
        self.recording_entry.grid(row=1, column=1, columnspan=4, sticky="ew", pady=(8, 0), padx=(0, 5))
        self.recording_button = ttk.Button(setup, text="Browse", command=self._choose_recording)
        self.recording_button.grid(row=1, column=5, pady=(8, 0))

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.demo_button = ttk.Button(actions, text="Run synthetic demo", command=self._start_demo)
        self.demo_button.pack(side="left", padx=(0, 6))
        self.live_button = ttk.Button(actions, text="Start live + record",
                                      command=lambda: self._start("live"))
        self.live_button.pack(side="left", padx=(0, 6))
        self.replay_button = ttk.Button(actions, text="Replay recording",
                                        command=lambda: self._start("replay"))
        self.replay_button.pack(side="left", padx=(0, 6))
        ttk.Label(actions, text="Replay speed").pack(side="left", padx=(9, 5))
        self.speed_box = ttk.Combobox(actions, textvariable=self.speed_var,
                                      values=("Instant", "1x", "5x"), state="readonly", width=9)
        self.speed_box.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Label(actions, textvariable=self.activity_var).pack(side="right")

        overview = ttk.Frame(main)
        overview.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        for col in range(3):
            overview.grid_columnconfigure(col, weight=1)
        self.connection_label = tk.Label(overview, text="DISCONNECTED", bg=PANEL, fg=RED,
                                         font=("Segoe UI", 12, "bold"), anchor="w", padx=15, pady=13)
        self.connection_label.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        tk.Label(overview, textvariable=self.ready_var, bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 12, "bold"), anchor="w", padx=15, pady=13).grid(
                     row=0, column=1, sticky="ew", padx=(0, 7))
        tk.Label(overview, textvariable=self.source_var, bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 12, "bold"), anchor="w", padx=15, pady=13).grid(
                     row=0, column=2, sticky="ew")

        panes = ttk.PanedWindow(main, orient="horizontal")
        panes.grid(row=4, column=0, sticky="nsew")
        field = ttk.LabelFrame(panes, text="Timing field", padding=7)
        forecast = ttk.LabelFrame(panes, text="Selected car forecast", padding=7)
        panes.add(field, weight=3)
        panes.add(forecast, weight=2)
        field.grid_columnconfigure(0, weight=1)
        field.grid_rowconfigure(2, weight=1)
        forecast.grid_columnconfigure(0, weight=1)
        forecast.grid_rowconfigure(3, weight=0)

        ttk.Label(field, textvariable=self.selected_var).grid(row=0, column=0, sticky="w", pady=(0, 7))
        ttk.Label(field, textvariable=self.timing_note_var,
                  foreground=MUTED, wraplength=850).grid(row=1, column=0, sticky="w", pady=(0, 7))
        self.timing_columns = (
            "pos", "car", "class", "race_gap", "physical_order", "physical_ahead_gap", "physical_behind_gap",
            "lap", "last", "vmax", "age", "status", "flag", "driver",
        )
        self.timing_tree = ttk.Treeview(field, columns=self.timing_columns, show="headings")
        timing_headers = (
            "Race position", "Car", "Class", "HH race gap", "Track order", "Physical ahead / measured gap", "Physical behind / measured gap",
            "Lap", "Last lap", "Peak km/h", "Update age (s)", "Status", "Flag", "Driver",
        )
        timing_widths = (94, 54, 89, 96, 90, 220, 220, 43, 79, 87, 91, 95, 111, 145)
        self.timing_header_labels = dict(zip(self.timing_columns, timing_headers))
        for column, label, width in zip(self.timing_columns, timing_headers, timing_widths):
            self.timing_tree.heading(
                column, text=label,
                command=lambda selected=column: self._sort_timing(selected))
            self.timing_tree.column(column, width=width, minwidth=width, stretch=column == "driver")
        self.timing_tree.tag_configure("selected_car", background="#285270")
        self.timing_tree.tag_configure("stale", foreground=RED)
        self.timing_tree.grid(row=2, column=0, sticky="nsew")
        timing_scroll = ttk.Scrollbar(field, orient="vertical", command=self.timing_tree.yview)
        timing_scroll.grid(row=2, column=1, sticky="ns")
        timing_scroll_x = ttk.Scrollbar(field, orient="horizontal", command=self.timing_tree.xview)
        timing_scroll_x.grid(row=3, column=0, sticky="ew")
        self.timing_tree.configure(yscrollcommand=timing_scroll.set,
                                   xscrollcommand=timing_scroll_x.set)

        self.forecast_label = tk.Label(forecast, textvariable=self.forecast_var, bg=PANEL,
                                       fg=AMBER, font=("Segoe UI", 14, "bold"),
                                       anchor="w", padx=11, pady=10)
        self.forecast_label.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(forecast, textvariable=self.forecast_info_var).grid(
            row=1, column=0, sticky="w", pady=(0, 5))
        forecast_help = ttk.Frame(forecast)
        forecast_help.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            forecast_help,
            text=("Horizons are common future times, one/two selected-car laps ahead. No traffic interactions. "
                  "Position shows overall / class; car ahead is the nearest car physically ahead."),
            foreground=MUTED, wraplength=760,
        ).pack(anchor="w")
        pit_actions = ttk.Frame(forecast_help)
        pit_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(pit_actions, text="Adjust pit times...", command=self._edit_pit_times).pack(side="left")
        ttk.Label(pit_actions, textvariable=self.pit_summary_var, foreground=MUTED).pack(side="left", padx=8)
        scenario_columns = ("scenario", "status", "pit_loss", "one_pos", "one_ahead", "one_pack",
                            "two_pos", "two_ahead", "two_pack")
        self.scenario_tree = ttk.Treeview(forecast, columns=scenario_columns,
                                           show="headings", height=13)
        scenario_headers = ("Scenario", "Status", "Pit loss (s)",
                            "Lap 1 position", "Lap 1 car ahead", "Lap 1 traffic",
                            "Lap 2 position", "Lap 2 car ahead", "Lap 2 traffic")
        scenario_widths = (135, 76, 76, 100, 115, 92, 100, 115, 92)
        for column, label, width in zip(scenario_columns, scenario_headers, scenario_widths):
            self.scenario_tree.heading(column, text=label)
            self.scenario_tree.column(column, width=width, minwidth=width,
                                      stretch=column == "scenario")
        self.scenario_tree.grid(row=3, column=0, sticky="nsew")
        scenario_scroll = ttk.Scrollbar(forecast, orient="horizontal", command=self.scenario_tree.xview)
        scenario_scroll.grid(row=4, column=0, sticky="ew")
        self.scenario_tree.configure(xscrollcommand=scenario_scroll.set)
        ttk.Label(forecast, text="Readiness and model notes").grid(
            row=5, column=0, sticky="sw", pady=(8, 4))
        self.details = tk.Text(forecast, height=9, bg=PANEL, fg=TEXT,
                               insertbackground=TEXT, relief="flat", wrap="word", padx=10, pady=9)
        self.details.grid(row=6, column=0, sticky="nsew")
        self.details.configure(state="disabled")
        forecast.grid_rowconfigure(6, weight=1)

        ttk.Label(main, text="Constant-pace baseline: no traffic interactions. Horizons use the selected car's lap pace; gaps are approximate.",
                  foreground=MUTED).grid(row=5, column=0, sticky="w", pady=(9, 0))

    def _sort_timing(self, column):
        if column == self.timing_sort_column:
            self.timing_sort_reverse = not self.timing_sort_reverse
        else:
            self.timing_sort_column = column
            self.timing_sort_reverse = False
        if self.current_snapshot is not None:
            self._render(self.current_snapshot)

    def _update_timing_headings(self):
        for column, label in self.timing_header_labels.items():
            if column == self.timing_sort_column:
                label += " v" if self.timing_sort_reverse else " ^"
            self.timing_tree.heading(column, text=label)

    def _load_car_options(self):
        try:
            profile, _ = read_event(Path(self.event_var.get()).expanduser())
            cars = profile.get("expected_car_numbers", []) if profile else []
        except (OSError, ValueError, TypeError):
            cars = []
        self.car_box.configure(values=cars, state="readonly" if cars else "normal")
        if cars and self.car_var.get() not in cars:
            self.car_var.set(cars[0])

    def _choose_event(self):
        path = filedialog.askopenfilename(initialdir=PROJECT_ROOT / "config",
                                          filetypes=[("JSON event profiles", "*.json"), ("All files", "*")])
        if path:
            self.event_var.set(path)
            self._load_car_options()
            self.pit_overrides = {}
            self.pit_event_path = None
            self._pit_summary()

    def _choose_recording(self):
        path = filedialog.askopenfilename(initialdir=PROJECT_ROOT / "recordings",
                                          filetypes=[("Timing recordings", "*.jsonl"), ("All files", "*")])
        if path:
            self.recording_var.set(path)

    def _pit_defaults(self):
        path = Path(self.event_var.get().strip()).expanduser().resolve()
        profile, _ = read_event(path)
        pit_path = event_file(path, profile, None, "pit_scenarios_file")
        defaults = load_scenarios(pit_path) if pit_path else []
        if self.pit_event_path != path:
            self.pit_overrides = {}
            self.pit_event_path = path
            self._pit_summary()
        apply_pit_overrides(defaults, self.pit_overrides)
        return defaults

    def _pit_summary(self):
        count = len(self.pit_overrides)
        self.pit_summary_var.set(f"{count} adjusted pit time(s) (*)" if count else "Pit times: file defaults")

    def _edit_pit_times(self):
        if self.pit_dialog is not None and self.pit_dialog.winfo_exists():
            self.pit_dialog.lift()
            return
        try:
            defaults = self._pit_defaults()
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Pit configuration", str(exc))
            return
        if not defaults:
            messagebox.showinfo("Pit times", "This event profile has no pit scenario file configured.")
            return
        self.pit_dialog = PitTimesDialog(self.root, defaults, self.pit_overrides, self._apply_pit_times)

    def _apply_pit_times(self, overrides):
        if self.session and self.pit_event_path == self.session.event_path.resolve():
            self.session.set_pit_overrides(overrides)
        self.pit_overrides = dict(overrides)
        self._pit_summary()
        if self.session and self.session.running:
            self.forecast_var.set("UPDATING PIT TIMES")
            self.forecast_label.configure(fg=AMBER)
            for item in self.scenario_tree.get_children():
                self.scenario_tree.delete(item)
            self._clear_details()

    def _start_demo(self):
        if self.session and self.session.running:
            return
        self.event_var.set(str(PROJECT_ROOT / "config" / "demo.json"))
        self.car_var.set("101")
        self._load_car_options()
        self._start("demo")

    def _start(self, mode):
        if self.session and self.session.running:
            return
        event_path = Path(self.event_var.get().strip()).expanduser()
        selected_car = self.car_var.get().strip()
        if not event_path.is_file() or not selected_car:
            messagebox.showerror("Session input", "Choose an existing event profile and a car number.")
            return
        recording = None
        if mode == "replay":
            recording = Path(self.recording_var.get().strip()).expanduser()
            if not recording.is_file():
                messagebox.showerror("Replay input", "Choose an existing recording file.")
                return
        replay_speed = {"Instant": 0, "1x": 1, "5x": 5}[self.speed_var.get()]
        try:
            self._pit_defaults()
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Pit configuration", str(exc))
            return
        self.session = DashboardSession(mode=mode, event_path=event_path,
                                         selected_car=selected_car, recording_path=recording,
                                         replay_speed=replay_speed, pit_overrides=self.pit_overrides)
        self.last_revision = 0
        self.activity_var.set("Running synthetic demo (20x source time)..." if mode == "demo" else
                              "Connecting to HH..." if mode == "live" else "Loading recording...")
        self._set_running(True)
        self.session.start()

    def _set_running(self, running):
        state = "disabled" if running else "normal"
        for widget in (self.event_entry, self.event_button, self.recording_entry,
                       self.recording_button, self.live_button, self.replay_button, self.demo_button):
            widget.configure(state=state)
        self.car_box.configure(state="readonly" if self.car_box.cget("values") else "normal")
        self.speed_box.configure(state="disabled" if running else "readonly")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _stop(self):
        if self.session:
            self.session.stop()
            self.activity_var.set("Stopping...")
            self.stop_button.configure(state="disabled")

    def _change_car(self, _event=None):
        car_number = self.car_var.get().strip()
        if not car_number:
            return
        if self.session and self.session.running and car_number != self.session.selected_car:
            self.session.select_car(car_number)
            self.forecast_var.set("SWITCHING CAR")
            self.forecast_label.configure(fg=AMBER)
            for item in self.scenario_tree.get_children():
                self.scenario_tree.delete(item)
            self._clear_details()
        elif self.session and car_number != self.session.selected_car:
            self.session.select_car(car_number)
            self.session.refresh_forecast()
            self.forecast_var.set("SWITCHING CAR")
            self.forecast_label.configure(fg=AMBER)
            self.forecast_info_var.set(f"Preparing baseline for car #{car_number}")
            for item in self.scenario_tree.get_children():
                self.scenario_tree.delete(item)

    def _poll(self):
        if self.session:
            while True:
                try:
                    kind, message = self.session.events.get_nowait()
                except Empty:
                    break
                if kind == "error":
                    self.activity_var.set(f"Error: {message}")
                    messagebox.showerror("Session error", message)
                elif kind == "finished":
                    if not self.activity_var.get().startswith("Error:"):
                        self.activity_var.set(message)
                    self._set_running(False)
                else:
                    self.activity_var.set(message)
                    if self.session.mode == "live" and message.startswith("Recording to "):
                        self.recording_var.set(message.removeprefix("Recording to "))
            revision, snapshot = self.session.latest()
            if (snapshot is not None and
                    snapshot["prediction"].get("pit_settings_revision", 0) != self.session.pit_settings_revision):
                self.session.refresh_forecast()
                revision, snapshot = self.session.latest()
            if revision != self.last_revision and snapshot is not None:
                self.last_revision = revision
                self._render(snapshot)
        if self.closing:
            if not self.session or not self.session.running:
                self.root.destroy()
                return
        self.root.after(200, self._poll)

    def _render(self, snapshot):
        self.current_snapshot = snapshot
        connected = snapshot["connected"]
        label = "SYNTHETIC DEMO" if self.session.mode == "demo" else ("RECORDED CONNECTED" if self.session.mode == "replay" and connected else (
            "HH CONNECTED" if connected else "HH DISCONNECTED"))
        self.connection_label.configure(text=label, fg=GREEN if connected else RED)
        prediction = snapshot["prediction"]
        readiness = prediction.get("readiness", {})
        excluded = readiness.get("excluded_cars", [])
        unreported = readiness.get("unreported_cars", [])
        pit_count = sum(car["status"] in PIT_STATUSES for car in excluded)
        terminal_count = len(excluded) - pit_count
        physical_readiness = readiness.get("physical_state", {})
        self.ready_var.set(f"{readiness.get('ready_cars', 0)} / {readiness.get('field_size', 0)} timing inputs ready"
                           + (f"  |  HH gaps {physical_readiness.get('ready_cars', 0)}/{physical_readiness.get('field_size', 0)}"
                              if "ready_cars" in physical_readiness else "")
                           + (f"  |  {pit_count} in pit" if pit_count else "")
                           + (f"  |  {terminal_count} retired/excluded" if terminal_count else "")
                           + (f"  |  {len(unreported)} not received" if unreported else ""))
        self.source_var.set("Source time: " + _number(snapshot.get("latest_session_time_s"), 1) + " s")
        field_state = (measured_field_state(snapshot) if snapshot.get("physical_field") is not None
                       else prediction.get("field_state") or {})
        physical_cars = {row["number"]: row for row in field_state.get("cars", [])}
        reference_car = field_state.get("reference_car")
        if not physical_cars:
            self.timing_note_var.set(
                "Race position and HH race gap are classification data. Physical traffic appears when "
                "the updated HH bridge sends crossing data. Click any heading to sort.")
        else:
            self.timing_note_var.set(
                "Physical gaps are last measured timing-line gaps; their ages are source-session seconds. "
                + (f"P1 #{reference_car} anchors display order only. " if reference_car else "No P1 reference is required. ")
                + "Click any heading to sort.")
        self._update_timing_headings()
        for item in self.timing_tree.get_children():
            self.timing_tree.delete(item)
        selected_row = next((row for row in snapshot["cars"]
                             if row["number"] == self.session.selected_car), None)

        rows = [(row, physical_cars.get(row["number"])) for row in snapshot["cars"]]

        def sort_value(item):
            row, estimate = item
            values = {
                "pos": row["position"],
                "car": row["number"],
                "class": row["category"],
                "race_gap": ((row.get("race_laps_back") or 0), row.get("race_gap_to_leader_s"))
                if row.get("race_gap_to_leader_s") is not None else None,
                "physical_order": estimate.get("physical_order") if estimate else None,
                "physical_ahead_gap": estimate.get("physical_gap_ahead_s") if estimate else None,
                "physical_behind_gap": estimate.get("physical_gap_behind_s") if estimate else None,
                "lap": row["lap"],
                "last": row["last_lap"],
                "vmax": row["max_speed_kmh"],
                "age": row["timing_age_s"],
                "status": row["status"],
                "flag": row["track_status"],
                "driver": row["driver"],
            }
            value = values[self.timing_sort_column]
            if value is None:
                return None
            if self.timing_sort_column == "car":
                text = str(value)
                return (0, int(text)) if text.isdigit() else (1, text.casefold())
            return value.casefold() if isinstance(value, str) else value

        available = [item for item in rows if sort_value(item) is not None]
        missing_sort_value = [item for item in rows if sort_value(item) is None]
        available.sort(key=sort_value, reverse=self.timing_sort_reverse)
        rows = available + missing_sort_value
        for row, estimate in rows:
            tags = ("selected_car",) if row["number"] == self.session.selected_car else (
                ("stale",) if row["timing_stale"] or (estimate and estimate.get("ready") is False) else ())
            self.timing_tree.insert("", "end", values=(
                row["position"] if row["position"] is not None else "-",
                row["number"], row["category"] or "-",
                _race_gap(row),
                estimate.get("physical_order", "-") if estimate else "-",
                _physical_neighbour(estimate, "ahead"),
                _physical_neighbour(estimate, "behind"),
                row["lap"],
                _lap_time(row["last_lap"]), _number(row["max_speed_kmh"]),
                _number(row["timing_age_s"]), row["status"] or "-", row["track_status"] or "-",
                row["driver"] or "-"), tags=tags)
        if selected_row:
            self.selected_var.set(
                f"#{selected_row['number']}  |  P{selected_row['position'] or '?'}"
                f" ({selected_row['position_source'] or 'unknown'}, {_number(selected_row['position_age_s'])}s old)"
                f"  |  lap {selected_row['lap']}  |  {selected_row['status'] or 'unknown status'}"
                f"  |  peak trap {_number(selected_row['max_speed_kmh'])} km/h")
        else:
            self.selected_var.set(f"#{self.session.selected_car}: awaiting timing")

        if prediction.get("pit_settings_revision", 0) != self.session.pit_settings_revision:
            self.forecast_var.set("UPDATING PIT TIMES")
            self.forecast_label.configure(fg=AMBER)
            for item in self.scenario_tree.get_children():
                self.scenario_tree.delete(item)
            self._clear_details()
            return

        if prediction.get("selected_car") != self.session.selected_car:
            self.forecast_var.set("SWITCHING CAR")
            self.forecast_label.configure(fg=AMBER)
            self.forecast_info_var.set(f"Preparing forecast for car #{self.session.selected_car}")
            for item in self.scenario_tree.get_children():
                self.scenario_tree.delete(item)
            self._clear_details()
            return

        status = prediction["status"]
        names = {"waiting_for_inputs": "WAITING FOR INPUTS", "calculating": "CALCULATING",
                 "baseline_projection": "BASELINE PROJECTION",
                 "held_projection": "HELD: LAST VALID FORECAST", "forecast_error": "FORECAST ERROR"}
        color = GREEN if status == "baseline_projection" else (
            RED if status == "forecast_error" else AMBER)
        non_green = {row["track_status"] for row in snapshot["cars"]
                     if row.get("status") not in EXCLUDED_STATUSES
                     and row.get("track_status") not in (None, "Green")}
        if status == "waiting_for_inputs" and non_green:
            self.forecast_var.set("PAUSED: NON-GREEN TRACK")
        elif status == "waiting_for_inputs" and any(
                reason.startswith("Awaiting expected cars:")
                for reason in prediction.get("missing", [])):
            self.forecast_var.set("WAITING: EXPECTED CAR NOT RECEIVED")
        elif status == "waiting_for_inputs" and physical_readiness and not physical_readiness.get("ready"):
            self.forecast_var.set("WAITING: HH PHYSICAL FIELD")
        elif status == "baseline_projection" and (pit_count or unreported):
            self.forecast_var.set("BASELINE: ON-TRACK FIELD")
        else:
            self.forecast_var.set(names.get(status, status.upper().replace("_", " ")))
        self.forecast_label.configure(fg=color)
        info = (f"Car #{self.session.selected_car}  |  forecast source "
                f"t={_number(prediction.get('as_of_session_time_s'), 1)} s")
        if status == "held_projection":
            info += f"  |  current t={_number(prediction.get('current_source_session_time_s'), 1)} s"
        if prediction.get("source_lag_s") is not None:
            info += f"  |  lag {_number(prediction['source_lag_s'])} s"
        if prediction.get("updating"):
            info += "  |  updating"
        if status == "waiting_for_inputs" and prediction.get("missing"):
            info += "  |  " + prediction["missing"][0]
        self.forecast_info_var.set(info)
        for item in self.scenario_tree.get_children():
            self.scenario_tree.delete(item)
        scenarios = prediction.get("scenarios", [])
        self.scenario_tree.configure(height=13 if scenarios else 5)
        for scenario in scenarios:
            values = list(scenario_values(scenario))
            if status == "held_projection" and values[1] == "READY":
                values[1] = "HELD"
            self.scenario_tree.insert("", "end", values=values)
        self._show_details(prediction, snapshot)

    def _show_details(self, prediction, snapshot):
        lines = []
        adjusted = [scenario for scenario in prediction.get("scenarios", [])
                    if scenario.get("pit_loss_source") == "engineer"]
        if adjusted:
            lines.append("* Engineer-adjusted total pit losses: " + "; ".join(
                f"{scenario['label']} {_number(scenario['net_pit_loss_s'])} s" for scenario in adjusted)
                + ". These replace the file defaults below for the adjusted scenarios.")
        if prediction.get("calibration_note"):
            lines.append(prediction["calibration_note"])
        excluded = prediction.get("readiness", {}).get("excluded_cars", [])
        unreported = prediction.get("readiness", {}).get("unreported_cars", [])
        if excluded:
            lines.append("Excluded from forecast: " + ", ".join(
                f"#{car['number']} ({car['status']})" for car in excluded))
        if unreported:
            lines.append("Not received from HH and omitted after the connection grace period: " +
                         ", ".join(f"#{car['number']}" for car in unreported))
        if any(car["status"] in PIT_STATUSES for car in excluded):
            lines.append("Pit cars are outside this forecast. Projected positions and gaps cover only "
                         "the modeled on-track field; a pit rejoin may change them.")
        if unreported:
            lines.append("An omitted car may have retired before connection. Its status is unverified, "
                         "so projected positions and gaps cover only cars reported by HH.")
        flags = sorted({row["track_status"] for row in snapshot["cars"]
                        if row.get("status") not in EXCLUDED_STATUSES
                        and row.get("track_status") not in (None, "Green")})
        if flags:
            lines.append("Received non-green track flags: " + ", ".join(flags) +
                         ". Forecasts resume after all required cars have fresh green timing and pace.")
        if prediction["status"] == "held_projection":
            hold = prediction["hold"]
            lines.append(f"Holding the prior forecast: car {hold['car']} crossing is overdue. No new projection uses its stale position.")
        elif prediction["status"] == "forecast_error":
            lines.append(prediction.get("reason", "Forecast calculation failed"))
        elif prediction["status"] == "waiting_for_inputs":
            missing = prediction.get("missing", [])
            lines.append("Input issues:")
            lines.extend("• " + reason for reason in missing[:8])
            if len(missing) > 8:
                lines.append(f"• Plus {len(missing) - 8} more issues")
        else:
            unconfigured = sum(item["status"] == "needs_configuration"
                               for item in prediction.get("scenarios", []))
            if unconfigured:
                lines.append(f"{unconfigured} pit scenarios need service durations. Stay-out is available.")
            lines.extend(prediction.get("assumptions", [])[:4])
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n\n".join(lines))
        self.details.configure(state="disabled")

    def _clear_details(self):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.configure(state="disabled")

    def _close(self):
        self.closing = True
        if self.session and self.session.running:
            self.session.stop()
            self.activity_var.set("Stopping before close...")
        else:
            self.root.destroy()


def run_gui(event_path=PROJECT_ROOT / "config" / "demo.json", selected_car="101"):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"Could not open the desktop window: {exc}") from exc
    DashboardApp(root, event_path, selected_car)
    root.mainloop()
    return 0
