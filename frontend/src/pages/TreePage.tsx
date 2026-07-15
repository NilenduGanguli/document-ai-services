/**
 * Knowledge tree — the nested subtree for a client (optionally one document).
 *
 * The tree implements the WAI-ARIA treeview keyboard model: ArrowUp/Down move
 * through visible rows, ArrowRight/Left expand/collapse or move to parent,
 * Home/End jump to the ends, Enter/Space open the provenance drawer.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { getTree } from '../lib/api';
import { useAsync } from '../hooks/useAsync';
import { useApiKey, useClientId, useMask } from '../hooks/useSettings';
import type { TreeNode } from '../lib/types';
import { Badge, SensitivityBadge, VerificationBadge } from '../components/Badge';
import { IconChevronRight, IconRefresh, IconTree } from '../components/Icons';
import { ProvenanceDrawer } from '../components/ProvenanceDrawer';
import { EmptyState, ErrorState, NeedsClient, NeedsKey, SkeletonLines } from '../components/States';

/** Short glyph per node type for the icon chip. */
const TYPE_GLYPH: Record<string, string> = {
  document: 'D',
  section: '§',
  chunk: '¶',
  table: '▦',
  figure: '◈',
  fact: 'F',
  summary: 'S',
};

interface FlatRow {
  node: TreeNode;
  depth: number;
  parentId: string | null;
  hasChildren: boolean;
}

/** Flatten the visible rows (respecting collapsed state) for keyboard nav. */
function flatten(
  nodes: TreeNode[],
  expanded: Set<string>,
  depth = 0,
  parentId: string | null = null,
  out: FlatRow[] = [],
): FlatRow[] {
  for (const node of nodes) {
    const hasChildren = (node.children?.length ?? 0) > 0;
    out.push({ node, depth, parentId, hasChildren });
    if (hasChildren && expanded.has(node.id)) {
      flatten(node.children, expanded, depth + 1, node.id, out);
    }
  }
  return out;
}

function nodeLabel(node: TreeNode): string {
  return (
    node.title ||
    node.attribute_key ||
    (node.content ? node.content.slice(0, 90) : '') ||
    node.path ||
    node.node_type
  );
}

/** Ids to auto-expand: roots and their immediate children. */
function initialExpanded(tree: TreeNode[]): Set<string> {
  const ids = new Set<string>();
  for (const root of tree) {
    ids.add(root.id);
    for (const child of root.children ?? []) ids.add(child.id);
  }
  return ids;
}

export function TreePage(): JSX.Element {
  const [clientId] = useClientId();
  const [apiKey] = useApiKey();
  const [mask, setMask] = useMask();
  const [params, setParams] = useSearchParams();

  const docId = params.get('doc_id');
  const pathFilter = params.get('path') ?? '';
  const [pathInput, setPathInput] = useState(pathFilter);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [drawerNode, setDrawerNode] = useState<TreeNode | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());

  const enabled = !!clientId && !!apiKey;
  const { data, error, loading, reload } = useAsync(
    (signal) =>
      getTree({ clientId, docId, path: pathFilter || null, mask, signal }),
    [clientId, docId, pathFilter, mask],
    enabled,
  );

  const tree = useMemo(() => data?.tree ?? [], [data]);

  // Auto-expand the top two levels whenever a new tree arrives.
  useEffect(() => {
    if (tree.length) setExpanded(initialExpanded(tree));
  }, [tree]);

  const rows = useMemo(() => flatten(tree, expanded), [tree, expanded]);

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const focusRow = useCallback((id: string) => {
    setSelected(id);
    rowRefs.current.get(id)?.focus();
  }, []);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>, row: FlatRow) => {
      const idx = rows.findIndex((r) => r.node.id === row.node.id);
      const isOpen = expanded.has(row.node.id);

      switch (e.key) {
        case 'ArrowDown': {
          e.preventDefault();
          const next = rows[idx + 1];
          if (next) focusRow(next.node.id);
          break;
        }
        case 'ArrowUp': {
          e.preventDefault();
          const prev = rows[idx - 1];
          if (prev) focusRow(prev.node.id);
          break;
        }
        case 'ArrowRight': {
          e.preventDefault();
          if (row.hasChildren && !isOpen) toggle(row.node.id);
          else if (row.hasChildren && isOpen) {
            const next = rows[idx + 1];
            if (next) focusRow(next.node.id);
          }
          break;
        }
        case 'ArrowLeft': {
          e.preventDefault();
          if (row.hasChildren && isOpen) toggle(row.node.id);
          else if (row.parentId) focusRow(row.parentId);
          break;
        }
        case 'Home': {
          e.preventDefault();
          const first = rows[0];
          if (first) focusRow(first.node.id);
          break;
        }
        case 'End': {
          e.preventDefault();
          const last = rows[rows.length - 1];
          if (last) focusRow(last.node.id);
          break;
        }
        case 'Enter':
        case ' ': {
          e.preventDefault();
          setDrawerNode(row.node);
          break;
        }
        default:
          break;
      }
    },
    [rows, expanded, toggle, focusRow],
  );

  if (!clientId) return <main className="page"><NeedsClient /></main>;
  if (!apiKey) return <main className="page"><NeedsKey /></main>;

  const renderNodes = (nodes: TreeNode[], depth: number, parentId: string | null): JSX.Element => (
    <ul role={depth === 0 ? undefined : 'group'}>
      {nodes.map((node) => {
        const hasChildren = (node.children?.length ?? 0) > 0;
        const isOpen = expanded.has(node.id);
        return (
          <li
            key={node.id}
            role="treeitem"
            aria-expanded={hasChildren ? isOpen : undefined}
            aria-selected={selected === node.id}
          >
            <div
              className={`tree-row${selected === node.id ? ' selected' : ''}`}
              tabIndex={selected === node.id || (!selected && depth === 0 && nodes[0] === node) ? 0 : -1}
              ref={(el) => {
                if (el) rowRefs.current.set(node.id, el);
                else rowRefs.current.delete(node.id);
              }}
              onClick={() => {
                setSelected(node.id);
                setDrawerNode(node);
              }}
              onKeyDown={(e) => onKeyDown(e, { node, depth, parentId, hasChildren })}
            >
              <button
                type="button"
                className={`tree-caret${isOpen ? ' open' : ''}${hasChildren ? '' : ' leaf'}`}
                onClick={(e) => {
                  e.stopPropagation();
                  toggle(node.id);
                }}
                tabIndex={-1}
                aria-label={isOpen ? 'Collapse' : 'Expand'}
              >
                <IconChevronRight size={13} />
              </button>

              <span className={`node-icon ${node.node_type}`} title={String(node.node_type)}>
                {TYPE_GLYPH[String(node.node_type)] ?? '•'}
              </span>

              <span className="tree-label" title={nodeLabel(node)}>
                {nodeLabel(node)}
              </span>

              {node.value_text && (
                <span className="tree-value" title={node.value_text}>
                  {node.value_text}
                </span>
              )}

              <span className="tree-spacer" />

              {node.masked && <Badge tone="neutral">masked</Badge>}
              {node.node_type === 'fact' && node.verification_status && (
                <VerificationBadge value={node.verification_status} />
              )}
              {node.sensitivity && node.sensitivity !== 'LOW' && (
                <SensitivityBadge value={node.sensitivity} />
              )}
            </div>

            {hasChildren && isOpen && renderNodes(node.children, depth + 1, node.id)}
          </li>
        );
      })}
    </ul>
  );

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">Knowledge tree</h1>
          <p className="page-sub">
            {docId ? (
              <>
                Scoped to document <code>{docId}</code>.{' '}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.delete('doc_id');
                    setParams(next);
                  }}
                >
                  Show all documents
                </button>
              </>
            ) : (
              'Every node this client knows. Select a node to see exactly where it came from.'
            )}
          </p>
        </div>
        <div className="toolbar">
          <label className="switch">
            <input type="checkbox" checked={mask} onChange={(e) => setMask(e.target.checked)} />
            <span className="switch-track" />
            Mask PII
          </label>
          <button type="button" className="btn btn-sm" onClick={reload} disabled={loading}>
            <IconRefresh size={13} /> Refresh
          </button>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <div className="card-title">{data ? `${data.count} nodes` : 'Tree'}</div>
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              const next = new URLSearchParams(params);
              if (pathInput.trim()) next.set('path', pathInput.trim());
              else next.delete('path');
              setParams(next);
            }}
          >
            <label className="field">
              <span className="sr-only">Filter by path prefix</span>
              <input
                className="input input-inline"
                value={pathInput}
                onChange={(e) => setPathInput(e.target.value)}
                placeholder="filter by path prefix…"
                spellCheck={false}
                style={{ minWidth: 200 }}
              />
            </label>
            <button type="submit" className="btn btn-sm">
              Apply
            </button>
          </form>
        </div>

        <div className="card-body">
          {loading && !data && <SkeletonLines lines={8} />}
          {error && <ErrorState error={error} onRetry={reload} />}
          {data && tree.length === 0 && (
            <EmptyState
              title="Nothing here"
              text={
                pathFilter
                  ? `No nodes match the path prefix "${pathFilter}".`
                  : 'This client has no knowledge nodes yet. Ingest a document first.'
              }
              icon={<IconTree size={20} />}
            />
          )}
          {tree.length > 0 && (
            <div className="tree" role="tree" aria-label="Knowledge tree">
              {renderNodes(tree, 0, null)}
            </div>
          )}
        </div>
      </section>

      <ProvenanceDrawer
        nodeId={drawerNode?.id ?? null}
        clientId={clientId}
        titleHint={drawerNode ? nodeLabel(drawerNode) : null}
        onClose={() => setDrawerNode(null)}
      />
    </main>
  );
}
