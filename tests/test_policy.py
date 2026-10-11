from worker.policy import assess_interaction, check_url

ORIGINS = ("http://127.0.0.1:8001",)


def test_url_allowlist():
    assert check_url("/erp/invoices", ORIGINS, base="http://127.0.0.1:8001/mail").allowed
    assert not check_url("https://evil.example/phish", ORIGINS).allowed
    assert not check_url("/__admin/state", ORIGINS, base="http://127.0.0.1:8001/").allowed
    assert not check_url("javascript:alert(1)", ORIGINS).allowed


def button(text, method="post"):
    return {"tag": "button", "type": "submit", "text": text, "form_method": method, "form_submit_label": text}


def test_risky_buttons_need_approval():
    for label in ["Submit for payment", "Update bank details", "Send email"]:
        d = assess_interaction(button(label), submits=True, mode="risky", read_only=False)
        assert d.allowed and d.needs_approval, label


def test_routine_buttons_do_not():
    for label in ["Save invoice", "Sign in", "Dismiss", "Search"]:
        d = assess_interaction(button(label), submits=True, mode="risky", read_only=False)
        assert d.allowed and not d.needs_approval, label


def test_links_are_navigation_not_actions():
    link = {"tag": "a", "text": "URGENT: updated bank details for all future payments", "href": "/mail/107"}
    assert not assess_interaction(link, submits=False, mode="risky", read_only=False).needs_approval


def test_read_only_mode_blocks_writes_but_allows_login_and_search():
    assert not assess_interaction(button("Save invoice"), submits=True, mode="risky", read_only=True).allowed
    assert assess_interaction(button("Sign in"), submits=True, mode="risky", read_only=True).allowed
    assert assess_interaction(button("Filter", method="get"), submits=True, mode="risky", read_only=True).allowed


def test_always_mode_gates_every_write():
    d = assess_interaction(button("Save invoice"), submits=True, mode="always", read_only=False)
    assert d.needs_approval
