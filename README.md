# Company Brain — local demo

A scoped, cited question-answering demo over a fictional company, Brindlewood Supply Co.
Every document in `seed/` is invented. Runs entirely on your laptop.

## Run

```sh
cp .env.example .env     # add ANTHROPIC_API_KEY (needed from gate 2 on)
docker compose up --build
```

- Web UI: http://localhost:5173
- API: http://localhost:8000

## Demo users

There is no auth: every API call carries `X-User-Id`, and the server resolves the scopes.
`X-User-Id` is demo only — replaces authentication, trivially spoofable, never ships.

| id     | person                        | scopes                   |
|--------|-------------------------------|--------------------------|
| priya  | Priya N., Head of Operations  | company-wide, operations |
| marcus | Marcus O., Finance Director   | company-wide, finance    |
| ada    | Ada L., CEO                   | company-wide, leadership |

## Demo script (browser)

1. Open http://localhost:5173
2. Signed in as **Priya N.** (default). Ask "What's our refund window for enterprise
   customers?" (prefilled). Expect 30 days, an amber "This changed on 4 March 2026"
   box with the v3 and v2 quotes, and Leadership shown locked in the sidebar.
3. Switch the dropdown to **Marcus O.** and ask the same question. Expect the same
   change box plus "Also true, under a condition": 45 days for annual contracts.
4. Ask "What is our parental leave policy?". Expect "No reliable source found" and
   "Try asking Grace H. (People)".

## Try it with curl

```sh
curl -s -H 'X-User-Id: priya'  'localhost:8000/api/search?q=refund+window+enterprise'
curl -s -H 'X-User-Id: marcus' 'localhost:8000/api/search?q=refund+window+enterprise'
```

## Tests

```sh
docker compose exec backend pytest -v              # no LLM needed
docker compose exec backend pytest -v -m llm       # calls the configured LLM
```

## Reset the data

```sh
docker compose down -v && docker compose up --build
```
