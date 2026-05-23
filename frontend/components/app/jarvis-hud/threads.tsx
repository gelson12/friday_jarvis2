'use client';

import { useMemo, useRef } from 'react';
import * as THREE from 'three';
import { Line } from '@react-three/drei';
import { useFrame } from '@react-three/fiber';
import type { SceneSignalsRefs } from './use-scene-signals';

const BASE = '#3CDFFF';
const THREAD_COUNT = 12;
const THREAD_LEN = 5.5;
const SEGMENTS = 24;

interface ThreadsProps {
  signals: SceneSignalsRefs;
}

// Twelve curved "neural threads" radiating from the core. Each thread is a
// quadratic bezier from origin to a fixed anchor, sampled into SEGMENTS
// points. Dashed line material animates dashOffset → signal traveling along
// the thread. Pulse direction inverts between inward (listening, thinking)
// and outward (speaking).
export function Threads({ signals }: ThreadsProps) {
  // Generate THREAD_COUNT pre-sampled bezier point arrays — fixed geometry,
  // useMemo to build once.
  const threads = useMemo(() => {
    const out: { points: THREE.Vector3[] }[] = [];
    for (let i = 0; i < THREAD_COUNT; i++) {
      const theta = (i / THREAD_COUNT) * Math.PI * 2;
      // Distribute slightly out of equatorial plane.
      const phi = Math.PI / 2 + (Math.random() - 0.5) * 0.8;
      const anchor = new THREE.Vector3(
        THREAD_LEN * Math.sin(phi) * Math.cos(theta),
        THREAD_LEN * Math.cos(phi),
        THREAD_LEN * Math.sin(phi) * Math.sin(theta)
      );
      const mid = anchor.clone().multiplyScalar(0.5);
      // Perp offset for curvature
      const perp = new THREE.Vector3(-Math.sin(theta), 0, Math.cos(theta)).multiplyScalar(
        (Math.random() - 0.5) * 1.8
      );
      mid.add(perp);

      const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(0, 0, 0), mid, anchor);
      const points = curve.getPoints(SEGMENTS);
      out.push({ points });
    }
    return out;
  }, []);

  // One ref per thread to animate dashOffset (and color/opacity).
  const lineRefs = useRef<Array<THREE.Object3D | null>>([]);
  const dashOffsets = useRef<number[]>(new Array(THREAD_COUNT).fill(0));
  const params = useRef({ pulseRate: 0.0, direction: 1, opacity: 0.25 });

  useFrame((_, dt) => {
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;

    let tRate = 0.0;
    let tDir = 1;
    let tOp = 0.2;
    switch (state) {
      case 'listening':
      case 'pre-connect-buffering':
        tRate = 1.0 + volume * 0.8;
        tDir = -1;
        tOp = 0.35 + volume * 0.3;
        break;
      case 'thinking':
        tRate = 2.4;
        tDir = -1;
        tOp = 0.5;
        break;
      case 'speaking':
        tRate = 1.6 + volume * 1.2;
        tDir = 1;
        tOp = 0.45 + volume * 0.4;
        break;
      case 'idle':
        tRate = 0.2;
        tOp = 0.22;
        break;
      case 'connecting':
      case 'initializing':
        tRate = 0.0;
        tOp = 0.15;
        break;
      default:
        tRate = 0.0;
        tOp = 0.18;
    }
    const k = Math.min(1, dt * 4);
    params.current.pulseRate += (tRate - params.current.pulseRate) * k;
    params.current.direction = tDir;
    params.current.opacity += (tOp - params.current.opacity) * k;

    for (let i = 0; i < THREAD_COUNT; i++) {
      // Stagger thread phases so pulses don't all fire on the same tick.
      const stagger = (i / THREAD_COUNT) * 0.8;
      dashOffsets.current[i] += dt * params.current.pulseRate * params.current.direction;
      const obj = lineRefs.current[i] as unknown as {
        material?: {
          dashOffset?: number;
          opacity?: number;
          uniforms?: Record<string, { value: number }>;
        };
      } | null;
      if (obj?.material) {
        // drei <Line> uses a MeshLineMaterial with uniforms.dashOffset
        const mat = obj.material as {
          dashOffset?: number;
          opacity?: number;
          uniforms?: { dashOffset?: { value: number }; opacity?: { value: number } };
        };
        if (mat.uniforms?.dashOffset) {
          mat.uniforms.dashOffset.value = dashOffsets.current[i] + stagger;
        } else if (mat.dashOffset !== undefined) {
          mat.dashOffset = dashOffsets.current[i] + stagger;
        }
        if (mat.uniforms?.opacity) {
          mat.uniforms.opacity.value = params.current.opacity;
        } else if (mat.opacity !== undefined) {
          mat.opacity = params.current.opacity;
        }
      }
    }
  });

  return (
    <group>
      {threads.map((t, i) => (
        <Line
          key={i}
          ref={(el) => {
            lineRefs.current[i] = el as unknown as THREE.Object3D | null;
          }}
          points={t.points}
          color={BASE}
          lineWidth={1.2}
          dashed
          dashScale={4}
          dashSize={0.06}
          gapSize={0.08}
          transparent
          opacity={0.25}
        />
      ))}
    </group>
  );
}
