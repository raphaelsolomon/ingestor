import llm_client
import hashlib
import json
from datetime import datetime, timezone
from dateutil import parser as dateparser
import store
from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv()



REVIEW_DATE = "2026-09-30"


def _addr_list(json_text: str | None) -> list:
    return json.loads(json_text) if json_text else []

def build_packet(conn, thread_id: str) -> tuple:
    thread = store.get_thread(conn, thread_id)
    messages = store.list_messages_by_thread(conn, thread_id)

    ref_map = {}
    packet_messages = []

    for i, m in enumerate(messages):
        ref = f"msg_{i + 1}"
        ref_map[ref] = m["id"]
        segments = store.list_segments_by_message(conn, m["id"])
        packet_messages.append({
            "ref": ref,
            "subject": m["subject"],
            "sender": m["sender"],
            "to": _addr_list(m["to_addrs"]),
            "cc": _addr_list(m["cc_addrs"]),
            "sent_at": m["sent_at"],
            "attachments": json.loads(m["attachments"]) if m["attachments"] else [],
            "segments": [
                {
                    "idx": s["id"], "author": s["author"], "sent_at_text": s["sent_at_text"],
                    "subject": s["subject"], "body": s["body"],
                }
                for s in segments
            ]
        })
    packet = {
        "thread": {
            "grouping_basis": thread["grouping_basis"],
            "grouping_uncertainty": thread["grouping_uncertainty"],
            "review_date": REVIEW_DATE,
        },
        "messages": packet_messages,
    }
    return packet, ref_map

def _source_text(conn, message_id: str, source_field: str, segment_index):
    message = store.get_message(conn, message_id)
    if not message:
        return None
    if source_field == "body":
        if segment_index is None:
            return None
        for seg in store.list_segments_by_message(conn, message_id):
            if seg["idx"] == segment_index:
                return seg["body"], seg["id"]
        return None
    field_map = {
        "subject": message["subject"],
        "sender": message["sender"],
        "sent_at": message["sent_at"],
        "to": " ".join(_addr_list(message["to_addrs"])),
        "cc": " ".join(_addr_list(message["cc_addrs"])),
        "attachment_name": " ".join(a["name"] for a in json.loads(message["attachments"]) or []),
    }
    text = field_map.get(source_field)
    return (text, None) if text is not None else None


def _validate_evidence(conn, ref_map: dict, evidence_list: list):
    surviving = []
    for item in evidence_list or []:
        message_id = ref_map.get(item.get("message_ref"))
        if message_id is None:
            continue
        result = _source_text(conn, message_id, item.get("source_field"), item.get("segment_index"))
        if result is None:
            continue
        text, segment_id = result
        quote = item.get("quote") or ""
        start = text.find(quote)
        if not quote or start == -1:
            continue
        surviving.append({
            "message_id": message_id,
            "segment_id": segment_id,
            "source_field": item.get("source_field"),
            "field": item.get("field"),
            "quote": quote,
            "start_offset": start,
            "end_offset": start + len(quote),
        })
    return surviving

def _is_past(deadline_text: str) -> bool:
    try:
        parsed = dateparser.parse(deadline_text, fuzzy=True)
        return parsed.date() < datetime.fromisoformat(REVIEW_DATE).date()
    except (ValueError, TypeError):
        return False

def validate_and_store(conn, thread_id: str, draft: dict, ref_map: dict, model_name: str) -> str:
    evidence = _validate_evidence(conn, ref_map, draft.get("evidence"))
    by_field = {}
    for e in evidence:
        by_field.setdefault(e["field"], []).append(e)

    uncertainty_notes = [draft.get("uncertainty")] if draft.get("uncertainty") else []

    priority = draft.get("priority")
    if not by_field.get("priority") and not by_field.get("reason"):
        priority = None
        review_status = "needs_evidence"
        uncertainty_notes.append("priority has no surviving evidence")
    else:
        review_status = "confirmed"

    reason = draft.get("reason") if by_field.get("reason") or by_field.get("priority") else None
    if draft.get("reason") and reason is None:
        uncertainty_notes.append("reason has no surviving evidence")

    alert = draft.get("alert")
    who = what = why = action = deadline = None
    stale = False

    if priority in (1, 2) and alert:
        required = ["who", "what", "why", "action"]
        fields = {}
        for f in required:
            if by_field.get(f):
                fields[f] = alert.get(f)
            else:
                uncertainty_notes.append(f"{f} has no surviving evidence")
        if len(fields) < len(required):
            review_status = "needs_evidence"
        who, what, why, action = fields.get("who"), fields.get("what"), fields.get("why"), fields.get("action")

        if by_field.get("deadline") and alert.get("deadline"):
            deadline = alert["deadline"]
            stale = _is_past(deadline)
        elif alert.get("deadline"):
            uncertainty_notes.append("deadline has no surviving evidence")

    now_iso = datetime.now(timezone.utc).isoformat()
    judgement_id = hashlib.sha1(f"{thread_id}:{now_iso}".encode()).hexdigest()
    store.insert_judgement(conn, {
        "id": judgement_id, "thread_id": thread_id, "priority": priority, "reason": reason,
        "uncertainty": "; ".join(uncertainty_notes) if uncertainty_notes else None,
        "review_status": review_status, "stale": int(stale), "who": who, "what": what, "why": why,
        "action": action, "deadline": deadline, "model_name": model_name,
        "created_at": now_iso,
    })
    for i, e in enumerate(evidence):
        sid = e.get("segment_id") or ""
        store.insert_evidence_span(conn, {
            "id": hashlib.sha1(f"{judgement_id}:{i}:{e['message_id']}:{sid}:{e['source_field']}:{e['field']}:{e['start_offset']}:{e['end_offset']}".encode()).hexdigest(),
            "judgement_id": judgement_id, "correction_id": None, **e,
        })
    return judgement_id

def run_judge(db_path: str, model: str) -> dict:
    conn = store.connect(db_path)
    judged, skipped = 0, 0
    for thread in store.list_active_threads(conn):
        messages = store.list_messages_by_thread(conn, thread["id"])

        if not any(m["parse_status"] == "ok" for m in messages):
            skipped += 1
            continue
        if store.latest_judgement_for_thread(conn, thread["id"]) is not None:
            continue
        packet, ref_map = build_packet(conn, thread["id"])
        draft = llm_client.judge_thread(packet, model)
        validate_and_store(conn, thread["id"], draft, ref_map, model)
        judged += 1
    conn.commit()
    return {"judged": judged, "skipped_unreadable": skipped}

if __name__ == "__main__":
    db_path = str(Path(__file__).resolve().parent.parent / "data" / "mailing.db")
    model = os.getenv("ANTHROPIC_MODEL")
    result = run_judge(db_path, model)
    print(result)