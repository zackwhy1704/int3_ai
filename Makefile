# Eval gate for models offered through the BYOK gateway (item g).
#
# Runs seed/<tenant>/questions.yaml through the REAL agent path — the
# OpenAI-compatible gateway doing tool-calling with the real ask_brain tool
# backed by the real brain — once per model, and enforces the pass bar:
# refusals N/N honoured and 0 foreign (cross-scope) citations. Only models that
# PASS are eligible for a tenant allowlist; failing models are not offered.
#
# These targets drive the running spike containers (gateway-spike + brain +
# mcp-brain on tenant-net). They stage the harness and question set into the
# gateway container, which can reach both the gateway (localhost:8001) and the
# brain. eval/gate.py is also runnable anywhere the same env vars are set.

GW            ?= gateway-spike
BRAIN_URL     ?= http://brain:8000
QUESTIONS_SRC ?= seed/brindlewood/questions.yaml

# MVP model, verified against Anthropic's live /v1/models endpoint. Sonnet ONLY:
# claude-haiku-5-5 is "pending stricter eval" and is on no default allowlist
# (see eval/README.md). Override ANTHROPIC_MODELS to evaluate others.
ANTHROPIC_MODELS ?= claude-sonnet-5-5

.PHONY: eval eval-openai _eval_stage

_eval_stage:
	docker cp eval/gate.py $(GW):/tmp/gate.py
	docker cp $(QUESTIONS_SRC) $(GW):/tmp/questions.yaml

# make eval — the Anthropic gate (gateway container already holds ANTHROPIC_API_KEY).
eval: _eval_stage
	docker exec \
	  -e GATEWAY_URL=http://localhost:8001 \
	  -e BRAIN_URL=$(BRAIN_URL) \
	  -e BRAIN_TOKEN=$$(docker exec mcp-brain printenv BRAIN_SERVICE_TOKEN) \
	  -e QUESTIONS=/tmp/questions.yaml \
	  -e MODELS=$(ANTHROPIC_MODELS) \
	  $(GW) python /tmp/gate.py

# make eval-openai — the one-command path to evaluate an OpenAI model LATER.
#
# No OpenAI key exists now and OpenAI is on NO allowlist: OpenAI is BUILT (the
# gateway's OpenAI-compatible provider path exists and is unit-tested with a
# mocked upstream), NOT EVALUATED, NOT OFFERED. This target must NOT run as part
# of `make eval`; it refuses unless OPENAI_API_KEY and OPENAI_MODELS are set.
#
# To evaluate later: put the OpenAI key in the gateway (service mode:
# `python admin_keys.py set-key --tenant T --provider openai` with KEY=...; dev:
# an openai route + key), then:  OPENAI_MODELS=<id,...> make eval-openai
# Only models that then PASS this same bar may be added to a tenant allowlist.
eval-openai: _eval_stage
	@if [ -z "$$OPENAI_API_KEY" ] && [ -z "$$OPENAI_MODELS" ]; then \
	  echo "OpenAI: built, not evaluated, not offered — no OPENAI_API_KEY set."; \
	  echo "Set OPENAI_API_KEY and OPENAI_MODELS=<id,...> to evaluate; this is"; \
	  echo "never run as part of 'make eval'."; \
	  exit 0; \
	fi; \
	docker exec \
	  -e GATEWAY_URL=http://localhost:8001 \
	  -e BRAIN_URL=$(BRAIN_URL) \
	  -e BRAIN_TOKEN=$$(docker exec mcp-brain printenv BRAIN_SERVICE_TOKEN) \
	  -e QUESTIONS=/tmp/questions.yaml \
	  -e MODELS=$$OPENAI_MODELS \
	  $(GW) python /tmp/gate.py

.PHONY: e2e e2e-down

# make e2e — headless E2E (DEMO.md steps 1-5 + negative checks) against a FRESH,
# ISOLATED stack (DEPLOY_PROJECT=hermes-e2e) that never touches a running
# hermes-deploy stack: build images -> up --build -> provision 3 users -> run the
# scenario -> teardown. Exits non-zero on any failure and prints a pass/fail table.
# Needs docker + an Anthropic key in deploy/hermes/.env (or the env). See
# deploy/hermes/e2e/.
e2e:
	bash deploy/hermes/e2e/make-e2e.sh

# make e2e-down — force-remove a leftover isolated e2e stack (never the default one).
e2e-down:
	DEPLOY_PROJECT=hermes-e2e bash deploy/hermes/e2e/teardown.sh
