'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ComponentType, MutableRefObject } from 'react';
import dynamic from 'next/dynamic';
import type { ForceGraphProps, NodeObject, LinkObject } from 'react-force-graph-2d';
import { forceX, forceY } from 'd3-force-3d';
import { forceCollide } from 'd3-force';
import { fetchFullGraph, fetchSynthesisStatus, postForceSynthesis } from '@/lib/api';
import type { ConfidenceLevel } from '@/lib/types';
import RedTeamReview from './RedTeamReview';

// ── Dynamic import: react-force-graph-2d requires window/canvas ──────────────
const ForceGraph2D = dynamic<ForceGraphProps>(
  () => import('react-force-graph-2d'),
  { ssr: false },
);
// FCwithRef in the package uses a generic function type that dynamic<T> can't
// express; cast through unknown to make ref forwarding work at runtime.
const FG2D = ForceGraph2D as unknown as ComponentType<
  ForceGraphProps & { ref?: MutableRefObject<any> }
>;

// ── Colour palette ────────────────────────────────────────────────────────────

const NODE_COLORS: Record<ConfidenceLevel, string> = {
  confirmed:  '#06b6d4',  // cyan-500
  testing:    '#10b981',  // emerald-500
  hypothesis: '#f43f5e',  // rose-500
};

const EDGE_COLORS: Record<string, string> = {
  CAUSES:          '#f43f5e',  // rose
  MITIGATES:       '#10b981',  // emerald
  EXACERBATES:     '#f97316',  // orange
  CORRELATES_WITH: '#06b6d4',  // cyan
  REQUIRES:        '#8b5cf6',  // violet
  PRECEDES:        '#3b82f6',  // blue
};

const CANVAS_BG = '#09090b';

// ── Semantic geography ────────────────────────────────────────────────────────
// Target X in simulation coordinates.  Unknown categories → 0 (centre).
// The actual canvas origin sits at (width/2, height/2) in screen space, so
// these values describe how far left / right of centre a zone should sit.

const CAT_ZONE_X: Record<string, number> = {
  // ── Left: interventions & protocols ───────────────────────────────────────
  supplement:     -220,
  protocol:     -220,
  intervention: -220,
  medication:   -220,
  drug:         -220,
  treatment:    -220,
  // ── Centre: diagnostics & biomarkers ────────────────────────────────────────
  lab_result:   0,
  biomarker:    0,
  diagnostic:   0,
  test:         0,
  marker:       0,
  // ── Right: symptoms & outcomes ────────────────────────────────────────────
  symptom:      220,
  biometric:    220,
  observation:  220,
  condition:    220,
  outcome:      220,
  side_effect:  220,
};

/** Normalise a category string to the lookup key format. */
function normCat(cat: string): string {
  return (cat ?? '').toLowerCase().replace(/[\s-]+/g, '_');
}

type CatZone = 'left' | 'center' | 'right' | 'unknown';

function catZone(cat: string): CatZone {
  const x = CAT_ZONE_X[normCat(cat)] ?? null;
  if (x === null) return 'unknown';
  if (x < -50)   return 'left';
  if (x >  50)   return 'right';
  return 'center';
}

/** HUD pill accent colour per zone. */
const ZONE_ACCENT: Record<CatZone, string> = {
  left:    '#06b6d4',  // cyan
  center:  '#10b981',  // emerald
  right:   '#f43f5e',  // rose
  unknown: '#a1a1aa',  // zinc-400
};

// ── Edge weight ───────────────────────────────────────────────────────────────

/** Stroke width by relationship type; causal links are thicker. */
function relWidth(relType: string): number {
  switch (relType) {
    case 'CAUSES':      return 2.5;
    case 'EXACERBATES': return 2.2;
    case 'MITIGATES':   return 2.0;
    case 'REQUIRES':    return 1.8;
    case 'PRECEDES':    return 1.4;
    default:            return 1.0;  // CORRELATES_WITH (thinnest / most uncertain)
  }
}

/** Hex alpha suffix: correlational edges get 53% opacity, causal edges stay full. */
function relAlphaSuffix(relType: string): string {
  return relType === 'CORRELATES_WITH' ? '88' : 'ff';
}

// ── Graph data types ──────────────────────────────────────────────────────────
// NodeObject carries [others: string]: any so extra fields are safe at runtime.

type GNode = NodeObject & {
  id: string;
  label: string;
  nodeColor: string;
  confidence: ConfidenceLevel;
  category: string;
  summary: string;
  lastUpdated: string;
  /** Edges touching this node; drives radius, opacity, and D3 physics weight. */
  degree: number;
  /** D3 physics weight (auto-read via the default nodeVal="val" accessor). */
  val: number;
};

type GLink = LinkObject & {
  linkColor: string;
  relType: string;
  evidence: string | null;
};

// ── NodePanel ─────────────────────────────────────────────────────────────────

const CONF_STYLES: Record<ConfidenceLevel, { label: string; cls: string }> = {
  confirmed:  { label: 'CONFIRMED',  cls: 'text-cyan-400    border-cyan-800/50    bg-cyan-950/60'    },
  testing:    { label: 'TESTING',    cls: 'text-emerald-400 border-emerald-800/50 bg-emerald-950/60' },
  hypothesis: { label: 'HYPOTHESIS', cls: 'text-rose-400    border-rose-800/50    bg-rose-950/60'    },
};

function NodePanel({ node, onClose }: { node: GNode; onClose: () => void }) {
  const conf = CONF_STYLES[node.confidence] ?? CONF_STYLES.hypothesis;
  return (
    <div className="absolute right-3 top-3 z-20 w-72 border border-zinc-700/80 bg-zinc-900/95 shadow-2xl ring-1 ring-zinc-800/50 backdrop-blur-sm">
      <div className="flex items-center justify-between border-b border-zinc-800/60 px-3.5 py-2.5">
        <span className="font-mono text-[0.625rem] tracking-[0.18em] text-zinc-500">NODE DETAIL</span>
        <button
          onClick={onClose}
          aria-label="Close panel"
          className="font-mono text-[0.75rem] text-zinc-600 transition-colors hover:text-zinc-300"
        >
          ✕
        </button>
      </div>
      <div className="flex flex-col gap-3 px-3.5 py-3">
        <p className="font-mono text-[0.875rem] font-bold leading-snug text-white">{node.label}</p>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`border px-2 py-0.5 font-mono text-[0.625rem] tracking-widest ${conf.cls}`}>
            {conf.label}
          </span>
          <span className="font-mono text-[0.625rem] uppercase tracking-widest text-zinc-500">
            {node.category}
          </span>
        </div>
        {node.summary && (
          <p className="font-sans text-xs leading-relaxed text-zinc-300">{node.summary}</p>
        )}
        <p className="font-mono text-[0.625rem] tracking-widest text-zinc-600">
          UPDATED {node.lastUpdated.slice(0, 10)}
        </p>
      </div>
    </div>
  );
}

// ── Legend ────────────────────────────────────────────────────────────────────

function Legend() {
  const items: [ConfidenceLevel, string][] = [
    ['confirmed',  '#06b6d4'],
    ['testing',    '#10b981'],
    ['hypothesis', '#f43f5e'],
  ];
  return (
    <div className="flex items-center gap-4">
      {items.map(([lvl, col]) => (
        <div key={lvl} className="flex items-center gap-1.5">
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: col }} />
          <span className="font-mono text-[0.625rem] uppercase tracking-widest text-zinc-500">{lvl}</span>
        </div>
      ))}
    </div>
  );
}

// ── Countdown formatter ───────────────────────────────────────────────────────

function fmtCountdown(s: number): string {
  return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, '0')}`;
}

// ── Main component ────────────────────────────────────────────────────────────

type SynthState = 'idle' | 'running' | 'cooldown';
type LoadState  = 'loading' | 'empty' | 'ready';

interface NetworkGraphProps {
  /** Bump this from the parent (Dashboard) to trigger a re-fetch even when the
   *  component was unmounted when the SYNTHESIS_COMPLETE event fired. */
  externalRefreshKey?: number;
}

export default function NetworkGraph({ externalRefreshKey = 0 }: NetworkGraphProps) {

  // ── Graph data ──────────────────────────────────────────────────────────────
  const [gNodes, setGNodes] = useState<GNode[]>([]);
  const [gLinks, setGLinks] = useState<GLink[]>([]);
  const [loadState,    setLoadState]    = useState<LoadState>('loading');

  // ── Interaction ─────────────────────────────────────────────────────────────
  const [selectedNode, setSelectedNode] = useState<GNode | null>(null);
  const [showRedTeam,  setShowRedTeam]  = useState(false);

  // ── Category filter ─────────────────────────────────────────────────────────
  const [allCategories,    setAllCategories]    = useState<string[]>([]);
  const [activeCategories, setActiveCategories] = useState<Set<string>>(new Set());

  // ── Synthesis ───────────────────────────────────────────────────────────────
  const [synthState,    setSynthState]    = useState<SynthState>('idle');
  const [cooldown,      setCooldown]      = useState(0);
  const [lastSynthDate, setLastSynthDate] = useState<string | null>(null);
  const [synthSummary,  setSynthSummary]  = useState<string | null>(null);
  /** Bumped internally after synthesis or red-team updates to re-fetch the graph. */
  const [refreshKey, setRefreshKey] = useState(0);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Canvas sizing ────────────────────────────────────────────────────────────
  const containerRef = useRef<HTMLDivElement>(null);
  /** Imperative ref to FG2D, used to tune D3 forces after mount. */
  const graphRef     = useRef<any>(null);
  /** Per-frame occlusion: bounding boxes of labels already drawn this frame. */
  const drawnRectsRef    = useRef<Array<{ x1: number; x2: number; y1: number; y2: number }>>([]);
  /** Timestamp of the first paintNode call in the current frame (ms). */
  const lastFrameTimeRef = useRef<number>(0);
  const [size, setSize] = useState({ w: 800, h: 600 });

  // ── Derived: per-category node count (HUD badges) ───────────────────────────
  const catCounts = useMemo(() => {
    const m = new Map<string, number>();
    gNodes.forEach((n) => m.set(n.category, (m.get(n.category) ?? 0) + 1));
    return m;
  }, [gNodes]);

  // ── Resize observer ──────────────────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ w: width, h: height });
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  // ── Synthesis status on mount ────────────────────────────────────────────────
  useEffect(() => {
    fetchSynthesisStatus()
      .then((s) => {
        setLastSynthDate(s.last_synthesis_date);
        if (s.cooldown_remaining_seconds > 0) {
          setCooldown(s.cooldown_remaining_seconds);
          setSynthState('cooldown');
        }
      })
      .catch(() => {});
  }, []);

  // ── Cooldown countdown tick ──────────────────────────────────────────────────
  useEffect(() => {
    if (synthState !== 'cooldown') {
      if (tickRef.current) clearInterval(tickRef.current);
      tickRef.current = null;
      return;
    }
    tickRef.current = setInterval(() => {
      setCooldown((prev) => {
        if (prev <= 1) { setSynthState('idle'); return 0; }
        return prev - 1;
      });
    }, 1000);
    return () => { if (tickRef.current) clearInterval(tickRef.current); };
  }, [synthState]);

  // ── Data fetch ───────────────────────────────────────────────────────────────
  useEffect(() => {
    setLoadState('loading');
    fetchFullGraph()
      .then((data) => {
        if (data.nodes.length === 0) {
          setLoadState('empty');
          setGNodes([]);
          setGLinks([]);
          setAllCategories([]);
          setActiveCategories(new Set());
          return;
        }

        const nodes: GNode[] = data.nodes.map((n) => ({
          id:          n.concept_name,
          label:       n.concept_name,
          nodeColor:   NODE_COLORS[n.confidence_level] ?? '#52525b',
          confidence:  n.confidence_level,
          category:    n.category,
          summary:     n.summary_text,
          lastUpdated: n.last_updated,
          degree:      0,  // patched below once links are known
          val:         1,
        }));

        const nodeIds = new Set(nodes.map((n) => n.id));
        const links: GLink[] = data.edges
          .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
          .map((e) => ({
            source:    e.source,
            target:    e.target,
            linkColor: EDGE_COLORS[e.relationship_type] ?? '#3f3f46',
            relType:   e.relationship_type,
            evidence:  e.evidence_summary,
          }));

        // Degree → radius, opacity, D3 physics weight
        const degreeMap = new Map<string, number>();
        links.forEach((l) => {
          const src = l.source as string;
          const tgt = l.target as string;
          degreeMap.set(src, (degreeMap.get(src) ?? 0) + 1);
          degreeMap.set(tgt, (degreeMap.get(tgt) ?? 0) + 1);
        });
        nodes.forEach((n) => {
          const d = degreeMap.get(n.id) ?? 0;
          n.degree = d;
          n.val    = Math.max(1, d);
        });
        // Sort descending by degree so paintNode processes hubs first; this
        // makes per-frame label occlusion deterministic and flicker-free.
        // Tie-break by ID so the order is fully stable across frames.
        nodes.sort((a, b) => b.degree - a.degree || a.id.localeCompare(b.id));
        // Build unique sorted category list for the filter HUD
        const cats = Array.from(
          new Set(nodes.map((n) => n.category).filter(Boolean))
        ).sort();
        setAllCategories(cats);
        setActiveCategories(new Set(cats));

        setGNodes(nodes);
        setGLinks(links);
        setLoadState('ready');
      })
      .catch(() => setLoadState('empty'));
  // externalRefreshKey is bumped by Dashboard whenever SYNTHESIS_COMPLETE fires,
  // even while this component is unmounted, so opening the Knowledge tab after
  // synthesis always fetches the latest graph state.
  // refreshKey is bumped by internal actions (handleSynthesize, RedTeamReview).
  }, [externalRefreshKey, refreshKey]);

  // ── D3 force tuning + semantic geography ─────────────────────────────────────
  // rAF defers one frame so the dynamic component is guaranteed to be mounted.
  useEffect(() => {
    if (loadState !== 'ready') return;
    const raf = requestAnimationFrame(() => {
      const fg = graphRef.current;
      if (!fg) return;

      // Strong repulsion: nodes need room for their label pills.
      fg.d3Force('charge')?.strength(-2800);
      fg.d3Force('link')?.distance(240);

      // Remove the default center force; semantic X/Y forces handle positioning
      fg.d3Force('center', null);

      // Semantic geography: gentle category nudge only with low strength so
      // dragging feels responsive and depth-2 siblings can spread freely.
      fg.d3Force(
        'x',
        forceX((n: any) => CAT_ZONE_X[normCat(n.category ?? '')] ?? 0).strength(0.02),
      );
      fg.d3Force('y', forceY(0).strength(0.008));

      // Label-aware collision: exclusion radius accounts for the full pill width.
      // Courier New at 11px is ~6.6px per character; we use 7 as a conservative
      // estimate so pills never clip.  Strength 1.0 makes it a hard constraint.
      fg.d3Force(
        'collide',
        forceCollide((n: any) => {
          const degree     = (n as GNode).degree ?? 0;
          const r          = Math.max(7, Math.min(16, 5 + degree * 1.2));
          const labelChars = Math.min(40, ((n as GNode).label?.length ?? 0));
          const pillWidth  = Math.max(60, labelChars * 7);
          return r + pillWidth;
        }).strength(1.0),
      );

      fg.d3ReheatSimulation();
    });
    return () => cancelAnimationFrame(raf);
  }, [loadState]);

  // ── Category toggle ───────────────────────────────────────────────────────────
  const toggleCategory = useCallback((cat: string) => {
    setActiveCategories((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) { next.delete(cat); } else { next.add(cat); }
      return next;
    });
  }, []);

  // ── Force synthesis ───────────────────────────────────────────────────────────
  const handleSynthesize = useCallback(async () => {
    if (synthState !== 'idle') return;
    setSynthState('running');
    setSynthSummary(null);
    try {
      const result = await postForceSynthesis();
      if (result.cooldown_remaining_seconds != null && !result.success) {
        setCooldown(result.cooldown_remaining_seconds);
        setSynthState('cooldown');
        return;
      }
      const total = (result.nodes_created ?? 0) + (result.nodes_updated ?? 0);
      const edges = result.edges_created ?? 0;
      setSynthSummary(`${total} nodes · ${edges} edges`);
      setLastSynthDate(result.synthesis_date ?? null);
      setCooldown(result.cooldown_seconds ?? 600);
      setSynthState('cooldown');
      setRefreshKey((k) => k + 1);
    } catch {
      setSynthState('idle');
    }
  }, [synthState]);

  // ── Link colour callback ──────────────────────────────────────────────────────
  // Combines filter ghosting (source OR target not active) with edge-type opacity.
  const linkColorCb = useCallback((l: LinkObject): string => {
    const link = l as GLink;
    const srcCat = typeof link.source === 'object'
      ? ((link.source as any)?.category as string) ?? '' : '';
    const tgtCat = typeof link.target === 'object'
      ? ((link.target as any)?.category as string) ?? '' : '';
    // Ghost if either endpoint's category is filtered out
    if ((srcCat && !activeCategories.has(srcCat)) || (tgtCat && !activeCategories.has(tgtCat))) {
      return 'rgba(63,63,70,0.06)';
    }
    // Causal links: full opacity; correlational: 53%
    return (link.linkColor ?? '#3f3f46') + relAlphaSuffix(link.relType);
  }, [activeCategories]);

  // ── Link width callback ────────────────────────────────────────────────────────
  const linkWidthCb = useCallback((l: LinkObject): number => {
    const link = l as GLink;
    const srcCat = typeof link.source === 'object'
      ? ((link.source as any)?.category as string) ?? '' : '';
    const tgtCat = typeof link.target === 'object'
      ? ((link.target as any)?.category as string) ?? '' : '';
    if ((srcCat && !activeCategories.has(srcCat)) || (tgtCat && !activeCategories.has(tgtCat))) {
      return 0.3;
    }
    return relWidth(link.relType);
  }, [activeCategories]);

  // ── Canvas node painter ───────────────────────────────────────────────────────
  //
  // Label placement: per-frame rightward displacement with leader lines.
  //
  // gNodes are sorted by degree descending at load time so react-force-graph
  // iterates them hub-first every frame.  When a label would overlap an
  // already-drawn rect in drawnRectsRef, it is shifted rightward past the
  // blocker.  If displaced, a thin leader line is drawn back to the node so
  // the connection is obvious.  Labels pushed beyond 3× their width are
  // suppressed (ultra-dense clusters only).
  // drawnRectsRef is reset once per animation frame via performance.now().
  //
  const paintNode = useCallback(
    (nodeObj: object, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const n = nodeObj as GNode;
      if (n.x == null || n.y == null) return;

      // ── Frame boundary: reset drawn-rect list once per animation frame ────────
      // All paintNode calls for one render frame are < 1 ms apart; consecutive
      // frames are ≥ 16 ms apart.  A gap > 1 ms means a new frame started.
      const now = performance.now();
      if (now - lastFrameTimeRef.current > 1) {
        drawnRectsRef.current    = [];
        lastFrameTimeRef.current = now;
      }

      const degree     = n.degree ?? 0;
      const isSelected = selectedNode?.id === n.id;
      const isFiltered = !activeCategories.has(n.category);
      const isIsolated = degree === 0 && !isFiltered;

      // Radius scales with connectivity: 7 px (isolated) → 16 px (hub)
      const r = Math.max(7, Math.min(16, 5 + degree * 1.2));

      const prevAlpha = ctx.globalAlpha;
      ctx.globalAlpha = isFiltered ? 0.05 : isIsolated ? 0.4 : 1.0;

      // ── Filtered ghost: just a faint dot, no label ────────────────────────────
      if (isFiltered) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, r * 0.6, 0, 2 * Math.PI);
        ctx.fillStyle = n.nodeColor;
        ctx.fill();
        ctx.globalAlpha = prevAlpha;
        return;
      }

      // ── Glow ring (selected) ──────────────────────────────────────────────────
      if (isSelected) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, r + 7, 0, 2 * Math.PI);
        ctx.fillStyle = n.nodeColor + '22';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(n.x, n.y, r + 7, 0, 2 * Math.PI);
        ctx.strokeStyle = n.nodeColor + '55';
        ctx.lineWidth   = 0.8 / globalScale;
        ctx.stroke();
      }

      // ── Node circle ───────────────────────────────────────────────────────────
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
      ctx.fillStyle = isSelected ? n.nodeColor : n.nodeColor + 'cc';
      ctx.fill();

      // ── Label pill: hidden at extreme overview ────────────────────────────────
      // Below 0.18× zoom the canvas-space font would be enormous; suppress all
      // labels (selected node is exempted from this early return).
      if (!isSelected && globalScale < 0.18) {
        ctx.globalAlpha = prevAlpha;
        return;
      }

      const fontSize = Math.max(9, 11 / globalScale);
      ctx.font       = `${fontSize}px "Courier New", Courier, monospace`;

      // Char budget scales with zoom: at low zoom font is already large, so
      // we truncate more aggressively to keep pills from eating the canvas.
      const maxChars =
        globalScale < 0.35 ? 12 :
        globalScale < 0.6  ? 16 :
        globalScale < 1.0  ? 22 :
        40;
      const raw   = n.label;
      const label = raw.length > maxChars ? raw.slice(0, maxChars - 1) + '…' : raw;

      const tw       = ctx.measureText(label).width;
      const padX     = 3.5 / globalScale;
      const padY     = 1.5 / globalScale;
      const pillCorr = 2   / globalScale;
      const gap      = 4   / globalScale;
      const pillW    = tw + padX * 2;
      const pillH    = fontSize + padY * 2;
      const pt       = n.y - pillH / 2;
      const pb       = n.y + pillH / 2;
      // Natural label start (just right of the node circle + gap).
      const oxNat    = n.x + r + gap;

      // ── Per-frame rightward displacement ──────────────────────────────────────
      // Find a clear horizontal slot: scan all existing rects for overlap,
      // jump ox to the right edge of the furthest blocker, repeat until clean.
      // ox only ever increases, so convergence is guaranteed in ≤ n passes.
      let ox = oxNat;
      let foundOverlap = true;
      while (foundOverlap) {
        foundOverlap = false;
        let maxX2 = ox;
        for (const rect of drawnRectsRef.current) {
          if (!(ox + pillW < rect.x1 || ox > rect.x2 || pb < rect.y1 || pt > rect.y2)) {
            if (rect.x2 > maxX2) maxX2 = rect.x2;
            foundOverlap = true;
          }
        }
        if (foundOverlap) ox = maxX2 + gap;
      }
      // Ultra-dense guard: suppress only if displaced beyond 3× the pill width.
      // Selected nodes are never suppressed regardless of displacement.
      if (!isSelected && ox - oxNat > pillW * 3) {
        ctx.globalAlpha = prevAlpha;
        return;
      }
      // Register the final rect so subsequent (lower-degree) nodes avoid it.
      drawnRectsRef.current.push({ x1: ox, x2: ox + pillW, y1: pt, y2: pb });

      // ── Leader line: drawn only when the label was displaced ──────────────────
      if (ox > oxNat) {
        ctx.beginPath();
        ctx.moveTo(oxNat, n.y);
        ctx.lineTo(ox, n.y);
        ctx.strokeStyle = n.nodeColor + '40';
        ctx.lineWidth   = 0.5 / globalScale;
        ctx.stroke();
      }

      // Rounded-rect pill (at the potentially-displaced ox)
      ctx.beginPath();
      ctx.moveTo(ox + pillCorr, pt);
      ctx.lineTo(ox + pillW - pillCorr, pt);
      ctx.quadraticCurveTo(ox + pillW, pt,  ox + pillW, pt + pillCorr);
      ctx.lineTo(ox + pillW, pb - pillCorr);
      ctx.quadraticCurveTo(ox + pillW, pb,  ox + pillW - pillCorr, pb);
      ctx.lineTo(ox + pillCorr, pb);
      ctx.quadraticCurveTo(ox, pb,  ox, pb - pillCorr);
      ctx.lineTo(ox, pt + pillCorr);
      ctx.quadraticCurveTo(ox, pt,  ox + pillCorr, pt);
      ctx.closePath();
      ctx.fillStyle   = 'rgba(9, 9, 11, 0.82)';
      ctx.fill();
      ctx.strokeStyle = n.nodeColor + '28';
      ctx.lineWidth   = 0.5 / globalScale;
      ctx.stroke();

      ctx.fillStyle    = isIsolated
        ? 'rgba(161, 161, 170, 0.65)'
        : 'rgba(228, 228, 231, 0.88)';
      ctx.textAlign    = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, ox + padX, n.y);

      ctx.globalAlpha = prevAlpha;
    },
    [selectedNode, activeCategories],
  );

  // ── Pointer hit-area: matches visual radius + click margin ───────────────────
  const paintPointerArea = useCallback(
    (nodeObj: object, color: string, ctx: CanvasRenderingContext2D) => {
      const n = nodeObj as GNode;
      if (n.x == null || n.y == null) return;
      const r = Math.max(7, Math.min(16, 5 + (n.degree ?? 0) * 1.2));
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r + 5, 0, 2 * Math.PI);
      ctx.fill();
    },
    [],
  );

  const isIdle = synthState === 'idle';
  // Memoised so react-force-graph-2d never sees a new graphData reference
  // unless the actual data changed; prevents spurious simulation restarts
  // (and the broken-drag symptom that comes with them).
  const graphData = useMemo(() => ({ nodes: gNodes, links: gLinks }), [gNodes, gLinks]);
  const allActive = activeCategories.size >= allCategories.length;

  return (
    <div className="flex h-full flex-col bg-zinc-950">

      {/* ── Control strip ───────────────────────────────────────────────────── */}
      <div className="flex shrink-0 items-center justify-between border-b border-zinc-800/60 bg-zinc-900/70 px-4 py-2.5">

        {/* Left: synthesis button + post-run summary */}
        <div className="flex items-center gap-4">
          <button
            onClick={handleSynthesize}
            disabled={!isIdle}
            className={`flex items-center gap-2 font-mono text-[0.6875rem] tracking-widest transition-colors disabled:cursor-not-allowed ${
              isIdle ? 'text-cyan-500 hover:text-cyan-300' : 'text-zinc-600'
            }`}
          >
            {synthState === 'running' && (
              <span className="h-2 w-2 shrink-0 animate-spin rounded-full border border-zinc-500 border-t-zinc-300" />
            )}
            {synthState === 'running'  && 'SYNTHESIZING...'}
            {synthState === 'cooldown' && `READY IN ${fmtCountdown(cooldown)}`}
            {synthState === 'idle'     && 'RUN SYNTHESIS'}
          </button>
          {synthSummary && synthState === 'cooldown' && (
            <span className="font-mono text-[0.625rem] tracking-widest text-emerald-600">
              {synthSummary} updated
            </span>
          )}
        </div>

        {/* Right: red-team toggle + legend + date stamp */}
        <div className="flex items-center gap-6">
          <button
            onClick={() => setShowRedTeam((v) => !v)}
            className={`flex items-center gap-1.5 font-mono text-[0.625rem] tracking-widest transition-colors ${
              showRedTeam
                ? 'text-rose-400 hover:text-rose-300'
                : 'text-zinc-600 hover:text-zinc-400'
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${showRedTeam ? 'bg-rose-500' : 'bg-zinc-700'}`} />
            RED TEAM
          </button>
          <Legend />
          {lastSynthDate && (
            <span className="font-mono text-[0.625rem] tracking-widest text-zinc-700">
              last run {lastSynthDate}
            </span>
          )}
        </div>
      </div>

      {/* ── Canvas area ──────────────────────────────────────────────────────── */}
      <div ref={containerRef} className="relative flex-1 overflow-hidden">

        {/* Loading */}
        {loadState === 'loading' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="animate-pulse font-mono text-[0.75rem] tracking-widest text-zinc-600">
              LOADING GRAPH…
            </span>
          </div>
        )}

        {/* Empty state */}
        {loadState === 'empty' && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4">
            <svg
              className="h-8 w-8 text-zinc-700"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.2}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z"
              />
            </svg>
            <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
              NO GRAPH DATA: RUN SYNTHESIS ABOVE
            </p>
          </div>
        )}

        {/* Force graph */}
        {loadState === 'ready' && (
          <FG2D
            ref={graphRef}
            graphData={graphData}
            width={size.w}
            height={size.h}
            backgroundColor={CANVAS_BG}
            // ── Node rendering ───────────────────────────────────────────────
            nodeId="id"
            nodeColor={(n) => (n as GNode).nodeColor}
            nodeLabel={() => ''}
            nodeCanvasObject={paintNode}
            nodeCanvasObjectMode={() => 'replace'}
            nodePointerAreaPaint={paintPointerArea}
            // ── Link rendering ───────────────────────────────────────────────
            linkSource="source"
            linkTarget="target"
            linkColor={linkColorCb}
            linkWidth={linkWidthCb}
            linkDirectionalArrowLength={4}
            linkDirectionalArrowRelPos={1}
            linkDirectionalArrowColor={linkColorCb}
            // Animated particles: match link colour (ghosted on filtered links)
            linkDirectionalParticles={2}
            linkDirectionalParticleWidth={1.5}
            linkDirectionalParticleColor={linkColorCb}
            // ── Physics ──────────────────────────────────────────────────────
            // Slower cooling (0.006) gives the stronger repulsion + collision
            // forces more time to fully resolve before the sim freezes.
            // Velocity decay 0.45 keeps drag fluid without oscillation.
            cooldownTicks={600}
            d3AlphaDecay={0.006}
            d3VelocityDecay={0.45}
            // ── Interaction ──────────────────────────────────────────────────
            enableNodeDrag
            onNodeClick={(nodeObj) => {
              const n = nodeObj as GNode;
              setSelectedNode((prev) => (prev?.id === n.id ? null : n));
            }}
            onBackgroundClick={() => setSelectedNode(null)}
          />
        )}

        {/* ── Category filter HUD: bottom centre ──────────────────────────── */}
        {loadState === 'ready' && allCategories.length > 0 && (
          <div className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 border border-zinc-800/60 bg-zinc-950/90 px-3 py-1.5 backdrop-blur-sm">

            {/* Label */}
            <span className="mr-0.5 font-mono text-[0.5625rem] tracking-[0.22em] text-zinc-700 select-none">
              FILTER
            </span>

            {/* Reset to all: only visible when something is filtered */}
            {!allActive && (
              <button
                onClick={() => setActiveCategories(new Set(allCategories))}
                className="border border-zinc-700/60 px-1.5 py-0.5 font-mono text-[0.5625rem] tracking-widest text-zinc-500 transition-colors hover:border-zinc-500 hover:text-zinc-300"
              >
                ALL
              </button>
            )}

            {/* One pill per category */}
            {allCategories.map((cat) => {
              const isActive = activeCategories.has(cat);
              const accent   = ZONE_ACCENT[catZone(cat)];
              const count    = catCounts.get(cat) ?? 0;
              return (
                <button
                  key={cat}
                  onClick={() => toggleCategory(cat)}
                  className={`flex items-center gap-1.5 border px-2 py-0.5 font-mono text-[0.5625rem] tracking-wider transition-all ${
                    isActive
                      ? 'border-zinc-600/80 text-zinc-200 hover:border-zinc-400 hover:text-white'
                      : 'border-zinc-800/60 text-zinc-700 hover:border-zinc-700 hover:text-zinc-500'
                  }`}
                >
                  {/* Zone colour dot */}
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full transition-opacity"
                    style={{
                      background: accent,
                      opacity: isActive ? 1 : 0.2,
                    }}
                  />
                  {/* Category name */}
                  {cat.replace(/_/g, ' ')}
                  {/* Node count badge */}
                  <span className={`font-mono transition-colors ${isActive ? 'text-zinc-600' : 'text-zinc-800'}`}>
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        )}

        {/* Node detail panel: right side */}
        {selectedNode && loadState === 'ready' && (
          <NodePanel node={selectedNode} onClose={() => setSelectedNode(null)} />
        )}

        {/* Red Team Audit panel: left side */}
        {showRedTeam && (
          <RedTeamReview
            onClose={() => setShowRedTeam(false)}
            onGraphUpdated={() => setRefreshKey((k) => k + 1)}
          />
        )}
      </div>
    </div>
  );
}
