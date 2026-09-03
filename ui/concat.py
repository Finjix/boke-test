"""Tkinter dialog for ordering and concatenating local videos."""

from __future__ import annotations

from datetime import datetime
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from config import AppConfig
from media.ffmpeg import concatenate_videos


VIDEO_FILETYPES = [
    ("视频", "*.mp4 *.mov *.mkv *.avi *.webm"),
    ("所有文件", "*.*"),
]


class ConcatenateWindow(tk.Toplevel):
    def __init__(self, parent: tk.Misc, config: AppConfig) -> None:
        super().__init__(parent)
        self.title("拼接视频")
        self.geometry("680x460")
        self.minsize(600, 400)
        self.transient(parent)

        self.config = config
        self.video_paths: list[Path] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._busy = False
        self._poll_id: str | None = None

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(100, self._poll_events)
        self.grab_set()
        self.focus_set()

    def _build(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        ttk.Label(
            root,
            text="视频顺序（可用上移/下移调整，拼接时按此顺序）",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.listbox = tk.Listbox(
            root,
            height=12,
            selectmode=tk.SINGLE,
            exportselection=False,
        )
        self.listbox.grid(row=1, column=0, sticky="nsew")

        controls = ttk.Frame(root)
        controls.grid(row=1, column=1, sticky="ns", padx=(10, 0))
        self.add_button = ttk.Button(
            controls,
            text="添加视频",
            command=self._add_videos,
        )
        self.add_button.pack(fill="x", pady=(0, 6))
        self.remove_button = ttk.Button(
            controls,
            text="移除",
            command=self._remove_selected,
        )
        self.remove_button.pack(fill="x", pady=3)
        self.up_button = ttk.Button(
            controls,
            text="上移",
            command=lambda: self._move_selected(-1),
        )
        self.up_button.pack(fill="x", pady=3)
        self.down_button = ttk.Button(
            controls,
            text="下移",
            command=lambda: self._move_selected(1),
        )
        self.down_button.pack(fill="x", pady=3)
        self.clear_button = ttk.Button(
            controls,
            text="清空",
            command=self._clear_videos,
        )
        self.clear_button.pack(fill="x", pady=3)

        self.count_var = tk.StringVar(value="已选择 0 个视频")
        ttk.Label(root, textvariable=self.count_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        self.status_var = tk.StringVar(value="请选择至少两个视频")
        ttk.Label(
            root,
            textvariable=self.status_var,
            wraplength=620,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.error_var = tk.StringVar()
        ttk.Label(
            root,
            textvariable=self.error_var,
            foreground="#b42318",
            wraplength=620,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        actions = ttk.Frame(root)
        actions.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        self.start_button = ttk.Button(
            actions,
            text="开始拼接",
            command=self._start,
        )
        self.start_button.pack(side="left", padx=(0, 8))
        self.close_button = ttk.Button(
            actions,
            text="关闭",
            command=self._on_close,
        )
        self.close_button.pack(side="left")

    def _add_videos(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self,
            title="添加要拼接的视频",
            filetypes=VIDEO_FILETYPES,
        )
        if not paths:
            return
        self.video_paths.extend(Path(path) for path in paths)
        self._refresh_list()
        self.error_var.set("")
        self.status_var.set("列表顺序即拼接顺序，可继续调整")

    def _refresh_list(self, *, selected_index: int | None = None) -> None:
        self.listbox.delete(0, tk.END)
        for index, path in enumerate(self.video_paths, start=1):
            self.listbox.insert(tk.END, f"{index}. {path.name}")
        self.count_var.set(f"已选择 {len(self.video_paths)} 个视频")
        if selected_index is not None and self.video_paths:
            selected_index = max(0, min(selected_index, len(self.video_paths) - 1))
            self.listbox.selection_set(selected_index)
            self.listbox.activate(selected_index)
            self.listbox.see(selected_index)

    def _selected_index(self) -> int | None:
        selection = self.listbox.curselection()
        return int(selection[0]) if selection else None

    def _remove_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        self.video_paths.pop(index)
        self._refresh_list(selected_index=min(index, len(self.video_paths) - 1))

    def _move_selected(self, offset: int) -> None:
        index = self._selected_index()
        if index is None:
            return
        target = index + offset
        if not 0 <= target < len(self.video_paths):
            return
        self.video_paths[index], self.video_paths[target] = (
            self.video_paths[target],
            self.video_paths[index],
        )
        self._refresh_list(selected_index=target)

    def _clear_videos(self) -> None:
        if self._busy:
            return
        self.video_paths.clear()
        self._refresh_list()
        self.error_var.set("")
        self.status_var.set("请选择至少两个视频")

    def _start(self) -> None:
        if self._busy:
            return
        if len(self.video_paths) < 2:
            self._set_error("至少选择 2 个视频")
            return
        try:
            destination = self._next_output_path()
        except OSError as exc:
            self._set_error(f"无法准备输出目录: {exc}")
            return

        paths = tuple(self.video_paths)
        self._set_busy(True)
        self.error_var.set("")
        self.status_var.set("正在拼接视频……")

        def worker() -> None:
            try:
                result = concatenate_videos(
                    paths,
                    destination,
                    ffmpeg_bin=self.config.ffmpeg_bin,
                    timeout=max(float(self.config.http_timeout), 600.0),
                )
            except Exception as exc:
                self.events.put(("error", str(exc)))
            else:
                self.events.put(("success", result))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _poll_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "success":
                    self._set_busy(False)
                    self.status_var.set(f"拼接完成：{Path(value).name}")
                else:
                    self._set_busy(False)
                    self._set_error(value)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._poll_id = self.after(100, self._poll_events)

    def _next_output_path(self) -> Path:
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = output_dir / f"concatenated_{timestamp}.mp4"
        suffix = 1
        while destination.exists() or destination in self.video_paths:
            destination = output_dir / f"concatenated_{timestamp}_{suffix:02d}.mp4"
            suffix += 1
        return destination

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (
            self.add_button,
            self.remove_button,
            self.up_button,
            self.down_button,
            self.clear_button,
            self.start_button,
        ):
            button.configure(state=state)
        self.listbox.configure(state=state)
        self.close_button.configure(state=state)

    def _set_error(self, error: object) -> None:
        message = " ".join(str(error).split())
        self.error_var.set(message or "拼接失败")

    def _on_close(self) -> None:
        if self._busy:
            return
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
