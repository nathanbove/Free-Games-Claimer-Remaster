"""Humble Choice on the rebuilt site: which month gets worked, and what an entry on its order page means.

Since the November 2026 redesign there is no order API to read: a run reads
pages. The fixtures below are shaped like the entries the new library's
order page hands the browser - a title, the entry's buttons, its whole
text, and its platform icon's name - which is all classify_entry decides on.
"""

import asyncio
import json
from datetime import date

import pytest

from src.stores.humble import (
    AUTH_FORM_JS,
    CONTENT_ENTRY_JS,
    LIBRARY_LINKS_JS,
    ORDER_ENTRIES_JS,
    REDEEM_LINK_JS,
    SHOWCASE_JS,
    SIGNED_IN_JS,
    URL_LIBRARY,
    URL_MEMBERSHIP,
    HumbleClaimer,
    choice_groups,
    classify_entry,
    entry_snippet,
    looks_like_code,
    month_display_name,
    month_slug_from_text,
    normalize_platform,
    parse_month_heading,
    parse_showcase,
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

    def test_the_new_librarys_shape_groups_by_bundle_line_alone(self):
        # The library as it stands since November 22, 2026: no bundle
        # headings anywhere, a few games to a page, and a newer bundle's
        # games ahead of the month's - every link's heading is empty and
        # its bundle line alone says what it belongs to.
        groups = choice_groups([
            _link("", "Doors of Insanity: ReOpened", "/home/library/content/d1",
                  sub="Deck the Halls with Deckbuilders"),
            _link("", "Check In, Please Playtest", "/home/library/content/n1",
                  sub="November 2026 Humble Choice"),
            _link("", "The Outer Worlds 2", "/home/library/content/n2",
                  sub="November 2026 Humble Choice"),
            _link("", "Mafia: The Old Country - Padrino Pack", "/home/library/content/n3",
                  sub="November 2026 Humble Choice"),
        ])
        assert [g["slug"] for g in groups] == ["november-2026"]
        assert [g["title"] for g in groups[0]["games"]] == [
            "Check In, Please Playtest", "The Outer Worlds 2",
            "Mafia: The Old Country - Padrino Pack"]


class TestShowcase:
    def test_a_label_and_its_buttons_speak(self):
        showcase = parse_showcase({"label": "November 2026 Humble Choice",
                                   "titles": ["Mafia: The Old Country", "DYSMANTLE"]})
        assert showcase == {"slug": "november-2026",
                            "titles": ["Mafia: The Old Country", "DYSMANTLE"]}

    def test_titles_are_cleaned_and_kept_once(self):
        showcase = parse_showcase({"label": "November 2026 Humble Choice",
                                   "titles": [" DYSMANTLE ", "DYSMANTLE", "", None]})
        assert showcase["titles"] == ["DYSMANTLE"]

    def test_no_strip_is_no_showcase(self):
        assert parse_showcase({"label": "", "titles": []}) is None
        assert parse_showcase({}) is None
        assert parse_showcase(None) is None
        assert parse_showcase("November 2026 Humble Choice") is None

    def test_a_label_that_names_no_choice_month_is_no_showcase(self):
        assert parse_showcase({"label": "Deck the Halls with Deckbuilders",
                               "titles": ["A Game"]}) is None

    def test_a_label_with_no_buttons_behind_it_is_no_showcase(self):
        assert parse_showcase({"label": "November 2026 Humble Choice", "titles": []}) is None


class TestPickMonth:
    """A run's month is picked on the showcase's word first, the library scan's second.

    The evidence rules are the point, and they come from the November
    22, 2026 run: an unsettled library page handed back nothing, the
    empty read was taken as proof, and a false "posted but not paid for"
    alert went out for a month that was paid for and had been worked for
    days. So a reading that proves nothing concludes nothing. A showcase
    naming the current month is positive evidence the month is yours -
    a details dialog that then names no order page is a site problem,
    and the run works no month and sends no alert. A scan naming no
    month is likewise no evidence. The unpaid alert is earned only by a
    scan that names an older month than the current one: the library
    demonstrably holds Choice months, and the current one is not the
    newest of them.
    """

    NOVEMBER_SHOWCASE = {"label": "November 2026 Humble Choice",
                         "titles": ["Mafia: The Old Country", "DYSMANTLE"]}
    ORDER_HREF = "/home/library/order-uuid"
    ORDER_URL = "https://www.humblebundle.com/home/library/order-uuid"

    def _claimer(self, *, membership_text="November 2026 Humble Choice - Member Home",
                 showcases=(), click_titles=(), redeem_href=ORDER_HREF, pages=None):
        """A claimer whose pages answer from the script they are handed.

        showcases: the readings successive showcase reads hand back, in
        order; the last one stands when the queue runs out, the way a
        settled page keeps answering. click_titles: the titles whose
        details button opens its dialog. redeem_href: the href the open
        dialog names, or None when it names none. pages: {page number:
        links} for the scan's library reads, by the fake page's address.
        Returns (claimer, alerted): `alerted` collects the slugs handed
        to _alert_unpaid, so a test can say whether the alert fired.
        """
        showcase_readings = list(showcases)
        pages = pages or {}

        class FakePage:
            def __init__(self):
                self.visits = []
                self.current = ""

            async def get(self, url):
                self.visits.append(url)
                self.current = url
                return None

            async def evaluate(self, js, await_promise=False):
                if "View details for" in js:
                    return any(json.dumps(title) in js for title in click_titles)
                return True  # the dialog dismiss

        async def _no_sleep(_seconds):
            return None

        async def _page_text():
            return membership_text

        async def _eval_json(js, _fallback=None):
            if "Go to Redeem" in js:
                return redeem_href
            if "TreeWalker" in js:
                url = claimer.page.current
                number = int(url.rsplit("?page=", 1)[1]) if "?page=" in url else 1
                return pages.get(number, [])
            if "aria-label" in js:
                if len(showcase_readings) > 1:
                    return showcase_readings.pop(0)
                return showcase_readings[0] if showcase_readings else None
            raise AssertionError(f"unexpected script: {js[:60]}")

        alerted = []

        async def _alert_unpaid(slug):
            alerted.append(slug)

        claimer = HumbleClaimer.__new__(HumbleClaimer)
        claimer.page = FakePage()
        claimer.sleep = _no_sleep
        claimer._page_text = _page_text
        claimer._eval_json = _eval_json
        claimer._alert_unpaid = _alert_unpaid
        return claimer, alerted

    def test_the_showcase_picks_the_current_month_with_its_order_page(self):
        claimer, alerted = self._claimer(
            showcases=[self.NOVEMBER_SHOWCASE], click_titles=["Mafia: The Old Country"])
        group = asyncio.run(claimer._pick_month())
        assert group["slug"] == "november-2026"
        assert group["order_url"] == self.ORDER_URL
        assert alerted == []
        # The membership read and one library load settle the whole pick:
        # no scan, no walk.
        assert claimer.page.visits == [URL_MEMBERSHIP, URL_LIBRARY]

    def test_the_next_showcase_title_is_tried_when_the_first_opens_no_dialog(self):
        claimer, alerted = self._claimer(
            showcases=[self.NOVEMBER_SHOWCASE], click_titles=["DYSMANTLE"])
        group = asyncio.run(claimer._pick_month())
        assert group["slug"] == "november-2026"
        assert group["order_url"] == self.ORDER_URL
        assert alerted == []

    def test_a_showcase_whose_dialog_names_no_order_page_concludes_nothing(self):
        # The showcase is positive evidence the month is his; a dialog
        # that names no order page after it is a site problem, so the
        # pick is inconclusive - no month worked, no alert sent - even
        # though the direct reading is taken twice before it is believed.
        claimer, alerted = self._claimer(
            showcases=[self.NOVEMBER_SHOWCASE], click_titles=["Mafia: The Old Country"],
            redeem_href=None)
        assert asyncio.run(claimer._pick_month()) is None
        assert alerted == []
        assert claimer.page.visits == [URL_MEMBERSHIP, URL_LIBRARY, URL_LIBRARY]

    def test_a_showcase_that_speaks_only_on_the_second_reading_still_picks(self):
        claimer, alerted = self._claimer(
            showcases=[None, self.NOVEMBER_SHOWCASE], click_titles=["Mafia: The Old Country"])
        group = asyncio.run(claimer._pick_month())
        assert group["slug"] == "november-2026"
        assert group["order_url"] == self.ORDER_URL
        assert alerted == []

    def test_no_showcase_and_an_older_month_in_the_library_alerts_and_falls_back(self):
        deck = [_link("", "Doors of Insanity: ReOpened", "/home/library/content/d1",
                      sub="Deck the Halls with Deckbuilders")]
        october = [_link("", "Row Game", "/home/library/content/o1",
                         sub="October 2026 Humble Choice")]
        claimer, alerted = self._claimer(
            membership_text="December 2026 Humble Choice - Member Home",
            showcases=[None], pages={1: deck, 2: october})
        group = asyncio.run(claimer._pick_month())
        assert group["slug"] == "october-2026"
        assert "order_url" not in group
        assert alerted == ["december-2026"]

    def test_no_showcase_and_the_current_month_in_the_library_picks_it_quietly(self):
        november = [_link("", "Mafia: The Old Country", "/home/library/content/n1",
                          sub="November 2026 Humble Choice")]
        claimer, alerted = self._claimer(showcases=[None], pages={1: november})
        group = asyncio.run(claimer._pick_month())
        assert group["slug"] == "november-2026"
        assert "order_url" not in group
        assert alerted == []

    def test_a_library_that_names_no_month_concludes_nothing(self):
        claimer, alerted = self._claimer(showcases=[None], pages={1: []})
        assert asyncio.run(claimer._pick_month()) is None
        assert alerted == []

    def test_no_current_month_named_picks_the_newest_owned(self):
        november = [_link("", "Mafia: The Old Country", "/home/library/content/n1",
                          sub="November 2026 Humble Choice")]
        claimer, alerted = self._claimer(
            membership_text="Member Home", pages={1: november})
        group = asyncio.run(claimer._pick_month())
        assert group["slug"] == "november-2026"
        assert alerted == []
        # No current month named, so no showcase is read at all: the
        # scan goes straight to the library's first page.
        assert claimer.page.visits == [URL_MEMBERSHIP, URL_LIBRARY]


def _newlib_link(title, bundle, href):
    """A library link as the library hands it over now: no heading, the bundle in its own line."""
    return {"heading": "", "title": title, "sub": bundle, "href": href}


class TestLibraryGroupsWalk:
    """The library read walks the pagination until the wanted month is read whole.

    Since November 22, 2026 the library lists a few games to a page with
    no bundle headings, so a month - twelve games - sits straddled
    across pages behind anything bought after it. The pick no longer
    reads months this way; the walk remains for gathering the cards of
    a month worked from its games' own pages, where every card counts.
    """

    DECK_PAGE = [
        _newlib_link("Doors of Insanity: ReOpened", "Deck the Halls with Deckbuilders",
                     "/home/library/content/d1"),
        _newlib_link("Lost for Swords", "Deck the Halls with Deckbuilders",
                     "/home/library/content/d2"),
    ]
    NOVEMBER_PAGE_2 = [
        _newlib_link("Check In, Please Playtest", "November 2026 Humble Choice",
                     "/home/library/content/n1"),
        _newlib_link("The Outer Worlds 2", "November 2026 Humble Choice",
                     "/home/library/content/n2"),
        _newlib_link("Mafia: The Old Country - Padrino Pack", "November 2026 Humble Choice",
                     "/home/library/content/n3"),
    ]
    NOVEMBER_PAGE_3 = [
        _newlib_link("Mafia: The Old Country - Gatto Nero Pack", "November 2026 Humble Choice",
                     "/home/library/content/n4"),
        _newlib_link("Mafia: The Old Country", "November 2026 Humble Choice",
                     "/home/library/content/n5"),
        _newlib_link("Squeakross: Home Squeak Home", "November 2026 Humble Choice",
                     "/home/library/content/n6"),
    ]
    OTHER_PAGE = [
        _newlib_link("An Older Game", "Some Older Bundle", "/home/library/content/o1"),
    ]

    def _claimer(self, pages):
        """A claimer whose library answers page by page: `pages` maps a page number to its links."""

        class FakePage:
            def __init__(self):
                self.visits = []
                self.current = ""

            async def get(self, url):
                self.visits.append(url)
                self.current = url
                return None

        async def _no_sleep(_seconds):
            return None

        claimer = HumbleClaimer.__new__(HumbleClaimer)
        claimer.page = FakePage()
        claimer.sleep = _no_sleep

        async def _eval_json(_js, _fallback=None):
            url = claimer.page.current
            number = int(url.rsplit("?page=", 1)[1]) if "?page=" in url else 1
            return pages.get(number, [])

        claimer._eval_json = _eval_json
        return claimer

    def test_a_month_straddling_pages_is_read_whole(self):
        claimer = self._claimer({1: self.DECK_PAGE, 2: self.NOVEMBER_PAGE_2,
                                 3: self.NOVEMBER_PAGE_3, 4: self.OTHER_PAGE})
        groups = asyncio.run(claimer._library_groups("november-2026"))
        assert [g["slug"] for g in groups] == ["november-2026"]
        assert [g["title"] for g in groups[0]["games"]] == [
            "Check In, Please Playtest", "The Outer Worlds 2",
            "Mafia: The Old Country - Padrino Pack",
            "Mafia: The Old Country - Gatto Nero Pack", "Mafia: The Old Country",
            "Squeakross: Home Squeak Home"]
        # The walk stops on the first page holding none of the month's
        # games, and never asks for page 5.
        assert claimer.page.visits == [URL_LIBRARY, URL_LIBRARY + "?page=2",
                                       URL_LIBRARY + "?page=3", URL_LIBRARY + "?page=4"]

    def test_with_no_current_month_the_newest_month_found_is_read_whole(self):
        claimer = self._claimer({1: self.NOVEMBER_PAGE_2, 2: self.NOVEMBER_PAGE_3,
                                 3: self.OTHER_PAGE})
        groups = asyncio.run(claimer._library_groups())
        assert [g["slug"] for g in groups] == ["november-2026"]
        assert len(groups[0]["games"]) == 6
        assert claimer.page.visits == [URL_LIBRARY, URL_LIBRARY + "?page=2",
                                       URL_LIBRARY + "?page=3"]

    def test_a_page_with_no_links_ends_the_walk(self):
        claimer = self._claimer({1: self.DECK_PAGE})
        groups = asyncio.run(claimer._library_groups("november-2026"))
        assert groups == []
        assert claimer.page.visits == [URL_LIBRARY, URL_LIBRARY + "?page=2"]

    def test_the_walk_is_capped_when_the_month_never_turns_up(self):
        pages = {}
        for number in range(1, 13):
            pages[number] = [_newlib_link(f"October Game {number}",
                                          "October 2026 Humble Choice",
                                          f"/home/library/content/oct{number}")]
        claimer = self._claimer(pages)
        groups = asyncio.run(claimer._library_groups("november-2026"))
        assert [g["slug"] for g in groups] == ["october-2026"]
        assert len(claimer.page.visits) == 10


class TestPageReadings:
    """Every reading _eval_json parses hands its answer back as JSON.

    The redeem link once did not, so the href the details dialog named
    could never be parsed ('Expecting value: line 1 column 1 (char 0)'
    in the run log) and the order page stayed unreachable.
    """

    def test_every_page_reading_answers_as_json(self):
        for js in (SIGNED_IN_JS, AUTH_FORM_JS, LIBRARY_LINKS_JS, SHOWCASE_JS,
                   REDEEM_LINK_JS, ORDER_ENTRIES_JS, CONTENT_ENTRY_JS):
            assert js.strip().startswith("JSON.stringify"), js[:60]


class TestClaimMonth:
    """A month is worked from its order page when the pick brought one, else from its games' pages.

    The walk's remaining job is in here: a month worked from its games'
    pages gets its cards read whole, from _library_groups - and a month
    whose pick brought an order page never touches the walk at all.
    """

    ORDER_URL = "https://www.humblebundle.com/home/library/order-uuid"

    def _claimer(self, *, order_entries=(), walked=()):
        claimer = HumbleClaimer.__new__(HumbleClaimer)
        claimer.notify_games = []
        calls = {"order": [], "content": [], "walk": 0, "settled": []}

        async def _order_entries(url):
            calls["order"].append(url)
            return list(order_entries)

        async def _content_entry(game):
            calls["content"].append(game["title"])
            return {"title": game["title"], "state": "revealed"}

        async def _library_groups(_slug=None):
            calls["walk"] += 1
            return list(walked)

        async def _claim_entry(entry, _url):
            calls["settled"].append(entry["title"])
            return "settled"

        claimer._order_entries = _order_entries
        claimer._content_entry = _content_entry
        claimer._library_groups = _library_groups
        claimer._claim_entry = _claim_entry
        return claimer, calls

    def test_a_pick_with_an_order_page_is_read_from_it_alone(self):
        entry = {"title": "A Game", "state": "revealed"}
        claimer, calls = self._claimer(order_entries=[entry])
        group = {"slug": "november-2026", "games": [], "order_url": self.ORDER_URL}
        asyncio.run(claimer._claim_month(group))
        assert calls["order"] == [self.ORDER_URL]
        assert calls["walk"] == 0
        assert calls["content"] == []
        assert calls["settled"] == ["A Game"]

    def test_a_pick_with_no_order_page_is_worked_from_its_cards_read_whole(self):
        # The pick's own games are only the scan page's few; the cards
        # the month is worked from are the walk's whole read.
        walked = [{"slug": "october-2026", "heading": "October 2026 Humble Choice",
                   "games": [{"title": "Row Game", "href": "/home/library/content/o1"},
                             {"title": "Second Game", "href": "/home/library/content/o2"}]}]
        claimer, calls = self._claimer(walked=walked)
        group = {"slug": "october-2026",
                 "games": [{"title": "Row Game", "href": "/home/library/content/o1"}]}
        asyncio.run(claimer._claim_month(group))
        assert calls["order"] == []
        assert calls["walk"] == 1
        assert calls["content"] == ["Row Game", "Second Game"]
        assert calls["settled"] == ["Row Game", "Second Game"]

    def test_an_order_page_that_reads_empty_falls_back_to_the_cards(self):
        walked = [{"slug": "november-2026", "heading": "November 2026 Humble Choice",
                   "games": [{"title": "A Game", "href": "/home/library/content/a1"}]}]
        claimer, calls = self._claimer(order_entries=[], walked=walked)
        group = {"slug": "november-2026", "games": [], "order_url": self.ORDER_URL}
        asyncio.run(claimer._claim_month(group))
        assert calls["order"] == [self.ORDER_URL]
        assert calls["walk"] == 1
        assert calls["content"] == ["A Game"]
        assert calls["settled"] == ["A Game"]


class TestRevealedKeyAlreadyHandled:
    """A revealed key that already stands under another game id is not queued again.

    The November 2026 incident: the rebuilt site's new game ids meant the
    first run under them recorded every November entry as existed, without
    its key - the "revealed outside the bot" standing. On the next run
    those codeless rows took the Padrino branch below and every key was
    saved as claimed, so the Steam queue posted all ten November keys to
    the duplicates channel: the games were already activated, or already
    posted, and DYSMANTLE went up twice. The two standings contradict each
    other across runs unless the Padrino save first checks whether the
    key already stands under another id. Identity is the string, not the
    title (Nathan's ruling): a sibling holding a different key settles
    nothing - that key is a new key and is saved like any other.

    No row at all is a different standing again: it means the key has
    never been processed, so the tests below save it and hand it to its
    store rather than treating an outside reveal as settled.
    """

    ORDER_URL = "https://www.humblebundle.com/home/library/order-uuid"
    GAME_ID = "order-uuid/squeakross-home-squeak-home"
    OLD_GAME_ID = "november-2026/squeakross-home-squeak-home"
    TITLE = "Squeakross: Home Squeak Home"
    KEY = "ABCDE-12345-FGHIJ"

    def _row(self, game_id, status, code=None):
        from src.core.database import ClaimedGame
        return ClaimedGame(store="humble", user="tester", game_id=game_id, title=self.TITLE,
                           url=self.ORDER_URL, status=status, code=code)

    def _settle(self, tmp_path, monkeypatch, rows):
        """Seed the rows, settle the revealed entry against them, and read the table back."""
        import src.stores.humble as humble
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from src.core.database import Base, ClaimedGame

        async def run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fgc.db'}")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(humble, "async_session", factory)
            async with factory() as session:
                session.add_all(rows)
                await session.commit()
            claimer = HumbleClaimer.__new__(HumbleClaimer)
            claimer.user, claimer.notify_games = "tester", []
            entry = classify_entry(_revealed_steam(), today=TODAY)
            entry["game_id"] = self.GAME_ID
            standing = await claimer._claim_entry(entry, self.ORDER_URL)
            async with factory() as session:
                saved = {r.game_id: r for r in (await session.execute(select(ClaimedGame))).scalars()}
            await engine.dispose()
            return standing, claimer.notify_games, saved

        return asyncio.run(run())

    @pytest.mark.parametrize("sibling_status", ["claimed and activated", "notified"])
    def test_the_same_key_under_another_id_is_not_saved_again(self, tmp_path, monkeypatch, sibling_status):
        # The November 2026 shape: the sibling row holds the IDENTICAL
        # string - activated from it, or posted as a duplicate from it
        # (the DYSMANTLE shape). The entry adds nothing.
        standing, notify, saved = self._settle(tmp_path, monkeypatch, [
            self._row(self.GAME_ID, "existed"),
            self._row(self.OLD_GAME_ID, sibling_status, code=self.KEY),
        ])
        assert standing == "settled"
        assert saved[self.GAME_ID].status == "existed" and saved[self.GAME_ID].code is None
        assert notify == [{"title": self.TITLE, "url": self.ORDER_URL, "status": "existed"}]

    @pytest.mark.parametrize("sibling_status", ["claimed and activated", "notified"])
    def test_a_different_key_under_another_id_is_saved_and_queued(self, tmp_path, monkeypatch, sibling_status):
        # Nathan's ruling: the sibling settled a DIFFERENT string for the
        # same game, so this key is a new key - the Padrino save runs and
        # it queues like any other, to be posted as a duplicate in its
        # own right if the game is owned.
        standing, notify, saved = self._settle(tmp_path, monkeypatch, [
            self._row(self.GAME_ID, "existed"),
            self._row(self.OLD_GAME_ID, sibling_status, code="ZZZZZ-99999-YYYYY"),
        ])
        assert standing == "settled"
        assert saved[self.GAME_ID].status == "claimed" and saved[self.GAME_ID].code == self.KEY
        assert json.loads(saved[self.GAME_ID].extra)["external_store"] == "steam"
        assert notify == [{"title": self.TITLE, "url": self.ORDER_URL, "status": "claimed"}]

    def test_a_key_with_no_sibling_is_saved_and_queued(self, tmp_path, monkeypatch):
        # The Padrino shape, unchanged: the reveal was recorded without the
        # key, and no other row ever settled the title, so the key is saved.
        standing, notify, saved = self._settle(tmp_path, monkeypatch, [
            self._row(self.GAME_ID, "existed"),
        ])
        assert standing == "settled"
        assert saved[self.GAME_ID].status == "claimed" and saved[self.GAME_ID].code == self.KEY
        assert json.loads(saved[self.GAME_ID].extra)["external_store"] == "steam"
        assert notify == [{"title": self.TITLE, "url": self.ORDER_URL, "status": "claimed"}]

    def test_a_revealed_key_with_no_row_is_saved_and_queued(self, tmp_path, monkeypatch):
        # Nathan's ruling: no row at all means the key has never been
        # processed. A reveal outside the bot can still be unclaimed, so
        # the key is saved exactly like one the bot revealed itself.
        standing, notify, saved = self._settle(tmp_path, monkeypatch, [])
        assert standing == "settled"
        assert saved[self.GAME_ID].status == "claimed" and saved[self.GAME_ID].code == self.KEY
        assert json.loads(saved[self.GAME_ID].extra)["external_store"] == "steam"
        assert notify == [{"title": self.TITLE, "url": self.ORDER_URL, "status": "claimed"}]

    def test_a_revealed_key_with_no_row_is_a_waiting_steam_key(self, tmp_path, monkeypatch):
        # The saved row is a genuine handoff, not just a record: it
        # satisfies the Steam queue's own waiting-key test, so the
        # redeemer will work it and make the duplicate check.
        from src.stores.steam import is_waiting_steam_key

        _standing, _notify, saved = self._settle(tmp_path, monkeypatch, [])
        row = saved[self.GAME_ID]
        assert is_waiting_steam_key(row.status, row.code, row.extra) is True
