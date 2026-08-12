import hashlib
import math
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from app.core.config import settings


def fallback_embedding(text: str, dimensions: int = 384) -> list[float]:
    """Dependency-free feature hashing keeps search useful if the model is unavailable."""
    vector = [0.0] * dimensions
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=settings.semantic_model, cache_dir=str(settings.intelligence_cache_path))


def embed_text(text: str, query: bool = False) -> list[float]:
    if not settings.semantic_enabled:
        return fallback_embedding(text)
    prefix = "query: " if query else "passage: "
    try:
        return list(_model().embed([prefix + text]))[0].tolist()
    except Exception:
        return fallback_embedding(text)


def extract_ocr(path: Path, mime_type: str) -> str:
    if not settings.ocr_enabled or not mime_type.startswith("image/"):
        return ""
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng", "--psm", "11"],
            capture_output=True, text=True, timeout=120, check=True,
        )
        return " ".join(result.stdout.split())[:100_000]
    except (OSError, subprocess.SubprocessError):
        return ""


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))
