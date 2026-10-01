import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS thread (
    id TEXT PRIMARY KEY,
    normalized_subject TEXT NOT NULL,
    grouping_basis TEXT NOT NULL CHECK (grouping_basis IN
        ('conversation_id','references','duplicate_hash','subject_participants_time','single_message')),
    grouping_confidence TEXT NOT NULL CHECK (grouping_confidence IN ('exact','conservative')),
    grouping_uncertainty TEXT,
    superseded_at TEXT
);

CREATE TABLE IF NOT EXISTS message (
    id TEXT PRIMARY KEY,
    thread_id TEXT REFERENCES thread(id),
    filename TEXT NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('ok','empty','corrupt')),
    parse_error TEXT,
    message_id TEXT,
    in_reply_to TEXT,
    references_list TEXT,
    content_hash TEXT,
    duplicate_of TEXT REFERENCES message(id),
    subject TEXT,
    sender TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    sent_at TEXT,
    body_raw TEXT,
    attachments TEXT
);

CREATE TABLE IF NOT EXISTS segment (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES message(id),
    idx INTEGER NOT NULL,
    author TEXT,
    sent_at_text TEXT,
    subject TEXT,
    body TEXT NOT NULL,
    is_latest INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS judgement (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES thread(id),
    priority INTEGER,
    reason TEXT,
    uncertainty TEXT,
    review_status TEXT NOT NULL CHECK (review_status IN ('confirmed','needs_evidence','superseded')),
    stale INTEGER NOT NULL DEFAULT 0,
    who TEXT,
    what TEXT,
    why TEXT,
    action TEXT,
    deadline TEXT,
    model_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_span (
    id TEXT PRIMARY KEY,
    judgement_id TEXT REFERENCES judgement(id),
    correction_id TEXT REFERENCES correction(id),
    message_id TEXT NOT NULL REFERENCES message(id),
    segment_id TEXT REFERENCES segment(id),
    source_field TEXT NOT NULL CHECK (source_field IN
        ('body','subject','sender','to','cc','sent_at','attachment_name')),
    field TEXT NOT NULL CHECK (field IN ('priority','who','what','why','action','deadline','reason')),
    quote TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS correction (
    id TEXT PRIMARY KEY,
    judgement_id TEXT NOT NULL REFERENCES judgement(id),
    field TEXT NOT NULL CHECK (field IN ('priority','uncertainty','who','what','why','action','deadline')),
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    basis TEXT NOT NULL CHECK (basis IN ('source_evidence','human_input')),
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --- thread -----------------------------------------------------------

def upsert_thread(conn, thread_id, normalized_subject, grouping_basis, grouping_confidence, grouping_uncertainty):
    conn.execute(
        """INSERT INTO thread (id, normalized_subject, grouping_basis, grouping_confidence, grouping_uncertainty)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             normalized_subject=excluded.normalized_subject,
             grouping_basis=excluded.grouping_basis,
             grouping_confidence=excluded.grouping_confidence,
             grouping_uncertainty=excluded.grouping_uncertainty""",
        (thread_id, normalized_subject, grouping_basis, grouping_confidence, grouping_uncertainty),
    )


def supersede_thread(conn, thread_id, when_iso):
    conn.execute("UPDATE thread SET superseded_at = ? WHERE id = ?", (when_iso, thread_id))


def list_active_threads(conn):
    return conn.execute("SELECT * FROM thread WHERE superseded_at IS NULL").fetchall()


def get_thread(conn, thread_id):
    return conn.execute("SELECT * FROM thread WHERE id = ?", (thread_id,)).fetchone()


# --- message ------------------------------------------------------------

def upsert_message(conn, row: dict):
    conn.execute(
        """INSERT INTO message (id, thread_id, filename, parse_status, parse_error, message_id, in_reply_to,
             references_list, content_hash, duplicate_of, subject, sender, to_addrs, cc_addrs, sent_at, body_raw,
             attachments)
           VALUES (:id, :thread_id, :filename, :parse_status, :parse_error, :message_id, :in_reply_to,
             :references_list, :content_hash, :duplicate_of, :subject, :sender, :to_addrs, :cc_addrs, :sent_at,
             :body_raw, :attachments)
           ON CONFLICT(id) DO UPDATE SET
             thread_id=excluded.thread_id, filename=excluded.filename, parse_status=excluded.parse_status,
             parse_error=excluded.parse_error, message_id=excluded.message_id, in_reply_to=excluded.in_reply_to,
             references_list=excluded.references_list, content_hash=excluded.content_hash,
             duplicate_of=excluded.duplicate_of, subject=excluded.subject, sender=excluded.sender,
             to_addrs=excluded.to_addrs, cc_addrs=excluded.cc_addrs, sent_at=excluded.sent_at,
             body_raw=excluded.body_raw, attachments=excluded.attachments""",
        row,
    )


def set_message_thread(conn, message_id, thread_id):
    conn.execute("UPDATE message SET thread_id = ? WHERE id = ?", (thread_id, message_id))


def list_messages(conn):
    return conn.execute("SELECT * FROM message").fetchall()


def list_messages_by_thread(conn, thread_id):
    return conn.execute(
        "SELECT * FROM message WHERE thread_id = ? ORDER BY sent_at", (thread_id,)
    ).fetchall()


def get_message(conn, message_id):
    return conn.execute("SELECT * FROM message WHERE id = ?", (message_id,)).fetchone()


# --- segment --------------------------------------------------------------

def delete_segments_for_message(conn, message_id):
    segment_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM segment WHERE message_id = ?", (message_id,)
        ).fetchall()
    ]
    if segment_ids:
        placeholders = ",".join("?" * len(segment_ids))
        conn.execute(
            f"DELETE FROM evidence_span WHERE segment_id IN ({placeholders})",
            segment_ids,
        )
    conn.execute("DELETE FROM segment WHERE message_id = ?", (message_id,))


def insert_segment(conn, row: dict):
    conn.execute(
        """INSERT INTO segment (id, message_id, idx, author, sent_at_text, subject, body, is_latest)
           VALUES (:id, :message_id, :idx, :author, :sent_at_text, :subject, :body, :is_latest)""",
        row,
    )


def list_segments_by_message(conn, message_id):
    return conn.execute(
        "SELECT * FROM segment WHERE message_id = ? ORDER BY idx", (message_id,)
    ).fetchall()


def get_segment(conn, segment_id):
    return conn.execute("SELECT * FROM segment WHERE id = ?", (segment_id,)).fetchone()


# --- judgement --------------------------------------------------------------

def insert_judgement(conn, row: dict):
    conn.execute(
        """INSERT INTO judgement (id, thread_id, priority, reason, uncertainty, review_status, stale, who, what,
             why, action, deadline, model_name, created_at)
           VALUES (:id, :thread_id, :priority, :reason, :uncertainty, :review_status, :stale, :who, :what, :why,
             :action, :deadline, :model_name, :created_at)""",
        row,
    )


def latest_judgement_for_thread(conn, thread_id):
    return conn.execute(
        """SELECT * FROM judgement WHERE thread_id = ? AND review_status != 'superseded'
           ORDER BY created_at DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()


def supersede_judgements_for_thread(conn, thread_id):
    conn.execute(
        "UPDATE judgement SET review_status = 'superseded' WHERE thread_id = ? AND review_status != 'superseded'",
        (thread_id,),
    )


def get_judgement(conn, judgement_id):
    return conn.execute("SELECT * FROM judgement WHERE id = ?", (judgement_id,)).fetchone()


# --- evidence span ------------------------------------------------------

def insert_evidence_span(conn, row: dict):
    conn.execute(
        """INSERT INTO evidence_span (id, judgement_id, correction_id, message_id, segment_id, source_field, field,
             quote, start_offset, end_offset)
           VALUES (:id, :judgement_id, :correction_id, :message_id, :segment_id, :source_field, :field, :quote,
             :start_offset, :end_offset)""",
        row,
    )


def list_evidence_for_judgement(conn, judgement_id):
    return conn.execute(
        "SELECT * FROM evidence_span WHERE judgement_id = ?", (judgement_id,)
    ).fetchall()


def list_evidence_for_correction(conn, correction_id):
    return conn.execute(
        "SELECT * FROM evidence_span WHERE correction_id = ?", (correction_id,)
    ).fetchall()


# --- correction -----------------------------------------------------------

def insert_correction(conn, row: dict):
    conn.execute(
        """INSERT INTO correction (id, judgement_id, field, old_value, new_value, reason, basis, created_at)
           VALUES (:id, :judgement_id, :field, :old_value, :new_value, :reason, :basis, :created_at)""",
        row,
    )


def list_corrections_for_judgement(conn, judgement_id):
    return conn.execute(
        "SELECT * FROM correction WHERE judgement_id = ? ORDER BY created_at", (judgement_id,)
    ).fetchall()
