# ChatGPT Agent Cloudflare lessons

Documentation version: `v1.0.0-codex.0`
Observed: `17 Sep 2026` on this macOS host
Windows evidence: debug Chrome over CDP, same day

This is the durable lesson record. [Operations](OPERATIONS.md) and
[Computer Use Agent](COMPUTER_USE_AGENT.md) own the current contract. Do not treat an empty
project debug profile as equivalent to Cache ChatGPT.

## What actually worked

`http://localhost:8666/cache/chatgpt` succeeded because it clones the daily signed-in Edge
profile. That clone already has ChatGPT cookies and Cloudflare clearance.

`http://localhost:8666/agent/edge/chatgpt` failed while it used
`local_store/agent_browser_profile/edge`. That project debug profile was empty, launched with
`--remote-debugging-port` and `--disable-extensions`, and then had Playwright attach over CDP.
ChatGPT sent the operator to `https://auth.openai.com/api/accounts/authorize?...` and Cloudflare
Turnstile looped on `Just a moment...`.

On Windows, Agent ChatGPT can still succeed through the project debug Chrome or Edge once that
profile is signed in. A live debug Chrome was verified by attaching over CDP and reading
`/api/auth/session` from the ChatGPT tab. That diagnostic is
[`scripts/tmp_probe_chrome_tab.py`](../scripts/tmp_probe_chrome_tab.py). Pass the current
endpoint; do not reuse an old port.

## Failure chain that must not be repeated

1. **Wrong profile.** Cloning daily Edge is the Cache ChatGPT path. The project debug profile is
   a separate, initially unsigned browser. Copying or launching it does not prove ChatGPT
   authentication.
2. **Playwright `connect_over_cdp` during a challenge.** Connecting enables Runtime on every
   page, including the Turnstile document. Reading `page.content()`, `evaluate()`, or even
   attaching while `auth.openai.com` is open restarts the challenge.
3. **Navigating `chatgpt.com` while authorize is open.** A new OAuth `state` mints a new
   authorize URL and a new Cloudflare loop.
4. **`open -n` against an occupied project profile.** A second Edge fights `SingletonLock`,
   replaces the process, and starts Cloudflare again. If the profile is still occupied, do not
   launch a replacement.
5. **Clicking, reloading, or Rechecking through the challenge.** Those are punishment-path
   attempts. Detect the challenge from Chromium HTTP `/json/list` URL and title only. Leave the
   window for the operator.
6. **Treating Windows debug-browser success as a macOS Edge Agent recipe.** Windows needs the
   debug profile because the daily browser locks cookies. macOS Cache already proved the daily
   clone works. macOS Edge Agent must reuse that clone, not the empty debug profile.

## Current contract

- macOS Edge Agent probes, login, and tasks clone the daily signed-in Edge profile, matching
  Cache ChatGPT. Login opens daily Edge. Recheck reads that clone.
- Windows Edge and Chrome Agent keep the project debug browser over CDP.
- macOS Edge Jury still uses the project debug profile and never copies daily Edge identity.
- Do not auto-solve Cloudflare. Do not click the checkbox from this application.

## Operator recovery

If Cache ChatGPT already shows a signed-in account, Recheck Agent after the service has loaded
this contract. Do not complete Cloudflare in the looping project debug Edge. Leave that window
alone. Do not restart the user-owned service on port `8666` merely to inspect this note.
