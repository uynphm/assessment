import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import CriteriaNode from "./CriteriaNode";

const API = "http://localhost:8000";

interface PolicyData {
  policy: {
    id: number;
    title: string;
    pdf_url: string;
    source_page_url: string;
    discovered_at: string;
  };
  download: {
    stored_location: string;
    downloaded_at: string;
    http_status: number;
    error: string | null;
  } | null;
  structured: {
    structured_json: {
      title: string;
      insurance_name: string;
      rules: {
        rule_id: string;
        rule_text: string;
        operator?: string;
        rules?: any[];
      };
    } | null;
    structured_at: string;
    llm_metadata: Record<string, any>;
    validation_error: string | null;
    extraction_method: string | null;
  } | null;
}

export default function PolicyView() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<PolicyData | null>(null);
  const [allExpanded, setAllExpanded] = useState(false);
  const [treeKey, setTreeKey] = useState(0);
  const [extracting, setExtracting] = useState(false);

  const fetchData = () =>
    fetch(`${API}/policies/${id}`)
      .then((r) => r.json())
      .then(setData);

  useEffect(() => {
    fetchData();
  }, [id]);

  // Poll while extraction is active
  useEffect(() => {
    if (!extracting) return;
    const interval = setInterval(fetchData, 3000);
    return () => clearInterval(interval);
  }, [extracting]);

  // Detect when extraction finishes and stop polling
  useEffect(() => {
    if (!extracting || !data?.structured) return;
    const justFinished =
      data.structured.structured_at &&
      new Date(data.structured.structured_at).getTime() > Date.now() - 10000;
    if (justFinished) setExtracting(false);
  }, [data, extracting]);

  const handleExtract = async () => {
    if (!id || extracting) return;
    setExtracting(true);
    try {
      await fetch(`${API}/structure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy_ids: [Number(id)] }),
      });
    } catch {
      setExtracting(false);
    }
  };

  if (!data) return <div className="p-6">Loading...</div>;

  const { policy, structured } = data;
  const tree = structured?.structured_json;
  const hasError = structured?.validation_error;
  const hasTree = tree && !hasError;

  return (
    <div className="max-w-7xl mx-auto p-6">
      <Link to="/" className="text-blue-500 text-sm hover:underline">
        ← Back to all policies
      </Link>

      <div className="flex items-start justify-between mt-4 mb-2 gap-4">
        <h1 className="text-2xl font-bold">{policy.title}</h1>
        <button
          onClick={handleExtract}
          disabled={extracting}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-40 whitespace-nowrap"
        >
          {extracting
            ? "⟳ Extracting..."
            : hasTree
            ? "Re-extract"
            : "Extract Now"}
        </button>
      </div>

      <div className="flex gap-4 text-sm text-gray-500 mb-6">
        <a
          href={policy.pdf_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-500 hover:underline"
        >
          View PDF
        </a>
        <a
          href={policy.source_page_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-500 hover:underline"
        >
          Source Page
        </a>
      </div>

      {/* Failed extraction block */}
      {structured && hasError && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4">
          <h3 className="text-sm font-semibold text-red-800 mb-2">
            ✗ Extraction failed
          </h3>
          <div className="text-sm text-red-700 mb-3">
            <div className="mb-1">
              <span className="font-medium">Method:</span>{" "}
              {structured.extraction_method || "unknown"}
            </div>
            <div className="mb-1">
              <span className="font-medium">Last attempt:</span>{" "}
              {structured.structured_at}
            </div>
          </div>
          <div className="text-xs text-red-700 bg-red-100 p-2 rounded font-mono whitespace-pre-wrap break-words">
            {structured.validation_error}
          </div>
          {structured.llm_metadata?.errors?.length > 0 && (
            <details className="mt-3 text-xs text-red-700">
              <summary className="cursor-pointer font-medium">
                LLM error history ({structured.llm_metadata.errors.length})
              </summary>
              <ul className="mt-2 space-y-1 bg-red-100 p-2 rounded font-mono">
                {structured.llm_metadata.errors.map(
                  (err: string, i: number) => (
                    <li key={i} className="break-words">
                      {err}
                    </li>
                  ),
                )}
              </ul>
            </details>
          )}
        </div>
      )}

      {/* Successful tree */}
      {hasTree && tree && (
        <div className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-lg font-semibold">{tree.title}</h2>
              <p className="text-sm text-gray-500">
                {tree.insurance_name} · {structured?.extraction_method} ·{" "}
                {structured?.structured_at}
              </p>
            </div>
            <button
              onClick={() => {
                setAllExpanded(!allExpanded);
                setTreeKey((k) => k + 1);
              }}
              className="text-sm px-3 py-1 border rounded hover:bg-gray-50"
            >
              {allExpanded ? "Collapse All" : "Expand All"}
            </button>
          </div>
          <CriteriaNode
            key={treeKey}
            node={tree.rules}
            depth={0}
            defaultExpanded={allExpanded}
          />
        </div>
      )}

      {/* No attempt yet */}
      {!structured && (
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-8 text-center text-gray-500">
          Not yet extracted. Click "Extract Now" to structure this policy.
        </div>
      )}
    </div>
  );
}
