import tempfile
from pathlib import Path

from ramai.core import DB
from ramai.people import People, explain, match, rank


def test_short_forms():
    assert match("KVR", "K V Rao", "kvrao@example.com")[0] >= 0.9                 # initials
    assert match("KVR", "Kolla Venkata Rao", "")[1] == "initials"
    assert match("kvr", "Someone Else", "kvr@example.com")[1] == "address starts with it"
    assert match("Suresh", "Suresh Babu Tech", "")[0] >= 0.84                       # first name
    assert match("srini rao", "Srinivasa Rao", "")[0] >= 0.84                      # starts of two words
    assert match("Srini", "Srinivas Reddy", "")[0] >= 0.8                          # nickname
    assert match("PSR sir", "PSR TPL", "")[0] >= 0.9                               # honorific ignored
    assert match("Venky", "Venkatesh Prasad", "")[0] >= 0.8
    assert match("KVR", "Ravi Kumar", "ravi@example.com")[0] == 0.0
    assert match("Asha", "Ashok Kumar", "")[0] < 0.84                              # not a word start match of the full word


def test_duplicates_pick_most_interaction_and_explain():
    c = [{"name": "Ravi Kumar", "key": "a", "score": 0.9, "interactions": 2},
         {"name": "Ravi Shankar", "key": "b", "score": 0.9, "interactions": 40},
         {"name": "Ravindra", "key": "c", "score": 0.84, "interactions": 99}]
    r = rank(c)
    assert r[0]["name"] == "Ravi Shankar" and r[-1]["name"] == "Ravindra"         # better match tier first, then interaction
    line = explain("Ravi", r)
    assert "Ravi Shankar" in line and "interact with most (40 vs 2)" in line and "Also matched" in line


def test_directory_learns_and_remembers_aliases():
    db = DB(Path(tempfile.mkdtemp()) / "t.db")
    p = People(db)
    p.note_from_line("Asha Menon <asha.menon@example.com>; Arun Rao <arun@example.com>", "mail")
    p.note(name="K V Rao", email="kvrao@example.com", source="mail", sent="mail")
    p.note(wa_title="KVR personal", source="whatsapp")
    p.note(wa_title="KVR", source="whatsapp", sent="whatsapp")
    assert p.candidates("KVR", "mail")[0]["email"] == "kvrao@example.com"
    assert p.candidates("KVR", "whatsapp")[0]["wa_title"] == "KVR"
    p.learn("kvr", "whatsapp", "wa:kvr personal")                                  # user chose another chat for 'kvr'
    top = p.candidates("kvr", "whatsapp")[0]
    assert top["wa_title"] == "KVR personal" and "before" in top["why"]
    assert p.candidates("asha", "mail")[0]["email"] == "asha.menon@example.com"


def test_outlook_style_addresses_are_accepted():
    db = DB(Path(tempfile.mkdtemp()) / "t.db")
    p = People(db)
    assert p.note("Kiran", "<k.raman@example.com>", source="outlook") == "k.raman@example.com"
