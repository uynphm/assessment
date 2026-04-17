"""
Structuring pipeline using Anthropic SDK:
1. PyMuPDF extracts TOC + text from PDF
2. 3-tier section detection via TOC
3. Claude extracts JSON from section (Pass 1)
4. Claude validates JSON against full text (Pass 2)
5. Pydantic schema validation
6. Store result or log error
"""

import os
import re
import json
import time
import fitz
from pydantic import BaseModel, ValidationError
from typing import Optional
from dotenv import load_dotenv
from database import get_dict_conn as get_conn, init_db

# LLM SDKs — both optional, loaded if their API key is set
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from openai import OpenAI, APIConnectionError as OpenAIConnError, APITimeoutError as OpenAITimeoutError, RateLimitError as OpenAIRateLimitError, AuthenticationError as OpenAIAuthError, APIStatusError as OpenAIStatusError
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BATCH_SIZE = 115
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_SECTION_CHARS = 15000
MAX_FULL_TEXT_CHARS = 45000
MAX_LLM_RETRIES = 3  # exponential backoff retries for transient errors

SYSTEM_PROMPT = """<role>
You are an expert Clinical Logic Extraction System.
</role>

<task>
Your task is to analyze the "medical necessity criteria" section of a clinical guideline PDF and extract EVERY rule, sub-rule, and condition into a strict JSON decision tree.
</task>

<definitions>
- INITIAL CRITERIA: The explicit medical prerequisites a patient must meet to receive FIRST-TIME approval/authorization for a new treatment, device, or diagnostic procedure. This strictly excludes maintaining coverage, extending expiring therapy, replacing broken equipment, or general contraindications.
</definitions>

<output_schema>
The output MUST have a SINGLE ROOT node at "rules". If the source document has multiple independent top-level criteria blocks, wrap them under a common parent node with an appropriate operator (AND if all must be met, OR if any indication suffices).

{
    "title": "Medical Necessity Criteria for [Process]",
    "insurance_name": "Oscar Health",
    "rules": {
        "rule_id": "1",
        "rule_text": "Procedures are considered medically necessary when ALL of the following criteria are met",
        "operator": "AND",
        "rules": [
            { "rule_id": "1.1", "rule_text": "Informed consent with explanation of risks, benefits, and alternatives" },
            {
                "rule_id": "1.2",
                "rule_text": "Adult aged 18 years or older with documentation of",
                "operator": "OR",
                "rules": [
                    { "rule_id": "1.2.1", "rule_text": "Body mass index (BMI) \u226540" },
                    {
                        "rule_id": "1.2.2",
                        "rule_text": "BMI \u226535 with ONE of the following comorbidities",
                        "operator": "OR",
                        "rules": [
                            { "rule_id": "1.2.2.1", "rule_text": "Clinically significant cardio-pulmonary disease" },
                            { "rule_id": "1.2.2.2", "rule_text": "Type 2 diabetes mellitus" }
                        ]
                    }
                ]
            }
        ]
    }
}
</output_schema>

<extraction_rules>
1. TITLE CONSTANT: The "title" key MUST always follow the format "Medical Necessity Criteria for [Process]". Never deviate from this title structure.
2. NUMBERING: Use hierarchical dot-notation with ONLY numeric segments (1, 1.1, 1.1.1, etc.). NEVER use letters or words.
3. FLAT LIST STRUCTURE: When a rule says "with ONE of the following", the listed items must be DIRECT children. Do NOT create intermediate wrapper nodes.
4. OPERATORS:
   - Assign "AND" if the parent text says "ALL of the following" or if list items end in "; and".
   - Assign "OR" if the parent text says "ONE of the following" or if list items end in "; or".
   - LEAF NODES have NO "operator" and NO "rules" array.
5. COMPLETENESS: Capture EVERY numbered/lettered item. Keep lists complete.
6. SCOPE STRICTNESS: Extract strictly the rules required for the FIRST-TIME authorization of the procedure (Initial Criteria). Explicitly EXCLUDE rules related to "continuation of therapy", "renewals", "device replacement", "contraindications", "definitions", and "experimental/investigational" sections. Do NOT extract them.
</extraction_rules>

<text_fidelity_rules>
- NO PARAPHRASING: You MUST NOT paraphrase the original medical terminology.
- SHORTEN ONLY: You may only shorten the text by dropping long explanatory clauses. Do NOT change the words you keep.
   - GOOD: "Diagnostic polysomnography (PSG) showing \u22655 obstructive events per hour"
   - BAD: "Sleep study" (Paraphrasing and losing critical threshold data)
   - GOOD: "Device is consistently used on at least 4 nights per week"
   - BAD: "Device is consistently used on at least 4 nights per week to ensure compliance with the initial treatment plan..." (Too long, drop the explanation)
</text_fidelity_rules>

<output_instructions>
Output ONLY valid JSON. Output your data enclosed inside <extracted_json> tags. Do not produce markdown code fencing.
</output_instructions>"""

VALIDATION_SYSTEM_PROMPT = """<role>
You are a QA specialist for structured insurance policy data.
</role>

<task>
You will receive the original criteria text and a JSON rule tree extracted from it. Your job is to audit the JSON, fix missing/excess elements, and ensure it follows the strict schema format.
</task>

<definitions>
- INITIAL CRITERIA: The explicit medical prerequisites a patient must meet to receive FIRST-TIME approval/authorization for a new treatment, device, or diagnostic procedure. This strictly excludes maintaining coverage, extending expiring therapy, replacing broken equipment, or general contraindications.
</definitions>

<validation_tasks>
Compare the input JSON to the source text. Check for:
- TITLE: Ensure the title identically matches "Medical Necessity Criteria for [Process]".
- MISSING CRITERIA: Cross-reference the JSON against the full source text. If the Pass 1 extractor accidentally skipped, truncated, or summarized any list items or nested conditions for initial authorization, you MUST re-inject them into the JSON tree.
- EXCESS CRITERIA: Cross-reference the JSON against the full source text. Aggressively delete ANY hallucinated rules that don't exist in the text. Furthermore, delete ANY rules that deal with "device replacements", "therapy continuations", or "renewals"\u2014ensure the tree strictly contains ONLY first-time authorization criteria.
- INCORRECT OPERATORS: Ensure AND / OR accurately align with the source text ("ALL of" vs "ONE of").
- TEXT FIDELITY: Ensure the rule_text is NOT paraphrased. It should be exact source terminology, shortened to a concise heading.
- ID INTEGRITY: Confirm strict numeric dot-notation for rule_ids.
</validation_tasks>

<output_instructions>
1. Output your reasoning inside <validation_report> tags, addressing the tasks above. Identify any issues first.
2. Output the final repaired JSON strict representation enclosed inside <corrected_json> tags. Output only valid JSON inside the tags.
If no changes are necessary, simply emit the original JSON inside the <corrected_json> tags.
</output_instructions>"""


from pydantic import field_validator, model_validator


class RuleNode(BaseModel):
    rule_id: str
    rule_text: str
    operator: Optional[str] = None  # "AND" or "OR" on non-leaf nodes
    rules: Optional[list["RuleNode"]] = None

    @field_validator("rule_id")
    @classmethod
    def rule_id_must_be_numeric_dot_notation(cls, v):
        if not re.fullmatch(r"\d+(\.\d+)*", v):
            raise ValueError(f"rule_id must be numeric dot-notation (e.g. '1', '1.2.3'), got '{v}'")
        return v

    @field_validator("rule_text")
    @classmethod
    def rule_text_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("rule_text must not be empty")
        return v

    @field_validator("operator")
    @classmethod
    def operator_must_be_and_or_or(cls, v):
        if v is not None and v not in ("AND", "OR"):
            raise ValueError(f"operator must be 'AND' or 'OR', got '{v}'")
        return v

    @model_validator(mode="after")
    def check_leaf_vs_branch_consistency(self):
        has_children = bool(self.rules)
        has_operator = self.operator is not None

        if has_children and not has_operator:
            raise ValueError(f"rule_id '{self.rule_id}' has children but no operator")
        if not has_children and has_operator:
            raise ValueError(f"rule_id '{self.rule_id}' is a leaf but has operator '{self.operator}'")
        return self


class CriteriaTree(BaseModel):
    title: str
    insurance_name: str
    rules: RuleNode

    @field_validator("insurance_name")
    @classmethod
    def insurance_name_must_mention_oscar(cls, v):
        if "Oscar" not in v:
            raise ValueError(f"insurance_name should contain 'Oscar', got '{v}'")
        return v


def extract_text(pdf_path: str) -> str:
    """Extract all text from a PDF using PyMuPDF."""
    text = ""
    doc = fitz.open(pdf_path)
    for page in doc:
        text += page.get_text() + "\n"
    doc.close()
    return text


START_PATTERNS = [
    r"(?i)Medical\s+Necessity\s+Criteria\s+for\s+Initial",
    r"(?i)Initial\s+(?:authorization|approval|treatment|clinical\s+review)\s+Criteria",
    r"(?i)Criteria\s+for\s+Initial\s+(?:authorization|approval)",
    r"(?i)Medical\s+Necessity\s+Criteria",
    r"(?i)General\s+Medical\s+Necessity\s+Criteria",
    r"(?i)Clinical\s+Indications",
]

END_PATTERNS = [
    r"(?i)\n[^\n]*Continuation\s+(?:of\s+)?(?:Therapy\s+)?Criteria",
    r"(?i)\n[^\n]*Subsequent\s+Clinical\s+Review",
    r"(?i)\n[^\n]*Renewal\s+Criteria",
    r"(?i)\n[^\n]*Reassessment\s+Criteria",
    r"(?i)\n[^\n]*Maintenance\s+(?:Therapy\s+)?Criteria",
    r"(?i)\n[^\n]*Experimental\s+or\s+Investigational",
    r"(?i)\n[^\n]*Not\s+Medically\s+Necessary",
    r"(?i)\n[^\n]*Applicable\s+Billing\s+Codes",
    r"(?i)\n[^\n]*References",
]


def _is_toc_entry(full_text: str, match_end: int) -> bool:
    """Check if a match is a TOC entry by looking for a trailing page number."""
    # Grab the rest of the line after the match
    newline = full_text.find("\n", match_end)
    if newline == -1:
        newline = len(full_text)
    rest_of_line = full_text[match_end:newline].strip()
    # TOC entries end with a page number like "... 3" or "... 12" (often with dots/spaces)
    return bool(re.match(r"^[\s.]*\d+\s*$", rest_of_line))


def _find_end(full_text: str, start_idx: int) -> int:
    """Find the earliest end marker after start_idx."""
    end_idx = len(full_text)
    for pat in END_PATTERNS:
        match = re.search(pat, full_text[start_idx:])
        if match:
            candidate = start_idx + match.start()
            if candidate < end_idx:
                end_idx = candidate
    return end_idx


def regex_extract(full_text: str) -> tuple[str | None, str]:
    """
    Find 'initial criteria' section using regex.
    Iterates through every matching start position and accepts the first one
    that produces a section >= 200 chars. Skips TOC entries and short matches.
    Returns (section, method) or (None, method) if no reliable boundary found.
    """
    for pat in START_PATTERNS:
        for match in re.finditer(pat, full_text):
            if _is_toc_entry(full_text, match.end()):
                continue

            start_idx = max(0, full_text.rfind("\n", 0, match.start()))
            end_idx = _find_end(full_text, start_idx)
            section = full_text[start_idx:end_idx].strip()

            if len(section) >= 200:
                return section, "regex_bounds"

    return None, "no_valid_match"


def extract_initial_section(pdf_path: str) -> tuple[str, str, dict]:
    """
    Section detection:
    1. Use PyMuPDF for text
    2. Run regex to find 'initial criteria' section
    3. If regex fails, return full text — let the LLM find it

    Returns: (section, full_text, metadata)
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return "", "", {"method": "error", "error": str(e)}

    try:
        full_text = ""
        for page in doc:
            full_text += page.get_text() + "\n"

        section, method = regex_extract(full_text)

        if section:
            return section, full_text, {"method": f"regex_{method}"}

        # Fallback: full text, let LLM scope it
        return full_text, full_text, {"method": f"full_text_fallback_{method}"}

    finally:
        doc.close()


def parse_json_response(raw: str) -> dict | None:
    """Extract JSON from LLM response. Unwraps list-wrapped objects."""
    parsed = None

    if "<corrected_json>" in raw and "</corrected_json>" in raw:
        json_str = raw.split("<corrected_json>")[1].split("</corrected_json>")[0].strip()
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            pass

    if parsed is None and "<extracted_json>" in raw and "</extracted_json>" in raw:
        json_str = raw.split("<extracted_json>")[1].split("</extracted_json>")[0].strip()
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            pass

    if parsed is None:
        combined = raw.strip()
        if combined.startswith("```"):
            combined = re.sub(r"^```(?:json)?\n?", "", combined)
            combined = re.sub(r"\n?```$", "", combined)
        try:
            parsed = json.loads(combined)
        except json.JSONDecodeError:
            return None

    # If LLM wrapped the object in a list, unwrap it
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]

    # Only accept dict responses
    if not isinstance(parsed, dict):
        return None

    return parsed


def _select_provider() -> str:
    """Pick LLM provider based on which API key is set."""
    has_anthropic_key = bool(os.getenv("ANTHROPIC_API_KEY")) and HAS_ANTHROPIC
    has_openai_key = bool(os.getenv("OPENAI_API_KEY")) and HAS_OPENAI
    if has_anthropic_key:
        return "anthropic"
    if has_openai_key:
        return "openai"
    raise RuntimeError(
        "No LLM provider available — set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env"
    )


PROVIDER = _select_provider()
MODEL = ANTHROPIC_MODEL if PROVIDER == "anthropic" else OPENAI_MODEL
print(f"LLM provider: {PROVIDER} (model: {MODEL})")


class TransientLLMError(Exception):
    """Retryable errors — rate limit, timeout, connection, provider overloaded."""


class PermanentLLMError(Exception):
    """Non-retryable errors — auth, bad request, unsupported model."""


def _call_anthropic(system_prompt: str, user_content: str) -> str:
    """Single Anthropic API call. Raises typed errors so the caller can decide."""
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text
    except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError) as e:
        raise TransientLLMError(f"{type(e).__name__}: {e}") from e
    except anthropic.APIStatusError as e:
        if e.status_code in (429, 503, 529):
            raise TransientLLMError(f"{e.status_code}: {e}") from e
        raise PermanentLLMError(f"{e.status_code}: {e}") from e


def _call_openai(system_prompt: str, user_content: str) -> str:
    """Single OpenAI API call. Raises typed errors so the caller can decide."""
    client = OpenAI()
    try:
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content or ""
    except (OpenAIConnError, OpenAITimeoutError, OpenAIRateLimitError) as e:
        raise TransientLLMError(f"{type(e).__name__}: {e}") from e
    except OpenAIAuthError as e:
        raise PermanentLLMError(f"auth: {e}") from e
    except OpenAIStatusError as e:
        if getattr(e, "status_code", 500) in (429, 503):
            raise TransientLLMError(f"{e.status_code}: {e}") from e
        raise PermanentLLMError(f"{e.status_code}: {e}") from e


def call_llm(system_prompt: str, user_content: str) -> str:
    """
    Provider-agnostic LLM call with retry on transient errors.
    - Retries up to MAX_LLM_RETRIES with exponential backoff on TransientLLMError
    - Raises PermanentLLMError immediately (auth, bad request) — no retry helps
    """
    last_error: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            if PROVIDER == "anthropic":
                return _call_anthropic(system_prompt, user_content)
            return _call_openai(system_prompt, user_content)
        except TransientLLMError as e:
            last_error = e
            if attempt == MAX_LLM_RETRIES - 1:
                raise
            wait = 2 ** attempt  # 1s, 2s, 4s
            print(f"  {PROVIDER} transient error (attempt {attempt + 1}/{MAX_LLM_RETRIES}): {e} — retrying in {wait}s")
            time.sleep(wait)
        except PermanentLLMError:
            # Don't retry — won't succeed
            raise

    # Unreachable, but satisfies type checker
    raise last_error or RuntimeError("unknown LLM failure")


def structure_with_llm(
    section: str, full_text: str = None
) -> tuple[dict | None, dict]:
    """
    2-pass extraction using the selected provider (Anthropic or OpenAI):
    Pass 1: Extract JSON from section
    Pass 2: Validate JSON against full text

    Error handling:
    - Transient errors (rate limit, timeout, provider overloaded) retry with backoff inside call_llm
    - Permanent errors (auth, bad request) raise PermanentLLMError and abort the policy
    - Invalid JSON from the LLM is retried once with error feedback
    - On Pass 2 failure, falls back to Pass 1 result rather than losing the extraction
    """
    if len(section) > MAX_SECTION_CHARS:
        section = section[:MAX_SECTION_CHARS]

    if full_text and len(full_text) > MAX_FULL_TEXT_CHARS:
        full_text = full_text[:MAX_FULL_TEXT_CHARS]

    llm_metadata = {
        "provider": PROVIDER,
        "model": MODEL,
        "strategy": "2-pass",
        "errors": [],
    }

    # Pass 1: Extract initial criteria as JSON
    print("  Pass 1: Extracting...")
    parsed: dict | None = None
    last_error: str | None = None
    for attempt in range(2):
        user_content = f"<input_criteria_section>\n{section}\n</input_criteria_section>\n\nOutput JSON:"
        if attempt == 1 and last_error:
            user_content += f"\n\nPrevious response was invalid: {last_error}. Return JSON only."

        try:
            output = call_llm(SYSTEM_PROMPT, user_content)
        except PermanentLLMError as e:
            # Auth / bad request — abort this policy
            llm_metadata["errors"].append(f"pass1_permanent: {e}")
            print(f"  Pass 1 permanent error: {e}")
            return None, llm_metadata
        except TransientLLMError as e:
            # Already retried MAX_LLM_RETRIES inside call_llm — give up
            llm_metadata["errors"].append(f"pass1_transient_exhausted: {e}")
            print(f"  Pass 1 transient retries exhausted: {e}")
            return None, llm_metadata

        parsed = parse_json_response(output)
        if parsed:
            print("  Pass 1: OK")
            break
        last_error = "malformed JSON"
        llm_metadata["errors"].append(f"pass1_invalid_json_attempt_{attempt + 1}")
        print(f"  Pass 1: invalid JSON (attempt {attempt + 1})")

    if parsed is None:
        return None, llm_metadata

    # Pass 2: Validate against full text — optional, falls back to Pass 1 result
    if full_text:
        print("  Pass 2: Validating against full text...")
        last_error = None
        for attempt in range(2):
            user_content = (
                f"Full text:\n{full_text}\n\n---\n\n"
                f"Extracted JSON:\n{json.dumps(parsed, indent=2)}\n\n"
                "Return corrected JSON only:"
            )
            if attempt == 1 and last_error:
                user_content += f"\n\nPrevious invalid: {last_error}. Return JSON only."

            try:
                output = call_llm(VALIDATION_SYSTEM_PROMPT, user_content)
            except PermanentLLMError as e:
                llm_metadata["errors"].append(f"pass2_permanent_using_pass1: {e}")
                print(f"  Pass 2 permanent error: {e} — using Pass 1 result")
                return parsed, llm_metadata
            except TransientLLMError as e:
                llm_metadata["errors"].append(f"pass2_transient_exhausted_using_pass1: {e}")
                print(f"  Pass 2 retries exhausted: {e} — using Pass 1 result")
                return parsed, llm_metadata

            validated = parse_json_response(output)
            if validated:
                print("  Pass 2: OK")
                return validated, llm_metadata
            last_error = "malformed JSON"
            llm_metadata["errors"].append(f"pass2_invalid_json_attempt_{attempt + 1}")
            print(f"  Pass 2: invalid JSON (attempt {attempt + 1})")

        llm_metadata["errors"].append("pass2_failed_using_pass1_result")
        print("  Pass 2 failed — using Pass 1 result")

    return parsed, llm_metadata


# Backwards-compat alias
structure_with_claude = structure_with_llm


def validate_tree(data: dict) -> tuple[CriteriaTree | None, str | None]:
    """Validate output against schema."""
    try:
        tree = CriteriaTree(**data)
        return tree, None
    except ValidationError as e:
        return None, str(e)


def run(limit: int = BATCH_SIZE, verbose: bool = False):
    """Structure all policies from DB."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT p.id, p.title, d.stored_location
        FROM policies p
        JOIN downloads d ON d.policy_id = p.id AND d.error IS NULL
        LEFT JOIN structured_policies sp ON sp.policy_id = p.id
        WHERE sp.id IS NULL
        LIMIT %s
    """,
        (limit,),
    )

    pending = cur.fetchall()
    print(f"Structuring {len(pending)} policies...\n")

    success_count = 0
    fail_count = 0

    for policy in pending:
        policy_id = policy["id"]
        title = policy["title"]
        pdf_path = policy["stored_location"]

        print(f"[{success_count + fail_count + 1}/{len(pending)}] {title[:60]}")

        try:
            section, full_text, section_meta = extract_initial_section(pdf_path)
            if not section or not section.strip():
                raise Exception(f"Empty section: {section_meta.get('method')}")
        except Exception as e:
            print(f"  ✗ Section extraction failed: {e}")
            cur.execute(
                """
                INSERT INTO structured_policies (policy_id, extracted_text, validation_error, extraction_method)
                VALUES (%s, %s, %s, %s)
            """,
                (policy_id, "", f"Section extraction failed: {e}", "failed"),
            )
            conn.commit()
            fail_count += 1
            continue

        print(f"  Section: {section_meta['method']} — {len(section)} chars")

        if verbose:
            print(f"\n  --- Section (first 500 chars) ---")
            print(section[:500])
            print(f"  ---\n")

        parsed, llm_metadata = structure_with_claude(section, full_text)

        extraction_method = section_meta["method"]

        if parsed is None:
            print(f"  ✗ LLM failed")
            cur.execute(
                """
                INSERT INTO structured_policies (policy_id, extracted_text, llm_metadata, validation_error, extraction_method)
                VALUES (%s, %s, %s, %s, %s)
            """,
                (
                    policy_id,
                    section[:5000],
                    json.dumps(llm_metadata),
                    "LLM failed",
                    extraction_method,
                ),
            )
            conn.commit()
            fail_count += 1
            continue

        parsed["title"] = f"Medical Necessity Criteria for {title}"
        parsed["insurance_name"] = "Oscar Health"

        validated, error = validate_tree(parsed)

        if error:
            print(f"  ✗ Validation failed: {error[:80]}")
            cur.execute(
                """
                INSERT INTO structured_policies (policy_id, extracted_text, structured_json, llm_metadata, validation_error, extraction_method)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (
                    policy_id,
                    section[:5000],
                    json.dumps(parsed),
                    json.dumps(llm_metadata),
                    error,
                    extraction_method,
                ),
            )
            conn.commit()
            fail_count += 1
            continue

        result_json = validated.model_dump()
        cur.execute(
            """
            INSERT INTO structured_policies (policy_id, extracted_text, structured_json, llm_metadata, extraction_method)
            VALUES (%s, %s, %s, %s, %s)
        """,
            (
                policy_id,
                section[:5000],
                json.dumps(result_json),
                json.dumps(llm_metadata),
                extraction_method,
            ),
        )
        conn.commit()
        success_count += 1
        print(f"  ✓ OK")

        if verbose:
            print(json.dumps(result_json, indent=2)[:500])

    cur.close()
    conn.close()
    print(f"\n{'=' * 50}")
    print(
        f"Done. {success_count} succeeded, {fail_count} failed out of {len(pending)} attempted."
    )


def run_single(pdf_path: str):
    """Test extraction on a single PDF."""
    print(f"Testing: {pdf_path}\n")

    section, full_text, meta = extract_initial_section(pdf_path)
    print(f"Method: {meta['method']}")
    print(f"Section: {len(section)} chars\n")

    print(f"{'=' * 60}")
    print("EXTRACTED TEXT (before LLM):")
    print(f"{'=' * 60}")
    print(section)
    print(f"{'=' * 60}\n")

    parsed, llm_meta = structure_with_claude(section, full_text)

    if parsed is None:
        print("✗ LLM failed")
        return

    parsed["insurance_name"] = "Oscar Health"

    validated, error = validate_tree(parsed)
    if error:
        print(f"✗ Validation failed: {error[:120]}")
        print(json.dumps(parsed, indent=2))
        return

    print("✓ Validation passed\n")
    print(json.dumps(validated.model_dump(), indent=2))

    output_path = pdf_path.replace(".pdf", "_structured.json")
    with open(output_path, "w") as f:
        json.dump(validated.model_dump(), f, indent=2)
    print(f"\nSaved to: {output_path}")


def run_dir(pdf_dir: str, limit: int = BATCH_SIZE):
    """Process first N PDFs from a directory. No DB — writes *_structured.json next to each PDF."""
    import glob
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))[:limit]
    print(f"Processing {len(pdfs)} PDFs from {pdf_dir}\n")

    success = fail = 0
    for i, pdf_path in enumerate(pdfs, 1):
        name = os.path.basename(pdf_path)
        print(f"[{i}/{len(pdfs)}] {name}")
        try:
            section, full_text, meta = extract_initial_section(pdf_path)
            if not section or not section.strip():
                print(f"  ✗ Empty section: {meta.get('method')}")
                fail += 1
                continue
            print(f"  Section: {meta['method']} — {len(section)} chars")

            parsed, _ = structure_with_claude(section, full_text)
            if parsed is None:
                print("  ✗ LLM failed")
                fail += 1
                continue

            parsed["title"] = f"Medical Necessity Criteria for {name.replace('.pdf', '')}"
            parsed["insurance_name"] = "Oscar Health"

            validated, error = validate_tree(parsed)
            if error:
                print(f"  ✗ Validation failed: {error[:80]}")
                fail += 1
                continue

            out = pdf_path.replace(".pdf", "_structured.json")
            with open(out, "w") as f:
                json.dump(validated.model_dump(), f, indent=2)
            print(f"  ✓ OK → {os.path.basename(out)}")
            success += 1
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}")
            fail += 1

    print(f"\n{'=' * 50}")
    print(f"Done. {success} succeeded, {fail} failed out of {len(pdfs)}.")


def main():
    import sys

    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    if "--pdf" in sys.argv:
        pdf_idx = sys.argv.index("--pdf") + 1
        run_single(sys.argv[pdf_idx])
    elif "--dir" in sys.argv:
        dir_idx = sys.argv.index("--dir") + 1
        pdf_dir = sys.argv[dir_idx]
        args = [a for j, a in enumerate(sys.argv[1:], 1) if not a.startswith("-") and sys.argv[j - 1] != "--dir"]
        limit = int(args[0]) if args else BATCH_SIZE
        run_dir(pdf_dir, limit=limit)
    else:
        init_db()
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        limit = int(args[0]) if args else BATCH_SIZE
        run(limit=limit, verbose=verbose)


if __name__ == "__main__":
    main()
