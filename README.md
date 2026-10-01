# mail-ingest

Executive email triage system for Yarns & Colors Co., Ltd.

Pipeline:

1. **ingest** — parses Outlook `.msg` files (extracts subject, sender, recipients, body segments), groups
   related messages into threads, writes to a local SQLite database.
2. **judge** — calls an LLM (configured via `.env`) to produce a draft priority judgement per thread
   (1 Immediate / 2 Action Today / 3 Reference / 4 Filter) with verbatim evidence spans traced back to
   specific message segments.
3. **review** (Streamlit) — human-facing UI at `src/index.py`. Lists threads by priority bucket, shows
   highlighted evidence inline, stores human corrections in a separate `correction` table so the
   original LLM judgement remains auditable.
4. **export** — `src/export_evalutation.py` produces a JSONL file of every thread with its original
   judgement, corrections, the effective (merged) judgement, and all traceable evidence spans.

```
mail-ingest/
├── src/
│   ├── ingest.py              # Step 1: .msg → DB
│   ├── judge.py               # Step 2: DB threads → LLM judgements
│   ├── llm_client.py          # Anthropic-compatible client with retry + fallback JSON parsing
│   ├── index.py               # Step 3: Streamlit review app (main entry: streamlit run src/index.py)
│   ├── export_evalutation.py  # Step 4: DB → JSONL export
│   ├── app.py                 # Convenience: runs ingest + judge + export in one process
│   └── store.py               # SQLite schema + all queries
├── data/                      # mailing.db + exports (gitignored — contains emails/secrets)
├── Emails/                    # Source .msg files (gitignored)
├── .env                       # API keys + paths (gitignored)
├── .gitignore
├── .streamlit/config.toml     # Streamlit theme + entrypoint
├── packages.txt               # System deps for Streamlit Community Cloud
└── requirements.txt           # Python deps
```

## Quick start

```bash
python3 -m venv .venv
.venv\Scripts\activate          # Windows:  source .venv/bin/activate  (Linux/macOS)
pip install -r requirements.txt

# Copy .env.example or create .env manually:
#   ANTHROPIC_API_KEY=...
#   ANTHROPIC_MODEL=...
#   ANTHROPIC_BASE_URL=...
#   DB_PATH=data/mailing.db
#   EMAILS_DIR=Emails

# Run the full pipeline:
python3 src/app.py path/to/your/Emails

# Launch the human review UI:
streamlit run src/index.py
```

## Deployment

The review app is deployable two ways:

- **Streamlit Community Cloud** — free shared tier. Push the repo, point the dashboard at
  `src/index.py` on `main`, paste env vars into the Secrets box.
- **Self-hosted** (recommended for confidential corporate email) via systemd or Docker on a VPS
  behind Nginx + Certbot.

See `.gitignore` for what's intentionally never committed: `.env`, `.venv/`, `data/*.db` (contains
emails and LLM judgements), and `Emails/` (raw source `.msg` files).
