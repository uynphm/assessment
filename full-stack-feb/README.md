## Oscar Medical Guidelines → PDF Scraper + “Initial Criteria” Tree Explorer (1 hour + 30 min Q/A)

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


