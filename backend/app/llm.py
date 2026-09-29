"""The one LLM call path for the demo: Claude with schema-constrained JSON output."""
import json
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


def structured(system: str, user: str, schema: dict) -> dict:
    """Return the model's reply, which the API constrains to match `schema`."""
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(text)
