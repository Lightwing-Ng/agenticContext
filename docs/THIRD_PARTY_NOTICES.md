# Third-Party Notices

Documentation version: `v1.3.0-codex.0`

The Browser search UI vendors Fuse.js v7.3.0 from its official distribution under the
Apache License 2.0. The minified module is stored at
`app/web/static/vendor/fuse.min.mjs`, with the corresponding license text at
`app/web/static/vendor/Fuse.js-LICENSE.txt`.

The iPhone runtime in `app/core/iphone/` is adapted from iPhone Use 0.3.10,
Copyright (c) 2026 ZHONG XIN, under the MIT License. Its full license is retained at
`app/core/iphone/LICENSE-iphone-use.txt`. Module headers and the preview JavaScript identify
the adaptation; upstream analytics, plugin packaging, and the bundled MCP Apps widget are
not included.

Explicit setup downloads Appium WebDriverAgent 16.14.0 at commit
`d17782422d55ff1e5e0ceb74eb1fd509cc0c35b6` from
[WebDriverAgent](https://github.com/appium/WebDriverAgent). Its BSD license remains in that
external checkout; WDA source, signed builds, and signing data are not bundled here.
USB forwarding and streaming use `appium-ios-device` 3.1.24 under Apache-2.0, pinned by
`app/core/iphone/tooling/package-lock.json` and installed into the private runtime directory.
Its license remains with the installed dependency. Python and other JavaScript dependencies
retain their own package licenses and notices.

`app/web/static/images/copilot.ico` is Microsoft's Copilot favicon, used to identify
the corresponding cache source. It was retrieved on 21 Oct 2026 from the signed-in
Copilot page's declared icon at
[Microsoft's static CDN](https://res.public.onecdn.static.microsoft/midgard/versionless/m365copilotresources/favicon/favicon.bab659eb.ico).
SHA-256: `f03e27de8f2a68bade6397d1107cf5b37fb76262d4eab9c4f607203825b9791b`.
