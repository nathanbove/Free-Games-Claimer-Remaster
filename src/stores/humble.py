"""Humble Bundle: claims the games of a Humble Choice subscription.

"Claiming" a Choice game means revealing its key. The month itself is never
unlocked by the bot: early unlock is a payment, and a subscriber's month is
created by the automatic billing on the last Tuesday, so by the time a run
looks, a paid month either exists as an order or it does not.

What a run does, in order:

  1. Sign in (HB_EMAIL / HB_PASSWORD, an authenticator code if asked, else you
     over VNC). Humble's own JSON API, asked from inside the signed-in page,
     says whether the session is alive.
  2. Find the one month to work on from its membership page. The page is
     the source: it carries the month's games (step 3) and, for the
     signed-in reader, names their order for the month in its own data.
     The run reads the current month's page first, stepping back a month
     at a time when a month names no order (skipped, or not billed yet);
     the order a page names is opened — the one full order read of the
     run — for the state of its keys. When the newest page lists its
     games but names no order, the month is posted but not paid for,
     and the run says so on the issues feed, once per month. Only when no recent page names an
     order does the run fall back to the order list, opening orders from
     its head until the newest Choice month turns up. The pick is a month
     from the current plan, the single all-games plan sold since February
     2022. Pick-limited months from before that switch are left alone:
     their unpicked games are a choice only you can make, and the bot
     must not spend picks. Older current-plan months are left alone too:
     unrevealed keys in them are a back catalogue to settle by hand, not
     a sweep for a monthly run, so they stay exactly as they are.
  3. Work from the lineup the month's page gave, because the order alone
     cannot see a game that was never selected on the site: it has
     no key entry there yet. Lineup games without an entry join the run as
     waiting games in their own right. For every
     game whose key is still unrevealed and unexpired, the run walks the
     site's own claim sequence for that one game: choose it (by the short
     identifier the site's claim button sends), read the order once more
     - choosing a game that had no entry is what makes Humble create it -
     then reveal its key. One game at a time: a single call choosing the
     whole month answered success while creating an entry for only the
     game that already had one. Both calls run inside the page, because
     Humble's anti-bot check turns away plain HTTP clients on those two
     endpoints while reads pass.
  4. Save each revealed key in the database, tagged with the store it
     belongs to. A Steam key is then activated by the Steam module in the
     same run when `steam` is in STORES too, and a GOG key is redeemed by
     the GOG module when `gog` is, exactly like the keys Prime Gaming and
     Fanatical hand out. Any other key waits in the database and on your
     Humble keys page, where a revealed key sits anyway.

A waiting key is revealed whether or not its game is already in your
library somewhere: duplicates are not this store's call. A revealed
Steam key waits in the database for the Steam module, which checks the
key queue against your Steam library when it works it - a key for a
game you already own is a spare there (posted to the duplicates
channel when one is configured, never activated on your account),
whichever store handed the key out. What this store does hand over is
the Steam app id the month's page named for a game, saved on the key's
row so that check can decide by the app id instead of by title.

One answer is a wait state, not an error: Humble can be out of keys for
a game (its reveal answer is "keys_depleted_email" - "Keys are
temporarily exhausted for this product"). There is nothing to fix and
nothing to do: the key simply is not there yet. The game stays
unrevealed, the run reports it as failed:keys-exhausted so the state
stays visible without reading as a fault, and because a run plans from
the order's live state, the next run tries it again on its own.

A linked-account grant has a wait state of its own: a Battle.net game's
reveal can answer "blizzard_pending" - "Your Battle.net grant is still
processing" - while the grant finishes on Blizzard's side. The same
shape applies: nothing to fix, the game stays unrevealed, the run
reports it as failed:grant-pending, and the next run tries it again.

Revealing is one-way: a revealed key can no longer be gifted from Humble,
and a month with a revealed key in it is no longer refundable. That is why
this store is opt-in (name `humble` in STORES) and why a key that has
already been revealed, by you or by an earlier run, is never touched again.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from src.core.claimer import BaseClaimer, OTP_KEY_ATTEMPTS, account_key
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.redact import mask_code
from src.core.run_state import needs_you

logger = logging.getLogger("fgc.humble")

SITE_HOST = "www.humblebundle.com"
URL_LOGIN = "https://www.humblebundle.com/login"
URL_KEYS = "https://www.humblebundle.com/home/keys"
URL_MEMBERSHIP = "https://www.humblebundle.com/membership/home"
API_ORDERS = "/api/v1/user/order"
API_ORDER = "/api/v1/order/{gamekey}?all_tpkds=true"
API_CHOOSE = "/humbler/choosecontent"
API_REDEEM = "/humbler/redeemkey"

# Choice became a single plan with every game included on 2022-02-01. Months
# bought before that are pick-limited, so their orders are never claimed from.
LEGACY_CUTOFF = datetime(2022, 2, 1, tzinfo=timezone.utc)

# One fetch, run inside the signed-in page: the session cookie and the CSRF
# token only exist there, and /humbler/* turns plain HTTP clients away.
FETCH_JS = """
    (async () => {
        const method = %s, url = %s, form = %s;
        const init = {method, credentials: 'include', headers: {'Accept': 'application/json'}};
        if (form) {
            const token = (document.cookie.match(/(?:^|; )csrf_cookie=([^;]*)/) || [])[1] || '';
            const body = new URLSearchParams();
            for (const [name, value] of form) body.append(name, value);
            init.headers['Content-Type'] = 'application/x-www-form-urlencoded';
            init.headers['csrf-prevention-token'] = decodeURIComponent(token);
            init.body = body;
        }
        try {
            const r = await fetch(url, init);
            return JSON.stringify({status: r.status, body: await r.text()});
        } catch (e) {
            return JSON.stringify({status: 0, body: '', error: String(e).slice(0, 120)});
        }
    })()
"""

# The sign-in form is drawn by script, so its fields are found by what they
# are, not by one build's ids: the e-mail box, the password box, the button.
LOGIN_FORM_JS = """
    JSON.stringify({
        href: location.href,
        email: !!document.querySelector('input[type="email"], input[name="username"], input#username'),
        password: !!document.querySelector('input[type="password"]'),
        code: !!document.querySelector('input[name*="code" i], input[autocomplete="one-time-code"]'),
        text: document.body ? document.body.innerText.slice(0, 400) : '',
    })
"""

FILL_LOGIN_JS = """
    (() => {
        const set = (el, value) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        };
        const email = document.querySelector('input[type="email"], input[name="username"], input#username');
        const password = document.querySelector('input[type="password"]');
        if (!email || !password) return false;
        set(email, %s);
        set(password, %s);
        const box = document.querySelector('input[name="remember"]');
        if (box && !box.checked) box.click();
        const button = document.querySelector('button[type="submit"], input[type="submit"]')
            || [...document.querySelectorAll('button')].find(b => /log\\s?in|sign\\s?in/i.test(b.textContent || ''));
        if (!button) return false;
        button.click();
        return true;
    })()
"""

FILL_CODE_JS = """
    (() => {
        const field = document.querySelector('input[name*="code" i], input[autocomplete="one-time-code"]');
        if (!field) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(field, %s);
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
        const button = document.querySelector('button[type="submit"], input[type="submit"]')
            || [...document.querySelectorAll('button')].find(b => /verify|continue|submit/i.test(b.textContent || ''));
        if (button) button.click();
        return true;
    })()
"""


def order_created(order: dict) -> datetime | None:
    """When an order was bought, None when Humble's stamp cannot be read."""
    raw = str((order or {}).get("created") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def is_choice_order(order: dict) -> bool:
    """True for a Humble Choice month: a subscription product, or one named Choice."""
    product = (order or {}).get("product") or {}
    if str(product.get("category") or "").strip().lower() == "subscription":
        return True
    name = f"{product.get('machine_name') or ''} {product.get('human_name') or ''}".lower()
    return "choice" in name


def _first_tuesday(year: int, month: int) -> date:
    """The first Tuesday of a month: the day its Choice lineup lands."""
    first = date(year, month, 1)
    return first + timedelta(days=(1 - first.weekday()) % 7)


def month_slugs(today: date, count: int = 4) -> list[str]:
    """Candidate membership-page slugs, newest first, for finding your month.

    The lineup in effect is the current month's from its first Tuesday
    on; before that it is still last month's. The run probes these in
    order and steps back when a month is not yours (skipped, or not
    billed yet), so a few months of slack are built in.
    """
    year, month = today.year, today.month
    if today < _first_tuesday(year, month):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    slugs = []
    for _ in range(max(1, count)):
        slugs.append(f"{calendar.month_name[month].lower()}-{year}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return slugs


def month_display_name(slug: str) -> str:
    """'november-2026' as 'November 2026': a month named the way its owner reads it."""
    name, _, year = str(slug or "").partition("-")
    return f"{name.capitalize()} {year}".strip()


def page_gamekey(page: str | None) -> str | None:
    """The order a membership page names for its month, or None.

    The page's per-game key data carries the gamekey of the signed-in
    reader's order for the month. Signed out, or for a month that is
    not the reader's, it names none — which is how the run tells a
    month of yours from a month that merely exists.
    """
    if not page:
        return None
    blob = _embedded_choice_options(page)
    if blob is None:
        return None
    data = blob.get("contentChoiceData") or {}
    for entry in (data.get("game_data") or {}).values():
        for tpkd in (entry or {}).get("tpkds") or []:
            gamekey = str(tpkd.get("gamekey") or "")
            if gamekey:
                return gamekey
    return None


def extract_tpks(order: dict) -> list[dict]:
    """An order's third-party keys (TPKs), one dict per key a game can hand out."""
    order = order or {}
    gamekey = str(order.get("gamekey") or "")
    found = []
    for tpk in ((order.get("tpkd_dict") or {}).get("all_tpks")) or []:
        try:
            key_index = int(tpk.get("key_index") or 0)
        except (TypeError, ValueError):
            key_index = 0
        try:
            steam_app_id = int(tpk.get("steam_app_id") or 0)
        except (TypeError, ValueError):
            steam_app_id = 0
        found.append({
            "gamekey": gamekey,
            "title": str(tpk.get("human_name") or "(untitled)"),
            "machine_name": str(tpk.get("machine_name") or ""),
            "key_type": str(tpk.get("key_type") or "").strip().lower(),
            "key_index": key_index,
            "revealed_key": tpk.get("redeemed_key_val") or None,
            "expired": bool(tpk.get("is_expired")),
            "expiry_date": str(tpk.get("expiry_date") or ""),
            "steam_app_id": steam_app_id,
        })
    return found


def tpk_state(tpk: dict) -> str:
    """Where one key stands before a run touches it: revealed, expired or waiting."""
    if (tpk or {}).get("revealed_key"):
        return "revealed"
    if (tpk or {}).get("expired"):
        return "expired"
    return "waiting"


def plan_reveals(tpks: list) -> list[dict]:
    """The waiting keys to reveal: unrevealed, unexpired, one per game and platform.

    A game offered as a Steam key or an Epic grant (a platform-choice title)
    keeps its Steam variant and drops the Epic one: a key can be activated,
    gifted or traded later, a keyless grant cannot be taken back. The same
    variant listed twice is revealed once.
    """
    steam_games = {(t["gamekey"], BaseClaimer._normalize_title(t["title"]))
                   for t in tpks or [] if "_choice_steam" in (t.get("machine_name") or "")}
    planned, seen = [], set()
    for tpk in tpks or []:
        if tpk_state(tpk) != "waiting":
            continue
        game = (tpk["gamekey"], BaseClaimer._normalize_title(tpk["title"]))
        machine = tpk.get("machine_name") or ""
        if "_choice_epic" in machine and game in steam_games:
            continue
        variant = game + (tpk.get("key_type") or "",)
        if ("_choice_steam" in machine or "_choice_epic" in machine) and variant in seen:
            continue
        seen.add(variant)
        planned.append(tpk)
    return planned


# A Choice key entry's name ends in _<platform>, in front of the word
# "choice" (or Humble's real-world typo "hoice"): chantsofsennaar_choice_steam,
# deeprockgalactic_survivor_choice_steam. A single regional or functional
# word can sit between the game's own name and that ending — row, naeu,
# europe, playtest — and is not part of the identifier the choosing call
# expects: mightmushroom_playtest_choice_steam is chosen as "mightmushroom".
_CHOICE_SUFFIX_RE = re.compile(r"^(.+)_c?hoice_([a-z][a-z_]*)$")
_CHOICE_MODIFIERS = frozenset({"row", "naeu", "europe", "playtest"})


def short_identifier(machine_name: str) -> str | None:
    """The identifier the choosing call expects for one key entry, or None.

    None for a name that is not Choice content at all: such an entry is
    never chosen, and is not a game in a month's lineup either. The exact
    identifier normally comes from the month's page (month_lineup); this
    derivation is the fallback for when the page could not be read, and
    resolves the one ambiguity — a game's own name can contain underscores
    too — by stripping only the modifier words Humble is known to insert.
    """
    match = _CHOICE_SUFFIX_RE.match((machine_name or "").strip().lower())
    if not match:
        return None
    base = match.group(1)
    head, _, last = base.rpartition("_")
    if head and last in _CHOICE_MODIFIERS:
        return head
    return base


def lineup_identifiers(lineup: list) -> dict:
    """Key-entry name -> the page identifier of its game, for a whole lineup."""
    identifiers = {}
    for game in lineup or []:
        for tpkd in game.get("tpks") or []:
            identifiers[tpkd["machine_name"]] = game["identifier"]
    return identifiers


def choose_identifiers(planned: list) -> list[str]:
    """The identifiers to choose for a plan, one per game, in plan order.

    A game that has no key entry yet carries the identifier its month's
    page gave it; a game that has one gives up the short form of its key
    entry's name. Games with neither are not Choice content and are left
    out: there is nothing to choose for them.
    """
    identifiers = []
    for tpk in planned or []:
        identifier = tpk.get("identifier") or short_identifier(tpk.get("machine_name") or "")
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def membership_slug(order: dict) -> str | None:
    """The /membership/<slug> path for an order's month, or None.

    The product's own choice_url wins when Humble provides one; otherwise
    the slug is the product's machine name in kebab case with the Choice
    suffix stripped (october_2026_choice -> october-2026).
    """
    product = (order or {}).get("product") or {}
    url = str(product.get("choice_url") or "").strip()
    if url:
        return url.strip("/").rsplit("/", 1)[-1] or None
    machine = str(product.get("machine_name") or "").lower()
    for suffix in ("_choice_storefront", "_choice"):
        if machine.endswith(suffix):
            machine = machine[: -len(suffix)]
            break
    return machine.replace("_", "-") or None


def _embedded_choice_options(page: str) -> dict | None:
    """The contentChoiceOptions object a membership page carries in its data.

    The page embeds its month as JSON inside a script block; the object is
    lifted out by matching its braces, because the block around it is not
    JSON as a whole. None when the marker or a readable object is absent.
    """
    marker = page.find('"contentChoiceOptions"')
    if marker < 0:
        return None
    start = page.find("{", marker)
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(page)):
        char = page[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    blob = json.loads(page[start:index + 1])
                except ValueError:
                    return None
                return blob if isinstance(blob, dict) else None
    return None


def month_lineup(page: str | None, product_machine: str = "") -> list | None:
    """A Choice month's games, from its membership page's own data.

    One entry per game: its page identifier (what the choosing call
    expects), its title, and the key entries it would hand out, in the
    page's display order. Items that are not games (a month's coupon, a
    discount) never appear: their key entries' names are not Choice
    content, which is also what keeps them unclaimable here. None when
    the page's data cannot be read or belongs to another month, so the
    caller can say plainly that the lineup went unchecked.
    """
    if not page:
        return None
    blob = _embedded_choice_options(page)
    if blob is None:
        return None
    blob_machine = str(blob.get("productMachineName") or "")
    if product_machine and blob_machine != product_machine and not blob_machine.startswith(product_machine + "_"):
        return None
    data = blob.get("contentChoiceData") or {}
    game_data = data.get("game_data") or {}
    if not isinstance(game_data, dict):
        return None
    lineup = []
    for identifier in data.get("display_order") or list(game_data):
        entry = game_data.get(identifier) or {}
        tpks = []
        for tpkd in entry.get("tpkds") or []:
            machine = str(tpkd.get("machine_name") or "")
            key_type = str(tpkd.get("key_type") or "").strip().lower()
            if short_identifier(machine) is None or key_type == "generic":
                continue
            if tpkd.get("visible") is False:
                continue
            tpks.append({
                "machine_name": machine,
                "key_type": key_type,
                "expired": bool(tpkd.get("is_expired")),
                "expiry_date": str(tpkd.get("expiration_date|datetime") or ""),
                "steam_app_id": tpkd.get("steam_app_id") or 0,
            })
        if not tpks:
            continue
        lineup.append({
            "identifier": str(identifier),
            "title": str(entry.get("title") or identifier),
            "tpks": tpks,
        })
    return lineup or None


def unchosen_tpks(order: dict, lineup: list) -> list[dict]:
    """Lineup games the order carries no key entry for yet, as waiting entries.

    Shaped like extract_tpks output so the rest of the run treats them the
    same, plus the page identifier the choosing call needs and an
    "unchosen" mark: choosing one is what makes Humble create its real
    key entry. A game lands here when it was never selected on the site —
    the order alone cannot see it at all.
    """
    present = {tpk["machine_name"] for tpk in extract_tpks(order)}
    gamekey = str((order or {}).get("gamekey") or "")
    missing = []
    for game in lineup or []:
        for tpkd in game.get("tpks") or []:
            if tpkd["machine_name"] in present:
                continue
            missing.append({
                "gamekey": gamekey,
                "title": game["title"],
                "machine_name": tpkd["machine_name"],
                "key_type": tpkd["key_type"],
                "key_index": 0,
                "revealed_key": None,
                "expired": tpkd["expired"],
                "expiry_date": tpkd["expiry_date"],
                "steam_app_id": tpkd.get("steam_app_id") or 0,
                "identifier": game["identifier"],
                "unchosen": True,
            })
    return missing


def reveal_outcome(http_status: int, payload) -> str:
    """What a redeemkey answer means: revealed, already, exhausted, pending, blocked or failed.

    "Blocked" is a 401/403: the session died or the anti-bot check answered,
    and no other key in this run would fare better, so the run stops there.
    "Exhausted" is Humble's key pool for the game standing empty
    ("keys_depleted_email"): a wait state on their side, not a fault here.
    "Pending" is a linked Battle.net grant still processing
    ("blizzard_pending"): the same kind of wait state.
    """
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("success") is True:
        return "revealed"
    if http_status in (401, 403):
        return "blocked"
    if payload.get("error") == "keys_depleted_email":
        return "exhausted"
    if payload.get("error") == "blizzard_pending":
        return "pending"
    said = json.dumps(payload).lower()
    if "exhausted" in said:
        return "exhausted"
    if "blizzard_pending" in said:
        return "pending"
    if "already" in said:
        return "already"
    return "failed"


def choose_outcome(http_status: int, payload) -> bool:
    """True only when Humble confirmed the choice outright.

    An answer that merely mentions choosing is not a confirmation: an error
    payload can echo the request's own field names, and reading one as
    success once left a game "chosen" in the log while Humble created no
    key entry for it. Whether a choice landed is proven by the order
    re-read that follows, not by wording.
    """
    payload = payload if isinstance(payload, dict) else {}
    return payload.get("success") is True


def choose_already(payload) -> bool:
    """True when Humble's choose answer is that the choice was already made.

    The observed shape is success:false with the refusal in the errors
    ("You've already made this choice."). Not a confirmation - the strict
    choose_outcome stays the judge of that - and not a problem either:
    the choice stands, which is all the reveal that follows needs.
    """
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("success") is True:
        return False
    return "already made this choice" in json.dumps(payload).lower()


def payload_snippet(payload) -> str:
    """What Humble answered, safe to log: any key value masked, the rest verbatim.

    The claim calls used to log only the HTTP status, so a 200 that carried
    no key was undiagnosable after the fact. This keeps the answer's shape
    (and its error text, when it sends any) in the log without ever
    printing a revealed key.
    """
    if payload is None:
        return "(no JSON body)"
    if isinstance(payload, dict):
        shown = {name: ("***" if name == "key" and value else value)
                 for name, value in payload.items()}
        text = json.dumps(shown, default=str)
    else:
        text = str(payload)
    return text[:240]


class HumbleClaimer(BaseClaimer):
    store_name = "humble"

    # ------------------------------------------------------------------
    # Humble's API, asked from inside the page
    # ------------------------------------------------------------------

    async def _fetch_raw(self, url: str, form: list | None = None) -> tuple[int, str]:
        """One request to humblebundle.com from the open page; (HTTP status, raw body)."""
        js = FETCH_JS % (
            json.dumps("POST" if form else "GET"),
            json.dumps(url),
            json.dumps(form) if form else "null",
        )
        try:
            raw = await self.page.evaluate(js, await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("Humble request to %s failed: %s", url, e)
            return 0, ""
        if answer.get("error"):
            logger.debug("Humble request to %s failed in the page: %s", url, answer["error"])
        return int(answer.get("status") or 0), str(answer.get("body") or "")

    async def _fetch(self, url: str, form: list | None = None) -> tuple[int, object]:
        """One request to humblebundle.com from the open page; (HTTP status, JSON or None)."""
        status, body = await self._fetch_raw(url, form)
        try:
            payload = json.loads(body) if body else None
        except (TypeError, ValueError):
            payload = None
        return status, payload

    async def _fetch_text(self, url: str) -> tuple[int, str]:
        """One GET from the open page, body unparsed: for pages, not API answers."""
        return await self._fetch_raw(url)

    async def _signed_in(self) -> bool:
        """Ask the order list: it answers 200 signed in and 401 signed out."""
        status, payload = await self._fetch(API_ORDERS)
        if status == 200 and isinstance(payload, list):
            return True
        logger.debug("Order list answered %s, not a signed-in session.", status)
        return False

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login_form(self) -> dict:
        try:
            raw = await self.page.evaluate(LOGIN_FORM_JS)
            return json.loads(raw) if isinstance(raw, str) else {}
        except Exception as e:
            logger.debug("Could not read the sign-in page: %s", e)
            return {}

    async def _fill_code(self) -> bool:
        """One authenticator code from HB_OTP_KEY into the code box, when there is one."""
        if not cfg.hb_otp_key:
            return False
        self._last_totp = await self._fresh_totp(cfg.hb_otp_key, self._last_totp)
        try:
            filled = await self.page.evaluate(FILL_CODE_JS % json.dumps(self._last_totp))
        except Exception as e:
            logger.debug("Could not type the authenticator code: %s", e)
            return False
        if filled:
            await self.sleep(5)
        return bool(filled)

    async def _ensure_logged_in(self) -> bool:
        """Signed in to Humble: by itself with HB_EMAIL / HB_PASSWORD, else you over VNC."""
        await self.page.get(URL_KEYS)
        await self.sleep(4)
        if await self._human_challenge_present():
            if not await self._wait_out_challenge("Humble Bundle", store_key=self.store_name):
                return False
        if await self._signed_in():
            self.log_signed_in(account_key(cfg.hb_email, None))
            return True

        if cfg.hb_email and cfg.hb_password:
            logger.info("Signing in to Humble Bundle as %s…", account_key(cfg.hb_email, None))
            await self.page.get(URL_LOGIN)
            await self.sleep(4)
            try:
                sent = await self.page.evaluate(
                    FILL_LOGIN_JS % (json.dumps(cfg.hb_email), json.dumps(cfg.hb_password)))
            except Exception as e:
                logger.debug("Could not fill the sign-in form: %s", e)
                sent = False
            if sent:
                await self.sleep(5)
                codes = 0
                for _ in range(10):
                    if await self._signed_in():
                        self.log_signed_in(account_key(cfg.hb_email, None))
                        return True
                    form = await self._login_form()
                    # A code box means the password went through and a second step is left.
                    if form.get("code"):
                        if codes < OTP_KEY_ATTEMPTS and await self._fill_code():
                            codes += 1
                            continue
                        break
                    if not form.get("password"):
                        break
                    await self.sleep(3)
            else:
                logger.debug("The sign-in form was not on %s", await self._page_brief())

        msg = self._vnc_notice(
            "Humble Bundle: login needs you",
            "The automated sign-in did not finish. Open the browser and sign in to Humble Bundle; "
            "a code Humble e-mails you (its Guard check) is typed there too.",
        )
        if await self._wait_for_vnc_login(self._signed_in, custom_msg=msg, store_key=self.store_name):
            self.log_signed_in(account_key(cfg.hb_email, None))
            return True
        return False

    # ------------------------------------------------------------------
    # Choice months
    # ------------------------------------------------------------------

    async def _open_order(self, gamekey: str) -> dict | None:
        """One order in full, keys included; None when Humble will not hand it over."""
        status, order = await self._fetch(API_ORDER.format(gamekey=gamekey))
        if status != 200 or not isinstance(order, dict):
            logger.debug("Order %s answered %s, skipped.", gamekey, status)
            return None
        return order

    async def _probe_month(self, slug: str) -> dict:
        """One membership page, read for its lineup and the order it names.

        The answer carries "fetched" (the page itself answered), "lineup"
        (its games, or None when its data cannot be read) and "gamekey"
        (the order it names for the signed-in reader, or None when the
        month is not theirs: skipped, or not billed yet).
        """
        status, page = await self._fetch_text(f"/membership/{slug}")
        if status != 200 or not page:
            return {"fetched": False, "lineup": None, "gamekey": None}
        lineup = month_lineup(page, slug.replace("-", "_"))
        if lineup is None:
            return {"fetched": True, "lineup": None, "gamekey": None}
        return {"fetched": True, "lineup": lineup, "gamekey": page_gamekey(page)}

    async def _settle_month(self, order: dict, slug: str) -> dict | None:
        """The picked month, checked before anything is claimed from it.

        The order a page names must be that month's Choice order and a
        current-plan one: anything else means the data disagrees with
        itself, and the run stops loudly rather than claim from the
        wrong month. A pick-limited month is left alone, as ever.
        Returns {"order": ..., "human_name": ...} or None.
        """
        product = order.get("product") or {}
        machine = str(product.get("machine_name") or "")
        if not is_choice_order(order) or not machine.startswith(slug.replace("-", "_")):
            logger.warning("The order the membership page named is not the %s Choice "
                           "month, so nothing is claimed this run.", slug)
            return None
        human = str(product.get("human_name") or machine)
        created = order_created(order)
        if created is not None and created < LEGACY_CUTOFF:
            logger.info("'%s' is a pick-limited month from before February 2022, "
                        "left alone: its unpicked games are yours to choose.", human)
            self.notify_games.append({"title": human, "url": URL_MEMBERSHIP,
                                      "status": "skipped:legacy-month"})
            return None
        return {"order": order, "human_name": human}

    async def _alert_unpaid(self, slug: str) -> None:
        """Say once, on the issues feed, that the newest month is posted but not paid for.

        Its membership page lists the games, but names no order of yours:
        you have not been billed for it - the subscription is paused, the
        month was skipped, or the billing failed. A marker row in the
        database is what makes it once per month, not once per run.
        """
        name = month_display_name(slug)
        if cfg.dryrun:
            logger.info("DRYRUN – '%s' is posted but not paid for; an alert would be sent.", name)
            return
        async with async_session() as session:
            _obj, created = await get_or_create(
                session, store="humble", user=self.user or "unknown",
                game_id=f"unpaid-alert/{slug}", title=f"{name} Humble Choice",
                url=URL_MEMBERSHIP, status="notified",
            )
            await session.commit()
        if not created:
            logger.debug("'%s' is posted but not paid for; already alerted about it.", name)
            return
        logger.warning("'%s' is posted but not paid for: no order exists for it.", name)
        await self.notify(
            f"⚠️ **Humble Choice: {name}** is posted but not paid for - its membership "
            "page lists the games, but no order exists for it yet. If you did not skip "
            "this month on purpose, the subscription may be paused or its billing failed.")

    async def _current_month(self) -> tuple[dict, list | None] | None:
        """The month this run works on: (its order, opened once; its lineup).

        Page-first: the current month's membership page, then one month
        back at a time, until a page names an order. Only when no recent
        page names one does the run fall back to the order list.
        """
        today = datetime.now().date()
        for index, slug in enumerate(month_slugs(today)):
            probe = await self._probe_month(slug)
            if not probe["fetched"]:
                logger.debug("The %s membership page did not answer; stepping back.", slug)
                continue
            if probe["lineup"] is None:
                logger.warning("The %s membership page answered but its lineup could not "
                               "be read, so no month can be settled this run.", slug)
                return None
            if not probe["gamekey"]:
                if index == 0:
                    # The newest month there is, and it names no order of
                    # yours: posted, but not paid for.
                    await self._alert_unpaid(slug)
                logger.debug("The %s month names no order of yours; stepping back.", slug)
                continue
            order = await self._open_order(probe["gamekey"])
            if order is None:
                logger.warning("The order for the %s month could not be opened, so "
                               "nothing is claimed this run.", slug)
                return None
            settled = await self._settle_month(order, slug)
            if settled is None:
                return None
            logger.info("Working on '%s', found from its membership page.",
                        settled["human_name"])
            return settled["order"], probe["lineup"]
        logger.warning("No recent membership page named an order of yours, so the "
                       "month is looked for in the order list instead.")
        return await self._fallback_month()

    async def _fallback_month(self) -> tuple[dict, list | None] | None:
        """The month from the order list, when no page names one.

        The list is only gamekeys, so orders are opened from its head —
        newest first, as Humble serves it — until the first current-plan
        Choice month turns up, and no further. A long fruitless walk is
        cut off rather than repeated: if the newest month is not near
        the head, something else is wrong and the run says so.
        """
        status, payload = await self._fetch(API_ORDERS)
        if status != 200 or not isinstance(payload, list):
            logger.warning("Humble's order list answered %s, so no month can be read "
                           "this run.", status)
            return None
        opened = 0
        for entry in payload:
            gamekey = str(entry.get("gamekey") or "") if isinstance(entry, dict) else ""
            if not gamekey:
                continue
            if opened >= 100:
                logger.warning("A hundred orders in, no current Choice month has "
                               "turned up; the month hunt stops here this run.")
                return None
            order = await self._open_order(gamekey)
            opened += 1
            if order is None or not is_choice_order(order):
                continue
            created = order_created(order)
            if created is not None and created < LEGACY_CUTOFF:
                continue
            slug = membership_slug(order)
            settled = await self._settle_month(order, slug or "")
            if settled is None:
                return None
            logger.warning("Working on '%s', picked from the order list because no "
                           "membership page named it.", settled["human_name"])
            return settled["order"], await self._month_lineup(order)
        logger.info("The order list holds no current Choice month either.")
        return None

    async def _remember(self, tpk: dict, db_status: str, key: str | None = None,
                        platform: str = "", overwrite: bool = True) -> None:
        """Save one key's standing in the database, under the account that claimed it."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="humble", user=self.user or "unknown",
                game_id=f"{tpk['gamekey']}/{tpk['machine_name']}/{tpk['key_index']}",
                title=tpk["title"], url=URL_KEYS, status=db_status,
            )
            if created or overwrite:
                obj.status = db_status
            if key:
                obj.code = key
                extra = {"external_store": platform or tpk["key_type"]}
                # The month's page names a Steam game's app outright; the
                # row remembers it, so the Steam module's library check can
                # decide by the app id instead of searching by title.
                try:
                    app_id = int(tpk.get("steam_app_id") or 0)
                except (TypeError, ValueError):
                    app_id = 0
                if app_id:
                    extra["steam_app_id"] = app_id
                obj.extra = json.dumps(extra)
            await session.commit()

    async def _choose_game(self, order: dict, tpk: dict) -> bool:
        """Choose one game, by the identifier the page itself uses.

        The identifier is the short form (choose_identifiers), the shape
        the site's own claim button sends - and it goes alone: the one
        captured wire trace of a working claim chooses a single game, and
        a batch call answered 200 while creating a key entry for only the
        game that already had one. A refusal is logged with Humble's own
        answer; the caller decides from the order, not from this, whether
        the game can still be revealed.
        """
        identifiers = choose_identifiers([tpk])
        if not identifiers:
            return True
        form = [["gamekey", str(order.get("gamekey") or "")], ["parent_identifier", "initial"]]
        form += [["chosen_identifiers[]", identifier] for identifier in identifiers]
        status, payload = await self._fetch(API_CHOOSE, form)
        chosen = choose_outcome(status, payload)
        if chosen:
            logger.debug("Choosing '%s' answered %s (chosen).", tpk["title"], status)
        elif choose_already(payload):
            logger.debug("Choosing '%s' answered %s (already chosen).", tpk["title"], status)
        else:
            logger.warning("Choosing '%s' was not confirmed (HTTP %s): %s",
                           tpk["title"], status, payload_snippet(payload))
        return chosen

    async def _month_lineup(self, order: dict) -> list | None:
        """The picked month's full lineup, from its membership page.

        The order alone cannot see a game that was never selected on the
        site — it has no key entry there — so the month's games are read
        from the page that offers them. None when the page or its data
        cannot be read, with the gap said out loud: a run that cannot see
        the lineup must not sound complete.
        """
        slug = membership_slug(order)
        if not slug:
            logger.warning("The month's membership page is not known, so games you have not "
                           "selected on the site yet, if any, are invisible this run.")
            return None
        status, page = await self._fetch_text(f"/membership/{slug}")
        product = (order.get("product") or {}).get("machine_name") or ""
        lineup = month_lineup(page, str(product)) if status == 200 else None
        if lineup is None:
            logger.warning("The month's lineup could not be read from its membership page, "
                           "so games you have not selected on the site yet, if any, are "
                           "invisible this run.")
        return lineup

    async def _materialize_one(self, order: dict, tpk: dict) -> dict | None:
        """The real key entry for a game that had none, read back after choosing.

        Choosing is what makes Humble create a game's key entry, so the
        order is read once more and the stand-in matched to the real entry
        by name. None when Humble still hands over no waiting entry: the
        game is named and left for the next run.
        """
        fresh = await self._open_order(str(order.get("gamekey") or ""))
        if fresh is None:
            logger.warning("The order could not be re-read after choosing, so '%s' waits "
                           "for the next run.", tpk["title"])
            return None
        for real in extract_tpks(fresh):
            if real["machine_name"] == tpk["machine_name"] and tpk_state(real) == "waiting":
                return real
        logger.warning("'%s' was chosen, but Humble handed over no key entry for it; "
                       "it waits for the next run.", tpk["title"])
        return None

    async def _key_from_order(self, tpk: dict) -> str | None:
        """The key for one entry as the order itself shows it now, or None.

        The reveal answer is not the only place a key shows up: the order
        carries it (redeemed_key_val) once the key stands revealed, however
        that happened - an answer that lost the key in transit, or a reveal
        done on the site between reading the order and asking.
        """
        fresh = await self._open_order(str(tpk.get("gamekey") or ""))
        if fresh is None:
            return None
        for real in extract_tpks(fresh):
            if real["machine_name"] == tpk["machine_name"] and real.get("revealed_key"):
                return str(real["revealed_key"])
        return None

    async def _reveal(self, tpk: dict) -> str:
        """Reveal one key and save it. Returns the run's standing: claimed, blocked or failed."""
        form = [["keytype", tpk["machine_name"]], ["key", tpk["gamekey"]],
                ["keyindex", str(tpk["key_index"])]]
        status, payload = await self._fetch(API_REDEEM, form)
        outcome = reveal_outcome(status, payload if isinstance(payload, dict) else {})
        if outcome == "blocked":
            logger.warning("Humble turned the reveal of '%s' away (HTTP %s): the session or its "
                           "anti-bot check, so the rest of this run waits.", tpk["title"], status)
            return "blocked"
        if outcome == "already":
            # Revealed between reading the order and asking, by you or another run.
            logger.info("'%s' was already revealed.", tpk["title"])
            await self._remember(tpk, "existed", overwrite=False)
            self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "existed"})
            return "claimed"

        if outcome == "exhausted":
            # Humble's key pool for this game is empty right now. Nothing
            # is broken and nothing needs doing - the key is simply not
            # there yet - so the game stays unrevealed and planned again
            # by the next run, and the report names the state instead of
            # reading as a reveal that failed.
            logger.warning("Humble is temporarily out of keys for '%s', so there is no key "
                           "to reveal yet. Nothing needs doing; it will be tried again on "
                           "the next run.", tpk["title"])
            await self._remember(tpk, "failed:keys-exhausted")
            self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                      "status": "failed:keys-exhausted"})
            return "failed"

        if outcome == "pending":
            # A Battle.net grant that is still processing on Blizzard's
            # side: the claim went through, the grant just has not landed
            # yet. Nothing is broken and nothing needs doing - the game
            # stays unrevealed and planned again by the next run, and the
            # report names the state instead of reading as a reveal that
            # failed.
            logger.warning("The Battle.net grant for '%s' is still processing, so there is "
                           "nothing to collect yet. Nothing needs doing; it will be tried "
                           "again on the next run.", tpk["title"])
            await self._remember(tpk, "failed:grant-pending")
            self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                      "status": "failed:grant-pending"})
            return "failed"

        key = str((payload or {}).get("key") or "") if outcome == "revealed" else ""
        if not key and "keyless" not in (tpk["machine_name"] or ""):
            # The answer carried no key. Before calling that a failure, ask
            # the order: Humble has a known state where the reveal answer
            # comes back without the key while the key itself stands
            # revealed in the order - and a key revealed on the site in the
            # meantime shows up the same way.
            found = await self._key_from_order(tpk)
            if found:
                logger.info("✓ Revealed '%s' - the key was read back from the order.",
                            tpk["title"])
                await self._remember(tpk, "claimed", key=found,
                                     platform=tpk["key_type"] or "humble")
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                          "status": "claimed"})
                return "claimed"
        if outcome != "revealed":
            logger.warning("Humble did not reveal the key for '%s' (HTTP %s): %s",
                           tpk["title"], status, payload_snippet(payload))
            await self._remember(tpk, "failed")
            self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "failed:reveal-failed"})
            return "failed"

        platform = tpk["key_type"] or "humble"
        if key:
            logger.info("✓ Revealed '%s' (%s key %s).", tpk["title"], platform, mask_code(key))
        else:
            # A keyless grant (an Epic variant linked to your account) has no key to hand over.
            logger.info("✓ Claimed '%s' (%s, no key: it goes to the linked account).", tpk["title"], platform)
        await self._remember(tpk, "claimed", key=key or None, platform=platform)
        self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "claimed"})
        return "claimed"

    async def _claim_game(self, order: dict, tpk: dict) -> str:
        """One game end to end: choose it, read its real key entry back, reveal it.

        The sequence follows the site's own claim click, one game at a
        time. Choosing the whole month in one call answered 200 while a
        game without a key entry never got one - the batch call registered
        only what was already there - so each game now gets its own choose
        call, and a game that had no entry is matched to the real one the
        choice created before anything tries to reveal it.
        """
        await self._choose_game(order, tpk)
        if tpk.get("unchosen"):
            real = await self._materialize_one(order, tpk)
            if real is None:
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS,
                                          "status": "skipped:choose-failed"})
                return "failed"
            tpk = real
        return await self._reveal(tpk)

    async def _claim_month(self, order: dict, lineup: list | None = None) -> None:
        """Settle one Choice month: record what is settled, reveal what is waiting.

        The month's games are the order's key entries plus the lineup games
        that have no entry yet (unchosen_tpks): a game never selected on
        the site is as much this month's game as any other, and the plan
        treats it the same. Every waiting key is revealed, duplicates
        included: whether a revealed Steam key is a spare is the Steam
        module's call when it works the key queue, not this store's.
        """
        tpks = extract_tpks(order)
        if lineup:
            # The page's identifiers are exact; stamp them onto the key
            # entries they belong to so choosing never has to derive one.
            identifiers = lineup_identifiers(lineup)
            for tpk in tpks:
                if tpk["machine_name"] in identifiers:
                    tpk["identifier"] = identifiers[tpk["machine_name"]]
            tpks += unchosen_tpks(order, lineup)
        for tpk in tpks:
            state = tpk_state(tpk)
            if state == "revealed":
                # Yours already, and a row the bot made for it keeps its key and queue place.
                await self._remember(tpk, "existed", overwrite=False)
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "existed"})
            elif state == "expired":
                logger.info("The key for '%s' expired on %s, before it was ever revealed.",
                            tpk["title"], tpk["expiry_date"] or "its publisher's date")
                await self._remember(tpk, "skipped:expired")
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "skipped:expired"})

        planned = plan_reveals(tpks)

        if not planned:
            logger.debug("Nothing waiting to be revealed in this month.")
            return
        logger.info("🎮 %d Humble Choice game(s) to reveal.", len(planned))

        if cfg.dryrun:
            for tpk in planned:
                logger.info("DRYRUN – skipped revealing '%s'.", tpk["title"])
                self.notify_games.append({"title": tpk["title"], "url": URL_KEYS, "status": "available (dry run)"})
            return

        for index, tpk in enumerate(planned):
            if await self._claim_game(order, tpk) == "blocked":
                left = len(planned) - index - 1
                if left:
                    logger.warning("%d more key(s) wait for the next run.", left)
                break

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point for the Humble Bundle claiming flow."""
        logger.debug("Starting Humble Bundle claiming flow")
        try:
            await self.start_browser()
            if not await self._ensure_logged_in():
                logger.error("Aborting Humble Bundle claim flow due to login failure.")
                needs_you(self.store_name, 0, "game")
                return

            found = await self._current_month()
            if found is None:
                logger.info("No Humble Choice month is waiting to be claimed.")
                return
            order, lineup = found
            await self._claim_month(order, lineup)

        except Exception as exc:
            logger.exception("Fatal error")
            if cfg.notify_errors:
                await self.notify(f"humble failed: {exc}")
        finally:
            await self.close_browser()


async def claim_humble() -> dict:
    """Convenience entry point."""
    claimer = HumbleClaimer()
    await claimer.run()
    return {"store": "Humble Bundle", "user": claimer.user, "games": claimer.notify_games}
