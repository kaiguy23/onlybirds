from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv

from . import consolidate, db, enrich, hotspots, ingest, rare, seasonality, targets
from .ebird import EBirdClient
from .ingest import pick_latest_csv

load_dotenv()  # picks up .env in cwd; no-op if absent

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Find birds you haven't seen at nearby hotspots.")


@app.command()
def run(
    csv: Path = typer.Option(..., exists=True, readable=True, envvar="ONLYBIRDS_CSV", help="Path to your life-list CSV, or a directory (newest MM-DD-YYYY.csv inside is used)."),
    lat: float = typer.Option(..., envvar="ONLYBIRDS_LAT", help="Latitude of your search center."),
    lon: float = typer.Option(..., envvar="ONLYBIRDS_LON", help="Longitude of your search center."),
    radius: int = typer.Option(25, help="Hotspot search radius in km (max 50)."),
    rare_radius: int = typer.Option(50, help="Rare-bird-alert search radius in km."),
    days_back: int = typer.Option(14, help="Days of recent obs to consider for hotspots."),
    rare_days: int = typer.Option(7, help="Lookback window for rare-bird alerts."),
    consolidate_radius_km: float = typer.Option(
        consolidate.DEFAULT_RADIUS_KM,
        "--consolidate-radius-km",
        help="Cluster hotspots within this distance (km) into a single consolidated hotspot.",
    ),
    db_path: Path = typer.Option(db.DEFAULT_DB_PATH, "--db", help="SQLite path."),
    force_refresh: bool = typer.Option(False, help="Bypass hotspot/enrichment caches."),
) -> None:
    """Run the full pipeline end-to-end."""
    if csv.is_dir():
        csv = pick_latest_csv(csv)
        typer.echo(f"  using latest CSV: {csv}")
    with db.session(db_path) as conn, EBirdClient() as client:
        typer.echo(f"[1/7] ingesting {csv}…")
        s1 = ingest.ingest_csv(conn, client, csv)
        typer.echo(f"      {s1}")

        typer.echo(f"[2/7] fetching hotspots within {radius} km of ({lat}, {lon})…")
        s2 = hotspots.fetch_nearby(conn, client, lat, lon, radius_km=radius, days_back=days_back, force=force_refresh)
        typer.echo(f"      {s2}")

        typer.echo(f"[3/7] consolidating hotspots within {consolidate_radius_km} km…")
        s3 = consolidate.consolidate_hotspots(conn, radius_km=consolidate_radius_km)
        typer.echo(f"      {s3}")

        typer.echo("[4/7] sampling historical obs for seasonality…")
        s4 = seasonality.compute_seasonality(conn, client, force=force_refresh)
        typer.echo(f"      {s4}")

        typer.echo("[5/7] computing target birds (hotspot − life list)…")
        s5 = targets.compute_targets(conn)
        typer.echo(f"      {s5}")

        typer.echo(f"[6/7] flagging rare targets (last {rare_days} days within {rare_radius} km)…")
        s6 = rare.mark_rare(conn, client, lat, lon, radius_km=rare_radius, days_back=rare_days)
        typer.echo(f"      {s6}")

        typer.echo("[7/7] enriching with Wikipedia…")
        s7 = enrich.enrich_targets(conn, force=force_refresh)
        typer.echo(f"      {s7}")

    typer.echo(f"\nDone. Run `onlybirds serve --db {db_path}` to view the dashboard.")


@app.command()
def serve(
    db_path: Path = typer.Option(db.DEFAULT_DB_PATH, "--db", help="SQLite path."),
    port: int = typer.Option(8501, help="Streamlit port."),
) -> None:
    """Launch the Streamlit dashboard."""
    dashboard = Path(__file__).resolve().parent / "dashboard" / "app.py"
    if not dashboard.exists():
        typer.echo(f"dashboard not found at {dashboard}", err=True)
        raise typer.Exit(1)
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(dashboard),
        "--server.port", str(port),
        "--", "--db", str(db_path),
    ]
    raise typer.Exit(subprocess.call(cmd))


@app.command(name="consolidate")
def consolidate_cmd(
    db_path: Path = typer.Option(db.DEFAULT_DB_PATH, "--db", help="SQLite path."),
    radius_km: float = typer.Option(
        consolidate.DEFAULT_RADIUS_KM,
        "--radius-km",
        help="Cluster hotspots within this distance (km).",
    ),
) -> None:
    """Re-run hotspot consolidation against existing DB rows."""
    with db.session(db_path) as conn:
        s = consolidate.consolidate_hotspots(conn, radius_km=radius_km)
        typer.echo(f"  {s}")


@app.command()
def status(db_path: Path = typer.Option(db.DEFAULT_DB_PATH, "--db")) -> None:
    """Quick counts of what's in the local DB."""
    with db.session(db_path) as conn:
        for table in ("observations", "hotspots", "hotspot_obs", "targets", "species_info", "species_seasonality", "taxonomy", "consolidated_hotspots", "consolidated_hotspot_members"):
            n = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            typer.echo(f"  {table:<28} {n}")


if __name__ == "__main__":
    app()
