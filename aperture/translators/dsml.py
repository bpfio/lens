"""DSML (Console Go XML) tool call normalization.

Detects and converts Console Go DSML-style tool calls embedded in
response content text to standard OpenAI tool_calls format.

Mirrors JS src/translators/dsml.js.
"""

import json
import re
import secrets

from ..config import DSML_CONTENT_MAX


def normalize_dsml_tool_calls(response_body: dict) -> dict:
    """Detect and normalize DSML tool calls to standard tool_calls format.

    Console Go sometimes returns tool calls as DSML XML embedded in content
    (with finish_reason="stop") instead of standard message.tool_calls.
    This function detects and normalizes the pattern.

    Args:
        response_body: Parsed upstream response dict.

    Returns:
        Modified response_body with tool_calls extracted from DSML XML.
        Returns unchanged if no DSML pattern found.
    """
    if not response_body:
        return response_body

    choices = response_body.get("choices")
    if not choices or not isinstance(choices, list) or not choices:
        return response_body

    choice = choices[0]
    msg = choice.get("message") or {}
    content = msg.get("content", "") or ""

    # Cap content length to prevent ReDoS
    if len(content) > DSML_CONTENT_MAX:
        return response_body

    # Quick check: look for invoke name=" pattern
    if not re.search(r"invoke\s+name\s*=\s*\"", content, re.IGNORECASE):
        return response_body

    tool_calls = []

    # Extract invoke blocks
    invoke_pattern = re.compile(
        r'invoke\s+name\s*=\s*"([^"]+)"([\s\S]*?)(?=invoke\s+name\s*=\s*"|$)',
        re.IGNORECASE,
    )
    for invoke_match in invoke_pattern.finditer(content):
        fn_name = invoke_match.group(1)
        block_content = invoke_match.group(2)

        args = {}
        param_pattern = re.compile(
            r'parameter\s+name\s*=\s*"([^"]+)"[^>]*>([\s\S]*?)</parameter',
            re.IGNORECASE,
        )
        for p_match in param_pattern.finditer(block_content):
            if p_match.group(1):
                raw_val = (p_match.group(2) or "").strip()
                val = raw_val
                if raw_val.lower() == "true":
                    val = True
                elif raw_val.lower() == "false":
                    val = False
                elif raw_val.lower() in ("null", "none"):
                    val = None
                else:
                    try:
                        val = int(raw_val)
                    except ValueError:
                        try:
                            val = float(raw_val)
                        except ValueError:
                            try:
                                val = json.loads(raw_val)
                            except (json.JSONDecodeError, ValueError):
                                val = raw_val
                args[p_match.group(1)] = val

        if args:
            tool_calls.append({
                "index": len(tool_calls),
                "id": f"call_dsml_{secrets.token_hex(8)}",
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": json.dumps(args),
                },
            })

    if not tool_calls:
        return response_body

    # Remove DSML blocks from content, keep any prose
    prose = re.sub(
        r'<invoke\s+name\s*=\s*"[^"]*"[\s\S]*?</invoke>',
        "",
        content,
        flags=re.IGNORECASE,
    ).strip()
    msg["content"] = prose or ""
    msg["tool_calls"] = tool_calls

    if choice.get("finish_reason") in ("stop", "length"):
        choice["finish_reason"] = "tool_calls"

    return response_body
