# Company Brain — local demo

A scoped, cited question-answering demo over a fictional company, Brindlewood Supply Co.
Every document in `seed/` is invented. Everything runs on this laptop except the
answer-writing model call, which goes to the Claude API.

## Start

```sh
cp .env.example .env     # first time only: add ANTHROPIC_API_KEY
docker compose up -d     # add --build after code changes
```

The demo is ready in under 10 seconds: open http://localhost:5173. Claims are pre-extracted
in `seed/claims.json`, so startup calls no model and needs no volume wipe.

**10 minutes before the meeting:** run `./preflight.sh`. Every line must say PASS.
It also refreshes the fallback cache (see "If it breaks").

## Demo script

Start with **Reset** (top right). That signs in as Priya and pre-fills the refund question.

**1. Scoping: same question, different people, different answers.**
- Signed in as **Priya N.** (Operations), click **Ask**.
- Point at the sidebar: she has Company-wide and Operations; **Leadership is locked**,
  and all she can see is that it exists and who owns it.
- Switch the dropdown to **Marcus O.** (Finance) and click the refund question under
  **Try asking**. His answer adds **45 days for annual contracts**, from a Finance
  document Priya can't see.
- Say: *"Same question, same company. Each person gets the answer their access
  allows, and nothing they can't open ever reaches the model."*

**2. Change tracking: which document is still true.**
- On either answer, point at the amber box: **"This changed on 4 March 2026. It used
  to say 14 days."** It shows both versions with their dates and verbatim quotes.
- Click **Open** on "Enterprise terms — v3". The cited paragraph is highlighted.
- Say: *"Search finds you both documents. We tell you which one is still true, and
  what it used to say."*

**3. Refusal: it doesn't make things up.**
- Type **"What is our parental leave policy?"** and click **Ask**.
- It answers **"No reliable source found. Try asking Grace H. (People)."**
- Say: *"If there's no source you're allowed to open, it says so and tells you who to
  ask. It never invents an answer."*

**If your partner asks "so Priya gets a wrong answer?"**: she gets the right answer
for what she's allowed to see, with the sources to prove it. That's the feature.

## If it breaks

1. **Run `./preflight.sh`.** It names the failing check.
2. **Containers not running, or the page won't load:** run `docker compose up -d`,
   wait 10 seconds, and reload. Don't use `down -v`: it isn't needed, and it empties
   the answer cache.
3. **The API key check fails, or you see "Couldn't get an answer":** the model can't
   be reached (no network, bad key, or API trouble). Any question already asked
   successfully, for that user, will still answer from the cache. A small blue
   "Cached answer" line says so honestly. The preflight asks all the scripted and
   Try-asking questions, so after a passing preflight the whole script works offline.
   Refusals never need the model.
4. **Still stuck:** run `docker compose restart backend`, then `docker compose logs
   backend | tail -30`.

The cache only ever holds real answers the model actually produced. It is keyed on the
question and the asker's access, so one person's cached answer is never shown to
someone with different access.

## Demo users

Every API call carries `X-User-Id`, and the server resolves the scopes.
`X-User-Id` is demo only — replaces authentication, trivially spoofable, never ships.

| id     | person                        | scopes                   |
|--------|-------------------------------|--------------------------|
| priya  | Priya N., Head of Operations  | company-wide, operations |
| marcus | Marcus O., Finance Director   | company-wide, finance    |
| ada    | Ada L., CEO                   | company-wide, leadership |

## Tests and tools

```sh
docker compose exec backend pytest -v                     # full suite, calls the API
docker compose exec backend pytest -v -m "not llm"        # no API calls
docker compose exec backend python -m app.calibrate       # refusal threshold measurement
docker compose exec backend python -m app.supersede_eval --runs 10   # supersede pass rates
```

**Regenerate the claims seed** (slow; calls the model once per chunk, about 2 minutes;
the results vary between runs, so re-check the demo afterwards):

```sh
docker compose exec -T backend python -m app.regenerate_claims > seed/claims.json
```

**Reset all data** (also empties the answer cache, so re-run `./preflight.sh` after):

```sh
docker compose down -v && docker compose up -d
```

## curl

```sh
curl -s -H 'X-User-Id: priya'  'localhost:8000/api/search?q=refund+window+enterprise'
curl -s -X POST -H 'X-User-Id: marcus' -H 'Content-Type: application/json' \
  -d '{"question":"What'\''s our refund window for enterprise customers?"}' localhost:8000/api/ask
```
