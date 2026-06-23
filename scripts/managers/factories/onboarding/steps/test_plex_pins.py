"""Tests for the Plex PIN-capture flow — the numbered Home-profile pick-list, live
PIN verification (mint the per-user token), and the graceful fallbacks (no scope /
fetch failure / headless → free-text titles; flaky verify never blocks).
"""
from __future__ import annotations

from scripts.managers.factories.onboarding import validators
from scripts.managers.factories.onboarding.steps import media
from scripts.managers.factories.onboarding.steps.media import (
    PlexStep,
    _select_indices,
    _verify_and_store_pin,
)


# ── _select_indices (1-based → 0-based, tolerant) ─────────────────────────────
def test_select_indices_comma_and_space():
    assert _select_indices("1,3 5", 5) == [0, 2, 4]


def test_select_indices_range_dedupe_and_clamp():
    assert _select_indices("2-4, 3", 10) == [1, 2, 3]      # range + overlap deduped
    assert _select_indices("0, 3, 99", 3) == [2]            # out-of-range dropped (incl. 0 and >count)
    assert _select_indices("", 3) == [] and _select_indices("  x ,, ", 3) == []
    assert _select_indices("-3", 3) == [] and _select_indices("3-", 3) == []  # half-ranges ignored


# ── _parse_plex_home_users (schema-tolerant, keeps uuid) ──────────────────────
def test_parse_home_users_shapes_and_flags():
    p = validators._parse_plex_home_users
    rob = p({"users": [{"uuid": "ua", "title": "Rob", "admin": True}]})[0]
    assert rob["is_admin"] and rob["uuid"] == "ua"
    assert p([{"id": 7, "friendlyName": "Kid", "restricted": True}])[0]["is_managed"]
    assert p({"MediaContainer": {"User": [{"uuid": "g", "username": "Guest", "hasPassword": 1}]}})[0]["protected"]
    assert p([{"title": ""}, "junk", {"no_title": 1}]) == []   # untitled / non-dict dropped
    assert p(None) == []


# ── _redact scrubs a PIN that rode in on a URL ────────────────────────────────
def test_redact_strips_pin_query_param():
    out = validators._redact("ConnectionError url: /home/users/ua/switch?pin=1234 (Caused by ...)")
    assert "1234" not in out and "[REDACTED]" in out


# ── plex_switch_user (the live PIN verify) ────────────────────────────────────
class _Resp:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._body


def _patch_post(monkeypatch, fn):
    monkeypatch.setattr(validators.requests, "post", fn)


def test_switch_ok_when_authtoken_minted(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(200, {"authToken": "tok-xyz"}))
    r = validators.plex_switch_user("admin", "cid", "ua", "1234")
    assert r["ok"] and not r["rejected"]


def test_switch_rejected_on_401(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(401))
    r = validators.plex_switch_user("admin", "cid", "ua", "9999")
    assert not r["ok"] and r["rejected"] and r["error"] == "PIN rejected"


def test_switch_inconclusive_on_http_error(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(500))
    r = validators.plex_switch_user("admin", "cid", "ua", "1234")
    assert not r["ok"] and not r["rejected"] and r["error"] == "HTTP 500"


def test_switch_ok_on_201_created(monkeypatch):
    # regression: Plex returns 201 Created (not 200) on a successful switch — a correct
    # PIN must verify, not fall into "couldn't verify (HTTP 201)".
    _patch_post(monkeypatch, lambda *a, **k: _Resp(201, {"authToken": "tok-201"}))
    r = validators.plex_switch_user("admin", "cid", "ua", "2085")
    assert r["ok"] and not r["rejected"]


def test_switch_inconclusive_on_2xx_without_token(monkeypatch):
    # 2xx means Plex accepted the PIN (a wrong PIN 4xx's); a missing token is a usability
    # unknown, NOT a rejection — must be inconclusive so a correct PIN isn't called wrong.
    _patch_post(monkeypatch, lambda *a, **k: _Resp(200, {"username": "x"}))
    r = validators.plex_switch_user("admin", "cid", "ua", "1234")
    assert not r["ok"] and not r["rejected"]


def test_switch_ok_with_alternate_token_keys(monkeypatch):
    # mirror the runtime's _extract_token: any of these shapes is a successful mint.
    for body in ({"authenticationToken": "x"}, {"token": "z"},
                 {"authentication_token": "w"}, {"user": {"authToken": "n"}},
                 {"user": {"authenticationToken": "m"}}):
        _patch_post(monkeypatch, lambda *a, _b=body, **k: _Resp(200, _b))
        assert validators.plex_switch_user("admin", "cid", "ua", "1234")["ok"], body


def test_switch_rejected_on_400_and_422(monkeypatch):
    for code in (400, 422):
        _patch_post(monkeypatch, lambda *a, _c=code, **k: _Resp(_c))
        r = validators.plex_switch_user("admin", "cid", "ua", "1234")
        assert not r["ok"] and r["rejected"], code


def test_switch_sends_device_contract_headers(monkeypatch):
    seen = {}

    def _capture(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["headers"] = headers
        return _Resp(200, {"authToken": "ok"})

    _patch_post(monkeypatch, _capture)
    validators.plex_switch_user("admin-tok", "cid-123", "ua", "4321")
    h = seen["headers"]
    assert h["X-Plex-Token"] == "admin-tok"
    assert h["X-Plex-Product"] == "Glidearr" and h["X-Plex-Version"] == "1.0"
    assert h["X-Plex-Client-Identifier"] == "cid-123"
    assert seen["params"] == {"pin": "4321"} and "switch" in seen["url"]


def test_switch_network_error_redacts_pin(monkeypatch):
    def _boom(*a, **k):
        raise Exception("Max retries exceeded url: /home/users/ua/switch?pin=1234")
    _patch_post(monkeypatch, _boom)
    r = validators.plex_switch_user("admin", "cid", "ua", "1234")
    assert not r["ok"] and not r["rejected"]
    assert "1234" not in r["error"] and "[REDACTED]" in r["error"]


def test_switch_missing_args_short_circuits(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert validators.plex_switch_user("", "cid", "ua", "1234")["error"] == "missing token/uuid/pin"
    assert validators.plex_switch_user("admin", "cid", "", "1234")["error"] == "missing token/uuid/pin"
    assert validators.plex_switch_user("admin", "cid", "ua", "")["error"] == "missing token/uuid/pin"


# ── prompter double ───────────────────────────────────────────────────────────
class _Prompter:
    """Scripted prompter. ``pins`` maps a secret path → a fixed PIN OR a list of PINs
    consumed across re-prompts (for the rejection-retry flow)."""

    def __init__(self, *, interactive=True, confirm=True, select="", titles="", pins=None,
                 choices=None):
        self.is_interactive = interactive
        self._confirm = confirm
        self._select = select
        self._titles = titles
        self._pins = pins or {}
        self._choices = choices or {}     # path → chosen value (else the prompt default)
        self.notices: list[str] = []
        self.warns: list[str] = []
        self.successes: list[str] = []

    def section(self, *a, **k): pass
    def notice(self, m): self.notices.append(m)
    def success(self, m): self.successes.append(m)
    def warn(self, m): self.warns.append(m)
    def confirm(self, path, label, default=False): return self._confirm
    def choice(self, path, label, options, default=None, required=False):
        return self._choices.get(path, default)
    def text(self, path, label, default="", required=False, secret=False):
        if path == "plex.pin_select":
            return self._select
        if path == "plex.pin_titles":
            return self._titles
        return default
    def secret(self, path, label, default="", required=False):
        val = self._pins.get(path, "")
        if isinstance(val, list):
            return val.pop(0) if val else ""
        return val


# ── _verify_and_store_pin (verify + retry-on-rejection) ───────────────────────
def _ok(*a, **k):      return {"ok": True, "rejected": False, "error": None}
def _reject(*a, **k):  return {"ok": False, "rejected": True, "error": "PIN rejected"}
def _flaky(*a, **k):   return {"ok": False, "rejected": False, "error": "HTTP 500"}


def test_verify_stores_and_confirms_on_success(monkeypatch):
    monkeypatch.setattr(validators, "plex_switch_user", _ok)
    p = _Prompter(pins={"plex.pins.Kids.pin": "1234"})
    pins = {}
    _verify_and_store_pin(p, pins, "Kids", "ua", "admin", "cid")
    assert pins == {"Kids": {"pin": "1234"}}
    assert any("verified for 'Kids'" in s for s in p.successes)


def test_verify_reprompts_then_accepts_corrected_pin(monkeypatch):
    calls = {"n": 0}

    def _first_reject_then_ok(*a, **k):
        calls["n"] += 1
        return _reject() if calls["n"] == 1 else _ok()

    monkeypatch.setattr(validators, "plex_switch_user", _first_reject_then_ok)
    p = _Prompter(pins={"plex.pins.Kids.pin": ["1111", "2222"]})   # wrong, then right
    pins = {}
    _verify_and_store_pin(p, pins, "Kids", "ua", "admin", "cid")
    assert pins == {"Kids": {"pin": "2222"}}                       # the corrected one
    assert any("rejected by Plex" in w for w in p.warns)
    assert any("verified" in s for s in p.successes)


def test_verify_persistent_rejection_saves_with_warning(monkeypatch):
    monkeypatch.setattr(validators, "plex_switch_user", _reject)
    p = _Prompter(pins={"plex.pins.Kids.pin": ["1111", "2222"]})
    pins = {}
    _verify_and_store_pin(p, pins, "Kids", "ua", "admin", "cid")
    assert pins == {"Kids": {"pin": "2222"}}                       # last entry kept
    assert any("still rejected" in w for w in p.warns)


def test_verify_inconclusive_saves_unverified(monkeypatch):
    monkeypatch.setattr(validators, "plex_switch_user", _flaky)
    p = _Prompter(pins={"plex.pins.Kids.pin": "1234"})
    pins = {}
    _verify_and_store_pin(p, pins, "Kids", "ua", "admin", "cid")
    assert pins == {"Kids": {"pin": "1234"}}
    assert any("Couldn't verify" in w for w in p.warns)


def test_verify_blank_pin_skips(monkeypatch):
    monkeypatch.setattr(validators, "plex_switch_user",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no verify on blank")))
    p = _Prompter(pins={})
    pins = {}
    _verify_and_store_pin(p, pins, "Kids", "ua", "admin", "cid")
    assert pins == {}


def test_verify_skipped_without_uuid(monkeypatch):
    monkeypatch.setattr(validators, "plex_switch_user",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no uuid → no verify")))
    p = _Prompter(pins={"plex.pins.Kids.pin": "1234"})
    pins = {}
    _verify_and_store_pin(p, pins, "Kids", "", "admin", "cid")     # empty uuid
    assert pins == {"Kids": {"pin": "1234"}}                       # stored, just not verified


# ── _configure_pins end-to-end flows ──────────────────────────────────────────
_ROSTER = [
    {"uuid": "u1", "title": "Trizzd", "is_admin": True, "is_managed": False, "protected": False},
    {"uuid": "u2", "title": "Kids", "is_admin": False, "is_managed": True, "protected": True},
    {"uuid": "u3", "title": "Guest", "is_admin": False, "is_managed": False, "protected": True},
]


def _run_pins(monkeypatch, prompter, *, scope_ok=True, roster_result=None,
              switch=_ok, cfg=None):
    if roster_result is not None:
        monkeypatch.setattr(validators, "plex_home_users", lambda *a, **k: roster_result)
    monkeypatch.setattr(validators, "plex_switch_user", switch)
    cfg = cfg if cfg is not None else {"plex": {}}
    step = PlexStep(logger=None)
    roster = step._fetch_home_roster(prompter, "tok", "cid", scope_ok=scope_ok)
    step._configure_pins(prompter, cfg, roster, "tok", "cid")
    return cfg


def test_roster_numbered_selection_verified(monkeypatch):
    p = _Prompter(select="2,3", pins={
        "plex.pins.Kids.pin": "1234",
        "plex.pins.Guest.pin": "9999",
    })
    cfg = _run_pins(monkeypatch, p, roster_result={"ok": True, "users": _ROSTER, "error": None})
    assert cfg["plex"]["pins"] == {"Kids": {"pin": "1234"}, "Guest": {"pin": "9999"}}
    listing = "\n".join(p.notices)
    assert "1) Trizzd" in listing and "PIN-protected" in listing and "owner" in listing
    assert sum("verified" in s for s in p.successes) == 2      # both PINs verified


def test_roster_wrong_pin_warns(monkeypatch):
    # selecting Kids, PIN rejected on every attempt → saved with the run-time warning.
    p = _Prompter(select="2", pins={"plex.pins.Kids.pin": ["bad1", "bad2"]})
    cfg = _run_pins(monkeypatch, p, roster_result={"ok": True, "users": _ROSTER, "error": None},
                    switch=_reject)
    assert cfg["plex"]["pins"] == {"Kids": {"pin": "bad2"}}
    assert any("still rejected" in w for w in p.warns)


def test_roster_selected_but_blank_pin_not_stored(monkeypatch):
    p = _Prompter(select="2", pins={})           # selected Kids but entered no PIN
    cfg = _run_pins(monkeypatch, p, roster_result={"ok": True, "users": _ROSTER, "error": None})
    assert "pins" not in cfg["plex"]             # nothing written


def test_empty_roster_short_circuits(monkeypatch):
    p = _Prompter(select="1")
    cfg = _run_pins(monkeypatch, p, roster_result={"ok": True, "users": [], "error": None})
    assert "pins" not in cfg["plex"]
    assert any("No Home profiles" in n for n in p.notices)


def test_fetch_failure_degrades_to_titles(monkeypatch):
    # roster fetch errors → warn + free-text title entry still works (no verify there).
    p = _Prompter(titles="Kids", pins={"plex.pins.Kids.pin": "1111"})
    cfg = _run_pins(monkeypatch, p,
                    roster_result={"ok": False, "users": [], "error": "HTTP 500"})
    assert cfg["plex"]["pins"] == {"Kids": {"pin": "1111"}}
    assert any("Couldn't list Home profiles" in w for w in p.warns)


def test_no_scope_uses_titles_without_fetch(monkeypatch):
    # scope_ok False → _fetch_home_roster never calls the API → [] → titles fallback.
    monkeypatch.setattr(validators, "plex_home_users",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch without scope")))
    monkeypatch.setattr(validators, "plex_switch_user", _ok)
    p = _Prompter(titles="Guest", pins={"plex.pins.Guest.pin": "2222"})
    cfg = {"plex": {}}
    step = PlexStep(logger=None)
    roster = step._fetch_home_roster(p, "tok", "cid", scope_ok=False)
    assert roster == []
    step._configure_pins(p, cfg, roster, "tok", "cid")
    assert cfg["plex"]["pins"] == {"Guest": {"pin": "2222"}}


def test_headless_uses_titles_path(monkeypatch):
    monkeypatch.setattr(validators, "plex_home_users",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch headless")))
    monkeypatch.setattr(validators, "plex_switch_user", _ok)
    p = _Prompter(interactive=False, titles="Kids", pins={"plex.pins.Kids.pin": "7"})
    cfg = {"plex": {}}
    step = PlexStep(logger=None)
    roster = step._fetch_home_roster(p, "tok", "cid", scope_ok=True)   # [] (not interactive)
    step._configure_pins(p, cfg, roster, "tok", "cid")
    assert cfg["plex"]["pins"] == {"Kids": {"pin": "7"}}


def test_decline_preserves_existing_pins(monkeypatch):
    p = _Prompter(confirm=False)
    cfg = {"plex": {"pins": {"Old": {"pin": "x"}}}}
    PlexStep(logger=None)._configure_pins(p, cfg, [], "tok", "cid")
    assert cfg["plex"]["pins"] == {"Old": {"pin": "x"}}


# ── PlexStep preserves unmanaged plex.* sub-keys on reconfigure ────────────────
class _FullPrompter:
    is_interactive = True

    def __init__(self, answers):
        self.a = answers

    def section(self, *a, **k): pass
    def notice(self, *a, **k): pass
    def success(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def confirm(self, path, label, default=False): return self.a.get(path, default)
    def text(self, path, label, default="", required=False, secret=False): return self.a.get(path, default)
    def integer(self, path, label, default=0, required=False): return self.a.get(path, default)
    def secret(self, path, label, default="", required=False): return self.a.get(path, default)


def test_plexstep_preserves_unmanaged_subkeys(monkeypatch):
    # reconfiguring Plex must NOT drop capability sub-keys it doesn't manage
    # (episodes/reconcile/...) — the regression the rebuild-from-scratch had.
    monkeypatch.setattr(media.validators, "plex_ping", lambda *a, **k: {"ok": True, "version": "v"})
    monkeypatch.setattr(media.validators, "plex_account_scope",
                        lambda *a, **k: {"ok": False, "error": "scope"})
    answers = {"plex.enable": True, "plex.url": "1.2.3.4", "plex.port": 32400,
               "plex.plex_token": "tok", "plex.plex_media_path": "/data", "plex.has_pins": False}
    cfg = {"plex": {"url": "old", "port": 32400, "plex_token": "", "plex_media_path": "/d",
                    "client_identifier": "cid-1", "episodes": {"enabled": True},
                    "reconcile": {"enabled": True}}}
    PlexStep(logger=None).run(_FullPrompter(answers), cfg, {})
    plex = cfg["plex"]
    assert plex["episodes"] == {"enabled": True} and plex["reconcile"] == {"enabled": True}
    assert plex["client_identifier"] == "cid-1"        # preserved, not regenerated
    assert plex["plex_token"] == "tok" and plex["url"] == "1.2.3.4"


def test_schema_has_plex_episodes_default_off():
    from scripts.managers.factories.onboarding import schema
    assert schema.empty_config()["plex"]["episodes"] == {"enabled": False}


def test_deep_merge_adds_episodes_if_absent_keeps_user_value():
    from scripts.managers.factories.onboarding import schema
    added = schema.deep_merge(schema.empty_config(), {"plex": {"url": "x"}})
    assert added["plex"]["episodes"] == {"enabled": False}          # added when absent
    kept = schema.deep_merge(schema.empty_config(), {"plex": {"episodes": {"enabled": True}}})
    assert kept["plex"]["episodes"] == {"enabled": True}            # user value survives


def test_existing_pin_marked_in_listing(monkeypatch):
    p = _Prompter(select="")                       # just render, pick nothing
    cfg = {"plex": {"pins": {"Kids": {"pin": "set"}}}}
    _run_pins(monkeypatch, p, roster_result={"ok": True, "users": _ROSTER, "error": None}, cfg=cfg)
    assert any("Kids" in n and "*" in n for n in p.notices)   # saved-PIN marker shown


def test_blank_existing_pin_not_marked(monkeypatch):
    # a placeholder {"pin": ""} (e.g. the owner row) must NOT show the saved marker.
    p = _Prompter(select="")
    cfg = {"plex": {"pins": {"Trizzd": {"pin": ""}}}}
    _run_pins(monkeypatch, p, roster_result={"ok": True, "users": _ROSTER, "error": None}, cfg=cfg)
    trizzd_line = next(n for n in p.notices if "Trizzd" in n)
    assert "*" not in trizzd_line


# ── _configure_profile_ages (parental controls → playlist gating) ──────────────
_AGE_ROSTER = [
    {"title": "Trizzd", "is_admin": True, "restriction_profile": None},
    {"title": "Wyatt", "is_admin": False, "restriction_profile": "little_kid"},   # auto-detected
    {"title": "Aiden / Raina", "is_admin": False, "restriction_profile": None},   # operator picks
]


def test_profile_ages_auto_detect_and_operator_pick(monkeypatch):
    # Wyatt auto-defaults to little_kid (Plex); operator pins Aiden/Raina to teen.
    p = _Prompter(choices={"plex.profile_ages.Aiden / Raina": "teen"})
    cfg = {"plex": {}}
    PlexStep(logger=None)._configure_profile_ages(p, cfg, _AGE_ROSTER)
    assert cfg["plex"]["playlists"]["profile_ages"] == {
        "Wyatt": "little_kid", "Aiden / Raina": "teen"}     # owner excluded; adult not stored


def test_profile_ages_adult_stored_only_when_plex_unrestricted(monkeypatch):
    # Adult chosen for Wyatt (Plex restricts him → little_kid): NOT stored — we never override a
    # real Plex restriction and un-gate a kid. Aiden/Raina (no Plex restriction) default to adult
    # and ARE stored, so the ungated-managed-profile warning knows it's an intentional adult.
    p = _Prompter(choices={"plex.profile_ages.Wyatt": "adult"})
    cfg = {"plex": {}}
    PlexStep(logger=None)._configure_profile_ages(p, cfg, _AGE_ROSTER)
    ages = cfg["plex"].get("playlists", {}).get("profile_ages", {})
    assert "Wyatt" not in ages                        # adult-over-restriction → not persisted
    assert ages.get("Aiden / Raina") == "adult"       # explicit adult, no Plex tier → recorded


# ── _configure_playlists (the opt-in playlist toggles) ────────────────────────
class _ConfirmPrompter:
    """Per-key confirm answers (default when a key is absent), plus notice capture."""
    def __init__(self, answers): self.a = answers; self.notices = []
    def notice(self, m): self.notices.append(m)
    def confirm(self, path, label, default=False): return self.a.get(path, default)


def test_configure_playlists_writes_toggles():
    p = _ConfirmPrompter({
        "plex.has_playlist_options": True,
        "plex.playlists.writeback.enabled": True,
        "plex.playlists.recency_boost.enabled": False,
        "plex.playlists.cold_start_kids_prior": True,
    })
    cfg = {"plex": {"episodes": {"enabled": True}}}      # scans on → no notice
    PlexStep(logger=None)._configure_playlists(p, cfg)
    pl = cfg["plex"]["playlists"]
    assert pl["writeback"]["enabled"] is True
    assert pl["recency_boost"]["enabled"] is False
    assert pl["cold_start_kids_prior"] is True
    assert not any("scans" in n or "episodes" in n for n in p.notices)


def test_configure_playlists_declined_writes_nothing():
    p = _ConfirmPrompter({"plex.has_playlist_options": False})
    cfg = {"plex": {}}
    PlexStep(logger=None)._configure_playlists(p, cfg)
    assert "playlists" not in cfg["plex"]


def test_configure_playlists_notices_when_scans_off():
    p = _ConfirmPrompter({"plex.has_playlist_options": True})   # all toggles default off
    cfg = {"plex": {}}                                          # episodes/movies off
    PlexStep(logger=None)._configure_playlists(p, cfg)
    assert any("episodes.enabled" in n for n in p.notices)      # points at the prerequisite
    assert cfg["plex"]["playlists"]["writeback"]["enabled"] is False


def test_profile_ages_declined_writes_nothing():
    p = _Prompter(confirm=False)
    cfg = {"plex": {}}
    PlexStep(logger=None)._configure_profile_ages(p, cfg, _AGE_ROSTER)
    assert "playlists" not in cfg["plex"]


def test_profile_ages_no_managed_profiles_is_noop():
    p = _Prompter()
    cfg = {"plex": {}}
    PlexStep(logger=None)._configure_profile_ages(
        p, cfg, [{"title": "Owner", "is_admin": True, "restriction_profile": None}])
    assert "playlists" not in cfg["plex"]


def test_profile_ages_preserves_other_playlists_settings():
    p = _Prompter(choices={"plex.profile_ages.Aiden / Raina": "teen"})
    cfg = {"plex": {"playlists": {"personal_tilt": 60}}}
    PlexStep(logger=None)._configure_profile_ages(p, cfg, _AGE_ROSTER)
    assert cfg["plex"]["playlists"]["personal_tilt"] == 60       # untouched
    assert cfg["plex"]["playlists"]["profile_ages"]["Aiden / Raina"] == "teen"


# ── _configure_playlists: This Week in History opt-in ─────────────────────────
class _TwihPrompter:
    """confirm/integer/text/notice for the anniversary-shelf branch."""
    def __init__(self, answers, text=None):
        self.a = answers; self.t = text or {}; self.notices = []
    def notice(self, m): self.notices.append(m)
    def confirm(self, path, label, default=False): return self.a.get(path, default)
    def integer(self, path, label, default=0, required=False): return self.a.get(path, default)
    def text(self, path, label, default="", required=False, secret=False):
        return self.t.get(path, default)


_TWIH_ROSTER = [
    {"uuid": "u1", "title": "Trizzd", "is_admin": True},
    {"uuid": "u2", "title": "Wyatt", "is_admin": False},
    {"uuid": "u3", "title": "Kids", "is_admin": False},
]


def test_configure_playlists_enables_twih_with_opt_in_numbers():
    p = _TwihPrompter(
        {"plex.has_playlist_options": True,
         "plex.playlists.this_week_in_history.enabled": True,
         "plex.playlists.this_week_in_history.cap": 5},
        {"plex.playlists.this_week_in_history.opt_in_users": "2,3"})   # numbers → roster titles
    cfg = {"plex": {"episodes": {"enabled": True}}}
    PlexStep(logger=None)._configure_playlists(p, cfg, _TWIH_ROSTER)
    twih = cfg["plex"]["playlists"]["this_week_in_history"]
    assert twih["enabled"] is True and twih["cap"] == 5
    assert twih["opt_in_users"] == ["Wyatt", "Kids"]


def test_configure_playlists_twih_off_by_default_still_recorded():
    p = _TwihPrompter({"plex.has_playlist_options": True})            # TWIH confirm defaults off
    cfg = {"plex": {"episodes": {"enabled": True}}}
    PlexStep(logger=None)._configure_playlists(p, cfg, _TWIH_ROSTER)
    assert cfg["plex"]["playlists"]["this_week_in_history"]["enabled"] is False


def test_configure_playlists_twih_accepts_literal_names_without_roster():
    p = _TwihPrompter(
        {"plex.has_playlist_options": True,
         "plex.playlists.this_week_in_history.enabled": True,
         "plex.playlists.this_week_in_history.cap": 7},
        {"plex.playlists.this_week_in_history.opt_in_users": "Wyatt, Wyatt, Guest"})
    cfg = {"plex": {"episodes": {"enabled": True}}}
    PlexStep(logger=None)._configure_playlists(p, cfg, None)          # no roster → literal, deduped
    assert cfg["plex"]["playlists"]["this_week_in_history"]["opt_in_users"] == ["Wyatt", "Guest"]
