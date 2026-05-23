'use client';

import { useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import type { SceneSignalsRefs } from './use-scene-signals';

const BASE = new THREE.Color('#3CDFFF');
const THREAD_COUNT = 12;
// Shortened from 5.5: at the previous length, threads extended far beyond
// the visible viewport, looking like errant lasers cutting the screen.
const THREAD_LEN = 2.6;

interface ThreadsProps {
  signals: SceneSignalsRefs;
}

// Twelve thin energy beams radiating from the core. Each beam is a
// stretched cylinder with an emissive cyan material — far more robust
// in production than drei's <Line> (which uses Line2 / MeshLine and
// occasionally fails to mount under Next.js prod bundling).
//
// Reactivity: per-thread emissive intensity oscillates with the agent
// state (faster pulse when thinking, amplitude-driven on speaking).
export function Threads({ signals }: ThreadsProps) {
  // Pre-compute thread orientations once.
  const threads = useMemo(() => {
    const out: { rotation: [number, number, number]; phase: number }[] = [];
    for (let i = 0; i < THREAD_COUNT; i++) {
      const theta = (i / THREAD_COUNT) * Math.PI * 2;
      const phi = Math.PI / 2 + (Math.random() - 0.5) * 0.8;
      const direction = new THREE.Vector3(
        Math.sin(phi) * Math.cos(theta),
        Math.cos(phi),
        Math.sin(phi) * Math.sin(theta)
      );
      // A cylinder's default axis is Y. Rotate so its Y axis points along `direction`.
      const q = new THREE.Quaternion().setFromUnitVectors(
        new THREE.Vector3(0, 1, 0),
        direction.clone().normalize()
      );
      const e = new THREE.Euler().setFromQuaternion(q);
      out.push({ rotation: [e.x, e.y, e.z], phase: Math.random() * Math.PI * 2 });
    }
    return out;
  }, []);

  const groupRef = useRef<THREE.Group>(null);
  const matRefs = useRef<Array<THREE.MeshBasicMaterial | null>>([]);
  const elapsed = useRef(0);

  useFrame((_, dt) => {
    elapsed.current += dt;
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;

    let pulseRate = 0.6;
    let baseIntensity = 0.25;
    let volumeBoost = 0;
    switch (state) {
      case 'listening':
      case 'pre-connect-buffering':
        pulseRate = 1.4;
        baseIntensity = 0.4;
        volumeBoost = volume * 0.45;
        break;
      case 'thinking':
        pulseRate = 3.0;
        baseIntensity = 0.55;
        break;
      case 'speaking':
        pulseRate = 2.2;
        baseIntensity = 0.5;
        volumeBoost = volume * 0.6;
        break;
    }

    for (let i = 0; i < THREAD_COUNT; i++) {
      const m = matRefs.current[i];
      if (!m) continue;
      const phase = threads[i].phase;
      const wave = 0.5 + 0.5 * Math.sin(elapsed.current * pulseRate + phase);
      m.opacity = baseIntensity + 0.4 * wave + volumeBoost;
    }

    if (groupRef.current) {
      groupRef.current.rotation.y += dt * 0.08;
    }
  });

  return (
    <group ref={groupRef}>
      {threads.map((t, i) => (
        // Place the cylinder so its base sits at the origin: a cylinder
        // is centered on its midpoint by default, so we offset along its
        // local Y by half the length, then apply the orientation.
        <group key={i} rotation={t.rotation}>
          <mesh position={[0, THREAD_LEN / 2, 0]}>
            <cylinderGeometry args={[0.008, 0.008, THREAD_LEN, 8, 1, true]} />
            <meshBasicMaterial
              ref={(el) => {
                matRefs.current[i] = el;
              }}
              color={BASE}
              transparent
              opacity={0.4}
              blending={THREE.AdditiveBlending}
              depthWrite={false}
              side={THREE.DoubleSide}
            />
          </mesh>
        </group>
      ))}
    </group>
  );
}
