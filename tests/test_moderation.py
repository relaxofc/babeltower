from babeltower.moderation import scan_intent_text


def test_scan_intent_text_detects_email():
    assert scan_intent_text("Contact me at owner@example.com") == ["email"]


def test_scan_intent_text_detects_dotless_email_like_handle():
    assert scan_intent_text("Reach research@stanford for context") == ["email"]


def test_scan_intent_text_detects_phone():
    assert scan_intent_text("Call +1 415 555 0199 after lunch") == ["phone"]


def test_scan_intent_text_detects_url():
    assert scan_intent_text("Portfolio is https://example.dev/work") == ["url"]
    assert scan_intent_text("Portfolio is example.ai") == ["url"]


def test_scan_intent_text_detects_handle():
    assert scan_intent_text("DM @founder_bot") == ["handle"]
    assert scan_intent_text("Use t.me/founderbot") == ["handle"]


def test_scan_intent_text_avoids_common_false_positives():
    assert scan_intent_text("The year @2025 is not a handle") == []
    assert scan_intent_text("Seeking 2 ML people for 2026 cohort") == []

