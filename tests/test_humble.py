"""Humble Choice: which months count, which keys get revealed, and what the answers mean.

The orders below are shaped like the ones Humble's own API hands the keys page:
a Choice month is an order whose product is a subscription, and each game in it
is a third-party key (TPK) that gains a `redeemed_key_val` only once revealed.
"""

from datetime import date

from src.stores.humble import (
    choose_identifiers,
    choose_outcome,
    payload_snippet,
    extract_tpks,
    is_choice_order,
    membership_slug,
    month_display_name,
    month_lineup,
    month_slugs,
    order_created,
    page_gamekey,
    plan_reveals,
    reveal_outcome,
    short_identifier,
    tpk_state,
    unchosen_tpks,
)


def _order(created="2026-10-06T17:00:00", machine="october_2026_choice", category="subscription", tpks=()):
    return {
        "gamekey": "abc123",
        "created": created,
        "product": {"machine_name": machine, "human_name": "October 2026 Choice", "category": category},
        "tpkd_dict": {"all_tpks": list(tpks)},
    }


def _tpk(name="A Game", machine="agame_choice_steam", key_type="steam", index=0, key=None, expired=False):
    return {
        "human_name": name,
        "machine_name": machine,
        "key_type": key_type,
        "key_index": index,
        "redeemed_key_val": key,
        "is_expired": expired,
        "expiry_date": "2027-11-03T17:00:00" if not expired else "2020-01-01T00:00:00",
    }


class TestWhichMonthsCount:
    def test_a_subscription_product_is_a_choice_month(self):
        assert is_choice_order(_order()) is True

    def test_a_month_named_choice_counts_without_the_category(self):
        assert is_choice_order(_order(category="bundle")) is True

    def test_an_ordinary_bundle_is_not_a_choice_month(self):
        order = _order(machine="great_games_bundle", category="bundle")
        order["product"]["human_name"] = "Great Games Bundle"
        assert is_choice_order(order) is False

    def test_an_unreadable_date_reads_as_no_date(self):
        assert order_created(_order(created="not a date")) is None
        assert order_created(_order(created="")) is None

    def test_a_zulu_stamp_reads_as_utc(self):
        created = order_created(_order(created="2026-10-06T17:00:00Z"))
        assert created is not None and created.tzinfo is not None


class TestExtractTpks:
    def test_the_fields_the_run_decides_on(self):
        (tpk,) = extract_tpks(_order(tpks=[_tpk(key="XXXXX-YYYYY-ZZZZZ")]))
        assert tpk["gamekey"] == "abc123"
        assert tpk["title"] == "A Game"
        assert tpk["key_type"] == "steam"
        assert tpk["revealed_key"] == "XXXXX-YYYYY-ZZZZZ"
        assert tpk["expired"] is False

    def test_an_order_without_keys_gives_none(self):
        assert extract_tpks(_order()) == []
        assert extract_tpks({}) == []

    def test_a_bad_key_index_reads_as_zero(self):
        (tpk,) = extract_tpks(_order(tpks=[_tpk(index="soon")]))
        assert tpk["key_index"] == 0


class TestTpkState:
    def test_revealed_expired_waiting(self):
        assert tpk_state(extract_tpks(_order(tpks=[_tpk(key="K")]))[0]) == "revealed"
        assert tpk_state(extract_tpks(_order(tpks=[_tpk(expired=True)]))[0]) == "expired"
        assert tpk_state(extract_tpks(_order(tpks=[_tpk()]))[0]) == "waiting"


class TestPlanReveals:
    def test_only_waiting_keys_are_planned(self):
        tpks = extract_tpks(_order(tpks=[
            _tpk(name="Waiting"),
            _tpk(name="Done", machine="done_choice_steam", key="XXXXX-11111-22222"),
            _tpk(name="Too Late", machine="late_choice_steam", expired=True),
        ]))
        assert [t["title"] for t in plan_reveals(tpks)] == ["Waiting"]

    def test_the_steam_variant_of_a_platform_choice_wins(self):
        tpks = extract_tpks(_order(tpks=[
            _tpk(machine="agame_choice_epic_keyless", key_type="epic"),
            _tpk(machine="agame_choice_steam", key_type="steam"),
        ]))
        planned = plan_reveals(tpks)
        assert [t["machine_name"] for t in planned] == ["agame_choice_steam"]

    def test_an_epic_only_variant_is_still_planned(self):
        tpks = extract_tpks(_order(tpks=[_tpk(machine="agame_choice_epic_keyless", key_type="epic")]))
        assert len(plan_reveals(tpks)) == 1

    def test_the_same_variant_twice_is_revealed_once(self):
        tpks = extract_tpks(_order(tpks=[_tpk(), _tpk()]))
        assert len(plan_reveals(tpks)) == 1

    def test_two_different_games_are_both_planned(self):
        tpks = extract_tpks(_order(tpks=[
            _tpk(),
            _tpk(name="B Game", machine="bgame_choice_steam"),
        ]))
        assert len(plan_reveals(tpks)) == 2


class TestRevealOutcome:
    def test_success_is_a_reveal(self):
        assert reveal_outcome(200, {"success": True, "key": "XXXXX"}) == "revealed"

    def test_a_refused_session_stops_the_run(self):
        assert reveal_outcome(401, {}) == "blocked"
        assert reveal_outcome(403, {"success": False}) == "blocked"

    def test_already_revealed_is_its_own_answer(self):
        assert reveal_outcome(200, {"success": False, "errors": {"key": ["Key already redeemed"]}}) == "already"

    def test_anything_else_failed(self):
        assert reveal_outcome(200, {"success": False}) == "failed"
        assert reveal_outcome(500, None) == "failed"


class TestChooseOutcome:
    def test_success_counts(self):
        assert choose_outcome(200, {"success": True}) is True

    def test_wording_alone_does_not(self):
        # An error echoing the request's own words is not a confirmation:
        # reading one as success once left a game "chosen" in the log
        # while Humble created no key entry for it.
        assert choose_outcome(200, {"success": False, "error": "Content already chosen"}) is False
        assert choose_outcome(200, {"success": False, "chosen_identifiers": ["x"]}) is False

    def test_a_plain_refusal_does_not(self):
        assert choose_outcome(403, {}) is False
        assert choose_outcome(200, {"success": False}) is False


class TestPayloadSnippet:
    def test_a_revealed_key_is_masked(self):
        snippet = payload_snippet({"success": True, "key": "ABCDE-FGHIJ-KLMNO"})
        assert "ABCDE" not in snippet
        assert '"success": true' in snippet

    def test_error_text_survives(self):
        assert "already redeemed" in payload_snippet({"errors": ["Key already redeemed"]})

    def test_no_body(self):
        assert payload_snippet(None) == "(no JSON body)"


class TestMonthSlugs:
    """The months a run probes for its month, newest first."""

    def test_before_the_first_tuesday_the_lineup_is_still_last_months(self):
        # Sunday 2026-11-01: November's lineup lands Tuesday the 3rd.
        assert month_slugs(date(2026, 11, 1))[:2] == ["october-2026", "september-2026"]

    def test_from_the_first_tuesday_the_new_month_leads(self):
        assert month_slugs(date(2026, 11, 3))[0] == "november-2026"
        assert month_slugs(date(2026, 10, 6))[0] == "october-2026"
        assert month_slugs(date(2026, 10, 5))[0] == "september-2026"

    def test_stepping_back_crosses_the_year(self):
        assert month_slugs(date(2027, 1, 1))[:2] == ["december-2026", "november-2026"]


class TestPageGamekey:
    """A membership page names the signed-in reader's order for the month."""

    def _page(self, gamekey):
        import json as _json

        blob = {"contentChoiceData": {"game_data": {
            "somegame": {"tpkds": [{"machine_name": "somegame_choice_steam",
                                    "gamekey": gamekey}]},
        }}}
        return '<script>var s = {"contentChoiceOptions": ' + _json.dumps(blob) + "};</script>"

    def test_the_named_order_is_read(self):
        assert page_gamekey(self._page("abc123")) == "abc123"

    def test_a_month_that_is_not_yours_names_none(self):
        assert page_gamekey(self._page(None)) is None
        assert page_gamekey("<html>no data</html>") is None
        assert page_gamekey(None) is None


class TestShortIdentifier:
    """The choosing call takes the short form of a key entry's name."""

    def test_the_shapes_humble_actually_uses(self):
        assert short_identifier("zerohour_row_choice_steam") == "zerohour"
        assert short_identifier("chantsofsennaar_choice_steam") == "chantsofsennaar"
        assert short_identifier("mightmushroom_playtest_choice_steam") == "mightmushroom"
        assert short_identifier("deeprockgalactic_survivor_choice_steam") == "deeprockgalactic_survivor"

    def test_humbles_own_typo_still_reads(self):
        assert short_identifier("road96_europe_hoice_steam") == "road96"

    def test_names_that_are_not_choice_content_have_none(self):
        assert short_identifier("ignplus_october_choicecoupon_2026") is None
        assert short_identifier("plain_old_steam") is None
        assert short_identifier("") is None


class TestChooseIdentifiers:
    def test_one_per_game_in_plan_order(self):
        planned = [
            {"machine_name": "rowgame_row_choice_steam"},
            {"machine_name": "deeprockgalactic_survivor_choice_steam",
             "identifier": "deeprockgalactic_survivor"},
            {"machine_name": "rowgame_row_choice_steam"},
            {"machine_name": "some_bundle_key"},
        ]
        assert choose_identifiers(planned) == ["rowgame", "deeprockgalactic_survivor"]


class TestMembershipSlug:
    def test_derived_from_the_product_name(self):
        assert membership_slug(_order()) == "october-2026"

    def test_the_products_own_url_wins(self):
        order = _order()
        order["product"]["choice_url"] = "/membership/march-2026"
        assert membership_slug(order) == "march-2026"


def _lineup_page():
    """A membership page's embedded data, shaped like the real October 2026 one."""
    import json as _json

    blob = {
        "productMachineName": "october_2026_choice",
        "contentChoiceData": {
            "display_order": ["rowgame", "deeprockgalactic_survivor", "ignplus_choicecoupon_2025"],
            "game_data": {
                "rowgame": {"title": "Row Game", "tpkds": [
                    {"machine_name": "rowgame_row_choice_steam", "key_type": "steam",
                     "visible": True, "is_expired": False,
                     "expiration_date|datetime": "2027-05-04T17:00:00"}]},
                "deeprockgalactic_survivor": {"title": "Deep Rock Galactic: Survivor", "tpkds": [
                    {"machine_name": "deeprockgalactic_survivor_choice_steam", "key_type": "steam",
                     "visible": True, "is_expired": False, "steam_app_id": 2321470,
                     "expiration_date|datetime": "2027-05-04T17:00:00"}]},
                "ignplus_choicecoupon_2025": {"title": "Get One Month of IGN Plus", "tpkds": [
                    {"machine_name": "ignplus_october_choicecoupon_2026", "key_type": "generic",
                     "visible": True, "is_expired": False,
                     "expiration_date|datetime": "2026-12-01T17:59:59"}]},
            },
        },
    }
    return '<script>var state = {"contentChoiceOptions": ' + _json.dumps(blob) + ', "other": 1};</script>'


class TestMonthLineup:
    """The month's games come from its membership page, not the order."""

    def test_the_games_in_display_order_and_no_coupon(self):
        lineup = month_lineup(_lineup_page(), "october_2026_choice")
        assert [game["title"] for game in lineup] == ["Row Game", "Deep Rock Galactic: Survivor"]
        assert lineup[1]["identifier"] == "deeprockgalactic_survivor"
        assert lineup[1]["tpks"][0]["machine_name"] == "deeprockgalactic_survivor_choice_steam"
        assert lineup[1]["tpks"][0]["key_type"] == "steam"

    def test_another_months_page_is_not_believed(self):
        assert month_lineup(_lineup_page(), "september_2026_choice") is None

    def test_the_storefront_variant_of_the_month_is_believed(self):
        page = _lineup_page().replace("october_2026_choice", "october_2026_choice_storefront")
        assert month_lineup(page, "october_2026") is not None

    def test_a_page_without_the_data_reads_as_no_lineup(self):
        assert month_lineup("<html>nothing here</html>", "october_2026_choice") is None
        assert month_lineup("", "october_2026_choice") is None
        assert month_lineup(None, "october_2026_choice") is None


class TestUnchosenTpks:
    """A game never selected on the site has no key entry; this stands one in."""

    def test_only_the_games_the_order_cannot_see(self):
        lineup = month_lineup(_lineup_page(), "october_2026_choice")
        order = _order(tpks=[_tpk(name="Row Game", machine="rowgame_row_choice_steam")])
        missing = unchosen_tpks(order, lineup)
        assert len(missing) == 1
        (tpk,) = missing
        assert tpk["title"] == "Deep Rock Galactic: Survivor"
        assert tpk["machine_name"] == "deeprockgalactic_survivor_choice_steam"
        assert tpk["key_type"] == "steam"
        assert tpk["gamekey"] == "abc123"
        assert tpk["identifier"] == "deeprockgalactic_survivor"
        assert tpk["steam_app_id"] == 2321470
        assert tpk["unchosen"] is True
        assert tpk_state(tpk) == "waiting"




class TestMonthDisplayName:
    def test_a_slug_reads_as_a_month(self):
        assert month_display_name("november-2026") == "November 2026"
        assert month_display_name("january-2027") == "January 2027"
