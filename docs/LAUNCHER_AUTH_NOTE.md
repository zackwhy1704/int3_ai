# Launcher auth: Hermes `oauth` remote mode vs. our ingress token swap

Design note (change A of the approved launcher design). Read at Hermes **v0.22.6**
(`f0d94882`) with `git show v0.22.6:<path>`. Verdict at the end.

## What Hermes's `oauth` remote mode is

- Desktop side: a remote connection with `authMode: 'oauth'` carries **no token** in
  `connections.json` (`connection-registry.ts:56-59`). REST is authed by an HttpOnly cookie
  pair: `hermes_session_at` (~15 min) and `hermes_session_rt` (rotating, reuse-detected);
  the WS upgrade needs a single-use `?ticket=` from `POST /api/auth/ws-ticket`
  (`connection-config.ts:15-18, 24-35, 99-104, 274-296`). The mode is picked when the
  gateway's public `/api/status` says `auth_required: true` (`connection-config.ts:1130-1133`).
- Desktop login is RFC 8252 native PKCE, **brokered by the Hermes backend**: desktop →
  `/auth/native/authorize` → upstream IDP → backend mints a one-time code → desktop redeems
  at `/auth/native/token` (`hermes_cli/dashboard_auth/native_flow.py:1-12`). The resulting
  tokens are kept in `<userData>/native-oauth-tokens.json` (`apps/desktop/electron/main.ts:7347-7363`),
  written through `encryptDesktopSecret` → `{encoding:'plain'}` under the default policy
  (`main.ts:8066-8070`).
- Backend side: the IDP is a **provider plugin** (`dashboard_auth/registry.py:1-3, 36-46`;
  protocol `dashboard_auth/base.py:92-135`). The bundled generic one is `self_hosted`, a plain
  OIDC relying party: discovery + PKCE + ID-token verification against the IDP's JWKS,
  RS/ES algorithms only, configured by `HERMES_DASHBOARD_OIDC_ISSUER/CLIENT_ID/...`
  (`plugins/dashboard_auth/self_hosted/__init__.py:1-11, 49-52, 300-315`). Issuer must be
  `https://` or loopback `http` (`:62-68`).

## Could our server be that provider for the gated backend?

Yes, without modifying Hermes: we would run an **OIDC provider** (discovery, `/authorize` with a
login page, `/token` with code + refresh grants, JWKS, signing-key rotation) and set the two
env vars on each gated backend. What it would cost:

1. **A new, security-critical service.** A standards-compliant OP with key rotation and refresh
   reuse detection is substantially more code and attack surface than one hashed-token table.
   CLAUDE.md puts token issuance under owner plan review for this reason.
2. **A second, interactive login per machine.** The PKCE flow runs in Hermes, in the system
   browser. The launcher cannot complete it for the user except by writing Hermes's internal
   `native-oauth-tokens.json`, which is undocumented internal state. Without that, users sign in
   to the launcher and then again in Hermes (or we add a browser SSO cookie, which is one more
   long-lived credential).
3. **The plaintext secret moves, it does not disappear.** The refresh token lands in
   `native-oauth-tokens.json` as `{encoding:'plain'}` by default (see above).
4. **Not testable before hosting.** The issuer must be https, or http on loopback. Container
   loopback is not host loopback, so the local demo stack cannot run it without TLS.
5. **More Hermes surface on our side:** the `/auth/*` and `/api/auth/ws-ticket` routes join the
   route allowlist, plus container egress to the issuer. This code path also moves fast
   upstream (e.g. the #94558 fix noted in `dashboard_auth/base.py:69-73`).

What it would give: a ~15 min access token, rotating refresh with reuse detection, and no
token in the WS URL. Those are real gains.

## Verdict: proceed with the ingress token swap (revisit after hosting)

Not clean enough for the partner MVP: points 2 and 4 are blockers. The swap gets most of the
benefit with far less new machinery:

- The backend's `HERMES_DASHBOARD_SESSION_TOKEN` (read once at start, `web_server.py:224`)
  becomes **server-internal**: random per backend, held only by that backend and its own
  ingress container, never stored in a table and never sent to a client.
- At each Launch the launcher gets a **desktop token** (12 h, SHA-256 at rest, bound to one
  device session and one backend). That is the only token in `connections.json`.
- A per-user **ingress auth proxy** (the slot the socat forwarder occupies today) checks the
  desktop token on every request and on the WS upgrade (`X-Hermes-Session-Token` / `?token=`,
  `connection-config.ts:13-14, 88`) through a backend introspection call, then swaps in the
  internal token. Failure gives 401 before Hermes. Revoking a device or token closes its open
  WebSockets within the re-check interval.
- Hermes and `route_proxy.py` are unchanged; the gated container is untouched.

Revisit `oauth` mode once hosted TLS exists, if per-request short-lived tokens become a
requirement. The swap does not rule that out.
