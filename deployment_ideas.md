# Deployment notes

Options for making the dashboard viewable beyond `localhost:8501`. Ranked by setup effort, lightest first.

## 1. Tailscale (recommended for solo use)

Access from your iPhone (or any device) over a private tailnet. ~10 min setup. Laptop must be on.

1. Sign up for free Tailscale account (Google/GitHub login).
2. On the Mac: `brew install --cask tailscale`, then log in.
3. Install Tailscale iOS app, log in with the same account.
4. Run `just serve` on the Mac.
5. From the iPhone (anywhere with internet): `http://your-macbook:8501` — replace with your Mac's tailnet name (visible in the Tailscale menu bar app).

No public URL, no Funnel needed. Funnel is only required if you want to share with people *not* on your tailnet.

## 2. Streamlit Community Cloud, single-user

Always-on hosted version. Your laptop can be off. ~30 min setup.

- Push this repo to a public GitHub repo.
- Deploy via [share.streamlit.io](https://share.streamlit.io).
- Put `EBIRD_API_KEY` and `ONLYBIRDS_CONTACT` in Streamlit secrets (TOML in the dashboard).
- Commit your CSV to `life-lists/` (or upload through a Streamlit file picker each visit).
- Manually re-run the pipeline locally and push when you want fresh data.

Caveats:
- Free tier requires a **public** repo, so your life list is technically public.
- Apps sleep after ~7 days of inactivity (~30s cold start to wake).
- 1 GB RAM per app — fine given the SQLite TTLs in this project.

## 3. GitHub Actions cron + Streamlit Cloud

Hands-off "always fresh, always on". ~1–2 hours setup.

- Same Streamlit Cloud deploy as option 2.
- Add a GitHub Actions workflow that runs the pipeline on a nightly cron, commits the updated `data/onlybirds.db`, and pushes.
- Streamlit Cloud auto-redeploys on push, so the dashboard always has fresh data.
- eBird key lives in GitHub Actions secrets, not needed at view time.

Same public-repo caveat as option 2.

## 4. Option 3 + Streamlit viewer auth

Same as option 3, but the dashboard URL itself is gated behind Google SSO so only whitelisted emails can load it. ~10 min on top of option 3.

- In the Streamlit Cloud app settings, set the app to **private** and add your Google email under "Viewers".
- Visiting the URL now redirects to a Google login; non-whitelisted accounts get a 403.
- No code changes — it's a hosting-side toggle.

What this protects vs. doesn't:
- ✅ Dashboard UI is not crawlable; nobody who stumbles on the URL can see your data through the app.
- ❌ The repo is still public, so `data/onlybirds.db` and `life-lists/*.csv` are readable directly on GitHub by anyone who finds the repo. The auth wall is on the *dashboard*, not the *files*.

If that gap matters, two ways to close it:
- **External-host the data**: keep the repo public for code, but move the DB/CSV to private S3/R2/GCS and have the dashboard fetch them at runtime using credentials from Streamlit secrets. The cron workflow writes to the bucket instead of committing.
- **Private repo**: simplest conceptually, but Streamlit Community Cloud's free tier historically doesn't deploy from private repos — confirm current pricing before counting on this.

For a personal life list the threat model is usually "don't want the dashboard discoverable", which option 4 alone solves. Only escalate to external hosting if you specifically don't want the raw data findable on GitHub.

## Multi-user "real product" path (out of scope for now)

If this ever becomes a public site:

- eBird API key is "personal use" — multi-user means BYO key per user, or contacting Cornell for a partnership key.
- Need user accounts to scope CSV uploads and API keys.
- Hosting cost grows with Wikipedia enrichment + per-user historic sampling.
- No public eBird endpoint for personal data — every user has to download and upload their own CSV (same friction as e.g. budget apps with bank exports).

Not worth it unless validation shows real demand.
