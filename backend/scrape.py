"""
PDF Discovery + Download script.
1. Scrapes Oscar's clinical guidelines page for all policy links
2. Visits each policy page to find the actual PDF URL
3. Stores policy metadata in DB (idempotent — skips duplicates)
4. Downloads all PDFs with retry + rate limiting
"""

import time
import re
import hashlib
import httpx
from bs4 import BeautifulSoup
from database import get_conn, init_db

SOURCE_URL = "https://www.hioscar.com/clinical-guidelines/medical"
BASE_URL = "https://www.hioscar.com"
RATE_LIMIT_SECONDS = 0.5
MAX_RETRIES = 3
BACKOFF_BASE = 1  # exponential backoff: 1s, 2s, 4s


def make_filename(policy_id: int, title: str) -> str:
    """
    Build a human-readable filename from the guideline code in the title.
    Examples:
      "Bariatric Surgery (CG008, Ver. 11)" → "CG008_v11.pdf"
      "Leqembi (PG138, Ver. 5)"            → "PG138_v5.pdf"
      (no code found)                       → "policy_{id}.pdf"
    """
    match = re.search(r"\b([CP]G\d+)[,\s]+Ver\.?\s*(\d+)", title, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}_v{match.group(2)}.pdf"

    match = re.search(r"\b([CP]G\d+)\b", title, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}.pdf"

    return f"policy_{policy_id}.pdf"


def discover_policies():
    """
    Parse Oscar's clinical guidelines page for all policy page links.
    Links are <a> tags with href like /medical/cg008v11 and text "PDF".
    Also records the run in discovery_runs for completeness monitoring.
    Returns list of dicts: { title, policy_page_url }
    """
    print(f"Fetching source page: {SOURCE_URL}")
    resp = httpx.get(SOURCE_URL, follow_redirects=True, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    policies = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Match /medical/... links (policy pages)
        if re.match(r"^/medical/", href) and href not in seen:
            seen.add(href)
            # Get the title from surrounding text — walk up to find the guideline name
            parent = a.find_parent("li") or a.find_parent("div") or a.find_parent("tr")
            title = parent.get_text(strip=True)[:200] if parent else href
            # Clean up title — remove "PDF" and "LINK" suffixes
            title = re.sub(r"\s*(PDF|LINK)\s*$", "", title).strip()
            if not title:
                title = href

            policies.append({
                "title": title,
                "policy_page_url": f"{BASE_URL}{href}",
            })

    print(f"Discovered {len(policies)} policy page links")

    # Record discovery run for completeness monitoring
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO discovery_runs (policies_found, source_url, source_html_snapshot)
            VALUES (%s, %s, %s)
        """, (len(policies), SOURCE_URL, resp.text[:50000]))

        # Warn on suspicious variance vs previous run
        cur.execute("""
            SELECT policies_found FROM discovery_runs
            WHERE id < currval(pg_get_serial_sequence('discovery_runs', 'id'))
            ORDER BY run_at DESC LIMIT 1
        """)
        prev = cur.fetchone()
        if prev:
            prev_count = prev["policies_found"]
            variance = abs(len(policies) - prev_count) / prev_count if prev_count else 0
            if variance > 0.05:
                print(f"  ⚠ DISCOVERY VARIANCE: {prev_count} → {len(policies)} ({variance:.1%} change)")

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  (discovery_runs log failed: {e})")

    return policies


def resolve_pdf_urls(policies):
    """
    Visit each policy page to find the actual PDF download URL.
    Oscar policy pages contain a link to a ctfassets.net PDF.
    Returns list of dicts: { title, pdf_url, source_page_url }
    """
    resolved = []
    print(f"Resolving PDF URLs from {len(policies)} policy pages...")

    for i, policy in enumerate(policies):
        url = policy["policy_page_url"]
        try:
            time.sleep(RATE_LIMIT_SECONDS)
            resp = httpx.get(url, follow_redirects=True, timeout=30)

            if resp.status_code != 200:
                print(f"  ✗ HTTP {resp.status_code}: {url}")
                continue

            # Look for PDF link in the page
            soup = BeautifulSoup(resp.text, "html.parser")

            # Strategy 1: find ctfassets.net PDF link in <a> tags
            pdf_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "ctfassets.net" in href and ".pdf" in href:
                    pdf_url = href if href.startswith("https") else f"https:{href}"
                    break

            # Strategy 2: check __NEXT_DATA__ for PDF URL
            if not pdf_url:
                script = soup.find("script", id="__NEXT_DATA__")
                if script:
                    import json
                    text = script.string
                    matches = re.findall(r'(//assets\.ctfassets\.net[^"]*\.pdf)', text)
                    if not matches:
                        matches = re.findall(r'(https://assets\.ctfassets\.net[^"]*\.pdf)', text)
                    if matches:
                        pdf_url = matches[0] if matches[0].startswith("https") else f"https:{matches[0]}"

            if pdf_url:
                resolved.append({
                    "title": policy["title"],
                    "pdf_url": pdf_url,
                    "source_page_url": url,
                })
                print(f"  ✓ [{i+1}/{len(policies)}] {policy['title'][:50]}")
            else:
                print(f"  ✗ [{i+1}/{len(policies)}] No PDF found: {url}")

        except Exception as e:
            print(f"  ✗ [{i+1}/{len(policies)}] Error: {e}")

    print(f"Resolved {len(resolved)} PDF URLs")
    return resolved


def store_policies(policies):
    """
    Insert discovered policies into DB.
    Skips duplicates via ON CONFLICT (pdf_url) DO NOTHING — idempotent.
    """
    conn = get_conn()
    cur = conn.cursor()
    inserted = 0

    for p in policies:
        cur.execute("""
            INSERT INTO policies (title, pdf_url, source_page_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (pdf_url) DO NOTHING
            RETURNING id
        """, (p["title"], p["pdf_url"], p["source_page_url"]))

        if cur.fetchone():
            inserted += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"Stored {inserted} new policies ({len(policies) - inserted} already existed)")


def download_pdfs():
    """
    Download all PDFs that haven't been downloaded yet.
    Rate limited + exponential backoff retry.
    """
    conn = get_conn()
    cur = conn.cursor()

    # Find policies without a successful download
    cur.execute("""
        SELECT p.id, p.pdf_url, p.title
        FROM policies p
        LEFT JOIN downloads d ON d.policy_id = p.id AND d.error IS NULL
        WHERE d.id IS NULL
    """)
    pending = cur.fetchall()
    print(f"Downloading {len(pending)} PDFs...")

    for policy in pending:
        policy_id = policy["id"]
        pdf_url = policy["pdf_url"]
        stored_location = f"pdfs/{make_filename(policy_id, policy['title'])}"

        for attempt in range(MAX_RETRIES):
            try:
                time.sleep(RATE_LIMIT_SECONDS)
                resp = httpx.get(pdf_url, follow_redirects=True, timeout=30)

                if resp.status_code == 200:
                    content = resp.content
                    content_hash = hashlib.sha256(content).hexdigest()

                    with open(stored_location, "wb") as f:
                        f.write(content)

                    cur.execute("""
                        INSERT INTO downloads (policy_id, stored_location, http_status, content_hash)
                        VALUES (%s, %s, %s, %s)
                    """, (policy_id, stored_location, resp.status_code, content_hash))
                    conn.commit()
                    print(f"  ✓ Downloaded: {policy['title'][:60]}")
                    break
                else:
                    raise Exception(f"HTTP {resp.status_code}")

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    print(f"  Retry {attempt + 1} for {policy['title'][:40]}... waiting {wait}s")
                    time.sleep(wait)
                else:
                    # Record failure in DB
                    cur.execute("""
                        INSERT INTO downloads (policy_id, stored_location, http_status, error)
                        VALUES (%s, %s, %s, %s)
                    """, (policy_id, stored_location, 0, str(e)))
                    conn.commit()
                    print(f"  ✗ Failed: {policy['title'][:60]} — {e}")

    cur.close()
    conn.close()
    print("Download complete.")


if __name__ == "__main__":
    init_db()
    policies = discover_policies()
    resolved = resolve_pdf_urls(policies)
    store_policies(resolved)
    download_pdfs()
