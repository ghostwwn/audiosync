"""
auto-sync — beat-reactive BLE lights + spotify

listens to audio, detects beats, pushes colors to cheap bluetooth led strips.
shows what's playing in a terminal ui. macos only for spotify (applescript).

usage:
    python auto_sync.py
    python auto_sync.py --list-devices
    python auto_sync.py --device 3
    python auto_sync.py --bass-only
    python auto_sync.py --no-spotify

see README.md for setup.
"""

# ──────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ──────────────────────────────────────────────────────────────────────────────
import asyncio
import argparse
import colorsys
import io
import signal
import subprocess
import sys
import time
from collections import deque
from typing import Optional, Tuple, List

import numpy as np
import pyaudio
import requests
from bleak import BleakScanner, BleakClient
from PIL import Image

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.text import Text
from rich.table import Table
from rich import box

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

BLE_DEVICE_NAMES    = ("QHM", "Triones", "Magic", "HappyLight")
CHARACTERISTIC_UUID = "0000ffd9-0000-1000-8000-00805f9b34fb"
BLE_SCAN_TIMEOUT    = 5.0

AUDIO_FORMAT   = pyaudio.paFloat32
AUDIO_RATE     = 44100
AUDIO_CHUNK    = 1024
AUDIO_CHANNELS = 1

ENERGY_HISTORY_LEN = 40
BEAT_THRESHOLD     = 1.25
MIN_VOLUME         = 0.008
BEAT_COOLDOWN      = 0.25

BASS_LOW_HZ  = 60
BASS_HIGH_HZ = 250

HUE_STEP_BASE      = 0.04
HUE_STEP_INTENSITY = 0.08

# TUI
ART_WIDTH  = 36   # chars wide (half-block pixels)
ART_HEIGHT = 18   # rows tall  (each row = 2 pixel rows)
WAVEFORM_WIDTH = 48
HISTORY_BARS   = 24

# Spotify poll interval (seconds)
SPOTIFY_POLL = 3.0

# ──────────────────────────────────────────────────────────────────────────────
# PALETTE  — deep-space dark, neon accents
# ──────────────────────────────────────────────────────────────────────────────
C_BG       = "#0d0d1a"   # near-black navy
C_PANEL    = "#12112a"   # panel background (slightly lighter)
C_BORDER   = "#2a2550"   # subtle purple border
C_ACCENT1  = "#c77dff"   # lavender
C_ACCENT2  = "#7b2fff"   # electric violet
C_NEON     = "#00f5d4"   # cyan-mint
C_WARN     = "#ff6b6b"   # soft red
C_DIM      = "#4a4570"   # muted text
C_WHITE    = "#e8e4ff"   # soft white
C_SPOTIFY  = "#1db954"   # Spotify green

GRADIENT   = ["#ff006e","#c77dff","#7b2fff","#00f5d4","#3a86ff"]

# ──────────────────────────────────────────────────────────────────────────────
# GLOBALS
# ──────────────────────────────────────────────────────────────────────────────
_loop: asyncio.AbstractEventLoop
_beat_queue: asyncio.Queue
_running = True

# Shared state written by various threads/tasks, read by TUI renderer
state = {
    "beat_count":    0,
    "bpm":           0.0,
    "hue":           0.0,
    "intensity":     0.0,
    "waveform":      deque([0.0] * WAVEFORM_WIDTH, maxlen=WAVEFORM_WIDTH),
    "beat_history":  deque([0.0] * HISTORY_BARS,   maxlen=HISTORY_BARS),
    "ble_name":      "—",
    "ble_mac":       "—",
    "ble_ok":        False,
    "audio_device":  "—",
    "track":         "Waiting for Spotify…",
    "artist":        "",
    "album":         "",
    "progress_pct":  0.0,
    "duration_s":    0,
    "art_image":     None,   # PIL Image or None
    "art_text":      None,   # pre-rendered rich Text or None
    "spotify_ok":    False,
    "status_msg":    "Starting…",
    "start_time":    time.monotonic(),
    "beat_times":    deque(maxlen=8),   # recent beat timestamps for BPM
}

# ──────────────────────────────────────────────────────────────────────────────
# SPOTIFY  (macOS AppleScript, no auth required)
# ──────────────────────────────────────────────────────────────────────────────

def _osa(script: str) -> str:
    """Run a one-liner AppleScript, return stdout stripped."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2
        )
        return r.stdout.strip()
    except Exception:
        return ""


def spotify_running() -> bool:
    out = _osa('tell application "System Events" to '
               '(name of processes) contains "Spotify"')
    return out == "true"


def get_spotify_track() -> dict:
    """Return dict with track/artist/album/artwork_url/position/duration or empty."""
    if not spotify_running():
        return {}
    try:
        state_str = _osa('tell application "Spotify" to player state as string')
        if state_str not in ("playing", "paused"):
            return {}
        track    = _osa('tell application "Spotify" to name of current track')
        artist   = _osa('tell application "Spotify" to artist of current track')
        album    = _osa('tell application "Spotify" to album of current track')
        art_url  = _osa('tell application "Spotify" to artwork url of current track')
        position = _osa('tell application "Spotify" to player position')
        duration = _osa('tell application "Spotify" to duration of current track')
        return {
            "track":    track,
            "artist":   artist,
            "album":    album,
            "art_url":  art_url,
            "position": float(position) if position else 0.0,
            "duration": int(duration)   if duration else 0,   # ms
            "playing":  state_str == "playing",
        }
    except Exception:
        return {}


def fetch_image(url: str) -> Optional[Image.Image]:
    """Download image from URL, return PIL Image or None."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=4,
                         headers={"User-Agent": "AutoSync/2.0 (macOS)"})
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        pass
    return None


def image_to_rich(img: Image.Image, width: int = ART_WIDTH,
                  height: int = ART_HEIGHT) -> Text:
    """
    Convert PIL image to a rich Text using half-block (▀) characters.
    Each character encodes two vertical pixels: fg = top, bg = bottom.
    Result is width chars wide × height rows tall.
    """
    resized = img.resize((width, height * 2), Image.LANCZOS)
    px = resized.load()
    text = Text(no_wrap=True)
    for row in range(height):
        for col in range(width):
            r1, g1, b1 = px[col, row * 2]
            r2, g2, b2 = px[col, row * 2 + 1]
            text.append(
                "▀",
                style=Style(
                    color=f"#{r1:02x}{g1:02x}{b1:02x}",
                    bgcolor=f"#{r2:02x}{g2:02x}{b2:02x}",
                )
            )
        text.append("\n")
    return text


def placeholder_art(hue: float, width: int = ART_WIDTH,
                    height: int = ART_HEIGHT) -> Text:
    """Animated gradient placeholder when no album art is available."""
    text = Text(no_wrap=True)
    for row in range(height):
        for col in range(width):
            t = (col / width + row / height * 0.5 + hue) % 1.0
            r, g, b = (int(x * 255) for x in colorsys.hsv_to_rgb(t, 0.8, 0.5))
            r2, g2, b2 = (int(x * 255)
                          for x in colorsys.hsv_to_rgb((t + 0.05) % 1.0, 0.8, 0.4))
            text.append("▀", style=Style(
                color=f"#{r:02x}{g:02x}{b:02x}",
                bgcolor=f"#{r2:02x}{g2:02x}{b2:02x}",
            ))
        text.append("\n")
    return text

# ──────────────────────────────────────────────────────────────────────────────
# AUDIO — Beat Detector
# ──────────────────────────────────────────────────────────────────────────────

class BeatDetector:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue, bass_only: bool = False):
        self._loop      = loop
        self._queue     = queue
        self._bass_only = bass_only
        self._history   = deque(maxlen=ENERGY_HISTORY_LEN)
        self._last_beat = 0.0
        self._bass_bins = self._make_bass_mask()

    @staticmethod
    def _make_bass_mask() -> np.ndarray:
        freqs = np.fft.rfftfreq(AUDIO_CHUNK, d=1.0 / AUDIO_RATE)
        return (freqs >= BASS_LOW_HZ) & (freqs <= BASS_HIGH_HZ)

    def _bass_energy(self, audio: np.ndarray) -> float:
        spectrum = np.abs(np.fft.rfft(audio))
        bass = spectrum[self._bass_bins]
        return float(np.sqrt(np.mean(bass ** 2))) if len(bass) else 0.0

    def __call__(self, in_data, frame_count, time_info, status):
        audio = np.frombuffer(in_data, dtype=np.float32)
        rms   = float(np.sqrt(np.mean(audio ** 2)))

        # Update waveform state (downsample chunk to a few points)
        chunk_peak = float(np.max(np.abs(audio)))
        state["waveform"].append(min(chunk_peak * 2.0, 1.0))

        beat_energy = self._bass_energy(audio) if self._bass_only else rms
        self._history.append(beat_energy)
        avg = sum(self._history) / len(self._history) if self._history else 0.0

        now = time.monotonic()
        if (rms > MIN_VOLUME
                and beat_energy > avg * BEAT_THRESHOLD
                and now - self._last_beat > BEAT_COOLDOWN):
            self._last_beat = now
            intensity = min(beat_energy / max(avg, 1e-9), 3.0)
            state["beat_history"].append(intensity)
            state["beat_times"].append(now)
            asyncio.run_coroutine_threadsafe(
                self._queue.put(intensity), self._loop
            )

        return (in_data, pyaudio.paContinue)


def find_device(p: pyaudio.PyAudio,
                device_arg: Optional[int]) -> Optional[int]:
    if device_arg is not None:
        return device_arg
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0 and "VB-Cable" in info.get("name", ""):
            return i
    return None   # system default


def list_devices() -> None:
    p = pyaudio.PyAudio()
    console = Console()
    t = Table(box=box.SIMPLE, show_header=True, header_style=f"bold {C_ACCENT1}")
    t.add_column("Index", style=C_NEON,    justify="right", width=6)
    t.add_column("Name",  style=C_WHITE,   min_width=30)
    t.add_column("Inputs",style=C_DIM,     justify="right", width=7)
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        ch = int(info.get("maxInputChannels", 0))
        if ch > 0:
            t.add_row(str(i), info.get("name", ""), str(ch))
    console.print(t)
    p.terminate()

# ──────────────────────────────────────────────────────────────────────────────
# BLE
# ──────────────────────────────────────────────────────────────────────────────

async def find_ble_device() -> Optional[Tuple[str, str]]:
    """Returns (name, address) or None."""
    state["status_msg"] = "Scanning for BLE lights…"
    devices = await BleakScanner.discover(timeout=BLE_SCAN_TIMEOUT)
    for d in devices:
        if d.name and any(kw in d.name for kw in BLE_DEVICE_NAMES):
            return (d.name, d.address)
    return None


def make_rgb_payload(r: int, g: int, b: int) -> bytearray:
    return bytearray([0x56, r, g, b, 0x00, 0xF0, 0xAA])


def estimate_bpm() -> float:
    times = list(state["beat_times"])
    if len(times) < 2:
        return 0.0
    gaps = [times[i+1] - times[i] for i in range(len(times)-1)]
    avg_gap = sum(gaps) / len(gaps)
    return 60.0 / avg_gap if avg_gap > 0 else 0.0


async def ble_worker(client: BleakClient) -> None:
    hue = 0.0
    while _running:
        try:
            intensity = await asyncio.wait_for(_beat_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        hue = (hue + HUE_STEP_BASE + HUE_STEP_INTENSITY * (intensity - 1.0)) % 1.0
        r, g, b = (int(x * 255) for x in colorsys.hsv_to_rgb(hue, 1.0, 1.0))

        try:
            await client.write_gatt_char(
                CHARACTERISTIC_UUID, make_rgb_payload(r, g, b), response=False
            )
        except Exception as exc:
            state["status_msg"] = f"BLE write error: {exc}"
            state["ble_ok"] = False
            break

        state["hue"]       = hue
        state["intensity"] = intensity
        state["beat_count"] += 1
        state["bpm"]       = estimate_bpm()
        state["status_msg"] = "Syncing"

# ──────────────────────────────────────────────────────────────────────────────
# SPOTIFY POLLER  (async task)
# ──────────────────────────────────────────────────────────────────────────────

async def spotify_poller(use_spotify: bool) -> None:
    if not use_spotify:
        state["track"]  = "Spotify disabled"
        state["artist"] = "--no-spotify flag"
        return

    last_art_url = ""
    while _running:
        info = await asyncio.get_event_loop().run_in_executor(None, get_spotify_track)
        if info:
            state["spotify_ok"]    = True
            state["track"]         = info.get("track",  "Unknown Track")
            state["artist"]        = info.get("artist", "Unknown Artist")
            state["album"]         = info.get("album",  "")
            pos_s  = info.get("position", 0.0)
            dur_ms = info.get("duration", 0)
            dur_s  = dur_ms / 1000.0
            state["duration_s"]    = dur_s
            state["progress_pct"]  = (pos_s / dur_s) if dur_s > 0 else 0.0

            art_url = info.get("art_url", "")
            if art_url and art_url != last_art_url:
                last_art_url = art_url
                img = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_image, art_url
                )
                if img:
                    state["art_image"] = img
                    state["art_text"]  = image_to_rich(img)
        else:
            state["spotify_ok"]   = False
            state["track"]        = "Nothing playing"
            state["artist"]       = "Open Spotify and play a track"
            state["progress_pct"] = 0.0

        await asyncio.sleep(SPOTIFY_POLL)

# ──────────────────────────────────────────────────────────────────────────────
# TUI RENDERING
# ──────────────────────────────────────────────────────────────────────────────

def gradient_text(text: str, colors: List[str] = GRADIENT,
                  bold: bool = True) -> Text:
    t = Text(no_wrap=True)
    n = len(colors) - 1
    for i, ch in enumerate(text):
        frac  = i / max(len(text) - 1, 1)
        lo    = int(frac * n)
        hi    = min(lo + 1, n)
        alpha = frac * n - lo

        def hex_lerp(c1: str, c2: str, a: float) -> str:
            r1, g1, b1 = int(c1[1:3],16), int(c1[3:5],16), int(c1[5:7],16)
            r2, g2, b2 = int(c2[1:3],16), int(c2[3:5],16), int(c2[5:7],16)
            r = int(r1 + (r2 - r1) * a)
            g = int(g1 + (g2 - g1) * a)
            b = int(b1 + (b2 - b1) * a)
            return f"#{r:02x}{g:02x}{b:02x}"

        color = hex_lerp(colors[lo], colors[hi], alpha)
        t.append(ch, style=Style(color=color, bold=bold))
    return t


def make_header() -> Text:
    title = gradient_text("▸ AUTO SYNC", bold=True)
    sub   = Text("  by ", style=Style(color=C_DIM))
    sub.append("@ghostwwn", style=Style(color=C_ACCENT1, bold=True))
    sub.append("  ·  beat-reactive BLE light controller",
               style=Style(color=C_DIM, italic=True))
    line = Text()
    line.append_text(title)
    line.append_text(sub)
    return line


def make_waveform_bar(width: int = WAVEFORM_WIDTH) -> Text:
    """Animated waveform visualiser."""
    bars  = " ▁▂▃▄▅▆▇█"
    beats = list(state["waveform"])
    hue   = state["hue"]
    text  = Text(no_wrap=True)
    for i, level in enumerate(beats[-width:]):
        char  = bars[min(int(level * 8), 8)]
        h     = (hue + i / width * 0.4) % 1.0
        r,g,b = (int(x*255) for x in colorsys.hsv_to_rgb(h, 1.0, 0.9))
        text.append(char, style=f"#{r:02x}{g:02x}{b:02x}")
    return text


def make_beat_history(width: int = HISTORY_BARS) -> Text:
    """Vertical bar chart of recent beat intensities."""
    bars   = " ▂▃▄▅▆▇█"
    hist   = list(state["beat_history"])
    hue    = state["hue"]
    text   = Text(no_wrap=True)
    for i, intensity in enumerate(hist[-width:]):
        level = min(intensity / 3.0, 1.0)
        char  = bars[int(level * 7)]
        h     = (hue + i / width * 0.6) % 1.0
        r,g,b = (int(x*255) for x in colorsys.hsv_to_rgb(h, 0.9, 1.0))
        text.append(char, style=f"#{r:02x}{g:02x}{b:02x}")
    return text


def make_progress_bar(pct: float, width: int = 30) -> Text:
    filled = int(pct * width)
    text = Text(no_wrap=True)
    text.append("━" * filled,       style=Style(color=C_SPOTIFY))
    text.append("╌" * (width-filled), style=Style(color=C_DIM))
    return text


def color_dot(r: int, g: int, b: int) -> Text:
    t = Text()
    t.append("⬤ ", style=f"#{r:02x}{g:02x}{b:02x}")
    return t


def fmt_time(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


def make_track_panel(term_width: int) -> Panel:
    """Left panel: album art + track info."""
    hue   = state["hue"]
    r, g, b = (int(x*255) for x in colorsys.hsv_to_rgb(hue, 1.0, 1.0))

    # Album art or animated placeholder
    if state["art_text"]:
        art = state["art_text"]
    else:
        art = placeholder_art(hue)

    art_block = Align.center(art)

    # Track name (truncated)
    track_name = state["track"]
    if len(track_name) > 30:
        track_name = track_name[:28] + "…"

    track_line = Text(no_wrap=True)
    track_line.append_text(color_dot(r, g, b))
    track_line.append(track_name, style=Style(color=C_WHITE, bold=True))

    artist_line = Text(no_wrap=True)
    artist_line.append("  ", style="")
    artist_line.append(state["artist"], style=Style(color=C_ACCENT1))

    album_line = Text(no_wrap=True)
    album_line.append("  ", style="")
    album_line.append(state["album"],  style=Style(color=C_DIM, italic=True))

    # Progress bar
    pos_s = state["progress_pct"] * state["duration_s"]
    dur_s = state["duration_s"]
    prog_bar = make_progress_bar(state["progress_pct"], width=ART_WIDTH)
    time_line = Text(no_wrap=True)
    time_line.append(fmt_time(pos_s), style=Style(color=C_DIM))
    time_line.append("  ")
    time_line.append_text(prog_bar)
    time_line.append("  ")
    time_line.append(fmt_time(dur_s), style=Style(color=C_DIM))

    spotify_badge = Text()
    if state["spotify_ok"]:
        spotify_badge.append("●", style=Style(color=C_SPOTIFY))
        spotify_badge.append(" SPOTIFY", style=Style(color=C_SPOTIFY, bold=True))
    else:
        spotify_badge.append("○", style=Style(color=C_DIM))
        spotify_badge.append(" SPOTIFY", style=Style(color=C_DIM))

    content = Group(
        art_block,
        Text(""),
        Align.center(track_line),
        Align.center(artist_line),
        Align.center(album_line),
        Text(""),
        Align.center(time_line),
        Text(""),
        Align.center(spotify_badge),
    )

    border_color = f"#{r:02x}{g:02x}{b:02x}" if state["beat_count"] % 2 == 0 else C_BORDER
    return Panel(
        content,
        border_style=Style(color=border_color),
        box=box.ROUNDED,
        padding=(0, 1),
    )


def make_beat_panel() -> Panel:
    """Right panel: live beat + BLE + audio stats."""
    hue = state["hue"]
    r, g, b = (int(x*255) for x in colorsys.hsv_to_rgb(hue, 1.0, 1.0))
    intensity = state["intensity"]

    # ── Big beat counter ──
    count_text = gradient_text(str(state["beat_count"]).zfill(5), bold=True)

    # ── Intensity ring (ASCII) ──
    level = min(intensity / 3.0, 1.0)
    bar_w = 20
    filled = int(level * bar_w)
    intensity_bar = Text(no_wrap=True)
    intensity_bar.append("▐", style=Style(color=C_DIM))
    for i in range(bar_w):
        if i < filled:
            h2 = (hue + i / bar_w * 0.3) % 1.0
            ri,gi,bi = (int(x*255) for x in colorsys.hsv_to_rgb(h2, 1.0, 1.0))
            intensity_bar.append("█", style=f"#{ri:02x}{gi:02x}{bi:02x}")
        else:
            intensity_bar.append("░", style=Style(color=C_DIM))
    intensity_bar.append("▌", style=Style(color=C_DIM))

    # ── BPM display ──
    bpm = state["bpm"]
    bpm_text = Text(no_wrap=True)
    bpm_text.append(f"{bpm:5.1f} ", style=Style(color=C_WHITE, bold=True))
    bpm_text.append("BPM", style=Style(color=C_DIM))

    # ── Waveform ──
    waveform = make_waveform_bar()
    beat_hist = make_beat_history()

    # ── BLE status ──
    ble_line = Text(no_wrap=True)
    if state["ble_ok"]:
        ble_line.append("● ", style=Style(color=C_NEON))
        ble_line.append(state["ble_name"], style=Style(color=C_WHITE, bold=True))
        ble_line.append(f"  {state['ble_mac']}", style=Style(color=C_DIM))
    else:
        ble_line.append("○ ", style=Style(color=C_WARN))
        ble_line.append("Disconnected", style=Style(color=C_WARN))

    # ── Audio device ──
    audio_line = Text(no_wrap=True)
    audio_line.append("⟁ ", style=Style(color=C_ACCENT1))
    audio_line.append(state["audio_device"], style=Style(color=C_DIM))

    # ── Runtime ──
    elapsed = int(time.monotonic() - state["start_time"])
    rt = Text(no_wrap=True)
    rt.append("⏱ ", style=Style(color=C_DIM))
    rt.append(fmt_time(elapsed), style=Style(color=C_DIM))

    # ── Current LED color swatch ──
    swatch = Text(no_wrap=True)
    swatch.append("  LED  ", style=Style(color=C_DIM))
    swatch.append("████", style=f"#{r:02x}{g:02x}{b:02x}")
    swatch.append(f"  #{r:02x}{g:02x}{b:02x}", style=Style(color=C_DIM))

    # ── Status dot ──
    status_text = Text(no_wrap=True)
    status_text.append("▸ ", style=Style(color=C_NEON))
    status_text.append(state["status_msg"], style=Style(color=C_WHITE))

    rows: List = [
        Text(""),
        Align.center(Text("BEATS", style=Style(color=C_DIM, bold=True))),
        Align.center(count_text),
        Text(""),
        Align.center(Text("INTENSITY", style=Style(color=C_DIM))),
        Align.center(intensity_bar),
        Text(""),
        Align.center(bpm_text),
        Text(""),
        Rule(style=Style(color=C_BORDER)),
        Text(""),
        Align.center(Text("WAVEFORM", style=Style(color=C_DIM))),
        Align.center(waveform),
        Text(""),
        Align.center(Text("BEAT HISTORY", style=Style(color=C_DIM))),
        Align.center(beat_hist),
        Text(""),
        Rule(style=Style(color=C_BORDER)),
        Text(""),
        ble_line,
        audio_line,
        swatch,
        rt,
        Text(""),
        status_text,
    ]

    border_color = f"#{r:02x}{g:02x}{b:02x}"
    return Panel(
        Group(*rows),
        border_style=Style(color=border_color),
        box=box.ROUNDED,
        padding=(0, 1),
    )


def make_footer() -> Text:
    t = Text(no_wrap=True)
    t.append("  ctrl+c", style=Style(color=C_DIM, bold=True))
    t.append(" to stop  ·  ", style=Style(color=C_DIM))
    t.append("@ghostwwn", style=Style(color=C_ACCENT1, bold=True))
    t.append("  ·  AUTO SYNC v2.0", style=Style(color=C_DIM))
    return Align.center(t)


def render_layout(term_width: int) -> Group:
    header = make_header()
    track_panel = make_track_panel(term_width)
    beat_panel  = make_beat_panel()

    # Side-by-side using a Table (more reliable than Layout for dynamic content)
    tbl = Table.grid(expand=True)
    tbl.add_column(ratio=45)
    tbl.add_column(ratio=55)
    tbl.add_row(track_panel, beat_panel)

    footer = make_footer()

    return Group(
        Text(""),
        Align.center(header),
        Rule(style=Style(color=C_BORDER)),
        tbl,
        Rule(style=Style(color=C_BORDER)),
        footer,
    )

# ──────────────────────────────────────────────────────────────────────────────
# SPLASH SCREEN
# ──────────────────────────────────────────────────────────────────────────────

SPLASH = r"""
 
    \   |   | __ \_ _|  _ \   ___|\ \   /  \  |  ___| 
   _ \  |   | |   | |  |   |\___ \ \   /    \ | |     
  ___ \ |   | |   | |  |   |      |   |   |\  | |     
_/    _\\___/ ____/___|\___/ _____/   |  _| \_|\____| 
"""

def print_splash(console: Console) -> None:
    console.print()
    for line in SPLASH.split("\n"):
        console.print(Align.center(gradient_text(line)), highlight=False)
    console.print()
    badge = Text()
    badge.append("            crafted by ", style=Style(color=C_DIM))
    badge.append("@ghostwwn", style=Style(color=C_ACCENT1, bold=True))
    console.print(Align.center(badge))
    console.print()

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    global _running

    console = Console()
    print_splash(console)

    # ── BLE ──────────────────────────────────────────────────────────────────
    result = await find_ble_device()
    if not result:
        console.print(f"[{C_WARN}]✗  No BLE lights found. Make sure they're powered on and in range.[/]")
        return
    ble_name, ble_mac = result
    state["ble_name"] = ble_name
    state["ble_mac"]  = ble_mac

    async with BleakClient(ble_mac) as client:
        state["ble_ok"] = True
        state["status_msg"] = "Connected"

        # Power ON + warm white
        await client.write_gatt_char(
            CHARACTERISTIC_UUID, bytearray([0xCC, 0x23, 0x33]), response=False
        )
        await asyncio.sleep(0.1)
        await client.write_gatt_char(
            CHARACTERISTIC_UUID, make_rgb_payload(255, 140, 30), response=False
        )

        # ── Audio ─────────────────────────────────────────────────────────────
        p       = pyaudio.PyAudio()
        dev_idx = find_device(p, args.device)
        if dev_idx is not None:
            dev_name = p.get_device_info_by_index(dev_idx).get("name", "?")
        else:
            dev_name = "System Default Mic"
        state["audio_device"] = dev_name

        detector = BeatDetector(_loop, _beat_queue, bass_only=args.bass_only)
        stream = p.open(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            input=True,
            input_device_index=dev_idx,
            frames_per_buffer=AUDIO_CHUNK,
            stream_callback=detector,
        )
        stream.start_stream()
        state["status_msg"] = "Listening…"

        # ── Spotify poller ────────────────────────────────────────────────────
        spotify_task = asyncio.create_task(
            spotify_poller(not args.no_spotify)
        )

        # ── BLE worker ────────────────────────────────────────────────────────
        ble_task = asyncio.create_task(ble_worker(client))

        # ── Live TUI ─────────────────────────────────────────────────────────
        try:
            with Live(
                render_layout(console.width),
                console=console,
                refresh_per_second=20,
                screen=True,
            ) as live:
                while _running:
                    live.update(render_layout(console.width))
                    await asyncio.sleep(0.05)
        finally:
            _running = False
            spotify_task.cancel()
            ble_task.cancel()
            stream.stop_stream()
            stream.close()
            p.terminate()
            console.clear()
            print_splash(console)
            console.print(Align.center(
                Text(f"  Session ended · {state['beat_count']} beats synced · @ghostwwn  ",
                     style=Style(color=C_ACCENT1))
            ))
            console.print()


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def _shutdown(sig, frame):
    global _running
    _running = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AUTO SYNC — beat-reactive BLE light controller by @ghostwwn"
    )
    parser.add_argument("--list-devices", action="store_true",
                        help="List audio input devices and exit")
    parser.add_argument("--device", type=int, default=None, metavar="INDEX",
                        help="Audio input device index (see --list-devices)")
    parser.add_argument("--bass-only", action="store_true",
                        help="React only to bass frequencies (60–250 Hz)")
    parser.add_argument("--no-spotify", action="store_true",
                        help="Disable Spotify integration")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        sys.exit(0)

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _beat_queue = asyncio.Queue()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        _loop.run_until_complete(main(args))
    finally:
        _loop.close()
