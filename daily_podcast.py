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


def get_all_episodes(rss_url):
    """取整个RSS feed的完整历史条目列表（不只是最新一条）。自己用requests取内容再交给
    feedparser解析（而不是让feedparser直接拿URL），因为feedparser内置的URL抓取不设超时，
    网络卡住时会无限期挂住"""
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


def transcribe_audio(model, audio_path, on_progress=None):
    """转录，返回带时间戳的逐句列表。on_progress(0~1) 每次进度有明显变化时回调"""
    segments, info = model.transcribe(
        audio_path,
        language="en",
        batch_size=4,
        vad_filter=True,
    )
    total_duration = info.duration
    result = []
    last_reported = -1

    with tqdm(desc="转录进度", total=round(total_duration, 1), unit="秒") as bar:
        last_end = 0
        for seg in segments:
            result.append({
                "start": seg.start,
                "end": seg.end,
                "en": seg.text.strip(),
            })
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


def save_text_files(segments, en_path, zh_path):
    """保存纯英文稿和纯中文稿两个txt文件"""
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(" ".join(seg["en"] for seg in segments))

    with open(zh_path, "w", encoding="utf-8") as f:
        f.write(" ".join(seg["zh"] for seg in segments if seg["zh"]))


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

segments.forEach((seg, i) => {
  const div = document.createElement("div");
  div.className = "line";
  div.id = "line-" + i;
  div.innerHTML = `<span class="time">${formatTime(seg.start)}</span>
                    <div class="en">${seg.en}</div>
                    <div class="zh">${seg.zh || ""}</div>`;
  div.onclick = () => { audio.currentTime = seg.start; audio.play(); };
  transcript.appendChild(div);
});

let currentIndex = -1;
audio.addEventListener("timeupdate", () => {
  const t = audio.currentTime;
  const idx = segments.findIndex((seg, i) => {
    const next = segments[i + 1];
    return t >= seg.start && (!next || t < next.start);
  });
  if (idx !== -1 && idx !== currentIndex) {
    if (currentIndex !== -1) {
      document.getElementById("line-" + currentIndex).classList.remove("active");
    }
    document.getElementById("line-" + idx).classList.add("active");
    document.getElementById("line-" + idx).scrollIntoView({ behavior: "smooth", block: "center" });
    currentIndex = idx;
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
    }


def _load_model():
    """加载GPU模型，返回(model, batched_model)。跑在后台线程（配合spinner.run_with_work）。"""
    model = WhisperModel(MODEL_PATH, device="cuda", compute_type="float16")
    batched_model = BatchedInferencePipeline(model=model)
    return model, batched_model


# ==================== 主流程 ====================
def main():
    print(f"=== RUN START {datetime.now()} ===")

    wait_if_fullscreen_active()

    latest_log = load_latest_log()
    os.makedirs(EPISODES_DIR, exist_ok=True)

    # 模型懒加载：只有真正确认有新一期要转录时才加载（只加载一次，供后续节目复用），
    # 这样"今天两个节目都没更新"的最常见情况就不用白等1-2分钟的GPU模型加载
    model = None
    batched_model = None
    showed_click_to_open = False

    for show_name, rss_url in FEEDS.items():
        print(f"\n--- Checking {show_name} ---")

        job = _check_show(show_name, rss_url, latest_log)
        if job is None:
            continue

        if model is None:
            spinner = SpinnerProgress("播客自动化")
            spinner.set_stage("正在加载GPU模型（首次可能需要1-2分钟）...")
            try:
                model, batched_model = spinner.run_with_work(_load_model)
            except Exception as e:
                print(f"Model loading failed: {e}")
                notify_simple("播客自动化启动失败", f"GPU模型加载失败: {e}"[:100])
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

        progress = FloatingProgress(f"「{show_name}」{current_title}")

        try:
            progress.update("下载音频中", 0.0)
            download_audio(
                audio_url, audio_path, retry_rss_url=rss_url,
                on_progress=lambda p: progress.update(f"下载中 {int(p*100)}%", p * 0.2),
            )

            progress.update("转录中（GPU运算）", 0.2)
            segments = transcribe_audio(
                batched_model, audio_path,
                on_progress=lambda p: progress.update(f"转录中 {int(p*100)}%", 0.2 + p * 0.6),
            )

            progress.update("翻译中", 0.8)
            segments = translate_segments(
                segments,
                on_progress=lambda p: progress.update(f"翻译中 {int(p*100)}%", 0.8 + p * 0.2),
            )

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)

            save_text_files(segments, en_txt_path, zh_txt_path)
            generate_html(current_title, audio_filename, segments, html_path)

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


def run_manual_job(show_name, episode_title, source_type, source):
    """手动任务模式的核心逻辑：不检查RSS、不循环多个节目，只处理指定的这一个音频。
    被 setup_wizard.py 当独立子进程调用——这样GUI本身不需要打包faster-whisper/ctranslate2这些
    体积巨大的转录依赖，也不用操心CUDA运行库有没有被打包进exe，因为跑的就是真实python环境。
    source_type: "local"（本地文件路径）/ "download"（音频URL）/ "zip"（"zip路径::压缩包内条目名"）"""
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
    else:  # download
        audio_filename = "audio.mp3"
        audio_dest = os.path.join(episode_dir, audio_filename)
        download_audio(
            source, audio_dest,
            on_progress=lambda p: _emit_json({"stage": f"下载中 {int(p * 100)}%", "progress": p * 0.2}),
        )

    model = WhisperModel(MODEL_PATH, device="cuda", compute_type="float16")
    batched_model = BatchedInferencePipeline(model=model)

    segments = transcribe_audio(
        batched_model, audio_dest,
        on_progress=lambda p: _emit_json({"stage": f"转录中 {int(p * 100)}%", "progress": 0.2 + p * 0.6}),
    )
    segments = translate_segments(
        segments,
        on_progress=lambda p: _emit_json({"stage": f"翻译中 {int(p * 100)}%", "progress": 0.8 + p * 0.2}),
    )

    with open(os.path.join(episode_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)
    save_text_files(
        segments,
        os.path.join(episode_dir, "transcript_en.txt"),
        os.path.join(episode_dir, "transcript_zh.txt"),
    )
    generate_html(episode_title, audio_filename, segments, os.path.join(episode_dir, "subtitles.html"))
    _emit_json({"done": True, "result_dir": episode_dir})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--manual-job":
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--manual-job", action="store_true")
        parser.add_argument("--show", required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--source-type", required=True, choices=["local", "download", "zip"])
        parser.add_argument("--source", required=True)
        cli_args = parser.parse_args()
        try:
            run_manual_job(cli_args.show, cli_args.title, cli_args.source_type, cli_args.source)
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
