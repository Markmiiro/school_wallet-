# ================================================
# tests/test_tuckshop_ui.py
# ------------------------------------------------
# Smoke tests for the tuck-shop device page itself (app/routes/tuckshop.py).
# Can't execute its JS here, but can catch the specific regressions this
# page has had before: a hardcoded merchant, and money calls with no auth.
# ================================================


def test_page_loads(client):
    r = client.get("/tuckshop/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_page_has_login_not_hardcoded_merchant(client):
    r = client.get("/tuckshop/")
    body = r.text
    assert "doLogin" in body
    assert "/auth/login" in body
    assert "MERCHANT_ID = 1" not in body


def test_page_sends_auth_header_on_payment(client):
    body = client.get("/tuckshop/").text
    # The /payments/nfc fetch must carry the bearer token, not go out anonymously.
    assert "headers: authHeaders()" in body


def test_merchant_school_lookup_used_by_the_page_is_public(client, merchant, school):
    # afterLogin() calls this with no special auth beyond the bearer token
    # it always sends — confirms the endpoint the page depends on exists
    # and returns the shape the JS expects (a "merchants" list).
    r = client.get(f"/merchants/school/{school.id}")
    assert r.status_code == 200
    assert "merchants" in r.json()


def test_page_queues_offline_payments_with_a_request_id(client):
    # saveOffline() must save the SAME request_id generated at tap time
    # (currentRequestId), not a fresh one — that's what makes a later
    # sync of this item recognizable as a repeat if the original charge
    # actually reached the server before the connection dropped.
    body = client.get("/tuckshop/").text
    assert "saveOffline(currentTagUid, amount, currentRequestId)" in body
    assert "request_id:  requestId" in body


def test_page_syncs_offline_queue_with_auth_and_drains_only_settled_items(client):
    body = client.get("/tuckshop/").text
    assert "function flushOfflineQueue" in body
    assert "/payments/sync" in body
    assert "...authHeaders()" in body  # the sync POST also carries the bearer token
    # Only items the server explicitly settled (processed or failed) are
    # removed from the local queue — anything else stays for next time.
    assert "data.details.processed.map(p => p.request_id)" in body
    assert "data.details.failed.map(f => f.request_id)" in body
    assert "queue.filter(item => !settled.has(item.request_id))" in body
    # Retriggers: connectivity return, page load once signed in, and
    # opportunistically after a successful online tap.
    assert "addEventListener('online', flushOfflineQueue)" in body
    assert "flushOfflineQueue();" in body
