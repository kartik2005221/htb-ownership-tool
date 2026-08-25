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
import http.client
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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
    """GET an API path (absolute URL) and return decoded JSON.

    Retries transient failures (5xx, timeouts mid-read, connection resets,
    truncated chunked bodies). Note: urllib only wraps connection-phase errors
    in URLError; timeouts/incomplete reads during body download surface as raw
    TimeoutError/OSError/http.client exceptions — all handled here.
    """
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

        except (OSError, http.client.HTTPException) as exc:
            # TimeoutError (socket read timeout), ConnectionResetError,
            # IncompleteRead/chunked-transfer glitches, DNS failures.
            # HTTPError was already handled above; everything left here is
            # transport-level and usually transient -> retry with backoff.
            last_err = exc
            if attempt < max_attempts:
                time.sleep(min(1.5 * attempt, 5.0))
                continue

    kind = "timed out reading response" if isinstance(last_err, TimeoutError) \
        else "connection failed"
    raise NetworkError(
        f"Request {kind} after {max_attempts} attempts: {last_err}\n"
        f"  [last endpoint: {path}]\n"
        "The HTB API can be slow at peak times; large activity feeds are the "
        "most common trigger. Try again, or check your network."
    )


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #

def resolve_user(token: str, ref: str) -> dict:
    """Accept a numeric HTB user id or an exact-ish username; return {id, name}.

    Numeric ids are validated implicitly by the profile fetch that follows —
    no extra API call here.
    """
    ref = str(ref).strip()
    if ref.isdigit():
        return {"id": int(ref), "name": None}

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


def fetch_all_pages(token: str, url_template: str,
                    max_workers: int = 6) -> list:
    """Fetch a paginated endpoint, page 1 first, then the rest concurrently.

    url_template must contain '{page}' (e.g. ".../machines?per_page=100&page={page}").
    Handles both 'last_page' (Laravel-style v4/v5 machines meta) and 'lastPage'
    (v5 activity meta). The HTB API tolerates parallel reads well; measured
    wall time for the full machine catalog drops from ~12s (one giant request)
    to ~4s (six small ones in flight at once).
    """
    first = api_get_json(url_template.format(page=1), token)
    pages = [first]
    meta = first.get("meta") or {}
    last_page = meta.get("last_page") or meta.get("lastPage") or 1
    if last_page > 1:
        with ThreadPoolExecutor(max_workers=min(last_page - 1, max_workers)) as pool:
            futures = [pool.submit(api_get_json, url_template.format(page=p), token)
                       for p in range(2, last_page + 1)]
            pages.extend(f.result() for f in futures)
    return pages


def fetch_machines(token: str) -> dict:
    """Fetch the v5 catalog; count totals and per-user owns."""
    machines = []
    total_count = None
    for payload in fetch_all_pages(
        token, f"{API_V5}/machines?per_page=100&page={{page}}"
    ):
        machines.extend(payload.get("data") or [])
        meta = payload.get("meta") or {}
        if meta.get("total") is not None:
            total_count = meta["total"]

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
        "active_records": [
            {"id": m["id"], "name": m.get("name") or str(m["id"]),
             "difficulty": m.get("difficultyText") or "?",
             "os": m.get("os") or "?"}
            for m in active
        ],
        # Only meaningful for the token owner (self mode):
        "active_user_ids": {m["id"] for m in active if m.get("authUserInUserOwns")},
        "active_root_ids": {m["id"] for m in active if m.get("authUserInRootOwns")},
    }


def fetch_challenges(token: str) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(api_get_json, f"{API_V4}/challenge/list", token)
        fr = pool.submit(api_get_json, f"{API_V4}/challenge/list/retired", token)
        active = (fa.result().get("challenges")) or []
        retired = (fr.result().get("challenges")) or []
    all_ch = active + retired
    return {
        "total": len(all_ch),
        "active": len(active),
        "retired": len(retired),
        "solved": sum(1 for c in all_ch if c.get("authUserSolve")),
        "solved_active": sum(1 for c in active if c.get("authUserSolve")),
        "active_ids": {c["id"] for c in active},
        "active_records": [
            {"id": c["id"], "name": c.get("name") or str(c["id"]),
             "difficulty": c.get("difficulty") or "?",
             "points": c.get("points") if c.get("points") is not None else "?"}
            for c in active
        ],
        # Only meaningful for the token owner (self mode):
        "solved_active_ids": {c["id"] for c in active if c.get("authUserSolve")},
    }


def fetch_activity_owns(token: str, user_id: int) -> dict:
    """Reconstruct a user's own-history from their public activity feed.

    Activity entries look like {type: 'root'|'user'|'challenge', id: <content id>}.
    Sets are de-duplicated (re-owned content after resets appears repeatedly).
    Works for ANY user, unlike the authUser* flags which describe the token
    owner only.
    """
    root_ids, user_ids, challenge_ids = set(), set(), set()
    pages = fetch_all_pages(
        token,
        f"{API_V5}/user/profile/activity/{user_id}?per_page=100&page={{page}}",
    )
    for payload in pages:
        for e in payload.get("data") or []:
            etype, eid = e.get("type"), e.get("id")
            if eid is None:
                continue
            if etype == "root":
                root_ids.add(eid)
            elif etype == "user":
                user_ids.add(eid)
            elif etype == "challenge":
                challenge_ids.add(eid)

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


DIFFICULTY_ORDER = {"very easy": 0, "easy": 1, "medium": 2, "hard": 3, "insane": 4}


def _diff_key(s):
    return DIFFICULTY_ORDER.get(str(s).lower(), 9)


def build_items(machines: dict, challenges: dict,
                activity_owns: dict | None = None) -> dict:
    """Per-item done/todo state for all ACTIVE content.

    Self mode uses the authoritative authUser* flags; other-user mode
    intersects their activity-derived id sets with the active content.
    """
    if activity_owns is None:
        m_user_ids = machines["active_user_ids"]
        m_root_ids = machines["active_root_ids"]
        ch_solved_ids = challenges["solved_active_ids"]
    else:
        m_user_ids = activity_owns["user_ids"] & machines["active_ids"]
        m_root_ids = activity_owns["root_ids"] & machines["active_ids"]
        ch_solved_ids = activity_owns["challenge_ids"] & challenges["active_ids"]

    machine_rows = []
    for rec in sorted(machines["active_records"], key=lambda x: x["name"].lower()):
        user, root = rec["id"] in m_user_ids, rec["id"] in m_root_ids
        status = ("done" if user and root else
                  "user only" if user else
                  "root only" if root else "todo")
        machine_rows.append({"name": rec["name"], "difficulty": rec["difficulty"],
                             "os": rec["os"], "user": user, "root": root,
                             "status": status})
    machine_rows.sort(key=lambda r: (r["status"] != "todo", _diff_key(r["difficulty"]),
                                     r["name"].lower()))

    challenge_rows = []
    for rec in sorted(challenges["active_records"], key=lambda x: x["name"].lower()):
        solved = rec["id"] in ch_solved_ids
        challenge_rows.append({"name": rec["name"], "difficulty": rec["difficulty"],
                               "points": rec["points"], "solved": solved})
    challenge_rows.sort(key=lambda r: (not r["solved"], _diff_key(r["difficulty"]),
                                       r["name"].lower()))

    return {
        "machines": machine_rows,
        "challenges": challenge_rows,
        "summary": {
            "machines_done": sum(1 for r in machine_rows if r["status"] == "done"),
            "machines_total": len(machine_rows),
            "challenges_solved": sum(1 for r in challenge_rows if r["solved"]),
            "challenges_total": len(challenge_rows),
        },
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
        "items": build_items(machines, challenges, activity_owns),
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

def render_items(r: dict) -> str:
    """Itemized checklist of all active machines/challenges (--list)."""
    items = r["items"]
    out = []

    def tcell(text, width, *codes):
        text = str(text)
        if len(text) > width:
            text = text[:width - 1] + "…"
        return paint(text.ljust(width), *codes)

    def mark(flag):
        return paint("✓", Style.GREEN, Style.BOLD) if flag else paint("·", Style.GREY)

    def status_cell(status):
        color = {"done": Style.GREEN, "todo": Style.GREY}.get(status, Style.CYAN)
        return paint(status, color)

    # ---------------- machines ----------------
    mrows = items["machines"]
    ms = items["summary"]
    nw, dw, ow = 24, 9, 9
    out.append(paint(f'ACTIVE MACHINES — {ms["machines_done"]}/{ms["machines_total"]} done'
                     f', {ms["machines_total"] - ms["machines_done"]} to go',
                     Style.BOLD))
    out.append("  " + paint("Name".ljust(nw) + " ".ljust(1) + "Diff".ljust(dw + 1)
                             + "OS".ljust(ow + 1) + "U R Status", Style.DIM))
    for row in mrows:
        scolor = {"done": Style.GREEN, "todo": Style.GREY}.get(row["status"], Style.CYAN)
        out.append("  " + tcell(row["name"], nw, Style.BOLD)
                   + " " + tcell(row["difficulty"], dw)
                   + " " + tcell(row["os"], ow)
                   + mark(row["user"]) + " "
                   + mark(row["root"]) + " "
                   + paint(str(row["status"]).ljust(9), scolor))

    # ---------------- challenges ----------------
    crows = items["challenges"]
    cs = items["summary"]
    cnw, cdw, cpw = 34, 10, 6
    out.append("")
    out.append(paint(f'ACTIVE CHALLENGES — {cs["challenges_solved"]}/{cs["challenges_total"]} solved'
                     f', {cs["challenges_total"] - cs["challenges_solved"]} to go',
                     Style.BOLD))
    out.append("  " + paint("Name".ljust(cnw) + " ".ljust(1) + "Diff".ljust(cdw + 1)
                             + "Pts".ljust(cpw + 1) + "Status", Style.DIM))
    for row in crows:
        out.append("  " + tcell(row["name"], cnw, Style.BOLD)
                   + " " + tcell(row["difficulty"], cdw)
                   + " " + tcell(row["points"], cpw)
                   + (paint("solved".ljust(6), Style.GREEN)
                      if row["solved"] else paint("todo".ljust(6), Style.GREY)))

    return "\n".join(out)


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
    parser.add_argument("--list", action="store_true",
                        help="itemize every active machine/challenge with its "
                             "done/todo status for the selected user")
    parser.add_argument("--timing", action="store_true",
                        help="print per-phase durations to stderr")
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

    t0 = time.perf_counter()
    phase_start = t0

    def tick(label):
        nonlocal phase_start
        if args.timing:
            now = time.perf_counter()
            print(paint(f"    [{label}] {now - phase_start:.2f}s",
                        Style.GREY), file=sys.stderr)
            phase_start = now

    try:
        token = load_token()

        if args.user is None:
            step("Authenticating...")
            profile = fetch_profile(token)
            target_id = None
        else:
            step(f"Resolving user {args.user!r}...")
            target = resolve_user(token, args.user)
            profile = fetch_profile(token, target["id"])
            target_id = target["id"]
        step(f"Found {profile.get('name')} (id {profile.get('_queried_user_id')})")
        tick("identity")

        # Machines, challenges and the activity feed are independent of each
        # other -> fetch them concurrently.
        step("Fetching machine catalog, challenges"
             + (", activity feed..." if target_id is not None else "..."))
        with ThreadPoolExecutor(max_workers=3) as pool:
            fm = pool.submit(fetch_machines, token)
            fc = pool.submit(fetch_challenges, token)
            fa = (pool.submit(fetch_activity_owns, token, profile["_queried_user_id"])
                  if target_id is not None else None)
            machines = fm.result()
            challenges = fc.result()
            activity_owns = fa.result() if fa is not None else None
        tick("content + activity")

        report = build_report(profile, machines, challenges, activity_owns)
        tick("report")
    except HtbToolError as exc:
        print(f"\n{paint('ERROR:', Style.BOLD, Style.RED)} {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report, full=args.full))
        if args.list:
            print()
            print(render_items(report))
    return 0

    if args.json:
        clean = {k: v for k, v in report.items()}
        print(json.dumps(clean, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
