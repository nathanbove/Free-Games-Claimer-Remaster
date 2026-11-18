"""Humble Choice on the rebuilt site: which month gets worked, and what an entry on its order page means.

Since the November 2026 redesign there is no order API to read: a run reads
pages. The fixtures below are shaped like the entries the new library's
order page hands the browser - a title, the entry's buttons, its whole
text, and its platform icon's name - which is all classify_entry decides on.
"""

from datetime import date

from src.stores.humble import (
    choice_groups,
    classify_entry,
    entry_snippet,
    looks_like_code,
    month_display_name,
    month_slug_from_text,
    normalize_platform,
    parse_month_heading,
    select_month,
    title_slug,
)

TODAY = date(2026, 11, 17)


def _entry(title="A Game", buttons=(), text="", icon=""):
    return {"title": title, "buttons": list(buttons), "text": text, "platform_icon": icon}


def _revealed_steam():
    return _entry(
        title="Squeakross: Home Squeak Home",
        buttons=["ABCDE-12345-FGHIJ", "Steam"],
        text="Squeakross: Home Squeak Home\nABCDE-12345-FGHIJ\nSteam\n"
             "Steam will not provide extra giftable copies of games you already own.",
        icon="Steam",
    )


def _unrevealed_ign():
    return _entry(
        title="One Month of IGN Plus",
        buttons=["Reveal"],
        text="One Month of IGN Plus\nReveal\n"
             "Keys expire. Please reveal your key before 01/01/2027.",
    )


def _redeemed_blizzard():
    return _entry(
        title="The Outer Worlds 2",
        text="The Outer Worlds 2\nRedeemed on Blizzard (Battle.net)",
    )


class TestClassifyEntry:
    def test_a_revealed_entry_carries_its_key_and_platform(self):
        entry = classify_entry(_revealed_steam(), today=TODAY)
        assert entry["state"] == "revealed"
        assert entry["code"] == "ABCDE-12345-FGHIJ"
        assert entry["platform"] == "steam"
        assert entry["title"] == "Squeakross: Home Squeak Home"

    def test_the_platform_can_come_from_the_icon_alone(self):
        raw = _revealed_steam()
        raw["buttons"] = ["ABCDE-12345-FGHIJ"]
        assert classify_entry(raw, today=TODAY)["platform"] == "steam"

    def test_a_non_steam_key_shape_still_reads_as_a_key(self):
        # IGN hands out codes like HUMBLE_XXXX, not Steam's three groups of five.
        raw = _entry(title="One Month of IGN Plus", buttons=["HUMBLE_3R18H7RIZ4TL"],
                     text="One Month of IGN Plus\nHUMBLE_3R18H7RIZ4TL")
        entry = classify_entry(raw, today=TODAY)
        assert entry["state"] == "revealed"
        assert entry["code"] == "HUMBLE_3R18H7RIZ4TL"

    def test_an_unrevealed_entry_waits_with_its_expiry(self):
        entry = classify_entry(_unrevealed_ign(), today=TODAY)
        assert entry["state"] == "unrevealed"
        assert entry["code"] is None
        assert entry["expiry"] == date(2027, 1, 1)

    def test_an_unrevealed_entry_past_its_date_is_expired(self):
        entry = classify_entry(_unrevealed_ign(), today=date(2027, 2, 1))
        assert entry["state"] == "expired"

    def test_an_entry_that_says_expired_is_expired(self):
        raw = _entry(text="A Game\nThis key has expired.")
        assert classify_entry(raw, today=TODAY)["state"] == "expired"

    def test_a_keyless_grant_is_redeemed_not_revealed(self):
        entry = classify_entry(_redeemed_blizzard(), today=TODAY)
        assert entry["state"] == "redeemed"
        assert entry["code"] is None
        assert entry["platform"] == "blizzard"

    def test_an_entry_with_nothing_readable_is_unknown(self):
        assert classify_entry(_entry(text="A Game"), today=TODAY)["state"] == "unknown"
        assert classify_entry({}, today=TODAY)["state"] == "unknown"
        assert classify_entry(None, today=TODAY)["title"] == "(untitled)"

    def test_the_reveal_by_date_alone_does_not_read_as_expired(self):
        # "Keys expire. Please reveal..." is the standing wording of every
        # unrevealed entry; only the word "expired" settles it early.
        raw = _unrevealed_ign()
        raw["text"] = raw["text"].replace("01/01/2027", "12/01/2026")
        assert classify_entry(raw, today=TODAY)["state"] == "unrevealed"


class TestLooksLikeCode:
    def test_key_shapes(self):
        assert looks_like_code("ABCDE-12345-FGHIJ") is True
        assert looks_like_code("HUMBLE_3R18H7RIZ4TL") is True
        assert looks_like_code("ABCD-EFGH") is True

    def test_labels_are_not_keys(self):
        assert looks_like_code("Steam") is False
        assert looks_like_code("Reveal") is False
        assert looks_like_code("Microsoft") is False
        assert looks_like_code("ABCDEFGH") is False  # one plain word, no digit or separator
        assert looks_like_code("Short1") is False
        assert looks_like_code("lower-case1") is False
        assert looks_like_code("") is False


class TestNormalizePlatform:
    def test_the_names_the_page_uses(self):
        assert normalize_platform("Steam") == "steam"
        assert normalize_platform("Blizzard (Battle.net)") == "blizzard"
        assert normalize_platform("Battle.net") == "blizzard"
        assert normalize_platform("GOG") == "gog"
        assert normalize_platform("Epic Games") == "epic"

    def test_no_name_is_no_platform(self):
        assert normalize_platform("") == ""
        assert normalize_platform(None) == ""


class TestEntrySnippet:
    def test_a_key_in_the_text_is_masked(self):
        snippet = entry_snippet("The key ABCDE-12345-FGHIJ could not be read")
        assert "ABCDE-12345-FGHIJ" not in snippet
        assert "could not be read" in snippet

    def test_plain_text_passes_through(self):
        assert entry_snippet("Nothing readable here") == "Nothing readable here"


class TestMonthNames:
    def test_a_slug_reads_as_a_month(self):
        assert month_display_name("november-2026") == "November 2026"
        assert month_display_name("january-2027") == "January 2027"

    def test_a_heading_names_its_month(self):
        assert parse_month_heading("November 2026 Humble Choice") == "november-2026"
        assert parse_month_heading("December 2026 Humble Choice") == "december-2026"

    def test_a_heading_without_a_choice_month_names_none(self):
        assert parse_month_heading("Humble Games Bundle") is None
        assert parse_month_heading("November 2026") is None  # a date, but not a Choice heading
        assert parse_month_heading("") is None

    def test_a_month_in_running_text(self):
        assert month_slug_from_text("Your November 2026 games are here") == "november-2026"
        assert month_slug_from_text("no month here") is None


class TestTitleSlug:
    def test_titles_become_id_pieces(self):
        assert title_slug("Mafia: The Old Country") == "mafia-the-old-country"
        assert title_slug("Mafia: The Old Country - Padrino Pack") == "mafia-the-old-country-padrino-pack"
        assert title_slug("DYSMANTLE") == "dysmantle"

    def test_a_title_with_nothing_usable_still_slugs(self):
        assert title_slug("!!!") == "game"
        assert title_slug("") == "game"


def _link(heading, title, href, sub=""):
    return {"heading": heading, "title": title, "sub": sub, "href": href}


class TestChoiceGroups:
    def test_games_group_under_their_month(self):
        groups = choice_groups([
            _link("November 2026 Humble Choice", "Mafia: The Old Country", "/home/library/content/aaa"),
            _link("November 2026 Humble Choice", "DYSMANTLE", "/home/library/content/bbb"),
            _link("October 2026 Humble Choice", "Row Game", "/home/library/content/ccc"),
        ])
        assert [g["slug"] for g in groups] == ["november-2026", "october-2026"]
        assert [g["title"] for g in groups[0]["games"]] == ["Mafia: The Old Country", "DYSMANTLE"]

    def test_a_links_own_bundle_line_groups_it_when_the_heading_is_missing(self):
        groups = choice_groups([
            _link("", "DYSMANTLE", "/home/library/content/bbb", sub="November 2026 Humble Choice"),
        ])
        assert [g["slug"] for g in groups] == ["november-2026"]

    def test_other_bundles_and_broken_links_are_left_out(self):
        groups = choice_groups([
            _link("Humble Games Bundle", "Some Game", "/home/library/content/ddd"),
            _link("November 2026 Humble Choice", "", "/home/library/content/eee"),
            _link("November 2026 Humble Choice", "No Href", ""),
            "not a link",
        ])
        assert groups == []

    def test_the_same_game_linked_twice_is_kept_once(self):
        link = _link("November 2026 Humble Choice", "Mafia: The Old Country", "/home/library/content/aaa")
        groups = choice_groups([link, dict(link)])
        assert len(groups[0]["games"]) == 1


class TestSelectMonth:
    def _groups(self):
        return choice_groups([
            _link("November 2026 Humble Choice", "Mafia: The Old Country", "/home/library/content/aaa"),
            _link("October 2026 Humble Choice", "Row Game", "/home/library/content/ccc"),
        ])

    def test_the_current_month_wins_when_owned(self):
        group, unpaid = select_month("november-2026", self._groups())
        assert group["slug"] == "november-2026"
        assert unpaid is False

    def test_an_unowned_current_month_falls_back_and_reports_unpaid(self):
        group, unpaid = select_month("december-2026", self._groups())
        assert group["slug"] == "november-2026"
        assert unpaid is True

    def test_no_current_month_read_picks_the_newest(self):
        group, unpaid = select_month(None, self._groups())
        assert group["slug"] == "november-2026"
        assert unpaid is False

    def test_no_groups_picks_nothing(self):
        group, unpaid = select_month("november-2026", [])
        assert group is None
        assert unpaid is True
        group, unpaid = select_month(None, [])
        assert group is None
        assert unpaid is False
