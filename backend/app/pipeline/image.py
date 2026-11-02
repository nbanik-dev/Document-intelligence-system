"""JPG / PNG / TIFF (multi-page) images -> OCR."""
from typing import Callable

from PIL import Image, ImageSequence, UnidentifiedImageError

from ..config import settings
from .ocr import ocr_image
from .pdf import CorruptFileError
from .types import PageText, ParsedDocument

Image.MAX_IMAGE_PIXELS = 200_000_000  # decompression-bomb guard


def parse_image(path: str, progress: Callable[[float], None] | None = None) -> ParsedDocument:
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise CorruptFileError(f"Cannot decode image: {e}") from e
    frames = [f.copy() for f in ImageSequence.Iterator(img)]
    out = ParsedDocument(kind="image")
    for i, frame in enumerate(frames):
        try:
            text, conf, words = ocr_image(frame)
            out.pages.append(PageText(i + 1, text, "ocr", conf, words=words))
            if conf < settings.ocr_min_confidence:
                out.warnings.append(f"frame {i + 1}: low OCR confidence ({conf:.0f})")
        except Exception as e:  # noqa: BLE001
            out.pages.append(PageText(i + 1, "", "error", error=str(e)))
            out.warnings.append(f"frame {i + 1}: {e}")
        if progress:
            progress((i + 1) / len(frames))
    if all(p.method == "error" for p in out.pages):
        raise CorruptFileError("OCR failed on every frame: " + "; ".join(out.warnings))
    return out
