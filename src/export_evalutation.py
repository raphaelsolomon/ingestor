import json
import store
from pathlib import Path

JUDGEMENT_FIELD = ["priority", "reason", "uncertainty", "who", "what", "why", "action", "deadline"]


def _evidence_rows(conn, judgement_id=None, correction_id=None):
    """
    Evidence can be attached either to an original judgement OR to a correction (see store.py's
    evidence_span table: it has a nullable judgement_id AND a nullable correction_id, so each
    row belongs to exactly one of the two).
    """
    if judgement_id is not None:
        rows = store.list_evidence_for_judgement(conn, judgement_id)
    elif correction_id is not None:
        rows = store.list_evidence_for_correction(conn, correction_id)
    else:
        raise ValueError("either judgement_id or correction_id is required")
    return [
        {
            "message_id": r["message_id"],
            "segment_id": r["segment_id"],
            "source_field": r["source_field"],
            "quote": r["quote"],
            "field": r["field"],
        }
        for r in rows
    ]


def _effective_judgement(judgement: dict, corrections: list) -> dict:
    """
    Return the effective judgement: the original judgement with any corrections
    (human edits) applied on top, field by field.
    """
    effective = {k: judgement[k] for k in JUDGEMENT_FIELD}
    for c in corrections:
        effective[c["field"]] = c["new_value"]
    return effective


def build_export(db_path: str) -> list:
    conn = store.connect(db_path)
    records = []

    for thread in store.list_active_threads(conn):
        messages = store.list_messages_by_thread(conn, thread["id"])
        judgement = store.latest_judgement_for_thread(conn, thread["id"])
        if judgement is None:
            continue

        corrections = [dict(c) for c in store.list_corrections_for_judgement(conn, judgement["id"])]
        for c in corrections:
            c["evidence"] = _evidence_rows(conn, correction_id=c["id"])

        message_dicts = []
        for m in messages:
            segments = store.list_segments_by_message(conn, m["id"])
            message_dicts.append({
                "filename": m["filename"],
                "subject": m["subject"],
                "sender": m["sender"],
                "to": json.loads(m["to_addrs"] or "[]"),
                "cc": json.loads(m["cc_addrs"] or "[]"),
                "sent_at": m["sent_at"],
                "segments": [
                    {
                        "idx": s["idx"],
                        "author": s["author"],
                        "sent_at_text": s["sent_at_text"],
                        "body": s["body"],
                    }
                    for s in segments
                ],
            })

        records.append({
            "thread_id": thread["id"],
            "grouping_basis": thread["grouping_basis"],
            "grouping_confidence": thread["grouping_confidence"],
            "grouping_uncertainty": thread["grouping_uncertainty"],
            "messages": message_dicts,
            "original_judgement": {
                **{k: judgement[k] for k in JUDGEMENT_FIELD},
                "model_name": judgement["model_name"],
                "created_at": judgement["created_at"],
                "evidence": _evidence_rows(conn, judgement_id=judgement["id"]),
            },
            "corrections": corrections,
            "effective_judgement": _effective_judgement(judgement, corrections),
        })
    return records


def write_export(db_path: str, output_path: str) -> int:
    records = build_export(db_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent.parent
    db_path = str(project_root / "data" / "mailing.db")
    output_path = str(project_root / "data" / "evaluation_export.jsonl")
    count = write_export(db_path, output_path)
    print(f"Wrote {count} evaluation records to {output_path}")
