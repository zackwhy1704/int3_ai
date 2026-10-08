# Company Brain — hosted pilot (in progress)

Scoped, cited question answering over a company's own documents, with a truth layer
that tracks which claim is still current. This branch line builds the hosted pilot
(gates 5–9, see CLAUDE.md).

The local partner demo is frozen at git tag **`demo-v1`**
(`git checkout demo-v1 && docker compose up -d`, then follow that README).

## Run locally

```sh
cp .env.example .env            # fill in the secrets (openssl rand -hex 32)
docker compose --profile dev up -d --build
docker compose run --rm tools python -m app.tenants.admin provision \
  --id brindlewood --name "Brindlewood Supply Co." --seed /seed/brindlewood \
  --google-domain brindlewood.example
```

Open http://localhost:5173 and sign in.

- **Test sign-in (dev only):** click "Continue with Test Google". On the test provider's
  form, enter any username (it becomes the account's stable id, e.g. `google-priya`) and
  these claims:
  `{"email": "priya@brindlewood.example", "email_verified": true, "hd": "brindlewood.example"}`.
  Seeded users are `priya`, `marcus` and `ada` `@brindlewood.example`.
- **Real Google / Microsoft:** set `GOOGLE_CLIENT_ID/SECRET` and `MS_CLIENT_ID/SECRET`
  in `.env`. Redirect URIs are `http://localhost:5173/api/auth/callback/google` and
  `.../callback/microsoft`. Then set the tenant's policy:
  `docker compose run --rm tools python -m app.tenants.admin configure --id <tenant> --google-domain <domain> --ms-tenant-id <entra tenant id>`.

## Operator commands (tools container: the only one with superuser credentials)

```sh
docker compose run --rm tools python -m app.tenants.admin provision --id acme \
  --name "Acme Pte Ltd" --admin-email ops@acme.example [--google-domain acme.example] \
  [--ms-tenant-id <guid>]
docker compose run --rm tools python -m app.tenants.admin configure --id acme --google-domain acme.example
docker compose run --rm tools pytest -v                  # full suite (provisions test tenants)
docker compose run --rm tools pytest -v -m "not llm"     # without model calls
./preflight.sh                                           # local stack health, PASS/FAIL per check
```

A tenant is one Postgres database (`t_<id>`) reachable only by its own role. Provisioning
creates the role and database, applies the schema and grants, and registers the tenant
and its users in the control database. If any step fails, it removes what it created.

**Reset everything:** `docker compose down -v`.
