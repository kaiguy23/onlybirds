"""Entry point for Streamlit Community Cloud.

Cloud's "Main file path" points here; it re-exports the dashboard's `main()`.
"""

import onlybirds.dashboard.app  # noqa: F401 — importing runs main() via its else-branch
