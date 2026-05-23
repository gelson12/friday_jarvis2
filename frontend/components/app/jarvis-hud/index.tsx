'use client';

import { Component, type ReactNode, useEffect, useState } from 'react';
import dynamic from 'next/dynamic';
import { FallbackSvg } from './fallback-svg';

// Lazy-load the heavy three.js/r3f/drei/postprocessing chunk via Next's
// dynamic so it's properly code-split and never SSRs.
const SceneCanvas = dynamic(() => import('./scene-canvas').then((m) => m.SceneCanvas), {
  ssr: false,
  loading: () => <FallbackSvg />,
});

interface Capabilities {
  webgl: boolean;
  reducedMotion: boolean;
  tier: number; // 1-3 from a rough gpu probe; 1 = mobile/integrated, 3 = discrete
}

function probeWebGL(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

// Cheap GPU heuristic: parse UNMASKED_RENDERER_WEBGL via the debug extension
// and bucket common substrings. We avoid drei's useDetectGPU because it
// requires the Canvas to mount first, which defeats the gating.
function probeGpuTier(): number {
  if (typeof window === 'undefined') return 1;
  try {
    const canvas = document.createElement('canvas');
    const gl = (canvas.getContext('webgl2') ||
      canvas.getContext('webgl')) as WebGLRenderingContext | null;
    if (!gl) return 1;
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    if (!ext) return 2;
    const renderer = String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '').toLowerCase();
    // Discrete / high-end markers
    if (/(rtx|gtx|radeon rx|m1|m2|m3|m4|apple gpu|arc a)/i.test(renderer)) return 3;
    // Integrated markers
    if (/(intel|iris|hd graphics|uhd graphics|vega|adreno|mali|powervr)/i.test(renderer)) return 2;
    return 2;
  } catch {
    return 2;
  }
}

function detectCapabilities(): Capabilities {
  if (typeof window === 'undefined') {
    return { webgl: false, reducedMotion: false, tier: 1 };
  }
  return {
    webgl: probeWebGL(),
    reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    tier: probeGpuTier(),
  };
}

// Any error inside the 3D scene falls back to the SVG instead of crashing
// the whole app. The Bridge / Canvas / shader code touches WebGL APIs and
// LiveKit hooks — a single bad assumption shouldn't take down the page.
class SceneErrorBoundary extends Component<
  { children: ReactNode; onError: () => void },
  { hasError: boolean }
> {
  constructor(props: { children: ReactNode; onError: () => void }) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  componentDidCatch(err: Error, info: { componentStack?: string }) {
    if (typeof console !== 'undefined') {
      // Loud, single-line message so it's easy to spot in Railway logs / DevTools.
      // Includes the component stack so we can pinpoint which child threw.
      console.error(
        '[jarvis-hud] 3D scene threw — falling back to SVG HUD.\n' +
          'Error: ' +
          (err && err.message ? err.message : String(err)) +
          (info && info.componentStack ? '\nStack:' + info.componentStack : '')
      );
      // Surface to a window flag so it's inspectable from the console:
      //   `window.__jarvisHudCrash` will be the most recent error.
      try {
        (window as unknown as { __jarvisHudCrash?: Error }).__jarvisHudCrash = err;
      } catch {
        /* sandbox */
      }
    }
    this.props.onError();
  }
  render() {
    if (this.state.hasError) return <FallbackSvg />;
    return this.props.children;
  }
}

export function JarvisHudBackground() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [mountScene, setMountScene] = useState(false);
  const [contextLost, setContextLost] = useState(false);
  const [sceneCrashed, setSceneCrashed] = useState(false);

  // Probe once on mount.
  useEffect(() => {
    setCaps(detectCapabilities());
  }, []);

  // Defer the 3D scene mount until the browser is idle so we never block
  // the initial paint of the rest of the page.
  useEffect(() => {
    if (!caps || !caps.webgl || caps.reducedMotion || caps.tier < 2) return;
    const ric =
      (window as unknown as { requestIdleCallback?: (cb: () => void) => number })
        .requestIdleCallback || ((cb: () => void) => window.setTimeout(cb, 0));
    const id = ric(() => setMountScene(true));
    return () => {
      const cic = (window as unknown as { cancelIdleCallback?: (id: number) => void })
        .cancelIdleCallback;
      if (cic) cic(id as number);
    };
  }, [caps]);

  // Wrapper stays in the same z-index slot as the original background.
  const wrapperStyle: React.CSSProperties = {
    position: 'fixed',
    inset: 0,
    zIndex: -10,
    overflow: 'hidden',
    pointerEvents: 'none',
    background: '#02060b',
  };

  // Until caps probe finishes, render the SVG so there's never a black flash.
  if (!caps) {
    return (
      <div aria-hidden style={wrapperStyle}>
        <FallbackSvg />
      </div>
    );
  }

  const use3d =
    caps.webgl &&
    !caps.reducedMotion &&
    caps.tier >= 2 &&
    !contextLost &&
    !sceneCrashed &&
    mountScene;

  return (
    <div aria-hidden style={wrapperStyle}>
      {use3d ? (
        <SceneErrorBoundary onError={() => setSceneCrashed(true)}>
          <SceneCanvas tier={caps.tier} onContextLost={() => setContextLost(true)} />
        </SceneErrorBoundary>
      ) : (
        <FallbackSvg />
      )}
    </div>
  );
}
