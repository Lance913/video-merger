"""
Video Merger - joins multiple long video files (e.g. 1-3 hour recordings)
into a single file, in whatever order you arrange them.

If all the clips already share the same resolution and codec, they are
joined with a raw stream copy - no re-encoding, so it finishes in seconds
even for many hours of footage. If they don't match, mismatched clips are
normalized (re-encoded) to a common resolution first, then joined the
same fast way.

Runs as a normal Python script, or as a bundled Windows .exe (built via
PyInstaller - see README.md and .github/workflows/build-windows.yml).
"""

import os
import re
import sys
import time
import queue
import shutil
import tempfile
import subprocess
import threading
from collections import Counter
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Video Merger"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts"}

# Hide the console window ffmpeg would otherwise flash on Windows.
_STARTUPINFO = None
if os.name == "nt":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW


def get_ffmpeg_path():
    """Locate ffmpeg: bundled binary when frozen (offline), else the
    auto-downloaded one from imageio-ffmpeg (dev/script mode)."""
    if getattr(sys, "frozen", False):
        bundled_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), bundled_name)
        if os.path.exists(bundled):
            return bundled
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class FfmpegError(RuntimeError):
    def __init__(self, message, output):
        super().__init__(message)
        self.output = output


def run_ffmpeg(args, cancel_event=None, on_time=None):
    """Run ffmpeg with the given args (list, without the 'ffmpeg' itself).
    Returns combined stdout+stderr output. Raises FfmpegError on non-zero
    exit, RuntimeError('Cancelled') if cancel_event is set mid-run.

    If on_time is given, it's called repeatedly with the number of seconds
    of output ffmpeg has processed so far (via ffmpeg's own -progress feed,
    which updates live regardless of preset/codec - unlike its normal
    stderr stats line, which only prints a final summary when not attached
    to a terminal)."""
    progress_path = None
    poller_stop = threading.Event()
    poller_thread = None

    if on_time is not None:
        fd, progress_path = tempfile.mkstemp(prefix="ffmpeg_progress_")
        os.close(fd)
        args = ["-progress", progress_path, "-nostats"] + list(args)

        def poll():
            while not poller_stop.is_set():
                try:
                    with open(progress_path, "r", errors="ignore") as f:
                        text = f.read()
                    matches = re.findall(r"out_time_ms=(-?\d+)", text)
                    if matches:
                        # NB: despite the name, ffmpeg's out_time_ms is microseconds.
                        us = int(matches[-1])
                        if us >= 0:
                            on_time(us / 1_000_000)
                except OSError:
                    pass
                poller_stop.wait(0.3)

        poller_thread = threading.Thread(target=poll, daemon=True)
        poller_thread.start()

    cmd = [get_ffmpeg_path()] + args
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        startupinfo=_STARTUPINFO,
    )
    output_lines = []
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                raise RuntimeError("Cancelled")
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line:
                output_lines.append(line)
    finally:
        poller_stop.set()
        if poller_thread is not None:
            poller_thread.join(timeout=1)
        if progress_path is not None:
            try:
                os.remove(progress_path)
            except OSError:
                pass

    output = "".join(output_lines)
    if proc.returncode != 0:
        raise FfmpegError("".join(output_lines[-30:]), output)
    return output


def probe_video_info(path):
    """Return dict(duration, width, height, vcodec, has_audio, acodec) by
    parsing ffmpeg's own stderr output (no ffprobe binary required)."""
    try:
        text = run_ffmpeg(["-i", path, "-f", "null", "-"])
    except FfmpegError as e:
        text = e.output

    dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if not dur_m:
        raise RuntimeError(f"Could not read info for {os.path.basename(path)} - is it a valid video file?")
    h, mnt, s = dur_m.groups()
    duration = int(h) * 3600 + int(mnt) * 60 + float(s)

    vid_m = re.search(r"Video:\s*([a-zA-Z0-9_]+).*?(\d{2,5})x(\d{2,5})", text)
    if not vid_m:
        raise RuntimeError(f"Could not find a video stream in {os.path.basename(path)}")
    vcodec, width, height = vid_m.group(1), int(vid_m.group(2)), int(vid_m.group(3))

    aud_m = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", text)

    return {
        "path": path,
        "duration": duration,
        "width": width,
        "height": height,
        "vcodec": vcodec,
        "has_audio": aud_m is not None,
        "acodec": aud_m.group(1) if aud_m else None,
    }


def _quote_concat_path(path):
    escaped = path.replace("'", "'\\''")
    return f"file '{escaped}'"


def concat_copy(files, output_path, tmp_dir, cancel_event, on_time=None):
    list_path = os.path.join(tmp_dir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write(_quote_concat_path(os.path.abspath(p)) + "\n")
    run_ffmpeg([
        "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", "-movflags", "+faststart", output_path,
    ], cancel_event=cancel_event, on_time=on_time)


def normalize_video(path, has_audio, target_w, target_h, out_path, cancel_event, on_time=None):
    vf = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    args = ["-y", "-i", path]
    if not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += ["-vf", vf, "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    if not has_audio:
        args += ["-map", "0:v", "-map", "1:a", "-shortest"]
    args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    run_ffmpeg(args, cancel_event=cancel_event, on_time=on_time)


def merge_videos(files, output_path, log, progress, cancel_event):
    """progress(percent, eta_seconds) is called repeatedly as work proceeds;
    eta_seconds is None until there's enough data to estimate it."""
    if len(files) < 2:
        raise RuntimeError("Add at least two videos to merge.")

    log("Reading video info...")
    infos = [probe_video_info(f) for f in files]
    for info in infos:
        log(f"  {os.path.basename(info['path'])}: {info['width']}x{info['height']}, "
            f"{info['vcodec']}, {info['duration']/60:.1f} min")

    same_format = (
        len({i["width"] for i in infos}) == 1
        and len({i["height"] for i in infos}) == 1
        and len({i["vcodec"] for i in infos}) == 1
        and len({i["acodec"] for i in infos}) == 1
    )
    total_duration = sum(i["duration"] for i in infos)
    # The fallback path processes the total runtime twice: once re-encoding
    # (normalize) and once stream-copying the result back together.
    total_work = total_duration if same_format else total_duration * 2

    start_time = time.time()
    completed_seconds = 0.0

    def report(current_step_seconds):
        done = min(completed_seconds + current_step_seconds, total_work)
        frac = done / total_work if total_work > 0 else 1.0
        elapsed = time.time() - start_time
        eta = elapsed * (1 - frac) / frac if frac > 0.01 and elapsed > 1.5 else None
        progress(frac * 100, eta)

    tmp_dir = tempfile.mkdtemp(prefix="video_merger_")
    try:
        if same_format:
            log("All clips share the same format - fast merge (no re-encoding)...")
            try:
                concat_copy(files, output_path, tmp_dir, cancel_event, on_time=report)
                completed_seconds += total_duration
                report(0)
                return
            except FfmpegError as e:
                log(f"  fast merge failed ({e}); falling back to re-encoding...")
        else:
            log("Clips differ in resolution/format - normalizing to a common format first "
                "(slower, only needed for the mismatched clips)...")

        res_counts = Counter((i["width"], i["height"]) for i in infos)
        target_w, target_h = res_counts.most_common(1)[0][0]
        log(f"  target resolution: {target_w}x{target_h}")

        normalized = []
        for idx, (path, info) in enumerate(zip(files, infos)):
            if cancel_event.is_set():
                raise RuntimeError("Cancelled")
            out = os.path.join(tmp_dir, f"norm_{idx:04d}.mp4")
            log(f"  normalizing {idx + 1}/{len(files)}: {os.path.basename(path)}")
            normalize_video(path, info["has_audio"], target_w, target_h, out, cancel_event, on_time=report)
            normalized.append(out)
            completed_seconds += info["duration"]
            report(0)

        log("Joining normalized clips...")
        concat_copy(normalized, output_path, tmp_dir, cancel_event, on_time=report)
        completed_seconds += total_duration
        report(0)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class VideoMergerApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("720x620")
        root.minsize(640, 540)

        self.files = []
        self.cancel_event = threading.Event()
        self.worker = None
        self.log_queue = queue.Queue()

        self._build_ui()
        self.root.after(100, self._drain_log_queue)

    # ---------------------------------------------------------- UI build
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        files_frame = ttk.LabelFrame(self.root, text="1. Videos to merge, in order (top = first)")
        files_frame.pack(fill="both", expand=True, **pad)

        self.files_list = tk.Listbox(files_frame, height=10, selectmode="extended")
        self.files_list.pack(fill="both", expand=True, side="left", padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(files_frame, command=self.files_list.yview)
        scroll.pack(side="left", fill="y", pady=8)
        self.files_list.config(yscrollcommand=scroll.set)

        btns = ttk.Frame(files_frame)
        btns.pack(side="left", fill="y", padx=8, pady=8)
        ttk.Button(btns, text="Add Files...", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Add Folder...", command=self.add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="Move Up", command=lambda: self.move_selected(-1)).pack(fill="x", pady=(12, 2))
        ttk.Button(btns, text="Move Down", command=lambda: self.move_selected(1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove Selected", command=self.remove_selected).pack(fill="x", pady=(12, 2))
        ttk.Button(btns, text="Clear", command=self.clear_files).pack(fill="x", pady=2)

        out_frame = ttk.LabelFrame(self.root, text="2. Output file")
        out_frame.pack(fill="x", **pad)
        self.output_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Videos", "merged.mp4")
        )
        ttk.Entry(out_frame, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ttk.Button(out_frame, text="Browse...", command=self.choose_output).pack(side="left", padx=8, pady=8)

        action_frame = ttk.Frame(self.root)
        action_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(action_frame, text="Start Merging", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(action_frame, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill="x", side="top")
        self.progress_label_var = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=self.progress_label_var).pack(anchor="w", pady=(4, 0))

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _refresh_listbox(self):
        self.files_list.delete(0, "end")
        for i, p in enumerate(self.files, 1):
            self.files_list.insert("end", f"{i}. {os.path.basename(p)}")

    # ------------------------------------------------------------ actions
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select videos, in the order you want them added",
            filetypes=[("Video files", " ".join(f"*{e}" for e in VIDEO_EXTS)), ("All files", "*.*")],
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
        self._refresh_listbox()

    def add_folder(self):
        folder = filedialog.askdirectory(title="Select a folder of videos")
        if not folder:
            return
        for name in sorted(os.listdir(folder)):
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                p = os.path.join(folder, name)
                if p not in self.files:
                    self.files.append(p)
        self._refresh_listbox()

    def move_selected(self, direction):
        sel = list(self.files_list.curselection())
        if not sel:
            return
        indices = sel if direction < 0 else list(reversed(sel))
        for i in indices:
            j = i + direction
            if 0 <= j < len(self.files):
                self.files[i], self.files[j] = self.files[j], self.files[i]
        self._refresh_listbox()
        for i in sel:
            j = max(0, min(len(self.files) - 1, i + direction))
            self.files_list.selection_set(j)

    def remove_selected(self):
        for i in reversed(self.files_list.curselection()):
            del self.files[i]
        self._refresh_listbox()

    def clear_files(self):
        self.files = []
        self._refresh_listbox()

    def choose_output(self):
        path = filedialog.asksaveasfilename(
            title="Save merged video as",
            defaultextension=".mp4",
            initialfile=os.path.basename(self.output_var.get()) or "merged.mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if path:
            self.output_var.set(path)

    def start(self):
        if len(self.files) < 2:
            messagebox.showwarning(APP_TITLE, "Add at least two videos first.")
            return
        if not self.output_var.get().strip():
            messagebox.showwarning(APP_TITLE, "Choose an output file first.")
            return
        if self.worker and self.worker.is_alive():
            return
        self.cancel_event.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0
        self.progress_label_var.set("0.00%")
        self._log_clear()

        params = dict(files=list(self.files), output_path=self.output_var.get())
        self.worker = threading.Thread(target=self._worker_main, kwargs=params, daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_event.set()
        self.log_queue.put(("log", "Cancelling..."))

    def _worker_main(self, files, output_path):
        try:
            out_dir = os.path.dirname(output_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            merge_videos(
                files, output_path,
                log=lambda msg: self.log_queue.put(("log", msg)),
                progress=lambda pct, eta: self.log_queue.put(("progress", (pct, eta))),
                cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                self.log_queue.put(("log", "Cancelled."))
            else:
                self.log_queue.put(("log", f"Done -> {output_path}"))
                self.log_queue.put(("progress", (100.0, 0)))
        except RuntimeError as e:
            if str(e) == "Cancelled":
                self.log_queue.put(("log", "Cancelled."))
            else:
                self.log_queue.put(("log", f"ERROR: {e}"))
        finally:
            self.log_queue.put(("finished", None))

    # -------------------------------------------------------------- log UI
    def _log_clear(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _log_append(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _drain_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self._log_append(payload)
                elif kind == "progress":
                    pct, eta = payload
                    self.progress["value"] = pct
                    label = f"{pct:.2f}%"
                    if eta is not None:
                        label += f"  —  ETA {format_duration(eta)} remaining"
                    self.progress_label_var.set(label)
                elif kind == "finished":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    VideoMergerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
