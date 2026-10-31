"""A choose-your-own-adventure for kids, told by a local agent, with memory across turns.

    python3.15 -m examples.story_game qwen3:4b-q4_K_M
"""

import argparse
import uuid

from brain.sdk import BrainApiError, BrainClient

STORYTELLER = """You run a gentle choose-your-own-adventure for children aged 6 to 10.
Tell the next part of the story in at most four short sentences, then offer exactly two numbered
choices. Keep it kind and exciting, with nothing scary. Never mention that you are an AI."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent")
    parser.add_argument("--url", default="http://127.0.0.1:8484")
    args = parser.parse_args()

    brain = BrainClient(args.url)
    game = f"story-{uuid.uuid4().hex[:8]}"
    theme = input("What should the story be about? ").strip() or "a dragon who loves pancakes"
    message = f"Start a story about {theme}."

    while True:
        try:
            for event in brain.stream(
                args.agent,
                message=message,
                conversation_id=game,
                system=STORYTELLER,
                think=False,
                auto_mount=True,
            ):
                if event["type"] == "content":
                    print(event["text"], end="", flush=True)
        except BrainApiError as error:
            print(f"The storyteller is not available: {error}")
            return
        try:
            choice = input("\n\nWhat do you do? ").strip()
        except EOFError, KeyboardInterrupt:
            print("\nThe end!")
            return
        message = f"I choose: {choice}"


if __name__ == "__main__":
    main()
