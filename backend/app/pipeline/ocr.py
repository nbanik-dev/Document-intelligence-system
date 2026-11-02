"""OCR with pre-processing and quality-aware retries."""
import logging
import os
import re

import cv2
import numpy as np
import pytesseract
from PIL import Image

from ..config import settings

# One OpenMP thread per Tesseract process: we parallelise across documents instead, which scales far better
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

log = logging.getLogger(__name__)


class OCRError(Exception):
    pass


def _to_gray(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)


def _upscale(gray: np.ndarray, min_width: int = 1600) -> np.ndarray:
    h, w = gray.shape
    if w < min_width:
        f = min_width / w
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    return gray


def _deskew(gray: np.ndarray) -> np.ndarray:
    inv = cv2.bitwise_not(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
    coords = np.column_stack(np.where(inv > 0))
    if len(coords) < 200:
        return gray
    angle = cv2.minAreaRect(coords.astype(np.float32))[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) < 0.3 or abs(angle) > 15:
        return gray
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _variants(img: Image.Image):
    """Yield progressively heavier pre-processing variants (cheap first)."""
    gray = _upscale(_to_gray(img))
    yield "plain", gray
    den = cv2.fastNlMeansDenoising(gray, None, 15, 7, 21)
    yield "deskew+otsu", cv2.threshold(_deskew(den), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    yield "adaptive", cv2.adaptiveThreshold(_deskew(cv2.medianBlur(gray, 3)), 255,
                                            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)


def _run(arr: np.ndarray) -> tuple[str, float, list]:
    cfg = "--psm 6"
    data = pytesseract.image_to_data(Image.fromarray(arr), lang=settings.ocr_lang, config=cfg,
                                     output_type=pytesseract.Output.DICT)
    words, confs = [], []
    lines: dict = {}
    for i, t in enumerate(data["text"]):
        t = t.strip()
        c = float(data["conf"][i])
        if not t or c < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(t)
        confs.append(c)
        words.append((t, c))
    text = "\n".join(" ".join(v) for _, v in sorted(lines.items()))
    return text, (sum(confs) / len(confs) if confs else 0.0), words


def ocr_image(img: Image.Image) -> tuple[str, float, list]:
    """Returns (text, mean word confidence 0-100, [(word, conf)]). Retries heavier pre-processing on poor confidence."""
    best_text, best_conf, best_words = "", -1.0, []
    try:
        for name, arr in _variants(img):
            text, conf, words = _run(arr)
            # prefer higher confidence, but never trade a long text for a near-empty one
            score = conf * min(1.0, (len(text) + 1) / max(len(best_text), 1)) if best_text else conf
            if score > best_conf:
                best_text, best_conf, best_words = text, conf if text else 0.0, words
            if best_conf >= settings.ocr_min_confidence + 15 and len(best_text) > 30:
                break
    except pytesseract.TesseractNotFoundError as e:
        raise OCRError("Tesseract is not installed on this host") from e
    except Exception as e:  # noqa: BLE001
        raise OCRError(f"OCR failed: {e}") from e
    return re.sub(r"[ \t]+", " ", best_text).strip(), max(best_conf, 0.0), best_words
