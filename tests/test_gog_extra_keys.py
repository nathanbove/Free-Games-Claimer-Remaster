"""Spare GOG codes: the library reading and the divert decision.

A Prime Gaming code whose game is already in your GOG library is a spare:
posted to the duplicates channel instead of redeemed. These are the pure
pieces of that decision; the browser and database work sits on top of them.
"""

import json

from src.stores.gog import divertable, extra_key_posted, library_titles


class TestLibraryTitles:
    def test_titles_come_out_normalized(self):
        payload = {"products": [{"title": "The Witcher 3: Wild Hunt"},
                                {"title": "Cyberpunk 2077"}]}
        assert library_titles(payload) == {"thewitcher3wildhunt", "cyberpunk2077"}

    def test_the_same_title_twice_is_one_entry(self):
        payload = {"products": [{"title": "Doom"}, {"title": "Doom"}]}
        assert len(library_titles(payload)) == 1

    def test_junk_shapes_read_as_an_empty_page(self):
        assert library_titles(None) == set()
        assert library_titles({}) == set()
        assert library_titles({"products": None}) == set()
        assert library_titles({"products": [{"name": "no title here"}, "junk"]}) == set()


class TestExtraKeyPosted:
    def test_the_flag_is_read_from_the_row(self):
        assert extra_key_posted(json.dumps({"extra_key_posted": True})) is True

    def test_anything_else_is_not_posted(self):
        assert extra_key_posted(None) is False
        assert extra_key_posted("") is False
        assert extra_key_posted("{}") is False
        assert extra_key_posted("not json") is False
        assert extra_key_posted(json.dumps({"external_store": "gog"})) is False


class TestDivertable:
    def test_an_owned_unposted_code_is_a_spare(self):
        owned = library_titles({"products": [{"title": "Doom"}]})
        assert divertable("Doom", json.dumps({"external_store": "gog"}), owned) is True

    def test_matching_ignores_case_and_punctuation(self):
        owned = library_titles({"products": [{"title": "The Witcher 3: Wild Hunt"}]})
        assert divertable("the witcher 3 wild hunt", "{}", owned) is True

    def test_an_unowned_code_is_redeemed_as_usual(self):
        owned = library_titles({"products": [{"title": "Doom"}]})
        assert divertable("Quake", "{}", owned) is False

    def test_a_posted_code_is_never_posted_twice(self):
        owned = library_titles({"products": [{"title": "Doom"}]})
        extra = json.dumps({"external_store": "gog", "extra_key_posted": True})
        assert divertable("Doom", extra, owned) is False

    def test_no_library_reading_diverts_nothing(self):
        assert divertable("Doom", "{}", set()) is False
        assert divertable("Doom", "{}", None) is False
