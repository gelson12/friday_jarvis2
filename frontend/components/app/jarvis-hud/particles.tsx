'use client';

import { useMemo, useRef } from 'react';
import * as THREE from 'three';
import { useFrame } from '@react-three/fiber';
import type { SceneSignalsRefs } from './use-scene-signals';

const BASE = new THREE.Color('#3CDFFF');

interface ParticlesProps {
  signals: SceneSignalsRefs;
  count?: number;
}

// Custom point cloud orbiting the core. Each particle lives in a spherical
// shell with its own angular velocity; the shared scene state nudges the
// overall radial drift (inward when listening/thinking, outward bursts when
// speaking) and the size scales gently with mic volume.
export function Particles({ signals, count = 350 }: ParticlesProps) {
  const pointsRef = useRef<THREE.Points>(null);
  const matRef = useRef<THREE.PointsMaterial>(null);

  const { positions, baseRadii, axisX, axisY, axisZ } = useMemo(() => {
    const positions = new Float32Array(count * 3);
    const baseRadii = new Float32Array(count);
    const axisX = new Float32Array(count);
    const axisY = new Float32Array(count);
    const axisZ = new Float32Array(count);

    for (let i = 0; i < count; i++) {
      const r = 2.4 + Math.random() * 4.2;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3 + 0] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      positions[i * 3 + 2] = r * Math.cos(phi);
      baseRadii[i] = r;

      // unit rotation axis per particle (random, normalized)
      const ax = Math.random() - 0.5;
      const ay = Math.random() - 0.5;
      const az = Math.random() - 0.5;
      const len = Math.hypot(ax, ay, az) || 1;
      axisX[i] = ax / len;
      axisY[i] = ay / len;
      axisZ[i] = az / len;
    }
    return { positions, baseRadii, axisX, axisY, axisZ };
  }, [count]);

  const elapsed = useRef(0);
  const radialDrift = useRef(0); // 0 = stable shell, negative = inward, positive = outward
  const speedScale = useRef(0.4);
  const sizeScale = useRef(1.0);

  useFrame((_, dt) => {
    elapsed.current += dt;
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;

    let tDrift = 0;
    let tSpeed = 0.4;
    let tSize = 1.0;
    switch (state) {
      case 'listening':
      case 'pre-connect-buffering':
        tDrift = -0.35;
        tSpeed = 0.8 + volume * 0.6;
        tSize = 1.0 + volume * 0.5;
        break;
      case 'thinking':
        tDrift = -0.6;
        tSpeed = 1.4;
        tSize = 1.1;
        break;
      case 'speaking':
        tDrift = 0.5 + volume * 0.6;
        tSpeed = 1.0 + volume * 0.5;
        tSize = 1.0 + volume * 0.6;
        break;
      case 'connecting':
      case 'initializing':
        tSpeed = 0.2;
        break;
    }
    const k = Math.min(1, dt * 3);
    radialDrift.current += (tDrift - radialDrift.current) * k;
    speedScale.current += (tSpeed - speedScale.current) * k;
    sizeScale.current += (tSize - sizeScale.current) * k;

    const pts = pointsRef.current;
    if (!pts) return;
    const posAttr = pts.geometry.attributes.position as THREE.BufferAttribute;
    const arr = posAttr.array as Float32Array;

    // Cheap per-particle orbit via Rodrigues rotation around its own axis.
    const angle = dt * speedScale.current;
    const cosA = Math.cos(angle);
    const sinA = Math.sin(angle);

    for (let i = 0; i < count; i++) {
      const ix = i * 3;
      const px = arr[ix + 0];
      const py = arr[ix + 1];
      const pz = arr[ix + 2];
      const kx = axisX[i];
      const ky = axisY[i];
      const kz = axisZ[i];
      // Rodrigues: v' = v cos + (k×v) sin + k (k·v)(1 − cos)
      const dot = kx * px + ky * py + kz * pz;
      const crossX = ky * pz - kz * py;
      const crossY = kz * px - kx * pz;
      const crossZ = kx * py - ky * px;
      const omc = 1 - cosA;
      let nx = px * cosA + crossX * sinA + kx * dot * omc;
      let ny = py * cosA + crossY * sinA + ky * dot * omc;
      let nz = pz * cosA + crossZ * sinA + kz * dot * omc;

      // Radial drift toward / away from a target shell radius unique per particle.
      const cur = Math.hypot(nx, ny, nz) || 1;
      const target = baseRadii[i] + radialDrift.current * 1.5;
      const newR = cur + (target - cur) * Math.min(1, dt * 1.5);
      const s = newR / cur;
      nx *= s;
      ny *= s;
      nz *= s;

      arr[ix + 0] = nx;
      arr[ix + 1] = ny;
      arr[ix + 2] = nz;
    }
    posAttr.needsUpdate = true;

    if (matRef.current) {
      matRef.current.size = 0.035 * sizeScale.current;
      matRef.current.opacity = 0.55 + Math.min(0.35, volume * 0.4);
    }
  });

  return (
    <points ref={pointsRef}>
      <bufferGeometry>
        <bufferAttribute
          attach="attributes-position"
          count={count}
          array={positions}
          itemSize={3}
          args={[positions, 3]}
        />
      </bufferGeometry>
      <pointsMaterial
        ref={matRef}
        color={BASE}
        size={0.04}
        sizeAttenuation
        transparent
        opacity={0.6}
        blending={THREE.AdditiveBlending}
        depthWrite={false}
      />
    </points>
  );
}
