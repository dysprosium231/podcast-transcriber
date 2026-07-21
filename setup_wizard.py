"""
播客自动化管理界面——「播客库」浏览已生成的节目，「设置」管理播客订阅、翻译服务商/API Key、
Whisper 模型下载、每日计划任务。生成/更新 config.json；API Key 通过 setx 写入当前用户的 Windows
环境变量（不存进 config.json）。

用 `pythonw setup_wizard.py` 运行不会弹黑框控制台；用 `python setup_wizard.py` 运行能在终端看到日志。

注意：setx 写的环境变量只对"之后新打开"的进程生效，当前正在运行的终端/程序需要重启才能读到。
"""
import os
import sys
import json
import ctypes
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tqdm import tqdm

# 界面文字发糊是没做DPI感知，Windows会用位图整体拉伸缩放窗口来适配系统缩放比例；
# 必须在创建任何窗口前设置好，和 daily_podcast.py 的悬浮窗用的是同一个办法
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

if getattr(sys, "frozen", False):
    # 打包成exe之后，__file__指向的是运行时解压的临时目录，不是exe真正所在的位置；
    # sys.executable才是exe自己的路径，要用它来定位同目录下的config.json/episodes/等
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
RUN_HIDDEN_VBS_PATH = os.path.join(SCRIPT_DIR, "run_hidden.vbs")
TASK_NAME_DEFAULT = "DailyPodcast"
EPISODES_DIR = os.path.join(SCRIPT_DIR, "episodes")

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

# 浅色现代风格：干净的白底 + 蓝色强调色，语义色（成功/警告/失败）统一在一处管理，
# 不在各个控件里散落写十六进制颜色
COLORS = {
    "bg": "#ffffff",
    "bg_elevated": "#f1f3f7",
    "field_bg": "#ffffff",
    "border": "#dde1e8",
    "fg": "#1f2430",
    "fg_muted": "#6b7280",
    "accent": "#3a5cf5",
    "accent_hover": "#5470f7",
    "success": "#15803d",
    "warning": "#b45309",
    "danger": "#dc2626",
}


def apply_modern_style(root):
    """浅色现代风格；ttk默认主题(vista/winnative)在Windows上基本不认颜色覆盖，
    必须先切到clam主题才能自定义配色。同时在这里把DPI缩放设对，避免文字发糊"""
    dpi = root.winfo_fpixels("1i")
    root.tk.call("tk", "scaling", dpi / 72.0)

    root.configure(bg=COLORS["bg"])

    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=COLORS["bg"], foreground=COLORS["fg"], font=("Segoe UI", 9))
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["fg"])
    style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["fg_muted"])
    style.configure("Success.TLabel", background=COLORS["bg"], foreground=COLORS["success"])
    style.configure("Danger.TLabel", background=COLORS["bg"], foreground=COLORS["danger"])
    style.configure("Warning.TLabel", background=COLORS["bg"], foreground=COLORS["warning"])
    style.configure("Heading.TLabel", background=COLORS["bg"], foreground=COLORS["fg"], font=("Segoe UI", 10, "bold"))

    style.configure(
        "TLabelframe", background=COLORS["bg"], bordercolor=COLORS["border"],
        relief="solid", borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label", background=COLORS["bg"], foreground=COLORS["fg"],
        font=("Segoe UI", 10, "bold"),
    )

    style.configure(
        "TButton", background=COLORS["field_bg"], foreground=COLORS["fg"],
        borderwidth=0, focuscolor=COLORS["bg"], padding=(10, 6),
    )
    style.map(
        "TButton",
        background=[("active", COLORS["border"]), ("disabled", COLORS["bg_elevated"])],
        foreground=[("disabled", COLORS["fg_muted"])],
    )

    style.configure(
        "Accent.TButton", background=COLORS["accent"], foreground="#ffffff",
        borderwidth=0, focuscolor=COLORS["bg"], padding=(12, 7), font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["border"])],
        foreground=[("disabled", COLORS["fg_muted"])],
    )

    style.configure(
        "TEntry", fieldbackground=COLORS["field_bg"], foreground=COLORS["fg"],
        insertcolor=COLORS["fg"], bordercolor=COLORS["border"], borderwidth=1,
        lightcolor=COLORS["border"], darkcolor=COLORS["border"],
    )
    style.configure(
        "TCombobox", fieldbackground=COLORS["field_bg"], background=COLORS["field_bg"],
        foreground=COLORS["fg"], arrowcolor=COLORS["fg"], bordercolor=COLORS["border"],
        lightcolor=COLORS["border"], darkcolor=COLORS["border"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", COLORS["field_bg"])],
        foreground=[("readonly", COLORS["fg"])],
    )
    root.option_add("*TCombobox*Listbox.background", COLORS["field_bg"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])

    style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["fg"])
    style.map("TCheckbutton", background=[("active", COLORS["bg"])])

    style.configure(
        "Treeview", background=COLORS["field_bg"], fieldbackground=COLORS["field_bg"],
        foreground=COLORS["fg"], borderwidth=0, rowheight=26,
    )
    style.configure(
        "Treeview.Heading", background=COLORS["bg_elevated"], foreground=COLORS["fg"],
        borderwidth=0, relief="flat", font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "Treeview",
        background=[("selected", COLORS["accent"])],
        foreground=[("selected", "#ffffff")],
    )

    style.configure(
        "TProgressbar", background=COLORS["accent"], troughcolor=COLORS["field_bg"],
        bordercolor=COLORS["border"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"],
    )

    # 页签本身应该是最显眼的导航元素：选中的页签用强调色文字+白底突出（跟下面内容区连成一片），
    # 未选中的页签用浅灰底+灰字往后退，视觉主次要清楚，不能让没选中的页签看起来更抢眼
    style.configure("TNotebook", background=COLORS["bg_elevated"], bordercolor=COLORS["border"], borderwidth=0)
    style.configure(
        "TNotebook.Tab", background=COLORS["bg_elevated"], foreground=COLORS["fg_muted"],
        padding=(22, 11), font=("Segoe UI", 10), borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", COLORS["bg"])],
        foreground=[("selected", COLORS["accent"])],
        font=[("selected", ("Segoe UI", 10, "bold"))],
    )


def style_text_widget(widget):
    """tk.Text不是ttk控件，不吃ttk.Style，得手动设颜色"""
    widget.configure(
        bg=COLORS["field_bg"], fg=COLORS["fg"], insertbackground=COLORS["fg"],
        relief="flat", borderwidth=1,
        highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"], highlightthickness=1,
    )


def make_checkmark_toggle(parent, text, variable, command=None):
    """clam主题下ttk.Checkbutton画的是纯色方块，不是真正的对勾；用☑/☐字符自己实现一个
    看起来像"打勾"的开关，点文字或者方块都能切换"""
    frame = ttk.Frame(parent)

    def render():
        checked = variable.get()
        box.config(
            text="☑" if checked else "☐",
            fg=COLORS["accent"] if checked else COLORS["fg_muted"],
        )

    def on_click(_event=None):
        variable.set(not variable.get())
        render()
        if command:
            command()

    box = tk.Label(frame, font=("Segoe UI", 12), bg=COLORS["bg"], cursor="hand2")
    box.pack(side="left")
    label = tk.Label(frame, text=text, bg=COLORS["bg"], fg=COLORS["fg"], font=("Segoe UI", 9), cursor="hand2")
    label.pack(side="left", padx=(4, 0))

    box.bind("<Button-1>", on_click)
    label.bind("<Button-1>", on_click)
    render()
    return frame


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


def _query_task_detail(task_name):
    """跑一次schtasks查询，返回原始stdout（查不到返回None）。时间/启用状态都从这一份输出里解析，
    避免多查几次schtasks"""
    result = _run_hidden(["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"])
    if result.returncode != 0:
        return None
    return result.stdout


def _find_field(output, *labels):
    for line in output.splitlines():
        stripped = line.strip()
        for label in labels:
            if stripped.startswith(label):
                return stripped.split(":", 1)[1].strip()
    return None


def get_existing_task_time(task_name):
    """查询这个计划任务现在配置的每日触发时间，找不到就返回None（用于回填）。
    schtasks的输出语言跟系统区域设置走，中文系统上是"开始时间:"而不是"Start Time:"，两种都认"""
    output = _query_task_detail(task_name)
    if output is None:
        return None
    return _find_field(output, "Start Time:", "开始时间:")


def get_existing_task_enabled(task_name):
    """查询这个计划任务当前是启用还是禁用，任务不存在返回None（用于回填开关状态）"""
    output = _query_task_detail(task_name)
    if output is None:
        return None
    state = _find_field(output, "Scheduled Task State:", "计划任务状态:")
    if state is None:
        return None
    return state in ("Enabled", "已启用")


def create_or_update_daily_task(task_name, hh, mm):
    """创建/覆盖一个每天固定时间启动run_hidden.vbs的计划任务，不需要管理员权限（当前用户任务）"""
    tr_value = f'wscript.exe "{RUN_HIDDEN_VBS_PATH}"'
    return _run_hidden([
        "schtasks", "/create", "/tn", task_name, "/tr", tr_value,
        "/sc", "daily", "/st", f"{hh:02d}:{mm:02d}", "/f",
    ])


def disable_daily_task(task_name):
    """禁用（不删除）已有的计划任务——保留任务定义，只是不再自动触发，随时可以重新启用"""
    return _run_hidden(["schtasks", "/change", "/tn", task_name, "/disable"])


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
    """「设置」页内容——可以直接当独立窗口用，也可以塞进别的容器（比如Notebook的一个tab）里，
    传进来的parent只要是个能当tk控件父容器的东西（Tk根窗口或者Frame都行）就可以。

    界面显示的内容永远是 config.json 实际内容的镜像：构造时读一次，「重新加载」按钮可以随时
    强制重新读取覆盖界面上的编辑（比如外部改过文件，或者想放弃本次没保存的修改），保存成功后
    也会重新读一次文件回填——不是假设写成功了就直接照抄内存里的值，而是真的按落盘后的文件为准。
    """

    def __init__(self, parent):
        self.parent = parent
        self.download_state = DownloadState()

        self._build_feeds_section()
        self._build_translation_section()
        self._build_model_section()
        self._build_schedule_section()
        self._build_bottom_buttons()

        self.reload_from_config()

    # ---------------- 加载 / 同步 ----------------
    def reload_from_config(self):
        """以 config.json 为准，重新读取并回填所有控件；计划任务时间也顺带从系统当前状态重新查一遍"""
        config = load_existing_config()
        self._populate_feeds(config.get("feeds", {}))
        self._populate_translation(config.get("translation", {}))
        self.model_size_var.set(config.get("whisper_model_size", "large-v3"))
        self._refresh_model_status()
        self._refresh_schedule_from_task()

    def _populate_feeds(self, feeds):
        self.feeds_tree.delete(*self.feeds_tree.get_children())
        for name, url in feeds.items():
            self.feeds_tree.insert("", "end", values=(name, url))

    def _populate_translation(self, translation):
        self.preset_var.set(translation.get("provider_name", "DeepSeek"))
        self.base_url_var.set(translation.get("base_url", ""))
        self.model_var.set(translation.get("model", ""))
        self.api_key_env_var.set(translation.get("api_key_env", ""))
        self.api_key_var.set("")  # 密钥本来就不存在config.json里，重新加载时清空输入框而不是显示假值
        self.extra_prompt_text.delete("1.0", "end")
        self.extra_prompt_text.insert("1.0", translation.get("extra_system_prompt", ""))

    # ---------------- 播客订阅 ----------------
    def _build_feeds_section(self):
        frame = ttk.LabelFrame(self.parent, text="播客订阅（节目名 + RSS地址）")
        frame.pack(fill="x", padx=14, pady=(14, 7))

        self.feeds_tree = ttk.Treeview(frame, columns=("name", "url"), show="headings", height=5)
        self.feeds_tree.heading("name", text="节目名")
        self.feeds_tree.heading("url", text="RSS地址")
        self.feeds_tree.column("name", width=120)
        self.feeds_tree.column("url", width=460)
        self.feeds_tree.pack(fill="x", padx=10, pady=(10, 6))

        add_row = ttk.Frame(frame)
        add_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(add_row, text="节目名").pack(side="left")
        self.feed_name_entry = ttk.Entry(add_row, width=14)
        self.feed_name_entry.pack(side="left", padx=(4, 10))
        ttk.Label(add_row, text="RSS地址").pack(side="left")
        self.feed_url_entry = ttk.Entry(add_row, width=38)
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
    def _build_translation_section(self):
        frame = ttk.LabelFrame(self.parent, text="翻译服务（OpenAI兼容接口，换服务商不用改代码）")
        frame.pack(fill="x", padx=14, pady=7)

        preset_row = ttk.Frame(frame)
        preset_row.pack(fill="x", padx=10, pady=(10, 5))
        ttk.Label(preset_row, text="预设服务商").pack(side="left")
        self.preset_var = tk.StringVar(value="DeepSeek")
        preset_combo = ttk.Combobox(
            preset_row, textvariable=self.preset_var,
            values=list(TRANSLATION_PRESETS.keys()), width=20,
        )
        preset_combo.pack(side="left", padx=(4, 0))
        preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        ttk.Label(
            preset_row, style="Muted.TLabel", text="（也可以直接输入自定义服务商名称）",
        ).pack(side="left", padx=(6, 0))

        grid = ttk.Frame(frame)
        grid.pack(fill="x", padx=10, pady=(4, 8))

        self.base_url_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.api_key_env_var = tk.StringVar()
        self.api_key_var = tk.StringVar()

        ttk.Label(grid, text="Base URL").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.base_url_var, width=45).grid(row=0, column=1, sticky="w")

        ttk.Label(grid, text="模型名称").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.model_var, width=45).grid(row=1, column=1, sticky="w")

        ttk.Label(grid, text="环境变量名").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.api_key_env_var, width=45).grid(row=2, column=1, sticky="w")

        ttk.Label(grid, text="API Key").grid(row=3, column=0, sticky="w", pady=3)
        self.api_key_entry = ttk.Entry(grid, textvariable=self.api_key_var, width=45, show="*")
        self.api_key_entry.grid(row=3, column=1, sticky="w")
        self.show_key_var = tk.BooleanVar(value=False)
        make_checkmark_toggle(
            grid, "显示", self.show_key_var, command=self._toggle_key_visibility,
        ).grid(row=3, column=2, padx=(6, 0))

        ttk.Label(
            frame, style="Muted.TLabel",
            text="不填 API Key 就只更新配置，环境变量保持不变；填了才会覆盖写入。",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        ttk.Label(frame, text="补充翻译提示（可选，比如专有名词纠错说明）").pack(anchor="w", padx=10)
        self.extra_prompt_text = tk.Text(frame, height=3, width=70)
        style_text_widget(self.extra_prompt_text)
        self.extra_prompt_text.pack(padx=10, pady=(4, 10))

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
    def _build_model_section(self):
        frame = ttk.LabelFrame(self.parent, text="Whisper 转录模型")
        frame.pack(fill="x", padx=14, pady=7)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=10)
        ttk.Label(row, text="模型大小").pack(side="left")
        self.model_size_var = tk.StringVar(value="large-v3")
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
        self.download_progress.pack(fill="x", padx=10, pady=(0, 5))
        self.download_status_label = ttk.Label(frame, text="", style="Muted.TLabel")
        self.download_status_label.pack(anchor="w", padx=10, pady=(0, 10))

    def _refresh_model_status(self):
        size = self.model_size_var.get()
        if model_is_downloaded(size):
            self.model_status_label.config(text="✅ 本地已有", style="Success.TLabel")
            self.download_btn.config(state="disabled")
        else:
            self.model_status_label.config(text="⬇ 本地未下载", style="Warning.TLabel")
            self.download_btn.config(state="normal")

    def _start_download(self):
        size = self.model_size_var.get()
        self.download_btn.config(state="disabled")
        self.download_status_label.config(
            text=f"正在下载 {size}（几百MB到几GB不等，取决于模型大小）...", style="Muted.TLabel",
        )
        start_model_download(size, self.download_state)
        self._poll_download()

    def _poll_download(self):
        state = self.download_state
        self.download_progress["value"] = state.progress * 100
        if state.error:
            self.download_status_label.config(text=f"下载失败：{state.error}", style="Danger.TLabel")
            self.download_btn.config(state="normal")
            return
        if state.done:
            self.download_status_label.config(text="下载完成", style="Success.TLabel")
            self._refresh_model_status()
            return
        if state.running:
            self.download_status_label.config(text=f"下载中... {state.desc}", style="Muted.TLabel")
            self.parent.after(200, self._poll_download)

    # ---------------- 定时任务 ----------------
    def _build_schedule_section(self):
        frame = ttk.LabelFrame(self.parent, text="每日自动运行（Windows 计划任务）")
        frame.pack(fill="x", padx=14, pady=7)

        enable_row = ttk.Frame(frame)
        enable_row.pack(fill="x", padx=10, pady=(10, 4))
        self.schedule_enabled_var = tk.BooleanVar(value=True)
        make_checkmark_toggle(
            enable_row, "启用每日自动运行", self.schedule_enabled_var,
            command=self._on_schedule_enabled_toggle,
        ).pack(side="left")

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(row, text="任务名称").pack(side="left")
        self.task_name_var = tk.StringVar(value=TASK_NAME_DEFAULT)
        ttk.Entry(row, textvariable=self.task_name_var, width=16).pack(side="left", padx=(4, 16))

        ttk.Label(row, text="每天几点触发").pack(side="left")
        self.hour_var = tk.StringVar(value="10")
        self.minute_var = tk.StringVar(value="00")
        self.hour_combo = ttk.Combobox(
            row, textvariable=self.hour_var, values=[f"{h:02d}" for h in range(24)],
            state="readonly", width=4,
        )
        self.hour_combo.pack(side="left", padx=(4, 2))
        ttk.Label(row, text=":").pack(side="left")
        self.minute_combo = ttk.Combobox(
            row, textvariable=self.minute_var, values=[f"{m:02d}" for m in range(0, 60, 5)],
            state="readonly", width=4,
        )
        self.minute_combo.pack(side="left", padx=(2, 0))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", padx=10, pady=(0, 5))
        ttk.Button(btn_row, text="应用定时任务设置", style="Accent.TButton", command=self._apply_schedule).pack(side="left")
        ttk.Button(btn_row, text="立即手动运行一次", command=self._run_schedule_now).pack(side="left", padx=(8, 0))

        self.schedule_status_label = ttk.Label(frame, text="", style="Muted.TLabel")
        self.schedule_status_label.pack(anchor="w", padx=10, pady=(2, 5))

        ttk.Label(
            frame, style="Muted.TLabel", justify="left", wraplength=620,
            text=(
                "勾选「启用每日自动运行」+ 填好时间 + 点「应用定时任务设置」会自动建好计划任务，之后每天到点自动运行。\n"
                "不想让它每天自动跑：取消勾选再点「应用」即可禁用（不会删除任务，随时可以重新勾选启用）。\n"
                "想立即手动跑一次（不受这个开关影响）：点「立即手动运行一次」，或者打开「任务计划程序」右键「运行」，"
                "或者命令行执行 schtasks /run /tn \"" + TASK_NAME_DEFAULT + "\"。"
            ),
        ).pack(anchor="w", padx=10, pady=(0, 10))

        self._on_schedule_enabled_toggle()

    def _on_schedule_enabled_toggle(self):
        state = "readonly" if self.schedule_enabled_var.get() else "disabled"
        self.hour_combo.config(state=state)
        self.minute_combo.config(state=state)

    def _refresh_schedule_from_task(self):
        """从系统当前的计划任务状态回填时间和启用开关（任务不存在就保留默认值不动）"""
        task_name = self.task_name_var.get().strip() or TASK_NAME_DEFAULT
        existing_time = get_existing_task_time(task_name)  # 比如 "10:00:00"
        if existing_time:
            try:
                parts = existing_time.split(":")
                self.hour_var.set(f"{int(parts[0]):02d}")
                self.minute_var.set(f"{int(parts[1]):02d}")
            except (ValueError, IndexError):
                pass
        enabled = get_existing_task_enabled(task_name)
        if enabled is not None:
            self.schedule_enabled_var.set(enabled)
        self._on_schedule_enabled_toggle()

    def _apply_schedule(self):
        task_name = self.task_name_var.get().strip()
        if not task_name:
            messagebox.showerror("错误", "任务名称不能为空")
            return

        if self.schedule_enabled_var.get():
            if not os.path.exists(RUN_HIDDEN_VBS_PATH):
                messagebox.showerror("错误", f"找不到 {RUN_HIDDEN_VBS_PATH}，请确认这个程序和 run_hidden.vbs 在同一个项目文件夹下")
                return
            hh, mm = int(self.hour_var.get()), int(self.minute_var.get())
            result = create_or_update_daily_task(task_name, hh, mm)
            if result.returncode == 0:
                self.schedule_status_label.config(
                    text=f"已设置：每天 {hh:02d}:{mm:02d} 自动运行「{task_name}」", style="Success.TLabel",
                )
            else:
                self.schedule_status_label.config(text=f"设置失败：{result.stderr.strip()}", style="Danger.TLabel")
        else:
            if get_existing_task_time(task_name) is None:
                self.schedule_status_label.config(text="还没有创建过这个任务，不需要禁用", style="Muted.TLabel")
                return
            result = disable_daily_task(task_name)
            if result.returncode == 0:
                self.schedule_status_label.config(text=f"已禁用「{task_name}」，不会再每日自动运行", style="Muted.TLabel")
            else:
                self.schedule_status_label.config(text=f"禁用失败：{result.stderr.strip()}", style="Danger.TLabel")

    def _run_schedule_now(self):
        task_name = self.task_name_var.get().strip()
        result = run_task_now(task_name)
        if result.returncode == 0:
            self.schedule_status_label.config(text=f"已触发「{task_name}」立即运行", style="Success.TLabel")
        else:
            self.schedule_status_label.config(
                text=f"触发失败：{result.stderr.strip()}（可能是任务还没创建，先点上面的应用按钮）",
                style="Danger.TLabel",
            )

    # ---------------- 保存 ----------------
    def _build_bottom_buttons(self):
        row = ttk.Frame(self.parent)
        row.pack(fill="x", padx=14, pady=14)
        ttk.Button(row, text="保存配置", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(row, text="重新加载", command=self._reload_clicked).pack(side="right", padx=(0, 8))
        self.save_status_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.save_status_label.pack(side="left")

    def _reload_clicked(self):
        self.reload_from_config()
        self.save_status_label.config(text="已按 config.json 当前内容重新加载", style="Muted.TLabel")

    def _gather_config(self):
        feeds = {}
        for item in self.feeds_tree.get_children():
            name, url = self.feeds_tree.item(item, "values")
            feeds[name] = url
        return {
            "feeds": feeds,
            "whisper_model_size": self.model_size_var.get(),
            "translation": {
                "provider_name": self.preset_var.get(),
                "base_url": self.base_url_var.get().strip(),
                "api_key_env": self.api_key_env_var.get().strip(),
                "model": self.model_var.get().strip(),
                "extra_system_prompt": self.extra_prompt_text.get("1.0", "end").strip(),
            },
        }

    def _save(self):
        config = self._gather_config()
        if not config["feeds"]:
            messagebox.showerror("错误", "至少要添加一个播客节目")
            return
        env_name = config["translation"]["api_key_env"]
        if not env_name:
            messagebox.showerror("错误", "环境变量名不能为空")
            return

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

        # 不直接假设内存里的值=文件里的值，保存完再真的读一遍文件回填，界面显示的永远是落盘后的真实内容
        self.reload_from_config()
        self.save_status_label.config(text="已保存并按文件内容刷新界面", style="Success.TLabel")
        messagebox.showinfo("完成", msg)


def scan_episodes():
    """扫描 episodes/ 目录，返回 [(节目名, 期数标题, 文件夹绝对路径), ...]"""
    result = []
    if not os.path.isdir(EPISODES_DIR):
        return result
    for show_name in sorted(os.listdir(EPISODES_DIR)):
        show_path = os.path.join(EPISODES_DIR, show_name)
        if not os.path.isdir(show_path):
            continue
        for ep_title in sorted(os.listdir(show_path)):
            ep_path = os.path.join(show_path, ep_title)
            if os.path.isdir(ep_path):
                result.append((show_name, ep_title, ep_path))
    return result


class HomeTab:
    """播客库主页——浏览 episodes/ 下已经生成的节目，双击/点按钮直接打开字幕页、音频或所在文件夹，
    不用自己去文件资源管理器里一层层找"""

    def __init__(self, parent):
        self.parent = parent

        top = ttk.Frame(parent)
        top.pack(fill="x", padx=14, pady=(14, 7))
        ttk.Label(top, text="episodes/ 下已生成的节目", style="Heading.TLabel").pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="right")

        self.tree = ttk.Treeview(parent, columns=("info",), show="tree headings", height=22)
        self.tree.heading("#0", text="节目 / 期数")
        self.tree.heading("info", text="")
        self.tree.column("#0", width=440)
        self.tree.column("info", width=140)
        self.tree.pack(fill="both", expand=True, padx=14, pady=(0, 7))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._open_subtitles())

        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", padx=14, pady=(0, 7))
        self.open_subtitles_btn = ttk.Button(
            btn_row, text="打开字幕页", style="Accent.TButton", command=self._open_subtitles, state="disabled",
        )
        self.open_subtitles_btn.pack(side="left")
        self.open_audio_btn = ttk.Button(btn_row, text="播放音频", command=self._open_audio, state="disabled")
        self.open_audio_btn.pack(side="left", padx=(8, 0))
        self.open_folder_btn = ttk.Button(btn_row, text="打开所在文件夹", command=self._open_folder, state="disabled")
        self.open_folder_btn.pack(side="left", padx=(8, 0))

        self.status_label = ttk.Label(parent, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w", padx=14, pady=(0, 14))

        self._node_paths = {}  # tree节点id -> 期数文件夹绝对路径（只有期数这一级节点才有）
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self._node_paths.clear()
        episodes = scan_episodes()

        shows = {}
        for show_name, ep_title, ep_path in episodes:
            shows.setdefault(show_name, []).append((ep_title, ep_path))

        for show_name, eps in shows.items():
            show_node = self.tree.insert("", "end", text=show_name, values=(f"{len(eps)} 期",), open=True)
            for ep_title, ep_path in eps:
                node = self.tree.insert(show_node, "end", text=ep_title, values=("",))
                self._node_paths[node] = ep_path

        if not episodes:
            self.status_label.config(text="还没有生成任何一期——跑一次 daily_podcast.py 之后回来刷新看看")
        else:
            self.status_label.config(text=f"共 {len(shows)} 个节目、{len(episodes)} 期")

        self._update_buttons_state()

    def _selected_episode_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self._node_paths.get(sel[0])

    def _on_select(self, _event=None):
        self._update_buttons_state()

    def _update_buttons_state(self):
        path = self._selected_episode_path()
        has_subtitles = bool(path) and os.path.exists(os.path.join(path, "subtitles.html"))
        has_audio = bool(path) and os.path.exists(os.path.join(path, "audio.mp3"))
        self.open_subtitles_btn.config(state="normal" if has_subtitles else "disabled")
        self.open_audio_btn.config(state="normal" if has_audio else "disabled")
        self.open_folder_btn.config(state="normal" if path else "disabled")

    def _open_subtitles(self):
        path = self._selected_episode_path()
        if not path:
            return
        target = os.path.join(path, "subtitles.html")
        if os.path.exists(target):
            os.startfile(target)

    def _open_audio(self):
        path = self._selected_episode_path()
        if not path:
            return
        target = os.path.join(path, "audio.mp3")
        if os.path.exists(target):
            os.startfile(target)

    def _open_folder(self):
        path = self._selected_episode_path()
        if path:
            os.startfile(path)


AUDIO_FILE_TYPES = [
    ("音频文件", "*.mp3 *.m4a *.wav *.flac *.aac *.ogg *.wma"),
    ("所有文件", "*.*"),
]

# 手动处理跟每天自动跑的daily_podcast.py共用同一套转录/翻译逻辑（不重新写一遍，靠导入复用），
# 模型在同一次程序运行里只加载一次，重复处理多个音频不用每次都等GPU模型重新加载
_manual_model = None
_manual_batched_model = None


class ManualState:
    """手动处理线程和GUI主线程之间只通过这几个简单字段传递状态（不直接跨线程碰tk控件）"""

    def __init__(self):
        self.running = False
        self.stage = ""
        self.progress = 0.0
        self.done = False
        self.error = None
        self.result_dir = None


def start_manual_processing(audio_src_path, show_name, episode_title, state):
    state.running = True
    state.progress = 0.0
    state.stage = "准备中..."
    state.done = False
    state.error = None
    state.result_dir = None

    def on_transcribe_progress(p):
        state.progress = p * 0.6
        state.stage = f"转录中 {int(p * 100)}%"

    def on_translate_progress(p):
        state.progress = 0.6 + p * 0.4
        state.stage = f"翻译中 {int(p * 100)}%"

    def worker():
        global _manual_model, _manual_batched_model
        try:
            import daily_podcast as dp
        except FileNotFoundError:
            state.error = "找不到 config.json，请先在「设置」页保存一次配置"
            state.running = False
            return
        except Exception as e:
            state.error = f"加载主程序模块失败：{e}"
            state.running = False
            return

        try:
            show_dir = os.path.join(EPISODES_DIR, show_name)
            os.makedirs(show_dir, exist_ok=True)
            safe_title = dp.sanitize_filename(episode_title)
            episode_dir = os.path.join(show_dir, safe_title)
            os.makedirs(episode_dir, exist_ok=True)

            ext = os.path.splitext(audio_src_path)[1] or ".mp3"
            audio_filename = f"audio{ext}"
            audio_dest = os.path.join(episode_dir, audio_filename)

            state.stage = "复制音频文件..."
            shutil.copyfile(audio_src_path, audio_dest)

            if _manual_model is None:
                state.stage = "正在加载GPU模型（首次可能需要1-2分钟）..."
                try:
                    _manual_model = dp.WhisperModel(dp.MODEL_PATH, device="cuda", compute_type="float16")
                    _manual_batched_model = dp.BatchedInferencePipeline(model=_manual_model)
                except Exception as e:
                    if getattr(sys, "frozen", False) and ("dll" in str(e).lower() or "cublas" in str(e).lower() or "cudnn" in str(e).lower()):
                        # 打包成exe之后不会带CUDA运行库（cublas/cudnn加起来有1.7GB，打包不现实），
                        # 手动处理这个功能在独立exe下需要电脑上本来就有能被找到的CUDA运行环境；
                        # 用 python setup_wizard.py 在项目自带的conda环境里跑就没有这个限制
                        raise RuntimeError(
                            "GPU模型加载失败，缺少CUDA运行库（cublas/cudnn等）。"
                            "这是独立exe的已知限制——这些库总共1.7GB左右，没有打包进exe。"
                            "「手动处理」这个功能建议用 `python setup_wizard.py` 在装好conda环境的电脑上运行，"
                            f"不受这个限制。\n\n原始错误：{e}"
                        ) from e
                    raise

            state.stage = "转录中（GPU运算）..."
            segments = dp.transcribe_audio(_manual_batched_model, audio_dest, on_progress=on_transcribe_progress)

            state.stage = "翻译中..."
            segments = dp.translate_segments(segments, on_progress=on_translate_progress)

            json_path = os.path.join(episode_dir, "data.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
            dp.save_text_files(
                segments,
                os.path.join(episode_dir, "transcript_en.txt"),
                os.path.join(episode_dir, "transcript_zh.txt"),
            )
            dp.generate_html(episode_title, audio_filename, segments, os.path.join(episode_dir, "subtitles.html"))

            state.progress = 1.0
            state.done = True
            state.result_dir = episode_dir
        except Exception as e:
            state.error = str(e)
        finally:
            state.running = False

    threading.Thread(target=worker, daemon=True).start()


class ManualTab:
    """手动处理——不是从RSS订阅来的音频（漏抓的某一集、别的录音）也能走同一套转录+翻译流程，
    产出格式和自动流程完全一样，处理完在「播客库」页签就能看到"""

    def __init__(self, parent):
        self.parent = parent
        self.state = ManualState()
        self.audio_path = None

        frame = ttk.LabelFrame(parent, text="手动添加音频并转录翻译")
        frame.pack(fill="x", padx=14, pady=14)

        file_row = ttk.Frame(frame)
        file_row.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Button(file_row, text="选择音频文件...", command=self._pick_file).pack(side="left")
        self.file_label = ttk.Label(file_row, text="未选择文件", style="Muted.TLabel")
        self.file_label.pack(side="left", padx=(10, 0))

        form = ttk.Frame(frame)
        form.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(form, text="节目名").grid(row=0, column=0, sticky="w", pady=3)
        self.show_var = tk.StringVar()
        self.show_combo = ttk.Combobox(form, textvariable=self.show_var, width=20)
        self.show_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(form, text="期数标题").grid(row=1, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.title_var, width=45).grid(row=1, column=1, sticky="w", padx=(6, 0))

        self.start_btn = ttk.Button(frame, text="开始转录+翻译", style="Accent.TButton", command=self._start)
        self.start_btn.pack(anchor="w", padx=10, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 5))
        self.status_label = ttk.Label(frame, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w", padx=10, pady=(0, 6))

        self.open_result_btn = ttk.Button(frame, text="打开字幕页", command=self._open_result, state="disabled")
        self.open_result_btn.pack(anchor="w", padx=10, pady=(0, 6))

        ttk.Label(
            frame, style="Muted.TLabel", wraplength=620, justify="left",
            text=(
                "用来处理不是从RSS订阅来的音频——比如某一集播客没被自动抓到，或者你有一段别的录音想转录翻译。"
                "选好音频文件、填节目名和标题，点「开始转录+翻译」就行，产出格式和自动流程完全一样，会出现在「播客库」里。"
                "节目名如果跟已有的一样，会归到同一个节目下面；不一样就会新建一个。\n"
                "提示：如果是用打包好的独立exe运行，这个功能需要电脑上有CUDA运行环境才能跑GPU转录；"
                "更省心的做法是用 python setup_wizard.py 在项目自带的conda环境里运行。"
            ),
        ).pack(anchor="w", padx=10, pady=(0, 10))

        self.refresh_show_choices()

    def refresh_show_choices(self):
        config = load_existing_config()
        self.show_combo["values"] = list(config.get("feeds", {}).keys())

    def _pick_file(self):
        path = filedialog.askopenfilename(title="选择音频文件", filetypes=AUDIO_FILE_TYPES)
        if not path:
            return
        self.audio_path = path
        self.file_label.config(text=os.path.basename(path), style="TLabel")

    def _start(self):
        if not self.audio_path:
            messagebox.showerror("错误", "请先选择音频文件")
            return
        show_name = self.show_var.get().strip()
        title = self.title_var.get().strip()
        if not show_name or not title:
            messagebox.showerror("错误", "节目名和期数标题都要填")
            return

        self.start_btn.config(state="disabled")
        self.open_result_btn.config(state="disabled")
        self.status_label.config(text="开始处理...", style="Muted.TLabel")
        self.progress_bar["value"] = 0
        start_manual_processing(self.audio_path, show_name, title, self.state)
        self._poll()

    def _poll(self):
        state = self.state
        self.progress_bar["value"] = state.progress * 100
        if state.error:
            self.status_label.config(text=f"处理失败：{state.error}", style="Danger.TLabel")
            self.start_btn.config(state="normal")
            return
        if state.done:
            self.status_label.config(text="处理完成！", style="Success.TLabel")
            self.start_btn.config(state="normal")
            self.open_result_btn.config(state="normal")
            self.refresh_show_choices()
            return
        if state.running:
            self.status_label.config(text=state.stage, style="Muted.TLabel")
            self.parent.after(200, self._poll)

    def _open_result(self):
        if self.state.result_dir:
            target = os.path.join(self.state.result_dir, "subtitles.html")
            if os.path.exists(target):
                os.startfile(target)


class App:
    """顶层窗口：「播客库」主页 + 「手动处理」+ 「设置」，用Notebook切换。切到某个页签时
    自动刷新一次那个页签的内容（比如设置页按config.json当前内容重新同步），避免显示的是旧值"""

    def __init__(self, root):
        self.root = root
        apply_modern_style(root)  # 顺带设置好了DPI缩放
        root.title("播客自动化")
        scale = root.winfo_fpixels("1i") / 96.0
        root.geometry(f"{int(760 * scale)}x{int(980 * scale)}")
        root.resizable(False, False)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        home_tab = ttk.Frame(self.notebook)
        manual_tab = ttk.Frame(self.notebook)
        settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(home_tab, text="播客库")
        self.notebook.add(manual_tab, text="手动处理")
        self.notebook.add(settings_tab, text="设置")

        self.home = HomeTab(home_tab)
        self.manual = ManualTab(manual_tab)
        self.settings = SetupWizard(settings_tab)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event=None):
        current = self.notebook.index(self.notebook.select())
        if current == 0:
            self.home.refresh()
        elif current == 1:
            self.manual.refresh_show_choices()
        else:
            self.settings.reload_from_config()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
