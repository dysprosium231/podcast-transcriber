"""
执行前确认通知（改用Windows原生通知+按钮，不再弹独立窗口）。
用户可以点击通知上的按钮：
  - "取消今天" -> 今天不运行，退出码 1
  - "延后30分钟" / "延后60分钟" -> 先等待，再退出码 0（run_daily.bat 会接着跑正式脚本）
  - "立即开始" 或不做任何操作等到倒计时结束 -> 退出码 0，立刻/自动继续

注意：通知在屏幕上停留约20多秒后会收进操作中心，之后再点击按钮的回调
是否触发，在没有完整注册应用标识(AUMID)的情况下不完全可靠。但倒计时到点
自动继续这个兜底逻辑不依赖按钮点击，独立生效，所以整体上不影响最终一定会
执行这个结果。

另外：如果检测到全屏游戏正在运行，完全不弹这个选择通知、不进入倒计时，
静默等待游戏结束后再弹出通知（避免打游戏时被打扰、也避免这期间误触发
后续的转录流程抢占显卡）。
"""

import sys
import os
import time
import threading
from windows_toasts import (
    InteractableWindowsToaster,
    Toast,
    ToastButton,
    ToastActivatedEventArgs,
)
from fullscreen_check import is_fullscreen_app_active

COUNTDOWN_SECONDS = 10 * 60  # 提前多久提醒，默认10分钟
GAME_RECHECK_INTERVAL = 5 * 60  # 检测到全屏游戏时，每隔多久重新检查一次是否已结束

# 全屏游戏运行时，静默等待（不弹任何通知），直到游戏结束
while is_fullscreen_app_active():
    print(f"Fullscreen game detected, waiting silently ({GAME_RECHECK_INTERVAL}s) before showing prompt...")
    sys.stdout.flush()
    time.sleep(GAME_RECHECK_INTERVAL)

done_event = threading.Event()
result = {"action": "proceed"}  # 默认值：如果超时无操作，按"继续"处理


def on_activated(args: ToastActivatedEventArgs):
    result["action"] = args.arguments
    done_event.set()


toaster = InteractableWindowsToaster("播客自动化")
toast = Toast([
    "今天的播客抓取即将开始",
    f"{COUNTDOWN_SECONDS // 60}分钟内不操作将自动开始",
])
toast.on_activated = on_activated
toast.AddAction(ToastButton("取消今天", "cancel"))
toast.AddAction(ToastButton("延后30分钟", "delay_30"))
toast.AddAction(ToastButton("延后60分钟", "delay_60"))
toast.AddAction(ToastButton("立即开始", "proceed"))

toaster.show_toast(toast)

# 等待用户点击按钮，或者超时后按默认值("proceed")继续
done_event.wait(timeout=COUNTDOWN_SECONDS)

action = result["action"]

if action == "cancel":
    print("USER_CANCELLED_TODAY")
    sys.stdout.flush()
    os._exit(1)

elif action.startswith("delay_"):
    minutes = int(action.split("_")[1])
    print(f"DELAYING_{minutes}_MINUTES")
    sys.stdout.flush()
    time.sleep(minutes * 60)
    os._exit(0)

else:
    print("PROCEEDING")
    sys.stdout.flush()
    os._exit(0)