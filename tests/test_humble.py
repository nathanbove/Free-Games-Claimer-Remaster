"""Humble Choice on the rebuilt site: which month gets worked, and what an entry on its order page means.

Since the November 2026 redesign there is no order API to read: a run reads
pages. The fixtures below are shaped like the entries the new library's
order page hands the browser - a title, the entry's buttons, its whole
text, and its platform icon's name - which is all classify_entry decides on.
"""

import asyncio
import json
from datetime import date

from src.stores.humble import (
    AUTH_FORM_JS,
    CONTENT_ENTRY_JS,
    LIBRARY_LINKS_JS,
    ORDER_ENTRIES_JS,
    REDEEM_LINK_JS,
    SIGNED_IN_JS,
    HumbleClaimer,
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


class TestOrderUrl:
    """The order page is reached through the details dialog's 'Go to Redeem' link.

    Every reading _eval_json parses hands its answer back as JSON. The
    redeem link once did not, so the href the dialog named could never be
    parsed ('Expecting value: line 1 column 1 (char 0)' in the run log)
    and every run fell back to the games' own pages, where the entries
    only the order page lists - a month's DLC packs - are invisible.
    """

    def test_every_page_reading_answers_as_json(self):
        for js in (SIGNED_IN_JS, AUTH_FORM_JS, LIBRARY_LINKS_JS,
                   REDEEM_LINK_JS, ORDER_ENTRIES_JS, CONTENT_ENTRY_JS):
            assert js.strip().startswith("JSON.stringify"), js[:60]

    def _claimer(self, href):
        class FakePage:
            async def get(self, url):
                return None

            async def evaluate(self, js, await_promise=False):
                if "View details for" in js:
                    return True
                if "Go to Redeem" in js:
                    # A browser running a JSON.stringify script hands back
                    # the quoted string, which is what _eval_json parses.
                    return json.dumps(href)
                raise AssertionError(f"unexpected script: {js[:60]}")

        async def _no_sleep(_seconds):
            return None

        claimer = HumbleClaimer.__new__(HumbleClaimer)
        claimer.page = FakePage()
        claimer.sleep = _no_sleep
        return claimer

    def test_the_dialogs_link_becomes_the_order_url(self):
        claimer = self._claimer("/home/library/order-uuid")
        group = {"games": [{"title": "A Game", "href": "/home/library/content/aaa"}]}
        url = asyncio.run(claimer._order_url(group))
        assert url == "https://www.humblebundle.com/home/library/order-uuid"

    def test_a_dialog_with_no_link_falls_back(self):
        claimer = self._claimer(None)
        group = {"games": [{"title": "A Game", "href": "/home/library/content/aaa"}]}
        assert asyncio.run(claimer._order_url(group)) is None


class TestRevealedKeyAlreadyHandled:
    """A revealed key whose title was already settled under another game id is not queued again.

    The November 2026 incident: the rebuilt site's new game ids meant the
    first run under them recorded every November entry as existed, without
    its key - the "revealed outside the bot" standing. On the next run
    those codeless rows took the Padrino branch below and every key was
    saved as claimed, so the Steam queue posted all ten November keys to
    the duplicates channel: the games were already activated, or already
    posted, and DYSMANTLE went up twice. The two standings contradict each
    other across runs unless the Padrino save first checks whether a
    sibling row already settled the title.
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

    def test_a_key_activated_under_another_id_is_not_saved_again(self, tmp_path, monkeypatch):
        standing, notify, saved = self._settle(tmp_path, monkeypatch, [
            self._row(self.GAME_ID, "existed"),
            self._row(self.OLD_GAME_ID, "claimed and activated", code="ZZZZZ-99999-YYYYY"),
        ])
        assert standing == "settled"
        assert saved[self.GAME_ID].status == "existed" and saved[self.GAME_ID].code is None
        assert notify == [{"title": self.TITLE, "url": self.ORDER_URL, "status": "existed"}]

    def test_a_key_posted_as_a_duplicate_is_not_saved_again(self, tmp_path, monkeypatch):
        # The DYSMANTLE shape: the sibling row is the duplicate posting itself.
        standing, notify, saved = self._settle(tmp_path, monkeypatch, [
            self._row(self.GAME_ID, "existed"),
            self._row(self.OLD_GAME_ID, "notified", code="ZZZZZ-99999-YYYYY"),
        ])
        assert standing == "settled"
        assert saved[self.GAME_ID].status == "existed" and saved[self.GAME_ID].code is None
        assert notify == [{"title": self.TITLE, "url": self.ORDER_URL, "status": "existed"}]

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
