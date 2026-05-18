"""Tests for embedding compute + load. Gemini client is monkeypatched."""

import numpy as np

from onlybirds import embeddings


def _fake_embed_batch(vectors_by_text: dict[str, list[float]]):
    """Build a stand-in for `_embed_batch` that returns deterministic vectors."""

    def _fn(client, texts, task_type):
        return [embeddings._normalize(np.asarray(vectors_by_text[t], dtype=np.float32)) for t in texts]

    return _fn


def _seed(conn, code, common, ebird_text=None, summary=None):
    conn.execute(
        "INSERT INTO taxonomy(species_code, common_name, sci_name, fetched_at) VALUES (?, ?, ?, ?)",
        (code, common, "Sci name", "2025-01-01"),
    )
    conn.execute(
        "INSERT INTO targets(species_code, first_flagged, is_rare) VALUES (?, '2025-01-01', 0)",
        (code,),
    )
    if ebird_text is not None or summary is not None:
        conn.execute(
            "INSERT INTO species_info(species_code, fetched_at, ebird_id_text, summary) "
            "VALUES (?, '2025-01-01', ?, ?)",
            (code, ebird_text, summary),
        )
    conn.commit()


def test_source_text_prefers_ebird_over_summary():
    text = embeddings._source_text("Robin", "ebird text", "wiki text")
    assert text == "Robin: ebird text"


def test_source_text_falls_back_to_summary():
    text = embeddings._source_text("Robin", None, "wiki text")
    assert text == "Robin: wiki text"


def test_source_text_returns_none_when_no_text():
    assert embeddings._source_text("Robin", None, None) is None
    assert embeddings._source_text("Robin", "", "") is None


def test_recompute_skips_when_hash_matches(conn, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _seed(conn, "a", "Alpha", ebird_text="alpha text")
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    monkeypatch.setattr(embeddings, "_embed_batch", _fake_embed_batch({"Alpha: alpha text": [1.0, 0.0, 0.0]}))

    s1 = embeddings.recompute_embeddings(conn)
    assert s1["updated"] == 1
    s2 = embeddings.recompute_embeddings(conn)
    assert s2["updated"] == 0
    assert s2["skipped_unchanged"] == 1


def test_recompute_re_embeds_when_text_changes(conn, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _seed(conn, "a", "Alpha", ebird_text="first")
    vectors = {"Alpha: first": [1.0, 0.0, 0.0], "Alpha: second": [0.0, 1.0, 0.0]}
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    monkeypatch.setattr(embeddings, "_embed_batch", _fake_embed_batch(vectors))
    embeddings.recompute_embeddings(conn)

    conn.execute("UPDATE species_info SET ebird_id_text='second' WHERE species_code='a'")
    conn.commit()
    s = embeddings.recompute_embeddings(conn)
    assert s["updated"] == 1
    assert s["skipped_unchanged"] == 0


def test_stored_vectors_are_normalized(conn, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _seed(conn, "a", "Alpha", ebird_text="x")
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    monkeypatch.setattr(embeddings, "_embed_batch", _fake_embed_batch({"Alpha: x": [3.0, 4.0, 0.0]}))  # |v|=5
    embeddings.recompute_embeddings(conn)
    row = conn.execute("SELECT embedding FROM species_info WHERE species_code='a'").fetchone()
    vec = np.frombuffer(row["embedding"], dtype=np.float32)
    assert vec.shape == (3,)
    np.testing.assert_allclose(vec, [0.6, 0.8, 0.0], atol=1e-6)


def test_load_embeddings_returns_matrix_in_query_order(conn, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _seed(conn, "a", "Alpha", ebird_text="alpha")
    _seed(conn, "b", "Beta", ebird_text="beta")
    monkeypatch.setattr(embeddings, "_client", lambda: None)
    monkeypatch.setattr(embeddings, "_embed_batch", _fake_embed_batch({
        "Alpha: alpha": [1.0, 0.0, 0.0], "Beta: beta": [0.0, 1.0, 0.0],
    }))
    embeddings.recompute_embeddings(conn)
    codes, mat = embeddings.load_embeddings(conn, ["a", "b", "missing"])
    assert set(codes) == {"a", "b"}
    assert mat.shape == (2, 3)


def test_load_embeddings_empty_when_no_embeddings(conn):
    codes, mat = embeddings.load_embeddings(conn, [])
    assert codes == []
    assert mat.shape == (0, embeddings.DIM)
