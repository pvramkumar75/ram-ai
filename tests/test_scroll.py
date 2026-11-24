from ramai.ocr import Line
from ramai.tools.scroll import _same_view, _signature, find_in, match_score


def L(text, y, x=10):
    return Line(text, x, y, 200, 16)


def test_match_contains_case_and_punctuation():
    assert match_score("TCAL Shipments", "tcal shipments to KSA") == 1.0
    assert match_score("12 November", "Wednesday, 12 November 2026") == 1.0


def test_match_tolerates_ocr_slips_but_not_unrelated_text():
    assert match_score("Lakshmi Traders", "Lakshmi Tradors") >= 0.86
    assert match_score("Lakshmi Traders", "HDFC Bank statement") == 0.0


def test_numbers_must_match_exactly_and_short_words_are_whole_words():
    assert match_score("Section 12: Routine notes", "Section 95: Routine notes") == 0.0
    assert match_score("12 November", "13 November") == 0.0
    assert match_score("ok", "book") == 0.0 and match_score("ok", "Ok, will do") == 1.0


def test_line_holding_the_text_wins_over_a_joined_pair():
    lines = [L("Lorem ipsum filler text for scrolling.", 100), L("Section 97: Copper price review", 118)]
    hit, score = find_in(lines, "Section 97: Copper price review")
    assert hit is lines[1] and score == 1.0


def test_text_wrapped_over_two_lines_is_found():
    lines = [L("Who is responsible for", 100), L("exemptions, so that I may", 118), L("contact them?", 136)]
    hit, score = find_in(lines, "responsible for exemptions")
    assert hit is lines[0] and score >= 0.95


def test_view_change_detection():
    a = _signature([L("alpha line", 10), L("beta line", 30), L("gamma line", 50)])
    assert _same_view(a, set(a))
    b = _signature([L("beta line", 10), L("gamma line", 30), L("delta line", 50)])
    assert not _same_view(a, b)


def test_measure_shift_from_lines_seen_twice():
    from ramai.tools.scroll import measure_shift

    before = [L("alpha text line", 300), L("bravo text line", 320), L("charlie text line", 340)]
    after = [L("alpha text line", 180), L("bravo text line", 200), L("charlie text line", 220), L("delta text line", 240)]
    assert measure_shift(before, after) == 120
    assert measure_shift(before, [L("zulu only", 10)]) is None


def test_app_aware_panes():
    from ramai.tools.scroll import pane_bounds

    assert pane_bounds("WhatsApp", 1942, "left") == (0, 543) and pane_bounds("WhatsApp", 1942, "right") == (543, 1942)
    assert pane_bounds("WhatsApp", 900, "left") == (0, 360)
    assert pane_bounds("Inbox - me@x.com - Outlook", 1900, "center")[0] < 1900 * 0.2
    assert pane_bounds("Report.docx - Word", 1000, "auto") == (0, 1000)
