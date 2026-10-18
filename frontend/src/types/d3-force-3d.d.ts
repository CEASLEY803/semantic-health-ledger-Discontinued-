/**
 * Minimal ambient type declarations for d3-force-3d.
 * react-force-graph-2d ships this as its physics engine; no @types package exists.
 * Only the two functions we actually call are declared here.
 */
declare module 'd3-force-3d' {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  export function forceX(x?: number | ((d: any) => number)): any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  export function forceY(y?: number | ((d: any) => number)): any;
}
