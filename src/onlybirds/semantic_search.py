"""Runtime semantic-search helpers for the dashboard.

`rank_species` is the core operation: given a free-text query and a candidate
pool of species_codes (already narrowed by hotspot/region/rarity filters in
the calling view), return the top-K species by cosine similarity against the
Gemini-embedded ID text we cached in `species_info.embedding`.

`narrate_top_matches_stream` is an optional Gemini Flash call that yields a
1–3 sentence narration in chunks ("Top guess: Mountain Chickadee — fits the
gray-with-black-cap description, common at this hotspot in May"). It always
degrades to no narration on failure; ranked results never depend on the LLM
succeeding.
"""

import os
import sqlite3
from typing import Iterable, Iterator

import pandas as pd
from google import genai
from google.genai import types

from . import embeddings

NARRATION_MODEL = "gemini-2.5-flash"


def rank_species(
    conn: sqlite3.Connection,
    query: str,
    species_codes: Iterable[str],
    top_k: int = 10,
) -> pd.DataFrame:
    """Rank a candidate pool of species against the embedded query.

    Returns a DataFrame with columns: species_code, similarity. Rows are
    sorted by similarity descending and capped at top_k. Species without a
    stored embedding are dropped (they can't be ranked).
    """
    codes_list = list(species_codes)
    if not codes_list or not query.strip():
        return pd.DataFrame(columns=["species_code", "similarity"])

    codes, matrix = embeddings.load_embeddings(conn, codes_list)
    if not codes:
        return pd.DataFrame(columns=["species_code", "similarity"])

    q = embeddings.embed_query(query.strip())
    sims = matrix @ q  # both sides are unit-normalized, so dot product == cosine

    df = pd.DataFrame({"species_code": codes, "similarity": sims})
    df = df.sort_values("similarity", ascending=False, ignore_index=True)
    return df.head(top_k)


def _narration_client() -> genai.Client | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    return genai.Client(api_key=key)


_SYSTEM_INSTRUCTION = (
    "You are a bird ID assistant helping narrow down a sighting. Each user "
    "turn refines the description. Top candidates (by embedding similarity "
    "to the cumulative description) are listed below the user message — "
    "the UI shows the species list separately, so DON'T re-list them. "
    "Each candidate's `ID:` field is its eBird Identification paragraph; "
    "it includes size/shape ('tiny', 'small songbird', 'medium-sized "
    "warbler', 'large raptor', body proportions, bill shape, tail length), "
    "plumage, and behavior. USE that info — especially size and shape — "
    "to justify your top guess and distinguish close alternatives. "
    "Write a clear answer: name your top guess and the field marks that "
    "lock it in (size + one or two distinguishing marks); if the user is "
    "following up, say whether the new detail shifts the answer or "
    "confirms it; if the top two candidates are within 0.05 similarity, "
    "mention the alternative and what would distinguish them. Plain "
    "prose, no markdown, no bullet points, finish every sentence."
)


def _build_narration_contents(
    query: str,
    candidates: pd.DataFrame,
    *,
    history: list[dict] | None = None,
) -> list[types.ContentDict]:
    """Shape the Gemini `contents` payload — shared with any future one-shot call."""
    lines = []
    for _, row in candidates.head(5).iterrows():
        bits = [f"- {row['common_name']}"]
        if "sci_name" in row and row["sci_name"]:
            bits.append(f"({row['sci_name']})")
        bits.append(f"similarity={row['similarity']:.2f}")
        if "recent_count" in row and pd.notna(row.get("recent_count")):
            bits.append(f"recent_count={int(row['recent_count'])}")
        if "last_seen" in row and row.get("last_seen"):
            bits.append(f"last_seen={row['last_seen']}")
        if "in_season" in row and bool(row.get("in_season")):
            bits.append("in_season")
        id_text = row.get("ebird_id_text") or row.get("summary") or ""
        line = " ".join(bits)
        if id_text:
            # Send the full eBird Identification paragraph. Max observed is
            # ~1.5k chars; with up to 5 candidates that's ~7.5k chars in the
            # prompt — trivial against Gemini's 1M context window.
            line += f"\n  ID: {id_text}"
        lines.append(line)

    candidate_block = "Candidates:\n" + "\n".join(lines)

    # Multi-turn payload as dicts (the SDK's ContentListUnionDict accepts
    # list[dict]; passing list[Content] also works at runtime but doesn't
    # satisfy the static union). Prior history is rendered verbatim; the
    # current user message has the candidate block appended so the model can
    # reason over both.
    contents: list[types.ContentDict] = []
    for turn in history or []:
        role = "user" if turn.get("role") == "user" else "model"
        text = (turn.get("content") or "").strip()
        if not text:
            continue
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append(
        {"role": "user", "parts": [{"text": f"{query}\n\n{candidate_block}"}]}
    )
    return contents


def narrate_top_matches_stream(
    query: str,
    candidates: pd.DataFrame,
    *,
    history: list[dict] | None = None,
) -> Iterator[str]:
    """Stream a short narration over the top candidates.

    `candidates` carries: common_name, sci_name, ebird_id_text (or summary),
    similarity, and optionally recent-sighting context (recent_count,
    last_seen, in_season).

    `history` is an optional list of prior `{role, content}` turns from the
    sidebar chat. When provided, the model sees the conversation so far and
    treats `query` as the latest user message — letting follow-ups like "but
    it had a longer tail" refine the prior answer instead of restarting. The
    candidate list is always derived from the *cumulative* description (the
    caller's job), so this only affects narration style, not ranking.

    Yields nothing on setup failure (no API key, empty candidates) and stops
    cleanly on mid-stream errors, so callers keep whatever text already
    arrived. Joining all yielded chunks gives the full narration (callers
    typically `.strip()` and treat empty as "no narration available").
    """
    client = _narration_client()
    if client is None or candidates.empty:
        return

    contents = _build_narration_contents(query, candidates, history=history)

    try:
        stream = client.models.generate_content_stream(
            model=NARRATION_MODEL,
            # The SDK accepts list[ContentDict] at runtime (it's documented as
            # the multi-turn shape), but ty resolves the parameter's
            # `ContentListUnionDict` union without that arm. Suppress.
            contents=contents,  # ty: ignore[invalid-argument-type]
            config=types.GenerateContentConfig(
                temperature=0.2,
                # No max_output_tokens cap — the system prompt asks for a
                # focused answer and the model's natural stopping behavior
                # is the right ceiling. Caps only ever caused mid-sentence
                # truncation.
                system_instruction=_SYSTEM_INSTRUCTION,
            ),
        )
        for chunk in stream:
            text = getattr(chunk, "text", None) or ""
            if text:
                yield text
    except Exception:
        return
