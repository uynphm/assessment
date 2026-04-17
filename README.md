# Oscar Medical Guidelines — PDF Scraper + "Initial Criteria" Tree Explorer

End-to-end pipeline that discovers Oscar Health's medical clinical guideline PDFs, downloads them, extracts the initial medical necessity criteria using an LLM, validates against a schema, and renders navigable decision trees in a React UI.

---

## Quick start

### Prerequisites
- Python 3.12+
- Node.js 20+
- PostgreSQL 17+
- Anthropic API key (or fallback LLM provider)

### 1. Create the database

```bash
psql -U postgres -c "CREATE DATABASE oscar_guidelines;"
psql -U postgres -c "CREATE USER oscar WITH PASSWORD 'oscar_dev_pw';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE oscar_guidelines TO oscar;"
psql -U postgres -d oscar_guidelines -c "GRANT ALL ON SCHEMA public TO oscar;"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY
```

### 3. Install backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python database.py           # create tables
```

### 4. Install frontend

```bash
cd frontend
npm install
```

---

## How to run

### Pipeline (CLI)

```bash
cd backend && source venv/bin/activate

python scrape.py             # discover + download all PDFs (~3 min)
python structure.py 15       # structure 15 policies with LLM
python structure.py --pdf pdfs/CG008_v11.pdf    # test a single PDF (no DB write)
```

### Pipeline (UI)

```bash
# Terminal 1: API
uvicorn api:app --reload

# Terminal 2: frontend
cd frontend
npm run dev
```

Open `http://localhost:5173`. The UI exposes the full pipeline:

- **Discover & Download** button → runs `scrape.py` in the background
- **Search** box → title + tree-content substring match
- **Filter** dropdown → all / structured / failed / not_structured / downloaded / not_downloaded
- **Checkbox + Extract Selected** → batch LLM structuring in the background
- **Click a row** → detail view with Re-extract button, collapsible tree, or error diagnostics

The UI polls `/stats` and `/scrape/status` while jobs are active so progress updates live.

---

## Which guidelines were structured

After a run, list them with:

```sql
SELECT p.id, p.title, sp.extraction_method
FROM structured_policies sp
JOIN policies p ON p.id = sp.policy_id
WHERE sp.validation_error IS NULL
ORDER BY p.id;
```

> _Populate this list after running the pipeline._

---

## Initial-only selection logic

Regex-based, with a full-text LLM fallback. No TOC dependence; works on any PDF whose text extracts cleanly.

### Step 1 — Full text extraction

`PyMuPDF (fitz)` extracts the entire document text.

### Step 2 — Regex section extraction

Iterates through ordered **start patterns** against the full text. For each match:
1. Skip if it's a TOC entry (followed by just a page number like `"... 3"`).
2. Skip if the resulting section is under 200 chars (likely a heading-only match).
3. Trim the section at the first **end pattern** match (continuation criteria, billing codes, references, etc.).
4. Accept the first match that produces a section >= 200 chars.

Start patterns (priority order):
- `Medical Necessity Criteria for Initial...`
- `Initial (authorization|approval|treatment|clinical review) Criteria`
- `Criteria for Initial (authorization|approval)`
- `Medical Necessity Criteria`
- `General Medical Necessity Criteria`
- `Clinical Indications`

End patterns:
- `Continuation (of Therapy) Criteria`
- `Subsequent Clinical Review`
- `Renewal Criteria` / `Reassessment Criteria` / `Maintenance Criteria`
- `Experimental or Investigational`
- `Not Medically Necessary`
- `Applicable Billing Codes`
- `References`

### Step 3 — Full-text LLM fallback

If no regex pattern yields a valid section, the entire document text is sent to the LLM with a system prompt explicitly instructing it to extract only initial criteria and ignore continuation/renewal sections.

### Tracking

Each policy stores `extraction_method` in the DB:
- `regex_regex_bounds` — regex matched and passed validation
- `full_text_fallback_no_valid_match` — regex failed, LLM given full document

This lets us audit extraction behavior across all policies without re-running.

### Known failure modes

1. **Non-standard phrasing** — policies using "First-time Authorization Requirements" or similar won't match any start pattern. Full-text fallback handles this.
2. **Missing end markers** — if a policy lacks all end patterns (rare), extraction runs to EOF. Over-capture is sent to the LLM, which relies on the prompt to scope.
3. **Continuation leakage in fallback** — tier 3 full-text has no boundary enforcement. Pass 2 LLM validation attempts to catch this.
4. **Pattern drift** — if Oscar changes templates, regex gets stale. The `discovery_runs` table tracks HTML snapshots to detect this upstream.

---

## Architecture

See [`SYSTEM_DESIGN.md`](./SYSTEM_DESIGN.md) for full Mermaid diagrams:
- High-level component architecture
- Per-policy data flow (sequence diagram)
- Initial-only selection decision tree
- Error handling cascade

**Tech stack:**

| Component | Choice | Why |
|-----------|--------|-----|
| Backend | FastAPI + PostgreSQL | Async support, native Pydantic, JSONB for trees |
| Frontend | React + Vite + Tailwind | Fast dev setup, no component library overhead |
| LLM | Anthropic or OpenAI (selected by env) | Both on ClarityCare's approved providers list; `ANTHROPIC_API_KEY` preferred, falls through to `OPENAI_API_KEY` |
| PDF extraction | PyMuPDF (fitz) | Fast text extraction, well-maintained |
| Scraping | httpx + BeautifulSoup | Minimal dependencies for Oscar's Next.js pages |
| Validation | Pydantic (recursive) | Catches malformed LLM output with clear errors |

---

## Non-functional guarantees

- **Rate limiting** — 0.5s between HTTP requests to Oscar
- **Download retry** — 3 attempts with exponential backoff (1s, 2s, 4s) on transient failures
- **LLM transient retry** — up to 3 attempts with exponential backoff for rate limits, timeouts, connection errors, and 429/503/529 responses
- **LLM permanent abort** — auth errors (401/403) and bad requests (400) fail the policy immediately — retrying won't help
- **LLM JSON retry** — one retry with error feedback if the LLM returns malformed JSON; then abort the policy
- **Idempotent** — all three stages (discover, download, structure) can be re-run safely
- **Error persistence** — every failure logged to DB with context
- **Completeness monitoring** — `discovery_runs` table logs count + HTML snapshot per scrape; >5% variance triggers warning
- **Content deduplication** — SHA-256 hash on every download

---

## Assessment spec

<details>
<summary>Original assessment instructions (expand)</summary>

## Oscar Medical Guidelines → PDF Scraper + "Initial Criteria" Tree Explorer (1 hour + 30 min Q/A)

### Goal
Build a small end-to-end system that:

- Discovers and downloads **all Medical guideline PDFs** linked from Oscar’s medical clinical guidelines page.
- Uses an LLM to structure **at least 10** guidelines’ **initial** medical necessity criteria into JSON decision trees like `oscar.json`.
- Persists both the scraped policy metadata and the structured tree in a database.
- Provides a UI to browse policies and clearly navigate/render the criteria tree.

Source page: [Oscar Clinical Guidelines: Medical](https://www.hioscar.com/clinical-guidelines/medical)

Example “multiple trees / initial vs continuation” policy page: [`https://www.hioscar.com/medical/cg013v11`](https://www.hioscar.com/medical/cg013v11)

Timebox: **120 minutes implementation + 30 minutes Q/A**.

---

### What you are building (high level)
Your solution must include the following components (implementation details are up to you):

- **PDF discovery**: identify every medical guideline PDF link from the source page.
- **PDF download**: download each discovered PDF and record success/failure.
- **Structuring pipeline (at least 10 guidelines)**: pick at least 10 policy PDFs, extract text, use an LLM to produce structured criteria trees, validate them, and store them.
- **UI**: list policies and render the structured criteria tree clearly.

---

### Data model requirements (minimum)
You must store at least:

- **Policies / guidelines (ALL PDFs discovered)**
  - `title` (best-effort from link text / page)
  - `pdf_url`
  - `source_page_url` (the page where the PDF was found)
  - `discovered_at`
  - Uniqueness: `pdf_url` must be unique (reruns must be idempotent)

- **Downloads (ALL PDFs)**
  - `policy_id` (or equivalent link to the policy record)
  - `stored_location` (file path or blob reference)
  - `downloaded_at`
  - `http_status` (or equivalent)
  - `error` (nullable; store failure reason)

- **Structured policies (AT LEAST 10)**
  - `policy_id` (one of the policies you chose)
  - `extracted_text` (or a reference to stored extracted text)
  - `structured_json` (the criteria tree)
  - `structured_at`
  - `llm_metadata` (model name and/or prompt; minimal is fine)
  - `validation_error` (nullable; store schema validation failures)

---

### Structured JSON format (required)
Your structured output must match the shape of `oscar.json` in this repo.

At minimum:

- Top level:
  - `title` (string)
  - `insurance_name` (string; set to `Oscar Health`)
  - `rules` (object; root node)

- `rules` node shape (recursive):
  - `rule_id` (string)
  - `rule_text` (string)
  - optional `operator` (string; `AND` or `OR`)
  - optional `rules` (array of child nodes)

Notes:

- Leaf nodes have `rule_id` + `rule_text`.
- Non-leaf nodes should include an `operator` and a `rules` array.

---

### Critical constraint: “initial only”
Some policies include:

- Separate **Initial** and **Continuation** criteria, and/or
- Multiple distinct criteria trees (e.g., multiple indications or pathways)

You must structure and store **at least 10 trees**, each representing the **initial** criteria of a different guideline.

You must:

- Implement a reasonable selection method (heuristics are allowed).
- Document your approach in your README section “Initial-only selection logic”.

If you can’t reliably detect “initial”, you may fallback to a deterministic heuristic (example: “first complete criteria tree”), but you must clearly explain it.

---

### Functional requirements (acceptance criteria)

#### A) PDF discovery (ALL)
- From the source page, discover **every** PDF link for medical guidelines.
- Store each in the DB with required metadata.
- Reruns must not duplicate existing records.

#### B) PDF download (ALL)
- Download every discovered PDF.
- Persist download outcomes (success/failure) and where the PDF is stored.
- Must include basic retry + rate limiting (lightweight is fine).

#### C) Structuring pipeline (AT LEAST 10 guidelines)
- Choose at least 10 discovered policies and structure them.
- Extract text from the PDF and feed it to an LLM.
- Validate the LLM output against the required schema.
- Store:
  - extracted text (or reference)
  - validated structured JSON
  - LLM metadata (minimum: model identifier)

#### D) UI (policy navigation + tree rendering)
- Show a list of discovered policies (at least title + PDF link).
- Indicate whether a policy has a structured tree.
- Provide a detail view for the structured policy that:
  - shows policy title + links (source and/or PDF)
  - renders the criteria as a navigable tree
  - supports expand/collapse per node (minimum)
  - clearly distinguishes operator nodes (`AND` / `OR`) from leaf criteria

---

### Non-functional requirements
- **Polite scraping**: include throttling and retries; avoid hammering the site.
- **Deterministic reruns**: discovery and download steps should be safe to re-run.
- **Error visibility**: failures should be visible in logs and persisted where relevant.

---

### Deliverables
At the end of 60 minutes, the reviewer should be able to:

- Confirm the DB contains **all** discovered PDF records from the source page.
- Confirm PDFs were downloaded (or see recorded failure reasons).
- View **at least 10** structured JSON trees stored in the DB, matching `oscar.json` shape.
- Open the UI and browse policies, and view the structured tree clearly.

Your repo must include:

- This README updated with:
  - Setup instructions (prereqs)
  - How to run: discovery, download, structuring, UI
  - Which policy you structured
  - “Initial-only selection logic” explanation
- An example environment file (`.env.example`) containing placeholders for any secrets (no real keys committed). The required LLM API key is referenced in `.env.example`.

---

### What we’ll cover in the 30-minute Q/A
- How you ensured PDF discovery completeness on the source page.
- How you handled retries, throttling, and idempotency.
- Your “initial-only” selection logic and its failure modes.
- How you validated LLM output and handled malformed JSON.
- Your UI approach to rendering large nested criteria trees.



</details>
