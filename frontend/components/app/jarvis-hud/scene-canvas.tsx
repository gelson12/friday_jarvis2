'use client';

import { useEffect, useRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';
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

// Sets the renderer clear color and the scene fog imperatively via
// useThree. Doing this in JSX with <color attach="background"> /
// <fog attach="fog"> works in dev but has been observed to silently
// fail under some prod bundles — falling back to direct r3f access
// is more robust.
function SceneSetup() {
  const { gl, scene } = useThree();
  useEffect(() => {
    gl.setClearColor(new THREE.Color('#02060b'), 1);
    scene.fog = new THREE.Fog('#02060b', 7, 16);
    return () => {
      scene.fog = null;
    };
  }, [gl, scene]);
  return null;
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

// Wireframe outer shell — gives the orb a "containment field" feel. Cheap:
// low-poly icosahedron, wireframe material, slow rotation.
function OuterShell() {
  const ref = useRef<THREE.Mesh>(null);
  useFrame((_, dt) => {
    if (!ref.current) return;
    ref.current.rotation.y += dt * 0.12;
    ref.current.rotation.x += dt * 0.05;
  });
  return (
    <mesh ref={ref}>
      <icosahedronGeometry args={[2.2, 1]} />
      <meshBasicMaterial
        color="#3CDFFF"
        wireframe
        transparent
        opacity={0.15}
        depthWrite={false}
      />
    </mesh>
  );
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
      <SceneSetup />
      <CameraParallax />
      <VisibilityPause />
      <ContextLossDetector onContextLost={onContextLost} />

      {/* Lighting — soft cyan rim so the orb's metalness reads */}
      <ambientLight intensity={0.25} />
      <pointLight position={[3, 2, 3]} intensity={0.8} color="#3CDFFF" />
      <pointLight position={[-3, -2, -3]} intensity={0.5} color="#9CF3FF" />

      <OuterShell />
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
  const particleCount = highTier ? 400 : 220;
  const dprCap = highTier ? 1.5 : 1.25;

  return (
    <>
      {Bridge}
      <Canvas
        // Pulled back from z=8 to z=14 so the orb + outer shell fit
        // comfortably with margin around the audio visualizer tile.
        camera={{ position: [0, 0, 14], fov: 45 }}
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
