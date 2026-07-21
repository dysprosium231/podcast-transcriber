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
        self.root.geometry("640x680")
        self.root.resizable(False, False)

        existing = load_existing_config()
        self.download_state = DownloadState()

        self._build_feeds_section(existing.get("feeds", {}))
        self._build_translation_section(existing.get("translation", {}))
        self._build_model_section(existing.get("whisper_model_size", "large-v3"))
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
