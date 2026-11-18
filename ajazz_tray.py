"""黑爵鼠标电量托盘 — Windows 系统托盘电量显示（支持 AJ179 / AJ159 星闪）。

用法：
    pythonw ajazz_tray.py        # 托盘常驻（无控制台窗口）
    python  ajazz_tray.py --once # 打印一次 JSON（供脚本或排查用）

协议自动识别：星闪（7768）优先，其次 Sonix（3151）。
除厂商的只读查询帧外不写设备，不改 DPI / 灯光 / 按键映射，不联网。
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import sys
import threading
import time
import winreg
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import ajazz_battery as core

APP_NAME = "鼠标电量"
REFRESH_SECONDS = 60
SLEEPING_RETRY_SECONDS = 15  # 读不到时缩短间隔，等鼠标唤醒
CLOCK_SYNC_SECONDS = 30  # 底座会在几分钟内把时钟重置回 00:00，必须定时重发
SCRIPT = Path(__file__).resolve()
CONFIG_FILE = SCRIPT.parent / "config.json"
QUIT_FLAG = SCRIPT.parent / "quit.flag"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AJAZZBatteryTray"
LEGACY_RUN_VALUES = ("AJ179BatteryTray",)  # 改名前的旧自启项，启动时顺手清掉

# 托盘实际只显示 16px（高 DPI 下 24/32px）。pystray 把 PIL 图存成 ICO 交给系统，
# Pillow 会按源图自动生成 16/24/32/48/64/128 多帧，所以母版取 128px：系统无论
# 缩放多少都能挑到合适的一帧，不会拿 32px 硬拉。绘制时再 4 倍超采样后缩回，
# 边缘才干净。
ICON_SIZE = 128
SUPERSAMPLE = 4

DIGIT_FONTS = [
    r"C:\Windows\Fonts\bahnschrift.ttf",  # 半窄体，同样宽度能放更大的字
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]
CJK_FONTS = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"]

COLOR_OK = (36, 168, 84)
COLOR_MID = (208, 120, 6)  # 比纯橙深一档，浅色任务栏上也能看清
COLOR_LOW = (218, 48, 48)
COLOR_CHARGE = (0, 118, 240)
COLOR_UNKNOWN = (126, 126, 126)

# 汉字字形本身比数字满，等比放大后会压过数字，缩到 0.82 才协调
CJK_FILL = 0.82
# 读不到电量时画两条短横，不撑满画布，否则 16px 下糊成一个色块
UNKNOWN_FILL = 0.62


@functools.lru_cache(maxsize=128)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _level_color(percent: int) -> tuple[int, int, int]:
    if percent <= 20:
        return COLOR_LOW
    if percent <= 50:
        return COLOR_MID
    return COLOR_OK


def _fit_size(text: str, box: int, path: str) -> int:
    """二分出这个字符串在方框里能放下的最大字号。"""
    lo, hi = 6, max(8, box * 2)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        left, top, right, bottom = _font(path, mid).getbbox(text)
        if right - left <= box and bottom - top <= box:
            lo = mid
        else:
            hi = mid - 1
    return lo


@functools.lru_cache(maxsize=1)
def _digit_family() -> str:
    """固定一种数字字体。

    不逐次挑「最大的字体」——不同数字宽度不同，会导致 90 用一套字形、45 换另一套，
    百分比跳动时字形跟着闪。这里按最宽的三位数选一次，全程沿用。
    """
    ref_box = 1000
    best_size, best_path = 0, DIGIT_FONTS[-1]
    for path in DIGIT_FONTS:
        if not os.path.exists(path):
            continue
        size = _fit_size("000", ref_box, path)
        if size > best_size:
            best_size, best_path = size, path
    return best_path if os.path.exists(best_path) else ""


def _text_font(text: str, box: int, cjk: bool) -> ImageFont.FreeTypeFont:
    if cjk:
        path = next((p for p in CJK_FONTS if os.path.exists(p)), "")
        return _font(path, _fit_size(text, box, path)) if path else ImageFont.load_default()
    path = _digit_family()
    if not path:
        return ImageFont.load_default()
    # 用同位数的最宽组合定字号：90 和 15 因此一样大，不会随数值忽大忽小
    return _font(path, _fit_size("0" * len(text), box, path))


def make_icon(percent: int | None, charging: bool = False) -> Image.Image:
    if charging and percent is not None:
        text, color, fill, cjk = str(percent), COLOR_CHARGE, 1.0, False
    elif charging:
        text, color, fill, cjk = "充", COLOR_CHARGE, CJK_FILL, True
    elif percent is None:
        text, color, fill, cjk = "--", COLOR_UNKNOWN, UNKNOWN_FILL, False
    else:
        text, color, fill, cjk = str(percent), _level_color(percent), 1.0, False

    canvas = ICON_SIZE * SUPERSAMPLE
    # 留 3% 边距，避免字形贴边被裁
    box = round((canvas - 2 * round(canvas * 0.03)) * fill)
    font = _text_font(text, box, cjk)

    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = font.getbbox(text)
    draw.text(
        ((canvas - (right - left)) / 2 - left, (canvas - (bottom - top)) / 2 - top),
        text,
        font=font,
        fill=color,
    )
    return img.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)


def _detail_text(
    status: core.Status, last: core.Status | None, clock_line: str | None = None
) -> str:
    if status.state == core.STATE_WIRELESS:
        head = f"电量：{status.percent}%（放电中）"
    elif status.state == core.STATE_CHARGING:
        head = (
            f"电量：{status.percent}%（充电中）"
            if status.percent is not None
            else "电量：充电中（已接入 USB，该通道未提供百分比）"
        )
    elif status.state == core.STATE_SLEEPING:
        head = "电量：鼠标休眠或待机中，暂时读不到"
    else:
        head = "电量：未检测到鼠标（接收器未插或鼠标断电）"

    lines = [head]
    if status.product:
        lines.append(f"设备：{status.product}")
    lines.append(f"更新时间：{status.updated_at}")
    if clock_line:
        lines.append(clock_line)
    if status.raw:
        lines.append("")
        lines.append(f"原始数据：\n{status.raw.hex(' ')}")
    if last and last.percent is not None and status.percent is None:
        lines.append("")
        lines.append(f"上次读数：{last.percent}%（{last.updated_at}）")
    return "\n".join(lines)


def show_message(title: str, text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def _startup_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{SCRIPT}"'


def load_config() -> dict:
    defaults = {"clock_sync": True}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            defaults.update(data)
    except (OSError, ValueError):
        pass
    return defaults


def save_config(config: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _run_value(name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return value or None
    except OSError:
        return None


def startup_enabled() -> bool:
    return bool(_run_value(RUN_VALUE))


def set_startup(enabled: bool) -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        # 先清掉同名项与改名前的旧项，避免留下两条
        for name in (RUN_VALUE, *LEGACY_RUN_VALUES):
            try:
                winreg.DeleteValue(key, name)
            except OSError:
                pass
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _startup_command())


def migrate_legacy_startup() -> None:
    """旧版自启项指向的是改名前的脚本，搬到新名下，否则开机只会静默失败。"""
    if startup_enabled():
        return
    if any(_run_value(name) for name in LEGACY_RUN_VALUES):
        try:
            set_startup(True)
        except OSError:
            pass


class Tray:
    def __init__(self) -> None:
        import pystray

        self.current: core.Status | None = None
        self.last: core.Status | None = None
        self._low_notified = False
        self._stop = threading.Event()

        self.config = load_config()
        self._clock_ok = False
        self._clock_at: float | None = None
        # 星闪接收器没有 OLED 底座，菜单里就不出现时钟项
        self.clock_supported = core.dock_clock_available()

        menu_items = [
            pystray.MenuItem("立即刷新", self.refresh, default=True),
            pystray.MenuItem("电量详情", self.show_detail),
            pystray.Menu.SEPARATOR,
        ]
        if self.clock_supported:
            menu_items += [
                pystray.MenuItem(
                    "底座显示时间",
                    self.toggle_clock,
                    checked=lambda item: bool(self.config.get("clock_sync")),
                ),
                pystray.MenuItem("立即同步时钟", self.sync_clock_now),
                pystray.Menu.SEPARATOR,
            ]
        menu_items += [
            pystray.MenuItem(
                "开机自动启动",
                self.toggle_startup,
                checked=lambda item: startup_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.quit),
        ]

        self.icon = pystray.Icon(
            "ajazz_battery",
            make_icon(None),
            APP_NAME,
            menu=pystray.Menu(*menu_items),
        )

    # ---------- 数据 ----------

    def refresh(self, icon=None, item=None) -> None:
        status = core.read_status()
        previous_state = self.current.state if self.current else None
        self.current = status

        # 接收器刚回到在线（例如拔掉充电线）时立刻补一次对时，不等 30 秒定时器
        if (
            self.clock_supported
            and status.state == core.STATE_WIRELESS
            and previous_state not in (None, core.STATE_WIRELESS)
            and self.config.get("clock_sync")
        ):
            threading.Thread(target=self._sync_clock, daemon=True).start()

        if status.percent is not None:
            self.last = status
            if status.percent <= 20 and not self._low_notified:
                self._low_notified = True
                try:
                    self.icon.notify(f"鼠标电量仅剩 {status.percent}%，该充电了", APP_NAME)
                except Exception:
                    pass
            elif status.percent > 20:
                self._low_notified = False

        self._apply(status)

    def _apply(self, status: core.Status) -> None:
        if status.state == core.STATE_WIRELESS:
            tooltip = f"{APP_NAME}\n电量 {status.percent}%\n更新于 {status.updated_at}"
        elif status.state == core.STATE_CHARGING:
            if status.percent is not None:
                tooltip = f"{APP_NAME}\n充电中 · 电量 {status.percent}%\n更新于 {status.updated_at}"
            else:
                tooltip = f"{APP_NAME}\n充电中（已接入 USB）"
        elif status.state == core.STATE_SLEEPING:
            tooltip = f"{APP_NAME}\n鼠标休眠中"
            if self.last and self.last.percent is not None:
                tooltip += f"\n上次读数 {self.last.percent}%"
        else:
            tooltip = f"{APP_NAME}\n未检测到鼠标"

        try:
            self.icon.icon = make_icon(status.percent, status.charging)
            self.icon.title = tooltip[:127]
        except Exception:
            pass

    # ---------- 菜单 ----------

    def show_detail(self, icon=None, item=None) -> None:
        text = _detail_text(
            self.current or core.Status(core.STATE_ABSENT),
            self.last,
            self._clock_line() if self.clock_supported else None,
        )
        threading.Thread(target=show_message, args=(APP_NAME, text), daemon=True).start()

    # ---------- 底座时钟 ----------

    def _clock_line(self) -> str:
        if not self.config.get("clock_sync"):
            return "底座时钟：已关闭（菜单「底座显示时间」可开启）"
        if self._clock_at is None:
            return "底座时钟：尚未同步"
        stamp = time.strftime("%H:%M:%S", time.localtime(self._clock_at))
        return f"底座时钟：{'已同步' if self._clock_ok else '同步失败（底座不在线）'}于 {stamp}"

    def sync_clock_now(self, icon=None, item=None) -> None:
        threading.Thread(target=self._sync_clock, daemon=True).start()

    def _sync_clock(self) -> None:
        if not self.config.get("clock_sync"):
            return
        ok = core.set_dock_clock()
        self._clock_ok = ok
        if ok:
            self._clock_at = time.time()

    def toggle_clock(self, icon=None, item=None) -> None:
        self.config["clock_sync"] = not self.config.get("clock_sync")
        save_config(self.config)
        if self.config["clock_sync"]:
            threading.Thread(target=self._sync_clock, daemon=True).start()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def toggle_startup(self, icon=None, item=None) -> None:
        try:
            if startup_enabled():
                set_startup(False)
            else:
                set_startup(True)
        except Exception as exc:
            threading.Thread(
                target=show_message, args=(APP_NAME, f"设置开机启动失败：{exc}"), daemon=True
            ).start()

    def quit(self, icon=None, item=None) -> None:
        self._stop.set()
        self.icon.stop()

    # ---------- 生命周期 ----------

    def _loop(self) -> None:
        """常规 60 秒一轮；鼠标休眠读不到时缩短到 15 秒，唤醒后能快点显示。"""
        while True:
            sleeping = (self.current is None) or (
                self.current.percent is None and not self.current.charging
            )
            if self._stop.wait(SLEEPING_RETRY_SECONDS if sleeping else REFRESH_SECONDS):
                return
            self.refresh()

    def _clock_loop(self) -> None:
        # 启动后先等 3 秒：固件上电约 2 秒后才接受时钟帧，太早发会被静默丢弃
        if self._stop.wait(3):
            return
        while True:
            self._sync_clock()
            if self._stop.wait(CLOCK_SYNC_SECONDS):
                return

    def _watch_quit(self) -> None:
        """支持 `--quit` 优雅退出（进程被系统拒绝直接结束时的备用通道）。"""
        while not self._stop.wait(1):
            if QUIT_FLAG.exists():
                try:
                    QUIT_FLAG.unlink()
                except OSError:
                    pass
                self.quit()
                return

    def run(self) -> None:
        threading.Thread(target=self.refresh, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        if self.clock_supported and self.config.get("clock_sync"):
            threading.Thread(target=self._clock_loop, daemon=True).start()
        threading.Thread(target=self._watch_quit, daemon=True).start()
        self.icon.run()


def single_instance() -> bool:
    """命名互斥体，保证只有一个托盘实例。"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\AJAZZBatteryTray")
    if not handle:
        return True
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    if "--quit" in sys.argv:
        QUIT_FLAG.write_text("1", encoding="utf-8")
        # 等运行中的实例自己退出（最多 3 秒）
        for _ in range(30):
            time.sleep(0.1)
            if not QUIT_FLAG.exists():
                print("已退出")
                return 0
        QUIT_FLAG.unlink(missing_ok=True)
        print("没有响应退出请求（可能没有实例在运行，或版本较旧）")
        return 1

    if "--sync-clock" in sys.argv:
        if not core.dock_clock_available():
            print(json.dumps({"clock_synced": False, "reason": "当前设备没有 OLED 底座"}, ensure_ascii=False))
            return 1
        ok = core.set_dock_clock()
        print(json.dumps({"clock_synced": ok, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
        return 0 if ok else 1

    if "--once" in sys.argv:
        status = core.read_status()
        clock_ok = core.set_dock_clock() if core.dock_clock_available() else None
        print(
            json.dumps(
                {
                    "ok": status.state == core.STATE_WIRELESS,
                    "protocol": core.active_protocol(),
                    "state": status.state,
                    "product": status.product,
                    "percent": status.percent,
                    "charging": status.charging,
                    "updated_at": status.updated_at,
                    "clock_synced": clock_ok,
                    "raw": status.raw.hex(" ") if status.raw else None,
                },
                ensure_ascii=False,
            )
        )
        return 0 if status.state != core.STATE_ABSENT else 1

    if not single_instance():
        return 0

    migrate_legacy_startup()
    try:
        Tray().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
