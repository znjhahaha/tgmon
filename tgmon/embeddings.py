"""Local CPU embeddings. Model weights are installed at build/setup time.

Normal queries only open local files: no API, credentials or runtime downloads.
Run ``python -m tgmon.embeddings --download`` once for a non-Docker install.
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_ID = "fastembed:bge-small-zh-v1.5:mean-chunks-v1"
CACHE_DIR = Path(os.getenv("TGMON_EMBEDDING_CACHE") or
                 Path(__file__).resolve().parent.parent / "models")
_model = None
_retry_after = 0.0
_model_lock = threading.Lock()


def _get_model():
    global _model, _retry_after
    with _model_lock:
        if _model is not None:
            return _model
        if time.monotonic() < _retry_after:
            return None
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(CACHE_DIR),
                                   threads=2, cuda=False, local_files_only=True)
        except Exception as exc:
            _retry_after = time.monotonic() + 60
            logger.warning("本地 embedding 模型不可用，保留关键词检索: %s", exc)
        return _model


def _normalize(vector) -> np.ndarray | None:
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if not arr.size or not np.isfinite(arr).all():
        return None
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else None


def embed_query(text: str) -> np.ndarray | None:
    if not text or not text.strip():
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        return _normalize(next(iter(model.query_embed(text))))
    except Exception as exc:
        logger.warning("本地查询向量计算失败: %s", exc)
        return None


def embed_documents(texts: list[str]) -> list[np.ndarray | None]:
    """Average overlapping chunks so long Chinese documents retain their tail."""
    result: list[np.ndarray | None] = [None] * len(texts)
    chunks, owners = [], []
    for owner, text in enumerate(texts):
        text = (text or "").strip()
        for start in range(0, len(text), 320):
            chunks.append(text[start:start + 384])
            owners.append(owner)
            if start + 384 >= len(text):
                break
    if not chunks:
        return result
    model = _get_model()
    if model is None:
        return result
    try:
        totals: dict[int, np.ndarray] = {}
        vectors = model.passage_embed(chunks, batch_size=16)
        for owner, vector in zip(owners, vectors, strict=True):
            arr = _normalize(vector)
            if arr is None:
                raise ValueError("模型返回无效向量")
            totals[owner] = totals.get(owner, np.zeros_like(arr)) + arr
        for owner, total in totals.items():
            result[owner] = _normalize(total)
    except Exception as exc:
        logger.warning("本地文档向量计算失败: %s", exc)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true",
                        help="Download model weights during installation")
    args = parser.parse_args()
    if args.download:
        from fastembed import TextEmbedding
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(CACHE_DIR),
                              threads=2, cuda=False, local_files_only=False)
        vector = next(iter(model.query_embed("角色复刻")))
    else:
        vector = embed_query("角色复刻")
    if vector is None:
        raise SystemExit("Local model missing; run with --download during setup.")
    print(f"{MODEL_NAME}: {len(vector)} dimensions, CPU, cache={CACHE_DIR}")


if __name__ == "__main__":
    main()
