"""Client for the local embedding service (embedding_service/, POST /embeddings).

Contract of that service:
    request   {"text": ["...", "..."]}
    response  {"embeddings": [[float x 384], ...]}   same order as the input

Texts are sent in batches of EMBEDDING_BATCH_SIZE; transient failures (timeout,
connection error, 5xx) are retried twice with backoff.
"""

import asyncio

import httpx

from app.core.config import settings
from app.core.constants import EMBEDDING_BATCH_SIZE, EMBEDDING_DIMENSIONS
from app.core.logger import get_logger

logger = get_logger()

_RETRIES = 2


class EmbeddingError(Exception):
    """The embedding service failed or returned something unusable."""


async def _post_batch(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    for attempt in range(_RETRIES + 1):
        try:
            r = await client.post(settings.EMBEDDING_SERVICE_URL, json={"text": texts})
            if r.status_code >= 500 and attempt < _RETRIES:
                raise httpx.HTTPStatusError(
                    "server error", request=r.request, response=r
                )
            r.raise_for_status()
            vectors = r.json()["embeddings"]
            break
        except (
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.HTTPStatusError,
        ) as exc:
            transient = not isinstance(exc, httpx.HTTPStatusError) or (
                exc.response.status_code >= 500
            )
            if not transient or attempt == _RETRIES:
                logger.error("Embedding request failed", extra={"error": str(exc)})
                raise EmbeddingError(str(exc)) from exc
            await asyncio.sleep(0.5 * 2**attempt)
        except (KeyError, ValueError) as exc:
            raise EmbeddingError("Malformed response from embedding service") from exc

    if len(vectors) != len(texts) or any(
        len(v) != EMBEDDING_DIMENSIONS for v in vectors
    ):
        raise EmbeddingError(
            f"Expected {len(texts)} vectors of {EMBEDDING_DIMENSIONS} dims"
        )
    return [[float(x) for x in v] for v in vectors]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts in order. Raises EmbeddingError on failure."""
    if not texts:
        return []
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            out.extend(await _post_batch(client, texts[i : i + EMBEDDING_BATCH_SIZE]))
    return out
