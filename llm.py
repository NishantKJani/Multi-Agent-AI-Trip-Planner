"""Generic agent loop: model calls tools until it calls submit_result with validated output."""
import json
from typing import Callable
import anthropic
from pydantic import BaseModel



def run_agent(client: anthropic.Anthropic, model: str, system: str, user: str,
              tools: list[dict], handlers: dict, output_model: type[BaseModel],
              trace, name: str, max_turns: int = 6,
              validate_result: Callable[[BaseModel], BaseModel] | None = None) -> BaseModel:
    submit = {"name": "submit_result", "description": "Submit your final answer.",
              "input_schema": output_model.model_json_schema()}
    messages = [{"role": "user", "content": user}]
    for _ in range(max_turns):
        resp = client.messages.create(model=model, max_tokens=1500, system=system,
                                      tools=tools + [submit], messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_result":
                result = output_model.model_validate(block.input)
                return validate_result(result) if validate_result else result
            out = handlers[block.name](**block.input)
            trace.log(name, "tool_call", f"{block.name}({json.dumps(block.input)}) -> {len(out)} rows")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results or "Call submit_result with your final answer."})
    raise RuntimeError(f"{name} did not submit a result in {max_turns} turns")
