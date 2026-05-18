"""Tests for rank_species against pre-seeded embeddings."""

import numpy as np

from onlybirds import embeddings, semantic_search


def _seed_with_vector(conn, code, common, ebird_text, vec):
    conn.execute(
        "INSERT INTO taxonomy(species_code, common_name, sci_name, fetched_at) VALUES (?, ?, ?, ?)",
        (code, common, "Sci name", "2025-01-01"),
    )
    conn.execute(
        "INSERT INTO targets(species_code, first_flagged, is_rare) VALUES (?, '2025-01-01', 0)",
        (code,),
    )
    norm = embeddings._normalize(np.asarray(vec, dtype=np.float32))
    conn.execute(
        "INSERT INTO species_info(species_code, fetched_at, ebird_id_text, embedding, embedding_source_hash, embedding_model) "
        "VALUES (?, '2025-01-01', ?, ?, 'fakehash', ?)",
        (code, ebird_text, norm.tobytes(), embeddings.MODEL),
    )
    conn.commit()


def test_rank_orders_by_cosine_similarity(conn, monkeypatch):
    # Two species, each as a unit vector in a different direction.
    _seed_with_vector(conn, "a", "Alpha", "alpha text", [1.0, 0.0])
    _seed_with_vector(conn, "b", "Beta", "beta text", [0.0, 1.0])
    embeddings.embed_query.cache_clear()
    monkeypatch.setattr(
        embeddings,
        "_embed_batch",
        lambda c, texts, task: [embeddings._normalize(np.asarray([0.9, 0.1], dtype=np.float32))],
    )
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    ranked = semantic_search.rank_species(conn, "query", ["a", "b"], top_k=2)
    assert list(ranked["species_code"]) == ["a", "b"]
    assert ranked.iloc[0]["similarity"] > ranked.iloc[1]["similarity"]


def test_rank_respects_candidate_pool(conn, monkeypatch):
    _seed_with_vector(conn, "a", "Alpha", "alpha", [1.0, 0.0])
    _seed_with_vector(conn, "b", "Beta", "beta", [0.0, 1.0])
    embeddings.embed_query.cache_clear()
    monkeypatch.setattr(
        embeddings,
        "_embed_batch",
        lambda c, texts, task: [embeddings._normalize(np.asarray([0.9, 0.1], dtype=np.float32))],
    )
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    # Even though "a" would rank highest globally, only "b" is in the pool.
    ranked = semantic_search.rank_species(conn, "q", ["b"], top_k=2)
    assert list(ranked["species_code"]) == ["b"]


def test_rank_empty_pool_returns_empty(conn):
    ranked = semantic_search.rank_species(conn, "q", [], top_k=5)
    assert ranked.empty
    assert list(ranked.columns) == ["species_code", "similarity"]


def test_rank_empty_query_returns_empty(conn):
    _seed_with_vector(conn, "a", "Alpha", "alpha", [1.0, 0.0])
    ranked = semantic_search.rank_species(conn, "   ", ["a"], top_k=5)
    assert ranked.empty


def test_rank_caps_at_top_k(conn, monkeypatch):
    for i in range(5):
        _seed_with_vector(conn, f"sp{i}", f"S{i}", "t", [1.0, float(i)])
    embeddings.embed_query.cache_clear()
    monkeypatch.setattr(
        embeddings,
        "_embed_batch",
        lambda c, texts, task: [embeddings._normalize(np.asarray([1.0, 0.0], dtype=np.float32))],
    )
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    ranked = semantic_search.rank_species(conn, "q", [f"sp{i}" for i in range(5)], top_k=2)
    assert len(ranked) == 2


def test_narrate_returns_none_when_no_api_key(monkeypatch):
    import pandas as pd
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    df = pd.DataFrame({"common_name": ["X"], "sci_name": ["Y"], "similarity": [0.9], "ebird_id_text": ["t"]})
    assert semantic_search.narrate_top_matches("q", df) is None
