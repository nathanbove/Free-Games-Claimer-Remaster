"""Spare GOG codes: the library reading and the divert decision.

A Prime Gaming code whose game is already in your GOG library is a spare:
posted to the duplicates channel instead of redeemed. The first classes
are the pure pieces of that decision; the last covers the divert's own
posting against a real database.
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


class TestDivertPostsOncePerCode:
    """The divert itself posts at most once per code, not just the decision to divert.

    The pending-codes pass works from snapshots taken before it starts,
    so two rows carrying one code - the same code saved again under a
    new game id - both read as divertable from their snapshots. The
    first divert marks the code's rows and posts; the second settles
    its rows the same way but must not post the code a second time.
    """

    CODE = "TKT8ED0BC5D94D2EBC"
    GOG_EXTRA = '{"external_store": "gog"}'

    def _row(self, game_id, status, extra=None):
        from src.core.database import ClaimedGame
        return ClaimedGame(store="prime", user="tester", game_id=game_id,
                           title="Doom", url="https://www.gog.com/redeem",
                           status=status, code=self.CODE,
                           extra=self.GOG_EXTRA if extra is None else extra)

    def _divert(self, tmp_path, monkeypatch, rows, snapshots):
        """Seed the rows, divert the named snapshots in order, and read the table back."""
        import asyncio
        from types import SimpleNamespace

        import src.core.database as database
        import src.core.notifier as notifier
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from src.core.database import Base, ClaimedGame
        from src.stores.gog import GOGClaimer

        posted = []

        async def run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fgc.db'}")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(database, "async_session", factory)

            async def record_duplicates(message):
                posted.append(message)

            monkeypatch.setattr(notifier, "notify_duplicates", record_duplicates)
            async with factory() as session:
                session.add_all(rows)
                await session.commit()
            async with factory() as session:
                loaded = {r.game_id: r for r in (await session.execute(select(ClaimedGame))).scalars()}
                session.expunge_all()
            stub = SimpleNamespace(notify_games=[])
            for game_id in snapshots:
                await GOGClaimer._divert_extra_key(stub, loaded[game_id])
            async with factory() as session:
                saved = {r.game_id: (r.status, extra_key_posted(r.extra)) for r in
                         (await session.execute(select(ClaimedGame))).scalars()}
            await engine.dispose()
            return posted, saved

        return asyncio.run(run())

    def test_two_rows_for_one_code_post_once(self, tmp_path, monkeypatch):
        posted, saved = self._divert(tmp_path, monkeypatch, [
            self._row("prime/one", "claimed"),
            self._row("prime/two", "claimed"),
        ], ["prime/one", "prime/two"])
        assert len(posted) == 1 and self.CODE in posted[0]
        assert saved["prime/one"] == ("notified", True)
        assert saved["prime/two"] == ("notified", True)

    def test_a_code_posted_in_an_earlier_run_is_not_posted_again(self, tmp_path, monkeypatch):
        flagged = json.dumps({"external_store": "gog", "extra_key_posted": True})
        posted, saved = self._divert(tmp_path, monkeypatch, [
            self._row("prime/one", "notified", extra=flagged),
            self._row("prime/two", "claimed"),
        ], ["prime/two"])
        assert posted == []
        assert saved["prime/two"] == ("notified", True)
