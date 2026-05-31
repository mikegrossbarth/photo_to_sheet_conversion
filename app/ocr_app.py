from __future__ import annotations

import base64
import csv
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from google import genai
except Exception:
    genai = None

from live_comps_ocr.cert_extraction import (
    ModelQuotaExceeded,
    ModelResponseParseError,
    TemporaryModelUnavailable,
)
from multi_card_extraction import identify_cards_sync
from xlsx_export import EXPORT_HEADERS, EXPORT_KEYS, build_export_rows, write_xlsx


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "outputs"
IMAGE_TYPES = [
    ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp"),
    ("All files", "*.*"),
]
DISPLAY_COLUMNS = (
    "status",
    "quality",
    "file",
    "card_index",
    "position",
    "grading_company",
    "cert_number",
    "player",
    "attributes",
    "year",
    "set",
    "grade",
    "confidence",
)
READ_ONLY_COLUMNS = {"status", "quality", "file", "card_index"}

COLUMN_HEADINGS = {
    "status": "Status",
    "quality": "Score",
    "file": "File",
    "card_index": "#",
    "position": "Position",
    "grading_company": "Company",
    "cert_number": "Cert #",
    "player": "Player / Subject",
    "year": "Year",
    "set": "Set",
    "attributes": "Attributes",
    "grade": "Grade",
    "confidence": "Confidence",
}

COLUMN_WIDTHS = {
    "status": (86, 150),
    "quality": (58, 76),
    "file": (140, 260),
    "card_index": (48, 62),
    "position": (90, 150),
    "grading_company": (82, 120),
    "cert_number": (108, 150),
    "player": (150, 260),
    "year": (64, 92),
    "set": (150, 300),
    "attributes": (150, 320),
    "grade": (64, 84),
    "confidence": (90, 120),
}


@dataclass
class CardRow:
    path: Path
    data: dict = field(default_factory=dict)

    def as_export_row(self) -> dict:
        row = {**self.data}
        row["source_file"] = self.path.name
        row["file"] = self.path.name
        return row


class OcrSpreadsheetApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OCR Photos to Spreadsheet")
        self.geometry("1180x720")
        self.minsize(980, 560)

        self.rows: list[CardRow] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_requested = False
        self.client = None
        self.summary_var = tk.StringVar(value="0 photos | 0 detected | 0 readable")
        self.cell_editor: tk.Entry | None = None
        self.editing_cell: tuple[str, str] | None = None

        self._load_env()
        self._build_ui()
        self.after(100, self._poll_events)

    def _load_env(self) -> None:
        if load_dotenv:
            load_dotenv(Path(__file__).resolve().parent / ".env")
            load_dotenv(ROOT / ".env")
            live_comps_env = (
                Path(r"C:\Users\User\Documents\Codex\2026-05-21\automatic-sheet-review\live-comps\.env")
            )
            if live_comps_env.exists():
                load_dotenv(live_comps_env, override=False)

    def _build_ui(self) -> None:
        palette = {
            "bg": "#eef1f4",
            "header": "#17212b",
            "header_text": "#f8fafc",
            "muted": "#64748b",
            "panel": "#ffffff",
            "line": "#cbd5e1",
            "button": "#2563eb",
            "button_hover": "#1d4ed8",
            "button_text": "#ffffff",
            "soft_button": "#f8fafc",
            "soft_hover": "#e2e8f0",
            "text": "#0f172a",
            "success": "#e7f7ee",
            "detected": "#fff7db",
            "error": "#fee2e2",
        }
        self.configure(bg=palette["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("App.TFrame", background=palette["bg"])
        style.configure("Panel.TFrame", background=palette["panel"])
        style.configure("Header.TFrame", background=palette["header"])
        style.configure("HeaderTitle.TLabel", background=palette["header"], foreground=palette["header_text"], font=("Segoe UI Semibold", 20))
        style.configure("HeaderSub.TLabel", background=palette["header"], foreground="#cbd5e1", font=("Segoe UI", 10))
        style.configure("Summary.TLabel", background=palette["panel"], foreground=palette["muted"], font=("Segoe UI Semibold", 10))
        style.configure("TLabel", background=palette["bg"], foreground=palette["text"], font=("Segoe UI", 10))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 10), padding=(14, 8), background=palette["button"], foreground=palette["button_text"], borderwidth=0)
        style.map("Primary.TButton", background=[("active", palette["button_hover"]), ("disabled", "#94a3b8")])
        style.configure("Soft.TButton", font=("Segoe UI", 10), padding=(14, 8), background=palette["soft_button"], foreground=palette["text"], borderwidth=1)
        style.map("Soft.TButton", background=[("active", palette["soft_hover"]), ("disabled", "#e2e8f0")])
        style.configure("Treeview", rowheight=32, font=("Segoe UI", 10), background=palette["panel"], fieldbackground=palette["panel"], foreground=palette["text"], borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), background="#e2e8f0", foreground="#334155", padding=(8, 7), borderwidth=0)
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", palette["text"])])
        style.configure("Horizontal.TProgressbar", background=palette["button"], troughcolor="#dbe3ec", bordercolor=palette["bg"], lightcolor=palette["button"], darkcolor=palette["button"])

        header = ttk.Frame(self, style="Header.TFrame", padding=(18, 16, 18, 16))
        header.pack(fill=tk.X)

        title_group = ttk.Frame(header, style="Header.TFrame")
        title_group.pack(side=tk.LEFT)
        ttk.Label(title_group, text="Graded Card OCR", style="HeaderTitle.TLabel").pack(anchor=tk.W)
        ttk.Label(
            title_group,
            text="Batch scan card photos, sort the strongest reads first, then export to spreadsheet.",
            style="HeaderSub.TLabel",
        ).pack(anchor=tk.W, pady=(3, 0))

        button_bar = ttk.Frame(header, style="Header.TFrame")
        button_bar.pack(side=tk.RIGHT)

        self.add_button = ttk.Button(button_bar, text="Add Pictures", command=self.add_pictures, style="Soft.TButton")
        self.add_button.pack(side=tk.LEFT, padx=(0, 8))
        self.folder_button = ttk.Button(button_bar, text="Add Folder", command=self.add_folder, style="Soft.TButton")
        self.folder_button.pack(side=tk.LEFT, padx=(0, 8))
        self.scan_button = ttk.Button(button_bar, text="Scan", command=self.scan, style="Primary.TButton")
        self.scan_button.pack(side=tk.LEFT, padx=(0, 8))
        self.export_button = ttk.Button(button_bar, text="Export Spreadsheet", command=self.export_spreadsheet, style="Soft.TButton")
        self.export_button.pack(side=tk.LEFT)

        summary = ttk.Frame(self, style="Panel.TFrame", padding=(18, 10, 18, 10))
        summary.pack(fill=tk.X, padx=16, pady=(14, 10))
        ttk.Label(summary, textvariable=self.summary_var, style="Summary.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            summary,
            text="Rows auto-sort by cert + company + readable details",
            style="Summary.TLabel",
        ).pack(side=tk.RIGHT)

        content = ttk.Frame(self, style="Panel.TFrame", padding=(1, 1, 1, 1))
        content.pack(fill=tk.BOTH, expand=True)
        content.configure(style="Panel.TFrame")
        content.pack_configure(padx=16, pady=(0, 12))

        self.tree = ttk.Treeview(content, columns=DISPLAY_COLUMNS, show="headings", selectmode="extended")
        for col in DISPLAY_COLUMNS:
            min_width, max_width = COLUMN_WIDTHS[col]
            self.tree.heading(col, text=COLUMN_HEADINGS[col])
            self.tree.column(col, width=min_width, minwidth=min_width, stretch=col in {"file", "set", "player"})
        self.tree.tag_configure("readable", background=palette["success"])
        self.tree.tag_configure("detected", background=palette["detected"])
        self.tree.tag_configure("error", background=palette["error"])
        self.tree.tag_configure("queued", background=palette["panel"])
        self.tree.bind("<Double-1>", self._begin_cell_edit)
        self.tree.grid(row=0, column=0, sticky="nsew")

        y_scrollbar = ttk.Scrollbar(content, orient=tk.VERTICAL, command=self.tree.yview)
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(content, orient=tk.HORIZONTAL, command=self.tree.xview)
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        bottom = ttk.Frame(self, style="App.TFrame", padding=(16, 0, 16, 16))
        bottom.pack(fill=tk.X)

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill=tk.X, side=tk.TOP, pady=(0, 8))

        self.status_var = tk.StringVar(value="Add photos of graded card slabs to begin.")
        self.status_label = ttk.Label(bottom, textvariable=self.status_var)
        self.status_label.pack(side=tk.LEFT)

        self.delete_button = ttk.Button(bottom, text="Delete Selected", command=self.delete_selected_rows, style="Soft.TButton")
        self.delete_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.clear_button = ttk.Button(bottom, text="Clear", command=self.clear_rows, style="Soft.TButton")
        self.clear_button.pack(side=tk.RIGHT)

    def add_pictures(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose card photos", filetypes=IMAGE_TYPES)
        self._add_paths([Path(p) for p in paths])

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder of card photos")
        if not folder:
            return
        paths = []
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"):
            paths.extend(Path(folder).glob(ext))
        self._add_paths(sorted(paths))

    def _add_paths(self, paths: list[Path]) -> None:
        seen = {row.path.resolve() for row in self.rows}
        added = 0
        for path in paths:
            if not path.exists() or path.resolve() in seen:
                continue
            self.rows.append(CardRow(path=path, data={"status": "Queued", "quality": 0}))
            seen.add(path.resolve())
            added += 1
        self._refresh_table()
        self._update_summary()
        self.status_var.set(f"Added {added} photo(s). {len(self.rows)} total queued. Multi-card photos will expand into separate rows after scanning.")

    def scan(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Scan running", "A scan is already running.")
            return
        if not self.rows:
            messagebox.showinfo("No photos", "Add pictures before scanning.")
            return
        if genai is None:
            messagebox.showerror(
                "Missing dependency",
                "google-genai is not installed. Run install_dependencies.bat, then open the app again.",
            )
            return

        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not api_key:
            messagebox.showerror(
                "Missing GOOGLE_API_KEY",
                "Create app\\.env or set GOOGLE_API_KEY in your environment before scanning.",
            )
            return

        self.client = genai.Client(api_key=api_key)
        self.stop_requested = False
        for row in self.rows:
            if row.data.get("status") in {"Done", "Detected", "Error", "No cards found"}:
                row.data["status"] = "Queued"
                row.data["quality"] = 0
        self.progress.configure(maximum=len(self.rows), value=0)
        self._refresh_table()
        self._update_summary()
        self.scan_button.configure(state=tk.DISABLED)
        self.add_button.configure(state=tk.DISABLED)
        self.folder_button.configure(state=tk.DISABLED)
        self.worker = threading.Thread(target=self._scan_worker, daemon=True)
        self.worker.start()

    def _scan_worker(self) -> None:
        source_rows = []
        seen_paths = set()
        for row in self.rows:
            resolved = row.path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            source_rows.append(row)
        for index, row in enumerate(source_rows):
            if self.stop_requested:
                break
            self.events.put(("status", (row.path, "Scanning")))
            try:
                image_b64 = base64.b64encode(row.path.read_bytes()).decode("utf-8")
                cards = identify_cards_sync(self.client, image_b64)
                if not cards:
                    cards = [{
                        "card_index": 1,
                        "position": "",
                        "is_graded_slab": False,
                        "grading_company": "unknown",
                        "cert_number": "",
                        "confidence": "low",
                        "status": "No cards found",
                        "error": "",
                    }]
                for card in cards:
                    has_value = any(card.get(key) for key in ("cert_number", "player", "year", "set", "attributes", "grade", "label_text"))
                    if card.get("is_graded_slab", True):
                        card["status"] = "Done" if has_value else "Detected"
                    else:
                        card["status"] = "No cards found"
                    card["error"] = ""
                self.events.put(("result", (row.path, cards)))
            except (TemporaryModelUnavailable, ModelQuotaExceeded, ModelResponseParseError) as error:
                self.events.put(("error", (row.path, str(error))))
            except Exception as error:
                self.events.put(("error", (row.path, str(error))))
            self.events.put(("progress", index + 1))
        self.events.put(("finished", None))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    path, status = payload
                    index = self._first_row_index_for_path(path)
                    if index is not None:
                        self.rows[index].data["status"] = status
                        self._update_row(index)
                        self._update_summary()
                    self.status_var.set(f"Scanning {Path(path).name}...")
                elif event == "result":
                    path, cards = payload
                    self._replace_rows_for_path(path, cards)
                    self._sort_rows()
                    self._refresh_table()
                    self._update_summary()
                elif event == "error":
                    path, error = payload
                    index = self._first_row_index_for_path(path)
                    if index is not None:
                        self.rows[index].data.update({"status": "Error", "error": error})
                        self._update_row(index)
                        self._update_summary()
                elif event == "progress":
                    self.progress.configure(value=payload)
                elif event == "finished":
                    self.scan_button.configure(state=tk.NORMAL)
                    self.add_button.configure(state=tk.NORMAL)
                    self.folder_button.configure(state=tk.NORMAL)
                    readable = sum(1 for row in self.rows if row.data.get("status") == "Done")
                    detected = sum(1 for row in self.rows if row.data.get("status") in {"Done", "Detected"})
                    photo_count = len({row.path.resolve() for row in self.rows})
                    self.status_var.set(
                        f"Scan complete. {detected} slab row(s) detected, {readable} readable row(s), from {photo_count} photo(s)."
                    )
                    self._update_summary()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, _ in enumerate(self.rows):
            self.tree.insert("", tk.END, iid=str(index), values=self._display_values(index), tags=(self._row_tag(index),))
        self._autosize_columns()

    def _update_row(self, index: int) -> None:
        iid = str(index)
        if self.tree.exists(iid):
            self.tree.item(iid, values=self._display_values(index))
            self.tree.item(iid, tags=(self._row_tag(index),))
            self._autosize_columns()

    def _display_values(self, index: int) -> tuple[str, ...]:
        row = self.rows[index]
        data = row.data
        values = []
        for col in DISPLAY_COLUMNS:
            if col == "file":
                values.append(row.path.name)
            else:
                values.append(str(data.get(col, "") or ""))
        return tuple(values)

    def _autosize_columns(self) -> None:
        for col in DISPLAY_COLUMNS:
            min_width, max_width = COLUMN_WIDTHS[col]
            header_width = len(COLUMN_HEADINGS[col]) * 8 + 24
            content_width = header_width
            for row in self.rows[:250]:
                if col == "file":
                    value = row.path.name
                else:
                    value = str(row.data.get(col, "") or "")
                content_width = max(content_width, len(value) * 7 + 28)
            width = max(min_width, min(content_width, max_width))
            self.tree.column(col, width=width, minwidth=min_width)

    def _begin_cell_edit(self, event) -> None:
        if self.worker and self.worker.is_alive():
            return
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if not row_id or not column_id:
            return
        col_index = int(column_id.replace("#", "")) - 1
        if col_index < 0 or col_index >= len(DISPLAY_COLUMNS):
            return
        column = DISPLAY_COLUMNS[col_index]
        if column in READ_ONLY_COLUMNS:
            return
        bbox = self.tree.bbox(row_id, column_id)
        if not bbox:
            return
        self._finish_cell_edit(save=False)

        x, y, width, height = bbox
        current_value = self.tree.set(row_id, column)
        editor = tk.Entry(self.tree, font=("Segoe UI", 10), relief=tk.SOLID, borderwidth=1)
        editor.insert(0, current_value)
        editor.select_range(0, tk.END)
        editor.focus_set()
        editor.place(x=x, y=y, width=width, height=height)
        editor.bind("<Return>", lambda _event: self._finish_cell_edit(save=True))
        editor.bind("<Escape>", lambda _event: self._finish_cell_edit(save=False))
        editor.bind("<FocusOut>", lambda _event: self._finish_cell_edit(save=True))
        self.cell_editor = editor
        self.editing_cell = (row_id, column)

    def _finish_cell_edit(self, save: bool) -> None:
        if not self.cell_editor or not self.editing_cell:
            return
        editor = self.cell_editor
        row_id, column = self.editing_cell
        value = editor.get()
        editor.destroy()
        self.cell_editor = None
        self.editing_cell = None

        if not save or column in READ_ONLY_COLUMNS:
            return
        try:
            row_index = int(row_id)
        except ValueError:
            return
        if row_index < 0 or row_index >= len(self.rows):
            return

        normalized = self._normalize_manual_value(column, value)
        self.rows[row_index].data[column] = normalized
        self.rows[row_index].data["quality"] = self._quality_score(self.rows[row_index].data)
        self._sort_rows()
        self._refresh_table()
        self._update_summary()
        self.status_var.set(f"Updated {COLUMN_HEADINGS[column]} for selected row.")

    def _normalize_manual_value(self, column: str, value: str) -> str:
        value = str(value or "").strip()
        if column == "cert_number":
            return "".join(ch for ch in value if ch.isdigit())
        return value.upper()

    def _row_tag(self, index: int) -> str:
        status = str(self.rows[index].data.get("status", "") or "").lower()
        if status == "done":
            return "readable"
        if status == "detected":
            return "detected"
        if status == "error":
            return "error"
        return "queued"

    def _update_summary(self) -> None:
        photo_count = len({row.path.resolve() for row in self.rows})
        detected = sum(1 for row in self.rows if row.data.get("status") in {"Done", "Detected"})
        readable = sum(1 for row in self.rows if row.data.get("status") == "Done")
        certs = sum(1 for row in self.rows if row.data.get("cert_number"))
        self.summary_var.set(f"{photo_count} photos | {detected} detected | {readable} readable | {certs} with certs")

    def _quality_score(self, data: dict) -> int:
        score = 0
        if data.get("cert_number"):
            score += 45
        company = str(data.get("grading_company", "") or "").strip().lower()
        if company and company != "unknown":
            score += 15
        if data.get("player"):
            score += 14
        if data.get("year"):
            score += 8
        if data.get("set"):
            score += 7
        if data.get("attributes"):
            score += 6
        if data.get("grade"):
            score += 7
        confidence = str(data.get("confidence", "") or "").lower()
        score += {"high": 4, "medium": 2, "low": 0}.get(confidence, 0)
        return min(score, 100)

    def _sort_rows(self) -> None:
        for row in self.rows:
            row.data["quality"] = self._quality_score(row.data)
        self.rows.sort(
            key=lambda row: (
                -int(row.data.get("quality") or 0),
                row.path.name.lower(),
                int(row.data.get("card_index") or 9999),
            )
        )

    def _first_row_index_for_path(self, path: Path) -> int | None:
        resolved = Path(path).resolve()
        for index, row in enumerate(self.rows):
            if row.path.resolve() == resolved:
                return index
        return None

    def _replace_rows_for_path(self, path: Path, cards: list[dict]) -> None:
        resolved = Path(path).resolve()
        first_index = self._first_row_index_for_path(path)
        if first_index is None:
            return
        self.rows = [row for row in self.rows if row.path.resolve() != resolved]
        new_rows = [CardRow(path=Path(path), data=card) for card in cards]
        for offset, row in enumerate(new_rows):
            if not row.data.get("card_index"):
                row.data["card_index"] = offset + 1
        self.rows[first_index:first_index] = new_rows

    def export_spreadsheet(self) -> None:
        if not self.rows:
            messagebox.showinfo("No rows", "Add and scan photos before exporting.")
            return
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        default_path = OUTPUT_DIR / f"graded-card-ocr-{timestamp}.xlsx"
        path = filedialog.asksaveasfilename(
            title="Save spreadsheet",
            initialdir=str(OUTPUT_DIR),
            initialfile=default_path.name,
            defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx"), ("CSV", "*.csv")],
        )
        if not path:
            return
        out_path = Path(path)
        self._sort_rows()
        rows = [row.as_export_row() for row in self.rows]
        if out_path.suffix.lower() == ".csv":
            self._write_csv(out_path, rows)
        else:
            write_xlsx(out_path, rows)
        self.status_var.set(f"Saved {out_path}")
        messagebox.showinfo("Export complete", f"Saved spreadsheet:\n{out_path}")

    def _write_csv(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_HEADERS)
            for row in build_export_rows(rows):
                writer.writerow([row.get(key, "") for key in EXPORT_KEYS])

    def clear_rows(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Scan running", "Wait for the scan to finish before clearing.")
            return
        self.rows.clear()
        self._refresh_table()
        self._update_summary()
        self.progress.configure(value=0)
        self.status_var.set("Cleared. Add photos of graded card slabs to begin.")

    def delete_selected_rows(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Scan running", "Wait for the scan to finish before deleting rows.")
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("No selection", "Select one or more card rows to delete.")
            return
        selected_indexes = sorted((int(iid) for iid in selected), reverse=True)
        for index in selected_indexes:
            if 0 <= index < len(self.rows):
                del self.rows[index]
        self._refresh_table()
        self._update_summary()
        self.status_var.set(f"Deleted {len(selected_indexes)} selected card row(s).")


if __name__ == "__main__":
    OcrSpreadsheetApp().mainloop()
