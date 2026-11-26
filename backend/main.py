"""Chat with the agent in the terminal, playing both the customer and the broker.

    python main.py                       # you type as the customer
    python main.py --demo                # plays a sample Whitefield buyer conversation
    python main.py --name "Priya Nair" --phone +971501234567 --source "Meta lead form"
"""
import argparse
import logging
import uuid

from dotenv import load_dotenv

from src.graph import create_graph
from src.runner import config_for, describe_pending, resume, run_turn
from src.util import db

DEMO = [
    "Hi, I saw your listing online. Looking for a 3BHK in Whitefield.",
    "Budget is around 1.5 to 1.8 Cr.",
    "We want to shift within 2 months, ready to move preferred. Home loan is already sanctioned.",
    "Saturday 11 AM works for us.",
]


def show(result, graph, thread_id: str) -> None:
    if result.reply:
        print(f"\nAgent: {result.reply}")
    score = result.lead_score
    if score:
        print(f"   [lead score {score['total']}/100 · {score['band']} · {score['next_action']}]")
    while result.pending:
        snap = graph.get_state(config_for(thread_id))
        print("\n--- Broker approval needed ---\n" + describe_pending(result.pending, snap.values))
        answer = input("Broker: type 'yes' to approve, or a reason to decline: ").strip()
        approve = answer.lower() in ("y", "yes", "haan")
        result = resume(graph, thread_id, approve, "" if approve else answer)
        if result.reply:
            print(f"\nAgent: {result.reply}")


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true")
    p.add_argument("--name", default="Rohan Mehta")
    p.add_argument("--phone", default="+919800004521")
    p.add_argument("--source", default="Portal enquiry")
    args = p.parse_args()

    db.ensure_listings()
    graph = create_graph()
    thread_id = f"cli-{uuid.uuid4().hex[:8]}"
    lead = {"name": args.name, "phone": args.phone, "source": args.source}

    if args.demo:
        for line in DEMO:
            print(f"\nCustomer: {line}")
            show(run_turn(graph, thread_id, line, lead), graph, thread_id)
        return

    print("Type as the customer. 'exit' to quit.")
    while True:
        text = input("\nCustomer: ").strip()
        if text.lower() in ("exit", "quit"):
            break
        if text:
            show(run_turn(graph, thread_id, text, lead), graph, thread_id)


if __name__ == "__main__":
    main()
