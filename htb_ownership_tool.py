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
  GET {V4}/user/info                 -> auth sanity check + user id/name
  GET {V4}/user/profile/basic/{id}   -> rank/points/owns/rank progress
  GET {V5}/machines?per_page=100&page=N -> full catalog + per-user own flags
  GET {V4}/challenge/list            -> active challenges + authUserSolve
  GET {V4}/challenge/list/retired    -> retired challenges + authUserSolve

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

def fetch_profile(token: str) -> dict:
    whoami = api_get_json(f"{API_V4}/user/info", token)
    info = whoami.get("info") or {}
    user_id, name = info.get("id"), info.get("name")
    if not user_id:
        raise ApiError("/user/info returned no user id — unexpected API answer.")

    basic = api_get_json(f"{API_V4}/user/profile/basic/{user_id}", token)
    profile = basic.get("profile") or {}
    profile["_auth_name"] = name
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
    }


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


def official_ownership(machines: dict, challenges: dict) -> dict:
    am = machines["active"]
    ac = challenges["active"]
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


def build_report(profile: dict, machines: dict, challenges: dict) -> dict:
    own_pct = official_ownership(machines, challenges)
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
            "machine_user_owns": machines["user_owns"],
            "machine_system_owns": machines["root_owns"],
            "machine_full_owns": machines["full_owns"],
            "challenge_solves": challenges["solved"],
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


def render_text(r: dict) -> str:
    def line(label, value):
        return f"  {label:<30} {value}"

    o, w, t = r["owns"], r["content_totals"], r["ownership_percent"]
    nb = r["next_rank_by_ownership"]
    pf = r["profile_next_rank_fields"]
    out = []
    out.append("=" * 62)
    out.append("  HACK THE BOX — CONTENT OWNERSHIP OVERVIEW")
    out.append("=" * 62)
    out.append(line("Username", r["username"]))
    out.append(line("Rank (profile)", r["rank"] or "-"))
    out.append(line("Rank (from ownership)", r["rank_from_ownership"]))
    out.append(line("Global ranking", "#" + str(r["global_ranking"]) if r["global_ranking"] else "-"))
    out.append(line("Points", r["points"]))
    out.append("-" * 62)
    out.append(line("Machine user owns (all time)", o["machine_user_owns"]))
    out.append(line("Machine system owns (all time)", o["machine_system_owns"]))
    out.append(line("Machine full owns (all time)", o["machine_full_owns"]))
    out.append(line("Challenge solves (all time)", o["challenge_solves"]))
    out.append("-" * 62)
    out.append(line("Machines total / active", f'{w["machines_total"]} / {w["machines_active"]}'))
    out.append(line("Challenges total / active", f'{w["challenges_total"]} / {w["challenges_active"]}'))
    out.append("-" * 62)
    out.append(line("CONTENT OWNERSHIP (HTB formula)", f'{t["overall"]}%'))
    if t.get("reported_by_api") is not None:
        out.append(line("  reported by HTB API", f'{t["reported_by_api"]}%'))
    out.append(line("  active system owns (weight 1)", t["active_system_owns"]))
    out.append(line("  active user owns (weight 1/2)", t["active_user_owns"]))
    out.append(line("  active challenge owns (weight 1/10)", t["active_challenge_owns"]))
    out.append("-" * 62)
    if nb.get("name"):
        out.append(line(f'Next rank: {nb["name"]} (>{nb["threshold"]}%)',
                        f'{nb["progress_percent"]}% there'))
    if pf.get("name"):
        out.append(line("API next-rank fields", f'{pf["name"]}, '
                        f'progress {pf["progress_percent"]}%, '
                        f'points left {pf["points_remaining"]}, '
                        f'sys owns req {pf["system_owns_required"]}'))
    out.append("=" * 62)
    out.append("  Ownership formula & rank thresholds:")
    out.append("  help.hackthebox.com — 'Introduction to HTB Labs'")
    out.append("=" * 62)
    return "\n".join(str(x) for x in out)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hack The Box Content Ownership overview (stdlib only)."
    )
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON instead of text")
    args = parser.parse_args()

    try:
        token = load_token()
        print("[1/4] Authenticating...", file=sys.stderr)
        profile = fetch_profile(token)

        print("[2/4] Fetching machine catalog...", file=sys.stderr)
        machines = fetch_machines(token)

        print("[3/4] Fetching challenges...", file=sys.stderr)
        challenges = fetch_challenges(token)

        print("[4/4] Building report...", file=sys.stderr)
        report = build_report(profile, machines, challenges)
    except HtbToolError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        clean = {k: v for k, v in report.items()}
        print(json.dumps(clean, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
