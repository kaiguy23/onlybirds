"""Sidebar chat widget for "describe the bird" semantic re-ranking.

The chat lives in `st.sidebar` so it's visible on every view (Map, Target
list, Compare, Hotspot detail, Consolidated detail). Each user turn adds to a
running description; the cumulative description is embedded and used to
re-rank the species frame the current view passes in.

State shape (single key in `st.session_state`):

    "semantic_chat": [
        {"role": "user", "content": "small gray bird with black cap"},
        {"role": "assistant", "content": "Top guess: Mountain Chickadee…"},
        {"role": "user", "content": "but the tail was longer"},
        ...
    ]

A `pending` flag is set when a user submits a new turn; the next call to
`apply_semantic_search(df)` from a view consumes it, computes ranking +
narration, appends the assistant turn, and triggers a rerun so the transcript
catches up. The widget is a no-op when GEMINI_API_KEY is unset.
"""

import html
import os

import pandas as pd
import streamlit as st

from onlybirds import db
from onlybirds.dashboard.urls import _consolidated_url, _hotspot_url
from onlybirds.dashboard.utils import _ebird_species_url
from onlybirds.semantic_search import narrate_top_matches_stream, rank_species

DEFAULT_TOP_K = 10

# Session-state keys. The chat is one list of {role, content} dicts; `pending`
# signals that the latest user turn still needs ranking+narration.
CHAT_KEY = "semantic_chat"
PENDING_KEY = "semantic_chat_pending"
INPUT_KEY = "semantic_chat_input"
# Bridges the streaming placeholder from `render_sidebar_input` (where it's
# created) to `apply_semantic_search` (which runs later in the same script
# pass and streams tokens into it).
STREAM_PH_KEY = "_semantic_chat_stream_ph"

# Quick-debug prompts — one-click submissions so you can shake the chat
# end-to-end without typing. Order is rough field-mark progression: solid
# canonical descriptions first, then ambiguous follow-up-style fragments to
# stress the multi-turn refinement path.
EXAMPLE_PROMPTS: list[tuple[str, str]] = [
    ("🪶 Chickadee", "small gray bird with black cap and white cheeks"),
    ("🐦 Hummingbird", "tiny iridescent green bird hovering with pink throat"),
    ("🦅 Bald eagle", "large soaring raptor with white head and tail"),
    ("🦆 Mallard drake", "dabbling duck with green head and yellow bill"),
    ("➕ longer tail", "longer tail than I first thought"),
    ("➕ near water", "near water, in reeds"),
]


_CHAT_CSS = """
<style>
/* Sidebar width — default ~244px squeezes long narrations into a thin column.
   Scope to the expanded state (aria-expanded='true') so the override doesn't
   fight Streamlit's collapse animation; without this, clicking « leaves the
   sidebar visible AND Streamlit also renders its » expand button. */
section[data-testid='stSidebar'][aria-expanded='true']{min-width:360px !important; max-width:420px !important;}

/* Chat layout. Rows flex left/right by role; bubbles cap at 85% so the
   asymmetry reads as a conversation rather than a list of stacked blocks. */
.ob-chat {display:flex; flex-direction:column; gap:4px; margin:6px 0 10px 0;}
.ob-chat-row {display:flex; width:100%;}
.ob-chat-row--user {justify-content:flex-end;}
.ob-chat-row--assistant {justify-content:flex-start;}

.ob-chat-bubble {
    max-width:85%;
    padding:8px 12px;
    border-radius:14px;
    line-height:1.4;
    font-size:0.92em;
    word-wrap:break-word;
    overflow-wrap:anywhere;
}
.ob-chat-bubble p {margin:0 0 4px 0;}
.ob-chat-bubble p:last-child {margin-bottom:0;}

/* User: iMessage blue, right-aligned, tail on bottom-right. */
.ob-chat-bubble--user {
    background:#2563eb;
    color:#ffffff;
    border-bottom-right-radius:4px;
}
.ob-chat-bubble--user a {color:#ffffff; text-decoration:underline;}

/* Assistant: light gray, left-aligned, tail on bottom-left. */
.ob-chat-bubble--assistant {
    background:#f1f3f5;
    color:#1f2937;
    border:1px solid #e5e7eb;
    border-bottom-left-radius:4px;
}
.ob-chat-bubble--assistant a {color:#1f4f99; text-decoration:none;}
.ob-chat-bubble--assistant a:hover {text-decoration:underline;}

/* Matches list inside an assistant bubble. */
.ob-matches {margin:8px 0 0 0; padding-left:18px; font-size:0.92em;}
.ob-matches li {margin:3px 0;}
.ob-matches .sim {color:#888; font-size:0.85em; margin-left:4px;}
.ob-matches .places {color:#666; font-size:0.85em; display:block; margin-top:2px;}
.ob-matches .places a {color:#1f4f99;}

/* Typing indicator while a turn is pending. */
.ob-typing {display:inline-block; opacity:0.6; font-style:italic;}
</style>
"""


def _gemini_ready() -> bool:
    return bool((os.environ.get("GEMINI_API_KEY") or "").strip())


def _matches_html(matches: list[dict]) -> str:
    """Build the inner `<ul>` for the matches block of an assistant bubble."""
    if not matches:
        return ""
    items: list[str] = []
    for m in matches:
        name = html.escape(str(m.get("common_name") or m.get("species_code") or "?"))
        sim = float(m.get("similarity") or 0.0)
        ebird = _ebird_species_url(m.get("species_code")) or ""
        name_html = (
            f"<a href='{html.escape(ebird)}' target='_blank' rel='noopener'>{name}</a>"
            if ebird
            else f"<strong>{name}</strong>"
        )
        place_links: list[str] = []
        for p in (m.get("places") or [])[:4]:
            pid = p.get("id")
            ptype = p.get("type")
            pname = p.get("name") or pid or ""
            if not pid or not ptype:
                continue
            # Streamlit clears session_state on full page reloads, and an
            # in-page anchor click with a new ?hotspot= URL is exactly that —
            # so a `_self` link would wipe the chat the moment the user
            # explored a place. Open in a new tab so the conversation stays
            # available; the user can compare the location against their
            # description without losing context.
            url = _consolidated_url(pid) if ptype == "area" else _hotspot_url(pid)
            label = (pname[:24] + "…") if len(pname) > 25 else pname
            place_links.append(
                f"<a href='{html.escape(url)}' target='_blank' "
                f"rel='noopener'>{html.escape(label)}</a>"
            )
        places_html = (
            f"<span class='places'>{' · '.join(place_links)}</span>"
            if place_links
            else ""
        )
        items.append(
            f"<li>{name_html}<span class='sim'>{sim:.2f}</span>{places_html}</li>"
        )
    return f"<ul class='ob-matches'>{''.join(items)}</ul>"


def _chat_row(side: str, inner: str) -> str:
    return (
        f"<div class='ob-chat-row ob-chat-row--{side}'>"
        f"<div class='ob-chat-bubble ob-chat-bubble--{side}'>{inner}</div>"
        f"</div>"
    )


def _streaming_bubble_html(text: str) -> str:
    """In-progress assistant bubble; empty `text` shows 'Thinking…'."""
    if text:
        body = html.escape(text).replace("\n", "<br>")
        inner = f"<p>{body}</p>"
    else:
        inner = "<span class='ob-typing'>Thinking…</span>"
    # Wrapped in its own `.ob-chat` so spacing matches the committed transcript
    # above; on rerun the placeholder is gone and the finalized message renders
    # inside that main transcript blob.
    return f"<div class='ob-chat'>{_chat_row('assistant', inner)}</div>"


def _message_html(msg: dict) -> str:
    role = msg.get("role", "user")
    content = html.escape(msg.get("content") or "").replace("\n", "<br>")
    side = "user" if role == "user" else "assistant"
    inner = f"<p>{content}</p>"
    if role == "assistant" and msg.get("matches"):
        inner += _matches_html(msg["matches"])
    return _chat_row(side, inner)


def _chat() -> list[dict]:
    return st.session_state.setdefault(CHAT_KEY, [])


def current_description() -> str:
    """Collapse the user turns of the chat into one cumulative description.

    Each user message becomes a sentence in the same paragraph. This is what
    we embed for ranking — embeddings prefer coherent descriptive text over
    a single recent fragment, so synthesizing across turns gives more stable
    rankings than re-embedding the latest message alone.
    """
    user_msgs = [m["content"].strip() for m in _chat() if m.get("role") == "user"]
    user_msgs = [m for m in user_msgs if m]
    if not user_msgs:
        return ""
    # Join with " — " so Gemini's embedder treats the bits as related clauses
    # rather than unrelated sentences. (Empirically the score gap between
    # right and wrong species widens slightly vs. a plain space join.)
    return " — ".join(user_msgs)


def _on_submit() -> None:
    """`st.text_input` on_change callback: append user turn, flag pending, clear input."""
    raw = (st.session_state.get(INPUT_KEY) or "").strip()
    if not raw:
        return
    _chat().append({"role": "user", "content": raw})
    st.session_state[PENDING_KEY] = True
    st.session_state[INPUT_KEY] = ""


def _on_reset() -> None:
    """Clear chat history and the pending flag."""
    st.session_state[CHAT_KEY] = []
    st.session_state[PENDING_KEY] = False


def _send_example(prompt: str) -> None:
    """Inject `prompt` as a user turn (same path as a real Enter submission)."""
    _chat().append({"role": "user", "content": prompt})
    st.session_state[PENDING_KEY] = True


def _chat_is_active() -> bool:
    """The chat only re-ranks meaningful when the species corpus is narrowed.

    Top-level Map / Target list with no region selected sees the full DB, where
    'small gray bird' matches half the corpus. Region-filtered list views and
    the detail/compare views are all narrowed by construction, so the chat is
    meaningful there. Drive the gate off URL params so we don't have to thread
    a flag through every view.
    """
    qp = st.query_params
    if qp.get("view") == "compare":
        return True
    if qp.get("hotspot") or qp.get("consolidated"):
        return True
    return bool((qp.get("region") or "").strip())


def render_sidebar_input() -> None:
    """Render the persistent chat in `st.sidebar` (transcript + input + clear).

    Call once from `main()` before routing — the sidebar is always visible, so
    the user can refine their description without losing place in a detail view.
    """
    if not _gemini_ready():
        return

    # Sidebar width + custom chat-bubble styling. We render messages as plain
    # HTML divs (not `st.chat_message`) so we can control alignment and color
    # per role: assistant bubbles sit on the left in light gray, user bubbles
    # on the right in iMessage blue. The default streamlit chat widget stacks
    # both on the left with avatar icons, which doesn't read as a chat.
    st.markdown(_CHAT_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("### 🔎 Describe the bird")

        if not _chat_is_active():
            # Hint keeps the feature discoverable; the input itself is
            # suppressed because typing without a narrowed corpus is a
            # confusing dead end.
            st.caption(
                "Pick a region (or open a hotspot / consolidated area) to "
                "narrow the species pool, then describe what you saw here."
            )
            return

        st.caption(
            "Type what you saw. Follow-ups refine the description — "
            "e.g. 'longer tail' or 'near water'. The current view's species "
            "list is re-ranked by similarity to the cumulative description."
        )

        # Quick-debug examples — one click submits the prompt as a user turn.
        # Useful for shaking the chat end-to-end without typing, and as a
        # default starting point for new users.
        with st.expander("Examples (one-click)", expanded=not _chat()):
            for i in range(0, len(EXAMPLE_PROMPTS), 2):
                row = st.columns(2)
                for col, (label, prompt) in zip(
                    row, EXAMPLE_PROMPTS[i : i + 2], strict=False
                ):
                    col.button(
                        label,
                        key=f"{CHAT_KEY}_ex_{i}_{label}",
                        on_click=_send_example,
                        args=(prompt,),
                        use_container_width=True,
                        help=prompt,
                    )

        # Transcript — render the entire conversation as one HTML block. This
        # gives us full control over alignment and color (assistant left/gray,
        # user right/blue) instead of streamlit's default avatar-left-stacked
        # layout. Each assistant message inlines its matches snapshot directly
        # inside the bubble.
        rows_html = "".join(_message_html(m) for m in _chat())
        if rows_html:
            st.markdown(f"<div class='ob-chat'>{rows_html}</div>", unsafe_allow_html=True)

        # In-progress bubble. The placeholder is created here but written into
        # from `apply_semantic_search` (which runs later in the same script
        # pass) so tokens can stream in as they arrive from Gemini.
        if st.session_state.get(PENDING_KEY):
            ph = st.empty()
            st.session_state[STREAM_PH_KEY] = ph
            ph.markdown(_streaming_bubble_html(""), unsafe_allow_html=True)

        # Input + clear. `on_change` on a text_input fires when the user hits
        # Enter; we use a fresh key after submit (cleared in _on_submit) so
        # the box empties. st.chat_input would be tidier but isn't supported
        # inside st.sidebar across all Streamlit versions — text_input is.
        st.text_input(
            "Your description",
            placeholder="e.g. small gray bird with black cap and white eyebrow…",
            key=INPUT_KEY,
            label_visibility="collapsed",
            on_change=_on_submit,
        )
        cols = st.columns(2)
        cols[0].button(
            "Clear chat",
            key=f"{CHAT_KEY}_clear",
            disabled=not _chat(),
            on_click=_on_reset,
            use_container_width=True,
        )
        cols[1].caption(f"{len([m for m in _chat() if m['role']=='user'])} turn(s)")


def _build_matches_snapshot(
    merged: pd.DataFrame,
    places_by_code: dict[str, list[dict]] | None,
    limit: int = 5,
) -> list[dict]:
    """Freeze the top-N rows into the JSON-able shape we attach to the bubble.

    The snapshot is rendered later inside the assistant chat bubble, so it
    must carry everything the renderer needs — species code, display name,
    similarity score, optional wiki URL, and optional places (hotspot/area
    refs). Capturing this at generation time means switching views doesn't
    mutate a past assistant message.
    """
    out: list[dict] = []
    for _, row in merged.head(limit).iterrows():
        code = row["species_code"]
        match = {
            "species_code": code,
            "common_name": row.get("common_name") or code,
            "similarity": float(row.get("similarity") or 0.0),
        }
        if places_by_code and code in places_by_code:
            match["places"] = places_by_code[code]
        out.append(match)
    return out


def apply_semantic_search(
    df: pd.DataFrame,
    *,
    db_path: str,
    top_k: int = DEFAULT_TOP_K,
    narrate: bool = True,
    places_by_code: dict[str, list[dict]] | None = None,
) -> pd.DataFrame:
    """Re-rank `df` against the cumulative chat description.

    When the chat is empty (or Gemini isn't configured), the input df is
    returned unchanged. Otherwise the top-K rows by cosine similarity are
    returned with a `similarity` column attached.

    If a user turn is pending a response, this call also generates the Gemini
    narration (with the full chat history as context), snapshots the top-K
    matches onto the assistant message (including `places_by_code` links if
    provided by the view), clears the pending flag, and triggers a rerun so
    the sidebar repaints. Subsequent views in the same run see the same
    ranked df without re-narrating or re-snapshotting.
    """
    if not _gemini_ready():
        return df
    if df.empty or "species_code" not in df.columns:
        return df
    description = current_description()
    if not description:
        return df

    conn = db.connect(db_path)
    try:
        ranked = rank_species(
            conn, description, df["species_code"].tolist(), top_k=top_k
        )
        if ranked.empty:
            if st.session_state.get(PENDING_KEY):
                _chat().append(
                    {
                        "role": "assistant",
                        "content": "No species in this view have an embedding "
                        "yet — run `just latest` to enrich.",
                    }
                )
                st.session_state[PENDING_KEY] = False
                st.rerun()
            return df

        merged = ranked.merge(df, on="species_code", how="left")

        # Only call the narration model when there's a fresh user turn waiting
        # on a response. On subsequent reruns (e.g. user switches views), the
        # ranking still updates against the cumulative description but we
        # reuse the prior assistant message — no extra Gemini calls.
        if narrate and st.session_state.get(PENDING_KEY):
            chat = _chat()
            # History = everything *before* the latest user turn; the latest
            # turn is passed as `query` so the model knows what's "new".
            history = chat[:-1] if chat and chat[-1].get("role") == "user" else chat
            latest_user = (
                chat[-1]["content"]
                if chat and chat[-1].get("role") == "user"
                else description
            )

            # Stream into the sidebar placeholder set up by `render_sidebar_input`.
            # If it's missing (caller forgot to render the sidebar, or the
            # script flow changed), fall back to accumulating silently — the
            # final message still lands in the transcript on rerun.
            ph = st.session_state.pop(STREAM_PH_KEY, None)
            accumulated = ""
            for chunk in narrate_top_matches_stream(
                latest_user, merged, history=history
            ):
                accumulated += chunk
                if ph is not None:
                    ph.markdown(
                        _streaming_bubble_html(accumulated),
                        unsafe_allow_html=True,
                    )

            narration = accumulated.strip() or None
            matches = _build_matches_snapshot(merged, places_by_code)
            _chat().append(
                {
                    "role": "assistant",
                    "content": narration
                    or "(no narration — see matches below)",
                    "matches": matches,
                }
            )
            st.session_state[PENDING_KEY] = False
            # Rerun so the streaming placeholder collapses into the committed
            # transcript blob (otherwise the bubble would briefly render twice).
            st.rerun()

        return merged
    finally:
        conn.close()


# Back-compat shim — existing call sites pass `key_prefix=…` from the old
# per-view input API. The arg is ignored now that the input is shared in the
# sidebar; new code should call `apply_semantic_search` directly.
def render_semantic_search(
    df: pd.DataFrame,
    *,
    key_prefix: str,  # noqa: ARG001 — kept for back-compat with existing callers
    db_path: str,
    top_k: int = DEFAULT_TOP_K,
    narrate: bool = True,
) -> pd.DataFrame:
    return apply_semantic_search(df, db_path=db_path, top_k=top_k, narrate=narrate)
