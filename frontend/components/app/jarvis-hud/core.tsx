'use client';

import { useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import type { SceneSignalsRefs } from './use-scene-signals';

const BASE = new THREE.Color('#3CDFFF');
const HOT = new THREE.Color('#9CF3FF');

interface CoreProps {
  signals: SceneSignalsRefs;
}

// The "conscious orb" — a glowing icosahedron at the scene center.
//
// We deliberately do NOT use drei's MeshDistortMaterial here. It works
// in dev but has been the most-frequent prod-build failure point in
// drei v10 (it relies on shader injection via patchShaders that Next
// 15 / Turbopack sometimes fail to bundle). A plain
// meshStandardMaterial with strong emissive + scale/displacement
// breathing on each frame gives a similar "alive" feel, looks gorgeous
// with bloom, and is bombproof in production.
//
// Reactivity: brightness, scale, and a vertex-displacement "breathing"
// factor are smoothed each frame toward a target derived from the
// LiveKit agent state + mic volume.
export function Core({ signals }: CoreProps) {
  const groupRef = useRef<THREE.Group>(null);
  const meshRef = useRef<THREE.Mesh>(null);
  const matRef = useRef<THREE.MeshStandardMaterial>(null);
  const innerMatRef = useRef<THREE.MeshBasicMaterial>(null);

  // Cache the rest-position vertex array; we modulate displacements from
  // this baseline so distortions don't accumulate.
  const restPositions = useRef<Float32Array | null>(null);
  const normals = useRef<Float32Array | null>(null);

  const p = useRef({
    breathe: 0.0,    // 0..0.3 — vertex displacement amplitude along normals
    breatheRate: 1.2,
    brightness: 0.5,
    scale: 1.0,
    spin: 0.05,
  });

  const elapsed = useRef(0);

  useFrame((_, dt) => {
    elapsed.current += dt;
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;

    let tBreathe = 0.06;
    let tRate = 1.0;
    let tBrightness = 0.5;
    let tScale = 1.0;
    let tSpin = 0.05;

    switch (state) {
      case 'connecting':
      case 'initializing':
        tBreathe = 0.05;
        tRate = 0.6;
        tBrightness = 0.45;
        tSpin = 0.04;
        break;
      case 'idle':
        tBreathe = 0.08;
        tRate = 0.9;
        tBrightness = 0.55;
        break;
      case 'listening':
      case 'pre-connect-buffering':
        tBreathe = 0.12 + volume * 0.15;
        tRate = 1.4 + volume * 0.6;
        tBrightness = 0.75 + volume * 0.4;
        tScale = 1.0 + volume * 0.08;
        tSpin = 0.1;
        break;
      case 'thinking':
        tBreathe = 0.22;
        tRate = 2.4;
        tBrightness = 0.85;
        tScale = 1.04;
        tSpin = 0.18;
        break;
      case 'speaking':
        tBreathe = 0.16 + volume * 0.2;
        tRate = 2.0;
        tBrightness = 0.85 + volume * 0.5;
        tScale = 1.02 + volume * 0.12;
        tSpin = 0.14;
        break;
    }

    const k = (rate: number) => Math.min(1, dt * rate);
    p.current.breathe += (tBreathe - p.current.breathe) * k(5);
    p.current.breatheRate += (tRate - p.current.breatheRate) * k(4);
    p.current.brightness += (tBrightness - p.current.brightness) * k(5);
    p.current.scale += (tScale - p.current.scale) * k(8);
    p.current.spin += (tSpin - p.current.spin) * k(3);

    // Vertex displacement breathing: push each vertex along its normal
    // by a slowly-varying offset. Cheap (a single loop) and looks like
    // the orb is breathing energy in and out.
    const mesh = meshRef.current;
    if (mesh) {
      const geom = mesh.geometry as THREE.BufferGeometry;
      const posAttr = geom.attributes.position as THREE.BufferAttribute;
      const normAttr = geom.attributes.normal as THREE.BufferAttribute;
      if (!restPositions.current || restPositions.current.length !== posAttr.array.length) {
        restPositions.current = new Float32Array(posAttr.array as Float32Array);
        normals.current = new Float32Array(normAttr.array as Float32Array);
      }
      const arr = posAttr.array as Float32Array;
      const rest = restPositions.current;
      const norm = normals.current!;
      const t = elapsed.current * p.current.breatheRate;
      const amp = p.current.breathe;
      const vertexCount = arr.length / 3;
      for (let i = 0; i < vertexCount; i++) {
        const ix = i * 3;
        // Phase per vertex from its rest position — gives uneven, organic motion.
        const phase = rest[ix] * 1.7 + rest[ix + 1] * 1.3 + rest[ix + 2] * 1.1;
        const d = Math.sin(t + phase) * amp;
        arr[ix + 0] = rest[ix + 0] + norm[ix + 0] * d;
        arr[ix + 1] = rest[ix + 1] + norm[ix + 1] * d;
        arr[ix + 2] = rest[ix + 2] + norm[ix + 2] * d;
      }
      posAttr.needsUpdate = true;
    }

    if (matRef.current) {
      matRef.current.emissiveIntensity = p.current.brightness * 2.4;
      // Color: blend between deep cyan and hot cyan as brightness rises.
      const hotMix = Math.min(1, Math.max(0, (p.current.brightness - 0.5) * 1.4));
      const c = BASE.clone().lerp(HOT, hotMix);
      matRef.current.color.copy(c);
      matRef.current.emissive.copy(c);
    }
    if (innerMatRef.current) {
      innerMatRef.current.opacity = 0.4 + p.current.brightness * 0.5;
    }
    if (groupRef.current) {
      groupRef.current.scale.setScalar(p.current.scale);
      groupRef.current.rotation.y += dt * p.current.spin;
      groupRef.current.rotation.x += dt * p.current.spin * 0.35;
    }
  });

  return (
    <group ref={groupRef}>
      {/* Main orb */}
      <mesh ref={meshRef}>
        <icosahedronGeometry args={[0.85, 4]} />
        <meshStandardMaterial
          ref={matRef}
          color={BASE}
          emissive={BASE}
          emissiveIntensity={1.4}
          metalness={0.3}
          roughness={0.15}
          flatShading={false}
        />
      </mesh>
      {/* Inner additive halo */}
      <mesh scale={1.05}>
        <sphereGeometry args={[1, 32, 32]} />
        <meshBasicMaterial
          ref={innerMatRef}
          color={HOT}
          transparent
          opacity={0.55}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </mesh>
      {/* Outer soft halo — very wide, low opacity, gives the bloom-friendly glow */}
      <mesh scale={1.45}>
        <sphereGeometry args={[1, 24, 24]} />
        <meshBasicMaterial
          color={BASE}
          transparent
          opacity={0.18}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </mesh>
    </group>
  );
}
