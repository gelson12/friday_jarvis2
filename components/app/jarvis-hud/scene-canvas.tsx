'use client';

import { useEffect, useRef } from 'react';
import { Canvas, useThree } from '@react-three/fiber';
import { Core } from './core';
import { Particles } from './particles';
import { PostFx } from './post-fx';
import { Rings } from './rings';
import { Threads } from './threads';
import { type SceneSignalsRefs, useSceneSignals } from './use-scene-signals';

interface SceneCanvasProps {
  /** GPU tier (drei useDetectGPU.tier). 2 = downgrade, 3+ = full effects */
  tier: number;
  onContextLost: () => void;
}

// Smooth mouse-based camera parallax. Runs inside the Canvas tree so it can
// touch the live camera instance each frame.
function CameraParallax() {
  const { camera, size } = useThree();
  const targetX = useRef(0);
  const targetY = useRef(0);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const nx = (e.clientX / size.width) * 2 - 1;
      const ny = (e.clientY / size.height) * 2 - 1;
      targetX.current = nx * 0.25;
      targetY.current = -ny * 0.18;
    };
    window.addEventListener('mousemove', onMove);
    return () => window.removeEventListener('mousemove', onMove);
  }, [size.width, size.height]);

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      camera.position.x += (targetX.current - camera.position.x) * 0.04;
      camera.position.y += (targetY.current - camera.position.y) * 0.04;
      camera.lookAt(0, 0, 0);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [camera]);

  return null;
}

// Pauses the render loop when the tab is hidden — saves significant CPU/GPU
// on long-running sessions where Jarvis is left open in a background tab.
function VisibilityPause() {
  const { gl, invalidate } = useThree();
  useEffect(() => {
    const onVis = () => {
      if (document.hidden) {
        gl.setAnimationLoop(null);
      } else {
        invalidate();
      }
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [gl, invalidate]);
  return null;
}

// Attaches a webglcontextlost listener; on fire, calls onContextLost so the
// parent can swap to the SVG fallback for the rest of the session.
function ContextLossDetector({ onContextLost }: { onContextLost: () => void }) {
  const { gl } = useThree();
  useEffect(() => {
    const canvas = gl.domElement;
    const handler = (e: Event) => {
      e.preventDefault();
      onContextLost();
    };
    canvas.addEventListener('webglcontextlost', handler);
    return () => canvas.removeEventListener('webglcontextlost', handler);
  }, [gl, onContextLost]);
  return null;
}

interface SceneContentsProps {
  signals: SceneSignalsRefs;
  highTier: boolean;
  particleCount: number;
  onContextLost: () => void;
}

function SceneContents({ signals, highTier, particleCount, onContextLost }: SceneContentsProps) {
  return (
    <>
      <CameraParallax />
      <VisibilityPause />
      <ContextLossDetector onContextLost={onContextLost} />

      {/* Deep-space ambience */}
      <color attach="background" args={['#02060b']} />
      <fog attach="fog" args={['#02060b', 6, 14]} />

      {/* Lighting just enough to register the orb's metalness */}
      <ambientLight intensity={0.2} />
      <pointLight position={[3, 2, 3]} intensity={0.6} color="#3CDFFF" />
      <pointLight position={[-3, -2, -3]} intensity={0.4} color="#3CDFFF" />

      <Core signals={signals} />
      <Rings />
      <Threads signals={signals} />
      <Particles signals={signals} count={particleCount} />

      <PostFx signals={signals} highTier={highTier} />
    </>
  );
}

export function SceneCanvas({ tier, onContextLost }: SceneCanvasProps) {
  const { stateRef, volumeRef, Bridge } = useSceneSignals();

  const highTier = tier >= 3;
  const particleCount = highTier ? 500 : 250;
  const dprCap = highTier ? 1.5 : 1.25;

  return (
    <>
      {/* Bridge mounts inside any LiveKitRoom ancestor and writes refs.
          It renders nothing visually. */}
      {Bridge}
      <Canvas
        camera={{ position: [0, 0, 8], fov: 50 }}
        dpr={[1, dprCap]}
        gl={{
          powerPreference: 'high-performance',
          antialias: false,
          alpha: true,
          stencil: false,
          depth: true,
        }}
        style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
        frameloop="always"
      >
        <SceneContents
          signals={{ stateRef, volumeRef }}
          highTier={highTier}
          particleCount={particleCount}
          onContextLost={onContextLost}
        />
      </Canvas>
    </>
  );
}

export default SceneCanvas;
