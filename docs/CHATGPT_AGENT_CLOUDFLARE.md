# ChatGPT Agent Cloudflare lessons

Documentation version: `v1.1.0-codex.0`
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
6. **Treating Cache success as proof that a daily-profile clone can browse ChatGPT.** Cache reads
   `/backend-api` with cookies; it never has to pass Turnstile on a page load. On 18 Sep 2026 a
   daily clone launched as native Edge showed `Just a moment...` 3 seconds after `/json/new`,
   with nothing attached over CDP. Under the same flags, a fresh empty profile and the project
   debug profile both loaded `chatgpt.com` without a challenge. Playwright-launched clones also
   fail Project Send. Do not route macOS Edge Agent through any daily-profile clone.

## Current contract

- macOS Edge Agent matches Windows: probes, Recheck, login, and tasks all use the persistent
  project debug Edge under `local_store/agent_browser_profile/edge` over CDP, even before its
  profile is initialized. Login opens `chatgpt.com` in that window through HTTP endpoints only;
  sign in there once, then Recheck. Later tasks reattach to the same signed-in profile.
- Attach is refused while `/json/list` shows a challenge, so Playwright never enables Runtime on
  a Turnstile page.
- Windows Edge and Chrome Agent keep the project debug browser over CDP.
- Cache ChatGPT keeps cloning the daily profile; it only calls APIs.
- Do not auto-solve Cloudflare. Do not click the checkbox from this application.

## Operator recovery

Choose `Open Edge to sign in` on the Agent page, sign in to ChatGPT in the project debug Edge
window, then Recheck. If a challenge appears there, complete it by hand in that window; nothing
is attached to it at that point. Do not restart the user-owned service on port `8666` merely to inspect this note.
