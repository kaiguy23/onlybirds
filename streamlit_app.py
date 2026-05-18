"""Entry point for Streamlit Community Cloud.

Cloud's "Main file path" points here; it re-exports the dashboard's `main()`.
"""

from dotenv import load_dotenv

from onlybirds.dashboard.app import main

# Pick up GEMINI_API_KEY (and friends) from a project `.env` when present.
# Streamlit Cloud sets secrets via its own env mechanism; load_dotenv is a no-op
# there.
load_dotenv()

main()
