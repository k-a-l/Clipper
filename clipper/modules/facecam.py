"""Facecam picker support: sample frames from a source and find the streamer's webcam box.

Detection (OpenCV, Apache-2.0, https://github.com/opencv/opencv-python):

1. **Faces that stay put.** The YuNet face detector (OpenCV Zoo, MIT; Haar cascades as a fallback)
   runs on several frames
   spread over time. A webcam face sits in roughly the same place in most frames, while faces in
   gameplay or cut-scenes move or vanish, so detections are clustered and the cluster seen in the
   most frames wins.
2. **The overlay's border.** Webcam overlays are rectangles whose edges stay fixed while the game
   behind them changes. Per-pixel edge strength is taken as a low percentile over the frames, which
   keeps only *persistent* edges; the strongest straight line on each side of the face (near where a
   typical webcam box would end) becomes that side of the box.
3. **Fallback shape.** Sides with no clear line use a typical webcam framing around the face, and
   anything within 3 % of the frame edge snaps to it.

Everything works in normalised (0–1) coordinates of the 16:9 source frame, the same as
``config.Facecam``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from clipper.config import Settings
from clipper.modules import yt_auth
from clipper.modules.utils import run_cmd

log = logging.getLogger(__name__)

WORK_W = 640  # detection resolution
FRAME_W = 1280  # picker background resolution
SNAP = 0.03


@dataclass
class CamGuess:
    x: float
    y: float
    w: float
    h: float
    confidence: float  # 0..1
    method: str  # "border" (overlay edges found) | "face" (framing around the face)
    face: tuple[float, float, float, float]  # normalised face box
    frames_with_face: int
    frames: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["face"] = dict(zip("xywh", self.face))
        return d


# ------------------------------------------------------------------ frames
async def frames_from_url(s: Settings, url: str, out_dir: Path, n: int = 6) -> list[Path]:
    """Sample ``n`` frames from a web video/live stream without downloading it (yt-dlp → ffmpeg)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--js-runtimes", s.js_runtime, *yt_auth.ytdlp_args(s),
           "-f", "b[height<=720][vcodec!=none]/bv*[height<=720]/b", "-O", "%(.{url,duration,is_live})j", url]
    res = await run_cmd(cmd, timeout=90)
    info = json.loads(res.stdout.strip().splitlines()[-1])
    media, duration, live = info.get("url"), float(info.get("duration") or 0), bool(info.get("is_live"))
    if not media:
        raise RuntimeError("couldn't open that video")
    vf = f"scale={FRAME_W}:-2"
    if live or duration <= 0:  # live: read ~12 s and keep evenly spaced frames
        await run_cmd([s.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-i", media, "-t", "12",
                       "-vf", f"fps={n / 12:.3f},{vf}", "-frames:v", str(n), "-q:v", "3",
                       str(out_dir / "f_%02d.jpg")], timeout=90)
        return sorted(out_dir.glob("f_*.jpg"))

    # recorded video: seek to n points across the middle 80 % (in parallel, input-side seek is fast)
    times = [duration * (0.1 + 0.8 * (i + 0.5) / n) for i in range(n)]
    sem = asyncio.Semaphore(3)

    async def one(i: int, t: float) -> None:
        async with sem:
            await run_cmd([s.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                           "-i", media, "-frames:v", "1", "-vf", vf, "-q:v", "3",
                           str(out_dir / f"f_{i:02d}.jpg")], timeout=60, check=False)

    await asyncio.gather(*(one(i, t) for i, t in enumerate(times)))
    return sorted(out_dir.glob("f_*.jpg"))


# --------------------------------------------------------------- detection
YUNET = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"


class _FaceFinder:
    """YuNet (OpenCV Zoo, MIT) when its model file is present; Haar cascades otherwise."""

    def __init__(self) -> None:
        import cv2

        self.yunet = None
        self.cascades: list[tuple[Any, bool]] = []
        if YUNET.is_file() and hasattr(cv2, "FaceDetectorYN"):
            self.yunet = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.6, 0.3, 50)
        else:
            base = Path(getattr(cv2, "data").haarcascades)
            front = cv2.CascadeClassifier(str(base / "haarcascade_frontalface_alt2.xml"))
            prof = cv2.CascadeClassifier(str(base / "haarcascade_profileface.xml"))
            if front.empty() or prof.empty():
                raise RuntimeError("no face model available (models/face_detection_yunet_2023mar.onnx missing)")
            self.cascades = [(front, False), (prof, False), (prof, True)]

    def __call__(self, bgr: Any) -> list[tuple[float, float, float, float]]:
        import cv2

        h, w = bgr.shape[:2]
        min_side = max(14, w // 50)
        found: list[tuple[float, float, float, float]] = []
        if self.yunet is not None:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(bgr)
            for f in faces if faces is not None else []:
                x, y, fw, fh = (float(v) for v in f[:4])
                if min(fw, fh) >= min_side and fw < w / 3:
                    found.append((max(0.0, x) / w, max(0.0, y) / h, fw / w, fh / h))
            return found
        eq = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        for casc, flip in self.cascades:
            img = cv2.flip(eq, 1) if flip else eq
            for x, y, fw, fh in casc.detectMultiScale(img, scaleFactor=1.1, minNeighbors=5,
                                                       minSize=(min_side, min_side), maxSize=(w // 3, h // 2)):
                fx = w - x - fw if flip else x
                found.append((fx / w, y / h, fw / w, fh / h))
        return found


def _cluster(per_frame: list[list[tuple[float, float, float, float]]]) -> tuple[list[tuple[float, ...]], int]:
    """Group detections that sit in the same place across frames; return the best group + frame count."""
    groups: list[dict[str, Any]] = []
    for fi, faces in enumerate(per_frame):
        for f in faces:
            cx, cy, size = f[0] + f[2] / 2, f[1] + f[3] / 2, max(f[2], f[3])
            for g in groups:
                gx, gy, gs = g["c"]
                if math.hypot(cx - gx, (cy - gy) * 9 / 16) < 0.6 * max(size, gs) and 0.5 < size / gs < 2:
                    g["boxes"].append(f)
                    g["frames"].add(fi)
                    n = len(g["boxes"])
                    g["c"] = ((gx * (n - 1) + cx) / n, (gy * (n - 1) + cy) / n, (gs * (n - 1) + size) / n)
                    break
            else:
                groups.append({"c": (cx, cy, size), "boxes": [f], "frames": {fi}})
    if not groups:
        return [], 0
    n = max(1, len(per_frame))

    def rank(g: dict[str, Any]) -> float:
        # seen in most frames first; then prefer the frame's sides/corners, where webcam overlays live
        # (a face in a video the streamer is watching tends to sit mid-screen)
        cx, cy, size = g["c"]
        side = 1 - 2 * min(cx, 1 - cx)
        updown = 1 - 2 * min(cy, 1 - cy)
        return len(g["frames"]) / n + 0.35 * side + 0.15 * updown + min(size, 0.2) * 0.25

    best = max(groups, key=rank)
    return best["boxes"], len(best["frames"])


def _median_box(boxes: list[tuple[float, ...]]) -> tuple[float, float, float, float]:
    cols = list(zip(*boxes))
    med = [sorted(c)[len(c) // 2] for c in cols]
    return med[0], med[1], med[2], med[3]


def _pick_line(edge: Any, mot_in: Any, mot_out: Any, positions: Any, target: float, window: float,
               floor: float) -> float | None:
    """Best overlay border among persistent lines.

    A real border has a strong edge that never moves *and* busier pixels on its outer side
    (gameplay) than on its inner side (the webcam room); lines inside the webcam fail the second test.
    """
    import numpy as np

    if edge.size == 0:
        return None
    base = float(np.median(edge)) + 1e-6
    ok = (edge >= floor) & (edge >= 3.0 * base)
    if not ok.any():
        return None
    near = 1 - np.clip(np.abs(positions - target) / max(window, 1e-6), 0, 1) * 0.6
    ratio = np.clip((mot_out + 1.0) / (mot_in + 1.0), 0.4, 4.0)
    score = np.where(ok, edge / float(edge.max()) * near * ratio, -1.0)
    i = int(np.argmax(score))
    return float(positions[i]) if score[i] > 0.35 else None


def detect(frames: list[Any]) -> CamGuess | None:
    """Find the webcam box in BGR frames (numpy arrays) of one source."""
    import cv2
    import numpy as np

    if not frames:
        return None
    small = []
    for f in frames:
        h, w = f.shape[:2]
        f = cv2.cvtColor(f, cv2.COLOR_GRAY2BGR) if f.ndim == 2 else f
        small.append(cv2.resize(f, (WORK_W, round(WORK_W * h / w)), interpolation=cv2.INTER_AREA))
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in small]
    finder = _FaceFinder()
    boxes, seen = _cluster([finder(f) for f in small])
    n = len(grays)
    if not boxes or seen < max(1, math.ceil(0.34 * n)):
        return None
    fx, fy, fw, fh = _median_box(boxes)
    H, W = grays[0].shape

    # typical framing around the face (a webcam usually shows head + shoulders, face ~30 % wide)
    cw = min(0.5, fw * 3.4)
    ch = min(0.6, cw * 16 / 9 / 1.4)  # ~4:3..16:9 box in pixels
    ex0 = fx + fw / 2 - cw / 2
    ey0 = fy + fh / 2 - ch * 0.42
    est = [ex0, ey0, ex0 + cw, ey0 + ch]  # left, top, right, bottom

    # persistent edges: low percentile over time keeps overlay borders, drops moving gameplay
    stack = np.stack([g.astype(np.float32) for g in grays])
    gx = np.stack([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in stack])
    gy = np.stack([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in stack])
    q = 25 if n >= 4 else 0
    sx, sy = np.percentile(gx, q, axis=0), np.percentile(gy, q, axis=0)
    motion = np.stack([cv2.GaussianBlur(g, (5, 5), 0) for g in stack]).std(axis=0)
    floor, band = 18.0, 6

    def px(v: float, size: int) -> int:
        return int(max(0, min(size, round(v * size))))

    def strip(m: Any, vertical: bool, lo: int, hi: int, span: tuple[int, int]) -> Any:
        """Mean of ``m`` over columns (vertical) or rows [lo, hi) within the face span."""
        if vertical:
            return m[span[0]:span[1], lo:hi].mean() if hi > lo else 0.0
        return m[lo:hi, span[0]:span[1]].mean() if hi > lo else 0.0

    found = 0
    sides = list(est)
    if n >= 3:  # edges need several frames to tell overlay from scene
        rows = (px(max(fy - fh * 0.5, 0), H), px(min(fy + fh * 1.8, 1), H))
        cols = (px(max(fx - fw * 0.8, 0), W), px(min(fx + fw * 1.8, 1), W))
        # webcams frame the head near the top, so the bottom edge can sit far below the face
        ranges = [(est[0] - cw * 0.6, fx - fw * 0.25), (est[1] - ch * 0.5, fy - fh * 0.2),
                  (fx + fw * 1.25, est[2] + cw * 0.6), (fy + fh * 1.4, est[3] + ch * 0.9)]
        for side, (lo, hi) in enumerate(ranges):
            vertical = side in (0, 2)
            outward = -1 if side in (0, 1) else 1
            size = W if vertical else H
            span = rows if vertical else cols
            a, b = px(max(lo, 0), size), px(min(hi, 1), size)
            if b - a < 3:
                continue
            idx = np.arange(a, b)
            edge = (sx[span[0]:span[1], a:b].mean(axis=0) if vertical else sy[a:b, span[0]:span[1]].mean(axis=1))
            m_in, m_out = [], []
            for i in map(int, idx):
                inner = (i + 2, i + 2 + band) if outward < 0 else (i - 2 - band, i - 2)
                outer = (i - 2 - band, i - 2) if outward < 0 else (i + 2, i + 2 + band)
                m_in.append(strip(motion, vertical, max(0, inner[0]), min(size, inner[1]), span))
                m_out.append(strip(motion, vertical, max(0, outer[0]), min(size, outer[1]), span))
            hit = _pick_line(edge, np.array(m_in), np.array(m_out), (idx + 0.5) / size, est[side],
                             (b - a) / size, floor)
            if hit is not None:
                sides[side] = hit
                found += 1
            elif (side < 2 and lo <= 0.02) or (side >= 2 and hi >= 0.98):
                sides[side] = 0.0 if side < 2 else 1.0  # webcams hug the frame edge

    left, top, right, bottom = sides
    # snap to the frame edges, keep the face inside, clamp
    left = 0.0 if left < SNAP else left
    top = 0.0 if top < SNAP else top
    right = 1.0 if right > 1 - SNAP else right
    bottom = 1.0 if bottom > 1 - SNAP else bottom
    left, top = max(0.0, min(left, fx)), max(0.0, min(top, fy))
    right, bottom = min(1.0, max(right, fx + fw)), min(1.0, max(bottom, fy + fh))
    w, h = right - left, bottom - top
    if w <= 0.03 or h <= 0.03 or w * h > 0.5:
        return None
    conf = round(min(1.0, seen / n) * (0.55 + 0.1 * found), 2)
    return CamGuess(x=round(left, 4), y=round(top, 4), w=round(w, 4), h=round(h, 4), confidence=conf,
                    method="border" if found >= 2 else "face", face=(round(fx, 4), round(fy, 4), round(fw, 4),
                                                                    round(fh, 4)),
                    frames_with_face=seen, frames=n)


def detect_files(paths: list[Path]) -> CamGuess | None:
    import cv2

    frames = [img for img in (cv2.imread(str(p)) for p in paths) if img is not None]
    return detect(frames)


def streamer_visible(paths: list[Path], box: tuple[float, float, float, float]) -> tuple[int, int]:
    """(frames with a face inside ``box``, frames checked): is the streamer actually on camera in this clip?

    ``box`` is the configured webcam region (x, y, w, h, normalised). It is checked with a 20 % margin
    (webcams drift, auto-detected boxes are tight) and upscaled first, so a small webcam still counts."""
    import cv2

    x, y, w, h = box
    mx, my = w * 0.2, h * 0.2
    finder = _FaceFinder()
    seen = checked = 0
    for p in paths:
        img = cv2.imread(str(p))
        if img is None:
            continue
        H, W = img.shape[:2]
        x0, y0 = int(max(0.0, x - mx) * W), int(max(0.0, y - my) * H)
        x1, y1 = int(min(1.0, x + w + mx) * W), int(min(1.0, y + h + my) * H)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        checked += 1
        scale = max(1.0, 480 / max(1, crop.shape[1]))
        if scale > 1:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        if finder(crop):
            seen += 1
    return seen, checked


def widen_for_sharpness(box: tuple[float, float, float, float], src_w: int, src_h: int, out_w: int,
                        max_upscale: float = 3.0) -> tuple[float, float, float, float]:
    """Grow a small webcam box around its centre so it is upscaled at most ``max_upscale``×.

    A 170 px webcam stretched to 1440 px (8×) is a blur; at ≤ 3× it stays sharp. Auto-detected boxes
    are often just the face, while the overlay around it is a 16:9 webcam, so the box grows towards
    a 16:9 shape (in pixels) — which then also sizes the panel (VideoEditor.cam_panel_h)."""
    x, y, w, h = box
    need = out_w / max_upscale / max(1, src_w)  # minimum width as a fraction of the frame
    if w >= need:
        return box
    # never more than 2.5× the detected box: on a low-resolution source the "sharp" width would
    # otherwise swallow most of the frame and the webcam panel would show gameplay
    nw = min(1.0, max(w, min(need, w * 2.5)))
    frame_aspect = src_w / max(1, src_h)
    nh = min(1.0, max(h, nw * frame_aspect / (16 / 9)))
    cx, cy = x + w / 2, y + h / 2
    nx = min(max(0.0, cx - nw / 2), 1.0 - nw)
    ny = min(max(0.0, cy - nh / 2), 1.0 - nh)
    return (round(nx, 4), round(ny, 4), round(nw, 4), round(nh, 4))
