import httpx
from httpx import TimeoutException

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger()


async def generate_embeddings(texts: list[str]) -> list[list[float]] | None:
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                settings.EMBEDDING_SERVICE_URL,
                json={"texts": texts},
                timeout=10.0,  # Set a timeout for the request
            )
            r.raise_for_status()  # Raise an error for HTTP errors
            return r.json()["embeddings"]
        except TimeoutException:
            logger.error("Request to embedding service timed out")
        except httpx.HTTPError as e:
            logger.error("Failed to generate embeddings", extra={"error": str(e)})
        return None
