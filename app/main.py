# -*- coding: utf-8 -*-
"""GwentTracker — 昆特牌对局记录与对手卡组预测工具。

原理：内存读取游戏对局数据，自动记录对手出牌并进行卡组预测。
"""
import ctypes
import hashlib
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog, simpledialog

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if getattr(sys, "frozen", False):
    # 打包为 exe 后：配置/历史/卡库等数据放在 exe 同级目录，而非临时解压目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from card_db import build_or_load, find_game_dir  # noqa: E402
import capture  # noqa: E402
import deck_predict  # noqa: E402
from glass import apply_caption, apply_glass  # noqa: E402

try:
    import customtkinter as ctk   # 圆角现代控件（CTkFrame/CTkEntry/CTkSwitch…）
    ctk.set_appearance_mode("light")   # 颜色全部显式传入，仅关闭系统跟随
except Exception:  # noqa: BLE001
    ctk = None                   # 环境缺失时退回原生 tk，功能不受影响

if ctk is not None:
    # 兼容垫片：旧代码大量使用 widget.cget("bg")，CTk 控件不支持该参数，
    # 这里映射到 fg_color（统一传主题色字符串），保证混用时不出错
    _ctk_orig_cget = ctk.CTkBaseClass.cget

    def _ctk_cget_compat(self, param):
        if param == "bg":
            try:
                fc = _ctk_orig_cget(self, "fg_color")
            except ValueError:
                return ""
            if isinstance(fc, str):
                return fc
            return fc[1] if len(fc) > 1 else fc[0]
        return _ctk_orig_cget(self, param)

    ctk.CTkBaseClass.cget = _ctk_cget_compat

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_CACHE = os.path.join(BASE_DIR, "cards_zh.json")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
LOCAL_DECKS_PATH = os.path.join(BASE_DIR, "decks_local.json")   # 用户录入卡组


def _bundled_path(name):
    """exe 内置资源路径：打包后位于 PyInstaller 解压目录（_MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    return os.path.join(base or BASE_DIR, name)


# 应用图标：优先 exe 同级（增量更新可单独下发），缺失时用打包内置资源。
# 之前只找同级文件，增量更新的老用户目录里没有 icon.ico，导致窗口/
# 任务栏回退到 Tk 默认羽毛图标。
ICON_PATH = os.path.join(BASE_DIR, "icon.ico")
ICON_BUNDLED = _bundled_path("icon.ico")
_app_icon = None    # 延迟判定结果缓存


def app_icon_path():
    global _app_icon
    if _app_icon is None:
        _app_icon = (ICON_PATH if os.path.exists(ICON_PATH)
                     else ICON_BUNDLED if os.path.exists(ICON_BUNDLED)
                     else "")
    return _app_icon


def apply_window_icon(win):
    """统一设置窗口/任务栏图标。

    iconbitmap 在部分环境下对任务栏不生效或因 ICO 格式报错，
    因此再用 PIL→iconphoto 兜底一次（PhotoImage 引用挂在窗口上防 GC）。
    """
    _icon = app_icon_path()
    if not _icon:
        return
    try:
        win.iconbitmap(_icon)
    except tk.TclError:
        pass
    try:
        from PIL import Image, ImageTk
        ph = getattr(win, "_app_icon_ph", None)
        if ph is None:
            ph = ImageTk.PhotoImage(Image.open(_icon))
        win._app_icon_ph = ph
        win.iconphoto(True, ph)
    except Exception:  # noqa: BLE001
        pass


BUILTIN_DECKS = _bundled_path("decks_builtin.json")   # 随 exe 内置的热门卡组库

# 中文/英文阵营名 → 预测库阵营代码（录入卡组时统一存储格式）
FAC_CODE = {"怪兽": "mon", "尼弗迦德": "nil", "北方领域": "nor",
            "松鼠党": "sco", "史凯利格": "ske", "辛迪加": "syn", "中立": "neu",
            "SK": "ske", "NG": "nil", "NR": "nor", "SC": "sco", "SY": "syn"}

FAC_ZH = {"mon": "怪兽", "nil": "尼弗迦德", "nor": "北方领域",
          "sco": "松鼠党", "ske": "史凯利格", "syn": "辛迪加", "neu": "中立"}

# 阵营小图标（彩色圆点 emoji，卡组库/录入列表用，更直观）
FAC_ICON = {"mon": "\U0001F534", "nil": "\u26AB", "nor": "\U0001F535",
            "sco": "\U0001F7E2", "ske": "\U0001F7E0", "syn": "\U0001F7E3",
            "neu": "\u26AA"}


def fac_disp(f):
    """阵营代码 → 「图标 + 中文名」展示（未知阵营降级为纯文本）。"""
    code = fac_code(f) if f not in FAC_ZH else f
    icon = FAC_ICON.get(code)
    zh = FAC_ZH.get(code)
    if icon and zh:
        return "%s %s" % (icon, zh)
    return zh or code or "—"


def fac_code(f):
    """任意阵营表述 → 库内阵营代码。"""
    if not f:
        return ""
    return f if f in ("mon", "nil", "nor", "sco", "ske", "syn", "neu") \
        else FAC_CODE.get(f, f)


def load_local_decks():
    """读取用户录入的本地卡组（decks_local.json，与历史/缓存同目录）。"""
    try:
        with open(LOCAL_DECKS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return [x for x in (d.get("decks") or []) if x.get("cards")]
    except Exception:  # noqa: BLE001
        return []


def save_local_decks(decks):
    try:
        with open(LOCAL_DECKS_PATH, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "decks": decks},
                      f, ensure_ascii=False, indent=1)
    except OSError as e:
        raise OSError("保存本地卡组库失败：%s" % e)

DEFAULT_CONFIG = {
    "game_dir": "",
    "pred_ladder": True,        # 卡组预测仅用于天梯/训练模式（其他模式可关）
    "deck_limit": 165,
    "dock_to_game": False,      # 主窗口自动停靠在游戏右侧（默认关）
    "theme": "hud",             # 主题 key，见 THEMES（hud = 猎魔 HUD）
    "pred_overlay": True,       # 对手卡组预测悬浮窗
    "pred_dx": None,            # 悬浮窗相对游戏右缘的 X 偏移（拖动后保存）
    "pred_dy": None,            # 悬浮窗相对游戏顶缘的 Y 偏移
    "pred_opacity": 0.95,       # 悬浮窗透明度（0.3-1.0）
    "pred_follow": False,       # 悬浮窗跟随游戏窗口移动（默认关）
    "pred_scale": 1.0,          # 悬浮窗缩放（0.7-1.5，设置中调整/拖角缩放）
    "pred_resizable": False,    # 允许拖右下角自由缩放悬浮窗（默认关）
    "hotkeys": {},              # 全局热键 {overlay/main/details: "Ctrl+Alt+X"}
    "pred_fail_limit": 6,       # 连续 N 张牌无匹配则提示“无法预测”并停止显示
    "deck_fetch_limit": 120,    # 从服务端更新卡组库的获取数量（20-240）
    "close_action": None,       # 关闭按钮行为：None=每次询问 / "close"=关闭 / "minimize"=最小化
    "art_full": False,          # 完整卡图资源是否已下载到本地
    "art_prompted": False,      # 是否已询问过完整卡图下载（仅首次启动询问）
    "risk_agreed": False,       # 首次启动风险提示：是否已同意（不同意则退出）
}

APP_VERSION = "2.2.1"            # 发布新版本时同步更新此处与 release\version.json

# UI 缩放系数 = 主屏 DPI / 96（150% 缩放 → 1.5），main() 创建 root 后设置。
# Canvas 手绘布局（悬浮窗/卡组明细）的固定像素尺寸据此放大；
# 字体为 point 尺寸由 Tk 随 DPI 自动放大，二者必须一致才不会文本溢出。
UI_SCALE = 1.0
UPDATE_URL = "http://47.116.199.195/gwent/app/manifest.json"   # 增量更新清单，用户不可改
DOWNLOAD_PAGE = "https://ntemews.cn/gwent/landing/"            # 官网（关于窗口「进入官网」）

# 战场实时点数（内存读取的场上力量）当前显示不准，暂时下线；修复后改回 True 即可
POINTS_ONLINE = False

THEMES = {
    "hud": {       # 猎魔 · HUD（现代游戏 HUD 风格，v1.8.0 默认）
        # 底色 #0E0F14；以下 rgba 均已在 #0E0F14 上合成后写死：
        "bg": "#0E0F14",
        "card": "#13131C",     # 面板 = rgba(20,20,30,0.75) 合成
        "sub": "#1D1E23",      # = rgba(255,255,255,0.06) 合成
        "fg": "#E8E6E1", "dim": "#8A8599",
        "accent": "#C9A84C",   # 尼弗迦德金（主强调）
        "accent2": "#E2C97E",  # 金色悬停提亮（派生色，仅 hover 用）
        "accent_fg": "#FFFFFF",  # 一级按钮白色图标
        "mine": "#4A7DB3",     # 我方 · 北方蓝
        "opp": "#B85450",      # 敌方 · 腐血橙
        "ok": "#58B368",       # 已连接状态灯（绿色呼吸）
        "warn": "#C9A84C", "err": "#B85450",
        "border": "#212227",   # = rgba(255,255,255,0.08) 合成
        "head_bg": "#15161B",  # = rgba(255,255,255,0.03) 合成
        "row_odd": "#131419",  # = rgba(255,255,255,0.02) 合成
        "row_sel": "#26272E",  # = rgba(255,255,255,0.10) 合成
        "gold": "#C9A84C",
        "btn_bg": "#1D1E23", "btn_act": "#26272E",
        "dark": True,
    },
    "light": {
        "bg": "#f3ede2", "card": "#faf6ee", "sub": "#f2ead9",
        "fg": "#3b3730", "dim": "#8d8474",
        "accent": "#b8863b", "accent2": "#d3a55f", "accent_fg": "#fffaf2",
        "mine": "#5f8fa8", "border": "#ded2bd",
        "head_bg": "#f1e9d8", "row_odd": "#f6efe1", "row_sel": "#e6d7ba",
        "gold": "#a8751b",
        "btn_bg": "#ffffff", "btn_act": "#ffffff",
        "dark": False,
    },
    "dark": {
        "bg": "#1b1c1f", "card": "#232428", "sub": "#2c2e33",
        "fg": "#e9e3d6", "dim": "#8d8578",
        "accent": "#d3a55f", "accent2": "#e5c078", "accent_fg": "#1b1c1f",
        "mine": "#86b4c9", "border": "#3c3e44",
        "head_bg": "#2b2d32", "row_odd": "#26282c", "row_sel": "#3a3c42",
        "gold": "#f5c451",
        "btn_bg": "#33353b", "btn_act": "#45474e",
        "dark": True,
    },
    "emerald": {   # 翡翠 · 松鼠党
        "bg": "#16201a", "card": "#1d2a22", "sub": "#25352b",
        "fg": "#e2ead9", "dim": "#8fa392",
        "accent": "#4caf6d", "accent2": "#6fd08a", "accent_fg": "#10241a",
        "mine": "#6fb3d8", "border": "#2e4234",
        "head_bg": "#223228", "row_odd": "#1f2d25", "row_sel": "#31473a",
        "gold": "#e5c078",
        "btn_bg": "#2a3c31", "btn_act": "#35493c",
        "dark": True,
    },
    "ocean": {     # 深海 · 史凯利格
        "bg": "#131a24", "card": "#1a2330", "sub": "#212c3c",
        "fg": "#dfe7f2", "dim": "#8695ab",
        "accent": "#4f8fc4", "accent2": "#6fb0dd", "accent_fg": "#0e1a26",
        "mine": "#67c2a5", "border": "#2c3a4d",
        "head_bg": "#1e2937", "row_odd": "#1b2532", "row_sel": "#2b3a4d",
        "gold": "#e8c268",
        "btn_bg": "#263444", "btn_act": "#304052",
        "dark": True,
    },
    "violet": {    # 暮紫 · 辛迪加
        "bg": "#1b1722", "card": "#241e2e", "sub": "#2d2539",
        "fg": "#e8e0f0", "dim": "#9a8fae",
        "accent": "#9b6fd0", "accent2": "#b78fe0", "accent_fg": "#1c1226",
        "mine": "#6fb3d8", "border": "#3a3050",
        "head_bg": "#292238", "row_odd": "#262035", "row_sel": "#372e4a",
        "gold": "#eec86f",
        "btn_bg": "#322a42", "btn_act": "#3e3452",
        "dark": True,
    },
    "crimson": {   # 绯红 · 尼弗迦德
        "bg": "#201619", "card": "#291c20", "sub": "#332328",
        "fg": "#ecdfd9", "dim": "#a28a84",
        "accent": "#c8554d", "accent2": "#e07a6e", "accent_fg": "#2a1210",
        "mine": "#7fb0c9", "border": "#453034",
        "head_bg": "#2f2125", "row_odd": "#2b1e22", "row_sel": "#433036",
        "gold": "#eeb64f",
        "btn_bg": "#38262b", "btn_act": "#452f35",
        "dark": True,
    },
}

THEME_ORDER = ["hud", "light", "dark", "emerald", "ocean", "violet", "crimson"]
THEME_NAMES = {
    "hud": "猎魔", "light": "浅色", "dark": "暗色", "emerald": "翡翠",
    "ocean": "深海", "violet": "暮紫", "crimson": "绯红",
}

# 以下颜色为当前主题的全局变量（主题切换时由 set_theme 重新赋值）
BASE = BG = "#f3ede2"
BG2 = "#faf6ee"
BG3 = "#f2ead9"
FG = "#3b3730"
FG_DIM = "#8d8474"
ACCENT = "#b8863b"
ACCENT2 = "#d3a55f"
ACCENT_FG = "#fffaf2"
MINE = "#5f8fa8"
OK = "#4c8a4c"
WARN = "#a87722"
ERR = "#b5514e"
BORDER = "#ded2bd"
HEAD_BG = "#f1e9d8"
ROW_ODD = "#f6efe1"
ROW_SEL = "#e6d7ba"
GOLD_FG = "#a8751b"
BTN_BG = "#ffffff"
BTN_ACT = "#ffffff"
OPP = "#b8863b"        # 敌方色（HUD 主题 = 腐血橙 #B85450；旧主题随 accent）

# 表面分层令牌（“实色 + 明暗分层 + 1px 高光”）：由 set_theme 按主题派生
SURF1 = BG2        # 面板表面（= BG2）
SURF2 = BG3        # 抬升表面：chip / 表头 / 按钮
EDGE = BORDER      # 面板 1px 外描边（比 BORDER 亮一档）
EDGE_SOFT = BORDER # 面板内部细分隔线
GLOSS = "#FFFFFF"  # 面板顶部 1px 受光高光
SHADE = "#000000"  # 面板底部 1px 暗边


def set_theme(name):
    """切换主题：重新绑定全局颜色变量（调用后需要重建界面）。"""
    global BASE, BG, BG2, BG3, FG, FG_DIM, ACCENT, ACCENT2, ACCENT_FG
    global MINE, BORDER, HEAD_BG, ROW_ODD, ROW_SEL, GOLD_FG, BTN_BG, BTN_ACT
    global OPP, OK, WARN, ERR
    pal = THEMES.get(name) or THEMES["light"]
    BASE = BG = pal["bg"]
    BG2 = pal["card"]
    BG3 = pal["sub"]
    FG = pal["fg"]
    FG_DIM = pal["dim"]
    ACCENT = pal["accent"]
    ACCENT2 = pal["accent2"]
    ACCENT_FG = pal["accent_fg"]
    MINE = pal["mine"]
    OPP = pal.get("opp", pal["accent"])
    OK = pal.get("ok", "#4c8a4c")
    WARN = pal.get("warn", "#a87722")
    ERR = pal.get("err", "#b5514e")
    BORDER = pal["border"]
    HEAD_BG = pal["head_bg"]
    ROW_ODD = pal["row_odd"]
    ROW_SEL = pal["row_sel"]
    GOLD_FG = pal["gold"]
    BTN_BG = pal["btn_bg"]
    BTN_ACT = pal["btn_act"]
    _apply_surfaces(bool(pal.get("dark")))


def _apply_surfaces(dark):
    """按当前 BG2 派生表面分层：SURF2 / EDGE / EDGE_SOFT / GLOSS / SHADE。

    暗色主题靠“向白提亮”制造受光面；浅色主题靠“向暗压深”制造阴影面，
    这样同一套令牌在 7 个主题下都能得到正确的明暗关系。
    """
    global SURF1, SURF2, EDGE, EDGE_SOFT, GLOSS, SHADE
    SURF1 = BG2
    if dark:
        SURF2 = _lift(BG2, 0.100)
        EDGE = _lift(BG2, 0.160)
        EDGE_SOFT = _lift(BG2, 0.080)
        GLOSS = _lift(BG2, 0.260)
        SHADE = _drop(BG2, 0.500)
    else:
        SURF2 = _drop(BG2, 0.035)
        EDGE = _drop(BG2, 0.120)
        EDGE_SOFT = _drop(BG2, 0.060)
        GLOSS = "#FFFFFF"
        SHADE = _drop(BG2, 0.080)


HELP_TEXT = (
    "GwentTracker（全自动）\n"
    "1. 启动游戏，本工具自动附加进程（状态显示“已连接”）\n"
    "2. 进入对局后自动开始记录：对手每打出一张牌立即计入，\n"
    "   小局切换自动识别，对局结束自动保存历史\n"
    "3. 中途打开本工具也会自动同步整局历史记录\n"
    "4. 双方领袖人口自动读取，各自人口上限 = 150 + 各自领袖人口，\n"
    "   无需手动设置；卡牌人口同样以游戏实时数据为准，\n"
    "   不受本地卡库过期（平衡补丁）影响\n"
    "\n"
    "卡组预测\n"
    "- 卡组预测仅支持天梯 / 训练模式，其他模式不适用：\n"
    "  点主窗口“对手卡组预测”面板右上角「模式:天梯/训练」按钮\n"
    "  可切换为其他模式（预测与悬浮窗将自动关闭）\n"
    "- 连续多张牌都匹配不上卡组库时（张数可在设置中调整），\n"
    "  悬浮窗会提示“无法预测”并停止显示预测结果，避免误导\n"
    "- 悬浮窗可在设置中关闭“跟随游戏窗口”，关闭后可拖到任意位置固定\n"
    "- 悬浮窗统计区显示双方人口进度条，人口随出牌实时变化；\n"
    "  对手打出新牌时对应卡牌行会高亮闪烁\n"
    "- 右键悬浮窗卡牌行可手动「标记为已打出 / 未打出」，也可右键\n"
    "  模糊搜索卡名手动添加（修正直接进墓地等不上场效果的计数）；\n"
    "  新对局开始时手动标记自动清除\n"
    "\n"
    "常见报错与解决\n"
    "- 状态一直“未连接”：请先启动游戏；若游戏以管理员身份运行，\n"
    "  本工具也需要以管理员身份运行（右键 → 以管理员身份运行）\n"
    "- 提示“附加游戏进程失败”：游戏刚更新后内部结构可能变化，\n"
    "  请等待版本适配；也可重启本工具后重试\n"
    "- 卡牌库加载失败 / 卡组预测不准：需联网；点主窗口\n"
    "  「更新卡组库」拉取最新热门卡组后重试\n"
    "- 悬浮窗卡图不显示：需联网下载卡面原画，网络恢复后会\n"
    "  自动重试；建议在「设置 → 卡图资源」一次性下载完整卡图包，\n"
    "  之后即可离线使用；也可删除安装目录 cards_art 文件夹重新下载\n"
    "- 检查更新失败 / 下载不动：多为网络波动，稍后在「关于」\n"
    "  窗口点「检查更新」重试；多次失败可到下载页重新下载\n"
    "  安装包，解压覆盖安装（历史记录与设置不会丢失）\n"
    "- 杀毒软件报毒 / 文件被删除：本工具只读取游戏内存数据，\n"
    "  不写入不修改，请在杀毒软件中添加信任目录后重新解压\n"
    "- 悬浮窗一直不出现：确认设置中悬浮窗开关已打开；\n"
    "  进入对局后会自动显示，若仍不出现请重启本工具\n"
    "- 界面卡住无响应：附加进程瞬间可能短暂卡顿，等待即可；\n"
    "  长时间无响应请重启软件（当前对局会自动保存）\n"
    "\n"
    "其他\n"
    "- 本工具只读取游戏内存数据，不写入、不修改游戏代码\n"
    "- 游戏更新后若读不到数据，用 Il2CppDumper 重新 dump 并\n"
    "  更新 offsets.json 中的偏移即可\n"
    "- 本工具与游戏进程交互仅限读取，请自行评估使用风险"
)

ABOUT_TEXT = (
    "GwentTracker v%s\n"
    "昆特牌对局记录与对手卡组预测工具\n"
    "\n"
    "主要功能\n"
    "- 全自动记录双方出牌与人口，小局自动分段\n"
    "- 对手卡组实时预测悬浮窗（卡图缩略 / 已出灰化 / 可拖动）\n"
    "- 双方人口进度条对比，实时掌握双方资源\n"
    "- 7 款界面主题：猎魔 HUD / 浅色 / 暗色 / 翡翠 / 深海 / 暮紫 / 绯红\n"
    "- 内置 276 套卡组库（含月报组十月推荐 36 套），可从服务端更新最新卡组\n"
    "- 对局历史自动保存（history 目录）\n"
    "- 卡组预测支持：天梯 / 训练模式\n"
    "\n"
    "说明\n"
    "- 仅读取游戏内存数据，不写入、不修改游戏代码\n"
    "- 与官方无关联，使用风险请自行评估\n"
    "- 数据来源：playgwent 官方热门卡组库"
)

CHANGELOG = (
    "更新日志\n"
    "\n"
    "v2.2.1（2026-11-12）\n"
    "- 修复：切换主题后窗口四周与面板缝隙残留旧主题的深色边\n"
    "- 修复：切换主题后「对局信息」不显示对手/回合/实时比分\n"
    "- 修复：Steam 版客户端被误识别成 GOG（改为按进程路径与\n"
    "  实际加载的平台 DLL 判定，steam_api 优先）\n"
    "\n"
    "v2.2.0（2026-11-12）\n"
    "- 界面整体重做：面板/控件/凹槽三级明暗分层 + 1px 顶部高光，\n"
    "  金色只留给品牌与主操作，层级一眼可辨\n"
    "- 标题栏换成主题底色、窗口圆角生效，不再出现灰白标题栏\n"
    "- 状态区改为胶囊标签（游戏/识别/对局），未开局时对局信息\n"
    "  自动收起，不再留空面板\n"
    "- 人口对比、出牌记录表格重新排版：数字对齐、进度条加粗、\n"
    "  表头降级为次级文字、列宽自适应\n"
    "- 「清空(不保存)」改为红色警示样式，与普通按钮区分\n"
    "- 修复：浅色主题切换时报 unknown option 弹错误框\n"
    "\n"
    "v2.1.5（2026-11-10）\n"
    "- 小局点数/胜负改为内存权威读取：对手投降、断线等比分不完整\n"
    "  场景也能准确判定胜负，历史记录附带结束原因\n"
    "- 修复：场上点数漏算效果生成的复制牌；开局先手方虚加 1 点\n"
    "- 对局信息栏美化：双色实时比分 + 局分计数 + 回合归属；\n"
    "  未开局时显示「开启预测请点悬浮窗按钮」提示\n"
    "- 出牌记录新增小局筛选（全部/第1局/第2局/第3局）\n"
    "- 识别栏新增 GOG / Steam 内存模式就绪状态（按运行进程判定）\n"
    "\n"
    "v2.1.3（2026-11-06）\n"
    "- 对手段位显示修正：普通天梯玩家显示「段位 X 级」（1-40），\n"
    "  仅职天玩家显示 MMR 分数\n"
    "- 点数改为领先/落后表述：我方领先 X 分 / 我方落后 X 分 / 比分持平\n"
    "\n"
    "v2.1.2（2026-11-06）\n"
    "- 对局信息行：删除 Lv 等级字段（无用且不准）\n"
    "- 对局信息行：新增双方实时点数（点数 12:15，随出牌即时刷新）\n"
    "- 天梯分数显示统一为「天梯 X 分」\n"
    "\n"
    "v2.1.1（2026-11-06）\n"
    "- 修复：进入对局后 readPlayerInfo 报 missing argument ——\n"
    "  漏定义 HasPassed/HasEndedTurn 偏移导致对局信息读取失败，\n"
    "  昵称/分数/模式识别随之全部失效\n"
    "- 主界面「卡组预测」面板改为「对局信息」：环形进度与候选\n"
    "  卡片流删除，预测功能全部由悬浮窗承载\n"
    "\n"
    "v2.1.0（2026-11-06）\n"
    "- 新增对局信息读取：对手昵称、职天分数、等级、回合归属\n"
    "  （卡组预测下方一栏升级为实时对局信息行）\n"
    "- 游戏栏显示模式识别：对局中·天梯 / 训练 / 休闲/友谊\n"
    "  （职天对手显示天梯分数，普通模式不显示分数）\n"
    "- 主页新增时段问候语：读取你的游戏昵称——\n"
    "  「早上好，XX · 今天也来打一局昆特牌吧！」\n"
    "- 对局记录保存对手昵称/模式/双方天梯分，供后续统计\n"
    "\n"
    "v2.0.6（2026-11-06）\n"
    "- 修复：打开胜率统计报错 NameError（趋势图表引用了不存在的颜色\n"
    "  常量 SUB，应为 BG3）\n"
    "\n"
    "v2.0.5（2026-11-06）\n"
    "- 胜率统计：阵营表固定显示全部六大阵营（未对阵过的显示 0 场 / —）\n"
    "- 胜率统计：近期趋势改为图表 —— 金色折线为累计胜率走势，\n"
    "  圆点颜色区分单场胜/负/平\n"
    "\n"
    "v2.0.4（2026-11-06）\n"
    "- 修复：设置页开关在部分环境下显示为系统白色方块（CTkSwitch 构建\n"
    "  失败回退）——统一改为游戏风格绿色方块勾选框，不再依赖 CTk\n"
    "\n"
    "v2.0.3（2026-11-06）\n"
    "- 修复：胜率统计「对阵各阵营」底部阵营被截断 —— 我方/对阵两表\n"
    "  改为左右并排，一屏可见全部阵营\n"
    "- 新增：复选框改为游戏风格 —— 深色空心方块 / 绿色实心方块+白勾\n"
    "  （录入卡组勾选、关闭询问；设置页开关选中色同步改为绿色）\n"
    "\n"
    "v2.0.2（2026-11-05）\n"
    "- 修复：胜率统计/设置窗口内容过高时显示不全（对阵各阵营少了辛迪加、\n"
    "  底部按钮被挤出）——两窗口改为可滚动布局，底部按钮固定常驻\n"
    "- 新增：全局热键支持自定义（设置中点击输入框按下组合键，保存即生效）\n"
    "- 修复：关于窗口未显示 v2.0.1 版本更新说明（更新日志常量未更新）\n"
    "- 修复：出牌记录「局」列过窄导致文本显示不全\n"
    "\n"
    "v2.0.1（2026-11-05）\n"
    "- 新增：天梯胜率统计（总体胜率/阵营分布/连胜连负/近期趋势）\n"
    "- 新增：对局结果手动标记（胜/负/平），自动判定不准时可覆盖\n"
    "- 新增：全局热键 Ctrl+Alt+H/M/D，可在设置中自定义按键组合\n"
    "- 新增：悬浮窗自由缩放（设置开启后拖右下角，70%-150%）\n"
    "- 新增：崩溃自动记录（crash_log.txt）与反馈引导，官网新增反馈表单\n"
    "- 修复：对局详情比分恒为 1-0（小局切换瞬间点数被清零覆盖）\n"
    "- 优化：启动期未登录时读取卡组库的报错噪音静默\n"
    "\n"
    "v1.96（2026-11-05）\n"
    "- 修复：「抽 1 弃 1」类效果弃置的牌未计入人口与卡组预测——\n"
    "  抽上来的新牌不在开局实例集合，弃牌事件被过滤；现抽牌时补记\n"
    "  实例、弃牌不再过滤\n"
    "- 界面细节优化与若干问题修复\n"
    "\n"
    "v1.9.6（2026-11-05）\n"
    "- 修复：撕牌识别初始化报错（STR_MOVED 未声明，严格模式下赋值即抛\n"
    "  ReferenceError，导致整个内存读取 agent 无法就绪）\n"
    "\n"
    "v1.9.5（2026-11-05）\n"
    "- 新增：对手撕牌/弃牌（手牌→墓地）自动识别——内存读取 CardMovedHistoryEntry，\n"
    "  识别 From.Location=手牌 且 To.Location=墓地 时计入对手已出牌，\n"
    "  悬浮窗自动灰化对应卡牌、卡组预测同步扣减剩余库存\n"
    "\n"
    "v1.9.4（2026-11-05）\n"
    "- 修复：150% 等高 DPI 缩放下文本显示不全、悬浮窗布局错乱——\n"
    "  悬浮窗/卡组明细的行尺寸与卡图横幅、圆按钮现随系统缩放等比放大，\n"
    "  与字体缩放精确对齐\n"
    "\n"
    "v1.9.3（2026-11-05）\n"
    "- 卡组明细窗口升级为与悬浮窗一致的游戏风格卡组列表\n"
    "- 修复：部分卡图加载不出（卡组库卡图 URL 现已全量注入共享池，\n"
    "  卡组明细/手动添加等界面均可正常显示）\n"
    "- 修复：手动添加卡牌窗口「添加为已打出」按钮文字显示不全\n"
    "\n"
    "v1.9.2（2026-11-05）\n"
    "- 修复：新版游戏对局无法识别（一直显示未开始、对手出牌不记录）——\n"
    "  游戏更新移动了内存布局，现改为运行时按字段名动态解析偏移，\n"
    "  自动适配 GOG / Steam 新旧版本，日后游戏更新也无需等修复\n"
    "- 修复：「从游戏读取我的卡组」在新版游戏下失效\n"
    "\n"
    "v1.9.1（2026-11-05）\n"
    "- 改进：关于窗口「进入官网」指向正式域名\n"
    "  ntemews.cn/gwent/landing\n"
    "\n"
    "v1.8.16（2026-11-04）\n"
    "- 新增：悬浮窗卡组列表改版为游戏卡组列表风格——金色人口方块 +\n"
    "  卡名 + 右侧卡图横幅 + 右下角张数角标（x2/剩n/m/已出）\n"
    "- 新增：卡图「按每张卡的构图智能裁切」——逐卡分析原画细节密度\n"
    "  自动选取最佳取景窗（人物/主体优先），不再一律居中\n"
    "\n"
    "v1.8.15（2026-11-04）\n"
    "- 修复：任务栏/窗口图标显示为默认羽毛——应用图标现随程序内置，\n"
    "  增量更新的老用户（目录中没有图标文件）也能正常显示\n"
    "- 修复：「对局详情 → 录入本地卡组库」窗口内容显示不全（搜索框、\n"
    "  搜索结果与保存按钮被卡牌列表挤出窗口）；窗口加大为 600x720，\n"
    "  底部操作区固定，缩小窗口也不会丢失\n"
    "\n"
    "v1.8.14（2026-11-04）\n"
    "- 修复：「我的卡组库」窗口过小导致「从内存读取录入 / 删除选中」\n"
    "  等底部按钮显示不全；操作区改为固定在底部，列表加纵向滚动条\n"
    "- 改进：「从内存读取录入」改为复选框勾选（默认全选，可全选/清空），\n"
    "  自动跳过 6 套内置新手卡组（$ 开头）；每行显示阵营图标更直观\n"
    "- 新增：「我的卡组库」列表新增「领袖」字段；新增「重命名」按钮，\n"
    "  录入的卡组可自定义名称\n"
    "- 新增：「关于」窗口新增「进入官网」「复制官网地址」按钮\n"
    "- 修复：设置窗口「关闭按钮 / 界面主题」下拉框在部分主题下\n"
    "  文字与背景同色看不清（改为显式主题配色的原生下拉框）\n"
    "- 优化：主窗口整体缩小（680x940 → 640x880），减少占屏\n"
    "\n"
    "v1.8.13（2026-11-04）\n"
    "- 修复：点击「设置」再关闭后程序假死（只能强制结束）——根因是设置\n"
    "  窗口中开关控件传入了当前 UI 库不支持的参数导致构建中断，残留的\n"
    "  半初始化控件在关闭时阻断窗口销毁并锁死输入；现控件构建整体兜底，\n"
    "  任何失败自动回退原生控件，设置窗口保证完整、可正常关闭\n"
    "- 改进：「我的卡组库」新增「从内存读取录入」——在游戏里创建好卡组\n"
    "  后，点按钮直接读取游戏内全部卡组，勾选需要的卡组一键录入，\n"
    "  重复录入自动去重（不再依赖对局结束后录入）\n"
    "- 移除：出牌记录框的狼头装饰图标（会遮挡卡牌文字）\n"
    "\n"
    "v1.8.12（2026-11-04）\n"
    "- 界面：CustomTkinter 全新美化（窗口/面板/控件圆角）\n"
    "- 新增：「我的卡组库」对局明细一键录入对手/我方卡组（可编辑卡牌），\n"
    "  录入卡组参与后续对局预测\n"
    "\n"
    "v1.8.11（2026-11-04）\n"
    "- 内置卡组库扩充至 312 套：新增月报组「十一月推荐」全阵营 36 套\n"
    "  主流卡组，按最新补丁人口校准\n"
    "\n"
    "v1.8.10（2026-11-04）\n"
    "- 内置卡组库扩充至 276 套：新增月报组「十月推荐」全阵营 36 套\n"
    "  主流卡组；增量更新清单纳入内置卡组库文件\n"
    "\n"
    "v1.8.9（2026-11-02）\n"
    "- 修复：对局结束后悬浮窗仍保留上局的人口对比与卡组预测——\n"
    "  现在从出牌历史中识别游戏的对局结束事件，结算画面一出现\n"
    "  就立即保存历史并清空悬浮窗，不再等到回到主菜单\n"
    "- 新增：首次启动弹出风险提示，需阅读并同意后才能使用\n"
    "  （仅首次，同意结果保存在配置中）\n"
    "- 调整：主窗口出牌记录表的卡牌列收窄，布局更紧凑\n"
    "- 调整：移除主窗口底部的「对局详情」按钮（标题栏按钮保留）\n"
    "\n"
    "v1.8.8（2026-11-02）\n"
    "- 修复：手动添加卡牌搜索不出结果——中文输入法提交候选词时\n"
    "  既不触发变量监听也不触发按键事件，现改为轮询检测输入变化，\n"
    "  任何输入方式都能实时出结果\n"
    "- 改进：手动添加卡牌的结果列表全新改版——每张卡显示\n"
    "  卡面缩略图 + 名称 + 阵营 + 人口，单击选中、双击直接添加，\n"
    "  支持滚轮滚动；卡图走统一共享池（下载过完整卡图包的\n"
    "  直接本地显示，未下载的自动联网获取）\n"
    "\n"
    "v1.8.7（2026-11-02）\n"
    "- 修复：手动添加卡牌的模糊搜索增强——单字搜索不再被拦截，\n"
    "  支持英文名模糊匹配（忽略空格与标点差异）；搜索异常会写入\n"
    "  日志便于排查\n"
    "- 修复：我方领袖被误计入卡组明细——领袖不属于卡组，同时\n"
    "  领袖技能使用不再被当作一张打出的卡计入对手卡组预测\n"
    "- 改进：返回游戏主菜单时立即清空悬浮窗的人口对比、卡组预测\n"
    "  与上局出牌记录（此前仅部分场景能识别主菜单状态，导致\n"
    "  上一局数据残留到下一局开始）\n"
    "\n"
    "v1.8.6（2026-11-02）\n"
    "- 修复：对局详情比分永远是 1-0、结果永远是获胜 —— 根因是\n"
    "  小局切换瞬间场上清空，0/0 点数先于回合事件到达，导致前几局\n"
    "  最终点数漏记；现记录最近一次非零点数作为快照兜底，\n"
    "  每一小局比分与整局胜负均正确记录\n"
    "- 修复：一局结束后悬浮窗仍显示上一局的人口对比与卡组预测；\n"
    "  现在对局结束即清空（此前悬浮窗隐藏时不清，再次显示仍残留）\n"
    "\n"
    "v1.8.5（2026-11-02）\n"
    "- 界面：卡组明细与悬浮窗中金卡改为金色加粗字体，铜卡为黑色字体\n"
    "  （带浅色描边保证深色卡图上可读）\n"
    "- 修复：对方打出的间谍（落在我方半场）不再被误计入我方卡组，\n"
    "  卡组明细与预测改为以出牌历史判定归属，不再按所在半场推断\n"
    "- 新增：悬浮窗右键卡牌行可手动「标记为已打出 / 未打出」，\n"
    "  修正直接进墓地等不上场效果导致的计数误差；已打出的牌也可\n"
    "  右键恢复为未打出\n"
    "- 新增：悬浮窗右键「手动添加卡牌」，支持卡名模糊搜索，\n"
    "  搜索到后一键补记为已打出；手动标记的卡牌行尾显示 ✎ 提示，\n"
    "  可一键清除全部标记，新对局开始时自动清空\n"
    "\n"
    "v1.8.4（2026-11-02）\n"
    "- 界面：Logo「GwentTracker」改用微软雅黑；重连按钮、呼吸灯、\n"
    "  预测匹配率圆盘、出牌表势力图标全部改为逐像素抗锯齿绘制，消除毛边\n"
    "- 新增：点标题栏关闭按钮时询问「关闭程序还是最小化」，可勾选\n"
    "  「记住我的选择」；后续行为也可在 设置 → 通用 中修改\n"
    "- 调整：主窗口自动停靠与悬浮窗跟随游戏窗口默认改为关闭\n"
    "  （老用户升级后一次性生效，可在设置中重新打开）\n"
    "- 设置窗口分组重排：通用 / 对局与预测 / 悬浮窗 / 卡图资源\n"
    "- 新增：「更新卡组库」先弹窗确认获取数量（默认 120 套）\n"
    "- 新增：标题栏新增「对局详情」圆形按钮（替换原最小化按钮）\n"
    "- 新增：对局详情支持按住 Ctrl/Shift 多选后批量删除\n"
    "- 新增：可一次性下载完整卡图资源包到本地，之后悬浮窗与对局详情\n"
    "  无需再逐张联网加载（首次启动时会询问是否下载）\n"
    "\n"
    "v1.8.3（2026-11-02）\n"
    "- 修复：出牌记录表永远空白的问题（打包环境下势力小图标构建失败，\n"
    "  表格渲染传入无效参数导致每张牌的渲染都中断；悬浮窗卡牌列表\n"
    "  也因此一直停在「等待对手出牌」——数据其实一直在正常记录）\n"
    "- 出牌表势力小图标改为纯 Tk 绘制，不再依赖 PIL\n"
    "- 修复：关闭程序时偶发长时间「未响应」（卡组库下载线程阻塞退出）\n"
    "\n"
    "v1.8.2（2026-11-02）\n"
    "- 修复：场上扫描事件无去重，同一张牌每 250ms 重复上报形成消息洪水，\n"
    "  拖累事件通道并给游戏进程带来额外压力（现在每局每张牌只上报一次）\n"
    "- 新增：运行诊断日志（agent_debug.log）与主线程卡死看门狗（hang_dump.log），\n"
    "  异常与界面无响应自动落盘，便于排查\n"
    "\n"
    "v1.8.1（2026-11-02）\n"
    "- 修复：游戏启动初期记牌器附加会导致读取线程静默死亡，\n"
    "  之后对手数据完全读不到的问题（现为延迟初始化 + 自动重连）\n"
    "- 修复：单个事件处理异常会中断事件轮询循环、\n"
    "  导致只记录第一张牌的问题\n"
    "\n"
    "v1.8.0（2026-11-02）\n"
    "- 全新「猎魔 HUD」主题（默认启用）：深色沉浸式界面 + 尼弗迦德金强调色，\n"
    "  参考现代游戏 HUD 风格重新设计全部面板\n"
    "- 标题区：金色衬线 Logo + 连接状态呼吸灯（绿色 2s 呼吸动画）+\n"
    "  重连/设置/主题/最小化四个 36px 圆形图标按钮\n"
    "- 人口对比改为左右分栏仪表盘：大号数字 0.3s 平滑过渡，\n"
    "  我方北方蓝 / 敌方腐血橙双色 6px 进度条 + 流光动画\n"
    "- 卡组预测：新增匹配率环形进度仪表，候选卡组改为横向卡片流，\n"
    "  支持鼠标滚轮横向滚动\n"
    "- 对局事件改为时间轴样式：彩色圆点区分我方（蓝）/敌方（橙）/异常（红）\n"
    "- 出牌记录表：卡名左侧新增势力小图标，行悬停高亮，\n"
    "  面板右下角狼学派线稿徽章水印装饰\n"
    "- 悬浮窗保留竖版样式，配色随猎魔 HUD 主题更新；\n"
    "  保持置顶与透明度可调\n"
    "- 全部主题统一使用新 HUD 布局：切换主题仅改变配色，\n"
    "  不再回退到旧版界面\n"
    "- 标题区圆形按钮新增鼠标悬浮说明（重连/设置/主题/最小化）\n"
    "- 对局事件新增预测结果记录：预测卡组变化、连续未匹配暂停时\n"
    "  均会记入时间轴，便于确认内存读取状态\n"
    "- 其余 6 款配色主题（浅色/暗色/翡翠/深海/暮紫/绯红）保留，\n"
    "  可用 ◐ 按钮随时切换\n"
    "\n"
    "v1.7.8（2026-11-02）\n"
    "- 我方卡组改为开局直接从内存读取真实卡牌（不再依赖卡组库匹配），\n"
    "  卡组明细与我方卡组显示完全准确\n"
    "- 对局详情「查看卡组明细」改为与悬浮窗相同的卡图行样式，\n"
    "  左右并排展示我方/对方完整卡组（不再使用纯文本）\n"
    "- 缩小出牌记录表格高度，主窗口布局更紧凑\n"
    "- 统计标签移至「对局事件」标题行右侧，窗口拉伸/缩小时\n"
    "  所有按钮始终完整显示\n"
    "\n"
    "v1.7.7（2026-11-02）\n"
    "- 对局详情支持手动删除记录（确认后删除，不可恢复）\n"
    "- 对局详情新增「查看卡组明细」：左右并排显示我方/对方\n"
    "  匹配到的完整卡组，逐张列出卡名、张数、人口，\n"
    "  ●已打出 ○未打出 ◆金卡，按人口从高到低排列\n"
    "- 对局结束后悬浮窗自动切换为「等待开始对局」，\n"
    "  并清空上一局的卡组预测与人口统计\n"
    "- 主窗口副标题改为「昆特助手」\n"
    "- 帮助窗口新增「常见报错与解决」章节：未连接、附加失败、\n"
    "  卡图不显示、更新失败、杀毒软件误报等问题的处理方法\n"
    "\n"
    "v1.7.6（2026-11-02）\n"
    "- 修复：v1.7.5 卡图无法加载的问题（打包缺少图像处理子模块导致\n"
    "  后台加载线程异常退出，现已修复并增加异常兜底）\n"
    "- 新增「对局详情」功能：\n"
    "  · 自动统计软件开启期间进行的对局数与总体胜率\n"
    "  · 对局列表：开局时间、双方阵营+领袖（含领袖人口）、小局比分、\n"
    "    胜负结果、对局总用时\n"
    "  · 对局详情：每小局双方最终点数、我方/对方卡组\n"
    "    （按各自已打出的牌 + 未打出的牌智能匹配）、结果与用时\n"
    "  · 旧版本历史文件自动兼容，可正常查看\n"
    "\n"
    "v1.7.5（2026-11-02）\n"
    "- 悬浮窗卡图画质重制：明暗/灰化改为像素级平滑混合，\n"
    "  彻底去掉旧的抖动点阵遮罩（卡图像素感的真正来源），\n"
    "  原画清晰通透，已出的牌改为低饱和灰化 + 弱化描边\n"
    "- 新增卡组库更新提醒：定期与官方热门榜对比，\n"
    "  检测到大规模更新（新卡组集中上线）时自动提示，\n"
    "  「更新卡组库」按钮同步显示“卡组库有更新！”\n"
    "- 进度条流动光效重做：主窗口与悬浮窗改为平滑渐变扫光，\n"
    "  帧率更顺滑，不再有生硬的白色竖线\n"
    "- 出牌高亮动画改为金色描边渐隐，画面更干净\n"
    "\n"
    "v1.7.4（2026-11-02）\n"
    "- 修复：悬浮窗可能一直不显示的问题——关闭“跟随游戏窗口”时，\n"
    "  若启动/进入对局瞬间游戏窗口尚未出现（如游戏还在加载、被最小化），\n"
    "  悬浮窗会永久卡在隐藏状态；现在游戏窗口出现后会自动完成首次\n"
    "  定位并正常显示\n"
    "\n"
    "v1.7.3（2026-10-30）\n"
    "- 修复：我方人口进度条偶发显示不正确——此前人口上限只按对手领袖\n"
    "  计算，双方领袖人口不同时我方上限就错了；现在双方领袖人口分别\n"
    "  读取、分别计算\n"
    "- 修复：对局刚开始瞬间（敌我身份未就绪时）出牌可能被记错方的\n"
    "  隐患，读取就绪前自动等待\n"
    "- 悬浮窗卡图全面转高清：彻底清理旧版本残留的低清卡图缓存，\n"
    "  自动重新下载高清原画\n"
    "\n"
    "v1.7.2（2026-10-30）\n"
    "- 修复：点击「立即更新」后进度条不动、下载不开始的问题\n"
    "- 更新窗口新增「取消更新」按钮，下载中可随时中止，\n"
    "  取消后自动清理临时文件，不改动任何现有文件\n"
    "- 更新窗口置顶显示，不再被其他窗口遮挡\n"
    "\n"
    "v1.7.1（2026-10-30）\n"
    "- 模拟更新测试：验证增量更新（只下载有变化的文件）、多线程分段\n"
    "  下载、更新进度窗口与旧版本残留自动清理\n"
    "\n"
    "v1.7.0（2026-10-30）\n"
    "- 软件改为安装包形式（解压即用），不再把全部文件整合进 exe\n"
    "- 新增增量更新：只下载有变化的文件，后续更新更快、更省流量\n"
    "- 更新下载改用多线程分段加速，速度大幅提升\n"
    "- 全新更新窗口：进度条 + 更新说明，下载期间自动锁定操作，\n"
    "  完成后自动重启生效\n"
    "- 更新完成后自动清理旧版本残留（.old / .bak / 临时文件）\n"
    "\n"
    "v1.6.2（2026-10-30）\n"
    "- 测试版本：内容与 v1.6.1 一致，用于验证自动更新流程\n"
    "\n"
    "v1.6.1（2026-10-30）\n"
    "- 进度条新增流动光效（主窗口与悬浮窗人口条、匹配率条）\n"
    "- 设置 / 帮助 / 关于窗口统一更换为主程序图标\n"
    "- 战场实时点数暂时下线（当前显示不准，修复后回归）\n"
    "\n"
    "v1.6.0（2026-10-30）\n"
    "- 点数改为当前小局实时点数，小局切换自动归零（不再跨小局累加）\n"
    "- 悬浮窗统计区改版：双方人口进度条 + 点数对比，修复文本重叠\n"
    "- 新增 6 款界面主题：浅色 / 暗色 / 翡翠 / 深海 / 暮紫 / 绯红\n"
    "- 内置 240 套热门卡组库（离线可用），支持从服务端更新，\n"
    "  获取数量可自行设置（默认 120）\n"
    "- 预测连续无匹配时提示“无法预测”并停止显示，阈值可在设置中调整\n"
    "- 新增“悬浮窗跟随游戏窗口”开关（设置中可关闭）\n"
    "- 对手出牌时悬浮窗对应卡牌行高亮闪烁，不再漏看\n"
    "- 修复卡组预测模式切换后文本不刷新的问题\n"
    "- 设置面板精简，移除 OCR 时代遗留参数\n"
    "\n"
    "v1.5.0（2026-10-30）\n"
    "- 移除 OCR 画面识别模式，改为纯内存读取（更稳定、无手动录入）\n"
    "- 正式更名为 GwentTracker，更换新图标\n"
    "- 卡组预测仅支持天梯 / 训练模式，其他模式自动不显示预测\n"
    "\n"
    "v1.4.2（2026-10-30）\n"
    "- 悬浮窗卡图升级为高清原画（previewImg.big）\n"
    "- 悬浮窗统计区整体下移，视觉更舒展\n"
    "\n"
    "v1.4.1（2026-10-30）\n"
    "- 修复点数初始值+1 偏差（生成牌误计入）\n"
    "- 修复卡图缺失：失败后 15 秒自动重试 + 预测更新强制刷新 URL\n"
    "- 悬浮窗顶部排版优化（阵营领袖行 + 匹配率进度条）\n"
    "- 新增悬浮窗透明度设置（30%-100%，实时预览）\n"
    "\n"
    "v1.4.0（2026-10-30）\n"
    "- 悬浮窗卡图改版：每张卡整行平铺原画背景（炉石记牌器样式），\n"
    "  全部卡牌均显示，费用宝石 + 卡名叠加在卡图上\n"
    "- 热门卡组库扩充至 120 套\n"
    "\n"
    "v1.3.0（2026-10-30）\n"
    "- 悬浮窗新增双方人口对比与实时点数差值\n"
    "- 悬浮窗卡组预测新增卡牌原画缩略图（随用随下、本地缓存）\n"
    "- 悬浮窗文本对齐修复，界面整体美化\n"
    "- 新增「关于」窗口\n"
    "\n"
    "v1.2.0（2026-10-30）\n"
    "- 卡组库下载提速，缓存按版本自动更新\n"
    "- 悬浮窗炉石风格改版，支持自由拖动\n"
    "\n"
    "v1.1.0\n"
    "- 新增对手卡组预测与预测悬浮窗"
)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if cfg.get("theme") == "light":
        cfg["theme"] = "hud"   # v1.8.0：旧默认浅色主题迁移为猎魔 HUD
    if not cfg.get("v184_seen"):
        # v1.8.4 一次性迁移：主窗口自动停靠与悬浮窗跟随游戏窗口
        # 改为默认关闭（用户仍可在设置中重新打开）
        cfg["v184_seen"] = True
        cfg["dock_to_game"] = False
        cfg["pred_follow"] = False
    if not cfg.get("hud_seen"):
        # v1.8.0 一次性迁移：老用户升级后首次启动也展示新 HUD 主题，
        # 之后用户手动切换的主题会被记住，不再覆盖
        cfg["hud_seen"] = True
        cfg["theme"] = "hud"
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------- 进度条流动光效辅助 ----------
def _hex_rgb(c):
    """'#rrggbb' -> (r, g, b)"""
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c0, c1, t):
    """两个 RGB 元组按 t 线性混合，返回 '#rrggbb'。"""
    return "#%02x%02x%02x" % tuple(
        int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))


def _flow_band(bw, phase):
    """流光带：返回 [(x, 强度0-1)]，一道平滑渐变高光扫过 0..bw-1 后循环。

    强度按抛物线分布（两端 0、中心 1），逐列插值绘制即为柔和扫光，
    避免 stipple 抖动点阵的廉价感。"""
    band = min(30, max(12, bw // 3))
    start = int((bw + band) * phase) - band
    cols = []
    for i in range(band):
        x = start + i
        if 0 <= x < bw:
            t = (i + 0.5) / band
            cols.append((x, 4.0 * t * (1.0 - t)))
    return cols


def _shade(c, t):
    """RGB 元组向黑压暗 t（0-1），返回 RGB 元组。"""
    return tuple(int(x * (1.0 - t)) for x in c)


def _tint(c, t):
    """RGB 元组向白提亮 t（0-1），返回 RGB 元组。"""
    return tuple(int(x + (255 - x) * t) for x in c)


def _mixt(c0, c1, t):
    """同 _mix，但入参/返回均为 RGB 元组（逐像素绘制用，避免字符串开销）。"""
    return tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))


def _rgb(c):
    """RGB 元组 -> '#rrggbb'。"""
    return "#%02x%02x%02x" % c


def _lift(c, t):
    """向白提亮 t（0-1），返回 hex：暗色主题的表面抬升。"""
    return _mix(_hex_rgb(c), (255, 255, 255), t)


def _drop(c, t):
    """向黑压深 t（0-1），返回 hex：阴影 / 浅色主题的边界。"""
    return _mix(_hex_rgb(c), (0, 0, 0), t)


def _outline_text(cv, x, y, text, anchor, font, fill, outline):
    """带描边文本：8 方向偏移先画描边色再画主体。

    用于深色卡图上的黑字（铜卡）：纯黑在深底上不可读，
    1px 浅色描边保证可辨识；浅色主题下描边几乎不可见。"""
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                   (-1, -1), (1, -1), (-1, 1), (1, 1)):
        cv.create_text(x + dx, y + dy, text=text, anchor=anchor,
                       font=font, fill=outline)
    cv.create_text(x, y, text=text, anchor=anchor, font=font, fill=fill)


def _aa_photo(size, shader):
    """逐像素抗锯齿绘制到透明 tk.PhotoImage，消除画布圆形/圆环毛边。

    Tk 画布的椭圆/圆弧原生无抗锯齿，小尺寸下毛边明显；改为按像素
    覆盖度直接混合颜色逐像素写入，边缘即为平滑过渡。
    shader(x, y) -> '#rrggbb' 或 None（该像素透明）。
    相邻同色像素合并为一次区域写入，减少 put 调用次数。"""
    im = tk.PhotoImage(width=size, height=size)
    for y in range(size):
        x = 0
        while x < size:
            c = shader(x, y)
            x0 = x
            while x < size and shader(x, y) == c:
                x += 1
            if c:
                im.put(c, to=(x0, y, x, y + 1))
    return im


class MatchRecord:
    def __init__(self):
        self.entries = []  # dict: card, count, first_time, round, side
        self.start_ts = time.time()      # 对局开始（首条数据/重放时间）
        self.end_ts = None               # 对局结束时间
        self.leaders = {"mine": 0, "opp": 0}     # 双方领袖模板ID
        self.leader_prov = {"mine": 0, "opp": 0}  # 双方领袖人口
        self.round_scores = {}           # 小局最终点数: round_no -> (我方, 对方)
        self.my_deck = {}                # 我方卡组（内存直读，模板ID->张数）
        self.meta = {}                   # 对局元数据（模式/昵称/天梯分，内存读取）
        self.final_winner = None         # 权威胜负（GameEndedHistoryEntry.WinnerID 映射）
        self.end_reason = None           # EEndGameReason 值（0=正常 1=投降 2=断线…）

    def add(self, card, meta, round_no, side=0):
        for e in self.entries:
            if (e["card"]["id"] == card["id"] and e["round"] == round_no
                    and e["side"] == side):
                e["count"] += 1
                return e
        e = {
            "card": card,
            "count": 1,
            "first_time": time.strftime("%H:%M:%S"),
            "round": round_no,
            "side": side,       # 0 = 对方, 1 = 我方
        }
        self.entries.append(e)
        return e

    def snapshot_round(self, round_no, pts):
        """记录小局结束时的最终点数（小局切换/对局结束时调用）。"""
        if not pts or pts[0] is None or pts[1] is None:
            return
        m, o = int(pts[0]), int(pts[1])
        if m <= 0 and o <= 0:
            return                       # 空局（没读到有效点数）
        old = self.round_scores.get(round_no)
        # 只保留更大值：切换瞬间可能已在下一小局扫到 0 之前补发
        if old is None or (m, o) > tuple(old):
            self.round_scores[round_no] = (m, o)

    def update_round_pts(self, round_no, pts):
        """points 事件实时驱动：把最新非零点数写入当前小局。

        此前只在「小局切换瞬间」快照上一局——但场上清零的 (0,0) 点数
        先于回合事件到达，快照时 _pts 已被覆盖，导致上一局点数丢失
        （比分永远 1-0 的根因）。实时写入后任何事件顺序都免疫：
        - 打牌期间 points 持续更新当前局（直接覆盖，取最后一次非零值，
          力量被移除导致点数下降时也跟随）；
        - 切换瞬间 points(0,0) → round_no 未切换时写入旧局、全零被过滤，
          round_no 切换后写入新局、同样被过滤，都不会污染旧局数据。"""
        if not pts or pts[0] is None or pts[1] is None:
            return
        m, o = int(pts[0]), int(pts[1])
        if m <= 0 and o <= 0:
            return                       # 清零/无效：不写入
        self.round_scores[round_no] = (m, o)

    def winner(self):
        """整局结果：优先权威胜负（内存读 GameEnded 的 WinnerID），
        投降/断线等比分不完整场景也判得准；退回小局比分判定。"""
        if self.final_winner:
            return self.final_winner
        mw = ow = 0
        for r in sorted(self.round_scores):
            m, o = self.round_scores[r]
            if m > o:
                mw += 1
            elif o > m:
                ow += 1
        if mw == 0 and ow == 0:
            return None
        if mw > ow:
            return "mine"
        if ow > mw:
            return "opp"
        return "draw"

    def duration(self):
        if self.end_ts is None or self.end_ts <= self.start_ts:
            return None
        return int(self.end_ts - self.start_ts)

    def total_provision(self, round_no=None, side=None):
        return sum(
            e["card"]["provision"] * e["count"]
            for e in self.entries
            if (round_no is None or e["round"] == round_no)
            and (side is None or e["side"] == side)
        )

    def total_cards(self, round_no=None, side=None):
        return sum(
            e["count"] for e in self.entries
            if (round_no is None or e["round"] == round_no)
            and (side is None or e["side"] == side)
        )

    def to_json(self):
        data = {
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
                self.end_ts or time.time())),
            "start_ts": self.start_ts,
            "duration": self.duration(),
            "winner": self.winner(),
            "end_reason": self.end_reason,
            "provision_opponent": self.total_provision(side=0),
            "provision_mine": self.total_provision(side=1),
            "total_cards": self.total_cards(),
            "rounds": [
                {"round": r, "mine": m, "opp": o}
                for r, (m, o) in sorted(self.round_scores.items())
            ],
            "cards": [
                {
                    "round": e["round"],
                    "side": "mine" if e["side"] else "opponent",
                    "id": e["card"]["id"],
                    "name": e["card"]["name_zh"],
                    "provision": e["card"]["provision"],
                    "power": e["card"]["power"],
                    "gold": bool(e["card"].get("gold")),
                    "count": e["count"],
                }
                for e in self.entries
            ],
        }
        data["leaders"] = {
            "mine": self.leaders.get("mine") or 0,
            "opp": self.leaders.get("opp") or 0,
        }
        data["leader_prov"] = {
            "mine": self.leader_prov.get("mine") or 0,
            "opp": self.leader_prov.get("opp") or 0,
        }
        if self.meta:
            data["match"] = dict(self.meta)   # 模式/昵称/天梯分等元数据
        if self.my_deck:
            data["my_deck"] = dict(self.my_deck)
        return data


def _round_rect(cv, x0, y0, x1, y1, r, **kw):
    """圆角矩形：单一闭合多边形，轮廓点手工采样（直边 + 圆弧密集折线）。

    历史教训：
    - smooth 样条多边形 → 直边波浪起伏（v1.9.12 前）；
    - 直线 + 90° 弧拼合 → 接缝断点、小弧折线棱角（v1.9.12）。
    现方案：一个 polygon 同时完成描边与填充（stipple 点阵支持），
    直边为纯直线、弧每 ~1.5px 采样一点，无接缝、无断点、无波动。"""
    import math
    fill = kw.get("fill", "")
    outline = kw.get("outline", "")
    width = kw.get("width", 1)
    stipple = kw.get("stipple", "")
    r = max(1, min(int(r), (x1 - x0) // 2, (y1 - y0) // 2))
    pts = []

    def arc(cx, cy, a0, a1):
        # 圆弧采样：Tk y 轴向下，数学角 θ 的点 = (cx+r·cosθ, cy−r·sinθ)
        n = max(4, r)   # 90° 弧取 r 段，每段约 1.6px，视觉平滑
        for i in range(n + 1):
            a = math.radians(a0 + (a1 - a0) * i / n)
            pts.append(cx + r * math.cos(a))
            pts.append(cy - r * math.sin(a))

    pts += [x0 + r, y0, x1 - r, y0]      # 顶边
    arc(x1 - r, y0 + r, 90, 0)           # 右上弧
    pts += [x1, y1 - r]                  # 右边
    arc(x1 - r, y1 - r, 0, -90)          # 右下弧
    pts += [x0 + r, y1]                  # 底边
    arc(x0 + r, y1 - r, -90, -180)       # 左下弧
    pts += [x0, y0 + r]                  # 左边
    arc(x0 + r, y0 + r, -180, -270)      # 左上弧（终点=首点，自动闭合）
    opts = {"fill": fill, "outline": outline, "width": width}
    if stipple:
        opts["stipple"] = stipple
    return cv.create_polygon(pts, **opts)


class GlassButton(tk.Canvas):
    """透明毛玻璃按钮：圆角、半透明质感、悬停描边高亮、支持禁用。"""

    def __init__(self, parent, text, command=None, accent=False, danger=False,
                 height=32, font_size=9, min_width=0, padx=15):
        self._pbg = parent.cget("bg")
        super().__init__(parent, bg=self._pbg, highlightthickness=0, bd=0,
                         height=height, width=80, cursor="hand2", takefocus=0)
        self.text = text
        self.command = command
        self.accent = accent
        self.danger = danger
        self.enabled = True
        self.hover = False
        self.pressed = False
        self._font = tkfont.Font(family="Microsoft YaHei UI", size=font_size,
                                 weight="bold" if accent else "normal")
        self._h = height
        self._minw = min_width
        self._padx = padx
        self._resize()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda e: self._paint())
        self._paint()

    # 兼容旧代码的 .config(text=...)
    def config(self, **kw):
        if "text" in kw:
            self.text = kw.pop("text")
            self._resize()
        super().config(**kw)
        self._paint()

    configure = config

    def _resize(self):
        w = max(self._minw, self._font.measure(self.text) + self._padx * 2)
        self.configure(width=w)

    def set_enabled(self, enabled, text=None):
        if text is not None:
            self.text = text
            self._resize()
        self.enabled = bool(enabled)
        self.hover = False
        self.pressed = False
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._paint()

    def set_accent(self, on):
        self.accent = bool(on)
        self._paint()

    def _colors(self):
        if not self.enabled:
            return (BTN_BG, BORDER, FG_DIM, "gray50")
        if self.danger:
            base, red = _hex_rgb(BG2), _hex_rgb(ERR)
            if self.pressed:
                return (_mix(base, red, 0.34), ERR, "#FFE9E6", "")
            if self.hover:
                return (_mix(base, red, 0.22), ERR, "#FFE9E6", "")
            return (BTN_BG, _mix(base, red, 0.50),
                    _mix(_hex_rgb(FG), red, 0.45), "gray25")
        if self.accent:
            return (ACCENT2 if (self.hover or self.pressed) else ACCENT,
                    ACCENT2, ACCENT_FG, "")
        if self.pressed:
            return (BTN_ACT, ACCENT, FG, "gray12")
        if self.hover:
            return (BTN_ACT, ACCENT2, FG, "gray12")
        return (BTN_BG, BORDER, FG, "gray25")

    def _paint(self):
        self.delete("all")
        w = max(self.winfo_width(), int(self.cget("width")))
        h = max(self.winfo_height(), self._h)
        fill, outline, fg, stipple = self._colors()
        opts = {"fill": fill, "outline": outline, "width": 1}
        if stipple:
            opts["stipple"] = stipple
        # 长方形小圆角：12 点写法下样条过渡约为 r 的一半，r=4 视觉仅 2px，
        # 明确的方形观感；毛玻璃半透明质感（stipple 点阵）保持不变
        _round_rect(self, 1, 1, w - 1, h - 1, int(4 * UI_SCALE), **opts)
        self.create_text(w // 2, h // 2, text=self.text, fill=fg,
                         font=self._font)

    def _on_enter(self, _e):
        if self.enabled:
            self.hover = True
            self._paint()

    def _on_leave(self, _e):
        self.hover = False
        self.pressed = False
        self._paint()

    def _on_press(self, _e):
        if not self.enabled:
            return
        self.pressed = True
        self._paint()

    def _on_release(self, e):
        if not self.enabled:
            return
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self.pressed = False
        self._paint()
        if inside and self.command:
            self.command()


class _Dot(tk.Canvas):
    """状态圆点：抗锯齿实心圆，支持改色与呼吸（alpha 0.45↔1.0）。"""

    def __init__(self, parent, color=FG_DIM, bg=BG, size=10, breathe=False):
        super().__init__(parent, width=size, height=size, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0)
        self._bg, self._size = bg, size
        self._color = color
        self.breathe = breathe
        self._alpha = 1.0
        self._cache = {}
        self._draw()

    def set_color(self, color):
        if color != self._color:
            self._color = color
            self._draw()

    def set_alpha(self, a):
        a = round(max(0.0, min(1.0, a)) * 20) / 20.0
        if abs(a - self._alpha) > 0.001:
            self._alpha = a
            self._draw()

    def _draw(self):
        key = (self._color, self._alpha)
        img = self._cache.get(key)
        if img is None:
            n = self._size
            base = _hex_rgb(self._bg)
            core = _mixt(base, _hex_rgb(self._color), self._alpha)
            r = n / 2.0 - 0.5
            cx = n / 2.0 - 0.5

            def shader(x, y):
                d = math.hypot(x + 0.5 - cx, y + 0.5 - cx)
                cov = max(0.0, min(1.0, r + 0.5 - d))
                if cov <= 0:
                    return None
                return _rgb(_mixt(base, core, cov))

            img = _aa_photo(n, shader)
            self._cache[key] = img
        try:
            self.delete("all")
            self.create_image(0, 0, anchor="nw", image=img)
        except tk.TclError:
            pass
        self._img = img


class _RoundBtn(tk.Canvas):
    """圆形图标按钮（36px）。

    primary=True：一级按钮——强调色填充 + 白色图标 + 4px 外发光；
    否则：二级玻璃按钮——透明底 + 1px 描边，hover 提亮约 20%。"""

    def __init__(self, parent, icon, command=None, primary=False, size=36,
                 tip=None):
        size = max(20, int(size * UI_SCALE))   # DPI 缩放（150% 下圆按钮等比放大）
        self._pbg = parent.cget("bg")
        super().__init__(parent, bg=self._pbg, highlightthickness=0, bd=0,
                         width=size, height=size, cursor="hand2", takefocus=0)
        self.icon, self.command = icon, command
        self.primary, self.size = primary, size
        self.hover = False
        self.pressed = False
        self._tip, self._tip_win, self._tip_job = tip, None, None
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda e: self._paint())
        self._paint()

    def _paint(self):
        self.delete("all")
        s = self.size
        c = s / 2.0
        r = s / 2.0 - 1
        base = _hex_rgb(self._pbg)
        if self.primary:
            # 一级操作：静止时为“金色细环 + 金色图标”，悬停/按下才填充实心，
            # 这样它仍是全场唯一金色控件，但不再是一个抢眼的实心金球
            on = self.hover or self.pressed
            fill = _hex_rgb(ACCENT) if on else None
            edge = _hex_rgb(ACCENT)
            fg = ACCENT_FG if on else ACCENT

            def shader(x, y):
                d = math.hypot(x + 0.5 - c, y + 0.5 - c)
                ec = max(0.0, min(1.0, 1.0 - abs(d - r)))
                fc = max(0.0, min(1.0, r + 0.5 - d)) \
                    if fill is not None else 0.0
                if ec <= 0 and fc <= 0:
                    return None
                col = _mixt(base, fill, fc) if fill is not None else base
                if ec > 0:
                    col = _mixt(col, edge, ec)
                return _rgb(col)
        else:
            # 幽灵按钮：静止完全无底无边，悬停才浮现浅色圆盘 + 图标提亮
            fill = _mixt(base, (255, 255, 255), 0.08) if self.hover else None
            if self.pressed:
                fill = _mixt(base, (255, 255, 255), 0.04)
            fg = FG if self.hover else FG_DIM

            def shader(x, y):
                d = math.hypot(x + 0.5 - c, y + 0.5 - c)
                fc = max(0.0, min(1.0, r + 0.5 - d)) \
                    if fill is not None else 0.0
                if fc <= 0:
                    return None
                return _rgb(_mixt(base, fill, fc))
        img = _aa_photo(s, shader)     # 抗锯齿圆形（create_oval 有毛边）
        self.create_image(0, 0, anchor="nw", image=img)
        self._img = img                # 持有引用防止被 GC
        self.create_text(c, c, text=self.icon, fill=fg,
                         font=("Segoe UI Symbol", 12))

    def _enter(self, _e):
        self.hover = True
        self._paint()
        # 悬浮说明：停留 0.55s 后在按钮下方弹出
        if self._tip and self._tip_job is None:
            self._tip_job = self.after(550, self._tip_show)

    def _tip_show(self):
        self._tip_job = None
        if not self._tip or self._tip_win is not None:
            return
        try:
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            tk.Label(win, text=self._tip, bg="#1D1E23", fg="#E8E6E1",
                     font=("Microsoft YaHei UI", 8), padx=8, pady=3,
                     relief="solid", bd=1).pack()
            x = self.winfo_rootx() + self.winfo_width() // 2
            y = self.winfo_rooty() + self.winfo_height() + 6
            win.update_idletasks()
            win.geometry("+%d+%d" % (
                max(4, x - win.winfo_width() // 2), y))
            self._tip_win = win
        except tk.TclError:
            self._tip_win = None

    def _tip_hide(self):
        if self._tip_job is not None:
            try:
                self.after_cancel(self._tip_job)
            except tk.TclError:
                pass
            self._tip_job = None
        if self._tip_win is not None:
            try:
                self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None

    def _leave(self, _e):
        self.hover = False
        self.pressed = False
        self._tip_hide()
        self._paint()

    def _press(self, _e):
        self.pressed = True
        self._tip_hide()
        self._paint()

    def _release(self, e):
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self.pressed = False
        self._paint()
        if inside and self.command:
            self.command()

    def destroy(self):
        self._tip_hide()
        super().destroy()


class _ArtPool:
    """卡牌原画共享池：磁盘 cards_art/ 缓存 + playgwent 下载 + PIL 预合成。

    悬浮窗与对局详情的卡组明细共用同一份 PhotoImage 缓存，
    避免重复下载、重复代码；下载就绪后通知各订阅视图重绘。"""

    ART_DIR = os.path.join(BASE_DIR, "cards_art")
    W = 268            # 行宽（与悬浮窗卡组行一致）
    ROW_H = 24
    THUMB_W = 60       # HUD 悬浮窗横向缩略图宽
    THUMB_H = 84       # 缩略图高（卡牌比例 ≈ 5:7）
    # 游戏卡组列表风格横幅（悬浮窗行右侧）：按每张卡的构图智能裁切
    BAND_W = 120
    BAND_H = 34
    BAND_FADE = 26           # 左缘向行底色渐隐宽度（预烘焙）
    BAND_BG = (23, 19, 16)   # 渐隐目标色 = 悬浮窗行底色

    def __init__(self, app):
        self.app = app
        self._art = {}              # tid -> PhotoImage（正常，已预压暗）
        self._art_dim = {}          # tid -> PhotoImage（已出的牌：灰化压暗版）
        self._thumb = {}            # tid -> PhotoImage（60px 缩略图，正常）
        self._thumb_done = {}       # tid -> PhotoImage（缩略图：灰化 + 40% 透明）
        self._band = {}             # tid -> PhotoImage（游戏风格横幅：构图裁切）
        self._band_dim = {}         # tid -> PhotoImage（已出版：灰化压暗横幅）
        self._art_url = {}          # tid -> playgwent 相对路径
        self._art_pending = set()   # 正在下载
        self._art_miss = set()      # 无图/下载失败（15 秒后自动解锁重试）
        self._subs = []             # 图片就绪后的重绘回调（各视图自行注册）
        try:
            os.makedirs(self.ART_DIR, exist_ok=True)
        except OSError:
            pass
        # DPI 缩放：卡图生成尺寸随主屏 DPI 放大，与行高/行宽精确对齐
        s = UI_SCALE
        self.row_w = int(_ArtPool.W * s)
        self.row_h = int(_ArtPool.ROW_H * s)
        self.thumb_w = max(1, int(_ArtPool.THUMB_W * s))
        self.thumb_h = max(1, int(_ArtPool.THUMB_H * s))
        self.band_w = max(1, int(_ArtPool.BAND_W * s))
        self.band_h = max(1, int(_ArtPool.BAND_H * s))
        self.band_fade = max(1, int(_ArtPool.BAND_FADE * s))
        self._purge_legacy()

    # ---------- 外部接口 ----------
    def subscribe(self, cb):
        """注册重绘回调（图片就绪时调用；回调内部需自行处理视图已销毁）。"""
        self._subs.append(cb)

    def unsubscribe(self, cb):
        """注销重绘回调（临时视图关闭时调用，避免回调列表无限增长）。"""
        try:
            self._subs.remove(cb)
        except ValueError:
            pass

    def get(self, tid, done=False):
        img = self._art_dim.get(tid) if done else self._art.get(tid)
        if img is None and done:
            img = self._art.get(tid)     # 灰化版未就绪时退回正常版
        return img

    def get_thumb(self, tid, done=False):
        """HUD 悬浮窗横向缩略图（60x84）；已出版 = 灰化 + 40% 不透明度。"""
        img = self._thumb_done.get(tid) if done else self._thumb.get(tid)
        if img is None and done:
            img = self._thumb.get(tid)
        return img

    def get_band(self, tid, done=False):
        """游戏卡组列表风格横幅（悬浮窗行右侧）；已出版灰化压暗。"""
        img = self._band_dim.get(tid) if done else self._band.get(tid)
        if img is None and done:
            img = self._band.get(tid)
        return img

    def url(self, tid):
        return self._art_url.get(tid)

    def set_url(self, tid, u):
        self._art_url[tid] = u
        self._art_miss.discard(tid)      # 拿到新 URL → 解除下载失败锁

    def request(self, tid):
        if tid in self._art_pending or tid in self._art_miss:
            return
        self._art_pending.add(tid)
        threading.Thread(target=self._worker, args=(tid,),
                         daemon=True).start()

    # ---------- 内部实现 ----------
    def _purge_legacy(self):
        """清理旧代次卡图缓存（.jpg）：早期版本缓存过 188×270 小图，
        会被 cover 放大导致模糊；.hd.jpg 为当前高清代次，不受影响。"""
        try:
            for name in os.listdir(self.ART_DIR):
                if name.endswith(".jpg") and not name.endswith(".hd.jpg"):
                    try:
                        os.remove(os.path.join(self.ART_DIR, name))
                    except OSError:
                        pass
        except OSError:
            pass

    def _worker(self, tid):
        """后台线程：磁盘缓存 → playgwent 下载 → 主线程上屏。

        整体兜底 try/except：任何异常（如打包缺子模块）都不能让线程
        静默死亡——否则 tid 永远锁在 _art_pending，卡图再也加载不出。"""
        # .hd.jpg 代次命名：源图升级/旧小图缓存时改名即可自动失效重建
        path = os.path.join(self.ART_DIR, "%s.hd.jpg" % tid)
        pil = None
        try:
            if os.path.exists(path):
                pil = self._load_pil(path)
            if pil is None:
                url = self._art_url.get(tid)
                if url:
                    import urllib.request
                    req = urllib.request.Request(
                        "https://www.playgwent.com" + url,
                        headers=deck_predict.UA)
                    with urllib.request.urlopen(req, timeout=12) as f:
                        raw = f.read()
                    with open(path, "wb") as fo:
                        fo.write(raw)
                    pil = self._load_pil(path)
        except Exception:  # noqa: BLE001
            pil = None
            try:
                os.remove(path)         # 下载失败/损坏的缓存图删掉可重试
            except OSError:
                pass
        if pil is None:
            self.app.root.after(0, lambda: self._failed(tid))
        else:
            self.app.root.after(0, lambda: self._ready(tid, pil))

    def _load_pil(self, path, done=False):
        """等比放大铺满整行 (row_w x row_h) 再垂直居中裁剪（cover）。

        随后用 PIL 预合成明暗（平滑像素级混合，替代 stipple 抖动遮罩）：
        正常版压暗保证文字可读；已出版再灰化，区分度更明显。"""
        from PIL import Image, ImageEnhance
        img = Image.open(path).convert("RGB")
        tw, th = self.row_w, self.row_h
        w, h = img.size
        scale = max(tw / w, th / h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.LANCZOS)
        w, h = img.size
        x0 = max(0, (w - tw) // 2)
        y0 = max(0, (h - th) // 2)
        img = img.crop((x0, y0, x0 + tw, y0 + th))
        img = ImageEnhance.Brightness(img).enhance(0.62 if done else 0.72)
        if done:
            img = ImageEnhance.Color(img).enhance(0.25)   # 去饱和
            img = ImageEnhance.Contrast(img).enhance(0.92)
        return img

    def _load_thumb(self, path, done=False):
        """缩略图：等比放大铺满 thumb_w×thumb_h 再居中裁剪（cover）。

        已出版：grayscale(100%) + 不透明度 0.4（RGBA alpha=102，
        Tk 叠加在面板底色上即为 40% 透明效果）。"""
        from PIL import Image, ImageOps
        img = Image.open(path).convert("RGB")
        tw, th = self.thumb_w, self.thumb_h
        w, h = img.size
        scale = max(tw / w, th / h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.LANCZOS)
        w, h = img.size
        x0 = max(0, (w - tw) // 2)
        y0 = max(0, (h - th) // 2)
        img = img.crop((x0, y0, x0 + tw, y0 + th))
        if done:
            img = ImageOps.grayscale(img).convert("RGBA")
            img.putalpha(102)      # 0.4 * 255
        return img

    @staticmethod
    def _smart_vwindow(img, ratio_wh):
        """按构图选择竖直裁切窗口（真·按每张卡的构图裁切）。

        源图为竖版全幅原画，横幅只能截取其中一个高度带。对每张卡
        分别计算：灰度水平梯度能量按行汇总 → 行剖面滑动窗口求
        「细节密度」峰值窗口，并施加轻微上移偏好（人物头肩通常偏上、
        底部多为暗角装饰）。返回全尺寸坐标 (y0, y1)。"""
        gw = 160                       # 工作分辨率（足够定位，毫秒级）
        w0, h0 = img.size
        g = img.convert("L").resize((gw, max(1, int(h0 * gw / w0))))
        w, h = g.size
        win = max(1, int(round(w * ratio_wh)))   # 窗口高（工作坐标）
        if win >= h:
            return 0, h0
        px = g.load()
        prof = [0.0] * h               # 行能量：相邻像素差分绝对值均值
        for y in range(h):
            s = 0
            last = px[0, y]
            for x in range(1, w):
                cur = px[x, y]
                d = cur - last
                s += d if d >= 0 else -d
                last = cur
            prof[y] = s / (w - 1.0)
        # 滑动窗口 + 上移偏好：窗口中心越靠上权重越高（最高 +15%）
        best, best_y = -1.0, 0
        acc = sum(prof[:win])
        for y0 in range(h - win + 1):
            if y0:
                acc += prof[y0 + win - 1] - prof[y0 - 1]
            c = (y0 + win / 2.0) / h          # 窗口中心相对位置 0..1
            score = acc / win * (1.15 - 0.3 * c)
            if score > best:
                best, best_y = score, y0
        scale = h0 / float(h)
        return int(best_y * scale), int((best_y + win) * scale)

    def _load_band(self, path, done=False):
        """悬浮窗行右侧横幅：竖直构图窗口裁切 → 缩放 → 压暗 →
        左缘向行底色渐隐（预烘焙，卡图与行文字区自然过渡）。"""
        from PIL import Image, ImageEnhance
        img = Image.open(path).convert("RGB")
        tw, th = self.band_w, self.band_h
        y0, y1 = self._smart_vwindow(img, th / float(tw))
        img = img.crop((0, y0, img.width, y1)).resize((tw, th), Image.LANCZOS)
        if done:
            img = ImageEnhance.Brightness(img).enhance(0.40)
            img = ImageEnhance.Color(img).enhance(0.30)
        else:
            img = ImageEnhance.Brightness(img).enhance(0.72)
        px = img.load()
        bg = self.BAND_BG
        fw = min(self.band_fade, tw)
        for x in range(fw):
            a = (x + 0.5) / fw              # 0（左缘）→ 1
            ia = 1.0 - a
            for y in range(th):
                r, g, b = px[x, y]
                px[x, y] = (int(r * a + bg[0] * ia),
                            int(g * a + bg[1] * ia),
                            int(b * a + bg[2] * ia))
        return img

    def _ready(self, tid, pil):
        self._art_pending.discard(tid)
        self._art_miss.discard(tid)          # 成功 → 解除 miss 锁
        try:
            from PIL import ImageTk
            path = os.path.join(self.ART_DIR, "%s.hd.jpg" % tid)
            self._art[tid] = ImageTk.PhotoImage(pil)
            dim = self._load_pil(path, done=True)
            self._art_dim[tid] = ImageTk.PhotoImage(dim)
            self._thumb[tid] = ImageTk.PhotoImage(self._load_thumb(path))
            self._thumb_done[tid] = ImageTk.PhotoImage(
                self._load_thumb(path, done=True))
            self._band[tid] = ImageTk.PhotoImage(self._load_band(path))
            self._band_dim[tid] = ImageTk.PhotoImage(
                self._load_band(path, done=True))
        except Exception:  # noqa: BLE001
            return
        self._redraw()

    def _failed(self, tid):
        self._art_pending.discard(tid)
        self._art_miss.add(tid)
        # 15 秒后自动解除锁，让下一次绘制重新触发下载
        self.app.root.after(15000, lambda tid=tid: self._art_miss.discard(tid))
        self._redraw()

    def _redraw(self):
        for cb in self._subs:
            try:
                cb()
            except tk.TclError:
                pass


class PredictionOverlay:
    """炉石记牌器风格：悬浮在游戏右侧的对手卡组预测窗。

    无边框置顶小窗，整窗可拖动（双击复位到游戏右缘），
    随游戏窗口移动。内容：人口对比 + 双方实时点数 + 完整预测卡组
    （游戏卡组列表风格：金色人口方块 + 卡名 + 右侧按构图裁切的
    卡图横幅 + 右下角张数角标，对手打完的牌灰化）。
    """

    W = 268
    ROW_H = 34           # 每行高度（游戏卡组列表风格：人口方块 + 右侧构图横幅）
    ART_W = 120          # 行右侧卡图横幅宽（与 _ArtPool.BAND_W 一致）
    STAT_H = 17          # 每条统计行（人口/点数）高
    HL_T = 1.5           # 出牌高亮动画时长（秒）

    def __init__(self, app):
        self.app = app
        self.shown = False
        self._offscreen = False
        self._docked_once = False   # 是否已完成过首次定位（show 时 dock 成功）
        self._drag0 = None
        self._moved = False
        self._game_pos = None
        self.dx = app.cfg.get("pred_dx")
        self.dy = app.cfg.get("pred_dy")
        # DPI 缩放：行尺寸/横幅宽度随主屏 DPI 放大（字体由 Tk 自动放大）
        s = UI_SCALE
        # 用户缩放（0.7-1.5）：设置滑块调整，开启"自由缩放"后可拖右下角
        self._scale = max(0.7, min(1.5, float(
            app.cfg.get("pred_scale", DEFAULT_CONFIG["pred_scale"]))))
        self._fs = s * self._scale      # 统一几何系数：DPI × 用户缩放
        self._resizable = bool(app.cfg.get("pred_resizable", False))
        self._resize0 = None            # 拖角缩放起点 (x_root, y_root, W0)
        self.W = int(PredictionOverlay.W * self._fs)
        self.ROW_H = int(PredictionOverlay.ROW_H * self._fs)
        self.ART_W = int(PredictionOverlay.ART_W * self._fs)
        self.STAT_H = max(12, int(PredictionOverlay.STAT_H * self._fs))
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self._opacity = float(app.cfg.get("pred_opacity", 0.95))
        try:
            self.win.attributes("-alpha", self._opacity)
        except tk.TclError:
            pass
        self.win.configure(bg=BG2)
        self.cv = tk.Canvas(self.win, width=self.W, height=120, bg=BG2,
                            highlightthickness=1, highlightbackground=BORDER,
                            bd=0)
        self.cv.pack(fill="both", expand=True)
        # 字体：point 尺寸随 DPI 由 Tk 放大；用户缩放系数需显式乘入
        def _font(sz, **kw):
            return tkfont.Font(family="Microsoft YaHei UI",
                               size=max(7, int(sz * self._scale)), **kw)
        self._f_title = _font(9, weight="bold")
        self._f_head = _font(8)
        self._f_stat = _font(8, weight="bold")
        self._f_name = _font(9)
        self._f_name_bold = _font(9, weight="bold")
        self._f_out = _font(9, overstrike=1)
        self._f_prov = _font(9, weight="bold")
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Double-Button-1>", self._redock)
        self.cv.bind("<Button-3>", self._rc_menu)
        self.cv.bind("<Motion>", self._hover_cursor)
        # 内容缓存（set_* 更新后统一 _render 重绘）
        self._view = ("state", "等待对手出牌…")
        self._rows_geo = []         # 行几何 (tid, y, remain)：右键命中用
        self._stats = None          # dict(my, myl, opp, oppl, mpts, opts)
        # 出牌高亮动画：tid -> 开始时间；_last_remains 用于对比新旧剩余张数
        self._hl = {}
        self._last_remains = None
        self._anim_job = None       # 高亮/闪烁动画帧循环
        self._last_stats = None     # 统计数值变化闪烁
        self._stats_flash = 0.0
        # 进度条流动光效：相位 + 几何缓存（_draw_flow_bands 用）
        self._flow_phase = 0.0
        self._flow_job = None
        self._flow_geom = []        # (x0, x1, y0, h, frac, fill_color)
        # 卡牌原画：使用全局共享池（与对局详情的卡组明细共用缓存）
        self.pool = app._artpool
        self.pool.subscribe(lambda: self._render() if self.shown else None)
        self.win.withdraw()   # 创建时先隐藏，show() 时再定位显示

    # ---------- 外部接口 ----------
    def set_opacity(self, v):
        """热更新悬浮窗透明度（设置面板滑块预览用）。"""
        v = max(0.3, min(1.0, float(v)))
        self._opacity = v
        try:
            self.win.attributes("-alpha", v)
        except tk.TclError:
            pass

    def set_scale(self, v, save=True):
        """热更新悬浮窗缩放（0.7-1.5）：设置滑块/拖右下角共用。"""
        v = max(0.7, min(1.5, float(v)))
        self._scale = v
        self._fs = UI_SCALE * v
        self.W = int(PredictionOverlay.W * self._fs)
        self.ROW_H = int(PredictionOverlay.ROW_H * self._fs)
        self.ART_W = int(PredictionOverlay.ART_W * self._fs)
        self.STAT_H = max(12, int(PredictionOverlay.STAT_H * self._fs))
        for f, sz in ((self._f_title, 9), (self._f_head, 8),
                      (self._f_stat, 8), (self._f_name, 9),
                      (self._f_name_bold, 9), (self._f_out, 9),
                      (self._f_prov, 9)):
            try:
                f.configure(size=max(7, int(sz * v)))
            except tk.TclError:
                pass
        try:
            self.cv.config(width=self.W)
        except tk.TclError:
            return
        self._render()
        if save:
            self.app.cfg["pred_scale"] = round(v, 2)
            save_config(self.app.cfg)

    def set_resizable(self, on):
        """开关右下角自由缩放手柄。"""
        self._resizable = bool(on)
        if self.shown:
            self._render()

    # ---------- 缩放手柄（右下角拖拽） ----------
    def _grip_zone(self, e):
        """事件坐标是否位于右下角缩放热区。"""
        g = max(14, int(16 * self._fs))
        h = self.cv.winfo_height()
        if h < 50:      # 未完成布局时退回 canvas 配置高度
            h = int(self.cv.cget("height"))
        return e.x >= self.W - g and e.y >= h - g

    def _hover_cursor(self, e):
        if self._resizable and not self._drag0 and not self._resize0 \
                and self._grip_zone(e):
            if str(self.cv.cget("cursor")) != "size_nw_se":
                self.cv.config(cursor="size_nw_se")
        elif str(self.cv.cget("cursor")):
            self.cv.config(cursor="")

    def _draw_grip(self):
        """右下角缩放手柄：三条斜线 + 底部留白（_render 末尾调用）。"""
        cv = self.cv
        h = int(cv.cget("height"))
        cv.config(height=h + int(10 * self._fs))
        h += int(10 * self._fs)
        W, c = self.W, FG_DIM
        for i in range(3):
            d = int((4 + i * 5) * self._fs)
            cv.create_line(W - d, h - int(2 * self._fs),
                           W - int(2 * self._fs), h - d,
                           fill=c, width=max(1, int(2 * UI_SCALE)))

    def _apply_resize(self, e, save=False):
        """按鼠标横移量换算缩放比例（保持宽高比例）。"""
        if not self._resize0:
            return
        x0, y0, w0, s0 = self._resize0
        new_w = max(int(PredictionOverlay.W * UI_SCALE * 0.7),
                    w0 + (e.x_root - x0))
        self.set_scale(s0 * new_w / w0, save=save)

    def set_state(self, text, alert=False):
        """无预测数据时的占位状态；alert=True 用警示色（无法预测提示）。"""
        self._view = ("state", text, WARN if alert else FG_DIM)
        self._last_remains = None   # 状态切换后不触发出牌高亮
        self._render()

    def set_result(self, r, played, db):
        """更新预测结果（完整卡组列表）。"""
        self._view = ("result", r, played, db)
        # 刷新卡图 URL：解除之前因临时下载失败被锁住的 tid，
        # 让它们重新尝试（playgwent 偶发 403 往往重试就通）
        for tid, u in (r.get("img") or {}).items():
            self.pool.set_url(tid, u)
        # 对比新旧剩余张数：减少的牌 = 对手刚打出 → 触发高亮动画
        rows = self._build_rows(r, played, db)
        remains = {tid: remain for _, _, _, remain, _, tid in rows}
        now = time.time()
        if self._last_remains:
            for tid, rem in remains.items():
                old = self._last_remains.get(tid)
                if old is not None and rem < old:
                    self._hl[tid] = now
        self._last_remains = remains
        self._render()

    def set_stats(self, stats):
        """更新人口对比/点数统计；stats=None 隐藏统计区。数值变化时闪烁提醒。"""
        self._stats = stats
        if stats:
            key = (stats.get("my"), stats.get("opp"),
                   stats.get("mpts"), stats.get("opts"))
            if key != self._last_stats:
                self._last_stats = key
                self._stats_flash = time.time()
        self._render()

    # ---------- 出牌高亮 / 闪烁动画 ----------
    def _hl_active(self):
        """仍有进行中的高亮或数值闪烁动画。"""
        now = time.time()
        for tid in list(self._hl):
            if now - self._hl[tid] > self.HL_T:
                del self._hl[tid]
        if self._hl:
            return True
        return bool(self._stats_flash) and now - self._stats_flash < 0.9

    def _schedule_anim(self):
        if self._hl_active() and self._anim_job is None and self.shown:
            self._anim_job = self.app.root.after(70, self._anim_tick)

    def _anim_tick(self):
        self._anim_job = None
        if not self.shown:
            return
        try:
            self._render()
        except tk.TclError:
            pass

    # ---------- 进度条流动光效 ----------
    def _draw_flow_bands(self):
        """按 _flow_geom 缓存的几何，在各进度条填充区内绘制平滑扫光。"""
        cv = self.cv
        cv.delete("ovflow")
        for x0, x1, y0, h, frac, fill in self._flow_geom:
            bw = int((x1 - x0) * frac)
            if bw < 20:
                continue
            rgb = _hex_rgb(fill)
            for dx, a in _flow_band(bw, self._flow_phase):
                cv.create_line(x0 + dx, y0, x0 + dx, y0 + h,
                               fill=_mix(rgb, (255, 255, 255), a * 0.85),
                               tags="ovflow")

    def _flow_tick(self):
        """流动光效帧循环（悬浮窗显示期间持续运行）。"""
        self._flow_job = None
        if not self.shown:
            return
        self._flow_phase = (self._flow_phase + 0.035) % 1.0
        try:
            self._draw_flow_bands()
        except tk.TclError:
            return
        self._flow_job = self.app.root.after(66, self._flow_tick)

    # ---------- 渲染 ----------
    def _render(self):
        cv = self.cv
        cv.delete("all")
        self._rows_geo = []         # 每帧重建行几何（右键命中用）
        self._flow_geom = []        # 每帧重建进度条几何（供流动光效绘制）
        W = self.W
        fs = self._fs               # 几何系数 = DPI × 用户缩放
        view = self._view
        y = int(8 * fs)
        if view[0] == "result":
            _, r, played, db = view
            cv.create_text(int(10 * fs), y, text="对手卡组预测", anchor="nw",
                           font=self._f_title, fill=ACCENT)
            cv.create_text(W - int(10 * fs), y, text="%d%%" % int(r["conf"] * 100),
                           anchor="ne", font=self._f_title, fill=ACCENT2)
            y += int(18 * fs)
            rows = self._build_rows(r, played, db)
            fzh = deck_predict.FACTION_ZH.get(r["faction"], r["faction"] or "?")
            leader = self.app._pred_deck_name(r)
            # 第 2 行：左侧 "[阵营] 领袖"，右侧 "剩 x/y 张"
            cv.create_text(int(10 * fs), y, text="[%s] %s" % (fzh, leader),
                           anchor="nw", font=self._f_head, fill=FG_DIM)
            cv.create_text(W - int(10 * fs), y, text="剩 %d/%d 张" % (
                sum(x[3] for x in rows), sum(x[2] for x in rows)),
                anchor="ne", font=self._f_head, fill=FG_DIM)
            y += int(15 * fs)
            # 第 3 行：预测匹配率进度条（简洁 1px 条）
            bar_h = max(2, int(3 * fs))
            frac = max(0.0, min(1.0, r["conf"]))
            cv.create_rectangle(int(10 * fs), y, W - int(10 * fs), y + bar_h,
                                fill=BORDER, width=0)
            cv.create_rectangle(int(10 * fs), y,
                                int(10 * fs + (W - 20 * fs) * frac), y + bar_h,
                                fill=ACCENT, width=0)
            self._flow_geom.append((int(10 * fs), W - int(10 * fs), y, bar_h,
                                    frac, ACCENT))
            y += bar_h + int(6 * fs)
        else:
            cv.create_text(int(10 * fs), y, text="对手卡组预测", anchor="nw",
                           font=self._f_title, fill=ACCENT)
            y += int(18 * fs)
        # 统计区：双方人口进度条 + 点数（与上方文本留出间隔，避免重叠）
        s = self._stats
        if s:
            y += int(4 * fs)
            y = self._draw_stats(y)
            y += int(4 * fs)
        cv.create_line(0, y, W, y, fill=BORDER)
        y += int(8 * fs)
        if view[0] == "result":
            self._draw_rows(y, rows)
        else:
            cv.config(height=y + int(44 * fs))
            cv.create_text(int(10 * fs), y + int(6 * fs), text=view[1],
                           anchor="nw", font=self._f_name,
                           fill=view[2] if len(view) > 2 else FG_DIM)
        # 开启自由缩放：右下角手柄（同时给底部留出热区）
        if self._resizable:
            self._draw_grip()
        # 进度条流动光效 + 出牌高亮/数值闪烁动画进行中 → 继续跑帧
        self._draw_flow_bands()
        self._schedule_anim()

    def _draw_stats(self, y0):
        """双方人口进度条 + 点数对比（三行），返回结束 y。"""
        cv = self.cv
        W = self.W
        fs = self._fs
        s = self._stats
        flash = bool(self._stats_flash) and \
            (time.time() - self._stats_flash) < 0.9
        bar_x0, bar_x1 = int(30 * fs), W - int(76 * fs)
        # 行 1/2：我方 / 敌方人口进度条
        for i, (label, used, limit, color) in enumerate((
                ("我", s["my"], s["myl"], MINE),
                ("敌", s["opp"], s["oppl"], ACCENT))):
            cy = y0 + i * self.STAT_H + self.STAT_H // 2
            cv.create_text(int(10 * fs), cy, text=label, anchor="w",
                           font=self._f_head, fill=color)
            frac = max(0.0, min(1.0, used / max(1, limit)))
            cv.create_rectangle(bar_x0, cy - max(3, int(4 * fs)),
                                bar_x1, cy + max(3, int(4 * fs)),
                                fill=BG3, width=0)
            if frac > 0:
                cv.create_rectangle(bar_x0, cy - max(3, int(4 * fs)),
                                    int(bar_x0 + (bar_x1 - bar_x0) * frac),
                                    cy + max(3, int(4 * fs)), fill=color, width=0)
            self._flow_geom.append((bar_x0, bar_x1, cy - max(3, int(4 * fs)),
                                    max(6, int(8 * fs)), frac, color))
            cv.create_text(W - int(8 * fs), cy, text="%d/%d" % (used, limit),
                           anchor="e", font=self._f_stat,
                           fill="#ffd98a" if flash else color)
        # 行 3：实时点数（当前小局场上力量）——显示异常，暂时下线（POINTS_ONLINE）
        if not POINTS_ONLINE:
            return y0 + 2 * self.STAT_H
        cy = y0 + 2 * self.STAT_H + self.STAT_H // 2
        cv.create_text(int(10 * fs), cy, text="点数", anchor="w",
                       font=self._f_head, fill=FG_DIM)
        mpts, opts = s["mpts"], s["opts"]
        if mpts is None:
            cv.create_text(int(48 * fs), cy, text="我 —", anchor="w",
                           font=self._f_stat, fill=FG_DIM)
            cv.create_text(int(110 * fs), cy, text="敌 —", anchor="w",
                           font=self._f_stat, fill=FG_DIM)
            diff_txt, diff_fg = "点数读取中", FG_DIM
        else:
            mpts, opts = mpts or 0, opts or 0
            cv.create_text(int(48 * fs), cy, text="我 %d" % mpts, anchor="w",
                           font=self._f_stat,
                           fill="#ffd98a" if flash else MINE)
            cv.create_text(int(110 * fs), cy, text="敌 %d" % opts, anchor="w",
                           font=self._f_stat,
                           fill="#ffd98a" if flash else ACCENT)
            pd = mpts - opts
            if pd > 0:
                diff_txt, diff_fg = "我方领先 %d" % pd, MINE
            elif pd < 0:
                diff_txt, diff_fg = "对方领先 %d" % -pd, ACCENT
            else:
                diff_txt, diff_fg = "双方持平", FG_DIM
        cv.create_text(W - int(8 * fs), cy, text=diff_txt, anchor="e",
                       font=self._f_head, fill=diff_fg)
        return y0 + 3 * self.STAT_H

    @staticmethod
    def _build_rows(r, played, db):
        """(人口, 卡名, 库内张数, 剩余, 金卡, tid) 列表：打完沉底。"""
        golds = r.get("gold") or set()
        rows = []
        for tid, n in (r.get("cards") or {}).items():
            remain = max(0, n - played.get(tid, 0))
            c = db.by_id.get(tid) if db else None
            name = c["name_zh"] if c else ("卡#" + tid)
            prov = r.get("provs", {}).get(tid) or (c["provision"] if c else 0)
            rows.append((prov, name, n, remain, tid in golds, tid))
        rows.sort(key=lambda x: (x[3] == 0, -x[0], -x[2]))
        return rows

    def _draw_rows(self, y0, rows):
        """游戏卡组列表风格行：金色人口方块 + 卡名 + 右侧构图横幅卡图 +
        右下角张数角标。行区固定深色配色（与游戏 UI 一致，不随主题）。"""
        cv = self.cv
        W = self.W
        h = self.ROW_H
        s = self._fs               # 几何系数 = DPI × 用户缩放
        # 行内固定像素随 DPI 缩放（人口方块/卡名起点/角标）
        gx0, gx1, gr = int(4 * s), int(24 * s), int(9 * s)
        gxmid = (gx0 + gx1) / 2
        nx = int(29 * s)
        pad = int(10 * s)
        cw_pad, cw_cjk, cw_lat = int(8 * s), int(9 * s), int(6 * s)
        ch_top, ch_bot, ch_my = int(15 * s), int(3 * s), int(9 * s)
        y = y0
        app = self.app
        done_fg = "#8f8873"
        for prov, name, n, remain, isgold, tid in rows:
            done = remain == 0
            self._rows_geo.append((tid, y, remain))
            has_art = self._draw_art_row(tid, y, done)
            # 刚被打出的牌：金色描边渐隐动画（1.5s），避免漏看
            t0 = self._hl.get(tid)
            if t0 is not None:
                age = time.time() - t0
                if age < self.HL_T:
                    p = 1.0 - age / self.HL_T          # 1 → 0
                    w_out = 3 if p > 0.5 else 2
                    cv.create_rectangle(1, y + 1, W - 2, y + h - 2,
                                        outline="#ffce6b", width=w_out)
                    cv.create_rectangle(0, y, W, y + h,
                                        outline="#fff3d6", width=1)
            cy = y + h / 2
            # 人口方块（游戏风格金方块 + 深色数字；已出灰化）
            cv.create_rectangle(gx0, cy - gr, gx1, cy + gr,
                                fill="#3b362c" if done else "#d9a93c",
                                outline="#0c0a07", width=1)
            cv.create_text(gxmid, cy, text=str(prov), anchor="c",
                           font=self._f_prov,
                           fill=done_fg if done else "#241a06")
            # 卡名：金卡金色加粗；铜卡浅色黑描边；已出删除线；超长截断
            maxw = W - self.ART_W - nx - pad
            disp = name
            if self._f_name.measure(disp) > maxw:
                while disp and self._f_name.measure(disp + "…") > maxw:
                    disp = disp[:-1]
                disp += "…"
            if done:
                cv.create_text(nx, cy, text=disp, anchor="w",
                               font=self._f_out, fill=done_fg)
            elif isgold:
                cv.create_text(nx, cy, text=disp, anchor="w",
                               font=self._f_name_bold, fill="#e9c874")
            else:
                _outline_text(cv, nx, cy, disp, "w", self._f_name,
                              "#f0ece1", "#000000")
            # 手动标记（右键修正）的牌在角标左侧加 ✎ 提示
            manual = tid in app._ov_extra or tid in app._ov_cancel
            # 右下角张数角标（游戏 x2 角标风格，压在卡图上）
            chip = "已出" if done else (
                "剩%d/%d" % (remain, n) if remain < n else "x%d" % n)
            cw = cw_pad + sum(cw_cjk if ord(ch) > 0x2E80 else cw_lat
                              for ch in chip)
            cx1 = W - gx0
            cx0 = cx1 - cw
            if manual and not done:
                cv.create_text(cx0 - int(4 * s), cy + gr, text="✎", anchor="e",
                               font=self._f_name, fill="#ffce6b")
            cv.create_rectangle(cx0, y + h - ch_top, cx1, y + h - ch_bot,
                                fill="#0f0c08", outline="#3a332a", width=1)
            cv.create_text((cx0 + cx1) / 2, y + h - ch_my, text=chip,
                           anchor="c", font=self._f_head,
                           fill=done_fg if done else "#e8d9a8")
            y += h
        cv.config(height=y + 6)

    # ---------- 右键手动修正 ----------
    def _rc_menu(self, e):
        """右键菜单：标记卡牌已打出/未打出、手动添加卡牌。

        场景：部分卡效果直接进墓地（不上场），内存按上场扫描会漏计；
        对方间谍等误判也可在此手动修正。"""
        row = None
        for tid, ry, remain in self._rows_geo:
            if ry <= e.y < ry + self.ROW_H:
                row = (tid, ry, remain)
                break
        m = tk.Menu(self.win, tearoff=0)
        if row is not None:
            tid, _ry, remain = row
            r = self._view[1] if self._view[0] == "result" else {}
            db = self._view[3] if self._view[0] == "result" else None
            c = db.by_id.get(tid) if db else None
            name = c["name_zh"] if c else ("卡#" + tid)
            if remain == 0:
                m.add_command(label="「%s」标记为未打出" % name,
                              command=lambda: self.app._ov_mark_unplayed(tid))
            else:
                m.add_command(label="「%s」标记为已打出" % name,
                              command=lambda: self.app._ov_mark_played(tid))
            m.add_separator()
        m.add_command(label="手动添加卡牌（模糊搜索）…",
                      command=self.app.on_ov_add_card)
        if self.app._ov_extra or self.app._ov_cancel:
            m.add_command(label="清除全部手动标记",
                          command=self.app._ov_manual_reset)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # ---------- 卡牌原画 ----------
    def _draw_art_row(self, tid, y, done):
        """行底 + 右侧构图横幅卡图；返回该行是否有原画。

        明暗与左缘渐隐已在生成 PhotoImage 时用 PIL 预烘焙
        （共享池 _load_band）；行底固定深色（游戏 UI 风格）。"""
        cv = self.cv
        W = self.W
        h = self.ROW_H
        cv.create_rectangle(0, y, W, y + h, fill="#171310", width=0)
        img = self.pool.get_band(tid, done)
        if img is not None:
            cv.create_image(W, y, anchor="ne", image=img)
            cv.create_rectangle(0, y, W - 1, y + h - 1,
                                outline="#2e2820", width=1)
            return True
        if self.pool.url(tid):
            self.pool.request(tid)
        # 无图占位：右侧留浅一档的空幅
        cv.create_rectangle(W - self.ART_W, y + 1, W - 1, y + h - 1,
                            fill="#221d15", width=0)
        cv.create_rectangle(0, y, W - 1, y + h - 1,
                            outline="#2e2820", width=1)
        return False

    # ---------- 位置与拖动 ----------
    def _game_rect(self):
        hwnd = self.app.game_hwnd or capture.find_gwent_window()[0]
        if not hwnd:
            return None
        try:
            if not capture.is_window_visible(hwnd):
                return None
            return capture.get_client_rect_screen(hwnd)
        except (OSError, IndexError):
            return None

    def _dock(self, force=False):
        rect = self._game_rect()
        if not rect:
            return False
        ox, oy, cw, ch = rect
        if self.dx is None or force:
            self.dx = -(self.W + 8)   # 默认贴游戏右缘内侧
            self.dy = max(60, int(ch * 0.25))
        self.win.geometry("+%d+%d" % (int(ox + cw + self.dx), int(oy + self.dy)))
        self._game_pos = rect
        return True

    def _press(self, e):
        if self._resizable and self._grip_zone(e):
            # 右下角热区：进入拖角缩放模式（不移动窗口）
            self._resize0 = (e.x_root, e.y_root, self.W, self._scale)
            self._moved = False
            return
        self._drag0 = (e.x_root - self.win.winfo_x(),
                       e.y_root - self.win.winfo_y())
        self._moved = False

    def _drag(self, e):
        if self._resize0:
            self._apply_resize(e)
            return
        if not self._drag0:
            return
        self._moved = True
        self.win.geometry("+%d+%d" % (e.x_root - self._drag0[0],
                                      e.y_root - self._drag0[1]))

    def _release(self, _e):
        if self._resize0:
            self._resize0 = None
            self.app.cfg["pred_scale"] = round(self._scale, 2)
            save_config(self.app.cfg)
            return
        if self._moved:
            self._moved = False
            self._save_offset()

    def _redock(self, _e):
        self._dock(force=True)
        self._save_offset()

    def _save_offset(self):
        rect = self._game_pos or self._game_rect()
        if not rect:
            return
        ox, oy, cw, _ch = rect
        self.dx = self.win.winfo_x() - (ox + cw)
        self.dy = self.win.winfo_y() - oy
        self.app.cfg["pred_dx"] = self.dx
        self.app.cfg["pred_dy"] = self.dy
        save_config(self.app.cfg)

    def show(self):
        self.shown = True
        self._offscreen = False
        if not self._dock():
            return           # 游戏窗口不存在时先不显示，follow 循环稍后处理
        self._docked_once = True
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.attributes("-topmost", True)
            self.win.attributes("-alpha", self._opacity)
        except tk.TclError:
            pass
        if self._flow_job is None:      # 启动进度条流动光效循环
            self._flow_job = self.app.root.after(95, self._flow_tick)

    def hide(self):
        self.shown = False
        self._docked_once = False
        if self._flow_job is not None:
            try:
                self.app.root.after_cancel(self._flow_job)
            except tk.TclError:
                pass
            self._flow_job = None
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def follow(self):
        """游戏窗口移动/最小化时跟随（由 App 定时调用）。
        设置中关闭“悬浮窗跟随游戏”后不自动移动（仅首次显示时定位）；
        但若开启悬浮窗时游戏窗口尚不可见（定位失败），仍需在游戏
        窗口出现后完成首次定位并显示——否则悬浮窗会永久卡在隐藏状态。"""
        if not self.shown:
            return
        rect = self._game_rect()
        if not self.app.cfg.get("pred_follow", True):
            if self._docked_once:
                return     # 已固定显示，不自动移动
            if not rect:
                return     # 游戏窗口仍未出现，继续等待
            if self._dock():
                self._docked_once = True
                try:
                    self.win.deiconify()
                    self.win.attributes("-topmost", True)
                    self.win.attributes("-alpha", self._opacity)
                except tk.TclError:
                    pass
            return
        if rect is None:
            if not self._offscreen:
                self._offscreen = True
                try:
                    self.win.withdraw()
                except tk.TclError:
                    pass
            return
        self._dock()
        if self._offscreen:
            self._offscreen = False
            try:
                self.win.deiconify()
                self.win.attributes("-topmost", True)
            except tk.TclError:
                pass

    def close(self):
        if self._anim_job is not None:
            try:
                self.app.root.after_cancel(self._anim_job)
            except Exception:  # noqa: BLE001
                pass
            self._anim_job = None
        if self._flow_job is not None:   # 停止进度条流动光效循环
            try:
                self.app.root.after_cancel(self._flow_job)
            except Exception:  # noqa: BLE001
                pass
            self._flow_job = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class _DeckRows(tk.Frame):
    """对局详情-卡组明细：与悬浮窗相同的游戏风格卡组行（构图横幅），带滚动。"""

    W = 268            # 与悬浮窗行宽一致，共用同一套已裁剪 PhotoImage
    ROW_H = 34
    ART_W = 120

    def __init__(self, app, parent):
        super().__init__(parent, bg=BG2)
        self.app = app
        self.pool = app._artpool
        self.pool.subscribe(self._redraw_safe)
        # DPI 缩放：行尺寸/横幅宽度随主屏 DPI 放大（与悬浮窗一致）
        s = UI_SCALE
        self.W = int(_DeckRows.W * s)
        self.ROW_H = int(_DeckRows.ROW_H * s)
        self.ART_W = int(_DeckRows.ART_W * s)
        self._rows = []
        self._title = ""
        self._f_title = tkfont.Font(family="Microsoft YaHei UI", size=9,
                                    weight="bold")
        self._f_name = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self._f_name_bold = tkfont.Font(family="Microsoft YaHei UI", size=9,
                                        weight="bold")
        self._f_out = tkfont.Font(family="Microsoft YaHei UI", size=9,
                                  overstrike=1)
        self._f_prov = tkfont.Font(family="Microsoft YaHei UI", size=9,
                                   weight="bold")
        self._f_head = tkfont.Font(family="Microsoft YaHei UI", size=8)
        cv = self.cv = tk.Canvas(self, width=self.W, height=240, bg=BG2,
                                 highlightthickness=1,
                                 highlightbackground=BORDER, bd=0,
                                 scrollregion=(0, 0, self.W, 1))
        sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.cv.bind("<MouseWheel>", self._wheel)
        self.cv.bind("<Button-4>", self._wheel)
        self.cv.bind("<Button-5>", self._wheel)

    def set_content(self, title, rows):
        self._title = title
        self._rows = rows or []
        self._render()
        self.cv.yview_moveto(0)

    def _wheel(self, e):
        up = getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4
        try:
            self.cv.yview_scroll(-1 if up else 1, "units")
        except tk.TclError:
            pass

    def _redraw_safe(self):
        try:
            if self.winfo_exists():
                self._render()
        except tk.TclError:
            pass

    def _render(self):
        cv = self.cv
        cv.delete("all")
        W, h = self.W, self.ROW_H
        s = UI_SCALE
        # 行内固定像素随 DPI 缩放（人口方块/卡名起点/角标）
        gx0, gx1, gr = int(4 * s), int(24 * s), int(9 * s)
        gxmid = (gx0 + gx1) / 2
        nx = int(29 * s)
        pad = int(10 * s)
        cw_pad, cw_cjk, cw_lat = int(8 * s), int(9 * s), int(6 * s)
        ch_top, ch_bot, ch_my = int(15 * s), int(3 * s), int(9 * s)
        y = 8
        cv.create_text(10, y, text=self._title, anchor="nw",
                       font=self._f_title, fill=ACCENT)
        y += 22
        done_fg = "#8f8873"
        for prov, name, n, remain, isgold, tid in self._rows:
            done = remain == 0
            has_art = self._art_row(tid, y, done)
            cy = y + h / 2
            # 人口方块（与悬浮窗一致：游戏风格金方块 + 深色数字；已出灰化）
            cv.create_rectangle(gx0, cy - gr, gx1, cy + gr,
                                fill="#3b362c" if done else "#d9a93c",
                                outline="#0c0a07", width=1)
            cv.create_text(gxmid, cy, text=str(prov), anchor="c",
                           font=self._f_prov,
                           fill=done_fg if done else "#241a06")
            # 卡名：金卡金色加粗；铜卡浅色黑描边；已出删除线；超长截断
            maxw = W - self.ART_W - nx - pad
            disp = name
            if self._f_name.measure(disp) > maxw:
                while disp and self._f_name.measure(disp + "…") > maxw:
                    disp = disp[:-1]
                disp += "…"
            if done:
                cv.create_text(nx, cy, text=disp, anchor="w",
                               font=self._f_out, fill=done_fg)
            elif isgold:
                cv.create_text(nx, cy, text=disp, anchor="w",
                               font=self._f_name_bold, fill="#e9c874")
            else:
                _outline_text(cv, nx, cy, disp, "w", self._f_name,
                              "#f0ece1", "#000000")
            # 右下角张数角标（游戏 x2 角标风格，压在卡图上）
            chip = "已出" if done else (
                "剩%d/%d" % (remain, n) if remain < n else "x%d" % n)
            cw = cw_pad + sum(cw_cjk if ord(ch) > 0x2E80 else cw_lat
                              for ch in chip)
            cx1 = W - gx0
            cx0 = cx1 - cw
            cv.create_rectangle(cx0, y + h - ch_top, cx1, y + h - ch_bot,
                                fill="#0f0c08", outline="#3a332a", width=1)
            cv.create_text((cx0 + cx1) / 2, y + h - ch_my, text=chip,
                           anchor="c", font=self._f_head,
                           fill=done_fg if done else "#e8d9a8")
            y += h
        if not self._rows:
            cv.create_text(10, y + 6,
                           text="未能匹配卡组（出牌记录不足或卡组库未覆盖）",
                           anchor="nw", font=self._f_head, fill=FG_DIM)
            y += 26
        cv.configure(scrollregion=(0, 0, W, y + 6))

    def _art_row(self, tid, y, done):
        """行底 + 右侧构图横幅卡图（与悬浮窗 _draw_art_row 相同）；返回该行是否有原画。"""
        cv = self.cv
        W, h = self.W, self.ROW_H
        pool = self.pool
        cv.create_rectangle(0, y, W, y + h, fill="#171310", width=0)
        img = pool.get_band(tid, done)
        if img is not None:
            cv.create_image(W, y, anchor="ne", image=img)
            cv.create_rectangle(0, y, W - 1, y + h - 1,
                                outline="#2e2820", width=1)
            return True
        if pool.url(tid):
            pool.request(tid)
        cv.create_rectangle(W - self.ART_W, y + 1, W - 1, y + h - 1,
                            fill="#221d15", width=0)
        cv.create_rectangle(0, y, W - 1, y + h - 1,
                            outline="#2e2820", width=1)
        return False


# ---------------- 崩溃收集与反馈 ----------------
FEEDBACK_EMAIL = "1599999966@qq.com"
CRASH_LOG = os.path.join(BASE_DIR, "crash_log.txt")


def _install_crash_hooks(root):
    """全局异常钩子：主线程 + Tk 回调异常写本地日志并弹窗引导反馈。

    服务端暂无收集接口，先落地 crash_log.txt（本地留档）；
    弹窗内一键复制错误详情或调起邮件客户端，降低反馈门槛。"""
    import traceback
    import urllib.parse

    last = {"ts": 0.0}

    def collect(kind, val, tb):
        txt = "".join(traceback.format_exception(type(val), val, tb)) \
            if val is not None else ""
        info = "[%s] %s v%s\n%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), kind, APP_VERSION, txt)
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(info + "\n")
        except OSError:
            pass
        return info

    def show(info):
        # 节流：30 秒内只弹一次，避免异常风暴连环弹窗
        now = time.time()
        if now - last["ts"] < 30:
            return
        last["ts"] = now
        try:
            win = tk.Toplevel(root)
            win.title("程序遇到问题")
            win.configure(bg=BG2)
            win.resizable(False, False)
            body = info[-1200:]
            tk.Label(win, text="程序遇到问题已自动记录，可将以下错误信息反馈给作者以便修复：",
                     bg=BG2, fg=FG, justify="left", wraplength=440,
                     font=("Microsoft YaHei UI", 9)).pack(
                anchor="w", padx=14, pady=(12, 6))
            box = tk.Text(win, bg=BG3, fg=FG_DIM, relief="flat", height=8,
                          width=64, wrap="char", font=("Consolas", 8),
                          padx=8, pady=6)
            box.insert("1.0", body)
            box.configure(state="disabled")
            box.pack(padx=14)
            acts = tk.Frame(win, bg=BG2)
            acts.pack(fill="x", padx=14, pady=(8, 12))

            def copy_err():
                root.clipboard_clear()
                root.clipboard_append(info)
                messagebox.showinfo("已复制", "错误信息已复制到剪贴板",
                                    parent=win)

            def send_mail():
                subject = "[GwentTracker v%s] 错误反馈" % APP_VERSION
                url = "mailto:%s?subject=%s&body=%s" % (
                    FEEDBACK_EMAIL,
                    urllib.parse.quote(subject),
                    urllib.parse.quote(body[:800]))
                import webbrowser
                webbrowser.open(url)

            def _btn(text, cmd):
                tk.Button(acts, text=text, command=cmd, bd=0, bg=BG3, fg=FG,
                          padx=12, pady=5, activebackground=BG3,
                          activeforeground=FG).pack(side="left", padx=(0, 8))

            _btn("复制错误信息", copy_err)
            _btn("邮件反馈", send_mail)
            _btn("关闭", win.destroy)
            win.transient(root)
            win.grab_set()
            win.geometry("+%d+%d" % (root.winfo_x() + 120,
                                     root.winfo_y() + 100))
        except Exception:  # noqa: BLE001
            pass        # 连弹窗都失败时只保留本地日志

    def sys_hook(t, v, tb):
        show(collect("主线程异常", v, tb))

    def tk_hook(exc, val, tb):
        show(collect("界面回调异常", val, tb))

    sys.excepthook = sys_hook
    root.report_callback_exception = tk_hook


# ---------------- 全局热键（RegisterHotKey，无第三方依赖，可自定义） ----------------
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
HOTKEY_DEFAULTS = {"overlay": "Ctrl+Alt+H", "main": "Ctrl+Alt+M",
                   "details": "Ctrl+Alt+D"}
HOTKEY_DESC = {1: "显示/隐藏悬浮窗", 2: "显示主窗口", 3: "打开对局详情"}


def parse_hotkey(s):
    """'Ctrl+Alt+H' / 'Ctrl+Shift+F5' → (mods, vk)；无效返回 None。

    仅接受修饰键组合（避免拦截普通输入），主键支持字母/数字/F1-F12。"""
    if not s:
        return None
    mods = 0
    key = None
    for p in s.split("+"):
        p = p.strip().lower()
        if not p:
            continue
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p == "win":
            mods |= MOD_WIN
        elif key is None:
            key = p
    if key is None or not mods:
        return None
    if len(key) == 1 and key.isalnum():
        return mods, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 12:
            return mods, 0x70 + n - 1
    return None


def _hotkey_specs(app):
    """从配置解析 3 个热键的 (id, 组合键字符串)。"""
    cfg = app.cfg.get("hotkeys") or {}
    return [(1, cfg.get("overlay") or HOTKEY_DEFAULTS["overlay"]),
            (2, cfg.get("main") or HOTKEY_DEFAULTS["main"]),
            (3, cfg.get("details") or HOTKEY_DEFAULTS["details"])]


def _hotkey_thread(app):
    """后台线程：注册热键 + 线程消息循环，WM_HOTKEY 投递到主线程队列。

    注册失败（组合键被其他程序占用）不致命：记录后跳过该键。"""
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    app.hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    failed = []
    for hid, key in _hotkey_specs(app):
        parsed = parse_hotkey(key)
        if not parsed or not user32.RegisterHotKey(None, hid,
                                                   parsed[0], parsed[1]):
            failed.append(hid)
    app.hotkey_failed = failed
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            app.hotkey_q.put(msg.wParam)


def _spawn_hotkey_thread(app):
    app.hotkey_failed = []
    threading.Thread(target=_hotkey_thread, args=(app,), daemon=True).start()


def _stop_hotkeys(app):
    """注销热键并结束热键线程（PostThreadMessage WM_QUIT，线程退出时注销）。"""
    tid = getattr(app, "hotkey_tid", 0)
    if tid:
        try:
            ctypes.windll.user32.PostThreadMessageW(tid, 0x0012, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        app.hotkey_tid = 0


def _start_hotkeys(app):
    """启动热键线程 + 主线程轮询分发；可重复调用以让自定义按键即时生效。"""
    if not sys.platform.startswith("win"):
        return
    if getattr(app, "_hotkey_running", False):
        # 已在运行：停旧线程后按最新配置重启（轮询 loop 复用）
        _stop_hotkeys(app)
        app.root.after(120, lambda: _spawn_hotkey_thread(app))
        return
    app.hotkey_q = queue.Queue()
    app.hotkey_failed = []
    app._hotkey_running = True
    threading.Thread(target=_hotkey_thread, args=(app,), daemon=True).start()

    def poll():
        while True:
            try:
                hid = app.hotkey_q.get_nowait()
            except queue.Empty:
                break
            try:
                if hid == 1:
                    app.on_toggle_overlay()
                elif hid == 2:
                    root = app.root
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                    root.attributes("-topmost", True)
                    root.after(200, lambda: root.attributes("-topmost", False))
                elif hid == 3:
                    app.on_match_details()
            except Exception:  # noqa: BLE001
                pass
        if app.hotkey_failed:
            app._log("部分全局热键注册失败（可能与其他程序冲突）："
                     + "、".join(HOTKEY_DESC[i] for i in app.hotkey_failed
                                 if i in HOTKEY_DESC))
            app.hotkey_failed = []
        app.root.after(150, poll)

    app.root.after(150, poll)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.db = None
        self.detector = None
        self.queue = queue.Queue()
        self.match = MatchRecord()
        self.in_match = False
        self.round_no = 1
        self.match_limit = self.cfg.get("deck_limit", 165)
        self.game_state = ("", "未启动")
        self.match_info = None      # 内存读取的对局信息（模式/昵称/天梯分/回合）
        self.rec_state = ("", "待机")
        self.game_hwnd = 0
        self._last_log = ""
        self._dock_ok = bool(self.cfg.get("dock_to_game", False))
        self._leader_prov = 0      # 对手领袖人口（自动读取，0 = 未知）
        self._my_leader_prov = 0   # 我方领袖人口（自动读取，0 = 未知）
        self._my_leader_tid = 0    # 我方领袖模板ID（对局详情用）
        self._my_deck_cards = {}   # 我方卡组（内存直读：模板ID->张数，对局详情用）
        self._artpool = _ArtPool(self)   # 卡牌原画共享池（悬浮窗/卡组明细共用）
        self._mem_suppress = False   # 手动结束对局后，抑制自动重开直至本场真正结束
        self.filter_side = None    # None=全部, 0=敌方, 1=我方
        self.filter_round = None   # None=全部, 1/2/3=对应小局
        self._bar_state = {}       # 进度条动画状态：canvas -> {cur, target, c, job}
        self._bar_flow = 0.0       # 进度条流动光效相位（0-1 循环推进）
        self._num_state = {}       # 大数字平滑过渡状态：label -> {cur, target, job}
        self._tree_icons = {}      # 出牌表势力小图标（HUD 主题）
        self._dot_imgs = {}        # 呼吸灯帧图缓存（抗锯齿）
        self._ov = None
        self._art_dl = None        # 完整卡图下载状态（None=空闲，dict=进行中）
        self._hover_iid = None     # 出牌表悬停行
        self._net_ok = False       # 标题区状态灯：是否已连接（绿色呼吸）
        self._dot_color = FG_DIM   # 标题区状态灯颜色
        self._builtin_decks, _ = deck_predict.load_builtin(BUILTIN_DECKS)
        cache_decks, cache_time = deck_predict.load_cache(
            os.path.join(BASE_DIR, "decks_cache.json"))
        # 卡图资源：本地录入卡组优先（用户自定义），其次服务端缓存，最后内置库兜底
        self.local_decks = load_local_decks()
        # 服务端缓存优先（更新），内置 276 套兜底补充冷门卡组
        self.decklib = deck_predict.merge_libraries(
            self.local_decks,
            deck_predict.merge_libraries(cache_decks, self._builtin_decks))
        self._sync_art_urls()   # 卡组库覆盖的卡图 URL 全量注入共享池
        self._lib_time = cache_time if cache_decks else 0
        self._lib_fetching = False   # 卡组库下载中（防重复触发）
        self._session_matches = 0    # 本次运行已结束并保存的对局数
        self._lib_check_job = None   # 卡组库更新检测定时器
        self._lib_outdated = False   # 检测到热门榜大规模更新（提醒中）
        self._opp_leader_tid = 0   # 对手领袖模板ID（卡组预测用）
        self._pred_key = None      # 预测缓存键：领袖+已出牌集合，未变化不重算
        self._pred_fail_streak = 0   # 连续无法匹配卡组的预测更新次数
        # 悬浮窗手动修正（右键）：直接进墓地等未上场效果导致的计数误差
        self._ov_extra = {}        # tid -> 手动补记的已打出张数
        self._ov_cancel = {}       # tid -> 手动回退为未打出的张数
        self._pts = None           # 双方实时点数 (mine, opp)
        self._pts_last = None      # 最近一次非零点数（小局切换瞬间场上已清零，快照兜底用）
        self._ov = None            # 对手卡组预测悬浮窗
        self._theme = self.cfg.get("theme", "light")
        set_theme(self._theme)

        root.title("GwentTracker v%s" % APP_VERSION)
        root.geometry("640x880")
        root.minsize(600, 780)
        self._set_root_bg(BG)
        self._glass_ok = False
        self._log_lines = []
        self._style()
        self._build()
        self._render()
        self._log("工具启动，卡牌库加载中…")
        threading.Thread(target=self._init_db, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(120, self._poll_queue)
        root.after(600, self._auto_connect)
        root.after(3000, self._watch_game_window)
        root.after(2500, self._dock_to_game)
        root.after(4000, self._dock_follow)
        root.after(6000, lambda: self.on_check_update(silent=True))   # 启动后静默检查更新
        root.after(4000, self._auto_refresh_decklib)                  # 卡组库自动更新
        root.after(30000, self._check_decklib_update)                 # 卡组库更新检测
        root.after(1200, self._ov_follow)                             # 悬浮窗跟随游戏窗口
        root.after(400, self._bar_flow_loop)                          # 进度条流动光效
        root.after(150, self._breathe_loop)                           # 标题区状态灯呼吸动画
        root.after(2500, self._ov_autoshow)                           # 游戏在运行则显示悬浮窗
        root.after(3500, self._maybe_prompt_art)                      # 首次启动询问完整卡图下载
        self._update_match_stats()                                    # 对局统计标签

    # ---------------- 毛玻璃与动画 ----------------
    def enable_glass(self):
        self.root.update_idletasks()
        hwnd = self._top_hwnd()
        mode = apply_glass(hwnd, dark=self._is_dark())
        apply_caption(hwnd, BG, FG, EDGE)   # 标题栏/边框取应用底色
        if mode:
            self._glass_ok = True
        else:
            self._set_root_bg(BASE)
        self._fade_in(self.root, 220)

    def _top_hwnd(self):
        try:
            import win32gui
            return win32gui.GetParent(self.root.winfo_id())
        except Exception:  # noqa: BLE001
            return self.root.winfo_id()

    def _set_root_bg(self, color):
        """直接走 Tcl 给窗口设底色，绕开 customtkinter 对 master.configure 的接管。

        CTk 控件在创建时会把父窗口的 configure 换成自己的闭包（用来把 bg
        同步给子控件）。切主题重建界面会销毁这些控件，但覆盖没有还原，于是
        root.configure(bg=…) 抛 invalid command name 并被静默吞掉，窗口底色
        停在旧主题上 —— 表现就是面板四周和面板缝隙露出一圈旧主题的深色“黑边”。
        """
        try:
            self.root.tk.call(self.root._w, "configure", "-background", color)
        except tk.TclError:
            try:
                self.root.configure(bg=color)
            except tk.TclError:
                pass

    def _fade_in(self, win, ms=200):
        try:
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            return
        steps = 10
        dt = max(10, ms // steps)

        def step(i=0):
            try:
                win.attributes("-alpha", (i + 1) / steps)
            except tk.TclError:
                return
            if i + 1 < steps:
                win.after(dt, lambda: step(i + 1))

        def restore():   # 动画异常时兜底，避免窗口停在半透明
            try:
                win.attributes("-alpha", 1.0)
            except tk.TclError:
                pass

        win.after(dt, step)
        win.after(ms + 80, restore)

    def _glass_toplevel(self, title, geometry=None):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG2 if not self._glass_ok else BG)
        win.transient(self.root)
        # 子窗口（设置/帮助/关于）统一使用主程序图标
        apply_window_icon(win)
        if geometry:
            win.geometry(geometry)
        win.update_idletasks()
        try:
            import win32gui
            hwnd = win32gui.GetParent(win.winfo_id())
            if self._glass_ok and apply_glass(hwnd, dark=self._is_dark()):
                win.configure(bg=BG)
            else:
                win.configure(bg=BASE)
        except Exception:  # noqa: BLE001
            win.configure(bg=BASE)
        self._fade_in(win, 160)
        return win

    def _scroll_frame(self, win, bg):
        """带纵向滚动条的内容容器：内容超高时可滚动到底部按钮不再被挤出。

        返回内容根 frame；鼠标滚轮滚动（Treeview 区域由表格自身处理）。"""
        cv = tk.Canvas(win, bg=bg, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(win, orient="vertical", command=cv.yview)
        frm = tk.Frame(cv, bg=bg)
        wid = cv.create_window((0, 0), window=frm, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cv.bind("<Configure>", lambda e: cv.itemconfig(wid, width=e.width))
        frm.bind("<Configure>",
                 lambda _e: cv.configure(scrollregion=cv.bbox("all")))

        def _wheel(e):
            if isinstance(e.widget, ttk.Treeview):
                return                    # 表格自身滚动，避免双重滚动
            cv.yview_scroll(-1 if e.delta > 0 else 1, "units")

        cv.bind("<Enter>", lambda _e: cv.bind_all("<MouseWheel>", _wheel))
        cv.bind("<Leave>", lambda _e: cv.unbind_all("<MouseWheel>"))
        return frm

    def confirm_risk(self):
        """首次启动风险提示：必须同意才能使用，不同意则退出软件。

        返回 True=同意并继续；False=退出。同意结果写入配置，之后不再弹出。
        """
        if self.cfg.get("risk_agreed"):
            return True
        win = self._glass_toplevel("风险提示 — 请阅读后选择", "480x430")
        win.grab_set()
        result = {"ok": False}

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(18, 0))
        tk.Label(body, text="使用前必读", bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        txt = (
            "1. 本工具通过读取游戏进程内存实现自动记录，\n"
            "    只读取、不写入、不修改任何游戏文件与代码。\n"
            "\n"
            "2. 使用任何第三方辅助工具都可能违反游戏用户协议，\n"
            "    存在账号被警告、限制或处罚的风险，请自行评估\n"
            "    并自愿承担全部后果。\n"
            "\n"
            "3. 本工具与 CD PROJEKT RED 官方无任何关联，\n"
            "    数据来源为本地读取与公开的热门卡组库。\n"
            "\n"
            "4. 请勿将本工具用于任何商业用途。"
        )
        tk.Label(body, text=txt, bg=BG, fg=FG, justify="left",
                 font=("Microsoft YaHei UI", 10), anchor="w",
                 wraplength=410).pack(anchor="w", pady=(10, 0))

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=26, pady=(14, 20))

        def agree():
            result["ok"] = True
            self.cfg["risk_agreed"] = True
            save_config(self.cfg)
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()

        def quit_app():
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()

        GlassButton(btns, "同意并继续", agree, accent=True, height=34,
                    font_size=10, padx=22).pack(side="left")
        GlassButton(btns, "退出", quit_app, height=34,
                    font_size=10, padx=22).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", quit_app)
        win.wait_window()
        return result["ok"]

    # ---------------- 初始化 ----------------
    def _init_db(self):
        try:
            gdir = self.cfg.get("game_dir") or find_game_dir()
            if gdir and gdir != self.cfg.get("game_dir"):
                self.cfg["game_dir"] = gdir
                save_config(self.cfg)
            db, err = build_or_load(gdir, DB_CACHE)
            self.db, self.db_err = db, err
            self.queue.put(("db", {"count": len(db.cards)}))
        except Exception as e:  # noqa: BLE001
            self.db_err = str(e)
            self.queue.put(("db", {"error": str(e)}))

    # ---------------- 界面 ----------------
    def _style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=BG2, foreground=FG, fieldbackground=BG2,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                     troughcolor=BG3)
        st.configure("TFrame", background=BG2)
        st.configure("TLabel", background=BG2, foreground=FG)
        st.configure("Treeview", background=BG2, foreground=FG,
                     fieldbackground=BG2, rowheight=28, borderwidth=0)
        st.configure("Treeview.Heading", background=SURF2, foreground=FG_DIM,
                     borderwidth=0, padding=(4, 6),
                     font=("Microsoft YaHei UI", 8))
        st.map("Treeview", background=[("selected", ROW_SEL)],
               foreground=[("selected", FG)])
        st.map("TCheckbutton", background=[("active", BG2)])

    def _btn(self, parent, text, cmd, accent=False, danger=False):
        """透明毛玻璃按钮（圆角）；danger=破坏性操作的红色语义。"""
        return GlassButton(parent, text, cmd, accent=accent, danger=danger)

    def _panel(self, parent, radius=14, **kw):
        """圆角卡片：实色表面 + 1px 描边 + 顶部 1px 受光高光（明暗分层）。"""
        pbg = parent.cget("bg") if hasattr(parent, "cget") else BG
        if ctk is not None:
            outer = ctk.CTkFrame(parent, fg_color=BG2, border_color=EDGE,
                                 border_width=1, corner_radius=radius,
                                 bg_color=pbg)
            outer.pack(**kw)
        else:
            outer = tk.Frame(parent, bg=EDGE)
            inner = tk.Frame(outer, bg=BG2)
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            outer.pack(**kw)
            outer = inner
        self._panel_edges(outer, radius)
        return outer

    def _panel_edges(self, panel, radius=14):
        """面板顶缘 1px 受光高光（skip 圆角区域），营造“受光 = 更近”的层次。"""
        try:
            r = max(6, int(radius))
            tk.Frame(panel, bg=GLOSS, height=1, bd=0,
                     highlightthickness=0).place(
                x=r + 2, y=1, relwidth=1.0, width=-(r + 2) * 2, height=1)
        except tk.TclError:
            pass

    def _chip(self, parent, title, value="—", dot=False, breathe=False):
        """状态胶囊：SURF2 底 + 1px 柔边 + 状态灯 + 「标题 值」。

        返回值为右侧的数值 Label（外部 _set_state 直接改它的文字/颜色），
        胶囊本身挂在返回值的 .chip 上。
        """
        pbg = parent.cget("bg") if hasattr(parent, "cget") else BG2
        if ctk is not None:
            box = ctk.CTkFrame(parent, fg_color=SURF2, corner_radius=999,
                               border_width=1, border_color=EDGE_SOFT,
                               bg_color=pbg)
        else:
            box = tk.Frame(parent, bg=SURF2, highlightbackground=EDGE_SOFT,
                           highlightthickness=1, bd=0)
        row = tk.Frame(box, bg=SURF2)
        row.pack(padx=12, pady=6)
        lbl = tk.Label(row, text=value, bg=SURF2, fg=FG,
                       font=("Microsoft YaHei UI", 9, "bold"))
        if dot:
            d = _Dot(row, FG_DIM, bg=SURF2, size=10, breathe=breathe)
            d.pack(side="left", padx=(0, 7))
            lbl.dot = d
            if breathe:
                self.dot_cv = d
        else:
            lbl.dot = None
        tk.Label(row, text=title, bg=SURF2, fg=FG_DIM,
                 font=("Microsoft YaHei UI", 8)).pack(side="left",
                                                      padx=(0, 6))
        lbl.pack(side="left")
        lbl.chip = box
        return lbl

    def _combo(self, parent, textvariable, values, width=14):
        """下拉框：ttk.Combobox + 显式注入当前主题配色。

        CTkOptionMenu 的外观跟随其内部亮暗模式，部分主题下选中文字与
        背景同色看不清；ttk 样式直接取当前主题的 BG3/FG，保证对比度。
        """
        st = ttk.Style()
        st.configure("GT.TCombobox", fieldbackground=BG3, background=BG3,
                     foreground=FG, arrowcolor=FG, bordercolor=BORDER,
                     lightcolor=BG3, darkcolor=BG3, padding=3)
        st.map("GT.TCombobox",
               fieldbackground=[("readonly", BG3), ("active", BG3)],
               foreground=[("readonly", FG), ("active", FG)],
               background=[("active", BG3)])
        # 下拉列表同样用主题配色（option 数据库对 ttk 列表框生效）
        for opt, val in (("*TCombobox*Listbox.background", BG2),
                         ("*TCombobox*Listbox.foreground", FG),
                         ("*TCombobox*Listbox.selectBackground", ROW_SEL),
                         ("*TCombobox*Listbox.selectForeground", FG)):
            self.root.option_add(opt, val)
        return ttk.Combobox(parent, textvariable=textvariable, values=values,
                            state="readonly", width=width,
                            style="GT.TCombobox",
                            font=("Microsoft YaHei UI", 9))

    def _entry(self, parent, textvariable=None, width=40, placeholder=""):
        """圆角输入框（CustomTkinter）；缺失时退回原生 tk.Entry。"""
        if ctk is not None:
            e = ctk.CTkEntry(parent, width=width, height=28, corner_radius=8,
                             border_width=1, border_color=BORDER,
                             fg_color=BG3, text_color=FG,
                             placeholder_text=placeholder or None,
                             font=("Microsoft YaHei UI", 10))
            if textvariable is not None:
                e.configure(textvariable=textvariable)
            return e
        e = tk.Entry(parent, width=width, bg=BG2, fg=FG, relief="flat",
                     insertbackground=FG, font=("Microsoft YaHei UI", 10))
        if textvariable is not None:
            e.config(textvariable=textvariable)
        return e

    def _switch(self, parent, text, variable):
        """设置项开关：游戏风格复选框（绿色实心方块 + 白勾，见 _check）。

        历史实现为 CustomTkinter 圆角滑块，但部分环境构建失败会回退到
        原生 ttk.Checkbutton（系统白框样式，与整体风格不符）；且用户
        希望与游戏内筛选器一致的绿色方块样式，故统一改用 _check 自绘，
        不再依赖 CTk 是否可用。
        v1.8.12 教训：CTk 构建失败曾使设置窗口构建中断（残窗 + grab
        卡死），_check 为纯 tk Canvas 实现，无外部依赖，天然免疫。
        """
        return self._check(parent, text, variable,
                           bg=parent.cget("bg")
                           if hasattr(parent, "cget") else BG)

    def _check(self, parent, text, variable, bg=None):
        """游戏风格复选框（Canvas 自绘）：未选=深色空心圆角方块，
        已选=绿色实心方块 + 白色对勾（参考昆特牌游戏内筛选器样式）。

        原生 tk.Checkbutton 在深色主题下是系统白框，风格不符；
        点击方块或文字均可切换，变量变化自动重绘。"""
        bg = bg if bg is not None else BG3
        box = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0,
                        cursor="hand2", takefocus=0)
        font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        def _box_pts(x0, y0, x1, y1, r):
            return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
                    x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
                    x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

        def _draw(*_a):
            box.delete("all")
            sel = bool(variable.get())
            s = int(15 * UI_SCALE)
            r = max(3, int(4 * UI_SCALE))
            x0 = y0 = s // 2
            x1, y1 = x0 + s, y0 + s
            box.create_polygon(_box_pts(x0, y0, x1, y1, r), smooth=True,
                               fill=OK if sel else BG2,
                               outline=OK if sel else FG_DIM)
            if sel:
                box.create_line(x0 + int(s * 0.24), y0 + int(s * 0.53),
                                x0 + int(s * 0.43), y0 + int(s * 0.72),
                                x1 - int(s * 0.18), y0 + int(s * 0.28),
                                fill="#FFFFFF", width=max(2, int(2 * UI_SCALE)),
                                capstyle="round", joinstyle="round")
            box.create_text(x1 + int(8 * UI_SCALE), (y0 + y1) // 2, text=text,
                            anchor="w", fill=FG, font=font)
            box.configure(width=x1 + int(14 * UI_SCALE) + font.measure(text),
                          height=y1 + int(4 * UI_SCALE))

        def _toggle(_e=None):
            variable.set(not bool(variable.get()))

        box.bind("<Button-1>", _toggle)
        tname = variable.trace_add("write", _draw)
        box.bind("<Destroy>", lambda _e: variable.trace_remove("write", tname))
        _draw()
        return box

    def _build(self):
        # v1.8.0 起全部主题统一使用 HUD 布局：切换主题只改变配色，
        # 不再回退到旧版界面
        self._build_hud()

    # ---------------- HUD 主题（猎魔） ----------------
    def _hud_title(self, parent, text, size=12):
        """面板小标题：3px 金色竖条 + 主色文字。

        金色只用于竖条这一处“点睛”，标题文字改为主色，避免整屏金光、
        让金色重新变成“强调”而不是“默认”。
        """
        tk.Frame(parent, width=3, height=15, bg=ACCENT).pack(side="left")
        tk.Label(parent, text=text, bg=parent.cget("bg"), fg=FG,
                 font=("Microsoft YaHei UI", size, "bold")
                 ).pack(side="left", padx=(7, 0))

    def _build_hud(self):
        """HUD 主题界面：深色沉浸 + 毛玻璃面板 + 金色强调（参考现代游戏 HUD）。"""
        root = self.root

        # —— 标题区：左金色 logo / 中连接状态（呼吸灯）/ 右圆形按钮 ——
        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=16, pady=(12, 4))
        logo_box = tk.Frame(head, bg=BG)
        logo_box.pack(side="left")
        tk.Label(logo_box, text="GwentTracker", font=("Microsoft YaHei UI", 17, "bold"),
                 bg=BG, fg=ACCENT).pack(side="left")
        tk.Label(logo_box, text="昆特助手 · v%s" % APP_VERSION,
                 font=("Microsoft YaHei UI", 8), bg=BG, fg=FG_DIM
                 ).pack(side="left", padx=(8, 2), pady=(6, 0))

        # 连接状态灯已下沉到下方状态芯片（游戏），标题区只留品牌 + 操作，
        # 消除“未启动”在两处重复出现的噪音
        tk.Frame(head, bg=BG).pack(side="left", expand=True, fill="both")

        bar = tk.Frame(head, bg=BG)
        bar.pack(side="right")
        self.btn_matches = _RoundBtn(bar, "☰", self.on_match_details,
                                     tip="对局详情")
        self.btn_matches.pack(side="left", padx=(6, 0))
        self.btn_stats = _RoundBtn(bar, "%", self.on_match_stats,
                                   tip="天梯胜率统计")
        self.btn_stats.pack(side="left", padx=(6, 0))
        self.btn_theme = _RoundBtn(bar, "◐", self.on_toggle_theme, tip="切换主题")
        self.btn_theme.pack(side="left", padx=(6, 0))
        self.btn_settings = _RoundBtn(bar, "⚙", self.on_settings, tip="设置")
        self.btn_settings.pack(side="left", padx=(6, 0))
        self.btn_reconnect = _RoundBtn(bar, "⟳", self.on_reconnect,
                                       primary=True, tip="重新连接游戏")
        self.btn_reconnect.pack(side="left")

        # —— 问候语：时段 + 内存读取的游戏昵称（对局中自动填充） ——
        self.lbl_greet = tk.Label(root, text="", bg=BG, fg=FG_DIM,
                                  font=("Microsoft YaHei UI", 9), anchor="w")
        self.lbl_greet.pack(fill="x", padx=20, pady=(0, 2))
        self._update_greeting()
        self._greet_loop()

        # —— 状态芯片：游戏 / 识别 / 对局（胶囊 + 状态灯，替代三栏表格） ——
        strip = self._panel(root, fill="x", padx=16, pady=(0, 8))
        srow = tk.Frame(strip, bg=BG2)
        srow.pack(fill="x", padx=12, pady=(7, 7))
        self.lbl_game = self._chip(srow, "游戏", "—", dot=True, breathe=True)
        self.lbl_game.chip.pack(side="left")
        self.lbl_rec = self._chip(srow, "识别", "—")
        self.lbl_rec.chip.pack(side="left", padx=(8, 0))
        self.lbl_round = self._chip(srow, "对局", "未开始")
        self.lbl_round.chip.pack(side="left", padx=(8, 0))
        self.lbl_frame_size = tk.Label(srow, text="", bg=BG2, fg=FG_DIM,
                                       font=("Microsoft YaHei UI", 8),
                                       anchor="e")
        self.lbl_frame_size.pack(side="right")

        # —— 人口对比仪表盘：左右分栏，中间细竖线 ——
        prov = self._panel(root, fill="x", padx=16, pady=(0, 8))
        top = tk.Frame(prov, bg=BG2)
        top.pack(fill="x", padx=12, pady=(8, 0))
        self._hud_title(top, "人口对比")
        self.lbl_delta = tk.Label(top, text="", bg=BG2, fg=FG_DIM,
                                  font=("Microsoft YaHei UI", 9))
        self.lbl_delta.pack(side="right")
        grid = tk.Frame(prov, bg=BG2)
        grid.pack(fill="x", padx=12, pady=(2, 10))
        grid.columnconfigure(0, weight=1, uniform="p")
        grid.columnconfigure(2, weight=1, uniform="p")
        self.my_used, self.my_sub, self.bar_mine = self._prov_col_hud(
            grid, 0, "我方", MINE, left=True)
        tk.Frame(grid, width=1, bg=EDGE_SOFT).grid(
            row=0, column=1, sticky="ns", padx=12)
        self.opp_used, self.opp_sub, self.bar = self._prov_col_hud(
            grid, 2, "对手", OPP, left=False)

        # —— 对局信息：对手昵称/职天分数/等级/回合归属（原卡组预测区，预测已全部移至悬浮窗） ——
        predp = self._panel(root, fill="x", padx=16, pady=(0, 8))
        prow = tk.Frame(predp, bg=BG2)
        prow.pack(fill="x", padx=12, pady=(8, 0))
        self._hud_title(prow, "对局信息", size=12)
        self.btn_decklib = self._btn(prow, "更新卡组库", self.on_refresh_decklib)
        self.btn_decklib.pack(side="right")
        self.btn_mydecks = self._btn(prow, "我的卡组库", self.on_local_decks)
        self.btn_mydecks.pack(side="right", padx=(0, 6))
        self.btn_overlay = self._btn(
            prow, "悬浮窗" if not self.cfg.get("pred_overlay", True)
            else "隐藏悬浮", self.on_toggle_overlay)
        self.btn_overlay.pack(side="right", padx=(0, 6))
        self.btn_ladder = self._btn(
            prow,
            "模式:天梯/训练" if self.cfg.get("pred_ladder", True)
            else "模式:其他(预测关)", self.on_toggle_ladder)
        self.btn_ladder.pack(side="right", padx=(0, 6))

        body = tk.Frame(predp, bg=BG2)
        body.pack(fill="x", padx=12, pady=(4, 8))
        self._pred_body = body
        self._pred_hint = tk.Label(
            predp, text="等待对局开始 · 进入对局后显示对手与当前比分",
            bg=BG2, fg=FG_DIM, font=("Microsoft YaHei UI", 9), anchor="w")
        # 信息行：对手昵称/段位/职天分（左） + 回合归属（右）
        info_row = tk.Frame(body, bg=BG2)
        info_row.pack(fill="x")
        self.lbl_pred_top = tk.Label(info_row, text="", bg=BG2, fg=FG,
                                     font=("Microsoft YaHei UI", 10, "bold"),
                                     anchor="w")
        self.lbl_pred_top.pack(side="left")
        self.lbl_pred_turn = tk.Label(info_row, text="", bg=BG2, fg=FG_DIM,
                                      font=("Microsoft YaHei UI", 9),
                                      anchor="e")
        self.lbl_pred_turn.pack(side="right")
        # 比分行：当前小局实时点数（大号双色：我方蓝 / 敌方橙）+ 局分计数
        score_row = tk.Frame(body, bg=BG2)
        score_row.pack(fill="x", pady=(5, 0))
        self.lbl_score_mine = tk.Label(score_row, text="—", bg=BG2, fg=MINE,
                                       font=("Consolas", 17, "bold"))
        self.lbl_score_mine.pack(side="left")
        tk.Label(score_row, text="  :  ", bg=BG2, fg=FG_DIM,
                 font=("Consolas", 13)).pack(side="left")
        self.lbl_score_opp = tk.Label(score_row, text="—", bg=BG2, fg=OPP,
                                      font=("Consolas", 17, "bold"))
        self.lbl_score_opp.pack(side="left")
        self.lbl_score_cap = tk.Label(score_row, text="", bg=BG2, fg=ACCENT,
                                      font=("Microsoft YaHei UI", 9, "bold"),
                                      anchor="e")
        self.lbl_score_cap.pack(side="right")
        # 兼容占位：预测提示逻辑引用这些 Label，HUD 主题下不再显示
        self.lbl_pred_alt = tk.Label(predp, text="")
        self.lbl_pred_miss = tk.Label(predp, text="")

        # —— 对局事件（时间轴）：左竖线 + 彩色圆点 ——
        logp = self._panel(root, fill="x", padx=16, pady=(0, 8))
        logh = tk.Frame(logp, bg=BG2)
        logh.pack(fill="x", padx=12, pady=(8, 0))
        self._hud_title(logh, "对局事件")
        self.lbl_match_stats = tk.Label(logh, text="", bg=BG2, fg=FG_DIM,
                                        font=("Microsoft YaHei UI", 8),
                                        anchor="e")
        self.lbl_match_stats.pack(side="right")
        lbody = tk.Frame(logp, bg=BG2)
        lbody.pack(fill="x", padx=12, pady=(2, 6))
        tk.Frame(lbody, width=2, bg=BORDER).pack(side="left", fill="y",
                                                 padx=(2, 8))
        self.txt_log = tk.Text(lbody, height=5, bg=BG2, fg=FG, relief="flat",
                               font=("Microsoft YaHei UI", 9), wrap="word",
                               highlightthickness=0, state="disabled", bd=0,
                               padx=0, spacing1=1, spacing3=1)
        self.txt_log.pack(side="left", fill="x", expand=True)
        for name, col in (("dot_m", MINE), ("dot_o", OPP),
                          ("dot_a", ACCENT), ("dot_e", ERR)):
            self.txt_log.tag_configure(
                name, foreground=col,
                font=("Microsoft YaHei UI", 9, "bold"))
        self.txt_log.tag_configure("t", foreground=FG_DIM,
                                   font=("Microsoft YaHei UI", 8))
        self.txt_log.tag_configure("m", foreground=FG,
                                   font=("Microsoft YaHei UI", 9))
        self.txt_log.tag_configure("dim", foreground=FG_DIM)

        # —— 出牌记录表：势力小图标 + 行悬停高亮 + 狼徽章水印 ——
        cols = ("no", "round", "side", "name", "prov", "count")
        holder = self._panel(root, fill="both", expand=True, padx=16,
                             pady=(0, 6))
        thead = tk.Frame(holder, bg=BG2)
        thead.pack(fill="x", padx=10, pady=(6, 2))
        self._hud_title(thead, "出牌记录")
        # 小局筛选：全部/第1局/第2局/第3局（与右侧阵营筛选叠加生效）
        for txt, rv, px in (("全部", None, 14), ("第1局", 1, 4),
                            ("第2局", 2, 4), ("第3局", 3, 4)):
            b = GlassButton(thead, txt,
                            lambda r=rv: self.on_filter_round(r),
                            height=24, font_size=8, padx=8)
            b.pack(side="left", padx=(px, 0))
            setattr(self, "btn_r_all" if rv is None else "btn_r%d" % rv, b)
        for txt, cmd, px in (("仅我方", lambda: self.on_filter(1), 0),
                             ("仅敌方", lambda: self.on_filter(0), 6),
                             ("全部", lambda: self.on_filter(None), 6)):
            b = GlassButton(thead, txt, cmd, height=24, font_size=8, padx=10)
            b.pack(side="right", padx=(0, px))
            if txt == "仅我方":
                self.btn_f_mine = b
            elif txt == "仅敌方":
                self.btn_f_opp = b
            else:
                self.btn_f_all = b
        self.tree = ttk.Treeview(holder, columns=cols, show="tree headings",
                                 height=5)
        self.tree.column("#0", width=26, minwidth=26, stretch=False,
                         anchor="center")
        for cid, title, w, anchor in [
            ("no", "#", 30, "e"),
            ("round", "局", 52, "center"),
            ("side", "方", 32, "center"),
            ("name", "卡 牌", 150, "w"),
            ("prov", "人口", 48, "e"),
            ("count", "次数", 48, "e"),
        ]:
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=w, anchor=anchor, stretch=False)
        self.tree.column("name", stretch=True)
        self.tree.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self.lbl_empty = tk.Label(holder, text="等待对局数据…", bg=BG2,
                                  fg=FG_DIM, font=("Microsoft YaHei UI", 9))
        self.lbl_empty.place(relx=0.5, rely=0.5, anchor="center")
        self.tree.tag_configure("odd", background=ROW_ODD)
        self.tree.tag_configure("even", background=BG2)
        self.tree.tag_configure("gold", foreground=GOLD_FG)
        self.tree.tag_configure(
            "hov", background=_mix(_hex_rgb(BG2), (255, 255, 255), 0.04))
        self.tree.bind("<Delete>", lambda e: self.on_delete())
        self.tree.bind("<Motion>", self._tree_hover, add="+")
        self.tree.bind("<Leave>", lambda e: self._tree_hover_clear())
        self._hover_iid = None
        self._make_tree_icons()

        # —— 底部操作行：常用操作 + 三级文字链接 ——
        ops = tk.Frame(root, bg=BG)
        ops.pack(fill="x", padx=16, pady=(0, 6))
        self.btn_delete = self._btn(ops, "删除选中", self.on_delete)
        self.btn_delete.pack(side="left")
        self._btn(ops, "清空(不保存)", self.on_clear_nosave,
                  danger=True).pack(side="left", padx=(8, 0))
        links = tk.Frame(ops, bg=BG)
        links.pack(side="right")
        for txt, cmd in (("帮助", self.on_help), ("关于", self.on_about),
                         ("检查更新", self.on_check_update)):
            l = tk.Label(links, text=txt, bg=BG, fg=FG_DIM,
                         font=("Microsoft YaHei UI", 9), cursor="hand2")
            l.pack(side="left", padx=(12, 0))
            l.bind("<Enter>", lambda e, w=l: w.config(fg=FG))
            l.bind("<Leave>", lambda e, w=l: w.config(fg=FG_DIM))
            l.bind("<Button-1>", lambda e, c=cmd: c())
        self._sync_pred_empty()

    def _sync_pred_empty(self):
        """空闲时收起「对局信息」的比分行，改显示一行提示，避免留空盒子。"""
        body = getattr(self, "_pred_body", None)
        hint = getattr(self, "_pred_hint", None)
        if body is None or hint is None:
            return
        busy = bool(self.in_match)
        if busy == getattr(self, "_pred_busy", None):
            return
        self._pred_busy = busy
        try:
            if busy:
                hint.pack_forget()
                body.pack(fill="x", padx=12, pady=(4, 8))
            else:
                body.pack_forget()
                hint.pack(fill="x", padx=12, pady=(2, 10))
        except tk.TclError:
            pass

    def _prov_col_hud(self, grid, col, title, color, left=True):
        """人口仪表盘单列：大数字 + 8px 圆角流光进度条。"""
        f = tk.Frame(grid, bg=BG2)
        f.grid(row=0, column=col, sticky="we",
               padx=(0, 12) if left else (12, 0))
        anc = "w" if left else "e"
        tk.Label(f, text=title, bg=BG2, fg=color,
                 font=("Microsoft YaHei UI", 9, "bold"),
                 anchor=anc).pack(fill="x")
        row = tk.Frame(f, bg=BG2)
        row.pack(fill="x", pady=(1, 0))
        used = tk.Label(row, text="0", bg=BG2, fg=color,
                        font=("Microsoft YaHei UI", 24, "bold"), anchor=anc)
        used.pack(side="left" if left else "right")
        sub = tk.Label(row, text="/ 166 · 剩 166", bg=BG2, fg=FG_DIM,
                       font=("Microsoft YaHei UI", 9), anchor=anc)
        sub.pack(side="left" if left else "right",
                 padx=(8, 0) if left else (0, 8), pady=(0, 4))
        bar = tk.Canvas(f, height=8, bg=BG2, highlightthickness=0, bd=0)
        bar.pack(fill="x", pady=(4, 0))
        bar.bind("<Configure>", lambda e: self._render())
        return used, sub, bar

    def _tween_num(self, lbl, value):
        """大数字平滑过渡（0.3s）：人口变化时逐帧逼近。"""
        st = self._num_state.setdefault(
            lbl, {"cur": None, "job": None, "target": None})
        if st["cur"] is None or not lbl.winfo_exists():
            st["cur"] = value
            try:
                lbl.config(text=str(value))
            except tk.TclError:
                pass
            return
        st["target"] = value
        if st["job"] is None:
            st["job"] = self.root.after(33, lambda: self._num_step(lbl))

    def _num_step(self, lbl):
        st = self._num_state.get(lbl)
        if st is None or not lbl.winfo_exists():
            return
        st["job"] = None
        cur, target = st["cur"], st["target"]
        if abs(target - cur) < 0.6:
            st["cur"] = target
        else:
            st["cur"] = cur + (target - cur) * 0.3
            st["job"] = self.root.after(33, lambda: self._num_step(lbl))
        try:
            lbl.config(text="%d" % round(st["cur"]))
        except tk.TclError:
            pass

    def _breathe_loop(self):
        """标题区状态灯：呼吸动画 opacity 0.5↔1.0，2s 循环（HUD 主题）。

        逐像素抗锯齿绘制（create_oval 有毛边），帧图按相位量化缓存。"""
        cv = getattr(self, "dot_cv", None)
        if cv is not None:
            try:
                if cv.winfo_exists():
                    t = (time.time() % 2.0) / 2.0
                    cv.set_alpha(0.45 + 0.55 * (0.5 - 0.5 * math.cos(2 * math.pi * t)))
            except tk.TclError:
                pass
        self.root.after(66, self._breathe_loop)

    def _pred_bar_reset(self):
        """主界面预测显示已全部移至悬浮窗，仅保留空实现兼容旧调用。"""
        pass

    def _make_tree_icons(self):
        """出牌表势力小图标（16x16）：我方蓝 / 敌方橙 / 敌方金卡金环。

        逐像素抗锯齿绘制（按覆盖度混合边缘色），纯 Tk 实现不依赖 PIL
        ——此前用 PIL.ImageTk，打包环境下构建失败会使 _tree_icons 为空，
        _render 传 image=None 触发 TclError（v1.8.3 根因）。
        """
        self._tree_icons = {}
        try:
            base = _hex_rgb(BG2)

            def mk(color, ring=None):
                col = _hex_rgb(color)
                ring_c = _hex_rgb(ring) if ring else None

                def shader(x, y):
                    d = math.hypot(x + 0.5 - 8, y + 0.5 - 8)
                    cc = max(0.0, min(1.0, 5.0 - d))     # 实心圆 r≈4.5
                    rc = 0.0
                    if ring_c is not None:
                        rc = max(0.0, min(1.0,
                                          min(d - 5.5, 7.0 - d) + 0.5))
                    if cc <= 0 and rc <= 0:
                        return None
                    out = base
                    if cc > 0:
                        out = _mixt(out, col, cc)
                    if rc > 0:
                        out = _mixt(out, ring_c, rc)
                    return _rgb(out)
                return _aa_photo(16, shader)

            self._tree_icons[1] = mk(MINE)
            self._tree_icons[0] = mk(OPP)
            self._tree_icons["gold0"] = mk(OPP, ring=GOLD_FG)
        except Exception:  # noqa: BLE001
            self._tree_icons = {}
            # 图标构建失败会导致出牌表退化（渲染异常已防御），落盘原因便于排查
            import traceback
            try:
                with open(os.path.join(BASE_DIR, "tracker_error.log"), "a",
                          encoding="utf-8") as f:
                    f.write("=== %s 出牌表势力图标构建失败 ===\n%s" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        traceback.format_exc()))
            except OSError:
                pass

    def _tree_hover(self, e):
        """出牌表行悬停高亮（hover 行背景提亮 0.04）。"""
        try:
            iid = self.tree.identify_row(e.y)
        except tk.TclError:
            return
        if iid == self._hover_iid:
            return
        self._tree_hover_clear()
        if iid:
            tags = [t for t in self.tree.item(iid, "tags") if t != "hov"]
            self.tree.item(iid, tags=("hov", *tags))
            self._hover_iid = iid

    def _tree_hover_clear(self):
        if not self._hover_iid:
            return
        try:
            iid = self._hover_iid
            tags = [t for t in self.tree.item(iid, "tags") if t != "hov"]
            self.tree.item(iid, tags=tuple(tags))
        except tk.TclError:
            pass
        self._hover_iid = None

    def _set_state(self, lbl, state):
        color, text = state
        if color:
            lbl.config(text=text, foreground=color)
        else:
            lbl.config(text=text)   # 初始状态无颜色，保持默认
        # 芯片前的状态灯跟随状态色（游戏芯片的灯还带呼吸动画）
        dot = getattr(lbl, "dot", None)
        if dot is not None:
            try:
                if dot.winfo_exists():
                    dot.set_color(color or FG_DIM)
            except tk.TclError:
                pass

    def _log(self, msg):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
        if line == self._last_log:
            return
        self._last_log = line
        self._log_lines.insert(0, line)
        del self._log_lines[60:]
        self._fill_log()

    def _fill_log(self):
        """对局事件时间轴：左侧竖线由面板绘制，事件行用彩色圆点区分来源。"""
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        for line in self._log_lines:
            t, _, msg = line.partition("  ")
            if msg.startswith("我方"):
                dot = "dot_m"       # 我方事件 · 北方蓝
            elif msg.startswith("对手"):
                dot = "dot_o"       # 敌方事件 · 腐血橙
            elif any(k in msg for k in ("失败", "异常", "无法", "断开", "未匹配")):
                dot = "dot_e"       # 异常事件 · 红
            else:
                dot = "dot_a"       # 一般事件 · 金
            self.txt_log.insert("end", "● ", dot)
            self.txt_log.insert("end", t + "  ", "t")
            self.txt_log.insert("end", msg + "\n", "m")
        self.txt_log.configure(state="disabled")

    # ---------------- 停靠 ----------------
    def _dock_to_game(self, force=False):
        """把主窗口贴靠到游戏客户区右侧，垂直居中于游戏。"""
        if not self.cfg.get("dock_to_game", False) and not force:
            return
        hwnd = self.game_hwnd or capture.find_gwent_window()[0]
        if not hwnd:
            return
        try:
            if not capture.is_window_visible(hwnd):
                return  # 游戏最小化时不动
            ox, oy, cw, ch = capture.get_client_rect_screen(hwnd)
        except (OSError, IndexError):
            return
        if cw <= 0:
            return
        self.root.update_idletasks()
        ww = self.root.winfo_width()
        wh = self.root.winfo_height()
        try:
            user32 = ctypes.windll.user32
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
        except Exception:  # noqa: BLE001
            sw, sh = 1920, 1080
        wh = min(wh, sh - 40)
        x = ox + cw + 8
        if x + ww > sw - 8:          # 右边放不下 → 贴到游戏左侧
            x = max(8, ox - ww - 8)
        y = oy + max(0, (ch - wh) // 2)
        y = max(8, min(y, sh - wh - 8))
        self.root.geometry("+%d+%d" % (x, y))

    def _dock_follow(self):
        if self._dock_ok:
            try:
                hwnd = self.game_hwnd or capture.find_gwent_window()[0]
                if hwnd:
                    cur = (self.root.winfo_x(), self.root.winfo_y())
                    try:
                        ox, oy, cw, ch = capture.get_client_rect_screen(hwnd)
                    except (OSError, IndexError):
                        ox = None
                    if ox is not None and cw > 0:
                        want_x = ox + cw + 8
                        try:
                            sw = ctypes.windll.user32.GetSystemMetrics(0)
                        except Exception:  # noqa: BLE001
                            sw = 1920
                        if want_x + self.root.winfo_width() > sw - 8:
                            want_x = max(8, ox - self.root.winfo_width() - 8)
                        want_y = oy + max(0, (ch - self.root.winfo_height()) // 2)
                        if abs(cur[0] - want_x) > 2 or abs(cur[1] - want_y) > 2:
                            self.root.geometry("+%d+%d" % (want_x, want_y))
            except Exception:  # noqa: BLE001
                pass
        self.root.after(1000, self._dock_follow)

    # ---------------- 游戏窗口监视 ----------------
    def _watch_game_window(self):
        # 更新游戏窗口句柄（用于停靠），同时尝试连接进程
        hwnd, _title = capture.find_gwent_window()
        if hwnd:
            self.game_hwnd = hwnd
        if self.detector is None and self.db is not None:
            self._connect()
        self.root.after(3000, self._watch_game_window)

    def _auto_connect(self):
        if self.detector is None:
            if self.db is not None:
                self._connect()
            else:
                self.root.after(2000, self._auto_connect)

    def _mem_leader_cb(self, tid, prov, mine=False):
        self.queue.put(("leader", {"tid": tid, "prov": prov, "mine": mine}))

    def _connect(self):
        if self.detector is not None:
            self.detector.stop()
            self.detector = None
        from mem_source import MemSource
        self.detector = MemSource(
            self.db, self._on_card, self._on_status, self.cfg,
            on_round=lambda r: self.queue.put(("round", r)),
            on_reset=lambda: self.queue.put(("reset", {})),
            on_matchover=lambda mo: self.queue.put(("matchover", mo)),
            on_roundend=lambda re_: self.queue.put(("roundend", re_)),
            on_leader=self._mem_leader_cb,
            on_points=lambda s1, s2, local: self.queue.put(
                ("points", {"s1": s1, "s2": s2, "local": local})),
            on_mydeck=lambda cards: self.queue.put(("mydeck", cards)),
            on_matchinfo=lambda mi: self.queue.put(("matchinfo", mi)))
        self.detector.start()
        self._log("内存读取：正在附加游戏进程…")

    def on_reconnect(self):
        if self.detector is not None:
            self.detector.stop()
            self.detector = None
        self.game_state = (WARN, "连接中…")
        self._set_state(self.lbl_game, self.game_state)
        if self.db is not None:
            self._connect()

    # ---------------- 队列轮询 ----------------
    def _on_card(self, card, meta):
        self.queue.put(("card", {"card": card, "meta": meta}))

    def _on_status(self, st):
        self.queue.put(("status", st))

    def _poll_queue(self):
        try:
            try:
                while True:
                    kind, data = self.queue.get_nowait()
                    if kind == "card":
                        self._auto_record(data["card"], data["meta"])
                    elif kind == "round":
                        self._apply_round(data)
                    elif kind == "reset":
                        self._apply_reset()
                    elif kind == "matchover":
                        # 游戏发出对局结束事件（结算画面出现）：
                        # 立即存档并清空悬浮窗，不必等回到主菜单
                        self._auto_end_match(data)
                    elif kind == "roundend":
                        self._apply_roundend(data)
                    elif kind == "leader":
                        self._apply_leader(data)
                    elif kind == "points":
                        self._apply_points(data)
                    elif kind == "mydeck":
                        self._apply_mydeck(data)
                    elif kind == "matchinfo":
                        self._apply_matchinfo(data)
                    elif kind == "status":
                        self._handle_status(data)
                    elif kind == "db":
                        if "error" in data:
                            self.db = None
                            self._log("卡牌库加载失败: %s" % data["error"])
                            self._set_state(self.lbl_rec, (ERR, "卡牌库失败"))
                        else:
                            self._log("卡牌库就绪: %d 张" % data["count"])
            except queue.Empty:
                pass
        except Exception:  # noqa: BLE001
            # 单个事件处理异常不能杀死轮询循环，否则之后所有事件静默丢失；
            # 窗口化 exe 没有控制台，traceback 必须落盘才能排查
            import traceback
            traceback.print_exc()
            try:
                with open(os.path.join(BASE_DIR, "tracker_error.log"), "a",
                          encoding="utf-8") as f:
                    f.write("=== %s ===\n%s" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        traceback.format_exc()))
            except OSError:
                pass
        finally:
            self.root.after(120, self._poll_queue)

    # ---------------- 自动记录 ----------------
    def _ensure_match(self):
        """检测到对局数据时自动开始记录（使用默认人口上限）。"""
        if self.in_match or self._mem_suppress:
            return
        self.match = MatchRecord()
        self._ov_extra = {}        # 新对局：清空上一场的手动修正
        self._ov_cancel = {}
        self._pts = None           # 新对局：点数重新读取
        self._pts_last = None      # 新对局：非零点数兜底同步清空
        self.match_limit = int(self.cfg.get("deck_limit", 165))
        if self._leader_prov > 0:
            self.match_limit = 150 + self._leader_prov
        self.match.leaders = {"mine": self._my_leader_tid,
                              "opp": self._opp_leader_tid}
        self.match.leader_prov = {"mine": self._my_leader_prov,
                                  "opp": self._leader_prov}
        self.match.my_deck = dict(self._my_deck_cards)
        self.in_match = True
        self.round_no = 1
        self._log("检测到对局，自动开始记录（人口上限 %d）" % self.match_limit)
        self._render()

    def _auto_record(self, card, meta):
        self._ensure_match()
        side = 1 if meta.get("mine") else 0
        self.match.add(card, meta, self.round_no, side)
        if meta.get("auto"):
            how = "召唤"
        elif meta.get("spawned"):
            how = "召唤"
        else:
            how = "打出"
        self._log("%s%s: %s（人口 %s）" % (
            "我方" if side else "对手", how, card["name_zh"], card["provision"]))
        self._render()

    def _apply_round(self, round_no):
        self._ensure_match()
        round_no = max(1, int(round_no))
        if round_no != self.round_no:
            # 小局切换：把刚结束小局的最终点数存档（胜负判定/对局详情用）
            self.match.snapshot_round(self.round_no, self._pts_snapshot())
            self.round_no = round_no
            self._log("第 %d 小局开始" % self.round_no)
            self._render()

    def _apply_roundend(self, data):
        """权威小局结算（RoundEndedHistoryEntry.RoundInfo.PlayerScores）：
        小局结束时双方最终得分，不依赖场上力量扫描（漏攻城排、投降清零都会判错）。"""
        if self.match is None:
            return
        local = int(data.get("local", 0) or 0)
        s1 = int(data.get("s1", 0) or 0)
        s2 = int(data.get("s2", 0) or 0)
        if local == 1:
            mine, opp = s1, s2
        elif local == 2:
            mine, opp = s2, s1
        else:
            return
        if mine <= 0 and opp <= 0:
            return
        rid = max(1, int(data.get("id", 0) or 0))   # 与小局号（1 起）对齐
        self.match.round_scores[rid] = (mine, opp)
        self._update_matchinfo_line()

    def _apply_reset(self):
        """新一场对局的历史重放：清空并重建。"""
        self._mem_suppress = False     # 新一场开始，解除抑制
        self._ensure_match()
        self.match = MatchRecord()
        self._ov_extra = {}        # 新一场：清空上一场的手动修正
        self._ov_cancel = {}
        self._pred_key = None
        self.match.leaders = {"mine": self._my_leader_tid,
                              "opp": self._opp_leader_tid}
        self.match.leader_prov = {"mine": self._my_leader_prov,
                                  "opp": self._leader_prov}
        self.match.my_deck = dict(self._my_deck_cards)
        self.round_no = 1
        self._pts = None               # 点数随新对局重扫后重新推送
        self._pts_last = None
        self._pred_fail_streak = 0     # 新对局重置预测失败计数
        self._log("同步对局历史…")
        self._render()

    def _my_limit(self):
        """我方人口上限 = 150 + 我方领袖人口（领袖未读到前沿用当前上限）。"""
        if self._my_leader_prov > 0:
            return 150 + self._my_leader_prov
        return self.match_limit

    def _opp_limit(self):
        """对手人口上限 = 150 + 对手领袖人口（领袖未读到前沿用当前上限）。"""
        if self._leader_prov > 0:
            return 150 + self._leader_prov
        return self.match_limit

    def _apply_leader(self, data):
        """双方领袖人口自动读取：各自上限 = 150 + 各自领袖人口。

        双方领袖人口可能不同，我方进度条必须用我方领袖人口，
        否则会出现"我方人口显示不正确"（此前只读对手领袖）。
        """
        tid = int(data.get("tid", 0) or 0)
        prov = int(data.get("prov", 0))
        if data.get("mine"):
            if tid:
                self._my_leader_tid = tid
            if prov <= 0 or prov == self._my_leader_prov:
                return
            self._my_leader_prov = prov
            self._log("我方领袖人口 %d → 我方人口上限 %d" % (prov, 150 + prov))
            self._render()
            return
        if tid:
            self._opp_leader_tid = tid
            self._pred_key = None       # 领袖变化触发重新预测
        if prov <= 0 or prov == self._leader_prov:
            return
        self._leader_prov = prov
        self.match_limit = 150 + prov
        self._log("对手领袖人口 %d → 对手人口上限自动设为 %d" % (prov, self.match_limit))
        self._render()

    def _auto_end_match(self, mo=None):
        self._mem_suppress = False     # 本场已真正结束，解除抑制
        if not self.in_match:
            return
        self.in_match = False
        self.match.snapshot_round(self.round_no, self._pts_snapshot())  # 末局最终点数
        self._apply_match_result(mo)   # 权威胜负（投降/断线比分不完整也判得准）
        self.match.end_ts = time.time()
        self.match.leaders = {"mine": self._my_leader_tid,
                              "opp": self._opp_leader_tid}
        self.match.leader_prov = {"mine": self._my_leader_prov,
                                  "opp": self._leader_prov}
        self._session_matches += 1
        w = self.match.winner()
        res = {"mine": "胜利", "opp": "失败", "draw": "平局"}.get(w, "")
        reason_tag = ""
        if self.match.end_reason == 1:
            reason_tag = "（对手投降）" if w == "mine" else "（我方投降）"
        elif self.match.end_reason == 2:
            reason_tag = "（对手断线）" if w == "mine" else "（我方断线）"
        self._log("对局结束%s%s，已保存历史" % (("（%s）" % res) if res else "", reason_tag)
                  if self._save_history() else "对局结束（无记录）")
        self._update_match_stats()
        self._render()
        # 悬浮窗切换为等待状态：清空上一局的卡组预测与人口统计
        # （无论悬浮窗当前是否可见都执行——隐藏时残留的上局数据会在
        #   再次显示时继续展示，造成“上局数据未清零”）
        ov = self._ov
        if ov is not None:
            try:
                ov.set_stats(None)
                if (self.cfg.get("pred_overlay", True)
                        and self.cfg.get("pred_ladder", True)):
                    ov.set_state("等待开始对局")
            except tk.TclError:
                pass

    def _apply_match_result(self, mo):
        """权威整局结果（GameEndedHistoryEntry.WinnerID）：胜负以胜者 ID 为准，
        投降/断线等比分不完整场景也判得准。EEndGameReason: 0=正常 1=投降 2=断线…"""
        if not mo or self.match is None:
            return
        local = int(mo.get("local", 0) or 0)
        wid = int(mo.get("winner", 0) or 0)
        if wid in (1, 2):
            self.match.final_winner = "mine" if wid == local else "opp"
        reason = mo.get("reason")
        if reason is not None:
            self.match.end_reason = int(reason)

    def _game_platform(self):
        """识别游戏发行渠道（GOG / Steam），用于识别栏状态展示
        （内存平台检测的兜底）。

        v2.2.1 起改为 Steam 优先：Steam 版一定装在 steamapps\\common 下，
        而 GOG 版也可能顺带带一份 steam_api64.dll；老逻辑先看 Galaxy/
        goggame 标记，Steam 版就会被误报成 GOG（用户实测踩到过）。
        """
        gd = (self.cfg.get("game_dir") or "").lower()
        if "steamapps" in gd:
            return "Steam"
        if "gog" in gd or "galaxy" in gd:
            return "GOG"
        if "steam" in gd:
            return "Steam"
        gdir = self.cfg.get("game_dir") or ""
        if gdir and os.path.isdir(gdir):
            try:
                if any(n.lower().startswith("goggame-")
                       for n in os.listdir(gdir)):
                    return "GOG"
            except OSError:
                pass
            if (os.path.exists(os.path.join(gdir, "steam_api64.dll"))
                    or os.path.exists(os.path.join(gdir, "steam_api.dll"))):
                return "Steam"
            for f in ("Galaxy64.dll", "Galaxy.dll"):
                if os.path.exists(os.path.join(gdir, f)):
                    return "GOG"
        return ""

    def _handle_status(self, data):
        state = data.get("state", "idle")
        if state.startswith("mem_"):
            self._handle_mem_status(state, data)
            return
        detail = data.get("detail", "")
        if state == "watching":
            self.rec_state = (OK, "监控中·等待检测" if self.in_match else "监控中")
        elif state == "scan_done":
            n = data.get("records", 0)
            base = "监控中·上次扫描 %s" % time.strftime("%H:%M:%S")
            if n:
                base += "（检测到 %d 张候选）" % n
            self.rec_state = (OK, base)
        elif state == "paused":
            self.rec_state = (WARN, "已暂停")
        elif state == "lost":
            self.rec_state = (ERR, "窗口丢失/最小化")
            self.game_state = (WARN, "最小化?")
        elif state == "size":
            self.lbl_frame_size.config(text="捕获画面: " + detail)
            return
        elif state == "error":
            self.rec_state = (ERR, "识别异常")
            self._log("识别异常: " + detail)
            return
        self._set_state(self.lbl_rec, self.rec_state)
        if state == "lost":
            self._set_state(self.lbl_game, self.game_state)

    def _handle_mem_status(self, state, data):
        detail = data.get("detail", "")
        if state == "mem_connecting":
            self.rec_state = (WARN, "连接游戏进程…")
            self._set_state(self.lbl_rec, self.rec_state)
        elif state == "mem_no_process":
            self.rec_state = (FG_DIM, "等待游戏启动")
            self.game_state = (FG_DIM, "未启动")
            self._set_state(self.lbl_rec, self.rec_state)
            self._set_state(self.lbl_game, self.game_state)
        elif state == "mem_attached":
            self.rec_state = (OK, "已附加·初始化")
            self.game_state = (OK, "已连接")
            self._set_state(self.lbl_rec, self.rec_state)
            self._set_state(self.lbl_game, self.game_state)
            self._log("已附加游戏进程（内存模式）")
        elif state == "mem_ready":
            # 平台以游戏进程实际加载的 DLL 为准（内存上报），
            # 读不到时退回安装目录检测
            plat = data.get("platform") or self._game_platform()
            self.rec_state = (OK, "内存模式就绪 · %s" % plat if plat else "内存模式就绪")
            self._set_state(self.lbl_rec, self.rec_state)
            self._log("内存读取就绪：对局数据自动同步%s"
                      % ("（%s 版）" % plat if plat else ""))
        elif state == "mem_state":
            if data.get("in_match"):
                mi = getattr(self, "match_info", None)
                mn = (mi or {}).get("mode_name") if mi else None
                self.game_state = (OK, "对局中·%s" % mn if mn else "对局中")
                self._set_state(self.lbl_game, self.game_state)
            else:
                self.game_state = (FG_DIM, "主菜单")
                self._set_state(self.lbl_game, self.game_state)
                self._auto_end_match()
        elif state == "mem_detached":
            self.rec_state = (WARN, "进程已断开")
            self.game_state = (WARN, "已断开")
            self._set_state(self.lbl_rec, self.rec_state)
            self._set_state(self.lbl_game, self.game_state)
            self._log("游戏进程断开，等待重连…")
        elif state == "mem_unknown":
            self._log("未知卡牌模板ID: %s（卡库需更新？）" % detail)
        elif state == "mem_attach_fail":
            self.rec_state = (WARN, "等待游戏就绪")
            self.game_state = (WARN, "连接重试中")
            self._set_state(self.lbl_rec, self.rec_state)
            self._set_state(self.lbl_game, self.game_state)

    # ---------------- 渲染 ----------------
    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._hover_iid = None    # 行被清空，悬停高亮状态一并失效
        shown = 0
        for idx, e in enumerate(self.match.entries):
            if self.filter_side is not None and e["side"] != self.filter_side:
                continue
            if (self.filter_round is not None
                    and e["round"] != self.filter_round):
                continue
            shown += 1
            tags = ["odd" if shown % 2 else "even"]
            if e["side"] == 0 and e["card"].get("gold"):
                tags.append("gold")   # 敌方金卡用金色文字区分
            img = self._tree_icons.get(e["side"], None)
            if e["side"] == 0 and e["card"].get("gold"):
                img = self._tree_icons.get("gold0", img)
            row = (shown, "第%d局" % e["round"], "我" if e["side"] else "敌",
                   e["card"]["name_zh"], e["card"]["provision"], e["count"])
            if img is None:
                # 注意：ttk.Treeview.insert 传 image=None 会抛 TclError
                # "unknown option ..."（参数错位），导致出牌表永远渲染失败——
                # 图标构建失败（如打包环境缺 PIL）时必须退化为不带 image 的插入
                self.tree.insert("", "end", iid=str(idx + 1), tags=tuple(tags),
                                 values=row)
            else:
                self.tree.insert("", "end", iid=str(idx + 1), tags=tuple(tags),
                                 image=img, values=row)
        if shown:
            self.lbl_empty.place_forget()
        else:
            self.lbl_empty.config(
                text="等待对局数据…" if not self.match.entries else "该筛选下暂无记录")
            self.lbl_empty.place(relx=0.5, rely=0.5, anchor="center")
        my_used = self.match.total_provision(side=1)
        opp_used = self.match.total_provision(side=0)
        my_lim = self._my_limit()
        opp_lim = self._opp_limit()
        self._tween_num(self.my_used, my_used)
        self.my_sub.config(text="/ %d · 剩 %d" % (my_lim, max(0, my_lim - my_used)))
        self._tween_num(self.opp_used, opp_used)
        self.opp_sub.config(text="/ %d · 剩 %d" % (opp_lim, max(0, opp_lim - opp_used)))
        # 进度条颜色按主题取色：HUD 主题下我方=北方蓝、敌方=腐血橙
        mr, mg, mb = _hex_rgb(MINE)
        self._draw_bar(self.bar_mine, my_used, my_lim,
                       _shade((mr, mg, mb), 0.55), _tint((mr, mg, mb), 0.30))
        orr, og, ob = _hex_rgb(OPP)
        self._draw_bar(self.bar, opp_used, opp_lim,
                       _shade((orr, og, ob), 0.55), _tint((orr, og, ob), 0.30))
        diff = my_used - opp_used
        if diff > 0:
            self.lbl_delta.config(text="我方多用 %d 人口" % diff, fg=MINE)
        elif diff < 0:
            self.lbl_delta.config(text="对方多用 %d 人口" % -diff, fg=ACCENT)
        else:
            self.lbl_delta.config(text="双方持平", fg=FG_DIM)
        self.lbl_round.config(text="第 %d 小局" % self.round_no if self.in_match else "未开始",
                              foreground=OK if self.in_match else FG_DIM)
        self._update_prediction()
        # 对局信息行（对手昵称/回合归属/实时比分）不归 _update_prediction 管，
        # 切主题重建控件后必须补刷一次，否则会一直显示占位的“— : —”。
        self._update_matchinfo_line()
        self._sync_pred_empty()
        self._ov_push_stats()

    def _draw_bar(self, canvas, used, limit, c0, c1):
        """渐变人口进度条（数值变化时带缓动动画）。"""
        frac = min(1.0, used / max(1, limit))
        st = self._bar_state.get(canvas)
        if st is None:
            # 首次：从 0 缓动到目标值
            st = self._bar_state[canvas] = {"cur": 0.0, "target": frac,
                                            "c": (c0, c1), "job": None}
            st["job"] = self.root.after(16, lambda c=canvas: self._bar_step(c))
            return
        st["c"] = (c0, c1)
        if abs(st["cur"] - frac) < 0.002 and st["job"] is None:
            self._paint_bar(canvas, frac, c0, c1)   # 尺寸变化等场景立即重绘
            return
        st["target"] = frac
        if st["job"] is None:
            st["job"] = self.root.after(16, lambda c=canvas: self._bar_step(c))

    def _bar_step(self, canvas):
        st = self._bar_state.get(canvas)
        if st is not None:
            st["job"] = None
        if st is None or not canvas.winfo_exists():
            return
        cur, target = st["cur"], st["target"]
        diff = target - cur
        if abs(diff) < 0.004:
            st["cur"] = target
            self._paint_bar(canvas, target, *st["c"])
            return
        st["cur"] = cur + diff * 0.22   # 缓动：每帧逼近目标 22%
        self._paint_bar(canvas, st["cur"], *st["c"])
        st["job"] = self.root.after(16, lambda c=canvas: self._bar_step(c))

    def _paint_bar(self, canvas, frac, c0, c1):
        canvas.delete("all")
        w = max(1, canvas.winfo_width())
        h = int(canvas.cget("height"))
        canvas.create_rectangle(0, 0, w, h, fill=SHADE, width=0)
        bw = int(w * frac)
        for x in range(bw):
            t = x / max(1, bw - 1) if bw > 1 else 1.0
            col = "#%02x%02x%02x" % tuple(
                int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
            canvas.create_line(x, 0, x, h, fill=col)
        if bw > 2:
            canvas.create_line(bw - 1, 0, bw - 1, h, fill="#ffffff", stipple="gray50")
        self._paint_band(canvas, frac, c0, c1)

    def _paint_band(self, canvas, frac, c0, c1):
        """进度条流动光效：一道平滑渐变高光扫过填充区（逐列插值，无抖动）。"""
        canvas.delete("band")
        if frac <= 0.002:
            return
        w = max(1, canvas.winfo_width())
        h = int(canvas.cget("height"))
        bw = int(w * frac)
        if bw < 20:
            return
        for dx, a in _flow_band(bw, self._bar_flow):
            # 底色取填充渐变在该列的颜色，再向白色提亮
            t = dx / max(1, bw - 1)
            base = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
            canvas.create_line(dx, 1, dx, h - 1,
                               fill=_mix(base, (255, 255, 255), a * 0.8),
                               tags="band")

    def _bar_flow_loop(self):
        """流动光效主循环：仅重绘各进度条的高光带，开销极低。"""
        self._bar_flow = (self._bar_flow + 0.03) % 1.0
        try:
            for canvas in list(self._bar_state):
                if canvas.winfo_exists():
                    st = self._bar_state[canvas]
                    self._paint_band(canvas, st["cur"], *st["c"])
        except tk.TclError:
            return          # 窗口销毁后停止
        self.root.after(66, self._bar_flow_loop)

    def on_filter(self, side):
        """出牌表筛选：全部 / 仅敌方 / 仅我方。"""
        self.filter_side = side
        self.btn_f_all.set_accent(side is None)
        self.btn_f_opp.set_accent(side == 0)
        self.btn_f_mine.set_accent(side == 1)
        self._render()

    def on_filter_round(self, r):
        """出牌表小局筛选：全部 / 第1局 / 第2局 / 第3局。"""
        self.filter_round = r
        self.btn_r_all.set_accent(r is None)
        self.btn_r1.set_accent(r == 1)
        self.btn_r2.set_accent(r == 2)
        self.btn_r3.set_accent(r == 3)
        self._render()

    # ---------------- 主题 ----------------
    def on_toggle_theme(self):
        """循环切换 7 款主题：猎魔 → 浅色 → 暗色 → 翡翠 → 深海 → 暮紫 → 绯红。"""
        try:
            idx = THEME_ORDER.index(self._theme)
        except ValueError:
            idx = 0
        self._set_theme(THEME_ORDER[(idx + 1) % len(THEME_ORDER)])

    def _is_dark(self):
        return bool(THEMES.get(self._theme, {}).get("dark"))

    def _set_theme(self, name):
        if name not in THEMES:
            return
        self._theme = name
        self.cfg["theme"] = name
        save_config(self.cfg)
        set_theme(name)
        for w in list(self.root.winfo_children()):
            w.destroy()
        self._bar_state = {}   # 旧画布已销毁，动画状态一并重置
        self._num_state = {}   # 数字补间标签已销毁，重置
        self._tree_icons = {}
        self._hover_iid = None
        # 先刷窗口底色再重建控件：CTkFrame 的 bg_color 取自父控件当前底色，
        # 底色若还停在旧主题，圆角外就会留一圈旧主题的边。
        self._set_root_bg(BG)
        self._style()
        self._build()
        self._set_state(self.lbl_game, self.game_state)
        self._set_state(self.lbl_rec, self.rec_state)
        self._fill_log()
        self._update_match_stats()
        self.on_filter(self.filter_side)
        self.enable_glass()
        self._log("已切换为「%s」主题" % THEME_NAMES.get(name, name))
        # 悬浮窗随主题销毁，需按新配色重建
        if self._ov is not None and self._ov.shown:
            self._ov = None
            ov = self._ov_ensure()
            if ov:
                ov.show()
                self._pred_key = None
                self._render()

    def on_clear_nosave(self):
        if messagebox.askyesno("清空", "清空当前记录（不保存历史）？"):
            self.match = MatchRecord()
            self._log("已清空当前记录")
            self._render()

    def _save_history(self):
        if not self.match.entries:
            return False
        try:
            os.makedirs(HISTORY_DIR, exist_ok=True)
            data = self.match.to_json()
            data["population_limit"] = self.match_limit
            # 双方领袖名称/阵营（对局详情展示用）
            for side in ("mine", "opp"):
                tid = str(data.get("leaders", {}).get(side) or "")
                c = self.db.by_id.get(tid) if (self.db and tid) else None
                data.setdefault("leader_info", {})[side] = {
                    "name": c["name_zh"] if c else "",
                    "faction": c.get("faction", "") if c else "",
                }
            # 我方卡组：开局内存直读（真实卡牌实例），读取失败才退回库匹配
            my_cards = dict(data.get("my_deck") or {})
            if my_cards:
                ld = str(data.get("leaders", {}).get("mine") or "")
                c = self.db.by_id.get(ld) if (self.db and ld) else None
                provs = {}
                for t in my_cards:
                    cc = self.db.by_id.get(t) if self.db else None
                    provs[t] = cc["provision"] if cc else 0
                # 金卡标记与卡图借最相近的库内卡组（仅作展示，内容以内存为准）
                best = deck_predict.predict(
                    self.decklib, dict(my_cards),
                    leader=ld if ld else None, top=1)
                b = best[0] if best else {}
                data["deck_mine"] = {
                    "name": c["name_zh"] if c else "",
                    "faction": c.get("faction", "") if c else "",
                    "cards": my_cards,
                    "provs": provs,
                    "gold": [str(t) for t in (b.get("gold") or [])
                             if str(t) in my_cards],
                    "img": dict(b.get("img") or {}),
                    "src": "memory",
                }
            else:
                data["deck_mine"] = self._predict_side_deck(data, 1)
            # 对方卡组：按已打出的牌匹配卡组库
            data["deck_opp"] = self._predict_side_deck(data, 0)
            path = os.path.join(HISTORY_DIR, "match_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except OSError:
            return False

    def _predict_side_deck(self, data, side):
        """按某方已打出的牌 + 卡组库匹配最符合卡组（历史保存用，含完整卡牌）。"""
        played = {}
        for e in self.match.entries:
            if e["side"] == side:
                tid = str(e["card"]["id"])
                played[tid] = played.get(tid, 0) + e["count"]
        if not played:
            return None
        ld = data.get("leaders", {}).get("mine" if side == 1 else "opp")
        res = deck_predict.predict(
            self.decklib, played,
            leader=str(ld) if ld else None, top=1)
        if not res:
            return None
        r = res[0]
        return {
            "name": self._pred_deck_name(r),
            "faction": r.get("faction", ""),
            "conf": round(r.get("conf", 0), 2),
            "matched": r.get("matched", 0),
            "possible": r.get("possible", 0),
            "cards": dict(r.get("cards") or {}),
            "provs": dict(r.get("provs") or {}),
            "gold": [str(t) for t in (r.get("gold") or [])],
            "img": dict(r.get("img") or {}),
            "src": "predict",
        }

    # ---------------- 对局详情 ----------------
    @staticmethod
    def _fmt_duration(sec):
        """秒数 → 「X分XX秒」/「X小时XX分」。"""
        if not sec or sec <= 0:
            return ""
        sec = int(sec)
        if sec >= 3600:
            return "%d小时%02d分" % (sec // 3600, (sec % 3600) // 60)
        return "%d分%02d秒" % (sec // 60, sec % 60)

    def _load_history_matches(self):
        """读取 history/*.json（新→旧），旧文件缺新字段时自动兼容推断。"""
        out = []
        try:
            names = [n for n in os.listdir(HISTORY_DIR)
                     if n.startswith("match_") and n.endswith(".json")]
        except OSError:
            return out
        for n in sorted(names, reverse=True):
            try:
                with open(os.path.join(HISTORY_DIR, n), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            m = {
                "file": n,
                "date": d.get("date", ""),
                "duration": d.get("duration"),
                "winner": d.get("winner"),
                "rounds": d.get("rounds") or [],
                "leaders": d.get("leaders") or {},
                "leader_prov": d.get("leader_prov") or {},
                "leader_info": d.get("leader_info") or {},
                "deck_mine": d.get("deck_mine") or {},
                "deck_opp": d.get("deck_opp") or {},
                "cards": d.get("cards") or [],
            }
            # 旧文件没有 rounds：按出牌记录汇总各小局人口
            if not m["rounds"]:
                agg = {}
                for c in m["cards"]:
                    r, s = c.get("round"), c.get("side")
                    if r is None or s not in ("mine", "opponent"):
                        continue
                    a = agg.setdefault(r, {"mine": 0, "opponent": 0})
                    a[s] += c.get("provision", 0) * c.get("count", 1)
                m["rounds"] = [{"round": r, "mine": v["mine"], "opp": v["opponent"]}
                               for r, v in sorted(agg.items())]
            # 旧文件没有 winner：按小局胜场推断
            if m["winner"] is None and m["rounds"]:
                mw = ow = 0
                for r in m["rounds"]:
                    if r["mine"] > r["opp"]:
                        mw += 1
                    elif r["opp"] > r["mine"]:
                        ow += 1
                m["winner"] = "mine" if mw > ow else (
                    "opp" if ow > mw else "draw")
            out.append(m)
        return out

    @staticmethod
    def _rounds_won(m):
        """小局胜场 (我方, 对方)。"""
        mw = ow = 0
        for r in m.get("rounds", []):
            if r["mine"] > r["opp"]:
                mw += 1
            elif r["opp"] > r["mine"]:
                ow += 1
        return mw, ow

    def _match_side_label(self, m, side):
        """「阵营 · 领袖名（+N人口）」，旧文件缺 leader_info 时从卡库补查。"""
        info = (m.get("leader_info") or {}).get(side) or {}
        name, fac = info.get("name", ""), info.get("faction", "")
        tid = str((m.get("leaders") or {}).get(side) or "")
        if (not name or not fac) and self.db and tid:
            c = self.db.by_id.get(tid)
            if c:
                name = name or c.get("name_zh", "")
                fac = fac or c.get("faction", "")
        if not name and tid:
            name = "领袖#" + tid
        parts = ([fac] if fac else []) + ([name] if name else [])
        label = " · ".join(parts) if parts else "未知"
        prov = (m.get("leader_prov") or {}).get(side) or 0
        if prov:
            label += "（+%d人口）" % prov
        return label

    def _predict_deck_from_cards(self, m, side_json):
        """旧历史文件缺 deck_mine/deck_opp：按出牌记录现算卡组匹配。"""
        played = {}
        for c in m.get("cards", []):
            if c.get("side") != side_json:
                continue
            tid = str(c.get("id"))
            played[tid] = played.get(tid, 0) + c.get("count", 1)
        if not played:
            return None
        ld = (m.get("leaders") or {}).get(
            "mine" if side_json == "mine" else "opp")
        res = deck_predict.predict(
            self.decklib, played, leader=str(ld) if ld else None, top=1)
        if not res:
            return None
        r = res[0]
        return {"name": self._pred_deck_name(r), "faction": r.get("faction", ""),
                "conf": r.get("conf", 0), "matched": r.get("matched", 0),
                "possible": r.get("possible", 0)}

    def _match_detail_text(self, m):
        """选中一场对局后的详情文本。"""
        res = {"mine": "胜利", "opp": "失败", "draw": "平局"}.get(
            m.get("winner"), "未知")
        lines = [
            "开局时间：%s    总用时：%s    结果：%s" % (
                m.get("date") or "未知",
                self._fmt_duration(m.get("duration")) or "未知", res),
            "我方：%s" % self._match_side_label(m, "mine"),
            "对方：%s" % self._match_side_label(m, "opp"),
        ]
        if m["rounds"]:
            lines.append("小局点数：" + "    ".join(
                "第%d局 %d : %d" % (r["round"], r["mine"], r["opp"])
                for r in m["rounds"]))
        else:
            lines.append("小局点数：无记录")
        for label, key, side_json in (("我方", "deck_mine", "mine"),
                                      ("对方", "deck_opp", "opponent")):
            d = m.get(key) or self._predict_deck_from_cards(m, side_json)
            if d and d.get("cards"):
                src = "内存读取" if d.get("src") == "memory" else "卡组库匹配"
                title = " · ".join(p for p in (d.get("name") or "",
                                               d.get("faction") or "") if p)
                lines.append("%s卡组：%s（共 %d 张 · %s）" % (
                    label, title or "未知", sum(d["cards"].values()), src))
            elif d:
                lines.append("%s卡组：%s（%s · 匹配 %d/%d · 置信 %.0f%%）" % (
                    label, d.get("name") or "?", d.get("faction") or "?",
                    d.get("matched", 0), d.get("possible", 0),
                    (d.get("conf") or 0) * 100))
            else:
                lines.append("%s卡组：未能匹配（出牌记录不足或卡组库未覆盖）" % label)
        return "\n".join(lines)

    def _update_match_stats(self):
        """刷新「本次运行 N 场 · 总体胜率」统计标签。"""
        total = win = draw = 0
        for m in self._load_history_matches():
            w = m.get("winner")
            if w is None:
                continue
            total += 1
            if w == "mine":
                win += 1
            elif w == "draw":
                draw += 1
        decisive = total - draw
        if total:
            txt = "本次运行 %d 场 · 总体 %d 场 胜率 %.0f%%（胜%d/负%d/平%d）" % (
                self._session_matches, total,
                win * 100.0 / decisive if decisive else 0.0,
                win, total - win - draw, draw)
        else:
            txt = "本次运行 %d 场" % self._session_matches
        try:
            self.lbl_match_stats.config(text=txt)
        except tk.TclError:
            pass

    def _mark_match_result(self, m, winner):
        """手动标记对局结果：把 winner 写回 history json 文件。"""
        path = os.path.join(HISTORY_DIR, m.get("file") or "")
        if not m.get("file") or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            d["winner"] = winner
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            self._log("已标记对局结果：%s（%s）" % (
                {"mine": "胜", "opp": "负", "draw": "平"}[winner],
                m.get("date") or "未知时间"))
        except (OSError, ValueError):
            self._log("标记结果失败（文件读写错误）")

    def _match_stats_data(self):
        """汇总 history 全部对局为统计页数据（天梯胜率统计用）。"""
        ms = self._load_history_matches()
        total = win = loss = draw = 0
        dur = dur_n = 0
        my, vs = {}, {}          # 阵营 → [总, 胜, 负, 平]
        ordered = []
        for m in ms:
            w = m.get("winner")
            if w not in ("mine", "opp", "draw"):
                continue
            total += 1
            if w == "mine":
                win += 1
            elif w == "opp":
                loss += 1
            else:
                draw += 1
            d = m.get("duration")
            if d and d > 0:
                dur += int(d)
                dur_n += 1
            fi = (((m.get("leader_info") or {}).get("mine") or {})
                  .get("faction")) or "未知"
            fo = (((m.get("leader_info") or {}).get("opp") or {})
                  .get("faction")) or "未知"
            a = my.setdefault(fi, [0, 0, 0, 0])
            b = vs.setdefault(fo, [0, 0, 0, 0])
            a[0] += 1
            b[0] += 1
            if w == "mine":
                a[1] += 1
                b[1] += 1
            elif w == "opp":
                a[2] += 1
                b[2] += 1
            else:
                a[3] += 1
                b[3] += 1
            ordered.append(w)
        ordered.reverse()        # 旧 → 新，用于连胜/连负
        # 连胜/连负：平局中断计数
        max_w = max_l = 0
        cur_kind, cur_n = None, 0
        for w in ordered:
            if w == "draw":
                cur_kind, cur_n = None, 0
                continue
            cur_kind, cur_n = w, (cur_n + 1 if cur_kind == w else 1)
            if w == "mine":
                max_w = max(max_w, cur_n)
            else:
                max_l = max(max_l, cur_n)
        return {
            "total": total, "win": win, "loss": loss, "draw": draw,
            "avg_dur": dur // dur_n if dur_n else None,
            "my": my, "vs": vs,
            "max_w": max_w, "max_l": max_l,
            "cur_kind": cur_kind, "cur_n": cur_n,
            "trend": ordered[-20:],  # 最近 20 场（旧 → 新）
        }

    def on_match_stats(self):
        """天梯胜率统计窗口：总体胜率 / 连胜连负 / 阵营分布 / 近期趋势。"""
        s = self._match_stats_data()
        win = self._glass_toplevel("天梯胜率统计", "712x640")
        # 底部按钮行先 pack 永远可见；内容区可滚动（阵营多时不再被截断）
        bg = BG2 if not self._glass_ok else BG
        acts = tk.Frame(win, bg=bg)
        acts.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        frm = self._scroll_frame(win, bg)
        inner = tk.Frame(frm, bg=bg)
        inner.pack(fill="both", expand=True, padx=14, pady=(10, 12))
        frm = inner
        bg = frm.cget("bg")

        def section(title):
            head = tk.Frame(frm, bg=bg)
            head.pack(fill="x", pady=(12, 2))
            tk.Frame(head, width=3, height=14, bg=ACCENT).pack(side="left")
            tk.Label(head, text=title, bg=bg, fg=ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold")
                     ).pack(side="left", padx=(6, 0))
            body = tk.Frame(frm, bg=bg)
            body.pack(fill="x", padx=(9, 0), pady=(2, 0))
            return body

        # —— 总览 ——
        g = section("总览")
        dec = s["win"] + s["loss"]
        rate = s["win"] * 100.0 / dec if dec else 0.0
        cur_txt = ("当前 %s%d" % ("连胜 " if s["cur_kind"] == "mine"
                                  else "连负 ", s["cur_n"])) \
            if s["cur_kind"] else "当前无连胜/连负"
        head = "共 %d 场    胜 %d / 负 %d / 平 %d    总体胜率 %.0f%%" % (
            s["total"], s["win"], s["loss"], s["draw"], rate)
        tk.Label(g, text=head, bg=bg, fg=GOLD_FG,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        sub = "最长连胜 %d · 最长连负 %d · %s" % (s["max_w"], s["max_l"], cur_txt)
        if s["avg_dur"]:
            sub += "    平均用时 %s" % self._fmt_duration(s["avg_dur"])
        tk.Label(g, text=sub, bg=bg, fg=FG_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")

        # —— 阵营胜率：左右两栏并排（行多时一屏可见，不再需要滚动翻找） ——
        g2 = tk.Frame(frm, bg=bg)
        g2.pack(fill="x", padx=(9, 0))
        left = tk.Frame(g2, bg=bg)
        left.pack(side="left", anchor="n")
        right = tk.Frame(g2, bg=bg)
        right.pack(side="left", anchor="n", padx=(22, 0))

        def col_head(parent, title):
            head = tk.Frame(parent, bg=bg)
            head.pack(fill="x", pady=(12, 2))
            tk.Frame(head, width=3, height=14, bg=ACCENT).pack(side="left")
            tk.Label(head, text=title, bg=bg, fg=ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold")
                     ).pack(side="left", padx=(6, 0))

        def fac_table_col(parent, title, subtitle, data):
            col_head(parent, title)
            body = tk.Frame(parent, bg=bg)
            body.pack(fill="x", padx=(9, 0), pady=(2, 0))
            tk.Label(body, text=subtitle, bg=bg, fg=FG_DIM,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w",
                                                          pady=(0, 2))
            cols = ("fac", "n", "w", "l", "d", "rate")
            # 固定展示六大阵营（未对阵过的显示 0 场 / —），
            # 其余（未知等）按场次追加在末尾
            order6 = ("怪兽", "尼弗迦德", "北方领域", "松鼠党",
                      "史凯利格", "辛迪加")
            rows = [(k, data.get(k, [0, 0, 0, 0])) for k in order6]
            rows += sorted(((k, v) for k, v in data.items()
                            if k not in order6), key=lambda kv: -kv[1][0])
            tree = ttk.Treeview(body, columns=cols, show="headings",
                                height=max(6, min(8, len(rows))))
            for cid, text, w_, anchor in (
                    ("fac", "阵营", 96, "w"), ("n", "场次", 44, "center"),
                    ("w", "胜", 36, "center"), ("l", "负", 36, "center"),
                    ("d", "平", 36, "center"), ("rate", "胜率", 56, "center")):
                tree.heading(cid, text=text)
                tree.column(cid, width=w_, anchor=anchor, stretch=False)
            for name, (n, w_, l_, d_) in rows:
                dec_ = w_ + l_
                tree.insert("", "end", values=(
                    name, n, w_, l_, d_,
                    "%.0f%%" % (w_ * 100.0 / dec_) if dec_ else "—"))
            tree.pack(fill="x")

        fac_table_col(left, "我方阵营胜率", "按我方领袖阵营汇总", s["my"])
        fac_table_col(right, "对阵各阵营", "按对方领袖阵营汇总", s["vs"])

        # —— 近期趋势（图表：累计胜率折线 + 每场胜负圆点） ——
        t = section("近期趋势")
        tk.Label(t, text=("最近 %d 场（左旧 → 右新）· 金色折线 = 累计胜率，"
                          "圆点颜色 = 单场结果" % len(s["trend"]))
                 if s["trend"] else "暂无对局数据",
                 bg=bg, fg=FG_DIM, font=("Microsoft YaHei UI", 9)
                 ).pack(anchor="w")
        cv = tk.Canvas(t, bg=BG3, highlightthickness=0, bd=0,
                       height=int(150 * UI_SCALE))
        cv.pack(fill="x")

        def _draw_trend(_e=None):
            cv.delete("all")
            W = cv.winfo_width()
            H = int(150 * UI_SCALE)
            if W < 60:
                return
            ml, mr = int(38 * UI_SCALE), int(14 * UI_SCALE)
            mt, mb = int(12 * UI_SCALE), int(24 * UI_SCALE)
            x0, x1 = ml, W - mr
            y0, y1 = mt, H - mb           # y0 = 100%（顶），y1 = 0%（底）

            def yr(p):
                return y1 - (y1 - y0) * p / 100.0

            # 网格：0 / 50 / 100%
            for p, lbl in ((0, "0%"), (50, "50%"), (100, "100%")):
                yy = yr(p)
                cv.create_line(x0, yy, x1, yy, fill=BORDER,
                               dash=(4, 4) if p == 50 else ())
                cv.create_text(x0 - 7, yy, text=lbl, anchor="e",
                               fill=FG_DIM, font=("Microsoft YaHei UI", 8))
            seq = s["trend"]
            n = len(seq)
            if not n:
                cv.create_text((x0 + x1) // 2, (y0 + y1) // 2,
                               text="暂无对局数据", fill=FG_DIM,
                               font=("Microsoft YaHei UI", 9))
                return
            step = (x1 - x0) / (n - 1) if n > 1 else 0
            pts, cw = [], 0.0
            for i, w in enumerate(seq):
                if w == "mine":
                    cw += 1
                elif w == "draw":
                    cw += 0.5          # 平局按半胜计入累计胜率
                pts.append((x0 + i * step, yr(cw * 100.0 / (i + 1))))
            # 累计胜率折线（金色平滑）
            if len(pts) > 1:
                cv.create_line(*[c for p_ in pts for c in p_],
                               fill=ACCENT, width=2, smooth=True)
            # 每场结果圆点（覆盖在折线上，颜色区分胜负平）
            col = {"mine": OK, "opp": ERR, "draw": WARN}
            r = int(3 * UI_SCALE)
            for i, w in enumerate(seq):
                xx, yy = pts[i]
                cv.create_oval(xx - r, yy - r, xx + r, yy + r,
                               fill=col[w], outline="")
            # 起止场次标注
            cv.create_text(x0, y1 + 12, text="1", fill=FG_DIM,
                           font=("Microsoft YaHei UI", 8))
            cv.create_text(x1, y1 + 12, text=str(n), fill=FG_DIM,
                           font=("Microsoft YaHei UI", 8))

        cv.bind("<Configure>", _draw_trend)

        self._btn(acts, "刷新", lambda: (win.destroy(),
                                         self.root.after(30,
                                                         self.on_match_stats))
                  ).pack(side="left")
        self._btn(acts, "关闭", win.destroy).pack(side="right")
        win.geometry("+%d+%d" % (self.root.winfo_x() + 80,
                                 self.root.winfo_y() + 40))

    def on_match_details(self):
        """对局详情窗口：历史对局列表 + 选中详情。"""
        matches = self._load_history_matches()
        win = self._glass_toplevel("对局详情", "760x560")
        frm = tk.Frame(win, bg=BG2 if not self._glass_ok else BG)
        frm.pack(fill="both", expand=True, padx=12, pady=(10, 12))

        # 顶部汇总
        total = len(matches)
        nw = sum(1 for m in matches if m.get("winner") == "mine")
        nl = sum(1 for m in matches if m.get("winner") == "opp")
        nd = sum(1 for m in matches if m.get("winner") == "draw")
        dec = nw + nl
        head = "共 %d 场对局    胜 %d / 负 %d / 平 %d    总体胜率 %.0f%%" % (
            total, nw, nl, nd, nw * 100.0 / dec if dec else 0.0)
        tk.Label(frm, text=head, bg=frm.cget("bg"), fg=GOLD_FG,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")

        cols = ("date", "mine", "opp", "score", "result", "dur")
        tree = ttk.Treeview(frm, columns=cols, show="headings", height=10,
                            selectmode="extended")   # 允许 Ctrl/Shift 多选
        for cid, text, w_, anchor in (
                ("date", "开局时间", 130, "w"),
                ("mine", "我方阵营·领袖", 165, "w"),
                ("opp", "对方阵营·领袖", 165, "w"),
                ("score", "比分", 60, "center"),
                ("result", "结果", 50, "center"),
                ("dur", "总用时", 80, "center")):
            tree.heading(cid, text=text)
            tree.column(cid, width=w_, anchor=anchor)

        res_fg = {"mine": OK, "opp": ERR, "draw": WARN}
        for i, m in enumerate(matches):
            mw, ow = self._rounds_won(m)
            res = {"mine": "胜", "opp": "负", "draw": "平"}.get(
                m.get("winner"), "—")
            date = m.get("date", "")
            if len(date) >= 16:      # 列表里显示到分钟即可
                date = date[5:16]
            tree.insert("", "end", iid=str(i), values=(
                date or "未知",
                self._match_side_label(m, "mine"),
                self._match_side_label(m, "opp"),
                "%d-%d" % (mw, ow) if (mw or ow) else "—",
                res, self._fmt_duration(m.get("duration")) or "—"))
        if not matches:
            tree.insert("", "end", iid="empty", values=(
                "暂无历史对局（完成一局对局后自动保存）", "", "", "", "", ""))

        # 详情文本
        det = tk.Text(frm, bg=BG3 if not self._glass_ok else BG, fg=FG,
                      relief="flat", font=("Microsoft YaHei UI", 9),
                      wrap="word", highlightthickness=0, height=8, padx=10, pady=8)
        cur = {"m": None}        # 当前选中对局

        def on_select(_ev=None):
            sel = [s for s in tree.selection() if s.isdigit()]
            det.configure(state="normal")
            det.delete("1.0", "end")
            cur["m"] = None
            if sel:
                m = matches[int(sel[0])]
                cur["m"] = m
                det.insert("end", self._match_detail_text(m))
                # 结果行着色
                w = m.get("winner")
                if w in res_fg:
                    det.tag_add("res", "1.0", "1.end")
                    det.tag_config("res", foreground=res_fg[w])
            det.configure(state="disabled")

        def delete_selected():
            sel = [s for s in tree.selection() if s.isdigit()]
            if not sel:
                messagebox.showinfo(
                    "删除对局",
                    "请先在列表中选中对局记录（可按住 Ctrl 或 Shift 多选）",
                    parent=win)
                return
            n = len(sel)
            if n > 1:
                msg = "确定删除选中的 %d 场对局记录？\n\n删除后无法恢复" % n
            else:
                msg = "确定删除该对局记录？\n\n%s\n删除后无法恢复" % (
                    matches[int(sel[0])].get("date") or "未知时间")
            if not messagebox.askyesno("删除对局", msg, parent=win):
                return
            removed = 0
            for s in sel:
                try:
                    os.remove(os.path.join(HISTORY_DIR,
                                           matches[int(s)]["file"]))
                    removed += 1
                except OSError:
                    pass
            self._log("已删除 %d 场对局记录" % removed)
            self._update_match_stats()
            win.destroy()
            self.root.after(30, self.on_match_details)   # 重新打开以刷新列表

        def mark_result(w):
            if cur["m"] is None:
                messagebox.showinfo("标记结果", "请先在列表中选中一场对局",
                                    parent=win)
                return
            self._mark_match_result(cur["m"], w)
            self._update_match_stats()
            win.destroy()
            self.root.after(30, self.on_match_details)   # 重开刷新列表

        tree.bind("<<TreeviewSelect>>", on_select)

        # 操作行
        acts = tk.Frame(frm, bg=frm.cget("bg"))
        self._btn(acts, "查看卡组明细",
                  lambda: self.on_match_deck_view(cur["m"], parent=win)
                  ).pack(side="left")
        self._btn(acts, "删除选中对局", delete_selected).pack(side="left", padx=(8, 0))
        self._btn(acts, "胜率统计", self.on_match_stats).pack(side="left", padx=(8, 0))
        # 手动标记结果（自动判定不准/误判时可覆盖）
        self._btn(acts, "标为胜", lambda: mark_result("mine")).pack(
            side="left", padx=(8, 0))
        self._btn(acts, "标为负", lambda: mark_result("opp")).pack(side="left")
        self._btn(acts, "标为平", lambda: mark_result("draw")).pack(side="left")
        self._btn(acts, "关闭", win.destroy).pack(side="right")

        tree.pack(fill="both", expand=True, pady=(8, 0))
        det.pack(fill="x", pady=(8, 0))
        acts.pack(fill="x", pady=(8, 0))
        if matches and matches[0].get("date"):
            tree.selection_set("0")   # 默认选中最新一场
            tree.see("0")
        win.geometry("+%d+%d" % (self.root.winfo_x() + 60, self.root.winfo_y() + 40))

    def _match_deck_full(self, m, side_json):
        """匹配对局某一方完整卡组（返回 predict 原始结果，含全部卡牌）。"""
        played = {}
        for c in m.get("cards", []):
            if c.get("side") != side_json:
                continue
            tid = str(c.get("id"))
            played[tid] = played.get(tid, 0) + c.get("count", 1)
        if not played:
            return None
        ld = (m.get("leaders") or {}).get(
            "mine" if side_json == "mine" else "opp")
        res = deck_predict.predict(
            self.decklib, played, leader=str(ld) if ld else None, top=1)
        return res[0] if res else None

    def _deck_rows_title(self, m, side_json):
        """卡组明细列表标题。"""
        d = m.get("deck_mine" if side_json == "mine" else "deck_opp") or {}
        if d.get("cards"):
            total = sum(d["cards"].values())
            if d.get("src") == "memory":
                fac = " · %s" % d["faction"] if d.get("faction") else ""
                return "内存读取 · 共 %d 张%s" % (total, fac)
            conf = d.get("conf")
            return "%s · 共 %d 张%s" % (
                d.get("name") or "卡组", total,
                " · 置信 %d%%" % int(conf * 100) if conf else "")
        return ""

    def _deck_display_rows(self, m, side_json):
        """卡组明细行数据：优先已保存卡组（内存直读/库匹配），旧文件退回
        出牌记录现算匹配。返回 _build_rows 行列表。"""
        d = m.get("deck_mine" if side_json == "mine" else "deck_opp") or {}
        r = None
        if d.get("cards"):
            provs = dict(d.get("provs") or {})
            for tid in d["cards"]:
                if not provs.get(tid):
                    c = self.db.by_id.get(tid) if self.db else None
                    provs[tid] = c["provision"] if c else 0
            r = {"cards": d["cards"], "provs": provs,
                 "gold": d.get("gold") or [],
                 "faction": d.get("faction") or "",
                 "leader_name": d.get("name") or ""}
        else:
            r = self._match_deck_full(m, side_json)
        if not r:
            return []
        played = {}
        for c in m.get("cards", []):
            if c.get("side") == side_json:
                tid = str(c.get("id"))
                played[tid] = played.get(tid, 0) + c.get("count", 1)
        # 卡图 URL：优先结果自带；内存直读的我方卡组用库中最相近卡组的图
        for tid, u in (r.get("img") or {}).items():
            self._artpool.set_url(tid, u)
        if not r.get("img"):
            ld = (m.get("leaders") or {}).get(
                "mine" if side_json == "mine" else "opp")
            best = deck_predict.predict(
                self.decklib, played,
                leader=str(ld) if ld else None, top=1)
            for tid, u in ((best[0].get("img") or {}) if best else {}).items():
                self._artpool.set_url(tid, u)
        return PredictionOverlay._build_rows(r, played, self.db)

    def on_match_deck_view(self, m, parent=None):
        """卡组明细窗口：与悬浮窗相同的卡图行，左右并排展示双方卡组。"""
        if not m:
            messagebox.showinfo("卡组明细", "请先在对局列表中选中一场对局",
                                parent=parent or self.root)
            return
        dw = self._glass_toplevel("卡组明细", "620x660")
        frm = tk.Frame(dw, bg=BG2 if not self._glass_ok else BG)
        frm.pack(fill="both", expand=True, padx=12, pady=(10, 12))
        res = {"mine": "胜利", "opp": "失败", "draw": "平局"}.get(
            m.get("winner"), "未知")
        tk.Label(frm, text="对局 %s · 结果：%s" % (
            m.get("date") or "未知", res), bg=frm.cget("bg"), fg=GOLD_FG,
            font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        body = tk.Frame(frm, bg=frm.cget("bg"))
        body.pack(fill="both", expand=True, pady=(8, 0))
        body.columnconfigure(0, weight=1, uniform="d")
        body.columnconfigure(1, weight=1, uniform="d")
        body.rowconfigure(0, weight=1)
        for col, label, side_json in ((0, "我方卡组", "mine"),
                                      (1, "对方卡组", "opponent")):
            pane = tk.Frame(body, bg=body.cget("bg"))
            pane.grid(row=0, column=col, sticky="nsew",
                      padx=(0, 8) if col == 0 else (8, 0))
            tk.Label(pane, text=label, bg=pane.cget("bg"), fg=ACCENT,
                     font=("Microsoft YaHei UI", 9, "bold"),
                     anchor="w").pack(fill="x")
            rows_w = _DeckRows(self, pane)
            rows_w.pack(fill="both", expand=True, pady=(4, 0))
            rows_w.set_content(self._deck_rows_title(m, side_json),
                               self._deck_display_rows(m, side_json))
            self._btn(pane, "录入本地卡组库",
                      lambda s=side_json: self._deck_edit_from_match(m, s,
                                                                     parent=dw)
                      ).pack(anchor="w", pady=(6, 0))
        dw.geometry("+%d+%d" % (self.root.winfo_x() + 40, self.root.winfo_y() + 30))

    # ---------------- 本地卡组库（用户录入） ----------------
    def _match_deck_record(self, m, side_json):
        """对局某方完整卡组的可编辑数据（cards/provs/gold/faction/名称）。

        优先用历史保存的卡组（内存直读/库匹配），旧文件退回现算匹配。
        """
        d = m.get("deck_mine" if side_json == "mine" else "deck_opp") or {}
        r = None
        if d.get("cards"):
            provs = dict(d.get("provs") or {})
            for tid in d["cards"]:
                if not provs.get(tid):
                    c = self.db.by_id.get(tid) if self.db else None
                    provs[tid] = c["provision"] if c else 0
            r = {"cards": dict(d["cards"]), "provs": provs,
                 "gold": [g for g in (d.get("gold") or []) if g in d["cards"]],
                 "faction": d.get("faction") or "",
                 "leader_name": d.get("name") or "",
                 "img": dict(d.get("img") or {})}
        else:
            r = self._match_deck_full(m, side_json)
        if not r:
            return None
        r = dict(r)
        r["cards"] = dict(r.get("cards") or {})
        r["provs"] = dict(r.get("provs") or {})
        r["gold"] = [str(g) for g in (r.get("gold") or []) if str(g) in r["cards"]]
        r["img"] = dict(r.get("img") or {})
        for tid in r["cards"]:
            if not r["provs"].get(tid):
                c = self.db.by_id.get(tid) if self.db else None
                r["provs"][tid] = c["provision"] if c else 0
        return r

    def _deck_edit_from_match(self, m, side_json, parent=None):
        """卡组明细 → 录入本地卡组库（可先编辑卡牌）。"""
        if not self.db:
            messagebox.showinfo("录入卡组", "卡牌库未加载完成，请稍后再试",
                                parent=parent or self.root)
            return
        r = self._match_deck_record(m, side_json)
        if not r:
            messagebox.showinfo("录入卡组",
                                "该对局未保存足够卡牌信息，无法录入",
                                parent=parent or self.root)
            return
        side_key = "mine" if side_json == "mine" else "opp"
        ld = str((m.get("leaders") or {}).get(side_key) or "")
        info = (m.get("leader_info") or {}).get(side_key) or {}
        lname = info.get("name") or r.get("leader_name") or ""
        self._deck_edit_dialog(r, ld, lname, r.get("faction") or "",
                               parent=parent)

    def _next_local_id(self):
        mx = 0
        for d in self.local_decks:
            did = str(d.get("id") or "")
            if did.startswith("L") and did[1:].isdigit():
                mx = max(mx, int(did[1:]))
        return "L%04d" % (mx + 1)

    def _cached_server_decks(self):
        d, _ = deck_predict.load_cache(
            os.path.join(BASE_DIR, "decks_cache.json"))
        return d

    def _remerge_decklib(self):
        """本地录入库变更后重并预测库（本地 > 服务端缓存 > 内置）。"""
        self.decklib = deck_predict.merge_libraries(
            self.local_decks,
            deck_predict.merge_libraries(self._cached_server_decks(),
                                         self._builtin_decks))
        self._pred_key = None

    def _save_local_deck(self, record):
        self.local_decks = [d for d in self.local_decks
                            if d.get("id") != record["id"]]
        self.local_decks.append(record)
        save_local_decks(self.local_decks)
        self._remerge_decklib()

    def _deck_edit_dialog(self, rec, leader_tid, leader_name, faction,
                          parent=None, title="录入本地卡组库"):
        """卡组编辑器：浏览/增删/调整卡牌后存入本地卡组库。

        rec: {cards: {tid: n}, provs: {tid: p}, gold: [tid], img: {tid: url}}
        保存后立即并入预测库，后续对局即可用该卡组参与预测。
        """
        cards = dict(rec.get("cards") or {})
        provs = dict(rec.get("provs") or {})
        gold = [str(g) for g in (rec.get("gold") or []) if str(g) in cards]
        img = dict(rec.get("img") or {})
        for tid in cards:
            if not provs.get(tid):
                c = self.db.by_id.get(tid) if self.db else None
                provs[tid] = c["provision"] if c else 0

        win = self._glass_toplevel(title, "600x720")
        win.grab_set()
        bg = BG2 if not self._glass_ok else BG
        frm = tk.Frame(win, bg=bg)
        frm.pack(fill="both", expand=True, padx=14, pady=(10, 10))

        fcode = fac_code(faction)
        fac_zh = FAC_ZH.get(fcode, faction or "")
        ld_disp = " · ".join(x for x in (fac_zh, leader_name or "未知领袖") if x)
        head = tk.Frame(frm, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text=ld_disp, bg=bg, fg=ACCENT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        name_var = tk.StringVar(value=leader_name or "")
        self._entry(head, textvariable=name_var, width=150,
                    placeholder="卡组名").pack(side="right")

        stat = tk.Label(frm, text="", bg=bg, fg=FG_DIM,
                        font=("Microsoft YaHei UI", 9))
        stat.pack(anchor="w", pady=(6, 0))

        # —— 底部操作区先按 side="bottom" 自下而上 pack（按钮区最底、其上
        # 搜索结果列表、再上搜索框）；卡牌列表 body 用 expand 占剩余空间，
        # 若后 pack 会把三者挤出窗口可视区（小屏上按钮/搜索框消失）——
        btns = tk.Frame(frm, bg=bg)
        btns.pack(side="bottom", fill="x", pady=(8, 0))
        lst = tk.Listbox(frm, height=4, bg=BG3, fg=FG, relief="flat",
                         highlightthickness=0, selectbackground=ROW_SEL,
                         selectforeground=FG, font=("Microsoft YaHei UI", 9))
        lst.pack(side="bottom", fill="x", pady=(4, 0))
        addf = tk.Frame(frm, bg=bg)
        addf.pack(side="bottom", fill="x", pady=(8, 0))

        def totals():
            return (sum(cards.values()),
                    sum(provs.get(t, 0) * n for t, n in cards.items()))

        def refresh_stat():
            n_cards, total = totals()
            stat.config(text="共 %d 张卡 · 人口 %d / %d" % (
                n_cards, total, self.match_limit),
                fg=ERR if total > self.match_limit else FG_DIM)

        body = (ctk.CTkScrollableFrame(frm, fg_color=BG2, corner_radius=10,
                                       bg_color=bg) if ctk is not None
                else tk.Frame(frm, bg=BG2))
        body.pack(fill="both", expand=True, pady=(6, 0))

        def rebuild_rows():
            for w in body.winfo_children():
                w.destroy()
            for tid in sorted(cards, key=lambda t: (-provs.get(t, 0), t)):
                c = self.db.by_id.get(tid) if self.db else None
                name = c["name_zh"] if c else ("卡#" + tid)
                is_gold = tid in gold
                roww = tk.Frame(body, bg=BG2)
                roww.pack(fill="x", pady=1)
                tk.Label(roww, text=name, width=20, anchor="w", bg=BG2,
                         fg=GOLD_FG if is_gold else FG,
                         font=("Microsoft YaHei UI", 9,
                               "bold" if is_gold else "normal")
                         ).pack(side="left")
                tk.Label(roww, text="人口%d" % provs.get(tid, 0), width=6,
                         anchor="e", bg=BG2, fg=FG_DIM,
                         font=("Microsoft YaHei UI", 8)
                         ).pack(side="left", padx=(4, 0))

                def mk_delta(d_, tid=tid):
                    def cb():
                        cards[tid] = max(1, cards[tid] + d_)
                        rebuild_rows()
                    return cb

                self._btn(roww, "移除",
                          lambda tid=tid: (cards.pop(tid, None),
                                           gold.remove(tid) if tid in gold
                                           else None,
                                           rebuild_rows())
                          ).pack(side="right")
                self._btn(roww, "−", mk_delta(-1)).pack(side="right")
                tk.Label(roww, text="x%d" % cards[tid], width=4, bg=BG2,
                         fg=FG, font=("Microsoft YaHei UI", 9, "bold")
                         ).pack(side="right")
                self._btn(roww, "+", mk_delta(1)).pack(side="right")
            refresh_stat()
        rebuild_rows()

        # —— 添加卡牌：模糊搜索 + 双击录入 ——
        var_q = tk.StringVar()
        self._entry(addf, textvariable=var_q, width=220,
                    placeholder="输入卡名添加卡牌…").pack(side="left")
        results = {"items": [], "last": None}

        def do_search():
            kw = var_q.get().strip()
            if kw == results["last"]:
                return
            results["last"] = kw
            lst.delete(0, "end")
            results["items"] = (self.db.search_fuzzy(kw, limit=12)
                                if (self.db and kw) else [])
            for c in results["items"]:
                lst.insert("end", "%s（%s · 人口%d）" % (
                    c["name_zh"], FAC_ZH.get(c.get("faction", ""),
                                             c.get("faction", "")),
                    c["provision"]))

        def add_selected(_e=None):
            sel = lst.curselection()
            if not sel:
                return
            c = results["items"][sel[0]]
            tid = str(c["id"])
            cards[tid] = cards.get(tid, 0) + 1
            # 卡库无稀有度字段，人口≥10 通常是金卡（仅影响金色显示）
            if (c.get("rarity") == 8 or c.get("provision", 0) >= 10) \
                    and tid not in gold:
                gold.append(tid)
            provs[tid] = c["provision"]
            var_q.set("")
            lst.delete(0, "end")
            results["last"] = ""
            rebuild_rows()

        lst.bind("<Double-Button-1>", add_selected)
        var_q.trace_add("write", lambda *_: do_search())

        def poll_ime():
            """中文输入法提交候选词不触发 trace，轮询兜底（同悬浮窗补牌逻辑）。"""
            if not win.winfo_exists():
                return
            do_search()
            win.after(300, poll_ime)
        poll_ime()

        def do_save():
            if not cards:
                messagebox.showwarning("录入卡组", "卡组为空，请先添加卡牌",
                                       parent=win)
                return
            n_cards, total = totals()
            if total > self.match_limit:
                if not messagebox.askyesno(
                        "录入卡组", "人口 %d 超过上限 %d，仍要保存吗？"
                        % (total, self.match_limit), parent=win):
                    return
            name = name_var.get().strip() or leader_name or "自定义卡组"
            rec_out = {
                "id": self._next_local_id(),
                "leader": str(leader_tid or ""),
                "leader_name": leader_name or name,
                "name": name,
                "faction": fac_code(faction),
                "cards": {t: int(n) for t, n in cards.items()},
                "provs": {t: int(p) for t, p in provs.items() if t in cards},
                "gold": [g for g in gold if g in cards],
                "img": {t: u for t, u in img.items() if t in cards},
                "prov_total": total,
                "votes": 0,
                "date": time.strftime("%Y-%m-%d"),
                "src": "local",
            }
            try:
                self._save_local_deck(rec_out)
            except OSError as e:
                messagebox.showerror("录入卡组", str(e), parent=win)
                return
            self._log("已录入本地卡组：%s（%d 张 · 人口 %d）"
                      % (name, n_cards, total))
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()
            messagebox.showinfo(
                "录入成功", "已录入本地卡组库：\n%s\n（%d 张 · 人口 %d）\n\n"
                "后续对局将使用该卡组参与预测。"
                % (name, n_cards, total), parent=self.root)

        self._btn(btns, "保存到本地卡组库", do_save, accent=True).pack(side="left")
        self._btn(btns, "取消", win.destroy).pack(side="right")
        return win

    def on_local_decks(self):
        """我的卡组库：查看/删除录入卡组 + 内存读取录入入口。"""
        win = self._glass_toplevel("我的卡组库", "560x520")
        frm = tk.Frame(win, bg=BG2 if not self._glass_ok else BG)
        frm.pack(fill="both", expand=True, padx=12, pady=(10, 12))
        head = "已录入 %d 套卡组（对局预测时与在线库合并使用）" % len(self.local_decks)
        head_lbl = tk.Label(frm, text=head, bg=frm.cget("bg"), fg=GOLD_FG,
                            font=("Microsoft YaHei UI", 10, "bold"))
        head_lbl.pack(anchor="w")

        # —— 底部操作区先 pack（side="bottom"），窗口再小按钮也不会被挤出可视区
        acts = tk.Frame(frm, bg=frm.cget("bg"))
        acts.pack(side="bottom", fill="x", pady=(8, 0))

        cols = ("name", "faction", "leader", "cards", "prov", "date")
        tree = ttk.Treeview(frm, columns=cols, show="headings", height=12)
        for cid, text, w_, anchor in (
                ("name", "卡组名", 140, "w"),
                ("faction", "阵营", 76, "w"),
                ("leader", "领袖", 110, "w"),
                ("cards", "张数", 44, "center"),
                ("prov", "人口", 50, "center"),
                ("date", "录入日期", 82, "w")):
            tree.heading(cid, text=text)
            tree.column(cid, width=w_, anchor=anchor)
        for i, d in enumerate(self.local_decks):
            tree.insert("", "end", iid=str(i), tags=("odd" if i % 2 else "even",),
                        values=(
                d.get("name") or d.get("leader_name") or d.get("id"),
                fac_disp(d.get("faction")),
                d.get("leader_name") or d.get("leader") or "—",
                sum((d.get("cards") or {}).values()),
                d.get("prov_total") or sum(
                    (d.get("provs") or {}).get(t, 0) * n
                    for t, n in (d.get("cards") or {}).items()),
                d.get("date") or ""))
        tree.tag_configure("odd", background=ROW_ODD)
        tree.tag_configure("even", background=BG2)
        if not self.local_decks:
            tree.insert("", "end", iid="empty", values=(
                "暂无录入卡组，可点「从内存读取录入」读取游戏内卡组",
                "", "", "", "", ""))

        def refresh_list():
            """重命名/删除后就地刷新列表（避免重开窗口闪一下）。"""
            for iid in tree.get_children():
                tree.delete(iid)
            for i, d in enumerate(self.local_decks):
                tree.insert("", "end", iid=str(i),
                            tags=("odd" if i % 2 else "even",), values=(
                    d.get("name") or d.get("leader_name") or d.get("id"),
                    fac_disp(d.get("faction")),
                    d.get("leader_name") or d.get("leader") or "—",
                    sum((d.get("cards") or {}).values()),
                    d.get("prov_total") or sum(
                        (d.get("provs") or {}).get(t, 0) * n
                        for t, n in (d.get("cards") or {}).items()),
                    d.get("date") or ""))
            if not self.local_decks:
                tree.insert("", "end", iid="empty", values=(
                    "暂无录入卡组，可点「从内存读取录入」读取游戏内卡组",
                    "", "", "", "", ""))
            head_lbl.configure(text="已录入 %d 套卡组（对局预测时与在线库合并使用）"
                               % len(self.local_decks))

        def _sel_idx():
            sel = [s for s in tree.selection() if s.isdigit()]
            if not sel:
                return None
            return int(sel[0])

        def rename_selected():
            idx = _sel_idx()
            if idx is None:
                messagebox.showinfo("重命名卡组", "请先选中要重命名的卡组",
                                    parent=win)
                return
            d = self.local_decks[idx]
            old = d.get("name") or ""
            new = simpledialog.askstring(
                "重命名卡组", "「%s」的新名称：" % (old or "未命名"),
                initialvalue=old, parent=win)
            if new is None:
                return
            new = new.strip()
            if not new or new == old:
                return
            d["name"] = new
            save_local_decks(self.local_decks)
            refresh_list()
            self._log("卡组已重命名：%s → %s" % (old or "未命名", new))

        def del_selected():
            sel = [s for s in tree.selection() if s.isdigit()]
            if not sel:
                messagebox.showinfo("删除卡组", "请先选中要删除的卡组",
                                    parent=win)
                return
            names = [self.local_decks[int(s)].get("name", "?") for s in sel]
            if not messagebox.askyesno(
                    "删除卡组", "确定删除 %d 套卡组？\n%s" % (
                        len(sel), "、".join(names)), parent=win):
                return
            for s in sorted((int(s) for s in sel), reverse=True):
                self.local_decks.pop(s)
            save_local_decks(self.local_decks)
            self._remerge_decklib()
            self._log("已从本地卡组库删除 %d 套" % len(sel))
            refresh_list()

        # —— 列表（带纵向滚动条，多套卡组可滚动查看）——
        tree_wrap = tk.Frame(frm, bg=frm.cget("bg"))
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        self._btn(acts, "从内存读取录入", self.on_local_deck_import,
                  accent=True).pack(side="left")
        self._btn(acts, "重命名", rename_selected).pack(side="left", padx=(8, 0))
        self._btn(acts, "删除选中", del_selected).pack(side="left", padx=(8, 0))
        self._btn(acts, "关闭", win.destroy).pack(side="right")

        tree_wrap.pack(fill="both", expand=True, pady=(8, 0))
        win.geometry("+%d+%d" % (self.root.winfo_x() + 50,
                                 self.root.winfo_y() + 40))

    def on_local_deck_import(self):
        """内存读取录入：直读游戏内存中的用户卡组库，选择后录入本地卡组库。

        游戏内创建的卡组登录后常驻内存（CollectionManager），无需进对局
        即可完整读取卡牌清单/领袖/战术牌/人口；读取只发生在用户主动
        点击时，不会自动录入。
        """
        if not self.db:
            messagebox.showinfo("录入我的卡组", "卡牌库未加载完成，请稍后再试",
                                parent=self.root)
            return
        self._log("正在从游戏内存读取卡组库…")

        def worker():
            from mem_source import read_deck_library
            ok, decks, err = read_deck_library()
            self.root.after(0, lambda: self._on_decklib_read(ok, decks, err))

        threading.Thread(target=worker, daemon=True).start()

    def _decklib_rec(self, d):
        """内存直读卡组 → 本地卡组库记录；与已录入卡组重复时返回 None。"""
        cards = {str(t): int(n) for t, n in (d.get("cards") or {}).items()}
        if not cards:
            return None
        ld = str(d.get("leader") or "")
        lc = self.db.by_id.get(ld) if ld else None
        lead = (lc or {}).get("name_zh") or ""
        name = (d.get("name") or "").strip() or lead or "游戏卡组"
        for old in self.local_decks:
            if (old.get("name") == name and old.get("leader") == ld
                    and (old.get("cards") or {}) == cards):
                return None                       # 同名同领袖同清单：已录入
        provs = {str(t): int(p) for t, p in (d.get("provs") or {}).items()
                 if str(t) in cards}
        return {
            "id": self._next_local_id(),
            "leader": ld,
            "leader_name": lead or name,
            "name": name,
            "faction": fac_code(d.get("faction") or ""),
            "cards": cards,
            "provs": provs,
            "gold": [g for g in (d.get("gold") or []) if g in cards],
            "img": {},
            "prov_total": d.get("prov_total") or sum(
                provs.get(t, 0) * n for t, n in cards.items()),
            "votes": 0,
            "date": time.strftime("%Y-%m-%d"),
            "src": "memory",
        }

    def _on_decklib_read(self, ok, decks, err):
        """内存直读回调：展示卡组选择列表，确认后批量录入。"""
        if not ok:
            self._log("内存读取失败：%s" % err)
            messagebox.showwarning(
                "从内存读取录入",
                "%s\n\n提示：需要游戏正在运行且已登录到主菜单\n"
                "（卡组数据登录后常驻内存，无需停留在构筑界面）。" % err,
                parent=self.root)
            return
        if not decks:
            self._log("内存读取成功，但未发现卡组")
            messagebox.showinfo(
                "从内存读取录入",
                "未在游戏内存中发现卡组，请先在游戏中创建卡组后重试。",
                parent=self.root)
            return
        # 过滤游戏内置新手卡组（名称带 $ 前缀的本地化占位名，共 6 套）
        decks = [d for d in decks
                 if not (d.get("name") or "").strip().startswith("$")]
        if not decks:
            self._log("内存读取成功，但除内置新手卡组外没有可用卡组")
            messagebox.showinfo(
                "从内存读取录入",
                "内存中只有游戏内置的新手卡组（无需录入）。\n\n"
                "请先在游戏中创建自己的卡组后重试。",
                parent=self.root)
            return
        self._log("内存读取成功：发现 %d 套卡组（已跳过内置新手卡组）" % len(decks))
        win = self._glass_toplevel("选择要录入的卡组", "560x560")
        win.grab_set()
        frm = tk.Frame(win, bg=BG2 if not self._glass_ok else BG)
        frm.pack(fill="both", expand=True, padx=14, pady=(10, 12))
        lbl_head = tk.Label(frm, text="从游戏内存读取到 %d 套卡组，勾选要录入的："
                            % len(decks), bg=frm.cget("bg"), fg=GOLD_FG,
                            font=("Microsoft YaHei UI", 10, "bold"))
        lbl_head.pack(anchor="w")

        # —— 底部按钮先 pack，保证任何窗口尺寸下都完整可见 ——
        acts = tk.Frame(frm, bg=frm.cget("bg"))
        acts.pack(side="bottom", fill="x", pady=(8, 0))

        # —— 复选框式卡组列表（可滚动）——
        wrap = tk.Frame(frm, bg=BG3,
                        highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(fill="both", expand=True, pady=(8, 0))
        cv = tk.Canvas(wrap, bg=BG3, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        inner = tk.Frame(cv, bg=BG3)
        cv.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        inner.bind("<Configure>", lambda e: cv.configure(
            scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(
            "inner", width=e.width))
        def _wheel(e):
            # 仅当光标位于列表上方时滚动（不劫持应用其它区域的滚轮）
            if (cv.winfo_rootx() <= e.x_root
                    < cv.winfo_rootx() + cv.winfo_width()
                    and cv.winfo_rooty() <= e.y_root
                    < cv.winfo_rooty() + cv.winfo_height()):
                cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
        cv.bind_all("<MouseWheel>", _wheel, add="+")
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)

        rows = []          # [(deck, BooleanVar)]
        for i, d in enumerate(decks):
            ld = str(d.get("leader") or "")
            lc = self.db.by_id.get(ld) if ld else None
            lead = lc["name_zh"] if lc else (d.get("leader_name") or
                                             d.get("leader") or "未知领袖")
            txt = "%s   %s  ·  %s  ·  %d张 / 人口%d" % (
                fac_disp(d.get("faction")),
                d.get("name") or "未命名", lead,
                d.get("card_count", 0), d.get("prov_total", 0))
            var = tk.BooleanVar(value=True)     # 默认全选，取消勾选即可排除
            cb = self._check(inner, txt, var, bg=BG3)
            cb.grid(row=i, column=0, sticky="w", padx=6, pady=1)
            inner.columnconfigure(0, weight=1)
            rows.append((d, var))

        def set_all(v):
            for _, var in rows:
                var.set(v)

        def do_import():
            sel = [d for d, var in rows if var.get()]
            if not sel:
                messagebox.showinfo("录入我的卡组", "请先勾选要录入的卡组",
                                    parent=win)
                return
            added, dup = 0, 0
            for d in sel:
                rec = self._decklib_rec(d)
                if rec is None:
                    dup += 1
                    continue
                self._save_local_deck(rec)
                added += 1
            try:
                win.grab_release()
            except tk.TclError:
                pass
            cv.bind_all("<MouseWheel>", None)   # 解除全局滚轮绑定
            win.destroy()
            if added:
                self._log("已录入 %d 套卡组到本地卡组库%s"
                          % (added, ("，跳过重复 %d 套" % dup) if dup else ""))
                messagebox.showinfo(
                    "录入成功",
                    "已录入 %d 套卡组到本地卡组库%s。\n\n"
                    "后续对局将使用这些卡组参与预测。"
                    % (added, ("，另有 %d 套与已录入卡组重复已跳过" % dup)
                       if dup else ""), parent=self.root)
            else:
                messagebox.showinfo(
                    "录入结果", "所选卡组均已录入过本地卡组库，未重复录入。",
                    parent=self.root)

        self._btn(acts, "录入所选", do_import, accent=True).pack(side="left")
        self._btn(acts, "全选", lambda: set_all(True)).pack(side="left",
                                                           padx=(8, 0))
        self._btn(acts, "清空", lambda: set_all(False)).pack(side="left",
                                                             padx=(8, 0))
        self._btn(acts, "取消", win.destroy).pack(side="right")

        def on_close_win():
            cv.bind_all("<MouseWheel>", None)
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close_win)
        win.geometry("+%d+%d" % (self.root.winfo_x() + 60,
                                 self.root.winfo_y() + 30))

    def on_delete(self):
        sel = self.tree.selection()
        if not sel or not sel[0].isdigit():
            return
        idx = int(sel[0]) - 1
        if 0 <= idx < len(self.match.entries):
            e = self.match.entries.pop(idx)
            self._log("已删除: %s" % e["card"]["name_zh"])
            self._render()

    # ---------------- 检查更新 ----------------
    @staticmethod
    def _ver_tuple(v):
        parts = []
        for x in str(v).strip().lstrip("vV").split("."):
            n = "".join(ch for ch in x if ch.isdigit())
            parts.append(int(n) if n else 0)
        return tuple(parts)

    @staticmethod
    def _sha1(path):
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _rel_path(rel):
        """manifest 相对路径 → 本地绝对路径。"""
        return os.path.join(BASE_DIR, *str(rel).replace("\\", "/").split("/"))

    def on_check_update(self, silent=False):
        """从内置更新源拉取 manifest.json，按文件增量更新。"""
        if not UPDATE_URL:
            return
        if not silent:
            self._log("正在检查更新…")
        threading.Thread(target=self._check_update_worker,
                         args=(silent,), daemon=True).start()

    def _check_update_worker(self, silent):
        import urllib.request
        try:
            req = urllib.request.Request(
                UPDATE_URL, headers={"User-Agent": "gwent-tracker/" + APP_VERSION})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            self.root.after(0, lambda: self._on_update_result(data, silent))
        except Exception as e:
            self.root.after(0, lambda e=e: self._on_update_error(e, silent))

    def _on_update_error(self, e, silent):
        self._log("检查更新失败: %s" % e)
        if not silent:
            messagebox.showwarning("检查更新", "检查失败：%s" % e)

    def _on_update_result(self, data, silent):
        ver = str(data.get("version", ""))
        notes = str(data.get("notes", ""))
        files = data.get("files")
        files = files if isinstance(files, dict) else {}
        if not ver:
            self._log("检查更新失败：更新源返回内容缺少 version 字段")
            return
        if self._ver_tuple(ver) <= self._ver_tuple(APP_VERSION):
            self._log("已是最新版本 (v%s)" % APP_VERSION)
            if not silent:
                messagebox.showinfo("检查更新", "当前已是最新版本 v%s" % APP_VERSION)
            return
        # 单文件 exe 旧版没有松散代码目录，无法增量更新，引导重新安装
        if getattr(sys, "frozen", False) and \
                not os.path.isdir(os.path.join(BASE_DIR, "app")):
            self._log("发现新版本 v%s，本次为安装包改版，请重新下载安装" % ver)
            msg = ("发现新版本 v%s（当前 v%s）\n\n更新内容：\n%s\n\n"
                   "本次版本改为安装包形式，需要重新下载安装一次，\n"
                   "之后的版本更新都将自动完成。"
                   % (ver, APP_VERSION, notes or "（无）"))
            if messagebox.askyesno("检查更新", msg + "\n\n是否打开下载页面？"):
                import webbrowser
                webbrowser.open(str(data.get("download") or DOWNLOAD_PAGE))
            return
        todo = self._diff_manifest(files)
        if not todo:
            self._log("已是最新版本 (v%s)" % APP_VERSION)
            if not silent:
                messagebox.showinfo("检查更新", "当前已是最新版本 v%s" % APP_VERSION)
            return
        self._log("发现新版本 v%s（当前 v%s），需更新 %d 个文件"
                  % (ver, APP_VERSION, len(todo)))
        if messagebox.askyesno(
                "检查更新",
                "发现新版本 v%s（当前 v%s）\n\n更新内容：\n%s\n\n是否立即更新？"
                % (ver, APP_VERSION, notes or "（无）")):
            self._start_update(ver, notes, todo)

    def _diff_manifest(self, files):
        """对比本地文件与更新清单，返回需下载的 {相对路径: 信息}。"""
        todo = {}
        for rel, info in files.items():
            if not isinstance(info, dict) or not info.get("sha1"):
                continue
            local = self._rel_path(rel)
            try:
                same = os.path.isfile(local) and self._sha1(local) == info["sha1"]
            except OSError:
                same = False
            if not same:
                todo[str(rel)] = info
        return todo

    # ---------------- 自动更新：模态窗口 + 多线程分段下载 ----------------
    SEG_THREADS = 8          # 分段并发数
    SEG_MIN = 512 * 1024     # 超过该大小的文件启用分段下载

    def _start_update(self, ver, notes, todo):
        if not getattr(sys, "frozen", False):
            # 源码模式不做自更新，打开下载页
            import webbrowser
            webbrowser.open(DOWNLOAD_PAGE)
            return
        self._upd_state = {"total": 0, "prev": 0, "cur": 0,
                           "file": "", "phase": "download", "error": ""}
        self._upd_cancel = False
        try:
            win, bar, lbl = self._update_window(ver, notes)
        except Exception as e:
            messagebox.showerror("自动更新", "更新窗口创建失败：%s" % e)
            return
        self._upd_refresh(win, bar, lbl)
        self._log("开始下载更新（%d 个文件）…" % len(todo))
        threading.Thread(target=self._dl_worker, args=(todo, win, ver),
                         daemon=True).start()

    def _update_window(self, ver, notes):
        """模态下载窗口：进度条 + 更新说明，下载期间锁定一切操作。"""
        win = tk.Toplevel(self.root)
        win.title("正在更新 GwentTracker")
        w, h = 470, 380
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(40, (sh - h) // 3)))
        win.resizable(False, False)
        win.configure(bg=BG)
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", lambda: None)   # 下载期间禁止关闭
        tk.Label(win, text="正在更新 GwentTracker", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(18, 2))
        tk.Label(win, text="即将更新到 v%s，完成后自动重启，请耐心等待" % ver,
                 bg=BG, fg=FG_DIM,
                 font=("Microsoft YaHei UI", 9)).pack()
        box = tk.Frame(win, bg=BG2, highlightthickness=1,
                       highlightbackground=BORDER)
        box.pack(fill="both", expand=True, padx=18, pady=(12, 6))
        txt = tk.Text(box, bg=BG2, fg=FG, relief="flat",
                      font=("Microsoft YaHei UI", 9), wrap="word",
                      highlightthickness=0, padx=10, pady=8)
        txt.pack(fill="both", expand=True)
        txt.insert("end", "更新说明\n\n" + (notes or "（无）"))
        txt.configure(state="disabled")
        bar = ttk.Progressbar(win, maximum=100, value=0, length=w - 60)
        bar.pack(pady=(8, 4))
        lbl = tk.Label(win, text="准备下载…", bg=BG, fg=FG,
                       font=("Microsoft YaHei UI", 9))
        lbl.pack(pady=(0, 8))
        tk.Button(win, text="取消更新", command=lambda: self._upd_cancel_job(win),
                  bg=BTN_BG, fg=FG, activebackground=BTN_ACT,
                  activeforeground=FG, relief="flat", padx=16, pady=2,
                  cursor="hand2",
                  font=("Microsoft YaHei UI", 9)).pack(pady=(0, 14))
        win.attributes("-topmost", True)     # 确保更新窗口置顶可见
        win.update_idletasks()
        try:
            win.grab_set()      # 模态：下载期间不允许操作主窗口
        except tk.TclError:
            pass
        return win, bar, lbl

    def _upd_refresh(self, win, bar, lbl):
        """下载窗口状态轮询（120ms）。"""
        if not win.winfo_exists():
            return
        st = self._upd_state
        if st["phase"] == "download":
            done, total = st["prev"] + st["cur"], st["total"] or 1
            pct = min(100.0, done * 100.0 / total)
            bar["value"] = pct
            name = st["file"] or "…"
            if len(name) > 36:
                name = "…" + name[-36:]
            lbl["text"] = "正在下载 %s   %.1f / %.1f MB（%.0f%%）" % (
                name, done / 1048576.0, total / 1048576.0, pct)
        elif st["phase"] == "apply":
            bar["value"] = 100
            lbl["text"] = "下载完成，正在应用更新…"
        elif st["phase"] == "restart":
            bar["value"] = 100
            lbl["text"] = "更新完成，正在重启…"
        elif st["phase"] == "cancelling":
            lbl["text"] = "正在取消更新…"
        elif st["phase"] == "cancelled":
            return
        elif st["phase"] == "error":
            bar["value"] = 100
            lbl["text"] = "更新失败：%s（可关闭本窗口稍后重试）" % st["error"]
            lbl["fg"] = ERR
            win.grab_release()
            win.protocol("WM_DELETE_WINDOW", win.destroy)
            return
        win.after(120, lambda: self._upd_refresh(win, bar, lbl))

    def _dl_worker(self, todo, win, ver):
        lock = threading.Lock()
        state = self._upd_state
        try:
            state["total"] = sum(int(i.get("size", 0)) for i in todo.values())
            for rel, info in todo.items():
                if self._upd_cancel:
                    raise _UpdCancelled()
                rel = str(rel).replace("\\", "/")
                local = self._rel_path(rel)
                os.makedirs(os.path.dirname(local) or BASE_DIR, exist_ok=True)
                tmp = (os.path.join(BASE_DIR, "update_new.exe")
                       if rel == "GwentTracker.exe" else local + ".new")
                url = UPDATE_URL.rsplit("/", 1)[0] + "/" + rel
                size = int(info.get("size", 0))
                with lock:
                    state["cur"], state["file"] = 0, rel
                ok = False
                if size >= self.SEG_MIN:
                    try:
                        self._dl_segmented(url, tmp, size, state, lock)
                        ok = True
                    except _UpdCancelled:
                        raise
                    except Exception:
                        with lock:
                            state["cur"] = 0
                if not ok:
                    self._dl_single(url, tmp, state, lock)
                if self._sha1(tmp) != info.get("sha1"):
                    raise IOError("文件校验失败：%s" % rel)
                with lock:
                    state["prev"] += state["cur"]
                    state["cur"] = 0
            state["phase"] = "apply"
            self.root.after(0, lambda: self._apply_update(todo, win, ver))
        except _UpdCancelled:
            state["phase"] = "cancelled"
            self._upd_cleanup(todo)
            self.root.after(0, lambda: self._upd_finish_cancel(win))
        except Exception as e:
            state["phase"] = "error"
            state["error"] = str(e)
            self._upd_cleanup(todo)
            self._log("更新下载失败: %s" % e)

    def _upd_cancel_job(self, win):
        """用户点击取消更新：停止下载并清理临时文件。"""
        self._upd_cancel = True
        if self._upd_state["phase"] == "download":
            self._upd_state["phase"] = "cancelling"
        self._log("正在取消更新…")

    def _upd_finish_cancel(self, win):
        self._log("更新已取消，未修改任何文件")
        try:
            win.grab_release()
        except tk.TclError:
            pass
        win.destroy()

    def _dl_segmented(self, url, tmp, size, state, lock):
        """多线程 Range 分段下载，写入同一文件的不同区间。"""
        import urllib.request
        n = self.SEG_THREADS
        seg = size // n
        ranges = []
        start = 0
        for i in range(n):
            end = size - 1 if i == n - 1 else start + seg - 1
            ranges.append((start, end))
            start = end + 1
        with open(tmp, "wb"):
            pass                      # 预创建空文件
        errs = []

        def part(a, b):
            try:
                req = urllib.request.Request(url, headers={
                    "Range": "bytes=%d-%d" % (a, b),
                    "User-Agent": "gwent-tracker/" + APP_VERSION})
                with urllib.request.urlopen(req, timeout=30) as r:
                    if r.getcode() != 206:
                        raise IOError("服务器不支持分段下载")
                    with open(tmp, "r+b") as f:
                        f.seek(a)
                        left = b - a + 1
                        while left > 0:
                            if self._upd_cancel:
                                raise _UpdCancelled()
                            chunk = r.read(min(1 << 16, left))
                            if not chunk:
                                raise IOError("连接中断")
                            f.write(chunk)
                            left -= len(chunk)
                            with lock:
                                state["cur"] += len(chunk)
            except Exception as ex:
                errs.append(ex)

        ts = [threading.Thread(target=part, args=rg, daemon=True)
              for rg in ranges]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if errs:
            raise errs[0]

    def _dl_single(self, url, tmp, state, lock):
        """单线程下载（小文件 / 服务器不支持 Range 时回退）。"""
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "gwent-tracker/" + APP_VERSION})
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
            while True:
                if self._upd_cancel:
                    raise _UpdCancelled()
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                with lock:
                    state["cur"] += len(chunk)

    def _upd_cleanup(self, todo):
        """清理下载残留的 .new / update_new.exe 临时文件。"""
        for rel in todo:
            if str(rel) == "GwentTracker.exe":
                paths = [os.path.join(BASE_DIR, "update_new.exe")]
            else:
                paths = [self._rel_path(rel) + ".new"]
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _apply_update(self, todo, win, ver):
        self._log("下载完成，正在应用更新…")
        try:
            exe_new = "GwentTracker.exe" in todo
            for rel in todo:
                if str(rel) == "GwentTracker.exe":
                    continue
                os.replace(self._rel_path(rel) + ".new", self._rel_path(rel))
            shutil.rmtree(os.path.join(BASE_DIR, "app", "__pycache__"),
                          ignore_errors=True)
            self.root.after(0, lambda: self._finish_update(exe_new, win, ver))
        except Exception as e:
            self._upd_state["phase"] = "error"
            self._upd_state["error"] = str(e)
            self._upd_cleanup(todo)

    def _finish_update(self, exe_new, win, ver):
        self._upd_state["phase"] = "restart"
        self._log("更新完成（v%s），正在重启…" % ver)
        exe = os.path.abspath(sys.executable)
        if exe_new:
            # Windows 允许重命名正在运行的 exe：旧文件改名 .old，下次启动自动清理
            try:
                os.rename(exe, exe + ".old")
            except OSError:
                pass
            try:
                os.replace(os.path.join(BASE_DIR, "update_new.exe"), exe)
            except OSError as e:
                self._upd_state["phase"] = "error"
                self._upd_state["error"] = "替换主程序失败：%s" % e
                return
        try:
            subprocess.Popen([exe], cwd=BASE_DIR)
        except OSError:
            pass
        self.root.after(600, self._on_close)

    # ---------------- 对手卡组预测 ----------------
    def _auto_refresh_decklib(self):
        """内置 276 套离线卡组库兜底；仅当完全无卡组数据时才自动联网下载。"""
        if self._lib_fetching:
            return
        if not self.decklib:
            self._log("本地无卡组库，自动下载热门卡组…")
            self._refresh_decklib()
        elif self._builtin_decks:
            self._log("已加载卡组库：%d 套（内置 %d 套，可点「更新卡组库」"
                      "从服务端获取最新）" % (len(self.decklib),
                                             len(self._builtin_decks)))

    def on_refresh_decklib(self):
        """点「更新卡组库」：先弹窗确认获取数量（默认 120），再开始下载。"""
        if self._lib_fetching:
            self._log("卡组库正在下载中，请稍候…")
            return
        win = self._glass_toplevel("更新卡组库", None)
        win.grab_set()
        win.resizable(False, False)
        box = tk.Frame(win, bg=win.cget("bg"))
        box.pack(fill="both", expand=True, padx=26, pady=(18, 16))
        tk.Label(box, text="更新卡组库", bg=box["bg"], fg=GOLD_FG,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(box, text="从服务端拉取最新热门卡组的数量（20-240），\n"
                           "数量越多预测越准，下载耗时也越长：",
                 bg=box["bg"], fg=FG, justify="left",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 6))
        var_n = tk.IntVar(value=max(20, min(240, int(
            self.cfg.get("deck_fetch_limit", 120)))))
        row = tk.Frame(box, bg=box["bg"])
        row.pack(anchor="w", pady=(0, 12))
        tk.Spinbox(row, from_=20, to=240, increment=10, width=6,
                   textvariable=var_n, bg=BG2, fg=FG, relief="flat",
                   insertbackground=FG, buttonbackground=BG2,
                   font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(row, text="套", bg=box["bg"], fg=FG_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 0))

        def start():
            try:
                n = max(20, min(240, int(var_n.get())))
            except (ValueError, tk.TclError):
                n = 120
            self.cfg["deck_fetch_limit"] = n
            save_config(self.cfg)
            win.destroy()
            self._log("开始从服务端更新卡组库（%d 套）…" % n)
            self._refresh_decklib()

        acts = tk.Frame(box, bg=box["bg"])
        acts.pack(fill="x")
        self._btn(acts, "开始更新", start, accent=True).pack(side="right")
        self._btn(acts, "取消", win.destroy).pack(side="right", padx=(0, 8))
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.root.winfo_x() + 90,
                                 self.root.winfo_y() + 120))

    def _refresh_decklib(self):
        self._lib_fetching = True
        self.btn_decklib.set_enabled(False, "更新中")
        cache = os.path.join(BASE_DIR, "decks_cache.json")
        limit = max(20, min(240, int(self.cfg.get("deck_fetch_limit", 120))))

        def work():
            try:
                decks = deck_predict.fetch_library(
                    max_decks=limit,
                    cache_path=cache,
                    progress=lambda d, t: self.root.after(
                        0, lambda a=d, b=t: self._log(
                            "卡组库下载中… %d/%d" % (a, b))),
                    on_partial=lambda dks: self.root.after(
                        0, lambda: self._decklib_partial(dks)))
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda e=e: self._decklib_fail(e))
                return
            self.root.after(0, lambda: self._decklib_done(decks))

        threading.Thread(target=work, daemon=True).start()

    def _decklib_partial(self, decks):
        """边下边用：每批数据到达即刷新本地卡组库，无需等全部下完。"""
        merged = deck_predict.merge_libraries(
            self.local_decks,
            deck_predict.merge_libraries(decks, self._builtin_decks))
        if len(merged) > len(self.decklib):
            self.decklib = merged
            self._lib_time = time.time()
            self._pred_key = None
            self._sync_art_urls()
            self._render()

    def _decklib_done(self, decks):
        self._lib_fetching = False
        self.btn_decklib.set_enabled(True, "更新卡组库")
        if not decks:
            self._log("卡组库更新失败：未获取到卡组数据")
            return
        self.decklib = deck_predict.merge_libraries(
            self.local_decks,
            deck_predict.merge_libraries(decks, self._builtin_decks))
        self._lib_time = time.time()
        self._pred_key = None
        self._lib_outdated = False          # 更新完成后清除提醒状态
        self._sync_art_urls()
        self._log("卡组库已更新：服务端 %d 套，合并内置库共 %d 套"
                  % (len(decks), len(self.decklib)))
        self._render()

    def _decklib_fail(self, e):
        self._lib_fetching = False
        self.btn_decklib.set_enabled(True, "更新卡组库")
        self._log("卡组库更新失败: %s（已保留 %d 套）" % (e, len(self.decklib)))

    # ---------------- 卡图资源 ----------------
    def _sync_art_urls(self):
        """把卡组库覆盖的全部卡图 URL 注入共享池。

        此前 URL 只在悬浮窗预测结果里逐张 set_url，其余界面（卡组明细/
        手动添加卡牌等）直接 request 时常因没有 URL 而永远加载不出。"""
        try:
            for tid, u in self._art_all_jobs().items():
                self._artpool.set_url(tid, u)
        except Exception:  # noqa: BLE001
            pass

    def _art_all_jobs(self):
        """卡组库全量去重卡图：tid -> playgwent 相对路径。"""
        jobs = {}
        for d in self.decklib:
            for tid, u in (d.get("img") or {}).items():
                tid, u = str(tid), str(u)
                if tid and u and tid not in jobs:
                    jobs[tid] = u
        return jobs

    def on_download_all_art(self):
        """一次性下载卡组库覆盖的全部卡面原画到本地 cards_art/。

        之后悬浮窗与对局详情直接命中磁盘缓存，无需再逐张联网加载。"""
        if self._art_dl is not None:
            self._log("卡图资源正在下载中，请稍候…")
            return
        jobs = {tid: u for tid, u in self._art_all_jobs().items()
                if not os.path.exists(os.path.join(_ArtPool.ART_DIR,
                                                   "%s.hd.jpg" % tid))}
        if not jobs:
            self.cfg["art_full"] = True
            save_config(self.cfg)
            self._log("完整卡图资源已就绪，无需下载")
            return
        self._art_dl = {"stop": False}
        total = len(jobs)
        win = self._glass_toplevel("下载卡图资源", None)
        win.protocol("WM_DELETE_WINDOW", lambda: None)   # 下载中禁止误关，用取消按钮
        box = tk.Frame(win, bg=win.cget("bg"))
        box.pack(fill="both", expand=True, padx=26, pady=(18, 16))
        tk.Label(box, text="正在下载完整卡图资源", bg=box["bg"], fg=GOLD_FG,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(box, text="下载到本地后，悬浮窗与对局详情无需再逐张联网加载。",
                 bg=box["bg"], fg=FG_DIM, font=("Microsoft YaHei UI", 8)
                 ).pack(anchor="w", pady=(4, 6))
        lbl = tk.Label(box, text="0 / %d" % total, bg=box["bg"], fg=FG,
                       font=("Microsoft YaHei UI", 9, "bold"))
        lbl.pack(anchor="w", pady=(0, 4))
        pb = ttk.Progressbar(box, length=340, maximum=total,
                             orient="horizontal")
        pb.pack(anchor="w", pady=(0, 10))
        self._btn(box, "取消下载",
                  lambda: self._art_dl.update(stop=True)).pack(anchor="w")
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.root.winfo_x() + 90,
                                 self.root.winfo_y() + 140))

        q = queue.Queue()
        for item in jobs.items():
            q.put(item)
        state = {"done": 0, "failed": 0, "workers": 6}

        def _progress():
            if not win.winfo_exists():
                return
            pb["value"] = state["done"]
            txt = "%d / %d" % (state["done"], total)
            if state["failed"]:
                txt += "（失败 %d 张，将在使用时自动重试）" % state["failed"]
            lbl.config(text=txt)

        def _finish():
            if self._art_dl is None or not win.winfo_exists():
                return
            stopped = self._art_dl["stop"]
            self._art_dl = None
            win.destroy()
            if stopped:
                self._log("卡图下载已取消：已完成 %d/%d，可稍后继续"
                          % (state["done"], total))
                return
            self.cfg["art_full"] = True
            save_config(self.cfg)
            self._artpool._art_miss.clear()   # 失败锁清除，缺图立即可用
            self._pred_key = None
            self._render()
            self._log("完整卡图资源下载完成：%d 张（失败 %d 张，使用时自动重试）"
                      % (state["done"] - state["failed"], state["failed"]))

        def _worker_done():
            state["workers"] -= 1
            if state["workers"] <= 0:
                _finish()

        def worker():
            import urllib.request
            while self._art_dl is not None and not self._art_dl["stop"]:
                try:
                    tid, u = q.get_nowait()
                except queue.Empty:
                    break
                path = os.path.join(_ArtPool.ART_DIR, "%s.hd.jpg" % tid)
                try:
                    if not os.path.exists(path):
                        req = urllib.request.Request(
                            "https://www.playgwent.com" + u,
                            headers=deck_predict.UA)
                        with urllib.request.urlopen(req, timeout=15) as f:
                            raw = f.read()
                        with open(path, "wb") as fo:
                            fo.write(raw)
                except Exception:  # noqa: BLE001
                    state["failed"] += 1
                state["done"] += 1
                self.root.after(0, _progress)
            self.root.after(0, _worker_done)

        for _ in range(6):
            threading.Thread(target=worker, daemon=True).start()

    def _maybe_prompt_art(self):
        """首次启动询问是否一次性下载完整卡图资源（此后不再询问）。"""
        if self.cfg.get("art_full") or self.cfg.get("art_prompted"):
            return
        self.cfg["art_prompted"] = True
        save_config(self.cfg)
        if messagebox.askyesno(
                "下载卡图资源",
                "是否立即一次性下载完整卡图资源包？\n\n"
                "下载后悬浮窗与对局详情将直接使用本地卡图，\n"
                "无需再逐张联网加载。\n"
                "（也可以稍后在 设置 → 卡图资源 中下载）",
                parent=self.root):
            self.on_download_all_art()
        else:
            self._log("已跳过完整卡图下载，可稍后在「设置 → 卡图资源」中下载")

    # ---------------- 卡组库更新检测 ----------------
    def _check_decklib_update(self):
        """定期与官方热门榜对比：新卡组占比高时提醒用户更新卡组库。

        只拉列表页（2 个请求），比整套下载轻量得多；检测节奏：
        启动 30 秒后首次，之后每 6 小时一次。"""
        if self._lib_fetching:
            self._lib_check_schedule()      # 正在下载，稍后再查
            return
        limit = max(20, min(240, int(self.cfg.get("deck_fetch_limit", 120))))

        def work():
            try:
                hot = deck_predict.fetch_hot_ids(limit)
            except Exception:  # noqa: BLE001
                hot = None
            self.root.after(0, lambda: self._decklib_check_done(hot, limit))

        threading.Thread(target=work, daemon=True).start()

    def _lib_check_schedule(self, hours=6):
        """安排下一次卡组库更新检测。"""
        if self._lib_check_job is not None:
            try:
                self.root.after_cancel(self._lib_check_job)
            except tk.TclError:
                pass
        self._lib_check_job = self.root.after(
            hours * 3600 * 1000, self._check_decklib_update)

    def _decklib_check_done(self, hot, limit):
        self._lib_check_schedule()
        if not hot:
            return                          # 拉取失败，等下一轮
        local = {str(d.get("id") or "") for d in self.decklib}
        new_cnt = sum(1 for gid in hot if gid not in local)
        threshold = max(15, limit // 4)     # 热门榜 1/4 以上是新卡组 → 大规模更新
        if new_cnt >= threshold:
            self._lib_outdated = True
            self._log("检测到卡组库大规模更新：热门榜前 %d 套中有 %d 套未收录，"
                      "建议点「更新卡组库」获取最新卡组" % (limit, new_cnt))
            try:
                if self.btn_decklib.winfo_exists():
                    self.btn_decklib.set_enabled(True, "卡组库有更新！")
            except tk.TclError:
                pass

    # ---------------- 预测悬浮窗 ----------------
    def _ov_ensure(self):
        if self._ov is None:
            try:
                self._ov = PredictionOverlay(self)
            except Exception:  # noqa: BLE001
                self._ov = None
        return self._ov

    def _ov_autoshow(self):
        """游戏在运行时自动弹出预测悬浮窗（仅天梯/训练模式）。"""
        if (self.cfg.get("pred_overlay", True)
                and self.cfg.get("pred_ladder", True)):
            self.on_toggle_overlay(turn_on=True)

    def on_toggle_overlay(self, turn_on=None):
        want = turn_on if turn_on is not None else not (
            self._ov is not None and self._ov.shown)
        self.cfg["pred_overlay"] = bool(want)
        save_config(self.cfg)
        try:
            if self.btn_overlay.winfo_exists():
                self.btn_overlay.set_enabled(True,
                                             "隐藏悬浮" if want else "悬浮窗")
        except tk.TclError:
            pass
        if want:
            ov = self._ov_ensure()
            if ov:
                ov.show()
                self._pred_key = None
                self._render()
        elif self._ov:
            self._ov.hide()

    def on_toggle_ladder(self):
        """切换卡组预测适用模式：天梯/训练 ↔ 其他（关闭预测）。"""
        new_on = not self.cfg.get("pred_ladder", True)
        self.cfg["pred_ladder"] = new_on
        save_config(self.cfg)
        self._pred_fail_streak = 0
        try:
            if self.btn_ladder.winfo_exists():
                self.btn_ladder.set_enabled(
                    True, "模式:天梯/训练" if new_on else "模式:其他(预测关)")
        except tk.TclError:
            pass
        if not new_on:
            if self._ov is not None and self._ov.shown:
                self._ov.hide()
            self._pred_bar_reset()
            self.lbl_pred_miss.config(text="")
        self._pred_key = None
        self._log("卡组预测已启用（仅天梯/训练模式）" if new_on
                  else "卡组预测已关闭（其他模式不适用）")
        # 立即刷新文案与悬浮窗（此前需等下一条游戏事件才会更新）
        if new_on and self.cfg.get("pred_overlay", True) and not (
                self._ov is not None and self._ov.shown):
            self.on_toggle_overlay(turn_on=True)
        self._render()

    def _ov_follow(self):
        if self._ov is not None:
            try:
                self._ov.follow()
            except Exception:  # noqa: BLE001
                pass
        self.root.after(1200, self._ov_follow)

    # ---------------- 悬浮窗手动修正 ----------------
    def _ov_manual_invalidate(self):
        """手动标记变化后强制重算预测并刷新悬浮窗。"""
        self._pred_key = None
        self._pred_fail_streak = 0   # 手动修正后重新尝试匹配
        self._update_prediction()

    def _ov_mark_played(self, tid):
        """把一张未打出的牌标记为已打出（补记直接进墓地等未上场效果）。"""
        tid = str(tid)
        cur = self._ov_cancel.get(tid, 0)
        if cur > 1:
            self._ov_cancel[tid] = cur - 1
        elif cur == 1:
            del self._ov_cancel[tid]
        else:
            self._ov_extra[tid] = self._ov_extra.get(tid, 0) + 1
        self._log("手动标记已打出: %s" % self._ov_card_name(tid))
        self._ov_manual_invalidate()

    def _ov_mark_unplayed(self, tid):
        """把一张已打出的牌回退为未打出（修正误计，如对方间谍）。"""
        tid = str(tid)
        cur = self._ov_extra.get(tid, 0)
        if cur > 1:
            self._ov_extra[tid] = cur - 1
        elif cur == 1:
            del self._ov_extra[tid]
        else:
            self._ov_cancel[tid] = self._ov_cancel.get(tid, 0) + 1
        self._log("手动标记未打出: %s" % self._ov_card_name(tid))
        self._ov_manual_invalidate()

    def _ov_manual_reset(self):
        """清除本局全部手动标记。"""
        self._ov_extra = {}
        self._ov_cancel = {}
        self._log("已清除全部手动标记")
        self._ov_manual_invalidate()

    def _ov_card_name(self, tid):
        c = self.db.by_id.get(str(tid)) if self.db else None
        return c["name_zh"] if c else ("卡#" + str(tid))

    def on_ov_add_card(self):
        """悬浮窗右键 → 手动添加卡牌：模糊搜索卡名后补记为已打出。

        结果以「卡图缩略 + 名称 + 人口」卡片列表展示；输入框用
        250ms 轮询检测变化——中文输入法提交候选词时既不触发
        StringVar trace 也不触发 KeyRelease，仅靠事件回调会漏检
        （表现为"输入了卡名但搜不出结果"）。"""
        if not self.db:
            messagebox.showinfo("手动添加卡牌", "卡牌库未加载，无法搜索",
                                parent=self.root)
            return
        win = self._glass_toplevel("手动添加卡牌", "380x560")
        frm = tk.Frame(win, bg=BG2 if not self._glass_ok else BG)
        frm.pack(fill="both", expand=True, padx=14, pady=(12, 10))
        tk.Label(frm, text="输入卡名（中/英文均可，支持模糊搜索）：",
                 bg=frm.cget("bg"), fg=FG,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        var = tk.StringVar()
        ent = self._entry(frm, textvariable=var, width=340,
                          placeholder="卡名，如：凯尔卓")
        ent.pack(fill="x", pady=(4, 8))
        ent.focus_set()
        cv = tk.Canvas(frm, bg=BG3 if not self._glass_ok else BG,
                       highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        f_name = tkfont.Font(family="Microsoft YaHei UI", size=10,
                             weight="bold")
        f_dim = tkfont.Font(family="Microsoft YaHei UI", size=8)
        ROW_H = 92                      # 卡片行高（缩略图 60x84 + 上下留白）
        CW = 352                        # 画布内容宽（380 - 两侧留白）
        state = {"list": [], "sel": 0, "rows": []}

        def draw_list():
            cv.delete("all")
            rows = []
            y = 4
            for i, c in enumerate(state["list"]):
                if i == state["sel"]:
                    cv.create_rectangle(2, y, CW - 2, y + ROW_H,
                                        fill=ROW_SEL, outline="")
                tid = str(c["id"])
                img = self._artpool.get_thumb(tid)
                if img is not None:
                    cv.create_image(2 + 32, y + ROW_H // 2, image=img)
                else:
                    self._artpool.request(tid)
                    cv.create_rectangle(4, y + 4, 62, y + ROW_H - 4,
                                        fill=BG, outline=BORDER)
                tx = 74
                cy = y + ROW_H // 2
                cv.create_text(tx, cy - 11, text=c["name_zh"], anchor="w",
                               font=f_name, fill=FG)
                cv.create_text(tx, cy + 12, text="%s · 人口 %d" % (
                    c["faction"], c["provision"]), anchor="w",
                    font=f_dim, fill=FG_DIM)
                rows.append((y, y + ROW_H))
                y += ROW_H + 2
            state["rows"] = rows
            cv.configure(scrollregion=(0, 0, 1, max(y + 4, cv.winfo_height())))

        def redraw_sub():
            """卡图异步下载就绪后重绘（窗口已关则跳过）。"""
            try:
                if win.winfo_exists():
                    draw_list()
            except tk.TclError:
                pass

        def do_search():
            try:
                state["list"] = self.db.search_fuzzy(var.get(), 40)
            except Exception as e:  # noqa: BLE001
                self._log("卡名搜索失败: %r" % e)
                state["list"] = []
            state["sel"] = 0
            cv.yview_moveto(0)
            draw_list()

        def row_at(yc):
            for i, (a, b) in enumerate(state["rows"]):
                if a <= yc < b:
                    return i
            return None

        def add_selected(_e=None):
            lst = state["list"]
            if lst and 0 <= state["sel"] < len(lst):
                c = lst[state["sel"]]
                close()
                self._ov_mark_played(str(c["id"]))

        def close():
            self._artpool.unsubscribe(redraw_sub)
            try:
                win.destroy()
            except tk.TclError:
                pass

        def on_click(e):
            i = row_at(cv.canvasy(e.y))
            if i is not None:
                state["sel"] = i
                draw_list()

        def on_dbl(e):
            i = row_at(cv.canvasy(e.y))
            if i is not None:
                state["sel"] = i
                add_selected()

        cv.bind("<Button-1>", on_click)
        cv.bind("<Double-Button-1>", on_dbl)
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        cv.bind("<Enter>", lambda _e: cv.focus_set())
        ent.bind("<Return>", lambda _e: add_selected())
        self._artpool.subscribe(redraw_sub)
        btns = tk.Frame(frm, bg=frm.cget("bg"))
        btns.pack(fill="x", pady=(8, 0))
        self._btn(btns, "添加为已打出", add_selected).pack(side="right")

        # 轮询输入变化（兼容输入法提交），并立即执行一次空搜索
        last = {"v": None}

        def poll():
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            v = var.get()
            if v != last["v"]:
                last["v"] = v
                do_search()
            win.after(250, poll)

        poll()

    def _ov_push(self, state=None, result=None, played=None, alert=False):
        """把当前预测状态推送到悬浮窗（未显示时忽略）。"""
        if (not self.cfg.get("pred_overlay", True)
                or not self.cfg.get("pred_ladder", True)):
            return
        ov = self._ov_ensure()
        if not ov or not ov.shown:
            return
        try:
            if result is not None:
                ov.set_result(result, played or {}, self.db)
            else:
                ov.set_state(state or "等待对手出牌…", alert=alert)
        except tk.TclError:
            pass

    def _apply_points(self, data):
        """内存模式实时点数：s1/s2 为玩家1/2 总点数，local 为本地方 id。"""
        local = int(data.get("local", 0) or 0)
        s1 = int(data.get("s1", 0) or 0)
        s2 = int(data.get("s2", 0) or 0)
        if local == 1:
            self._pts = (s1, s2)
        elif local == 2:
            self._pts = (s2, s1)
        else:
            return                 # 本地方未知，无法区分敌我
        # 记录最近一次非零点数：小局切换时场上先清空（0/0 先于回合事件
        # 到达），快照该小局最终点数时需退回此值，否则比分漏记
        if self._pts[0] > 0 or self._pts[1] > 0:
            self._pts_last = self._pts
            # 实时归属当前小局（比分 1-0 根治：见 MatchRecord.update_round_pts）
            if self.in_match and self.match is not None:
                self.match.update_round_pts(self.round_no, self._pts)
        self._ov_push_stats()
        self._update_matchinfo_line()   # 对局信息行同步实时点数

    def _pts_snapshot(self):
        """可用于小局快照的点数：当前为零/未知时退回最近一次非零值。"""
        if self._pts and (self._pts[0] or self._pts[1]):
            return self._pts
        return self._pts_last

    def _apply_mydeck(self, cards):
        """内存直读的我方卡组：开局真实卡牌（含领袖），模板ID->张数。"""
        if not cards:
            return
        cards = {str(k): int(v) for k, v in cards.items() if int(v) > 0}
        if not cards:
            return
        first = not self._my_deck_cards
        self._my_deck_cards = cards
        if self.match is not None:
            self.match.my_deck = cards
        if first:
            self._log("已读取我方卡组：%d 种卡牌" % len(cards))

    # ---------------- 对局信息（内存读取：模式/昵称/天梯分/回合归属） ----------------
    def _apply_matchinfo(self, m):
        """内存读取的对局信息：游戏模式、回合归属、双方昵称/等级/天梯分。

        EGameMode（GameInstance._gameMode）: 0=SinglePlayer 人机，1/2=线上；
        线上且对手 MMR>0 = 天梯（职天），MMR=0 = 休闲/友谊赛。
        """
        mode = int(m.get("mode", 0) or 0)
        turn = m.get("turn", "opp")
        mine = m.get("mine") or {}
        opp = m.get("opp") or {}
        opp_mmr = int(opp.get("mmr", 0) or 0)
        if mode == 0:
            mode_name = "训练"
        elif opp_mmr > 0:
            mode_name = "天梯"
        else:
            mode_name = "休闲/友谊"
        self.match_info = {"mode": mode, "mode_name": mode_name,
                           "turn": turn, "mine": mine, "opp": opp}
        if self.in_match and self.match is not None:
            self.match.meta = {
                "mode": mode, "mode_name": mode_name,
                "my_name": mine.get("name") or "",
                "my_mmr": int(mine.get("mmr", 0) or 0),
                "my_level": int(mine.get("level", 0) or 0),
                "opp_name": opp.get("name") or "",
                "opp_mmr": opp_mmr,
                "opp_level": int(opp.get("level", 0) or 0),
            }
        self._update_matchinfo_line()
        self._update_greeting()
        if self.in_match:
            self.game_state = (OK, "对局中·%s" % mode_name)
            self._set_state(self.lbl_game, self.game_state)

    def _update_matchinfo_line(self):
        """对局信息栏：信息行（昵称/段位/回合归属）+ 比分行（大号双色实时点数）。

        原先单行文字太单调，改为「信息行 + 比分行」双层布局：比分用
        我方蓝 / 敌方橙大号数字直观呈现，右侧追加局分计数（权威小局结算驱动）。
        """
        mi = getattr(self, "match_info", None)
        if not mi or not self.in_match:
            # 未开局给功能入口提示；已进对局但对手信息还没读到时给过渡文案
            # （重连、中途挂载常见，仍显示“等待开始对局”会显得状态错乱）
            if self.in_match:
                tip = "已进入对局，正在读取对手信息…"
            else:
                tip = "等待开始对局，若需开启对手卡组预测功能请点击悬浮窗按钮"
            lbl = getattr(self, "lbl_pred_top", None)
            if lbl is not None:
                lbl.config(text=tip, fg=FG_DIM)
            for attr in ("lbl_pred_turn", "lbl_score_mine",
                         "lbl_score_opp", "lbl_score_cap"):
                lbl = getattr(self, attr, None)
                if lbl is not None:
                    lbl.config(text="")
            return
        opp = mi.get("opp") or {}
        name = (opp.get("name") or "").strip() or "未知对手"
        mmr = int(opp.get("mmr", 0) or 0)
        rank = int(opp.get("rank", 0) or 0)
        info = name
        if mmr > 0:
            info += " · 职天 %d 分" % mmr      # 职天玩家才有天梯分数
        elif rank > 0:
            info += " · 段位 %d 级" % rank     # 普通天梯显示段位等级（1-40）
        if getattr(self, "lbl_pred_top", None) is not None:
            self.lbl_pred_top.config(text=info, fg=FG)
        # 回合归属
        if getattr(self, "lbl_pred_turn", None) is not None:
            if mi.get("turn") == "mine":
                self.lbl_pred_turn.config(text="⚔ 你的回合", fg=OK)
            else:
                self.lbl_pred_turn.config(text="⌛ 对方回合", fg=FG_DIM)
        # 当前小局实时点数（我方 / 敌方）
        pts = getattr(self, "_pts", None)
        if pts is not None:
            mine_s = str(int(pts[0] or 0))
            opp_s = str(int(pts[1] or 0))
        else:
            mine_s = opp_s = "—"
        if getattr(self, "lbl_score_mine", None) is not None:
            self.lbl_score_mine.config(text=mine_s)
            self.lbl_score_opp.config(text=opp_s)
        # 局分（小局胜负计数，由权威小局结算 round_scores 驱动）
        mw = ow = 0
        if self.match is not None:
            for _r, (mm, oo) in sorted(self.match.round_scores.items()):
                if mm > oo:
                    mw += 1
                elif oo > mm:
                    ow += 1
        if getattr(self, "lbl_score_cap", None) is not None:
            if mw or ow:
                self.lbl_score_cap.config(text="局分 %d : %d" % (mw, ow), fg=ACCENT)
            else:
                self.lbl_score_cap.config(text="", fg=FG_DIM)

    # ---------------- 首页问候语 ----------------
    def _greet_text(self):
        h = time.localtime().tm_hour
        if 5 <= h < 11:
            return "早上好"
        if 11 <= h < 13:
            return "中午好"
        if 13 <= h < 18:
            return "下午好"
        if 18 <= h < 23:
            return "晚上好"
        return "夜深了"

    def _update_greeting(self):
        lbl = getattr(self, "lbl_greet", None)
        if lbl is None:
            return
        name = ((getattr(self, "match_info", None) or {})
                .get("mine") or {}).get("name") or ""
        disp = name.strip() or "指挥官"
        lbl.config(text="%s，%s · 今天也来打一局昆特牌吧！"
                   % (self._greet_text(), disp))

    def _greet_loop(self):
        """时段问候随时间自动刷新（每 30 秒）。"""
        self._update_greeting()
        self.root.after(30000, self._greet_loop)

    def _ov_push_stats(self):
        """把人口对比/实时点数推送到悬浮窗统计区。"""
        if (not self.cfg.get("pred_overlay", True)
                or not self.cfg.get("pred_ladder", True)):
            return
        ov = self._ov
        if ov is None or not ov.shown:
            return
        stats = None
        if self.in_match:
            mpts, opts = (self._pts or (None, None))
            stats = {
                "my": self.match.total_provision(side=1),
                "myl": self._my_limit(),
                "opp": self.match.total_provision(side=0),
                "oppl": self._opp_limit(),
                "mpts": mpts,
                "opts": opts,
            }
        try:
            ov.set_stats(stats)
        except tk.TclError:
            pass

    def _pred_deck_name(self, r):
        name = r.get("leader_name")
        if not name and self.db:
            c = self.db.by_id.get(r["leader"])
            name = c["name_zh"] if c else ""
        return name or ("领袖#" + str(r["leader"]))

    def _update_prediction(self):
        if not self.cfg.get("pred_ladder", True):
            if self._pred_key != "off":
                self._pred_key = "off"
                self.lbl_pred_miss.config(text="")
                self._pred_bar_reset()
                ov = self._ov
                if ov is not None and ov.shown:
                    try:
                        ov.set_state("卡组预测已关闭\n（仅支持天梯 / 训练模式）")
                    except tk.TclError:
                        pass
            return
        played = {}
        for e in self.match.entries:
            if e["side"] == 0:
                cid = str(e["card"]["id"])
                played[cid] = played.get(cid, 0) + e["count"]
        # 手动修正叠加（右键标记）：直接进墓地等未上场效果漏记 → 补记；
        # 误记（如对方间谍）→ 回退为未打出
        if self._ov_extra or self._ov_cancel:
            for tid, n in self._ov_extra.items():
                played[tid] = played.get(tid, 0) + n
            for tid, n in self._ov_cancel.items():
                if tid in played:
                    played[tid] = max(0, played[tid] - n)
        key = (self._opp_leader_tid, tuple(sorted(self._ov_extra.items())),
               tuple(sorted(self._ov_cancel.items())),
               tuple(sorted(played.items())))
        if key == self._pred_key:
            return
        self._pred_key = key

        if not self.decklib:
            self.lbl_pred_miss.config(text="")
            self._pred_bar_reset()
            self._ov_push(state="卡组库未加载，请先点主窗口\n「更新卡组库」")
            return
        if not played:
            self._pred_fail_streak = 0
            self._last_pred_label = None
            self.lbl_pred_miss.config(text="")
            self._pred_bar_reset()
            self._ov_push(state="等待对手出牌…")
            return

        res = deck_predict.predict(
            self.decklib, played,
            leader=str(self._opp_leader_tid) if self._opp_leader_tid else None,
            top=3)
        if not res:
            return
        r = res[0]
        # 连续多张牌无法匹配任何卡组 → 提示“无法预测”并停止显示，避免误导
        if r["matched"] == 0:
            self._pred_fail_streak += 1
        else:
            self._pred_fail_streak = 0
        limit = max(2, int(self.cfg.get("pred_fail_limit", 6)))
        if self._pred_fail_streak >= limit:
            if self._pred_fail_streak == limit:   # 只在首次暂停时记录
                self._log("连续 %d 张牌未匹配到热门卡组库，已暂停预测" % limit)
            self.lbl_pred_miss.config(
                text="对手卡组可能不在热门库中；已停止预测，"
                     "匹配到后自动恢复（阈值可在设置中调整）")
            self._pred_bar_reset()
            self._ov_push(state="⚠ 暂无法预测对手卡组\n"
                                "已连续 %d 张未匹配到热门卡组库\n"
                                "匹配到后将自动恢复预测"
                          % self._pred_fail_streak, alert=True)
            return
        fzh = deck_predict.FACTION_ZH.get(r["faction"], r["faction"] or "?")
        # 预测卡组变化时记入对局事件（便于确认内存数据读取是否正常）
        pred_label = "[%s] %s" % (fzh, self._pred_deck_name(r))
        if pred_label != getattr(self, "_last_pred_label", None):
            self._last_pred_label = pred_label
            self._log("对手卡组预测：%s（匹配 %d%%）"
                      % (pred_label, int(r["conf"] * 100)))
        # 预测结果仅展示在悬浮窗（主界面已改为对局信息行）
        self.lbl_pred_miss.config(text="")
        self._pred_bar_reset()
        # 手动补记的卡若不在预测卡组内，注入为独立行（全部计为已出）展示
        extra_tids = [t for t in self._ov_extra if t not in (r.get("cards") or {})]
        if extra_tids:
            r2 = dict(r)
            cards = dict(r.get("cards") or {})
            provs = dict(r.get("provs") or {})
            for tid in extra_tids:
                cards[tid] = self._ov_extra[tid]
                if not provs.get(tid):
                    c = self.db.by_id.get(tid) if self.db else None
                    provs[tid] = c["provision"] if c else 0
            r2["cards"], r2["provs"] = cards, provs
            self._ov_push(result=r2, played=played)
        else:
            self._ov_push(result=r, played=played)

    # ---------------- 设置 ----------------
    def on_settings(self):
        win = self._glass_toplevel("设置", "520x680")
        win.grab_set()
        bg = BG2 if not self._glass_ok else BG
        # 内容区可滚动：选项多时不至于把底部按钮挤出窗口外
        frm = self._scroll_frame(win, bg)
        inner = tk.Frame(frm, bg=bg)
        inner.pack(fill="both", expand=True, padx=18, pady=(12, 10))
        frm = inner

        def section(title):
            """分组：金色竖条标题 + 内容容器。"""
            head = tk.Frame(frm, bg=bg)
            head.pack(fill="x", pady=(12, 2))
            tk.Frame(head, width=3, height=14, bg=ACCENT).pack(side="left")
            tk.Label(head, text=title, bg=bg, fg=ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold")
                     ).pack(side="left", padx=(6, 0))
            body = tk.Frame(frm, bg=bg)
            body.pack(fill="x", padx=(9, 0), pady=(2, 0))
            return body

        def row(parent, label):
            r = tk.Frame(parent, bg=bg)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, bg=bg, fg=FG,
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            return r

        # —— 通用 ——
        g = section("通用")
        r_dir = row(g, "游戏目录")
        e_dir = self._entry(r_dir, width=240)
        e_dir.insert(0, self.cfg.get("game_dir", ""))
        e_dir.pack(side="left", padx=(10, 6))
        self._btn(r_dir, "浏览", lambda: self._pick_dir(e_dir)).pack(side="left")

        r_close = row(g, "关闭按钮")
        close_map = {"每次询问": None, "直接关闭程序": "close",
                     "最小化到任务栏": "minimize"}
        close_disp = {v: k for k, v in close_map.items()}
        var_close = tk.StringVar(value=close_disp.get(
            self.cfg.get("close_action"), "每次询问"))
        self._combo(r_close, var_close,
                    values=list(close_map.keys())).pack(side="left",
                                                        padx=(10, 0))

        r_theme = row(g, "界面主题")
        theme_keys = list(THEME_ORDER)
        theme_disp = [THEME_NAMES[k] for k in theme_keys]
        var_theme = tk.StringVar(
            value=THEME_NAMES.get(self._theme, "猎魔 HUD"))
        self._combo(r_theme, var_theme, values=theme_disp).pack(
            side="left", padx=(10, 0))

        # —— 对局与预测 ——
        p = section("对局与预测")
        vars_ = {}
        for label, key, lo, hi in (
                ("默认人口上限", "deck_limit", 60, 300),
                ("预测失败阈值（连续 N 张无匹配则停止提示）", "pred_fail_limit", 2, 60),
                ("卡组库获取数量（20-240，从服务端更新）", "deck_fetch_limit", 20, 240)):
            r_num = row(p, label)
            v = tk.StringVar(value=str(self.cfg.get(key,
                                                    DEFAULT_CONFIG[key])))
            vars_[key] = (v, lo, hi)
            self._entry(r_num, textvariable=v, width=70).pack(
                side="left", padx=(10, 0))
        var_dock = tk.BooleanVar(value=bool(self.cfg.get(
            "dock_to_game", DEFAULT_CONFIG["dock_to_game"])))
        self._switch(p, "主窗口自动停靠游戏右侧（游戏移动时跟随）",
                     var_dock).pack(anchor="w", pady=(6, 0))

        # —— 悬浮窗 ——
        o = section("悬浮窗")
        var_follow = tk.BooleanVar(value=bool(self.cfg.get(
            "pred_follow", DEFAULT_CONFIG["pred_follow"])))
        self._switch(o, "跟随游戏窗口移动（关闭后可拖到任意位置固定）",
                     var_follow).pack(anchor="w", pady=(0, 2))
        r_opa = row(o, "窗口透明度 (30%-100%)")
        var_opa = tk.DoubleVar(value=float(self.cfg.get("pred_opacity",
                                                        0.95)))

        def _opa_change(_v):
            v = float(var_opa.get())
            opa_lbl.config(text="%.0f%%" % (v * 100))
            # 实时预览（若悬浮窗存在）
            if self._ov is not None:
                try:
                    self._ov.set_opacity(v)
                except Exception:  # noqa: BLE001
                    pass

        opa_lbl = tk.Label(r_opa, text="%.0f%%" % (var_opa.get() * 100),
                           bg=bg, fg=FG_DIM)
        if ctk is not None:
            ctk.CTkSlider(r_opa, from_=0.3, to=1.0, variable=var_opa,
                          command=_opa_change, width=160, corner_radius=8,
                          progress_color=ACCENT, button_color=ACCENT2,
                          fg_color=BG3, bg_color=bg
                          ).pack(side="left", padx=(10, 8))
        else:
            ttk.Scale(r_opa, from_=0.3, to=1.0, variable=var_opa,
                      orient="horizontal", command=_opa_change, length=160
                      ).pack(side="left", padx=(10, 8))
        opa_lbl.pack(side="left")
        var_resize = tk.BooleanVar(value=bool(self.cfg.get(
            "pred_resizable", DEFAULT_CONFIG["pred_resizable"])))
        self._switch(o, "允许自由调整悬浮窗大小（拖右下角缩放）",
                     var_resize).pack(anchor="w", pady=(6, 0))
        r_sc = row(o, "悬浮窗大小 (70%-150%)")
        var_scale = tk.DoubleVar(value=float(self.cfg.get(
            "pred_scale", DEFAULT_CONFIG["pred_scale"])))
        sc_lbl = tk.Label(r_sc, text="%d%%" % (var_scale.get() * 100),
                          bg=bg, fg=FG_DIM)

        def _scale_change(_v):
            v = float(var_scale.get())
            sc_lbl.config(text="%d%%" % (v * 100))
            # 实时预览（若悬浮窗存在）
            if self._ov is not None:
                try:
                    self._ov.set_scale(v, save=False)
                except Exception:  # noqa: BLE001
                    pass

        if ctk is not None:
            ctk.CTkSlider(r_sc, from_=0.7, to=1.5, variable=var_scale,
                          command=_scale_change, width=160, corner_radius=8,
                          progress_color=ACCENT, button_color=ACCENT2,
                          fg_color=BG3, bg_color=bg
                          ).pack(side="left", padx=(10, 8))
        else:
            ttk.Scale(r_sc, from_=0.7, to=1.5, variable=var_scale,
                      orient="horizontal", command=_scale_change, length=160
                      ).pack(side="left", padx=(10, 8))
        sc_lbl.pack(side="left")

        # —— 全局热键 ——
        h = section("全局热键")
        tk.Label(h, text="点击输入框后按下想要的组合键（Ctrl/Alt/Shift + 字母、"
                         "数字或 F1-F12），保存后立即生效",
                 bg=bg, fg=FG_DIM, justify="left",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 0))
        hk_cfg = self.cfg.get("hotkeys") or {}
        hk_vars = {}

        def hk_row(key, label, default):
            r = row(h, label)
            var = tk.StringVar(
                value=(hk_cfg.get(key) or HOTKEY_DEFAULTS[key]).upper())
            hk_vars[key] = var
            e = self._entry(r, textvariable=var, width=130)
            e.pack(side="left", padx=(10, 0))

            def capture(ev):
                k = ev.keysym
                if k == "BackSpace":        # 清空重按
                    var.set("")
                    return "break"
                if k in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                         "Alt_L", "Alt_R", "Win_L", "Win_R",
                         "Meta_L", "Meta_R", "Escape"):
                    return "break"          # 只等主键
                mods = []
                if ev.state & 0x0004:
                    mods.append("Ctrl")
                if ev.state & 0x0001:
                    mods.append("Shift")
                if ev.state & 0x20000:
                    mods.append("Alt")
                if ev.state & 0x0008:
                    mods.append("Win")
                if not mods:
                    return "break"          # 必须带修饰键
                var.set("+".join(mods + [k if len(k) > 1 else k.upper()]))
                return "break"

            e.bind("<Key>", capture)

        hk_row("overlay", "悬浮窗显隐", "Ctrl+Alt+H")
        hk_row("main", "主窗口置前", "Ctrl+Alt+M")
        hk_row("details", "对局详情", "Ctrl+Alt+D")

        # —— 卡图资源 ——
        a = section("卡图资源")
        state = "已下载完整卡图资源" if self.cfg.get("art_full") \
            else "尚未下载完整卡图资源"
        tk.Label(a, text="%s。一次性下载卡组库覆盖的全部卡面原画，\n"
                         "之后悬浮窗与对局详情直接使用本地卡图，无需逐张联网加载。"
                  % state, bg=bg, fg=FG_DIM, justify="left",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 6))
        self._btn(a, "下载完整卡图资源",
                  lambda: (win.destroy(), self.on_download_all_art())
                  ).pack(anchor="w")

        def apply():
            for key, (v, lo, hi) in vars_.items():
                try:
                    val = int(v.get())
                except ValueError:
                    continue
                self.cfg[key] = max(lo, min(hi, val))
            self.cfg["dock_to_game"] = bool(var_dock.get())
            self.cfg["pred_follow"] = bool(var_follow.get())
            self.cfg["pred_opacity"] = max(0.3, min(1.0,
                                                    float(var_opa.get())))
            self.cfg["pred_resizable"] = bool(var_resize.get())
            self.cfg["pred_scale"] = max(0.7, min(1.5,
                                                  float(var_scale.get())))
            if self._ov is not None:
                try:
                    self._ov.set_resizable(self.cfg["pred_resizable"])
                except Exception:  # noqa: BLE001
                    pass
            self._dock_ok = self.cfg["dock_to_game"]
            self.cfg["close_action"] = close_map.get(var_close.get())
            self.cfg["game_dir"] = e_dir.get().strip().strip('"')
            # 全局热键：非法组合回退默认值，保存后重新注册立即生效
            hk = {}
            for key, var in hk_vars.items():
                hk[key] = var.get().upper() if parse_hotkey(var.get()) \
                    else HOTKEY_DEFAULTS[key]
            self.cfg["hotkeys"] = hk
            _start_hotkeys(self)
            save_config(self.cfg)
            self._log("设置已保存")
            disp = var_theme.get()
            new_theme = theme_keys[theme_disp.index(disp)] if disp in theme_disp \
                else self._theme
            win.destroy()
            if new_theme != self._theme:
                self.root.after(20, lambda: self._set_theme(new_theme))

        acts = tk.Frame(win, bg=bg)
        acts.pack(side="bottom", fill="x", padx=18, pady=(2, 12))
        self._btn(acts, "保存设置", apply, accent=True).pack(side="left")
        self._btn(acts, "取消", win.destroy).pack(side="left", padx=(8, 0))
        win.geometry("+%d+%d" % (self.root.winfo_x() + 100, self.root.winfo_y() + 50))

    def _pick_dir(self, entry):
        d = filedialog.askdirectory(parent=self.root, title="选择 GWENT 安装目录")
        if d:
            entry.delete(0, "end")
            entry.insert(0, d)

    def on_help(self):
        win = self._glass_toplevel("帮助", "440x430")
        txt = tk.Text(win, bg=BG2 if not self._glass_ok else BG, fg=FG, relief="flat",
                      font=("Microsoft YaHei UI", 9), wrap="word",
                      highlightthickness=0, padx=12, pady=10)
        txt.pack(fill="both", expand=True)
        txt.insert("end", HELP_TEXT)
        txt.configure(state="disabled")
        win.geometry("+%d+%d" % (self.root.winfo_x() + 110, self.root.winfo_y() + 60))

    def on_about(self):
        win = self._glass_toplevel("关于", "440x620")
        # 底部按钮行先 pack，永远可见
        acts = tk.Frame(win, bg=BG2 if not self._glass_ok else BG)
        acts.pack(side="bottom", fill="x", padx=12, pady=(0, 10))

        def open_site():
            import webbrowser
            webbrowser.open(DOWNLOAD_PAGE)

        self._btn(acts, "进入官网", open_site, accent=True).pack(side="right")
        self._btn(acts, "复制官网地址",
                  lambda: (self.root.clipboard_clear(),
                           self.root.clipboard_append(DOWNLOAD_PAGE),
                           self._log("官网地址已复制"))).pack(side="right",
                                                             padx=(0, 8))
        txt = tk.Text(win, bg=BG2 if not self._glass_ok else BG, fg=FG, relief="flat",
                      font=("Microsoft YaHei UI", 9), wrap="word",
                      highlightthickness=0, padx=12, pady=10)
        txt.pack(fill="both", expand=True)
        txt.insert("end", ABOUT_TEXT % APP_VERSION)
        txt.insert("end", "\n" + CHANGELOG)
        txt.configure(state="disabled")
        win.geometry("+%d+%d" % (self.root.winfo_x() + 110, self.root.winfo_y() + 60))

    # ---------------- 关闭 ----------------
    def _ask_close_action(self):
        """点关闭按钮时询问：关闭程序还是最小化到任务栏（可记住选择）。"""
        win = self._glass_toplevel("关闭 GwentTracker", None)
        win.grab_set()
        win.resizable(False, False)
        box = tk.Frame(win, bg=win.cget("bg"))
        box.pack(fill="both", expand=True, padx=26, pady=(18, 16))
        tk.Label(box, text="要关闭程序，还是最小化到任务栏？", bg=box["bg"],
                 fg=FG, font=("Microsoft YaHei UI", 10, "bold")).pack(
            anchor="w", pady=(0, 8))
        var_remember = tk.BooleanVar(value=False)
        self._check(box, "记住我的选择，不再询问",
                    var_remember, bg=box["bg"]).pack(anchor="w", pady=(0, 12))
        act = {"v": None}

        def pick(v):
            act["v"] = v
            if v in ("close", "minimize") and var_remember.get():
                self.cfg["close_action"] = v
                save_config(self.cfg)
            win.destroy()

        btns = tk.Frame(box, bg=box["bg"])
        btns.pack(fill="x")
        self._btn(btns, "关闭程序", lambda: pick("close"),
                  accent=True).pack(side="left")
        self._btn(btns, "最小化窗口", lambda: pick("minimize")
                  ).pack(side="left", padx=(8, 0))
        self._btn(btns, "取消", lambda: pick(None)).pack(side="right")
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.root.winfo_x() + 120,
                                 self.root.winfo_y() + 160))
        win.protocol("WM_DELETE_WINDOW", lambda: pick(None))
        self.root.wait_window(win)
        return act["v"]

    def _on_close(self):
        act = self.cfg.get("close_action")
        if act not in ("close", "minimize"):
            act = self._ask_close_action()
            if act not in ("close", "minimize"):
                return                     # 用户取消
        if act == "minimize":
            self.root.iconify()
            return
        if self.in_match:
            self._save_history()
        save_config(self.cfg)
        self.root.destroy()


class _UpdCancelled(Exception):
    """用户取消更新，用于中断下载流程。"""


def _cleanup_update_leftovers():
    """清理更新残留：旧版 exe 备份、.new 临时文件、旧更新器、字节码缓存。"""
    if not getattr(sys, "frozen", False):
        return
    exe = os.path.abspath(sys.executable)
    leftovers = [exe + ".old", exe + ".bak",
                 os.path.join(BASE_DIR, "update_new.exe"),
                 os.path.join(BASE_DIR, "_selfupdate.py"),   # 1.6.x 旧更新器
                 os.path.join(BASE_DIR, "updater.exe")]
    app_dir = os.path.join(BASE_DIR, "app")
    if os.path.isdir(app_dir):
        try:
            for name in os.listdir(app_dir):
                if name.endswith(".new"):
                    leftovers.append(os.path.join(app_dir, name))
        except OSError:
            pass
        leftovers.append(os.path.join(app_dir, "__pycache__"))
    for p in leftovers:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _start_hang_watchdog(root):
    """主线程卡死看门狗：UI 线程 >15s 无心跳时把全部线程栈 dump 到文件。

    AppHang（未响应→被用户关闭）曾表现为"记牌器界面全空后消失"，
    窗口化 exe 无控制台无日志，只能靠此机制留下现场。
    """
    try:
        import faulthandler
        hang_f = open(os.path.join(BASE_DIR, "hang_dump.log"), "a",
                      encoding="utf-8", buffering=1)
        state = {"last": time.time()}

        def ping():
            state["last"] = time.time()   # 只有主线程活着才会执行
            root.after(2000, ping)

        root.after(2000, ping)

        def watch():
            while True:
                time.sleep(5)
                if time.time() - state["last"] > 15:
                    try:
                        hang_f.write("\n=== UI 线程无响应 %s ===\n"
                                     % time.strftime("%Y-%m-%d %H:%M:%S"))
                        faulthandler.dump_traceback(file=hang_f)
                    except Exception:  # noqa: BLE001
                        pass
                    state["last"] = time.time()   # 每 15s 最多记一次现场

        threading.Thread(target=watch, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def main():
    global UI_SCALE
    _cleanup_update_leftovers()
    capture.ensure_dpi_aware()
    root = tk.Tk()
    try:
        UI_SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:  # noqa: BLE001
        pass
    # 更新重启后旧进程可能尚未退出，稍后再补删一次残留
    root.after(8000, _cleanup_update_leftovers)
    app = App(root)
    apply_window_icon(root)
    app.enable_glass()
    _install_crash_hooks(root)   # 崩溃收集：本地日志 + 反馈引导
    # 首次启动风险提示：未同意则退出
    if not app.confirm_risk():
        root.destroy()
        return
    _start_hotkeys(app)          # 全局热键（Ctrl+Alt+H/M/D）
    _start_hang_watchdog(root)
    root.mainloop()


if __name__ == "__main__":
    main()

