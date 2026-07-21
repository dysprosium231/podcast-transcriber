"""
配置向导——图形界面填写播客订阅、翻译服务商/API Key、选择并下载 Whisper 转录模型。
生成/更新 config.json；API Key 通过 setx 写入当前用户的 Windows 环境变量（不存进 config.json）。

用 `pythonw setup_wizard.py` 运行不会弹黑框控制台；用 `python setup_wizard.py` 运行能在终端看到日志。

注意：setx 写的环境变量只对"之后新打开"的进程生效，当前正在运行的终端/程序需要重启才能读到。
"""
import os
import json
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tqdm import tqdm

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
RUN_HIDDEN_VBS_PATH = os.path.join(SCRIPT_DIR, "run_hidden.vbs")
TASK_NAME_DEFAULT = "DailyPodcast"

WHISPER_MODEL_CHOICES = [
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v2", "large-v3", "distil-large-v3",
]

TRANSLATION_PRESETS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "api_key_env": "DEEPSEEK_API_KEY"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key_env": "OPENAI_API_KEY"},
    "Moonshot (Kimi)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "api_key_env": "MOONSHOT_API_KEY"},
    "自定义": {"base_url": "", "model": "", "api_key_env": ""},
}


def load_existing_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _run_hidden(args):
    """跑一个命令行工具但不弹黑框窗口（schtasks本身没有GUI，用CREATE_NO_WINDOW避免闪一下）"""
    return subprocess.run(
        args, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def get_existing_task_time(task_name):
    """查询这个计划任务现在配置的每日触发时间，找不到就返回None（用于向导打开时回填）。
    schtasks的输出语言跟系统区域设置走，中文系统上是"开始时间:"而不是"Start Time:"，两种都认"""
    result = _run_hidden(["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Start Time:") or stripped.startswith("开始时间:"):
            return stripped.split(":", 1)[1].strip()
    return None


def create_or_update_daily_task(task_name, hh, mm):
    """创建/覆盖一个每天固定时间启动run_hidden.vbs的计划任务，不需要管理员权限（当前用户任务）"""
    tr_value = f'wscript.exe "{RUN_HIDDEN_VBS_PATH}"'
    return _run_hidden([
        "schtasks", "/create", "/tn", task_name, "/tr", tr_value,
        "/sc", "daily", "/st", f"{hh:02d}:{mm:02d}", "/f",
    ])


def run_task_now(task_name):
    return _run_hidden(["schtasks", "/run", "/tn", task_name])


def model_is_downloaded(size):
    d = os.path.join(MODELS_DIR, size)
    return os.path.isdir(d) and os.path.exists(os.path.join(d, "model.bin"))


class DownloadState:
    """下载线程和GUI主线程之间只通过这几个简单字段传递状态（不直接跨线程碰tk控件）"""

    def __init__(self):
        self.running = False
        self.progress = 0.0
        self.desc = ""
        self.done = False
        self.error = None


def start_model_download(size, state):
    state.running = True
    state.progress = 0.0
    state.desc = "准备下载..."
    state.done = False
    state.error = None

    def worker():
        try:
            from huggingface_hub import snapshot_download

            outer_state = state

            class ProgressTqdm(tqdm):
                def update(self, n=1):
                    super().update(n)
                    if self.total:
                        outer_state.progress = self.n / self.total
                    outer_state.desc = self.desc or "下载中"

            target_dir = os.path.join(MODELS_DIR, size)
            os.makedirs(target_dir, exist_ok=True)
            snapshot_download(
                repo_id=f"Systran/faster-whisper-{size}",
                local_dir=target_dir,
                tqdm_class=ProgressTqdm,
            )
            state.progress = 1.0
            state.done = True
        except Exception as e:
            state.error = str(e)
        finally:
            state.running = False

    threading.Thread(target=worker, daemon=True).start()


class SetupWizard:
    def __init__(self, root):
        self.root = root
        self.root.title("播客自动化 - 配置向导")
        self.root.geometry("640x900")
        self.root.resizable(False, False)

        existing = load_existing_config()
        self.download_state = DownloadState()

        self._build_feeds_section(existing.get("feeds", {}))
        self._build_translation_section(existing.get("translation", {}))
        self._build_model_section(existing.get("whisper_model_size", "large-v3"))
        self._build_schedule_section()
        self._build_bottom_buttons()

    # ---------------- 播客订阅 ----------------
    def _build_feeds_section(self, feeds):
        frame = ttk.LabelFrame(self.root, text="播客订阅（节目名 + RSS地址）")
        frame.pack(fill="x", padx=12, pady=(12, 6))

        self.feeds_tree = ttk.Treeview(frame, columns=("name", "url"), show="headings", height=5)
        self.feeds_tree.heading("name", text="节目名")
        self.feeds_tree.heading("url", text="RSS地址")
        self.feeds_tree.column("name", width=120)
        self.feeds_tree.column("url", width=460)
        self.feeds_tree.pack(fill="x", padx=8, pady=(8, 4))

        for name, url in feeds.items():
            self.feeds_tree.insert("", "end", values=(name, url))

        add_row = ttk.Frame(frame)
        add_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(add_row, text="节目名").pack(side="left")
        self.feed_name_entry = ttk.Entry(add_row, width=14)
        self.feed_name_entry.pack(side="left", padx=(4, 10))
        ttk.Label(add_row, text="RSS地址").pack(side="left")
        self.feed_url_entry = ttk.Entry(add_row, width=40)
        self.feed_url_entry.pack(side="left", padx=(4, 10))
        ttk.Button(add_row, text="添加", command=self._add_feed).pack(side="left", padx=2)
        ttk.Button(add_row, text="删除选中", command=self._remove_selected_feed).pack(side="left", padx=2)

    def _add_feed(self):
        name = self.feed_name_entry.get().strip()
        url = self.feed_url_entry.get().strip()
        if not name or not url:
            messagebox.showwarning("提示", "节目名和RSS地址都要填")
            return
        self.feeds_tree.insert("", "end", values=(name, url))
        self.feed_name_entry.delete(0, "end")
        self.feed_url_entry.delete(0, "end")

    def _remove_selected_feed(self):
        for item in self.feeds_tree.selection():
            self.feeds_tree.delete(item)

    # ---------------- 翻译服务 ----------------
    def _build_translation_section(self, translation):
        frame = ttk.LabelFrame(self.root, text="翻译服务（OpenAI兼容接口，换服务商不用改代码）")
        frame.pack(fill="x", padx=12, pady=6)

        preset_row = ttk.Frame(frame)
        preset_row.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(preset_row, text="预设服务商").pack(side="left")
        self.preset_var = tk.StringVar(value=translation.get("provider_name", "DeepSeek"))
        preset_combo = ttk.Combobox(
            preset_row, textvariable=self.preset_var,
            values=list(TRANSLATION_PRESETS.keys()), state="readonly", width=20,
        )
        preset_combo.pack(side="left", padx=(4, 0))
        preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)

        grid = ttk.Frame(frame)
        grid.pack(fill="x", padx=8, pady=(4, 8))

        self.base_url_var = tk.StringVar(value=translation.get("base_url", ""))
        self.model_var = tk.StringVar(value=translation.get("model", ""))
        self.api_key_env_var = tk.StringVar(value=translation.get("api_key_env", ""))
        self.api_key_var = tk.StringVar()

        ttk.Label(grid, text="Base URL").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.base_url_var, width=45).grid(row=0, column=1, sticky="w")

        ttk.Label(grid, text="模型名称").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.model_var, width=45).grid(row=1, column=1, sticky="w")

        ttk.Label(grid, text="环境变量名").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.api_key_env_var, width=45).grid(row=2, column=1, sticky="w")

        ttk.Label(grid, text="API Key").grid(row=3, column=0, sticky="w", pady=2)
        self.api_key_entry = ttk.Entry(grid, textvariable=self.api_key_var, width=45, show="*")
        self.api_key_entry.grid(row=3, column=1, sticky="w")
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            grid, text="显示", variable=self.show_key_var, command=self._toggle_key_visibility,
        ).grid(row=3, column=2, padx=(6, 0))

        ttk.Label(
            frame, foreground="#888",
            text="不填 API Key 就只更新配置，环境变量保持不变；填了才会覆盖写入。",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        extra_prompt = translation.get("extra_system_prompt", "")
        ttk.Label(frame, text="补充翻译提示（可选，比如专有名词纠错说明）").pack(anchor="w", padx=8)
        self.extra_prompt_text = tk.Text(frame, height=3, width=70)
        self.extra_prompt_text.insert("1.0", extra_prompt)
        self.extra_prompt_text.pack(padx=8, pady=(2, 8))

    def _on_preset_selected(self, _event=None):
        preset = TRANSLATION_PRESETS.get(self.preset_var.get())
        if not preset:
            return
        self.base_url_var.set(preset["base_url"])
        self.model_var.set(preset["model"])
        self.api_key_env_var.set(preset["api_key_env"])

    def _toggle_key_visibility(self):
        self.api_key_entry.config(show="" if self.show_key_var.get() else "*")

    # ---------------- Whisper模型 ----------------
    def _build_model_section(self, current_size):
        frame = ttk.LabelFrame(self.root, text="Whisper 转录模型")
        frame.pack(fill="x", padx=12, pady=6)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="模型大小").pack(side="left")
        self.model_size_var = tk.StringVar(value=current_size)
        size_combo = ttk.Combobox(
            row, textvariable=self.model_size_var,
            values=WHISPER_MODEL_CHOICES, state="readonly", width=16,
        )
        size_combo.pack(side="left", padx=(4, 10))
        size_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_model_status())

        self.model_status_label = ttk.Label(row, text="")
        self.model_status_label.pack(side="left", padx=(0, 10))

        self.download_btn = ttk.Button(row, text="下载此模型", command=self._start_download)
        self.download_btn.pack(side="left")

        self.download_progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.download_progress.pack(fill="x", padx=8, pady=(0, 4))
        self.download_status_label = ttk.Label(frame, text="", foreground="#888")
        self.download_status_label.pack(anchor="w", padx=8, pady=(0, 8))

        self._refresh_model_status()

    def _refresh_model_status(self):
        size = self.model_size_var.get()
        if model_is_downloaded(size):
            self.model_status_label.config(text="✅ 本地已有", foreground="#2a2")
            self.download_btn.config(state="disabled")
        else:
            self.model_status_label.config(text="⬇ 本地未下载", foreground="#a60")
            self.download_btn.config(state="normal")

    def _start_download(self):
        size = self.model_size_var.get()
        self.download_btn.config(state="disabled")
        self.download_status_label.config(text=f"正在下载 {size}（几百MB到几GB不等，取决于模型大小）...")
        start_model_download(size, self.download_state)
        self._poll_download()

    def _poll_download(self):
        state = self.download_state
        self.download_progress["value"] = state.progress * 100
        if state.error:
            self.download_status_label.config(text=f"下载失败：{state.error}", foreground="#c00")
            self.download_btn.config(state="normal")
            return
        if state.done:
            self.download_status_label.config(text="下载完成", foreground="#2a2")
            self._refresh_model_status()
            return
        if state.running:
            self.download_status_label.config(text=f"下载中... {state.desc}", foreground="#888")
            self.root.after(200, self._poll_download)

    # ---------------- 定时任务 ----------------
    def _build_schedule_section(self):
        frame = ttk.LabelFrame(self.root, text="每日自动运行（Windows 计划任务）")
        frame.pack(fill="x", padx=12, pady=6)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="任务名称").pack(side="left")
        self.task_name_var = tk.StringVar(value=TASK_NAME_DEFAULT)
        ttk.Entry(row, textvariable=self.task_name_var, width=16).pack(side="left", padx=(4, 16))

        ttk.Label(row, text="每天几点触发").pack(side="left")
        default_hh, default_mm = 10, 0
        existing_time = get_existing_task_time(TASK_NAME_DEFAULT)  # 比如 "10:00:00"，任务已存在时按现状回填
        if existing_time:
            try:
                parts = existing_time.split(":")
                default_hh, default_mm = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass
        self.hour_var = tk.StringVar(value=f"{default_hh:02d}")
        self.minute_var = tk.StringVar(value=f"{default_mm:02d}")
        ttk.Combobox(
            row, textvariable=self.hour_var, values=[f"{h:02d}" for h in range(24)],
            state="readonly", width=4,
        ).pack(side="left", padx=(4, 2))
        ttk.Label(row, text=":").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.minute_var, values=[f"{m:02d}" for m in range(0, 60, 5)],
            state="readonly", width=4,
        ).pack(side="left", padx=(2, 0))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(btn_row, text="创建/更新每日计划任务", command=self._create_schedule).pack(side="left")
        ttk.Button(btn_row, text="立即手动运行一次", command=self._run_schedule_now).pack(side="left", padx=(8, 0))

        self.schedule_status_label = ttk.Label(frame, text="", foreground="#888")
        self.schedule_status_label.pack(anchor="w", padx=8, pady=(2, 4))

        ttk.Label(
            frame, foreground="#888", justify="left", wraplength=600,
            text=(
                "点「创建/更新每日计划任务」会用上面填的时间自动建好计划任务，之后每天到点自动运行，不需要再打开这个向导。\n"
                "想手动触发（不等到点）：点上面「立即手动运行一次」，或者打开「任务计划程序」找到这个任务名右键「运行」，"
                "或者命令行执行 schtasks /run /tn \"" + TASK_NAME_DEFAULT + "\"。"
            ),
        ).pack(anchor="w", padx=8, pady=(0, 8))

    def _create_schedule(self):
        task_name = self.task_name_var.get().strip()
        if not task_name:
            messagebox.showerror("错误", "任务名称不能为空")
            return
        if not os.path.exists(RUN_HIDDEN_VBS_PATH):
            messagebox.showerror("错误", f"找不到 {RUN_HIDDEN_VBS_PATH}，请确认这个向导和 run_hidden.vbs 在同一个项目文件夹下")
            return
        hh, mm = int(self.hour_var.get()), int(self.minute_var.get())
        result = create_or_update_daily_task(task_name, hh, mm)
        if result.returncode == 0:
            self.schedule_status_label.config(
                text=f"已设置：每天 {hh:02d}:{mm:02d} 自动运行「{task_name}」", foreground="#2a2",
            )
        else:
            self.schedule_status_label.config(text=f"创建失败：{result.stderr.strip()}", foreground="#c00")

    def _run_schedule_now(self):
        task_name = self.task_name_var.get().strip()
        result = run_task_now(task_name)
        if result.returncode == 0:
            self.schedule_status_label.config(text=f"已触发「{task_name}」立即运行", foreground="#2a2")
        else:
            self.schedule_status_label.config(
                text=f"触发失败：{result.stderr.strip()}（可能是任务还没创建，先点上面的创建按钮）",
                foreground="#c00",
            )

    # ---------------- 保存 ----------------
    def _build_bottom_buttons(self):
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=12, pady=12)
        ttk.Button(row, text="保存配置", command=self._save).pack(side="right")
        ttk.Button(row, text="关闭", command=self.root.destroy).pack(side="right", padx=(0, 8))

    def _save(self):
        feeds = {}
        for item in self.feeds_tree.get_children():
            name, url = self.feeds_tree.item(item, "values")
            feeds[name] = url
        if not feeds:
            messagebox.showerror("错误", "至少要添加一个播客节目")
            return

        env_name = self.api_key_env_var.get().strip()
        if not env_name:
            messagebox.showerror("错误", "环境变量名不能为空")
            return

        config = {
            "feeds": feeds,
            "whisper_model_size": self.model_size_var.get(),
            "translation": {
                "provider_name": self.preset_var.get(),
                "base_url": self.base_url_var.get().strip(),
                "api_key_env": env_name,
                "model": self.model_var.get().strip(),
                "extra_system_prompt": self.extra_prompt_text.get("1.0", "end").strip(),
            },
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        api_key = self.api_key_var.get().strip()
        msg = "config.json 已保存。"
        if api_key:
            try:
                subprocess.run(["setx", env_name, api_key], check=True, capture_output=True)
                msg += f"\n\nAPI Key 已写入环境变量 {env_name}。\n注意：setx 只对之后新打开的终端/程序生效，当前正在运行中的程序需要重启才能读到新值。"
            except Exception as e:
                msg += f"\n\n但写入环境变量失败：{e}\n可以自己手动执行：setx {env_name} 你的key"
        messagebox.showinfo("完成", msg)


if __name__ == "__main__":
    root = tk.Tk()
    SetupWizard(root)
    root.mainloop()
