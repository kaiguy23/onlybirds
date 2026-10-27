import os
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx

LOGIN_URL = "https://ebird.org/login"
LIFELIST_URL = "https://ebird.org/lifelist"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


class LifeListError(RuntimeError):
    pass


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action") or ""
            self._in_form = True
        elif tag == "input" and self._in_form and a.get("type") == "hidden" and a.get("name"):
            self.fields[a["name"] or ""] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def _login(client: httpx.Client, username: str, password: str) -> None:
    """Sign in through Cornell's CAS form; leaves EBIRD_SESSIONID in the client's cookie jar."""
    page = client.get(LOGIN_URL)
    form = _LoginFormParser()
    form.feed(page.text)
    if form.action is None or "execution" not in form.fields:
        raise LifeListError(f"couldn't find the eBird login form at {page.url}")
    r = client.post(
        urljoin(str(page.url), form.action),
        data={**form.fields, "username": username, "password": password, "rememberMe": "on"},
    )
    if "EBIRD_SESSIONID" not in client.cookies or r.url.host != "ebird.org":
        raise LifeListError("eBird login failed; check EBIRD_USERNAME / EBIRD_PASSWORD.")


def fetch_life_list(out_dir: Path | str, timeout: float = 30.0) -> Path:
    """Download the world life list CSV to out_dir/MM-DD-YYYY.csv.

    The public eBird API has no life-list endpoint, so this uses the website's
    CSV export. Signs in with EBIRD_USERNAME/EBIRD_PASSWORD, or falls back to an
    existing EBIRD_SESSIONID browser cookie.
    """
    username = os.environ.get("EBIRD_USERNAME")
    password = os.environ.get("EBIRD_PASSWORD")
    session_id = os.environ.get("EBIRD_SESSIONID")
    if not (username and password) and not session_id:
        raise LifeListError("set EBIRD_USERNAME and EBIRD_PASSWORD (or EBIRD_SESSIONID) in .env.")

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
    ) as client:
        if username and password:
            _login(client, username, password)
        else:
            client.cookies.set("EBIRD_SESSIONID", session_id or "", domain="ebird.org")
        r = client.get(
            LIFELIST_URL,
            params={"r": "world", "time": "life", "fmt": "csv"},
            headers={"Referer": LIFELIST_URL},
        )
    if "text/csv" not in r.headers.get("content-type", ""):
        hint = "login didn't stick" if username and password else "EBIRD_SESSIONID has likely expired"
        raise LifeListError(f"eBird returned {r.status_code} ({r.headers.get('content-type')}) instead of a CSV; {hint}.")
    out = Path(out_dir) / f"{date.today():%m-%d-%Y}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return out
