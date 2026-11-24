from ramai.teach import Builder, clean_draft, describe, draft_prompt, is_secret_field, public_steps, slug, to_text

SAP = {"hwnd": 1, "app": "saplogon", "title": "SAP Easy Access"}
XL = {"hwnd": 2, "app": "EXCEL", "title": "trial.xlsx - Excel"}


def test_clicks_double_click_and_drag():
    b = Builder()
    b.click(0.0, 100, 100, SAP, {"name": "Execute", "type": "button"}, (0.1, 0.2))
    b.click(0.3, 102, 101, SAP, {"name": "Execute", "type": "button"}, (0.1, 0.2))      # quick second click -> double
    b.click(2.0, 300, 300, XL, {}, (0.5, 0.5))
    b.release(2.2, 500, 300, (0.7, 0.5))                                                   # moved while held -> drag
    assert [s["kind"] for s in b.steps] == ["double_click", "drag"]
    assert describe(b.steps[0]).startswith('Double-click the button "Execute"')
    assert "to 70% across" in describe(b.steps[1])


def test_typing_joins_per_field_and_backspace_edits():
    b = Builder()
    calls = []
    field = lambda: calls.append(1) or {"name": "Material", "type": "edit"}
    for i, ch in enumerate("45001"):
        b.char(i * 0.1, ch, SAP, field)
    b.backspace(0.6, SAP)
    b.char(0.7, "2", SAP, field)
    b.key(1.0, "Enter", SAP)
    b.key(1.2, "Enter", SAP)
    assert len(calls) == 1                     # focused field looked up once per typing step
    assert describe(b.steps[0]) == 'Type "45002" into the edit "Material"'
    assert describe(b.steps[1]) == "Press Enter x2"


def test_password_fields_are_never_recorded():
    b = Builder()
    for ch in "secret123":
        b.char(0, ch, SAP, {"name": "Password", "type": "edit", "password": True})
    s = b.steps[0]
    assert s["masked"] and s["text"] == "" and "secret123" not in describe(s) and "NOT recorded" in describe(s)
    assert is_secret_field({"name": "Enter OTP"}) and is_secret_field({"name": "PIN"}) and not is_secret_field({"name": "Spinning"})
    b.backspace(1, SAP)                         # backspace inside the hidden field stays hidden
    assert len(b.steps) == 1


def test_scroll_merges_same_direction():
    b = Builder()
    b.scroll(0, -1, XL)
    b.scroll(0.2, -2, XL)
    b.scroll(0.4, 1, XL)
    assert [describe(s) for s in b.steps] == ["Scroll down 3 notches", "Scroll up 1 notch"]


def test_unnamed_click_uses_nearby_text_then_position():
    b = Builder()
    b.click(0, 10, 10, SAP, {"type": "pane"}, (0.25, 0.5))
    b.steps[0]["near"] = "Material Document List"
    assert describe(b.steps[0]) == 'Click the text "Material Document List" [25% across, 50% down the window]'
    b.click(5, 50, 50, SAP, {}, (0.4, 0.6))
    assert describe(b.steps[1]) == "Click at 40% across, 60% down the window"


def test_text_grouped_by_window_and_kept_steps_only():
    b = Builder()
    b.click(0, 1, 1, SAP, {"name": "Command field", "type": "edit"}, (0.1, 0.1))
    b.char(1, "x", SAP, {"name": "Password", "password": True})
    b.click(3, 9, 9, XL, {"name": "Save", "type": "button"}, (0.1, 0.1))
    txt = to_text(b.steps, keep={0, 2})
    assert "In window: SAP Easy Access (saplogon)" in txt and "In window: trial.xlsx - Excel" in txt
    assert "1. Click" in txt and "2. Click the button \"Save\"" in txt and "password" not in txt.lower()
    ps = public_steps(b.steps)
    assert ps[1]["masked"] and not ps[1]["typed"] and ps[0]["window"].startswith("SAP")


def test_slug_prompt_and_draft_cleanup():
    assert slug("MB51 stock report / material!") == "mb51_stock_report_material"
    assert slug("ab") == "ab_skill"
    msgs = draft_prompt("MB51", "material changes", "In window: SAP\n  1. Click")
    assert msgs[0]["role"] == "system" and "never write a password" in msgs[0]["content"] and "MB51" in msgs[1]["content"]
    assert clean_draft("```markdown\n## When to use\nx\n```") == "## When to use\nx"


def test_taught_skill_braces_do_not_break_system_prompt(tmp_path, monkeypatch):
    from ramai.tools import skills

    monkeypatch.setattr(skills, "user_dir", lambda: tmp_path)
    (tmp_path / "mb51_report.md").write_text("---\nname: mb51_report\ndescription: MB51 for {material}\ntaught: yes\n---\nsteps", encoding="utf-8")
    brief = skills.skills_brief()
    assert "mb51_report: MB51 for {material} (TAUGHT" in brief
    from ramai.engine import SYSTEM

    SYSTEM.replace("{skills}", brief.replace("{", "{{").replace("}", "}}")).format(user="u", device="d", today="t", memory="m", roots="r")


def test_input_hooks_really_install():
    """Regression: a 64-bit module handle truncated to 32 bits made Windows refuse the hooks (error 126) silently."""
    import time

    from ramai.takeover import Takeover
    from ramai.teach import Recorder

    rec = Recorder()
    rec.start()
    try:
        assert rec.recording and rec._hook_ok
    finally:
        rec.stop()
    tk = Takeover()
    tk.arm()
    try:
        for _ in range(40):
            if tk.hooked:
                break
            time.sleep(0.05)
        assert tk.hooked
    finally:
        tk.disarm()
