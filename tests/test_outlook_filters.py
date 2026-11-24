import pytest

from ramai.tools.outlook_com import ADDRESSED, TO_ME_TAG, recipient_filters

OWNER = "asha@example.com"


def test_own_address_or_me_becomes_the_to_me_flag():
    for who in ("asha@example.com", "ASHA@example.com", "me", "Myself"):
        flts, used, other = recipient_filters(None, who, OWNER)
        assert used == "to_me" and other == "" and flts == [ADDRESSED["to_me"]] and TO_ME_TAG in flts[0]


def test_explicit_addressed_kept_and_other_people_match_names():
    flts, used, other = recipient_filters("only_to_me", OWNER, OWNER)
    assert used == "only_to_me" and flts == [ADDRESSED["only_to_me"]]
    flts, used, other = recipient_filters(None, "ravi.k@example.com", OWNER)
    assert used is None and other == "ravi.k@example.com" and "LIKE '%ravi.k%'" in flts[0] and "displaycc" in flts[0]
    flts, _, _ = recipient_filters("cc_me", "O'Brien", OWNER)
    assert len(flts) == 2 and "O''Brien" in flts[1]          # quotes escaped for Outlook's filter


def test_bad_addressed_value_rejected():
    with pytest.raises(ValueError):
        recipient_filters("everyone", None, OWNER)
    assert recipient_filters(None, None, OWNER) == ([], None, "")
