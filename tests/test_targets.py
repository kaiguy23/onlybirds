from onlybirds.targets import compute_targets


def _insert_observation(conn, code: str) -> None:
    conn.execute(
        "INSERT INTO observations(species_code, observed_on, lat, lon, location, source_row) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (code, "2026-01-01", 40.0, -73.0, "home", 0),
    )


def _insert_hotspot_obs(conn, hotspot_id: str, code: str) -> None:
    # hotspot_obs has PK (hotspot_id, species_code) — ensure hotspot row exists first.
    conn.execute(
        "INSERT OR IGNORE INTO hotspots(hotspot_id, name, lat, lon, region, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (hotspot_id, hotspot_id, 40.0, -73.0, "US-CA", "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO hotspot_obs(hotspot_id, species_code, last_seen, how_many, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (hotspot_id, code, "2026-01-01", 1, "2026-01-01T00:00:00"),
    )


class TestComputeTargets:
    def test_no_hotspot_obs(self, conn):
        stats = compute_targets(conn)
        assert stats == {"targets": 0}

    def test_excludes_already_seen(self, conn):
        _insert_observation(conn, "norcar")
        _insert_hotspot_obs(conn, "L1", "norcar")
        _insert_hotspot_obs(conn, "L1", "amecro")
        stats = compute_targets(conn)
        assert stats == {"targets": 1}
        codes = {r["species_code"] for r in conn.execute("SELECT species_code FROM targets")}
        assert codes == {"amecro"}

    def test_distinct_across_hotspots(self, conn):
        # Same target species seen at multiple hotspots → still one target row.
        _insert_hotspot_obs(conn, "L1", "blujay")
        _insert_hotspot_obs(conn, "L2", "blujay")
        stats = compute_targets(conn)
        assert stats == {"targets": 1}

    def test_resets_on_each_run(self, conn):
        _insert_hotspot_obs(conn, "L1", "blujay")
        compute_targets(conn)
        # Now the user has logged blujay — next run should drop it.
        _insert_observation(conn, "blujay")
        stats = compute_targets(conn)
        assert stats == {"targets": 0}
        assert conn.execute("SELECT COUNT(*) AS c FROM targets").fetchone()["c"] == 0

    def test_is_rare_defaults_to_zero(self, conn):
        _insert_hotspot_obs(conn, "L1", "blujay")
        compute_targets(conn)
        row = conn.execute("SELECT is_rare FROM targets WHERE species_code = 'blujay'").fetchone()
        assert row["is_rare"] == 0

    def test_keep_rare_survives_a_lifelist_sync(self, conn):
        _insert_hotspot_obs(conn, "L1", "amecro")
        _insert_hotspot_obs(conn, "L1", "vermfly")
        compute_targets(conn)
        conn.execute(
            "UPDATE targets SET is_rare = 1, rare_seen_at = '2026-10-20', rare_loc_name = 'Pond' "
            "WHERE species_code IN ('amecro', 'vermfly')"
        )
        _insert_observation(conn, "vermfly")  # seen since the last run

        compute_targets(conn, keep_rare=True)
        rows = {r["species_code"]: r for r in conn.execute("SELECT * FROM targets")}
        assert set(rows) == {"amecro"}
        assert rows["amecro"]["is_rare"] == 1
        assert rows["amecro"]["rare_loc_name"] == "Pond"

    def test_full_run_resets_rare(self, conn):
        _insert_hotspot_obs(conn, "L1", "amecro")
        compute_targets(conn)
        conn.execute("UPDATE targets SET is_rare = 1")
        compute_targets(conn)
        assert conn.execute("SELECT is_rare FROM targets").fetchone()["is_rare"] == 0
