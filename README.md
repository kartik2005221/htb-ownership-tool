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
   python3 htb_ownership_tool.py              # your own overview (simple view)
   python3 htb_ownership_tool.py 1234567      # any user by HTB id
   python3 htb_ownership_tool.py someusername # or by username
   python3 htb_ownership_tool.py --full       # every field the API provides
   python3 htb_ownership_tool.py --list       # itemized checklist of active content
   python3 htb_ownership_tool.py --json       # machine-readable JSON (includes items)
   ```

### Two views

- **Simple (default):** rank, ownership bar with API cross-check, next-rank
  progress, active content totals.
- **`--full`:** everything — identity detail, formula computation
  (`numerator / denominator × 100`) with per-flag weights, retired totals,
  all-time owns, and both computed and API-provided progression fields.
- **`--list`:** itemized tables of *all* active machines and challenges with
  done/todo status for the selected user — a personal checklist of what's left:

```
ACTIVE MACHINES — 14/19 done, 5 to go
  Name                     Diff      OS        U R Status
  SmartHire                Medium    Linux    · · todo
  Pirate                   Hard      Windows  · · todo
  ...
  Cohort                   Easy      Linux    ✓ ✓ done

ACTIVE CHALLENGES — 0/198 solved, 198 to go
  Name                               Diff       Pts    Status
  Baby Frame                         Very Easy  0     todo
  ...
```

### Output styling

The report is colored (ANSI, no dependencies): rank names are colored by tier,
ownership/progress bars use block characters, and the computed ownership is
marked `✓` / `⚠` against the API's own number. Colors turn off automatically
when stdout is piped or redirected, when `NO_COLOR` is set, or via
`--no-color` — `--json` output is never styled.

### How other users' ownership is computed

The `authUserIn*Owns` flags on list endpoints always describe **the token
owner**, so for other users the script reconstructs their own-history from
their public activity feed (`GET /api/v5/user/profile/activity/{id}`, entries
`root` / `user` / `challenge`) and intersects it with the currently-active
machine/challenge sets before applying HTB's formula. The result is
cross-checked against that user's `rank_ownership` from their profile — on
every account tested so far, computed and API-reported values matched exactly.

The script only reads `HTB_TOKEN` from the environment — it never logs it,
prints it, or writes it anywhere. `.gitignore` blocks token/env files anyway.

## Example output

Dummy data, simple view (default):

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HTB CONTENT OWNERSHIP · example_user
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Rank .................. Hacker  ·  #1234 global  ·  320 points
  Ownership ............. ████████░░░░░░░░░░░░░░ 35.00%  ✓ HTB says 35.00%
      system 10 · user 12 · challenge 15 (active content)
  Next rank ............. Pro Hacker  (> 45%)
  Progress .............. █████████████░░░░░░░░░ 60.00%
  Active content ........ 20/548 machines  ·  200/850 challenges
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

`--full` view (same dummy data):

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HTB CONTENT OWNERSHIP · example_user
────────────────────────────────────────────────────────────────
  IDENTITY
  Username .............. example_user
  User id ............... 1234567
  Viewing ............... your own account
  Rank (profile) ........ Hacker
  Rank (computed) ....... Hacker
  Global ranking ........ #1234
  Points ................ 320
  CONTENT OWNERSHIP (official HTB formula)
  Ownership ............. ████████░░░░░░░░░░░░░░ 35.00%
  Reported by API ....... 35.00% ✓ HTB says 35.00%
  Computation ........... 17.5 / 50 × 100
  Active system owns .... 10  (weight ×1)
  Active user owns ...... 12  (weight ×1/2)
  Active challenge owns .. 15  (weight ×1/10)
  ACTIVE CONTENT
  Machines .............. 20 active / 548 total  (548 records fetched)
  Challenges ............ 200 active / 850 total
  Retired challenges .... 650
  ALL-TIME OWNS
  Machine user owns ..... 16
  Machine system owns ... 11
  Challenge solves ...... 42
  PROGRESSION
  Next rank (ownership) .. Pro Hacker  (threshold > 45%)
  Interpolated progress .. █████████████░░░░░░░░░ 60.00%
  API next-rank name .... Pro Hacker
  API rank progress ..... 60.00%
  API points remaining .. 25.5
  API sys owns required .. 45
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  formula & thresholds: help.hackthebox.com — 'Introduction to HTB Labs'
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
number (`rank_ownership` from the profile endpoint). On every account tested,
computed and API-reported values matched exactly, as did the interpolated
progress to the next rank versus the API's `current_rank_progress`.

## Data sources

| Endpoint | Used for |
|---|---|
| `GET /api/v4/user/info` | auth sanity check, own user id/name |
| `GET /api/v4/user/profile/basic/{id}` | rank, ranking, points, owns, next-rank fields (any user) |
| `GET /api/v5/machines?per_page=100&page=N` | full catalog + `authUserIn*Owns` flags; `state == "free"` = active |
| `GET /api/v4/challenge/list`, `/challenge/list/retired` | totals + `authUserSolve` |
| `GET /api/v4/search/fetch?query=` | username → id resolution |
| `GET /api/v5/user/profile/activity/{id}` | any user's own history for ownership reconstruction |

### Just want the official number, fast?

The API **does** report Content Ownership directly: `rank_ownership` in the
profile endpoint is HTB's own figure (the same one the rank system uses):

```bash
curl -s -A "htb-cli" -H "Authorization: Bearer $HTB_TOKEN" \
  "https://labs.hackthebox.com/api/v4/user/profile/basic/USER_ID" \
  | jq '.profile.rank_ownership'
```

What that one-liner does *not* give you is the decomposition (how many active
system/user/challenge owns it's made of) or any cross-check — that is what this
script computes and verifies against the official value.

## Performance

Typical wall time is ~5–10 s. Where it goes and what was done about it:

- The v5 machine-catalog endpoint is slow server-side (~11.5 s time-to-first-byte
  for one giant `per_page=1000` request; gzip doesn't help — it's generation,
  not transfer). The script instead fetches six `per_page=100` pages
  **concurrently**, which takes ~3–4 s of wall time.
- Challenge lists and the target user's activity feed are fetched in parallel
  with the catalog (`ThreadPoolExecutor`, stdlib).
- Run with `--timing` to see per-phase durations on stderr.

Transient failures (socket timeouts mid-read, truncated chunked responses,
connection resets, 5xx, 429) are retried automatically with backoff; only
persistent failures produce an error.

## Error handling

Failures produce specific messages, not tracebacks:

| Symptom | Meaning |
|---|---|
| HTTP 401 `Unauthenticated.` or 302 → `/login` | token invalid/expired/revoked — generate a new one |
| HTTP 403 with Cloudflare `error code: 1010` | request blocked by Cloudflare's browser-signature ban before reaching HTB (see below); also try disabling VPN/proxy exits |
| HTTP 429 | rate limited (auto-retries with backoff) |
| Timeout / connection errors | retried automatically; if persistent, the HTB API is slow or your network path is flaky |

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
