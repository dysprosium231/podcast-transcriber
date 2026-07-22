"""
播客自动化管理界面——「播客库」浏览已生成的节目，「设置」管理播客订阅、翻译服务商/API Key、
Whisper 模型下载、每日计划任务。生成/更新 config.json；API Key 通过 setx 写入当前用户的 Windows
环境变量（不存进 config.json）。

用 `pythonw setup_wizard.py` 运行不会弹黑框控制台；用 `python setup_wizard.py` 运行能在终端看到日志。

注意：setx 写的环境变量只对"之后新打开"的进程生效，当前正在运行的终端/程序需要重启才能读到。
"""
import os
import re
import sys
import json
import ctypes
import zipfile
import subprocess
import threading
import feedparser
import requests
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tqdm import tqdm

# 默认走国内镜像下载Whisper模型，huggingface.co国内经常连不上/巨慢。用setdefault而不是直接
# 赋值：如果用户自己已经设过HF_ENDPOINT，尊重用户的选择，不覆盖。想换回官方源见README里的说明。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

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
RUN_DAILY_BAT_PATH = os.path.join(SCRIPT_DIR, "run_daily.bat")
DAILY_PODCAST_PATH = os.path.join(SCRIPT_DIR, "daily_podcast.py")
TASK_NAME_DEFAULT = "DailyPodcast"
EPISODES_DIR = os.path.join(SCRIPT_DIR, "episodes")

SENSEVOICE_REPO = "lovemefan/SenseVoice-onnx"
SENSEVOICE_MODEL_DIR = os.path.join(MODELS_DIR, "sensevoice")
SENSEVOICE_MODEL_FILES = [
    "embedding.npy", "sense-voice-encoder-int8.onnx", "chn_jpn_yue_eng_ko_spectok.bpe.model",
    "fsmnvad-offline.onnx", "am.mvn", "fsmn-am.mvn", "fsmn-config.yaml",
]

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

WHISPER_MODEL_CHOICES = [
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v2", "large-v3", "distil-large-v3",
]

# 只支持中英两种语言+自动识别；配置文件里存value（"auto"/"en"/"zh"），下拉框显示label
LANGUAGE_LABELS = {"auto": "自动识别（推荐）", "en": "英文", "zh": "中文"}
LANGUAGE_VALUES_BY_LABEL = {v: k for k, v in LANGUAGE_LABELS.items()}

# 转录引擎：whisper（GPU，英文/口音更稳）或sensevoice（纯CPU，不需要GPU/CUDA，中文更快更准还
# 自带标点，但英文鲁棒性不如whisper）。配置文件里存value，下拉框显示label
ENGINE_LABELS = {
    "whisper": "Whisper（需要GPU，英文/口音更稳）",
    "sensevoice": "SenseVoice（不需要GPU，中文更快更准）",
}
ENGINE_VALUES_BY_LABEL = {v: k for k, v in ENGINE_LABELS.items()}

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

    style.configure(".", background=COLORS["bg"], foreground=COLORS["fg"], font=("Segoe UI", 10))
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["fg"])
    style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["fg_muted"])
    style.configure("Success.TLabel", background=COLORS["bg"], foreground=COLORS["success"])
    style.configure("Danger.TLabel", background=COLORS["bg"], foreground=COLORS["danger"])
    style.configure("Warning.TLabel", background=COLORS["bg"], foreground=COLORS["warning"])
    style.configure("Heading.TLabel", background=COLORS["bg"], foreground=COLORS["fg"], font=("Segoe UI", 12, "bold"))

    style.configure(
        "TLabelframe", background=COLORS["bg"], bordercolor=COLORS["border"],
        relief="solid", borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label", background=COLORS["bg"], foreground=COLORS["fg"],
        font=("Segoe UI", 11, "bold"),
    )

    style.configure(
        "TButton", background=COLORS["field_bg"], foreground=COLORS["fg"],
        borderwidth=0, focuscolor=COLORS["bg"], padding=(11, 7), font=("Segoe UI", 10),
    )
    style.map(
        "TButton",
        background=[("active", COLORS["border"]), ("disabled", COLORS["bg_elevated"])],
        foreground=[("disabled", COLORS["fg_muted"])],
    )

    style.configure(
        "Accent.TButton", background=COLORS["accent"], foreground="#ffffff",
        borderwidth=0, focuscolor=COLORS["bg"], padding=(13, 8), font=("Segoe UI", 10, "bold"),
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
        foreground=COLORS["fg"], borderwidth=0, rowheight=30, font=("Segoe UI", 10),
    )
    style.configure(
        "Treeview.Heading", background=COLORS["bg_elevated"], foreground=COLORS["fg"],
        borderwidth=0, relief="flat", font=("Segoe UI", 10, "bold"),
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
        padding=(24, 12), font=("Segoe UI", 11), borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", COLORS["bg"])],
        foreground=[("selected", COLORS["accent"])],
        font=[("selected", ("Segoe UI", 11, "bold"))],
    )


def make_scrollable(container):
    """把container包一层Canvas+Scrollbar纵向滚动区域，返回内层Frame——调用方把原本要往
    container里塞的内容改成塞进这个返回值就行，逻辑不用大改。

    三个页签（播客库/手动处理下的三个子页签/设置）内容加起来都可能比主窗口高——主窗口是
    resizable(False, False)的固定大小，缩小到能在常见笔记本分辨率下放得下之后，任何一个
    页签内容稍微多一点（比如播客库的期数列表、手动处理里的三个子表单）就会被裁掉一截，
    连按钮都点不到。每个页签都套一层，而不是只套内容最多的「设置」那一个。"""
    canvas = tk.Canvas(container, bg=COLORS["bg"], highlightthickness=0)
    scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    inner = ttk.Frame(canvas)
    inner_window = canvas.create_window((0, 0), window=inner, anchor="nw")

    inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(inner_window, width=e.width))

    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    # 内容发生大改动的时候（比如播客库刷新、RSS拉取到新一批entries）如果不主动挪回顶部，
    # 用户停留的滚动位置很容易变成对着新内容里的一片空白（旧内容更长、滚动条停在下面，
    # 刷新后新内容变短，那个位置就已经超出新内容范围了）。调用方在内容大改之后调一下
    # 这个方法，滚回顶部，不留下莫名其妙的空白
    inner.scroll_to_top = lambda: canvas.yview_moveto(0)

    return inner


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

    box = tk.Label(frame, font=("Segoe UI", 13), bg=COLORS["bg"], cursor="hand2")
    box.pack(side="left")
    label = tk.Label(frame, text=text, bg=COLORS["bg"], fg=COLORS["fg"], font=("Segoe UI", 10), cursor="hand2")
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


def sensevoice_model_is_downloaded():
    return all(os.path.exists(os.path.join(SENSEVOICE_MODEL_DIR, f)) for f in SENSEVOICE_MODEL_FILES)


def start_sensevoice_download(state):
    """跟start_model_download()是并列的两条下载逻辑，SenseVoice这边全程纯CPU/ONNX，
    不涉及CUDA，模型也小得多（int8量化版约240MB，whisper large-v3有好几个GB）"""
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

            # 实测HF镜像偶尔会在几个KB大小的小文件上抽风（HEAD请求404几次），大文件反而没事，
            # 按文件校验+重试几次基本都能过，不能假设snapshot_download一次就齐活
            os.makedirs(SENSEVOICE_MODEL_DIR, exist_ok=True)
            missing = SENSEVOICE_MODEL_FILES
            for _attempt in range(4):
                snapshot_download(
                    repo_id=SENSEVOICE_REPO,
                    local_dir=SENSEVOICE_MODEL_DIR,
                    allow_patterns=missing,
                    tqdm_class=ProgressTqdm,
                )
                missing = [f for f in SENSEVOICE_MODEL_FILES
                           if not os.path.exists(os.path.join(SENSEVOICE_MODEL_DIR, f))]
                if not missing:
                    break
            else:
                raise RuntimeError(f"以下模型文件始终下载不全，请稍后重试：{missing}")
            state.progress = 1.0
            state.done = True
        except Exception as e:
            state.error = str(e)
        finally:
            state.running = False

    threading.Thread(target=worker, daemon=True).start()


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

    def __init__(self, container):
        self.download_state = DownloadState()
        self.sensevoice_download_state = DownloadState()
        self.parent = make_scrollable(container)

        self._build_feeds_section()
        self._build_translation_section()
        self._build_engine_section()
        self._build_model_section()
        self._build_sensevoice_section()
        self._build_runtime_section()
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
        self._refresh_sensevoice_status()
        self.engine_var.set(ENGINE_LABELS.get(config.get("transcribe_engine", "whisper"), ENGINE_LABELS["whisper"]))
        self.language_var.set(LANGUAGE_LABELS.get(config.get("language", "auto"), LANGUAGE_LABELS["auto"]))
        self.python_exe_var.set(config.get("python_exe", ""))
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
        self.extra_prompt_text = tk.Text(frame, height=3, width=70, font=("Segoe UI", 10))
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
    def _build_engine_section(self):
        frame = ttk.LabelFrame(self.parent, text="转录引擎与语言")
        frame.pack(fill="x", padx=14, pady=7)

        engine_row = ttk.Frame(frame)
        engine_row.pack(fill="x", padx=10, pady=10)
        ttk.Label(engine_row, text="转录引擎").pack(side="left")
        self.engine_var = tk.StringVar(value=ENGINE_LABELS["whisper"])
        ttk.Combobox(
            engine_row, textvariable=self.engine_var,
            values=list(ENGINE_LABELS.values()), state="readonly", width=28,
        ).pack(side="left", padx=(4, 10))

        lang_row = ttk.Frame(frame)
        lang_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(lang_row, text="转录语言").pack(side="left")
        self.language_var = tk.StringVar(value=LANGUAGE_LABELS["auto"])
        ttk.Combobox(
            lang_row, textvariable=self.language_var,
            values=list(LANGUAGE_LABELS.values()), state="readonly", width=16,
        ).pack(side="left", padx=(4, 10))

        ttk.Label(
            frame, style="Muted.TLabel", wraplength=680, justify="left",
            text="Whisper效果更成熟，尤其擅长处理口音较重的英文，但要跑GPU（需要CUDA环境）。"
                 "SenseVoice全程CPU/ONNX Runtime，不需要GPU、不需要装CUDA，中文识别更快更准还"
                 "自带标点，但英文鲁棒性不如Whisper——没有GPU、或者主要转录中文内容的话优先"
                 "选SenseVoice；转录以英文为主、追求最佳准确率就用Whisper。\n"
                 "只支持中文/英文两种语言。自动识别是转录时顺带判断的，不额外花时间；"
                 "源音频语言比较确定的话手动指定能避免偶尔识别错。识别为中文时会自动跳过翻译这一步。",
        ).pack(anchor="w", padx=10, pady=(0, 10))

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

    def _build_sensevoice_section(self):
        frame = ttk.LabelFrame(self.parent, text="SenseVoice 转录模型（纯CPU，不需要GPU/CUDA）")
        frame.pack(fill="x", padx=14, pady=7)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=10)
        ttk.Label(row, text="模型：SenseVoiceSmall（int8量化版，约240MB）").pack(side="left")

        self.sensevoice_status_label = ttk.Label(row, text="")
        self.sensevoice_status_label.pack(side="left", padx=(10, 10))

        self.sensevoice_download_btn = ttk.Button(row, text="下载此模型", command=self._start_sensevoice_download)
        self.sensevoice_download_btn.pack(side="left")

        self.sensevoice_download_progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.sensevoice_download_progress.pack(fill="x", padx=10, pady=(0, 5))
        self.sensevoice_download_status_label = ttk.Label(frame, text="", style="Muted.TLabel")
        self.sensevoice_download_status_label.pack(anchor="w", padx=10, pady=(0, 10))

    def _refresh_sensevoice_status(self):
        if sensevoice_model_is_downloaded():
            self.sensevoice_status_label.config(text="✅ 本地已有", style="Success.TLabel")
            self.sensevoice_download_btn.config(state="disabled")
        else:
            self.sensevoice_status_label.config(text="⬇ 本地未下载", style="Warning.TLabel")
            self.sensevoice_download_btn.config(state="normal")

    def _start_sensevoice_download(self):
        self.sensevoice_download_btn.config(state="disabled")
        self.sensevoice_download_status_label.config(text="正在下载（约240MB）...", style="Muted.TLabel")
        start_sensevoice_download(self.sensevoice_download_state)
        self._poll_sensevoice_download()

    def _poll_sensevoice_download(self):
        state = self.sensevoice_download_state
        self.sensevoice_download_progress["value"] = state.progress * 100
        if state.error:
            self.sensevoice_download_status_label.config(text=f"下载失败：{state.error}", style="Danger.TLabel")
            self.sensevoice_download_btn.config(state="normal")
            return
        if state.done:
            self.sensevoice_download_status_label.config(text="下载完成", style="Success.TLabel")
            self._refresh_sensevoice_status()
            return
        if state.running:
            self.sensevoice_download_status_label.config(text=f"下载中... {state.desc}", style="Muted.TLabel")
            self.parent.after(200, self._poll_sensevoice_download)

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

    # ---------------- 运行环境 ----------------
    def _build_runtime_section(self):
        frame = ttk.LabelFrame(self.parent, text="运行环境（手动处理用来跑转录/翻译的Python）")
        frame.pack(fill="x", padx=14, pady=7)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=10)
        self.python_exe_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.python_exe_var, width=50).pack(side="left")
        ttk.Button(row, text="浏览...", command=self._browse_python_exe).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="自动检测", command=self._auto_detect_python_exe).pack(side="left", padx=(6, 0))

        self.runtime_status_label = ttk.Label(frame, text="", style="Muted.TLabel")
        self.runtime_status_label.pack(anchor="w", padx=10, pady=(0, 5))

        ttk.Label(
            frame, style="Muted.TLabel", wraplength=680, justify="left",
            text=(
                "「手动处理」页签的转录/翻译不在这个界面自己的进程里跑，而是把任务交给这里指定的"
                "Python解释器（装了faster-whisper等依赖的那个环境，比如conda环境里的python.exe）"
                "当独立子进程执行。留空的话会尝试自动从 run_daily.bat 里读取。"
                "「保存配置」会同步把这个路径写进 run_daily.bat（每日自动运行实际用的就是它）。"
            ),
        ).pack(anchor="w", padx=10, pady=(0, 10))

    def _browse_python_exe(self):
        path = filedialog.askopenfilename(title="选择python.exe", filetypes=[("python.exe", "python.exe"), ("所有文件", "*.*")])
        if path:
            self.python_exe_var.set(path)

    def _auto_detect_python_exe(self):
        found = find_python_exe(prefer_config=False)
        if found:
            self.python_exe_var.set(found)
            self.runtime_status_label.config(text=f"检测到：{found}", style="Success.TLabel")
        else:
            self.runtime_status_label.config(text="没有自动检测到，请点「浏览...」手动选择", style="Warning.TLabel")

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
            "transcribe_engine": ENGINE_VALUES_BY_LABEL.get(self.engine_var.get(), "whisper"),
            "language": LANGUAGE_VALUES_BY_LABEL.get(self.language_var.get(), "auto"),
            "python_exe": self.python_exe_var.get().strip(),
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

        sync_run_daily_bat_conda_env(config["python_exe"])

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


def _episode_meta(ep_path):
    """读一个期数文件夹里的meta.json（daily_podcast.py处理完会写这个）。旧数据（这个功能
    上线之前处理的）没有这个文件，返回空字典，调用方要按"什么日期信息都没有"来处理，不能假设
    一定有内容。"""
    meta_path = os.path.join(ep_path, "meta.json")
    if not os.path.exists(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _episode_date(ep_path):
    """从meta.json解析出一个datetime，优先用RSS发布时间，没有就用处理时刻兜底。两种时间格式
    不一样（RSS是"Tue, 21 Jul 2026 09:54:49 GMT"这种邮件日期格式，处理时刻是ISO格式），
    都解析失败（或者压根没有meta.json）就返回None，调用方各自决定怎么兜底。"""
    date_str = _episode_meta(ep_path).get("published") or _episode_meta(ep_path).get("processed_at") or ""
    if not date_str:
        return None
    from datetime import datetime as _dt
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        pass
    try:
        return _dt.fromisoformat(date_str)
    except ValueError:
        return None


def scan_episodes():
    """扫描 episodes/ 目录，返回 [(节目名, 期数标题, 文件夹绝对路径, 显示用日期字符串), ...]，
    每个节目内部按日期新到旧排序——RSS的itunes:episode（严格意义上的"第几期"编号）几乎没播客在用
    （实测过两个真实feed都没有），只能退而求其次用发布时间/处理时刻代替真正的期数。
    没有日期信息的（旧数据）排在最后，按标题字母序。"""
    result = []
    if not os.path.isdir(EPISODES_DIR):
        return result
    for show_name in sorted(os.listdir(EPISODES_DIR)):
        show_path = os.path.join(EPISODES_DIR, show_name)
        if not os.path.isdir(show_path):
            continue
        ep_titles = [
            t for t in os.listdir(show_path)
            if os.path.isdir(os.path.join(show_path, t))
        ]

        def sort_key(title):
            dt = _episode_date(os.path.join(show_path, title))
            return (dt is None, -dt.timestamp() if dt else 0, title)

        ep_titles.sort(key=sort_key)

        for ep_title in ep_titles:
            ep_path = os.path.join(show_path, ep_title)
            dt = _episode_date(ep_path)
            display_date = dt.strftime("%Y-%m-%d") if dt else ""
            result.append((show_name, ep_title, ep_path, display_date))
    return result


class HomeTab:
    """播客库主页——浏览 episodes/ 下已经生成的节目，双击/点按钮直接打开字幕页、音频或所在文件夹，
    不用自己去文件资源管理器里一层层找"""

    def __init__(self, container):
        self.parent = make_scrollable(container)
        parent = self.parent

        top = ttk.Frame(parent)
        top.pack(fill="x", padx=14, pady=(14, 7))
        ttk.Label(top, text="episodes/ 下已生成的节目", style="Heading.TLabel").pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="right")

        # 按节目名分组显示（show="tree headings"）：节目是父节点（默认展开），期数是子节点。
        # height=14：这个页签也套了外层滚动区域，Treeview不用靠自己撑满所有行才能看全，
        # 超过14行外层滚动条就能接着看，不用把整个页签撑得比窗口还高
        self.tree = ttk.Treeview(parent, columns=("date",), show="tree headings", height=14)
        self.tree.heading("#0", text="节目")
        self.tree.heading("date", text="日期")
        self.tree.column("#0", width=460)
        self.tree.column("date", width=110)
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
        self.open_srt_btn = ttk.Button(btn_row, text="打开SRT字幕", command=self._open_srt, state="disabled")
        self.open_srt_btn.pack(side="left", padx=(8, 0))
        self.open_vtt_btn = ttk.Button(btn_row, text="打开VTT字幕", command=self._open_vtt, state="disabled")
        self.open_vtt_btn.pack(side="left", padx=(8, 0))
        self.open_folder_btn = ttk.Button(btn_row, text="打开所在文件夹", command=self._open_folder, state="disabled")
        self.open_folder_btn.pack(side="left", padx=(8, 0))

        self.status_label = ttk.Label(parent, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w", padx=14, pady=(0, 14))

        self._node_paths = {}  # tree节点id -> 期数文件夹绝对路径（只有期数这一级子节点才有，
                                # 节目名那一级父节点不在这里面，选中父节点时按钮全部禁用）
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self._node_paths.clear()
        episodes = scan_episodes()  # 已经按 节目名→日期新到旧 分组排好序

        shows = {}  # 节目名 -> 这个节目在tree里的父节点id
        for show_name, ep_title, ep_path, display_date in episodes:
            if show_name not in shows:
                shows[show_name] = self.tree.insert("", "end", text=show_name, open=True)
            ep_node = self.tree.insert(shows[show_name], "end", text=ep_title, values=(display_date,))
            self._node_paths[ep_node] = ep_path

        # 父节点标题后面补上这个节目有几期，等tree都插完、数量确定了再统一回填
        for show_name, show_node in shows.items():
            count = len(self.tree.get_children(show_node))
            self.tree.item(show_node, text=f"{show_name}（{count}期）")

        if not episodes:
            self.status_label.config(text="还没有生成任何一期——跑一次 daily_podcast.py 之后回来刷新看看")
        else:
            self.status_label.config(text=f"共 {len(shows)} 个节目、{len(episodes)} 期")

        self._update_buttons_state()
        self.parent.scroll_to_top()

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
        has_srt = bool(path) and os.path.exists(os.path.join(path, "subtitles.srt"))
        has_vtt = bool(path) and os.path.exists(os.path.join(path, "subtitles.vtt"))
        self.open_subtitles_btn.config(state="normal" if has_subtitles else "disabled")
        self.open_audio_btn.config(state="normal" if has_audio else "disabled")
        self.open_srt_btn.config(state="normal" if has_srt else "disabled")
        self.open_vtt_btn.config(state="normal" if has_vtt else "disabled")
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

    def _open_srt(self):
        path = self._selected_episode_path()
        if not path:
            return
        target = os.path.join(path, "subtitles.srt")
        if os.path.exists(target):
            os.startfile(target)

    def _open_vtt(self):
        path = self._selected_episode_path()
        if not path:
            return
        target = os.path.join(path, "subtitles.vtt")
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
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".wma"}

# 手动处理不在这个界面自己的进程里跑转录/翻译——那样会逼着GUI也打包faster-whisper/ctranslate2
# 这些体积巨大又依赖CUDA运行库的东西。而是调用真实python环境（比如conda环境）里的
# daily_podcast.py 作为独立子进程，通过它逐行往stdout打JSON汇报进度，这里解析读取。
# 界面进程本身保持轻量，也彻底不会有"打包的exe里缺CUDA DLL"这种问题。
_PY_EXE_CACHE = None


def sanitize_filename(title):
    """跟daily_podcast.py里的同名函数逻辑一致——就是这么短，直接抄一份，不为这一个函数去
    导入整个daily_podcast.py（那样会把它的faster-whisper等重量级依赖也带进GUI进程）"""
    return re.sub(r'[\\/*?:"<>|]', "", title)[:80]


def resolve_apple_podcasts_feed(url):
    """跟daily_podcast.py里的同名函数逻辑一致，同样的理由直接抄一份而不是导入。
    苹果播客链接（podcasts.apple.com/.../idNNNNNN）通过苹果公开的iTunes Lookup API
    查出真正的RSS地址；不是苹果链接就原样返回。"""
    match = re.search(r"podcasts\.apple\.com/[^\s]*?/id(\d+)", url)
    if not match:
        return url
    podcast_id = match.group(1)
    resp = requests.get(f"https://itunes.apple.com/lookup?id={podcast_id}", timeout=15, headers=RSS_HEADERS)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results or not results[0].get("feedUrl"):
        raise ValueError("这个苹果播客ID没有查到对应的RSS地址（可能不是播客节目，或者苹果没公开这个字段）")
    return results[0]["feedUrl"]


def resolve_apple_episode_guid(url):
    """如果链接里带着?i=数字（苹果链接指到具体某一期，不只是节目主页），查出这一期在
    RSS里对应的<guid>，方便调用方从拉取到的列表里精确定位到这一期。查不到（比如太老的
    单集，苹果这个episode查询接口最多只给最近200条）就返回None，调用方按"没定位到具体
    某一期，就当成拉取整档节目"处理，不报错、不中断。"""
    show_match = re.search(r"podcasts\.apple\.com/[^\s?]*?/id(\d+)", url)
    episode_match = re.search(r"[?&]i=(\d+)", url)
    if not show_match or not episode_match:
        return None
    show_id = show_match.group(1)
    episode_id = int(episode_match.group(1))
    resp = requests.get(
        f"https://itunes.apple.com/lookup?id={show_id}&entity=podcastEpisode&limit=200",
        timeout=15, headers=RSS_HEADERS,
    )
    resp.raise_for_status()
    for item in resp.json().get("results") or []:
        if item.get("trackId") == episode_id:
            return item.get("episodeGuid")
    return None


def find_python_exe(prefer_config=True):
    """找一个装好了转录/翻译依赖的真实python解释器（不是这个GUI程序自己）。
    优先级：config.json里显式指定 > 解析run_daily.bat里的CONDA_ENV > 如果不是打包的exe、
    自己本来就在正确的环境里运行，直接用自己"""
    if prefer_config:
        config = load_existing_config()
        explicit = config.get("python_exe", "").strip()
        if explicit and os.path.exists(explicit):
            return explicit

    if os.path.exists(RUN_DAILY_BAT_PATH):
        try:
            with open(RUN_DAILY_BAT_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"set\s+CONDA_ENV=(.+)", content, re.IGNORECASE)
            if m:
                candidate = os.path.join(m.group(1).strip(), "python.exe")
                if os.path.exists(candidate):
                    return candidate
        except Exception:
            pass

    if not getattr(sys, "frozen", False):
        return sys.executable  # 用 python setup_wizard.py 运行时，自己就在正确的环境里

    return None


def sync_run_daily_bat_conda_env(python_exe):
    """把run_daily.bat里的CONDA_ENV同步成python_exe所在目录。

    「应用定时任务设置」只负责建/改schtasks任务本身，实际每天触发时真正执行的
    run_daily.bat读的是它自己文件里硬编码的CONDA_ENV，跟GUI这边配置的python_exe
    完全是两回事——不同步的话，新用户走完整个设置向导、「手动处理」测试也一切正常
    （那条路径靠find_python_exe的兜底逻辑，能兜到当前正在运行GUI的这个解释器），
    但每天真正自动触发时run_daily.bat仍然用着仓库里带的旧路径，静默失败且没有任何
    界面提示。这里在保存配置时顺手把CONDA_ENV同步过去，两边保持一致。

    只在python_exe非空时才动手——不知道该同步成什么就不猜，宁可保持原样。
    读写失败（文件被占用、没权限之类）静默跳过，不应该因为这个阻断保存配置。"""
    if not python_exe or not os.path.exists(RUN_DAILY_BAT_PATH):
        return
    conda_env = os.path.dirname(python_exe)
    try:
        with open(RUN_DAILY_BAT_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        new_content, count = re.subn(
            r"(set\s+CONDA_ENV=).+", lambda m: m.group(1) + conda_env, content,
            count=1, flags=re.IGNORECASE,
        )
        if count:
            with open(RUN_DAILY_BAT_PATH, "w", encoding="utf-8") as f:
                f.write(new_content)
    except Exception:
        pass


def build_subprocess_env(python_exe):
    """给子进程准备PATH，跟run_daily.bat里设置的一样，让faster-whisper能找到cublas/cudnn等DLL"""
    conda_env = os.path.dirname(python_exe)
    env = os.environ.copy()
    prefix = os.pathsep.join([
        conda_env,
        os.path.join(conda_env, "Library", "mingw-w64", "bin"),
        os.path.join(conda_env, "Library", "usr", "bin"),
        os.path.join(conda_env, "Library", "bin"),
        os.path.join(conda_env, "Scripts"),
        os.path.join(conda_env, "bin"),
    ])
    env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    # 子进程的stdout被重定向成管道时，Python默认按系统区域设置的编码写（中文Windows是GBK），
    # 而这边父进程读的时候固定按UTF-8解码——两边编码对不上，中文进度文字/标题就会被读乱。
    # 强制子进程stdio走UTF-8，跟父进程解码方式对齐
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class BatchJob:
    """一项待处理的任务：一个音频（不管来自本地文件、RSS下载还是zip压缩包）要归到哪个节目、
    起什么标题。source_type是"local"/"download"/"zip"之一，source是对应的路径/URL/(zip路径,条目名)"""

    def __init__(self, show_name, episode_title, source_type, source, published=""):
        self.show_name = show_name
        self.episode_title = episode_title
        self.source_type = source_type
        self.source = source
        self.published = published  # 只有RSS历史下载场景才有，本地文件/zip没有这个概念
        self.status = "排队中"
        self.error = None
        self.result_dir = None


class BatchState:
    """批量处理线程和GUI主线程之间只通过这几个简单字段传递状态（不直接跨线程碰tk控件）"""

    def __init__(self):
        self.jobs = []
        self.current_index = -1
        self.running = False
        self.done = False
        self.cancel_requested = False
        self.item_progress = 0.0


def episode_target_dir(job):
    safe_title = sanitize_filename(job.episode_title)
    return os.path.join(EPISODES_DIR, job.show_name, safe_title)


def check_conflicts(jobs):
    """处理开始前一次性检查哪些目标文件夹已经存在（已经处理过），用于批量询问是否覆盖，
    而不是一项项弹窗打断"""
    conflicts = []
    for job in jobs:
        episode_dir = episode_target_dir(job)
        if os.path.exists(os.path.join(episode_dir, "subtitles.html")):
            conflicts.append(job)
    return conflicts


def confirm_and_filter_conflicts(jobs):
    """有冲突就一次性弹一个框列出全部冲突项，问是否覆盖；返回过滤后的任务列表
    （用户选"否"就去掉那些冲突项，其余照常）。返回None表示用户想直接取消整个操作"""
    conflicts = check_conflicts(jobs)
    if not conflicts:
        return jobs
    names = "\n".join(f"「{j.show_name}」{j.episode_title}" for j in conflicts)
    overwrite = messagebox.askyesnocancel(
        "部分内容已存在",
        f"以下 {len(conflicts)} 项之前已经处理过：\n\n{names}\n\n"
        "是否覆盖重新处理？\n「是」=全部覆盖　「否」=跳过这些、只处理其余项　「取消」=不处理任何项目",
    )
    if overwrite is None:
        return None
    if overwrite:
        return jobs
    conflict_ids = {id(j) for j in conflicts}
    return [j for j in jobs if id(j) not in conflict_ids]


def _process_one_job(python_exe, job, on_stage):
    """把这一项任务扔给真实python环境里的daily_podcast.py当子进程处理，
    从它逐行打的JSON里读进度；不在GUI自己的进程里碰任何转录/翻译代码"""
    if job.source_type == "zip":
        zip_path, entry_name = job.source
        source_arg = f"{zip_path}::{entry_name}"
    else:
        source_arg = job.source

    # config.json里存的就是"auto"/"en"/"zh"这个原始value（下拉框label<->value的转换在
    # SettingsTab保存时就做完了），这里直接读，不用再转一次
    config = load_existing_config()
    language = config.get("language", "auto")
    engine = config.get("transcribe_engine", "whisper")
    args = [
        python_exe, DAILY_PODCAST_PATH, "--manual-job",
        "--show", job.show_name, "--title", job.episode_title,
        "--source-type", job.source_type, "--source", source_arg,
        "--published", job.published, "--language", language, "--engine", engine,
    ]
    env = build_subprocess_env(python_exe)
    proc = subprocess.Popen(
        args, cwd=SCRIPT_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    result_dir = None
    error_msg = None
    tail_lines = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            tail_lines.append(line)  # tqdm之类的普通日志行，不是JSON，留着万一失败时给用户看
            continue
        if "error" in msg:
            error_msg = msg["error"]
        elif msg.get("done"):
            result_dir = msg.get("result_dir")
        else:
            on_stage(msg.get("stage", ""), msg.get("progress", 0.0))

    proc.wait()
    if error_msg:
        raise RuntimeError(error_msg)
    if proc.returncode != 0 or not result_dir:
        tail = "\n".join(tail_lines[-15:])
        raise RuntimeError(f"子进程处理失败（退出码 {proc.returncode}）：{tail or '没有更多输出信息'}")
    return result_dir


def run_batch(jobs, state):
    """排队顺序处理一批任务（下载/转录/翻译不能并行，GPU和网络本来就得一个个来）。
    取消只影响还没开始的排队项，已经在跑的那一项会跑完"""
    state.jobs = jobs
    state.current_index = -1
    state.running = True
    state.done = False
    state.cancel_requested = False
    state.item_progress = 0.0

    def worker():
        python_exe = find_python_exe()
        if not python_exe:
            for j in jobs:
                j.status = "失败"
                j.error = "找不到可用的Python环境，请到「设置」页的「运行环境」里手动指定或自动检测"
            state.running = False
            state.done = True
            return

        for idx, job in enumerate(jobs):
            if state.cancel_requested:
                job.status = "已取消"
                continue
            state.current_index = idx
            state.item_progress = 0.0
            job.status = "处理中"

            def on_stage(text, progress, _job=job):
                _job.status = text
                state.item_progress = progress

            try:
                job.result_dir = _process_one_job(python_exe, job, on_stage)
                job.status = "完成"
            except Exception as e:
                job.status = "失败"
                job.error = str(e)

        state.running = False
        state.done = True

    threading.Thread(target=worker, daemon=True).start()


class BatchProgressWidget:
    """RSS历史下载、批量本地导入共用的排队进度展示：每项一行状态 + 当前项进度条 + 取消剩余"""

    def __init__(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.frame = frame

        self.status_tree = ttk.Treeview(frame, columns=("status",), show="tree headings", height=7)
        self.status_tree.heading("#0", text="标题")
        self.status_tree.heading("status", text="状态")
        self.status_tree.column("#0", width=420)
        self.status_tree.column("status", width=160)
        self.status_tree.pack(fill="both", expand=True, padx=10, pady=(6, 4))

        self.progress_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 4))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", padx=10, pady=(0, 8))
        self.cancel_btn = ttk.Button(btn_row, text="取消剩余排队项", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.overall_label = ttk.Label(btn_row, text="", style="Muted.TLabel")
        self.overall_label.pack(side="left", padx=(10, 0))

        self._job_nodes = {}
        self._state = None
        self._on_all_done = None

    def start(self, jobs, state, on_all_done=None):
        self._state = state
        self._on_all_done = on_all_done
        self.status_tree.delete(*self.status_tree.get_children())
        self._job_nodes = {}
        for job in jobs:
            node = self.status_tree.insert("", "end", text=f"「{job.show_name}」{job.episode_title}", values=(job.status,))
            self._job_nodes[id(job)] = node
        self.cancel_btn.config(state="normal")
        self.overall_label.config(text=f"共 {len(jobs)} 项", style="Muted.TLabel")
        self._poll()

    def _cancel(self):
        if self._state:
            self._state.cancel_requested = True
        self.cancel_btn.config(state="disabled")

    def _poll(self):
        state = self._state
        for job in state.jobs:
            node = self._job_nodes.get(id(job))
            if node:
                self.status_tree.item(node, values=(job.status,))
        self.progress_bar["value"] = state.item_progress * 100
        if state.done:
            done_count = sum(1 for j in state.jobs if j.status == "完成")
            fail_count = sum(1 for j in state.jobs if j.status == "失败")
            skip_count = sum(1 for j in state.jobs if j.status == "已取消")
            text = f"全部结束：{done_count} 完成，{fail_count} 失败"
            if skip_count:
                text += f"，{skip_count} 未开始就被取消"
            self.overall_label.config(text=text, style="Success.TLabel" if fail_count == 0 else "Danger.TLabel")
            self.cancel_btn.config(state="disabled")
            if self._on_all_done:
                self._on_all_done()
            return
        if state.running:
            idx = state.current_index
            self.overall_label.config(text=f"正在处理第 {idx + 1}/{len(state.jobs)} 项", style="Muted.TLabel")
            self.frame.after(200, self._poll)


class SingleFilePane:
    """手动处理 - 单个文件：不是从RSS订阅来的音频（漏抓的某一集、别的录音）也能走同一套
    转录+翻译流程，产出格式和自动流程完全一样，处理完在「播客库」页签就能看到"""

    def __init__(self, parent):
        self.parent = parent
        self.state = BatchState()
        self.audio_path = None

        file_row = ttk.Frame(parent)
        file_row.pack(fill="x", padx=10, pady=(14, 6))
        ttk.Button(file_row, text="选择音频文件...", command=self._pick_file).pack(side="left")
        self.file_label = ttk.Label(file_row, text="未选择文件", style="Muted.TLabel")
        self.file_label.pack(side="left", padx=(10, 0))

        # 本地文件和视频链接二选一：填了链接就用链接（走yt-dlp解析下载音频），没填链接
        # 才看有没有选本地文件。两个都填的话优先链接——因为链接这栏是后加的，选文件那个
        # 按钮更显眼，用户更可能是先点了选文件、后来想起来其实是要粘链接又忘了清空
        url_row = ttk.Frame(parent)
        url_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(url_row, text="或者粘贴视频链接（YouTube/B站）").pack(side="left")
        self.video_url_var = tk.StringVar()
        ttk.Entry(url_row, textvariable=self.video_url_var, width=40).pack(side="left", padx=(6, 0))

        form = ttk.Frame(parent)
        form.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(form, text="节目名").grid(row=0, column=0, sticky="w", pady=3)
        self.show_var = tk.StringVar()
        self.show_combo = ttk.Combobox(form, textvariable=self.show_var, width=20)
        self.show_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(form, text="期数标题").grid(row=1, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.title_var, width=45).grid(row=1, column=1, sticky="w", padx=(6, 0))

        self.start_btn = ttk.Button(parent, text="开始转录+翻译", style="Accent.TButton", command=self._start)
        self.start_btn.pack(anchor="w", padx=10, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(parent, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 5))
        self.status_label = ttk.Label(parent, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w", padx=10, pady=(0, 6))

        self.open_result_btn = ttk.Button(parent, text="打开字幕页", command=self._open_result, state="disabled")
        self.open_result_btn.pack(anchor="w", padx=10, pady=(0, 6))

        ttk.Label(
            parent, style="Muted.TLabel", wraplength=680, justify="left",
            text=(
                "节目名如果跟已有的一样，会归到同一个节目下面；不一样就会新建一个。\n"
                "视频链接支持yt-dlp能解析的网站（YouTube、B站等），只下音频不下视频。"
                "YouTube这边经常会被识别成「机器人」拦掉，不稳定，B站相对稳一些。\n"
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
        video_url = self.video_url_var.get().strip()
        if video_url:
            source_type, source = "ytdlp", video_url
        elif self.audio_path:
            source_type, source = "local", self.audio_path
        else:
            messagebox.showerror("错误", "请先选择音频文件，或者粘贴一个视频链接")
            return
        show_name = self.show_var.get().strip()
        title = self.title_var.get().strip()
        if not show_name or not title:
            messagebox.showerror("错误", "节目名和期数标题都要填")
            return

        jobs = [BatchJob(show_name, title, source_type, source)]
        jobs = confirm_and_filter_conflicts(jobs)
        if not jobs:
            return

        self.start_btn.config(state="disabled")
        self.open_result_btn.config(state="disabled")
        self.status_label.config(text="开始处理...", style="Muted.TLabel")
        self.progress_bar["value"] = 0
        self._job = jobs[0]
        run_batch(jobs, self.state)
        self._poll()

    def _poll(self):
        state = self.state
        self.progress_bar["value"] = state.item_progress * 100
        if state.done:
            job = self._job
            self.start_btn.config(state="normal")
            if job.status == "完成":
                self.status_label.config(text="处理完成！", style="Success.TLabel")
                self.open_result_btn.config(state="normal")
                self.refresh_show_choices()
            elif job.status == "已取消":
                self.status_label.config(text="已取消", style="Muted.TLabel")
            else:
                self.status_label.config(text=f"处理失败：{job.error}", style="Danger.TLabel")
            return
        if state.running:
            self.status_label.config(text=self._job.status, style="Muted.TLabel")
            self.parent.after(200, self._poll)

    def _open_result(self):
        if self._job.result_dir:
            target = os.path.join(self._job.result_dir, "subtitles.html")
            if os.path.exists(target):
                os.startfile(target)


class RssHistoryPane:
    """手动处理 - RSS历史下载：daily_podcast.py正常运行只抓最新一期，这里可以把RSS里的完整
    历史列出来，勾选想要的几期，一次性批量下载+转录+翻译。也支持临时粘贴一个不在订阅列表里的
    RSS地址，不局限于config.json里已经配置好的节目"""

    def __init__(self, parent):
        self.parent = parent
        self.state = BatchState()
        self.entries = []  # [(tree节点id, 标题, 音频URL, 发布时间)]

        # 节目名跟「单个文件」页签同一套逻辑：一个Combobox身兼两职——下拉选已有的会
        # 顺带把RSS地址自动填上（这里才有的额外行为，因为这里确实需要一个RSS地址才能
        # 拉取列表）；也可以直接敲一个还没配置过的新节目名，这时候RSS地址就自己填/粘贴。
        # 不再跟"下载后归到节目名"分成两个各管一段的字段，选的/敲的就是最终要用的节目名。
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=10, pady=(14, 6))
        ttk.Label(top, text="节目名").pack(side="left")
        self.show_var = tk.StringVar()
        self.show_combo = ttk.Combobox(top, textvariable=self.show_var, width=14)
        self.show_combo.pack(side="left", padx=(4, 12))
        self.show_combo.bind("<<ComboboxSelected>>", self._on_show_selected)

        ttk.Label(top, text="RSS地址").pack(side="left")
        self.rss_url_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.rss_url_var, width=32).pack(side="left", padx=(4, 12))
        self.fetch_btn = ttk.Button(top, text="拉取列表", style="Accent.TButton", command=self._fetch)
        self.fetch_btn.pack(side="left")
        self._fetch_state = None

        # 拉出来的历史动辄上千期（实测过intelligence这个feed有1977期），光靠滚动翻不现实，
        # 加个搜索框按标题过滤——用detach/reattach而不是删掉重插，这样勾选状态不会因为
        # 搜索字符串变化就丢失（用户可能搜一下勾几个、再搜别的关键词接着勾）
        search_row = ttk.Frame(parent)
        search_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(search_row, text="搜索标题").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._apply_filter)
        ttk.Entry(search_row, textvariable=self.search_var, width=40).pack(side="left", padx=(4, 0))

        tree_row = ttk.Frame(parent)
        tree_row.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.tree = ttk.Treeview(
            tree_row, columns=("date",), show="tree headings", height=9, selectmode="extended",
        )
        self.tree.heading("#0", text="标题")
        self.tree.heading("date", text="发布时间")
        self.tree.column("#0", width=420)
        self.tree.column("date", width=170)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll = ttk.Scrollbar(tree_row, orient="vertical", command=self.tree.yview)
        tree_scroll.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        sel_row = ttk.Frame(parent)
        sel_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(sel_row, text="全选", command=self._select_all).pack(side="left")
        ttk.Button(sel_row, text="取消全选", command=lambda: self.tree.selection_remove(*self.tree.get_children())).pack(side="left", padx=(6, 0))
        self.fetch_status_label = ttk.Label(sel_row, text="", style="Muted.TLabel")
        self.fetch_status_label.pack(side="left", padx=(10, 0))

        self.start_btn = ttk.Button(parent, text="下载并处理选中项", style="Accent.TButton", command=self._start)
        self.start_btn.pack(anchor="w", padx=10, pady=(0, 8))

        self.progress_widget = BatchProgressWidget(parent)

        ttk.Label(
            parent, style="Muted.TLabel", wraplength=680, justify="left",
            text="选中多期会排队依次下载+转录+翻译（不能同时跑多个，GPU一次只能处理一个）。"
                 "已经处理过的期数重新选中会先问是否覆盖，一次性问完不会一项项弹窗。",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        self.refresh_show_choices()

    def refresh_show_choices(self):
        config = load_existing_config()
        shows = list(config.get("feeds", {}).keys())
        self.show_combo["values"] = shows

    def _on_show_selected(self, _event=None):
        """下拉选了一个已配置节目——顺手把它的RSS地址也自动填上，省得再去设置页签抄一遍。
        如果这个节目在config.json里没存RSS地址（比如手动处理时随手起的名字），就什么都不填，
        用户自己粘贴，不强行清空已经填好的内容"""
        show = self.show_var.get()
        config = load_existing_config()
        url = config.get("feeds", {}).get(show)
        if url:
            self.rss_url_var.set(url)

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _apply_filter(self, *_args):
        """按标题过滤显示——detach不匹配的，重新attach匹配的，不是删了重插，
        已经勾选的项目哪怕暂时被搜索词过滤掉了，勾选状态也还留着，回头清空搜索词
        还能看到之前勾的还在。"""
        query = self.search_var.get().strip().lower()
        for node, title, _url, _pub in self.entries:
            if not query or query in title.lower():
                self.tree.move(node, "", "end")
            else:
                self.tree.detach(node)

    def _fetch(self):
        url = self.rss_url_var.get().strip()
        if not url:
            messagebox.showerror("错误", "请先选择已配置节目，或者自己填一个RSS地址")
            return

        # 抓取+解析（尤其是历史很长的feed，比如上千期）如果直接在主线程做，界面会卡成
        # "未响应"好几秒——跟run_batch/模型下载一样的套路：丢到后台线程跑，主线程只用
        # after()轮询一个普通对象的状态，不直接跨线程碰tk控件
        self.fetch_btn.config(state="disabled")
        self.fetch_status_label.config(text="拉取中...", style="Muted.TLabel")

        state = {"done": False, "error": None, "entries": None, "target_guid": None}
        self._fetch_state = state

        def worker():
            try:
                # 先看一眼是不是苹果播客链接，是的话换成它背后真正的RSS地址；
                # 不是苹果链接resolve_apple_podcasts_feed会原样把url传回来，不影响原逻辑
                real_url = resolve_apple_podcasts_feed(url)
                # 如果链接还带着?i=（指向具体某一期而不只是节目主页），顺便查一下这一期
                # 对应的RSS guid，等下把entries插进tree之后可以直接选中它，不用用户自己找
                state["target_guid"] = resolve_apple_episode_guid(url)
                # 跟daily_podcast.py的get_all_episodes同样的抓取方式（自己带超时，不让
                # feedparser直接拿URL），这里直接内联一遍而不是导入daily_podcast.py，
                # 避免GUI进程被迫加载它那些转录/翻译相关的重量级依赖
                r = requests.get(real_url, timeout=30, headers=RSS_HEADERS)
                r.raise_for_status()
                state["entries"] = feedparser.parse(r.content).entries
            except Exception as e:
                state["error"] = str(e)
            finally:
                state["done"] = True

        threading.Thread(target=worker, daemon=True).start()
        self.parent.after(200, self._poll_fetch)

    def _poll_fetch(self):
        state = self._fetch_state
        if not state["done"]:
            self.parent.after(200, self._poll_fetch)
            return

        self.fetch_btn.config(state="normal")

        if state["error"] is not None:
            messagebox.showerror("错误", f"拉取RSS失败：{state['error']}")
            self.fetch_status_label.config(text="", style="Muted.TLabel")
            return

        self.tree.delete(*self.tree.get_children())
        self.search_var.set("")  # 换了一批新拉取的entries，之前搜索框里残留的关键词清掉
        self.entries = []
        target_node = None
        for entry in state["entries"]:
            enclosures = entry.get("enclosures") or []
            if not enclosures:
                continue
            audio_url = enclosures[0].href
            pub = entry.get("published", "")
            node = self.tree.insert("", "end", text=entry.get("title", "（无标题）"), values=(pub,))
            self.entries.append((node, entry.get("title", "（无标题）"), audio_url, pub))
            if state["target_guid"] and entry.get("id") == state["target_guid"]:
                target_node = node

        if not self.entries:
            self.fetch_status_label.config(text="没有找到带音频链接的条目", style="Warning.TLabel")
        elif target_node:
            # 链接里指定了具体某一期，而且在列表里精确定位到了——直接选中+滚动过去，
            # 不用用户自己从几百上千期里去找
            self.tree.selection_set(target_node)
            self.tree.see(target_node)
            self.fetch_status_label.config(
                text=f"共 {len(self.entries)} 期，已定位到链接指定的那一期", style="Success.TLabel",
            )
        else:
            self.fetch_status_label.config(text=f"共 {len(self.entries)} 期", style="Muted.TLabel")

    def _start(self):
        target_show = self.show_var.get().strip()
        if not target_show:
            messagebox.showerror("错误", "请先填节目名（下拉选一个已有的，或者直接敲一个新的）")
            return
        selected_ids = set(self.tree.selection())
        if not selected_ids:
            messagebox.showerror("错误", "请先勾选要下载的期数")
            return

        jobs = [
            BatchJob(target_show, title, "download", url, published=pub)
            for node, title, url, pub in self.entries
            if node in selected_ids
        ]
        jobs = confirm_and_filter_conflicts(jobs)
        if not jobs:
            return

        self.start_btn.config(state="disabled")
        run_batch(jobs, self.state)
        self.progress_widget.start(jobs, self.state, on_all_done=self._on_done)

    def _on_done(self):
        self.start_btn.config(state="normal")
        self.refresh_show_choices()


class LocalBatchPane:
    """手动处理 - 批量本地导入：一整个文件夹或zip压缩包里的一堆mp3（不是来自任何RSS的本地
    播客归档），逐个建文件夹转录翻译。标题默认用文件名（去掉扩展名），不支持逐条改名——
    想要自定义标题就用「单个文件」那个页签"""

    def __init__(self, parent):
        self.parent = parent
        self.state = BatchState()
        self.discovered = []  # [(显示名, source_type, source)]

        top = ttk.Frame(parent)
        top.pack(fill="x", padx=10, pady=(14, 6))
        ttk.Button(top, text="选择文件夹...", command=self._pick_folder).pack(side="left")
        ttk.Button(top, text="选择ZIP压缩包...", command=self._pick_zip).pack(side="left", padx=(8, 0))
        self.source_label = ttk.Label(top, text="未选择", style="Muted.TLabel")
        self.source_label.pack(side="left", padx=(10, 0))

        self.tree = ttk.Treeview(parent, columns=(), show="tree", height=9, selectmode="extended")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        sel_row = ttk.Frame(parent)
        sel_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(sel_row, text="全选", command=self._select_all).pack(side="left")
        ttk.Button(sel_row, text="取消全选", command=lambda: self.tree.selection_remove(*self.tree.get_children())).pack(side="left", padx=(6, 0))

        target_row = ttk.Frame(parent)
        target_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(target_row, text="归到节目名").pack(side="left")
        self.target_show_var = tk.StringVar()
        self.show_combo = ttk.Combobox(target_row, textvariable=self.target_show_var, width=20)
        self.show_combo.pack(side="left", padx=(6, 0))

        self.start_btn = ttk.Button(parent, text="批量转录+翻译选中项", style="Accent.TButton", command=self._start)
        self.start_btn.pack(anchor="w", padx=10, pady=(0, 8))

        self.progress_widget = BatchProgressWidget(parent)

        ttk.Label(
            parent, style="Muted.TLabel", wraplength=680, justify="left",
            text="只扫描所选文件夹本身（不含子文件夹）里的音频文件；标题默认取文件名（不含扩展名）。"
                 "支持 mp3/m4a/wav/flac/aac/ogg/wma。选中多个会排队依次处理。",
        ).pack(anchor="w", padx=10, pady=(0, 10))

        self.refresh_show_choices()

    def refresh_show_choices(self):
        # 跟「单个文件」页签同一套逻辑：下拉能看到已有节目，也可以直接敲一个新的——
        # 之前这里是空实现，Combobox的下拉列表一直是空的，只能打字，看不到已有选项，
        # 跟另外两个页签不一致
        config = load_existing_config()
        self.show_combo["values"] = list(config.get("feeds", {}).keys())

    def _pick_folder(self):
        folder = filedialog.askdirectory(title="选择包含音频文件的文件夹")
        if not folder:
            return
        found = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            ext = os.path.splitext(name)[1].lower()
            if os.path.isfile(path) and ext in AUDIO_EXTENSIONS:
                title = os.path.splitext(name)[0]
                found.append((title, "local", path))
        self._populate(found, f"文件夹：{folder}")

    def _pick_zip(self):
        zip_path = filedialog.askopenfilename(title="选择ZIP压缩包", filetypes=[("ZIP压缩包", "*.zip")])
        if not zip_path:
            return
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
        except Exception as e:
            messagebox.showerror("错误", f"打开压缩包失败：{e}")
            return
        found = []
        for name in sorted(names):
            ext = os.path.splitext(name)[1].lower()
            if ext in AUDIO_EXTENSIONS and not name.endswith("/"):
                title = os.path.splitext(os.path.basename(name))[0]
                found.append((title, "zip", (zip_path, name)))
        self._populate(found, f"压缩包：{os.path.basename(zip_path)}")

    def _populate(self, found, source_desc):
        self.discovered = found
        self.tree.delete(*self.tree.get_children())
        for title, _stype, _source in found:
            self.tree.insert("", "end", text=title)
        if found:
            self.source_label.config(text=f"{source_desc}（找到 {len(found)} 个音频文件）", style="TLabel")
        else:
            self.source_label.config(text=f"{source_desc}（没有找到音频文件）", style="Warning.TLabel")

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _start(self):
        target_show = self.target_show_var.get().strip()
        if not target_show:
            messagebox.showerror("错误", "请填写归到哪个节目名")
            return
        selected_indices = {self.tree.index(i) for i in self.tree.selection()}
        if not selected_indices:
            messagebox.showerror("错误", "请先勾选要处理的音频")
            return

        jobs = [
            BatchJob(target_show, title, stype, source)
            for i, (title, stype, source) in enumerate(self.discovered)
            if i in selected_indices
        ]
        jobs = confirm_and_filter_conflicts(jobs)
        if not jobs:
            return

        self.start_btn.config(state="disabled")
        run_batch(jobs, self.state)
        self.progress_widget.start(jobs, self.state, on_all_done=self._on_done)

    def _on_done(self):
        self.start_btn.config(state="normal")


class ManualProcessingTab:
    """手动处理——三种来源殊途同归，都是走同一套 run_batch/_process_one_job 引擎：
    单个本地文件、RSS历史里挑几期、本地一批音频（文件夹或zip）"""

    def __init__(self, parent):
        inner = ttk.Notebook(parent)
        inner.pack(fill="both", expand=True, padx=4, pady=4)

        single_frame = ttk.Frame(inner)
        rss_frame = ttk.Frame(inner)
        batch_frame = ttk.Frame(inner)
        inner.add(single_frame, text="单个文件")
        inner.add(rss_frame, text="RSS历史下载")
        inner.add(batch_frame, text="批量本地导入")

        # 三个子页签各自套一层滚动区域——批量场景（RSS历史/批量导入）勾了很多期数时，
        # 排队状态列表加上表单本身很容易比窗口还高
        self.single = SingleFilePane(make_scrollable(single_frame))
        self.rss = RssHistoryPane(make_scrollable(rss_frame))
        self.batch = LocalBatchPane(make_scrollable(batch_frame))

    def refresh_show_choices(self):
        self.single.refresh_show_choices()
        self.rss.refresh_show_choices()
        self.batch.refresh_show_choices()


class App:
    """顶层窗口：「播客库」主页 + 「手动处理」+ 「设置」，用Notebook切换。切到某个页签时
    自动刷新一次那个页签的内容（比如设置页按config.json当前内容重新同步），避免显示的是旧值"""

    def __init__(self, root):
        self.root = root
        apply_modern_style(root)  # 顺带设置好了DPI缩放
        root.title("播客自动化")
        scale = root.winfo_fpixels("1i") / 96.0
        # 原来是1040，在1080p以下、或者DPI缩放大一点的屏幕上比可用工作区还高（实测过
        # 1076px内容 vs 1019px工作区，「保存配置」按钮直接被挤到屏幕外点不到）。
        # 现在每个页签都套了滚动区域（见make_scrollable），窗口本身不需要这么高了，
        # 缩小到700，内容超出的部分靠各页签自己的滚动条查看
        root.geometry(f"{int(800 * scale)}x{int(700 * scale)}")
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
        self.manual = ManualProcessingTab(manual_tab)
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
