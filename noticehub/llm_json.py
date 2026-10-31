import json
import re
from typing import Any

def parse_json(text: str, *, expect: str = "object") -> Any:
    value = str(text or "").replace("```json", "").replace("```JSON", "").replace("```", "")
    value = re.sub(r"\"thinking\"\s*:\s*\"(?:\\.|[^\"])*\"\s*,?", "", value, flags=re.S)
    start = value.find("{" if expect == "object" else "[")
    end = value.rfind("}" if expect == "object" else "]")
    if start < 0 or end < start:
        raise ValueError("LLM output does not contain JSON")
    try:
        result = json.loads(value[start:end + 1])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON from LLM") from exc
    if expect == "object" and not isinstance(result, dict):
        raise ValueError("expected JSON object")
    if expect == "array" and not isinstance(result, list):
        raise ValueError("expected JSON array")
    return result
