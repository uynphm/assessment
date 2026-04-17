"""
Database setup and helpers for the Oscar Guidelines scraper.
Uses PostgreSQL with psycopg2 — 3 tables: policies, downloads, structured_policies.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://oscar:oscar_dev_pw@localhost:5432/oscar_guidelines"
)


def get_conn():
    """Get a new database connection with dict cursor."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


get_dict_conn = get_conn  # Alias for compatibility


def init_db():
    """Create tables if they don't exist. Safe to re-run."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS policies (
            id SERIAL PRIMARY KEY,
            title VARCHAR NOT NULL,
            pdf_url VARCHAR UNIQUE NOT NULL,
            source_page_url VARCHAR NOT NULL,
            discovered_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id SERIAL PRIMARY KEY,
            policy_id INTEGER REFERENCES policies(id) ON DELETE CASCADE,
            stored_location VARCHAR NOT NULL,
            downloaded_at TIMESTAMP DEFAULT NOW(),
            http_status INTEGER,
            error TEXT,
            content_hash VARCHAR
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS structured_policies (
            id SERIAL PRIMARY KEY,
            policy_id INTEGER REFERENCES policies(id) ON DELETE CASCADE,
            extracted_text TEXT,
            structured_json JSONB,
            structured_at TIMESTAMP DEFAULT NOW(),
            llm_metadata JSONB,
            validation_error TEXT,
            extraction_method VARCHAR
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id SERIAL PRIMARY KEY,
            run_at TIMESTAMP DEFAULT NOW(),
            policies_found INTEGER NOT NULL,
            source_url VARCHAR NOT NULL,
            source_html_snapshot TEXT
        );
    """)

    # Index for frequent queries
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_downloads_policy_id ON downloads(policy_id);
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_structured_policy_id ON structured_policies(policy_id);
    """)
    # GIN index for querying inside structured JSON trees
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_structured_json ON structured_policies USING GIN (structured_json);
    """)
    # Index for filtering by extraction method
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_extraction_method ON structured_policies(extraction_method);
    """)
    # Trigram index on title — speeds up ILIKE '%foo%' substring search used by /policies?q=...
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_policies_title_trgm
        ON policies USING GIN (title gin_trgm_ops);
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("Database initialized successfully.")


if __name__ == "__main__":
    init_db()
