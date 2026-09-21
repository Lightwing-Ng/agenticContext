# Gemini Tunnel setup

Documentation version: `v1.3.2-codex.0`

Last verified against the linked provider documentation: 21 Sep 2026.

## What this connection is

AgenticContext gives Gemini the same ten project-scoped MCP tools used by the ChatGPT
connection, but it does not send Gemini through OpenAI Secure MCP Tunnel. Gemini Custom Apps
connect to a public HTTPS Streamable HTTP endpoint. AgenticContext therefore keeps the two
transports separate:

```text
ChatGPT -> OpenAI Secure MCP Tunnel -> loopback /mcp
Gemini  -> dedicated public HTTPS hostname -> OAuth 2.1 -> /mcp/gemini
                                              |
                                              v
                                the same ten-tool MCP service
```

The Gemini hostname exposes only OAuth discovery, authorization, token exchange, and
`/mcp/gemini`. The local console, static assets, settings APIs, and ChatGPT `/mcp` route remain
unavailable on that public Host. Do not publish port 8666 directly, bind the application to a
public interface, or configure Gemini with No authentication.

Google's consumer Gemini Custom Apps help currently lists these prerequisites: the user is at
least 18 years old, is in the United States, uses a personal Google Account rather than a work or
school account, has Keep Activity enabled, uses English, and connects from Gemini on the web or
mobile app. Google may change those eligibility rules. Gemini asks for confirmation before a
connected app performs a write action. See [Google's Custom Apps help](https://support.google.com/gemini/answer/17209137?co=GENIE.Platform%3DDesktop&hl=en).

## 1. Create a stable HTTPS hostname

A stable named reverse tunnel is recommended. The example below uses Cloudflare Tunnel. Any other
reverse proxy must provide a publicly trusted TLS certificate, set the origin Host to the exact
configured public hostname, forward only the six documented OAuth/MCP paths, and send all other
paths to a proxy-owned 404 without contacting port 8666. Host preservation alone is not a
substitute for the proxy path allowlist.

Install `cloudflared` by following [Cloudflare's Tunnel setup guide](https://developers.cloudflare.com/tunnel/get-started/).
AgenticContext does not install, launch, stop, or update `cloudflared` for you.

Authenticate, create a named tunnel, and assign a dedicated DNS name:

```bash
cloudflared tunnel login
cloudflared tunnel create agenticcontext-gemini
cloudflared tunnel route dns agenticcontext-gemini agenticcontext.example.com
```

Create a Cloudflare configuration file using the tunnel UUID and the credentials file printed by
the create command:

```yaml
tunnel: 00000000-0000-0000-0000-000000000000
credentials-file: /absolute/path/to/00000000-0000-0000-0000-000000000000.json

ingress:
  - hostname: agenticcontext.example.com
    path: '^/(\.well-known/oauth-authorization-server|\.well-known/oauth-protected-resource(/mcp/gemini)?|oauth/(authorize|token)|mcp/gemini)$'
    service: http://127.0.0.1:8666
    originRequest:
      httpHostHeader: agenticcontext.example.com
  - hostname: agenticcontext.example.com
    service: http_status:404
  - service: http_status:404
```

Validate the rule order before starting the tunnel, then confirm a public control-plane path is
handled by the proxy-owned 404 rule:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://agenticcontext.example.com/mcp/gemini
cloudflared tunnel ingress rule https://agenticcontext.example.com/api/agent/tunnel/gemini/copy-value
```

Start it from the host's normal Terminal or PowerShell:

```bash
cloudflared tunnel run agenticcontext-gemini
```

Cloudflare documents the [named-tunnel workflow](https://developers.cloudflare.com/tunnel/get-started/)
and the [ordered hostname/path rules and required catch-all](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/).
Keep the Cloudflare tunnel credentials owner-only and outside the repository.

## 2. Configure AgenticContext

1. Start AgenticContext normally and open `http://127.0.0.1:8666/agent/tunnel/gemini`.
2. In the Gemini panel's Step 1, enter only the public origin, for example
   `https://agenticcontext.example.com`. Do not add `/mcp/gemini`, a query string, embedded
   credentials, or a trailing path.
3. Save the origin. AgenticContext creates an independent Gemini OAuth client ID, client secret,
   and signing key in `gemini-tunnel-credentials.json` beside `settings.json`. The file is written
   atomically and is required to use mode `0600` on POSIX. On Windows, it remains in the current
   user's settings directory; verify that directory's ACL grants access only to the intended
   account before exposing the public hostname.
4. Use the three explicit copy buttons to copy the MCP server URL, client ID, and client secret.
   The secret is returned only for that authenticated local click. It is not placed in HTML,
   initial page JSON, browser storage, the URL, status responses, or logs. Copying the secret does
   not approve an OAuth callback.

Generating the client credentials and entering them into one chosen Gemini Custom App is the
operator's explicit pre-authorization of that static confidential client. AgenticContext does not
offer Dynamic Client Registration. Its authorization-code flow still requires the generated
client secret, exact resource and scope binding, an exact per-code redirect URI match, and PKCE
S256 before it issues a bearer token. The first complete valid authorization request is held only
in process memory for 10 minutes while the authenticated local Agent UI displays its exact callback
URI. The operator must approve or deny that exact request. Approval is single-use and bound to the
complete client, resource, scope, PKCE challenge, redirect URI, and state values. AgenticContext
also binds the local decision to an opaque per-request review identity, so an expired or replaced
request cannot be approved from a stale UI. It does not assume an undocumented consumer callback
hostname. After the first approved code, its complete redirect URI is pinned for the life of the
process.

Saving the origin means the local OAuth/MCP gateway is configured; it does not prove that the
reverse tunnel is running or that Gemini has connected. The sidebar changes to Active only after
an authenticated Gemini tool call reaches this process.

## 3. Add the Custom App in Gemini

1. Open [Gemini Connected Apps](https://gemini.google.com/apps) in the eligible personal account.
2. Choose **Settings → Connected Apps → Custom apps → Add a custom app**.
3. Name the app `AgenticContext` and paste the copied MCP server URL.
4. Because AgenticContext intentionally does not offer Dynamic Client Registration, open
   **Advanced features** or **Show more**, then paste the copied OAuth client ID and client secret.
5. Continue the OAuth connection. AgenticContext's public authorization page waits while the exact
   callback is reviewed; it does not redirect automatically.
6. Return to the local Agent → Tunnel → Gemini panel. Read the complete pending callback URI and
   requested host. Choose `Approve callback` only if they match the connection you just started;
   otherwise choose `Deny`. The waiting page refreshes and continues only when the same complete
   request consumes that one approval.
7. Inspect the discovered tool list before accepting it.

The expected catalog contains exactly:

- `project_overview`
- `list_files`
- `search_files`
- `read_files`
- `apply_edits`
- `write_file`
- `delete_file`
- `run_check`
- `show_changes`
- `review_changes`

Google's enterprise custom MCP documentation separately requires a public HTTPS Streamable HTTP
server and documents OAuth support. Consumer Custom Apps and Gemini Enterprise are different
products; evidence from one must not be represented as proof for the other. See
[Google Cloud's custom MCP server guide](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server).
That enterprise guide publishes the fixed redirect URI
`https://vertexaisearch.cloud.google.com/oauth-redirect`; do not treat it as evidence of the
consumer Gemini callback URI.

## 4. Verify with a read-only call

In a new Gemini conversation, type `@AgenticContext` and select the connected app. Begin with a
read-only request, for example:

```text
@AgenticContext Use project_overview for project agenticContext, then list the top-level files.
Do not change any files.
```

Confirm all of the following:

- Gemini shows the AgenticContext app selection.
- The local sidebar changes from Configured to Active.
- The recent call identifies the expected tool and project.
- The response contains no absolute project path or credential value.

Only then test a write in a disposable project. Gemini should present its write confirmation,
and AgenticContext still enforces the project's `writable` flag, read receipts, SHA-256 guards,
approved checks, and final review ordering.

## Rotation, revocation, and recovery

- **Revoke Gemini access:** clear the public origin in Step 1. This removes the local OAuth client
  credentials and signing key, so existing access and refresh tokens stop validating. Stop or
  remove the reverse tunnel separately.
- **Rotate after exposure:** clear the configuration, save the origin again, update the Gemini
  Custom App with the newly generated client ID and secret, and reconnect.
- **401 Unauthorized:** confirm the Gemini app is using the current client credentials and repeat
  the OAuth connection. Never place an access token or client secret in the MCP URL.
- **Pending callback does not appear:** leave the provider's waiting authorization page open, then
  refresh the local Gemini Tunnel status. The request expires after 10 minutes and must be started
  again if it is no longer shown.
- **Invalid redirect URI:** compare the complete pending URI with the provider flow you started.
  Deny an unexpected URI instead of approving it. The first approved complete callback URI is
  pinned until AgenticContext restarts; AgenticContext does not guess a consumer callback domain.
- **404 Not Found:** confirm the request uses the exact configured hostname and the copied
  `/mcp/gemini` URL. A 404 for `/`, `/agent`, `/settings`, `/static`, or `/mcp` on the public
  hostname is intentional.
- **Configured but not Active:** confirm `cloudflared` is running, DNS resolves to the named
  tunnel, and Gemini completed OAuth. Configured is local state, not a remote health claim.
- **After an AgenticContext restart:** reconnect the Gemini Custom App. Authorization codes and
  bearer/refresh grants are deliberately kept only in process memory; saved client credentials
  remain stable, but grants from the previous process no longer validate.
- **Tool changes do not appear:** restart AgenticContext to load the source change, then refresh or
  recreate the Gemini Custom App so it performs a new `tools/list` request.

The OAuth discovery and bearer challenge follow the
[MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).
Focused local tests can verify the gateway, tool catalog, credential redaction, and UI behavior;
only an actual Gemini connection proves provider-side eligibility, OAuth interoperability, tool
discovery, and confirmation behavior.
