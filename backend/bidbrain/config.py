"""Runtime settings, read once from the environment (and `.env` at the repo root)."""
from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv

BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
load_dotenv(ROOT / ".env")

VENDOR = BACKEND / "vendor_bidintel"
FACTS_JSON = VENDOR / "results" / "raw" / "facts.json"
TEXT_CACHE = VENDOR / "results" / "raw" / "text_cache.json"
DATASET = VENDOR / "dataset"
DOCUMENTS = DATASET / "documents"
DATA = ROOT / "data"
EMBED_CACHE = DATA / "chunk_embeddings.npz"

FALKOR_HOST = os.getenv("FALKOR_HOST", "localhost")
FALKOR_PORT = int(os.getenv("FALKOR_PORT", "6379"))
BASE_GRAPH = os.getenv("BASE_GRAPH", "bidbrain_base")

# Every date in the graph is also stored as an integer day number so Cypher
# filters compare ints instead of depending on the dialect's date functions.
EPOCH = "1970-01-01"

COMPANY = "National Infrastructure Corp. Ltd."
# The "as of" date for validity checks. The corpus is frozen with every
# certificate dated 2026-03-31; demos and evals use this as "today".
AS_OF = os.getenv("BIDBRAIN_AS_OF", "2026-03-31")
