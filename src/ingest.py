import sys
import extract_msg
from pathlib import Path
import re
import hashlib
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import store
import json

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# How close 2 same-subject emails' timestamp must be to be considered part 
# of the same business matter. Not specified anyhere authoritative - this is a judgement baked
# into the name constant so it visisble and tunable 
SUBJECT_MATCH_WINDOW_DAYS = 14

# Reply/Forward Prefix to strip before comparing subjects, across every language this corpus supports
# Ignorecase only affect latin
# Letters (RE, FW, FWD, R, I) - it has no effect on the CJK characters which dont have case to ignore
SUBJECT_PREFIX = re.compile(
    r"^\s*(RE|FW|FWD|R|I|回复|答复|转发|返信|転送)\s*[:：]\s*", re.IGNORECASE
)

# A loose email-address matcher, used to pull real address out of Outlook's
# Display name <address> formatted To/Cc/sender strings
EMAIL_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w-]+")

# When Outlook embeds a quoted historical message inside a reply/foreard body.
# it precedes it with a lableel block like:
#   发件人: ...
#   发送时间: ...
#   主题: ...
# These are the label words for "From", "Sent", and "Subject" in each
# language this corpus is expected to contain (Chinese, English, Italian,
# Japanese). We only need the "From" label to *find* each embedded message;
# Sent/Subject areparsed afterwards once we already know where it starts
FROM_LABELS = ["发件人", "From", "Da", "差出人", "送信者"]
SENT_LABELS = ["发送时间", "Sent", "Inviato", "送信日時"]
SUBJECT_LABELS = ["主题", "Subject", "Oggetto", "件名"]

# To/Cc-equivalent lables are not stored ont he segment, but e still need to recognize them
# as "yes, this is still part of the header block" so the header parsing loop doesnt stop
# early when it hits a To/Cc label
KNOWN_LABELS = FROM_LABELS + SENT_LABELS + SUBJECT_LABELS + [
    "收件人", "To", "A", "抄送", "Cc", "CC", "送信者"
]

# Find every line that starts with a new embedded historical message. Built from
# FROM_LABELS so adding a language later only means editting one list. 
# body = """Hi please see below:

# 发件人: 王经理 <wang@company.cn>
# 发送时间: 2026年9月15日
# 主题: Packaging declaration

# Please confirm receipt.
# From: John Smith <john@example.co.uk>
# Sent: 15 September 2026 14:22
# Subject: Re: Packaging declaration

# Original chain..."""

# for m in FROM_LINE.finditer(body):
#     print(repr(m.group(1)))

# '王经理 <wang@company.cn>'
# 'John Smith <john@example.co.uk>'
FROM_LINE = re.compile(
    r"^[ \t]*(?:" + "|".join(re.escape(l) for l in FROM_LABELS) + r")[:：][ \t]*(.*)$",
    re.MULTILINE,
)

def _label_value(line: str, labels: list[str]) -> str | None:
    """
    if  'line' starts with any of the 'labels' followed by a colon, 
    return the rest of the value after the colon, Plain string matching, not regex  - 
    by the time this is called, we already inside a small located chunk, so a full regex is not necessary

    examples:
                    # Input                 # labels param           # Output
    _label_value("  主题: 包装声明  ",           SUBJECT_LABELS)       # → "包装声明"
    _label_value("Sent: 2026-09-15 10:30\t",    SENT_LABELS)          # → "2026-09-15 10:30"
    _label_value("发件人：John <j@x.com>",       FROM_LABELS)          # → "John <j@x.com>"   (uses full-width ：)
    _label_value("To: team@x.com",              SENT_LABELS)          # → None  ("To" not in SENT_LABELS)
    _label_value("random body text",            FROM_LABELS)          # → None
    _label_value("主题 : missing space",         SUBJECT_LABELS)       # → None  (space before colon breaks it — by design, strict)
    """
    for label in labels:
        stripped = line.strip()
        if stripped.startswith(label + ":"):
            return stripped[len(label) + 1:].strip()
        if stripped.startswith(label + "："):
            return stripped[len(label) + 1:].strip()
    return None

def _is_known_label_line(line: str) -> bool:
    """
    is the line *any* recognized header label(From/Sent/Subject/To/Cc in any language)? used to decide
    "are we still inside the header block or has the actual quoted message body started?"

    examples:
    _is_known_label_line("  收件人: 客户列表  ")  # → True   (收件人 = "To" in Chinese)
    _is_known_label_line("Sent: 2026-09-15")      # → True
    _is_known_label_line("Cc: boss@x.com")        # → True
    _is_known_label_line("Hi Team,")              # → False  → parser knows "body begins"
    _is_known_label_line("主题 : spaced colon")   # → False  (same strictness as above)
    """
    stripped = line.strip()
    return any(stripped.startswith(label + ":") or stripped.startswith(label + "：") for label in KNOWN_LABELS)

def normalize_subject(subject: str) -> str:
    """
    remove reply/forward prefixes, repeatedly - a double-replied email can look like "RE: RE: FWD: original subject", 
    so we loop until a pass makes no further change rather than stripping just once

    examples:
    normalize_subject("RE: RE: FWD: original subject")  # → "original subject"
    normalize_subject("Packaging declaration")          # → "Packaging declaration"
    normalize_subject("  RE:  FWD: 回复：转发：Packaging declaration  ")  # → "Packaging declaration"
    # pass 1: SUB_PREFIX strips first match "  RE:  "
    #         → "FWD: 回复：转发：Packaging declaration"
    # pass 2: strips "FWD: "
    #         → "回复：转发：Packaging declaration"
    # pass 3: strips "回复："
    #         → "转发：Packaging declaration"
    # pass 4: strips "转发："
    #         → "Packaging declaration"
    # pass 5: no change, exit
    # → "Packaging declaration"
    """
    subject = (subject or "").strip()
    previous = None
    while previous != subject:
        previous = subject
        subject = SUBJECT_PREFIX.sub("", subject).strip()
    return subject


def split_segments(body: str, envelope_author: str) -> list[dict]:
    """
    Outlook embeds the entire reply chain as quoted text inside one body.
    This turns that one block of text into a list of segments: segment 0 is the actual new message text (what
    this email's author wrote), and each later segment is one quoted historical message, nearest-first.

    example:
      body = "Thanks, see below.\n\nFrom: John <j@x.com>\nSent: 2026-09-30\nSubject: RE: Quote\n\nHi, here's the quote."
      split_segments(body, "Mary <m@x.com>")
      # [
      #   {"idx":0, "author":"Mary <m@x.com>",       "body":"Thanks, see below.", "is_latest":True},
      #   {"idx":1, "author":"John <j@x.com>",       "body":"Hi, here's the quote.", "subject":"Quote", "sent_at_text":"2026-09-30", "is_latest":False}
      # ]
    """

    body = body or ""
    matches = list(FROM_LINE.finditer(body))
    if not matches:
        # no embedded history at all - the body is just a single paragraph
        return [{
            "idx": 0,
            "author": envelope_author,
            "sent_at_text": None,
            "subject": None,
            "body": body.strip(),
            "is_latest": True,
            
        }]
    # Everything before the first "From:" line is the latest, unquoted message body
    segments = [{
        "idx": 0,
        "author": envelope_author,
        "sent_at_text": None,
        "subject": None,
        "body": body[:matches[0].start()].strip(),
        "is_latest": True,
    }]

    # Each match marks here one embedded message starts. Slice the body from
    # this match to the next match (or end of body) - that slice is "header
    # block + quoted message block" for exactly one historical message body.
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end].strip()
        lines = chunk.splitlines()
       
        # line 0 is the "From:" line
        author = _label_value(lines[0], FROM_LABELS)
        sent_at_text = None
        subject = None
        header_end_line = 1

        # Walk through the header block (From/Sent/Subject/To/Cc) 
        # in whatever order and combination this paticular email used
        # until we hit a blank line - that is where the actual quoted message body starts
        # text begins. capped at 12 lines as a safety net against a malformed block that
        # never has a blank line
        for j in range(1, min(len(lines), 12)):
            line = lines[j]
            # Outlook sometimes pad blank seperator lines  with a non breaking space
            # instead of being truly empty - normalize it
            normalized = line.replace("\xa0", "").strip()
            if normalized == "":
                header_end_line = j + 1
                break
            sent_val = _label_value(line, SENT_LABELS)
            if sent_val is not None:
                sent_at_text = sent_val
            subj_val = _label_value(line, SUBJECT_LABELS)
            if subj_val is not None:
                subject = subj_val
            if not _is_known_label_line(line):
                # Hit a line that isnt bland and isnt a recognized label -
                # then header block must already be over (no blank line was used as a seperator in this paticular email body)
                header_end_line = j
                break
            header_end_line = j + 1
        segment_body = "\n".join(lines[header_end_line:]).strip()
        segments.append({
            "idx": i + 1,
            "author": author,
            "sent_at_text": sent_at_text,
            "subject": normalize_subject(subject),
            "body": segment_body,
            "is_latest": False,
        })
    return segments
    
    
@dataclass
class ParsedMessage:
    filename: str
    parse_status: str
    parse_error: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list = field(default_factory=list)
    content_hash: str | None = None
    subject: str | None = None
    sender: str | None = None
    to_addrs: list = field(default_factory=list)
    cc_addrs: list = field(default_factory=list)
    sent_at: str | None = None
    body: str | None = None
    attachments: list = field(default_factory=list)
    segments: list = field(default_factory=list)

    @property
    def id(self) -> str:
        identity = self.message_id or f"file:{self.filename}"
        return hashlib.sha1(identity.encode("utf-8")).hexdigest()
    
def parse_msg_file(path: Path) -> ParsedMessage:
    filename = path.name
    if path.stat().st_size == 0:
        return ParsedMessage(filename=filename, parse_status="empty", parse_error="Empty file - (O bytes)")
        
    # open the actual message file
    try:
        msg = extract_msg.openMsg(path)
    except Exception as e:
        return ParsedMessage(filename=filename, parse_status="corrupt", parse_error=str(e))
        
    try:
        subject = msg.subject or ""
        sender = msg.sender or ""
        to_addrs = EMAIL_ADDRESS_RE.findall(msg.to or "")
        cc_addrs = EMAIL_ADDRESS_RE.findall(msg.cc or "")
        sent_at = msg.date.isoformat() if msg.date else None
        body_raw = msg.body or ""
        message_id = msg.messageId
        in_reply_to = msg.inReplyTo or None
        references = []
        try: 
            raw_refs = msg.header.get("References", "") if msg.header else None
            if raw_refs is not None:
                references = re.findall(r"<[^>]+>", raw_refs)
        except Exception as e:
            references = []
            
        attachments = []
        for a in msg.attachments:
            try:
                name = a.longFilename or a.shortFilename or "unamed"
                size = len(a.data) if a.data else 0
                attachments.append({ "name": name, "size": size })
            except Exception as e:
                attachments.append({ "name": "unreadable", "size": None, "error": str(e) })

        content_hash = hashlib.sha256(
            f"{subject.strip()}\n{body_raw.strip()}".encode("utf-8")
        ).hexdigest()
        parsed = ParsedMessage(
            filename=filename,
            parse_status="ok", message_id=message_id, in_reply_to=in_reply_to,
            references=references, content_hash=content_hash, subject=subject,
            sender=sender, to_addrs=to_addrs, cc_addrs=cc_addrs,
            sent_at=sent_at, body=body_raw, attachments=attachments,
        )
        parsed.segments = split_segments(body_raw, sender)
        return parsed
    except Exception as e:
        return ParsedMessage(filename=filename, parse_status="corrupt", parse_error=str(e))
    finally:
        msg.close()

class DisJointSet:
    """
        Classic union-find: let us merge the messages into a group pairwise(A joins B, B joins C) and then
        read back the final groups at all once, without having to track "which group is X in" by hand as merges happens
    """
    def __init__(self):
        self.parent = {}

    def add(self, item):
        self.parent.setdefault(item, item)

    def find(self, item):
        p = self.parent[item]
        if p == item:
            return item
        root = self.find(p)
        # path compression - speed up future find operations
        self.parent[item] = root 
        return root
    
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb
        
    def groups(self):
        out = {}
        for item in self.parent:
            root = self.find(item)
            out.setdefault(root, []).append(item)
        return out

def _participants(msg: ParsedMessage) -> set:
    """
        All emails addresses involved in the conversation (send + to + cc), lowercase for comparison
    """
    addrs = set(to.lower() for to in msg.to_addrs) | set(cc.lower() for cc in msg.cc_addrs)
    sender_match = EMAIL_ADDRESS_RE.search(msg.sender or "")
    if sender_match is not None:
        addrs.add(sender_match.group(0).lower())
    return addrs

def _date_close(a_iso: str | None, b_iso: str | None) -> bool:
    """
        Check if two dates are close enough to be considered the same day
    """
    if not a_iso or not b_iso:
        return False
    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
    except ValueError:
        return False
    return abs(a - b) <= timedelta(days=SUBJECT_MATCH_WINDOW_DAYS)

def _group_messages(messages: list) -> list:
    """
        Three Passes, each one merging hatever the previous pass didnt already catch
        Orders matter: the strongest, most-certain evidence goes first, so by the time e reach the "conservative" pass 
        anything that as already prperly linked is already executed from needing a guess. 
    """
    ok = [m for m in messages if m.parse_status == "ok"]
    unreadable = [m for m in messages if m.parse_status != "ok"]

    by_msgid = {m.message_id: m for m in ok if m.message_id}
    dsu = DisJointSet()
    for msg in ok:
        dsu.add(msg.id)

    joined_by_reference = set()
    for m in ok:
        linked_ids = ([m.in_reply_to] if m.in_reply_to else []) + m.references
        for ref in linked_ids:
            target = by_msgid.get(ref)
            if target is not None and target.id != m.id and (m.id, target.id) not in joined_by_reference:
                dsu.union(m.id, target.id)
                joined_by_reference.add((m.id, target.id))
    
    by_hash: dict = {}
    for m in ok:
        by_hash.setdefault(m.content_hash, []).append(m)
    joined_by_hash = set()
    for dupes in by_hash.values():
        for i in range(1, len(dupes)):
            dsu.union(dupes[0].id, dupes[i].id)
            joined_by_hash.add(frozenset([dupes[0].id, dupes[i].id]))
    
    normalized = {m.id: normalize_subject(m.subject) for m in ok}
    by_subject: dict = {}
    for m in ok:
        by_subject.setdefault(normalized[m.id], []).append(m)
    uncertainty_notes: dict = {}

    for subject_key, candidates in by_subject.items():
        if not subject_key or len(candidates) < 2:
            continue
        for i in range(len(candidates)):
            for j in range(i+1, len(candidates)):
                a, b = candidates[i], candidates[j]
                if dsu.find(a.id) == dsu.find(b.id):
                    continue
                overlap = _participants(a) & _participants(b)
                close_in_time = _date_close(a.sent_at, b.sent_at)
                if overlap and close_in_time:
                    dsu.union(a.id, b.id)
                else:
                    reason = "no shared participant" if not overlap else "dates too far aparts"
                    uncertainty_notes[a.id] = f"same normalized subject as {b.filename} nut not merged ({reason})"
                    uncertainty_notes[b.id] = f"same normalized subject as {a.filename} nut not merged ({reason})"
    groups = []
    for member_ids in dsu.groups().values():
        members = [m for m in ok if m.id in member_ids]
        if len(members) == 1:
            basis, confidence = "single_message", "exact"
        elif any(frozenset((a.id, b.id)) in joined_by_reference for a in members for b in members):
            basis, confidence = "reference", "exact"
        elif any(frozenset((a.id, b.id)) in joined_by_hash for a in members for b in members):
            basis, confidence = "duplicate_hash", "exact"
        else:
            basis, confidence = "subject_participants_time", "conservative"
        note = next((uncertainty_notes[m.id] for m in members if m.id in uncertainty_notes), None)
        groups.append({
            "message_ids": [m.id for m in members],
            "filenames": [m.filename for m in members],
            "basis": basis,
            "confidence": confidence,
            "uncertainty": note,
            "normalized_subject": normalize_subject(members[0].subject) if members else None
        })

    for m in unreadable:
        groups.append({
            "message_ids": [m.id],
            "filenames": [m.filename],
            "basis": "single_message",
            "confidence": "exact",
            "uncertainty": None,
            "normalized_subject": ""
        })
    return groups

def run_ingest(emails_dir: str, db_path: str) -> dict:
    paths = sorted(Path(emails_dir).glob("*.msg"))

    conn = store.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()


    parsed = [parse_msg_file(path) for path in paths]
    groups = _group_messages(parsed)
    for m in parsed:
        store.upsert_message(conn,  {
            "id": m.id, "thread_id": None, "filename": m.filename, "parse_status": m.parse_status, 
            "parse_error": m.parse_error, "message_id": m.message_id, "in_reply_to": m.in_reply_to,
            "references_list": "\n".join(m.references) if m.references else None, "content_hash": m.content_hash, "duplicate_of": None,
            "subject": m.subject, "sender": m.sender, "to_addrs": json.dumps(m.to_addrs), 
            "cc_addrs": json.dumps(m.cc_addrs), "sent_at": m.sent_at, "body_raw": m.body, 
            "attachments": json.dumps(m.attachments),
        })
        store.delete_segments_for_message(conn, m.id)
        for seg in m.segments:
            store.insert_segment(conn, {
                "id": hashlib.sha1(f"{m.id}:{seg['idx']}".encode()).hexdigest(),
                "message_id": m.id, **seg,
            })

        if m.parse_status != "ok":
            print(f"{m.filename}: {m.parse_status}" + (f"({m.parse_error if m.parse_error else ''})"))
        if m.parse_status == "ok":
            print(f"  subject={m.subject!r} sender={m.sender!r} segment={len(m.segments)}")
    print()
    existing_threads = {t["id"]: t for t in store.list_active_threads(conn)}
    existing_members: dict = {}
    for t in existing_threads.values():
        for msg_row in store.list_messages_by_thread(conn, t["id"]):
            existing_members.setdefault(t["id"], set()).add(msg_row["id"])
    
    for g in groups:
        print(f"thread [{g['basis']}/{g['confidence']}]: {g['filenames']}" + 
        (f"   -- uncertainty: {g['uncertainty']}" if g['uncertainty'] is not None else ""))
        members_ids = sorted(g["message_ids"])
        thread_id = hashlib.sha1("|".join(members_ids).encode()).hexdigest()

        overlapping_old = [
            tid for tid, members in existing_members.items()
            if tid != thread_id and members & set(members_ids)
        ]
        for old_id in overlapping_old:
            store.supersede_thread(conn, old_id, now)
            store.supersede_judgements_for_thread(conn, old_id)

        store.upsert_thread(conn, thread_id, g["normalized_subject"], g["basis"], g["confidence"], g['uncertainty'])
        for mid in members_ids:
            store.set_message_thread(conn, mid, thread_id)

    conn.commit()

    return {
        "messages": len(parsed),
        "ok": sum(1 for m in parsed if m.parse_status == "ok"),
        "empty": sum(1 for m in parsed if m.parse_status == "empty"),
        "corrupt": sum(1 for m in parsed if m.parse_status == "corrupt"),
        "threads": len(groups),
    }

if __name__ == "__main__":
    import sys
    import os
    from dotenv import load_dotenv
    load_dotenv()

    emails_dir = sys.argv[1] if len(sys.argv) > 1 else "emails"
    db_path = str(Path(__file__).resolve().parent.parent / "data" / "mailing.db")
    model = os.getenv("ANTHROPIC_MODEL", "MiniMax-M3")
    
    print(f"ingesting {emails_dir}")
    print(run_ingest(emails_dir, db_path))