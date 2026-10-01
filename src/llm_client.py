import json
import os

import anthropic

JUDGEMENT_TOOL_NAME = "record_judgement"

JUDGEMENT_TOOL_SCHEMA = {
    "type": "object",
    "required": ["priority", "reason", "uncertainty", "evidence", "alert"],
    "properties": {
        "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
        "reason": {"type": "string"},
        "uncertainty": {"type": ["string", "null"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["message_ref", "source_field", "segment_index", "quote", "field"],
                "properties": {
                    "message_ref": {"type": "string", "description": "the msg_N ref this quote comes from"},
                    "source_field": {
                        "type": "string",
                        "enum": ["body", "subject", "sender", "to", "cc", "sent_at", "attachment_name"],
                    },
                    "segment_index": {
                        "type": ["integer", "null"],
                        "description": "set when source_field is body; which segment the quote is from",
                    },
                    "quote": {"type": "string", "description": "verbatim substring of that source"},
                    "field": {
                        "type": "string",
                        "enum": ["priority", "who", "what", "why", "action", "deadline", "reason"],
                    },
                },
            },
        },
        "alert": {
            "type": ["object", "null"],
            "properties": {
                "who": {"type": "string"},
                "what": {"type": "string"},
                "why": {"type": "string"},
                "action": {"type": "string"},
                "deadline": {"type": ["string", "null"]},
            },
        },
    },
}

SYSTEM_PROMPT = """You are the Judge component of an executive email triage system for Yarns & Colors Co., Ltd. \
The executive is Jacky. You receive one already-grouped thread (a business matter that may span several source \
messages) and return a single draft judgement.

Priority is about what Jacky must do with this matter. One thread receives one priority:
1 Immediate Attention - material harm, a customer commitment, or an escalation if Jacky does not see it now
2 Action / Decision Required Today - a decision or reply is required from Jacky, with an owner and a deadline \
when the mail states one
3 Reference / Observation - useful context, no action from Jacky
4 Filter / No Executive Attention - sales blasts, mass mail, system notices

A senior sender, the word "urgent", or Jacky being on Cc does not by itself raise the priority. Judge by what the \
thread actually says is happening and what it asks Jacky to do.

Threads may be in English, Chinese, Italian, or Japanese. Read them in their original language. Alert prose (who, \
what, why, action, deadline) is written in English; quotes in evidence are verbatim text in the original language.

Every important claim - the priority itself, and every alert field - must be backed by at least one evidence \
entry naming the exact message (by its msg_N ref), the source field, and a verbatim quote copied from that \
field or segment. Do not invent a quote; do not paraphrase into a quote. If the thread does not clearly support \
a fact, do not assert it - put it in "uncertainty" instead and leave the related alert field out of your answer \
or leave uncertainty non-null explaining the gap.

Only produce alert fields when priority is 1 or 2. Set alert to null for priority 3 or 4. "why" must be either a \
quoted consequence, or a reasoned inference that still names the quotes it draws on - never an unsupported \
assertion. Deadline should only be filled when the thread states a time; otherwise leave it null.

Judge the thread as of its own latest message's sent date, not as of today - a March thread is not urgent just \
because it is being reviewed later.

Call record_judgement exactly once."""

def _client() -> anthropic.Client:
    api_key=os.getenv("ANTHROPIC_API_KEY")
    base_url=os.getenv("ANTHROPIC_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL must be set in .env")
    
    return anthropic.Client(api_key=api_key, base_url=base_url)

def judge_thread(packet: dict, model: str = os.getenv("ANTHROPIC_MODEL")) -> dict:
    client = _client()
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        tools=[{
            "name": JUDGEMENT_TOOL_NAME,
            "description": "Record the draft judgement for the thread",
            "input_schema": JUDGEMENT_TOOL_SCHEMA,
        }],
        extra_body={"reasoning_split": True},
        tool_choice={"type": "tool", "name": JUDGEMENT_TOOL_NAME},
        messages=[{"role": "user", "content": json.dumps(packet, ensure_ascii=False, indent=2)}]
    )
    tool_use =next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("LLM Model did not return a tool_use block with the judgement.")
    return tool_use.input 
