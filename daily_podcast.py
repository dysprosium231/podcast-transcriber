import feedparser
import requests
import os
import sys
import re
import time
import json
import ctypes
import shutil
import zipfile
import tarfile
from datetime import datetime
from faster_whisper import WhisperModel, BatchedInferencePipeline
from openai import OpenAI
from tqdm import tqdm
from windows_toasts import InteractableWindowsToaster, Toast
import threading
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
from fullscreen_check import is_fullscreen_app_active

# 默认走国内镜像下载Whisper模型，huggingface.co国内经常连不上/巨慢。用setdefault而不是直接
# 赋值：如果用户自己已经设过HF_ENDPOINT（比如setx设成官方源或者别的镜像），尊重用户的选择，
# 不覆盖。想换回官方源见README里的说明。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 悬浮进度窗要清晰显示，必须在创建任何窗口前把进程标记为DPI感知，
# 否则Windows会用位图整体拉伸缩放窗口来适配系统缩放比例，导致文字和边框发糊
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# 脚本自身所在目录：无论项目文件夹被移动/改盘符到哪，都据此定位模型等资源，
# 不再依赖写死的绝对路径。run_daily.bat 已经把CWD切到项目根目录，但这里改用
# __file__ 自定位，即使脚本以后被从别的CWD直接调用也不受影响。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================== 配置 ====================
# 订阅节目、模型大小、翻译服务商都在 config.json 里配置（不进版本库，每个人的都不一样）。
# 首次使用请复制 config.example.json 为 config.json 再按需修改。
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"找不到配置文件 {CONFIG_PATH}，请先复制 config.example.json 为 config.json 并按需修改"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()

FEEDS = CONFIG["feeds"]  # {节目名: RSS地址}，节目名会用作文件夹名和通知里的显示名

_WHISPER_MODEL_SIZE = CONFIG.get("whisper_model_size", "large-v3")
_LOCAL_MODEL_DIR = os.path.join(SCRIPT_DIR, "models", _WHISPER_MODEL_SIZE)
# 本地手动放好的模型文件夹优先用；不存在（或者是个空/不完整文件夹，比如下载中断留下的）
# 就直接把模型名交给faster-whisper，它会自动从HuggingFace下载并缓存
_LOCAL_MODEL_READY = os.path.exists(os.path.join(_LOCAL_MODEL_DIR, "model.bin"))
MODEL_PATH = _LOCAL_MODEL_DIR if _LOCAL_MODEL_READY else _WHISPER_MODEL_SIZE
EPISODES_DIR = "episodes"  # 结构: episodes/节目名/期数标题/
LATEST_LOG = "latest_episodes.txt"

LANGUAGE = CONFIG.get("language", "auto")  # "auto"/"en"/"zh"，只支持这两种语言+自动识别

# 喂给Whisper的initial_prompt：只影响第一个30秒窗口的解码上下文，不是全程生效，但对国际
# 新闻类播客常见的人名/机构名（比如Zelensky、Syrskyi这类不常见拼写）有实测帮助——没有提示
# 的话Whisper遇到词表里没有、发音又不常见的词，容易按发音硬猜一个拼写相近但错误的词。
# 默认给一个泛用的新闻播客提示，不针对某一档节目；想要更精确可以在config.json里自己改
WHISPER_INITIAL_PROMPT = CONFIG.get(
    "whisper_initial_prompt",
    "This is a news and current affairs podcast. It may include names of international "
    "political, military, and public figures.",
)

# 转录引擎："whisper"（默认，GPU跑，英文/口音更稳）或"sensevoice"（纯CPU，不需要GPU/CUDA，
# 中文更快更准还自带标点，但英文鲁棒性不如whisper，见README里的对比说明）
TRANSCRIBE_ENGINE = CONFIG.get("transcribe_engine", "whisper")

SENSEVOICE_REPO = "lovemefan/SenseVoice-onnx"
SENSEVOICE_MODEL_DIR = os.path.join(SCRIPT_DIR, "models", "sensevoice")
SENSEVOICE_MODEL_FILES = [
    "embedding.npy", "sense-voice-encoder-int8.onnx", "chn_jpn_yue_eng_ko_spectok.bpe.model",
    "fsmnvad-offline.onnx", "am.mvn", "fsmn-am.mvn", "fsmn-config.yaml",
]
# int8量化版本（约240MB），纯CPU跑起来也够快，没必要为了一点精度差异去下载937MB的fp32版本

# 说话人分离（可选，默认关）：跟转录引擎（whisper/sensevoice）完全独立的第三套模型，纯CPU/ONNX，
# 不需要HuggingFace账号/token（直接走GitHub Releases直链），底层是sherpa-onnx对pyannote分割模型
# 的ONNX转换版 + 3D-Speaker声纹embedding模型
ENABLE_DIARIZATION = CONFIG.get("enable_diarization", False)
DIARIZATION_NUM_SPEAKERS = CONFIG.get("diarization_num_speakers", -1)  # -1表示自动判断说话人数

# 可选：yt-dlp下载视频链接时带上指定浏览器里已登录的cookies，伪装成真实登录用户请求，
# 能明显缓解YouTube的"Sign in to confirm you're not a bot"拦截（不保证100%有效，且要求
# 这台电脑上那个浏览器里保持着有效的YouTube登录状态）。留空就是不带cookies，跟以前一样
YTDLP_COOKIES_BROWSER = CONFIG.get("ytdlp_cookies_browser", "")

DIARIZATION_MODEL_DIR = os.path.join(SCRIPT_DIR, "models", "diarization")
DIARIZATION_SEGMENTATION_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
DIARIZATION_EMBEDDING_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
DIARIZATION_SEGMENTATION_FILE = os.path.join(
    DIARIZATION_MODEL_DIR, "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx"
)
DIARIZATION_EMBEDDING_FILE = os.path.join(
    DIARIZATION_MODEL_DIR, "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)

TRANSLATION_CONFIG = CONFIG.get("translation", {})
TRANSLATION_PROVIDER_NAME = TRANSLATION_CONFIG.get("provider_name", "翻译服务")
TRANSLATION_MODEL = TRANSLATION_CONFIG.get("model")
TRANSLATION_API_KEY_ENV = TRANSLATION_CONFIG.get("api_key_env", "TRANSLATION_API_KEY")
TRANSLATION_EXTRA_SYSTEM_PROMPT = TRANSLATION_CONFIG.get("extra_system_prompt", "")

# 翻译走OpenAI兼容接口——DeepSeek/OpenAI/Moonshot/智谱等大部分服务商都兼容这套SDK，
# 换服务商只需要改config.json里的base_url/api_key_env/model，不用改代码
translation_client = OpenAI(
    api_key=os.environ.get(TRANSLATION_API_KEY_ENV),
    base_url=TRANSLATION_CONFIG.get("base_url"),
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

SEGMENTS_PER_TRANSLATION_BATCH = 20  # 每次翻译请求打包多少句，减少API调用次数
TRANSLATE_TIMEOUT_SECONDS = 60  # 单次翻译请求超时时间，避免API/代理无响应时整个流程卡死
TRANSLATE_MAX_RETRIES = 2  # 超时/失败后的重试次数（不含首次尝试）

FULLSCREEN_CHECK_INTERVAL = 30 * 60  # 检测到全屏应用时，等待多久再重新检查（秒）
FULLSCREEN_MAX_WAIT_ROUNDS = 8  # 最多等待多少轮（8轮*30分钟=4小时），超过就不再等，直接开始

toaster = InteractableWindowsToaster("播客自动化")

# ==================== 全屏检测（避免和游戏抢显卡） ====================
def wait_if_fullscreen_active():
    """如果检测到全屏应用（大概率在玩游戏），推迟开始，避免和显卡资源抢占冲突"""
    rounds = 0
    while is_fullscreen_app_active() and rounds < FULLSCREEN_MAX_WAIT_ROUNDS:
        rounds += 1
        print(f"Fullscreen app detected, delaying {FULLSCREEN_CHECK_INTERVAL // 60} min (round {rounds})")
        notify_simple("检测到全屏应用运行中", f"将推迟{FULLSCREEN_CHECK_INTERVAL // 60}分钟后重新检查")
        time.sleep(FULLSCREEN_CHECK_INTERVAL)

    if rounds >= FULLSCREEN_MAX_WAIT_ROUNDS:
        print("Waited too long, proceeding anyway")


# ==================== 通知相关 ====================
def notify_simple(title, message):
    """普通一次性通知"""
    try:
        toast = Toast([title, message])
        toaster.show_toast(toast)
    except Exception as e:
        print(f"Notify failed (harmless): {e}")


def notify_click_to_open(title, message, target_path):
    """带点击跳转的通知，点击后用默认程序打开target_path"""
    try:
        toast = Toast([title, message])
        toast.on_activated = lambda _: os.startfile(os.path.abspath(target_path))
        toaster.show_toast(toast)
    except Exception as e:
        print(f"Notify failed (harmless): {e}")


def _rounded_rect_points(x1, y1, x2, y2, r):
    """返回圆角矩形的多边形顶点（配合 canvas smooth=True 使用），r 会自动夹在合理范围内"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]


class FloatingProgress:
    """右下角常驻悬浮小窗，无边框置顶、圆角卡片风格，用来展示单期处理的实时进度（下载/转录/翻译）。
    跑完（finish）后短暂停留（进度条变绿）再自动关闭，不占用操作中心空间。"""

    WIDTH = 360
    HEIGHT = 100

    BG_CARD = "#1e1f2b"
    BORDER = "#34364a"
    FG_TITLE = "#f2f3f8"
    FG_STAGE = "#9296ad"
    ACCENT_RUNNING = "#6c8cff"
    ACCENT_DONE = "#4ade80"
    TRACK = "#33354a"
    MASK = "#ff00ff"  # 用作透明遮罩色，抠出圆角窗外的区域

    def __init__(self, title):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", self.MASK)

        # 现在进程已是DPI感知的，这里的像素密度反映真实屏幕DPI；
        # 显式设置tk scaling并按比例放大窗口尺寸，避免高分屏下窗口物理尺寸变小、字体渲染错位
        dpi = self.root.winfo_fpixels("1i")
        self.scale = scale = dpi / 96.0
        self.root.tk.call("tk", "scaling", dpi / 72.0)

        width = int(self.WIDTH * scale)
        height = int(self.HEIGHT * scale)
        self.pad = pad = int(16 * scale)
        self.radius = int(14 * scale)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - width - int(16 * scale)
        y = screen_h - height - int(56 * scale)  # 避开任务栏
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.configure(bg=self.MASK)

        self.canvas = tk.Canvas(
            self.root, width=width, height=height,
            bg=self.MASK, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.canvas.create_polygon(
            _rounded_rect_points(1, 1, width - 1, height - 1, self.radius),
            smooth=True, fill=self.BG_CARD, outline=self.BORDER, width=1,
        )

        title_font = tkfont.Font(root=self.root, family="Segoe UI", size=10, weight="bold")
        stage_font = ("Segoe UI", 9)
        percent_font = ("Segoe UI", 9, "bold")

        # 按实际渲染像素宽度截断标题（而不是固定字符数），中英文字符宽度不一致，
        # 字符数截断要么切太早留白、要么切太晚被窗口边界裁掉一半字
        avail_w = width - pad * 2
        display_title = title
        if title_font.measure(display_title) > avail_w:
            while len(display_title) > 1 and title_font.measure(display_title + "…") > avail_w:
                display_title = display_title[:-1]
            display_title = display_title.rstrip() + "…"

        self.canvas.create_text(
            pad, int(16 * scale), text=display_title, anchor="w",
            font=title_font, fill=self.FG_TITLE,
        )

        stage_y = int(44 * scale)
        self.stage_item = self.canvas.create_text(
            pad, stage_y, text="准备中...", anchor="w",
            font=stage_font, fill=self.FG_STAGE,
        )
        self.percent_item = self.canvas.create_text(
            width - pad, stage_y, text="0%", anchor="e",
            font=percent_font, fill=self.FG_TITLE,
        )

        # 进度条轨道（圆角胶囊）+ 填充部分，独立于ttk主题，方便自由配色
        bar_h = int(6 * scale)
        bar_y1 = height - int(20 * scale)
        bar_y2 = bar_y1 + bar_h
        self.bar_x1 = pad
        self.bar_x2 = width - pad
        self.bar_y1 = bar_y1
        self.bar_y2 = bar_y2
        self.bar_r = bar_h / 2

        self.canvas.create_polygon(
            _rounded_rect_points(self.bar_x1, bar_y1, self.bar_x2, bar_y2, self.bar_r),
            smooth=True, fill=self.TRACK, outline="",
        )
        self.fill_item = self.canvas.create_polygon(
            _rounded_rect_points(self.bar_x1, bar_y1, self.bar_x1, bar_y2, self.bar_r),
            smooth=True, fill=self.ACCENT_RUNNING, outline="",
        )

        self.root.update()

    def _set_fill(self, progress_0_to_1, color):
        progress_0_to_1 = max(0.0, min(1.0, progress_0_to_1))
        fill_w = (self.bar_x2 - self.bar_x1) * progress_0_to_1
        x2 = self.bar_x1 + max(fill_w, self.bar_r * 2 * (1 if fill_w > 0 else 0))
        r = min(self.bar_r, fill_w / 2) if fill_w > 0 else 0
        points = _rounded_rect_points(self.bar_x1, self.bar_y1, max(x2, self.bar_x1), self.bar_y2, r)
        self.canvas.coords(self.fill_item, *points)
        self.canvas.itemconfig(self.fill_item, fill=color)

    def update(self, stage_text, progress_0_to_1):
        try:
            self.canvas.itemconfig(self.stage_item, text=stage_text)
            self.canvas.itemconfig(
                self.percent_item, text=f"{int(max(0, min(1, progress_0_to_1)) * 100)}%"
            )
            self._set_fill(progress_0_to_1, self.ACCENT_RUNNING)
            self.root.update()
        except Exception as e:
            print(f"Floating progress update failed (harmless): {e}")

    def finish(self, final_text):
        try:
            self.canvas.itemconfig(self.stage_item, text=final_text, fill=self.ACCENT_DONE)
            self.canvas.itemconfig(self.percent_item, text="100%")
            self._set_fill(1.0, self.ACCENT_DONE)
            self.root.update()
        except Exception as e:
            print(f"Floating progress finish failed (harmless): {e}")
        try:
            self.root.after(1200, self.root.destroy)
            self.root.mainloop()
        except Exception:
            pass

    def close_now(self):
        """立即关闭，不做"变绿完成"的效果——用于阶段切换（比如模型加载完成、马上进入下一步），
        而不是任务真正完成的场合"""
        try:
            self.root.destroy()
        except Exception:
            pass


class SpinnerProgress:
    """常驻悬浮窗，用一个转动的圆环表示"正在处理但没有具体进度可展示"的阶段
    （模型加载、逐个节目的RSS检查），一直转到真正开始下载才关闭。

    tk的窗口/控件必须只由创建它们的那个线程访问——早期版本让这个窗口自己在独立线程里
    开一个Tk()、跑自己的mainloop，而主线程后面又会创建FloatingProgress的Tk()，两边分属
    不同线程但同属一个进程，真实运行时会随机触发"Tcl_AsyncDelete: async handler deleted
    by the wrong thread"直接把整个进程崩掉（不是退出时才崩，运行期间随时可能发生，
    实测过哪怕整个下载/转录/翻译流程全部成功，也会在最后收尾阶段冒出这个崩溃）。

    改成这个类本身的Tk窗口和事件循环（mainloop）留在调用者所在的线程——也就是main()的
    主线程，跟脚本里其他所有Tk对象（FloatingProgress）保持同一个线程。真正阻塞的工作
    （模型加载、RSS请求）通过 run_with_work() 放到后台线程去跑，主线程只负责跑tk事件
    循环、驱动转圈动画，直到后台线程干完活。work_fn内部可以调用 spinner.set_stage(text)
    更新文字（线程安全，只是设置一个普通属性，不碰tk对象）。"""

    def __init__(self, title):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", FloatingProgress.MASK)

        dpi = self.root.winfo_fpixels("1i")
        scale = dpi / 96.0
        self.root.tk.call("tk", "scaling", dpi / 72.0)

        width = int(FloatingProgress.WIDTH * scale)
        height = int(FloatingProgress.HEIGHT * scale)
        pad = int(16 * scale)
        radius = int(14 * scale)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - width - int(16 * scale)
        y = screen_h - height - int(56 * scale)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.configure(bg=FloatingProgress.MASK)

        self.canvas = tk.Canvas(
            self.root, width=width, height=height,
            bg=FloatingProgress.MASK, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.canvas.create_polygon(
            _rounded_rect_points(1, 1, width - 1, height - 1, radius),
            smooth=True, fill=FloatingProgress.BG_CARD, outline=FloatingProgress.BORDER, width=1,
        )

        title_font = tkfont.Font(root=self.root, family="Segoe UI", size=10, weight="bold")
        stage_font = ("Segoe UI", 9)

        spinner_r = int(9 * scale)
        cx = width - pad - spinner_r
        cy = int(44 * scale)

        avail_w = width - pad * 2
        display_title = title
        if title_font.measure(display_title) > avail_w:
            while len(display_title) > 1 and title_font.measure(display_title + "…") > avail_w:
                display_title = display_title[:-1]
            display_title = display_title.rstrip() + "…"

        self.canvas.create_text(
            pad, int(16 * scale), text=display_title, anchor="w",
            font=title_font, fill=FloatingProgress.FG_TITLE,
        )
        self._pending_stage = "准备中..."
        self.stage_item = self.canvas.create_text(
            pad, int(44 * scale), text=self._pending_stage, anchor="w",
            font=stage_font, fill=FloatingProgress.FG_STAGE,
            width=(cx - spinner_r - pad) - pad,
        )
        self.spinner_item = self.canvas.create_arc(
            cx - spinner_r, cy - spinner_r, cx + spinner_r, cy + spinner_r,
            start=0, extent=270, style="arc",
            outline=FloatingProgress.ACCENT_RUNNING, width=max(2, int(2 * scale)),
        )

        self._angle = 0
        self._last_shown_stage = None
        self._done = False
        self.root.update()

    def set_stage(self, text):
        self._pending_stage = text

    def _tick(self):
        if self._done:
            self.root.quit()
            return
        try:
            self._angle = (self._angle - 24) % 360
            self.canvas.itemconfig(self.spinner_item, start=self._angle)
            if self._pending_stage != self._last_shown_stage:
                self.canvas.itemconfig(self.stage_item, text=self._pending_stage)
                self._last_shown_stage = self._pending_stage
        except Exception:
            self.root.quit()
            return
        self.root.after(50, self._tick)

    def run_with_work(self, work_fn):
        """在后台线程跑 work_fn()（无参数），当前线程留在这里跑tk事件循环驱动动画，
        直到 work_fn 结束才返回；work_fn 的返回值会被原样返回，它抛出的异常会在这里
        重新抛出（跨线程转发）。跑完（不管成功失败）这个悬浮窗都会被销毁。"""
        result = {"value": None, "error": None}

        def runner():
            try:
                result["value"] = work_fn()
            except Exception as e:
                result["error"] = e
            finally:
                self._done = True

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        self._tick()
        self.root.mainloop()
        thread.join()
        try:
            self.root.destroy()
        except Exception:
            pass
        if result["error"] is not None:
            raise result["error"]
        return result["value"]


# ==================== 工具函数 ====================
def sanitize_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title)[:80]


def load_latest_log():
    log = {}
    if os.path.exists(LATEST_LOG):
        with open(LATEST_LOG, "r", encoding="utf-8") as f:
            for line in f:
                if "::" in line:
                    show, title = line.strip().split("::", 1)
                    log[show] = title
    return log


def save_latest_log(log):
    with open(LATEST_LOG, "w", encoding="utf-8") as f:
        for show, title in log.items():
            f.write(f"{show}::{title}\n")


def resolve_apple_podcasts_feed(url):
    """如果传进来的是苹果播客链接（podcasts.apple.com/.../idNNNNNN），通过苹果公开的
    iTunes Lookup API查出这档播客真正的RSS地址并返回；不是苹果链接就原样返回，调用方
    不需要先判断类型，直接无脑传进来就行。苹果播客本身也是靠RSS分发的，只是套了一层
    自己的网页/App壳子——这个查询接口是苹果公开、免注册、免Key的。"""
    match = re.search(r"podcasts\.apple\.com/[^\s]*?/id(\d+)", url)
    if not match:
        return url
    podcast_id = match.group(1)
    resp = requests.get(f"https://itunes.apple.com/lookup?id={podcast_id}", timeout=15, headers=HEADERS)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results or not results[0].get("feedUrl"):
        raise ValueError("这个苹果播客ID没有查到对应的RSS地址（可能不是播客节目，或者苹果没公开这个字段）")
    return results[0]["feedUrl"]


def get_all_episodes(rss_url):
    """取整个RSS feed的完整历史条目列表（不只是最新一条）。自己用requests取内容再交给
    feedparser解析（而不是让feedparser直接拿URL），因为feedparser内置的URL抓取不设超时，
    网络卡住时会无限期挂住"""
    rss_url = resolve_apple_podcasts_feed(rss_url)
    r = requests.get(rss_url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    return feed.entries


def get_newest_episode(rss_url):
    entries = get_all_episodes(rss_url)
    if not entries:
        return None
    return entries[0]


def download_audio(url, filepath, retry_rss_url=None, max_retries=2, on_progress=None):
    """下载音频，带字节进度条 + 失败重试。on_progress(0~1) 可选，用于回报下载百分比"""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, stream=True, timeout=60, headers=HEADERS)
            r.raise_for_status()
            total_size = int(r.headers.get("content-length", 0))

            if total_size > 0 and total_size < 1000:
                raise ValueError(f"返回内容异常小（{total_size}字节），可能是错误响应而非音频")

            downloaded = 0
            with open(filepath, "wb") as f, tqdm(
                desc="下载进度",
                total=total_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
            ) as bar:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    bar.update(len(chunk))
                    downloaded += len(chunk)
                    if on_progress and total_size > 0:
                        on_progress(downloaded / total_size)
            return

        except (requests.exceptions.RequestException, ValueError) as e:
            # RequestException覆盖超时/连接中断等网络异常，不再只捕获HTTPError——
            # 网络抖动这种最该重试的场景，之前反而落不到这个重试分支里
            print(f"Download failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                print("Waiting 60s before retry...")
                time.sleep(60)
                if retry_rss_url:
                    newest = get_newest_episode(retry_rss_url)
                    if newest and newest.enclosures:
                        url = newest.enclosures[0].href
            else:
                print(f"Failed after {max_retries} retries, skipping this episode")
                raise


def download_via_ytdlp(url, dest_dir, on_progress=None):
    """从YouTube/B站等yt-dlp支持的网站链接下载纯音频轨，存到dest_dir下（文件名由yt-dlp
    自己根据视频标题决定，不是提前定好的），返回实际生成的文件完整路径。

    只下音频不下视频（format选bestaudio），而且不额外转码成mp3——faster-whisper底层用
    PyAV解码（跟着faster-whisper一起装的av这个包自带FFmpeg），本来就能直接吃yt-dlp下载
    下来的原始格式（webm/m4a等），没必要在这一步额外转一遍格式，省时间也不用额外装ffmpeg。

    yt-dlp自己根据链接域名识别是哪个网站，YouTube/B站等它支持的站点通用同一套调用方式，
    不需要在这里区分是哪个平台。"""
    import yt_dlp

    result_path = {"value": None}

    def hook(d):
        if d["status"] == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)
        elif d["status"] == "finished":
            result_path["value"] = d["filename"]

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(dest_dir, "%(title).100s.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,  # 链接如果是播放列表里的一个视频，只下这一个，不是整个列表
    }
    if YTDLP_COOKIES_BROWSER:
        ydl_opts["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    if not result_path["value"] or not os.path.exists(result_path["value"]):
        raise RuntimeError("yt-dlp下载完成但没能定位到生成的文件，可能是链接不受支持")
    return result_path["value"]


def transcribe_audio(model, audio_path, language="auto", on_progress=None):
    """转录，返回(带时间戳的逐句列表, 识别到的语言)。on_progress(0~1) 每次进度有明显变化时回调。
    language: "auto"（交给whisper自动识别）/"en"/"zh"（跳过识别，直接按指定语言转录，短音频或
    中英混杂时手动指定更准）。只支持中英两种语言——非中文一律按"en"分支处理（存进en字段），
    这样后续翻译/字幕/播放页的逻辑不用为更多语言分支。"""
    segments, info = model.transcribe(
        audio_path,
        language=None if language == "auto" else language,
        batch_size=4,
        vad_filter=True,
        initial_prompt=WHISPER_INITIAL_PROMPT or None,
        word_timestamps=True,  # 词级时间戳，给字幕页逐词高亮用；实测在batched管线上也正常产出
    )
    detected_lang = language if language != "auto" else (info.language or "en")
    is_zh = detected_lang == "zh"
    total_duration = info.duration
    result = []
    last_reported = -1

    with tqdm(desc="转录进度", total=round(total_duration, 1), unit="秒") as bar:
        last_end = 0
        for seg in segments:
            text = seg.text.strip()
            entry = {
                "start": seg.start,
                "end": seg.end,
                "en": "" if is_zh else text,
                "zh": text if is_zh else "",
            }
            # 词级时间戳只有Whisper引擎有（SenseVoice是按VAD段整段出文本，没有词边界）。
            # 用短key（w/s/e）压JSON体积；这些词拼起来正好等于上面的text，对应的是"主行"
            # 那一行（英文源就是en，中文源就是zh），字幕页拿它做逐词卡拉OK式高亮
            if seg.words:
                entry["words"] = [
                    {"w": w.word, "s": round(w.start, 2), "e": round(w.end, 2)}
                    for w in seg.words
                ]
            result.append(entry)
            bar.update(round(seg.end - last_end, 1))
            last_end = seg.end

            if on_progress and total_duration > 0:
                percent = last_end / total_duration
                # 每变化超过2%才回调一次，避免通知更新过于频繁
                if percent - last_reported >= 0.02:
                    last_reported = percent
                    on_progress(min(percent, 1.0))

        bar.update(bar.total - bar.n)  # 补满进度条（VAD跳过静音段导致总时长对不齐）

    if on_progress:
        on_progress(1.0)

    return result, detected_lang


# Whisper的segment边界经常从句子中间断开（VAD切分/解码窗口决定的，不是按语义），词级时间戳
# 打开时(word_timestamps=True)才有机会重新按真实句子边界分组。规则实测过（真实6分钟新闻播客、
# 847个词）：句末标点后的间隔中位数只有0.6秒、10分位数只有0.24秒，句中间隔99%分位数也只有
# 0.46秒——两类间隔分布严重重叠，单靠间隔阈值切不出可靠边界，标点必须是主要依据，间隔只能当
# 兜底（且要求已经攒了一定长度，避免把正常语速停顿处硬切开）
REGROUP_SENTENCE_END_CHARS = set(".?!。？！")
REGROUP_MAX_CHARS = 160  # 滚动阅读页是普通div，浏览器里长句自然换行，没有Buzz那种"必须塞进
# 单屏视频字幕"的硬约束，不需要照抄它的42字符。而且regroup跑在翻译之前，切得越碎，喂给
# DeepSeek的就越是残句、译文质量越受影响——所以这个上限应该尽量宽松，只用来兜底极端的
# 长句，不是常规切分手段。实测关掉上限看自然句长分布（同一期访谈类节目）：中位数119字符、
# 90分位163字符，160能让约九成句子完整过关，只截断极少数的长尾
REGROUP_LONG_GAP_SECONDS = 1.5  # 实测847词里只有1处间隔超过这个数，基本都是真实停顿/转场
REGROUP_MIN_CHARS_FOR_GAP_SPLIT = 20  # 停顿再长，攒的内容太短也不当句子边界，避免切出残句

# 只看"最后一个字符是不是句末标点"这个简单规则，会把常见缩写后面的句点误判成句子结束
# （比如"Mr. Trump"从Mr.这里就断了）——Buzz自己的正则实现也有这个问题，不是我们独有的。
# 加一个小黑名单缓解最常见的几种，不追求覆盖所有缩写（真做到需要完整的NLP分句器，这里
# 只是新闻播客里常见的那几类：称谓、国家代号、月份缩写）
REGROUP_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "gen.", "sen.", "rep.", "gov.", "pres.",
    "u.s.", "u.k.", "u.n.", "e.g.", "i.e.", "vs.", "etc.", "no.", "vol.",
    "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.",
}


def regroup_words_into_sentences(segments, is_zh):
    """把transcribe_audio()产出的词级数据，按真实句子边界重新分组，替换掉Whisper自己那些从
    句子中间断开的segment。SenseVoice没有词级时间戳（segments里没有"words"字段），原样返回，
    不生效——这个函数只对Whisper引擎起作用。

    单趟前向累加：逐词加入当前句，遇到句末标点/超长/长停顿兜底就收尾，不是"先按间隔切、
    再按标点切"这种多趟独立操作叠加——避免几条规则互相打架，边界优先级更好控制。"""
    words = []
    for seg in segments:
        if seg.get("words"):
            words.extend(seg["words"])
    if not words:
        return segments

    result = []
    current_words = []
    current_text = ""

    def flush():
        if not current_words:
            return
        text = current_text.strip()
        result.append({
            "start": current_words[0]["s"],
            "end": current_words[-1]["e"],
            "en": "" if is_zh else text,
            "zh": text if is_zh else "",
            "words": current_words,
        })

    for i, w in enumerate(words):
        # whisper有些复合词会拆成两个token，第二个没有前导空格、是直接接在上一个词后面的
        # 延续（比如"so"和"-called"两个token拼成"so-called"）——不是这个词是不是新句子的
        # 信号，只是"这个token算不算一个真正的新词开头"。长停顿兜底/超长兜底都只能在真正
        # 新词开头处断，不然会像这次实测(Sinica那期)踩到的一样，把"so-called"从中间切成
        # "...these so" / "-called low-altitude..."两行，肉眼可见地断错地方
        is_real_word_start = w["w"].startswith(" ") or is_zh
        gap = w["s"] - words[i - 1]["e"] if i > 0 else 0.0

        if current_words and is_real_word_start:
            if (
                gap > REGROUP_LONG_GAP_SECONDS
                and len(current_text.strip()) >= REGROUP_MIN_CHARS_FOR_GAP_SPLIT
            ) or len(current_text.strip()) >= REGROUP_MAX_CHARS:
                flush()
                current_words, current_text = [], ""

        current_words.append(w)
        # w["w"]是whisper自己给出的token，英文本来就带前导空格（" Why"/" get"这样），
        # 拼接不用再插分隔符，插了反而重复空格；中文token一般不带前导空格，也不需要分隔符
        current_text += w["w"]

        stripped = w["w"].strip()
        ends_sentence = (
            bool(stripped)
            and stripped[-1] in REGROUP_SENTENCE_END_CHARS
            and stripped.lower() not in REGROUP_ABBREVIATIONS
        )

        if ends_sentence:
            flush()
            current_words, current_text = [], ""

    flush()
    return result


TRANSLATION_BASE_SYSTEM_PROMPT = (
    "你是专业的新闻播客翻译。下面是带编号的英文字幕行，请将每一行翻译成通顺准确的中文，"
    "严格按照相同的编号格式逐行返回翻译结果，不要合并或拆分行，不要添加编号之外的任何说明。"
)


def translate_segments(segments, on_progress=None):
    """按批次翻译逐句文本，保留时间戳对应关系。on_progress(0~1) 按批次汇报进度"""
    batches = [
        segments[i:i + SEGMENTS_PER_TRANSLATION_BATCH]
        for i in range(0, len(segments), SEGMENTS_PER_TRANSLATION_BATCH)
    ]
    total_batches = len(batches)

    system_prompt = TRANSLATION_BASE_SYSTEM_PROMPT
    if TRANSLATION_EXTRA_SYSTEM_PROMPT:
        # 针对某个具体节目的专有名词纠错之类的补充说明，在config.json里按需填写，
        # 通用逻辑不写死任何特定播客的内容
        system_prompt += "\n" + TRANSLATION_EXTRA_SYSTEM_PROMPT

    for batch_idx, batch in enumerate(tqdm(batches, desc="翻译进度", unit="批")):
        numbered_input = "\n".join(f"{i+1}. {seg['en']}" for i, seg in enumerate(batch))

        translated_text = None
        for attempt in range(TRANSLATE_MAX_RETRIES + 1):
            try:
                response = translation_client.chat.completions.create(
                    model=TRANSLATION_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": numbered_input},
                    ],
                    timeout=TRANSLATE_TIMEOUT_SECONDS,
                )
                translated_text = response.choices[0].message.content
                break
            except Exception as e:
                print(f"Translation batch {batch_idx + 1}/{total_batches} failed (attempt {attempt + 1}): {e}")
                if attempt < TRANSLATE_MAX_RETRIES:
                    time.sleep(5)

        translated_lines = {}
        if translated_text:
            for line in translated_text.strip().split("\n"):
                match = re.match(r"^\s*(\d+)[.\、]\s*(.+)$", line)
                if match:
                    idx, zh_text = match.groups()
                    translated_lines[int(idx)] = zh_text.strip()
        else:
            print(f"Translation batch {batch_idx + 1}/{total_batches} gave up after "
                  f"{TRANSLATE_MAX_RETRIES + 1} attempts, leaving this batch untranslated")

        for i, seg in enumerate(batch):
            seg["zh"] = translated_lines.get(i + 1, "")  # 找不到对应翻译（含请求彻底失败）时留空，不阻断流程

        if on_progress:
            on_progress((batch_idx + 1) / total_batches)

    return segments


def save_episode_meta(meta, episode_dir):
    """存一份期数元数据（发布时间等），播客库列表要靠这个按时间排序/分组显示——RSS的
    itunes:episode（真正的"第几期"编号）几乎没有播客在用（实测两个真实feed都没有），
    没有更靠谱的数据源，只能退而求其次记发布时间。手动导入的本地音频没有RSS发布时间，
    published留空，用processed_at（导入时刻）兜底排序。"""
    with open(os.path.join(episode_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def save_text_files(segments, en_path, zh_path):
    """保存纯英文稿和纯中文稿两个txt文件"""
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(" ".join(seg["en"] for seg in segments))

    with open(zh_path, "w", encoding="utf-8") as f:
        f.write(" ".join(seg["zh"] for seg in segments if seg["zh"]))


def _format_timestamp(seconds, decimal_mark):
    """把秒数转成字幕时间戳格式 HH:MM:SS<分隔符>mmm。SRT用逗号分隔毫秒，VTT用句点，
    两种格式其它部分完全一样，所以共用同一个函数，靠decimal_mark参数区分"""
    total_ms = round(seconds * 1000)
    hours, rem_ms = divmod(total_ms, 3600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal_mark}{ms:03d}"


def _subtitle_cue_text(seg):
    """英文+中文各一行；某一行缺失（翻译失败，或源语言本来就是中文没有英文）就只留有内容的那行，
    不留空行占位。有说话人标签（开了说话人分离）就再加一行放在最前面"""
    lines = [line for line in (seg["en"], seg["zh"]) if line]
    if seg.get("speaker"):
        lines = [f"[{seg['speaker']}]"] + lines
    return "\n".join(lines)


def generate_srt(segments, output_path):
    """标准SRT字幕，可以直接拖进大部分视频播放器/剪辑软件，一句英文一句中文对照"""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start = _format_timestamp(seg["start"], ",")
            end = _format_timestamp(seg["end"], ",")
            f.write(f"{i}\n{start} --> {end}\n{_subtitle_cue_text(seg)}\n\n")


def generate_vtt(segments, output_path):
    """WebVTT字幕，跟SRT内容一样，格式上是网页<track>标签和部分播放器更认的那种"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for i, seg in enumerate(segments, start=1):
            start = _format_timestamp(seg["start"], ".")
            end = _format_timestamp(seg["end"], ".")
            f.write(f"{i}\n{start} --> {end}\n{_subtitle_cue_text(seg)}\n\n")


def generate_html(title, audio_filename, segments, output_path):
    """生成带音频播放器的双语字幕滚动页面"""
    data_json = json.dumps(segments, ensure_ascii=False)

    html_template = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: "Segoe UI", -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    max-width: 760px; margin: 0 auto; padding: 0 24px 40px; background: #f7f7f7;
  }
  #sticky-header { position: sticky; top: 0; background: #f7f7f7; padding: 18px 0 14px; z-index: 10; }
  h1 { font-size: 17px; color: #222; margin: 0 0 12px; font-weight: 600; line-height: 1.4; }
  audio { width: 100%; display: block; border-radius: 999px; }
  audio::-webkit-media-controls-panel { background-color: #fff; }
  #transcript { margin-top: 8px; }
  .line { padding: 16px 18px; margin-bottom: 10px; border-radius: 10px; cursor: pointer; transition: background 0.2s; }
  .line:hover { background: #eee; }
  .line.active { background: #fff3cd; }
  .time { color: #aaa; font-size: 12px; margin-right: 8px; font-variant-numeric: tabular-nums; }
  .speaker { font-size: 12px; font-weight: 600; margin-right: 8px; }
  /* 逐词高亮：整句用浅黄底(.line.active)标出当前句，当前正在念的那个词再叠一层更深的琥珀色 */
  .w { border-radius: 3px; transition: background 0.1s; }
  .w-active { background: #ffce54; }
  .en { color: #2b2b2b; font-size: 16px; line-height: 1.7; letter-spacing: 0.1px; }
  .zh { color: #666; font-size: 15px; line-height: 1.9; margin-top: 8px; letter-spacing: 0.3px; }
</style>
</head>
<body>
  <div id="sticky-header">
    <h1>__TITLE__</h1>
    <audio id="audio" controls src="__AUDIO_FILENAME__"></audio>
  </div>
  <div id="transcript"></div>

<script>
const segments = __DATA_JSON__;
const audio = document.getElementById("audio");
const transcript = document.getElementById("transcript");

function formatTime(s) {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

// 说话人按名字哈希到固定的几个颜色上，同一个人从头到尾颜色一致；具体是哪几个说话人
// 生成HTML的时候还不知道（取决于这一集实际识别出几个人），所以只能在前端动态分配
const SPEAKER_COLORS = ["#4f8ef7", "#e8823f", "#3fae6a", "#a862d6", "#d64f8f", "#5aa8a8"];
function speakerColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return SPEAKER_COLORS[hash % SPEAKER_COLORS.length];
}

segments.forEach((seg, i) => {
  const div = document.createElement("div");
  div.className = "line";
  div.id = "line-" + i;
  // 哪个字段有内容就用哪个当主行（大号黑字）；源语言本来就是中文时en是空的，
  // 这时zh要顶替成主行，而不是照旧当小号灰字的翻译行
  const primary = seg.en || seg.zh;
  const secondary = (seg.en && seg.zh) ? seg.zh : "";
  const speakerHtml = seg.speaker
    ? `<span class="speaker" style="color:${speakerColor(seg.speaker)}">${seg.speaker}</span>` : "";
  // 有词级时间戳（Whisper引擎）就把主行拆成一个个<span>好逐词高亮；没有（SenseVoice，
  // 或者旧数据）就照旧整行渲染，降级成只有整句高亮，不影响使用
  let primaryHtml;
  if (seg.words && seg.words.length) {
    primaryHtml = seg.words.map((w, j) => `<span class="w" id="w-${i}-${j}">${w.w}</span>`).join("");
  } else {
    primaryHtml = primary;
  }
  div.innerHTML = `<span class="time">${formatTime(seg.start)}</span>${speakerHtml}
                    <div class="en">${primaryHtml}</div>
                    <div class="zh">${secondary}</div>`;
  div.onclick = () => { audio.currentTime = seg.start; audio.play(); };
  transcript.appendChild(div);
});

let currentIndex = -1;
let currentWordEl = null;
function clearWord() {
  if (currentWordEl) { currentWordEl.classList.remove("w-active"); currentWordEl = null; }
}
audio.addEventListener("timeupdate", () => {
  const t = audio.currentTime;
  const idx = segments.findIndex((seg, i) => {
    const next = segments[i + 1];
    return t >= seg.start && (!next || t < next.start);
  });
  if (idx === -1) return;
  if (idx !== currentIndex) {
    if (currentIndex !== -1) {
      document.getElementById("line-" + currentIndex).classList.remove("active");
    }
    clearWord();  // 换句了，先清掉上一句里残留的词高亮
    document.getElementById("line-" + idx).classList.add("active");
    document.getElementById("line-" + idx).scrollIntoView({ behavior: "smooth", block: "center" });
    currentIndex = idx;
  }
  // 当前句里定位正在念的词：取"起始时间已经<=当前播放位置"的最后一个词。用这种"最后一个
  // 已开始的词"而不是"落在[s,e)区间里的词"，是因为词与词之间可能有间隙、也可能出现零时长
  // 的词（whisper偶尔给出s==e），区间判断会在这些位置漏高亮，这种判据不会
  const seg = segments[idx];
  if (seg.words && seg.words.length) {
    let wj = -1;
    for (let j = 0; j < seg.words.length; j++) {
      if (seg.words[j].s <= t) wj = j; else break;
    }
    const wel = wj === -1 ? null : document.getElementById(`w-${idx}-${wj}`);
    if (wel !== currentWordEl) {
      clearWord();
      if (wel) { wel.classList.add("w-active"); currentWordEl = wel; }
    }
  } else {
    clearWord();
  }
});
</script>
</body>
</html>
"""

    html_content = (
        html_template
        .replace("__TITLE__", title)
        .replace("__AUDIO_FILENAME__", audio_filename)
        .replace("__DATA_JSON__", data_json)
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)


def _check_show(show_name, rss_url, latest_log):
    """检查一个节目的RSS，没有新一期就返回None，有的话返回这一集待处理的信息。"""
    show_dir = os.path.join(EPISODES_DIR, show_name)
    os.makedirs(show_dir, exist_ok=True)

    try:
        newest = get_newest_episode(rss_url)
    except Exception as e:
        print(f"RSS parse failed: {e}")
        notify_simple(f"「{show_name}」抓取失败", f"RSS解析出错: {e}"[:100])
        return None

    if newest is None:
        print("RSS empty, skipping")
        return None

    current_title = newest.title
    recorded_title = latest_log.get(show_name)

    if current_title == recorded_title:
        print(f"No update, latest is still: {current_title}")
        notify_simple(f"「{show_name}」检查完成", f"暂无新一期，最新仍是：{current_title[:50]}")
        return None

    print(f"New episode found: {current_title}")
    notify_simple(f"「{show_name}」发现新一期", f"{current_title[:60]}，开始下载和处理")

    if not newest.enclosures:
        print("No audio link, skipping")
        return None
    audio_url = newest.enclosures[0].href

    safe_title = sanitize_filename(current_title)
    episode_dir = os.path.join(show_dir, safe_title)
    os.makedirs(episode_dir, exist_ok=True)

    return {
        "audio_url": audio_url,
        "current_title": current_title,
        "episode_dir": episode_dir,
        "published": newest.get("published", ""),
    }


def _ensure_whisper_model_downloaded():
    """本地没有模型文件的话，WhisperModel(...)构造时会交给faster-whisper自己内部去下载——
    但那条路径没有重试/兜底，实测HF镜像偶尔在个别文件上卡住会直接导致整个加载失败。这里在
    构造WhisperModel之前先用跟设置页「下载此模型」同一套重试+直接GET兜底的逻辑保证下载完整，
    不依赖faster-whisper自己那条不够健壮的下载路径"""
    if _LOCAL_MODEL_READY:
        return
    from huggingface_hub import HfApi, snapshot_download

    repo_id = f"Systran/faster-whisper-{_WHISPER_MODEL_SIZE}"
    os.makedirs(_LOCAL_MODEL_DIR, exist_ok=True)
    try:
        all_files = [f for f in HfApi().list_repo_files(repo_id) if f != ".gitattributes"]
    except Exception:
        all_files = None

    last_error = None
    for _attempt in range(4):
        try:
            snapshot_download(repo_id=repo_id, local_dir=_LOCAL_MODEL_DIR)
            last_error = None
        except Exception as e:
            last_error = e
        if all_files and not [f for f in all_files if not os.path.exists(os.path.join(_LOCAL_MODEL_DIR, f))]:
            last_error = None
            break

    missing = [f for f in all_files if not os.path.exists(os.path.join(_LOCAL_MODEL_DIR, f))] if all_files else []
    for filename in missing:
        url = f"{os.environ['HF_ENDPOINT']}/{repo_id}/resolve/main/{filename}"
        _download_file(url, os.path.join(_LOCAL_MODEL_DIR, filename))
    missing = [f for f in all_files if not os.path.exists(os.path.join(_LOCAL_MODEL_DIR, f))] if all_files else []

    if missing:
        raise RuntimeError(f"以下Whisper模型文件始终下载不全，请稍后重试：{missing}")
    if last_error and not all_files:
        raise last_error


def _load_model():
    """加载GPU模型，返回(model, batched_model)。跑在后台线程（配合spinner.run_with_work）。"""
    _ensure_whisper_model_downloaded()
    # MODEL_PATH是模块导入时算好的，那时候本地可能还没有模型文件；下载可能是刚在上面这行
    # 才发生的，不能沿用那个导入时就定死的旧值，得看现在_LOCAL_MODEL_DIR里是不是真的有了
    model_path = _LOCAL_MODEL_DIR if os.path.exists(os.path.join(_LOCAL_MODEL_DIR, "model.bin")) else MODEL_PATH
    model = WhisperModel(model_path, device="cuda", compute_type="float16")
    batched_model = BatchedInferencePipeline(model=model)
    return model, batched_model


def load_engine_model(engine):
    """按引擎名加载对应的模型会话（whisper走GPU，sensevoice走纯CPU/ONNX，互不依赖）"""
    if engine == "sensevoice":
        return _load_sensevoice_model()
    return _load_model()


def run_transcribe(engine, session, audio_path, language="auto", on_progress=None):
    """按引擎名分发到对应的转录函数，接口统一返回(segments, detected_lang)"""
    if engine == "sensevoice":
        return transcribe_audio_sensevoice(session, audio_path, language=language, on_progress=on_progress)
    _model, batched_model = session
    return transcribe_audio(batched_model, audio_path, language=language, on_progress=on_progress)


def sensevoice_model_ready():
    return all(os.path.exists(os.path.join(SENSEVOICE_MODEL_DIR, f)) for f in SENSEVOICE_MODEL_FILES)


def download_sensevoice_model(tqdm_class=None):
    """下载SenseVoice的ONNX模型文件（一次性，之后离线可用）。全程纯CPU/ONNX Runtime，
    不涉及CUDA，跟whisper的GPU模型下载是完全独立的两套东西。
    实测HF镜像偶尔会在几个KB大小的小文件上一直卡在HEAD请求这一步（snapshot_download内部靠
    HEAD判断文件是否存在/要不要重新下），同一个文件重试多少次都一样会卡；直接用requests对
    resolve/main/<文件名>这个URL发GET请求反而每次都成功——绕开了HEAD这一步，所以retry几轮
    snapshot_download还是缺文件的话，最后退化成直接GET每个缺失文件"""
    from huggingface_hub import snapshot_download

    os.makedirs(SENSEVOICE_MODEL_DIR, exist_ok=True)
    missing = SENSEVOICE_MODEL_FILES
    last_error = None
    for attempt in range(4):
        try:
            snapshot_download(
                repo_id=SENSEVOICE_REPO,
                local_dir=SENSEVOICE_MODEL_DIR,
                allow_patterns=missing,
                tqdm_class=tqdm_class,
            )
        except Exception as e:
            last_error = e
        missing = [f for f in SENSEVOICE_MODEL_FILES
                   if not os.path.exists(os.path.join(SENSEVOICE_MODEL_DIR, f))]
        if not missing:
            return
    for filename in missing:
        try:
            url = f"{os.environ['HF_ENDPOINT']}/{SENSEVOICE_REPO}/resolve/main/{filename}"
            _download_file(url, os.path.join(SENSEVOICE_MODEL_DIR, filename))
        except Exception as e:
            last_error = e
    missing = [f for f in SENSEVOICE_MODEL_FILES
               if not os.path.exists(os.path.join(SENSEVOICE_MODEL_DIR, f))]
    if missing:
        raise RuntimeError(f"以下模型文件始终下载不全，请稍后重试：{missing}") from last_error


def diarization_model_ready():
    return os.path.exists(DIARIZATION_SEGMENTATION_FILE) and os.path.exists(DIARIZATION_EMBEDDING_FILE)


def _download_file(url, dest_path, on_progress=None):
    """通用的流式下载+字节进度，跟download_audio()是同一个模式，抽出来给非音频文件复用"""
    r = requests.get(url, stream=True, timeout=60, headers=HEADERS)
    r.raise_for_status()
    total_size = int(r.headers.get("content-length", 0))
    downloaded = 0
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            if on_progress and total_size > 0:
                on_progress(downloaded / total_size)


def download_diarization_model(on_progress=None):
    """下载说话人分离要用的两个模型：pyannote分割模型（判断"什么时候有人在说话、说话人有没有
    切换"）+ 3D-Speaker声纹embedding模型（判断"是谁在说话"）。全部走GitHub Releases直链下载，
    不需要HuggingFace账号/token，是跟whisper/SenseVoice完全独立的第三套模型。"""
    os.makedirs(DIARIZATION_MODEL_DIR, exist_ok=True)

    if not os.path.exists(DIARIZATION_SEGMENTATION_FILE):
        seg_archive = os.path.join(DIARIZATION_MODEL_DIR, "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
        _download_file(DIARIZATION_SEGMENTATION_URL, seg_archive, on_progress)
        with tarfile.open(seg_archive) as tf:
            tf.extractall(DIARIZATION_MODEL_DIR)
        os.remove(seg_archive)

    if not os.path.exists(DIARIZATION_EMBEDDING_FILE):
        _download_file(DIARIZATION_EMBEDDING_URL, DIARIZATION_EMBEDDING_FILE, on_progress)

    if not diarization_model_ready():
        raise RuntimeError("说话人分离模型下载后校验未通过，请重试")


# 自动判断模式（说话人数量留空）下的硬上限：真实测试中，一集25分钟的双人播客靠threshold
# 自动判断，哪怕threshold开到API上限1.0，还是能拆出15个"说话人"，更极端的配置下测出过57个。
# 这不是能靠调参根治的问题——长播客里穿插的广告、引语原声、客座嘉宾本来就是不同的真实
# 人声，聚类算法把它们分开有它的道理，只是不符合"只有几个固定说话人"这个预期。这里加一道
# 兜底：自动判断的结果一旦超过这个上限，就退化成固定聚类数重新跑一遍——不保证这个数字更
# 准确，但至少不会再出现几十个说话人这种明显失控的结果
MAX_AUTO_SPEAKERS = 8


def _load_diarization_model(num_clusters_override=None):
    """加载说话人分离会话，跟_load_sensevoice_model()是并列的第三条模型加载路径，
    纯CPU/ONNX，不依赖GPU。num_clusters_override用于MAX_AUTO_SPEAKERS兜底重跑，
    不传时用配置里的DIARIZATION_NUM_SPEAKERS"""
    import sherpa_onnx

    num_clusters = DIARIZATION_NUM_SPEAKERS if num_clusters_override is None else num_clusters_override

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        # 分割模型和embedding模型的num_threads各自默认是1（单线程！），实测一小时的播客
        # 单线程跑要三四十分钟，比转录本身还慢；跟SenseVoice一样开4线程，实测能提速到几分钟内
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=DIARIZATION_SEGMENTATION_FILE
            ),
            num_threads=4,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=DIARIZATION_EMBEDDING_FILE, num_threads=4),
        # threshold=0.5（sherpa-onnx的默认值）在真实长音频上严重失控：拿一集25分钟的双人播客
        # 实测扫过0.5/0.7/0.8/0.9/1.0整个区间，"说话人"数量是57/34/29/20/15——同一个人的不同
        # 片段被拆成几十个不同的人，而且哪怕把threshold开到API允许的上限1.0，还是收敛不到
        # 播客实际的2个人。也就是说：不知道确切说话人数量时，靠threshold自动判断这条路本身
        # 就有明显上限，不是随便调个数字就能修好的，这里选一个稍微不那么离谱的默认值，但
        # 治标不治本。真正可靠的解法是DIARIZATION_NUM_SPEAKERS不留空——同一集音频直接指定
        # num_clusters=2实测精确聚成2个说话人，跟threshold那条路完全不是一个量级的准确率。
        # 设置页因此改成更明确建议"知道确切人数就填，别指望自动判断"
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_clusters, threshold=1.0),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("说话人分离模型配置校验失败，请确认models/diarization下的模型文件完整")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def _decode_mono_16k_array(audio_path):
    """解码任意格式音频成16kHz单声道float32 numpy数组——说话人分离模型直接吃这个格式的数组输入，
    不像SenseVoice那边的VAD要求喂文件路径"""
    import av
    import numpy as np

    container = av.open(audio_path)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    for frame in container.decode(audio=0):
        for rframe in resampler.resample(frame):
            chunks.append(rframe.to_ndarray())
    container.close()
    pcm = np.concatenate(chunks, axis=1).flatten()
    return pcm.astype(np.float32) / 32768.0


def diarize_audio(sd, audio_path, on_progress=None):
    """跑说话人分离，返回[(start, end, speaker_id), ...]，时间单位秒。这一步完全不产生/
    修改任何转录时间戳，只是对同一段音频单独做一次分析，出来的结果之后靠assign_speakers()
    按时间重叠去匹配转录segment"""
    waveform = _decode_mono_16k_array(audio_path)
    if sd.sample_rate != 16000:
        raise RuntimeError(f"说话人分离模型要求采样率{sd.sample_rate}Hz，跟解码逻辑假设的16000不一致")

    def _cb(num_processed_chunk, num_total_chunks):
        if on_progress and num_total_chunks:
            on_progress(num_processed_chunk / num_total_chunks)
        return 0

    result = sd.process(waveform, callback=_cb).sort_by_start_time()

    # 自动判断模式下，结果超过MAX_AUTO_SPEAKERS就整段重跑一遍，改用固定聚类数兜底。
    # 这里只在DIARIZATION_NUM_SPEAKERS本来就是-1（自动判断）时触发——用户已经手动指定
    # 人数的话，不管结果是什么都不应该再被这道兜底逻辑覆盖
    speaker_count = len(set(r.speaker for r in result))
    if DIARIZATION_NUM_SPEAKERS == -1 and speaker_count > MAX_AUTO_SPEAKERS:
        fallback_sd = _load_diarization_model(num_clusters_override=MAX_AUTO_SPEAKERS)
        result = fallback_sd.process(waveform, callback=_cb).sort_by_start_time()

    return [(r.start, r.end, r.speaker) for r in result]


def assign_speakers(segments, diar_result):
    """给每句转录segment按时间重叠度贴说话人标签，不改动segment原有的start/end时间戳。

    diar_result里不同说话人的区间本来就允许互相重叠——分割模型是按帧多标签判断"这一刻有哪几个人
    在说话"，真实的抢话/插话会体现成两个不同说话人的区间在时间上重叠，这不是识别错误，是模型真的
    识别到了同时说话。之前的实现只取重叠时长最大的那一个（赢者通吃），重叠里另一个说话人的信息
    虽然diar_result里明明有、却被直接丢掉了——这里改成把重叠占比超过阈值的说话人都保留下来，
    多人重叠时标成"说话人1、说话人2"这样，不再假装只有一个人在说"""
    OVERLAP_RATIO_THRESHOLD = 0.2  # 重叠时长占这句话自身时长的比例，低于这个当噪声/边界误差不计入
    for seg in segments:
        seg_duration = seg["end"] - seg["start"]
        overlaps = [
            (speaker_id, min(seg["end"], d_end) - max(seg["start"], d_start))
            for d_start, d_end, speaker_id in diar_result
        ]
        overlaps = [(spk, ov) for spk, ov in overlaps if ov > 0]
        if not overlaps or seg_duration <= 0:
            seg["speaker"] = ""
            continue
        overlaps.sort(key=lambda x: -x[1])
        significant = {spk for spk, ov in overlaps if ov / seg_duration >= OVERLAP_RATIO_THRESHOLD}
        if not significant:
            significant = {overlaps[0][0]}  # 都没到阈值的话至少保留重叠最多的那个，不留空
        seen = set()
        ordered = [spk for spk, _ in overlaps if spk in significant and not (spk in seen or seen.add(spk))]
        seg["speaker"] = "、".join(f"说话人{spk + 1}" for spk in ordered)
    return segments


_SENSEVOICE_LANG_CODES = {"auto": 0, "zh": 3, "en": 4}
# 输出文本开头会带一串特殊token，比如"<|zh|><|NEUTRAL|><|Speech|><|withitn|>"，分别是语言/情绪/
# 声音事件/是否做了ITN——语言标签内容是固定的语言代码小写，情绪/事件标签是大写单词，
# 必须大小写不敏感匹配+匹配任意内容，不能只认小写字母，不然"<|NEUTRAL|>"这种漏刷不掉
_SENSEVOICE_ANY_TAG_RE = re.compile(r"<\|[^|]+\|>")
_SENSEVOICE_LANG_TAG_RE = re.compile(r"<\|(zh|en|yue|ja|ko|nospeech)\|>", re.IGNORECASE)


def _load_sensevoice_model():
    """加载SenseVoice的ONNX会话（编码器+VAD+分词器），返回(front, model, vad)供transcribe_audio_sensevoice复用。
    跑在后台线程（配合spinner.run_with_work），跟_load_model()是并列的两条加载路径。"""
    from sensevoice.onnx.sense_voice_ort_session import SenseVoiceInferenceSession
    from sensevoice.utils.frontend import WavFrontend
    from sensevoice.utils.fsmn_vad import FSMNVad

    # 必须传绝对路径：这个第三方包内部拼VAD模型路径时有个重复拼接的bug（拿root_dir又和一个
    # 已经带了root_dir前缀的相对路径再拼一次），传绝对路径能让pathlib的"绝对路径覆盖前缀"
    # 行为正好绕开这个bug，不用去改site-packages里的代码
    front = WavFrontend(os.path.join(SENSEVOICE_MODEL_DIR, "am.mvn"))
    model = SenseVoiceInferenceSession(
        os.path.join(SENSEVOICE_MODEL_DIR, "embedding.npy"),
        os.path.join(SENSEVOICE_MODEL_DIR, "sense-voice-encoder-int8.onnx"),
        os.path.join(SENSEVOICE_MODEL_DIR, "chn_jpn_yue_eng_ko_spectok.bpe.model"),
        -1, 4,  # device=-1（CPU），4个推理线程
    )
    vad = FSMNVad(SENSEVOICE_MODEL_DIR)
    return front, model, vad


def _decode_to_16k_wav(audio_path, wav_path):
    """用PyAV把任意格式的音频解码+重采样成16kHz单声道，写成wav文件——SenseVoice的VAD
    (FSMNVad.segments_offline)直接读文件本身，且写死只认16kHz，不像whisper那边ctranslate2
    内部自己处理任意采样率/格式，这里得手动转好了再喂给它"""
    import av

    container = av.open(audio_path)
    out = av.open(wav_path, "w")
    out_stream = out.add_stream("pcm_s16le", rate=16000)
    out_stream.layout = "mono"
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    for frame in container.decode(audio=0):
        for rframe in resampler.resample(frame):
            for packet in out_stream.encode(rframe):
                out.mux(packet)
    for packet in out_stream.encode(None):
        out.mux(packet)
    container.close()
    out.close()


def transcribe_audio_sensevoice(sv_session, audio_path, language="auto", on_progress=None):
    """跟transcribe_audio()接口对齐：返回(segments, detected_lang)。sv_session是
    _load_sensevoice_model()返回的(front, model, vad)三元组。"""
    import tempfile
    import soundfile as sf

    front, model, vad = sv_session

    fd, tmp_wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        _decode_to_16k_wav(audio_path, tmp_wav_path)
        waveform, _ = sf.read(tmp_wav_path, dtype="float32")
        vad_segments = vad.segments_offline(tmp_wav_path)
    finally:
        os.remove(tmp_wav_path)

    lang_code = _SENSEVOICE_LANG_CODES.get(language, 0)
    result = []
    total = len(vad_segments) or 1
    detected_lang = None

    for i, part in enumerate(vad_segments):
        audio_feats = front.get_features(waveform[part[0] * 16:part[1] * 16])
        raw_text = model(audio_feats[None, ...], language=lang_code, use_itn=True)

        tag_match = _SENSEVOICE_LANG_TAG_RE.search(raw_text)
        seg_lang = tag_match.group(1).lower() if tag_match else "en"
        if seg_lang not in ("zh", "en"):
            seg_lang = "en"  # 只支持中英，粤语/日语/韩语等一律归到en桶，跟whisper那边保持一致
        if detected_lang is None:
            detected_lang = seg_lang

        text = _SENSEVOICE_ANY_TAG_RE.sub("", raw_text).strip()
        result.append({
            "start": part[0] / 1000,
            "end": part[1] / 1000,
            "en": "" if seg_lang == "zh" else text,
            "zh": text if seg_lang == "zh" else "",
        })

        if on_progress:
            on_progress((i + 1) / total)

    return result, (detected_lang or "en")


# ==================== 主流程 ====================
def main():
    print(f"=== RUN START {datetime.now()} ===")

    wait_if_fullscreen_active()

    latest_log = load_latest_log()
    os.makedirs(EPISODES_DIR, exist_ok=True)

    # 模型懒加载：只有真正确认有新一期要转录时才加载（只加载一次，供后续节目复用），
    # 这样"今天两个节目都没更新"的最常见情况就不用白等模型加载的时间
    session = None
    showed_click_to_open = False

    for show_name, rss_url in FEEDS.items():
        print(f"\n--- Checking {show_name} ---")

        job = _check_show(show_name, rss_url, latest_log)
        if job is None:
            continue

        if session is None:
            spinner = SpinnerProgress("播客自动化")
            spinner.set_stage(
                "正在加载SenseVoice模型（CPU，首次可能需要几十秒）..."
                if TRANSCRIBE_ENGINE == "sensevoice" else
                "正在加载GPU模型（首次可能需要1-2分钟）..."
            )
            try:
                session = spinner.run_with_work(lambda: load_engine_model(TRANSCRIBE_ENGINE))
            except Exception as e:
                print(f"Model loading failed: {e}")
                notify_simple("播客自动化启动失败", f"模型加载失败: {e}"[:100])
                return

        current_title = job["current_title"]
        audio_url = job["audio_url"]
        episode_dir = job["episode_dir"]

        audio_filename = "audio.mp3"
        audio_path = os.path.join(episode_dir, audio_filename)
        json_path = os.path.join(episode_dir, "data.json")
        html_path = os.path.join(episode_dir, "subtitles.html")
        en_txt_path = os.path.join(episode_dir, "transcript_en.txt")
        zh_txt_path = os.path.join(episode_dir, "transcript_zh.txt")
        srt_path = os.path.join(episode_dir, "subtitles.srt")
        vtt_path = os.path.join(episode_dir, "subtitles.vtt")

        progress = FloatingProgress(f"「{show_name}」{current_title}")

        try:
            progress.update("下载音频中", 0.0)
            download_audio(
                audio_url, audio_path, retry_rss_url=rss_url,
                on_progress=lambda p: progress.update(f"下载中 {int(p*100)}%", p * 0.2),
            )

            progress.update("转录中" + ("" if TRANSCRIBE_ENGINE == "sensevoice" else "（GPU运算）"), 0.2)
            segments, detected_lang = run_transcribe(
                TRANSCRIBE_ENGINE, session, audio_path, language=LANGUAGE,
                on_progress=lambda p: progress.update(f"转录中 {int(p*100)}%", 0.2 + p * 0.6),
            )
            # 必须在翻译之前做：按真实句子边界重组之后再翻译，送进去的是完整句子，
            # 译文质量跟着改善（不用再被迫把Whisper断在句子中间的残句硬翻完整）
            segments = regroup_words_into_sentences(segments, is_zh=(detected_lang == "zh"))

            if detected_lang == "zh":
                progress.update("识别到中文，跳过翻译", 0.85)
            else:
                progress.update("翻译中", 0.7)
                segments = translate_segments(
                    segments,
                    on_progress=lambda p: progress.update(f"翻译中 {int(p*100)}%", 0.7 + p * 0.15),
                )

            diarization_used = False
            if ENABLE_DIARIZATION:
                try:
                    progress.update("识别说话人...", 0.87)
                    diar_session = _load_diarization_model()
                    diar_result = diarize_audio(
                        diar_session, audio_path,
                        on_progress=lambda p: progress.update(f"识别说话人 {int(p*100)}%", 0.87 + p * 0.1),
                    )
                    segments = assign_speakers(segments, diar_result)
                    diarization_used = True
                except Exception as e:
                    # 说话人分离失败不阻断主流程，就当没开这个功能，正常出转录+翻译结果
                    print(f"Speaker diarization failed, skipping: {e}")

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)

            save_text_files(segments, en_txt_path, zh_txt_path)
            generate_html(current_title, audio_filename, segments, html_path)
            generate_srt(segments, srt_path)
            generate_vtt(segments, vtt_path)
            save_episode_meta(
                {
                    "published": job.get("published", ""),
                    "processed_at": datetime.now().isoformat(),
                    "language": detected_lang,
                    "engine": TRANSCRIBE_ENGINE,
                    "diarization": diarization_used,
                },
                episode_dir,
            )

            latest_log[show_name] = current_title
            save_latest_log(latest_log)
            print(f"Done: {current_title}")
            print(f"Folder: {episode_dir}")

            progress.finish("已完成，点击查看")
            notify_click_to_open(f"「{show_name}」有新一期", current_title[:60], html_path)
            showed_click_to_open = True

        except Exception as e:
            print(f"Processing failed: {e}")
            progress.finish(f"处理失败: {e}"[:60])
            notify_simple(f"「{show_name}」处理失败", f"{current_title[:40]}：{e}"[:100])
            continue

    if showed_click_to_open:
        # notify_click_to_open的点击回调是靠这个python进程自己活着去接WinRT的激活事件——
        # 这里没有给应用注册真正的AUMID/激活器（那是普通装好的软件才有的，需要一整套
        # 快捷方式+注册表/COM设置），进程一退出，回调就彻底没人接了。main()跑完这里
        # 马上就要os._exit()，如果不等一下，通知刚弹出来、用户还没来得及点，进程已经死了，
        # 点了也没反应——之前真实测试就是这么复现的。这里等一下不能保证100%（用户几分钟后
        # 从操作中心里点还是会失效），但比"进程立刻退出、点了必然没反应"好得多。
        print("Waiting a bit before exit so the completion notification stays clickable...")
        time.sleep(8)

    print(f"\n=== RUN END {datetime.now()} ===")


def _emit_json(payload):
    """给手动任务模式用：按行往stdout打JSON，调用方（setup_wizard.py起的子进程）按行解析。
    用flush=True保证父进程能实时读到，不会被缓冲卡住"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_manual_job(show_name, episode_title, source_type, source, published="", language=None, engine=None,
                    enable_diarization=None):
    """手动任务模式的核心逻辑：不检查RSS、不循环多个节目，只处理指定的这一个音频。
    被 setup_wizard.py 当独立子进程调用——这样GUI本身不需要打包faster-whisper/ctranslate2这些
    体积巨大的转录依赖，也不用操心CUDA运行库有没有被打包进exe，因为跑的就是真实python环境。
    source_type: "local"（本地文件路径）/ "download"（音频URL）/ "zip"（"zip路径::压缩包内条目名"）
    published: 可选，RSS历史下载场景下这一期原本的发布时间（GUI那边已经从RSS条目里读到了，
    传过来存进meta.json）；本地文件/zip没有这个概念，留空，播客库改用处理时刻排序
    language/engine/enable_diarization留空（None）就用config.json里的全局默认值，跟GUI手动处理
    面板的行为一致；显式传值（比如CLI里带了--language/--engine）才会覆盖config.json"""
    language = LANGUAGE if language is None else language
    engine = TRANSCRIBE_ENGINE if engine is None else engine
    show_dir = os.path.join(EPISODES_DIR, show_name)
    os.makedirs(show_dir, exist_ok=True)
    safe_title = sanitize_filename(episode_title)
    episode_dir = os.path.join(show_dir, safe_title)
    os.makedirs(episode_dir, exist_ok=True)

    if source_type == "local":
        ext = os.path.splitext(source)[1] or ".mp3"
        audio_filename = f"audio{ext}"
        audio_dest = os.path.join(episode_dir, audio_filename)
        _emit_json({"stage": "复制音频文件...", "progress": 0.05})
        shutil.copyfile(source, audio_dest)
        _emit_json({"stage": "准备转录...", "progress": 0.2})
    elif source_type == "zip":
        zip_path, entry_name = source.split("::", 1)
        ext = os.path.splitext(entry_name)[1] or ".mp3"
        audio_filename = f"audio{ext}"
        audio_dest = os.path.join(episode_dir, audio_filename)
        _emit_json({"stage": "解压音频文件...", "progress": 0.05})
        with zipfile.ZipFile(zip_path) as zf, zf.open(entry_name) as src, open(audio_dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        _emit_json({"stage": "准备转录...", "progress": 0.2})
    elif source_type == "ytdlp":
        _emit_json({"stage": "解析视频链接...", "progress": 0.02})
        downloaded_path = download_via_ytdlp(
            source, episode_dir,
            on_progress=lambda p: _emit_json({"stage": f"下载中 {int(p * 100)}%", "progress": p * 0.2}),
        )
        # yt-dlp自己按视频标题决定文件名和扩展名，这里统一改名成audio.<原始扩展名>，
        # 跟其它来源保持"文件名固定叫audio，只有扩展名不同"这个约定，方便播客库/其它
        # 地方按固定文件名找音频，不用另外记一份"这一期音频文件到底叫什么"
        ext = os.path.splitext(downloaded_path)[1] or ".m4a"
        audio_filename = f"audio{ext}"
        audio_dest = os.path.join(episode_dir, audio_filename)
        if downloaded_path != audio_dest:
            shutil.move(downloaded_path, audio_dest)
    else:  # download
        audio_filename = "audio.mp3"
        audio_dest = os.path.join(episode_dir, audio_filename)
        download_audio(
            source, audio_dest,
            on_progress=lambda p: _emit_json({"stage": f"下载中 {int(p * 100)}%", "progress": p * 0.2}),
        )

    session = load_engine_model(engine)
    segments, detected_lang = run_transcribe(
        engine, session, audio_dest, language=language,
        on_progress=lambda p: _emit_json({"stage": f"转录中 {int(p * 100)}%", "progress": 0.2 + p * 0.6}),
    )
    segments = regroup_words_into_sentences(segments, is_zh=(detected_lang == "zh"))
    if detected_lang == "zh":
        _emit_json({"stage": "识别到中文，跳过翻译", "progress": 0.85})
    else:
        segments = translate_segments(
            segments,
            on_progress=lambda p: _emit_json({"stage": f"翻译中 {int(p * 100)}%", "progress": 0.7 + p * 0.15}),
        )

    do_diarization = ENABLE_DIARIZATION if enable_diarization is None else enable_diarization
    diarization_used = False
    if do_diarization:
        try:
            _emit_json({"stage": "识别说话人...", "progress": 0.87})
            diar_session = _load_diarization_model()
            diar_result = diarize_audio(
                diar_session, audio_dest,
                on_progress=lambda p: _emit_json({"stage": f"识别说话人 {int(p * 100)}%", "progress": 0.87 + p * 0.1}),
            )
            segments = assign_speakers(segments, diar_result)
            diarization_used = True
        except Exception as e:
            print(f"Speaker diarization failed, skipping: {e}")

    with open(os.path.join(episode_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)
    save_text_files(
        segments,
        os.path.join(episode_dir, "transcript_en.txt"),
        os.path.join(episode_dir, "transcript_zh.txt"),
    )
    generate_html(episode_title, audio_filename, segments, os.path.join(episode_dir, "subtitles.html"))
    generate_srt(segments, os.path.join(episode_dir, "subtitles.srt"))
    generate_vtt(segments, os.path.join(episode_dir, "subtitles.vtt"))
    save_episode_meta(
        {
            "published": published,
            "processed_at": datetime.now().isoformat(),
            "language": detected_lang,
            "engine": engine,
            "diarization": diarization_used,
        },
        episode_dir,
    )
    _emit_json({"done": True, "result_dir": episode_dir})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--manual-job":
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--manual-job", action="store_true")
        parser.add_argument("--show", required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--source-type", required=True, choices=["local", "download", "zip", "ytdlp"])
        parser.add_argument("--source", required=True)
        parser.add_argument("--published", default="")
        parser.add_argument("--language", default=None, choices=["auto", "en", "zh"])
        parser.add_argument("--engine", default=None, choices=["whisper", "sensevoice"])
        parser.add_argument("--enable-diarization", dest="enable_diarization", action="store_true", default=None)
        parser.add_argument("--no-diarization", dest="enable_diarization", action="store_false")
        cli_args = parser.parse_args()
        try:
            run_manual_job(
                cli_args.show, cli_args.title, cli_args.source_type, cli_args.source,
                published=cli_args.published, language=cli_args.language, engine=cli_args.engine,
                enable_diarization=cli_args.enable_diarization,
            )
        except Exception as e:
            _emit_json({"error": str(e)})
            sys.stdout.flush()
            os._exit(1)
        sys.stdout.flush()
        os._exit(0)

    main()
    sys.stdout.flush()
    os._exit(0)


# ============================================================
#
#                   ██████████████
#                   ██████████████
#                 ████  ██████  ████
#                   ██████████████
#                   ██████████████
#                   ██  ██  ██  ██
#                   ██  ██  ██  ██
#
#                     claw'd was here 🦀
#         thanks for putting up with all my debugging, human
#
# ============================================================
