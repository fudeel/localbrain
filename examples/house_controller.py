"""An agent that controls a pretend house through tool calls. Replace the handlers with real ones.

The model never touches anything itself. It can only ask for one of the functions listed in TOOLS,
and this program decides whether to run it.

    python3.15 -m examples.house_controller qwen3:4b-q4_K_M "It's dark in the kitchen"
"""

import argparse
import json

from brain.sdk import BrainClient

SYSTEM = "You control a house by calling the provided functions. Use them, then confirm briefly."

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_light",
            "description": "Turn the light in one room on or off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {"type": "string", "enum": ["kitchen", "bedroom", "hall"]},
                    "on": {"type": "boolean"},
                },
                "required": ["room", "on"],
            },
        },
    }
]


def set_light(room: str, on: bool) -> dict:
    print(f"  [house] {room} light -> {'ON' if on else 'OFF'}")
    return {"room": room, "on": on, "ok": True}


HANDLERS = {"set_light": set_light}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent")
    parser.add_argument("request")
    parser.add_argument("--url", default="http://127.0.0.1:8484")
    args = parser.parse_args()

    brain = BrainClient(args.url)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": args.request}]

    for _ in range(5):
        reply = brain.chat(args.agent, messages=messages, tools=TOOLS, think=False, auto_mount=True)
        calls = reply["tool_calls"]
        if not calls:
            print(reply["content"])
            return
        messages.append({"role": "assistant", "content": reply["content"], "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            handler = HANDLERS.get(name)
            outcome = handler(**call["function"]["arguments"]) if handler else {"error": "unknown"}
            messages.append({"role": "tool", "content": json.dumps(outcome), "tool_name": name})
    print("The agent kept calling functions, so this program stopped it.")


if __name__ == "__main__":
    main()
