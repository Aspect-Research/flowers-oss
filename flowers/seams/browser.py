"""Browser seam — headless browser automation for the no-API LAST MILE (a form submit, a booking).

``FakeBrowser`` is the offline, scriptable world the engine + tests run against; it can simulate a
submit that does NOT land (the fabricated-completion case the gate must HARD-refuse), a surface with
NO independent observation (unverifiable -> ask owner), and a NON-independent observer (observer ==
actor, which the gate must reject). ``BrowserbaseBrowser`` is the gated live adapter (Browserbase
cloud sessions + Playwright/CDP).

This seam only PRODUCES the independent observation; the trust contract (effect_kind='cua', observer
!= actor, screenshots never verify) lives in ``flowers.trustgate`` and is fed by the broker. See
``flowers.seams.interfaces.Browser``.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request

from flowers import policy, runtime
from flowers.seams.interfaces import BrowserActResult
from flowers.seams.search import _NoInternalRedirect, is_ssrf_blocked_host

# Read-only "observe-then-act" preview verbs: return the page's candidate actionable elements (with a
# stable ref + human label + selector) so the model resolves the EXACT control before a side-effecting
# submit — DOM-resilient (no brittle CSS guessing) and deterministic. This is the Stagehand-style
# observe(); the name `inspect` keeps it distinct from the trust-path verification ``observe()``.
_INSPECT_VERBS = frozenset({"inspect", "observe", "elements", "candidates"})


def is_side_effecting_action(action: str) -> bool:
    return policy.is_side_effecting("browser", action)


def _ref(params: dict):
    """The identifying token for this action's expected confirmation (drives the fingerprint)."""
    p = params or {}
    return p.get("ref") or p.get("confirmation") or p.get("summary") or p.get("subject")


def _fingerprint(params: dict) -> dict | None:
    ref = _ref(params or {})
    return {"confirmation": ref} if ref else None


class FakeBrowser:
    """Offline, scriptable :class:`Browser`. State per user: ``{user_id: {"confirmations": {id: fields}}}``.

      * ``drop_actions`` — side-effecting actions that 'succeed' but never land a confirmation (the
        fabricated browser-completion case the gate must HARD-refuse).
      * ``no_observation`` — ``observe()`` returns ``None`` (no independent observation -> unverifiable).
      * ``self_sourced`` — ``observer_id`` collapses to the acting session (observer == actor, which the
        gate must reject as non-independent).
      * ``pages`` — ``{url_substr: text}`` returned by read-only navigate/extract actions.
    """

    def __init__(self, *, drop_actions=(), no_observation=False, self_sourced=False, pages=None,
                 elements=None):
        self._state: dict[str, dict[str, dict[str, dict]]] = {}
        self._drop = {(a or "").lower() for a in drop_actions}
        self._no_observation = no_observation
        self._self_sourced = self_sourced
        self._pages = dict(pages or {})
        # inspect (observe-then-act preview) candidates: {url_substr: [{ref,label,selector,...}]}; an
        # entry keyed "" matches any page. Read-only; lets a test drive the resolve-before-submit flow.
        self._elements = dict(elements or {})
        self._counter = 0
        self._current: dict[str, str] = {}   # user_id -> current page url (navigate -> extract/click sequences)

    def available(self) -> bool:
        return True

    def _actor(self, user_id: str) -> str:
        return f"browser-session:{user_id}"

    def observer_id(self, user_id: str) -> str:
        # An INDEPENDENT observer is a DIFFERENT session/channel; self_sourced collapses it to the actor.
        return self._actor(user_id) if self._self_sourced else f"browser-observer:{user_id}"

    def _confirmations(self, user_id: str) -> dict:
        return self._state.setdefault(user_id, {}).setdefault("confirmations", {})

    def act(self, *, action: str, params: dict, user_id: str) -> BrowserActResult:
        params = params or {}
        actor = self._actor(user_id)
        if not is_side_effecting_action(action):
            # discrete read-only driving: navigate (any read op carrying a url) updates the current page;
            # a later click/type/extract with no url reads the CURRENT page — so multi-step browsing works.
            if params.get("url"):
                self._current[user_id] = str(params["url"])
            url = str(params.get("url") or self._current.get(user_id, ""))
            text = next((t for sub, t in self._pages.items() if sub and sub in url), params.get("_text", ""))
            els = []
            if (action or "").strip().lower() in _INSPECT_VERBS:
                # observe-then-act preview: the actionable elements on the CURRENT page (a read).
                els = next((list(v) for sub, v in self._elements.items() if not sub or sub in url), [])
            return BrowserActResult(ok=True, actor=actor, url=url, text=text, elements=els)
        # last-mile side effect: lands a confirmation on the surface unless this action is dropped
        if (action or "").lower() not in self._drop:
            self._counter += 1
            self._confirmations(user_id)[f"c{self._counter}"] = {
                "confirmation": _ref(params) or "ok", "target": params.get("target", ""),
                "action": (action or "").lower()}
        return BrowserActResult(ok=True, actor=actor, url=str(params.get("url", "")), text="submitted")

    def observe(self, *, action: str, params: dict, user_id: str) -> dict | None:
        if self._no_observation:
            return None
        return {k: dict(v) for k, v in self._confirmations(user_id).items()}

    def fingerprint(self, *, action: str, params: dict) -> dict | None:
        return _fingerprint(params or {})


# --------------------------------------------------------------------------- live (Browserbase)

_BB_BASE = "https://api.browserbase.com/v1"
_OBS_CAP = 50000      # chars kept in a matched confirmation field — large enough that a genuine token is
#                       not truncated away (a false 'not landed'); the raw read is still byte-bounded below
_ACT_TEXT_CAP = 8000  # page text returned to the MODEL (kept small; the model doesn't need the whole DOM)
_SECRET_RE = re.compile(r"(?:apikey|api_key|token)=[^&\s\"']+|wss?://\S+", re.IGNORECASE)


def _redact(s: object) -> str:
    """Strip Browserbase connect URLs / apiKey params from a message. The connectUrl embeds the LIVE API
    key and a raw Playwright/CDP error can carry it; it must never reach the (credential-less) model or logs."""
    return _SECRET_RE.sub("<redacted>", str(s))


def _bb_request(method: str, path: str, *, api_key: str, body: dict | None = None, timeout: float = 30):
    """A raw Browserbase REST call (urllib; no SDK dependency). Auth header is X-BB-API-Key."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_BB_BASE + path, data=data, method=method,
                                 headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _same_site(host_a: str, host_b: str) -> bool:
    """True iff two hostnames are the SAME site — equal, or one a subdomain of the other. Stricter than a
    registrable-domain heuristic (won't treat all of co.uk as one site); errs toward rejecting."""
    a, b = (host_a or "").lower().strip("."), (host_b or "").lower().strip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _observation_allowed(observe_url: str, action_url: str) -> bool:
    """An independent observation is TRUSTWORTHY only if it reads the SAME site the action affected and is
    not an internal/SSRF host. THE load-bearing trust fix: observe_url comes from the UNTRUSTED model, so a
    cross-site observer (point it at an attacker-controlled echo) or an internal/metadata host must NOT be
    allowed to confirm an effect — those degrade to unverifiable (ask the owner)."""
    oh = urllib.parse.urlparse(observe_url or "").hostname or ""
    ah = urllib.parse.urlparse(action_url or "").hostname or ""
    if not oh or not ah or not _same_site(oh, ah):   # pure check first (no DNS) — cross-site -> reject
        return False
    return not is_ssrf_blocked_host(oh)              # never observe an internal/loopback/metadata host


def _oob_get(url: str, *, timeout: float = 30) -> str:
    """An OUT-OF-BAND read (plain HTTP, a channel independent of the acting browser) behind the SAME SSRF
    guard the search fetch uses: refuse internal/loopback/metadata hosts and re-validate every redirect
    hop. Returns the RAW body (no HTML stripping) so a JSON read-back stays parseable."""
    if is_ssrf_blocked_host(urllib.parse.urlparse(url).hostname or ""):
        raise RuntimeError("refused: observation host is internal/non-public (SSRF guard)")
    opener = urllib.request.build_opener(_NoInternalRedirect())
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "FlowersBrowserObserver/1.0"})
    with opener.open(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def _parse_observation(body: str) -> dict:
    """Turn an independent-observation response into ``{item_id: {field: value}}`` (the shape
    ``effects.snapshot_diff`` consumes). The webhook.site requests JSON (``{"data": [<request>, ...]}``)
    becomes one item per request keyed by its request id, whose ``confirmation`` field is url + query +
    body — url/query FIRST so a token that lands in the URL survives the per-item cap. Any other body is a
    single re-observed surface keyed by a HASH OF ITS CONTENT, so that when a confirmation token appears
    the key CHANGES and the before/after diff sees it as an ADDED item (a constant key would diff as
    'changed', which the fingerprint matcher ignores -> a false 'not landed')."""
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        out: dict[str, dict] = {}
        for i, req in enumerate(payload["data"]):
            if not isinstance(req, dict):
                continue
            rid = str(req.get("uuid") or req.get("id") or i)
            confirmation = " ".join([                       # url + query FIRST -> a token in the URL is kept
                str(req.get("url", "")),
                json.dumps(req.get("query", {}), sort_keys=True),
                str(req.get("content", "")),
            ])[:_OBS_CAP]
            out[rid] = {"confirmation": confirmation, "method": str(req.get("method", ""))}
        return out
    text = (body or "").strip()
    if not text:
        return {}
    key = "oob:" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]   # content-keyed -> 'added'
    return {key: {"confirmation": text[:_OBS_CAP]}}


class BrowserbaseBrowser:
    """Live Browser adapter — Browserbase cloud sessions driven via Playwright-over-CDP. Gated on
    ``BROWSERBASE_API_KEY`` + ``BROWSERBASE_PROJECT_ID`` (unavailable offline, so the engine uses
    ``FakeBrowser``).

    Trust model: the ACTING session navigates/clicks/submits; verification is an INDEPENDENT
    observation, NEVER the actor's own page. ``observe()`` re-reads the affected surface either
    OUT-OF-BAND over plain HTTP (``observe_via='http'`` of an ``observe_url`` — a channel the acting
    browser can't spoof; the strongest) or via a FRESH Browserbase session (``observe_via='browser'``,
    a session distinct from the actor). The broker stamps the resulting cua effect with
    ``observer`` (this adapter's observer id) != ``actor`` (the acting session id), so the gate accepts
    it only on the expected fingerprint via that independent channel. No ``observe_url`` -> ``None`` ->
    the gate routes to ask-owner.

    Sessions are created with an explicit SHORT timeout and released via REQUEST_RELEASE in ``close()``
    so the tiny free-tier budget (1 browser-hour/month) is not silently drained.
    """

    KEY_ENV = "BROWSERBASE_API_KEY"
    PROJECT_ENV = "BROWSERBASE_PROJECT_ID"

    def __init__(self, *, api_key: str | None = None, project_id: str | None = None,
                 session_timeout: int = 120, connect_timeout_ms: int = 60000, context_store=None):
        self._api_key = api_key
        self._project_id = project_id
        self._session_timeout = max(60, min(int(session_timeout), 21600))  # Browserbase allows 60..21600s
        self._connect_timeout_ms = connect_timeout_ms
        self._pw = None
        # Persistent-login support (#5): when a ``context_store`` (a Store) is injected, a
        # session for (user_id, site) is created WITH the site's persistent Browserbase context, so a login
        # survives across runs. Actors are keyed by (user_id, site) so each site has its own logged-in
        # session; ``_current_site`` tracks the last-navigated host for url-less follow-up calls.
        self._context_store = context_store
        self._actors: dict[tuple[str, str], dict] = {}   # (user_id, site) -> {"session_id","browser","page"}
        self._current_site: dict[str, str] = {}          # user_id -> last-navigated host

    # ---- gating ----
    def available(self) -> bool:
        if runtime.force_offline():
            return False               # the offline kill-switch wins, even over explicitly-injected creds
        if self._api_key and self._project_id:
            return True
        return runtime.adapter_available(key_env=self.KEY_ENV) and bool(runtime.env(self.PROJECT_ENV))

    def _key(self) -> str:
        return self._api_key or runtime.env(self.KEY_ENV)

    def _proj(self) -> str:
        return self._project_id or runtime.env(self.PROJECT_ENV)

    def _require_available(self) -> None:
        """Refuse to touch the network when unavailable (offline by contract) — mirrors the search /
        integrations live adapters, so the offline suite can construct this without risking a call."""
        if not self.available():
            raise RuntimeError("BrowserbaseBrowser is unavailable (offline or "
                               "BROWSERBASE_API_KEY/BROWSERBASE_PROJECT_ID unset); refusing a network call")

    # ---- session lifecycle ----
    def _ensure_pw(self):
        if self._pw is None:
            from playwright.sync_api import sync_playwright  # imported lazily; only needed live
            self._pw = sync_playwright().start()
        return self._pw

    def _new_session(self, context_id: str | None = None) -> tuple[str, str]:
        body: dict = {"projectId": self._proj(), "timeout": self._session_timeout}
        if context_id:
            # start the session FROM the persistent context (logged-in profile) and PERSIST any new
            # cookies/storage back to it, so a one-time login keeps working across runs.
            body["browserSettings"] = {"context": {"id": context_id, "persist": True}}
        s = _bb_request("POST", "/sessions", api_key=self._key(), body=body)
        sid, curl = (s or {}).get("id"), (s or {}).get("connectUrl")
        if not sid or not curl:   # an error-shaped 200 body must be an honest failure, not a KeyError
            raise RuntimeError(f"Browserbase /sessions returned no id/connectUrl: {_redact(str(s)[:200])}")
        return str(sid), str(curl)

    def _ensure_context(self, site: str) -> str | None:
        """Get-or-create the persistent Browserbase context for one site. Cached in the injected
        ``context_store`` so the SAME context (its cookies/login) is reused across runs.
        Returns None when no store is wired or no site is known (-> an ephemeral session, today's behaviour).
        Site-scoped: a context is only ever reused for the SAME site that created it (no credential bleed)."""
        if not (self._context_store and site):
            return None
        existing = self._context_store.get_browser_context(site)
        if existing:
            return existing
        try:
            resp = _bb_request("POST", "/contexts", api_key=self._key(), body={"projectId": self._proj()})
        except Exception:
            return None             # context creation failed -> fall back to an ephemeral session
        cid = (resp or {}).get("id")
        if not cid:
            return None
        self._context_store.save_browser_context(site, str(cid))
        return str(cid)

    @staticmethod
    def _site_for(url: str) -> str:
        """The site key for a url: its registrable-ish host (lowercased), used to scope the context. An
        empty/relative url yields '' (the caller then falls back to the user's current site)."""
        raw = str(url or "")
        if raw and "//" not in raw:
            raw = "//" + raw            # urlparse needs a scheme/'//' to populate hostname
        return (urllib.parse.urlparse(raw).hostname or "").lower()

    def _site_of(self, user_id: str, params: dict) -> str:
        """The site this action targets — from its url/target host, else the user's last-navigated site
        (so a url-less click/extract continuing on the current page reuses the same logged-in session)."""
        host = self._site_for(params.get("url") or params.get("target") or "")
        if host:
            self._current_site[user_id] = host
            return host
        return self._current_site.get(user_id, "")

    def _release(self, session_id: str) -> None:
        try:
            _bb_request("POST", f"/sessions/{session_id}", api_key=self._key(),
                        body={"status": "REQUEST_RELEASE"})
        except Exception:
            pass   # double-release / already-ended -> ignore; this is best-effort cleanup

    def _connect(self, connect_url: str):
        pw = self._ensure_pw()
        browser = pw.chromium.connect_over_cdp(connect_url, timeout=self._connect_timeout_ms)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return browser, page

    def _actor(self, user_id: str, site: str = "") -> dict:
        key = (user_id, site)
        st = self._actors.get(key)
        if st is None:
            context_id = self._ensure_context(site)   # persistent per-site login, if wired
            sid, curl = self._new_session(context_id)
            try:
                browser, page = self._connect(curl)
            except Exception:
                self._release(sid)        # never ORPHAN a billed session if connect fails after create
                raise
            st = {"session_id": sid, "browser": browser, "page": page}
            self._actors[key] = st
        return st

    # ---- Protocol ----
    _CLICK_VERBS = frozenset({"click", "press", "tap"})
    _TYPE_VERBS = frozenset({"type", "fill", "enter", "input"})
    _SELECT_VERBS = frozenset({"select"})
    _CHECK_VERBS = frozenset({"check"})
    _BACK_VERBS = frozenset({"back"})

    def act(self, *, action: str, params: dict, user_id: str) -> BrowserActResult:
        self._require_available()
        params = params or {}
        try:   # _actor (session create + CDP connect) is INSIDE the try so a setup failure is a result
            st = self._actor(user_id, self._site_of(user_id, params))
            page, actor = st["page"], f"browser-session:{st['session_id']}"
            if is_side_effecting_action(action):
                return self._submit(page, actor, params)             # the last-mile (the verified effect)
            return self._drive(page, actor, (action or "").strip().lower(), params)   # a discrete read op
        except Exception as e:  # a driving/session failure is a result, not a crash; REDACT (connectUrl key)
            return BrowserActResult(ok=False, actor=f"browser-session:{user_id}",
                                    error=_redact(f"{type(e).__name__}: {e}"))

    def _drive(self, page, actor, verb: str, params: dict) -> BrowserActResult:
        """ONE discrete read-only browse op — navigate / click / type / select / check / back / extract. The
        session PERSISTS, so a model can navigate -> type -> click -> extract across separate calls (real
        multi-step browsing). NAVIGATE-FIRST whenever a url is supplied (so a click/type that carries a url
        navigates then acts in one call, matching _submit), then perform the verb. A click/type/select/check
        with an EMPTY selector returns ok=False (never page.click('')); a verb that performs nothing returns
        the page text (a read). Returns the resulting page text (a selector's, or body)."""
        if params.get("url"):
            page.goto(str(params["url"]), wait_until="domcontentloaded", timeout=45000)
        if verb in _INSPECT_VERBS:
            # observe-then-act: return the page's actionable elements so the model picks the EXACT control
            # before submitting (a read — no side effect), plus the page text for context.
            els = self._inspect_elements(page)
            text = page.inner_text("body")[:_ACT_TEXT_CAP]
            return BrowserActResult(ok=True, actor=actor, url=page.url, text=text, elements=els)
        sel = str(params.get("selector") or params.get("click") or "").strip()
        if verb in self._CLICK_VERBS:
            if not sel:
                return BrowserActResult(ok=False, actor=actor, url=page.url, error="click requires a selector")
            page.click(sel)
        elif verb in self._TYPE_VERBS:
            if not sel:
                return BrowserActResult(ok=False, actor=actor, url=page.url, error="type requires a selector")
            page.fill(sel, str(params.get("text") or params.get("value") or ""))
        elif verb in self._SELECT_VERBS:
            if not sel:
                return BrowserActResult(ok=False, actor=actor, url=page.url, error="select requires a selector")
            page.select_option(sel, str(params.get("value") or params.get("option") or ""))
        elif verb in self._CHECK_VERBS and sel:
            page.set_checked(sel, bool(params.get("checked", True)))
        elif verb in self._BACK_VERBS:
            page.go_back(wait_until="domcontentloaded", timeout=45000)
        # else (navigate/scroll/hover/wait/extract/read/check-without-selector): just read the resulting page
        text = page.inner_text(str(params.get("selector") or "body"))[:_ACT_TEXT_CAP]
        return BrowserActResult(ok=True, actor=actor, url=page.url, text=text)

    def _submit(self, page, actor, params: dict) -> BrowserActResult:
        """The SIDE-EFFECTING last-mile: navigate (if a url is given), fill any fields, click the submit
        control — the composite that constitutes the verified effect."""
        url = params.get("url")
        if url:
            page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
        for sel, val in (params.get("fill") or {}).items():
            page.fill(str(sel), str(val))
        if params.get("click"):
            page.click(str(params["click"]))
        text = page.inner_text(str(params.get("selector") or "body"))[:_ACT_TEXT_CAP]
        return BrowserActResult(ok=True, actor=actor, url=page.url, text=text)

    # The observe-then-act DOM read: actionable, VISIBLE elements with a human label + a usable selector,
    # so the model targets the right control by what it SAYS, not a brittle hand-written CSS path. Capped
    # so a huge page can't blow the prompt. (Live; covered by FakeBrowser offline, verified on Browserbase.)
    _INSPECT_JS = """() => {
        const sel = 'a,button,input,select,textarea,[role=button],[onclick]';
        const out = [];
        for (const el of document.querySelectorAll(sel)) {
            if (out.length >= 60) break;
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;            // skip hidden
            const label = (el.innerText || el.value || el.getAttribute('aria-label')
                || el.getAttribute('placeholder') || el.name || '').trim().slice(0, 80);
            let css = '';
            if (el.id) css = '#' + CSS.escape(el.id);
            else if (el.getAttribute('name')) css = el.tagName.toLowerCase()
                + '[name="' + el.getAttribute('name') + '"]';
            out.push({ref: 'e' + out.length, tag: el.tagName.toLowerCase(),
                      type: el.getAttribute('type') || '', label, selector: css});
        }
        return out;
    }"""

    def _inspect_elements(self, page) -> list:
        try:
            els = page.evaluate(self._INSPECT_JS)
            return list(els) if isinstance(els, list) else []
        except Exception:
            return []

    def observe(self, *, action: str, params: dict, user_id: str) -> dict | None:
        self._require_available()
        params = params or {}
        obs_url = params.get("observe_url")
        if not obs_url:
            return None    # no independent observation surface -> unverifiable (ask owner)
        # THE trust gate on the (model-chosen) observation: it must read the SAME site the action affected
        # and not be an internal host — else it is not a trustworthy independent observation -> ask owner.
        action_url = params.get("url") or params.get("target")
        if not _observation_allowed(str(obs_url), str(action_url or "")):
            return None
        try:
            if (params.get("observe_via") or "http").lower() == "browser":
                return self._observe_via_fresh_session(str(obs_url))
            return _parse_observation(_oob_get(str(obs_url)))
        except Exception:
            return None    # an observation failure -> unverifiable, NEVER a fabricated 'landed'

    def _observe_via_fresh_session(self, obs_url: str) -> dict:
        """A FRESH Browserbase session (distinct from the actor) re-loads the surface. Released even if
        the connect fails (sid is bound before _connect, so the finally always releases)."""
        sid = None
        try:
            sid, curl = self._new_session()
            browser, page = self._connect(curl)
            try:
                page.goto(obs_url, wait_until="domcontentloaded", timeout=45000)
                body = page.inner_text("body")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
            return _parse_observation(body)
        finally:
            if sid:
                self._release(sid)

    def observer_id(self, user_id: str) -> str:
        # An identity distinct from the acting session ("browser-session:<id>"); the genuine
        # independence is enforced by observe() using a separate channel (out-of-band HTTP / fresh session).
        return f"browserbase-observer:{user_id}"

    def fingerprint(self, *, action: str, params: dict) -> dict | None:
        return _fingerprint(params or {})

    # ---- cleanup (release sessions so the free-tier budget isn't drained) ----
    def close(self, user_id: str | None = None) -> None:
        keys = [k for k in list(self._actors) if user_id is None or k[0] == user_id]
        for key in keys:
            st = self._actors.pop(key, None)
            if not st:
                continue
            try:
                st["browser"].close()
            except Exception:
                pass
            self._release(st["session_id"])
        if not self._actors and self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
