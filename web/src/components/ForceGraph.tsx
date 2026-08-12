import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphEntity, GraphRelationship } from "@/api/client";
import { ENTITY_TYPE_LABELS, REL_TYPE_LABELS, entityColor, relColor, relWidth } from "@/components/graphMeta";

/**
 * 轻量 SVG 力导向图（无外部依赖）。
 *
 * 节点 = 实体（圆圈，颜色按 entity_type），边 = 关系（连线，颜色/粗细按 rel_type）。
 * 交互：拖拽节点、悬停高亮邻居并显示名称、点击节点回调 onSelect。
 * 颜色全部走语义 token（var(--chart-*) / var(--muted-foreground)），自动适配明暗主题。
 */

interface FNode {
  id: string;
  name: string;
  type: string;
  confidence: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
  degree: number;
}

interface FEdge {
  source: string;
  target: string;
  rel_type: string;
  confidence: number;
}

const W = 920;
const H = 520;

/**
 * Build the initial graph layout (pure): node/edge arrays derived from the
 * props, with node radii scaled by degree and starting positions spread on a
 * circle so the graph is legible before the physics simulation warms up.
 */
function buildGraph(entities: GraphEntity[], relationships: GraphRelationship[]): { nodes: FNode[]; edges: FEdge[] } {
  const nodes: FNode[] = entities.map((e) => ({
    id: e.entity_id,
    name: e.name,
    type: e.entity_type,
    confidence: e.confidence,
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
    r: 6,
    degree: 0,
  }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges: FEdge[] = [];
  for (const r of relationships) {
    if (r.source_id === r.target_id) continue;
    if (byId.has(r.source_id) && byId.has(r.target_id)) {
      edges.push({ source: r.source_id, target: r.target_id, rel_type: r.rel_type, confidence: r.confidence });
    }
  }
  for (const e of edges) {
    byId.get(e.source)!.degree++;
    byId.get(e.target)!.degree++;
  }
  for (const n of nodes) n.r = 6 + Math.min(n.degree, 6);

  const count = nodes.length;
  const radius = count === 1 ? 0 : Math.min(200, 70 + count * 4);
  nodes.forEach((n, i) => {
    const angle = count === 1 ? 0 : (i / count) * Math.PI * 2;
    n.x = W / 2 + Math.cos(angle) * radius + (Math.random() - 0.5) * 30;
    n.y = H / 2 + Math.sin(angle) * radius + (Math.random() - 0.5) * 30;
  });

  return { nodes, edges };
}

interface ForceGraphProps {
  entities: GraphEntity[];
  relationships: GraphRelationship[];
  selectedId?: string | null;
  onSelect?: (entity: GraphEntity) => void;
}

export function ForceGraph({ entities, relationships, selectedId, onSelect }: ForceGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const nodesRef = useRef<FNode[]>([]);
  const edgesRef = useRef<FEdge[]>([]);
  const rafRef = useRef<number | null>(null);
  const draggingRef = useRef<FNode | null>(null);
  const movedRef = useRef(false);

  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; name: string; type: string } | null>(null);
  // Render snapshot. Using STATE (not a ref) for the rendered nodes/edges is
  // the key: a ref change never triggers a React render, so on first mount the
  // SVG was empty (nodesRef populated by the effect, but no re-render happened
  // with the new ref value). We write both the ref (for the physics loop) and
  // this snapshot (for rendering).
  const [graphSnapshot, setGraphSnapshot] = useState<{ nodes: FNode[]; edges: FEdge[] }>({ nodes: [], edges: [] });
  // `frame` value is intentionally never read — bumping it alone re-renders the
  // SVG after each physics tick. Node positions are mutated in place on the
  // `graphSnapshot` objects, so the render reads fresh coordinates each time.
  const [, setFrame] = useState(0);

  // Rebuild the graph whenever the data changes. This commits state DURING
  // render (the React-sanctioned "adjust state while rendering" pattern) rather
  // than in an effect, so the physics snapshot is ready before the first paint
  // and react-hooks/set-state-in-effect has nothing to flag. `entities` /
  // `relationships` come from the parent memoized, so the comparison settles.
  const [builtFor, setBuiltFor] = useState<{ entities: GraphEntity[]; relationships: GraphRelationship[] } | null>(null);
  if (builtFor?.entities !== entities || builtFor?.relationships !== relationships) {
    setBuiltFor({ entities, relationships });
    setGraphSnapshot(buildGraph(entities, relationships));
  }

  // One physics tick: repulsion + spring attraction + centering + damping.
  const step = useCallback((): number => {
    const nodes = nodesRef.current;
    const edges = edgesRef.current;
    if (nodes.length === 0) return 0;

    const byId = new Map(nodes.map((n) => [n.id, n]));
    const REPEL = 1600;
    const SPRING_K = 0.022;
    const REST = 150;
    const CENTER_K = 0.012;
    const DAMP = 0.85;
    const MAX_SPEED = 9;
    const MAX_FORCE = 9;

    // Pairwise repulsion (inverse-square, clamped).
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          d2 = 1;
        }
        const d = Math.sqrt(d2);
        if (d < 230) {
          const f = Math.min(REPEL / d2, MAX_FORCE);
          const fx = (dx / d) * f;
          const fy = (dy / d) * f;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
      }
    }

    // Spring attraction along edges.
    for (const e of edges) {
      const a = byId.get(e.source);
      const b = byId.get(e.target);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = SPRING_K * (d - REST);
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    // Centering pull.
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * CENTER_K;
      n.vy += (H / 2 - n.y) * CENTER_K;
    }

    // Integrate + clamp to bounds.
    let energy = 0;
    for (const n of nodes) {
      n.vx *= DAMP;
      n.vy *= DAMP;
      const sp = Math.hypot(n.vx, n.vy);
      if (sp > MAX_SPEED) {
        n.vx *= MAX_SPEED / sp;
        n.vy *= MAX_SPEED / sp;
      }
      n.x += n.vx;
      n.y += n.vy;
      n.x = Math.max(24, Math.min(W - 24, n.x));
      n.y = Math.max(24, Math.min(H - 24, n.y));
      energy += sp;
    }
    return energy / nodes.length;
  }, []);

  const startSim = useCallback(() => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    let ticks = 0;
    const loop = () => {
      const energy = step();
      setFrame((f) => f + 1);
      ticks++;
      if (energy > 0.6 && ticks < 500 && !draggingRef.current) {
        rafRef.current = requestAnimationFrame(loop);
      } else {
        rafRef.current = null;
      }
    };
    rafRef.current = requestAnimationFrame(loop);
  }, [step]);

  // Simulation lifecycle: keep the physics refs in sync with the committed
  // graph and restart the animation whenever the graph is rebuilt.
  useEffect(() => {
    nodesRef.current = graphSnapshot.nodes;
    edgesRef.current = graphSnapshot.edges;
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    draggingRef.current = null;
    startSim();

    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
  }, [graphSnapshot, startSim]);

  // Hover highlight set: the node itself + its direct neighbors.
  const highlightId = hoveredId ?? selectedId ?? null;
  const neighborIds = useMemo(() => {
    const set = new Set<string>();
    if (!highlightId) return set;
    set.add(highlightId);
    for (const e of graphSnapshot.edges) {
      if (e.source === highlightId) set.add(e.target);
      if (e.target === highlightId) set.add(e.source);
    }
    return set;
  }, [highlightId, graphSnapshot]);

  const nodeById = useMemo(() => new Map(graphSnapshot.nodes.map((n) => [n.id, n])), [graphSnapshot]);

  // ── drag / pointer helpers ──
  const toSvgPoint = (clientX: number, clientY: number) => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    return {
      x: (clientX - rect.left) / (rect.width / W),
      y: (clientY - rect.top) / (rect.height / H),
    };
  };

  const handlePointerDown = (e: React.PointerEvent, id: string) => {
    const node = nodeById.get(id);
    if (!node) return;
    e.preventDefault();
    const p = toSvgPoint(e.clientX, e.clientY);
    node.x = p.x;
    node.y = p.y;
    node.vx = 0;
    node.vy = 0;
    movedRef.current = false;
    draggingRef.current = node;
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    setFrame((f) => f + 1);
  };

  const handlePointerMove = (e: React.PointerEvent) => {
    if (draggingRef.current) {
      const p = toSvgPoint(e.clientX, e.clientY);
      draggingRef.current.x = Math.max(24, Math.min(W - 24, p.x));
      draggingRef.current.y = Math.max(24, Math.min(H - 24, p.y));
      movedRef.current = true;
      setFrame((f) => f + 1);
      return;
    }
    // Hover tooltip.
    const target = nodeById.get((e.currentTarget as Element).getAttribute("data-id") ?? "");
    if (target) {
      const svg = svgRef.current;
      const container = containerRef.current;
      if (svg && container) {
        const sRect = svg.getBoundingClientRect();
        const cRect = container.getBoundingClientRect();
        setTooltip({
          x: sRect.left - cRect.left + (target.x / W) * sRect.width,
          y: sRect.top - cRect.top + (target.y / H) * sRect.height,
          name: target.name,
          type: target.type,
        });
      }
    }
  };

  const handlePointerEnd = () => {
    if (draggingRef.current) {
      draggingRef.current = null;
      startSim();
    }
    setHoveredId(null);
    setTooltip(null);
  };

  const nodes = graphSnapshot.nodes;
  const edges = graphSnapshot.edges;

  return (
    <div ref={containerRef} className="relative overflow-hidden rounded-lg border bg-background/40">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto select-none touch-none"
        role="img"
        aria-label="知识图谱力导向可视化"
      >
        {/* edges */}
        {edges.map((e, i) => {
          const a = nodeById.get(e.source);
          const b = nodeById.get(e.target);
          if (!a || !b) return null;
          const active = !highlightId || (neighborIds.has(a.id) && neighborIds.has(b.id));
          return (
            <line
              key={i}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={relColor(e.rel_type)}
              strokeWidth={relWidth(e.rel_type)}
              strokeOpacity={(0.3 + e.confidence * 0.45) * (active ? 1 : 0.12)}
              strokeLinecap="round"
            />
          );
        })}
        {/* nodes */}
        {nodes.map((n) => {
          const active = !highlightId || neighborIds.has(n.id);
          const isSelected = n.id === selectedId;
          return (
            <g
              key={n.id}
              data-id={n.id}
              className="cursor-pointer"
              onPointerDown={(e) => handlePointerDown(e, n.id)}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerEnd}
              onPointerCancel={handlePointerEnd}
              onPointerEnter={() => setHoveredId(n.id)}
              onPointerLeave={() => {
                if (!draggingRef.current) {
                  setHoveredId(null);
                  setTooltip(null);
                }
              }}
              onClick={(e) => {
                e.stopPropagation();
                if (movedRef.current) {
                  movedRef.current = false;
                  return;
                }
                const ent = entities.find((x) => x.entity_id === n.id);
                if (ent) onSelect?.(ent);
              }}
            >
              {isSelected && (
                <circle cx={n.x} cy={n.y} r={n.r + 5} fill="none" stroke="var(--primary)" strokeWidth={1.5} strokeDasharray="3 3" />
              )}
              <circle cx={n.x} cy={n.y} r={n.r} fill={entityColor(n.type)} fillOpacity={active ? 0.92 : 0.18} />
              <circle cx={n.x} cy={n.y} r={Math.max(n.r, 15)} fill="transparent" />
            </g>
          );
        })}
      </svg>

      {/* hint */}
      <div className="pointer-events-none absolute left-2.5 top-2 text-[11px] text-muted-foreground">
        拖拽节点 · 悬停高亮邻居 · 点击查看关系
      </div>

      {/* hover tooltip */}
      {tooltip && (
        <div
          className="pointer-events-none absolute z-10 rounded-md bg-popover px-2.5 py-1.5 text-xs shadow-md ring-1 ring-border"
          style={{ left: tooltip.x, top: tooltip.y, transform: "translate(-50%, -130%)" }}
        >
          <div className="font-medium text-popover-foreground">{tooltip.name}</div>
          <div className="text-muted-foreground">{ENTITY_TYPE_LABELS[tooltip.type] ?? tooltip.type}</div>
        </div>
      )}

      {/* legend */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t px-3 py-2 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">实体</span>
        {Object.entries(ENTITY_TYPE_LABELS).map(([t, label]) => (
          <span key={t} className="inline-flex items-center gap-1.5">
            <span className="size-2.5 rounded-full" style={{ background: entityColor(t) }} />
            {label}
          </span>
        ))}
        <span className="font-medium text-foreground ml-1">关系</span>
        {Object.entries(REL_TYPE_LABELS).map(([t, label]) => (
          <span key={t} className="inline-flex items-center gap-1.5">
            <span className="w-4 h-0.5 rounded" style={{ background: relColor(t) }} />
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}

export default ForceGraph;
