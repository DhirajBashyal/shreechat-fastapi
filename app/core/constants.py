API_RATE_LIMIT = "5/minute"

# Embedding model served by embedding_service/. Changing it requires a migration
# because kb_chunks.embedding is a fixed-size vector column.
EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384
