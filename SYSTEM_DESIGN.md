# System Design — Oscar Medical Guidelines Pipeline

## High-level architecture

```mermaid
flowchart TB
    subgraph External["External"]
        Oscar["Oscar Health site<br/>/clinical-guidelines/medical"]
        LLMAPI["LLM Provider<br/>Anthropic OR OpenAI<br/>(selected via env)"]
    end

    subgraph UI["Frontend (React + Vite + Tailwind)"]
        PoliciesTable["PoliciesTable<br/>— search<br/>— filter dropdown<br/>— checkbox select<br/>— Discover&Download btn<br/>— Extract Selected btn"]
        PolicyView["PolicyView<br/>— tree renderer<br/>— Expand/Collapse All<br/>— Extract / Re-extract<br/>— error display"]
    end

    subgraph API["Backend (FastAPI)"]
        Scrape["POST /scrape<br/>background"]
        Structure["POST /structure<br/>background"]
        ListAPI["GET /policies<br/>GET /policies/:id<br/>GET /stats<br/>GET /scrape/status"]
    end

    subgraph Pipeline["Pipeline Modules"]
        Discover["discover_policies()<br/>BeautifulSoup + httpx"]
        Resolve["resolve_pdf_urls()<br/>visit each policy page"]
        Download["download_pdfs()<br/>retry + rate limit + SHA256"]
        Extract["extract_initial_section()<br/>PyMuPDF + regex"]
        LLM["structure_with_llm()<br/>2-pass extract + validate"]
        Validate["Pydantic<br/>CriteriaTree schema"]
    end

    subgraph Storage["Storage"]
        Postgres[("PostgreSQL<br/>policies<br/>downloads<br/>structured_policies<br/>discovery_runs")]
        Disk[("Local disk<br/>backend/pdfs/<br/><i>→ S3 bucket in prod</i>")]
    end

    %% Frontend → API
    PoliciesTable -->|click Scrape| Scrape
    PoliciesTable -->|click Extract| Structure
    PoliciesTable -->|fetch + poll| ListAPI
    PolicyView -->|fetch + poll| ListAPI
    PolicyView -->|click Extract| Structure

    %% Scrape flow
    Scrape --> Discover
    Discover -->|GET| Oscar
    Discover --> Resolve
    Resolve -->|GET each page| Oscar
    Resolve --> Download
    Download -->|GET PDFs| Oscar
    Download --> Disk
    Download --> Postgres
    Discover -->|log run| Postgres

    %% Structure flow
    Structure --> Extract
    Extract --> Disk
    Extract --> LLM
    LLM -->|Pass 1 + Pass 2| LLMAPI
    LLM --> Validate
    Validate --> Postgres

    %% API reads
    ListAPI --> Postgres

    classDef external fill:#fef3c7,stroke:#d97706,color:#000
    classDef ui fill:#dbeafe,stroke:#2563eb,color:#000
    classDef api fill:#e0e7ff,stroke:#4f46e5,color:#000
    classDef pipeline fill:#dcfce7,stroke:#16a34a,color:#000
    classDef storage fill:#fce7f3,stroke:#db2777,color:#000

    class Oscar,LLMAPI external
    class PoliciesTable,PolicyView ui
    class Scrape,Structure,ListAPI api
    class Discover,Resolve,Download,Extract,LLM,Validate pipeline
    class Postgres,Disk storage
```

## Data flow per policy

```mermaid
sequenceDiagram
    participant User
    participant UI as React UI
    participant API as FastAPI
    participant BG as Background Task
    participant PDF as PyMuPDF
    participant LLM as LLM Provider<br/>(Anthropic OR OpenAI)
    participant DB as PostgreSQL

    User->>UI: click "Extract Selected"
    UI->>API: POST /structure {policy_ids}
    API->>BG: queue task
    API-->>UI: 200 {queued: [...]}

    loop For each policy_id
        BG->>DB: SELECT stored_location, title
        BG->>PDF: extract_text()
        PDF-->>BG: full text
        BG->>BG: regex_extract() finds "initial criteria" section
        BG->>LLM: Pass 1 — extract JSON from section
        LLM-->>BG: structured tree
        BG->>LLM: Pass 2 — validate against source
        LLM-->>BG: corrected tree
        BG->>BG: Pydantic validate schema
        BG->>DB: INSERT structured_policies
    end

    loop Every 3s while jobs active
        UI->>API: GET /stats
        API->>DB: COUNT + GROUP BY
        API-->>UI: {successful, failed, active_jobs}
        UI->>UI: update status badges
    end
```

## Initial-only selection decision tree

```mermaid
flowchart TD
    Start["PDF downloaded"] --> Extract["PyMuPDF: full text"]
    Extract --> Regex["Regex: find start pattern<br/>— Medical Necessity Criteria for Initial<br/>— Initial Authorization Criteria<br/>— General Medical Necessity Criteria<br/>— etc."]

    Regex --> Found{"Match found?"}
    Found -->|No match| Fallback["Full text → LLM<br/>extraction_method: full_text_fallback"]

    Found -->|Match| TOCCheck{"Is it a TOC entry?<br/>followed by page number"}
    TOCCheck -->|Yes| NextMatch["Skip, try next match"]
    NextMatch --> Found

    TOCCheck -->|No| LenCheck{"Section >= 200 chars?"}
    LenCheck -->|Too short| NextMatch
    LenCheck -->|OK| EndMarker["Trim at end marker<br/>— Continuation Criteria<br/>— Subsequent Clinical Review<br/>— Experimental<br/>— References"]

    EndMarker --> Section["Section → LLM<br/>extraction_method: regex_regex_bounds"]

    Section --> Pass1["Pass 1: extract JSON"]
    Fallback --> Pass1
    Pass1 --> Pass2["Pass 2: validate vs source"]
    Pass2 --> Pydantic["Pydantic schema validation"]
    Pydantic --> Store["Store in structured_policies"]

    classDef decision fill:#fef3c7,stroke:#d97706
    classDef action fill:#dcfce7,stroke:#16a34a
    classDef terminal fill:#dbeafe,stroke:#2563eb

    class Found,TOCCheck,LenCheck decision
    class Extract,Regex,NextMatch,EndMarker,Section,Fallback,Pass1,Pass2,Pydantic action
    class Start,Store terminal
```

## Error handling cascade

Single provider (Anthropic or OpenAI, selected at startup). Errors are classified into two types; retry policy depends on the type.

```mermaid
flowchart TD
    Call["call_llm(system, user)"] --> Provider["PROVIDER = 'anthropic' or 'openai'<br/>(chosen at startup from env)"]

    Provider --> Invoke["Invoke provider SDK"]

    Invoke --> Success{"Response OK?"}
    Success -->|Yes| Parse["parse_json_response()"]

    Parse --> ValidJSON{"Valid JSON?"}
    ValidJSON -->|Yes| Done["Return JSON"]
    ValidJSON -->|No| JSONRetry["Re-prompt with error feedback<br/>(1 retry)"]
    JSONRetry --> Invoke

    Success -->|No| Classify{"Error type?"}

    Classify -->|ConnectionError / Timeout<br/>RateLimit / 429 / 503 / 529| Transient["TransientLLMError"]
    Classify -->|AuthError / 401 / 403<br/>BadRequest / 400| Permanent["PermanentLLMError"]

    Transient --> Backoff["Retry with exponential backoff<br/>1s, 2s, 4s (max 3 attempts)"]
    Backoff --> Invoke
    Backoff -->|Exhausted| LogTransient["Log: pass_transient_exhausted<br/>abort this policy"]

    Permanent --> LogPermanent["Log: pass_permanent<br/>abort this policy"]

    LogTransient --> Store["DB: structured_policies<br/>validation_error + llm_metadata.errors[]"]
    LogPermanent --> Store

    classDef error fill:#fee2e2,stroke:#dc2626,color:#000
    classDef action fill:#dcfce7,stroke:#16a34a,color:#000
    classDef decision fill:#fef3c7,stroke:#d97706,color:#000
    classDef terminal fill:#dbeafe,stroke:#2563eb,color:#000

    class Transient,Permanent,LogTransient,LogPermanent error
    class Invoke,Parse,JSONRetry,Backoff action
    class Success,ValidJSON,Classify decision
    class Done,Store terminal
```

### Classification rules

| Provider error | Type | Retry? |
|----------------|------|--------|
| `APIConnectionError` | Transient | ✓ exp backoff (1s, 2s, 4s) |
| `APITimeoutError` | Transient | ✓ |
| `RateLimitError` | Transient | ✓ |
| Status 429, 503, 529 | Transient | ✓ |
| `AuthenticationError` / 401, 403 | Permanent | ✗ abort |
| Status 400 / bad request | Permanent | ✗ abort |
| Any other exception | Raised to caller | — |

### JSON-level retry

Separate from error retries. If the LLM returns malformed JSON:
- **Retry once** with the parse error appended to the prompt
- If second attempt also fails, log `passN_invalid_json` and abort the policy

### Pass 2 downgrade

If Pass 2 (validation pass) hits any terminal error, falls back to Pass 1's result rather than losing the extraction. Logged as `pass2_..._using_pass1` in metadata.
