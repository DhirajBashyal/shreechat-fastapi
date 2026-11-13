API_RATE_LIMIT = "5/minute"

# Embedding model served by embedding_service/. Changing it requires a migration
# because kb_chunks.embedding is a fixed-size vector column.
EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384

# paraphrase-multilingual-MiniLM-L12-v2 reads at most 128 word-piece tokens per
# text and silently ignores the rest, so knowledge chunks must stay below that
# (docs/ai/RAG_ARCHITECTURE.md §2 assumes a 8k-token OpenAI model; not ours).
EMBEDDING_MAX_TOKENS = 128
KB_CHUNK_TOKENS = 100
KB_CHUNK_OVERLAP_TOKENS = 15
EMBEDDING_BATCH_SIZE = 64
