"""黑爵（AJAZZ）无线鼠标电量读取 —— 支持两代私有协议。

自动识别，无需配置：

  A. 星闪 / NearLink 方案（AJ159 NL / NL P 等，VID 0x7768）
     接收器 7768:6425、鼠标插线时本体枚举为 7768:6D25（PID 因型号而异）。
     厂商集合 usage_page=0xFFB2，走 input/output report（report id 6，64 字节），
     没有 feature report。这一点和 B 完全相反，是本机型的关键差异。

      帧格式： 06 | class | len(=1+载荷长度) | 00 | 00 | type | 载荷...
      读电量： 写  06 05 01 00 00 00（class 0x05 = 鼠标信息块，type 0）
              回  06 05 24 00 00 01 | <35 字节值>
                  值[7] = 电量百分比
                  值[1..2] = 产品 PID（小端）；两字节全 0 表示鼠标休眠，块是空的
                  值[10] = DPI 档位数，值[11] = 当前档，值[24] = 回报率索引
      充电判定：鼠标插线后本体会作为独立 USB 设备枚举，所以“本体在线”= 已接入 USB。
                接收器转发的充电标志恒为 1，不可信，本项目不使用。

  B. Sonix 方案（AJ179 APEX 等，VID 0x3151）
     feature report：先发 0xF7 心跳拉起 2.4G 遥测链路，再 GET_FEATURE 0x05 读电量。
     细节见下方 AN179 段落。

只发送上面列出的只读查询帧（以及 B 的无载荷心跳），不写 DPI / 灯光 / 按键映射 / 配对。
星闪协议的 class 0x12、0x31 会破坏设备里的按键映射，本模块完全不触碰。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import hid

STATE_WIRELESS = "wireless"
STATE_CHARGING = "charging"
STATE_SLEEPING = "sleeping"
STATE_ABSENT = "absent"

PROTO_NEARLINK = "nearlink"
PROTO_SONIX = "sonix"


@dataclass
class Status:
    state: str
    percent: int | None = None
    raw: bytes | None = None
    product: str | None = None
    path: str | None = None
    timestamp: float = field(default_factory=time.time)

    @property
    def charging(self) -> bool:
        return self.state == STATE_CHARGING

    @property
    def updated_at(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


def _path_str(path) -> str:
    return path.decode(errors="replace") if isinstance(path, bytes) else str(path)


# ===========================================================================
# A. 星闪 / NearLink（VID 0x7768，usage_page 0xFFB2，report id 6）
# ===========================================================================

NL_VID = 0x7768
NL_RECEIVER_PID = 0x6425  # 1K Nano NearLink 接收器
NL_BODY_PIDS = {0x6D25}  # AJ159 NL P 本体（插线时枚举），其它型号的 PID 未逐一收录
NL_USAGE_PAGE = 0xFFB2  # 承载状态帧的厂商集合
NL_REPORT_ID = 0x06
NL_REPORT_LEN = 64
NL_INFO_CLASS = 0x05  # 鼠标信息块（只读）
NL_NOTIFY_CLASS = 0xFA  # 设备主动推送：tag/value
NL_TAG_LINK = 0x01
NL_TAG_BATTERY = 0x03
NL_INFO_PERCENT = 7
NL_INFO_STAGES = 10
NL_INFO_ACTIVE = 11
NL_INFO_DPI = 12
NL_INFO_RATE = 24
NL_REPLY_TIMEOUT = 1.0  # 接收器转发较慢，1 秒仍无回应视为休眠

# 同一时刻只允许一个线程读写厂商集合
_NL_LOCK = threading.Lock()


def _nl_nodes() -> dict[int, dict]:
    """当前在线的星闪设备：{PID: 集合}，只保留带 0xFFB2 厂商集合的设备。"""
    nodes: dict[int, dict] = {}
    for d in hid.enumerate(NL_VID, 0):
        if d.get("usage_page") != NL_USAGE_PAGE:
            continue
        nodes.setdefault(d["product_id"], d)
    return nodes


def _nl_build(cls: int, type_: int = 0x00, value: bytes = b"") -> bytes:
    body = bytes([NL_REPORT_ID, cls, len(value) + 1, 0x00, 0x00, type_]) + value
    return bytes(bytearray(body).ljust(NL_REPORT_LEN, b"\x00"))


def _nl_drain(dev) -> None:
    """丢掉残留帧，否则会读到上一次查询的旧答案。"""
    for _ in range(64):
        if not dev.read(NL_REPORT_LEN):
            return


def _nl_reply(dev, cls: int, timeout: float = NL_REPLY_TIMEOUT) -> bytes | None:
    """等 class 匹配的应答帧。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = dev.read(NL_REPORT_LEN)
        if data and len(data) >= 6 and data[0] == NL_REPORT_ID and data[1] == cls:
            return bytes(data)
        time.sleep(0.004)
    return None


def _nl_frame_value(frame: bytes) -> bytes:
    """取帧载荷：len 含 type 字节，所以载荷长度 = len - 1。"""
    length = frame[2]
    return frame[6 : 6 + length - 1] if length > 1 else b""


def _nl_probe(node: dict) -> dict:
    """发一次 class 0x05 查询，收集应答。

    鼠标休眠时接收器不会回信息块，而是推送 0xFA / tag 0x01 / value 0
    （链路断开），这是比等超时更明确的信号。
    """
    result: dict = {"info": None, "link": None, "battery": None, "charge": None}

    with _NL_LOCK:
        dev = hid.device()
        try:
            dev.open_path(node["path"])
        except OSError:
            return result
        try:
            dev.set_nonblocking(1)
            _nl_drain(dev)
            dev.write(_nl_build(NL_INFO_CLASS))
            end = time.monotonic() + NL_REPLY_TIMEOUT
            while time.monotonic() < end:
                data = dev.read(NL_REPORT_LEN)
                if not data or len(data) < 6 or data[0] != NL_REPORT_ID:
                    time.sleep(0.004)
                    continue
                frame = bytes(data)
                cls = frame[1]
                if cls == NL_INFO_CLASS:
                    result["info"] = _nl_frame_value(frame)
                    break
                if cls == NL_NOTIFY_CLASS:
                    tag = frame[5]
                    value = _nl_frame_value(frame)
                    if tag == NL_TAG_BATTERY and value:
                        result["battery"] = value[0]
                        if len(value) > 1:
                            # 1 = 充电中，2 = 电池供电。接收器恒转发 1，不可信
                            result["charge"] = value[1]
                    elif tag == NL_TAG_LINK and value:
                        result["link"] = value[0]
                        if value[0] == 0:  # 鼠标未连接，不用再等
                            break
        except OSError:
            return result
        finally:
            try:
                dev.close()
            except Exception:
                pass
    return result


def _nl_parse_percent(value: bytes | None) -> int | None:
    """信息块的 byte7 是电量；休眠时设备回一个空块（PID 两字节为 0）。"""
    if not value or len(value) < NL_INFO_PERCENT + 1:
        return None
    if value[1:3] == b"\x00\x00":
        return None
    percent = value[NL_INFO_PERCENT]
    if percent > 100:
        return None
    return percent


def _nl_measure(node: dict, attempts: int) -> dict:
    """取一次读数：优先信息块，退回推送帧。"""
    probe: dict = {}
    for i in range(attempts):
        probe = _nl_probe(node)
        percent = _nl_parse_percent(probe["info"])
        if percent is not None:
            probe["percent"] = percent
            return probe
        if probe["battery"] is not None and probe["battery"] <= 100:
            probe["percent"] = probe["battery"]
            return probe
        if probe["link"] == 0:  # 已明确未连接，重试没意义
            break
        if i + 1 < attempts:
            time.sleep(0.15)
    probe.setdefault("percent", None)
    return probe


def _nl_is_body(node: dict) -> bool:
    """是「鼠标本体」而不是「接收器」。

    接收器产品名固定为 "AJAZZ NearLink"；本体是具体型号名（AJ159 NL P 等）。
    2.4G 线时两个都在线，此时本体在线即代表插了 USB 线。
    """
    pid = node["product_id"]
    if pid in NL_BODY_PIDS:
        return True
    if pid == NL_RECEIVER_PID:
        return False
    return "nearlink" not in (node.get("product_string") or "").lower()


def _read_nearlink(attempts: int = 2) -> Status | None:
    """返回 None 表示这台机器上没有星闪设备（交给另一套协议）。"""
    nodes = _nl_nodes()
    if not nodes:
        return None

    bodies = [n for n in nodes.values() if _nl_is_body(n)]
    receiver = next((n for n in nodes.values() if not _nl_is_body(n)), None)

    # 本体在线 = 插了 USB 线 = 充电中。接收器此时可能仍在线，但优先报本体。
    for node in bodies:
        probe = _nl_measure(node, attempts)
        return Status(
            STATE_CHARGING,
            percent=probe.get("percent"),
            raw=probe.get("info"),
            product=node.get("product_string"),
            path=_path_str(node["path"]),
        )

    if receiver is None:
        return Status(STATE_SLEEPING)

    probe = _nl_measure(receiver, attempts)
    if probe.get("percent") is None:
        return Status(
            STATE_SLEEPING,
            raw=probe.get("info"),
            product=receiver.get("product_string"),
        )
    return Status(
        STATE_WIRELESS,
        percent=probe["percent"],
        raw=probe.get("info"),
        product=receiver.get("product_string"),
        path=_path_str(receiver["path"]),
    )


# ===========================================================================
# B. Sonix 方案（VID 0x3151，feature report）
#
#  1. 厂商私有控制集合 usage_page=0xFFFF, usage=0x02 上 SET_FEATURE 一个
#     “状态轮询”帧：report id 0x00，opcode 0xF7，其余全 0，无载荷无校验。
#     这就是厂商驱动每秒发一次的心跳，作用是拉起 2.4G 状态回传链路；
#     不发它，状态页永远是全零。
#  2. 等约 50ms，GET_FEATURE report 0x05：
#     有效帧 = report id 后、电量字节前的字节必须为 0；
#     Windows 下电量在 byte3（0x64 = 100%）。
#
#  无线 2.4G 接收器 -> 3151:5007 "AJAZZ 2.4G 8K"，可读电量
#  插线 / 充电      -> 3151:4026 "AJAZZ AJ179 APEX"，改走有线通道，
#                      优先尝试读电量，读不到则只判定“充电中”，不编造数字。
#
# 底座 OLED 时钟（opcode 0x28 SET_OLEDCLOCK）：同一条控制集合，64 字节无校验帧：
#   帧[0]=0x00 report id, 帧[1]=0x28 命令, 帧[2..7]=0,
#   帧[8]=0xD7 必需标记（缺了固件静默丢弃整包）,
#   帧[9..10]=年（大端）, 帧[11]=月, 帧[12]=日, 帧[13]=时, 帧[14]=分, 帧[15]=秒。
#   固件只在启动约 2 秒后接受该帧，且会在几分钟内把时钟重置回 00:00，
#   所以必须定时重发。星闪方案的接收器没有底座/时钟命令，无此功能。
# ===========================================================================

VENDOR_ID = 0x3151
VENDOR_USAGE_PAGE = 0xFFFF
VENDOR_USAGE = 0x02
STATUS_REPORT_ID = 0x05
POLL_REPORT_ID = 0x00
POLL_OPCODE = 0xF7
REPORT_LEN = 65  # report id + 64 bytes payload
SETTLE_SECONDS = 0.05

CLOCK_COMMAND = 0x28
CLOCK_MARKER = 0xD7

_IO_LOCK = threading.Lock()

WIRELESS_PIDS = {0x5007, 0x4027, 0x5C2F}
WIRED_PIDS = {0x4026, 0x4006, 0x5008}


def _group_devices() -> dict[int, dict]:
    """按 PID 归并当前在线的 AJAZZ HID 设备，并挑出可用的厂商控制集合。"""
    devices: dict[int, dict] = {}
    for d in hid.enumerate(VENDOR_ID, 0):
        pid = d["product_id"]
        entry = devices.setdefault(
            pid, {"product": d.get("product_string") or "", "vendor": None}
        )
        if d.get("usage_page") != VENDOR_USAGE_PAGE:
            continue
        # 优先 usage=0x02 的控制集合：只有它能读出状态帧
        if d.get("usage") == VENDOR_USAGE:
            entry["vendor"] = d
        elif entry["vendor"] is None or entry["vendor"].get("usage") != VENDOR_USAGE:
            entry["vendor"] = d
    return devices


def _poll_status_link(handle) -> bool:
    """发送厂商状态轮询（无载荷），拉起 2.4G 电量回传链路。"""
    frame = bytearray(REPORT_LEN)
    frame[0] = POLL_REPORT_ID
    frame[1] = POLL_OPCODE
    try:
        handle.send_feature_report(bytes(frame))
        return True
    except OSError:
        return False


def _parse_frame(data: bytes) -> int | None:
    """解析 0x05 帧，返回电量；帧无效或链路未就绪返回 None。"""
    if len(data) < 4:
        return None
    charge = 3 if data[0] == STATUS_REPORT_ID else 2
    # 有效帧：report id 与电量之间必须全 0（重连瞬间会返回脏数据）
    if any(data[1:charge]):
        return None
    percent = data[charge]
    if percent == 0 or percent > 100:
        return None  # 0 = 链路尚未就绪，不能当作 0%
    return percent


def _read_once(collection: dict) -> tuple[int, bytes] | None:
    with _IO_LOCK:
        handle = hid.device()
        try:
            handle.open_path(collection["path"])
        except OSError:
            return None
        try:
            _poll_status_link(handle)
            time.sleep(SETTLE_SECONDS)
            data = bytes(handle.get_feature_report(STATUS_REPORT_ID, REPORT_LEN))
        except OSError:
            return None
        finally:
            try:
                handle.close()
            except Exception:
                pass

    percent = _parse_frame(data)
    return None if percent is None else (percent, data)


def build_clock_frame(when: time.struct_time | None = None) -> bytes:
    """构造 SET_OLEDCLOCK 帧（65 字节：report id + 64）。"""
    t = when or time.localtime()
    frame = bytearray(REPORT_LEN)
    frame[0] = 0x00  # report id（该接口不声明 report id，必须为 0）
    frame[1] = CLOCK_COMMAND  # payload[0] = 0x28
    frame[8] = CLOCK_MARKER  # payload[7] = 0xD7，缺了固件静默丢弃
    frame[9] = (t.tm_year >> 8) & 0xFF  # 年，大端
    frame[10] = t.tm_year & 0xFF
    frame[11] = t.tm_mon
    frame[12] = t.tm_mday
    frame[13] = t.tm_hour
    frame[14] = t.tm_min
    frame[15] = t.tm_sec
    return bytes(frame)


def _find_dock_collection() -> dict | None:
    """底座 = Sonix 2.4G 接收器；只有它有 OLED 时钟。"""
    for pid, info in _group_devices().items():
        if info["vendor"] is not None and (
            pid in WIRELESS_PIDS or "2.4G" in info["product"].upper()
        ):
            return info["vendor"]
    return None


def set_dock_clock(when: time.struct_time | None = None) -> bool:
    """把当前时间推到底座 OLED。设备不在线或发送失败返回 False。"""
    collection = _find_dock_collection()
    if collection is None:
        return False

    with _IO_LOCK:
        handle = hid.device()
        try:
            handle.open_path(collection["path"])
        except OSError:
            return False
        try:
            # 固件上电后约 2 秒才开始接受该帧，太早发会被静默丢弃
            time.sleep(0.05)
            handle.send_feature_report(build_clock_frame(when))
            return True
        except OSError:
            return False
        finally:
            try:
                handle.close()
            except Exception:
                pass


def _read_sonix(attempts: int = 2) -> Status:
    devices = _group_devices()
    if not devices:
        return Status(STATE_ABSENT)

    # 1) 无线接收器：可读真实百分比
    for pid, info in devices.items():
        if pid in WIRELESS_PIDS or "2.4G" in info["product"].upper():
            if info["vendor"] is None:
                continue
            for _ in range(attempts):
                result = _read_once(info["vendor"])
                if result:
                    percent, raw = result
                    return Status(
                        STATE_WIRELESS,
                        percent=percent,
                        raw=raw,
                        product=info["product"],
                        path=_path_str(info["vendor"]["path"]),
                    )
                time.sleep(0.1)
            return Status(STATE_SLEEPING, product=info["product"])

    # 2) 有线 / 插线充电：优先读电量，读不到就只报充电状态
    for pid, info in devices.items():
        if pid in WIRED_PIDS:
            percent = raw = None
            if info["vendor"] is not None:
                result = _read_once(info["vendor"])
                if result:
                    percent, raw = result
            return Status(
                STATE_CHARGING,
                percent=percent,
                raw=raw,
                product=info["product"],
            )

    # 3) 设备在但读不到
    for info in devices.values():
        if info["vendor"] is not None:
            return Status(STATE_SLEEPING, product=info["product"])
    return Status(STATE_SLEEPING)


# ===========================================================================
# 统一入口
# ===========================================================================


def dock_clock_available() -> bool:
    """底层是否是带 OLED 底座的 Sonix 接收器（星闪接收器没有这个功能）。"""
    return _find_dock_collection() is not None


def active_protocol() -> str:
    """当前接的是哪套协议：'nearlink' / 'sonix' / ''（都没有）。"""
    if _nl_nodes():
        return PROTO_NEARLINK
    if _group_devices():
        return PROTO_SONIX
    return ""


def read_status(attempts: int = 2) -> Status:
    nearlink = _read_nearlink(attempts)
    if nearlink is not None:
        return nearlink
    return _read_sonix(attempts)


if __name__ == "__main__":
    import sys

    if "--watch" in sys.argv:
        # 每 2 秒读一次，用来确认「动一下鼠标就能读到」的排查场景
        try:
            while True:
                s = read_status()
                percent = f"{s.percent}%" if s.percent is not None else "-"
                print(f"{time.strftime('%H:%M:%S')}  {s.state:<9}{percent}", flush=True)
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        raise SystemExit(0)

    proto = active_protocol()
    status = read_status()
    print(f"协议: {proto or '未检测到'}")
    print(f"状态: {status.state}")
    if status.percent is not None:
        print(f"电量: {status.percent}%")
    if status.product:
        print(f"设备: {status.product}")
    if status.raw:
        print(f"原始帧: {status.raw.hex(' ')}")
