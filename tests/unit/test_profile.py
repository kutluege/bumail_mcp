"""Interest profile: storage round trip and Turkish-aware matching."""

from __future__ import annotations

import tomllib

import pytest

from posta_mcp import profile as pf
from posta_mcp.profile import Interest, Matcher, Profile, ProfileError, fold, suggest_keywords


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "profile.toml"
    monkeypatch.setenv("POSTA_PROFILE", str(p))
    return p


# --------------------------------------------------------------------------- folding


def test_fold_turkish_case_rules():
    assert fold("İSTANBUL") == fold("istanbul") == "istanbul"
    assert fold("ISPARTA") == fold("ısparta") == fold("isparta") == "isparta"
    assert fold("GÜNEY KAMPÜS") == fold("güney kampüs") == "guney kampus"
    assert fold("ĞŞÖÜÇİıI") == "gsoucii" + "i"
    assert fold("  Kampüs'te\n\tYEMEKHANE ") == "kampuste yemekhane"
    assert fold("i̇") == "i"  # i + combining dot (what Python's lower() makes of İ)


# --------------------------------------------------------------------------- matching


def interest(name: str, *kws: str) -> Profile:
    return Profile(interests=[Interest(name=name, keywords=list(kws))])


@pytest.mark.parametrize(
    "text",
    [
        "Güney Kampüs yemekhanesi kapalı",
        "GÜNEY KAMPÜS",
        "güney kampüs",
        "GUNEY KAMPUS",
        "Güney Kampüsünde duyuru",
        "Güney Kampüs'te etkinlik",
        "Güney Kampüsü'nden servis",
        "güney   kampüs\nyeni bina",  # whitespace is collapsed
        "Open day at the South Campus",
        "SOUTH CAMPUS",
        "Güney Yerleşkesi",
        "güney kampüslerimizdeki",
    ],
)
def test_guney_kampus_matches(text):
    p = interest("Güney Kampüs", *suggest_keywords("Güney Kampüs"))
    assert Matcher(p).match(text) == ["Güney Kampüs"]


@pytest.mark.parametrize(
    "text",
    ["yemekhanede", "YEMEKHANE", "Yemekhaneye", "yemekhanesinde", "Cafeteria hours", "menüsü",
     "Haftalık MENÜ", "yemekler", "yemeklerde"],
)  # fmt: skip
def test_yemekhane_matches(text):
    p = interest("yemekhane", *suggest_keywords("yemekhane"))
    assert Matcher(p).match(text) == ["yemekhane"]


@pytest.mark.parametrize(
    "text", ["tenisçi", "TENİSÇİ", "TENISCI", "tenis kortu", "tenise", "tenisle", "Tennis court",
             "kortlar", "kortta"],
)  # fmt: skip
def test_tenis_matches(text):
    p = interest("tenis", *suggest_keywords("tenis"))
    assert Matcher(p).match(text) == ["tenis"]


@pytest.mark.parametrize(
    "text",
    [
        "kortizol seviyesi",  # 'kort' + non-suffix
        "stenis",  # no left word boundary
        "güneyli kampanya",  # different words
        "kampüs",  # half of a two-word keyword is not enough
        "yemeksiz",  # not one of the keywords
        "",
    ],
)
def test_no_false_positives(text):
    p = Profile(
        interests=[
            Interest(name="tenis", keywords=["tenis", "kort"]),
            Interest(name="Güney Kampüs", keywords=["güney kampüs"]),
            Interest(name="yemekhane", keywords=["yemekhane", "menü"]),
        ]
    )
    assert Matcher(p).match(text) == []


def test_capital_dotted_and_dotless_i_in_text_and_keyword():
    text = "KIRMIZI İŞARET, BİLİŞİM ve ISPARTA hakkında"
    for kw in ("kırmızı işaret", "kirmizi isaret", "KIRMIZI İŞARET", "bilişim", "ısparta"):
        assert Matcher(interest("x", kw)).match(text) == ["x"], kw


def test_interest_name_itself_matches():
    assert Matcher(interest("Kütüphane")).match("KÜTÜPHANEDE sınav saatleri") == ["Kütüphane"]


def test_matches_several_interests_in_profile_order():
    p = Profile(
        interests=[
            Interest(name="tenis", keywords=["tenis"]),
            Interest(name="yemekhane", keywords=["yemekhane"]),
        ]
    )
    assert Matcher(p).match("yemekhane ve tenis", "") == ["tenis", "yemekhane"]
    assert Matcher(p).match("konu: tenis", "içerik: yemekhane") == ["tenis", "yemekhane"]
    assert Matcher(p).match("hiçbiri") == []
    assert pf.match_interests(p, "TENİS") == ["tenis"]


# --------------------------------------------------------------------------- storage


def test_load_missing_file_is_empty(path):
    assert pf.load().is_empty and pf.load().notes == ""


def test_profile_path_logic(tmp_path):
    assert pf.profile_path({"POSTA_PROFILE": str(tmp_path / "p.toml")}) == tmp_path / "p.toml"
    cfg = tmp_path / "cfgdir" / "config.toml"
    assert pf.profile_path({"POSTA_CONFIG": str(cfg)}) == tmp_path / "cfgdir" / "profile.toml"
    assert pf.profile_path({"POSTA_PROFILE": "  "}).name == "profile.toml"  # blank = unset


def test_round_trip_with_turkish_and_awkward_text(path):
    p = Profile(
        interests=[
            Interest(name='Güney "Kampüs" \\ ğüşıöçİ', keywords=["güney kampüs", "south campus"]),
            Interest(name="tenis", keywords=[]),
        ],
        notes="Haftada 2 gün tenis.\nSatır 2 ’tırnak’",
    )
    pf.save(p)
    assert pf.load() == p.model_copy(update={"notes": p.notes.replace("\n", " ")})
    text = path.read_text("utf-8")
    assert "ğüşıöçİ" in text  # written as readable UTF-8, not escapes
    tomllib.loads(text)


def test_add_interest_defaults_for_known_names_only(path):
    added = pf.add_interest("Güney Kampüs")
    assert "south campus" in added.keywords and "güney yerleşke" in added.keywords
    assert "cafeteria" in pf.add_interest("yemekhane").keywords
    assert "tennis" in pf.add_interest("Tenis").keywords
    assert pf.add_interest("kütüphane").keywords == []  # nothing invented for unknown names
    assert [i.name for i in pf.load().interests] == [
        "Güney Kampüs",
        "yemekhane",
        "Tenis",
        "kütüphane",
    ]


def test_explicit_keywords_are_stored_exactly(path):
    got = pf.add_interest("tenis", ["tenis", "TENIS", "kort"])  # dupes fold together
    assert got.keywords == ["tenis", "kort"]
    assert "tennis" in suggest_keywords("tenis")  # offered as a suggestion, not inserted
    assert "tennis" not in pf.load().interests[0].keywords


def test_add_existing_interest_merges_keywords(path):
    pf.add_interest("tenis", ["tenis"])
    pf.add_interest("TENİS", ["Tennis"])
    (only,) = pf.load().interests
    assert only.name == "tenis" and only.keywords == ["tenis", "Tennis"]


def test_remove_interest(path):
    pf.add_interest("tenis")
    pf.add_interest("yemekhane")
    assert pf.remove_interest("TENİS") is True
    assert pf.remove_interest("tenis") is False
    assert [i.name for i in pf.load().interests] == ["yemekhane"]


def test_set_notes(path):
    pf.add_interest("tenis")
    pf.set_notes("Güney Kampüs'te kalıyorum")
    assert pf.load().notes == "Güney Kampüs'te kalıyorum"
    assert len(pf.load().interests) == 1


def test_validation_errors(path):
    with pytest.raises(ProfileError):
        pf.add_interest("   ")
    with pytest.raises(ProfileError):
        pf.add_interest("x" * 500)
    with pytest.raises(ProfileError):
        pf.add_interest("tenis", [""])
    with pytest.raises(ProfileError):
        pf.save(Profile(notes="n" * 5000))
    assert not path.exists()


def test_corrupt_file_is_a_clean_error(path):
    path.write_text("[[interests]\nname = ", encoding="utf-8")
    with pytest.raises(ProfileError) as exc:
        pf.load()
    assert "name = " not in str(exc.value)  # never echoes file contents


def test_save_is_atomic_and_leaves_no_temp_files(path):
    pf.add_interest("tenis")
    pf.add_interest("yemekhane")
    assert [p.name for p in path.parent.iterdir()] == ["profile.toml"]


def test_failed_save_keeps_old_file(path, monkeypatch):
    pf.add_interest("tenis")
    before = path.read_text("utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pf.os, "replace", boom)
    with pytest.raises(OSError):
        pf.add_interest("yemekhane")
    assert path.read_text("utf-8") == before
    assert [p.name for p in path.parent.iterdir()] == ["profile.toml"]


def test_example_file_is_valid_and_has_the_three_interests():
    profile = pf._parse(tomllib.loads(pf.example_text()))
    assert [i.name for i in profile.interests] == ["Güney Kampüs", "Yemekhane", "Tenis"]
    m = Matcher(profile)
    assert m.match("South Campus") == ["Güney Kampüs"]
    assert m.match("cafeteria menu") == ["Yemekhane"]
    assert m.match("tennis") == ["Tenis"]
