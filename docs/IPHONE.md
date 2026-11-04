# iPhone control

Documentation version: `v1.0.4-codex.0`

The Agent sidebar has three modes: Agentic, Jurors, and iPhone. Open
`http://localhost:8666/iphone` on the Mac connected to your phone. The page provides
setup, a live preview, manual controls, and an explicit ChatGPT Tunnel access checkbox.
The implementation adapts iPhone Use 0.3.10 and its WebDriverAgent (WDA) runtime.

## Requirements

- macOS and the project's Python 3.13 or newer environment.
- Full Xcode, including its initial component installation and an Apple signing account.
  Command Line Tools alone cannot build and deploy WDA. Choose an Xcode release compatible
  with both this Mac and the phone's iOS version using Apple's
  [Xcode requirements](https://developer.apple.com/xcode/system-requirements).
- Git, npm 10 or newer, and a supported Node.js release: 20.x starting at 20.19,
  22.x starting at 22.12, or 24 and newer. Node.js 21 and 23 are not supported.
  The Mac check reports missing or unsupported prerequisites.
- A real iPhone connected by USB. Unlock it, trust this Mac, and enable Developer Mode when
  requested. Account sign-in, device trust, passcodes, and Face ID stay in Apple's system UI.

Windows still runs the other application features. Its iPhone page explains the macOS
requirement and never imports the macOS runtime or accesses a device.

## First connection

1. Open Xcode once, finish its setup, and add your Apple signing account. If the Mac check
   still reports Command Line Tools, open `Xcode > Settings > Locations` and select the
   installed Xcode in `Command Line Tools`, then check again. This also applies when the
   installed app is named `Xcode-beta.app`. Apple's
   [command-line tools settings guide](https://developer.apple.com/documentation/xcode/configuring-command-line-tools-settings)
   also describes the equivalent `xcode-select` command.
2. Open Agent > iPhone. Under **​Check this Mac​**, choose **​Run check​** and resolve each
   reported prerequisite.
3. Under **​Find iPhone​**, choose **​Search​**, then select the connected phone. Complete its
   Trust and Developer Mode prompts yourself and keep it awake. Follow Apple's
   [Developer Mode instructions](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)
   if that setting is not yet visible.
4. In **​Signing​**, enter your 10-character Team ID and a unique bundle identifier such as
   `com.example.myphone.wda`, then choose **​Save​**. Use your own identifier, not this example.
5. Choose **​Download​**, then **​Build​**. Each job displays its current state and diagnostic log.
   The source is pinned to WebDriverAgent 16.14.0; download/build are explicit actions.
6. Choose **​Start​**. Allow the existing job to finish startup rather than launching repeated
   jobs. The first start installs the pinned USB transport dependencies into the private
   runtime directory. Complete any signing or developer-trust prompt on the phone.
7. Choose **​Connect​**. Only a successful full connection check establishes that phone actions
   are usable; a visible preview alone is insufficient evidence.

The project does not require a separate Appium Server. Reuse a healthy existing WDA service
and build on subsequent visits. **​Stop​** stops only processes owned by this runtime; it does
not uninstall apps. WDA stays running across a normal web application reload, while the
web application's owned preview connection closes.

The USB transport pins `appium-ios-device` 3.1.24 and overrides its transitive `shell-quote`
dependency to 1.11.0, the patched release for
[GHSA-pqg4-j6r4-53mv](https://github.com/advisories/GHSA-pqg4-j6r4-53mv). The next explicit
**​Start​** refreshes a previously installed transport when its saved lock-file hash differs
from the bundled lock file.

## Manual use

- Click the live screen to tap; drag to swipe. Rotation temporarily disables screen input
  until the preview and phone coordinate system agree.
- Focus a field on the phone, enter text in **​Type text​**, and choose **​Type​**. Shift+Return adds a newline;
  **​Press Return after typing​** explicitly requests the phone's Return action.
  A draft edited while an earlier send is pending is retained.
- Use the toolbar to refresh, go Home, capture a screenshot, or pause/resume the preview.
  Screenshot capture uses the clipboard where available and falls back to a file download.
- In **​Launch app​**, enter an app name and choose **​Open​** to find matches, then choose
  **​Open​** beside the intended app. The **​installed​** label appears only when installation
  was verified. A known bundle identifier can be entered and opened directly.
- Before entering a password or completing Face ID, pause the preview, finish on the phone,
  and resume explicitly. An automatic lock pause can clear after a successful unlock check.

## ChatGPT Tunnel

1. Complete the phone connection above.
2. On the iPhone page, enable **​Allow iPhone access through ChatGPT Tunnel​**. This is a
   separate, local-only grant. It is off by default and resets after an application restart.
   Enabling it does not build, start, connect, or operate the phone.
3. Choose **​Open ChatGPT Tunnel​** and configure the connection at `/agent/tunnel/chatgpt`
   as described in [Operations](OPERATIONS.md). Refresh or reconnect the ChatGPT app so its
   tool catalog includes the `pua_` phone tools.
   Existing project tools and their file permissions remain unchanged. Phone control is a
   host capability and does not require selecting a writable project.
4. Ask ChatGPT to verify `pua_ready` with `recover=false`, then perform a concrete phone task,
   for example: "Check the iPhone connection, open Settings, and tell me which page is visible."
   Screenshots are returned as native MCP images, without local filesystem paths.
5. Clear **​Allow iPhone access through ChatGPT Tunnel​** to reject new phone calls.
   A previously admitted operation may finish.

The grant exposes 15 phone tools for readiness, observation, finding, tapping, swiping,
typing, buttons, app discovery/launch, batching, scrolling, collection, metrics, and preview
pause/resume. It does not expose local setup, signing, installation, start, or stop tools.
Runtime recovery remains local: if the phone channel needs a restart, use the iPhone page.
Gemini Tunnel tools and the browser Agent action protocol do not gain phone access.

The existing Tunnel authentication and provider-admission checks still apply. Phone actions
are serialized with the manual page and standalone MCP client. Tunnel activity records the
tool name and outcome, not entered text, observed phone content, or image data. The migration
does not include upstream analytics.

## Standalone MCP clients

Use the absolute path to `scripts/iphone_mcp.sh` as the client's stdio server command.
It launches the Python server from the repository root and keeps stdout reserved for JSON-RPC.
No Codex plugin installation or upstream screen widget is required.

For this checkout, the command is:

```text
/Users/lightwing/Desktop/agenticContext/scripts/iphone_mcp.sh
```

The standalone server exposes all 17 model tools, including `pua_doctor` and `pua_setup`.
Its access is independent of the ChatGPT Tunnel checkbox. It shares the default private state
directory, signed build, WDA session, and operation lock with the web page.
Optional arguments are `--state-dir /absolute/private/directory` and `--url http://127.0.0.1:18100`.
An override must match the page's configuration if both clients should control the same phone.

## Private state and troubleshooting

State defaults to `~/.local/share/iphone-use`. The legacy
`~/.local/share/iphone-use-wda` directory is reused when it contains `config.json` and
the new default is absent.
`IPHONE_USE_STATE_DIR`, followed by legacy `WDA_STATE_DIR`, can override it; an explicit
server argument takes precedence. State directories inside Git checkouts are refused.
Source, dependency installations, signing configuration, screenshots, and job logs stay
outside the project. Do not remove this directory to repair an ordinary connection failure.

- **​Full Xcode missing:​** install and initialize Xcode, select its developer directory, and
  rerun the Mac check. Installing this migration does not replace Xcode.
- **​No phone found:​** check USB, unlock/trust, and Developer Mode before rediscovering.
- **​Signing/build failure:​** read the displayed build log; verify the account, Team ID,
  bundle identifier, and Xcode/iOS compatibility. Resolve signing in Xcode when necessary.
- **​Already busy:​** wait for the existing job or phone operation; do not replay an uncertain
  tap, text entry, submission, or batch.
- **​Empty accessibility tree:​** quit iPhone Mirroring, unlock the phone, and reconnect.
- **​Phone tools missing in ChatGPT:​** check **​Allow iPhone access through ChatGPT Tunnel​**
  after an application restart, then refresh or reconnect the ChatGPT app. Clearing the
  checkbox rejects previously cached phone tools too.
- **​Local-only notice:​** open the page on the connected Mac. LAN clients cannot view phone
  frames, change the Tunnel grant, or issue phone commands through the page API.

Offline tests use fake WDA servers, isolated runtime directories, and mocked browser routes.
Passing them does not establish Apple signing, USB pairing, or real-device readiness. See
[Testing](TESTING.md) for the verification commands and
[Third-party notices](THIRD_PARTY_NOTICES.md) for source attribution.
