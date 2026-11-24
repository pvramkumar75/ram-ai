from ramai.signoff import strip_ai_signoff, strip_closing_block


def test_ai_signoff_removed():
    body = "Dear Asha,\n\nPlease see the points below.\n1. Spare parts\n\nRegards,\nRAM AI (on behalf of A Kumar)"
    assert strip_ai_signoff(body) == "Dear Asha,\n\nPlease see the points below.\n1. Spare parts"
    assert strip_ai_signoff("Hi\n\n- sent by RAM AI") == "Hi"
    assert strip_ai_signoff("Copper 12203.5 (+3.5)\n24/11/26 11:00:01 AM") == "Copper 12203.5 (+3.5)\n24/11/26 11:00:01 AM"


def test_closing_block_removed_only_at_the_end():
    assert strip_closing_block("Dear Asha,\n\nThe PO is released.\n\nRegards,\nKiran Rao") == "Dear Asha,\n\nThe PO is released."
    keep = "Dear Asha,\n\nThanks for the quote. We will revert by Friday with our comments on the payment terms and delivery."
    assert strip_closing_block(keep) == keep
