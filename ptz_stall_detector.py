#!/usr/bin/env python3
"""
PtzFeatureStallDetector: Ported directly and faithfully from CamXposed-master
(subhranshu/camXposed/PtzFeatureStallDetector.java).

Lightweight feature tracker and stall detector designed for pan/tilt cameras.
Finds the dominant coherent background translation using:
1. Primary: Harris corner detection + 128-bit BRIEF binary descriptors (seed 0x51f15e)
   + Hamming distance matching with Lowe's ratio test + RANSAC translation consensus.
2. Fallback: Multi-region normalized cross-correlation (NCC) on Sobel gradient magnitudes
   across 12 spatial grid patches when feature matches are insufficient or unreliable.
3. Conservative stall policy: if both independent detectors are uncertain,
   assumed_unchanged is True (view_changed is False).
"""

from __future__ import annotations

import io
import math
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw


# ---------------------------------------------------------------------------
# Constants matching PtzFeatureStallDetector.java exactly
# ---------------------------------------------------------------------------
W: int = 320
H: int = 240
BITS: int = 128
MAX_FEATURES: int = 420
MIN_MATCHES: int = 8
MIN_INLIER_RATIO: float = 0.28
MOVE_THRESHOLD_PX: float = 1.8
TEMPLATE_SIZE: int = 32
TEMPLATE_RADIUS: int = 16
TEMPLATE_MIN_SCORE: float = 0.68
TEMPLATE_MIN_MATCHES: int = 4
TEMPLATE_MIN_CELLS: int = 3
TEMPLATE_CONSENSUS_PX: float = 3.0


def _generate_brief_pattern() -> List[Tuple[int, int, int, int]]:
    seed = 0x51F15E
    pairs: List[Tuple[int, int, int, int]] = []
    for _ in range(BITS):
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        ax = ((seed >> 24) & 0xFF) % 9 - 4
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        ay = ((seed >> 24) & 0xFF) % 9 - 4
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        bx = ((seed >> 24) & 0xFF) % 9 - 4
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        by = ((seed >> 24) & 0xFF) % 9 - 4
        pairs.append((ax, ay, bx, by))
    return pairs


BRIEF_PAIRS: List[Tuple[int, int, int, int]] = _generate_brief_pattern()


class Match:
    """Represents a feature match between before and after frames."""
    __slots__ = ("x1", "y1", "x2", "y2", "inlier")

    def __init__(self, x1: float, y1: float, x2: float, y2: float, inlier: bool):
        self.x1: float = float(x1)
        self.y1: float = float(y1)
        self.x2: float = float(x2)
        self.y2: float = float(y2)
        self.inlier: bool = bool(inlier)


class Feature:
    """Internal feature point with Harris score and 128-bit BRIEF descriptor."""
    __slots__ = ("x", "y", "score", "d0", "d1")

    def __init__(self, x: int, y: int, score: float, d0: int, d1: int):
        self.x: int = x
        self.y: int = y
        self.score: float = float(score)
        self.d0: int = d0  # low 64 bits
        self.d1: int = d1  # high 64 bits


class TemplateMatch:
    """Internal candidate match from multi-region template correlation."""
    __slots__ = ("x", "y", "dx", "dy", "score", "cell")

    def __init__(self, x: int, y: int, dx: int, dy: int, score: float, cell: int):
        self.x: int = x
        self.y: int = y
        self.dx: int = dx
        self.dy: int = dy
        self.score: float = float(score)
        self.cell: int = cell


class Result:
    """Complete diagnostic result matching PtzFeatureStallDetector.Result in Java."""

    def __init__(self):
        self.keypoints_before: int = 0
        self.keypoints_after: int = 0
        self.good_matches: int = 0
        self.inliers: int = 0
        self.spatial_cells: int = 0
        self.inlier_ratio: float = 0.0
        self.dx: float = 0.0
        self.dy: float = 0.0
        self.displacement: float = 0.0
        self.confidence: float = 0.0
        self.reliable: bool = False
        self.view_changed: bool = False
        self.template_attempted: bool = False
        self.template_reliable: bool = False
        self.assumed_unchanged: bool = False
        self.template_candidates: int = 0
        self.template_matches: int = 0
        self.template_inliers: int = 0
        self.template_spatial_cells: int = 0
        self.template_median_score: float = 0.0
        self.template_dx: float = 0.0
        self.template_dy: float = 0.0
        self.template_displacement: float = 0.0
        self.template_diagnostic_jpeg: Optional[bytes] = None
        self.matches: List[Match] = []
        self.diagnostic_jpeg: Optional[bytes] = None

    def summary(self) -> str:
        """Exact string formatting matching PtzFeatureStallDetector.Result.summary() in Java."""
        return (
            f"ORB-like binary features (custom BRIEF/Harris), RANSAC translation: "
            f"kp={self.keypoints_before}/{self.keypoints_after}, ratioMatches={self.good_matches}, "
            f"backgroundInliers={self.inliers}, inlierRatio={self.inlier_ratio*100.0:.1f}%, "
            f"spatialCells={self.spatial_cells}, medianShift=({self.dx:.2f},{self.dy:.2f})px, "
            f"displacement={self.displacement:.2f}px, confidence={self.confidence*100.0:.1f}%, "
            f"reliable={self.reliable}, viewpointChanged={self.view_changed}; "
            f"templateAttempted={self.template_attempted}, templateCandidates={self.template_candidates}, "
            f"templateMatches={self.template_matches}, templateInliers={self.template_inliers}, "
            f"templateCells={self.template_spatial_cells}, templateMedianScore={self.template_median_score:.2f}, "
            f"templateShift=({self.template_dx:.2f},{self.template_dy:.2f})px, "
            f"templateDisplacement={self.template_displacement:.2f}px, templateReliable={self.template_reliable}, "
            f"assumedUnchanged={self.assumed_unchanged}"
        )


def _median(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _decode_and_gray(data: bytes) -> Tuple[Optional[Image.Image], Optional[np.ndarray]]:
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.size != (W, H):
            im = im.resize((W, H), Image.Resampling.BILINEAR)
        rgb = np.asarray(im, dtype=np.int32)
        gray = (rgb[:, :, 0] * 299 + rgb[:, :, 1] * 587 + rgb[:, :, 2] * 114) // 1000
        return im, gray
    except Exception:
        return None, None


def _descriptor(g_2d: np.ndarray, x: int, y: int) -> Tuple[int, int]:
    d0 = 0
    d1 = 0
    for bit, (ax, ay, bx, by) in enumerate(BRIEF_PAIRS):
        if g_2d[y + ay, x + ax] < g_2d[y + by, x + bx]:
            if bit < 64:
                d0 |= (1 << bit)
            else:
                d1 |= (1 << (bit - 64))
    return d0, d1


def _detect(g_2d: np.ndarray) -> List[Feature]:
    gx_map = np.zeros((H, W), dtype=np.float64)
    gy_map = np.zeros((H, W), dtype=np.float64)
    gx_map[:, 1:W-1] = g_2d[:, 2:W] - g_2d[:, 0:W-2]
    gy_map[1:H-1, :] = g_2d[2:H, :] - g_2d[0:H-2, :]

    xx_map = cv2.boxFilter(gx_map * gx_map, -1, (5, 5), normalize=False, borderType=cv2.BORDER_CONSTANT)
    yy_map = cv2.boxFilter(gy_map * gy_map, -1, (5, 5), normalize=False, borderType=cv2.BORDER_CONSTANT)
    xy_map = cv2.boxFilter(gx_map * gy_map, -1, (5, 5), normalize=False, borderType=cv2.BORDER_CONSTANT)

    tr_map = xx_map + yy_map
    score_map = (xx_map * yy_map - xy_map * xy_map) - 0.045 * (tr_map * tr_map)

    all_features: List[Feature] = []
    for y in range(5, H - 5, 2):
        for x in range(5, W - 5, 2):
            score = score_map[y, x]
            if score > 180000.0:
                d0, d1 = _descriptor(g_2d, x, y)
                all_features.append(Feature(x, y, score, d0, d1))

    all_features.sort(key=lambda f: f.score, reverse=True)

    grid = np.zeros((8, 6), dtype=np.int32)
    out: List[Feature] = []
    for f in all_features:
        cx = f.x // 40
        cy = f.y // 40
        if grid[cx, cy] >= 10:
            continue
        near = False
        for q in out:
            if (f.x - q.x) ** 2 + (f.y - q.y) ** 2 < 64:
                near = True
                break
        if not near:
            out.append(f)
            grid[cx, cy] += 1
            if len(out) >= MAX_FEATURES:
                break
    return out


def _match(a: List[Feature], b: List[Feature]) -> List[Match]:
    out: List[Match] = []
    used = [False] * len(b)
    for f in a:
        best = -1
        d1 = 999
        d2 = 999
        for j, q in enumerate(b):
            d = (f.d0 ^ q.d0).bit_count() + (f.d1 ^ q.d1).bit_count()
            if d < d1:
                d2 = d1
                d1 = d
                best = j
            elif d < d2:
                d2 = d
        if best >= 0 and d1 < 58 and d1 < d2 * 0.78 and not used[best]:
            q = b[best]
            out.append(Match(f.x, f.y, q.x, q.y, False))
            used[best] = True
    return out


def _gradient_magnitude(g_2d: np.ndarray) -> np.ndarray:
    out = np.zeros((H, W), dtype=np.float32)
    g = g_2d.astype(np.float32)
    gx = (-g[:-2, :-2] - 2 * g[1:-1, :-2] - g[2:, :-2] + g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:])
    gy = (-g[:-2, :-2] - 2 * g[:-2, 1:-1] - g[:-2, 2:] + g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:])
    out[1:H-1, 1:W-1] = np.hypot(gx, gy)
    return out


def _annotate(base_img: Image.Image, matches: List[Match], note: str) -> bytes:
    im = base_img.copy().convert("RGB")
    draw = ImageDraw.Draw(im)
    shown = 0
    for m in matches:
        if shown >= 100:
            break
        shown += 1
        color = (0, 255, 0) if m.inlier else (255, 0, 0)
        draw.line([(m.x1, m.y1), (m.x2, m.y2)], fill=color, width=1)
        r = 2.6
        draw.ellipse([m.x2 - r, m.y2 - r, m.x2 + r, m.y2 + r], fill=color, outline=color)
    draw.rectangle([0, 0, W, 18], fill=(0, 0, 0))
    draw.text((3, 3), note, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=78)
    return buf.getvalue()


def _annotate_templates(base_img: Image.Image, matches: List[TemplateMatch], note: str) -> bytes:
    im = base_img.copy().convert("RGB")
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, W, 18], fill=(0, 0, 0))
    draw.text((3, 3), note, fill=(255, 255, 255))
    half = TEMPLATE_SIZE // 2
    for m in matches:
        draw.rectangle([m.x - half, m.y - half, m.x + half, m.y + half], outline=(0, 255, 0), width=1)
        draw.line([(m.x, m.y), (m.x + m.dx, m.y + m.dy)], fill=(255, 255, 0), width=1)
        draw.ellipse([m.x + m.dx - 2.5, m.y + m.dy - 2.5, m.x + m.dx + 2.5, m.y + m.dy + 2.5], fill=(255, 255, 0))
        draw.text((m.x - half, max(0, m.y - half - 10)), f"{m.score:.2f}", fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=78)
    return buf.getvalue()


def _apply_template_fallback(r: Result, base_img: Optional[Image.Image], ga_2d: np.ndarray, gb_2d: np.ndarray) -> None:
    r.template_attempted = True
    ga = _gradient_magnitude(ga_2d)
    gb = _gradient_magnitude(gb_2d)
    candidates: List[TemplateMatch] = []
    centers = [
        [40, 40], [120, 40], [200, 40], [280, 40],
        [40, 120], [120, 120], [200, 120], [280, 120],
        [40, 200], [120, 200], [200, 200], [280, 200]
    ]
    half = TEMPLATE_SIZE // 2
    for index, (cx, cy) in enumerate(centers):
        x0 = cx - half
        y0 = cy - half
        patch = ga[y0:y0+TEMPLATE_SIZE, x0:x0+TEMPLATE_SIZE]
        area = TEMPLATE_SIZE * TEMPLATE_SIZE
        template_mean = float(np.mean(patch))
        template_sq = float(np.sum(patch * patch))
        template_var = template_sq - area * template_mean * template_mean
        if template_var < 18.0:
            continue
        r.template_candidates += 1

        best = -2.0
        best_dx = 0
        best_dy = 0

        for dy in range(-TEMPLATE_RADIUS, TEMPLATE_RADIUS + 1):
            sy = y0 + dy
            if sy < 0 or sy + TEMPLATE_SIZE > H:
                continue
            for dx in range(-TEMPLATE_RADIUS, TEMPLATE_RADIUS + 1):
                sx = x0 + dx
                if sx < 0 or sx + TEMPLATE_SIZE > W:
                    continue
                target = gb[sy:sy+TEMPLATE_SIZE, sx:sx+TEMPLATE_SIZE]
                s = float(np.sum(target))
                s_sq = float(np.sum(target * target))
                mean = s / area
                var = s_sq - area * mean * mean
                if var < 18.0:
                    continue
                cross = float(np.sum(patch * target))
                cov = cross - area * template_mean * mean
                score = cov / math.sqrt(template_var * var)
                if score > best:
                    best = score
                    best_dx = dx
                    best_dy = dy

        if best >= TEMPLATE_MIN_SCORE:
            candidates.append(TemplateMatch(cx, cy, best_dx, best_dy, best, index))

    r.template_matches = len(candidates)
    if not candidates:
        if base_img is not None:
            r.template_diagnostic_jpeg = _annotate_templates(base_img, candidates, "Template matching: no confident textured regions")
        r.assumed_unchanged = True
        r.reliable = True
        r.view_changed = False
        return

    xs = [float(m.dx) for m in candidates]
    ys = [float(m.dy) for m in candidates]
    seed_x = _median(xs)
    seed_y = _median(ys)

    in_x: List[float] = []
    in_y: List[float] = []
    in_scores: List[float] = []
    cells = [False] * 12
    for m in candidates:
        if math.hypot(m.dx - seed_x, m.dy - seed_y) <= TEMPLATE_CONSENSUS_PX:
            in_x.append(float(m.dx))
            in_y.append(float(m.dy))
            in_scores.append(float(m.score))
            cells[m.cell] = True

    r.template_spatial_cells = sum(1 for c in cells if c)
    r.template_inliers = len(in_x)
    r.template_dx = _median(in_x)
    r.template_dy = _median(in_y)
    r.template_displacement = math.hypot(r.template_dx, r.template_dy)
    r.template_median_score = _median(in_scores)
    r.template_reliable = (
        r.template_inliers >= TEMPLATE_MIN_MATCHES and r.template_spatial_cells >= TEMPLATE_MIN_CELLS
    )
    if base_img is not None:
        r.template_diagnostic_jpeg = _annotate_templates(
            base_img, candidates, "Template NCC: green=consensus, red=outlier; arrows=matched shift"
        )

    if r.template_reliable:
        r.reliable = True
        r.view_changed = (r.template_displacement >= MOVE_THRESHOLD_PX)
        r.dx = r.template_dx
        r.dy = r.template_dy
        r.displacement = r.template_displacement
        r.confidence = min(1.0, r.template_inliers / 8.0) * min(1.0, r.template_median_score)
    else:
        r.assumed_unchanged = True
        r.reliable = True
        r.view_changed = False


class PtzFeatureStallDetector:
    """Exact Python equivalent of subhranshu.camXposed.PtzFeatureStallDetector."""

    Match = Match
    Result = Result

    @staticmethod
    def compare(before_jpeg: bytes, after_jpeg: bytes) -> Result:
        r = Result()
        a_im: Optional[Image.Image] = None
        b_im: Optional[Image.Image] = None
        try:
            a_im, ga = _decode_and_gray(before_jpeg)
            b_im, gb = _decode_and_gray(after_jpeg)
            if a_im is None or b_im is None or ga is None or gb is None:
                return r

            fa = _detect(ga)
            fb = _detect(gb)
            r.keypoints_before = len(fa)
            r.keypoints_after = len(fb)

            raw = _match(fa, fb)
            r.good_matches = len(raw)
            if not raw:
                r.diagnostic_jpeg = _annotate(b_im, r.matches, "Insufficient feature matches; trying template matching")
                _apply_template_fallback(r, b_im, ga, gb)
                return r

            best_count = 0
            best_x = 0.0
            best_y = 0.0
            for m in raw:
                vx = round((m.x2 - m.x1) / 2.0) * 2.0
                vy = round((m.y2 - m.y1) / 2.0) * 2.0
                count = sum(1 for n in raw if math.hypot((n.x2 - n.x1) - vx, (n.y2 - n.y1) - vy) <= 3.2)
                if count > best_count:
                    best_count = count
                    best_x = vx
                    best_y = vy

            xs: List[float] = []
            ys: List[float] = []
            in_flags: List[bool] = [False] * len(raw)
            for i, m in enumerate(raw):
                dx = m.x2 - m.x1
                dy = m.y2 - m.y1
                if math.hypot(dx - best_x, dy - best_y) <= 3.2:
                    in_flags[i] = True
                    xs.append(dx)
                    ys.append(dy)

            r.inliers = len(xs)
            r.inlier_ratio = float(r.inliers) / max(1, len(raw))
            r.dx = _median(xs)
            r.dy = _median(ys)
            r.displacement = math.hypot(r.dx, r.dy)

            cells = np.zeros((4, 4), dtype=bool)
            for i, m in enumerate(raw):
                tagged = Match(m.x1, m.y1, m.x2, m.y2, in_flags[i])
                r.matches.append(tagged)
                if in_flags[i]:
                    cx = min(3, int(m.x2 / W * 4))
                    cy = min(3, int(m.y2 / H * 4))
                    cells[cx, cy] = True

            r.spatial_cells = int(np.sum(cells))
            r.confidence = r.inlier_ratio * min(1.0, r.inliers / 24.0) * min(1.0, r.spatial_cells / 6.0)
            r.reliable = (r.inliers >= MIN_MATCHES and r.inlier_ratio >= MIN_INLIER_RATIO and r.spatial_cells >= 3)
            r.view_changed = (r.reliable and r.displacement >= MOVE_THRESHOLD_PX)
            r.diagnostic_jpeg = _annotate(b_im, r.matches, "Green=coherent background; red=outlier/moving object")

            if not r.reliable:
                _apply_template_fallback(r, b_im, ga, gb)

            return r
        except Exception:
            try:
                if b_im is not None:
                    _, ga_safe = _decode_and_gray(before_jpeg if a_im is not None else after_jpeg)
                    _, gb_safe = _decode_and_gray(after_jpeg)
                    if ga_safe is not None and gb_safe is not None:
                        _apply_template_fallback(r, b_im, ga_safe, gb_safe)
            except Exception:
                pass
            return r
