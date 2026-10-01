import sys
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from ingest import run_ingest
from judge import run_judge
from export_evalutation import write_export


if __name__ == "__main__":
    emails_dir = sys.argv[1] if len(sys.argv) > 1 else "emails"
    db_path = str(Path(__file__).resolve().parent.parent / "data" / "mailing.db")
    output_path = str(Path(__file__).resolve().parent.parent / "data" / "evaluation_export.jsonl")

    model = os.getenv("ANTHROPIC_MODEL", "MiniMax-M3")

    print("Ingest results:", run_ingest(emails_dir, db_path))
    print("Judge results:", run_judge(db_path, model))
    count = write_export(db_path, output_path)
    print(f"Export: wrote {count} evaluation records to {output_path}")
