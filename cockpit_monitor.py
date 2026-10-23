#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cockpit Monitor — monitor leve de temperaturas e ventoinhas com
mostradores analógicos no estilo cockpit de avião.

Mede: CPU, GPU, RAM, SSD, HDD (°C) e RPM das ventoinhas.

  Interface : PySide6 (Qt) — desenho vetorial com anti-aliasing, 60 fps
  Windows   : LibreHardwareMonitorLib.dll via pythonnet (rode como administrador)
  Linux     : psutil (hwmon / lm-sensors) + nvidia-smi para GPUs NVIDIA

Ao minimizar, a janela some e mini-relógios aparecem na barra de tarefas,
ao lado do relógio do Windows. Clique neles para abrir o painel de novo.

Uso:
  python cockpit_monitor.py               # sensores reais
  python cockpit_monitor.py --demo        # valores simulados
  python cockpit_monitor.py --minimized   # já inicia nos mini-relógios
"""

import argparse
import getpass
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from functools import partial
from pathlib import Path
from xml.sax.saxutils import escape

from PySide6.QtCore import (QElapsedTimer, QEvent, QObject, QPointF, QRect, QRectF, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPalette, QPen, QPixmap, QPolygonF, QRadialGradient)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame, QGroupBox, QLineEdit,
                               QGridLayout, QHBoxLayout, QLabel, QLayout, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton, QSpinBox,
                               QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget)

IS_WIN = os.name == "nt"
APP_NAME = "CockpitMonitor"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "cockpit.log"


def log(msg):
    """Registro simples em %APPDATA%\\CockpitMonitor\\cockpit.log (para diagnóstico)."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 300_000:
            LOG_FILE.unlink()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------- sensores
# chave, título, mínimo, máximo da escala
TEMP_SPECS = [
    ("cpu", "CPU", 0, 120),
    ("gpu", "GPU", 0, 120),
    ("ram", "RAM", 0, 100),
    ("ssd", "SSD", 0, 100),
    ("hdd", "HDD", 0, 80),
]
FAN_MAX_RPM = 3000      # escala inicial; cresce sozinha se uma ventoinha passar disso
MAX_FANS = 10
COLUMNS = 5

MONO = "Consolas" if IS_WIN else "DejaVu Sans Mono"
SANS = "Segoe UI" if IS_WIN else "DejaVu Sans"
START, SWEEP = 225.0, 270.0   # arco de 270°, do canto inferior esquerdo ao inferior direito

# ---------------------------------------------------------------- cores e temas
COLOR_FIELDS = [
    ("needle_ok", "Ponteiro normal", "#efefe8"),
    ("needle_hot", "Ponteiro / alerta", "#ff3b30"),
    ("lcd_text", "Números dos visores", "#74d27e"),
    ("band_ok", "Faixa normal da escala", "#46574b"),
    ("band_hot", "Faixa de alerta da escala", "#b8372b"),
    ("text", "Números e títulos", "#e6e6df"),
    ("dim", "Textos secundários", "#7f878d"),
    ("face", "Fundo do mostrador", "#15181b"),
    ("bezel", "Aro metálico", "#5f666d"),
    ("panel", "Placa dos relógios", "#25292d"),
    ("lcd", "Visor digital", "#0a0d0b"),
    ("caution", "Luz MASTER CAUTION", "#d98a00"),
    ("background", "Fundo da janela", "#16191c"),
]
DEFAULT_COLORS = {k: v for k, _, v in COLOR_FIELDS}
THEME_REV = 3      # muda quando o tema padrão muda (reaplica as cores novas)
PRESETS = {
    "Grafite (padrão)": {},
    "Cockpit verde (clássico)": {
        "needle_ok": "#3cff6e", "lcd_text": "#3cff6e", "band_ok": "#1f7a3c", "band_hot": "#c42a20",
        "text": "#e3e8ee", "dim": "#6c7783", "face": "#11161a", "bezel": "#6d7884",
        "panel": "#1d2229", "lcd": "#010302", "background": "#0a0d10", "caution": "#ff3b30"},
    "Verde-oliva": {
        "needle_ok": "#e6d9a8", "lcd_text": "#e6d9a8", "band_ok": "#4f5a32", "band_hot": "#b0341f",
        "text": "#e3dcc0", "dim": "#8d8a6c", "face": "#171a12", "bezel": "#6b6f4e",
        "panel": "#2f3524", "lcd": "#0c0e08", "background": "#1b1f15", "caution": "#d9b300"},
    "Visão noturna": {
        "needle_ok": "#7dff9a", "lcd_text": "#7dff9a", "band_ok": "#1d4a2a", "band_hot": "#7a2a22",
        "text": "#8fe0a2", "dim": "#3f6e4c", "face": "#050806", "bezel": "#2a332d",
        "panel": "#0c110d", "lcd": "#020403", "background": "#060908"},
    "Âmbar noturno": {
        "needle_ok": "#ffb000", "lcd_text": "#ffb000", "band_ok": "#7a5200", "text": "#ffdf9e",
        "dim": "#8a7650", "bezel": "#7a6a55", "panel": "#201b14", "face": "#14110c",
        "background": "#0d0b08"},
}

DEFAULT_CONFIG = {
    "colors": dict(DEFAULT_COLORS),
    "warn": {"cpu": 85, "gpu": 83, "ram": 75, "ssd": 70, "hdd": 50},
    "fan_warn_pct": 90,
    "interval": 1.0,
    "needle_speed": 8.0,
    "gauge_size": 200,
    "always_on_top": False,
    "close_to_tray": False,
    "autostart": False,
    "start_minimized": True,
    "start_mode": "tray",
    "hide_missing": True,
    "fps_warn": 30,                 # FPS abaixo disso: ponteiro vermelho
    "fps_max": 240,                 # escala do relógio de FPS
    "tray_monitor": "",             # "" = monitor principal; senão o nome da tela (\\.\\DISPLAY2)           # esconde relógios de sensores que este PC não tem           # tray = abre só os mini-relógios | panel = abre o painel
    "autostart_path": "",
    "tray_enabled": True,
    "tray_items": ["cpu", "gpu", "ram", "ssd"],
    "tray_style": "dial_text",      # "dial_text" = relógio + texto | "dial" = só relógio
    "tray_size": 38,
    "tray_background": False,
    "tray_animation": True,
    "boot_style": "engine",         # engine = partida do motor | pop = simples | none
    "boot_when": "start",           # start = só ao abrir o programa | always = sempre que minimizar
    "boot_pace": 1.0,               # multiplicador da duração (maior = mais lento)
    "alert_glow": True,
    "notify": {
        "windows": True, "telegram": False, "tg_token": "", "tg_chat": "",
        "whatsapp": False, "wa_phone": "", "wa_key": "",
        "sustain": 5, "cooldown": 10, "recover": True,
    },
    "tray_offset": 0,
    "hide_fullscreen": True,
}

COL = dict(DEFAULT_COLORS)
THEME_VER = [0]
FX = {"glow": True, "pulse": 0.0}


def apply_theme(colors):
    COL.clear()
    COL.update(DEFAULT_COLORS)
    COL.update(colors or {})
    THEME_VER[0] += 1


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text("utf-8"))
        for k, v in data.items():
            if k in cfg:
                if isinstance(cfg[k], dict) and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        if "boot_style" not in data and data.get("tray_animation") is False:
            cfg["boot_style"] = "none"
        if "fps_warn" not in data and "fps" not in cfg["tray_items"]:   # versão nova: mostra o FPS
            cfg["tray_items"] = list(cfg["tray_items"]) + ["fps"]
        rev = data.get("theme_rev")
        if rev is None or rev < 2:                     # tema novo: aplica as cores padrão
            cfg["colors"] = dict(DEFAULT_COLORS)
        elif rev < 3 and cfg["colors"].get("lcd_text") == "#97b88a":
            cfg["colors"]["lcd_text"] = DEFAULT_COLORS["lcd_text"]   # verde mais forte
    except Exception:
        pass
    return cfg


def save_config(cfg):
    cfg["theme_rev"] = THEME_REV
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(CONFIG_FILE)
    except Exception:
        pass


def load_fonts():
    """Rajdhani (títulos) e Share Tech Mono (números) — fontes livres na pasta fonts."""
    global MONO, SANS
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "fonts"
    fams = set()
    for f in sorted(base.glob("*.ttf")):
        i = QFontDatabase.addApplicationFont(str(f))
        if i >= 0:
            fams.update(QFontDatabase.applicationFontFamilies(i))
    if "Share Tech Mono" in fams:
        MONO = "Share Tech Mono"
    if "Rajdhani" in fams:
        SANS = "Rajdhani"


def app_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def qc(color, alpha=255):
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def qfont(family, px, bold=False):
    f = QFont(family)
    f.setPixelSize(max(1, int(round(px))))
    f.setBold(bold and family != "Share Tech Mono")     # evita negrito "borrado" falso
    f.setHintingPreference(QFont.PreferDefaultHinting)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


# ============================================================== Windows (ctypes)
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _u32 = ctypes.windll.user32
    _sh32 = ctypes.windll.shell32

    class _APPBARDATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                    ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]          # MONITORINFOEXW

    _u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _u32.FindWindowW.restype = wintypes.HWND
    _u32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    _u32.FindWindowExW.restype = wintypes.HWND
    _u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.GetForegroundWindow.restype = wintypes.HWND
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _u32.MonitorFromWindow.restype = wintypes.HANDLE
    _u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
    _u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _sh32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(_APPBARDATA)]
    _sh32.SHAppBarMessage.restype = ctypes.c_size_t


def _monitor_of(hwnd):
    """(nome do monitor, retângulo físico) da janela."""
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    _u32.GetMonitorInfoW(_u32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
    return mi.szDevice, mi.rcMonitor


def _to_logical(screen, mon, r):
    """Converte um RECT físico (pixels reais) para coordenadas lógicas do Qt nessa tela."""
    dpr = screen.devicePixelRatio()
    g = screen.geometry()
    return QRect(round(g.x() + (r.left - mon.left) / dpr), round(g.y() + (r.top - mon.top) / dpr),
                 round((r.right - r.left) / dpr), round((r.bottom - r.top) / dpr))


def screen_by_name(name):
    for sc in QGuiApplication.screens():
        if sc.name() == name:
            return sc
    return QGuiApplication.primaryScreen()


def taskbar_info(screen=None):
    """(retângulo da barra em px lógicos, posição da bandeja/relógio, lado da tela)."""
    prim = QGuiApplication.primaryScreen()
    scr = screen or prim
    if IS_WIN:
        try:
            if scr is prim:
                tray = _u32.FindWindowW("Shell_TrayWnd", None)
                cls_children = ("TrayNotifyWnd",)
            else:                                   # barra de outro monitor
                tray, h = None, None
                while True:
                    h = _u32.FindWindowExW(None, h, "Shell_SecondaryTrayWnd", None)
                    if not h:
                        break
                    if _monitor_of(h)[0] == scr.name():
                        tray = h
                        break
                cls_children = ()
            if tray:
                dev, mon = _monitor_of(tray)
                rc = wintypes.RECT()
                _u32.GetWindowRect(tray, ctypes.byref(rc))
                tb = _to_logical(scr, mon, rc)
                if rc.right - rc.left >= rc.bottom - rc.top:
                    edge = "top" if rc.top <= mon.top + 2 else "bottom"
                else:
                    edge = "left" if rc.left <= mon.left + 2 else "right"
                near = None
                for cls in cls_children:
                    notify = _u32.FindWindowExW(tray, None, cls, None)
                    if notify:
                        nr = wintypes.RECT()
                        _u32.GetWindowRect(notify, ctypes.byref(nr))
                        if nr.right > nr.left:
                            lr = _to_logical(scr, mon, nr)
                            near = lr.left() if edge in ("top", "bottom") else lr.top()
                if near is None:                    # barra secundária: deixa espaço para o relógio
                    near = (tb.right() - 150) if edge in ("top", "bottom") else (tb.bottom() - 110)
                return tb, near, edge
        except Exception as e:
            log(f"taskbar_info: {e}")
    geo, av = scr.geometry(), scr.availableGeometry()
    h = geo.bottom() - av.bottom()
    if h < 24:
        h = 48
    return QRect(geo.left(), geo.bottom() - h + 1, geo.width(), h), geo.right() - 230, "bottom"


class ForegroundWatcher(QObject):
    """Avisa na hora em que outra janela (ex.: a barra de tarefas) vem para frente."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._hooks = []
        self._proc = None
        if not IS_WIN:
            return
        try:
            proto = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                       wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
            self._proc = proto(lambda *a: self.changed.emit())
            _u32.SetWinEventHook.restype = wintypes.HANDLE
            _u32.SetWinEventHook.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HANDLE, proto,
                                             wintypes.DWORD, wintypes.DWORD, wintypes.UINT]
            # EVENT_SYSTEM_FOREGROUND (3) e EVENT_OBJECT_REORDER (0x8004), WINEVENT_OUTOFCONTEXT
            for ev in (3, 0x8004):
                h = _u32.SetWinEventHook(ev, ev, None, self._proc, 0, 0, 0)
                if h:
                    self._hooks.append(h)
        except Exception as e:
            log(f"ForegroundWatcher: {e}")


def raise_topmost(widget):
    if IS_WIN:
        try:   # HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE|SWP_NOOWNERZORDER
            _u32.SetWindowPos(int(widget.winId()), wintypes.HWND(-1), 0, 0, 0, 0,
                              0x0001 | 0x0002 | 0x0010 | 0x0200)
        except Exception:
            pass
    else:
        widget.raise_()


SHELL_CLASSES = {
    "WorkerW", "Progman", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
    "ForegroundStaging", "LockScreenBackstopFrame", "TaskListThumbnailWnd", "Xaml_WindowedPopupClass",
    "ApplicationFrameWindow", "#32768", "tooltips_class32", "SysShadow",
}


def fullscreen_app_active(device=None):
    """True quando um jogo/vídeo está em tela cheia no primeiro plano."""
    if not IS_WIN:
        return False
    try:
        hwnd = _u32.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(128)
        _u32.GetClassNameW(hwnd, buf, 128)
        if buf.value in SHELL_CLASSES or buf.value.startswith(("Qt", "Windows.UI.")):
            return False
        rc = wintypes.RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(rc))
        dev, m = _monitor_of(hwnd)
        if device and dev != device:
            return False
        return rc.left <= m.left and rc.top <= m.top and rc.right >= m.right and rc.bottom >= m.bottom
    except Exception:
        return False


# ---------------------------------------------------------------- iniciar com o Windows
TASK_NAME = "CockpitMonitor"
TASK_REV = 2        # muda quando a tarefa precisa ser recriada (ex.: sem atraso, prioridade normal)


def _launch_command(minimized):
    flag = "--minimized" if minimized else ""
    if getattr(sys, "frozen", False):                      # .exe do PyInstaller
        exe = sys.executable
        return exe, flag, str(Path(exe).parent)
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")                      # sem janela de console
    script = Path(__file__).resolve()
    return str(pyw if pyw.exists() else py), f'"{script}" {flag}'.strip(), str(script.parent)


def set_autostart(enable, minimized=True):
    minimized = True   # ao iniciar com o Windows abre só os mini-relógios
    """Usa o Agendador de Tarefas (roda como administrador, sem pedir UAC no logon)."""
    if not IS_WIN:
        return False, "Iniciar com o sistema só está disponível no Windows."
    flags = 0x08000000   # CREATE_NO_WINDOW
    if not enable:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, creationflags=flags)
        return True, ""
    cmd, args, wd = _launch_command(minimized)
    user = f'{os.environ.get("USERDOMAIN", "")}\\{getpass.getuser()}'
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Inicia o Cockpit Monitor junto com o Windows</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(cmd)}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(wd)}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        xml_path = CONFIG_DIR / "autostart_task.xml"
        xml_path.write_text(xml, encoding="utf-16")
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
                           capture_output=True, text=True, errors="replace", creationflags=flags)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "").strip() or "schtasks falhou."
    except Exception as e:
        return False, str(e)


# ============================================================== modelo + animação
class Channel:
    """Um sensor: valor real, posição animada da seta e quem desenha ele."""

    def __init__(self, animator, key, title, unit, vmin, vmax, warn, major, minor,
                 label_div=1.0, auto_scale=False, warn_frac=None, short=None, is_fan=False,
                 is_fps=False):
        self.animator = animator
        self.key, self.title, self.unit = key, title, unit
        self.short = short or title
        self.vmin, self.vmax, self.warn = float(vmin), float(vmax), warn
        self.major, self.minor, self.label_div = major, minor, label_div
        self.auto_scale, self.warn_frac, self.is_fan = auto_scale, warn_frac, is_fan
        self.is_fps = is_fps      # FPS: o ruim é o valor baixo, e não gera alerta/pisca
        self.subtitle = ""        # nome do jogo (FPS)
        self.value = None
        self.x = self.target = self.vmin
        self.v = 0.0
        self.views = []
        self.appear = 1.0      # 0 = desligado, 1 = ligado (partida do motor)
        self.ever = False      # já teve alguma leitura?
        self.override = None   # posição forçada da seta durante a partida

    @property
    def hot(self):
        if self.is_fps:
            return False
        return self.value is not None and self.warn is not None and self.value >= self.warn

    def pos(self):
        return self.x if self.override is None else self.override

    @property
    def needle_hot(self):
        if self.is_fps:
            if self.override is not None or self.value is None or not self.warn:
                return False
            return self.x <= self.warn
        if self.override is not None:
            return self.warn is not None and self.override >= self.warn
        return self.value is not None and self.warn is not None and self.x >= self.warn

    @property
    def shown_hot(self):
        if self.is_fps:
            return self.needle_hot
        return self.needle_hot if self.override is not None else self.hot

    def frac(self, val):
        return (val - self.vmin) / (self.vmax - self.vmin)

    def text(self):
        v = self.override if self.override is not None else self.value
        if v is None:
            return "--" if self.is_fps else "N/D"
        return f"{v:.0f}" if (self.is_fan or self.is_fps) else f"{v:.0f}°C"

    def redraw(self):
        for w in self.views:
            if w.isVisible():
                if hasattr(w, "update_channel"):
                    w.update_channel(self)
                else:
                    w.update()

    def visible(self):
        return any(w.isVisible() for w in self.views)

    def set(self, value):
        if value is not None and self.auto_scale and value > self.vmax:
            self.vmax = math.ceil(value / 1000.0) * 1000.0
            self.major = 1000 if self.vmax <= 8000 else 2000
            if self.warn_frac:
                self.warn = self.vmax * self.warn_frac
        old_txt = self.text()
        if value is not None:
            self.ever = True
        self.value = value
        self.target = self.vmin if value is None else float(value)
        if self.override is None and not self.visible():
            self.x, self.v = self.target, 0.0      # ninguém está vendo: não anima
            return
        span = self.vmax - self.vmin
        if (self.override is None and abs(self.target - self.x) < span * 0.004
                and abs(self.v) < span * 0.01):
            # variação menor que ~0,5 °C: não vale animar, só atualiza o número se mudou
            if self.text() != old_txt:
                self.redraw()
            return
        self.redraw()
        self.animator.wake(self)

    def step(self, dt, w):
        """Mola criticamente amortecida: acelera e freia suave, sem passar do ponto."""
        self.v += (w * w * (self.target - self.x) - 2 * w * self.v) * dt
        self.x += self.v * dt
        span = self.vmax - self.vmin
        moving = not (abs(self.target - self.x) < span * 1e-3 and abs(self.v) < span * 6e-3)
        if not moving:
            self.x, self.v = self.target, 0.0
        self.redraw()
        return moving


class Animator(QObject):
    """Relógio único de ~60 fps; só roda enquanto alguma seta está em movimento."""

    def __init__(self, speed_fn):
        super().__init__()
        self.speed_fn = speed_fn
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()
        self.active = set()

    def wake(self, ch):
        self.active.add(ch)
        if not self.timer.isActive():
            self.clock.start()
            self.timer.start()

    def _tick(self):
        dt = min(0.05, self.clock.restart() / 1000.0)
        w = self.speed_fn()
        self.active = {c for c in self.active if c.step(dt, w)}
        if not self.active:
            self.timer.stop()


def _xy(deg, r):
    a = math.radians(deg)
    return QPointF(r * math.cos(a), -r * math.sin(a))


def _deg(ch, val):
    return START - SWEEP * max(-0.02, min(1.02, ch.frac(val)))


def _needle(p, ch, length, width, tail):
    """Ponteiro de instrumento: lança afiada, contrapeso escuro e sombra projetada."""
    if ch.value is None and ch.override is None:
        col = qc(COL["dim"])
    else:
        col = qc(COL["needle_hot"] if ch.needle_hot else COL["needle_ok"])
        if ch.override is None and ch.hot and FX["glow"]:
            dark = col.darker(330)
            a = FX["pulse"]
            col = QColor(int(dark.red() + (col.red() - dark.red()) * a),
                         int(dark.green() + (col.green() - dark.green()) * a),
                         int(dark.blue() + (col.blue() - dark.blue()) * a))
    w = width
    lance = QPolygonF([QPointF(0, -w), QPointF(length * 0.80, -w * 0.55), QPointF(length, 0),
                       QPointF(length * 0.80, w * 0.55), QPointF(0, w)])
    tailp = QPolygonF([QPointF(w * 0.5, -w * 0.85), QPointF(-tail * 0.7, -w * 0.75),
                       QPointF(-tail * 0.7, w * 0.75), QPointF(w * 0.5, w * 0.85)])
    ang = -_deg(ch, ch.pos())
    cw = QPointF(-tail * 0.78, 0)
    cr = w * 1.55
    # sombra (deslocada sempre para baixo/direita, como luz vinda de cima)
    p.save()
    p.translate(length * 0.025 + 1.2, length * 0.03 + 1.6)
    p.rotate(ang)
    p.setPen(Qt.NoPen)
    p.setBrush(qc("#000000", 115))
    p.drawPolygon(lance)
    p.drawPolygon(tailp)
    p.drawEllipse(cw, cr, cr)
    p.restore()
    # corpo
    p.save()
    p.rotate(ang)
    edge = QPen(qc("#000000", 210), max(0.5, w * 0.2))
    edge.setJoinStyle(Qt.MiterJoin)
    p.setPen(edge)
    tail_col = qc("#1d2125")
    p.setBrush(tail_col)
    p.drawPolygon(tailp)
    p.setBrush(col)
    p.drawPolygon(lance)
    # vinco central (dá o aspecto de ponteiro estampado)
    p.setPen(QPen(col.darker(125), max(0.4, w * 0.18), Qt.SolidLine, Qt.FlatCap))
    p.drawLine(QPointF(w * 2.2, 0), QPointF(length * 0.82, 0))
    # contrapeso
    p.setPen(QPen(qc("#000000", 220), max(0.5, w * 0.22)))
    p.setBrush(tail_col)
    p.drawEllipse(cw, cr, cr)
    p.setPen(QPen(QColor(COL["bezel"]).darker(140), max(0.4, w * 0.16)))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(cw, cr * 0.55, cr * 0.55)
    p.restore()


def _hub(p, r):
    b = QColor(COL["bezel"])
    p.setPen(QPen(qc("#000000"), r * 0.18))
    hg = QRadialGradient(-r * 0.35, -r * 0.35, r * 1.3)
    hg.setColorAt(0, b.lighter(120))
    hg.setColorAt(1, b.darker(300))
    p.setBrush(hg)
    p.drawEllipse(QPointF(0, 0), r, r)
    p.setPen(Qt.NoPen)
    p.setBrush(qc("#0d0f11"))
    p.drawEllipse(QPointF(0, 0), r * 0.62, r * 0.62)
    p.setBrush(b.darker(150))
    p.drawEllipse(QPointF(0, 0), r * 0.2, r * 0.2)


def _bands(p, ch, r, width):
    span = ch.vmax - ch.vmin
    fw = 1.0 if ch.warn is None else max(0.0, min(1.0, (ch.warn - ch.vmin) / span))
    box = QRectF(-r, -r, 2 * r, 2 * r)
    p.setBrush(Qt.NoBrush)
    if getattr(ch, "is_fps", False):          # FPS: vermelho no começo da escala
        if fw > 0:
            p.setPen(QPen(qc(COL["band_hot"], 235), width, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(box, int((START - SWEEP * fw) * 16), int(SWEEP * fw * 16))
        if fw < 1:
            p.setPen(QPen(qc(COL["band_ok"], 220), width, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(box, int((START - SWEEP) * 16), int(SWEEP * (1 - fw) * 16) + 1)
        return
    if fw > 0:
        p.setPen(QPen(qc(COL["band_ok"], 220), width, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(box, int((START - SWEEP * fw) * 16), int(SWEEP * fw * 16))
    if fw < 1:
        p.setPen(QPen(qc(COL["band_hot"], 235), width, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(box, int((START - SWEEP) * 16), int(SWEEP * (1 - fw) * 16) + 1)


def blink_red(min_alpha=0.12):
    """Vermelho piscando (acompanha o pulso do halo)."""
    c = qc(COL["needle_hot"])
    if FX["glow"]:
        a = FX["pulse"]
        c.setAlphaF(min_alpha + (1 - min_alpha) * a)
    return c


def _alert_glow(p, r_in, r_out):
    """Halo vermelho pulsante em volta do relógio."""
    a = FX["pulse"]
    red = COL["needle_hot"]
    k = r_in / r_out
    g = QRadialGradient(0, 0, r_out)
    g.setColorAt(0, qc(red, 0))
    g.setColorAt(max(0.0, k - 0.1), qc(red, 0))
    g.setColorAt(k, qc(red, int(120 + 135 * a)))
    g.setColorAt(1, qc(red, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(QPointF(0, 0), r_out, r_out)


def _mini_base(p, ch):
    b = QColor(COL["bezel"])
    lg = QLinearGradient(-35, -45, 35, 45)
    lg.setColorAt(0, b.lighter(110))
    lg.setColorAt(1, b.darker(420))
    p.setPen(Qt.NoPen)
    p.setBrush(lg)
    p.drawEllipse(QPointF(0, 0), 49, 49)
    f = QColor(COL["face"])
    fg = QRadialGradient(0, -12, 52)
    fg.setColorAt(0, f)
    fg.setColorAt(1, f.darker(600))
    p.setPen(QPen(qc("#000000"), 1))
    p.setBrush(fg)
    p.drawEllipse(QPointF(0, 0), 44, 44)
    _bands(p, ch, 38, 6)
    p.setPen(QPen(qc(COL["text"]), 2.6, Qt.SolidLine, Qt.FlatCap))
    n = int(round((ch.vmax - ch.vmin) / ch.major))
    for i in range(n + 1):
        d = _deg(ch, ch.vmin + i * ch.major)
        p.drawLine(_xy(d, 43), _xy(d, 34))


def _mini_top(p):
    _hub(p, 6.5)
    gl = QLinearGradient(0, -44, 0, -5)
    gl.setColorAt(0, qc("#ffffff", 14))
    gl.setColorAt(1, qc("#ffffff", 0))
    p.setPen(Qt.NoPen)
    p.setBrush(gl)
    p.drawEllipse(QRectF(-34, -42, 68, 40))


def _mini_text(p, ch):
    p.setPen(qc(COL["dim"]))
    p.setFont(qfont(SANS, 17, True))
    p.drawText(QRectF(-40, -30, 80, 20), Qt.AlignCenter, ch.short[:4].upper())
    shown = ch.override if ch.override is not None else ch.value
    p.setPen(qc(COL["dim"]) if shown is None else
             (blink_red() if ch.hot and ch.override is None else
              qc(COL["needle_hot"] if ch.shown_hot else COL["lcd_text"])))
    p.setFont(qfont(MONO, 21, True))
    p.drawText(QRectF(-40, 14, 80, 24), Qt.AlignCenter, "--" if shown is None else f"{shown:.0f}")


def draw_mini_dial(p, ch, show_text):
    """Mini-relógio em um espaço de 100×100 unidades (centro em 0,0). Sem cache."""
    if ch.hot and FX["glow"] and ch.override is None:
        _alert_glow(p, 46, 66)
    _mini_base(p, ch)
    if show_text:
        _mini_text(p, ch)
    _needle(p, ch, 38, 4.4, 12)
    _mini_top(p)


_MINI_CACHE = {}


def _mini_layers(ch, d, dpr):
    """Partes fixas do mini-relógio (aro, escala, eixo, vidro) prontas em imagem."""
    key = (round(d * dpr), ch.vmax, ch.warn, ch.major, THEME_VER[0])
    hit = _MINI_CACHE.get(ch.key)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    px = round(d * dpr)

    def layer(fn):
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        q.translate(px / 2, px / 2)
        q.scale(px / 100.0, px / 100.0)
        fn(q)
        q.end()
        pm.setDevicePixelRatio(dpr)
        return pm
    base, top = layer(lambda q: _mini_base(q, ch)), layer(_mini_top)
    _MINI_CACHE[ch.key] = (key, base, top)
    return base, top


def paint_mini_cached(p, ch, d, dpr, show_text):
    """Desenha o mini-relógio usando as camadas cacheadas (painter já centralizado)."""
    base, top = _mini_layers(ch, d, dpr)
    box = QRectF(-d / 2, -d / 2, d, d)
    if ch.hot and FX["glow"] and ch.override is None:
        p.save()
        p.scale(d / 100.0, d / 100.0)
        _alert_glow(p, 46, 66)
        p.restore()
    p.drawPixmap(box, base, QRectF(base.rect()))
    p.save()
    p.scale(d / 100.0, d / 100.0)
    if show_text:
        _mini_text(p, ch)
    _needle(p, ch, 38, 4.4, 12)
    p.restore()
    p.drawPixmap(box, top, QRectF(top.rect()))


# ============================================================== mostrador grande
class Gauge(QWidget):
    """Mostrador analógico vetorial. Desenhado num sistema de 200×200 unidades."""

    def __init__(self, ch, size):
        super().__init__()
        self.ch = ch
        ch.views.append(self)
        self._cache = None
        self.setFixedSize(size, size)

    def _begin(self, p):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing
                         | QPainter.SmoothPixmapTransform)
        s = min(self.width(), self.height()) / 200.0
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(s, s)

    def _background(self):
        ch = self.ch
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, ch.vmax, ch.warn, ch.major, THEME_VER[0])
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        pm = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        self._begin(p)
        self._paint_static(p)
        p.end()
        self._cache = (key, pm)
        return pm

    def _paint_static(self, p):
        ch = self.ch
        panel, bezel = QColor(COL["panel"]), QColor(COL["bezel"])
        # placa
        g = QLinearGradient(0, -100, 0, 100)
        g.setColorAt(0, panel)
        g.setColorAt(1, panel.darker(160))
        p.setPen(QPen(panel.lighter(140), 1.5))
        p.setBrush(g)
        p.drawRoundedRect(QRectF(-98, -98, 196, 196), 9, 9)
        # parafusos
        for sx, sy in ((-86, -86), (86, -86), (-86, 86), (86, 86)):
            rg = QRadialGradient(sx - 1.5, sy - 1.5, 6)
            rg.setColorAt(0, bezel.darker(110))
            rg.setColorAt(1, bezel.darker(380))
            p.setPen(QPen(qc("#07090b"), 0.8))
            p.setBrush(rg)
            p.drawEllipse(QPointF(sx, sy), 4.6, 4.6)
            p.setPen(QPen(qc("#0b0d10"), 1.4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(sx - 2.7, sy + 2.7), QPointF(sx + 2.7, sy - 2.7))
        # aro metálico
        lg = QLinearGradient(-70, -90, 70, 90)
        lg.setColorAt(0, bezel)
        lg.setColorAt(0.5, bezel.darker(260))
        lg.setColorAt(1, bezel.darker(600))
        p.setPen(Qt.NoPen)
        p.setBrush(lg)
        p.drawEllipse(QPointF(0, 0), 93, 93)
        lg2 = QLinearGradient(70, 90, -70, -90)
        lg2.setColorAt(0, bezel.darker(120))
        lg2.setColorAt(1, bezel.darker(480))
        p.setBrush(lg2)
        p.drawEllipse(QPointF(0, 0), 88.5, 88.5)
        # mostrador
        face = QColor(COL["face"])
        fg = QRadialGradient(0, -20, 95)
        fg.setColorAt(0, face)
        fg.setColorAt(1, face.darker(600))
        p.setPen(QPen(qc("#000000"), 1.2))
        p.setBrush(fg)
        p.drawEllipse(QPointF(0, 0), 85, 85)
        _bands(p, ch, 75.0, 6.5)
        # escala
        n = int(round((ch.vmax - ch.vmin) / ch.major))
        major_pen = QPen(qc(COL["text"]), 2.6, Qt.SolidLine, Qt.FlatCap)
        minor_pen = QPen(QColor(COL["text"]).darker(135), 1.3, Qt.SolidLine, Qt.FlatCap)
        p.setFont(qfont(SANS, 14.5, True))
        for i in range(n + 1):
            v = ch.vmin + i * ch.major
            d = _deg(ch, v)
            p.setPen(major_pen)
            p.drawLine(_xy(d, 81), _xy(d, 66))
            c = _xy(d, 53)
            p.setPen(qc(COL["text"]))
            p.drawText(QRectF(c.x() - 18, c.y() - 9, 36, 18), Qt.AlignCenter,
                       f"{v / ch.label_div:g}")
            if i < n:
                p.setPen(minor_pen)
                for j in range(1, ch.minor):
                    d2 = _deg(ch, v + j * ch.major / ch.minor)
                    p.drawLine(_xy(d2, 81), _xy(d2, 73))
        # textos
        p.setPen(qc(COL["dim"]))
        p.setFont(qfont(SANS, 10.5, True))
        p.drawText(QRectF(-40, -38, 80, 14), Qt.AlignCenter, ch.unit)
        p.setPen(qc(COL["text"]))
        p.setFont(qfont(SANS, 12.5, True))
        p.drawText(QRectF(-48, 19, 96, 18), Qt.AlignCenter, ch.title)
        # visor digital
        p.setPen(QPen(panel.lighter(160), 1))
        p.setBrush(qc(COL["lcd"]))
        p.drawRoundedRect(QRectF(-25, 42, 50, 20), 3, 3)

    def paintEvent(self, _event):
        ch = self.ch
        p = QPainter(self)
        p.drawPixmap(0, 0, self._background())
        self._begin(p)
        if ch.hot and FX["glow"] and ch.override is None:
            a = FX["pulse"]
            red = COL["needle_hot"]
            p.setBrush(Qt.NoBrush)
            for w, k in ((8, 0.25), (3, 0.95)):
                p.setPen(QPen(qc(red, int((60 + 195 * a) * k)), w))
                p.drawRoundedRect(QRectF(-97, -97, 194, 194), 9, 9)
            p.setPen(QPen(qc(red, int(50 + 170 * a)), 3))
            p.drawEllipse(QPointF(0, 0), 92, 92)
        if ch.override is not None:
            lcd_col = qc(COL["needle_hot"] if ch.needle_hot else COL["lcd_text"])
            txt = f"{ch.override:.0f}"
        elif ch.value is None:
            lcd_col, txt = qc(COL["dim"]), "N/D"
        else:
            lcd_col = (blink_red() if ch.hot else
                       qc(COL["needle_hot"] if ch.is_fps and ch.needle_hot else COL["lcd_text"]))
            txt = f"{ch.value:.0f}"
        if ch.appear < 1:
            lcd_col.setAlphaF(ch.appear)
        p.setPen(lcd_col)
        p.setFont(qfont(MONO, 17))
        p.drawText(QRectF(-25, 42, 50, 20), Qt.AlignCenter, txt)
        _needle(p, ch, 73, 3.7, 22)
        _hub(p, 8)
        if ch.is_fps and ch.subtitle:
            p.setPen(qc(COL["dim"]))
            p.setFont(qfont(SANS, 9.5, True))
            p.drawText(QRectF(-45, 63, 90, 14), Qt.AlignCenter, ch.subtitle[:18])
        if ch.appear < 1:     # painel "desligado" antes da partida
            p.setPen(Qt.NoPen)
            p.setBrush(qc("#000000", int(170 * (1 - ch.appear))))
            p.drawEllipse(QPointF(0, 0), 85, 85)
        gl = QLinearGradient(0, -85, 0, -10)
        gl.setColorAt(0, qc("#ffffff", 11))
        gl.setColorAt(1, qc("#ffffff", 0))
        p.setBrush(gl)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(-66, -82, 132, 76))
        p.end()


# ============================================================== leitura de sensores
def _prefer(items, prefs):
    """items = [(nome, valor)]; devolve o sensor preferido ou o mais quente."""
    if not items:
        return None
    low = [(n.lower(), v) for n, v in items]
    for p in prefs:
        for n, v in low:
            if n == p or n.startswith(p):
                return v
    return max(v for _, v in low)


class DemoBackend:
    name = "DEMO — valores simulados"

    def read(self):
        t = time.time()

        def w(base, amp, per):
            return base + amp * math.sin(t / per)

        return {
            "cpu": w(68, 24, 7), "gpu": w(58, 26, 9), "ram": w(46, 8, 11),
            "ssd": w(44, 10, 13), "hdd": w(36, 5, 17),
            "fans": [("cpu", "CPU Fan", w(1500, 700, 6)),
                     ("case1", "Case Fan 1", w(900, 250, 10)),
                     ("gpu", "GPU Fan", w(1900, 900, 8))],
        }

    def close(self):
        pass


class NvidiaSmiBackend:
    """Plano B no Windows: só a GPU NVIDIA, pelo nvidia-smi que vem com o driver."""
    name = "nvidia-smi (somente GPU)"

    def __init__(self):
        self.exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
        self.ok = Path(self.exe).exists()

    def read(self):
        try:
            out = subprocess.run([self.exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3,
                                 creationflags=0x08000000 if IS_WIN else 0).stdout
            vals = [float(x) for x in out.split()]
            return {"gpu": max(vals) if vals else None, "fans": []}
        except Exception:
            return {}

    def close(self):
        pass


class NullBackend:
    name = "sem sensores"

    def read(self):
        return {}

    def close(self):
        pass


class LHMBackend:
    """Windows: lê os sensores pela LibreHardwareMonitorLib.dll (via pythonnet)."""
    name = "LibreHardwareMonitor"
    GPU_KINDS = ("GpuNvidia", "GpuAmd", "GpuIntel")

    def __init__(self):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        dll_dir = next((d for d in (base / "lib", base)
                        if (d / "LibreHardwareMonitorLib.dll").exists()), None)
        if dll_dir is None:
            raise RuntimeError("LibreHardwareMonitorLib.dll não encontrada na pasta 'lib' (veja o README).")
        try:
            import pythonnet
            pythonnet.load("netfx")
        except Exception as e:          # já carregado ou versão antiga do pythonnet
            log(f"pythonnet.load: {e}")
        import clr  # pythonnet
        sys.path.append(str(dll_dir))
        try:
            clr.AddReference(str(dll_dir / "LibreHardwareMonitorLib.dll"))
        except Exception:
            clr.AddReference("LibreHardwareMonitorLib")
        from LibreHardwareMonitor.Hardware import Computer
        pc = Computer()
        pc.IsCpuEnabled = True
        pc.IsGpuEnabled = True
        pc.IsStorageEnabled = True
        pc.IsMemoryEnabled = True
        pc.IsMotherboardEnabled = True
        pc.IsControllerEnabled = True
        pc.Open()
        self.pc = pc
        self._disk_types = None
        found = []
        for top in pc.Hardware:
            found.append(f"{top.HardwareType}:{top.Name}")
        log("LHM ok, dll em " + str(dll_dir) + " | " + "; ".join(found))

    @staticmethod
    def _walk(hw):
        yield hw
        for sub in hw.SubHardware:
            yield from LHMBackend._walk(sub)

    @staticmethod
    def _is_ssd(hw):
        t = hw.GetType().Name.lower()
        n = str(hw.Name).lower()
        return any(k in t or k in n for k in ("nvme", "ssd"))

    @staticmethod
    def _norm(name):
        return re.sub(r"[^a-z0-9]", "", str(name).lower())

    def _load_disk_types(self):
        """Pergunta ao Windows o tipo de cada disco (SSD/HDD) — uma vez só."""
        self._disk_types, self._no_hdd = {}, False
        try:
            data = self._disks_wmi()
            if data is not None:
                self._apply_disks(data)
                return
        except Exception as e:
            log(f"WMI discos falhou: {e}")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,SpindleSpeed | "
                 "ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout
            data = json.loads(out) if out.strip() else []
            if isinstance(data, dict):
                data = [data]
            self._apply_disks(data)
        except Exception as e:
            log(f"Get-PhysicalDisk falhou: {e}")

    @staticmethod
    def _disks_wmi():
        import clr
        clr.AddReference("System.Management")
        from System.Management import ManagementObjectSearcher
        q = ManagementObjectSearcher(r"root\Microsoft\Windows\Storage",
                                     "SELECT FriendlyName, MediaType, SpindleSpeed FROM MSFT_PhysicalDisk")
        out = []
        for d in q.Get():
            def val(k):
                v = d[k]
                return None if v is None else (str(v) if k == "FriendlyName" else int(v))
            out.append({"FriendlyName": val("FriendlyName"), "MediaType": val("MediaType"),
                        "SpindleSpeed": val("SpindleSpeed")})
        return out

    def _apply_disks(self, data):
        hdds = 0
        for d in data:
            mt = str(d.get("MediaType"))
            spin = d.get("SpindleSpeed")
            is_hdd = mt in ("3", "HDD") or (isinstance(spin, int) and 0 < spin < 4294967295)
            is_ssd = mt in ("4", "SSD", "5", "SCM") or spin == 0
            kind = "hdd" if is_hdd else ("ssd" if is_ssd else None)
            self._disk_types[self._norm(d.get("FriendlyName") or "")] = kind
            hdds += is_hdd
        self._no_hdd = bool(data) and hdds == 0
        log(f"Discos (Windows): {data} -> sem HDD={self._no_hdd}")

    def _storage_kind(self, hw):
        if self._disk_types is None:
            self._load_disk_types()
        if self._no_hdd:
            return "ssd"
        n = self._norm(hw.Name)
        for name, kind in self._disk_types.items():
            if kind and name and (name in n or n in name):
                return kind
        return "ssd" if self._is_ssd(hw) else "hdd"

    def read(self):
        cpu, gpu, ram, ssd, hdd, fans = [], [], [], [], [], []
        for top in self.pc.Hardware:
            kind = str(top.HardwareType)
            for h in self._walk(top):
                h.Update()
                for s in h.Sensors:
                    v = s.Value
                    if v is None:
                        continue
                    v, st, n = float(v), str(s.SensorType), str(s.Name)
                    if st == "Fan":
                        fans.append((f"{h.Name}/{n}", n, v))
                    elif st == "Temperature":
                        if kind == "Cpu":
                            cpu.append((n, v))
                        elif kind in self.GPU_KINDS:
                            gpu.append((n, v))
                        elif kind == "Memory":
                            ram.append(v)
                        elif kind == "Storage":
                            (ssd if self._storage_kind(top) == "ssd" else hdd).append(v)
        return {
            "cpu": _prefer(cpu, ("core (tctl/tdie)", "cpu package", "package", "tctl", "tdie", "core average")),
            "gpu": _prefer(gpu, ("gpu core", "core")),
            "ram": max(ram, default=None),
            "ssd": max(ssd, default=None),
            "hdd": max(hdd, default=None),
            "fans": fans,
        }

    def close(self):
        try:
            self.pc.Close()
        except Exception:
            pass


class PsutilBackend:
    """Linux: lê hwmon via psutil; GPU NVIDIA via nvidia-smi."""
    name = "psutil / hwmon"

    def __init__(self):
        import psutil
        self.ps = psutil
        self.nvsmi = shutil.which("nvidia-smi")

    def _nvidia(self):
        if not self.nvsmi:
            return None
        try:
            out = subprocess.run([self.nvsmi, "--query-gpu=temperature.gpu",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=2).stdout
            vals = [float(x) for x in out.split()]
            return max(vals) if vals else None
        except Exception:
            return None

    def read(self):
        T = self.ps.sensors_temperatures() or {}

        def group(keys, prefs=()):
            for k in keys:
                ents = T.get(k)
                if ents:
                    return _prefer([(e.label or k, e.current) for e in ents], prefs)
            return None

        def hottest(keys, labels=None):
            vals = [e.current for k in keys for e in T.get(k, [])
                    if labels is None or (e.label or "").lower() in labels]
            return max(vals, default=None)

        fans = []
        for chip, ents in (self.ps.sensors_fans() or {}).items():
            for i, e in enumerate(ents):
                title = e.label or f"{chip} {i + 1}"
                fans.append((f"{chip}/{i}", title, float(e.current)))

        return {
            "cpu": group(("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz"),
                         ("package", "tctl", "tdie")),
            "gpu": group(("amdgpu", "nouveau", "radeon", "i915", "xe"), ("edge", "junction"))
                   or self._nvidia(),
            "ram": hottest(("spd5118", "jc42")),
            "ssd": hottest(("nvme",), {"composite", ""}) or hottest(("nvme",)),
            "hdd": hottest(("drivetemp",)),
            "fans": fans,
        }

    def close(self):
        pass


def make_backend(demo):
    if demo:
        return DemoBackend(), None
    if IS_WIN:
        warn = None
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                warn = "Sem privilégios de administrador: CPU/ventoinhas podem aparecer como N/D."
        except Exception:
            pass
        try:
            return LHMBackend(), warn
        except Exception as e:
            import traceback
            log("Falha ao iniciar LHM: " + traceback.format_exc())
            fb = NvidiaSmiBackend()
            msg = (f"Sensores não carregaram ({type(e).__name__}: {str(e)[:120]}). "
                   f"Detalhes em {LOG_FILE}")
            return (fb if fb.ok else NullBackend()), msg
    try:
        b = PsutilBackend()
        if not hasattr(b.ps, "sensors_temperatures"):
            return NullBackend(), "Este sistema não expõe sensores pelo psutil."
        return b, None
    except Exception as e:
        return NullBackend(), f"Erro ao iniciar sensores: {e}"


# ============================================================== FPS do jogo (PresentMon)
def foreground_pid():
    if not IS_WIN:
        return None
    try:
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(_u32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value
    except Exception:
        return None


class FpsMonitor:
    """Lê o tempo de cada quadro de todos os jogos via PresentMon (Intel, código aberto)
    e calcula o FPS do jogo em primeiro plano — sem injetar nada no jogo."""
    SESSION = "CockpitFPS"
    IGNORE = {"dwm.exe", "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
              "shellexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe",
              "cockpitmonitor.exe", "python.exe", "pythonw.exe"}

    def __init__(self):
        base = app_dir()
        self.exe = next((str(f) for f in (base / "tools" / "PresentMon.exe", base / "PresentMon.exe")
                         if f.exists()), None)
        self.ok = IS_WIN and self.exe is not None
        self.lock = threading.Lock()
        self.frames, self.names, self.last_seen = {}, {}, {}
        self.game = None
        self.proc = None
        self.stop = threading.Event()

    def start(self):
        if self.ok:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        args = [self.exe, "--output_stdout", "--no_console_stats", "--session_name", self.SESSION,
                "--stop_existing_session"]
        for name in ("dwm.exe", "explorer.exe"):
            args += ["--exclude", name]
        while not self.stop.is_set():
            try:
                self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                             creationflags=0x08000000 if IS_WIN else 0)
                header = None
                for line in self.proc.stdout:
                    if self.stop.is_set():
                        break
                    parts = line.rstrip("\r\n").split(",")
                    if header is None:
                        if "Application" in parts and "ProcessID" in parts:
                            ia, ip = parts.index("Application"), parts.index("ProcessID")
                            im = next((parts.index(c) for c in ("MsBetweenPresents", "msBetweenPresents",
                                                                 "FrameTime") if c in parts), None)
                            header = im is not None
                            log(f"PresentMon ok, colunas: {parts[:6]}...")
                        continue
                    try:
                        pid, ms = int(parts[ip]), float(parts[im])
                    except (ValueError, IndexError):
                        continue
                    if not 0 < ms < 2000:
                        continue
                    with self.lock:
                        dq = self.frames.get(pid)
                        if dq is None:
                            dq = self.frames[pid] = deque(maxlen=600)
                            self.names[pid] = parts[ia]
                        dq.append(ms)
                        self.last_seen[pid] = time.monotonic()
                code = self.proc.wait(timeout=5)
                log(f"PresentMon terminou (código {code})")
            except Exception as e:
                log(f"PresentMon erro: {e}")
            self.stop.wait(5)                   # tenta de novo se cair

    @staticmethod
    def _fps(dq):
        total, n = 0.0, 0
        for ms in reversed(dq):                 # últimos ~1 s de quadros
            total += ms
            n += 1
            if total >= 1000:
                break
        return 1000.0 * n / total if total > 0 and n >= 2 else None

    def current(self):
        """(fps, nome do jogo) ou (None, "")."""
        now = time.monotonic()
        fg = foreground_pid()
        with self.lock:
            for pid in [p for p, t in self.last_seen.items() if now - t > 3]:
                self.frames.pop(pid, None)
                self.last_seen.pop(pid, None)
            alive = {p for p in self.frames if self.names.get(p, "").lower() not in self.IGNORE}
            # troca de jogo só quando a janela da frente é tela cheia (ou não há jogo ainda)
            if fg in alive and (self.game not in alive or (fg != self.game and fullscreen_app_active())):
                self.game = fg
            if self.game not in alive:
                self.game = None
                return None, ""
            fps = self._fps(self.frames[self.game])
            name = self.names.get(self.game, "")
        return fps, re.sub(r"\.exe$", "", name, flags=re.I)

    def shutdown(self):
        self.stop.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            subprocess.run([self.exe, "--terminate_existing_session", "--session_name", self.SESSION],
                           capture_output=True, timeout=5, creationflags=0x08000000)
        except Exception:
            pass


# ============================================================== mini-relógios na barra
def _ease_out_back(u):
    c1 = 1.55
    c3 = c1 + 1
    return 1 + c3 * (u - 1) ** 3 + c1 * (u - 1) ** 2


def _smooth(u):
    return u * u * (3 - 2 * u)


class TaskbarDials(QWidget):
    """Janela transparente sobre a barra de tarefas, ao lado da bandeja do relógio."""
    STAGGER, DUR_IN = 0.22, 0.6           # entrada simples: um relógio por vez
    STAGGER_OUT, DUR_OUT = 0.035, 0.18    # saída rápida

    def __init__(self, ctrl):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.ctrl = ctrl
        self.items = []
        self.phase = "hidden"            # hidden | in | shown | out
        self.vertical = False
        self.suspended = False           # escondido por app em tela cheia
        self.hover = False
        self.drag = None
        self.clock = QElapsedTimer()
        self.frame = QTimer(self)
        self.frame.setTimerType(Qt.PreciseTimer)
        self.frame.setInterval(16)
        self.frame.timeout.connect(self._frame)
        self.fs_count = 0
        self.keeper = QTimer(self)
        self.keeper.setInterval(350)
        self.keeper.timeout.connect(self._keep)

    # ---- itens e geometria
    def refresh_items(self):
        keys = self.ctrl.cfg["tray_items"]
        chans = self.ctrl.channels
        self.items = [ch for k, ch in chans.items()
                      if k in keys and k != "fps" and not self.ctrl.is_missing(ch)]
        if "fps" in keys and "fps" in chans and self.ctrl.fps_active:
            self.items.insert(0, chans["fps"])     # FPS sempre o primeiro (ponta esquerda)
        for ch in self.ctrl.channels.values():      # só redesenha a barra pelos itens dela
            if ch in self.items and self not in ch.views:
                ch.views.append(self)
            elif ch not in self.items and self in ch.views:
                ch.views.remove(self)
        if self.isVisible():
            self.reposition()
        self.update()

    def _metrics(self, thickness):
        cfg = self.ctrl.cfg
        d = max(20, min(int(cfg["tray_size"]), thickness - 6))
        text = cfg["tray_style"] == "dial_text" and not self.vertical
        tile = d + (int(d * 1.2) + 6 if text else 0)
        return d, tile, 6, text

    def reposition(self):
        tb, near, edge = taskbar_info(self.ctrl.target_screen())
        self.vertical = edge in ("left", "right")
        thick = tb.width() if self.vertical else tb.height()
        d, tile, gap, _ = self._metrics(thick)
        n = max(1, len(self.items))
        length = 8 + n * tile + (n - 1) * gap
        off = int(self.ctrl.cfg.get("tray_offset", 0))
        if self.vertical:
            end = near if near is not None else tb.bottom() - 220
            y = max(tb.top(), min(end - length - 6 + off, tb.bottom() - length))
            rect = QRect(tb.left(), y, tb.width(), length)
        else:
            end = near if near is not None else tb.right() - 260
            x = max(tb.left(), min(end - length - 6 + off, tb.right() - length))
            rect = QRect(x, tb.top(), length, tb.height())
        if rect != self.geometry():
            self.setGeometry(rect)

    def _slots(self):
        thick = self.width() if self.vertical else self.height()
        d, tile, gap, text = self._metrics(thick)
        out = []
        for i in range(len(self.items)):
            a = 4 + i * (tile + gap)
            if self.vertical:
                out.append((QPointF(thick / 2, a + d / 2), d, text))
            else:
                out.append((QPointF(a + d / 2, thick / 2), d, text))
        return out

    # ---- animação de entrada / saída
    def show_animated(self, boot=False):
        if self.phase in ("in", "shown") and self.isVisible():
            return
        self.reposition()
        style = self.ctrl.cfg["boot_style"]
        if boot and style == "engine":
            self.phase = "shown"
            self.ctrl.boot.start(self.items)
        else:
            self.phase = "in" if style != "none" else "shown"
        self.clock.start()
        self.show()
        raise_topmost(self)
        self.frame.start()
        self.keeper.start()

    def hide_animated(self):
        self.suspended = False
        if self.phase == "hidden":
            return
        if self.ctrl.cfg["boot_style"] == "none" or not self.isVisible():
            self.hide_now()
            return
        self.phase = "out"
        self.clock.start()
        self.frame.start()

    def hide_now(self):
        self.phase = "hidden"
        self.frame.stop()
        self.keeper.stop()
        self.hide()

    def _progress(self, i):
        n = len(self.items)
        t = self.clock.elapsed() / 1000.0
        if self.phase == "shown":
            return self.items[i].appear
        if self.phase == "in":
            u = (t - i * self.STAGGER) / self.DUR_IN
            return 0.0 if u <= 0 else _ease_out_back(min(1.0, u))
        if self.phase == "out":
            u = (t - (n - 1 - i) * self.STAGGER_OUT) / self.DUR_OUT
            return 1.0 - _smooth(max(0.0, min(1.0, u)))
        return 0.0

    def _frame(self):
        n = max(1, len(self.items))
        t = self.clock.elapsed() / 1000.0
        if self.phase == "in" and t > (n - 1) * self.STAGGER + self.DUR_IN:
            self.phase = "shown"
            self.frame.stop()
        elif self.phase == "out" and t > (n - 1) * self.STAGGER_OUT + self.DUR_OUT:
            self.hide_now()
            return
        elif self.phase == "shown":
            self.frame.stop()
        self.update()

    def _keep(self):
        if self.phase not in ("in", "shown"):
            return
        fs_now = (self.ctrl.cfg["hide_fullscreen"]
                  and fullscreen_app_active(self.ctrl.target_screen().name()))
        self.fs_count = self.fs_count + 1 if fs_now else 0
        fs = self.fs_count >= 3              # só esconde se ficar em tela cheia por ~1 s
        if fs and not self.suspended:
            self.suspended = True
            self.hide()
        elif not fs and self.suspended:
            self.suspended = False            # volta na hora, sem repetir a animação
            self.reposition()
            self.show()
            raise_topmost(self)
        elif not fs and self.drag is None:
            self.reposition()
            raise_topmost(self)

    def raise_now(self):
        if self.isVisible() and not self.suspended and self.drag is None:
            raise_topmost(self)

    # ---- desenho
    def _tile_rect(self, i):
        thick = self.width() if self.vertical else self.height()
        d, tile, gap, _ = self._metrics(thick)
        a = 4 + i * (tile + gap)
        if self.vertical:
            return QRect(0, a - 8, self.width(), tile + 16)
        return QRect(a - 8, 0, tile + 16, self.height())

    def update_channel(self, ch):
        """Redesenha só o pedaço do relógio que mudou (economiza CPU)."""
        if self.phase != "shown" or self.hover or ch not in self.items:
            self.update()
        else:
            self.update(self._tile_rect(self.items.index(ch)))

    def paintEvent(self, e):
        dirty = e.rect()
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))      # área clicável
        r = QRectF(self.rect()).adjusted(1, 4, -1, -4)
        if self.ctrl.cfg["tray_background"]:
            p.setPen(QPen(qc(COL["panel"]).lighter(150), 1))
            p.setBrush(qc(COL["background"], 225))
            p.drawRoundedRect(r, 7, 7)
        if self.hover:
            p.setPen(Qt.NoPen)
            p.setBrush(qc("#ffffff", 18))
            p.drawRoundedRect(r, 7, 7)
        for i, (ch, (c, d, text)) in enumerate(zip(self.items, self._slots())):
            if not dirty.intersects(self._tile_rect(i)):
                continue
            pr = self._progress(i)
            if pr <= 0.001:
                continue
            if ch.hot and FX["glow"] and ch.override is None and pr > 0.99:
                thick = self.width() if self.vertical else self.height()
                tile = self._metrics(thick)[1]
                if self.vertical:
                    rr = QRectF(2, c.y() - d / 2 - 3, thick - 4, tile + 6)
                else:
                    rr = QRectF(c.x() - d / 2 - 4, 3, tile + 8, thick - 6)
                p.setPen(QPen(qc(COL["needle_hot"], int(60 + 160 * FX["pulse"])), 1.2))
                p.setBrush(qc(COL["needle_hot"], int(20 + 70 * FX["pulse"])))
                p.drawRoundedRect(rr, 6, 6)
            p.save()
            p.setOpacity(max(0.0, min(1.0, pr)))
            rise = (1 - min(1.0, pr)) * d * 0.55
            p.translate(c.x() + (rise if self.vertical else 0), c.y() + (0 if self.vertical else rise))
            s = 0.55 + 0.45 * pr
            p.scale(s, s)
            paint_mini_cached(p, ch, d, self.devicePixelRatioF(), show_text=not text)
            if text:
                x0 = d / 2 + 6
                p.setPen(qc(COL["dim"]).lighter(125))
                p.setFont(qfont(SANS, max(9, d * 0.27), True))
                p.drawText(QRectF(x0, -d / 2, d * 1.6, d / 2), Qt.AlignLeft | Qt.AlignBottom,
                           ch.short[:9])
                col = (COL["dim"] if ch.value is None and ch.override is None else
                       COL["needle_hot"] if ch.shown_hot else COL["lcd_text"])
                p.setPen(blink_red() if ch.hot and ch.override is None else qc(col))
                p.setFont(qfont(MONO, max(10, d * 0.34), True))
                p.drawText(QRectF(x0, 0, d * 1.6, d / 2), Qt.AlignLeft | Qt.AlignTop,
                           ch.text())
            p.restore()
        p.end()

    # ---- mouse: clique abre, arrastar ajusta a posição, botão direito = menu
    def enterEvent(self, _e):
        self.hover = True
        self.update()

    def leaveEvent(self, _e):
        self.hover = False
        self.update()

    def _axis(self, e):
        g = e.globalPosition()
        return g.y() if self.vertical else g.x()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.drag = (self._axis(e), int(self.ctrl.cfg.get("tray_offset", 0)), False)

    def mouseMoveEvent(self, e):
        if self.drag:
            start, off0, moved = self.drag
            delta = self._axis(e) - start
            if moved or abs(delta) > 4:
                self.drag = (start, off0, True)
                self.ctrl.cfg["tray_offset"] = int(off0 + delta)
                self.reposition()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.drag:
            moved = self.drag[2]
            self.drag = None
            if moved:
                save_config(self.ctrl.cfg)
            else:
                self.ctrl.restore()

    def contextMenuEvent(self, e):
        self.ctrl.menu.exec(e.globalPos())


# ============================================================== janela principal
class Annunciator(QWidget):
    """Luz de painel com texto: apagada (escura) ou acesa."""

    def __init__(self, text, color_key, width, text_on="#ffffff"):
        super().__init__()
        self.text, self.color_key, self.text_on = text, color_key, text_on
        self.setFixedSize(width, 32)
        self.on = False

    def set_on(self, on):
        if on != self.on:
            self.on = on
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        base = QColor(COL.get(self.color_key, self.color_key))
        if self.on:
            g = QLinearGradient(0, 0, 0, self.height())
            g.setColorAt(0, base.lighter(118))
            g.setColorAt(1, base.darker(112))
            p.setPen(QPen(qc("#000000"), 1.5))
            p.setBrush(g)
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QPen(base.lighter(160), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), 2, 2)
            txt = qc(self.text_on)
        else:
            p.setPen(QPen(qc("#000000"), 1.5))
            p.setBrush(base.darker(520))
            p.drawRoundedRect(r, 3, 3)
            txt = base.darker(230)
        p.setPen(txt)
        p.setFont(qfont(SANS, 13, True))
        p.drawText(r, Qt.AlignCenter, self.text)
        p.end()


class HeaderBar(QWidget):
    def paintEvent(self, _e):
        p = QPainter(self)
        panel = QColor(COL["panel"])
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, panel.lighter(112))
        g.setColorAt(1, panel.darker(118))
        p.fillRect(self.rect(), g)
        p.setPen(QPen(panel.lighter(165), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()


class Reader(QObject):
    """Inicia os sensores e lê numa thread separada — a interface não espera por eles."""
    ready = Signal(object, object)
    started = Signal(str, object)

    def __init__(self, demo, interval):
        super().__init__()
        self.demo, self.interval = demo, interval
        self.backend = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        t0 = time.monotonic()
        self.backend, msg = make_backend(self.demo)
        log(f"sensores prontos em {time.monotonic() - t0:.1f} s ({self.backend.name})")
        self.started.emit(self.backend.name, msg)
        while not self.stop.is_set():
            try:
                self.ready.emit(self.backend.read(), None)
            except Exception as e:
                self.ready.emit(None, f"Erro na leitura: {e}")
            self.stop.wait(self.interval)

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=2)
        if self.backend:
            self.backend.close()


class MainWindow(QWidget):
    def __init__(self, ctrl, source_name, startup_msg):
        super().__init__()
        self.ctrl = ctrl
        self.source_name, self.startup_msg = source_name, startup_msg
        self.last_err = None
        self.gauges = []
        self.setWindowTitle("Cockpit Monitor")
        self.setWindowIcon(ctrl.icon)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setSizeConstraint(QLayout.SetFixedSize)      # janela acompanha o conteúdo
        self.header = HeaderBar()
        self.header.setFixedHeight(66)
        head = QHBoxLayout(self.header)
        head.setContentsMargins(12, 7, 14, 7)
        head.setSpacing(10)
        self.logo = QLabel()
        self.logo.setFixedSize(52, 52)
        head.addWidget(self.logo)
        tbox = QVBoxLayout()
        tbox.setSpacing(0)
        self.title = QLabel("COCKPIT MONITOR")
        self.subtitle = QLabel("ENGINE & THERMAL INDICATION")
        tbox.addStretch(1)
        tbox.addWidget(self.title)
        tbox.addWidget(self.subtitle)
        tbox.addStretch(1)
        head.addLayout(tbox)
        head.addStretch(1)
        self.btn_cfg = QPushButton("⚙  CONFIG")
        self.btn_cfg.setCursor(Qt.PointingHandCursor)
        self.btn_cfg.setFixedHeight(32)
        self.btn_cfg.clicked.connect(ctrl.open_settings)
        head.addWidget(self.btn_cfg)
        self.overheat = Annunciator("OVERHEAT", "needle_hot", 104)
        head.addWidget(self.overheat)
        self.caution = Annunciator("MASTER CAUTION", "caution", 152, text_on="#1a1205")
        head.addWidget(self.caution)
        root.addWidget(self.header)

        body = QVBoxLayout()
        body.setContentsMargins(14, 4, 14, 10)
        body.setSpacing(6)
        root.addLayout(body)

        self.placards = []
        body.addLayout(self._placard("TEMPERATURAS"))
        self.temp_grid = QGridLayout()
        self.temp_grid.setSpacing(8)
        body.addLayout(self.temp_grid)
        self.fps_box = QVBoxLayout()
        self.fps_box.setSpacing(6)
        body.addLayout(self.fps_box)
        body.addLayout(self._placard("VENTOINHAS"))
        self.fan_grid = QGridLayout()
        self.fan_grid.setSpacing(8)
        self.fan_grid.setAlignment(Qt.AlignLeft)
        self.nofan = QLabel("Nenhuma ventoinha detectada")
        self.fan_grid.addWidget(self.nofan, 0, 0)
        body.addLayout(self.fan_grid)

        self.status = QLabel()
        self.status.setContentsMargins(14, 5, 14, 5)
        root.addWidget(self.status)

        n_temp = 0
        for k, ch in ctrl.channels.items():
            if ch.is_fps:
                self.fps_box.addLayout(self._placard("JOGO"))
                row = QHBoxLayout()
                g = Gauge(ch, ctrl.cfg["gauge_size"])
                row.addWidget(g)
                row.addStretch(1)
                self.fps_box.addLayout(row)
                self.gauges.append(g)
            elif not ch.is_fan:
                g = Gauge(ch, ctrl.cfg["gauge_size"])
                self.temp_grid.addWidget(g, 0, n_temp)
                self.gauges.append(g)
                n_temp += 1
        self.restyle()

    def _placard(self, text):
        h = QHBoxLayout()
        h.setContentsMargins(0, 8, 0, 0)
        bar = QFrame()
        bar.setFixedSize(4, 14)
        lab = QLabel(text)
        line = QFrame()
        line.setFixedHeight(1)
        h.addWidget(bar)
        h.addSpacing(4)
        h.addWidget(lab)
        h.addSpacing(6)
        h.addWidget(line, 1)
        self.placards.append((bar, lab, line))
        return h

    def add_fan_gauge(self, ch):
        n = sum(1 for g in self.gauges if g.ch.is_fan)
        g = Gauge(ch, self.ctrl.cfg["gauge_size"])
        self.nofan.hide()
        self.fan_grid.addWidget(g, n // COLUMNS, n % COLUMNS)
        self.gauges.append(g)

    def restyle(self):
        bg, text, dim = COL["background"], COL["text"], COL["dim"]
        line = QColor(COL["panel"]).lighter(140).name()
        self.setStyleSheet(f"MainWindow {{ background:{bg}; }}")
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(bg))
        self.setPalette(pal)
        self.title.setStyleSheet(f"color:{text}; font: bold 27px '{SANS}'; letter-spacing:1px;")
        self.subtitle.setStyleSheet(f"color:{dim}; font: bold 12px '{SANS}'; letter-spacing:1px;")
        dpr = self.devicePixelRatioF() or 1.0
        pm = self.ctrl.logo_pixmap(round(52 * dpr))
        pm.setDevicePixelRatio(dpr)
        self.logo.setPixmap(pm)
        btn_bg = QColor(COL["panel"]).darker(115).name()
        self.btn_cfg.setStyleSheet(
            f"QPushButton {{ color:{QColor(text).darker(125).name()}; background:{btn_bg};"
            f" border:1.5px solid #000; border-radius:3px; padding:0 14px;"
            f" font: bold 13px '{SANS}'; letter-spacing:1px; }}"
            f"QPushButton:hover {{ color:{text}; background:{QColor(btn_bg).lighter(130).name()}; }}")
        for bar, lab, ln in self.placards:
            bar.setStyleSheet(f"background:{dim};")
            lab.setStyleSheet(f"color:{QColor(dim).lighter(125).name()}; font: bold 14px '{SANS}';"
                              f" letter-spacing:1px;")
            ln.setStyleSheet(f"background:{line};")
        self.header.update()
        self.overheat.update()
        self.caution.update()
        self.nofan.setStyleSheet(f"color:{dim}; font: 12px '{SANS}'; padding:10px 4px;")
        self.set_status(self.last_err)
        for g in self.gauges:
            g.update()

    def set_status(self, err):
        self.last_err = err
        msg = err or self.startup_msg
        bar = QColor(COL["background"]).darker(160).name()
        if msg:
            self.status.setText("⚠  " + msg)
            color = "#ffb000"
        else:
            self.status.setText(f"Fonte: {self.source_name}   ·   leitura a cada "
                                f"{self.ctrl.cfg['interval']:g} s")
            color = COL["dim"]
        self.status.setStyleSheet(f"background:{bar}; color:{color}; font: bold 12px '{SANS}';"
                                  f" letter-spacing:0.5px;")

    def apply_size(self, size):
        for g in self.gauges:
            g.setFixedSize(size, size)

    def changeEvent(self, e):
        if (e.type() == QEvent.WindowStateChange and self.isMinimized()
                and self.ctrl.cfg["tray_enabled"]):
            QTimer.singleShot(0, self.ctrl.to_taskbar)
        super().changeEvent(e)

    def closeEvent(self, e):
        if self.ctrl.cfg["close_to_tray"] and not self.ctrl.quitting:
            e.ignore()
            self.ctrl.to_taskbar()
        else:
            e.accept()
            self.ctrl.quit()


# ============================================================== configurações
class SettingsDialog(QDialog):
    def __init__(self, ctrl):
        super().__init__(ctrl.window if ctrl.window.isVisible() else None)
        self.ctrl = ctrl
        self.cfg = json.loads(json.dumps(ctrl.cfg))
        self.setWindowTitle("Configurações — Cockpit Monitor")
        self.setWindowIcon(ctrl.icon)
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._tab_colors(), "Aparência")
        tabs.addTab(self._tab_taskbar(), "Barra de tarefas")
        tabs.addTab(self._tab_alerts(), "Alertas")
        tabs.addTab(self._tab_notify(), "Notificações")
        tabs.addTab(self._tab_general(), "Geral")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        bb.button(QDialogButtonBox.Ok).setText("OK")
        bb.button(QDialogButtonBox.Cancel).setText("Cancelar")
        bb.button(QDialogButtonBox.Apply).setText("Aplicar")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        lay.addWidget(bb)

    # ---- abas
    def _tab_colors(self):
        w = QWidget()
        g = QGridLayout(w)
        g.addWidget(QLabel("Tema pronto:"), 0, 0)
        self.preset = QComboBox()
        self.preset.addItem("Personalizado")
        self.preset.addItems(list(PRESETS))
        self.preset.currentTextChanged.connect(self._preset_chosen)
        g.addWidget(self.preset, 0, 1)
        self.color_btns = {}
        for row, (k, label, _) in enumerate(COLOR_FIELDS, start=1):
            g.addWidget(QLabel(label), row, 0)
            b = QPushButton()
            b.setFixedSize(90, 24)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(partial(self._pick, k, label))
            g.addWidget(b, row, 1)
            self.color_btns[k] = b
        reset = QPushButton("Restaurar cores padrão")
        reset.clicked.connect(lambda: self._set_colors(dict(DEFAULT_COLORS)))
        g.addWidget(reset, len(COLOR_FIELDS) + 1, 0, 1, 2)
        g.setColumnStretch(2, 1)
        self._refresh_swatches()
        return w

    def _tab_taskbar(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.tray_enabled = QCheckBox("Ao minimizar, mostrar mini-relógios na barra de tarefas")
        self.tray_enabled.setChecked(self.cfg["tray_enabled"])
        v.addWidget(self.tray_enabled)
        row = QHBoxLayout()
        row.addWidget(QLabel("Estilo:"))
        self.tray_style = QComboBox()
        self.tray_style.addItem("Relógio + nome e valor ao lado", "dial_text")
        self.tray_style.addItem("Só o relógio (mais compacto)", "dial")
        self.tray_style.setCurrentIndex(0 if self.cfg["tray_style"] == "dial_text" else 1)
        row.addWidget(self.tray_style)
        row.addSpacing(16)
        row.addWidget(QLabel("Tamanho:"))
        self.tray_size = QSpinBox()
        self.tray_size.setRange(24, 64)
        self.tray_size.setSuffix(" px")
        self.tray_size.setValue(int(self.cfg["tray_size"]))
        row.addWidget(self.tray_size)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(QLabel("Relógios que aparecem na barra (na ordem abaixo):"))
        self.tray_list = QListWidget()
        order = sorted(self.ctrl.channels.items(), key=lambda kv: kv[0] != "fps")
        for k, ch in order:
            it = QListWidgetItem("FPS do jogo (só aparece com jogo aberto)" if ch.is_fps else
                                 ch.title if not ch.is_fan else f"Ventoinha — {ch.title}")
            it.setData(Qt.UserRole, k)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if k in self.cfg["tray_items"] else Qt.Unchecked)
            self.tray_list.addItem(it)
        v.addWidget(self.tray_list)
        if not any(ch.is_fan for ch in self.ctrl.channels.values()):
            hint = QLabel("As ventoinhas entram nesta lista assim que forem detectadas.")
            hint.setStyleSheet("color:#8a95a1;")
            v.addWidget(hint)
        ag = QGridLayout()
        ag.addWidget(QLabel("Animação de entrada:"), 0, 0)
        self.boot_style = QComboBox()
        for label, val in (("Partida do motor (liga um por vez e testa até o vermelho)", "engine"),
                           ("Simples (aparecem em sequência)", "pop"), ("Nenhuma", "none")):
            self.boot_style.addItem(label, val)
        self.boot_style.setCurrentIndex(max(0, self.boot_style.findData(self.cfg["boot_style"])))
        ag.addWidget(self.boot_style, 0, 1, 1, 2)
        ag.addWidget(QLabel("Fazer a partida:"), 1, 0)
        self.boot_when = QComboBox()
        self.boot_when.addItem("Só ao abrir o programa", "start")
        self.boot_when.addItem("Toda vez que minimizar", "always")
        self.boot_when.setCurrentIndex(max(0, self.boot_when.findData(self.cfg["boot_when"])))
        ag.addWidget(self.boot_when, 1, 1, 1, 2)
        ag.addWidget(QLabel("Velocidade da partida:"), 2, 0)
        self.boot_speed = QComboBox()
        for label, val in (("Lenta", 1.6), ("Normal", 1.0), ("Rápida", 0.7), ("Bem rápida", 0.5)):
            self.boot_speed.addItem(label, val)
        cur = float(self.cfg["boot_pace"])
        self.boot_speed.setCurrentIndex(min(range(self.boot_speed.count()),
                                            key=lambda i: abs(self.boot_speed.itemData(i) - cur)))
        ag.addWidget(self.boot_speed, 2, 1)
        test = QPushButton("▶  Testar")
        test.clicked.connect(lambda: self.ctrl.preview_boot(self.boot_speed.currentData()))
        ag.addWidget(test, 2, 2)
        ag.setColumnStretch(1, 1)
        v.addLayout(ag)
        self.tray_bg = QCheckBox("Fundo escuro atrás dos mini-relógios (útil com barra clara)")
        self.tray_bg.setChecked(self.cfg["tray_background"])
        v.addWidget(self.tray_bg)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Monitor:"))
        self.tray_monitor = QComboBox()
        self.tray_monitor.addItem("Monitor principal (automático)", "")
        prim = QGuiApplication.primaryScreen()
        for i, sc in enumerate(QGuiApplication.screens(), start=1):
            g = sc.geometry()
            dpr = sc.devicePixelRatio()
            label = (f"Monitor {i} — {round(g.width() * dpr)}×{round(g.height() * dpr)}"
                     f"{'  (principal)' if sc is prim else ''}   {sc.model() or sc.name()}")
            self.tray_monitor.addItem(label.strip(), sc.name())
        idx = self.tray_monitor.findData(self.cfg.get("tray_monitor", ""))
        self.tray_monitor.setCurrentIndex(max(0, idx))
        mrow.addWidget(self.tray_monitor, 1)
        v.addLayout(mrow)
        mhint = QLabel("Em outro monitor, ative no Windows: Configurações → Personalização → Barra de "
                       "tarefas → \"Mostrar a barra de tarefas em todas as telas\".")
        mhint.setWordWrap(True)
        mhint.setStyleSheet("color:#8a95a1;")
        v.addWidget(mhint)
        self.hide_fs = QCheckBox("Esconder durante jogos e vídeos em tela cheia")
        self.hide_fs.setChecked(self.cfg["hide_fullscreen"])
        v.addWidget(self.hide_fs)
        rp = QPushButton("Voltar à posição padrão (ao lado do relógio)")
        rp.clicked.connect(lambda: self.cfg.__setitem__("tray_offset", 0))
        v.addWidget(rp)
        tip = QLabel("Dica: arraste os mini-relógios para ajustar a posição; clique para abrir o painel.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a95a1;")
        v.addWidget(tip)
        return w

    def _tab_alerts(self):
        w = QWidget()
        g = QGridLayout(w)
        g.addWidget(QLabel("A seta fica vermelha (e o MASTER CAUTION pisca) a partir de:"), 0, 0, 1, 2)
        self.warn_spins = {}
        for row, (k, title, lo, hi) in enumerate(TEMP_SPECS, start=1):
            g.addWidget(QLabel(title), row, 0)
            s = QSpinBox()
            s.setRange(30, hi)
            s.setSuffix(" °C")
            s.setValue(int(self.cfg["warn"].get(k, hi)))
            g.addWidget(s, row, 1)
            self.warn_spins[k] = s
        r = len(TEMP_SPECS) + 1
        g.addWidget(QLabel("Ventoinhas (% da escala)"), r, 0)
        self.fan_pct = QSpinBox()
        self.fan_pct.setRange(50, 100)
        self.fan_pct.setSuffix(" %")
        self.fan_pct.setValue(int(self.cfg["fan_warn_pct"]))
        g.addWidget(self.fan_pct, r, 1)
        self.glow = QCheckBox("Piscar em vermelho em volta do relógio quando estiver em alerta")
        self.glow.setChecked(self.cfg["alert_glow"])
        g.addWidget(self.glow, r + 1, 0, 1, 3)
        note = QLabel("Avisos no celular (Telegram / WhatsApp): aba Notificações.")
        note.setStyleSheet("color:#8a95a1;")
        g.addWidget(note, r + 2, 0, 1, 3)
        g.addWidget(QLabel("FPS baixo (ponteiro vermelho) abaixo de"), r + 3, 0)
        self.fps_warn = QSpinBox()
        self.fps_warn.setRange(0, 300)
        self.fps_warn.setSuffix(" FPS")
        self.fps_warn.setValue(int(self.cfg.get("fps_warn", 30)))
        g.addWidget(self.fps_warn, r + 3, 1)
        g.addWidget(QLabel("Escala do relógio de FPS até"), r + 4, 0)
        self.fps_max = QComboBox()
        for v in (60, 120, 144, 165, 240, 360):
            self.fps_max.addItem(f"{v} FPS", v)
        self.fps_max.setCurrentIndex(max(0, self.fps_max.findData(int(self.cfg.get("fps_max", 240)))))
        g.addWidget(self.fps_max, r + 4, 1)
        g.setRowStretch(r + 5, 1)
        g.setColumnStretch(2, 1)
        return w

    def _tab_general(self):
        w = QWidget()
        g = QGridLayout(w)
        self.autostart = QCheckBox("Iniciar junto com o Windows")
        self.autostart.setChecked(self.cfg["autostart"])
        g.addWidget(self.autostart, 0, 0, 1, 2)
        hint = QLabel("    Ao ligar o PC abre só os mini-relógios; o painel abre ao clicar neles.")
        hint.setStyleSheet("color:#8a95a1;")
        g.addWidget(hint, 1, 0, 1, 3)
        self.close_tray = QCheckBox("Botão fechar (X) minimiza em vez de sair")
        self.close_tray.setChecked(self.cfg["close_to_tray"])
        g.addWidget(self.close_tray, 2, 0, 1, 2)
        g.addWidget(QLabel("Ao abrir o programa"), 8, 0)
        self.start_mode = QComboBox()
        self.start_mode.addItem("Só os mini-relógios na barra", "tray")
        self.start_mode.addItem("O painel completo", "panel")
        self.start_mode.setCurrentIndex(0 if self.cfg.get("start_mode", "tray") == "tray" else 1)
        g.addWidget(self.start_mode, 8, 1)
        self.on_top = QCheckBox("Janela sempre por cima das outras")
        self.on_top.setChecked(self.cfg["always_on_top"])
        g.addWidget(self.on_top, 3, 0, 1, 2)
        self.hide_missing = QCheckBox("Esconder relógios de sensores que este PC não tem")
        self.hide_missing.setChecked(self.cfg.get("hide_missing", True))
        g.addWidget(self.hide_missing, 10, 0, 1, 3)
        g.addWidget(QLabel("Leitura dos sensores a cada"), 4, 0)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 10)
        self.interval.setSingleStep(0.5)
        self.interval.setSuffix(" s")
        self.interval.setValue(float(self.cfg["interval"]))
        g.addWidget(self.interval, 4, 1)
        g.addWidget(QLabel("Velocidade dos ponteiros"), 5, 0)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(3, 20)
        self.speed.setSingleStep(1)
        self.speed.setValue(float(self.cfg["needle_speed"]))
        g.addWidget(self.speed, 5, 1)
        g.addWidget(QLabel("Tamanho dos relógios no painel"), 6, 0)
        self.gsize = QSpinBox()
        self.gsize.setRange(140, 320)
        self.gsize.setSingleStep(10)
        self.gsize.setSuffix(" px")
        self.gsize.setValue(int(self.cfg["gauge_size"]))
        g.addWidget(self.gsize, 6, 1)
        g.setRowStretch(11, 1)
        g.setColumnStretch(2, 1)
        return w

    def _tab_notify(self):
        n = self.cfg["notify"]
        self._jobs = set()
        w = QWidget()
        v = QVBoxLayout(w)
        top = QLabel("Avisos enviados quando uma temperatura passa do limite da aba Alertas.")
        top.setWordWrap(True)
        v.addWidget(top)
        self.n_win = QCheckBox("Notificação do Windows (aviso no canto da tela)")
        self.n_win.setChecked(n["windows"])
        v.addWidget(self.n_win)

        tg = QGroupBox("Telegram  (grátis, recomendado)")
        g = QGridLayout(tg)
        self.n_tg = QCheckBox("Enviar alertas pelo Telegram")
        self.n_tg.setChecked(n["telegram"])
        g.addWidget(self.n_tg, 0, 0, 1, 3)
        g.addWidget(QLabel("Token do bot:"), 1, 0)
        self.tg_token = QLineEdit(n["tg_token"])
        self.tg_token.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.tg_token.setPlaceholderText("123456789:AA...")
        g.addWidget(self.tg_token, 1, 1, 1, 2)
        g.addWidget(QLabel("Chat ID:"), 2, 0)
        self.tg_chat = QLineEdit(n["tg_chat"])
        g.addWidget(self.tg_chat, 2, 1)
        det = QPushButton("Detectar")
        det.clicked.connect(self._tg_detect)
        g.addWidget(det, 2, 2)
        h = QLabel("1. No Telegram, abra o @BotFather → /newbot → copie o token.\n"
                   "2. Abra o seu bot novo e mande qualquer mensagem (ex.: oi).\n"
                   "3. Cole o token acima e clique em Detectar.")
        h.setStyleSheet("color:#8a95a1;")
        g.addWidget(h, 3, 0, 1, 2)
        t1 = QPushButton("Enviar teste")
        t1.clicked.connect(lambda: self._test("Telegram", lambda txt: send_telegram(
            self.tg_token.text(), self.tg_chat.text(), txt)))
        g.addWidget(t1, 3, 2, Qt.AlignBottom)
        g.setColumnStretch(1, 1)
        v.addWidget(tg)

        wa = QGroupBox("WhatsApp  (via CallMeBot — serviço gratuito de terceiros, uso pessoal)")
        g = QGridLayout(wa)
        self.n_wa = QCheckBox("Enviar alertas pelo WhatsApp")
        self.n_wa.setChecked(n["whatsapp"])
        g.addWidget(self.n_wa, 0, 0, 1, 3)
        g.addWidget(QLabel("Seu número:"), 1, 0)
        self.wa_phone = QLineEdit(n["wa_phone"])
        self.wa_phone.setPlaceholderText("+5511999998888")
        g.addWidget(self.wa_phone, 1, 1, 1, 2)
        g.addWidget(QLabel("API key:"), 2, 0)
        self.wa_key = QLineEdit(n["wa_key"])
        self.wa_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        g.addWidget(self.wa_key, 2, 1, 1, 2)
        h = QLabel("1. Salve o contato +34 644 43 38 04 no celular.\n"
                   "2. Mande para ele no WhatsApp: I allow callmebot to send me messages\n"
                   "3. Ele responde com a sua APIKEY — cole acima.")
        h.setStyleSheet("color:#8a95a1;")
        g.addWidget(h, 3, 0, 1, 2)
        t2 = QPushButton("Enviar teste")
        t2.clicked.connect(lambda: self._test("WhatsApp", lambda txt: send_whatsapp(
            self.wa_phone.text(), self.wa_key.text(), txt)))
        g.addWidget(t2, 3, 2, Qt.AlignBottom)
        g.setColumnStretch(1, 1)
        v.addWidget(wa)

        row = QHBoxLayout()
        row.addWidget(QLabel("Avisar se ficar acima do limite por"))
        self.n_sustain = QSpinBox()
        self.n_sustain.setRange(0, 600)
        self.n_sustain.setSuffix(" s")
        self.n_sustain.setValue(int(n["sustain"]))
        row.addWidget(self.n_sustain)
        row.addWidget(QLabel("   repetir a cada"))
        self.n_cooldown = QSpinBox()
        self.n_cooldown.setRange(1, 1440)
        self.n_cooldown.setSuffix(" min")
        self.n_cooldown.setValue(int(n["cooldown"]))
        row.addWidget(self.n_cooldown)
        row.addStretch(1)
        v.addLayout(row)
        self.n_recover = QCheckBox("Avisar também quando a temperatura voltar ao normal")
        self.n_recover.setChecked(n["recover"])
        v.addWidget(self.n_recover)
        v.addStretch(1)
        return w

    def _async(self, fn, done):
        job = AsyncJob(fn)
        self._jobs.add(job)

        def finish(res, err):
            self._jobs.discard(job)
            done(res, err)
        job.done.connect(finish)
        job.start()

    def _tg_detect(self):
        token = self.tg_token.text()

        def done(res, err):
            if err:
                QMessageBox.warning(self, "Telegram", str(err))
            elif not res or not res[0]:
                QMessageBox.information(self, "Telegram", "Nenhuma mensagem encontrada.\n"
                                        "Mande um \"oi\" para o seu bot no Telegram e clique em Detectar de novo.")
            else:
                self.tg_chat.setText(res[0])
                self.n_tg.setChecked(True)
                QMessageBox.information(self, "Telegram", f"Chat encontrado: {res[1] or res[0]}")
        self._async(lambda: telegram_detect_chat(token), done)

    def _test(self, name, fn):
        txt = f"✅ Teste do Cockpit Monitor ({platform.node()}): os alertas vão chegar aqui."

        def done(_res, err):
            if err:
                QMessageBox.warning(self, name, f"Não foi possível enviar:\n{err}")
            else:
                QMessageBox.information(self, name, "Mensagem de teste enviada! Confira no celular.")
        self._async(lambda: fn(txt), done)

    # ---- cores
    def _refresh_swatches(self):
        for k, b in self.color_btns.items():
            c = self.cfg["colors"].get(k, DEFAULT_COLORS[k])
            fg = "#000000" if QColor(c).lightness() > 140 else "#ffffff"
            b.setText(c.upper())
            b.setStyleSheet(f"background:{c}; color:{fg}; border:1px solid #3a424c;"
                            f" border-radius:3px; font: 11px '{MONO}';")

    def _set_colors(self, colors, keep_preset=False):
        self.cfg["colors"] = colors
        self._refresh_swatches()
        if not keep_preset:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(0)
            self.preset.blockSignals(False)

    def _preset_chosen(self, name):
        if name in PRESETS:
            c = dict(DEFAULT_COLORS)
            c.update(PRESETS[name])
            self._set_colors(c, keep_preset=True)

    def _pick(self, key, label):
        cur = QColor(self.cfg["colors"].get(key, DEFAULT_COLORS[key]))
        c = QColorDialog.getColor(cur, self, label)
        if c.isValid():
            colors = dict(self.cfg["colors"])
            colors[key] = c.name()
            self._set_colors(colors)

    # ---- salvar
    def _collect(self):
        c = self.cfg
        c["tray_enabled"] = self.tray_enabled.isChecked()
        c["tray_style"] = self.tray_style.currentData()
        c["tray_size"] = self.tray_size.value()
        c["tray_items"] = [self.tray_list.item(i).data(Qt.UserRole)
                           for i in range(self.tray_list.count())
                           if self.tray_list.item(i).checkState() == Qt.Checked]
        c["boot_style"] = self.boot_style.currentData()
        c["boot_when"] = self.boot_when.currentData()
        c["boot_pace"] = float(self.boot_speed.currentData())
        c["tray_animation"] = c["boot_style"] != "none"
        c["alert_glow"] = self.glow.isChecked()
        c["notify"] = {
            "windows": self.n_win.isChecked(),
            "telegram": self.n_tg.isChecked(),
            "tg_token": self.tg_token.text().strip(),
            "tg_chat": self.tg_chat.text().strip(),
            "whatsapp": self.n_wa.isChecked(),
            "wa_phone": self.wa_phone.text().strip(),
            "wa_key": self.wa_key.text().strip(),
            "sustain": self.n_sustain.value(),
            "cooldown": self.n_cooldown.value(),
            "recover": self.n_recover.isChecked(),
        }
        c["tray_background"] = self.tray_bg.isChecked()
        c["hide_fullscreen"] = self.hide_fs.isChecked()
        c["tray_monitor"] = self.tray_monitor.currentData()
        if c["tray_monitor"] != self.ctrl.cfg.get("tray_monitor", ""):
            c["tray_offset"] = 0                 # posição volta para perto do relógio no monitor novo
        c["warn"] = {k: s.value() for k, s in self.warn_spins.items()}
        c["fan_warn_pct"] = self.fan_pct.value()
        c["fps_warn"] = self.fps_warn.value()
        c["fps_max"] = int(self.fps_max.currentData())
        c["autostart"] = self.autostart.isChecked()
        c["start_minimized"] = True
        c["close_to_tray"] = self.close_tray.isChecked()
        c["start_mode"] = self.start_mode.currentData()
        c["always_on_top"] = self.on_top.isChecked()
        c["hide_missing"] = self.hide_missing.isChecked()
        c["interval"] = self.interval.value()
        c["needle_speed"] = self.speed.value()
        c["gauge_size"] = self.gsize.value()
        return json.loads(json.dumps(c))

    def _apply(self):
        self.ctrl.apply_config(self._collect())
        self.autostart.setChecked(self.ctrl.cfg["autostart"])

    def _ok(self):
        self._apply()
        self.accept()


# ============================================================== partida do motor
def _ease_in_out(u):
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def _back(u, c1=1.15):
    c3 = c1 + 1
    return 1 + c3 * (u - 1) ** 3 + c1 * (u - 1) ** 2


class BootSequence(QObject):
    """Liga os relógios um por vez: acende, ponteiro sobe até o vermelho, segura
    "roncando", desce até o valor real e estabiliza — só então liga o próximo."""
    APPEAR, UP, HOLD, DOWN, GAP = 0.22, 0.42, 0.14, 0.48, 0.06

    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.items = []
        self.speed, self.lead = 1.0, 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.clock = QElapsedTimer()

    def start(self, channels, speed=None, lead=0.35):
        self.cancel()
        self.items = list(channels)
        if not self.items:
            return
        self.speed = float(speed or self.ctrl.cfg.get("boot_pace", 1.0))
        self.lead = lead
        for ch in self.items:
            ch.appear, ch.override = 0.0, ch.vmin
            ch.redraw()
        self.clock.start()
        self.timer.start()

    def cancel(self):
        self.timer.stop()
        for ch in self.items:
            ch.appear, ch.override = 1.0, None
            ch.x, ch.v = ch.target, 0.0
            ch.redraw()
        self.items = []

    def _tick(self):
        A, U, H, D, G = (x * self.speed for x in (self.APPEAR, self.UP, self.HOLD, self.DOWN, self.GAP))
        per = A + U + H + D + G
        t = self.clock.elapsed() / 1000.0 - self.lead
        busy = False
        for i, ch in enumerate(self.items):
            lt = t - i * per
            span = ch.vmax - ch.vmin
            if lt < A + U + H + D:
                busy = True
            if lt < 0:
                continue
            if lt < A:
                ch.appear, ch.override = _smooth(lt / A), ch.vmin
            elif lt < A + U:
                ch.appear = 1.0
                ch.override = ch.vmin + span * _ease_in_out((lt - A) / U)
            elif lt < A + U + H:      # segura no vermelho com uma leve vibração
                ch.override = ch.vmax - span * 0.015 * (0.5 + 0.5 * math.sin(lt * 70))
            elif lt < A + U + H + D:
                u = (lt - A - U - H) / D
                ch.override = ch.vmax + (ch.target - ch.vmax) * _back(u)
            elif ch.override is not None:
                ch.appear, ch.override = 1.0, None
                ch.x, ch.v = ch.target, 0.0
            else:
                continue
            ch.redraw()
        if not busy:
            self.timer.stop()
            self.items = []


# ============================================================== notificações
def _http(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "CockpitMonitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("description", body)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Sem conexão: {e.reason}")


def send_telegram(token, chat, text):
    token, chat = token.strip(), str(chat).strip()
    if not token or not chat:
        raise ValueError("Preencha o token do bot e o chat ID.")
    body = _http(f"https://api.telegram.org/bot{token}/sendMessage",
                 urllib.parse.urlencode({"chat_id": chat, "text": text}).encode())
    if not json.loads(body).get("ok"):
        raise RuntimeError(body[:200])


def telegram_detect_chat(token):
    token = token.strip()
    if not token:
        raise ValueError("Cole primeiro o token do bot.")
    data = json.loads(_http(f"https://api.telegram.org/bot{token}/getUpdates"))
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("first_name") or chat.get("title") or chat.get("username") or ""
            return str(chat["id"]), name
    return None, None


def send_whatsapp(phone, key, text):
    phone = re.sub(r"[\s\-()]", "", phone)
    key = key.strip()
    if not phone or not key:
        raise ValueError("Preencha o número (com +55) e a API key.")
    body = _http("https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": key}), timeout=25)
    plain = re.sub(r"<[^>]+>", " ", body)
    if re.search(r"invalid|error|not\s+(allowed|activated)", plain, re.I):
        raise RuntimeError(" ".join(plain.split())[:200])


class AsyncJob(QObject):
    done = Signal(object, object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.done.emit(self.fn(), None)
        except Exception as e:
            self.done.emit(None, e)


class Notifier(QObject):
    failed = Signal(str)

    def __init__(self, ctrl):
        super().__init__()
        self.ctrl = ctrl
        self.state = {}
        self.failed.connect(self._show_fail)

    def check(self):
        cfg = self.ctrl.cfg["notify"]
        now = time.monotonic()
        for ch in self.ctrl.channels.values():
            if ch.is_fan or ch.warn is None:
                continue
            st = self.state.setdefault(ch.key, {"since": None, "sent": -1e9, "alerted": False})
            if ch.hot:
                if st["since"] is None:
                    st["since"] = now
                if (now - st["since"] >= cfg["sustain"]
                        and now - st["sent"] >= max(1, cfg["cooldown"]) * 60):
                    first = not st["alerted"]
                    st["alerted"], st["sent"] = True, now
                    self.send(self._alert_text(ch, first), warn=True)
            elif ch.value is None or ch.value < ch.warn - 3:     # 3 °C de histerese
                if st["alerted"] and cfg["recover"]:
                    v = "sem leitura" if ch.value is None else f"{ch.value:.0f} °C"
                    self.send(f"🟢 Cockpit Monitor ({platform.node()})\n"
                              f"{ch.title} voltou ao normal: {v}", warn=False)
                st["since"], st["alerted"] = None, False

    def _alert_text(self, ch, first):
        head = "🔴 ALERTA DE TEMPERATURA" if first else "🔴 Continua acima do limite"
        others = "  ·  ".join(f"{c.title} {c.value:.0f}°C" for c in self.ctrl.channels.values()
                              if not c.is_fan and c.value is not None)
        return (f"{head} — {platform.node()}\n"
                f"{ch.title}: {ch.value:.0f} °C (limite {ch.warn:.0f} °C)\n\n"
                f"{others}\n{time.strftime('%d/%m/%Y %H:%M')}")

    def send(self, text, warn=True):
        cfg = self.ctrl.cfg["notify"]
        if cfg["windows"]:
            self.ctrl.tray.showMessage("Cockpit Monitor", text,
                                       QSystemTrayIcon.Warning if warn else QSystemTrayIcon.Information,
                                       10000)
        if self.ctrl.demo:          # no modo demo não manda nada para o celular
            return
        jobs = []
        if cfg["telegram"]:
            jobs.append(("Telegram", partial(send_telegram, cfg["tg_token"], cfg["tg_chat"], text)))
        if cfg["whatsapp"]:
            jobs.append(("WhatsApp", partial(send_whatsapp, cfg["wa_phone"], cfg["wa_key"], text)))
        if jobs:
            threading.Thread(target=self._run, args=(jobs,), daemon=True).start()

    def _run(self, jobs):
        for name, fn in jobs:
            try:
                fn()
            except Exception as e:
                self.failed.emit(f"{name}: {e}")

    def _show_fail(self, msg):
        self.ctrl.tray.showMessage("Cockpit Monitor — falha ao enviar alerta", msg,
                                   QSystemTrayIcon.Critical, 10000)


# ============================================================== controle geral
def make_icon():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for name in ("cockpit.ico", "cockpit_logo.png"):
        if (base / name).exists():
            icon = QIcon(str(base / name))
            if not icon.isNull():
                return icon
    icon = QIcon()
    animator = Animator(lambda: 8.0)
    ch = Channel(animator, "icon", "CPU", "", 0, 100, 80, 20, 4)
    ch.value = ch.x = 62
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHints(QPainter.Antialiasing)
        p.translate(s / 2, s / 2)
        p.scale(s / 100.0, s / 100.0)
        draw_mini_dial(p, ch, show_text=False)
        p.end()
        icon.addPixmap(pm)
    return icon


class Controller(QObject):
    def __init__(self, app, args, server):
        super().__init__()
        self.app, self.server = app, server
        self.quitting = False
        first_run = not CONFIG_FILE.exists()
        self.reads = 0
        self.cfg = load_config()
        if args.interval:
            self.cfg["interval"] = args.interval
        if args.size:
            self.cfg["gauge_size"] = args.size
        apply_theme(self.cfg["colors"])
        self.icon = make_icon()
        app.setWindowIcon(self.icon)

        self.animator = Animator(lambda: float(self.cfg["needle_speed"]))
        self.demo = bool(args.demo)
        FX["glow"] = bool(self.cfg["alert_glow"])
        self.boot = BootSequence(self)
        self.notifier = Notifier(self)
        self.pulse_clock = QElapsedTimer()
        self.pulse_timer = QTimer(self)
        self.pulse_timer.setTimerType(Qt.PreciseTimer)
        self.pulse_timer.setInterval(33)
        self.pulse_timer.timeout.connect(self._pulse)
        self.channels = {}
        for key, title, lo, hi in TEMP_SPECS:
            self.channels[key] = Channel(self.animator, key, title, "°C", lo, hi,
                                         self.cfg["warn"].get(key, hi), 20, 4)
        self.seen_fans = set()
        self.fps_active = bool(args.demo)
        self.fps_last = 0.0
        self.fps_mon = FpsMonitor()
        if self.fps_mon.ok or args.demo:
            fmax = int(self.cfg.get("fps_max", 240))
            self.channels["fps"] = Channel(self.animator, "fps", "FPS", "", 0, fmax,
                                           self.cfg.get("fps_warn", 30), self._fps_major(fmax), 4,
                                           short="FPS", is_fps=True)
            self.fps_timer = QTimer(self)
            self.fps_timer.setInterval(500)
            self.fps_timer.timeout.connect(self._fps_tick)
            self.fps_timer.start()
            if not args.demo:
                self.fps_mon.start()
        else:
            log("PresentMon.exe não encontrado: relógio de FPS desativado")

        self.window = MainWindow(self, "iniciando sensores…", None)
        self.window.setWindowFlag(Qt.WindowStaysOnTopHint, bool(self.cfg["always_on_top"]))
        self.overlay = TaskbarDials(self)
        self.overlay.refresh_items()

        self.menu = QMenu()
        self.menu.addAction("Abrir painel", self.restore)
        self.menu.addAction("Configurações…", self.open_settings)
        self.menu.addSeparator()
        self.menu.addAction("Sair", self.quit)
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip("Cockpit Monitor")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_click)
        self.tray.show()
        self.fg_watch = ForegroundWatcher()
        self.fg_watch.changed.connect(lambda: QTimer.singleShot(30, self.overlay.raise_now))
        self._startup_autostart(first_run)
        if first_run:
            QTimer.singleShot(2500, lambda: self.tray.showMessage(
                "Cockpit Monitor", "Estou aqui na barra de tarefas. Clique nos relógios para abrir o painel.",
                QSystemTrayIcon.Information, 8000))

        self.blink = False
        self.blink_timer = QTimer(self)
        self.blink_timer.setInterval(500)
        self.blink_timer.timeout.connect(self._blink)
        self.blink_timer.start()

        server.newConnection.connect(self._other_instance)
        self.reader = Reader(args.demo, float(self.cfg["interval"]))
        self.reader.ready.connect(self.on_data)
        self.reader.started.connect(self._sensors_started)
        self.reader.start()

        start_tray = args.minimized or (self.cfg.get("start_mode", "tray") == "tray" and not args.show)
        if start_tray and self.cfg["tray_enabled"]:
            # só os mini-relógios; o painel abre ao clicar neles
            self.overlay.refresh_items()
            self._wait_taskbar(0)
        else:
            if self.cfg["boot_style"] == "engine":
                self.boot.start([c for c in self.channels.values() if not c.is_fan], lead=0.5)
            self.window.show()

    # ---- dados
    @staticmethod
    def _fps_major(fmax):
        return 10 if fmax <= 60 else 20 if fmax <= 144 else 30 if fmax <= 180 else 40 if fmax <= 240 else 60

    def _fps_tick(self):
        ch = self.channels.get("fps")
        if ch is None:
            return
        if self.demo:
            t = time.time()
            fps, name = 118 + 40 * math.sin(t / 5) + 6 * math.sin(t * 1.7), "jogo-demo"
            if math.sin(t / 23) > 0.85:
                fps = 24 + 3 * math.sin(t)          # mostra a faixa vermelha de vez em quando
        else:
            fps, name = self.fps_mon.current()
        ch.subtitle = name
        ch.set(None if fps is None else min(fps, ch.vmax * 1.05))
        now = time.monotonic()
        if fps is not None:
            self.fps_last = now
            self._set_fps_active(True)
        elif now - self.fps_last > 3:              # jogo fechado há 3 s: some da barra
            self._set_fps_active(False)

    def _set_fps_active(self, active):
        if active == self.fps_active:
            return
        self.fps_active = active
        ch = self.channels["fps"]
        if active:
            ch.appear = 0.0
            self.overlay.refresh_items()
            self._fade(ch, 1.0)
        else:
            def done():
                ch.appear = 1.0
                self.overlay.refresh_items()
            self._fade(ch, 0.0, done)

    def _fade(self, ch, target, then=None):
        """Anima só o relógio de FPS entrando/saindo da barra."""
        start = ch.appear
        clock = QElapsedTimer()
        clock.start()
        timer = QTimer(self)
        timer.setInterval(16)

        def tick():
            u = min(1.0, clock.elapsed() / 450.0)
            ch.appear = start + (target - start) * _smooth(u)
            self.overlay.update()
            if u >= 1.0:
                timer.stop()
                timer.deleteLater()
                if then:
                    then()
        timer.timeout.connect(tick)
        timer.start()

    def target_screen(self):
        name = self.cfg.get("tray_monitor", "")
        return screen_by_name(name) if name else QGuiApplication.primaryScreen()

    def _wait_taskbar(self, tries):
        """Logo depois do login a barra de tarefas pode ainda não existir: espera por ela."""
        ready = (not IS_WIN) or bool(_u32.FindWindowW("Shell_TrayWnd", None))
        if ready or tries >= 60:
            log(f"barra pronta após {tries * 0.25:.1f} s; mostrando relógios")
            QTimer.singleShot(150, lambda: self.overlay.show_animated(boot=True))
        else:
            QTimer.singleShot(250, lambda: self._wait_taskbar(tries + 1))

    def _sensors_started(self, name, msg):
        self.window.source_name, self.window.startup_msg = name, msg
        self.window.set_status(None)

    def is_missing(self, ch):
        return (self.cfg.get("hide_missing", True) and self.reads >= 4 and not ch.ever
                and not self.demo and not ch.is_fps)

    def _apply_missing(self):
        for g in self.window.gauges:
            g.setVisible(not self.is_missing(g.ch))
        self.overlay.refresh_items()

    def on_data(self, data, err):
        if data is not None:
            self.reads += 1
            for key, _, _, _ in TEMP_SPECS:
                self.channels[key].set(data.get(key))
            if self.reads == 4:                       # depois de ~4 leituras decide o que esconder
                self._apply_missing()
            for key, title, rpm in data.get("fans", []):
                self._fan(key, title, rpm)
            tip = "   ".join(f"{ch.short} {ch.text()}" for ch in self.channels.values())
            self.tray.setToolTip("Cockpit Monitor\n" + tip)
            self.overlay.setToolTip(tip)
            self.notifier.check()
            self._update_pulse()
            if err is None and self.cpu_missing_hint(data):
                err = self.cpu_missing_hint(data)
        self.window.set_status(err)

    def cpu_missing_hint(self, data):
        if self.demo or data.get("cpu") is not None:
            return None
        if IS_WIN:
            return ("CPU sem leitura: abra o programa como administrador. Se continuar, abra o "
                    "LibreHardwareMonitor.exe como admin uma vez (ele instala o driver).")
        return "CPU sem leitura: instale o lm-sensors (sudo sensors-detect)."

    def _update_pulse(self):
        want = FX["glow"] and any(c.hot for c in self.channels.values())
        if want and not self.pulse_timer.isActive():
            self.pulse_clock.start()
            self.pulse_timer.start()
        elif not want and self.pulse_timer.isActive():
            self.pulse_timer.stop()
            FX["pulse"] = 0.0
            for c in self.channels.values():
                c.redraw()

    def _pulse(self):
        t = self.pulse_clock.elapsed() / 1000.0
        FX["pulse"] = 0.5 - 0.5 * math.cos(2 * math.pi * 1.1 * t)
        for c in self.channels.values():
            if c.hot:
                c.redraw()

    def _startup_autostart(self, first_run):
        """1ª vez do .exe: liga o início com o Windows. Depois, mantém o caminho do .exe certo."""
        if not (IS_WIN and getattr(sys, "frozen", False)) or self.demo:
            return
        exe = sys.executable
        want = self.cfg["autostart"] or first_run
        if want and (self.cfg.get("autostart_path") != exe or self.cfg.get("task_rev") != TASK_REV):
            ok, msg = set_autostart(True)
            log(f"autostart -> {exe}: {'ok' if ok else msg}")
            if ok:
                self.cfg["autostart"] = True
                self.cfg["autostart_path"] = exe
                self.cfg["task_rev"] = TASK_REV
        save_config(self.cfg)

    def logo_pixmap(self, px):
        for name in ("cockpit_logo.png", "cockpit.ico"):
            f = app_dir() / name
            if f.exists():
                pm = QPixmap(str(f))
                if not pm.isNull():
                    return pm.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return self.icon.pixmap(px, px)

    def preview_boot(self, speed):
        if self.overlay.isVisible():
            items = self.overlay.items
        else:
            items = [c for c in self.channels.values() if not c.is_fan]
        self.boot.start(items, speed=speed, lead=0.1)

    def _fan(self, key, title, rpm):
        if rpm is None:
            return
        if rpm > 0:
            self.seen_fans.add(key)
        if key not in self.seen_fans:          # ignora conectores sem ventoinha
            return
        ck = "fan:" + key
        ch = self.channels.get(ck)
        if ch is None:
            if sum(1 for c in self.channels.values() if c.is_fan) >= MAX_FANS:
                return
            frac = self.cfg["fan_warn_pct"] / 100.0
            ch = Channel(self.animator, ck, title[:14], "RPM ×1000", 0, FAN_MAX_RPM,
                         FAN_MAX_RPM * frac, 1000, 5, label_div=1000, auto_scale=True,
                         warn_frac=frac, short=title[:9], is_fan=True)
            self.channels[ck] = ch
            self.window.add_fan_gauge(ch)
            self.overlay.refresh_items()
        ch.set(rpm)

    def _blink(self):
        hot = any(c.hot for c in self.channels.values() if not c.is_fan)
        self.blink = (not self.blink) if hot else False
        self.window.caution.set_on(self.blink)
        self.window.overheat.set_on(hot)

    # ---- janela <-> barra de tarefas
    def to_taskbar(self):
        self.boot.cancel()
        self.window.hide()
        self.window.setWindowState(Qt.WindowNoState)
        if self.cfg["tray_enabled"]:
            self.overlay.show_animated(boot=self.cfg["boot_when"] == "always")

    def restore(self):
        self.boot.cancel()
        self.overlay.hide_animated()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.window.isVisible():
                self.to_taskbar()
            else:
                self.restore()

    def _other_instance(self):
        sock = self.server.nextPendingConnection()
        if sock:
            sock.close()
        self.restore()

    # ---- configurações
    def open_settings(self):
        dlg = SettingsDialog(self)
        dlg.exec()

    def apply_config(self, new):
        old = self.cfg
        self.cfg = new
        apply_theme(new["colors"])
        FX["glow"] = bool(new["alert_glow"])
        self._update_pulse()
        for k, ch in self.channels.items():
            if ch.is_fps:
                ch.warn = new.get("fps_warn", 30)
                fmax = float(new.get("fps_max", 240))
                if fmax != ch.vmax:
                    ch.vmax, ch.major = fmax, self._fps_major(fmax)
            elif ch.is_fan:
                ch.warn_frac = new["fan_warn_pct"] / 100.0
                ch.warn = ch.vmax * ch.warn_frac
            else:
                ch.warn = new["warn"].get(k, ch.vmax)
            ch.redraw()
        self.reader.interval = float(new["interval"])
        self.window.apply_size(int(new["gauge_size"]))
        self.window.restyle()
        if bool(old.get("always_on_top")) != bool(new["always_on_top"]):
            vis = self.window.isVisible()
            self.window.setWindowFlag(Qt.WindowStaysOnTopHint, bool(new["always_on_top"]))
            if vis:
                self.window.show()
        self._apply_missing()
        if not new["tray_enabled"] and self.window.isVisible():
            self.overlay.hide_now()
        if (new["autostart"] != old.get("autostart")
                or (new["autostart"] and new["start_minimized"] != old.get("start_minimized"))):
            ok, msg = set_autostart(new["autostart"], new["start_minimized"])
            if ok:
                self.cfg["autostart_path"] = (sys.executable if new["autostart"] and
                                              getattr(sys, "frozen", False) else "")
            if not ok:
                self.cfg["autostart"] = old.get("autostart", False)
                QMessageBox.warning(None, "Iniciar com o Windows",
                                    "Não foi possível configurar o início automático.\n"
                                    "Abra o Cockpit Monitor como administrador e tente de novo.\n\n"
                                    + msg)
        save_config(self.cfg)

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.reader.shutdown()
        self.fps_mon.shutdown()
        self.tray.hide()
        self.overlay.hide_now()
        self.app.quit()


def dark_palette():
    pal = QPalette()
    roles = {
        QPalette.Window: "#14181c", QPalette.WindowText: "#e3e8ee", QPalette.Base: "#0d1013",
        QPalette.AlternateBase: "#1a1f24", QPalette.ToolTipBase: "#1a1f24",
        QPalette.ToolTipText: "#e3e8ee", QPalette.Text: "#e3e8ee", QPalette.Button: "#1d2229",
        QPalette.ButtonText: "#e3e8ee", QPalette.Highlight: "#2f8f4e",
        QPalette.HighlightedText: "#ffffff", QPalette.PlaceholderText: "#6c7783",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#5a636d"))
    return pal


def single_instance_server():
    """Garante uma cópia só; se já houver outra aberta, pede para ela aparecer."""
    name = f"{APP_NAME}-{getpass.getuser()}"
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return None
    QLocalServer.removeServer(name)
    srv = QLocalServer()
    srv.listen(name)
    return srv


def main():
    ap = argparse.ArgumentParser(description="Monitor de temperaturas estilo cockpit")
    ap.add_argument("--demo", action="store_true", help="usa valores simulados")
    ap.add_argument("--minimized", action="store_true", help="inicia já nos mini-relógios")
    ap.add_argument("--show", action="store_true", help="abre o painel completo ao iniciar")
    ap.add_argument("--interval", type=float, help="segundos entre leituras")
    ap.add_argument("--size", type=int, help="tamanho de cada mostrador (px)")
    ap.add_argument("--enable-autostart", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.enable_autostart:          # usado pelo instalador
        ok, _ = set_autostart(True)
        if ok:
            cfg = load_config()
            cfg["autostart"] = True
            save_config(cfg)
        return

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Cockpit Monitor")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    load_fonts()
    srv = single_instance_server()
    if srv is None:
        return
    ctrl = Controller(app, a, srv)  # noqa: F841 (mantém o controlador vivo)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
