"""The load-bearing trust gate — ONE deterministic decision core, no LLM anywhere in the path.

This is the project's entire differentiator. It refuses to report "done" on a finish that
rests on a stale read, a confirmed reliability signature, an unmet objective check, or an external
effect the world does not actually reflect — *prevention*, mechanically, with no model in the trust
path. Any advisory layer (an owner override or the autonomy mandate) may only ever make this stricter,
never authorize what it refuses.

**Zero-dependency rule.** Pure stdlib, no ``from flowers...`` imports — every function is pure over
plain primitives (str hashes, plain dicts, sets of paths). It can be unit-tested in isolation and
(later) loaded by file path into a constrained context (e.g. a sandbox-side staleness probe) without
importing the package. Keep it that way.

The flat record shape ``classify_effects`` consumes IS the public trust contract; see
``flowers.types.EffectRecord.as_gate_dict``.
"""

from __future__ import annotations

import hashlib
import os
import re

# Distinct marker for "this file no longer exists / cannot be read" at finish-time re-hash.
# Deliberately not a possible content hash and distinct from "<none>" (unknown content).
DELETED_MARKER = "<deleted>"

# Box-observation per-file size ceiling (bytes) — the pre-run workdir snapshot must never choke on a
# multi-megabyte data file or checked-in binary. ~256 KiB.
_SNAPSHOT_MAX_BYTES = 256 * 1024

# --------------------------------------------------------------------------- hashing


def content_hash(text) -> str:
    """A short stable hash of content. Equal text yields equal hashes; ``None`` hashes to a sentinel
    so "unknown content" is distinguishable from any real content."""
    if text is None:
        return "<none>"
    if not isinstance(text, str):
        text = str(text)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


# ------------------------------------------------ box-observation (workdir snapshot)
#
# The finish-time re-hash keys on the worker's OWN read/write set. But an agent can do all its I/O
# via shell (zero read/write events) and leave the re-hash blind to a stale artifact built on a
# since-changed source. So we also observe the workdir filesystem directly: snapshot it BEFORE the
# run, then ask which pre-existing files DRIFTED underneath it. Self-report-independent.


def _box_disk_hash(abspath: str) -> str:
    """Raw-BYTE fingerprint of a file (sha1 of the bytes, not utf-8 text) so BINARY sources are
    tracked, not skipped. Box-observation's own self-consistent hash. ``DELETED_MARKER`` on an
    unreadable/vanished file (deterministic, never raises)."""
    try:
        with open(abspath, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return DELETED_MARKER


def snapshot_dir(workdir) -> dict:
    """Snapshot every regular file under ``workdir`` as ``{relpath: raw-byte-hash}`` (the
    box-observation baseline, captured BEFORE the worker runs). Files larger than
    ``_SNAPSHOT_MAX_BYTES`` are skipped. Deterministic; a missing/non-directory workdir yields ``{}``."""
    snap: dict[str, str] = {}
    if not workdir or not os.path.isdir(workdir):
        return snap
    root = str(workdir)
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            abspath = os.path.join(dirpath, fname)
            try:
                if not os.path.isfile(abspath):
                    continue
                if os.path.getsize(abspath) > _SNAPSHOT_MAX_BYTES:
                    continue
            except OSError:
                continue
            digest = _box_disk_hash(abspath)
            if digest == DELETED_MARKER:
                continue
            rel = os.path.normcase(os.path.relpath(abspath, root))
            snap[rel] = digest
    return snap


def snapshot_drift(baseline: dict, current: dict) -> list:
    """Box-observation drift between two ``{relpath: hash}`` snapshots — baseline relpaths whose content
    CHANGED or VANISHED. NEW files (absent from baseline) are NOT drift (the agent's own artifacts).
    Pure; works for ANY sandbox (local fs OR a remote E2B microVM) because it compares snapshots, not
    paths — so the operator never reads the wrong filesystem for a remote box."""
    return sorted(rel for rel, believed in baseline.items() if (current or {}).get(rel) != believed)


# ----------------------------------------------------- post-hoc confirmation


def has_identical_redo(events: list[dict], flagged: set[str]) -> bool:
    """True iff some flagged path has a later successful write whose produced hash equals an earlier
    successful write's hash to that path — an identical-content redo. Hash-less writes can never
    *confirm* a redo (they stay advisory)."""
    seen: dict[str, set[str]] = {}
    for ev in events:
        if ev.get("kind") != "write" or not bool(ev.get("ok", True)) or not ev.get("path"):
            continue
        path = ev["path"]
        if path not in flagged:
            continue
        h = ev.get("hash")
        if h is None:
            continue
        hashes = seen.setdefault(path, set())
        if h in hashes:
            return True
        hashes.add(h)
    return False


def final_retry_failed(events: list[dict], flagged: set[str]) -> bool:
    """True iff some flagged command's FINAL occurrence in the stream still failed. A later success
    means the retry resolved (debugging, working as intended) — stays advisory."""
    final_ok: dict[str, bool] = {}
    for ev in events:
        if ev.get("kind") != "run":
            continue
        target = ev.get("path") or "(run)"
        if target in flagged:
            final_ok[target] = bool(ev.get("ok", True))
    return any(not ok for ok in final_ok.values())


def confirm_gate_breaking(
    sigs: list[str],
    events: list[dict],
    flagged_rewrites: set[str],
    flagged_retries: set[str],
) -> list[str]:
    """Of the signatures that fired in flight, which still contradict the completion at finish time.
    Returned in a stable order (completion, then redo, then retry)."""
    breaking: list[str] = []
    if "unsupported-completion" in sigs:
        breaking.append("unsupported-completion")
    if "forgot-own-edit" in sigs and has_identical_redo(events, flagged_rewrites):
        breaking.append("forgot-own-edit")
    if "failed-retry" in sigs and final_retry_failed(events, flagged_retries):
        breaking.append("failed-retry")
    return breaking


# --------------------------------------------------------------------- effect verification


# Evidence that is the AGENT'S OWN self-report and can therefore NEVER verify a side-effecting
# effect. A CUA "booked"/"posted" backed only by a screenshot, or any record whose ``verification``
# says it was self-asserted, is routed to ``unverifiable`` (ask the owner) — never accepted on the
# agent's word. Legacy/auto records never set these fields, so the guard is a strict no-op on them.
_SELF_REPORT_EVIDENCE: frozenset[str] = frozenset({"self_report", "screenshot", "self-report"})


def _is_self_sourced(rec: dict) -> bool:
    """True iff the effect evidence comes from the actor itself (a screenshot/self-report, or an
    observer whose identity equals the actor's). Defined only over the new optional fields, so legacy
    records read as not-self-sourced."""
    v = str(rec.get("verification") or "").strip().lower()
    if v in _SELF_REPORT_EVIDENCE:
        return True
    observer = rec.get("observer")
    actor = rec.get("actor")
    return observer is not None and actor is not None and observer == actor


# Effect kinds produced OUTSIDE a trusted proxy chokepoint (the agent can influence how they are
# recorded) must carry POSITIVE independent provenance to be accepted on expected/drift alone — a
# fail-closed allow-list. Trusted-proxy (composio) records are NOT in this set, so their verdict is
# unchanged.
_PROVENANCE_REQUIRED_TOOLKITS: frozenset[str] = frozenset({"browser", "cua"})


def _requires_provenance(rec: dict) -> bool:
    return (str(rec.get("toolkit") or "").lower() in _PROVENANCE_REQUIRED_TOOLKITS
            or str(rec.get("effect_kind") or "").lower() == "cua")


def _has_independent_provenance(rec: dict) -> bool:
    """True iff the record names an observer DISTINCT from the actor — the necessary condition for
    treating a CUA/comms effect's evidence as independent of the agent. No observer fails closed."""
    observer = rec.get("observer")
    actor = rec.get("actor")
    return bool(observer) and observer != actor


def classify_effects(
    actions: list[dict], *, claimed_done: bool
) -> tuple[list[str], list[str]]:
    """Verify a run's CLAIMED side-effecting effects against its effect log — the general entry
    covering every effect kind uniformly. Pure over plain dicts; no I/O, no LLM.

    ``actions`` is the effect log (in order); the TERMINAL record per ``action_id`` is what counts.
    Returns ``(unverified, unverifiable)``:

      * ``unverified``  — a side-effecting effect contradicted by the record (forwarded but read-back
        shows no such effect, or never authorized/executed yet the run claims done). Refused like a
        fabricated completion.
      * ``unverifiable`` — forwarded but NO reliable read-back exists (``drift_present`` is None), OR
        the only evidence is the agent's self-report/screenshot: routed to the owner (never auto-accept).

    Verification checks PRESENCE of the expected effect, never exclusivity, so concurrent external
    writers are never mistaken for failure.
    """
    if not claimed_done:
        return [], []
    terminal: dict[str, dict] = {}
    order: list[str] = []
    for rec in actions:
        aid = str(rec.get("action_id") or rec.get("request_id")
                  or f"{rec.get('toolkit')}:{rec.get('action')}")
        if aid not in terminal:
            order.append(aid)
        terminal[aid] = rec
    unverified: list[str] = []
    unverifiable: list[str] = []
    landed_gks: set[str] = set()      # grant_keys (action IDENTITIES) an independent read-back verified
    # A non-forwarded record that claims done but a retry of the SAME action later landed. Keyed by
    # identity (grant_key), NOT the toolkit:action label — a verified send to one recipient must never
    # forgive a FAILED send to another. Only genuinely retryable phases qualify; a refused (money/
    # illegal) or a deferred (unauthorized/unconnected) record is a hard refuse no sibling can soften.
    retryable_attempts: list[tuple[str, str]] = []   # (label, grant_key)
    for aid in order:
        rec = terminal[aid]
        side_effecting = rec.get("side_effecting")
        # A provenance-required record that CLAIMS an effect cannot be a read-only fetch: treat a
        # MISSING side_effecting as True so an omitted field fails CLOSED. No-op on proxy records
        # (which always set the field).
        if side_effecting is None and _requires_provenance(rec) and (
                "expected_present" in rec or "drift_present" in rec):
            side_effecting = True
        if not side_effecting:
            continue
        label = f"{rec.get('toolkit', '?')}:{rec.get('action', '?')}"
        phase = rec.get("phase")
        drift = rec.get("drift_present")        # True | False | None
        expected = rec.get("expected_present")  # True | False | None (precise)
        if phase == "forwarded":
            # An INDEPENDENT observation that the effect is ABSENT is a HARD refuse, even when other
            # evidence is self-reported. A provably-failed effect is never softened to ask-owner.
            if expected is False:
                unverified.append(label)
                continue
            # Self-report / screenshot / observer==actor can never VERIFY -> ask the owner.
            if _is_self_sourced(rec):
                unverifiable.append(label)
                continue
            # A CUA/comms-kind record needs POSITIVE independent provenance to be accepted on
            # expected/drift alone — else ask owner. No-op on composio records.
            if _requires_provenance(rec) and not _has_independent_provenance(rec):
                unverifiable.append(label)
                continue
            if expected is True:
                gk = rec.get("grant_key")
                if gk:
                    landed_gks.add(str(gk))     # this exact action verified -> can supersede its own retry
                continue                        # the EXPECTED effect is present -> verified
            # A provenance-required effect is verified ONLY by its expected fingerprint via the
            # independent observer — never by bare drift of a model-chosen surface. An independent
            # observer that saw NO change (drift False) is positive evidence of ABSENCE -> hard refuse;
            # only a MISSING observation (drift None) softens to ask-owner.
            if _requires_provenance(rec):
                if drift is False:
                    unverified.append(label)
                else:
                    unverifiable.append(label)
                continue
            # Composio with NO expected fingerprint (``expected`` is None here — True/False were handled
            # above). Bare presence of drift cannot prove THE specific effect landed: a CONCURRENT
            # unrelated writer also shows drift, so accepting it would be a false positive. It is never
            # auto-verified — only a read-back proving ABSENCE (drift False) is a hard refuse; anything
            # else routes to the owner.
            if drift is False:
                unverified.append(label)        # independent read-back shows NO change -> not supported
            else:
                unverifiable.append(label)      # drift True/None, no fingerprint -> ask the owner
        elif phase == "failed" and rec.get("mutating", True) is False:
            # A cleanly-FAILED NON-mutating fetch (e.g. a repo clone that errored on a bad slug before the
            # agent resolved the right one). The adapter itself reported failure AND nothing external was
            # touched, so there is no effect to fabricate and nothing to leak — it is honest history, not a
            # claimed-but-absent effect. It never poisons the run. (Mutating failures fall through to the
            # strict same-identity rule below: a failed send/create/delete is NEVER silently forgiven.)
            continue
        elif phase in ("failed", "attempted"):
            # A genuinely retryable attempt while the run claims done. A retry is normal: if a LATER
            # record of the SAME action (matched by grant_key identity) verifiably landed, this attempt
            # is stale history, not a fabricated completion (found live: a provider-failed create then
            # a verified create of identical params). Resolved after the loop against landed_gks — a
            # record with no grant_key, or whose identity never landed, stays a hard refuse.
            retryable_attempts.append((label, str(rec.get("grant_key") or "")))
        else:
            # refused / denied / deferred while the run claims done -> fabricated/unauthorized
            # completion. No sibling can forgive it: money/illegal refusals and unconnected/unapproved
            # deferrals are hard refuses by construction.
            unverified.append(label)
    for lbl, gk in retryable_attempts:
        if not (gk and gk in landed_gks):
            unverified.append(lbl)          # no verified retry of THIS exact action -> refuse
    return sorted(set(unverified)), sorted(set(unverifiable))


def _forwarded_labels(actions: list[dict], *, strict: bool) -> list[str]:
    """Shared core of ``verified_effects`` (strict) and ``landed_effects`` (relaxed). Walks the terminal
    record per action and collects the labels of side-effecting, forwarded, not-proven-missing effects.

    The ONLY difference is the composio-integration acceptance bar:
      * ``strict=True``  -> only a PRECISE expected fingerprint counts (``expected_present is True``).
      * ``strict=False`` -> forwarded-and-not-proven-missing counts (``True`` OR unverifiable ``None``).
    The CUA/browser provenance path is ALWAYS strict either way — a self-reported browser action can
    never satisfy a done-check on its own word; only an independent observer's confirmed fingerprint
    does. Pure, no I/O, no LLM."""
    terminal: dict[str, dict] = {}
    order: list[str] = []
    for rec in actions:
        aid = str(rec.get("action_id") or rec.get("request_id")
                  or f"{rec.get('toolkit')}:{rec.get('action')}")
        if aid not in terminal:
            order.append(aid)
        terminal[aid] = rec
    out: list[str] = []
    for aid in order:
        rec = terminal[aid]
        side_effecting = rec.get("side_effecting")
        if side_effecting is None and _requires_provenance(rec) and (
                "expected_present" in rec or "drift_present" in rec):
            side_effecting = True
        if not side_effecting or rec.get("phase") != "forwarded":
            continue
        if _is_self_sourced(rec) or rec.get("expected_present") is False:
            continue                          # proven-missing / self-sourced never counts (both modes)
        label = f"{rec.get('toolkit', '?')}:{rec.get('action', '?')}"
        if _requires_provenance(rec):
            if rec.get("expected_present") is True and _has_independent_provenance(rec):
                out.append(label)             # CUA/browser: only the fingerprint via an independent observer
            continue
        if strict:
            if rec.get("expected_present") is True:
                out.append(label)             # composio: verified ONLY by the precise expected fingerprint
        else:
            out.append(label)                 # composio: forwarded & not proven-missing (True or unverifiable)
    return sorted(set(out))


def verified_effects(actions: list[dict]) -> list[str]:
    """The labels of side-effecting effects the gate considers VERIFIED-as-landed — independently
    confirmed by the precise expected fingerprint. This is what the run's final "verified effects"
    report surfaces and what the fabrication gate treats as proven. An unverifiable (no reliable
    read-back) effect is NOT here. Pure, no I/O, no LLM."""
    return _forwarded_labels(actions, strict=True)


def landed_effects(actions: list[dict]) -> list[str]:
    """The labels an ``effect_landed`` done-check must consult: side-effecting effects that were
    FORWARDED to the provider and NOT proven-missing — i.e. verified OR honestly unverifiable (the
    provider accepted the call but no reliable read-back exists to independently confirm it). A
    proven-missing effect (``expected_present is False``) is excluded, so a genuinely-dropped send
    still fails the check and legitimately retries. This is DELIBERATELY looser than
    ``verified_effects``: the action already went out, so demanding independent confirmation before
    calling the step done would force a blind retry — a duplicate send. The broker's own idempotency
    (it never re-sends a forwarded-and-not-proven-missing action) rests on the same rule. Pure."""
    return _forwarded_labels(actions, strict=False)


# ----------------------------------------------------- objective "done" checks
#
# Generalizes "verified done" beyond code: the contract layer freezes objectively-checkable criteria
# up-front, a subset carry a typed ``objective_check`` decided MECHANICALLY at finish (no LLM). The
# bundle is plain primitives:
#   {"files": [relpaths present], "texts": {path: text}, "fetched_urls": [urls fetched through the proxy]}.
# An unknown/unevaluable kind -> UNMET (fail toward refusing).

_URL_RE = re.compile(r"https?://[^\s<>\"'`\[\]]+", re.IGNORECASE)
_CLOSER = {")": "(", "]": "["}


def _cited_urls(text: str) -> list[str]:
    """Every http(s) URL in ``text``, with trailing SENTENCE punctuation trimmed but URL-internal
    parens preserved (an unbalanced trailing ``)``/``]`` is peeled — a markdown close or prose wrap)."""
    out: list[str] = []
    for m in _URL_RE.findall(text or ""):
        u = m.rstrip(".,;:!?")
        while u and u[-1] in _CLOSER and u.count(u[-1]) > u.count(_CLOSER[u[-1]]):
            u = u[:-1].rstrip(".,;:!?")
        out.append(u)
    return out


def canonical_url(u: str) -> str:
    """A canonical form for membership comparison so trivially-equivalent URLs are neither a bypass
    nor a false refusal: lowercase scheme+host, drop default port, strip fragment, drop a lone
    trailing slash, fold a leading ``www.``."""
    try:
        import urllib.parse
        p = urllib.parse.urlsplit(u.strip())
        scheme = (p.scheme or "").lower()
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if p.port and not ((scheme == "http" and p.port == 80)
                           or (scheme == "https" and p.port == 443)):
            host = f"{host}:{p.port}"
        path = p.path.rstrip("/")
        query = ("?" + p.query) if p.query else ""
        return f"{scheme}://{host}{path}{query}"
    except Exception:  # pragma: no cover
        return (u or "").strip().rstrip("/")


def _objective_check_one(kind: str, params: dict, files: list, texts: dict,
                         fetched: set) -> tuple[bool, str]:
    """Evaluate ONE objective check against the bundle -> ``(ok, detail)``. ``detail`` is an
    actionable, model-facing explanation when it fails so a redirect fixes it in one shot. Pure."""
    if kind == "source_membership":
        deliverable = params.get("deliverable")
        scan = [texts.get(deliverable, "")] if deliverable else list(texts.values())
        cited = [u for blob in scan for u in _cited_urls(blob)]
        fetched_canon = {canonical_url(u) for u in fetched}
        seen, missing = set(), []
        for u in cited:
            cu = canonical_url(u)
            if cu in seen:
                continue
            seen.add(cu)
            if cu not in fetched_canon:
                missing.append(u)
        if not missing:
            return True, ""
        return False, (
            "the deliverable cites sources this run did NOT fetch — fetch each of these BEFORE "
            "citing it, or remove it from the deliverable: " + ", ".join(missing))
    if kind == "file_exists":
        path = str(params.get("path", ""))
        return (path in files), ("" if path in files
                                 else f"the expected deliverable file is missing: {path}")
    if kind == "file_count":
        suffix = str(params.get("suffix", "") or "")
        n = sum(1 for f in files if (not suffix or f.endswith(suffix)))
        try:
            need = int(params.get("min", 1))
        except (TypeError, ValueError):
            return False, "file_count check has a non-integer 'min'"
        return (n >= need), ("" if n >= need
                             else f"expected at least {need} {suffix or ''} file(s), found {n}")
    if kind == "regex_present":
        path = str(params.get("path", ""))
        try:
            ok = bool(re.search(str(params.get("pattern", "")), texts.get(path, "")))
        except re.error:
            return False, f"regex_present check has an invalid pattern for {path}"
        return ok, ("" if ok else f"{path} does not contain the required pattern "
                    f"{params.get('pattern', '')!r}")
    if kind == "effect_landed":
        # A non-filesystem deliverable: a named side-effect (toolkit:action) must have LANDED in the
        # effect log. The bundle carries ``landed_effects`` (labels that were forwarded and not
        # proven-missing — verified OR honestly unverifiable). A proven-missing send is absent here, so
        # it still fails and retries; an unverifiable-but-forwarded one passes, so we don't blind-retry
        # (double-send) something the provider already accepted.
        want = str(params.get("label", ""))
        landed = set(params.get("_landed_effects", []))  # injected by the operator at finish
        return (want in landed), ("" if want in landed
                                  else f"the required effect {want} did not land (no send was forwarded)")
    return False, f"unknown objective-check kind {kind!r} (treated as unmet — fail toward refusing)"


def _objective_iter(criteria: list, bundle: dict):
    """Yield ``(criterion_id, ok, detail)`` for each criterion carrying an objective_check."""
    files = list((bundle or {}).get("files") or [])
    texts = dict((bundle or {}).get("texts") or {})
    fetched = set((bundle or {}).get("fetched_urls") or [])
    # ``landed_effects`` is what effect_landed rests on; accept the legacy ``verified_effects`` key too
    # (older bundles / direct callers) so a strict-verified label still satisfies the check.
    landed_effects = list((bundle or {}).get("landed_effects")
                          or (bundle or {}).get("verified_effects") or [])
    for c in criteria or []:
        if not isinstance(c, dict):
            continue
        check = c.get("objective_check")
        if not isinstance(check, dict) or not check.get("kind"):
            continue
        cid = str(c.get("id") or check.get("kind"))
        kind = str(check.get("kind"))
        params = dict(check.get("params")) if isinstance(check.get("params"), dict) else {}
        if kind == "effect_landed":
            params["_landed_effects"] = landed_effects  # let the check see what actually landed
        ok, detail = _objective_check_one(kind, params, files, texts, fetched)
        yield cid, ok, detail


def evaluate_objective_checks(criteria: list, bundle: dict) -> list[str]:
    """Deterministically evaluate criteria carrying an ``objective_check`` against the evidence
    bundle. Returns the ids of criteria that are objectively UNMET. Pure, no I/O, no LLM."""
    return [cid for cid, ok, _detail in _objective_iter(criteria, bundle) if not ok]


def describe_objective_failures(criteria: list, bundle: dict) -> str:
    """An actionable explanation of WHICH objective checks failed and HOW to fix them, folded into a
    redirect. Empty when nothing failed."""
    return "; ".join(detail for _cid, ok, detail in _objective_iter(criteria, bundle)
                     if not ok and detail)


# --------------------------------------------------------------------- final verdict


def gate_verdict(
    *,
    claimed_done: bool,
    ok: bool,
    stale_files: list[str],
    gate_breaking: list[str],
    unverified_external: list[str] = (),
    unverifiable_external: list[str] = (),
    objective_unmet: list[str] = (),
) -> tuple[bool, str]:
    """The hard "done" gate — the single mechanical refusal. Returns ``(accept_done?, reason)``.

    Refuses a claimed-done — even one the worker swears to — when the record shows it untrustworthy:
    it rests on a stale read, a reliability signature was confirmed post-hoc, a required objective
    check is unmet, a claimed external effect is not reflected by read-back (``unverified_external``),
    or a claimed external effect cannot be verified at all (``unverifiable_external`` -> route to the
    owner). A result that does not even claim done, or that errored, trivially fails. Reliability-first
    floor; nothing may ACCEPT against it.
    """
    if not claimed_done:
        return False, "the worker did not claim completion"
    if not ok:
        return False, "the worker did not finish cleanly (it errored)"
    if stale_files:
        return False, (
            "the result rests on stale reads — "
            f"{', '.join(sorted(stale_files))} changed since the worker last read it; "
            "the finished work may be out of date"
        )
    breaking = sorted(set(gate_breaking))
    if breaking:
        return False, (
            "a reliability detector contradicts the completion claim: "
            f"{', '.join(breaking)}"
        )
    obj = sorted(set(objective_unmet))
    if obj:
        return False, (
            "a required objective check did not pass — the deliverable does not yet meet the "
            f"frozen definition of done: {', '.join(obj)}"
        )
    unver = sorted(set(unverified_external))
    if unver:
        return False, (
            "a claimed external effect is not reflected by the external state "
            f"(read-back shows no such effect): {', '.join(unver)}"
        )
    unverifiable = sorted(set(unverifiable_external))
    if unverifiable:
        return False, (
            "a claimed external effect could not be verified (no reliable read-back) — "
            f"needs your confirmation: {', '.join(unverifiable)}"
        )
    return True, "no reliability flags; the completion claim is supported by the record"
