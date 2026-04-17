import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

interface Policy {
  id: number;
  title: string;
  pdf_url: string;
  source_page_url: string;
  discovered_at: string;
  download_status: "success" | "failed";
  structure_status: "none" | "success" | "failed";
}

type StatusFilter =
  | "all"
  | "structured"
  | "failed"
  | "not_structured"
  | "downloaded"
  | "not_downloaded";

interface Stats {
  policies_discovered: number;
  downloads_successful: number;
  downloads_failed: number;
  structures_successful: number;
  structures_failed: number;
  extraction_method_breakdown: Record<string, number>;
  active_jobs: Record<string, string>;
}

const API = "http://localhost:8000";

export default function PoliciesTable() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [stats, setStats] = useState<Stats | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [scrapeState, setScrapeState] = useState<{
    status: string;
    message: string;
  } | null>(null);
  const limit = 50;

  const fetchPolicies = () => {
    const params = new URLSearchParams({
      page: String(page),
      limit: String(limit),
      status,
    });
    if (debouncedQuery) params.set("q", debouncedQuery);
    return fetch(`${API}/policies?${params}`)
      .then((r) => r.json())
      .then((data) => {
        setPolicies(data.policies);
        setTotal(data.total);
      });
  };

  const fetchStats = () =>
    fetch(`${API}/stats`)
      .then((r) => r.json())
      .then(setStats);

  const fetchScrapeStatus = () =>
    fetch(`${API}/scrape/status`)
      .then((r) => r.json())
      .then(setScrapeState);

  const runScrape = async () => {
    try {
      await fetch(`${API}/scrape`, { method: "POST" });
      fetchScrapeStatus();
    } catch (e) {
      console.error(e);
    }
  };

  // Debounce search input by 300ms
  useEffect(() => {
    const id = setTimeout(() => {
      setDebouncedQuery(query);
      setPage(1); // reset to first page on new search
    }, 300);
    return () => clearTimeout(id);
  }, [query]);

  useEffect(() => {
    fetchPolicies();
    fetchStats();
    fetchScrapeStatus();
  }, [page, debouncedQuery, status]);

  // Poll scrape status while it's running
  useEffect(() => {
    if (
      scrapeState?.status !== "discovering" &&
      scrapeState?.status !== "downloading"
    )
      return;
    const id = setInterval(() => {
      fetchScrapeStatus();
      fetchPolicies();
      fetchStats();
    }, 3000);
    return () => clearInterval(id);
  }, [scrapeState]);

  // Reset to page 1 when filter changes
  useEffect(() => {
    setPage(1);
  }, [status]);

  // Poll stats while any jobs are active so UI updates as structuring finishes
  useEffect(() => {
    const hasActive = stats && Object.keys(stats.active_jobs || {}).length > 0;
    if (!hasActive) return;
    const id = setInterval(() => {
      fetchStats();
      fetchPolicies();
    }, 3000);
    return () => clearInterval(id);
  }, [stats]);

  const totalPages = Math.ceil(total / limit);

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (selected.size === policies.length) setSelected(new Set());
    else setSelected(new Set(policies.map((p) => p.id)));
  };

  const extractSelected = async () => {
    if (selected.size === 0) return;
    setSubmitting(true);
    try {
      await fetch(`${API}/structure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy_ids: Array.from(selected) }),
      });
      setSelected(new Set());
      await fetchStats();
    } finally {
      setSubmitting(false);
    }
  };

  const activeCount = stats ? Object.keys(stats.active_jobs || {}).length : 0;

  return (
    <div className="max-w-5xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-2">Oscar Medical Guidelines</h1>
      <p className="text-gray-500 mb-2">{total} policies discovered</p>

      {/* Stats bar */}
      {stats && (
        <div className="flex items-center gap-4 text-sm text-gray-600 mb-4">
          <span>✓ Downloaded: {stats.downloads_successful}</span>
          <span>✓ Structured: {stats.structures_successful}</span>
          {stats.structures_failed > 0 && (
            <span className="text-red-600">✗ Failed: {stats.structures_failed}</span>
          )}
          {activeCount > 0 && (
            <span className="text-blue-600">⟳ Active: {activeCount}</span>
          )}
          <div className="flex-1" />
          <button
            onClick={runScrape}
            disabled={
              scrapeState?.status === "discovering" ||
              scrapeState?.status === "downloading"
            }
            className="px-3 py-1 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-40"
          >
            {scrapeState?.status === "discovering"
              ? "⟳ Discovering..."
              : scrapeState?.status === "downloading"
              ? "⟳ Downloading..."
              : "Discover & Download"}
          </button>
        </div>
      )}

      {/* Scrape status message */}
      {scrapeState &&
        scrapeState.status !== "idle" &&
        scrapeState.status !== "done" && (
          <div
            className={`px-3 py-2 text-sm rounded mb-4 ${
              scrapeState.status === "error"
                ? "bg-red-50 text-red-700 border border-red-200"
                : "bg-blue-50 text-blue-700 border border-blue-200"
            }`}
          >
            {scrapeState.message}
          </div>
        )}

      {/* Action bar */}
      <div className="flex items-center gap-3 mb-4 pb-2 border-b border-gray-200">
        <input
          type="text"
          placeholder="Search policies by name..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="flex-1 px-3 py-1 text-sm border rounded focus:outline-none focus:ring-2 focus:ring-blue-500"
        />

        {/* Filter dropdown */}
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as StatusFilter)}
          className="px-3 py-1 text-sm border rounded bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="all">All policies</option>
          <option value="structured">Structured (success)</option>
          <option value="failed">Failed structuring</option>
          <option value="not_structured">Never structured</option>
          <option value="downloaded">Downloaded</option>
          <option value="not_downloaded">Not downloaded</option>
        </select>

        <span className="text-sm text-gray-500 whitespace-nowrap">
          {selected.size} selected
        </span>
        <button
          onClick={extractSelected}
          disabled={selected.size === 0 || submitting}
          className="px-3 py-1 text-sm bg-blue-600 text-white rounded disabled:opacity-40 hover:bg-blue-700 whitespace-nowrap"
        >
          {submitting ? "Queuing..." : `Extract Selected (${selected.size})`}
        </button>
      </div>

      <table className="w-full text-left border-collapse">
        <thead>
          <tr className="border-b border-gray-200">
            <th className="py-2 px-3 w-8">
              <input
                type="checkbox"
                checked={selected.size === policies.length && policies.length > 0}
                onChange={toggleAll}
              />
            </th>
            <th className="py-2 px-3 text-sm text-gray-500">Title</th>
            <th className="py-2 px-3 text-sm text-gray-500 w-24">PDF</th>
            <th className="py-2 px-3 text-sm text-gray-500 w-24">Download</th>
            <th className="py-2 px-3 text-sm text-gray-500 w-28">Tree</th>
          </tr>
        </thead>
        <tbody>
          {policies.map((p) => {
            const active = stats?.active_jobs?.[p.id];
            return (
              <tr
                key={p.id}
                className="border-b border-gray-100 hover:bg-gray-50"
              >
                <td className="py-2 px-3">
                  <input
                    type="checkbox"
                    checked={selected.has(p.id)}
                    onChange={() => toggle(p.id)}
                  />
                </td>
                <td className="py-2 px-3">
                  <Link
                    to={`/policy/${p.id}`}
                    className="text-blue-600 hover:underline"
                  >
                    {p.title}
                  </Link>
                </td>
                <td className="py-2 px-3">
                  <a
                    href={p.pdf_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-500 text-sm hover:underline"
                  >
                    View PDF
                  </a>
                </td>
                <td className="py-2 px-3">
                  <span
                    className={`text-xs px-2 py-0.5 rounded ${
                      p.download_status === "success"
                        ? "bg-green-100 text-green-700"
                        : "bg-red-100 text-red-700"
                    }`}
                  >
                    {p.download_status}
                  </span>
                </td>
                <td className="py-2 px-3">
                  {active ? (
                    <span className="text-xs px-2 py-0.5 rounded bg-blue-100 text-blue-700">
                      ⟳ {active}
                    </span>
                  ) : p.structure_status === "success" ? (
                    <span className="text-xs px-2 py-0.5 rounded bg-purple-100 text-purple-700">
                      Structured
                    </span>
                  ) : p.structure_status === "failed" ? (
                    <span className="text-xs px-2 py-0.5 rounded bg-red-100 text-red-700">
                      Failed
                    </span>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex gap-2 mt-4 justify-center">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1 border rounded text-sm disabled:opacity-30"
          >
            Prev
          </button>
          <span className="px-3 py-1 text-sm text-gray-500">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="px-3 py-1 border rounded text-sm disabled:opacity-30"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
