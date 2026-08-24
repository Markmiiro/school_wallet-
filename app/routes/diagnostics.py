# ================================================
# app/routes/diagnostics.py
# ------------------------------------------------
# One read-only endpoint answering a single question that was previously
# unanswerable from outside the process:
#
#   "Is this deployment actually going to talk to Yo Uganda?"
#
# That question has bitten twice. YO_LIVE=true was set in Railway while
# the code reading it was not yet deployed, so the variable was inert and
# nothing observable said so. Before that, APP_ENV=development meant every
# payout returned a fake SUCCEEDED with no network call, and the payouts
# table recorded "sent" either way — see the delivery-marker contract at
# the top of app/momo.py.
#
# WHAT THIS DELIBERATELY DOES NOT RETURN:
#   - YO_USERNAME, YO_PASSWORD, YO_PRIVATE_KEY, or any other credential
#   - the YO_API_URL path, query, or userinfo — scheme + host ONLY, built
#     from urlsplit().hostname rather than .netloc, because netloc carries
#     any user:password@ embedded in the URL and would leak it verbatim
#   - anything writable: no state is read from or written to the database
# ================================================

from urllib.parse import urlsplit

from fastapi import APIRouter

from app import momo

router = APIRouter()


def _safe_host(raw_url: str) -> str:
    """
    Reduce a URL to "scheme://host", dropping path, query, port and any
    embedded credentials. Returns "" when the URL is unset or unparseable —
    never the raw string, which is the whole point.
    """
    if not raw_url:
        return ""
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return "<unparseable>"
    if not parts.scheme or not parts.hostname:
        return "<unparseable>"
    return f"{parts.scheme}://{parts.hostname}"


@router.get("/yo")
def yo_mode():
    """
    Whether Yo calls leave this process, and where they would go.

    test_mode=true means every Yo call returns a fabricated response with
    no network request. A payout recorded "sent" in that state is not
    evidence that any money moved.
    """
    return {
        "test_mode":        momo._is_test_mode(),
        "yo_live":          momo.YO_LIVE,
        "yo_live_explicit": momo._YO_LIVE_EXPLICIT,
        "yo_api_host":      _safe_host(momo.YO_API_URL),
    }
