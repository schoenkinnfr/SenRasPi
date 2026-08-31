"""
Talks to the Sentinelle server.

SPDX-License-Identifier: AGPL-3.0-only

Two things happen here and nothing else:

  redeem_code()  one-time exchange of a 10-character pairing code, typed on
                 this Pi, for a long-lived dashboard-scoped kiosk token.
  Poller         a background thread that fetches /kiosk/data on an interval
                 and hands the newest snapshot to the renderer.

Deliberately stdlib-only (urllib, not requests). This runs on a 1GB Pi that
may boot before the network is up; every dependency avoided is one less thing
that can fail to import at 3am.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "sentinelle-pi-display/1.0"
TIMEOUT = 15


class PairError(RuntimeError):
    """A pairing attempt failed in a way worth showing the user verbatim."""


def _post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            pass
        raise PairError(f"server said {e.code}{': ' + detail if detail else ''}") from e
    except urllib.error.URLError as e:
        raise PairError(f"cannot reach {url} — {e.reason}") from e


def redeem_code(base_url: str, code: str, label: str) -> dict[str, Any]:
    """Exchanges a pairing code for a kiosk token.

    The code is single-use and short-lived; the token that comes back is
    neither. Returns {"token", "base_url", "label", "scope"}.
    """
    normalised = normalise_code(code)
    url = f"{base_url.rstrip('/')}/pair/redeem"
    out = _post_json(url, {"code": normalised, "label": label})
    if not out.get("token"):
        raise PairError("server accepted the code but returned no token")
    return out


CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_CONFUSABLE = {"0": "O", "O": "O", "1": "I", "I": "I", "L": "I"}


def normalise_code(code: str) -> str:
    """Uppercases, strips separators, and rejects anything not 10 valid chars.

    The alphabet excludes 0/O and 1/I/L, so a code read off a screen and typed
    on a Pi keyboard cannot be ambiguous. If someone types the confusable
    character anyway we do NOT silently substitute — a wrong guess would burn
    one of their rate-limited attempts on a code they typed correctly. We say
    which character is the problem instead.
    """
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    bad = sorted({ch for ch in cleaned if ch not in CODE_ALPHABET})
    if bad:
        hints = [f"{ch!r}" + (f" (did you mean {_CONFUSABLE[ch]}?)" if ch in _CONFUSABLE else "")
                 for ch in bad]
        raise PairError("codes never contain " + ", ".join(hints))
    if len(cleaned) != 10:
        raise PairError(f"a pairing code is 10 characters; you typed {len(cleaned)}")
    return cleaned


def format_code(code: str) -> str:
    """XXXXX-XXXXX, which is how the web app shows it."""
    return f"{code[:5]}-{code[5:]}" if len(code) == 10 else code


# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """What the renderer draws. `ok` false means we have nothing current."""

    ok: bool = False
    data: dict[str, Any] | None = None
    fetched_at: float = 0.0
    consecutive_failures: int = 0
    last_error: str | None = None

    @property
    def offline(self) -> bool:
        """Two failures in a row. One is a blip; two is worth telling the user.

        Shown as a banner rather than by blanking the number, because a screen
        that goes empty reads as broken hardware — the useful message is "this
        number is the last one I got, and I'm no longer getting them".
        """
        return self.consecutive_failures >= 2


class Poller(threading.Thread):
    """Fetches /kiosk/data forever. Never raises into the render loop."""

    daemon = True

    def __init__(self, cfg, on_update=None):
        super().__init__(name="sentinelle-poller")
        self.cfg = cfg
        self.snapshot = Snapshot()
        # NOT self._stop: threading.Thread already has a private _stop()
        # method that join() calls internally, and shadowing it with an Event
        # makes join() raise "'Event' object is not callable" -- from inside
        # the standard library, on a line you did not write.
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._on_update = on_update

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def refresh_now(self) -> None:
        """Ask for an immediate fetch — used when someone taps the screen."""
        self._wake.set()

    def get(self) -> Snapshot:
        with self._lock:
            return self.snapshot

    # ------------------------------------------------------------------
    def _url(self) -> str:
        q = urllib.parse.urlencode({"k": self.cfg.token, "h": self.cfg.hours})
        return f"{self.cfg.kiosk_url}?{q}"

    def _fetch_once(self) -> None:
        req = urllib.request.Request(
            self._url(), headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
            with self._lock:
                self.snapshot = Snapshot(ok=True, data=payload, fetched_at=time.time())
        except urllib.error.HTTPError as e:
            # 401 is terminal in practice — the token was revoked from Settings,
            # or it is ambient-scoped and cannot read this endpoint. Say so
            # plainly instead of retrying into a wall silently.
            msg = {
                401: "access revoked or wrong scope — re-pair this display",
                404: "no glucose data on the server yet",
            }.get(e.code, f"server error {e.code}")
            self._record_failure(msg)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            self._record_failure(f"offline — {getattr(e, 'reason', e)}")

    def _record_failure(self, msg: str) -> None:
        with self._lock:
            prev = self.snapshot
            self.snapshot = Snapshot(
                ok=prev.ok,
                data=prev.data,
                fetched_at=prev.fetched_at,
                consecutive_failures=prev.consecutive_failures + 1,
                last_error=msg,
            )

    def run(self) -> None:
        backoff = 0
        while not self._stopping.is_set():
            self._fetch_once()
            if self._on_update:
                try:
                    self._on_update(self.get())
                except Exception:
                    pass  # a broken callback must never kill the poller
            snap = self.get()
            # Back off on repeated failure so a server outage does not turn
            # into a request every 60s from every Pi anyone ever paired, but
            # cap it: the screen should recover within a couple of minutes of
            # the network coming back, not twenty.
            backoff = min(snap.consecutive_failures, 4) * 15 if snap.consecutive_failures else 0
            self._wake.wait(self.cfg.poll_seconds + backoff)
            self._wake.clear()


# ─────────────────────────────────────────────────────────────────────────────
# The daily review behind the OTHER page.
#
# All of the work happens on the server: four agents read the last 24 hours and
# leave two or three sentences and a joke in a table. The Pi's whole job is to
# fetch that text every quarter of an hour and, when someone presses REFRESH,
# to ask for a new one and then watch for it to land.
#
# Failure here must be completely inert. The glucose display is the reason this
# device exists; a review that will not load is a card that says so, never a
# missing number.


@dataclass
class Review:
    """The newest review the server has, or why we do not have one."""

    ok: bool = False
    data: dict[str, Any] | None = None
    fetched_at: float = 0.0
    last_error: str | None = None
    #: A refresh has been asked for and the server has not finished yet.
    generating: bool = False

    @property
    def recommendation(self) -> str | None:
        return (self.data or {}).get("recommendation")

    @property
    def joke(self) -> str | None:
        return (self.data or {}).get("joke")

    @property
    def is_today(self) -> bool:
        return bool((self.data or {}).get("is_today"))


class ReviewPoller(threading.Thread):
    """Fetches /kiosk/review slowly, and POSTs /kiosk/review/refresh on demand.

    Two intervals rather than one: the review changes once a day, so asking
    every fifteen minutes is already generous — but once a refresh has been
    requested the answer is a minute or two away, and waiting a quarter of an
    hour to notice it would make the button feel broken.
    """

    daemon = True

    #: How often to re-ask while the server is generating.
    BUSY_SECONDS = 20
    #: Give up on "still generating" after this long and go back to the slow
    #: interval, so a server-side failure cannot leave the panel spinning.
    BUSY_TIMEOUT = 6 * 60

    def __init__(self, cfg):
        super().__init__(name="sentinelle-review")
        self.cfg = cfg
        self.review = Review()
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._refresh_requested = False
        self._busy_since = 0.0

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def get(self) -> Review:
        with self._lock:
            return self.review

    def refresh_now(self) -> None:
        """Ask the server to regenerate. Returns immediately; the answer
        arrives on a later poll."""
        with self._lock:
            self._refresh_requested = True
            self.review = Review(
                ok=self.review.ok, data=self.review.data,
                fetched_at=self.review.fetched_at, last_error=None, generating=True,
            )
        self._busy_since = time.time()
        self._wake.set()

    # ------------------------------------------------------------------
    def _post_refresh(self) -> None:
        body = json.dumps({"k": self.cfg.token}).encode()
        req = urllib.request.Request(
            self.cfg.review_refresh_url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("error", "")
            except Exception:
                pass
            # 429 is the server's rate limit, and it is a normal answer to a
            # button on a wall: say so on the panel rather than treating it as
            # a fault.
            self._fail("too soon — try again shortly" if e.code == 429
                       else f"refresh refused ({e.code}{': ' + detail if detail else ''})")
            self._busy_since = 0.0
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self._fail(f"cannot reach the server — {getattr(e, 'reason', e)}")
            self._busy_since = 0.0

    def _fetch_once(self) -> None:
        url = f"{self.cfg.review_url}?{urllib.parse.urlencode({'k': self.cfg.token})}"
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            self._fail({
                401: "access revoked or wrong scope — re-pair this display",
                404: "this server has no review endpoint yet",
            }.get(e.code, f"server error {e.code}"))
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            self._fail(f"offline — {getattr(e, 'reason', e)}")
            return

        generating = bool(payload.get("generating"))
        if not generating and self._busy_since:
            self._busy_since = 0.0
        with self._lock:
            self.review = Review(
                ok=bool(payload.get("ok")), data=payload,
                fetched_at=time.time(), last_error=None, generating=generating,
            )

    def _fail(self, msg: str) -> None:
        with self._lock:
            prev = self.review
            self.review = Review(
                ok=prev.ok, data=prev.data, fetched_at=prev.fetched_at,
                last_error=msg, generating=False,
            )

    def run(self) -> None:
        while not self._stopping.is_set():
            if self._refresh_requested:
                with self._lock:
                    self._refresh_requested = False
                self._post_refresh()
            self._fetch_once()

            busy = self._busy_since and (time.time() - self._busy_since) < self.BUSY_TIMEOUT
            if self._busy_since and not busy:
                self._busy_since = 0.0          # stopped waiting; it is not coming
            delay = self.BUSY_SECONDS if busy else max(self.cfg.review_minutes, 5) * 60
            self._wake.wait(delay)
            self._wake.clear()
