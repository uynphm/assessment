"""
FastAPI API — serves policy data to the React frontend.
Endpoints: list policies, get policy detail, pipeline stats.
"""

import json
import threading
from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from database import get_conn
from structure import (
    extract_initial_section,
    structure_with_claude,
    validate_tree,
)
from scrape import (
    discover_policies,
    resolve_pdf_urls,
    store_policies,
    download_pdfs,
)

app = FastAPI(title="Oscar Guidelines API")

# Allow React dev server to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Simple in-memory state for active structuring jobs
_active_jobs: dict[int, str] = {}  # policy_id -> "queued" | "running"
_jobs_lock = threading.Lock()

# Scrape pipeline state — "idle", "discovering", "downloading", "done", "error"
_scrape_state: dict = {"status": "idle", "message": "", "updated_at": None}
_scrape_lock = threading.Lock()


def _set_scrape_state(status: str, message: str = ""):
    import datetime
    with _scrape_lock:
        _scrape_state["status"] = status
        _scrape_state["message"] = message
        _scrape_state["updated_at"] = datetime.datetime.utcnow().isoformat()


class StructureRequest(BaseModel):
    policy_ids: list[int]


@app.get("/policies")
def list_policies(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    q: str = Query("", description="Optional title search (case-insensitive)"),
    status: str = Query("all", description="Filter: all | structured | failed | not_structured | downloaded | not_downloaded"),
):
    """List discovered policies with pagination + optional title search + structure filter."""
    conn = get_conn()
    cur = conn.cursor()
    offset = (page - 1) * limit

    # Build WHERE clauses
    conditions: list = []
    params: list = []

    if q.strip():
        # Match title OR any rule_text inside the structured criteria tree
        conditions.append("""(
            p.title ILIKE %s
            OR EXISTS (
                SELECT 1 FROM structured_policies sp3
                WHERE sp3.policy_id = p.id
                  AND sp3.validation_error IS NULL
                  AND sp3.structured_json::text ILIKE %s
            )
        )""")
        like = f"%{q.strip()}%"
        params.extend([like, like])

    if status == "structured":
        conditions.append(
            "EXISTS (SELECT 1 FROM structured_policies sp2 "
            "WHERE sp2.policy_id = p.id AND sp2.validation_error IS NULL)"
        )
    elif status == "failed":
        conditions.append(
            "EXISTS (SELECT 1 FROM structured_policies sp2 "
            "WHERE sp2.policy_id = p.id AND sp2.validation_error IS NOT NULL)"
        )
    elif status == "not_structured":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM structured_policies sp2 "
            "WHERE sp2.policy_id = p.id)"
        )
    elif status == "downloaded":
        conditions.append(
            "EXISTS (SELECT 1 FROM downloads d2 "
            "WHERE d2.policy_id = p.id AND d2.error IS NULL)"
        )
    elif status == "not_downloaded":
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM downloads d2 "
            "WHERE d2.policy_id = p.id AND d2.error IS NULL)"
        )

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Get filtered count
    cur.execute(f"SELECT COUNT(*) as total FROM policies p {where}", params)
    total = cur.fetchone()["total"]

    # Get paginated policies with download status + structure outcome (success/failed/none)
    cur.execute(f"""
        SELECT
            p.id,
            p.title,
            p.pdf_url,
            p.source_page_url,
            p.discovered_at,
            CASE WHEN d.error IS NULL AND d.id IS NOT NULL THEN 'success' ELSE 'failed' END as download_status,
            CASE
                WHEN sp.id IS NULL THEN 'none'
                WHEN sp.validation_error IS NULL THEN 'success'
                ELSE 'failed'
            END as structure_status
        FROM policies p
        LEFT JOIN downloads d ON d.policy_id = p.id
        LEFT JOIN structured_policies sp ON sp.policy_id = p.id
        {where}
        ORDER BY p.id
        LIMIT %s OFFSET %s
    """, params + [limit, offset])

    policies = cur.fetchall()

    # Convert timestamps to strings for JSON serialization
    for p in policies:
        if p["discovered_at"]:
            p["discovered_at"] = str(p["discovered_at"])

    cur.close()
    conn.close()

    return {
        "policies": policies,
        "total": total,
        "page": page,
        "limit": limit,
    }


@app.get("/policies/{policy_id}")
def get_policy(policy_id: int):
    """Get policy detail with structured tree if available."""
    conn = get_conn()
    cur = conn.cursor()

    # Get policy
    cur.execute("SELECT * FROM policies WHERE id = %s", (policy_id,))
    policy = cur.fetchone()

    if not policy:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Policy not found")

    # Convert timestamp
    if policy["discovered_at"]:
        policy["discovered_at"] = str(policy["discovered_at"])

    # Get download info
    cur.execute("""
        SELECT stored_location, downloaded_at, http_status, error
        FROM downloads WHERE policy_id = %s
        ORDER BY downloaded_at DESC LIMIT 1
    """, (policy_id,))
    download = cur.fetchone()
    if download and download["downloaded_at"]:
        download["downloaded_at"] = str(download["downloaded_at"])

    # Get latest structuring attempt — success OR failure
    cur.execute("""
        SELECT structured_json, structured_at, llm_metadata, validation_error, extraction_method
        FROM structured_policies WHERE policy_id = %s
        ORDER BY structured_at DESC LIMIT 1
    """, (policy_id,))
    structured = cur.fetchone()
    if structured and structured["structured_at"]:
        structured["structured_at"] = str(structured["structured_at"])

    cur.close()
    conn.close()

    return {
        "policy": policy,
        "download": download,
        "structured": structured,
    }


@app.get("/stats")
def get_stats():
    """Pipeline stats — total policies, downloads, structured, tier breakdown."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as total FROM policies")
    total_policies = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as total FROM downloads WHERE error IS NULL")
    successful_downloads = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as total FROM downloads WHERE error IS NOT NULL")
    failed_downloads = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as total FROM structured_policies WHERE validation_error IS NULL")
    successful_structures = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) as total FROM structured_policies WHERE validation_error IS NOT NULL")
    failed_structures = cur.fetchone()["total"]

    cur.execute("""
        SELECT extraction_method, COUNT(*) as count
        FROM structured_policies
        WHERE validation_error IS NULL
        GROUP BY extraction_method
    """)
    method_breakdown = {row["extraction_method"]: row["count"] for row in cur.fetchall()}

    cur.close()
    conn.close()

    with _jobs_lock:
        active = dict(_active_jobs)

    return {
        "policies_discovered": total_policies,
        "downloads_successful": successful_downloads,
        "downloads_failed": failed_downloads,
        "structures_successful": successful_structures,
        "structures_failed": failed_structures,
        "extraction_method_breakdown": method_breakdown,
        "active_jobs": active,
    }


def _structure_one(policy_id: int):
    """Run the full structure pipeline for one policy. Writes result to DB."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT p.title, d.stored_location FROM policies p JOIN downloads d ON d.policy_id = p.id AND d.error IS NULL WHERE p.id = %s", (policy_id,))
        row = cur.fetchone()
        if not row:
            return
        title = row["title"]
        pdf_path = row["stored_location"]

        # Delete any existing structured_policies row so we can re-run
        cur.execute("DELETE FROM structured_policies WHERE policy_id = %s", (policy_id,))
        conn.commit()

        with _jobs_lock:
            _active_jobs[policy_id] = "running"

        section, full_text, section_meta = extract_initial_section(pdf_path)
        if not section or not section.strip():
            cur.execute(
                "INSERT INTO structured_policies (policy_id, extracted_text, validation_error, extraction_method) VALUES (%s, %s, %s, %s)",
                (policy_id, "", f"Section extraction failed: {section_meta.get('method')}", "failed"),
            )
            conn.commit()
            return

        extraction_method = section_meta["method"]
        parsed, llm_metadata = structure_with_claude(section, full_text)

        if parsed is None:
            cur.execute(
                "INSERT INTO structured_policies (policy_id, extracted_text, llm_metadata, validation_error, extraction_method) VALUES (%s, %s, %s, %s, %s)",
                (policy_id, section[:5000], json.dumps(llm_metadata), "LLM failed", extraction_method),
            )
            conn.commit()
            return

        parsed["title"] = f"Medical Necessity Criteria for {title}"
        parsed["insurance_name"] = "Oscar Health"

        validated, error = validate_tree(parsed)
        if error:
            cur.execute(
                "INSERT INTO structured_policies (policy_id, extracted_text, structured_json, llm_metadata, validation_error, extraction_method) VALUES (%s, %s, %s, %s, %s, %s)",
                (policy_id, section[:5000], json.dumps(parsed), json.dumps(llm_metadata), error, extraction_method),
            )
            conn.commit()
            return

        result_json = validated.model_dump()
        cur.execute(
            "INSERT INTO structured_policies (policy_id, extracted_text, structured_json, llm_metadata, extraction_method) VALUES (%s, %s, %s, %s, %s)",
            (policy_id, section[:5000], json.dumps(result_json), json.dumps(llm_metadata), extraction_method),
        )
        conn.commit()
    finally:
        with _jobs_lock:
            _active_jobs.pop(policy_id, None)
        cur.close()
        conn.close()


def _structure_batch(policy_ids: list[int]):
    """Run _structure_one for each policy_id sequentially."""
    for pid in policy_ids:
        try:
            _structure_one(pid)
        except Exception as e:
            print(f"Error structuring policy {pid}: {e}")


@app.post("/structure")
def trigger_structure(req: StructureRequest, background_tasks: BackgroundTasks):
    """
    Queue structuring for a list of policy_ids. Runs in background.
    Returns immediately with the list of queued IDs.
    """
    if not req.policy_ids:
        raise HTTPException(status_code=400, detail="policy_ids cannot be empty")

    with _jobs_lock:
        for pid in req.policy_ids:
            _active_jobs[pid] = "queued"

    background_tasks.add_task(_structure_batch, req.policy_ids)
    return {"queued": req.policy_ids, "count": len(req.policy_ids)}


def _run_scrape_pipeline():
    """Run discovery → resolve → store → download in sequence."""
    try:
        _set_scrape_state("discovering", "Scraping Oscar's guidelines page...")
        policies = discover_policies()
        if not policies:
            _set_scrape_state("error", "No policy links found")
            return

        _set_scrape_state("discovering", f"Resolving PDF URLs from {len(policies)} policy pages...")
        resolved = resolve_pdf_urls(policies)
        store_policies(resolved)

        _set_scrape_state("downloading", f"Downloading {len(resolved)} PDFs...")
        download_pdfs()

        _set_scrape_state("done", f"Discovered {len(resolved)} policies, downloads complete")
    except Exception as e:
        _set_scrape_state("error", str(e))
        print(f"Scrape error: {e}")


@app.post("/scrape")
def trigger_scrape(background_tasks: BackgroundTasks):
    """
    Trigger discovery + download pipeline in background.
    Returns immediately. Poll /scrape/status for progress.
    """
    with _scrape_lock:
        if _scrape_state["status"] in ("discovering", "downloading"):
            raise HTTPException(
                status_code=409,
                detail=f"Scrape already running: {_scrape_state['status']}",
            )
    _set_scrape_state("discovering", "Starting...")
    background_tasks.add_task(_run_scrape_pipeline)
    return {"status": "started"}


@app.get("/scrape/status")
def get_scrape_status():
    """Check the current scrape pipeline state."""
    with _scrape_lock:
        return dict(_scrape_state)
