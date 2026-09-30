"""The one LLM call path for the demo: Claude with schema-constrained JSON output."""
import json
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


class Unavailable(Exception):
    """The model call failed: network, timeout, auth, API error or unparseable reply."""


def structured(system: str, user: str, schema: dict, max_tokens: int = 1024) -> dict:
    """Return the model's reply, which the API constrains to match `schema`."""
    try:
        client = anthropic.Anthropic(timeout=30, max_retries=1)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return json.loads(text)
    except Exception as e:  # the SDK raises TypeError, not APIError, when no key is set
        raise Unavailable(f"{type(e).__name__}: {e}") from e
