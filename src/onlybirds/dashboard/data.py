"""Load all dashboard data from the SQLite onlybirds DB."""

from dataclasses import dataclass

import pandas as pd
import streamlit as st

from onlybirds import db


@dataclass(frozen=True, slots=True)
class DashboardData:
    targets: pd.DataFrame
    hotspots: pd.DataFrame
    hotspot_targets: pd.DataFrame
    seasonality: pd.DataFrame
    consolidated_hotspots: pd.DataFrame
    consolidated_members: pd.DataFrame
    consolidated_targets: pd.DataFrame


@st.cache_data(ttl=60)
def load_data(db_path: str) -> DashboardData:
    with db.session(db_path) as conn:
        targets = pd.read_sql_query(
            """
            SELECT t.species_code,
                   x.common_name, x.sci_name, x.family,
                   t.is_rare, t.rare_seen_at, t.rare_lat, t.rare_lon, t.rare_loc_name,
                   s.summary, s.image_url, s.wiki_url,
                   (SELECT COUNT(DISTINCT ho.hotspot_id)
                    FROM hotspot_obs ho
                    WHERE ho.species_code = t.species_code) AS hotspot_count
            FROM targets t
            JOIN taxonomy x ON x.species_code = t.species_code
            LEFT JOIN species_info s ON s.species_code = t.species_code
            ORDER BY t.is_rare DESC, x.common_name
            """,
            conn,
        )
        seasonality = pd.read_sql_query(
            """
            SELECT ss.species_code, ss.region, ss.months
            FROM species_seasonality ss
            JOIN targets t ON t.species_code = ss.species_code
            """,
            conn,
        )
        hotspots = pd.read_sql_query(
            """
            SELECT h.hotspot_id, h.name, h.lat, h.lon, h.region,
                   COUNT(DISTINCT ho.species_code) FILTER (WHERE ho.species_code IN
                       (SELECT species_code FROM targets)) AS target_count,
                   COUNT(DISTINCT ho.species_code) FILTER (WHERE ho.species_code IN
                       (SELECT species_code FROM targets WHERE is_rare = 1)) AS rare_target_count
            FROM hotspots h
            LEFT JOIN hotspot_obs ho ON ho.hotspot_id = h.hotspot_id
            GROUP BY h.hotspot_id
            """,
            conn,
        )
        hotspot_targets = pd.read_sql_query(
            """
            SELECT ho.hotspot_id, t.species_code, x.common_name, t.is_rare,
                   ho.last_seen, ho.how_many,
                   s.image_url, s.wiki_url
            FROM hotspot_obs ho
            JOIN targets t ON t.species_code = ho.species_code
            JOIN taxonomy x ON x.species_code = t.species_code
            LEFT JOIN species_info s ON s.species_code = t.species_code
            ORDER BY t.is_rare DESC, ho.last_seen DESC
            """,
            conn,
        )
        # Consolidated hotspots: dedupe targets across member hotspots.
        consolidated_hotspots = pd.read_sql_query(
            """
            SELECT ch.consolidated_id, ch.name, ch.lat, ch.lon, ch.member_count,
                   COUNT(DISTINCT t.species_code) AS target_count,
                   COUNT(DISTINCT CASE WHEN t.is_rare = 1 THEN t.species_code END) AS rare_target_count
            FROM consolidated_hotspots ch
            LEFT JOIN consolidated_hotspot_members chm
                   ON chm.consolidated_id = ch.consolidated_id
            LEFT JOIN hotspot_obs ho ON ho.hotspot_id = chm.hotspot_id
            LEFT JOIN targets t ON t.species_code = ho.species_code
            GROUP BY ch.consolidated_id
            """,
            conn,
        )
        consolidated_members = pd.read_sql_query(
            """
            SELECT chm.consolidated_id, h.hotspot_id, h.name, h.lat, h.lon, h.region
            FROM consolidated_hotspot_members chm
            JOIN hotspots h ON h.hotspot_id = chm.hotspot_id
            """,
            conn,
        )
        # Attach a region to each consolidated hotspot — the most common region
        # among its members (members are within ~1.5 km, so usually unanimous).
        if not consolidated_hotspots.empty and not consolidated_members.empty:
            region_per_cid = (
                consolidated_members.groupby("consolidated_id")["region"]
                .agg(
                    lambda s: s.dropna().mode().iat[0]
                    if not s.dropna().empty
                    else None
                )
            )
            consolidated_hotspots["region"] = consolidated_hotspots[
                "consolidated_id"
            ].map(region_per_cid)
        else:
            consolidated_hotspots["region"] = None
        # MAX(last_seen) is fine for ISO-formatted strings; same for how_many.
        consolidated_targets = pd.read_sql_query(
            """
            SELECT chm.consolidated_id, t.species_code, x.common_name, t.is_rare,
                   MAX(ho.last_seen) AS last_seen,
                   MAX(ho.how_many)  AS how_many,
                   s.image_url, s.wiki_url
            FROM consolidated_hotspot_members chm
            JOIN hotspot_obs ho ON ho.hotspot_id = chm.hotspot_id
            JOIN targets t ON t.species_code = ho.species_code
            JOIN taxonomy x ON x.species_code = t.species_code
            LEFT JOIN species_info s ON s.species_code = t.species_code
            GROUP BY chm.consolidated_id, t.species_code
            ORDER BY t.is_rare DESC, last_seen DESC
            """,
            conn,
        )
    return DashboardData(
        targets=targets,
        hotspots=hotspots,
        hotspot_targets=hotspot_targets,
        seasonality=seasonality,
        consolidated_hotspots=consolidated_hotspots,
        consolidated_members=consolidated_members,
        consolidated_targets=consolidated_targets,
    )


@st.cache_data(ttl=60)
def load_hotspot_all_species(db_path: str, hotspot_id: str) -> pd.DataFrame:
    """Every species observed at a hotspot, with taxonomy + species_info joined.

    Unlike `hotspot_targets`, this is not filtered by the targets table — seen
    species are included. `is_rare` is coalesced to 0 for non-target species.
    """
    with db.session(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT ho.species_code,
                   x.common_name, x.sci_name, x.family,
                   COALESCE(t.is_rare, 0) AS is_rare,
                   (t.species_code IS NOT NULL) AS is_target,
                   t.rare_seen_at, t.rare_lat, t.rare_lon, t.rare_loc_name,
                   s.summary, s.image_url, s.wiki_url,
                   (SELECT COUNT(DISTINCT ho2.hotspot_id)
                    FROM hotspot_obs ho2
                    WHERE ho2.species_code = ho.species_code) AS hotspot_count,
                   ho.last_seen, ho.how_many
            FROM hotspot_obs ho
            JOIN taxonomy x ON x.species_code = ho.species_code
            LEFT JOIN targets t ON t.species_code = ho.species_code
            LEFT JOIN species_info s ON s.species_code = ho.species_code
            WHERE ho.hotspot_id = ?
            ORDER BY is_rare DESC, ho.last_seen DESC
            """,
            conn,
            params=[hotspot_id],
        )


@st.cache_data(ttl=60)
def load_consolidated_all_species(
    db_path: str, consolidated_id: str
) -> pd.DataFrame:
    """Every species observed across a consolidated hotspot's members, deduped."""
    with db.session(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT ho.species_code,
                   x.common_name, x.sci_name, x.family,
                   COALESCE(t.is_rare, 0) AS is_rare,
                   (t.species_code IS NOT NULL) AS is_target,
                   t.rare_seen_at, t.rare_lat, t.rare_lon, t.rare_loc_name,
                   s.summary, s.image_url, s.wiki_url,
                   (SELECT COUNT(DISTINCT ho2.hotspot_id)
                    FROM hotspot_obs ho2
                    WHERE ho2.species_code = ho.species_code) AS hotspot_count,
                   MAX(ho.last_seen) AS last_seen,
                   MAX(ho.how_many)  AS how_many
            FROM consolidated_hotspot_members chm
            JOIN hotspot_obs ho ON ho.hotspot_id = chm.hotspot_id
            JOIN taxonomy x ON x.species_code = ho.species_code
            LEFT JOIN targets t ON t.species_code = ho.species_code
            LEFT JOIN species_info s ON s.species_code = ho.species_code
            WHERE chm.consolidated_id = ?
            GROUP BY ho.species_code
            ORDER BY is_rare DESC, last_seen DESC
            """,
            conn,
            params=[consolidated_id],
        )
