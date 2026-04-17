import { useState } from "react";

interface RuleNode {
  rule_id: string;
  rule_text: string;
  operator?: string;
  rules?: RuleNode[];
}

interface CriteriaNodeProps {
  node: RuleNode;
  depth: number;
  defaultExpanded?: boolean; // passed down to set initial expansion state
}

export default function CriteriaNode({
  node,
  depth,
  defaultExpanded,
}: CriteriaNodeProps) {
  // If parent forced expansion state, use it; otherwise default to depth < 2
  const initial = defaultExpanded !== undefined ? defaultExpanded : depth < 2;
  const [expanded, setExpanded] = useState(initial);
  const isLeaf = !node.rules || node.rules.length === 0;

  // Cap indentation at depth 5 to keep long rule_ids from pushing content off-screen
  const indent = Math.min(depth, 5) * 24;

  return (
    <div style={{ marginLeft: indent }} className="my-1">
      <div
        className={`flex items-start gap-2 p-2 rounded cursor-pointer hover:bg-gray-100 ${
          !isLeaf ? "font-medium" : ""
        }`}
        onClick={() => !isLeaf && setExpanded(!expanded)}
      >
        {/* Expand/collapse toggle */}
        {!isLeaf ? (
          <span className="text-gray-400 w-4 flex-shrink-0 mt-0.5">
            {expanded ? "▼" : "▶"}
          </span>
        ) : (
          <span className="text-green-500 w-4 flex-shrink-0 mt-0.5">•</span>
        )}

        {/* Operator badge */}
        {node.operator && (
          <span
            className={`text-xs px-1.5 py-0.5 rounded font-bold flex-shrink-0 ${
              node.operator === "AND"
                ? "bg-blue-100 text-blue-700"
                : "bg-amber-100 text-amber-700"
            }`}
          >
            {node.operator}
          </span>
        )}

        {/* Rule ID + text (wraps long text instead of overflowing) */}
        <span className="flex-1 min-w-0 leading-relaxed">
          <span className="text-gray-400 text-sm mr-1 whitespace-nowrap">
            {node.rule_id}
          </span>
          <span className="text-gray-800 break-words">{node.rule_text}</span>
        </span>
      </div>

      {/* Children */}
      {expanded && node.rules && (
        <div>
          {node.rules.map((child) => (
            <CriteriaNode
              key={child.rule_id}
              node={child}
              depth={depth + 1}
              defaultExpanded={defaultExpanded}
            />
          ))}
        </div>
      )}
    </div>
  );
}
