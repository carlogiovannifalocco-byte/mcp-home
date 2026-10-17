"""Free-text to actions with Amazon Bedrock (Converse API + tool use). Optional: only used by do_from_sentence."""

from __future__ import annotations

import os
from datetime import date

ACTION_TOOL = {
    "toolSpec": {
        "name": "plan_actions",
        "description": "The household actions contained in the user's sentence.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {"enum": ["add_grocery_item", "add_bill", "log_chore", "add_pantry_item"]},
                                "args": {
                                    "type": "object",
                                    "description": "add_grocery_item: item, quantity. add_bill: provider, amount, due "
                                    "(YYYY-MM-DD), optional repeat (weekly, monthly, bimonthly, quarterly, yearly). "
                                    "log_chore: chore, person. add_pantry_item: item, expires (YYYY-MM-DD).",
                                },
                            },
                            "required": ["tool", "args"],
                        },
                    }
                },
                "required": ["actions"],
            }
        },
    }
}


def _client():
    import boto3

    return boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))


def plan_from_sentence(sentence: str, client=None, today: date | None = None) -> list[dict]:
    model = os.getenv("BEDROCK_MODEL_ID")
    if not model:
        raise ValueError("Free-text commands need Amazon Bedrock: set BEDROCK_MODEL_ID.")
    today = today or date.today()
    client = client or _client()
    resp = client.converse(
        modelId=model,
        system=[{"text": f"Today is {today.isoformat()} ({today.strftime('%A')}). Extract every household action. "
                         "Resolve relative dates to YYYY-MM-DD. Do not invent amounts."}],
        messages=[{"role": "user", "content": [{"text": sentence}]}],
        toolConfig={"tools": [ACTION_TOOL], "toolChoice": {"tool": {"name": "plan_actions"}}},
        inferenceConfig={"temperature": 0},
    )
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"].get("actions", [])
    return []
