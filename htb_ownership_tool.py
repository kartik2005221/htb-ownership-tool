#!/usr/bin/env python3
"""
htb_ownership_tool.py — Hack The Box Content Ownership overview.

Standalone, stdlib-only. Reads your App Token from the HTB_TOKEN environment
variable (never from files, never printed) and reports:

  - username, rank, global ranking, points
  - progress toward next rank
  - system owns / user owns (machines), challenge owns
  - Content Ownership % using HTB's official formula (active content only,
    weighted: root 1, user 1/2, challenge 1/10) with rank-threshold ladder
  - active machines & challenges totals

Tested endpoints (as used by app.hackthebox.com and htb-cli):
  GET {V4}/user/info                 -> auth sanity check + user id/name (self)
  GET {V4}/user/profile/basic/{id}   -> rank/points/owns/rank progress (any user)
  GET {V5}/machines?per_page=100&page=N -> full catalog + per-user own flags
  GET {V4}/challenge/list            -> active challenges + authUserSolve
  GET {V4}/challenge/list/retired    -> retired challenges + authUserSolve
  GET {V5}/user/profile/activity/{id} -> any user's own history (root/user/challenge)

For your own account, ownership uses the authoritative authUser* flags.
For other users (by id or username), their active owns are reconstructed from
the public activity feed intersected with currently-active content, then
cross-checked against HTB's reported rank_ownership.

Auth notes (verified empirically):
  - A missing/expired token yields HTTP 302 -> https://app.hackthebox.com/login
  - HTTP 403 is NOT an auth failure: it is Cloudflare error 1010 blocking the
    client signature (e.g. Python's default User-Agent). This tool always sends
    its own User-Agent, which is enough to pass.
"""

import argparse
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_V4 = "https://labs.hackthebox.com/api/v4"
API_V5 = "https://labs.hackthebox.com/api/v5"
USER_AGENT = "htb-ownership-tool/1.0"
TOKEN_ENV = "HTB_TOKEN"


# --------------------------------------------------------------------------- #
# Terminal styling — raw ANSI codes, no dependencies.
# Auto-disabled when stdout is not a TTY (pipes/redirects/--json stay clean),
# when NO_COLOR is set, or via --no-color.
# --------------------------------------------------------------------------- #

class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[97m"
    GREY = "\033[90m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_YELLOW = "\033[93m"


USE_COLOR = False


def paint(text, *codes: str) -> str:
    if not USE_COLOR or text is None:
        return "" if text is None else str(text)
    return "".join(codes) + str(text) + Style.RESET


# One color per rank tier, aligned with RANK_LADDER order below.
RANK_TIER_COLORS = [
    Style.GREY,             # Noob
    Style.WHITE,            # Script Kiddie
    Style.GREEN,            # Hacker
    Style.CYAN,             # Pro Hacker
    Style.BLUE,             # Elite Hacker
    Style.MAGENTA,          # Guru
    Style.BRIGHT_YELLOW,    # Omniscient
]


def rank_color(rank_name) -> str:
    if not rank_name:
        return Style.GREY
    for i, (name, _) in enumerate(RANK_LADDER):
        if name.lower() == str(rank_name).lower():
            return RANK_TIER_COLORS[i]
    return Style.WHITE


def tier_color_for_pct(pct: float) -> str:
    """Color reflecting which ownership tier a percentage falls into."""
    if pct >= 100:
        return RANK_TIER_COLORS[6]
    code = RANK_TIER_COLORS[0]
    for i, (_, threshold) in enumerate(RANK_LADDER[1:], start=1):
        if pct > threshold:
            code = RANK_TIER_COLORS[i]
    return code


def bar(pct_value: float, width: int = 22, color: str = Style.CYAN) -> str:
    pct_value = max(0.0, min(100.0, float(pct_value or 0)))
    filled = int(round(width * pct_value / 100.0))
    blocks = "█" * filled + "░" * (width - filled)
    return paint(blocks, color, Style.BOLD)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class HtbToolError(Exception):
    """Fatal, user-facing error."""


class MissingTokenError(HtbToolError):
    pass


class MalformedTokenError(HtbToolError):
    pass


class NetworkError(HtbToolError):
    pass


class AuthError(HtbToolError):
    """Token rejected by HTB (HTTP 302 redirect to login)."""


class WafBlockError(HtbToolError):
    """Request blocked before reaching the API (Cloudflare 403)."""


class RateLimitError(HtbToolError):
    """HTTP 429 from the API."""


class ApiError(HtbToolError):
    """Any other non-200 API answer."""


# --------------------------------------------------------------------------- #
# Token handling — env var only, never logged
# --------------------------------------------------------------------------- #

def load_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise MissingTokenError(
            f"Environment variable {TOKEN_ENV} is not set.\n"
            f"Get your App Token at https://app.hackthebox.com/profile/settings "
            f"and run:  export {TOKEN_ENV}='...'"
        )
    if len(token.split(".")) != 3:
        raise MalformedTokenError(
            f"{TOKEN_ENV} does not look like an App Token "
            "(expected three dot-separated parts).\n"
            "Copy the token from https://app.hackthebox.com/profile/settings"
        )
    return token


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable auto-following so 302->login can be detected precisely."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def api_get_json(path: str, token: str, timeout: float = 30.0,
                max_attempts: int = 3) -> dict:
    """GET an API path (absolute URL) and return decoded JSON."""
    req = urllib.request.Request(
        path,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _opener.open(req, timeout=timeout)
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ApiError(f"{path}: response is not valid JSON ({exc})") from exc

        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            snippet = body[:200].decode("utf-8", "replace")

            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "")
                if "/login" in location:
                    raise AuthError(
                        "HTB rejected the token (redirected to login).\n"
                        "It is most likely expired or revoked. Generate a fresh "
                        "App Token at https://app.hackthebox.com/profile/settings\n"
                        f"  [endpoint: {path}]"
                    ) from exc
                raise ApiError(f"Unexpected redirect to {location} for {path}") from exc

            if exc.code in (401,):
                raise AuthError(
                    "HTB rejected the token (HTTP 401 'Unauthenticated').\n"
                    "The token is invalid, expired or revoked. Generate a fresh "
                    "App Token at https://app.hackthebox.com/profile/settings\n"
                    f"  [endpoint: {path}]"
                ) from exc

            if exc.code == 403:
                low = snippet.lower()
                if "error code" in low or "cloudflare" in low or exc.headers.get("server", "").lower() == "cloudflare":
                    raise WafBlockError(
                        "Blocked by Cloudflare before reaching the HTB API "
                        "(HTTP 403, browser-signature ban).\n"
                        "This happens when requests carry a scripted-looking "
                        "User-Agent (e.g. Python's default) or go through a "
                        "filtering proxy/VPN exit.\n"
                        f"  Response said: {snippet.strip()[:120]!r}"
                    ) from exc
                raise ApiError(f"Forbidden by API for {path}: {snippet.strip()}") from exc

            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                if attempt < max_attempts:
                    wait = float(retry_after) if retry_after else 2.0 * attempt
                    time.sleep(min(wait, 15.0))
                    last_err = exc
                    continue
                raise RateLimitError(
                    "Rate limited by the HTB API (HTTP 429). "
                    f"Retry after {retry_after or 'a while'} seconds."
                ) from exc

            if 500 <= exc.code < 600 and attempt < max_attempts:
                time.sleep(attempt)
                last_err = exc
                continue

            raise ApiError(f"HTB API returned HTTP {exc.code} for {path}: {snippet.strip()}") from exc

        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if attempt < max_attempts:
                time.sleep(attempt)
                last_err = exc
                continue
            raise NetworkError(
                f"Could not reach labs.hackthebox.com: {reason}\n"
                "Check DNS/internet, or a proxy/firewall intercepting TLS."
            ) from exc

    raise NetworkError(f"Request failed after {max_attempts} attempts: {last_err}")


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #

def resolve_user(token: str, ref: str) -> dict:
    """Accept a numeric HTB user id or an exact-ish username; return {id, name}."""
    ref = str(ref).strip()
    if ref.isdigit():
        uid = int(ref)
        basic = api_get_json(f"{API_V4}/user/profile/basic/{uid}", token)
        profile = basic.get("profile") or {}
        if not profile:
            raise ApiError(f"No HTB user found with id {uid}.")
        return {"id": uid, "name": profile.get("name")}

    results = api_get_json(f"{API_V4}/search/fetch?query={urllib.parse.quote(ref)}", token)
    users = results.get("users") or []
    if not users:
        raise ApiError(f"No HTB user matching {ref!r}.")
    exact = next((u for u in users if u.get("value", "").lower() == ref.lower()), users[0])
    return {"id": exact["id"], "name": exact.get("value")}


def fetch_profile(token: str, user_id: int | None = None) -> dict:
    if user_id is None:
        whoami = api_get_json(f"{API_V4}/user/info", token)
        info = whoami.get("info") or {}
        user_id, name = info.get("id"), info.get("name")
        if not user_id:
            raise ApiError("/user/info returned no user id — unexpected API answer.")
        source = f"{API_V4}/user/profile/basic/{user_id}"
    else:
        name, source = None, f"{API_V4}/user/profile/basic/{user_id}"

    basic = api_get_json(source, token)
    profile = basic.get("profile") or {}
    if not profile:
        raise ApiError(
            f"Profile endpoint returned nothing for user {user_id} — the account "
            "may not exist or its profile is private."
        )
    profile["_auth_name"] = name
    profile["_queried_user_id"] = user_id
    return profile


def fetch_machines(token: str) -> dict:
    """Paginate the v5 catalog; count totals and per-user owns."""
    machines = []
    page = 1
    total_pages = None
    total_count = None
    while True:
        payload = api_get_json(f"{API_V5}/machines?per_page=100&page={page}", token)
        data = payload.get("data") or []
        machines.extend(data)
        meta = payload.get("meta") or {}
        total_pages = meta.get("last_page")
        total_count = meta.get("total")
        if page >= (total_pages or 1):
            break
        page += 1

    user_owns = sum(1 for m in machines if m.get("authUserInUserOwns"))
    root_owns = sum(1 for m in machines if m.get("authUserInRootOwns"))
    full_owns = sum(1 for m in machines
                    if m.get("authUserInUserOwns") and m.get("authUserInRootOwns"))
    active = [m for m in machines if m.get("state") == "free"]

    return {
        "total": total_count if total_count is not None else len(machines),
        "fetched": len(machines),
        "user_owns": user_owns,
        "root_owns": root_owns,
        "full_owns": full_owns,
        "active": len(active),
        "active_user_owns": sum(1 for m in active if m.get("authUserInUserOwns")),
        "active_system_owns": sum(1 for m in active if m.get("authUserInRootOwns")),
        "active_ids": {m["id"] for m in active},
    }


def fetch_challenges(token: str) -> dict:
    active = (api_get_json(f"{API_V4}/challenge/list", token).get("challenges")) or []
    retired = (api_get_json(f"{API_V4}/challenge/list/retired", token).get("challenges")) or []
    all_ch = active + retired
    return {
        "total": len(all_ch),
        "active": len(active),
        "retired": len(retired),
        "solved": sum(1 for c in all_ch if c.get("authUserSolve")),
        "solved_active": sum(1 for c in active if c.get("authUserSolve")),
        "active_ids": {c["id"] for c in active},
    }


def fetch_activity_owns(token: str, user_id: int, pause: float = 0.2) -> dict:
    """Reconstruct a user's own-history from their public activity feed.

    Activity entries look like {type: 'root'|'user'|'challenge', id: <content id>}.
    Sets are de-duplicated (re-owned content after resets appears repeatedly).
    Works for ANY user, unlike the authUser* flags which describe the token
    owner only.
    """
    root_ids, user_ids, challenge_ids = set(), set(), set()
    page = 1
    while True:
        payload = api_get_json(
            f"{API_V5}/user/profile/activity/{user_id}?page={page}", token
        )
        entries = payload.get("data") or []
        for e in entries:
            etype, eid = e.get("type"), e.get("id")
            if eid is None:
                continue
            if etype == "root":
                root_ids.add(eid)
            elif etype == "user":
                user_ids.add(eid)
            elif etype == "challenge":
                challenge_ids.add(eid)

        meta = payload.get("meta") or {}
        last_page = meta.get("lastPage") or 1
        if page >= last_page:
            break
        page += 1
        time.sleep(pause)

    return {"root_ids": root_ids, "user_ids": user_ids, "challenge_ids": challenge_ids}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

# Official HTB Ownership Percentage formula (Hack The Box Help Center,
# "Introduction to HTB Labs", section "How Ranks are Achieved"):
#
#   (ActiveSystemOwns + (ActiveUserOwns / 2) + (ActiveChallengeOwns / 10))
#   / (activeMachines + (activeMachines / 2) + (activeChallenges / 10)) * 100
#
# Only ACTIVE content counts. Rank thresholds: Noob >=0%, Script Kiddie >5%,
# Hacker >20%, Pro Hacker >45%, Elite Hacker >70%, Guru >90%, Omniscient 100%.

RANK_LADDER = [
    ("Noob", 0.0),
    ("Script Kiddie", 5.0),
    ("Hacker", 20.0),
    ("Pro Hacker", 45.0),
    ("Elite Hacker", 70.0),
    ("Guru", 90.0),
    ("Omniscient", 100.0),
]


def rank_for(pct: float) -> str:
    if pct >= 100:
        return "Omniscient"
    current = "Noob"
    for name, threshold in RANK_LADDER[1:]:
        if pct > threshold:
            current = name
        else:
            break
    return current


def next_rank_info(pct: float):
    for name, threshold in RANK_LADDER:
        if pct <= threshold or (name == "Omniscient" and pct < 100 and threshold == 100):
            return {"name": name, "threshold": threshold}
    return None


def official_ownership(machines: dict, challenges: dict,
                       activity_owns: dict | None = None) -> dict:
    am = machines["active"]
    ac = challenges["active"]

    if activity_owns is not None:
        # Another user's owns, reconstructed from their public activity feed
        # intersected with the *currently active* content.
        aso = len(activity_owns["root_ids"] & machines["active_ids"])
        auo = len(activity_owns["user_ids"] & machines["active_ids"])
        aco = len(activity_owns["challenge_ids"] & challenges["active_ids"])
    else:
        aso = machines["active_system_owns"]
        auo = machines["active_user_owns"]
        aco = challenges["solved_active"]

    numerator = aso + (auo / 2) + (aco / 10)
    denominator = am + (am / 2) + (ac / 10)
    overall = round(100.0 * numerator / denominator, 2) if denominator else 0.0

    return {
        "overall": overall,
        "active_system_owns": aso,
        "active_user_owns": auo,
        "active_challenge_owns": aco,
        "numerator": round(numerator, 3),
        "denominator": round(denominator, 3),
    }


def build_report(profile: dict, machines: dict, challenges: dict,
                 activity_owns: dict | None = None) -> dict:
    own_pct = official_ownership(machines, challenges, activity_owns)
    pct_value = own_pct["overall"]

    computed_rank = rank_for(pct_value)
    nxt = next_rank_info(pct_value)
    if nxt is not None:
        progress = round(100.0 * (pct_value - _prev_threshold(computed_rank))
                         / (nxt["threshold"] - _prev_threshold(computed_rank)), 2)
    else:
        progress = 100.0

    return {
        "username": profile.get("name") or profile.get("_auth_name"),
        "user_id": profile.get("_queried_user_id"),
        "is_self": activity_owns is None,
        "rank": profile.get("rank"),
        "rank_from_ownership": computed_rank,
        "global_ranking": profile.get("ranking"),
        "points": profile.get("points"),
        "ownership_percent": {
            **own_pct,
            "reported_by_api": profile.get("rank_ownership"),
        },
        "next_rank_by_ownership": {
            **(nxt or {"name": None, "threshold": None}),
            "progress_percent": progress,
        },
        "profile_next_rank_fields": {
            "name": profile.get("next_rank"),
            "progress_percent": profile.get("current_rank_progress"),
            "points_remaining": profile.get("next_rank_points"),
            "system_owns_required": profile.get("rank_requirement"),
        },
        "owns": {
            # All-time counts come from the profile endpoint, which carries the
            # target user's own numbers for ANY user id.
            "machine_user_owns": profile.get("user_owns",
                                             machines["user_owns"]),
            "machine_system_owns": profile.get("system_owns",
                                               machines["root_owns"]),
            "challenge_solves": (len(activity_owns["challenge_ids"])
                                 if activity_owns is not None
                                 else challenges["solved"]),
        },
        "content_totals": {
            "machines_total": machines["total"],
            "machines_active": machines["active"],
            "challenges_total": challenges["total"],
            "challenges_active": challenges["active"],
            "challenges_retired": challenges["retired"],
        },
    }


def _prev_threshold(rank_name: str) -> float:
    for name, threshold in RANK_LADDER:
        if name == rank_name:
            return threshold
    return 0.0


def render_text(r: dict, full: bool = False) -> str:
    WIDTH = 64
    o, w, t = r["owns"], r["content_totals"], r["ownership_percent"]
    nb = r["next_rank_by_ownership"]
    pf = r["profile_next_rank_fields"]

    def rule(char="─", code=Style.GREY):
        return paint(char * WIDTH, code)

    def header(text):
        return "  " + paint(text, Style.DIM, Style.BRIGHT_CYAN)

    def kv(label, value):
        dots = "." * max(2, 22 - len(label))
        return f"  {paint(label, Style.WHITE)} {paint(dots, Style.GREY)} {value}"

    def sub(text):
        return "      " + paint(text, Style.GREY)

    def pct_str(v):
        return paint(f"{float(v):.2f}%", Style.BOLD)

    out = []
    who = r["username"] or "?"
    if not r.get("is_self") and r.get("user_id"):
        who += f" (id {r['user_id']})"

    banner = rule("━", Style.CYAN)
    out.append(banner)
    out.append("  " + paint("HTB CONTENT OWNERSHIP · ", Style.BOLD)
               + paint(who, Style.BOLD, Style.BRIGHT_CYAN))

    pct_v = t["overall"]
    tier = tier_color_for_pct(pct_v)
    ownership_bar = f'{bar(pct_v, color=tier)} {paint(f"{pct_v:.2f}%", Style.BOLD, tier)}'
    reported = t.get("reported_by_api")
    match_note = ""
    if reported is not None:
        ok = abs(float(reported) - float(pct_v)) < 0.01
        match_note = ("  " + paint(("✓" if ok else "⚠") + f" HTB says {float(reported):.2f}%",
                                   Style.GREEN if ok else Style.YELLOW))

    # ------------------------------------------------------------------ #
    # SIMPLE MODE (default)
    # ------------------------------------------------------------------ #
    if not full:
        rank_bits = [paint(str(r["rank"] or "-"), Style.BOLD, rank_color(r["rank"]))]
        if r["rank_from_ownership"] and r["rank_from_ownership"] != r["rank"]:
            rank_bits.append(paint(f'(ownership implies: {r["rank_from_ownership"]})',
                                   Style.YELLOW))
        out.append(kv("Rank",
                      " ".join(rank_bits)
                      + paint(f'  ·  #{r["global_ranking"] or "?"} global', Style.GREY)
                      + paint(f'  ·  {r["points"] if r["points"] is not None else "?"} points',
                              Style.GREY)))
        out.append(kv("Ownership", ownership_bar + match_note))
        out.append(sub(f'system {t["active_system_owns"]} · '
                       f'user {t["active_user_owns"]} · '
                       f'challenge {t["active_challenge_owns"]} (active content)'))
        out.append(kv("Next rank",
                      (paint(str(nb.get("name")), Style.BOLD,
                            rank_color(nb.get("name")))
                       + paint(f'  (> {nb["threshold"]:g}%)', Style.GREY))
                      if nb.get("name") else paint(str(pf.get("name") or "-"),
                                                   Style.BOLD)))
        prog = nb.get("progress_percent")
        if prog is not None:
            nxt_col = rank_color(nb.get("name"))
            out.append(kv("Progress",
                          f'{bar(prog, color=nxt_col)} '
                          + paint(f"{prog:.2f}%", Style.BOLD, nxt_col)))
        out.append(kv("Active content",
                      paint(f'{w["machines_active"]}', Style.BOLD, Style.GREEN)
                      + paint(f'/{w["machines_total"]} machines', Style.GREY)
                      + paint("  ·  ", Style.GREY)
                      + paint(f'{w["challenges_active"]}', Style.BOLD, Style.GREEN)
                      + paint(f'/{w["challenges_total"]} challenges', Style.GREY)))

    # ------------------------------------------------------------------ #
    # FULL MODE (--full): every field the API gives us
    # ------------------------------------------------------------------ #
    else:
        out.append(rule())

        out.append(header("IDENTITY"))
        out.append(kv("Username", paint(str(r["username"] or "-"), Style.BOLD)))
        out.append(kv("User id", str(r.get("user_id") or "-")))
        out.append(kv("Viewing", "your own account"
                     if r.get("is_self") else "another user's public profile"))
        out.append(kv("Rank (profile)",
                      paint(str(r["rank"] or "-"), Style.BOLD, rank_color(r["rank"]))))
        out.append(kv("Rank (computed)",
                      paint(str(r["rank_from_ownership"] or "-"), Style.BOLD,
                            rank_color(r["rank_from_ownership"]))))
        out.append(kv("Global ranking",
                      "#" + str(r["global_ranking"]) if r["global_ranking"] else "-"))
        out.append(kv("Points", str(r["points"] if r["points"] is not None else "-")))

        out.append(header("CONTENT OWNERSHIP (official HTB formula)"))
        out.append(kv("Ownership", ownership_bar))
        if reported is not None:
            out.append(kv("Reported by API",
                          pct_str(reported) + " " + match_note.strip()))
        out.append(kv("Computation",
                      paint(f'{t["numerator"]:g} / {t["denominator"]:g} × 100',
                            Style.GREY)))
        out.append(kv("Active system owns",
                      f'{t["active_system_owns"]}'
                      + paint("  (weight ×1)", Style.GREY)))
        out.append(kv("Active user owns",
                      f'{t["active_user_owns"]}'
                      + paint("  (weight ×1/2)", Style.GREY)))
        out.append(kv("Active challenge owns",
                      f'{t["active_challenge_owns"]}'
                      + paint("  (weight ×1/10)", Style.GREY)))

        out.append(header("ACTIVE CONTENT"))
        fetched = f'  ({w["fetched"]} records fetched)' if w.get("fetched") else ""
        out.append(kv("Machines",
                      f'{w["machines_active"]} active / {w["machines_total"]} total'
                      + paint(fetched, Style.GREY)))
        out.append(kv("Challenges",
                      f'{w["challenges_active"]} active / {w["challenges_total"]} total'))
        out.append(kv("Retired challenges", str(w["challenges_retired"])))

        out.append(header("ALL-TIME OWNS"))
        out.append(kv("Machine user owns", str(o["machine_user_owns"])))
        out.append(kv("Machine system owns", str(o["machine_system_owns"])))
        out.append(kv("Challenge solves", str(o["challenge_solves"])))

        out.append(header("PROGRESSION"))
        if nb.get("name"):
            nxt_col = rank_color(nb["name"])
            out.append(kv("Next rank (ownership)",
                          paint(str(nb["name"]), Style.BOLD, nxt_col)
                          + paint(f'  (threshold > {nb["threshold"]:g}%)', Style.GREY)))
            prog = nb.get("progress_percent")
            if prog is not None:
                out.append(kv("Interpolated progress",
                              f'{bar(prog, color=nxt_col)} '
                              + paint(f"{prog:.2f}%", Style.BOLD, nxt_col)))
        out.append(kv("API next-rank name",
                      paint(str(pf.get("name") or "-"), Style.BOLD,
                            rank_color(pf.get("name")))))
        if pf.get("progress_percent") is not None:
            out.append(kv("API rank progress", pct_str(pf["progress_percent"])))
        if pf.get("points_remaining") is not None:
            out.append(kv("API points remaining", str(pf["points_remaining"])))
        if pf.get("system_owns_required") is not None:
            out.append(kv("API sys owns required", str(pf["system_owns_required"])))

    out.append(banner)
    if full:
        out.append(paint("  formula & thresholds: help.hackthebox.com — 'Introduction to HTB Labs'",
                         Style.DIM))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    global USE_COLOR
    parser = argparse.ArgumentParser(
        description="Hack The Box Content Ownership overview (stdlib only). "
                    "Pass a numeric user id or username to inspect any account; "
                    "omit it to inspect your own."
    )
    parser.add_argument("user", nargs="?", default=None,
                        help="HTB user id or username (default: yourself)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON instead of text")
    parser.add_argument("--full", "-f", action="store_true",
                        help="show all available information (default: simple view)")
    parser.add_argument("--no-color", action="store_true",
                        help="disable colored output (also auto-disabled when "
                             "piped, or when NO_COLOR is set)")
    args = parser.parse_args()

    USE_COLOR = (
        not args.no_color
        and not os.environ.get("NO_COLOR")
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )
    if os.name == "nt":
        os.system("")  # best-effort: enable ANSI escapes on Windows 10+ consoles

    def step(msg):
        print(paint(f"==> {msg}", Style.DIM), file=sys.stderr)

    try:
        token = load_token()
        activity_owns = None

        if args.user is None:
            step("Authenticating...")
            profile = fetch_profile(token)
        else:
            step(f"Resolving user {args.user!r}...")
            target = resolve_user(token, args.user)
            step(f"Found {target['name']} (id {target['id']})")
            profile = fetch_profile(token, target["id"])

        step("Fetching machine catalog...")
        machines = fetch_machines(token)

        step("Fetching challenges...")
        challenges = fetch_challenges(token)

        if args.user is not None:
            step(f"Reconstructing {profile.get('name')}'s active owns "
                 "from their activity feed...")
            activity_owns = fetch_activity_owns(token, profile["_queried_user_id"])

        report = build_report(profile, machines, challenges, activity_owns)
    except HtbToolError as exc:
        print(f"\n{paint('ERROR:', Style.BOLD, Style.RED)} {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report, full=args.full))
    return 0

    if args.json:
        clean = {k: v for k, v in report.items()}
        print(json.dumps(clean, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
