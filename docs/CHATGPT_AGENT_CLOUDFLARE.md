# ChatGPT Agent Cloudflare lessons

Documentation version: `v1.2.1-codex.0`
Observed: `17 Sep 2026` on this macOS host
Windows evidence: debug Chrome over CDP, same day
Rechecked: `24 Sep 2026` on this macOS host

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

## Historical failure chain

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
6. **Assuming a Cache API check proves Project Send.** On 18 Sep 2026 a daily clone launched as
   native Edge showed `Just a moment...` after `/json/new`, without a CDP attachment. Playwright
   clones also failed Project Send. On 24 Sep 2026 a fresh daily-profile clone passed the
   ChatGPT account check and Agent model/source discovery. A further read-only test on the same
   day reached the configured Project URL, but its challenge persisted for 20 seconds and the
   composer never appeared. Project Send is currently blocked on this host.

## Current contract

- macOS ChatGPT Edge Agent uses the same daily Edge sign-in as Cache. Probes, Recheck, source
  discovery, Project and history reads, and tasks clone that profile and disable CDP fallback.
  The login action opens the daily Edge. It does not require a second project-profile sign-in.
  A Project challenge fails closed before a task submits a prompt.
- Other macOS Edge Agent providers and Edge Jury retain their existing project debug browser
  paths. Jury never reads the daily Edge profile.
- Attach is refused while `/json/list` shows a challenge, so Playwright never enables Runtime on
  a Turnstile page.
- Windows Edge and Chrome Agent keep the project debug browser over CDP.
- Cache ChatGPT keeps cloning the daily profile.
- Do not auto-solve Cloudflare. Do not click the checkbox from this application.

## Operator recovery

For ChatGPT, choose `Open Edge to sign in` only if the daily Edge session is signed out, then
Recheck. If a challenge appears, complete it by hand in the browser; the application does not
solve it automatically. Do not restart the user-owned service on port `8666` merely to inspect
this note.
