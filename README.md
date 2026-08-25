# htb-ownership-tool

A single-file, **stdlib-only** Python script that gives a full Hack The Box
**Content Ownership** overview: rank, points, global ranking, machine/challenge
owns, ownership percentages, active content totals, and progress to next rank.

No pip installs. No config files. Token comes from an environment variable.

## Setup

1. Copy your **App Token** from <https://app.hackthebox.com/profile/settings>
   (it looks like three dot-separated parts).
2. Export it in your shell:

   ```bash
   export HTB_TOKEN='...'   # your token; never committed, never printed
   ```

3. Run:

   ```bash
   python3 htb_ownership_tool.py          # human-readable table
   python3 htb_ownership_tool.py --json   # machine-readable JSON
   ```

The script only reads `HTB_TOKEN` from the environment — it never logs it,
prints it, or writes it anywhere. `.gitignore` blocks token/env files anyway.

## Example output

```
==========================================================
  HACK THE BOX — CONTENT OWNERSHIP OVERVIEW
==========================================================
  Username                   kallu752
  Rank                       Hacker
  Global ranking             #785
  Points                     247
----------------------------------------------------------
  Machine user owns          27
  Machine system owns        24
  Machine full owns          24
  Challenge solves           0
----------------------------------------------------------
  Machines total             548
  Machines active            19
  Challenges total           850
  Challenges active          198
----------------------------------------------------------
  CONTENT OWNERSHIP          1.72%
    machine ownership        4.38%
    challenge ownership      0.0%
----------------------------------------------------------
  Next rank                  Pro Hacker
  Progress to next rank      93.92%
  Points remaining           19.566
  System owns required       20
==========================================================
```

## What "Content Ownership %" means here

HTB does not publish this number through the public API, so the script computes
it transparently:

```
ownership % = (fully-owned machines + solved challenges)
              ------------------------------------------ × 100
              (total machines + total challenges)
```

A machine counts as owned when **both** user and root flags are yours; a
challenge counts once solved. Per-category percentages are shown separately.
Sherlocks/Fortresses/Prolabs are out of scope for now but easy to add — the API
returns per-user flags on their list endpoints too.

## Data sources

| Endpoint | Used for |
|---|---|
| `GET /api/v4/user/info` | auth sanity check, user id/name |
| `GET /api/v4/user/profile/basic/{id}` | rank, ranking, points, owns, next-rank fields |
| `GET /api/v5/machines?per_page=100&page=N` | full catalog + `authUserIn*Owns` flags; `state == "free"` = active |
| `GET /api/v4/challenge/list`, `/challenge/list/retired` | totals + `authUserSolve` |

## Error handling

Failures produce specific messages, not tracebacks:

| Symptom | Meaning |
|---|---|
| HTTP 401 `Unauthenticated.` or 302 → `/login` | token invalid/expired/revoked — generate a new one |
| HTTP 403 with Cloudflare `error code: 1010` | request blocked by Cloudflare's browser-signature ban before reaching HTB (see below); also try disabling VPN/proxy exits |
| HTTP 429 | rate limited (auto-retries with backoff) |
| URLError | DNS/network/TLS problem reaching `labs.hackthebox.com` |

## The original 403, explained (with evidence)

My first attempt was:

```
GET https://labs.hackthebox.com/api/v4/user/profile/basic/{id}
Authorization: Bearer <token>
```
→ **HTTP 403**. Meanwhile `htb-cli info` worked against the *same endpoint*.

### Root cause: it was never an auth problem

The 403 response body is literally `error code: 1010` with `server: cloudflare`.
Cloudflare error **1010** is a *browser-signature ban*: the WAF blocks requests
whose client fingerprint looks like a script — most notoriously Python's default
`User-Agent` (`Python-urllib/3.x`). The request never reached HTB's API at all.

### Evidence (controlled experiments against labs.hackthebox.com)

| Request variant | Result |
|---|---|
| No auth header / garbage bearer | **302** → `https://app.hackthebox.com/login` (how HTB reports bad tokens) |
| Valid token + UA `Python-urllib/3.x` | **403** `error code: 1010` — blocked even *with* valid auth |
| Valid token + UA `python-requests/x` | **404** Cloudflare interstitial page |
| Valid token + UA `curl/*`, `htb-cli`, or any custom string | **200** |

So: wrong endpoint? No — same one htb-cli uses (`reference-only/cmd/info.go:151`).
Wrong header scheme? No — plain `Authorization: Bearer` is correct
(`reference-only/lib/utils/utils.go:411`). Wrong token type? No — an expired or
bad token yields 401/302, never 403.

**The only meaningful difference** was htb-cli sending its own
`User-Agent: htb-cli` (utils.go:410). Any non-Python-default User-Agent passes;
this tool sends `htb-ownership-tool/1.0`.

### Notes on htb-cli (observations only, no action taken)

- It sets an explicit `Host:` header on GETs (utils.go:417) — redundant, Go
  derives it from the URL; harmless.
- Its transport uses `InsecureSkipVerify: true` (utils.go:420-424), which
  disables TLS certificate verification for every request — a real security
  smell if you're ever on hostile networks, worth knowing about even though we
  didn't change it.
