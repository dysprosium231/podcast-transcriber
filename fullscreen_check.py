"""全屏游戏检测（避免和游戏抢显卡/弹通知打扰）。
被 daily_podcast.py 和 prompt_before_run.py 共用。
"""

import ctypes
import ctypes.wintypes
import psutil

NON_GAME_PROCESS_NAMES = {
    # 浏览器（含全屏/无边框模式）
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
    # 视频/媒体播放器
    "vlc.exe", "potplayer.exe", "potplayermini64.exe", "mpv.exe", "wmplayer.exe",
    # 常见办公/演示软件全屏模式
    "powerpnt.exe", "acrobat.exe", "acrord32.exe",
    # 其他常见非游戏全屏场景
    "explorer.exe", "obs64.exe",
}


def is_fullscreen_app_active():
    """检测当前前台窗口是否是全屏游戏（排除浏览器/视频播放器等常见全屏但非游戏的场景，
    也排除普通"最大化"窗口——用DWM的实际可见边界而不是GetWindowRect，
    因为GetWindowRect对最大化窗口会包含隐形边框，导致误判成全屏）"""
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    rect = ctypes.wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        # DWM查询失败时退回GetWindowRect（极少数情况，比如某些老旧应用）
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

    window_w = rect.right - rect.left
    window_h = rect.bottom - rect.top

    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)

    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buf, 256)
    class_name = class_buf.value

    if class_name in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False

    # 用实际可见边界严格匹配屏幕尺寸（不再用>=，避免边框误差），
    # 最大化窗口因为任务栏占位，实际可见区域必然小于完整屏幕尺寸
    if window_w != screen_w or window_h != screen_h:
        return False

    try:
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = psutil.Process(pid.value)
        process_name = process.name().lower()
        if process_name in NON_GAME_PROCESS_NAMES:
            return False
    except Exception:
        pass

    return True
