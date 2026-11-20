import argparse
import json
import os
from getpass import getpass
from pathlib import Path

from ontology_engine import ENGINE_VERSION, load_project, build_messages, generate


def main():
    parser = argparse.ArgumentParser(description="Generate one ontology file using existing vocabulary terms only.")
    parser.add_argument("--config", default=str(Path(__file__).with_name("settings.json")))
    parser.add_argument("--preview", action="store_true", help="Validate inputs and show the initial prompt; no model call or output file.")
    args = parser.parse_args()
    project = load_project(args.config)
    cfg = project["config"]
    print(f"Engine {ENGINE_VERSION} | {cfg.provider}/{cfg.model} | {cfg.mode}")
    print("CQs:", ", ".join(cq["id"] for cq in project["selected_cqs"]))
    if args.preview:
        current = project["selected_cqs"] if cfg.mode == "joint" else project["selected_cqs"][:1]
        print(json.dumps(build_messages(project, current), indent=2, ensure_ascii=False))
        return 0
    if cfg.provider == "openai" and not os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ["OPENAI_API_KEY"] = getpass("OpenAI API key (hidden): ").strip()
    result = generate(project)
    return 0 if result["status"] == "generated" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
