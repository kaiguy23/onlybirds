"""Compute and cache Gemini embeddings for species ID text.

We embed the eBird Identification blurb (falling back to the Wikipedia summary)
for every species the dashboard might display, then store the normalized
float32 vector as a BLOB on `species_info`. Storing normalized vectors lets
query-time similarity collapse to a single dot product against the corpus
matrix.

Re-embed is incremental: a sha256 of (source_text + model + dim) is stored
alongside, and rows whose hash still matches are skipped on subsequent runs.
"""

import functools
import hashlib
import os
import re
import sqlite3
import time

import numpy as np
from google import genai
from google.genai import types
from tqdm import tqdm

MODEL = "gemini-embedding-2"
DIM = 768
DOC_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"
# Gemini's free tier counts each item inside a batch against the per-minute
# request quota, so a single 100-item batch exhausts the budget. Smaller
# batches let us pace work between retries without burning whole minutes on a
# single rate-limit window.
BATCH_SIZE = 50
MAX_RETRIES = 5
DEFAULT_BACKOFF_S = 30.0


def _client() -> genai.Client:
    """Build a client from GEMINI_API_KEY in the environment.

    .env is loaded once by the CLI entry point; for ad-hoc use, callers can
    `dotenv.load_dotenv()` themselves.
    """
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set; cannot compute embeddings")
    return genai.Client(api_key=key)


def _source_text(common_name: str, ebird_id_text: str | None, summary: str | None) -> str | None:
    """Pick the best available ID text and prefix it with the common name.

    Prefixing with the species name gives the embedding a small grounding
    signal: queries that mention a name match more strongly, and entries
    without one are still ranked purely on appearance.
    """
    body = (ebird_id_text or summary or "").strip()
    if not body:
        return None
    return f"{common_name}: {body}" if common_name else body


def _hash(text: str) -> str:
    digest = hashlib.sha256(f"{text}\x00{MODEL}\x00{DIM}".encode("utf-8")).hexdigest()
    return digest


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec.astype(np.float32, copy=False)
    return (vec / norm).astype(np.float32, copy=False)


def _parse_retry_delay(err: Exception) -> float:
    """Pull the `retryDelay` hint out of a Gemini 429 error message.

    The SDK surfaces the raw error dict in str(e); we look for `retryDelay:
    '19s'` or similar. Falls back to a generous default so we don't spin.
    """
    m = re.search(r"retryDelay['\"]*\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s", str(err))
    if m:
        # Add a small jitter so two parallel runs don't unblock together.
        return float(m.group(1)) + 1.0
    return DEFAULT_BACKOFF_S


def _embed_batch(client: genai.Client, texts: list[str], task_type: str) -> list[np.ndarray]:
    # gemini-embedding-2 treats a `list[str]` as a single multi-part Content and
    # returns one embedding for the whole batch. Wrapping each text in its own
    # `Content` is what triggers per-text embeddings. (gemini-embedding-001
    # accepts the bare list — we standardize on the Content wrapper so both
    # models behave the same.)
    config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIM)
    contents = [types.Content(parts=[types.Part(text=t)]) for t in texts]
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.models.embed_content(model=MODEL, contents=contents, config=config)  # ty: ignore[invalid-argument-type]
            embeddings_out = resp.embeddings or []
            return [_normalize(np.asarray(e.values, dtype=np.float32)) for e in embeddings_out]
        except Exception as e:  # noqa: BLE001 — we want to inspect the error
            last_err = e
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(_parse_retry_delay(e))
    assert last_err is not None
    raise last_err


def recompute_embeddings(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    """Embed every species in the display scope; skip rows whose text is unchanged.

    Returns counts so the CLI step can print a one-line summary matching the
    shape used by other pipeline stages.
    """
    rows = conn.execute(
        """
        SELECT x.species_code, x.common_name, s.ebird_id_text, s.summary, s.embedding_source_hash
        FROM taxonomy x
        LEFT JOIN species_info s ON s.species_code = x.species_code
        WHERE x.species_code IN (SELECT species_code FROM targets)
           OR x.species_code IN (SELECT species_code FROM hotspot_obs)
        """
    ).fetchall()

    pending: list[tuple[str, str, str]] = []  # (species_code, source_text, new_hash)
    skipped_no_text = 0
    skipped_unchanged = 0
    for r in rows:
        text = _source_text(r["common_name"], r["ebird_id_text"], r["summary"])
        if text is None:
            skipped_no_text += 1
            continue
        new_hash = _hash(text)
        if not force and r["embedding_source_hash"] == new_hash:
            skipped_unchanged += 1
            continue
        pending.append((r["species_code"], text, new_hash))

    if not pending:
        return {
            "updated": 0,
            "skipped_unchanged": skipped_unchanged,
            "skipped_no_text": skipped_no_text,
            "failed": 0,
            "total": len(rows),
        }

    client = _client()
    updated = 0
    failed = 0
    last_error: str | None = None
    for start in tqdm(range(0, len(pending), BATCH_SIZE), desc="  embed", unit="batch", leave=False):
        batch = pending[start:start + BATCH_SIZE]
        try:
            vectors = _embed_batch(client, [t for _, t, _ in batch], DOC_TASK)
        except Exception as e:  # noqa: BLE001
            failed += len(batch)
            last_error = f"{type(e).__name__}: {str(e)[:200]}"
            continue
        for (code, _, new_hash), vec in zip(batch, vectors):
            conn.execute(
                "UPDATE species_info SET embedding = ?, embedding_source_hash = ?, embedding_model = ? "
                "WHERE species_code = ?",
                (vec.tobytes(), new_hash, MODEL, code),
            )
            updated += 1
    conn.commit()
    if last_error is not None:
        print(f"      embedding errors (last seen): {last_error}")

    return {
        "updated": updated,
        "skipped_unchanged": skipped_unchanged,
        "skipped_no_text": skipped_no_text,
        "failed": failed,
        "total": len(rows),
    }


@functools.lru_cache(maxsize=128)
def embed_query(text: str) -> np.ndarray:
    """Embed a user search query as a normalized float32 vector.

    Cached so repeated identical searches in a Streamlit session are free.
    The cache lives for the lifetime of the Python process.
    """
    client = _client()
    vectors = _embed_batch(client, [text], QUERY_TASK)
    return vectors[0]


def load_embeddings(
    conn: sqlite3.Connection, species_codes: list[str]
) -> tuple[list[str], np.ndarray]:
    """Load stored embeddings for a candidate pool into a single (N, DIM) matrix.

    Returns (codes_in_matrix_order, matrix). Species without an embedding are
    silently dropped — callers should treat the returned list as the actually
    rankable subset.
    """
    if not species_codes:
        return [], np.empty((0, DIM), dtype=np.float32)
    placeholders = ",".join("?" * len(species_codes))
    rows = conn.execute(
        f"SELECT species_code, embedding FROM species_info "
        f"WHERE species_code IN ({placeholders}) AND embedding IS NOT NULL",
        species_codes,
    ).fetchall()
    codes = [r["species_code"] for r in rows]
    if not codes:
        return [], np.empty((0, DIM), dtype=np.float32)
    matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    return codes, matrix
