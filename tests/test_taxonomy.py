import datetime as dt
from typing import cast

from onlybirds.ebird import EBirdClient
from onlybirds.taxonomy import _is_stale, refresh_if_stale, resolve_names


class FakeClient:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls = 0

    def taxonomy(self):
        self.calls += 1
        return self.rows


def _seed_taxonomy(conn, entries: list[tuple[str, str, str]], fetched_at: str | None = None) -> None:
    fetched_at = fetched_at or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO taxonomy(species_code, common_name, sci_name, family, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(c, com, sci, "Cardinalidae", fetched_at) for c, com, sci in entries],
    )


class TestIsStale:
    def test_empty_is_stale(self, conn):
        assert _is_stale(conn) is True

    def test_recent_not_stale(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        assert _is_stale(conn) is False

    def test_old_is_stale(self, conn):
        old = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=120)).isoformat(timespec="seconds")
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")], fetched_at=old)
        assert _is_stale(conn) is True


class TestRefreshIfStale:
    def test_skips_when_fresh(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        client = FakeClient([])
        n = refresh_if_stale(conn, cast(EBirdClient, client))
        assert client.calls == 0
        assert n == 1

    def test_fetches_when_stale_and_filters_non_species(self, conn):
        client = FakeClient(
            [
                {"speciesCode": "norcar", "comName": "Northern Cardinal", "sciName": "Cardinalis cardinalis", "familyComName": "Cardinals", "category": "species"},
                {"speciesCode": "amecro", "comName": "American Crow", "sciName": "Corvus brachyrhynchos", "familyComName": "Crows", "category": "species"},
                # non-species categories should be filtered out (hybrid, slash, etc.)
                {"speciesCode": "x1", "comName": "Hybrid", "sciName": "X y", "familyComName": "?", "category": "hybrid"},
            ]
        )
        n = refresh_if_stale(conn, cast(EBirdClient, client))
        assert n == 2
        codes = {r["species_code"] for r in conn.execute("SELECT species_code FROM taxonomy")}
        assert codes == {"norcar", "amecro"}


class TestResolveNames:
    def test_exact_common_name(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        assert resolve_names(conn, ["Northern Cardinal"]) == {"Northern Cardinal": "norcar"}

    def test_case_insensitive(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        assert resolve_names(conn, ["northern cardinal", "  NORTHERN CARDINAL  "]) == {
            "northern cardinal": "norcar",
            "  NORTHERN CARDINAL  ": "norcar",
        }

    def test_scientific_name(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        assert resolve_names(conn, ["Cardinalis cardinalis"]) == {"Cardinalis cardinalis": "norcar"}

    def test_fuzzy_match(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        # Minor typo — should resolve via WRatio.
        out = resolve_names(conn, ["Northren Cardinal"])
        assert out["Northren Cardinal"] == "norcar"

    def test_unmatched_returns_none(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        assert resolve_names(conn, ["xyzabc unrelated text"]) == {"xyzabc unrelated text": None}

    def test_high_threshold_rejects_loose_match(self, conn):
        _seed_taxonomy(conn, [("norcar", "Northern Cardinal", "Cardinalis cardinalis")])
        out = resolve_names(conn, ["completely different"], min_score=99)
        assert out["completely different"] is None
