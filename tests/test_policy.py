from worker.policy import assess_interaction, check_url

ORIGINS = ("http://127.0.0.1:8001",)


def test_url_allowlist():
    assert check_url("/erp/invoices", ORIGINS, base="http://127.0.0.1:8001/mail").allowed
    assert not check_url("https://evil.example/phish", ORIGINS).allowed
    assert not check_url("/__admin/state", ORIGINS, base="http://127.0.0.1:8001/").allowed
    assert not check_url("javascript:alert(1)", ORIGINS).allowed


def button(text, method="post", action="/erp/invoices"):
    return {"tag": "button", "type": "submit", "text": text, "form_method": method, "form_action": action,
            "form_submit_label": text}


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


def test_benign_label_on_risky_endpoint_needs_approval():
    # Found in self-review: a "Save" button on the bank-details form must not slip through.
    for action in ["/erp/vendors/1/bank", "/erp/invoices/INV-1/submit", "http://127.0.0.1:8001/mail/101/reply"]:
        d = assess_interaction(button("Save", action=action), submits=True, mode="risky", read_only=False)
        assert d.allowed and d.needs_approval and "posts to" in d.reason, action


def test_read_only_blocks_risky_endpoint_even_with_safe_label():
    assert not assess_interaction(button("OK", action="/erp/vendors/1/bank"), submits=True, mode="risky",
                                  read_only=True).allowed


def test_benign_endpoints_are_not_flagged():
    for action in ["/erp/invoices", "/erp/login", "/erp/dismiss-announcement"]:
        d = assess_interaction(button("Save", action=action), submits=True, mode="risky", read_only=False)
        assert d.allowed and not d.needs_approval, action
    # A GET form (search) to a risky-looking path only reads.
    assert not assess_interaction(button("Go", method="get", action="/mail/101/reply"), submits=True,
                                  mode="risky", read_only=False).needs_approval


def test_enter_in_text_field_uses_form_endpoint():
    field = {"tag": "input", "type": "text", "text": "", "form_method": "post",
             "form_action": "/erp/vendors/1/bank", "form_submit_label": "Save"}
    assert assess_interaction(field, submits=True, mode="risky", read_only=False).needs_approval
