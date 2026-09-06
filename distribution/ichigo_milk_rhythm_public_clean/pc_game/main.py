
from __future__ import annotations

import argparse
import glob
import json
import math
import os
# macOS/RetinaでSDLができるだけ高DPI描画を使うようにします。
os.environ.setdefault("SDL_VIDEO_HIGHDPI_DISABLED", "0")
import queue
import random
import sys
import threading
import time
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chapters import CHAPTERS, get_chapter, BOARD, GOAL_INDEX, get_space
try:
    from ranking_sync import RankingSync
except ImportError:
    # 配布版では個人情報・送信キーを含むランキング送信機能を同梱しません。
    RankingSync = None

try:
    import pygame
except ImportError:
    print("pygame がありません。`python -m pip install -r requirements.txt` を実行してください。")
    raise

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception:  # pyserial is optional in keyboard mode
    serial = None
    list_ports = None

try:
    import cv2  # type: ignore
except Exception:  # OpenCV is optional; video backgrounds fall back to static colors.
    cv2 = None

BASE = Path(__file__).resolve().parent
LEADERBOARD_PATH = BASE / "scores.json"
PROFILE_PATH = BASE / "profile.json"
TAMBOURINE_SFX_PATH = BASE / "assets" / "sfx" / "tambourine_hit.wav"
TAMBOURINE_SFX_CANDIDATES = [
    BASE / "assets" / "sfx" / "tambourine_hit.wav",
    BASE / "assets" / "sfx" / "tambourine_hit.mp3",
]
MAX_PLAYER_NAME = 16
MAX_RANKING_RECORDS = 100
WIDTH, HEIGHT = 1280, 720
GAME_VERSION = "v76-final-score-sound-order"
LANE_Y = 410
HIT_X = 210
SPAWN_X = WIDTH + 80
NOTE_SPEED = 520.0  # px/sec


def _set_screen_size(w: int, h: int) -> None:
    """画面サイズに依存する定数を再計算する。"""
    global WIDTH, HEIGHT, LANE_Y, HIT_X, SPAWN_X
    WIDTH, HEIGHT = w, h
    LANE_Y = int(h * 0.57)
    HIT_X = int(w * 0.16)
    SPAWN_X = w + 80
JUDGE = [
    (0.045, "PERFECT", 1000),
    (0.090, "GREAT", 700),
    (0.140, "GOOD", 300),
]
MISS_WINDOW = 0.180
PROGRESS_CHECKPOINTS = [60.0]
CLEAR_GOOD_RATE = 0.80

# 物理タンバリン入力の重複・誤爆対策。
# v31: 「1振りで“タタ”と2回拾われ必ずBAD」問題を修正。
#   - ゾーンで感度(デバウンス)を切替: 通常は短め(連打も拾う)、ロールはさらに高感度。
#   - 成功HIT直後に“空振り”が来たら「1振りの反動」とみなしBADにしない(REBOUND)。
# - INPUT_DEBOUNCE_NORMAL_SEC: 通常ノーツの最小HIT間隔。連打(8分=BPM150で200ms)より小さく。
# - INPUT_DEBOUNCE_ROLL_SEC:   ロールゾーンの最小HIT間隔。シャカシャカを取りこぼさないよう短く。
# - REBOUND_EMPTY_WINDOW_SEC:  成功直後この秒数内の“空振り”は反動として無視(BADにしない)。
# - INPUT_NOTE_GATE_SEC: ノーツから何秒離れたHITまで受け付けるか(MISS窓より少し小さく)。
INPUT_DEBOUNCE_NORMAL_SEC = 0.085
INPUT_DEBOUNCE_ROLL_SEC = 0.025
# いちごを叩いた後やROLL終了直後の「戻り振動」はBADにしない。
# FRDM側の連打取得を55ms間隔にしても、通常ノーツの誤BADを防ぐ。
REBOUND_EMPTY_WINDOW_SEC = 0.32
INPUT_NOTE_GATE_SEC = 0.135
INPUT_MIN_POWER = 0
SERIAL_MSG_DEBOUNCE_MS = 25  # ROLLの細かい連打は通し、ほぼ同時の重複だけ除去
CHAPTER_INPUT_GUARD_SEC = 1.0
SONG_TIME_UP_SEC = 3.0
RESULT_INPUT_GUARD_SEC = 1.2
FATE_SUSPENSE_SEC = 2.8
READY_CALIBRATION_SEC = 3.0
GAMEOVER_AUTO_ADVANCE_SEC = 29.0
GAMEOVER_BUTTON_REVEAL_SEC = 7.0
STORY_CHARS_PER_SEC = 24.0
STORY_LINE_PAUSE_CHARS = 5

# いちご牛乳(キメ)= タンバリンを上に振り上げる動きで取るとボーナス。
# 重力ベクトルから「上」を求め、叩いた瞬間の動的加速度が上向きに大きければ「上げ振り」と判定。
KIME_UP_THRESHOLD_MG = 380   # 上向き成分がこれ以上で「上げ振り」候補。実機用に少し甘め
KIME_UP_RATIO = 0.85         # 上向き成分が横成分に近ければ上げ振り扱い
KIME_BONUS = 300             # 上げ振りでキメた時の加点



@dataclass
class Song:
    id: str
    title: str
    artist: str
    audio: Path
    cover: Path | None
    mv: Path | None
    bpm: float
    offset: float
    chart: Path
    duration: float


class SerialHitReader(threading.Thread):
    def __init__(self, port: str, baud: int, out_queue: queue.Queue[dict[str, Any]]):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.out_queue = out_queue
        self.stop_flag = threading.Event()
        self.status = "serial: starting"

    def run(self) -> None:
        if serial is None:
            self.status = "serial: pyserial not installed"
            return
        try:
            with serial.Serial(self.port, self.baud, timeout=0.02) as ser:
                self.status = f"serial: connected {self.port}"
                while not self.stop_flag.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore").strip()
                    msg: dict[str, Any] | None = None
                    if line.startswith("{"):
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            msg = None
                    elif line.startswith("HIT"):
                        # Zephyrファームウェアの "HIT <strength_mg>" 形式
                        parts = line.split()
                        try:
                            power = float(parts[1]) if len(parts) > 1 else 700.0
                        except ValueError:
                            power = 700.0
                        msg = {"type": "hit", "power": power}
                    elif line.startswith("FACE"):
                        parts = line.split()
                        try:
                            msg = {"type": "face", "face": int(parts[1])}
                        except (ValueError, IndexError):
                            msg = None
                    if msg is None:
                        continue
                    msg["pc_time"] = time.monotonic()
                    self.out_queue.put(msg)
        except Exception as e:
            self.status = f"serial error: {e}"

    def stop(self) -> None:
        self.stop_flag.set()


_UI_FONT_PATH_CACHE: dict[bool, str | None] = {}


def find_ui_font_path(bold: bool = False) -> str | None:
    """Find the UI font path.

    v39: 文字化け対策として、判定用の get_metrics チェックをやめる。
    まず同梱の ZenMaruGothic を必ず使う。これが前の見た目の本命。
    DotGothic16 は同梱されたまま残すが、本文UIには使わない。
    """
    if bold in _UI_FONT_PATH_CACHE:
        return _UI_FONT_PATH_CACHE[bold]

    # 1) ユーザーが明示したフォントだけ最優先。
    env_font = os.environ.get("ICHIGO_MILK_FONT")
    if env_font and os.path.exists(env_font):
        _UI_FONT_PATH_CACHE[bold] = env_font
        return env_font

    # 2) 同梱フォントを無条件で最優先。
    #    pygame.font.get_metrics() は環境により日本語の有無チェックに失敗することがあるので使わない。
    bundle = BASE / "assets" / "fonts"
    bundled_candidates = [
        bundle / ("ZenMaruGothic-Bold.ttf" if bold else "ZenMaruGothic-Regular.ttf"),
        bundle / "ZenMaruGothic-Regular.ttf",
        bundle / "ZenMaruGothic-Bold.ttf",
    ]
    for candidate in bundled_candidates:
        if candidate.exists():
            _UI_FONT_PATH_CACHE[bold] = str(candidate)
            return str(candidate)

    # 3) Macの日本語フォントへフォールバック。ここでも文字化けしやすいデフォルトフォントへすぐ落とさない。
    exact_paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc" if bold else "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W5.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in exact_paths:
        if os.path.exists(path):
            _UI_FONT_PATH_CACHE[bold] = path
            return path

    preferred_names = [
        "Hiragino Sans W6" if bold else "Hiragino Sans W3",
        "Hiragino Sans",
        "Hiragino Kaku Gothic ProN W6" if bold else "Hiragino Kaku Gothic ProN W3",
        "Hiragino Kaku Gothic ProN",
        "Hiragino Maru Gothic ProN",
        "Yu Gothic Medium",
        "Yu Gothic",
        "YuGothic",
        "Noto Sans CJK JP",
        "Noto Sans JP",
        "Meiryo",
        "Arial Unicode MS",
    ]
    for name in preferred_names:
        try:
            path = pygame.font.match_font(name, bold=bold)
            if path and os.path.exists(path) and "/opt/X11/" not in path:
                _UI_FONT_PATH_CACHE[bold] = path
                return path
        except Exception:
            pass

    _UI_FONT_PATH_CACHE[bold] = None
    return None


_FONT_ERROR_PRINTED: set[tuple[str, int]] = set()


def font(size: int, bold: bool = False) -> pygame.font.Font:
    """Return the bundled clean Japanese UI font."""
    path = find_ui_font_path(bold)
    if path:
        try:
            f = pygame.font.Font(path, size)
            # ZenMaruGothic-Bold.ttf を直接選ぶので、余計な疑似太字はかけない。
            if "ZenMaruGothic" not in Path(path).name:
                f.set_bold(bold)
            return f
        except Exception as e:
            key = (path, size)
            if key not in _FONT_ERROR_PRINTED:
                print(f"フォントを開けませんでした: {path} ({e})")
                _FONT_ERROR_PRINTED.add(key)

    # 最終手段。ここに来たら assets/fonts のZenMaruGothicが見つかっていない。
    # 起動は継続するが、画面下部にフォント不足の警告を出す。
    f = pygame.font.Font(None, size)
    f.set_bold(bold)
    return f

def scaled_px(px: int, *, min_px: int | None = None, max_px: int | None = None) -> int:
    """現在の画面サイズに応じて、UI用のピクセル値をゆるく拡大縮小する。"""
    scale = min(WIDTH / 1280.0, HEIGHT / 720.0)
    value = int(round(px * scale))
    if min_px is not None:
        value = max(min_px, value)
    if max_px is not None:
        value = min(max_px, value)
    return max(1, value)


_DOT_FONT_CACHE: dict[int, pygame.font.Font] = {}


def dot_font(size: int) -> pygame.font.Font:
    """スコア・コンボ・判定用のドット(LED)フォント。なければ通常フォント。"""
    if size in _DOT_FONT_CACHE:
        return _DOT_FONT_CACHE[size]
    path = BASE / "assets" / "fonts" / "DotGothic16-Regular.ttf"
    try:
        f = pygame.font.Font(str(path), size) if path.exists() else font(size, True)
    except Exception:
        f = font(size, True)
    _DOT_FONT_CACHE[size] = f
    return f


_GLOW_CACHE: dict[tuple[int, tuple[int, int, int]], pygame.Surface] = {}


def glow_surface(radius: int, color: tuple[int, int, int]) -> pygame.Surface:
    """加算合成(BLEND_ADD)用のグロー。中心ほど明るい円。"""
    key = (radius, color)
    if key in _GLOW_CACHE:
        return _GLOW_CACHE[key]
    surf = pygame.Surface((radius * 2, radius * 2))
    for i in range(radius, 0, -2):
        k = (1.0 - i / radius) ** 2
        c = (int(color[0] * k), int(color[1] * k), int(color[2] * k))
        pygame.draw.circle(surf, c, (radius, radius), i)
    _GLOW_CACHE[key] = surf
    return surf


_LOGO_CACHE: dict[int, pygame.Surface] = {}


def logo_image(max_width: int) -> "pygame.Surface | None":
    """位置GOMILKロゴをmax_width幅に縮小して返す(キャッシュつき)。"""
    if max_width in _LOGO_CACHE:
        return _LOGO_CACHE[max_width]
    path = BASE / "assets" / "logo" / "gomilk_logo.png"
    if not path.exists():
        return None
    img = pygame.image.load(str(path)).convert_alpha()
    w, h = img.get_size()
    if w > max_width:
        img = pygame.transform.smoothscale(img, (max_width, int(h * max_width / w)))
    _LOGO_CACHE[max_width] = img
    return img


def draw_enter_icon(surface: pygame.Surface, center, color=(255, 245, 255)) -> pygame.Rect:
    """Enterキー風アイコン(角丸 + ↵)。"""
    rect = pygame.Rect(0, 0, 50, 30)
    rect.center = center
    pygame.draw.rect(surface, color, rect, border_radius=8)
    pygame.draw.rect(surface, (60, 40, 80), rect, 2, border_radius=8)
    cx, cy = rect.center
    pygame.draw.line(surface, (60, 40, 80), (cx + 10, cy - 6), (cx + 10, cy + 2), 2)
    pygame.draw.line(surface, (60, 40, 80), (cx + 10, cy + 2), (cx - 8, cy + 2), 2)
    pygame.draw.polygon(surface, (60, 40, 80),
                        [(cx - 8, cy + 2), (cx - 3, cy - 3), (cx - 3, cy + 7)])
    return rect


def draw_delete_icon(surface: pygame.Surface, center, color=(255, 245, 255)) -> pygame.Rect:
    """deleteキー風アイコン(角丸 + ⌫)。"""
    rect = pygame.Rect(0, 0, 58, 30)
    rect.center = center
    pygame.draw.rect(surface, color, rect, border_radius=8)
    pygame.draw.rect(surface, (60, 40, 80), rect, 2, border_radius=8)
    cx, cy = rect.center
    pts = [(cx - 18, cy), (cx - 10, cy - 8), (cx + 14, cy - 8),
           (cx + 14, cy + 8), (cx - 10, cy + 8)]
    pygame.draw.polygon(surface, (60, 40, 80), pts, 2)
    pygame.draw.line(surface, (60, 40, 80), (cx - 2, cy - 4), (cx + 8, cy + 4), 2)
    pygame.draw.line(surface, (60, 40, 80), (cx + 8, cy - 4), (cx - 2, cy + 4), 2)
    return rect


def draw_heart(surface: pygame.Surface, color, center, r: float) -> None:
    """ハート♥型を描画。"""
    cx, cy = center
    pts = []
    for i in range(24):
        a = i / 24 * math.tau
        x = 16 * math.sin(a) ** 3
        y = -(13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a))
        pts.append((cx + x * r / 18, cy + y * r / 18))
    pygame.draw.polygon(surface, color, pts)


def draw_star(surface: pygame.Surface, color, center, r: float, rot: float = 0.0) -> None:
    pts = []
    for i in range(8):
        ang = rot + i * math.pi / 4
        rad = r if i % 2 == 0 else r * 0.42
        pts.append((center[0] + math.cos(ang) * rad, center[1] + math.sin(ang) * rad))
    pygame.draw.polygon(surface, color, pts)


JUDGE_COLORS = {
    "PERFECT": (255, 224, 92),
    "GREAT": (123, 232, 200),
    "GOOD": (143, 177, 255),
    "BAD": (220, 150, 160),
    "MISS": (150, 150, 165),
    "EMPTY": (175, 165, 195),
    "ROLL": (255, 200, 100),  # ロール: オレンジ寄り黄
    "KIME": (255, 150, 210),  # キメ(いちご牛乳を上げ振り): ピンク
    "UP": (255, 185, 210),    # いちご牛乳を横振りした時の注意
}
JUDGE_LABEL = {"EMPTY": "から振り", "ROLL": "ROLL!", "KIME": "キメ！", "UP": "上げて!"}


def render_fit(font_obj: pygame.font.Font, text: str, color: tuple[int, int, int], max_width: int, min_size: int = 16, bold: bool = False) -> pygame.Surface:
    """Render text, shrinking Japanese titles only when they would overflow."""
    surf = font_obj.render(text, True, color)
    if surf.get_width() <= max_width:
        return surf
    size = max(min_size, font_obj.get_height() - 6)
    while size >= min_size:
        f = font(size, bold)
        surf = f.render(text, True, color)
        if surf.get_width() <= max_width:
            return surf
        size -= 2
    return surf

def load_songs() -> list[Song]:
    with open(BASE / "assets/songs.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    songs = []
    for row in data:
        cover = BASE / row["cover"] if row.get("cover") else None
        mv = BASE / row["mv"] if row.get("mv") else None
        songs.append(
            Song(
                id=row["id"],
                title=row["title"],
                artist=row.get("artist", ""),
                audio=BASE / row["audio"],
                cover=cover if cover and cover.exists() else None,
                mv=mv if mv and mv.exists() else None,
                bpm=float(row.get("bpm", 120)),
                offset=float(row.get("offset", 0)),
                chart=BASE / row["chart"],
                duration=float(row.get("duration", 180)),
            )
        )
    return songs


def _auto_mark_kime(notes: list[dict[str, Any]]) -> None:
    """譜面に明示されていない場合、見せ場になりやすいノーツを kime 扱いにする。

    ルールは控えめにして、
    - ロール直後の最初の通常ノーツ
    - 曲の最後の通常ノーツ
    を kime にする。
    すでに kind が指定されているノーツは上書きしない。
    """
    after_roll = False
    last_tap_index = None
    for i, n in enumerate(notes):
        kind = n.get("kind", n.get("type", "tap"))
        if kind == "roll":
            after_roll = True
            continue
        if kind == "tap":
            last_tap_index = i
            if after_roll:
                n["kind"] = "kime"
                n["type"] = "kime"
                after_roll = False
        else:
            after_roll = False
    if last_tap_index is not None:
        last = notes[last_tap_index]
        if last.get("kind", "tap") == "tap":
            last["kind"] = "kime"
            last["type"] = "kime"


def load_chart(path: Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        chart = json.load(f)
    notes = chart["notes"]
    for i, n in enumerate(notes):
        n.setdefault("kind", n.get("type", "tap"))
        n.setdefault("type", n.get("kind", "tap"))
        n["idx"] = i
        n["hit"] = False
        n["missed"] = False
        # ロールノーツの追加プロパティ
        if n.get("kind") == "roll":
            n.setdefault("end_time", float(n["time"]) + 1.0)
            n.setdefault("expected_hits", 4)
            n["hit_count"] = 0
            n["completed"] = False

    if not any(n.get("kind") in ("kime", "accent", "strong") for n in notes):
        _auto_mark_kime(notes)

    for n in notes:
        if n.get("kind") in ("accent", "strong"):
            n["kind"] = "kime"
            n["type"] = "kime"
    return notes


def load_json_file(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"{path.name} を読めませんでした: {e}")
    return default


def save_json_file(path: Path, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"{path.name} を保存できませんでした: {e}")


def clean_player_name(name: str) -> str:
    name = "".join(ch for ch in name.strip() if ch not in "\r\n\t")
    return name[:MAX_PLAYER_NAME] or "プレイヤー"


def filter_name_input(text: str) -> str:
    """名前入力欄に入れてよい文字だけ残す。IME確定文字もここを通す。"""
    return "".join(ch for ch in text if ch.isprintable() and ch not in "\r\n\t")


def load_leaderboard() -> dict[str, list[dict[str, Any]]]:
    data = load_json_file(LEADERBOARD_PATH, {})
    if not isinstance(data, dict):
        return {}
    clean: dict[str, list[dict[str, Any]]] = {}
    for song_id, rows in data.items():
        if isinstance(song_id, str) and isinstance(rows, list):
            clean[song_id] = [r for r in rows if isinstance(r, dict)]
    return clean


def sort_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """得点順。同点は第1位エンド、最大コンボ、先着順で決める。"""
    return sorted(rows, key=lambda r: (
        -int(r.get("score", 0)),
        -(1 if r.get("ending") == "bestten_first" else 0),
        -int(r.get("max_combo", 0)),
        str(r.get("played_at", "")),
    ))


def load_profile() -> dict[str, Any]:
    data = load_json_file(PROFILE_PATH, {})
    return data if isinstance(data, dict) else {}


def leaderboard_key(song: Song, mode: str | None = None) -> str:
    # v10からは、おためし/フルを分けず、曲ごとに1つのランキングへ保存します。
    return song.id


def mode_label(mode: str) -> str:
    return "チャレンジ"


def fit_image(surface: pygame.Surface, size: tuple[int, int]) -> pygame.Surface:
    sw, sh = surface.get_size()
    tw, th = size
    scale = max(tw / sw, th / sh)
    img = pygame.transform.smoothscale(surface, (int(sw * scale), int(sh * scale)))
    rect = img.get_rect(center=(tw // 2, th // 2))
    crop = pygame.Surface(size, pygame.SRCALPHA)
    crop.blit(img, rect)
    return crop


def fit_image_contain(surface: pygame.Surface, size: tuple[int, int]) -> pygame.Surface:
    """画像全体が見えるように中央に収める。余白はストーリー画面になじむ濃い紫。"""
    sw, sh = surface.get_size()
    tw, th = size
    scale = min(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    img = pygame.transform.smoothscale(surface, (nw, nh))
    canvas = pygame.Surface(size, pygame.SRCALPHA)
    canvas.fill((34, 22, 58, 255))
    rect = img.get_rect(center=(tw // 2, th // 2))
    canvas.blit(img, rect)
    return canvas


class VideoBackground:
    """Loop an MP4 as a silent pygame background.

    pygame does not have reliable built-in MP4 playback, so OpenCV is used when
    installed. If OpenCV cannot open the file, the caller simply uses a static
    fallback background.
    """

    def __init__(self, path: Path | None, size: tuple[int, int]):
        self.path = path
        self.size = size
        self.cap = None
        self.fps = 24.0
        self.next_frame_at = 0.0
        self.frame: pygame.Surface | None = None
        self.enabled = False
        if cv2 is None or path is None or not path.exists():
            return
        try:
            self.cap = cv2.VideoCapture(str(path))
            if self.cap and self.cap.isOpened():
                fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 24.0)
                if 1.0 <= fps <= 120.0:
                    self.fps = fps
                self.enabled = True
        except Exception as e:
            print(f"MV背景を開けませんでした: {e}")
            self.cap = None
            self.enabled = False

    def get_frame(self) -> pygame.Surface | None:
        if not self.enabled or self.cap is None or cv2 is None:
            return None
        now = time.monotonic()
        if self.frame is not None and now < self.next_frame_at:
            return self.frame
        try:
            ok, frame = self.cap.read()
            if not ok:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.cap.read()
            if not ok or frame is None:
                return self.frame
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w = frame.shape[:2]
            raw = pygame.image.frombuffer(frame.tobytes(), (w, h), "RGB").convert()
            self.frame = fit_image(raw, self.size)
            self.next_frame_at = now + 1.0 / self.fps
            return self.frame
        except Exception as e:
            print(f"MV背景フレームを読めませんでした: {e}")
            self.enabled = False
            return self.frame

    def draw(self, screen: pygame.Surface, dim_alpha: int = 150) -> bool:
        frame = self.get_frame()
        if frame is None:
            return False
        screen.blit(frame, (0, 0))
        dim = pygame.Surface(self.size, pygame.SRCALPHA)
        dim.fill((10, 5, 28, dim_alpha))
        screen.blit(dim, (0, 0))
        return True


class Game:
    def __init__(self, port: str | None, keyboard: bool, video: bool = True, sfx: bool = True):
        self.public_name_confirmed = False
        self.ranking_sync = None
        try:
            if RankingSync is not None and (BASE / "ranking_config.json").exists():
                self.ranking_sync = RankingSync(BASE)
        except Exception:
            print("[速報] 送信設定を確認してください。ゲームはMac内の保存のみで動作します")
        pygame.mixer.pre_init(44100, -16, 2, 512)  # バッファ小さめ=音の遅延を減らす
        pygame.init()
        pygame.mixer.init()
        pygame.mixer.set_num_channels(24)
        # ディスプレイサイズから初期ウィンドウサイズを決定
        info = pygame.display.Info()
        dw, dh = max(800, info.current_w), max(450, info.current_h)
        target_w = min(1920, int(dw * 0.92))
        target_h = min(1080, int(dh * 0.88))
        if target_w / target_h > 16 / 9:
            target_w = int(target_h * 16 / 9)
        else:
            target_h = int(target_w * 9 / 16)
        _set_screen_size(target_w, target_h)
        self.fullscreen = False
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.RESIZABLE)
        pygame.display.set_caption("位置GOMILK リズムタンバリン")
        self.clock = pygame.time.Clock()
        self.font_path = find_ui_font_path(False)
        self.font_path_bold = find_ui_font_path(True)
        self._refresh_fonts()
        self.text_editing = ""
        self.rank_button_rect = pygame.Rect(WIDTH - 310, 34, 240, 50)
        self.name_button_rect = pygame.Rect(WIDTH - 310, 94, 240, 44)
        self.result_name_button_rect = pygame.Rect(WIDTH//2 - 340, 622, 210, 46)
        self.result_rank_button_rect = pygame.Rect(WIDTH//2 + 130, 622, 210, 46)
        self.tambourine_sfx: pygame.mixer.Sound | None = None
        self.tambourine_sfx_source = ""
        if sfx:
            for sfx_path in TAMBOURINE_SFX_CANDIDATES:
                if not sfx_path.exists():
                    continue
                try:
                    self.tambourine_sfx = pygame.mixer.Sound(str(sfx_path))
                    self.tambourine_sfx.set_volume(0.85)
                    self.tambourine_sfx_source = sfx_path.name
                    break
                except Exception as e:
                    print(f"タンバリン効果音を読み込めませんでした: {sfx_path.name}: {e}")
            if self.tambourine_sfx is None:
                print("タンバリン効果音ファイルが見つからないか、読み込めませんでした。")
        self.sparkle_sfx: pygame.mixer.Sound | None = None
        self.drumroll_sfx: pygame.mixer.Sound | None = None
        self.fanfare_jaan_sfx: pygame.mixer.Sound | None = None
        self.gate_end_sfx: pygame.mixer.Sound | None = None
        self.fanfare_1st_sfx: pygame.mixer.Sound | None = None
        self.applause_sfx: pygame.mixer.Sound | None = None
        self.celebration_sfx: pygame.mixer.Sound | None = None
        self.ready_voice_sfx: pygame.mixer.Sound | None = None
        self.fate_voice_sfx: pygame.mixer.Sound | None = None
        self.bestten_voice_sfx: pygame.mixer.Sound | None = None
        self.disband_voice_sfx: pygame.mixer.Sound | None = None
        self.thanks_voice_sfx: pygame.mixer.Sound | None = None
        if sfx:
            sfx_dir = BASE / "assets" / "sfx"
            def _load(name, vol):
                path = sfx_dir / f"{name}.wav"
                if not path.exists():
                    return None
                try:
                    snd = pygame.mixer.Sound(str(path))
                    snd.set_volume(vol)
                    return snd
                except Exception as e:
                    print(f"{name}.wav を読み込めませんでした: {e}")
                    return None
            self.sparkle_sfx = _load("perfect_sparkle", 0.55)
            self.drumroll_sfx = _load("drumroll", 0.55)
            self.fanfare_jaan_sfx = _load("fanfare_jaan", 0.7)
            self.gate_end_sfx = _load("gate_end", 0.72)
            self.fanfare_1st_sfx = _load("fanfare_1st", 0.7)
            self.applause_sfx = _load("applause", 0.55)
            self.celebration_sfx = _load("celebration", 0.65)
            self.ready_voice_sfx = _load("ready_voice", 0.8)
            self.fate_voice_sfx = _load("fate_voice", 0.85)
            self.bestten_voice_sfx = _load("bestten_voice", 0.85)
            self.disband_voice_sfx = _load("disband_voice", 0.85)
            self.thanks_voice_sfx = _load("thanks_voice", 0.85)
        # macOSのIME・文字入力小窓は、公開名入力画面だけで有効にする。
        try:
            pygame.key.stop_text_input()
        except Exception:
            pass
        self._text_input_active = False
        self.songs = load_songs()
        self.video_enabled = video
        self.mv_bg_path = next((song.mv for song in self.songs if song.mv), None)
        self.bg_video = VideoBackground(self.mv_bg_path, (WIDTH, HEIGHT)) if video else None
        self.selected = 0
        self.profile = load_profile()
        self.full_unlocked = self.profile.get("full_unlocked", {})
        if not isinstance(self.full_unlocked, dict):
            self.full_unlocked = {}
        self.player_name = str(self.profile.get("player_name", ""))[:MAX_PLAYER_NAME]
        self.name_draft = self.player_name
        self.state = "title"
        self.pause_entered_at = 0.0
        self.pause_selected = 0       # 0=ゲームに戻る / 1=体験を終了
        self.pause_exit_confirm = False
        self.pause_snapshot: pygame.Surface | None = None
        self.pause_option_rects: list[pygame.Rect] = []
        self.pause_song_time = 0.0
        self.pause_resume_pending = False
        self.pause_resume_started_at = 0.0
        self.pause_resume_until = 0.0
        self.ranking_page = "overall"
        self.ranking_return_state = "select"
        self.title_entered_at = time.monotonic()
        # 1回の体験を3曲まとめて扱うためのセッション情報。
        self.session_id = uuid.uuid4().hex
        self.session_records: list[dict[str, Any]] = []
        self.session_total_score = 0
        self.session_total_combo = 0
        self.session_ending: str | None = None
        self.final_rank: int | None = None
        self.final_score_entered_at = 0.0
        self.final_thanks_voice_played = False
        self.final_score_reveal_sfx_played = False
        self.final_record_saved = False
        # 本番前の3種類練習。
        self.practice_step = 0
        self.practice_hits = 0
        self.practice_return_to = 4
        self.practice_feedback = ""
        self.practice_feedback_at = 0.0
        self.practice_completed_at = 0.0
        self.practice_note_started_at = 0.0
        self.practice_target_time = 2.8
        self.name_entered_at = 0.0
        # 画面切替直後に残ったタンバリン/キー入力を受け付けないための時刻。
        self.input_locked_until = 0.0
        self.time_up_entered_at = 0.0
        self.song_end_kind = "complete"
        # ---- ストーリー演出BGM ----
        self.bgm_path = BASE / "assets" / "bgm" / "sugoroku_bgm.mp3"  # ファイル名は旧版のまま利用
        self.goal_bgm_path = BASE / "assets" / "bgm" / "game_goal.mp3"
        self.normal_end_bgm_path = BASE / "assets" / "bgm" / "ending_scroll.mp3"
        self.current_bgm: str | None = None  # None / "story" / "song" / "goal_end" / "normal_end"
        # ---- エンディング/脱落演出 ----
        self.ending_entered_at = 0.0
        self.ending_outcome: dict[str, Any] | None = None
        self.ending_result = 0
        self.gameover_entered_at = 0.0
        self.gameover_outcome: dict[str, Any] | None = None
        self.gameover_chapter: dict[str, Any] | None = None
        # ---- ストーリー進行状態 ----
        self.current_chapter_id = int(self.profile.get("current_chapter", 1))
        # ---- 旧ボード互換用: ストーリー版では使わない ----
        self.board_pos = 0
        self.board_phase = "ready"     # ready / rolling / moving / fortune
        self.board_roll_at = 0.0       # 旧ボード互換用
        self.dice_value = 0            # 出目(1〜3)
        self.board_steps_left = 0      # 旧ボード互換用
        self.board_step_at = 0.0       # 旧ボード互換用
        self.board_msg = ""            # コマ移動後などの一言
        self.fortune_space = None      # 旧ボード互換用
        self.fortune_entered_at = 0.0
        self.return_after_event = "story"  # 旧互換用。ストーリー版では盤面に戻らない
        self.chapter_substage = "intro"  # intro / dice_rolling / result
        self.dice_result = 0
        self.fate_attempt = 1
        self.fate_reason = "first_try"
        self.dice_animation_at = 0.0
        self.dice_locked_at = 0.0
        self.chapter_entered_at = time.monotonic()
        self.chapter_event_outcome = None
        # 後で音ゲーから戻ってくる時に使うコールバック先の章
        self.chapter_return_to = None
        self.leaderboard = load_leaderboard()
        self.last_rank: int | None = None
        self.aborted = False
        self.song: Song | None = None
        self.cover_cache: dict[str, pygame.Surface] = {}
        self.chapter_img_cache: dict[str, pygame.Surface] = {}
        self.notes: list[dict[str, Any]] = []
        self.started_at = 0.0
        self.music_play_wall = 0.0  # music.play()を呼んだ実時刻(同期の基準)
        self.play_mode = "challenge"
        self.play_duration = 0.0
        self.checkpoints: list[float] = []
        self.next_checkpoint_index = 0
        self.gate_failed = False
        self.failed_gate_seconds: float | None = None
        self.last_checkpoint_seconds: float | None = None
        self.last_checkpoint_rate = 0.0
        self.total_notes_in_run = 0
        self.judge_counts = {"PERFECT": 0, "GREAT": 0, "GOOD": 0, "BAD": 0, "MISS": 0, "EMPTY": 0}
        self.good_rate = 0.0
        self.clear_result = False
        self.just_unlocked_full = False
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.judge_text = ""
        self.judge_until = 0.0
        self.keyboard = keyboard
        self.hit_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.reader: SerialHitReader | None = None
        self.last_serial_hit_ms = -10**9
        if port:
            self.reader = SerialHitReader(port, 115200, self.hit_queue)
            self.reader.start()
        self.particles: list[dict[str, Any]] = []
        self.ambient_sparks: list[dict[str, float]] = [
            {"x": float(random.uniform(0, WIDTH)),
             "y": float(LANE_Y + random.uniform(-80, 80)),
             "v": float(random.uniform(80, 220)),
             "a": float(random.randint(60, 130)),
             "last": 0.0}
            for _ in range(18)
        ]
        self.ribbon_at = -9.0
        self.last_raw: dict[str, Any] | None = None
        # ---- 重力ベクトル(キメ=上げ振り判定の基準)。初期は水平持ち(Zが下) ----
        self.gravity_vec = [0.0, 0.0, 1000.0]
        self.kime_flash_at = -9.0
        self.kime_recalib_at = -9.0
        # ---- 結果発表のドラマ演出 ----
        self.result_started_at = 0.0
        self.result_stage = "idle"  # idle/drumroll/reveal/celebrate
        self.result_bg_image: pygame.Surface | None = None
        bg_path = BASE / "assets" / "logo" / "result_bg.png"
        if bg_path.exists():
            try:
                self.result_bg_image = pygame.image.load(str(bg_path)).convert()
                if self.result_bg_image.get_size() != (WIDTH, HEIGHT):
                    self.result_bg_image = pygame.transform.smoothscale(self.result_bg_image, (WIDTH, HEIGHT))
            except Exception as e:
                print(f"result_bg読込失敗: {e}")
        self.result_sfx_fired = {"jaan": False, "first": False, "applause": False, "celebration": False}
        self.result_auto_return_at = 0.0
        self.last_textinput_at = -10.0
        self.last_manual_ime_commit_text = ""
        self.last_manual_ime_commit_at = -10.0
        self.name_input_rect = pygame.Rect(0, 0, 1, 1)
        self.title_bg_img = self.start_logo_img = self.board_map_img = self.start_screen_img = None
        self.title_voice_sfx = self.start_voice_sfx = None
        self.title_entered_at = time.monotonic()
        self.start_entered_at = 0.0
        try:
            for attr, fn in [("title_bg_img","title_bg.png"),("board_map_img","board_map.png")]:
                fp = BASE / "assets" / "logo" / fn
                if fp.exists(): setattr(self, attr, pygame.image.load(str(fp)).convert())
            fp = BASE / "assets" / "logo" / "start_logo.png"
            if fp.exists(): self.start_logo_img = pygame.image.load(str(fp)).convert_alpha()
            fp = BASE / "assets" / "logo" / "start_screen.png"
            if fp.exists(): self.start_screen_img = pygame.image.load(str(fp)).convert()
        except Exception as e:
            print(f"タイトル画像読込エラー: {e}")
        if sfx:
            for attr, fn, vol in [("title_voice_sfx","title_voice.wav",0.8),("start_voice_sfx","start_voice.wav",0.7)]:
                fp = BASE / "assets" / "sfx" / fn
                if fp.exists():
                    try:
                        snd = pygame.mixer.Sound(str(fp)); snd.set_volume(vol); setattr(self, attr, snd)
                    except Exception: pass
        if self.title_voice_sfx:
            try: self.title_voice_sfx.play()
            except Exception: pass
        self.note_tap_img: pygame.Surface | None = None
        self.note_kime_img: pygame.Surface | None = None
        self.note_tap_size = 0
        self.note_kime_size = 0
        self._load_note_assets()
        # ---- リズム同期まわり ----
        self.music_anchor: float | None = None  # 音楽クロックに同期した開始時刻の推定値
        self.latency_offset = float(self.profile.get("latency_offset", 0.0))
        # ---- 演出タイマー ----
        self.judge_kind = ""
        self.judge_at = 0.0
        self.combo_pop_at = -9.0
        self.hit_flash_at = -9.0
        self.led_wave_at = -9.0
        self.last_physical_hit_at = -10.0
        self.last_success_hit_at = -10.0  # 直近で“ノーツ/ロールに当たった”時刻(反動判定用)
        self._on_screen_resize()

    def song_time(self) -> float:
        """曲の現在時刻。

        v10は play() を呼んだ後の壁時計だけで測っていたため、
        オーディオの起動遅延ぶんノーツが早くずれていた。
        ここでは pygame.mixer.music.get_pos() (音楽側のクロック) に
        ゆっくり追従するアンカーを持ち、なめらか&正確の両立をする。
        さらに - / + キーで好みに微調整できる(latency_offset)。

        v23: 曲開始前のカウントダウン期間中は負の値を返して、
        ノーツが流れてこないようにする。
        """
        if self.state != "play" or not self.song:
            return 0.0
        if getattr(self, "pause_resume_pending", False):
            return float(self.pause_song_time)
        now = time.monotonic()
        # カウントダウン中: music_started がFalse なら started_at を基準に負値を返す
        if not getattr(self, "music_started", True):
            return now - self.started_at  # started_at = カウントダウン終了予定時刻 なので負値
        # 基準は「music.play()を呼んだ実時刻」。これで必ず曲頭(0秒)から始まる。
        wall_anchor = self.music_play_wall or self.started_at
        base = now - wall_anchor
        # get_pos()はMP3+ループBGM後に累積した異常値を返すことがあるので、
        # 実時刻基準と近い(妥当な)時だけ微調整に使う。異常なら無視して実時刻基準のまま。
        try:
            pos_ms = pygame.mixer.music.get_pos()
        except Exception:
            pos_ms = -1
        anchor = wall_anchor
        if pos_ms is not None and pos_ms >= 0:
            pos_s = pos_ms / 1000.0
            if abs(pos_s - base) < 0.30:  # 妥当な範囲のみ採用
                est_anchor = now - pos_s
                if self.music_anchor is None:
                    self.music_anchor = est_anchor
                else:
                    self.music_anchor += (est_anchor - self.music_anchor) * 0.05
                anchor = self.music_anchor
            else:
                self.music_anchor = None  # 異常値 → 実時刻基準にフォールバック
        return now - anchor - self.song.offset + self.latency_offset

    def update_countdown(self) -> None:
        """カウントダウン終了時に音楽を再生開始する。"""
        if getattr(self, "pause_resume_pending", False):
            now = time.monotonic()
            if now < self.pause_resume_until:
                return
            wait = max(0.0, self.pause_resume_until - self.pause_resume_started_at)
            if getattr(self, "music_started", False):
                self.music_play_wall += wait
                if self.music_anchor is not None:
                    self.music_anchor += wait
                try:
                    pygame.mixer.music.unpause()
                except Exception:
                    pass
            else:
                self.started_at += wait
                self.countdown_started_at += wait
            self.pause_resume_pending = False
            self.last_physical_hit_at = -10.0
            self.last_success_hit_at = -10.0
            pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])
        if not hasattr(self, "music_started") or self.music_started:
            return
        now = time.monotonic()
        if now >= self.started_at:
            try:
                pygame.mixer.music.play()
            except Exception as e:
                print(f"音源を再生できませんでした: {e}")
            self.music_started = True
            self.music_play_wall = time.monotonic()  # 再生開始の実時刻=同期の基準
            self.music_anchor = None  # 再計算

    def is_full_unlocked(self, song: Song | None) -> bool:
        # 古いprofile.jsonとの互換用。v10では解放制ではなく、全員が最初からチャレンジ開始です。
        return True

    def selected_mode_for(self, song: Song | None) -> str:
        return "challenge"

    def score_rate_through(self, cutoff_seconds: float) -> tuple[int, float]:
        """Return GOOD以上率 for notes whose time is within cutoff_seconds."""
        if not self.notes:
            return 0, 1.0
        total = 0
        goodish = 0
        for n in self.notes:
            # ROLLは開始時刻ではなく、区間を最後まで終えた時点で1判定とする。
            t = float(n.get("end_time", n.get("time", 0))) \
                if n.get("kind") == "roll" else float(n.get("time", 0))
            if t > cutoff_seconds:
                break
            total += 1
            if n.get("judge") in ("PERFECT", "GREAT", "GOOD", "ROLL"):
                goodish += 1
        if total == 0:
            return 0, 1.0
        return total, goodish / total

    def live_success_rate(self) -> tuple[int, int, float | None]:
        """プレイ中に判定が確定したノーツだけで、現在の成功率を返す。"""
        judged = 0
        success = 0
        for n in self.notes:
            judge = n.get("judge")
            if judge not in ("PERFECT", "GREAT", "GOOD", "ROLL", "BAD", "MISS"):
                continue
            judged += 1
            if judge in ("PERFECT", "GREAT", "GOOD", "ROLL"):
                success += 1
        return judged, success, (success / judged if judged else None)

    def calculate_result_flags(self, cutoff_seconds: float | None = None) -> None:
        if cutoff_seconds is None:
            cutoff_seconds = min(max(0.0, self.song_time()), self.play_duration or 0.0)
        self.total_notes_in_run, self.good_rate = self.score_rate_through(cutoff_seconds)
        self.clear_result = self.good_rate >= CLEAR_GOOD_RATE

    def unlock_full_if_needed(self) -> None:
        # v10ではフル解放という概念を使いません。
        self.just_unlocked_full = False

    def result_should_return_to_story(self) -> bool:
        """結果画面の次がストーリー章ならTrue。Q選曲から遊んだ時はFalse。"""
        return isinstance(self.chapter_return_to, int) and get_chapter(self.chapter_return_to) is not None

    def continue_after_result(self) -> None:
        """結果画面から、ストーリー中なら次章へ。譜面選択からなら曲選択へ戻る。"""
        if self.result_should_return_to_story():
            return_to = int(self.chapter_return_to)
            self.chapter_return_to = None
            self.enter_chapter(return_to)
        else:
            self.chapter_return_to = None
            self.state = "select"

    def update_result_auto_return(self) -> None:
        """ストーリー中の曲だけ、結果を少し見せた後に自動で物語へ戻す。"""
        if self.state != "result" or not self.result_should_return_to_story():
            return
        if self.result_auto_return_at and time.monotonic() >= self.result_auto_return_at:
            self.continue_after_result()

    def start_song(self, index: int, mode: str | None = None) -> None:
        self.song = self.songs[index]
        # 展示版: 60秒審査を通過した人だけ、曲ごとの自然な終端まで続行する。
        self.play_mode = "challenge"
        self.play_duration = self.song.duration
        self.checkpoints = [sec for sec in PROGRESS_CHECKPOINTS if sec < self.play_duration]
        self.next_checkpoint_index = 0
        self.gate_failed = False
        self.failed_gate_seconds = None
        self.last_checkpoint_seconds = None
        self.last_checkpoint_rate = 0.0
        self.notes = load_chart(self.song.chart)
        self.total_notes_in_run = sum(
            1 for n in self.notes
            if float(n.get("end_time", n.get("time", 0))) <= self.play_duration
        )
        self.judge_counts = {"PERFECT": 0, "GREAT": 0, "GOOD": 0, "BAD": 0, "MISS": 0, "EMPTY": 0}
        self.good_rate = 0.0
        self.clear_result = False
        self.just_unlocked_full = False
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.judge_text = ""
        self.last_rank = None
        self.aborted = False
        self.particles.clear()
        # ---- タンバリン入力デバウンス用 ----
        self.last_hit_at = -10.0
        self.last_physical_hit_at = -10.0
        self.last_success_hit_at = -10.0
        # ---- 60秒チェックポイント通過の祝福演出用 ----
        self.checkpoint_celebrated_at = -100.0
        self.checkpoint_celebrated_gate = 0
        self.checkpoint_celebrated_rate = 0.0
        # ---- 「かまえて！」3秒 → 「3, 2, 1, START!」 ----
        self.countdown_started_at = time.monotonic()
        self.ready_calibration_duration = READY_CALIBRATION_SEC
        self.number_countdown_duration = 3.5
        self.countdown_duration = self.ready_calibration_duration + self.number_countdown_duration
        self.music_started = False
        if self.ready_voice_sfx:
            try:
                self.ready_voice_sfx.stop()
                self.ready_voice_sfx.play()
            except Exception:
                pass
        try:
            pygame.mixer.music.load(str(self.song.audio))
            self.current_bgm = "song"  # ストーリーBGMから曲へ切替(BGM管理が止めないように)
            # 音楽の再生はカウントダウン終了時にupdate内で開始する
        except Exception as e:
            print(f"音源を読み込めませんでした: {e}")
        self.started_at = time.monotonic() + self.countdown_duration
        self.music_anchor = None
        self.state = "play"

    def save_score(self) -> None:
        if not self.song:
            return
        # 念のため保存直前に scores.json を読み直します。
        # これで、前回のプレイ結果や別ウィンドウで作られた記録を上書きで消しにくくします。
        self.leaderboard = load_leaderboard()
        name = clean_player_name(self.player_name)
        self.player_name = name
        self.profile["player_name"] = name
        save_json_file(PROFILE_PATH, self.profile)
        key = leaderboard_key(self.song, self.play_mode)
        rows = list(self.leaderboard.get(key, []))
        played_at = datetime.now().isoformat(timespec="microseconds")
        record = {
            "record_id": f"{self.song.id}:challenge:{played_at}:{random.randrange(1000000)}",
            "name": name,
            "score": int(self.score),
            "max_combo": int(self.max_combo),
            "song_title": self.song.title,
            "mode": "challenge",
            "duration": round(float(self.play_duration), 2),
            "failed_gate_seconds": self.failed_gate_seconds,
            "last_checkpoint_seconds": self.last_checkpoint_seconds,
            "last_checkpoint_rate": round(float(self.last_checkpoint_rate), 4),
            "good_rate": round(float(self.good_rate), 4),
            "clear": bool(self.clear_result),
            "aborted": bool(self.aborted),
            "played_seconds": round(max(0.0, float(self.song_time())), 2),
            "played_at": played_at,
            "session_id": self.session_id,
        }
        rows.append(record)
        sorted_rows = sort_scores(rows)
        self.last_rank = next(
            (i + 1 for i, r in enumerate(sorted_rows) if r.get("record_id") == record["record_id"]),
            None,
        )
        # 低めのスコアや途中終了も「保存された」と分かるように、上位20ではなく多めに残します。
        # 画面表示は今まで通り TOP 5 / TOP 20 だけを使います。
        self.leaderboard[key] = sorted_rows[:MAX_RANKING_RECORDS]
        save_json_file(LEADERBOARD_PATH, self.leaderboard)
        # 総合ランキングは完了した曲だけを1人分として合算する。
        if not self.aborted:
            self.session_records.append(record)
            self.session_total_score = sum(int(r.get("score", 0)) for r in self.session_records)
            self.session_total_combo = sum(int(r.get("max_combo", 0)) for r in self.session_records)

    def finish_song(self, aborted: bool = False) -> None:
        if self.state != "play":
            return
        # 音楽が止まった瞬間に終了音を鳴らし、無音の間を作らない。
        self.song_end_kind = (
            "gate_failed"
            if self.gate_failed and self.failed_gate_seconds is not None
            else "complete"
        )
        pygame.mixer.music.stop()
        end_sfx = self.gate_end_sfx if self.song_end_kind == "gate_failed" else self.fanfare_jaan_sfx
        if not aborted and end_sfx:
            try:
                end_sfx.play()
            except Exception:
                pass
        self.aborted = aborted
        if self.gate_failed and self.failed_gate_seconds is not None:
            self.calculate_result_flags(self.failed_gate_seconds)
        else:
            self.calculate_result_flags()
        if aborted:
            self.clear_result = False
            self.just_unlocked_full = False
        else:
            self.unlock_full_if_needed()
        self.save_score()
        if aborted:
            # 途中終了は、完了済みの曲だけで感謝・総合結果画面へ。
            self.chapter_return_to = None
            self.enter_final_score()
            return
        # 曲の最後の一振りやSpace連打を次画面へ持ち越さない、自動の幕間。
        self.state = "time_up"
        self.time_up_entered_at = time.monotonic()
        self.input_locked_until = self.time_up_entered_at + SONG_TIME_UP_SEC + RESULT_INPUT_GUARD_SEC
        pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])

    def enter_song_result(self) -> None:
        """審査終了の幕間の後、通常の曲結果画面へ入る。"""
        self.state = "result"
        self.result_started_at = time.monotonic()
        self.result_stage = "drumroll"
        self.result_sfx_fired = {"jaan": False, "first": False, "applause": False, "celebration": False}
        self.result_auto_return_at = time.monotonic() + (6.5 if self.result_should_return_to_story() else 0.0)
        pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])
        if self.drumroll_sfx:
            try:
                self.drumroll_sfx.play()
            except Exception:
                pass

    def update_time_up(self) -> None:
        if self.state == "time_up" and time.monotonic() - self.time_up_entered_at >= SONG_TIME_UP_SEC:
            # 展示ストーリーでは曲別結果を挟まず、終了演出からそのまま次章へ進む。
            # Qキーからの単曲プレイも、曲別結果を出さず選曲画面へ戻す。
            self.continue_after_result()

    def draw_time_up(self) -> None:
        """完走／60秒審査終了を分けて見せる、操作不能の自動幕間。"""
        completed = self.song_end_kind == "complete"
        self.screen.fill((44, 8, 46) if completed else (24, 10, 38))
        if self.song is not None:
            bg = self.cover_for(self.song, (WIDTH, HEIGHT))
            bg.set_alpha(75)
            self.screen.blit(bg, (0, 0))
        veil = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        veil.fill((58, 8, 48, 170) if completed else (22, 8, 38, 200))
        self.screen.blit(veil, (0, 0))
        cx, cy = WIDTH // 2, HEIGHT // 2
        elapsed = time.monotonic() - self.time_up_entered_at
        # 紙吹雪は文字より先に描き、メッセージの可読性を保つ。
        if completed:
            self._draw_confetti(elapsed, n=60)
        pulse = 1.0 + 0.04 * math.sin(elapsed * 6.0)
        title_text = "FINISH!" if completed else "60秒審査 終了！"
        sub_text = "完走！審査終了！" if completed else "今回のチャレンジはここまで！"
        title_color = (255, 225, 105) if completed else (238, 220, 255)
        title = font(scaled_px(94 if completed else 76, min_px=60, max_px=130), True).render(
            title_text, True, title_color)
        title = pygame.transform.rotozoom(title, 0, pulse)
        self.draw_text_shadow(title, title.get_rect(center=(cx, cy - scaled_px(46, min_px=34, max_px=70))))
        sub = font(scaled_px(44, min_px=32, max_px=62), True).render(
            sub_text, True, (255, 240, 255))
        self.draw_text_shadow(sub, sub.get_rect(center=(cx, cy + scaled_px(60, min_px=44, max_px=84))))
        wait = self.small.render("そのまま次の物語をお待ちください", True, (225, 215, 245))
        self.screen.blit(wait, wait.get_rect(center=(cx, cy + scaled_px(120, min_px=88, max_px=150))))

    def stop_song(self) -> None:
        # 途中でやめても、その時点のスコアをランキングに残します。
        self.finish_song(aborted=True)

    def enter_pause(self) -> None:
        """音ゲーを安全に止め、調整または途中終了を選べる画面へ。"""
        if self.state != "play":
            return
        self.pause_song_time = self.song_time()
        self.pause_snapshot = self.screen.copy()
        self.pause_entered_at = time.monotonic()
        self.pause_selected = 0
        self.pause_exit_confirm = False
        try:
            pygame.mixer.music.pause()
        except Exception:
            pass
        self.state = "pause"
        pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])

    def resume_from_pause(self) -> None:
        """停止していた時間を曲時計から除外し、3秒の構え直し後に再開する。"""
        if self.state != "pause":
            return
        paused_for = max(0.0, time.monotonic() - self.pause_entered_at)
        if getattr(self, "music_started", False):
            self.music_play_wall += paused_for
            if self.music_anchor is not None:
                self.music_anchor += paused_for
        else:
            self.started_at += paused_for
            self.countdown_started_at += paused_for
        # 再開直後の入力を捨て、画面上に3秒の「かまえて！」を重ねる。
        self.pause_resume_started_at = time.monotonic()
        self.pause_resume_until = self.pause_resume_started_at + 3.0
        self.pause_resume_pending = True
        self.input_locked_until = self.pause_resume_until
        self.last_physical_hit_at = -10.0
        self.last_success_hit_at = -10.0
        self.state = "play"
        pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])

    def exit_from_pause(self) -> None:
        """確認後の途中終了。現在曲を途中終了として保存し、感謝・総合結果へ。"""
        if self.state != "pause":
            return
        self.state = "play"  # finish_songの通常経路を安全に再利用する。
        self.stop_song()

    def draw_pause(self) -> None:
        """誤操作では終わらない、展示スタッフ向け一時停止・調整画面。"""
        if self.pause_snapshot is not None:
            self.screen.blit(self.pause_snapshot, (0, 0))
        else:
            self.screen.fill((22, 12, 35))
        veil = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        veil.fill((12, 7, 24, 205))
        self.screen.blit(veil, (0, 0))

        cx = WIDTH // 2
        panel_w = min(WIDTH - 80, 820)
        panel_h = min(HEIGHT - 60, 560)
        panel = pygame.Rect(cx - panel_w // 2, (HEIGHT - panel_h) // 2, panel_w, panel_h)
        self.draw_panel(panel, alpha=242)
        title = font(scaled_px(58, min_px=38, max_px=76), True).render(
            "一時停止", True, (255, 235, 250))
        self.draw_text_shadow(title, title.get_rect(center=(cx, panel.y + int(panel_h * 0.12))))

        off_ms = int(round(self.latency_offset * 1000))
        info_lines = (
            "G：いまの構えを基準にする",
            f"−／＋：タイミング調整　現在 {off_ms:+d} ms",
        )
        for i, line in enumerate(info_lines):
            s = font(scaled_px(26, min_px=19, max_px=34), True).render(
                line, True, (235, 225, 250))
            self.screen.blit(s, s.get_rect(center=(
                cx, panel.y + int(panel_h * (0.25 + i * 0.09)))))

        if time.monotonic() - self.kime_recalib_at < 1.3:
            done = self.small.render("基準姿勢を更新しました！", True, (255, 225, 145))
            self.screen.blit(done, done.get_rect(center=(cx, panel.y + int(panel_h * 0.43))))

        if self.pause_exit_confirm:
            warning = font(scaled_px(31, min_px=23, max_px=42), True).render(
                "本当に今回の体験を終了しますか？", True, (255, 205, 170))
            self.screen.blit(warning, warning.get_rect(center=(cx, panel.y + int(panel_h * 0.56))))
            yes = font(scaled_px(27, min_px=20, max_px=36), True).render(
                "Enter：終了する　／　Esc：一時停止へ戻る", True, (255, 245, 255))
            self.screen.blit(yes, yes.get_rect(center=(cx, panel.y + int(panel_h * 0.70))))
            return

        labels = ("ゲームに戻る", "体験を終了")
        self.pause_option_rects = []
        for i, label in enumerate(labels):
            button_h = max(48, min(62, int(panel_h * 0.12)))
            button_y = panel.y + int(panel_h * (0.50 + i * 0.17))
            rect = pygame.Rect(cx - 250, button_y, 500, button_h)
            self.pause_option_rects.append(rect)
            selected = i == self.pause_selected
            fill = (176, 64, 138) if selected else (48, 35, 68)
            border = (255, 225, 165) if selected else (155, 135, 175)
            pygame.draw.rect(self.screen, fill, rect, border_radius=24)
            pygame.draw.rect(self.screen, border, rect, 3, border_radius=24)
            s = font(scaled_px(28, min_px=21, max_px=38), True).render(
                ("▶ " if selected else "　") + label, True, (255, 248, 252))
            self.screen.blit(s, s.get_rect(center=rect.center))
        hint = self.small.render("↑／↓で選択　Enterで決定　Escでもゲームに戻る",
                                 True, (220, 215, 238))
        self.screen.blit(hint, hint.get_rect(center=(cx, panel.bottom - 38)))

    def finalize_session_record(self) -> None:
        """3曲分を1人分の総合記録として保存・送信する。"""
        if self.final_record_saved or not self.session_records:
            return
        self.final_record_saved = True
        played_at = datetime.now().isoformat(timespec="microseconds")
        ending = self.session_ending or "unfinished"
        record = {
            "record_id": f"overall:{self.session_id}",
            "name": clean_player_name(self.player_name),
            "score": int(self.session_total_score),
            "max_combo": int(self.session_total_combo),
            "song_title": "全3曲 総合",
            "mode": "story",
            "ending": ending,
            "fate_attempt": int(self.fate_attempt),
            "fate_reason": self.fate_reason,
            "completed_songs": len(self.session_records),
            "session_id": self.session_id,
            "played_at": played_at,
        }
        self.leaderboard = load_leaderboard()
        rows = list(self.leaderboard.get("overall", []))
        rows.append(record)
        rows = sort_scores(rows)
        self.final_rank = next((i + 1 for i, row in enumerate(rows)
                                if row.get("record_id") == record["record_id"]), None)
        self.leaderboard["overall"] = rows[:MAX_RANKING_RECORDS]
        save_json_file(LEADERBOARD_PATH, self.leaderboard)
        if self.ranking_sync:
            try:
                self.ranking_sync.annotate(record, "overall", self.public_name_confirmed)
                self.ranking_sync.enqueue(record)
            except Exception:
                print("[速報] 総合記録を送信待ちにできませんでした。Macには保存済みです")

    def enter_final_score(self) -> None:
        """物語エンドのあとに、得点と展示ランキングを発表する。"""
        self.session_total_score = sum(int(r.get("score", 0)) for r in self.session_records)
        self.session_total_combo = sum(int(r.get("max_combo", 0)) for r in self.session_records)
        self.finalize_session_record()
        self.final_score_entered_at = time.monotonic()
        self.final_thanks_voice_played = False
        self.final_score_reveal_sfx_played = False
        self.state = "final_score"
        # 「あなたの得点は！！」の間はドラムロールでためる。
        if self.drumroll_sfx:
            try: self.drumroll_sfx.play()
            except Exception: pass

    def return_to_title(self) -> None:
        """1人の体験を閉じ、次の人用のタイトル画面へ戻す。"""
        try: pygame.mixer.music.stop()
        except Exception: pass
        self.current_bgm = None
        self.title_entered_at = time.monotonic()
        self.state = "title"

    def _refresh_fonts(self) -> None:
        """画面サイズに応じてUIフォントを作り直す。"""
        self.big = font(scaled_px(56, min_px=36, max_px=90), True)
        self.mid = font(scaled_px(34, min_px=24, max_px=56), True)
        self.small = font(scaled_px(24, min_px=18, max_px=38))
        self.tiny = font(scaled_px(18, min_px=14, max_px=28))
        self.rank_font = font(scaled_px(20, min_px=16, max_px=30))
        self.score_font = font(scaled_px(40, min_px=28, max_px=64), True)
        self.judge_font = font(scaled_px(54, min_px=34, max_px=84), True)
        self.combo_font = font(scaled_px(82, min_px=50, max_px=128), True)

    def draw_bottom_right_credit(self, text: str, bottom_margin: int = 20,
                                 text_color: tuple[int, int, int] = (235, 225, 255),
                                 fill_rgba: tuple[int, int, int, int] = (24, 16, 48, 170)) -> None:
        """右下に読めるクレジットを半透明プレート付きで表示する。"""
        label = self.tiny.render(text, True, text_color)
        pad_x = scaled_px(14, min_px=10, max_px=20)
        pad_y = scaled_px(8, min_px=6, max_px=14)
        plate = pygame.Surface((label.get_width() + pad_x * 2, label.get_height() + pad_y * 2), pygame.SRCALPHA)
        pygame.draw.rect(plate, fill_rgba, plate.get_rect(), border_radius=14)
        pygame.draw.rect(plate, (255, 235, 255, 90), plate.get_rect(), width=1, border_radius=14)
        rect = plate.get_rect(bottomright=(WIDTH - scaled_px(18, min_px=12, max_px=28), HEIGHT - bottom_margin))
        self.screen.blit(plate, rect.topleft)
        self.screen.blit(label, (rect.x + pad_x, rect.y + pad_y))

    def _load_note_assets(self) -> None:
        """ノーツ画像(いちご / いちご牛乳)を現在の画面サイズ向けに読み込む。"""
        def _load_scaled(path: Path, size: int) -> pygame.Surface | None:
            if not path.exists():
                return None
            try:
                img = pygame.image.load(str(path)).convert_alpha()
                return pygame.transform.smoothscale(img, (size, size))
            except Exception as e:
                print(f"ノーツ画像読込失敗 {path.name}: {e}")
                return None

        self.note_tap_size = scaled_px(112, min_px=88, max_px=152)
        self.note_kime_size = scaled_px(144, min_px=112, max_px=184)
        tap_path = BASE / "assets" / "notes" / "strawberry_note.png"
        kime_path = BASE / "assets" / "notes" / "milk_kime_note.png"
        self.note_tap_img = _load_scaled(tap_path, self.note_tap_size)
        self.note_kime_img = _load_scaled(kime_path, self.note_kime_size)

    def toggle_fullscreen(self) -> None:
        """F11でフルスクリーン⇔ウィンドウを切り替える。"""
        self.fullscreen = not self.fullscreen
        info = pygame.display.Info()
        if self.fullscreen:
            _set_screen_size(info.current_w, info.current_h)
            self.screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.FULLSCREEN)
        else:
            target_w = min(1920, int(info.current_w * 0.92))
            target_h = min(1080, int(info.current_h * 0.88))
            if target_w / target_h > 16 / 9:
                target_w = int(target_h * 16 / 9)
            else:
                target_h = int(target_w * 9 / 16)
            _set_screen_size(target_w, target_h)
            self.screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.RESIZABLE)
        self._on_screen_resize()

    def _on_screen_resize(self) -> None:
        """画面サイズが変わった時にキャッシュとボタン位置を作り直す。"""
        self._refresh_fonts()
        self.cover_cache.clear()
        self.chapter_img_cache.clear()
        self._load_note_assets()
        self.rank_button_rect = pygame.Rect(WIDTH - scaled_px(310, min_px=250, max_px=380), scaled_px(34, min_px=20, max_px=44), scaled_px(240, min_px=200, max_px=300), scaled_px(50, min_px=42, max_px=60))
        self.name_button_rect = pygame.Rect(WIDTH - scaled_px(310, min_px=250, max_px=380), scaled_px(94, min_px=72, max_px=120), scaled_px(240, min_px=200, max_px=300), scaled_px(44, min_px=38, max_px=54))
        self.result_name_button_rect = pygame.Rect(WIDTH // 2 - scaled_px(340, min_px=260, max_px=420), HEIGHT - scaled_px(110, min_px=90, max_px=140), scaled_px(210, min_px=180, max_px=270), scaled_px(46, min_px=38, max_px=56))
        self.result_rank_button_rect = pygame.Rect(WIDTH // 2 + scaled_px(130, min_px=100, max_px=170), HEIGHT - scaled_px(110, min_px=90, max_px=140), scaled_px(210, min_px=180, max_px=270), scaled_px(46, min_px=38, max_px=56))
        if self.video_enabled:
            self.bg_video = VideoBackground(self.mv_bg_path, (WIDTH, HEIGHT))
        bg_path = BASE / "assets" / "logo" / "result_bg.png"
        if bg_path.exists():
            try:
                self.result_bg_image = pygame.image.load(str(bg_path)).convert()
                if self.result_bg_image.get_size() != (WIDTH, HEIGHT):
                    self.result_bg_image = pygame.transform.smoothscale(self.result_bg_image, (WIDTH, HEIGHT))
            except Exception:
                pass
        try:
            _LOGO_CACHE.clear()
        except Exception:
            pass

    def play_tambourine_sfx(self, strength: float = 700.0) -> None:
        if not self.tambourine_sfx:
            return
        try:
            volume = max(0.45, min(1.0, float(strength) / 900.0))
            self.tambourine_sfx.set_volume(volume)
            self.tambourine_sfx.play()
        except Exception:
            pass

    def register_hit(self, strength: float = 1.0, up_gesture: bool = False) -> None:
        wall_now = time.monotonic()
        now = self.song_time()

        # カウントダウン中は受け付けない
        if now < 0:
            return
        # 弱すぎるHITを捨てたい時用。今は0なので実質無効。
        if strength < INPUT_MIN_POWER:
            return

        # 今ロールゾーンの中にいるか? → 感度(デバウンス)を切り替える
        in_roll_zone = False
        active_roll = None
        for n in self.notes:
            if n.get("kind") != "roll" or n.get("completed"):
                continue
            s = float(n.get("time", 0))
            e = float(n.get("end_time", s))
            if s - 0.05 <= now <= e + 0.05:
                in_roll_zone = True
                active_roll = n
                break

        # ROLL直後50msに置かれたいちご牛乳は、ROLLの受付余白と重なる。
        # KIMEノーツが判定範囲に入ったら、振り方に関係なくROLLより先に選ぶ。
        # 選んだ後で、上げ振りならKIME、そうでなければ「上にあげよう」と判定する。
        nearby_kime = None
        if active_roll is not None:
            for n in self.notes:
                if n.get("kind") != "kime" or n["hit"] or n["missed"]:
                    continue
                if abs(float(n["time"]) - now) <= INPUT_NOTE_GATE_SEC:
                    nearby_kime = n
                    active_roll = None
                    in_roll_zone = False
                    break

        debounce = INPUT_DEBOUNCE_ROLL_SEC if in_roll_zone else INPUT_DEBOUNCE_NORMAL_SEC
        # 近すぎる連続HITは同じ振りの反動として捨てる(ゾーンで感度可変)
        if wall_now - self.last_physical_hit_at < debounce:
            return

        # ===== ロールノーツの判定: ゾーン内なら振るたびにcount+1(高感度) =====
        if active_roll is not None:
            n = active_roll
            self.last_physical_hit_at = wall_now
            self.last_success_hit_at = wall_now
            self.play_tambourine_sfx(strength)
            n["hit_count"] = int(n.get("hit_count", 0)) + 1
            # 振った回数は得点・コンボへ反映するが、判定数には加えない。
            # ROLL区間終了時に、区間全体を1判定として数える。
            self.score += 200 + min(self.combo, 200) * 1
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            self.set_judge("ROLL", 200, strength)
            if self.sparkle_sfx and n["hit_count"] % 4 == 0:
                try: self.sparkle_sfx.play()
                except Exception: pass
            return

        # ===== 通常ノーツの判定 =====
        # 一番近い未処理ノーツを探す。
        target = nearby_kime
        best_diff = abs(float(nearby_kime["time"]) - now) if nearby_kime is not None else 999.0
        for n in self.notes:
            if n.get("kind") == "roll":
                continue  # ロールはここでは扱わない
            if n["hit"] or n["missed"]:
                continue
            dt = float(n["time"]) - now
            if dt < -INPUT_NOTE_GATE_SEC:
                continue
            if dt > INPUT_NOTE_GATE_SEC:
                break
            diff = abs(dt)
            if diff < best_diff:
                best_diff = diff
                target = n

        # ノーツが近くに無い場合:
        #   直前に成功HITがあった直後の“空振り”は「1振りの反動(タタの2発目)」とみなして無視。
        #   そうでなければ素直にBAD(振ったのに無反応問題は避ける)。
        if target is None:
            self.last_physical_hit_at = wall_now
            if wall_now - self.last_success_hit_at < REBOUND_EMPTY_WINDOW_SEC:
                return  # 反動 → BADにしない・音も鳴らさない
            self.play_tambourine_sfx(strength)
            self.combo = 0
            self.judge_counts["EMPTY"] = self.judge_counts.get("EMPTY", 0) + 1
            self.set_judge("BAD", 0, strength)
            return

        self.last_physical_hit_at = wall_now
        self.play_tambourine_sfx(strength)

        # いちご牛乳(キメ)ノーツは「上げ振り」専用。
        # 横振りや普通の叩きではGOODにせず、1回分の入力としてBAD扱いにする。
        # こうすると「ミルクは上に上げたらヒット」が画面上でもルールとして分かる。
        if target.get("kind") == "kime" and not up_gesture:
            target["hit"] = True
            target["judge"] = "BAD"
            self.combo = 0
            self.judge_counts["BAD"] = self.judge_counts.get("BAD", 0) + 1
            self.last_success_hit_at = wall_now  # 直後の反動でBADが二重に出ないようにする
            self.set_judge("UP", 0, strength)
            return

        for win, name, points in JUDGE:
            if best_diff <= win:
                target["hit"] = True
                target["judge"] = name
                self.last_success_hit_at = wall_now
                self.judge_counts[name] = self.judge_counts.get(name, 0) + 1
                self.score += points + min(self.combo, 200) * 2
                self.combo += 1
                self.max_combo = max(self.max_combo, self.combo)
                # いちご牛乳(キメ)を『上げ振り』で取ったらボーナス＆特別演出
                if target.get("kind") == "kime" and up_gesture and points > 0:
                    self.score += KIME_BONUS
                    self.kime_flash_at = wall_now
                    self.set_judge("KIME", points + KIME_BONUS, max(strength, 900))
                else:
                    self.set_judge(name, points, strength)
                if name == "PERFECT" and self.sparkle_sfx:
                    try:
                        self.sparkle_sfx.play()
                    except Exception:
                        pass
                return

        # ノーツ近くではあるが判定窓外ならBAD。
        self.combo = 0
        self.judge_counts["EMPTY"] = self.judge_counts.get("EMPTY", 0) + 1
        self.set_judge("BAD", 0, strength)

    def set_judge(self, text: str, points: int, strength: float) -> None:
        self.judge_text = JUDGE_LABEL.get(text, text)
        self.judge_kind = text
        self.judge_at = time.monotonic()
        self.judge_until = self.judge_at + 0.5
        if points:
            self.hit_flash_at = self.judge_at
            self.combo_pop_at = self.judge_at
            self.led_wave_at = self.judge_at
        col = JUDGE_COLORS.get(text, (255, 230, 160))
        power = max(0.6, min(1.8, strength / 700.0))
        n_dots = {"PERFECT": 24, "GREAT": 16, "GOOD": 10}.get(text, 4 if points else 3)
        for _ in range(n_dots):
            ang = random.uniform(0, math.tau)
            spd = random.uniform(60, 330) * power
            self.particles.append({
                "x": float(HIT_X), "y": float(LANE_Y),
                "vx": math.cos(ang) * spd,
                "vy": math.sin(ang) * spd - 90,
                "life": random.uniform(0.35, 0.8), "max": 0.8,
                "r": random.uniform(2.5, 6.0),
                "col": col, "star": False, "rot": 0.0,
            })
        if text == "PERFECT":
            for _ in range(10):
                ang = random.uniform(0, math.tau)
                spd = random.uniform(120, 480)
                self.particles.append({
                    "x": float(HIT_X), "y": float(LANE_Y),
                    "vx": math.cos(ang) * spd,
                    "vy": math.sin(ang) * spd - 160,
                    "life": random.uniform(0.6, 1.2), "max": 1.2,
                    "r": random.uniform(8.0, 16.0),
                    "col": (255, 240, 170), "star": True,
                    "rot": random.uniform(0, math.tau),
                })
            # ハート♥をふわっと舞い上げる
            for _ in range(4):
                self.particles.append({
                    "x": float(HIT_X) + random.uniform(-30, 30),
                    "y": float(LANE_Y) + random.uniform(-10, 10),
                    "vx": random.uniform(-90, 90),
                    "vy": random.uniform(-260, -160),
                    "life": random.uniform(0.9, 1.4), "max": 1.4,
                    "r": random.uniform(10, 14),
                    "col": (255, 110, 170), "heart": True, "rot": 0.0,
                })
        # 50/100/...の節目コンボで画面にリボン状の光が走る
        if points and self.combo > 0 and self.combo % 50 == 0:
            self.ribbon_at = self.judge_at

    def consume_serial(self) -> None:
        while True:
            try:
                msg = self.hit_queue.get_nowait()
            except queue.Empty:
                return
            typ = msg.get("type") or msg.get("ev")
            if typ == "hit":
                if time.monotonic() < self.input_locked_until:
                    continue
                msg_ms = int(msg.get("ms", -1))
                if msg_ms >= 0:
                    if msg_ms - self.last_serial_hit_ms < SERIAL_MSG_DEBOUNCE_MS:
                        continue
                    self.last_serial_hit_ms = msg_ms
                if self.state == "play":
                    # 叩いた瞬間のXYZから「上げ振り(=いちご牛乳)」かを判定
                    up = False
                    try:
                        up = self._is_up_gesture(float(msg.get("x", 0)),
                                                 float(msg.get("y", 0)),
                                                 float(msg.get("z", 0)))
                    except Exception:
                        up = False
                    self.register_hit(float(msg.get("power", msg.get("strength", 500))),
                                      up_gesture=up)
                elif self.state == "practice":
                    up = False
                    try:
                        up = self._is_up_gesture(float(msg.get("x", 0)),
                                                 float(msg.get("y", 0)),
                                                 float(msg.get("z", 0)))
                    except Exception:
                        pass
                    self.register_practice_hit(float(msg.get("power", msg.get("strength", 500))),
                                               up_gesture=up)
                elif self.state == "chapter":
                    ch = get_chapter(self.current_chapter_id)
                    if ch and ch.get("type") == "tambourine":
                        self.trigger_chapter_action()
            elif typ == "raw":
                self.last_raw = msg
                self._update_gravity(msg)

    def _update_gravity(self, msg: dict[str, Any]) -> None:
        """raw(常時送信)から重力ベクトルを低域追従で更新。
        カウントダウン中(構えて静止)は素早く合わせ、実質3秒キャリブレーションになる。"""
        try:
            gx = float(msg["x"]); gy = float(msg["y"]); gz = float(msg["z"])
        except Exception:
            return
        if self.state == "play" and not getattr(self, "music_started", True):
            # 最初の「かまえて！」3秒だけで上下判定の基準を測り、
            # 3・2・1に入ったら値を固定する。
            if time.monotonic() - self.countdown_started_at >= self.ready_calibration_duration:
                return
            a = 0.25
        elif self.state == "practice":
            a = 0.25
        else:
            a = 0.03
        self.gravity_vec[0] += (gx - self.gravity_vec[0]) * a
        self.gravity_vec[1] += (gy - self.gravity_vec[1]) * a
        self.gravity_vec[2] += (gz - self.gravity_vec[2]) * a

    def _is_up_gesture(self, x: float, y: float, z: float) -> bool:
        """叩いた瞬間の動的加速度が『縦(重力軸)方向に大きい』=上げ振りならTrue。
        横シャカ(いちご)は横成分が大きいので除外。実機の符号差に強いよう縦の大きさで判定。"""
        gx, gy, gz = self.gravity_vec
        gmag = math.sqrt(gx * gx + gy * gy + gz * gz) or 1.0
        ux, uy, uz = gx / gmag, gy / gmag, gz / gmag        # 重力(縦)方向の単位ベクトル
        mx, my, mz = x - gx, y - gy, z - gz                 # 動的加速度(重力を除く)
        vert = mx * ux + my * uy + mz * uz                  # 縦成分(符号つき)
        lx, ly, lz = mx - vert * ux, my - vert * uy, mz - vert * uz
        lateral = math.sqrt(lx * lx + ly * ly + lz * lz)    # 横成分の大きさ
        return abs(vert) > KIME_UP_THRESHOLD_MG and abs(vert) > lateral * KIME_UP_RATIO

    def recalibrate_gravity(self) -> None:
        """今の静止姿勢を『下』として即キャリブレーション(Gキー)。"""
        if self.last_raw:
            try:
                self.gravity_vec = [float(self.last_raw["x"]),
                                    float(self.last_raw["y"]),
                                    float(self.last_raw["z"])]
                self.kime_recalib_at = time.monotonic()
            except Exception:
                pass

    def update_misses(self) -> None:
        now = self.song_time()
        if now < 0:  # カウントダウン中はMISS判定しない
            return
        last_note_time = 0.0
        for n in self.notes:
            t = float(n["time"])
            # ロールノーツの場合は end_time も考慮
            if n.get("kind") == "roll":
                end_t = float(n.get("end_time", t))
                if end_t > last_note_time:
                    last_note_time = end_t
                # ゾーン終了後の処理
                if not n.get("completed") and now > end_t + 0.1:
                    n["completed"] = True
                    hits = int(n.get("hit_count", 0))
                    expected = int(n.get("expected_hits", 4))
                    # 期待回数の50%未満ならMISS
                    if hits < expected * 0.5:
                        n["missed"] = True
                        n["judge"] = "MISS"
                        self.combo = 0
                        self.judge_counts["MISS"] = self.judge_counts.get("MISS", 0) + 1
                        self.set_judge("MISS", 0, 0.5)
                    else:
                        n["hit"] = True
                        n["judge"] = "ROLL"
                        self.judge_counts["ROLL"] = self.judge_counts.get("ROLL", 0) + 1
                        # 上手い子ほど高得点: 振った回数に応じた完了ボーナス
                        bonus = hits * 30
                        if hits >= expected:
                            bonus += 300  # フルロール達成ボーナス
                        self.score += bonus
                        self.set_judge("ROLL", bonus, 1.0)
                continue
            if t > last_note_time:
                last_note_time = t
            if n["hit"] or n["missed"]:
                continue
            if t > self.play_duration:
                break
            if now - t > MISS_WINDOW:
                n["missed"] = True
                n["judge"] = "MISS"
                self.combo = 0
                self.judge_counts["MISS"] = self.judge_counts.get("MISS", 0) + 1
                self.set_judge("MISS", 0, 0.5)
            elif t > now + 2.0:
                break
        self.check_progress_gate(now)
        # 編集済み音源の設定終端まで必ず再生する。譜面末尾では早期終了しない。
        if self.song and now >= self.play_duration:
            self.finish_song()

    def check_progress_gate(self, now: float) -> None:
        if not self.song or self.gate_failed:
            return
        while self.next_checkpoint_index < len(self.checkpoints):
            gate = self.checkpoints[self.next_checkpoint_index]
            if now <= gate + MISS_WINDOW:
                return
            total, rate = self.score_rate_through(gate)
            self.last_checkpoint_seconds = gate
            self.last_checkpoint_rate = rate
            if rate < CLEAR_GOOD_RATE:
                self.gate_failed = True
                self.failed_gate_seconds = gate
                self.calculate_result_flags(gate)
                self.finish_song(aborted=False)
                return
            # v26: チェックポイント通過！派手にお祝い演出を出す。
            self.checkpoint_celebrated_at = time.monotonic()
            self.checkpoint_celebrated_gate = int(gate)
            self.checkpoint_celebrated_rate = rate
            # 効果音: ファンファーレ「ジャーン!」
            if self.fanfare_jaan_sfx:
                try:
                    self.fanfare_jaan_sfx.play()
                except Exception:
                    pass
            # ハートと星の祝福パーティクル
            for _ in range(40):
                ang = random.uniform(0, math.tau)
                spd = random.uniform(200, 500)
                self.particles.append({
                    "x": float(WIDTH // 2),
                    "y": float(HEIGHT // 2),
                    "vx": math.cos(ang) * spd,
                    "vy": math.sin(ang) * spd - 200,
                    "life": random.uniform(1.0, 1.8),
                    "max": 1.8,
                    "r": random.uniform(8, 18),
                    "col": random.choice([(255, 200, 230), (255, 230, 170),
                                          (200, 220, 255), (255, 170, 200)]),
                    "star": random.random() < 0.5,
                    "heart": random.random() < 0.3,
                    "rot": random.uniform(0, math.tau),
                })
            self.next_checkpoint_index += 1

    def update_bgm(self) -> None:
        """状態に合わせてBGMを切り替える。最終結果は専用BGMを使う。"""
        desired: str | None = None
        path: Path | None = None
        volume = 0.42

        if self.state in ("chapter", "name"):
            desired = "story"
            path = self.bgm_path
            volume = 0.42
        elif self.state == "ending":
            desired = "goal_end" if self.ending_result == 1 else "normal_end"
            path = self.goal_bgm_path if self.ending_result == 1 else self.normal_end_bgm_path
            volume = 0.54
        elif self.state == "gameover":
            desired = "normal_end"
            path = self.normal_end_bgm_path
            volume = 0.48

        if desired and path and path.exists():
            if self.current_bgm != desired:
                try:
                    pygame.mixer.music.stop()
                    pygame.mixer.music.load(str(path))
                    pygame.mixer.music.set_volume(volume)
                    pygame.mixer.music.play(-1)
                    self.current_bgm = desired
                except Exception as e:
                    print(f"BGM再生失敗({desired}): {e}")
                    self.current_bgm = None
        elif desired:
            # 専用BGMが見つからない時は、ストーリーBGMにフォールバック。
            fallback = self.bgm_path
            if fallback.exists() and self.current_bgm != "story":
                try:
                    pygame.mixer.music.stop()
                    pygame.mixer.music.load(str(fallback))
                    pygame.mixer.music.set_volume(0.42)
                    pygame.mixer.music.play(-1)
                    self.current_bgm = "story"
                except Exception as e:
                    print(f"ストーリーBGM再生失敗: {e}")
                    self.current_bgm = None
        else:
            # 音ゲー中(song)はそちらに任せる。それ以外で演出BGMが鳴っていたら止める。
            if self.current_bgm in ("story", "goal_end", "normal_end"):
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
                self.current_bgm = None

    def enter_ending(self) -> None:
        """最終章成功 → エンディング演出へ。"""
        self.session_ending = "bestten_first"
        self.ending_result = self.dice_result
        self.ending_outcome = self.chapter_event_outcome
        self.profile["last_goal_result"] = self.dice_result
        save_json_file(PROFILE_PATH, self.profile)
        self.ending_entered_at = time.monotonic()
        self.input_locked_until = self.ending_entered_at + RESULT_INPUT_GUARD_SEC
        self.state = "ending"
        if self.bestten_voice_sfx:
            try: self.bestten_voice_sfx.play()
            except Exception: pass
        # 紙吹雪を盛大に仕込む
        for _ in range(90):
            self.particles.append({
                "x": random.uniform(0, WIDTH), "y": random.uniform(-HEIGHT, 0),
                "vx": random.uniform(-40, 40), "vy": random.uniform(60, 200),
                "life": random.uniform(2.0, 4.0), "max": 4.0,
                "r": random.uniform(5, 11),
                "col": random.choice([(255, 150, 200), (255, 220, 120),
                                      (150, 210, 255), (190, 255, 180), (220, 170, 255)]),
                "star": random.random() < 0.3, "rot": random.uniform(0, math.tau),
            })
        for snd in (self.fanfare_1st_sfx, self.applause_sfx, self.celebration_sfx):
            if snd:
                try: snd.play()
                except Exception: pass

    def enter_gameover(self) -> None:
        """脱落 → 専用の脱落演出へ。"""
        self.session_ending = "disbanded"
        self.gameover_outcome = self.chapter_event_outcome
        self.gameover_chapter = get_chapter(self.current_chapter_id)
        self.gameover_entered_at = time.monotonic()
        self.input_locked_until = self.gameover_entered_at + RESULT_INPUT_GUARD_SEC
        self.state = "gameover"
        if self.disband_voice_sfx:
            try: self.disband_voice_sfx.play()
            except Exception: pass

    def update_gameover(self) -> None:
        """解散エンドロールを最後まで見せたら、自動で総合得点へ進む。"""
        if (self.state == "gameover"
                and time.monotonic() - self.gameover_entered_at >= GAMEOVER_AUTO_ADVANCE_SEC):
            self.enter_final_score()

    def _draw_confetti(self, elapsed: float, n: int = 70) -> None:
        """エンディング用の降りそそぐ紙吹雪(決定論的・軽量)。"""
        rng = random.Random(20260616)
        cols = [(255, 150, 200), (255, 220, 120), (150, 210, 255),
                (190, 255, 180), (220, 170, 255)]
        for i in range(n):
            bx = rng.uniform(0, WIDTH)
            speed = rng.uniform(70, 170)
            size = rng.uniform(7, 15)
            col = cols[i % len(cols)]
            y = ((elapsed * speed + rng.uniform(0, HEIGHT)) % (HEIGHT + 60)) - 30
            x = bx + math.sin(elapsed * 2 + i) * 22
            piece = pygame.Surface((int(size), int(size * 0.6)), pygame.SRCALPHA)
            piece.fill((*col, 235))
            piece = pygame.transform.rotate(piece, (elapsed * 140 + i * 31) % 360)
            self.screen.blit(piece, piece.get_rect(center=(int(x), int(y))))

    def update_particles(self, dt: float) -> None:
        alive = []
        for p in self.particles:
            p["life"] -= dt
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            gravity = 90 if p.get("heart") else 420
            p["vy"] += gravity * dt
            if p.get("star"):
                p["rot"] = p.get("rot", 0.0) + 5.0 * dt
            if p["life"] > 0:
                alive.append(p)
        self.particles = alive

    def draw_backstage_background(self, dim_alpha: int = 155) -> None:
        if self.bg_video and self.bg_video.draw(self.screen, dim_alpha):
            return
        self.screen.fill((18, 14, 36))

    def draw_panel(self, rect: pygame.Rect, alpha: int = 165, border: bool = True) -> None:
        panel = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
        pygame.draw.rect(panel, (22, 14, 48, alpha), panel.get_rect(), border_radius=28)
        if border:
            pygame.draw.rect(panel, (255, 220, 255, 105), panel.get_rect(), width=3, border_radius=28)
        self.screen.blit(panel, rect.topleft)

    def draw_button(self, rect: pygame.Rect, label: str, active: bool = True) -> None:
        base = (255, 220, 255) if active else (95, 85, 115)
        fill = (58, 35, 82) if active else (38, 34, 52)
        pygame.draw.rect(self.screen, base, rect, border_radius=18)
        pygame.draw.rect(self.screen, fill, rect.inflate(-6, -6), border_radius=15)
        label_surf = render_fit(self.small, label, (255, 245, 255), rect.width - 24, 16, True)
        self.screen.blit(label_surf, label_surf.get_rect(center=rect.center))

    def draw_text_shadow(self, surf: pygame.Surface, pos: tuple[int, int] | pygame.Rect) -> None:
        shadow = surf.copy()
        shadow.fill((0, 0, 0, 140), special_flags=pygame.BLEND_RGBA_MULT)
        if isinstance(pos, pygame.Rect):
            rect = pos.copy()
            shadow_rect = pos.copy()
            shadow_rect.move_ip(3, 3)
            self.screen.blit(shadow, shadow_rect)
            self.screen.blit(surf, rect)
        else:
            x, y = pos
            self.screen.blit(shadow, (x + 3, y + 3))
            self.screen.blit(surf, (x, y))

    def cover_for(self, song: Song, size=(260, 150)) -> pygame.Surface:
        key = song.id + str(size)
        if key in self.cover_cache:
            return self.cover_cache[key]
        if song.cover and song.cover.exists():
            img = pygame.image.load(str(song.cover)).convert()
            img = fit_image(img, size)
        else:
            img = pygame.Surface(size)
            img.fill((55, 40, 85))
            pygame.draw.rect(img, (255, 180, 220), img.get_rect(), 4, border_radius=24)
        self.cover_cache[key] = img
        return img

    def chapter_image(self, filename: str | None, size: tuple[int, int] | None = None, mode: str = "cover") -> "pygame.Surface | None":
        """章の背景イラストを指定サイズにフィットして返す(キャッシュつき)。

        mode="cover": 枠いっぱいに表示(中央クロップ)
        mode="contain": 画像全体を表示(余白あり)
        """
        if not filename:
            return None
        if size is None:
            size = (WIDTH, HEIGHT)
        key = f"{filename}@{size[0]}x{size[1]}@{mode}"
        if key in self.chapter_img_cache:
            return self.chapter_img_cache[key]
        path = BASE / "assets" / "story" / filename
        img = None
        if path.exists():
            try:
                raw = pygame.image.load(str(path)).convert_alpha()
                if mode == "contain":
                    img = fit_image_contain(raw, size)
                else:
                    img = fit_image(raw, size)
            except Exception as e:
                print(f"chapter画像読込失敗 {filename}: {e}")
                img = None
        self.chapter_img_cache[key] = img
        return img

    def draw_rounded_image(self, img: pygame.Surface, rect: pygame.Rect, radius: int = 20,
                           border: tuple[int, int, int] = (255, 220, 255)) -> None:
        """imgをrectに角丸クリップして描画し、枠を付ける。imgはrectと同サイズ前提。"""
        size = rect.size
        mask = pygame.Surface(size, pygame.SRCALPHA)
        pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
        clipped = img.copy()
        clipped.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        self.screen.blit(clipped, rect.topleft)
        pygame.draw.rect(self.screen, border, rect, 3, border_radius=radius)

    def top_scores_for(self, song: Song | None, mode: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        if song is None:
            return []
        # v10は song.id に保存。v9以前の :trial / :full も読み込みだけ互換で混ぜます。
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for key in [song.id, f"{song.id}:trial", f"{song.id}:full", f"{song.id}:challenge"]:
            for row in self.leaderboard.get(key, []):
                rid = str(row.get("record_id") or f"{key}:{len(rows)}")
                if rid in seen:
                    continue
                seen.add(rid)
                rows.append(row)
        return sort_scores(rows)[:limit]

    def draw_leaderboard(self, song: Song | None, x: int, y: int, limit: int = 5, mode: str | None = None) -> None:
        rows = self.top_scores_for(song, None, limit)
        visible_rows = max(1, min(limit, len(rows) if rows else 1))
        self.draw_panel(pygame.Rect(x - 18, y - 14, 520, 72 + visible_rows * 30), alpha=150)
        title = "ランキング" if song else "ランキング"
        title_surf = self.small.render(title, True, (255, 230, 170))
        self.screen.blit(title_surf, (x, y))
        if not rows:
            self.screen.blit(self.tiny.render("まだ記録なし。最初の1位を取りにいこう！", True, (230, 230, 255)), (x, y + 38))
            return
        for i, row in enumerate(rows):
            name = str(row.get("name", "MILK"))[:MAX_PLAYER_NAME]
            score = int(row.get("score", 0))
            combo = int(row.get("max_combo", 0))
            line_y = y + 38 + i * 30
            self.screen.blit(self.rank_font.render(f"{i+1}位", True, (255, 230, 170)), (x, line_y))
            self.screen.blit(render_fit(self.rank_font, name, (245, 245, 255), 180, 16), (x + 54, line_y))
            self.screen.blit(self.rank_font.render(f"{score:>7}", True, (245, 245, 255)), (x + 258, line_y))
            self.screen.blit(self.rank_font.render(f"コンボ {combo}", True, (220, 240, 255)), (x + 360, line_y))

    # =========================================================================
    # 章立てアイドルライフゲーム関連
    # =========================================================================
    def enter_chapter(self, chapter_id: int) -> None:
        """指定章へ移動してストーリー演出を始める。"""
        ch = get_chapter(chapter_id)
        if ch is None:
            self.state = "select"
            return
        self.current_chapter_id = chapter_id
        self.profile["current_chapter"] = chapter_id
        # 旧ボードの途中復帰を無効化
        self.profile["board_pos"] = 0
        save_json_file(PROFILE_PATH, self.profile)
        self.chapter_entered_at = time.monotonic()
        self.input_locked_until = self.chapter_entered_at + CHAPTER_INPUT_GUARD_SEC
        pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])
        self.chapter_substage = "intro"
        self.dice_result = 0
        self.chapter_event_outcome = None
        self.state = "chapter"
        # 運命ボタンを押した後ではなく、最終判定の説明画面に入った時点で再生する。
        if ch.get("type") == "tambourine" and self.fate_voice_sfx:
            try:
                self.fate_voice_sfx.stop()
                self.fate_voice_sfx.play()
            except Exception:
                pass

    def advance_chapter(self) -> None:
        """章のイベント完了 → 次の章へ進む。盤面には戻らない。"""
        outcome = self.chapter_event_outcome or {}
        if outcome.get("dead"):
            self.enter_gameover()
            return
        if outcome.get("goal"):
            self.enter_ending()
            return
        ch = get_chapter(self.current_chapter_id) or {}
        next_id = int(outcome.get("next") or ch.get("next") or 0)
        if next_id and get_chapter(next_id) is not None:
            self.enter_chapter(next_id)
        else:
            self.enter_ending()

    def restart_from_beginning(self) -> None:
        """新しい体験を開始し、第1章の名前入力へ移動する。"""
        self.board_pos = 0
        self.dice_value = 0
        self.board_steps_left = 0
        self.profile["board_pos"] = 0
        self.profile["current_chapter"] = 2
        save_json_file(PROFILE_PATH, self.profile)
        self.session_id = uuid.uuid4().hex
        self.session_records = []
        self.session_total_score = 0
        self.session_total_combo = 0
        self.session_ending = None
        self.final_rank = None
        self.final_record_saved = False
        self.name_draft = ""
        self.enter_name_screen(clear=True, return_to=2)

    def open_song_select(self) -> None:
        """Qキー用: ストーリーを壊さず、3曲の譜面選択画面を開く。"""
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        self.current_bgm = None
        self.chapter_return_to = None
        self.state = "select"

    def open_ranking_screen(self, return_state: str | None = None) -> None:
        """総合ランキングから開き、Qで各曲ランキングへ切り替える。"""
        self.leaderboard = load_leaderboard()
        previous = return_state or self.state
        if previous in ("ranking", "play", "time_up", "name"):
            previous = "select"
        self.ranking_return_state = previous
        self.ranking_page = "overall"
        self.state = "ranking"

    def close_ranking_screen(self) -> None:
        target = self.ranking_return_state
        if target not in ("title", "start", "select", "chapter", "board"):
            target = "select"
        self.state = target

    def cycle_ranking_screen(self) -> None:
        """総合 → 3曲 → 元画面、の順で切り替える。"""
        if self.ranking_page == "overall":
            self.ranking_page = "songs"
        else:
            self.close_ranking_screen()

    # ----- 旧ボード互換コード(ストーリー版では通常使わない) -----
    def save_board_pos(self) -> None:
        self.profile["board_pos"] = self.board_pos
        save_json_file(PROFILE_PATH, self.profile)

    def start_story(self) -> None:
        """第1章の名前入力から章立てストーリーを開始する。"""
        self.restart_from_beginning()

    def start_board(self) -> None:
        """旧版互換名。現在は盤面を使わず、章立てストーリーを開始する。"""
        self.start_story()

    def enter_board_space(self, pos: int) -> None:
        """旧ボード互換: 指定位置のイベントを発生させる。"""
        self.board_pos = max(0, min(pos, GOAL_INDEX))
        self.save_board_pos()
        space = get_space(self.board_pos)
        if "chapter" in space:
            self.return_after_event = "board"
            self.enter_chapter(space["chapter"])
        elif "fortune" in space:
            self.fortune_space = space["fortune"]
            self.fortune_entered_at = time.monotonic()
            self.board_phase = "fortune"
            self.state = "board"
        else:
            self.board_phase = "ready"
            self.state = "board"

    def return_to_board(self) -> None:
        """旧版互換名。ストーリー版では現在の章の次へ進む。"""
        self.advance_chapter()

    def board_roll(self) -> None:
        """旧ボード互換: ランダム移動を開始する。"""
        if self.board_phase != "ready":
            return
        if self.board_pos >= GOAL_INDEX:
            return
        self.board_phase = "rolling"
        self.board_roll_at = time.monotonic()
        if self.drumroll_sfx:
            try: self.drumroll_sfx.play()
            except Exception: pass

    def board_fortune_confirm(self) -> None:
        """旧ボード互換: fortune効果を適用する。"""
        space = self.fortune_space or {}
        step = int(space.get("step", 0))
        self.fortune_space = None
        if step:
            self.board_pos = max(0, min(self.board_pos + step, GOAL_INDEX))
            self.save_board_pos()
        self.return_to_board()

    def update_board(self) -> None:
        """旧ボード互換: 移動アニメを進める。"""
        if self.state != "board":
            return
        now = time.monotonic()
        if self.board_phase == "rolling":
            if now - self.board_roll_at >= 1.2:
                self.dice_value = random.choice([1, 2, 3])
                self.board_steps_left = self.dice_value
                self.board_phase = "moving"
                self.board_step_at = now + 0.4
                if self.fanfare_jaan_sfx:
                    try: self.fanfare_jaan_sfx.play()
                    except Exception: pass
        elif self.board_phase == "moving":
            if self.board_steps_left > 0 and now >= self.board_step_at:
                self.board_pos = min(self.board_pos + 1, GOAL_INDEX)
                self.board_steps_left -= 1
                self.board_step_at = now + 0.34
                snd = getattr(self, "tambourine_sfx", None)
                if snd:
                    try: snd.play()
                    except Exception: pass
                if self.board_steps_left <= 0 or self.board_pos >= GOAL_INDEX:
                    self.board_phase = "arrived"
                    self.board_step_at = now + 0.5
        elif self.board_phase == "arrived":
            if now >= self.board_step_at:
                self.enter_board_space(self.board_pos)


    def _board_nodes(self, area: pygame.Rect) -> list[tuple[int, int]]:
        """旧ボード互換: 座標を返す。"""
        cols = 5
        n = len(BOARD)
        rows = (n + cols - 1) // cols
        pad_x = area.width * 0.09
        pad_y = area.height * 0.20
        cw = (area.width - pad_x * 2) / max(1, cols - 1)
        rh = (area.height - pad_y * 2) / max(1, rows - 1)
        pts = []
        for i in range(n):
            r, c = divmod(i, cols)
            if r % 2 == 1:
                c = cols - 1 - c
            pts.append((int(area.x + pad_x + c * cw),
                        int(area.y + pad_y + r * rh)))
        return pts

    def draw_board(self) -> None:
        """旧ボード互換画面。ストーリー版の通常ルートでは表示しない。"""
        now = time.monotonic()
        if self.board_map_img is not None:
            self.screen.blit(pygame.transform.smoothscale(
                self.board_map_img, (WIDTH, HEIGHT)), (0, 0))
            ov = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            ov.fill((14, 8, 30, 200))
            self.screen.blit(ov, (0, 0))
        else:
            self.screen.fill((26, 14, 46))

        cx = WIDTH // 2
        margin = max(20, WIDTH // 48)
        # ロゴ(小)を先に描画 → 盤面・コマを上に重ねる
        title_logo = logo_image(min(210, WIDTH // 5))
        if title_logo is not None:
            self.screen.blit(title_logo, (margin, margin - 4))
        vtag = self.tiny.render(GAME_VERSION, True, (255, 235, 250))
        vtag.set_alpha(170)
        self.screen.blit(vtag, vtag.get_rect(bottomright=(WIDTH - 10, HEIGHT - 8)))

        # 盤面エリア(上)とコントロールパネル(下)。ロゴと被らないよう少し下げる
        panel_h = int(HEIGHT * 0.26)
        top_gap = int(HEIGHT * 0.10)
        board_area = pygame.Rect(margin, margin + top_gap, WIDTH - margin * 2,
                                 HEIGHT - panel_h - margin * 2 - top_gap - 10)
        nodes = self._board_nodes(board_area)

        # 道(マス間の線)
        for i in range(len(nodes) - 1):
            pygame.draw.line(self.screen, (255, 210, 240), nodes[i], nodes[i + 1], 6)
            pygame.draw.line(self.screen, (180, 130, 200), nodes[i], nodes[i + 1], 2)

        # マス
        node_r = max(20, int(board_area.height * 0.085))
        for i, (nx, ny) in enumerate(nodes):
            if i == GOAL_INDEX:
                base, ring = (255, 225, 120), (255, 255, 255)
            elif i < self.board_pos:
                base, ring = (255, 190, 225), (255, 230, 245)
            elif i == self.board_pos:
                base, ring = (255, 240, 120), (255, 255, 255)
            else:
                base, ring = (120, 100, 150), (190, 175, 215)
            pygame.draw.circle(self.screen, base, (nx, ny), node_r)
            pygame.draw.circle(self.screen, ring, (nx, ny), node_r, 3)
            label = "GOAL" if i == GOAL_INDEX else str(i + 1)
            num = font(int(node_r * 0.9), True).render(label, True, (60, 30, 70))
            self.screen.blit(num, num.get_rect(center=(nx, ny)))
            name = get_space(i).get("label", "")
            nm = render_fit(self.tiny, name, (245, 235, 255), int(node_r * 3.0), 11, True)
            self.screen.blit(nm, nm.get_rect(center=(nx, ny + node_r + 12)))

        # コマ(プレイヤー)
        px, py = nodes[min(self.board_pos, len(nodes) - 1)]
        bob = int(math.sin(now * 4) * 5)
        piece_y = py - node_r - 16 + bob
        pygame.draw.circle(self.screen, (255, 90, 160), (px, piece_y), 16)
        pygame.draw.circle(self.screen, (255, 255, 255), (px, piece_y), 16, 3)
        pygame.draw.polygon(self.screen, (255, 90, 160),
                            [(px - 8, piece_y + 12), (px + 8, piece_y + 12), (px, piece_y + 24)])

        # ---- 下部コントロールパネル ----
        panel = pygame.Rect(margin, HEIGHT - panel_h - margin, WIDTH - margin * 2, panel_h)
        self.draw_panel(panel, alpha=224)
        ph = panel.height

        def clamp(v, lo, hi):
            return int(max(lo, min(hi, v)))

        if self.board_phase == "fortune":
            sp = self.fortune_space or {}
            col = (255, 235, 150) if sp.get("good") else (180, 210, 255)
            head = render_fit(font(clamp(ph * 0.2, 20, 34), True),
                              ("OK " if sp.get("good") else "もう一歩 ") + sp.get("title", ""),
                              col, panel.width - 50, 18, True)
            self.draw_text_shadow(head, head.get_rect(center=(cx, panel.y + int(ph * 0.26))))
            body = render_fit(font(clamp(ph * 0.11, 14, 22), True), sp.get("text", ""),
                              (255, 245, 255), panel.width - 60, 12, True)
            self.screen.blit(body, body.get_rect(center=(cx, panel.y + int(ph * 0.52))))
            self._draw_event_button("つづける", cx, panel.bottom - int(ph * 0.18),
                                    hint="Enter / Space / クリック", elapsed=now)
        elif self.board_phase == "rolling":
            t = render_fit(font(clamp(ph * 0.12, 16, 24), True), "タンバリンで運命を決めてるたん…！",
                           (255, 235, 250), panel.width - 50, 14, True)
            self.screen.blit(t, t.get_rect(center=(cx, panel.y + int(ph * 0.26))))
            disp = random.choice([1, 2, 3])
            sc = 1.0 + math.sin((now - self.board_roll_at) * 30) * 0.06
            big = font(clamp(ph * 0.5, 40, 96), True).render(str(disp), True, (255, 230, 150))
            big = pygame.transform.rotozoom(big, 0, sc)
            self.draw_text_shadow(big, big.get_rect(center=(cx, panel.y + int(ph * 0.66))))
        elif self.board_phase == "moving":
            big = font(clamp(ph * 0.4, 36, 80), True).render(str(self.dice_value), True, (255, 230, 150))
            self.draw_text_shadow(big, big.get_rect(center=(cx, panel.y + int(ph * 0.42))))
            t = render_fit(font(clamp(ph * 0.12, 16, 24), True),
                           f"{self.dice_value}章へ進む！", (255, 245, 255), panel.width - 50, 14, True)
            self.screen.blit(t, t.get_rect(center=(cx, panel.y + int(ph * 0.78))))
        else:  # ready
            name = self.player_name or "あなた"
            head = render_fit(font(clamp(ph * 0.15, 18, 28), True),
                              f"{name}の番！タンバリンを振って運命判定！",
                              (255, 235, 250), panel.width - 50, 16, True)
            self.draw_text_shadow(head, head.get_rect(center=(cx, panel.y + int(ph * 0.28))))
            self._draw_event_button("タンバリンを振る！", cx,
                                    panel.bottom - int(ph * 0.30),
                                    hint="Enter / Space / クリック / 実機振り", elapsed=now,
                                    w=460, color=(255, 100, 180))


    def _draw_event_button(self, label: str, cx: int, cy: int, hint: str | None = None,
                           elapsed: float = 0.0, w: int = 340,
                           color: tuple[int, int, int] = (255, 140, 200),
                           alpha: int = 255) -> None:
        """ストーリーイベント共通のピンク角丸ボタン(+点滅ヒント)。"""
        alpha = max(0, min(255, int(alpha)))
        target = self.screen
        if alpha < 255:
            target = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        btn = pygame.Rect(0, 0, w, 58)
        btn.center = (cx, cy)
        pygame.draw.rect(target, color, btn, border_radius=29)
        pygame.draw.rect(target, (255, 200, 230), btn, 3, border_radius=29)
        lbl = render_fit(font(28, True), label, (255, 255, 255), w - 30, 18, True)
        target.blit(lbl, lbl.get_rect(center=btn.center))
        if hint:
            blink = (math.sin(elapsed * 3) + 1) / 2
            h = self.tiny.render(hint, True, (225, 220, 250))
            h.set_alpha(int(140 + 115 * blink))
            target.blit(h, h.get_rect(center=(cx, btn.bottom + 12)))
        if alpha < 255:
            target.set_alpha(alpha)
            self.screen.blit(target, (0, 0))

    def _chapter_reveal_lines(self, ch: dict[str, Any]) -> list[str]:
        """現在の場面で、1文字ずつ表示する本文を返す。"""
        ch_type = ch.get("type", "story")
        if ch_type == "story":
            source = ch.get("story", [])
        elif ch_type == "rhythm":
            source = ch.get("intro", [])
        elif ch_type == "tambourine" and self.chapter_substage == "intro":
            source = ch.get("intro", [])
        else:
            source = []
        return [str(line).replace("{name}", self.player_name or "あなた") for line in source]

    def _chapter_reveal_duration(self, ch: dict[str, Any]) -> float:
        lines = self._chapter_reveal_lines(ch)
        units = sum(len(line) for line in lines)
        units += max(0, len(lines) - 1) * STORY_LINE_PAUSE_CHARS
        return units / STORY_CHARS_PER_SEC if units else 0.0

    def choose_fate_result(self) -> int:
        """得点と同名での挑戦回数を反映し、1=第1位 / 2=解散を返す。"""
        self.session_total_score = sum(int(r.get("score", 0)) for r in self.session_records)
        board = load_leaderboard()
        overall = list(board.get("overall", []))
        current_name = clean_player_name(self.player_name).casefold()
        prior_attempts = sum(
            1 for row in overall
            if clean_player_name(str(row.get("name", ""))).casefold() == current_name
        )
        self.fate_attempt = prior_attempts + 1
        top_score = max((int(row.get("score", 0)) for row in overall), default=None)

        # 現在の総合1位以上の得点なら、実力でハッピーエンド確定。
        if top_score is None or self.session_total_score >= top_score:
            self.fate_reason = "provisional_first"
            result = 1
        elif self.fate_attempt >= 3:
            self.fate_reason = "third_try_guaranteed"
            result = 1
        else:
            chance = 0.5 if self.fate_attempt == 2 else (1.0 / 3.0)
            self.fate_reason = "second_try_half" if self.fate_attempt == 2 else "first_try_third"
            result = 1 if random.random() < chance else 2
        print(
            f"[運命] {self.player_name or 'プレイヤー'}: {self.fate_attempt}回目 / "
            f"総合{self.session_total_score}点 / 判定={self.fate_reason} / "
            f"結果={'ベストテン第1位' if result == 1 else '解散'}"
        )
        return result

    def draw_chapter_event(self) -> None:
        """個別章のイベント画面。上半分に絵、下半分にセリフパネル。"""
        now = time.monotonic()
        elapsed = now - self.chapter_entered_at
        ch = get_chapter(self.current_chapter_id)
        if ch is None:
            self.state = "select"
            return
        cx = WIDTH // 2
        ch_type = ch.get("type", "story")

        # 背景(暗色)
        self.screen.fill((24, 13, 42))

        # ===== 上部: 章の絵 =====
        margin = max(18, WIDTH // 56)
        # v42: 章タイトル/アイコンを出さず、絵と本文に画面を使う。
        # 画像は大きめにし、ストーリー絵は切れにくい contain 表示にする。
        if ch_type == "tambourine":
            art_ratio = 0.62
        elif ch_type == "story":
            art_ratio = 0.68
        else:
            art_ratio = 0.64
        art_w = WIDTH - margin * 2
        art_h = int(HEIGHT * art_ratio)
        art_rect = pygame.Rect(margin, margin, art_w, art_h)
        art_mode = "contain" if ch_type in ("story", "rhythm", "tambourine") else "cover"
        art = self.chapter_image(ch.get("image"), (art_w, art_h), mode=art_mode)
        if art is not None:
            self.draw_rounded_image(art, art_rect, radius=24)
        else:
            ph = pygame.Surface((art_w, art_h)); ph.fill((40, 26, 66))
            self.screen.blit(ph, art_rect.topleft)
            pygame.draw.rect(self.screen, (255, 220, 255), art_rect, 3, border_radius=24)

        # ===== 下部: セリフ/演出パネル =====
        gap = max(10, HEIGHT // 60)
        content = pygame.Rect(margin, art_rect.bottom + gap,
                              WIDTH - margin * 2,
                              HEIGHT - art_rect.bottom - gap - margin)
        self.draw_panel(content, alpha=224)

        # ===== レイアウトはパネル高さ(hp)に比例させ、どの画面サイズでも溢れないように =====
        hp = content.height

        def clamp(v, lo, hi):
            return int(max(lo, min(hi, v)))

        # v46: chapterだけ左上に復活。name/iconは出さず、本文/introを広く中央表示。
        chapter_label = ch.get("chapter", "")
        if chapter_label:
            # story/intro本文と同じくらいの大きさ。左上表示のまま色だけ変える。
            label_px = clamp(hp * 0.12, 20, 31)
            label_surf = render_fit(font(label_px, True), chapter_label,
                                    (255, 225, 170), content.width - 56, 13, True)
            self.draw_text_shadow(label_surf, (content.x + 24, content.y + 14))

        body_top = content.y + int(hp * 0.20)
        btn_cy = content.bottom - int(hp * 0.13)

        def draw_center_lines(lines, *, reveal: bool = True, color=(255, 245, 255),
                              area_bottom_pad: float = 0.04) -> int:
            """storyとintroを、位置を動かさず1文字ずつ表示する。"""
            clean_lines = [str(line).replace("{name}", self.player_name or "あなた") for line in lines]
            total_units = sum(len(line) for line in clean_lines)
            total_units += max(0, len(clean_lines) - 1) * STORY_LINE_PAUSE_CHARS
            visible_units = total_units if not reveal else max(0, int(elapsed * STORY_CHARS_PER_SEC))
            all_visible = visible_units >= total_units
            n = max(1, len(clean_lines))
            area_top = body_top
            area_bot = btn_cy - max(26, int(hp * area_bottom_pad))
            # 行間を詰め、その分フォントを大きくして展示距離でも読みやすくする。
            natural_h = clamp(hp * 0.17, 30, 48)
            line_h = min(natural_h, max(24.0, (area_bot - area_top) / n))
            group_h = line_h * n
            area_top += max(0, (area_bot - area_top - group_h) * 0.5)
            fsize = clamp(line_h * 0.88, 21, 38)
            fnt = font(fsize, True)
            remaining_units = visible_units
            for i, line in enumerate(clean_lines):
                shown_count = len(line) if not reveal else max(0, min(len(line), remaining_units))
                remaining_units = max(0, remaining_units - len(line))
                if shown_count >= len(line):
                    remaining_units = max(0, remaining_units - STORY_LINE_PAUSE_CHARS)
                if shown_count <= 0 and reveal:
                    continue
                shown = line[:shown_count]
                raw_full = fnt.render(line, True, color)
                raw_part = fnt.render(shown, True, color)
                max_w = content.width - 58
                scale = min(1.0, max_w / max(1, raw_full.get_width()))
                if scale < 0.999:
                    full_w = max(1, int(raw_full.get_width() * scale))
                    part_w = max(1, int(raw_part.get_width() * scale))
                    part_h = max(1, int(raw_part.get_height() * scale))
                    surf = pygame.transform.smoothscale(raw_part, (part_w, part_h))
                else:
                    full_w = raw_full.get_width()
                    surf = raw_part
                line_y = int(area_top + line_h * (i + 0.5))
                line_x = cx - full_w // 2
                self.screen.blit(surf, surf.get_rect(midleft=(line_x, line_y)))
                if reveal and shown_count < len(line) and int(now * 3) % 2 == 0:
                    cursor_x = line_x + surf.get_width() + 2
                    cursor_h = max(12, int(surf.get_height() * 0.72))
                    pygame.draw.rect(self.screen, color,
                                     (cursor_x, line_y - cursor_h // 2, 3, cursor_h),
                                     border_radius=2)
            return len(clean_lines) if all_visible else 0

        if ch_type == "story":
            story = ch.get("story", [])
            visible = draw_center_lines(story, reveal=True)
            if visible >= len(story):
                self._draw_event_button(ch.get("action_label", "次へ"), cx, btn_cy,
                                        hint="Enter / Space / クリック", elapsed=elapsed)

        elif ch_type == "rhythm":
            intro = ch.get("intro", [])
            visible = draw_center_lines(intro, reveal=True)
            if visible >= len(intro):
                self._draw_event_button(ch.get("action_label", "音ゲースタート！"), cx, btn_cy,
                                        hint="Enter / Space / クリック", elapsed=elapsed)

        elif ch_type == "tambourine":
            substage = self.chapter_substage

            if substage == "intro":
                intro = ch.get("intro", [])
                # 最終判定のintroもstoryと同じくらいの大きさで中央に表示。
                visible = draw_center_lines(intro, reveal=True, color=(255, 235, 250), area_bottom_pad=0.26)
                if visible >= len(intro):
                    self._draw_event_button(ch.get("action_label", "タンバリンを振る！"), cx, btn_cy,
                                            hint="Enter / Space / クリック / 実機振り",
                                            elapsed=elapsed, w=520, color=(255, 100, 180))

            elif substage == "dice_rolling":
                roll_elapsed = now - self.dice_animation_at
                dots = "." * (1 + int(roll_elapsed * 2.5) % 3)
                sc = 1.0 + math.sin(roll_elapsed * 4.0) * 0.025
                surf = render_fit(font(clamp(hp * 0.20, 30, 68), True),
                                  f"15369の運命やいかに{dots}",
                                  (255, 230, 170), content.width - 70, 18, True)
                surf = pygame.transform.rotozoom(surf, 0, sc)
                self.draw_text_shadow(surf, surf.get_rect(center=(cx, (body_top + btn_cy) // 2)))
                msg = render_fit(font(clamp(hp * 0.07, 16, 26), True),
                                 "ベストテン第1位か、それとも解散か――",
                                 (255, 245, 255), content.width - 50, 14, True)
                self.screen.blit(msg, msg.get_rect(center=(cx, btn_cy)))
                if roll_elapsed >= FATE_SUSPENSE_SEC:
                    self.dice_result = self.choose_fate_result()
                    self.dice_locked_at = now
                    self.chapter_event_outcome = ch["outcomes"].get(self.dice_result, ch["outcomes"][2])
                    pygame.event.clear([pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN])
                    if self.dice_result == 1:
                        self.enter_ending()
                    else:
                        self.enter_gameover()

            elif substage == "result":
                rdt = now - self.dice_locked_at
                outcome = self.chapter_event_outcome or {}
                is_goal = bool(outcome.get("goal"))
                result_label = "ベストテン第1位" if is_goal else "解散"
                result_color = (255, 230, 110) if is_goal else (255, 135, 155)
                if rdt > 0.4:
                    # 数字は出さず、プレイヤーに見える結果は二択だけにする。
                    ra_top = body_top
                    ra_bot = btn_cy - 32
                    ra_h = max(60, ra_bot - ra_top)

                    title = render_fit(font(clamp(ra_h * 0.22, 26, 48), True), result_label,
                                       result_color, content.width - 60, 18, True)
                    self.draw_text_shadow(title, title.get_rect(center=(cx, ra_top + int(ra_h * 0.20))))

                    t = render_fit(font(clamp(ra_h * 0.13, 16, 28), True), outcome.get("title", result_label),
                                   (255, 245, 255), content.width - 60, 15, True)
                    self.draw_text_shadow(t, t.get_rect(center=(cx, ra_top + int(ra_h * 0.44))))

                    desc_lines = outcome.get("desc", "").split("\n")
                    nd = max(1, len(desc_lines))
                    dz_top = ra_top + int(ra_h * 0.60)
                    d_h = (ra_bot - dz_top) / nd
                    for i, ln in enumerate(desc_lines):
                        s = render_fit(font(clamp(d_h * 0.7, 12, 21), True), ln,
                                       (255, 245, 255), content.width - 60, 11, True)
                        self.screen.blit(s, s.get_rect(center=(cx, int(dz_top + d_h * (i + 0.5)))))
                else:
                    surf = render_fit(font(clamp(hp * 0.24, 34, 78), True), result_label,
                                      result_color, content.width - 80, 20, True)
                    bounce = 1.0 + max(0.0, 0.5 - rdt) * 0.65
                    surf = pygame.transform.rotozoom(surf, 0, min(1.45, bounce))
                    self.draw_text_shadow(surf, surf.get_rect(center=(cx, (body_top + btn_cy) // 2)))

                if rdt > 1.5:
                    if outcome.get("dead"):
                        bt = "終わりを見る"
                    elif outcome.get("goal"):
                        bt = "ゴールへ"
                    else:
                        bt = "次へ"
                    self._draw_event_button(bt, cx, btn_cy, elapsed=elapsed)

    def draw_ending(self) -> None:
        """最終タンバリンで第1位になった時のゴールエンディング。"""
        now = time.monotonic(); elapsed = now - self.ending_entered_at
        cx = WIDTH // 2
        self.screen.fill((30, 16, 52))
        margin = max(18, WIDTH // 56)
        art_w = WIDTH - margin * 2
        art_h = int(HEIGHT * 0.62)
        art_rect = pygame.Rect(margin, margin, art_w, art_h)
        art = self.chapter_image("story_ranking.png", (art_w, art_h), mode="contain")
        if art is not None:
            self.draw_rounded_image(art, art_rect, radius=24, border=(255, 230, 150))
        self._draw_confetti(elapsed, n=95)

        gap = max(10, HEIGHT // 68)
        content = pygame.Rect(margin, art_rect.bottom + gap,
                              WIDTH - margin * 2, HEIGHT - art_rect.bottom - gap - margin)
        self.draw_panel(content, alpha=226)
        hp = content.height

        def clamp(v, lo, hi):
            return int(max(lo, min(hi, v)))

        big = font(clamp(hp * 0.25, 30, 70), True).render("ベストテン第1位！", True, (255, 225, 120))
        sc = 1.0 + max(0.0, 0.45 - elapsed) * 1.2
        big = pygame.transform.rotozoom(big, 0, min(1.45, sc))
        self.draw_text_shadow(big, big.get_rect(center=(cx, content.y + int(hp * 0.22))))

        name = self.player_name or "あなた"
        lines = [
            "15369はついにベストテン第1位を獲得！",
            f"{name}のタンバリンが、最高のゴールへ導いた。",
        ]
        line_px = clamp(hp * 0.105, 19, 30)
        for i, ln in enumerate(lines):
            s = render_fit(font(line_px, True), ln, (255, 235, 250),
                           content.width - 60, 12, True)
            self.screen.blit(s, s.get_rect(center=(cx, content.y + int(hp * 0.48) + i * int(hp * 0.12))))

        btn_cy = content.bottom - int(hp * 0.13)
        self._draw_event_button(
            "あなたの得点を見る", cx, btn_cy,
            hint="Enter / Space / クリック", elapsed=elapsed,
            w=460, color=(220, 55, 145))

    def draw_gameover(self) -> None:
        """第1位を取れなかった時の通常エンド。終わりのスクロール風に表示する。"""
        now = time.monotonic(); elapsed = now - self.gameover_entered_at
        cx = WIDTH // 2
        self.screen.fill((18, 12, 28))
        margin = max(18, WIDTH // 56)
        art_w = WIDTH - margin * 2
        art_h = int(HEIGHT * 0.60)
        art_rect = pygame.Rect(margin, margin, art_w, art_h)
        art = self.chapter_image("story_nomalend.png", (art_w, art_h), mode="contain")
        if art is not None:
            self.draw_rounded_image(art, art_rect, radius=24, border=(190, 150, 210))

        # 下部を少し暗くして、映画のエンドロール風に文字を流す。
        fade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        fade.fill((0, 0, 0, 0))
        pygame.draw.rect(fade, (8, 4, 14, 170), pygame.Rect(0, int(HEIGHT * 0.54), WIDTH, int(HEIGHT * 0.46)))
        self.screen.blit(fade, (0, 0))

        outcome = self.gameover_outcome or {}
        title = outcome.get("title", "15369 解散")
        name = self.player_name or "あなた"
        scroll_lines = [
            title,
            "",
            "ベストテン第1位には、あと少し届かなかった。",
            "約束どおり、15369はここで解散することになった。",
            "",
            "メンバーたちは泣きながら笑って、最後のステージを降りた。",
            f"{name}はタンバリンを大切にしまい、また新しい日々へ歩き出す。",
            "",
            "みんなはそれぞれの生活へ戻っていく。",
            "でも、いちごみるく色の思い出は、ずっと消えない。",
            "",
            "END",
        ]

        def clamp(v, lo, hi):
            return int(max(lo, min(hi, v)))

        line_px = clamp(HEIGHT * 0.042, 23, 38)
        title_px = clamp(HEIGHT * 0.060, 34, 54)
        start_y = HEIGHT + 40 - elapsed * 42
        line_gap = clamp(HEIGHT * 0.056, 36, 52)
        for i, ln in enumerate(scroll_lines):
            y = int(start_y + i * line_gap)
            if y < -80 or y > HEIGHT + 80:
                continue
            if i == 0:
                surf = render_fit(font(title_px, True), ln, (255, 215, 230), WIDTH - 120, 18, True)
            elif ln == "END":
                surf = render_fit(font(title_px, True), ln, (255, 235, 170), WIDTH - 120, 18, True)
            else:
                surf = render_fit(font(line_px, True), ln, (245, 235, 248), WIDTH - 150, 14, True)
            self.draw_text_shadow(surf, surf.get_rect(center=(cx, y)))

        # エンドロールを十分読んだ後だけ、ボタンをふわっと表示する。
        if elapsed >= GAMEOVER_BUTTON_REVEAL_SEC:
            reveal_alpha = min(255, int((elapsed - GAMEOVER_BUTTON_REVEAL_SEC) / 1.0 * 255))
            self._draw_event_button(
                "あなたの得点を見る", cx,
                HEIGHT - scaled_px(72, min_px=62, max_px=90),
                hint="Enter / Space / クリック", elapsed=elapsed,
                w=460, color=(220, 55, 145), alpha=reveal_alpha)

    def draw_final_score(self) -> None:
        """物語上の運エンド後に、実力で決まる総合得点と順位を発表する。"""
        elapsed = time.monotonic() - self.final_score_entered_at
        # 2秒後、得点の数字が現れる瞬間に「ジャーン！」を鳴らす。
        if (not self.final_score_reveal_sfx_played
                and elapsed >= 2.0):
            self.final_score_reveal_sfx_played = True
            if self.drumroll_sfx:
                try: self.drumroll_sfx.stop()
                except Exception: pass
            if self.fanfare_jaan_sfx:
                try: self.fanfare_jaan_sfx.play()
                except Exception: pass
        # 「あなたの得点は」→得点表示が終わり、体験ありがとう画面へ切り替わってから再生する。
        if (not self.final_thanks_voice_played
                and elapsed >= 4.1
                and self.thanks_voice_sfx):
            self.final_thanks_voice_played = True
            try: self.thanks_voice_sfx.play()
            except Exception: pass
        cx, cy = WIDTH // 2, HEIGHT // 2
        self.screen.fill((25, 12, 44))
        if self.result_bg_image is not None:
            bg = pygame.transform.smoothscale(self.result_bg_image, (WIDTH, HEIGHT))
            self.screen.blit(bg, (0, 0))
            shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            shade.fill((18, 8, 34, 100))
            self.screen.blit(shade, (0, 0))

        # まず「あなたの得点は!!」、次に得点、最後に展示ランキングを見せる。
        if elapsed < 2.0:
            shake = int(math.sin(elapsed * 38) * 5)
            t1 = font(scaled_px(50, min_px=36, max_px=72), True).render(
                "そして――", True, (255, 225, 245))
            t2 = font(scaled_px(82, min_px=56, max_px=112), True).render(
                "あなたの得点は！！", True, (255, 240, 170))
            self.draw_text_shadow(t1, t1.get_rect(center=(cx, cy - 90)))
            self.draw_text_shadow(t2, t2.get_rect(center=(cx + shake, cy + 20)))
            return

        if elapsed < 4.0:
            label = font(scaled_px(38, min_px=28, max_px=54), True).render(
                f"完了した曲：{len(self.session_records)}曲", True, (255, 225, 245))
            score = font(scaled_px(118, min_px=80, max_px=160), True).render(
                f"{self.session_total_score:,}点！", True, (255, 235, 120))
            pop = min(1.25, 1.0 + max(0.0, 0.45 - (elapsed - 2.0)) * 0.5)
            score = pygame.transform.rotozoom(score, 0, pop)
            self.screen.blit(label, label.get_rect(center=(cx, cy - 110)))
            self.draw_text_shadow(score, score.get_rect(center=(cx, cy + 10)))
            return

        if self.final_rank == 1:
            art = self.chapter_image("story_provisional_first.png", (WIDTH, HEIGHT), mode="contain")
            if art is not None:
                self.screen.blit(art, (0, 0))
            self._draw_confetti(elapsed, n=95)
        else:
            self.draw_panel(pygame.Rect(int(WIDTH * 0.10), int(HEIGHT * 0.16),
                                        int(WIDTH * 0.80), int(HEIGHT * 0.66)), alpha=205)
            thanks = font(scaled_px(68, min_px=48, max_px=94), True).render(
                "体験ありがとう！", True, (255, 235, 250))
            self.draw_text_shadow(thanks, thanks.get_rect(center=(cx, cy - 110)))
            if self.final_rank is not None:
                rank_text = f"総合 {self.final_rank}位"
            elif self.session_records:
                rank_text = "総合記録を保存しました"
            else:
                rank_text = "また挑戦してね！"
            rank = font(scaled_px(86, min_px=58, max_px=120), True).render(
                rank_text, True, (255, 230, 135))
            self.draw_text_shadow(rank, rank.get_rect(center=(cx, cy + 10)))
            detail = self.mid.render(
                f"総合得点 {self.session_total_score:,}点 ／ 完了 {len(self.session_records)}曲",
                True, (225, 225, 255))
            self.screen.blit(detail, detail.get_rect(center=(cx, cy + 105)))
        hint = self.small.render("Enter / Space でタイトル画面へ", True, (255, 245, 255))
        self.screen.blit(hint, hint.get_rect(center=(cx, HEIGHT - 34)))

    def trigger_chapter_action(self) -> None:
        """現在のチャプターでEnter/Spaceまたはクリックが押された時の処理。"""
        ch = get_chapter(self.current_chapter_id)
        if ch is None:
            self.state = "select"
            return
        ch_type = ch.get("type", "story")
        now = time.monotonic()
        if now < self.input_locked_until:
            return
        elapsed = now - self.chapter_entered_at

        # 文章表示中の操作は、場面を進めず全文表示だけ行う。
        reveal_duration = self._chapter_reveal_duration(ch)
        if reveal_duration > 0.0 and elapsed < reveal_duration:
            self.chapter_entered_at = now - reveal_duration
            return

        if ch_type == "story":
            story = ch.get("story", [])
            if ch.get("practice"):
                self.enter_practice(int(ch.get("next", 4)))
            else:
                self.advance_chapter()
        elif ch_type == "rhythm":
            # 指定曲の音ゲーへ
            song_id = ch.get("song_id")
            for i, s in enumerate(self.songs):
                if s.id == song_id:
                    self.selected = i
                    self.chapter_return_to = ch.get("next")
                    self.start_song(i)
                    return
        elif ch_type == "tambourine":
            if self.chapter_substage == "intro":
                self.chapter_substage = "dice_rolling"
                self.dice_animation_at = now
                if self.drumroll_sfx:
                    try: self.drumroll_sfx.play()
                    except Exception: pass
            elif self.chapter_substage == "result":
                if now - self.dice_locked_at < 1.5:
                    return  # まだ結果表示中
                outcome = self.chapter_event_outcome
                if outcome and outcome.get("dead"):
                    self.enter_gameover()
                elif outcome and outcome.get("goal"):
                    self.current_chapter_id = ch.get("id", self.current_chapter_id)
                    self.enter_ending()
                else:
                    self.advance_chapter()

    def append_name_text(self, text: str) -> None:
        """TEXTINPUT/IME確定文字だけを名前へ追加する。"""
        incoming = filter_name_input(text)
        if not incoming:
            return
        space_left = MAX_PLAYER_NAME - len(self.name_draft)
        if space_left > 0:
            self.name_draft += incoming[:space_left]

    def enter_name_screen(self, *, clear: bool = False, return_to: Any = None) -> None:
        """名前入力画面へ入る時の共通処理。IME入力を明示的にONにする。"""
        self.public_name_confirmed = False
        if clear:
            self.name_draft = ""
        self.text_editing = ""
        self.chapter_return_to = return_to
        self.name_entered_at = time.monotonic()
        try:
            pygame.key.start_text_input()
            pygame.key.set_text_input_rect(self.name_input_rect)
            self._text_input_active = True
        except Exception:
            pass
        self.state = "name"

    def sync_text_input_mode(self) -> None:
        """IMEは公開名入力中だけ有効にし、他画面の入力小窓を防ぐ。"""
        should_enable = self.state == "name"
        if should_enable == self._text_input_active:
            return
        try:
            if should_enable:
                pygame.key.start_text_input()
                pygame.key.set_text_input_rect(self.name_input_rect)
            else:
                pygame.key.stop_text_input()
            self._text_input_active = should_enable
        except Exception:
            pass

    def enter_practice(self, return_to: int = 4) -> None:
        """本番と同じ流れるレーンで3種類を1回ずつ練習する。"""
        self.practice_step = 0
        self.practice_hits = 0
        self.practice_return_to = return_to
        self.practice_feedback = "右から流れてくるマークを見てね！"
        self.practice_feedback_at = 0.0
        self.practice_completed_at = 0.0
        self.practice_note_started_at = time.monotonic() + 0.5
        self.practice_target_time = 2.8
        self.state = "practice"

    def register_practice_hit(self, strength: float = 700.0, up_gesture: bool = False) -> None:
        if self.state != "practice" or self.practice_completed_at:
            return
        now = time.monotonic()
        elapsed = now - self.practice_note_started_at
        window = 0.62 if self.practice_step == 1 else 0.46
        if abs(elapsed - self.practice_target_time) > window:
            retry_actions = (
                "ピンクの丸に重なったら、横に振る・叩く！",
                "ピンクの丸に重なったら、すばやく3回連打！",
                "ピンクの丸に重なったら、上へ振り上げる！",
            )
            self.practice_feedback = retry_actions[min(self.practice_step, 2)]
            self.practice_feedback_at = now
            return
        self.play_tambourine_sfx(strength)
        if self.practice_step == 0:
            if up_gesture:
                self.practice_feedback = "いちごは横に振る・叩く！"
                self.practice_feedback_at = now
                return
            self.practice_feedback = "できた！ いちご成功！"
            self.practice_step = 1
            self.practice_hits = 0
            self.practice_note_started_at = now + 0.8
        elif self.practice_step == 1:
            if up_gesture:
                self.practice_feedback = "ROLLは横にすばやく振ろう！"
                self.practice_feedback_at = now
                return
            self.practice_hits += 1
            if self.practice_hits < 3:
                self.practice_feedback = f"その調子！ あと{3 - self.practice_hits}回！"
                self.practice_feedback_at = now
                return
            self.practice_feedback = "できた！ ROLL成功！"
            self.practice_step = 2
            self.practice_note_started_at = now + 0.8
        else:
            if not up_gesture:
                self.practice_feedback = "タンバリンを上へ振り上げよう！"
                self.practice_feedback_at = now
                return
            self.practice_feedback = "3種類ぜんぶ成功！"
            self.practice_step = 3
            self.practice_completed_at = now
        self.practice_feedback_at = now

    def update_practice(self) -> None:
        if self.state != "practice":
            return
        now = time.monotonic()
        if self.practice_completed_at:
            if now - self.practice_completed_at >= 1.4:
                self.enter_chapter(self.practice_return_to)
            return
        if now - self.practice_note_started_at > self.practice_target_time + 1.0:
            self.practice_note_started_at = now + 0.45
            self.practice_hits = 0
            self.practice_feedback = "大丈夫！ もう一度、丸に重なる瞬間をねらおう！"
            self.practice_feedback_at = now

    def draw_practice_cards_legacy(self) -> None:
        """いちご・ROLL・いちご牛乳を順番に体験する案内画面。"""
        self.screen.fill((28, 15, 48))
        cx = WIDTH // 2
        title = font(scaled_px(52, min_px=38, max_px=72), True).render(
            "本番前にタンバリン練習！", True, (255, 240, 255))
        self.draw_text_shadow(title, title.get_rect(center=(cx, scaled_px(82, min_px=60, max_px=110))))
        steps = [
            ("いちご", "タンバリンを横に振る・叩く", "Space"),
            ("ROLL", "すばやく3回、連続で横に振る", "Space × 3"),
            ("いちご牛乳", "タンバリンを上へ振り上げる", "↑キー"),
        ]
        card_w = min(scaled_px(430, min_px=300, max_px=520), (WIDTH - 120) // 3)
        card_h = int(HEIGHT * 0.54)
        gap = scaled_px(26, min_px=14, max_px=34)
        total_w = card_w * 3 + gap * 2
        x0 = cx - total_w // 2
        for i, (label, instruction, key_hint) in enumerate(steps):
            rect = pygame.Rect(x0 + i * (card_w + gap), int(HEIGHT * 0.20), card_w, card_h)
            active = i == min(self.practice_step, 2)
            done = i < self.practice_step
            fill = (110, 58, 118, 235) if active else ((64, 75, 82, 215) if done else (45, 31, 68, 205))
            panel = pygame.Surface(rect.size, pygame.SRCALPHA)
            pygame.draw.rect(panel, fill, panel.get_rect(), border_radius=28)
            pygame.draw.rect(panel, (255, 225, 150) if active else (190, 170, 215), panel.get_rect(), 4, border_radius=28)
            self.screen.blit(panel, rect)
            # 画像の上に重なって見えるため、手順番号の1・2・3は表示しない。
            if done:
                ms = font(scaled_px(42, min_px=30, max_px=58), True).render(
                    "✓", True, (255, 235, 150))
                self.screen.blit(ms, ms.get_rect(
                    center=(rect.x + int(card_w * 0.10), rect.y + int(card_h * 0.09))))
            # 本番レーンで使っているノーツ画像を、そのまま大きく見せる。
            source_img = self.note_kime_img if i == 2 else (self.note_tap_img if i == 0 else None)
            # ROLLは、いちご画像なしで本番と同じ黄色い連打バーだけを表示する。
            if i == 1:
                bar = pygame.Rect(rect.centerx - int(card_w * 0.34),
                                  rect.y + int(card_h * 0.19),
                                  int(card_w * 0.68), int(card_h * 0.12))
                glow = pygame.Surface((bar.width + 36, bar.height + 30), pygame.SRCALPHA)
                pygame.draw.rect(glow, (255, 205, 105, 75), glow.get_rect(),
                                 border_radius=(bar.height + 30) // 2)
                self.screen.blit(glow, (bar.x - 18, bar.y - 15),
                                 special_flags=pygame.BLEND_ADD)
                pygame.draw.rect(self.screen, (255, 205, 105), bar,
                                 border_radius=bar.height // 2)
                pygame.draw.rect(self.screen, (255, 245, 205), bar, 3,
                                 border_radius=bar.height // 2)
                for sx in range(bar.x + 32, bar.right - 20, 58):
                    pygame.draw.circle(self.screen, (255, 250, 215),
                                       (sx, bar.centery), 5)
            if source_img is not None:
                img_size = min(int(card_w * 0.42), int(card_h * 0.34))
                note_img = pygame.transform.smoothscale(source_img, (img_size, img_size))
                self.screen.blit(note_img, note_img.get_rect(
                    center=(rect.centerx, rect.y + int(card_h * 0.25))))
            ls = render_fit(font(scaled_px(34, min_px=25, max_px=46), True), label,
                            (255, 245, 255), rect.width - 30, 20, True)
            self.screen.blit(ls, ls.get_rect(center=(rect.centerx, rect.y + int(card_h * 0.48))))
            ins = render_fit(font(scaled_px(24, min_px=18, max_px=32), True), instruction,
                             (255, 225, 245), rect.width - 32, 15, True)
            self.screen.blit(ins, ins.get_rect(center=(rect.centerx, rect.y + int(card_h * 0.66))))
            kh = font(scaled_px(20, min_px=16, max_px=28), True).render(
                f"キーボード：{key_hint}", True, (205, 220, 255))
            self.screen.blit(kh, kh.get_rect(center=(rect.centerx, rect.y + int(card_h * 0.84))))
        feedback = self.practice_feedback or "1番から順番にやってみよう！"
        fs = font(scaled_px(34, min_px=25, max_px=48), True).render(feedback, True, (255, 230, 150))
        self.draw_text_shadow(fs, fs.get_rect(center=(cx, int(HEIGHT * 0.82))))
        hold = self.small.render("最初に胸の前で構えて静止すると、上下を正しく判定できます", True, (235, 225, 255))
        self.screen.blit(hold, hold.get_rect(center=(cx, int(HEIGHT * 0.91))))

    def draw_practice_flow(self) -> None:
        """本番レーンを使い、判定サークルの意味から体験して覚える。"""
        self.screen.fill((28, 15, 48))
        cx = WIDTH // 2
        title = font(scaled_px(45, min_px=32, max_px=62), True).render(
            "本番と同じ画面で練習！", True, (255, 240, 255))
        self.draw_text_shadow(title, title.get_rect(center=(cx, int(HEIGHT * 0.08))))

        names = ("いちご：横に振る・叩く", "ROLL：すばやく3回連打",
                 "いちご牛乳：上へ振り上げる")
        keys = ("キーボード：Space", "キーボード：Space × 3", "キーボード：↑")
        step = min(self.practice_step, 2)
        instruction = font(scaled_px(31, min_px=22, max_px=42), True).render(
            names[step], True, (255, 225, 150))
        self.screen.blit(instruction, instruction.get_rect(center=(cx, int(HEIGHT * 0.17))))
        key = self.small.render(keys[step], True, (205, 220, 255))
        self.screen.blit(key, key.get_rect(center=(cx, int(HEIGHT * 0.22))))

        lane_y = int(HEIGHT * 0.55)
        hit_x = int(WIDTH * 0.16)
        spawn_x = WIDTH + 60
        band = pygame.Rect(0, lane_y - int(HEIGHT * 0.12), WIDTH, int(HEIGHT * 0.24))
        pygame.draw.rect(self.screen, (14, 9, 24), band)
        pygame.draw.line(self.screen, (255, 120, 180), (0, band.top), (WIDTH, band.top), 2)
        pygame.draw.line(self.screen, (255, 120, 180), (0, band.bottom), (WIDTH, band.bottom), 2)

        pygame.draw.circle(self.screen, (255, 90, 145), (hit_x, lane_y), 42, 6)
        pygame.draw.circle(self.screen, (255, 225, 238), (hit_x, lane_y), 24, 2)
        circle_actions = (
            "重なった瞬間に、横に振る・叩く！",
            "重なったら、すばやく3回連打！",
            "重なった瞬間に、上へ振り上げる！",
        )
        arrow_text = render_fit(
            font(scaled_px(26, min_px=19, max_px=35), True),
            circle_actions[step], (255, 235, 165), WIDTH - hit_x - 110, 17, True)
        arrow_x = min(WIDTH - arrow_text.get_width() - 20, hit_x + 80)
        self.screen.blit(arrow_text, (arrow_x, band.top + 16))
        pygame.draw.line(self.screen, (255, 235, 165),
                         (arrow_x - 10, band.top + 48), (hit_x + 25, lane_y - 34), 4)

        elapsed = time.monotonic() - self.practice_note_started_at
        progress = elapsed / max(0.1, self.practice_target_time)
        x = int(spawn_x + (hit_x - spawn_x) * progress)
        if self.practice_step < 3:
            if step == 1:
                bar_w = scaled_px(250, min_px=170, max_px=330)
                bar_h = scaled_px(42, min_px=30, max_px=56)
                bar = pygame.Rect(x - bar_w // 2, lane_y - bar_h // 2, bar_w, bar_h)
                pygame.draw.rect(self.screen, (255, 205, 105), bar,
                                 border_radius=bar_h // 2)
                pygame.draw.rect(self.screen, (255, 245, 205), bar, 3,
                                 border_radius=bar_h // 2)
                count = font(scaled_px(27, min_px=20, max_px=36), True).render(
                    f"{self.practice_hits}/3", True, (80, 38, 10))
                self.screen.blit(count, count.get_rect(center=bar.center))
            else:
                source = self.note_tap_img if step == 0 else self.note_kime_img
                size = scaled_px(76, min_px=54, max_px=100)
                if source is not None:
                    img = pygame.transform.smoothscale(source, (size, size))
                    self.screen.blit(img, img.get_rect(center=(x, lane_y)))
                else:
                    pygame.draw.circle(self.screen, (255, 180, 210),
                                       (x, lane_y), size // 2)

        feedback = self.practice_feedback
        fs = render_fit(font(scaled_px(32, min_px=23, max_px=44), True), feedback,
                        (255, 230, 150), WIDTH - 80, 18, True)
        self.draw_text_shadow(fs, fs.get_rect(center=(cx, int(HEIGHT * 0.78))))
        progress_text = self.small.render(
            f"練習 {min(self.practice_step + 1, 3)} / 3　　成功したら次のマークへ進みます",
            True, (235, 225, 255))
        self.screen.blit(progress_text, progress_text.get_rect(center=(cx, int(HEIGHT * 0.88))))

    def draw_title(self) -> None:
        now = time.monotonic(); elapsed = now - self.title_entered_at
        if self.title_bg_img is not None:
            self.screen.blit(pygame.transform.smoothscale(self.title_bg_img, (WIDTH, HEIGHT)), (0, 0))
        else:
            self.screen.fill((20, 10, 40))
        # バージョン表示(右下) — どのビルドか一目で分かるように
        vtag = self.tiny.render(GAME_VERSION, True, (255, 235, 250))
        vtag.set_alpha(180)
        self.screen.blit(vtag, vtag.get_rect(bottomright=(WIDTH - 12, HEIGHT - 10)))
        if elapsed < 1.0:
            fade = pygame.Surface((WIDTH, HEIGHT)); fade.fill((0, 0, 0)); fade.set_alpha(int(255 * (1.0 - elapsed)))
            self.screen.blit(fade, (0, 0))
        if elapsed > 2.0:
            blink = (math.sin((elapsed - 2.0) * 3.0) + 1) / 2
            prompt = font(36, True).render("Enter / Space でストーリー開始　Qでランキング", True, (255, 245, 255))
            prompt.set_alpha(int(80 + 175 * blink))
            self.screen.blit(prompt, prompt.get_rect(center=(WIDTH // 2, HEIGHT - 80)))
        if elapsed > 1.5:
            alpha = min(255, int((elapsed - 1.5) * 300))
            cr = self.tiny.render("位置GOMILK — DigiKey Make ONE Challenge 2026", True, (200, 195, 230))
            cr.set_alpha(alpha)
            self.screen.blit(cr, cr.get_rect(center=(WIDTH // 2, HEIGHT - scaled_px(26, min_px=22, max_px=36))))
            self.draw_bottom_right_credit("VOICEVOX:春日部つむぎ", bottom_margin=scaled_px(16, min_px=12, max_px=22), text_color=(215, 208, 240), fill_rgba=(18, 12, 38, min(210, max(120, alpha))))

    def draw_start(self) -> None:
        """スタート画面: ハナが作った豪華なスタート画像を全画面表示。"""
        now = time.monotonic(); elapsed = now - self.start_entered_at
        if self.start_screen_img is not None:
            self.screen.blit(pygame.transform.smoothscale(self.start_screen_img, (WIDTH, HEIGHT)), (0, 0))
        else:
            self.screen.fill((20, 10, 40))
            if self.start_logo_img is not None:
                lw = min(int(WIDTH * 0.55), 750); ratio = lw / self.start_logo_img.get_width()
                lh = int(self.start_logo_img.get_height() * ratio)
                self.screen.blit(pygame.transform.smoothscale(self.start_logo_img, (lw, lh)),
                    pygame.transform.smoothscale(self.start_logo_img, (lw, lh)).get_rect(center=(WIDTH // 2, HEIGHT // 2)))
        # フェードイン
        if elapsed < 0.8:
            fade = pygame.Surface((WIDTH, HEIGHT)); fade.fill((0, 0, 0))
            fade.set_alpha(int(255 * (1.0 - elapsed / 0.8)))
            self.screen.blit(fade, (0, 0))
        # 操作ヒント
        if elapsed > 1.0:
            blink = (math.sin((elapsed - 1.0) * 3.5) + 1) / 2
            self.draw_bottom_right_credit("VOICEVOX:春日部つむぎ", bottom_margin=scaled_px(16, min_px=12, max_px=22), text_color=(235, 225, 255), fill_rgba=(24, 16, 48, 185))
            hint = self.tiny.render("Enter / Space / クリック でストーリー開始　Qでランキング", True, (255, 240, 255))
            hint.set_alpha(int(120 + 135 * blink))
            self.screen.blit(hint, hint.get_rect(center=(WIDTH // 2, HEIGHT - scaled_px(24, min_px=20, max_px=34))))

    def draw_name_input(self) -> None:
        # 応募・誓約書サイン画面: 上に応募イラスト、下に誓約書と入力欄
        cx = WIDTH // 2
        self.screen.fill((24, 13, 42))
        margin = max(20, WIDTH // 48)
        art_w = WIDTH - margin * 2
        art_h = int(HEIGHT * 0.55)
        art_rect = pygame.Rect(margin, margin, art_w, art_h)
        art = self.chapter_image("story_audition.png", (art_w, art_h))
        if art is not None:
            self.draw_rounded_image(art, art_rect, radius=24)

        gap = max(10, HEIGHT // 60)
        panel = pygame.Rect(margin, art_rect.bottom + gap, WIDTH - margin * 2,
                            HEIGHT - art_rect.bottom - gap - margin)
        self.draw_panel(panel, alpha=226)
        hp = panel.height

        def clamp(v, lo, hi):
            return int(max(lo, min(hi, v)))

        lab = font(clamp(hp * 0.10, 20, 30), True).render("オーディションにエントリー！", True, (255, 220, 170))
        self.screen.blit(lab, lab.get_rect(center=(cx, panel.y + int(hp * 0.10))))
        # ストーリーと同じように、説明を1文字ずつ表示する。
        name_elapsed = max(0.0, time.monotonic() - self.name_entered_at)
        reveal_count = int(name_elapsed * STORY_CHARS_PER_SEC)
        head_full = "ランキングに載せる、公開用ニックネームを入力してね"
        pledge_full = "本名や個人情報は入力しないでね。ランキングはネットで公開されます。"
        head_text = head_full[:reveal_count]
        pledge_start = len(head_full) + STORY_LINE_PAUSE_CHARS
        pledge_text = pledge_full[:max(0, reveal_count - pledge_start)]
        head = render_fit(font(clamp(hp * 0.12, 20, 31), True),
                          head_text,
                          (255, 245, 255), panel.width - 50, 14, True)
        self.draw_text_shadow(head, head.get_rect(center=(cx, panel.y + int(hp * 0.20))))
        pledge = render_fit(font(clamp(hp * 0.095, 17, 26), True),
                            pledge_text,
                            (255, 230, 220), panel.width - 50, 12, True)
        self.screen.blit(pledge, pledge.get_rect(center=(cx, panel.y + int(hp * 0.31))))

        # 入力ボックス
        box_w = min(640, panel.width - 80)
        box_h = clamp(hp * 0.26, 56, 92)
        box = pygame.Rect(cx - box_w // 2, panel.y + int(hp * 0.45), box_w, box_h)
        self.name_input_rect = box.copy()
        try:
            pygame.key.set_text_input_rect(box)
        except Exception:
            pass
        pygame.draw.rect(self.screen, (255, 220, 255), box, border_radius=20)
        pygame.draw.rect(self.screen, (34, 25, 58), box.inflate(-8, -8), border_radius=16)
        editing = self.text_editing
        cursor_on = int(time.monotonic() * 2) % 2 == 0
        if not self.name_draft and not editing:
            ph = render_fit(font(clamp(box_h * 0.34, 18, 30), True), "公開用ニックネームを入力",
                            (170, 165, 200), box.width - 50, 16, True)
            self.screen.blit(ph, ph.get_rect(center=box.center))
            if cursor_on:
                pygame.draw.rect(self.screen, (255, 245, 255),
                                 (box.left + 30, box.centery - box_h // 4, 3, box_h // 2))
        else:
            shown = self.name_draft + editing + ("|" if cursor_on else " ")
            nm = render_fit(font(clamp(box_h * 0.42, 20, 36), True), shown, (255, 245, 255),
                            box.width - 50, 18, True)
            self.screen.blit(nm, nm.get_rect(center=box.center))

        hint = render_fit(self.tiny, "Enter=決定  /  Backspace=1文字けす  /  最大16文字",
                          (210, 220, 250), panel.width - 50, 12)
        self.screen.blit(hint, hint.get_rect(center=(cx, panel.bottom - int(hp * 0.16))))
        sub = render_fit(self.tiny, "Enterで名前とランキング公開に同意して進みます",
                         (185, 195, 235), panel.width - 50, 11)
        self.screen.blit(sub, sub.get_rect(center=(cx, panel.bottom - int(hp * 0.07))))

    def draw_select(self) -> None:
        self.draw_backstage_background(165)

        # ---- ヘッダー: ロゴ + 副題 + プレイヤー名 ----
        self.draw_panel(pygame.Rect(42, 28, WIDTH - 84, 156), alpha=135)
        logo = logo_image(260)
        # ロゴはパネル内に完全に収める
        logo_w = logo.get_width() if logo else 0
        logo_h = logo.get_height() if logo else 0
        if logo is not None:
            self.screen.blit(logo, (64, 28 + (156 - logo_h) // 2))
            sub_x = 64 + logo_w + 26
        else:
            t = self.big.render("位置GOMILK", True, (255, 245, 255))
            self.draw_text_shadow(t, (60, 48))
            sub_x = 60 + t.get_width() + 18
        rt_title = self.mid.render("リズムタンバリン", True, (255, 235, 250))
        self.screen.blit(rt_title, (sub_x, 56))
        # 副題はボタン領域とぶつからないよう幅制限
        sub_max_w = self.rank_button_rect.left - sub_x - 24
        sub = render_fit(self.small, "Qでランキング / 上下キー または 1〜3 で曲をえらんで、Enterでスタート",
                         (235, 230, 255), sub_max_w, 18)
        self.screen.blit(sub, (sub_x, 104))
        plabel = self.tiny.render("プレイヤー", True, (200, 195, 230))
        self.screen.blit(plabel, (sub_x, 138))
        pname = self.small.render(self.player_name if self.player_name else "(名前未設定)", True, (255, 230, 170))
        self.screen.blit(pname, (sub_x + plabel.get_width() + 10, 134))
        self.draw_button(self.rank_button_rect, "ランキングを見る")
        self.draw_button(self.name_button_rect, "名前を変更")

        # ---- 曲カード(画面サイズに応じて大きく) ----
        # 横3枚を画面幅いっぱいに広げる
        n = len(self.songs)
        margin = 60
        gap = 32
        cards_top = 220
        # 下段(ランキング/インフォ)の上端
        bottom_panel_top = HEIGHT - 170
        avail_w = WIDTH - margin * 2 - gap * (n - 1)
        card_w = avail_w // max(1, n)
        # カバーは16:9相当(横長ジャケに合う)
        cover_h = int(card_w * 9 / 16)
        card_h = cover_h + 130  # カバー + テキスト領域
        # 上下を画面に収めるよう card_h を調整
        max_card_h = bottom_panel_top - cards_top - 20
        if card_h > max_card_h:
            scale = max_card_h / card_h
            card_w = int(card_w * scale)
            cover_h = int(card_w * 9 / 16)
            card_h = cover_h + 130
            # 中央寄せ
            total_w = card_w * n + gap * (n - 1)
            margin = (WIDTH - total_w) // 2

        for i, song in enumerate(self.songs):
            x = margin + i * (card_w + gap)
            y = cards_top
            selected = i == self.selected
            rect = pygame.Rect(x - 8, y - 8, card_w + 16, card_h + 16)
            border_col = (255, 220, 255) if selected else (70, 60, 100)
            pygame.draw.rect(self.screen, border_col, rect, border_radius=24)
            pygame.draw.rect(self.screen, (34, 25, 58), rect.inflate(-8, -8), border_radius=20)
            # カバー
            cover = self.cover_for(song, (card_w, cover_h))
            self.screen.blit(cover, (x, y))
            # タイトル
            name = render_fit(self.mid, song.title, (255, 245, 255), card_w - 16, 22, True)
            self.screen.blit(name, (x + 8, y + cover_h + 14))
            minutes, seconds = int(song.duration) // 60, int(song.duration) % 60
            meta = render_fit(self.small, f"BPM {song.bpm:g} ・ {minutes}:{seconds:02d} ・ かんたん",
                              (190, 230, 255), card_w - 16, 14)
            self.screen.blit(meta, (x + 8, y + cover_h + 60))
            if selected:
                hint = self.small.render("Enterでスタート！", True, (255, 190, 220))
                self.screen.blit(hint, (x + 8, y + cover_h + 92))

        # ---- 下段: 説明(左) + 選択中の曲のランキング(右) ----
        bottom_y = bottom_panel_top + 10
        info1 = self.small.render("60秒審査で成功率80%以上なら、曲の自然な終わりまで続行！", True, (225, 220, 250))
        self.screen.blit(info1, (66, bottom_y))
        status = "キーボード練習モード" if self.keyboard else "タンバリン接続モード"
        if self.reader:
            status = self.reader.status
        msg = self.tiny.render(status, True, (180, 180, 210))
        self.screen.blit(msg, (66, HEIGHT - 40))
        f11hint = self.tiny.render("F11: フルスクリーン切り替え", True, (180, 180, 210))
        self.screen.blit(f11hint, (66, HEIGHT - 64))
        chosen_song = self.songs[self.selected] if self.songs else None
        self.draw_leaderboard(chosen_song, WIDTH - 560, bottom_y, 3)

    def draw_play_controls_guide(self) -> None:
        """プレイ中も忘れないよう、3種類の操作を上部に常時表示する。"""
        guide_w = min(WIDTH - 80, 940)
        guide_h = max(44, min(54, HEIGHT // 13))
        guide_x = (WIDTH - guide_w) // 2
        guide_y = max(76, min(84, HEIGHT // 9))
        panel = pygame.Surface((guide_w, guide_h), pygame.SRCALPHA)
        pygame.draw.rect(panel, (31, 18, 48, 224), panel.get_rect(), border_radius=18)
        pygame.draw.rect(panel, (255, 180, 220, 190), panel.get_rect(), 2, border_radius=18)

        items = (
            ("tap", "いちご：叩く"),
            ("roll", "ROLL：連打"),
            ("kime", "いちご牛乳：上にあげる"),
        )
        cell_w = guide_w / 3
        guide_font = font(max(18, min(24, guide_h // 2)), True)
        icon_size = max(28, guide_h - 14)
        for i, (kind, label) in enumerate(items):
            left = int(i * cell_w)
            right = int((i + 1) * cell_w)
            if i:
                pygame.draw.line(panel, (255, 200, 230, 100),
                                 (left, 9), (left, guide_h - 9), 2)
            icon_cx = left + 28
            if kind == "tap" and self.note_tap_img is not None:
                icon = pygame.transform.smoothscale(self.note_tap_img, (icon_size, icon_size))
                panel.blit(icon, icon.get_rect(center=(icon_cx, guide_h // 2)))
            elif kind == "kime" and self.note_kime_img is not None:
                icon = pygame.transform.smoothscale(self.note_kime_img, (icon_size, icon_size))
                panel.blit(icon, icon.get_rect(center=(icon_cx, guide_h // 2)))
            elif kind == "roll":
                roll_rect = pygame.Rect(0, 0, icon_size + 10, max(14, icon_size // 3))
                roll_rect.center = (icon_cx + 4, guide_h // 2)
                pygame.draw.rect(panel, (255, 213, 116), roll_rect,
                                 border_radius=roll_rect.height // 2)
                pygame.draw.rect(panel, (255, 245, 205), roll_rect, 2,
                                 border_radius=roll_rect.height // 2)
            text_left = left + 58
            available = max(80, right - text_left - 8)
            label_surf = render_fit(guide_font, label, (255, 248, 252),
                                    available, 14, True)
            panel.blit(label_surf, label_surf.get_rect(
                midleft=(text_left, guide_h // 2)))
        self.screen.blit(panel, (guide_x, guide_y))

    def draw_live_success_gauge(self) -> None:
        """60秒審査の目標80％に対する、現在の確定判定成功率を表示する。"""
        judged, success, rate = self.live_success_rate()
        gauge_w = min(WIDTH - 100, 520)
        gauge_h = scaled_px(20, min_px=16, max_px=28)
        gauge_x = (WIDTH - gauge_w) // 2
        gauge_y = HEIGHT - scaled_px(116, min_px=86, max_px=150)
        if rate is None:
            label_text = "現在の成功率：判定待ち　／　目標80％"
            shown_rate = 0.0
            color = (180, 150, 195)
        else:
            pct = int(round(rate * 100))
            label_text = f"現在の成功率 {pct}%　（成功 {success}/{judged}）　目標80％"
            shown_rate = max(0.0, min(1.0, rate))
            color = (115, 235, 175) if rate >= CLEAR_GOOD_RATE else (255, 155, 185)
        label = font(scaled_px(23, min_px=17, max_px=31), True).render(
            label_text, True, (255, 245, 252))
        self.draw_text_shadow(label, label.get_rect(
            midbottom=(WIDTH // 2, gauge_y - 7)))
        pygame.draw.rect(self.screen, (55, 42, 65),
                         (gauge_x, gauge_y, gauge_w, gauge_h), border_radius=gauge_h // 2)
        fill_w = int(gauge_w * shown_rate)
        if fill_w > 0:
            pygame.draw.rect(self.screen, color,
                             (gauge_x, gauge_y, fill_w, gauge_h), border_radius=gauge_h // 2)
        target_x = gauge_x + int(gauge_w * CLEAR_GOOD_RATE)
        pygame.draw.line(self.screen, (255, 235, 140),
                         (target_x, gauge_y - 5), (target_x, gauge_y + gauge_h + 5), 4)
        target = self.tiny.render("80%", True, (255, 235, 140))
        self.screen.blit(target, target.get_rect(midtop=(target_x, gauge_y + gauge_h + 5)))

    def draw_play(self) -> None:
        assert self.song is not None
        now_mono = time.monotonic()
        now = self.song_time()

        # ---- 背景: カバーをうっすら + ミルクチョコの暗幕 ----
        self.screen.fill((26, 17, 24))
        bg = self.cover_for(self.song, (WIDTH, HEIGHT))
        bg.set_alpha(70)
        self.screen.blit(bg, (0, 0))
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((22, 12, 20, 150))
        self.screen.blit(overlay, (0, 0))

        # ---- プレイ帯(基板風のダークな帯) ----
        band = pygame.Rect(0, LANE_Y - 96, WIDTH, 192)
        band_surf = pygame.Surface(band.size, pygame.SRCALPHA)
        band_surf.fill((14, 9, 14, 215))
        self.screen.blit(band_surf, band.topleft)
        pygame.draw.line(self.screen, (255, 140, 180), (0, band.top), (WIDTH, band.top), 2)
        pygame.draw.line(self.screen, (255, 140, 180), (0, band.bottom), (WIDTH, band.bottom), 2)

        # ---- WS2812B風LEDストリップ(帯の下端)。ヒットすると判定色の波が走る ----
        wave_dt = now_mono - self.led_wave_at
        wave_col = JUDGE_COLORS.get(self.judge_kind, (255, 150, 190))
        for x in range(40, WIDTH - 16, 32):
            base = (74, 56, 66)
            col = base
            r = 3
            if 0.0 <= wave_dt < 0.5:
                wave_x = wave_dt * 2600.0
                d = abs(x - HIT_X)
                if abs(d - wave_x) < 70:
                    k = 1.0 - abs(d - wave_x) / 70.0
                    col = tuple(min(255, int(base[i] + (wave_col[i] - base[i]) * k)) for i in range(3))
                    r = 3 + int(3 * k)
            pygame.draw.circle(self.screen, col, (x, LANE_Y + 76), r)

        # ---- 判定サークル: ビートに合わせて脈打つ ----
        beat = 60.0 / max(60.0, float(self.song.bpm))
        pulse = 0.5 + 0.5 * math.cos((max(0.0, now) % beat) / beat * math.tau)
        self.screen.blit(glow_surface(72, (110, 36, 62)), (HIT_X - 72, LANE_Y - 72),
                         special_flags=pygame.BLEND_ADD)
        pygame.draw.circle(self.screen, (255, 93, 143), (HIT_X, LANE_Y), 40 + int(3 * pulse), 5)
        pygame.draw.circle(self.screen, (255, 228, 238), (HIT_X, LANE_Y), 23, 2)

        # ヒット直後: 判定色のリングが広がる
        flash_dt = now_mono - self.hit_flash_at
        if 0.0 <= flash_dt < 0.22:
            k = 1.0 - flash_dt / 0.22
            ring_r = int(40 + 72 * (1.0 - k))
            pygame.draw.circle(self.screen, wave_col, (HIT_X, LANE_Y), ring_r, max(1, int(7 * k)))
            glow_col = tuple(int(c * 0.6 * k) for c in wave_col)
            self.screen.blit(glow_surface(84, glow_col), (HIT_X - 84, LANE_Y - 84),
                             special_flags=pygame.BLEND_ADD)

        # ---- アンビエントなキラキラ筋(常時、控えめ) ----
        for sp in self.ambient_sparks:
            dt_sp = now_mono - sp.get("last", now_mono)
            sp["last"] = now_mono
            sp["x"] -= sp["v"] * dt_sp
            if sp["x"] < -20:
                sp["x"] = WIDTH + random.uniform(0, 200)
                sp["y"] = LANE_Y + random.uniform(-80, 80)
                sp["v"] = random.uniform(80, 220)
            spark = pygame.Surface((6, 6), pygame.SRCALPHA)
            pygame.draw.circle(spark, (255, 220, 240, int(sp.get("a", 110))), (3, 3), 2)
            self.screen.blit(spark, (int(sp["x"]) - 3, int(sp["y"]) - 3))

        # ---- リボン光(50コンボ節目で発動): 上下に流れる二重のキラキラ筋 ----
        ribbon_dt = now_mono - self.ribbon_at
        if 0.0 <= ribbon_dt < 0.8:
            k = 1.0 - ribbon_dt / 0.8
            ribbon = pygame.Surface((WIDTH, 200), pygame.SRCALPHA)
            for i in range(0, WIDTH, 8):
                phase = math.sin(i * 0.025 + ribbon_dt * 12)
                y1 = 100 + phase * 60
                y2 = 100 - phase * 60
                a = int(220 * k)
                pygame.draw.circle(ribbon, (255, 200, 235, a), (i, int(y1)), 3)
                pygame.draw.circle(ribbon, (200, 220, 255, a), (i, int(y2)), 3)
            self.screen.blit(ribbon, (0, LANE_Y - 100))

        # 次のノーツの接近リング(縮んでいき、判定円に重なる瞬間がジャスト)
        nxt = next((n for n in self.notes
                    if not n["hit"] and not n["missed"] and float(n["time"]) >= now - 0.05), None)
        if nxt is not None:
            dt_next = float(nxt["time"]) - now
            if 0.0 <= dt_next < 0.8:
                rr = 42 + dt_next * 230.0
                pygame.draw.circle(self.screen, (255, 215, 232), (HIT_X, LANE_Y), int(rr), 2)

        # ---- ノーツ ----
        # ふだんはミルク色(クリーム+うすピンク)で安心感、MISSすると赤に変わる
        note_glow_cream = glow_surface(38, (170, 130, 90))
        note_glow_pink = glow_surface(34, (150, 80, 110))
        miss_glow = glow_surface(30, (160, 30, 30))
        roll_glow = glow_surface(40, (180, 130, 40))
        wob = math.sin(now_mono * 6.0)
        for n in self.notes:
            t = float(n["time"])
            kind = n.get("kind", "tap")

            # ---- ロールノーツ(連打ゾーン): 黄色いバー ----
            if kind == "roll":
                if n.get("completed"):
                    continue
                end_t = float(n.get("end_time", t))
                if end_t > self.play_duration:
                    break
                # 表示位置(画面外なら省略)
                x_start = HIT_X + (t - now) * NOTE_SPEED
                x_end = HIT_X + (end_t - now) * NOTE_SPEED
                if x_end < -40:
                    continue
                if x_start > SPAWN_X:
                    if t - now > 3.0:
                        break
                    continue
                xs = int(max(x_start, -10))
                xe = int(min(x_end, WIDTH + 10))
                bar_h = 36
                # グロー
                glow_w = xe - xs
                if glow_w > 0:
                    bar_glow = pygame.Surface((glow_w + 80, bar_h + 60), pygame.SRCALPHA)
                    pygame.draw.rect(bar_glow, (255, 200, 100, 80),
                                     bar_glow.get_rect().inflate(-20, -20),
                                     border_radius=bar_h)
                    self.screen.blit(bar_glow, (xs - 40, LANE_Y - bar_h//2 - 30),
                                     special_flags=pygame.BLEND_ADD)
                    # 本体バー
                    bar_rect = pygame.Rect(xs, LANE_Y - bar_h // 2, glow_w, bar_h)
                    # ゾーン内かどうかで色変える
                    in_zone = (t - 0.05 <= now <= end_t + 0.05)
                    base = (255, 220, 140) if in_zone else (255, 200, 100)
                    pygame.draw.rect(self.screen, base, bar_rect, border_radius=bar_h // 2)
                    pygame.draw.rect(self.screen, (255, 240, 200), bar_rect, 3, border_radius=bar_h // 2)
                    # 連打カウントを中央に
                    hits = int(n.get("hit_count", 0))
                    expected = int(n.get("expected_hits", 4))
                    if in_zone or hits > 0:
                        cnt_surf = font(30, True).render(
                            f"{hits}/{expected}", True, (90, 40, 0))
                        cx_bar = (xs + xe) // 2
                        self.screen.blit(cnt_surf, cnt_surf.get_rect(
                            center=(cx_bar, LANE_Y)))
                    # 連打中のキラキラ(ゾーン内のみ脈打つ)
                    if in_zone:
                        pulse = abs(math.sin(now_mono * 12))
                        for star_x in range(xs + 30, xe - 30, 80):
                            sr = 4 + int(pulse * 3)
                            pygame.draw.circle(self.screen, (255, 250, 200),
                                               (star_x, LANE_Y - bar_h // 2 - 8), sr)
                continue

            # ---- 通常ノーツ(タップ / キメ) ----
            if n["hit"]:
                continue
            if t > self.play_duration:
                break
            x = HIT_X + (t - now) * NOTE_SPEED
            if x < -40:
                continue
            if x > SPAWN_X:
                if t - now > 3.0:
                    break
                continue
            xi = int(x)
            note_kind = n.get("kind", "tap")
            is_kime = note_kind == "kime"
            if n["missed"]:
                # MISS後: 赤(警告色)で残骸を残す
                self.screen.blit(miss_glow, (xi - 30, LANE_Y - 30),
                                 special_flags=pygame.BLEND_ADD)
                pygame.draw.circle(self.screen, (220, 70, 80), (xi, LANE_Y), 16)
                pygame.draw.circle(self.screen, (160, 30, 40), (xi, LANE_Y), 16, 2)
                continue

            if is_kime:
                self.screen.blit(note_glow_cream, (xi - 42, LANE_Y - 42),
                                 special_flags=pygame.BLEND_ADD)
                self.screen.blit(note_glow_pink, (xi - 38, LANE_Y - 38),
                                 special_flags=pygame.BLEND_ADD)
                if self.note_kime_img is not None:
                    rect = self.note_kime_img.get_rect(center=(xi, LANE_Y))
                    self.screen.blit(self.note_kime_img, rect)
                else:
                    pygame.draw.circle(self.screen, (255, 242, 222), (xi, LANE_Y), 26)
                    pygame.draw.circle(self.screen, (255, 150, 160), (xi, LANE_Y), 18)
                lbl = self.tiny.render("上げ振り!", True, (255, 250, 230))
                lbl_rect = lbl.get_rect(midbottom=(xi, LANE_Y - self.note_kime_size // 2 - 4))
                self.screen.blit(lbl, lbl_rect)
            else:
                self.screen.blit(note_glow_cream, (xi - 38, LANE_Y - 38),
                                 special_flags=pygame.BLEND_ADD)
                self.screen.blit(note_glow_pink, (xi - 34, LANE_Y - 34),
                                 special_flags=pygame.BLEND_ADD)
                if self.note_tap_img is not None:
                    rect = self.note_tap_img.get_rect(center=(xi, LANE_Y))
                    self.screen.blit(self.note_tap_img, rect)
                else:
                    r_out = 22 + int(wob * 1.5)
                    pygame.draw.circle(self.screen, (255, 240, 215), (xi, LANE_Y), r_out)
                    pygame.draw.circle(self.screen, (255, 175, 200), (xi, LANE_Y), r_out - 6)
                    pygame.draw.circle(self.screen, (255, 250, 250), (xi - 7, LANE_Y - 7), 6)

        # ---- パーティクル(判定色のしぶき + PERFECTの星) ----
        for prt in self.particles:
            k = max(0.0, min(1.0, prt["life"] / prt["max"]))
            col = prt.get("col", (255, 230, 160))
            c = (int(col[0] * (0.35 + 0.65 * k)),
                 int(col[1] * (0.35 + 0.65 * k)),
                 int(col[2] * (0.35 + 0.65 * k)))
            if prt.get("star"):
                draw_star(self.screen, c, (prt["x"], prt["y"]),
                          prt["r"] * (0.5 + 0.5 * k), prt.get("rot", 0.0))
            elif prt.get("heart"):
                draw_heart(self.screen, c, (prt["x"], prt["y"]),
                           prt["r"] * (0.5 + 0.5 * k))
            else:
                pygame.draw.circle(self.screen, c, (int(prt["x"]), int(prt["y"])),
                                   max(1, int(prt["r"] * (0.4 + 0.6 * k))))

        # ---- 判定テキスト: 判定色 + ポップ(出た瞬間ちょっと大きい) ----
        if now_mono < self.judge_until:
            age = now_mono - self.judge_at
            scale = 1.0 + max(0.0, 0.45 - age * 3.4)
            jc = JUDGE_COLORS.get(self.judge_kind, (255, 255, 255))
            j = self.judge_font.render(self.judge_text, True, jc)
            if scale > 1.001:
                j = pygame.transform.rotozoom(j, 0, scale)
            self.draw_text_shadow(j, j.get_rect(center=(HIT_X + 40, LANE_Y - 150)))

        # ---- コンボ: ドットフォントでバウンス ----
        if self.combo >= 2:
            age = now_mono - self.combo_pop_at
            scale = 1.0 + max(0.0, 0.30 - age * 2.8)
            combo_surf = self.combo_font.render(str(self.combo), True, (255, 245, 250))
            if scale > 1.001:
                combo_surf = pygame.transform.rotozoom(combo_surf, 0, scale)
            self.draw_text_shadow(combo_surf, combo_surf.get_rect(center=(WIDTH // 2, 208)))
            lbl = self.small.render("COMBO", True, (255, 185, 212))
            self.screen.blit(lbl, lbl.get_rect(center=(WIDTH // 2, 152)))

        # ---- HUD ----
        self.screen.blit(render_fit(self.mid, self.song.title, (255, 245, 255), 620, 22, True), (50, 32))
        score_surf = self.score_font.render(f"{self.score:07d}", True, (255, 245, 250))
        self.draw_text_shadow(score_surf, score_surf.get_rect(topright=(WIDTH - 50, 30)))
        self.draw_play_controls_guide()
        self.draw_live_success_gauge()

        # ---- 進行バー ----
        prog = max(0.0, min(1.0, now / max(1.0, self.play_duration)))
        pygame.draw.rect(self.screen, (62, 48, 70), (50, HEIGHT - 55, WIDTH - 100, 12), border_radius=8)
        pygame.draw.rect(self.screen, (255, 140, 195), (50, HEIGHT - 55, int((WIDTH - 100) * prog), 12), border_radius=8)
        if self.last_raw:
            raw = self.last_raw
            text = f"センサー x={raw.get('x')} y={raw.get('y')} z={raw.get('z')} energy={raw.get('energy')}"
            self.screen.blit(self.tiny.render(text, True, (200, 220, 255)), (55, HEIGHT - 36))
        remaining = max(0, int(self.play_duration - now))
        next_gate = None
        if self.next_checkpoint_index < len(self.checkpoints):
            next_gate = self.checkpoints[self.next_checkpoint_index]
        if next_gate is not None:
            gate_text = f"60秒審査まで {max(0, int(next_gate - now)):02d}秒 / 成功率80%で続行"
        elif self.last_checkpoint_seconds is not None:
            gate_text = "最終区間！このまま完走しよう"
        else:
            gate_text = "60秒審査クリア！ 曲の終わりまで続行"
        off_ms = int(round(self.latency_offset * 1000))
        info = self.tiny.render(
            f"残り {remaining:02d}秒 / {gate_text} / 判定調整 {off_ms:+d}ms / Esc: 一時停止",
            True, (215, 210, 232))
        self.screen.blit(info, info.get_rect(bottomright=(WIDTH - 50, HEIGHT - 14)))

        # キメ(上げ振り)成功の薄いピンクフラッシュ
        kf = now_mono - self.kime_flash_at
        if 0.0 <= kf < 0.35:
            fl = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            fl.fill((255, 150, 210, int(70 * (1.0 - kf / 0.35))))
            self.screen.blit(fl, (0, 0))
        # 重力キャリブレーション(Gキー)確認
        rc = now_mono - self.kime_recalib_at
        if 0.0 <= rc < 1.2:
            msg = font(34, True).render("重力リセット！ いまの向きを基準に", True, (255, 230, 250))
            msg.set_alpha(int(255 * (1.0 - rc / 1.2)))
            self.screen.blit(msg, msg.get_rect(center=(WIDTH // 2, 90)))

        # ---- 60秒チェックポイント通過の派手な祝福 ----
        cp_elapsed = now_mono - getattr(self, "checkpoint_celebrated_at", -100.0)
        if 0.0 <= cp_elapsed < 3.0:
            k = 1.0 - cp_elapsed / 3.0  # 1→0のフェード
            # 全体に薄いキラキラオーバーレイ
            kira = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            kira.fill((255, 220, 240, int(40 * k)))
            self.screen.blit(kira, (0, 0))
            # 中央に「✨ クリア！ ✨」(バウンスイン)
            if cp_elapsed < 0.5:
                scale = 0.3 + cp_elapsed * 2.4
            else:
                scale = 1.5 + math.sin(cp_elapsed * 6) * 0.1
            gate_sec = getattr(self, "checkpoint_celebrated_gate", 60)
            rate = getattr(self, "checkpoint_celebrated_rate", 0.0)
            title_surf = font(80, True).render(
                f"{gate_sec}秒チェック クリア！", True, (255, 240, 180))
            title_surf = pygame.transform.rotozoom(title_surf, 0, scale / 1.5)
            title_surf.set_alpha(int(255 * min(1.0, k * 2)))
            self.draw_text_shadow(title_surf, title_surf.get_rect(
                center=(WIDTH // 2, HEIGHT // 2 - 40)))
            sub = font(40, True).render(
                f"成功率 {int(round(rate*100))}% 達成！（ROLL含む）", True, (255, 200, 230))
            sub.set_alpha(int(255 * min(1.0, k * 2)))
            self.screen.blit(sub, sub.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 40)))
            # 続行メッセージ
            if cp_elapsed > 1.0:
                cont = self.mid.render("このまま最後まで完走しよう！", True, (255, 230, 250))
                cont.set_alpha(int(255 * min(1.0, k * 2)))
                self.screen.blit(cont, cont.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 100)))

        # ---- カウントダウン演出 (3..2..1..START!) ----
        if not getattr(self, "music_started", True) and not self.pause_resume_pending:
            remaining_cd = self.started_at - now_mono
            ready_elapsed = now_mono - self.countdown_started_at
            in_ready = ready_elapsed < self.ready_calibration_duration
            # 全体を暗くする
            cd_overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            cd_overlay.fill((10, 5, 18, 180))
            self.screen.blit(cd_overlay, (0, 0))
            # 最初の3秒は構えと上下判定、その後に3・2・1・START!。
            if in_ready:
                text = "かまえて！"
            elif remaining_cd > 2.5:
                text = "3"
            elif remaining_cd > 1.5:
                text = "2"
            elif remaining_cd > 0.5:
                text = "1"
            elif remaining_cd > -0.5:
                text = "START!"
            else:
                text = ""
            if text:
                # 1秒のあいだに、各数字が1個のまま滑らかに大きくなる。
                if text == "かまえて！":
                    sc = 1.0 + 0.04 * math.sin(ready_elapsed * 5.0)
                    color = (255, 235, 150)
                    f_size = 112
                elif text == "START!":
                    sc = 1.0 + 0.07 * math.sin(max(0.0, 0.5 - remaining_cd) * 10.0)
                    color = (255, 200, 240)
                    f_size = 160
                else:
                    numeral_start = {"3": 3.5, "2": 2.5, "1": 1.5}[text]
                    progress = max(0.0, min(1.0, numeral_start - remaining_cd))
                    ease = progress * progress * (3.0 - 2.0 * progress)
                    sc = 0.68 + 0.52 * ease
                    color = (255, 240, 180)
                    f_size = 200

                surf = font(f_size, True).render(text, True, color)
                surf = pygame.transform.rotozoom(surf, 0, sc)
                # 多重描画せず、数字は常に1個だけ表示する。
                self.draw_text_shadow(
                    surf, surf.get_rect(center=(WIDTH // 2, HEIGHT // 2)))
            # 上に「曲名」を表示
            tname = font(40, True).render(self.song.title, True, (255, 245, 255))
            tname.set_alpha(220)
            self.screen.blit(tname, tname.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 180)))
            hint_text = ("胸の前でタンバリンを構えて、動かさないでね！"
                         if in_ready else "そのまま構えて、スタートを待ってね！")
            hint = self.mid.render(hint_text, True, (255, 235, 180))
            hint.set_alpha(220)
            self.screen.blit(hint, hint.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 180)))
            if in_ready:
                seconds_left = max(1, int(math.ceil(
                    self.ready_calibration_duration - ready_elapsed)))
                hint2_text = f"上下判定を準備しています… あと{seconds_left}秒"
            else:
                hint2_text = "上下判定の準備OK！"
            hint2 = self.small.render(hint2_text, True, (255, 205, 235))
            hint2.set_alpha(210)
            self.screen.blit(hint2, hint2.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 214)))

        # 一時停止からの復帰は、入力も音も3秒止めて安全に構え直す。
        if self.pause_resume_pending:
            resume_left = max(0.0, self.pause_resume_until - now_mono)
            overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            overlay.fill((10, 5, 18, 190))
            self.screen.blit(overlay, (0, 0))
            title = font(scaled_px(92, min_px=58, max_px=130), True).render(
                "かまえて！", True, (255, 235, 150))
            self.draw_text_shadow(title, title.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 55)))
            count = max(1, int(math.ceil(resume_left)))
            sub = font(scaled_px(38, min_px=27, max_px=54), True).render(
                f"{count}秒後に再開します", True, (255, 225, 245))
            self.screen.blit(sub, sub.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 55)))
            hint = self.small.render("胸の前でタンバリンを構えて、動かさないでね！",
                                     True, (235, 225, 250))
            self.screen.blit(hint, hint.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 115)))

    def draw_result(self) -> None:
        """結果発表: ドラムロール → ジャーン!でスコア表示 → 1位なら拍手とキラキラ."""
        now_mono = time.monotonic()
        elapsed = now_mono - self.result_started_at

        # 演出ステージの自動進行
        if self.result_stage == "drumroll" and elapsed >= 1.6:
            self.result_stage = "reveal"
            if not self.result_sfx_fired["jaan"] and self.fanfare_jaan_sfx:
                try: self.fanfare_jaan_sfx.play()
                except Exception: pass
                self.result_sfx_fired["jaan"] = True
        if self.result_stage == "reveal" and elapsed >= 2.4:
            self.result_stage = "celebrate"
            # 1位なら派手に祝う
            if self.last_rank == 1 and not self.result_sfx_fired["first"]:
                if self.fanfare_1st_sfx:
                    try: self.fanfare_1st_sfx.play()
                    except Exception: pass
                self.result_sfx_fired["first"] = True
            if self.last_rank and self.last_rank <= 3:
                if not self.result_sfx_fired["applause"] and self.applause_sfx:
                    try: self.applause_sfx.play()
                    except Exception: pass
                    self.result_sfx_fired["applause"] = True
            if self.clear_result and not self.result_sfx_fired["celebration"] and self.celebration_sfx:
                try: self.celebration_sfx.play()
                except Exception: pass
                self.result_sfx_fired["celebration"] = True

        # ---- 背景: 派手なキラキラ画像 ----
        if self.result_bg_image is not None:
            self.screen.blit(self.result_bg_image, (0, 0))
            # 暗くしすぎないよう薄い暗幕
            overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
            overlay.fill((20, 12, 30, 80))
            self.screen.blit(overlay, (0, 0))
        else:
            self.draw_backstage_background(150)

        # ====== ドラムロール段階 ======
        if self.result_stage == "drumroll":
            big = font(96, True)
            t = big.render("結果発表", True, (255, 245, 255))
            # 揺らす(緊張感)
            shake = math.sin(elapsed * 60) * 5 * min(1.0, elapsed)
            self.draw_text_shadow(t, t.get_rect(center=(WIDTH // 2 + int(shake), HEIGHT // 2 - 40)))
            # ドット増えていく
            dots = "." * (int(elapsed * 6) % 4)
            sub2 = self.mid.render(f"スコア集計中{dots}", True, (255, 230, 170))
            self.screen.blit(sub2, sub2.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 50)))
            # ドラムロールっぽい横線パルス(下に)
            pulse = abs(math.sin(elapsed * 22))
            line_w = int(220 + pulse * 80)
            line_y = HEIGHT // 2 + 110
            line_alpha = int(180 + pulse * 70)
            line_surf = pygame.Surface((line_w, 6), pygame.SRCALPHA)
            pygame.draw.rect(line_surf, (255, 200, 230, line_alpha), line_surf.get_rect(), border_radius=3)
            self.screen.blit(line_surf, line_surf.get_rect(center=(WIDTH // 2, line_y)))
            return

        # ====== reveal/celebrate 段階 共通 ======
        # パネルを画面サイズに応じて中央配置
        panel_w = min(820, WIDTH - 80)
        panel_h = min(680, HEIGHT - 120)
        panel_x = (WIDTH - panel_w) // 2
        panel_y = (HEIGHT - panel_h) // 2 - 20
        self.draw_panel(pygame.Rect(panel_x, panel_y, panel_w, panel_h), alpha=160)
        cx = WIDTH // 2

        # タイトル: 「ジャーン!」のタイミングでバウンド
        reveal_dt = elapsed - 1.6
        scale = 1.0
        if 0.0 <= reveal_dt < 0.5:
            scale = 1.0 + max(0.0, 0.5 - reveal_dt * 2) * 0.6
        title = font(76, True).render("結果発表", True, (255, 245, 255))
        if scale > 1.001:
            title = pygame.transform.rotozoom(title, 0, scale)
        self.draw_text_shadow(title, title.get_rect(center=(cx, panel_y + 60)))

        if self.song:
            shown_name = self.player_name if self.player_name else "(名無し)"
            song_line = render_fit(self.small, f"{self.song.title} ／ プレイヤー：{shown_name}",
                                   (235, 230, 255), panel_w - 80, 16)
            self.screen.blit(song_line, song_line.get_rect(center=(cx, panel_y + 114)))

        # ---- スコア ----
        label = self.small.render("スコア", True, (255, 230, 170))
        self.screen.blit(label, label.get_rect(center=(cx, panel_y + 152)))
        score_alpha = int(255 * min(1.0, reveal_dt / 0.4))
        score = font(66, True).render(f"{self.score:07d}", True, (255, 230, 170))
        score.set_alpha(score_alpha)
        self.draw_text_shadow(score, score.get_rect(center=(cx, panel_y + 202)))
        combo = self.mid.render(f"最大コンボ {self.max_combo}", True, (220, 240, 255))
        combo.set_alpha(score_alpha)
        self.screen.blit(combo, combo.get_rect(center=(cx, panel_y + 256)))

        # ---- 結果ステータス ----
        good_pct = int(round(self.good_rate * 100))
        if self.aborted:
            status_text = "とちゅうで終了"
        elif self.gate_failed and self.failed_gate_seconds is not None:
            status_text = f"{int(self.failed_gate_seconds)}秒審査でおしまい"
        elif self.clear_result:
            status_text = "クリア！おめでとう！"
        else:
            status_text = "完走!クリアまであと少し"
        ok = self.clear_result and not self.gate_failed
        status = render_fit(self.mid, f"{status_text}  成功率 {good_pct}%(目標80%・ROLL含む)",
                            (255, 220, 255) if ok else (255, 210, 170), panel_w - 60, 20, True)
        self.screen.blit(status, status.get_rect(center=(cx, panel_y + 302)))

        # ---- 判定内訳 ----
        seg_font = font(26, True)
        segs = [seg_font.render(f"{key} {self.judge_counts.get(key, 0)}", True, JUDGE_COLORS[key])
                for key in ("PERFECT", "GREAT", "GOOD", "ROLL", "BAD", "MISS")]
        total_w = sum(t.get_width() for t in segs) + 20 * (len(segs) - 1)
        x = cx - total_w // 2
        for t in segs:
            self.screen.blit(t, (x, panel_y + 336))
            x += t.get_width() + 20

        # ---- 実際の加点方式を短く明示 ----
        formula_text = "得点：PERFECT 1000／GREAT 700／GOOD 300／ROLLは振るほど加点／いちご牛乳成功＋300／コンボ加点"
        formula = render_fit(self.tiny, formula_text, (255, 225, 170), panel_w - 60, 12)
        self.screen.blit(formula, formula.get_rect(center=(cx, panel_y + 374)))

        # ---- 補足 ----
        if self.gate_failed and self.failed_gate_seconds is not None:
            hint_text = f"今回の判定対象 {self.total_notes_in_run} / 60秒時点の成功率が80%未満だったのでここまで"
        elif self.last_checkpoint_seconds is not None:
            hint_text = f"今回の判定対象 {self.total_notes_in_run} / 60秒審査通過:{int(round(self.last_checkpoint_rate * 100))}%"
        else:
            hint_text = f"今回の判定対象 {self.total_notes_in_run}"
        hint = render_fit(self.small, hint_text, (230, 225, 255), panel_w - 60, 14)
        self.screen.blit(hint, hint.get_rect(center=(cx, panel_y + 402)))
        if self.last_rank is not None:
            rank_color = (255, 220, 110) if self.last_rank == 1 else (255, 220, 255)
            rank_text = f"今回の記録:{self.last_rank}位"
            if self.last_rank == 1:
                rank_text += "  1位獲得!"
            rank = self.mid.render(rank_text, True, rank_color) if self.last_rank == 1 \
                   else self.small.render(rank_text, True, rank_color)
            self.screen.blit(rank, rank.get_rect(center=(cx, panel_y + 430)))

        # ---- ランキング(パネル内に収める) ----
        lb_x = cx - 250
        lb_y = panel_y + 458
        self.draw_leaderboard(self.song, lb_x, lb_y, 3)

        # ---- ボタン(パネル下端付近) ----
        btn_y = panel_y + panel_h - 60
        self.result_name_button_rect = pygame.Rect(cx - 340, btn_y, 210, 46)
        self.result_rank_button_rect = pygame.Rect(cx + 130, btn_y, 210, 46)
        self.draw_button(self.result_name_button_rect, "名前変更")
        self.draw_button(self.result_rank_button_rect, "ランキング一覧")
        if self.result_should_return_to_story():
            sec_left = max(0, int(math.ceil(self.result_auto_return_at - now_mono))) if self.result_auto_return_at else 0
            msg_text = f"Enter / クリック: ストーリーへ戻る（自動であと{sec_left}秒） / R: ランキング / N: 名前変更"
        else:
            msg_text = "Enter: 曲をえらぶ / R: ランキング / N: 名前変更"
        msg = render_fit(self.small, msg_text, (220, 210, 255), panel_w + 120, 16)
        self.screen.blit(msg, msg.get_rect(center=(cx, panel_y + panel_h + 22)))

        # ====== celebrate段階の追加演出: 紙吹雪 ======
        if self.result_stage == "celebrate":
            celebrate_dt = elapsed - 2.4
            # 紙吹雪: クリア状態によって量を調整
            if not hasattr(self, "confetti") or self.confetti is None:
                self.confetti = []
            if self.last_rank == 1:
                spawn_rate, max_count, duration = 0.8, 90, 6.0
            elif self.last_rank and self.last_rank <= 3:
                spawn_rate, max_count, duration = 0.4, 50, 4.0
            elif self.clear_result:
                spawn_rate, max_count, duration = 0.25, 30, 3.0
            else:
                spawn_rate, max_count, duration = 0.0, 0, 0.0
            if celebrate_dt < duration and random.random() < spawn_rate and len(self.confetti) < max_count:
                self.confetti.append({
                    "x": random.uniform(0, WIDTH),
                    "y": random.uniform(-50, -10),
                    "vx": random.uniform(-40, 40),
                    "vy": random.uniform(60, 160),
                    "kind": random.choice(["heart", "star", "circle"]),
                    "col": random.choice([(255, 200, 220), (255, 230, 170),
                                          (200, 220, 255), (230, 200, 255), (255, 170, 200)]),
                    "r": random.uniform(6, 12), "rot": random.uniform(0, math.tau),
                    "vrot": random.uniform(-3, 3), "born": now_mono,
                })
            # 描画と更新
            if hasattr(self, "confetti") and self.confetti:
                dt_frame = 1/60
                alive = []
                for c in self.confetti:
                    c["x"] += c["vx"] * dt_frame
                    c["y"] += c["vy"] * dt_frame
                    c["vy"] += 40 * dt_frame
                    c["rot"] += c["vrot"] * dt_frame
                    age = now_mono - c["born"]
                    if c["y"] < HEIGHT + 20 and age < 6:
                        if c["kind"] == "heart":
                            draw_heart(self.screen, c["col"], (c["x"], c["y"]), c["r"])
                        elif c["kind"] == "star":
                            draw_star(self.screen, c["col"], (c["x"], c["y"]), c["r"], c["rot"])
                        else:
                            pygame.draw.circle(self.screen, c["col"], (int(c["x"]), int(c["y"])), int(c["r"] * 0.6))
                        alive.append(c)
                self.confetti = alive

    def draw_overall_ranking(self) -> None:
        self.draw_backstage_background(160)
        outer = pygame.Rect(scaled_px(44, min_px=28, max_px=70), scaled_px(28, min_px=18, max_px=44),
                            WIDTH - scaled_px(88, min_px=56, max_px=140), HEIGHT - scaled_px(62, min_px=42, max_px=96))
        self.draw_panel(outer, alpha=190)
        cx = WIDTH // 2
        title = font(scaled_px(54, min_px=36, max_px=82), True).render("位置GO MILK 総合ランキング", True, (255, 240, 170))
        self.draw_text_shadow(title, title.get_rect(center=(cx, outer.y + scaled_px(52, min_px=36, max_px=76))))
        sub = self.small.render("3曲の合計得点で決まる展示ランキング", True, (245, 225, 255))
        self.screen.blit(sub, sub.get_rect(center=(cx, outer.y + scaled_px(96, min_px=70, max_px=132))))

        rows = sort_scores(list(self.leaderboard.get("overall", [])))[:10]
        list_top = outer.y + scaled_px(132, min_px=96, max_px=178)
        row_h = max(30, min(scaled_px(62, min_px=34, max_px=76), (outer.bottom - list_top - scaled_px(55, min_px=38, max_px=72)) // 10))
        list_w = min(int(WIDTH * 0.72), scaled_px(1080, min_px=650, max_px=1280))
        x = cx - list_w // 2
        if not rows:
            empty = self.mid.render("まだ記録がありません。最初の1位を目指そう！", True, (255, 225, 245))
            self.screen.blit(empty, empty.get_rect(center=(cx, HEIGHT // 2)))
        for i, row in enumerate(rows):
            rect = pygame.Rect(x, list_top + i * row_h, list_w, row_h - scaled_px(6, min_px=4, max_px=9))
            colors = [(255, 205, 70), (215, 225, 245), (225, 160, 105)]
            accent = colors[i] if i < 3 else (205, 170, 235)
            fill = (*accent, 58 if i < 3 else 34)
            layer = pygame.Surface(rect.size, pygame.SRCALPHA)
            pygame.draw.rect(layer, fill, layer.get_rect(), border_radius=18)
            pygame.draw.rect(layer, (*accent, 210), layer.get_rect(), 2, border_radius=18)
            self.screen.blit(layer, rect.topleft)
            rank = font(scaled_px(28, min_px=19, max_px=40), True).render(f"{i + 1}位", True, accent)
            self.screen.blit(rank, rank.get_rect(midleft=(rect.x + scaled_px(24, min_px=14, max_px=34), rect.centery)))
            name = str(row.get("name", "MILK"))[:MAX_PLAYER_NAME]
            name_s = render_fit(font(scaled_px(28, min_px=19, max_px=40), True), name, (255, 248, 255),
                                int(list_w * 0.48), 16, True)
            self.screen.blit(name_s, name_s.get_rect(midleft=(rect.x + int(list_w * 0.18), rect.centery)))
            score_s = font(scaled_px(28, min_px=19, max_px=40), True).render(f"{int(row.get('score', 0)):,}点", True, (255, 238, 180))
            self.screen.blit(score_s, score_s.get_rect(midright=(rect.right - scaled_px(24, min_px=14, max_px=34), rect.centery)))
        hint = self.small.render("Q / Enter / Space：3曲ランキング　　Esc：戻る", True, (235, 225, 255))
        self.screen.blit(hint, hint.get_rect(center=(cx, outer.bottom - scaled_px(24, min_px=16, max_px=34))))

    def draw_song_rankings(self) -> None:
        """3曲を横3列、ジャケット＋縦TOP5で大きく表示する。"""
        self.draw_backstage_background(165)
        margin = scaled_px(28, min_px=16, max_px=42)
        gap = scaled_px(18, min_px=10, max_px=28)
        header_h = scaled_px(92, min_px=64, max_px=126)
        footer_h = scaled_px(48, min_px=34, max_px=66)
        card_w = (WIDTH - margin * 2 - gap * 2) // 3
        card_y = header_h
        card_h = HEIGHT - header_h - footer_h
        title = font(scaled_px(48, min_px=32, max_px=72), True).render("3曲ランキング", True, (255, 240, 180))
        self.draw_text_shadow(title, title.get_rect(center=(WIDTH // 2, scaled_px(45, min_px=32, max_px=62))))
        medal_colors = [(255, 210, 75), (220, 230, 250), (230, 165, 110)]

        for idx, song in enumerate(self.songs[:3]):
            card = pygame.Rect(margin + idx * (card_w + gap), card_y, card_w, card_h)
            layer = pygame.Surface(card.size, pygame.SRCALPHA)
            pygame.draw.rect(layer, (45, 24, 72, 225), layer.get_rect(), border_radius=24)
            pygame.draw.rect(layer, (255, 190, 230, 190), layer.get_rect(), 3, border_radius=24)
            self.screen.blit(layer, card.topleft)

            cover_margin = scaled_px(12, min_px=8, max_px=18)
            cover_h = min(int(card_h * 0.37), int(card_w * 0.58))
            cover_rect = pygame.Rect(card.x + cover_margin, card.y + cover_margin,
                                     card.w - cover_margin * 2, cover_h)
            cover = self.cover_for(song, cover_rect.size)
            self.draw_rounded_image(cover, cover_rect, radius=18, border=(255, 210, 235))

            song_y = cover_rect.bottom + scaled_px(24, min_px=15, max_px=32)
            song_title = render_fit(font(scaled_px(28, min_px=18, max_px=38), True), song.title,
                                    (255, 238, 175), card.w - scaled_px(24, min_px=16, max_px=36), 16, True)
            self.screen.blit(song_title, song_title.get_rect(center=(card.centerx, song_y)))

            rows = self.top_scores_for(song, None, 5)
            rows_top = song_y + scaled_px(30, min_px=20, max_px=42)
            available = card.bottom - scaled_px(16, min_px=10, max_px=24) - rows_top
            row_h = max(34, available // 5)
            if not rows:
                empty = self.small.render("まだ記録なし", True, (230, 220, 245))
                self.screen.blit(empty, empty.get_rect(center=(card.centerx, rows_top + row_h)))
            for i, row in enumerate(rows):
                rr = pygame.Rect(card.x + cover_margin, rows_top + i * row_h,
                                 card.w - cover_margin * 2, row_h - scaled_px(6, min_px=4, max_px=8))
                accent = medal_colors[i] if i < 3 else (210, 185, 235)
                row_layer = pygame.Surface(rr.size, pygame.SRCALPHA)
                pygame.draw.rect(row_layer, (*accent, 42), row_layer.get_rect(), border_radius=14)
                self.screen.blit(row_layer, rr.topleft)
                rank = font(scaled_px(23, min_px=16, max_px=32), True).render(f"{i + 1}位", True, accent)
                self.screen.blit(rank, rank.get_rect(midleft=(rr.x + scaled_px(10, min_px=6, max_px=15), rr.centery)))
                name = str(row.get("name", "MILK"))[:MAX_PLAYER_NAME]
                name_s = render_fit(font(scaled_px(23, min_px=16, max_px=32), True), name, (255, 248, 255),
                                    int(rr.w * 0.43), 14, True)
                self.screen.blit(name_s, name_s.get_rect(midleft=(rr.x + int(rr.w * 0.20), rr.centery)))
                score_s = font(scaled_px(21, min_px=15, max_px=30), True).render(f"{int(row.get('score', 0)):,}", True, (255, 235, 185))
                self.screen.blit(score_s, score_s.get_rect(midright=(rr.right - scaled_px(10, min_px=6, max_px=15), rr.centery)))
        hint = self.small.render("Q / Enter / Space：戻る　　Esc：すぐ戻る", True, (235, 225, 255))
        self.screen.blit(hint, hint.get_rect(center=(WIDTH // 2, HEIGHT - scaled_px(22, min_px=15, max_px=30))))

    def draw_ranking_screen(self) -> None:
        if self.ranking_page == "songs":
            self.draw_song_rankings()
        else:
            self.draw_overall_ranking()

    def handle_event(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.QUIT:
            if self.state == "play":
                self.finish_song(aborted=True)
            return False
        if (event.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN)
                and time.monotonic() < self.input_locked_until):
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.state == "pause":
                if self.pause_exit_confirm:
                    return True
                for i, rect in enumerate(self.pause_option_rects):
                    if rect.collidepoint(event.pos):
                        self.pause_selected = i
                        if i == 0:
                            self.resume_from_pause()
                        else:
                            self.pause_exit_confirm = True
                        return True
                return True
            if self.state == "ranking":
                self.cycle_ranking_screen()
                return True
            if self.state in ("title", "start"):
                if self.state == "title":
                    self.state = "start"; self.start_entered_at = time.monotonic()
                    if self.title_voice_sfx:
                        try: self.title_voice_sfx.stop()
                        except Exception: pass
                    if self.start_voice_sfx:
                        try: self.start_voice_sfx.play()
                        except Exception: pass
                elif time.monotonic() - self.start_entered_at > 1.5:
                    self.name_draft = ""; self.text_editing = ""
                    self.start_story()
            elif self.state == "board":
                if self.board_phase == "ready":
                    self.board_roll()
                elif self.board_phase == "fortune":
                    self.board_fortune_confirm()
                return True
            elif self.state == "chapter":
                self.trigger_chapter_action()
                return True
            elif self.state == "ending":
                self.enter_final_score()
                return True
            elif self.state == "gameover":
                if time.monotonic() - self.gameover_entered_at >= GAMEOVER_BUTTON_REVEAL_SEC:
                    self.enter_final_score()
                return True
            elif self.state == "practice":
                self.register_practice_hit(900, up_gesture=self.practice_step >= 2)
                return True
            elif self.state == "final_score":
                if time.monotonic() - self.final_score_entered_at >= 4.0:
                    self.return_to_title()
                return True
            elif self.state == "select" and self.rank_button_rect.collidepoint(event.pos):
                self.open_ranking_screen("select")
                return True
            if self.state == "select" and self.name_button_rect.collidepoint(event.pos):
                self.enter_name_screen(clear=True)
                return True
            if self.state == "result" and self.result_rank_button_rect.collidepoint(event.pos):
                self.open_ranking_screen("select")
                return True
            if self.state == "result" and self.result_name_button_rect.collidepoint(event.pos):
                self.enter_name_screen(clear=True)
                return True
            if self.state == "result":
                self.continue_after_result()
                return True
        if self.state == "name" and event.type == pygame.TEXTINPUT:
            incoming = filter_name_input(event.text)
            now = time.monotonic()
            # Mac の IME で、Enter 時に TEXTINPUT が来ず TEXTEDITING だけ残る環境がある。
            # そのため Enter 側で手動確定した直後に同じ TEXTINPUT が遅れて来たら二重入力を防ぐ。
            if not (incoming and incoming == self.last_manual_ime_commit_text and
                    now - self.last_manual_ime_commit_at < 0.25):
                self.append_name_text(incoming)
            self.text_editing = ""
            self.last_textinput_at = now
            return True
        if self.state == "name" and event.type == pygame.TEXTEDITING:
            self.text_editing = filter_name_input(event.text or "")
            return True
        # ---- 共通: F11でフルスクリーン切り替え ----
        if event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
            self.toggle_fullscreen()
            return True
        # ---- 共通: Qキーで総合／3曲ランキングを切り替え ----
        # 名前入力中だけは、プレイヤー名に q/Q を入れられるように除外する。
        if event.type == pygame.KEYDOWN and event.key == pygame.K_q and self.state != "name":
            if self.state == "ranking":
                self.cycle_ranking_screen()
            elif self.state in ("title", "start", "select", "chapter", "board"):
                self.open_ranking_screen()
            return True
        # ---- ウィンドウリサイズ ----
        if event.type == pygame.VIDEORESIZE:
            new_w, new_h = max(800, event.w), max(450, event.h)
            _set_screen_size(new_w, new_h)
            self.screen = pygame.display.set_mode((new_w, new_h), pygame.RESIZABLE)
            self._on_screen_resize()
            return True
        if event.type == pygame.KEYDOWN:
            if self.state == "pause":
                if self.pause_exit_confirm:
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        self.exit_from_pause()
                    elif event.key == pygame.K_ESCAPE:
                        self.pause_exit_confirm = False
                        self.pause_selected = 1
                    return True
                if event.key == pygame.K_ESCAPE:
                    self.resume_from_pause()
                elif event.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_w):
                    self.pause_selected = 1 - self.pause_selected
                elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                    if self.pause_selected == 0:
                        self.resume_from_pause()
                    else:
                        self.pause_exit_confirm = True
                elif event.key == pygame.K_g:
                    self.recalibrate_gravity()
                elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    self.latency_offset = max(-0.3, round(self.latency_offset - 0.005, 3))
                    self.profile["latency_offset"] = self.latency_offset
                    save_json_file(PROFILE_PATH, self.profile)
                elif event.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS, pygame.K_SEMICOLON):
                    self.latency_offset = min(0.3, round(self.latency_offset + 0.005, 3))
                    self.profile["latency_offset"] = self.latency_offset
                    save_json_file(PROFILE_PATH, self.profile)
                return True
            if self.state == "title":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                    self.state = "start"; self.start_entered_at = time.monotonic()
                    if self.title_voice_sfx:
                        try: self.title_voice_sfx.stop()
                        except Exception: pass
                    if self.start_voice_sfx:
                        try: self.start_voice_sfx.play()
                        except Exception: pass
                return True
            if self.state == "start":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                    if time.monotonic() - self.start_entered_at > 1.5:
                        self.name_draft = ""; self.text_editing = ""
                        self.start_story()
                return True
            # ---- 旧ボード画面(ストーリー版では通常使わない) ----
            if self.state == "board":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                    if self.board_phase == "ready":
                        self.board_roll()
                    elif self.board_phase == "fortune":
                        self.board_fortune_confirm()
                elif event.key == pygame.K_ESCAPE:
                    self.state = "select"
                elif event.key == pygame.K_r:
                    self.restart_from_beginning()
                return True
            # ---- ストーリーチャプターイベント画面 ----
            if self.state == "chapter":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                    self.trigger_chapter_action()
                elif event.key == pygame.K_ESCAPE:
                    self.enter_final_score()
                return True
            if self.state == "practice":
                if event.key == pygame.K_ESCAPE:
                    self.enter_final_score()
                elif event.key in (pygame.K_UP, pygame.K_w):
                    self.register_practice_hit(900, up_gesture=True)
                elif event.key in (pygame.K_SPACE, pygame.K_RETURN, pygame.K_KP_ENTER):
                    self.register_practice_hit(700, up_gesture=False)
                return True
            # ---- エンディング / 解散演出 ----
            if self.state == "ending":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE, pygame.K_r):
                    self.enter_final_score()
                elif event.key == pygame.K_ESCAPE:
                    self.enter_final_score()
                return True
            if self.state == "gameover":
                elapsed = time.monotonic() - self.gameover_entered_at
                if (event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE, pygame.K_r)
                        and elapsed >= GAMEOVER_BUTTON_REVEAL_SEC):
                    self.enter_final_score()
                elif event.key == pygame.K_ESCAPE:
                    # 展示スタッフ用の緊急スキップだけは常に残す。
                    self.enter_final_score()
                return True
            if self.state == "final_score":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE, pygame.K_ESCAPE):
                    if time.monotonic() - self.final_score_entered_at >= 4.0:
                        self.return_to_title()
                return True
            if self.state == "name":
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    # IME(かな漢字変換)で未確定の文字がある場合、最初のEnterは
                    # 変換の確定だけに使う。次のEnterで初めて決定する。
                    if self.text_editing:
                        committed = self.text_editing
                        self.append_name_text(committed)
                        self.text_editing = ""
                        self.last_manual_ime_commit_text = committed
                        self.last_manual_ime_commit_at = time.monotonic()
                        self.last_textinput_at = self.last_manual_ime_commit_at
                        return True
                    # IME確定直後(50ms以内)のEnterは同じキー入力が2重に発火
                    # していることがあるので無視する(Macで頻発する挙動)。
                    if time.monotonic() - getattr(self, "last_textinput_at", -10.0) < 0.05:
                        return True
                    # フェイルセーフ: 名前が空のままEnterは無視
                    if not self.name_draft:
                        return True
                    self.player_name = clean_player_name(self.name_draft)
                    self.public_name_confirmed = True
                    self.name_draft = self.player_name
                    self.profile["player_name"] = self.player_name
                    save_json_file(PROFILE_PATH, self.profile)
                    self.text_editing = ""
                    # 名前確定後: 章立てストーリーなら指定章へ、それ以外は選曲へ
                    if self.chapter_return_to == "board":
                        self.chapter_return_to = None
                        self.return_to_board()
                    elif isinstance(self.chapter_return_to, int) and get_chapter(self.chapter_return_to) is not None:
                        return_to = self.chapter_return_to
                        self.chapter_return_to = None
                        self.enter_chapter(return_to)
                    else:
                        self.chapter_return_to = None
                        self.state = "select"
                elif event.key == pygame.K_BACKSPACE:
                    if self.text_editing:
                        self.text_editing = self.text_editing[:-1]
                    else:
                        self.name_draft = self.name_draft[:-1]
                elif event.key == pygame.K_ESCAPE:
                    self.player_name = clean_player_name(self.name_draft)
                    self.text_editing = ""
                    self.state = "select"
                else:
                    # v40: 英数字が二重入力される原因だった KEYDOWN unicode fallback は使わない。
                    # 文字入力は pygame.TEXTINPUT に一本化し、日本語IMEの未確定文字だけ TEXTEDITING/Enter で扱う。
                    pass
            elif self.state == "select":
                if event.key == pygame.K_n:
                    self.name_draft = self.player_name
                    self.enter_name_screen(clear=False)
                elif event.key in (pygame.K_DOWN, pygame.K_RIGHT):
                    self.selected = (self.selected + 1) % len(self.songs)
                elif event.key in (pygame.K_UP, pygame.K_LEFT):
                    self.selected = (self.selected - 1) % len(self.songs)
                elif event.key in (pygame.K_1, pygame.K_KP1):
                    self.selected = 0
                elif event.key in (pygame.K_2, pygame.K_KP2) and len(self.songs) > 1:
                    self.selected = 1
                elif event.key in (pygame.K_3, pygame.K_KP3) and len(self.songs) > 2:
                    self.selected = 2
                elif event.key == pygame.K_s:
                    self.play_tambourine_sfx(900)
                elif event.key in (pygame.K_RETURN, pygame.K_SPACE):
                    self.chapter_return_to = None
                    self.start_song(self.selected)
                elif event.key == pygame.K_r:
                    self.open_ranking_screen("select")
            elif self.state == "play":
                if event.key == pygame.K_ESCAPE:
                    self.enter_pause()
                elif event.key == pygame.K_SPACE:
                    self.register_hit(700)                       # いちご(横振り)
                elif event.key in (pygame.K_UP, pygame.K_w):
                    self.register_hit(900, up_gesture=True)      # いちご牛乳(上げ振り)
            elif self.state == "result":
                if event.key == pygame.K_n:
                    self.name_draft = self.player_name
                    self.enter_name_screen(clear=False)
                elif event.key == pygame.K_r:
                    self.state = "ranking"
                elif event.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_ESCAPE):
                    self.continue_after_result()
            elif self.state == "ranking":
                if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                    self.cycle_ranking_screen()
                elif event.key in (pygame.K_r, pygame.K_ESCAPE):
                    self.close_ranking_screen()
        return True

    def run(self) -> None:
        running = True
        while running:
            dt = self.clock.tick(60) / 1000.0
            self.sync_text_input_mode()
            for event in pygame.event.get():
                running = self.handle_event(event)
            self.sync_text_input_mode()
            self.consume_serial()
            self.update_bgm()
            if self.state == "board":
                self.update_board()
            if self.state == "play":
                self.update_countdown()
                self.update_misses()
            if self.state == "practice":
                self.update_practice()
            if self.state == "time_up":
                self.update_time_up()
            if self.state == "gameover":
                self.update_gameover()
            self.update_particles(dt)
            if self.state == "result":
                self.update_result_auto_return()
            if self.state == "title":
                self.draw_title()
            elif self.state == "start":
                self.draw_start()
            elif self.state == "name":
                self.draw_name_input()
            elif self.state == "practice":
                self.draw_practice_flow()
            elif self.state == "board":
                self.draw_board()
            elif self.state == "chapter":
                self.draw_chapter_event()
            elif self.state == "ending":
                self.draw_ending()
            elif self.state == "gameover":
                self.draw_gameover()
            elif self.state == "final_score":
                self.draw_final_score()
            elif self.state == "select":
                self.draw_select()
            elif self.state == "play":
                self.draw_play()
            elif self.state == "pause":
                self.draw_pause()
            elif self.state == "time_up":
                self.draw_time_up()
            elif self.state == "ranking":
                self.draw_ranking_screen()
            else:
                self.draw_result()
            pygame.display.flip()
        if self.reader:
            self.reader.stop()
        if self.ranking_sync:
            self.ranking_sync.close()
        pygame.quit()


def print_ports() -> None:
    if list_ports is None:
        print("pyserial が未インストールなのでポート一覧を出せません。")
        return
    print("Serial ports:")
    for p in list_ports.comports():
        print(f"  {p.device}\t{p.description}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="FRDM-MCXC444 のシリアルポート。例: /dev/tty.usbmodemXXXX")
    parser.add_argument("--keyboard", action="store_true", help="タンバリンなしで Space キーだけで遊ぶ")
    parser.add_argument("--list-ports", action="store_true", help="シリアルポート候補を表示")
    parser.add_argument("--no-video", action="store_true", help="MV背景を使わない。古いPCや重い時の安全モード")
    parser.add_argument("--no-sfx", action="store_true", help="タンバリン効果音を鳴らさない")
    args = parser.parse_args()
    if args.list_ports:
        print_ports()
        return
    Game(port=args.port, keyboard=args.keyboard or not args.port, video=not args.no_video, sfx=not args.no_sfx).run()


if __name__ == "__main__":
    main()
