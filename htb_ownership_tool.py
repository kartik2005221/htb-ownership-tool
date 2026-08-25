#!/usr/bin/env python3
"""
htb_ownership_tool.py — Hack The Box Content Ownership overview.

Standalone, stdlib-only. Reads your App Token from the HTB_TOKEN environment
variable (never from files, never printed) and reports:

  - username, rank, global ranking, points
  - progress toward next rank
  - system owns / user owns (machines), challenge owns
  - Content Ownership % (formula documented in README)
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
    active = sum(1 for m in machines if m.get("state") == "free")

    return {
        "total": total_count if total_count is not None else len(machines),
        "fetched": len(machines),
        "user_owns": user_owns,
        "root_owns": root_owns,
        "full_owns": full_owns,
        "active": active,
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
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def build_report(profile: dict, machines: dict, challenges: dict) -> dict:
    # Content ownership: a machine counts once BOTH flags are owned;
    # a challenge counts once solved.
    content_owned = machines["full_owns"] + challenges["solved"]
    content_total = machines["total"] + challenges["total"]

    return {
        "username": profile.get("name") or profile.get("_auth_name"),
        "rank": profile.get("rank"),
        "global_ranking": profile.get("ranking"),
        "points": profile.get("points"),
        "next_rank": {
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
        "ownership_percent": {
            "overall": pct(content_owned, content_total),
            "machines": pct(machines["full_owns"], machines["total"]),
            "challenges": pct(challenges["solved"], challenges["total"]),
            "_formula": "(full machine owns + solved challenges) / (total machines + total challenges)",
            "_owned_items": content_owned,
            "_total_items": content_total,
        },
    }


def render_text(r: dict) -> str:
    def line(label, value):
        return f"  {label:<26} {value}"

    o, w, t = r["owns"], r["content_totals"], r["ownership_percent"]
    nr = r["next_rank"]
    out = []
    out.append("=" * 58)
    out.append("  HACK THE BOX — CONTENT OWNERSHIP OVERVIEW")
    out.append("=" * 58)
    out.append(line("Username", r["username"]))
    out.append(line("Rank", r["rank"] or "-"))
    out.append(line("Global ranking", "#" + str(r["global_ranking"]) if r["global_ranking"] else "-"))
    out.append(line("Points", r["points"]))
    out.append("-" * 58)
    out.append(line("Machine user owns", o["machine_user_owns"]))
    out.append(line("Machine system owns", o["machine_system_owns"]))
    out.append(line("Machine full owns", o["machine_full_owns"]))
    out.append(line("Challenge solves", o["challenge_solves"]))
    out.append("-" * 58)
    out.append(line("Machines total", w["machines_total"]))
    out.append(line("Machines active", w["machines_active"]))
    out.append(line("Challenges total", w["challenges_total"]))
    out.append(line("Challenges active", w["challenges_active"]))
    out.append("-" * 58)
    out.append(line("CONTENT OWNERSHIP", f'{t["overall"]}%'))
    out.append(line("  machine ownership", f'{t["machines"]}%'))
    out.append(line("  challenge ownership", f'{t["challenges"]}%'))
    out.append("-" * 58)
    if nr["name"]:
        out.append(line("Next rank", nr["name"]))
        if nr["progress_percent"] is not None:
            out.append(line("Progress to next rank", f'{nr["progress_percent"]}%'))
        if nr["points_remaining"] is not None:
            out.append(line("Points remaining", nr["points_remaining"]))
        if nr["system_owns_required"] is not None:
            out.append(line("System owns required", nr["system_owns_required"]))
    out.append("=" * 58)
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
