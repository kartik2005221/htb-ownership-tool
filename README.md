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
   python3 htb_ownership_tool.py              # your own overview
   python3 htb_ownership_tool.py 2274028      # any user by HTB id
   python3 htb_ownership_tool.py someusername # or by username
   python3 htb_ownership_tool.py --json       # machine-readable JSON
   ```

### How other users' ownership is computed

The `authUserIn*Owns` flags on list endpoints always describe **the token
owner**, so for other users the script reconstructs their own-history from
their public activity feed (`GET /api/v5/user/profile/activity/{id}`, entries
`root` / `user` / `challenge`) and intersects it with the currently-active
machine/challenge sets before applying HTB's formula. The result is
cross-checked against that user's `rank_ownership` from their profile — on a
Pro Hacker test account both agreed exactly (45.96%).

The script only reads `HTB_TOKEN` from the environment — it never logs it,
prints it, or writes it anywhere. `.gitignore` blocks token/env files anyway.

## Example output

```
==============================================================
  HACK THE BOX — CONTENT OWNERSHIP OVERVIEW
==============================================================
  Username                       kallu752
  Rank (profile)                 Hacker
  Rank (from ownership)          Hacker
  Global ranking                 #785
  Points                         247
--------------------------------------------------------------
  Machine user owns (all time)   27
  Machine system owns (all time) 24
  Machine full owns (all time)   24
  Challenge solves (all time)    0
--------------------------------------------------------------
  Machines total / active        548 / 19
  Challenges total / active      850 / 198
--------------------------------------------------------------
  CONTENT OWNERSHIP (HTB formula) 43.48%
    reported by HTB API          43.48%
    active system owns (weight 1) 14
    active user owns (weight 1/2) 14
    active challenge owns (weight 1/10) 0
--------------------------------------------------------------
  Next rank: Pro Hacker (>45.0%) 93.92% there
  API next-rank fields           Pro Hacker, progress 93.92%, points left 19.566, sys owns req 20
==============================================================
```

## What "Content Ownership %" means

Per HTB's official documentation ([Introduction to HTB Labs — Help Center](
https://help.hackthebox.com/en/articles/5185158-introduction-to-htb-labs)),
ownership is computed over **active content only** (retired machines/challenges
contribute nothing, though an already-earned rank is never lost), weighted:

```
ownership % = (ActiveSystemOwns + ActiveUserOwns/2 + ActiveChallengeOwns/10)
              --------------------------------------------------------- × 100
              (activeMachines + activeMachines/2 + activeChallenges/10)
```

- a machine root flag ("system own") counts fully, its user flag half
- a challenge counts one tenth
- "active" = `state == "free"` in `/api/v5/machines`; membership of
  `/api/v4/challenge/list` for challenges (the `isActive` field is always false
  and useless)

Official rank thresholds: Noob ≥0%, Script Kiddie >5%, Hacker >20%, Pro Hacker
>45%, Elite Hacker >70%, Guru >90%, Omniscient 100%.

The script computes this independently and cross-checks it against HTB's own
number (`rank_ownership` from the profile endpoint). On the test account both
agree exactly (43.48%), as does the interpolated progress to next rank
(93.92% vs the API's `current_rank_progress`).

## Data sources

| Endpoint | Used for |
|---|---|
| `GET /api/v4/user/info` | auth sanity check, own user id/name |
| `GET /api/v4/user/profile/basic/{id}` | rank, ranking, points, owns, next-rank fields (any user) |
| `GET /api/v5/machines?per_page=100&page=N` | full catalog + `authUserIn*Owns` flags; `state == "free"` = active |
| `GET /api/v4/challenge/list`, `/challenge/list/retired` | totals + `authUserSolve` |
| `GET /api/v4/search/fetch?query=` | username → id resolution |
| `GET /api/v5/user/profile/activity/{id}` | any user's own history for ownership reconstruction |

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
