"""Humble Bundle: claims the games of a Humble Choice subscription.

"Claiming" a Choice game means revealing its key. The month itself is never
unlocked by the bot: early unlock is a payment, and a subscriber's month is
created by the automatic billing on the last Tuesday, so by the time a run
looks, a paid month either exists in your library or it does not.

Humble rebuilt its site in November 2026 and the machinery the old version
of this module used went away with the old site: the JSON order API answers
404, the sign-in page it filled is gone, and the membership pages no longer
carry the month's data in their markup. What the new site does have is
pages a person reads, so this module works the way a person does, in the
browser:

  1. Sign in. The session in the browser profile is checked on the library
     page, which only a signed-in reader can see. Signing in itself now
     starts at auth.humblebundle.com: an e-mail-first form, then a password
     where the account has one, then a code — from HB_OTP_KEY when the
     account asks its authenticator, or the code Humble e-mails, which only
     you can read, so that step is yours over VNC, like every other store.
  2. Find the one month to work on. The membership home names the current
     month; your library's first page groups what you own, newest first.
     The run works the current month when it is in your library, else the
     newest month that is. When the current month is posted but not in
     your library — subscription paused, month skipped, or billing failed —
     the run says so on the issues feed, once per month, and works your
     newest paid month instead. Only one month is worked per run: older
     months are a back catalogue to settle by hand, not a sweep, and
     pick-limited months from before February 2022 are never the newest
     month any more, so they are left alone by the same rule.
  3. Read the month's order page in your library. It lists every key of the
     month, DLC packs included — more than the library's game cards show —
     each entry in one of three states: revealed, with the key itself as a
     button on the page; unrevealed, with a Reveal button and the date the
     key must be revealed by; or a keyless grant already redeemed into a
     linked account ("Redeemed on Blizzard (Battle.net)").
  4. Settle each entry. A key that stands revealed is saved in the
     database, tagged with the store it belongs to, so the store can
     make its duplicate check. That includes a key with no row at
     all: a reveal outside the bot can still be unclaimed, and no
     row means the key has never been processed. A row the bot made
     earlier without the key in it (the reveal was recorded, the key
     itself was not) does get the key saved, so the key takes its
     place in the queue like any other - unless the same key already
     stands recorded under a different game id (queued, activated,
     or posted as a duplicate from that row), in which case the key
     is recorded without being queued a second time. A key is its
     string: a different key for the same game is a new key, and
     takes its place in the queue the same way. An unrevealed key is
     revealed by pressing its Reveal button and reading the key the page
     shows afterwards; a keyless grant is recorded as claimed with no key.

A waiting key is revealed whether or not its game is already in your
library somewhere: duplicates are not this store's call. A revealed
Steam key waits in the database for the Steam module, which checks the
key queue against your Steam library when it works it - a key for a
game you already own is a spare there (posted to the duplicates
channel when one is configured, never activated on your account),
whichever store handed the key out.

Two answers are wait states, not errors. Humble can be out of keys for
a game ("Keys are temporarily exhausted for this product"): there is
nothing to fix and nothing to do, the key simply is not there yet, so
the game stays unrevealed and the run reports it as
failed:keys-exhausted. A linked-account grant can likewise still be
processing on the other store's side: the game stays unrevealed and
the run reports failed:grant-pending. Either way the next run, which
plans from the pages' live state, tries again on its own.

Revealing is one-way: a revealed key can no longer be gifted from Humble,
and a month with a revealed key in it is no longer refundable. That is why
this store is opt-in (name `humble` in STORES) and why a key that has
already been revealed is never touched again.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date

from src.core.claimer import BaseClaimer, account_key
from src.core.config import cfg
from src.core.database import async_session, get_or_create
from src.core.redact import mask_code
from src.core.run_state import needs_you
from src.core.url_security import url_has_allowed_host

logger = logging.getLogger("fgc.humble")

SITE_HOST = "www.humblebundle.com"
URL_LIBRARY = "https://www.humblebundle.com/home/library"
URL_MEMBERSHIP = "https://www.humblebundle.com/membership/home"

# ---------------------------------------------------------------------------
# Page reading: small scripts run in the page, shaped by what the new site
# serves. Fields are found by what they are (a button's label, an entry's
# classes), not by one build's ids, because the site is drawn by script and
# its ids change between builds.
# ---------------------------------------------------------------------------

# Signed in is a state of the library page: the new site sends a signed-out
# reader to auth.humblebundle.com, and a signed-in one gets their library
# and a Logout entry in the account menu.
SIGNED_IN_JS = """
    JSON.stringify({
        host: location.host,
        signedIn: location.host === 'www.humblebundle.com' && !!document.body && (
            document.body.innerText.includes('Your Library')
            || [...document.querySelectorAll('a, button')].some(el => el.textContent.trim() === 'Logout')),
    })
"""

# The auth site's steps share one shape of question - which boxes are on
# this screen - so the sign-in loop reads the screen instead of assuming
# an order: e-mail first, then a password or a code, depending on the
# account.
AUTH_FORM_JS = """
    JSON.stringify({
        href: location.href,
        email: !!document.querySelector('input[type="email"], input[name="email" i]'),
        password: !!document.querySelector('input[type="password"]'),
        code: !!document.querySelector('input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i]'),
        sendCode: [...document.querySelectorAll('button')].some(b => /send code/i.test(b.textContent || '')),
        text: document.body ? document.body.innerText.slice(0, 400) : '',
    })
"""

# Typing the way React notices: the native value setter, then the events.
_SET_VALUE_JS = """
        const set = (el, value) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        };
"""

FILL_EMAIL_JS = """
    (() => {
""" + _SET_VALUE_JS + """
        const email = document.querySelector('input[type="email"], input[name="email" i]');
        if (!email) return false;
        set(email, %s);
        const button = [...document.querySelectorAll('button')]
            .find(b => /continue/i.test(b.textContent || '')) || document.querySelector('button[type="submit"]');
        if (!button) return false;
        button.click();
        return true;
    })()
"""

FILL_PASSWORD_JS = """
    (() => {
""" + _SET_VALUE_JS + """
        const password = document.querySelector('input[type="password"]');
        if (!password) return false;
        set(password, %s);
        const box = document.querySelector('input[name="remember"]');
        if (box && !box.checked) box.click();
        const button = [...document.querySelectorAll('button')]
            .find(b => /continue|log\\s?in|sign\\s?in/i.test(b.textContent || ''))
            || document.querySelector('button[type="submit"], input[type="submit"]');
        if (!button) return false;
        button.click();
        return true;
    })()
"""

FILL_CODE_JS = """
    (() => {
""" + _SET_VALUE_JS + """
        const field = document.querySelector('input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i]');
        if (!field) return false;
        set(field, %s);
        const button = [...document.querySelectorAll('button')]
            .find(b => /continue|verify|submit/i.test(b.textContent || ''))
            || document.querySelector('button[type="submit"], input[type="submit"]');
        if (button) button.click();
        return true;
    })()
"""

CLICK_SEND_CODE_JS = """
    (() => {
        const button = [...document.querySelectorAll('button')]
            .find(b => /send code/i.test(b.textContent || ''));
        if (!button) return false;
        button.click();
        return true;
    })()
"""

# The library's game links, each with the bundle heading it sits under.
# Headings are tracked in document order - a heading names the games that
# follow it - because the cards' own markup does not say which bundle a
# card belongs to; a link's own text does name its bundle, and is carried
# along as the fallback grouping.
LIBRARY_LINKS_JS = """
    JSON.stringify((() => {
        const month = /^(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{4}/;
        const links = [];
        let heading = '';
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
        for (let node = walker.currentNode; node; node = walker.nextNode()) {
            if (/^H[1-6]$/.test(node.tagName) || node.getAttribute('role') === 'heading') {
                const text = (node.textContent || '').trim();
                if (month.test(text)) heading = text;
            }
            if (node.tagName === 'A' && (node.getAttribute('href') || '').includes('/home/library/content/')) {
                const lines = (node.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                links.push({heading, title: lines[0] || '', sub: lines.slice(1).join(' '),
                            href: node.getAttribute('href')});
            }
        }
        return links;
    })())
"""

CLICK_VIEW_DETAILS_JS = """
    (() => {
        const wanted = 'View details for ' + %s;
        const button = [...document.querySelectorAll('button')]
            .find(b => b.getAttribute('aria-label') === wanted);
        if (!button) return false;
        button.click();
        return true;
    })()
"""

# The details dialog's way to the month's order page. Like every reading
# _eval_json parses, the answer is JSON: the bare href is not, and handing
# it back unquoted made the parse fail and the order page unreachable.
REDEEM_LINK_JS = """
    JSON.stringify((() => {
        const link = [...document.querySelectorAll('a')]
            .find(a => (a.textContent || '').trim() === 'Go to Redeem'
                    && (a.getAttribute('href') || '').startsWith('/home/library/')
                    && !(a.getAttribute('href') || '').includes('/content/'));
        return link ? link.getAttribute('href') : null;
    })())
"""

# One key entry on an order page is a row with these classes; its title is
# the accent line inside it, its platform icon names itself, and everything
# the entry says is in its text.
_ENTRY_CLASSES = ("flex", "flex-wrap", "items-center", "justify-between", "gap-4", "px-4", "py-3")
_TITLE_CLASSES = ("text-base", "leading-normal", "font-normal", "text-accent", "truncate")

ORDER_ENTRIES_JS = """
    JSON.stringify((() => {
        const entryClasses = %s, titleClasses = %s;
        const rows = [...document.querySelectorAll('div')]
            .filter(d => entryClasses.every(c => d.classList.contains(c)));
        return rows.map(row => {
            const title = [...row.querySelectorAll('div')]
                .find(d => titleClasses.every(c => d.classList.contains(c)));
            const icon = row.querySelector('[role="img"]');
            return {
                title: title ? title.textContent.trim() : '',
                buttons: [...row.querySelectorAll('button')].map(b => (b.innerText || '').trim()),
                text: row.innerText || '',
                platform_icon: icon ? (icon.getAttribute('aria-label') || '') : '',
            };
        });
    })())
""" % (json.dumps(list(_ENTRY_CLASSES)), json.dumps(list(_TITLE_CLASSES)))

# A game's own page carries the same state in the page at large: its title
# is the page title, and its buttons and text are read whole - the claim
# logic tells a key from the navigation around it by the key's shape.
CONTENT_ENTRY_JS = """
    JSON.stringify((() => {
        const icon = document.querySelector('[role="img"]');
        return {
            title: document.title || '',
            buttons: [...document.querySelectorAll('button')].map(b => (b.innerText || '').trim()).filter(Boolean),
            text: document.body ? document.body.innerText.slice(0, 2000) : '',
            platform_icon: icon ? (icon.getAttribute('aria-label') || '') : '',
        };
    })())
"""

# Pressing Reveal is the claim: the page asks Humble itself (a server
# action on the same address) and redraws the entry with its key, so the
# bot presses the button a person would and reads the page again.
CLICK_REVEAL_JS = """
    (() => {
        const title = %s;
        const entryClasses = %s, titleClasses = %s;
        let scope = document;
        if (title) {
            const named = [...document.querySelectorAll('div')]
                .find(d => titleClasses.every(c => d.classList.contains(c)) && d.textContent.trim() === title);
            if (!named) return false;
            scope = named;
            while (scope && !(scope.classList && entryClasses.every(c => scope.classList.contains(c)))) {
                scope = scope.parentElement;
            }
            if (!scope) return false;
        }
        const button = [...scope.querySelectorAll('button')]
            .find(b => (b.innerText || '').trim() === 'Reveal');
        if (!button) return false;
        button.click();
        return true;
    })()
""" % ("%s", json.dumps(list(_ENTRY_CLASSES)), json.dumps(list(_TITLE_CLASSES)))


# ---------------------------------------------------------------------------
# Pure helpers: the readings a run decides on, testable without a browser.
# ---------------------------------------------------------------------------

_MONTH_NAMES = [calendar.month_name[m] for m in range(1, 13)]
_MONTH_RE = re.compile(
    r"\b(" + "|".join(_MONTH_NAMES) + r")\s+(\d{4})\b", re.IGNORECASE)


def month_display_name(slug: str) -> str:
    """'november-2026' as 'November 2026': a month named the way its owner reads it."""
    name, _, year = str(slug or "").partition("-")
    return f"{name.capitalize()} {year}".strip()


def month_slug_from_text(text: str) -> str | None:
    """The first '<Month> <Year>' a page names, as a slug, or None.

    Pages name months the way people read them ("November 2026 Humble
    Choice"); the slug is the same month in the shape the rest of the run
    compares and files things under.
    """
    match = _MONTH_RE.search(str(text or ""))
    if not match:
        return None
    return f"{match.group(1).lower()}-{match.group(2)}"


def parse_month_heading(text: str) -> str | None:
    """The Choice month a heading names, as a slug, or None when it names none.

    A heading counts only when it names a Choice month: the library groups
    other bundles too, and their headings are not months this run works.
    """
    if "choice" not in str(text or "").lower():
        return None
    return month_slug_from_text(text)


def title_slug(title: str) -> str:
    """A game's title as a stable id piece: 'Mafia: The Old Country' -> 'mafia-the-old-country'."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return slug or "game"


def choice_groups(links: list) -> list[dict]:
    """Bundle the library's game links into their Choice months, newest first.

    Each link the page hands over carries the heading it sits under and its
    own bundle line ("<Month> <Year> Humble Choice"); the heading wins when
    it names a Choice month, the link's own line is the fallback. Groups
    keep the page's order, which is newest first, and a game linked twice
    (a card and a dialog link, say) is kept once.
    """
    groups: list[dict] = []
    by_slug: dict[str, dict] = {}
    for link in links or []:
        if not isinstance(link, dict):
            continue
        slug = parse_month_heading(str(link.get("heading") or "")) \
            or parse_month_heading(str(link.get("sub") or ""))
        title = str(link.get("title") or "").strip()
        href = str(link.get("href") or "").strip()
        if not slug or not title or not href:
            continue
        group = by_slug.get(slug)
        if group is None:
            group = {"slug": slug, "heading": f"{month_display_name(slug)} Humble Choice",
                     "games": []}
            by_slug[slug] = group
            groups.append(group)
        if all(game["href"] != href for game in group["games"]):
            group["games"].append({"title": title, "href": href})
    return groups


def select_month(current_slug: str | None, groups: list) -> tuple[dict | None, bool]:
    """Pick the month a run works: (the group, whether the current month is unpaid).

    The current month wins when it is in the library; when a read that did
    find Choice months does not hold it, it is posted but not paid for, and
    the newest month the library does hold is worked instead. With no
    current month read, the newest group is the pick and nothing is
    unpaid. No groups at all is no evidence either way - an empty read
    proves nothing - so it picks nothing and calls nothing unpaid.
    """
    if not groups:
        return None, False
    if current_slug:
        for group in groups:
            if group["slug"] == current_slug:
                return group, False
        return groups[0], True
    return groups[0], False


# The words a button can carry that are not keys: the platform names an
# entry's second button shows, and the actions the page offers.
_PLATFORM_NAMES = {
    "steam": "steam", "gog": "gog", "gog.com": "gog", "epic": "epic",
    "epic games": "epic", "battle.net": "blizzard", "blizzard": "blizzard",
    "blizzard (battle.net)": "blizzard", "battle.net (blizzard)": "blizzard",
    "rockstar": "rockstar", "ubisoft": "ubisoft", "ea": "ea", "origin": "ea",
    "humble": "humble", "itch.io": "itchio", "xbox": "microsoft",
    "microsoft": "microsoft", "nintendo": "nintendo", "playstation": "playstation",
}
_UI_WORDS = frozenset({
    "reveal", "redeem", "claim", "link", "copy", "download", "activate",
    "view details", "go to redeem", "logout", "search", "open menu",
})
_CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]*")


def _named_platform(raw: str) -> str:
    """The tag for a text that names a platform, or "" when it names none.

    Only a known name counts: a button's text is a platform when it is one
    of the names in _PLATFORM_NAMES, or plainly contains one of the two
    names platforms wear several ways (Steam, Battle.net / Blizzard).
    """
    name = str(raw or "").strip().lower()
    if name in _PLATFORM_NAMES:
        return _PLATFORM_NAMES[name]
    if "battle" in name or "blizzard" in name:
        return "blizzard"
    if "steam" in name:
        return "steam"
    return ""


def normalize_platform(raw: str) -> str:
    """A platform as the database tags it: 'Steam' -> 'steam', 'Blizzard (Battle.net)' -> 'blizzard'.

    The Steam module works the key queue by the exact tag "steam", so the
    names the page shows have to land on the tags the queue knows. A name
    with no known tag is kept, cleaned to one lowercase word, rather than
    dropped: the tag is still the truth about where the key belongs.
    """
    named = _named_platform(raw)
    if named:
        return named
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").strip().lower())


def looks_like_code(text: str) -> bool:
    """True when a button's text is a key, not a label.

    Keys differ per store - a Steam key is three groups of five, an IGN
    code is HUMBLE_XXXX - so the shape asked for is the one they share: at
    least eight characters of capitals, digits, dashes and underscores,
    with at least one digit or separator in it. A plain word, however
    long, is a label; a platform name is a label too.
    """
    word = str(text or "").strip()
    if len(word) < 8 or len(word) > 64:
        return False
    if word.lower() in _UI_WORDS or word.lower() in _PLATFORM_NAMES:
        return False
    if not _CODE_RE.fullmatch(word):
        return False
    return any(char.isdigit() for char in word) or "-" in word or "_" in word


_EXPIRY_RE = re.compile(r"reveal your key before\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)
_REDEEMED_RE = re.compile(r"Redeemed on\s+(.+?)(?:\.|\n|$)")


def classify_entry(raw: dict, today: date | None = None) -> dict:
    """One order-page entry, read into the standing a run acts on.

    The entry arrives as the page shows it - its title, its buttons'
    texts, its whole text, its platform icon's name - and leaves as
    {title, state, code, platform, expiry}: state is revealed (a button
    carries the key), unrevealed (a Reveal button waits), redeemed (a
    keyless grant, already in the linked account), expired, or unknown,
    which a run reports and never touches.
    """
    raw = raw or {}
    title = str(raw.get("title") or "").strip() or "(untitled)"
    buttons = [str(b or "").strip() for b in raw.get("buttons") or []]
    text = str(raw.get("text") or "")
    lowered = text.lower()

    expiry = None
    match = _EXPIRY_RE.search(text)
    if match:
        try:
            expiry = date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            expiry = None

    entry = {"title": title, "state": "unknown", "code": None, "platform": "",
             "expiry": expiry, "text": text}

    code = next((b for b in buttons if looks_like_code(b)), None)
    if code:
        entry["state"] = "revealed"
        entry["code"] = code
        named = next((b for b in buttons if b != code and _named_platform(b)), "")
        entry["platform"] = _named_platform(named) \
            or _named_platform(str(raw.get("platform_icon") or ""))
        return entry

    granted = _REDEEMED_RE.search(text)
    if granted:
        entry["state"] = "redeemed"
        entry["platform"] = normalize_platform(granted.group(1))
        return entry

    if "expired" in lowered:
        entry["state"] = "expired"
        return entry

    if any(b.lower() == "reveal" for b in buttons):
        if expiry is not None and expiry < (today or date.today()):
            # The date the key had to be revealed by has passed: it is an
            # expired key wearing its Reveal button still.
            entry["state"] = "expired"
            return entry
        entry["state"] = "unrevealed"
        named = next((b for b in buttons if b.lower() != "reveal" and _named_platform(b)), "")
        entry["platform"] = _named_platform(named) \
            or _named_platform(str(raw.get("platform_icon") or ""))
        return entry

    return entry


def entry_snippet(text: str) -> str:
    """What an entry said, safe to log: anything shaped like a key is masked."""
    words = str(text or "").split()
    shown = [mask_code(w) if looks_like_code(w) else w for w in words]
    return " ".join(shown)[:240]


class HumbleClaimer(BaseClaimer):
    store_name = "humble"

    # ------------------------------------------------------------------
    # Page reading
    # ------------------------------------------------------------------

    async def _eval_json(self, js: str, fallback=None):
        """Run a script in the page and read its JSON answer; fallback when it cannot be read."""
        try:
            raw = await self.page.evaluate(js, await_promise=True)
            answer = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.debug("Could not read the page: %s", e)
            return fallback
        return answer if answer is not None else fallback

    async def _page_text(self, limit: int = 4000) -> str:
        """The page's visible text, up to a limit: what a reader of the page would see."""
        text = await self._eval_json(
            f"JSON.stringify(document.body ? document.body.innerText.slice(0, {int(limit)}) : '')", "")
        return text if isinstance(text, str) else ""

    async def _signed_in(self) -> bool:
        """True when the page the browser sits on is a signed-in reader's library."""
        answer = await self._eval_json(SIGNED_IN_JS, {})
        return bool(isinstance(answer, dict) and answer.get("signedIn"))

    async def _signed_in_after_nav(self) -> bool:
        """The check the VNC wait polls: the library's state, read without disturbing you.

        While you are on the auth site - typing the code Humble e-mailed
        you - the check only looks at the address and backs off:
        navigating away would wipe what you are typing. The auth flow
        hands back to the main site on its own when it finishes; only
        then, or when the browser sits somewhere neutral, does the check
        steer to the library and ask there.
        """
        current = await self._eval_json("JSON.stringify(location.href)", "")
        if url_has_allowed_host(str(current), "auth.humblebundle.com"):
            return False
        if url_has_allowed_host(str(current), SITE_HOST):
            return await self._signed_in()
        await self.page.get(URL_LIBRARY)
        await self.sleep(4)
        return await self._signed_in()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _auth_form(self) -> dict:
        """Which boxes the auth site's current step shows: e-mail, password, code."""
        answer = await self._eval_json(AUTH_FORM_JS, {})
        return answer if isinstance(answer, dict) else {}

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

    async def _auth_steps(self) -> bool:
        """Walk the auth site's steps after the e-mail: a password, or a code.

        The steps are read, not assumed: an account with a password is
        asked for it, an account without one is sent a code by e-mail.
        That code only you can read, so when Humble offers to send it the
        walk stops and hands over to the VNC wait; an authenticator code
        from HB_OTP_KEY is tried once where the box looks like one.
        """
        tried_password = False
        tried_code = False
        idle = 0
        for _ in range(20):
            if await self._human_challenge_present():
                return False
            form = await self._auth_form()
            host = str(form.get("href") or "")
            if SITE_HOST in host:
                # The flow handed back to the main site: signed in, or about to say so.
                return await self._signed_in()
            if form.get("password") and cfg.hb_password and not tried_password:
                tried_password = True
                idle = 0
                try:
                    sent = await self.page.evaluate(FILL_PASSWORD_JS % json.dumps(cfg.hb_password))
                except Exception as e:
                    logger.debug("Could not fill the password step: %s", e)
                    sent = False
                if not sent:
                    return False
                await self.sleep(5)
                continue
            if form.get("code"):
                if form.get("sendCode"):
                    # The code goes to your e-mail: yours to type, over VNC.
                    try:
                        await self.page.evaluate(CLICK_SEND_CODE_JS)
                    except Exception as e:
                        logger.debug("Could not ask Humble to send its code: %s", e)
                    return False
                if cfg.hb_otp_key and not tried_code:
                    tried_code = True
                    idle = 0
                    if await self._fill_code():
                        continue
                return False
            idle += 1
            if idle >= 3:
                # A screen with nothing the bot can answer (a Google-only
                # step, an error): the VNC wait is the way through.
                return False
            await self.sleep(3)
        return await self._signed_in()

    async def _ensure_logged_in(self) -> bool:
        """Signed in to Humble: by itself with HB_EMAIL / HB_PASSWORD, else you over VNC."""
        await self.page.get(URL_LIBRARY)
        await self.sleep(5)
        if await self._human_challenge_present():
            if not await self._wait_out_challenge("Humble Bundle", store_key=self.store_name):
                return False
        if await self._signed_in():
            self.log_signed_in(account_key(cfg.hb_email, None))
            return True

        if cfg.hb_email:
            logger.info("Signing in to Humble Bundle as %s…", account_key(cfg.hb_email, None))
            # The library sends a signed-out reader to the auth site, which
            # is where the browser should already be sitting.
            try:
                sent = await self.page.evaluate(FILL_EMAIL_JS % json.dumps(cfg.hb_email))
            except Exception as e:
                logger.debug("Could not fill the sign-in e-mail: %s", e)
                sent = False
            if sent:
                await self.sleep(4)
                if await self._auth_steps():
                    self.log_signed_in(account_key(cfg.hb_email, None))
                    return True
            else:
                logger.debug("The sign-in form was not on %s", await self._page_brief())

        msg = self._vnc_notice(
            "Humble Bundle: login needs you",
            "The automated sign-in did not finish. Open the browser and sign in to Humble Bundle; "
            "a code Humble e-mails you is typed there too.",
        )
        if await self._wait_for_vnc_login(self._signed_in_after_nav, custom_msg=msg,
                                          store_key=self.store_name):
            self.log_signed_in(account_key(cfg.hb_email, None))
            return True
        return False

    # ------------------------------------------------------------------
    # Choice months
    # ------------------------------------------------------------------

    async def _alert_unpaid(self, slug: str) -> None:
        """Say once, on the issues feed, that the newest month is posted but not paid for.

        Its membership page lists the games, but your library holds no
        order for it: you have not been billed - the subscription is
        paused, the month was skipped, or the billing failed. A marker
        row in the database is what makes it once per month, not once
        per run.
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
        logger.warning("'%s' is posted but not paid for: your library holds no order for it.", name)
        await self.notify(
            f"⚠️ **Humble Choice: {name}** is posted but not paid for - its membership "
            "page lists the games, but your library holds no order for it yet. If you did "
            "not skip this month on purpose, the subscription may be paused or its billing failed.")

    async def _pick_month(self) -> dict | None:
        """The month this run works on, as {slug, heading, games}, or None.

        The membership home names the current month; the library's first
        page names the months you own. The current month is the pick when
        it is owned; a current month that a real library read does not
        hold is alerted as unpaid, and the newest owned month is the pick
        instead. A library read that finds no months at all proves
        nothing - the page may not have settled yet - so it is read once
        more, and if it still finds nothing the run works no month and
        makes no unpaid call.
        """
        await self.page.get(URL_MEMBERSHIP)
        await self.sleep(5)
        current_slug = parse_month_heading(await self._page_text())
        if current_slug is None:
            logger.debug("The membership page named no current Choice month.")
        groups = await self._library_groups()
        if not groups:
            logger.debug("The library read found no Choice months; reading the library once more.")
            groups = await self._library_groups()
        if not groups:
            logger.warning("The library page named no Choice months on either read, "
                           "so no month is worked and no unpaid call is made this run.")
            return None
        group, unpaid = select_month(current_slug, groups)
        if unpaid and current_slug:
            await self._alert_unpaid(current_slug)
        if group is None:
            return None
        logger.info("Working on '%s', the newest Choice month in your library.",
                    month_display_name(group["slug"]))
        return group

    async def _library_groups(self) -> list:
        """The Choice months the library's first page names, newest first.

        What the read found is logged - how many links it handed back,
        and the months they grouped into - so a later empty or partial
        read can be told apart from a library that genuinely holds less.
        """
        await self.page.get(URL_LIBRARY)
        await self.sleep(5)
        links = await self._eval_json(LIBRARY_LINKS_JS, [])
        links = links if isinstance(links, list) else []
        groups = choice_groups(links)
        logger.debug("The library read found %d link(s), in Choice month(s): %s.",
                     len(links), [group["slug"] for group in groups] or "none")
        return groups

    async def _order_url(self, group: dict) -> str | None:
        """The month's order page, by way of its first game's details dialog.

        The dialog's "Go to Redeem" link is the only way the site names
        the order page; None when the dialog does not offer it, and the
        caller falls back to the games' own pages.
        """
        if not group.get("games"):
            return None
        await self.page.get(URL_LIBRARY)
        await self.sleep(5)
        first = group["games"][0]
        try:
            clicked = await self.page.evaluate(CLICK_VIEW_DETAILS_JS % json.dumps(first["title"]))
        except Exception as e:
            logger.debug("Could not open the details for '%s': %s", first["title"], e)
            clicked = False
        if not clicked:
            logger.debug("No details button for '%s' on the library page.", first["title"])
            return None
        await self.sleep(3)
        href = await self._eval_json(REDEEM_LINK_JS, None)
        if not href:
            logger.debug("The details dialog named no order page.")
            return None
        return f"https://{SITE_HOST}{href}"

    # ------------------------------------------------------------------
    # Saving standings
    # ------------------------------------------------------------------

    async def _row_for(self, game_id: str) -> tuple[str, str | None] | None:
        """The standing already saved for one game: (status, code), or None when there is no row."""
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame).where(
                    ClaimedGame.store == "humble",
                    ClaimedGame.user == (self.user or "unknown"),
                    ClaimedGame.game_id == game_id,
                ))
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return row.status, row.code

    async def _key_already_recorded(self, code: str, game_id: str) -> bool:
        """Whether this very key already stands in a sibling row.

        A key is its string, not its title. A game can stand in the
        database under more than one game id: the id scheme changed with
        the rebuilt site, so a month first read by older code keeps its
        old rows beside the new ones. When the identical string is
        already recorded under another id, in any standing, this entry
        adds nothing - the key was already queued, activated or posted
        from that row, and saving it again under this game id would
        queue it a second time and post it as a duplicate a second
        time. A sibling holding a different string is a different key
        for the same game, and settles nothing here.
        """
        from sqlalchemy import select

        from src.core.database import ClaimedGame

        async with async_session() as session:
            result = await session.execute(
                select(ClaimedGame.id).where(
                    ClaimedGame.store == "humble",
                    ClaimedGame.user == (self.user or "unknown"),
                    ClaimedGame.game_id != game_id,
                    ClaimedGame.code == code,
                ))
            return result.first() is not None

    async def _remember(self, game_id: str, title: str, url: str, db_status: str,
                        key: str | None = None, platform: str = "",
                        overwrite: bool = True) -> None:
        """Save one key's standing in the database, under the account that claimed it."""
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store="humble", user=self.user or "unknown",
                game_id=game_id, title=title, url=url, status=db_status,
            )
            if created or overwrite:
                obj.status = db_status
            if key:
                obj.code = key
                obj.extra = json.dumps({"external_store": platform or "humble"})
            await session.commit()

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    async def _order_entries(self, order_url: str) -> list[dict]:
        """The month's entries from its order page, classified, each with its game id."""
        order_id = order_url.rstrip("/").rsplit("/", 1)[-1]
        await self.page.get(order_url)
        await self.sleep(5)
        raws = await self._eval_json(ORDER_ENTRIES_JS, [])
        entries = []
        for raw in raws if isinstance(raws, list) else []:
            entry = classify_entry(raw if isinstance(raw, dict) else {})
            entry["game_id"] = f"{order_id}/{title_slug(entry['title'])}"
            entry["page_url"] = order_url
            entries.append(entry)
        return entries

    async def _content_entry(self, game: dict) -> dict:
        """One game's standing from its own page, for when the order page cannot be read."""
        url = f"https://{SITE_HOST}{game['href']}"
        await self.page.get(url)
        await self.sleep(4)
        raw = await self._eval_json(CONTENT_ENTRY_JS, {})
        entry = classify_entry(raw if isinstance(raw, dict) else {})
        if entry["title"] == "(untitled)" or not raw.get("title"):
            entry["title"] = game["title"]
        content_id = game["href"].rstrip("/").rsplit("/", 1)[-1]
        entry["game_id"] = f"{content_id}/{title_slug(entry['title'])}"
        entry["page_url"] = url
        return entry

    async def _claim_entry(self, entry: dict, order_url: str) -> str:
        """Settle one entry. Returns the run's standing: settled, failed or blocked."""
        title = entry["title"]
        state = entry["state"]
        row = await self._row_for(entry["game_id"])

        if state == "revealed":
            code = entry["code"]
            platform = entry["platform"] or "humble"
            if row is not None and row[1]:
                # The bot saved this key before: it keeps its row and its queue place.
                await self._remember(entry["game_id"], title, order_url, "existed",
                                     overwrite=False)
                self.notify_games.append({"title": title, "url": order_url, "status": "existed"})
            elif row is not None and await self._key_already_recorded(code, entry["game_id"]):
                # The identical key already stands recorded under a
                # different game id - queued, activated, or posted as a
                # duplicate from that row. This codeless row therefore
                # adds nothing: saving the same string here would queue
                # it a second time. A different string would be a
                # different key and would fall through to the save below.
                logger.info("The key for '%s' already stands under another entry, so it is "
                            "recorded without being queued again.", title)
                await self._remember(entry["game_id"], title, order_url, "existed")
                self.notify_games.append({"title": title, "url": order_url, "status": "existed"})
            elif row is not None:
                # The Padrino shape: the reveal was recorded but the key
                # itself never was, so the key could never queue. Save it
                # now, claimed, and it queues like a key the bot revealed.
                logger.info("✓ '%s' stood revealed with no key saved; its key (%s) is saved now.",
                            title, mask_code(code))
                await self._remember(entry["game_id"], title, order_url, "claimed",
                                     key=code, platform=platform)
                self.notify_games.append({"title": title, "url": order_url, "status": "claimed"})
            else:
                # No row at all: the key has never been processed. A
                # reveal outside the bot can still be unclaimed, so save
                # it like a key the bot revealed itself and let its
                # store's duplicate check decide whether it is a spare
                # or an activation.
                logger.info("✓ '%s' stood revealed with no record of it; its key (%s) is "
                            "saved now and handed to its store for the duplicate check.",
                            title, mask_code(code))
                await self._remember(entry["game_id"], title, order_url, "claimed",
                                     key=code, platform=platform)
                self.notify_games.append({"title": title, "url": order_url, "status": "claimed"})
            return "settled"

        if state == "redeemed":
            # A keyless grant, already in the linked account: claimed, with no key.
            if row is not None and row[0] == "claimed" and not row[1]:
                self.notify_games.append({"title": title, "url": order_url, "status": "existed"})
                return "settled"
            logger.info("✓ Claimed '%s' (%s, no key: it goes to the linked account).",
                        title, entry["platform"] or "humble")
            await self._remember(entry["game_id"], title, order_url, "claimed",
                                 platform=entry["platform"] or "humble")
            self.notify_games.append({"title": title, "url": order_url, "status": "claimed"})
            return "settled"

        if state == "expired":
            when = entry["expiry"].isoformat() if entry.get("expiry") else "its publisher's date"
            logger.info("The key for '%s' expired on %s, before it was ever revealed.", title, when)
            await self._remember(entry["game_id"], title, order_url, "skipped:expired")
            self.notify_games.append({"title": title, "url": order_url, "status": "skipped:expired"})
            return "settled"

        if state == "unknown":
            logger.warning("The entry for '%s' could not be read, so it is left alone: %s",
                           title, entry_snippet(entry.get("text", "")))
            return "failed"

        # state == "unrevealed"
        if cfg.dryrun:
            logger.info("DRYRUN – skipped revealing '%s'.", title)
            self.notify_games.append({"title": title, "url": order_url, "status": "available (dry run)"})
            return "settled"
        return await self._reveal(entry, order_url)

    async def _reveal(self, entry: dict, order_url: str) -> str:
        """Reveal one key by pressing its Reveal button, then read the page again."""
        title = entry["title"]
        on_order_page = entry.get("page_url") == order_url
        current = await self._eval_json("JSON.stringify(location.href)", "")
        if str(current).rstrip("/") != str(entry["page_url"]).rstrip("/"):
            await self.page.get(entry["page_url"])
            await self.sleep(4)
        try:
            clicked = await self.page.evaluate(
                CLICK_REVEAL_JS % json.dumps(title if on_order_page else None))
        except Exception as e:
            logger.debug("Could not press Reveal for '%s': %s", title, e)
            clicked = False
        if not clicked:
            logger.warning("No Reveal button could be pressed for '%s'.", title)
            await self._remember(entry["game_id"], title, order_url, "failed:reveal-failed")
            self.notify_games.append({"title": title, "url": order_url, "status": "failed:reveal-failed"})
            return "failed"
        await self.sleep(4)
        if await self._human_challenge_present():
            logger.warning("A human check is blocking the reveal of '%s', so the rest "
                           "of this run waits.", title)
            return "blocked"

        fresh = await self._reread_entry(entry)
        if fresh is not None and fresh["state"] == "revealed" and fresh["code"]:
            logger.info("✓ Revealed '%s' (%s key %s).", title,
                        fresh["platform"] or "humble", mask_code(fresh["code"]))
            await self._remember(entry["game_id"], title, order_url, "claimed",
                                 key=fresh["code"], platform=fresh["platform"] or "humble")
            self.notify_games.append({"title": title, "url": order_url, "status": "claimed"})
            return "settled"
        if fresh is not None and fresh["state"] == "redeemed":
            logger.info("✓ Claimed '%s' (%s, no key: it goes to the linked account).",
                        title, fresh["platform"] or "humble")
            await self._remember(entry["game_id"], title, order_url, "claimed",
                                 platform=fresh["platform"] or "humble")
            self.notify_games.append({"title": title, "url": order_url, "status": "claimed"})
            return "settled"
        if fresh is not None and fresh["state"] == "expired":
            await self._remember(entry["game_id"], title, order_url, "skipped:expired")
            self.notify_games.append({"title": title, "url": order_url, "status": "skipped:expired"})
            return "settled"

        said = (fresh or {}).get("text", "") if fresh else ""
        lowered = said.lower()
        if "temporarily exhausted" in lowered or "out of keys" in lowered:
            # Humble's key pool for this game is empty right now. Nothing
            # is broken and nothing needs doing - the key is simply not
            # there yet - so the game stays unrevealed and the next run
            # tries again, with the state named instead of a fault.
            logger.warning("Humble is temporarily out of keys for '%s', so there is no key "
                           "to reveal yet. Nothing needs doing; it will be tried again on "
                           "the next run.", title)
            await self._remember(entry["game_id"], title, order_url, "failed:keys-exhausted")
            self.notify_games.append({"title": title, "url": order_url, "status": "failed:keys-exhausted"})
            return "failed"
        if "still processing" in lowered or "grant is pending" in lowered:
            # A linked-account grant still processing on the other side:
            # the same kind of wait state, named the same way.
            logger.warning("The grant for '%s' is still processing, so there is nothing "
                           "to collect yet. Nothing needs doing; it will be tried again "
                           "on the next run.", title)
            await self._remember(entry["game_id"], title, order_url, "failed:grant-pending")
            self.notify_games.append({"title": title, "url": order_url, "status": "failed:grant-pending"})
            return "failed"
        logger.warning("Humble did not reveal the key for '%s': %s",
                       title, entry_snippet(said) or "(the entry could not be re-read)")
        await self._remember(entry["game_id"], title, order_url, "failed:reveal-failed")
        self.notify_games.append({"title": title, "url": order_url, "status": "failed:reveal-failed"})
        return "failed"

    async def _reread_entry(self, entry: dict) -> dict | None:
        """The same entry, read again from the page the Reveal press left open."""
        if "/home/library/content/" not in str(entry.get("page_url") or ""):
            # An order page: the entry is the row carrying the same title.
            raws = await self._eval_json(ORDER_ENTRIES_JS, [])
            for raw in raws if isinstance(raws, list) else []:
                fresh = classify_entry(raw if isinstance(raw, dict) else {})
                if fresh["title"] == entry["title"]:
                    return fresh
            return None
        raw = await self._eval_json(CONTENT_ENTRY_JS, {})
        if not isinstance(raw, dict) or not raw:
            return None
        return classify_entry(raw)

    async def _claim_month(self, group: dict) -> None:
        """Settle one Choice month: record what is settled, reveal what is waiting.

        The order page is the month's work list - it carries the DLC
        entries the library's cards do not show. Only when it cannot be
        reached does the run work from the games' own pages, and says so:
        a month settled that way may miss an entry only the order page
        lists.
        """
        order_url = await self._order_url(group)
        entries = []
        if order_url:
            entries = await self._order_entries(order_url)
        if not entries:
            logger.warning("The month's order page could not be read, so its games are "
                           "worked from their own pages; an entry only the order page "
                           "lists (a DLC pack, say) is invisible this run.")
            order_url = order_url or URL_LIBRARY
            for game in group["games"]:
                entries.append(await self._content_entry(game))
        if not entries:
            logger.warning("No entries could be read for '%s' at all.",
                           month_display_name(group["slug"]))
            return
        waiting = sum(1 for e in entries if e["state"] == "unrevealed")
        if waiting:
            logger.info("🎮 %d Humble Choice game(s) to reveal.", waiting)
        for index, entry in enumerate(entries):
            outcome = "settled"
            try:
                outcome = await self._claim_entry(entry, order_url)
            except Exception:
                logger.exception("Settling '%s' failed", entry["title"])
                outcome = "failed"
            if outcome == "blocked":
                left = len(entries) - index - 1
                if left:
                    logger.warning("%d more entr(ies) wait for the next run.", left)
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

            group = await self._pick_month()
            if group is None:
                logger.info("No Humble Choice month is waiting to be claimed.")
                return
            await self._claim_month(group)

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
