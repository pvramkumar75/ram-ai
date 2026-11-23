from ramai.textfmt import render


def test_markers_removed_and_bullets_wrap_under_text():
    md = "- **METAL INFO** 4:05 pm — Delhi spot rates: copper armature 1089/1214, CC rod 1200/1340, aluminium rod 340, lead 194/216"
    out = render(md, width=60, color=False)
    assert "**" not in out and out.startswith("  • METAL INFO 4:05 pm")
    assert all(line.startswith("    ") for line in out.splitlines()[1:])   # continuation aligned under the text
    assert max(len(line) for line in out.splitlines()) <= 60


def test_headings_numbers_links_code_tables():
    md = ("## Needs you\n1. TCAL question\n2. See [pricing](https://example.com/p) and `no renewals`\n\n"
          "| Chat | Unread |\n|---|---|\n| HDFC Bank | 17 |\n")
    out = render(md, width=80, color=False)
    for gone in ("#", "[", "](", "`", "|", "---"):
        assert gone not in out
    assert "Needs you" in out and "  1. TCAL question" in out and "pricing (https://example.com/p)" in out
    assert "  HDFC Bank   17" in out


def test_colour_uses_real_bold_and_snake_case_survives():
    out = render("**Bold** and file_name_here and 2*3*4", width=80, color=True)
    assert "\033[1mBold\033[22m" in out and "file_name_here" in out and "2*3*4" in out
