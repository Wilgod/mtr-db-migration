"""
Metadata Horizontal — desktop tool.

Upload a CSV/XLSX of SharePoint metadata: the fixed columns listed in
config.json -> HorizontalMetadata.Columns go to one row per file in the
horizontal table; every other (non-skipped) column stays in the vertical
"Metadata" table. "Build Metadata Matrix" makes a separate table with every
distinct MetadataKey as a column (Metadata is left unchanged). Logic lives in
metadata_horizontal.py.

    python mh_app.py
"""
import os
import queue
import shutil
import sys
import threading
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from sqlalchemy import create_engine, text

import metadata_horizontal as mh

COLOR_DARK_BG = "#19222D"
COLOR_PANEL   = "#111922"
COLOR_ORANGE  = "#FF9900"
COLOR_BLUE    = "#0073BB"
COLOR_WHITE   = "#F2F3F3"
COLOR_ERROR   = "#FF4444"
VIEW_ROWS     = 500


class MetadataHorizontalApp(ctk.CTk):

    def __init__(self, config_path=mh.CONFIG_FILE):
        super().__init__()
        self.title("Metadata Horizontal")
        self.geometry("1300x800")
        self.minsize(900, 560)
        self.configure(fg_color=COLOR_DARK_BG)
        self._config_path = config_path
        self._engine = None
        self._app_config = {}
        self._busy = False
        self._ui_queue = queue.Queue()   # worker threads -> UI thread

        self._load_config()
        self._build()
        self._apply_tree_style()
        self._refresh_cfg_label()
        self._log("Ready", f"Config: {self._config_path}")
        self._drain_ui_queue()

    # ------------------------------------------------------------------ config
    def _load_config(self):
        try:
            self._app_config = mh.load_config(self._config_path)
        except Exception as e:
            self._app_config = {}
            messagebox.showerror("config.json", f"Cannot read {self._config_path}:\n{e}")

    def _cfg(self):
        """Fresh, validated config (re-read from disk so edits apply without restart)."""
        self._load_config()
        return mh.HConfig(self._app_config)

    def _refresh_cfg_label(self):
        try:
            cfg = mh.HConfig(self._app_config)
            self._cfg_label.configure(
                text=f"Table: {cfg.table}   |   {len(cfg.columns)} fixed columns: "
                     + ", ".join(cfg.columns.values()),
                text_color="gray")
            self._remove_var.set(cfg.remove_moved)
        except ValueError as e:
            self._cfg_label.configure(text=str(e).replace("\n", " "), text_color=COLOR_ERROR)

    # ------------------------------------------------------------------ layout
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=3)
        self.grid_rowconfigure(5, weight=1)

        # Row 0: connection
        conn = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=6)
        conn.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        c = self._app_config.get("Connection", {})
        self._vars = {}
        for label, key, width, default in (("Host", "Host", 160, "localhost"), ("Port", "Port", 60, 5432),
                                           ("Database", "Database", 220, ""), ("User", "User", 120, "postgres"),
                                           ("Role", "Role", 110, "")):
            ctk.CTkLabel(conn, text=label, text_color="gray").pack(side="left", padx=(10, 2))
            v = ctk.StringVar(value=str(c.get(key, default)))
            ctk.CTkEntry(conn, textvariable=v, width=width).pack(side="left", padx=(0, 4))
            self._vars[key] = v
        ctk.CTkLabel(conn, text="Password", text_color="gray").pack(side="left", padx=(10, 2))
        self._pw_var = ctk.StringVar(value=os.environ.get("PGPASSWORD", ""))
        self._pw_entry = ctk.CTkEntry(conn, textvariable=self._pw_var, width=120, show="•")
        self._pw_entry.pack(side="left", padx=(0, 8))
        self._krb_var = ctk.BooleanVar(value=bool(c.get("Kerberos", False)))
        ctk.CTkCheckBox(conn, text="Kerberos", variable=self._krb_var, width=90,
                        command=self._toggle_krb).pack(side="left", padx=(0, 8))
        self._toggle_krb()
        ctk.CTkButton(conn, text="Connect", width=90, fg_color=COLOR_ORANGE, text_color="black",
                      font=ctk.CTkFont(weight="bold"), command=self._connect).pack(side="left", padx=4)
        self._conn_status = ctk.CTkLabel(conn, text="● not connected", text_color="gray")
        self._conn_status.pack(side="left", padx=8)

        # Row 1: actions
        tb = ctk.CTkFrame(self, fg_color="transparent")
        tb.grid(row=1, column=0, sticky="ew", padx=8, pady=2)
        ctk.CTkButton(tb, text="⬆  Upload CSV/XLSX", width=170, fg_color=COLOR_ORANGE, text_color="black",
                      font=ctk.CTkFont(weight="bold"), command=self._upload).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="Create / Sync Table", width=150, fg_color=COLOR_BLUE,
                      command=self._ensure).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="⇄  Convert existing Metadata", width=210, fg_color="#1a6b5a",
                      command=self._convert).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="▦  Build Matrix", width=130, fg_color="#6a3d8a",
                      command=self._matrix).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="View Table", width=100, fg_color="#2a4a7a",
                      command=self._view).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="View Matrix", width=100, fg_color="#2a4a7a",
                      command=self._view_matrix).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="Show DDL", width=90, fg_color="#444444",
                      command=self._show_ddl).pack(side="left", padx=4)
        ctk.CTkButton(tb, text="Open config.json", width=130, fg_color="#444444",
                      command=self._open_config).pack(side="right", padx=4)
        ctk.CTkButton(tb, text="Reload config", width=110, fg_color="#444444",
                      command=self._reload_config).pack(side="right", padx=4)

        # Row 2: config summary
        bar = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=6)
        bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 4))
        self._cfg_label = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=11), text_color="gray",
                                       justify="left", anchor="w", wraplength=950)
        self._cfg_label.pack(side="left", padx=10, pady=4, fill="x", expand=True)
        self._remove_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(bar, text="Convert: remove moved keys from Metadata",
                        variable=self._remove_var, text_color="gray").pack(side="right", padx=8)

        # Row 3: grid
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=3, column=0, sticky="nsew", padx=8)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self._tree = ttk.Treeview(frame, show="headings")
        sy = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        sx = ttk.Scrollbar(frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")

        # Row 4: status
        self._status = ctk.CTkLabel(self, text="", text_color="gray", font=ctk.CTkFont(size=11), anchor="w")
        self._status.grid(row=4, column=0, sticky="ew", padx=12)

        # Row 5: log
        self._log_box = ctk.CTkTextbox(self, height=150, fg_color=COLOR_PANEL, text_color=COLOR_WHITE,
                                       font=("Consolas", 11))
        self._log_box.grid(row=5, column=0, sticky="nsew", padx=8, pady=(2, 8))

    def _apply_tree_style(self):
        s = ttk.Style()
        s.theme_use("default")
        s.configure("Treeview", background="#1e2b3a", foreground=COLOR_WHITE, fieldbackground="#1e2b3a",
                    rowheight=22, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background="#2e4a6a", foreground=COLOR_WHITE,
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected", COLOR_BLUE)])

    # ------------------------------------------------------------------ helpers
    def _ui(self, fn):
        """Run fn on the UI thread. Safe to call from worker threads (Tk is not)."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    def _log(self, status, msg):
        line = f"[{datetime.now():%H:%M:%S}] {status:<8} | {msg}\n"

        def upd():
            self._log_box.insert("end", line)
            self._log_box.see("end")
        self._ui(upd)

    def _set_status(self, msg, color="gray"):
        self._ui(lambda: self._status.configure(text=msg, text_color=color))

    def _run(self, title, fn, then_view=False, view_table=None):
        """Run fn(engine, cfg, log) in a worker thread; one job at a time."""
        if not self._engine:
            messagebox.showwarning("Not connected", "Connect to the database first.")
            return
        if self._busy:
            messagebox.showinfo("Busy", "Another job is still running.")
            return
        try:
            cfg = self._cfg()
        except ValueError as e:
            messagebox.showerror("HorizontalMetadata config", str(e))
            return
        self._refresh_cfg_label()
        self._busy = True
        self._set_status(f"{title}: running…")
        self._log(title, "started")

        def worker():
            try:
                res = fn(self._engine, cfg, lambda m: self._log(title, m))
            except Exception as e:
                msg = str(e).strip().split("\n")[0]
                self._log("ERROR", f"{title}: {msg}")
                self._set_status(f"{title} failed: {msg}", COLOR_ERROR)
                return
            finally:
                self._busy = False
            summary = ", ".join(f"{k}={v:,}" for k, v in (res or {}).items() if isinstance(v, int))
            self._set_status(f"{title}: done  {summary}", COLOR_WHITE)
            if then_view:
                self._ui(lambda: self._view(view_table))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ actions
    def _toggle_krb(self):
        self._pw_entry.configure(state="disabled" if self._krb_var.get() else "normal")

    def _connect(self):
        conn = {k: v.get().strip() for k, v in self._vars.items()}
        conn["Kerberos"] = self._krb_var.get()
        try:
            conn["Port"] = int(conn["Port"])
        except ValueError:
            messagebox.showerror("Port", "Port must be a number.")
            return
        try:
            conn["Role"] = mh.role_name(conn["Role"])
        except ValueError as e:
            messagebox.showerror("Role", str(e))
            return
        self._vars["Role"].set(conn["Role"])
        self._app_config.setdefault("Connection", {}).update(conn)
        url = mh.connection_url(self._app_config, password=self._pw_var.get())
        self._conn_status.configure(text="● connecting…", text_color="gray")

        def worker():
            try:
                eng = create_engine(url, pool_pre_ping=True)
                with eng.connect() as c:
                    ver = c.execute(text("SELECT version()")).scalar().split(",")[0]
                    ver += "  | login " + c.execute(text("SELECT session_user || ', running as ' || current_user")).scalar()
                    has_files = c.execute(text("SELECT to_regclass('public.\"Files\"') IS NOT NULL")).scalar()
            except Exception as e:
                msg = str(e).strip().split("\n")[0]
                self._ui(lambda: self._conn_status.configure(text="● failed", text_color=COLOR_ERROR))
                self._log("ERROR", f"Connect: {msg}")
                return
            if self._engine:
                self._engine.dispose()
            self._engine = eng
            self._ui(lambda: self._conn_status.configure(
                text=f"● {conn['Database']}@{conn['Host']}", text_color=COLOR_ORANGE))
            self._log("Connect", ver + ("" if has_files else "  — WARNING: no \"Files\" table in this database"))
            # remember connection fields (never the password)
            try:
                disk = mh.load_config(self._config_path)
                disk.setdefault("Connection", {}).update(conn)
                mh.save_config(disk, self._config_path)
            except Exception as e:
                self._log("WARN", f"Could not save connection to config.json: {e}")
            self._ui(self._view)

        threading.Thread(target=worker, daemon=True).start()

    def _ensure(self):
        self._run("Sync table",
                  lambda eng, cfg, log: {"added_columns": len(mh.ensure_table(eng, cfg, log=log))},
                  then_view=True)

    def _upload(self):
        path = filedialog.askopenfilename(
            title="Upload metadata (CSV or XLSX)",
            filetypes=[("Metadata files", "*.csv *.xlsx *.xlsm"), ("CSV files", "*.csv"),
                       ("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")])
        if not path:
            return
        folder = self._app_config.get("UnmatchedReportFolder") or "logs"
        report_dir = folder if os.path.isabs(folder) else os.path.join(mh.BASE_DIR, folder)

        def job(eng, cfg, log):
            return mh.import_file(eng, cfg, path, log=log, report_dir=report_dir,
                                  progress=lambda n: self._set_status(f"Upload: {n:,} rows read…"))
        self._run("Upload", job, then_view=True)

    def _convert(self):
        remove = self._remove_var.get()
        if not messagebox.askyesno(
                "Convert existing Metadata",
                "Copy the configured keys from the vertical Metadata table into the horizontal "
                "table (one row per file)?\n\n"
                + ("The moved keys will then be DELETED from Metadata."
                   if remove else "Metadata will be left unchanged.")):
            return
        self._run("Convert",
                  lambda eng, cfg, log: mh.convert_existing(eng, cfg, remove_from_vertical=remove, log=log),
                  then_view=True)

    def _matrix(self):
        try:
            table = mh.matrix_table(self._app_config)
        except ValueError as e:
            messagebox.showerror("MetadataMatrix config", str(e))
            return
        if not messagebox.askyesno(
                "Build Metadata Matrix",
                f"Rebuild {table} from Metadata?\n\nOne row per file, one column per distinct "
                f"MetadataKey. {table} is dropped and created again; Metadata is not changed."):
            return
        self._run("Matrix", lambda eng, cfg, log: mh.build_matrix(eng, self._app_config, log=log),
                  then_view=True, view_table=table)

    def _view_matrix(self):
        try:
            self._view(mh.matrix_table(self._app_config))
        except ValueError as e:
            self._set_status(str(e).split("\n")[0], COLOR_ERROR)

    def _view(self, table=None):
        if not self._engine:
            return
        try:
            table = table or mh.HConfig(self._app_config).table
        except ValueError as e:
            self._set_status(str(e).split("\n")[0], COLOR_ERROR)
            return

        def worker():
            try:
                with self._engine.connect() as conn:
                    if not conn.execute(text("SELECT to_regclass(:t)"),
                                        {"t": f"public.{mh.q(table)}"}).scalar():
                        self._set_status(f"Table {table} does not exist yet — click Create / Sync Table, "
                                         f"Build Matrix or upload a file")
                        return
                    total = conn.execute(text(f"SELECT COUNT(*) FROM public.{mh.q(table)}")).scalar()
                    res = conn.execute(text(
                        f'SELECT f."OriginalSharePointPath", h.* FROM public.{mh.q(table)} h '
                        f'JOIN public."Files" f ON f."FileId" = h."FileId" '
                        f'ORDER BY h."FileId" LIMIT {VIEW_ROWS}'))
                    cols, rows = list(res.keys()), res.fetchall()
            except Exception as e:
                self._set_status(str(e).strip().split("\n")[0], COLOR_ERROR)
                return
            self._ui(lambda: self._populate(cols, rows, total, table))

        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, cols, rows, total, table):
        self._tree.delete(*self._tree.get_children())
        # index ids: matrix column names are arbitrary metadata keys (spaces, duplicates of ours)
        ids = [f"c{i}" for i in range(len(cols))]
        self._tree["columns"] = ids
        for i, c in zip(ids, cols):
            self._tree.heading(i, text=c)
            self._tree.column(i, width=min(max(len(c) * 9, 90), 300), minwidth=40, stretch=False)
        for r in rows:
            self._tree.insert("", "end", values=["" if v is None else str(v) for v in r])
        self._status.configure(text=f"{table}: showing {len(rows):,} of {total:,} row(s), {len(cols)} column(s)",
                               text_color=COLOR_WHITE)

    def _show_ddl(self):
        try:
            cfg = self._cfg()
        except ValueError as e:
            messagebox.showerror("HorizontalMetadata config", str(e))
            return
        win = ctk.CTkToplevel(self)
        win.title(f"DDL — {cfg.table}")
        win.geometry("780x560")
        box = ctk.CTkTextbox(win, font=("Consolas", 12))
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("end", mh.ddl(cfg, with_sites=True)
                   + '\n\n-- "SiteId" (+ FK to "Sites" and trigger) is only added when the database\n'
                     '-- has the Sites table from the consolidation (MtrMigration_Consolidated).\n')

    def _reload_config(self):
        self._load_config()
        self._refresh_cfg_label()
        self._log("Config", f"Reloaded {self._config_path}")

    def _open_config(self):
        try:
            os.startfile(self._config_path)   # opens in the default editor (Windows)
        except Exception as e:
            messagebox.showinfo("config.json", f"{self._config_path}\n\n{e}")


if __name__ == "__main__":
    # exe (build_exe.ps1): first run writes the bundled default config.json next to the exe
    if getattr(sys, "frozen", False) and not os.path.exists(mh.CONFIG_FILE):
        shutil.copy2(os.path.join(sys._MEIPASS, "config.json"), mh.CONFIG_FILE)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    MetadataHorizontalApp().mainloop()
