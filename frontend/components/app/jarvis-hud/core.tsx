'use client';

import { useRef } from 'react';
import * as THREE from 'three';
import { MeshDistortMaterial } from '@react-three/drei';
import { useFrame } from '@react-three/fiber';
import type { SceneSignalsRefs } from './use-scene-signals';

const BASE = new THREE.Color('#3CDFFF');
const DESATURATED = new THREE.Color('#5BB8C8');

interface CoreProps {
  signals: SceneSignalsRefs;
}

// The "conscious orb" — a distorting icosahedron with additive inner glow.
// Material parameters (distort, speed, emissive intensity, color saturation,
// uniform scale) are smoothed each frame toward a target derived from the
// LiveKit agent state and mic volume. Brand cyan locked; only V/S vary.
export function Core({ signals }: CoreProps) {
  const groupRef = useRef<THREE.Group>(null);
  // MeshDistortMaterial's ref shape varies across drei versions — use a
  // permissive any-typed ref and feature-detect each property at write
  // time so a property rename in a future drei version can't crash us.
  const matRef = useRef<unknown>(null);
  const innerMatRef = useRef<THREE.MeshBasicMaterial>(null);

  const p = useRef({
    distort: 0.15,
    speed: 1.0,
    brightness: 0.35,
    sat: 1.0,
    scale: 1.0,
  });

  useFrame((_, dt) => {
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;

    let tDistort = 0.15;
    let tSpeed = 1.0;
    let tBrightness = 0.35;
    let tSat = 1.0;
    let tScale = 1.0;

    switch (state) {
      case 'connecting':
      case 'initializing':
        tBrightness = 0.4;
        tDistort = 0.1;
        tSpeed = 0.6;
        tSat = 0.4;
        break;
      case 'idle':
        tBrightness = 0.45;
        tDistort = 0.18;
        tSpeed = 0.8;
        break;
      case 'listening':
      case 'pre-connect-buffering':
        tBrightness = 0.6 + volume * 0.4;
        tDistort = 0.3 + volume * 0.2;
        tSpeed = 1.3 + volume * 0.5;
        tScale = 1.0 + volume * 0.06;
        break;
      case 'thinking':
        tBrightness = 0.7;
        tDistort = 0.5;
        tSpeed = 2.5;
        tScale = 1.04;
        break;
      case 'speaking':
        tBrightness = 0.75 + volume * 0.5;
        tDistort = 0.35 + volume * 0.3;
        tSpeed = 2.0;
        tScale = 1.02 + volume * 0.1;
        break;
      // 'disconnected' / 'failed' / unknown → defaults
    }

    const k = (rate: number) => Math.min(1, dt * rate);
    p.current.distort += (tDistort - p.current.distort) * k(6);
    p.current.speed += (tSpeed - p.current.speed) * k(4);
    p.current.brightness += (tBrightness - p.current.brightness) * k(5);
    p.current.sat += (tSat - p.current.sat) * k(4);
    p.current.scale += (tScale - p.current.scale) * k(8);

    const m = matRef.current as
      | (THREE.MeshStandardMaterial & { distort?: number; speed?: number })
      | null;
    if (m) {
      if ('distort' in m) m.distort = p.current.distort;
      if ('speed' in m) m.speed = p.current.speed;
      m.emissiveIntensity = p.current.brightness * 1.8;
      const c = BASE.clone()
        .lerp(DESATURATED, 1 - p.current.sat)
        .multiplyScalar(p.current.brightness);
      if (m.color) m.color.copy(c);
      if (m.emissive) m.emissive.copy(BASE).multiplyScalar(p.current.brightness * 0.6);
    }
    if (innerMatRef.current) {
      innerMatRef.current.opacity = 0.35 + p.current.brightness * 0.4;
    }
    if (groupRef.current) {
      groupRef.current.scale.setScalar(p.current.scale);
      groupRef.current.rotation.y += dt * 0.05;
      groupRef.current.rotation.x += dt * 0.02;
    }
  });

  return (
    <group ref={groupRef}>
      <mesh>
        <icosahedronGeometry args={[1.2, 4]} />
        <MeshDistortMaterial
          // drei v10's DistortMaterialImpl is not exported; the runtime
          // instance has .distort / .speed / .color / .emissive which we
          // touch via a feature-detected unknown ref in useFrame above.
          ref={matRef as unknown as React.Ref<never>}
          color={BASE}
          emissive={BASE}
          emissiveIntensity={1.2}
          metalness={0.2}
          roughness={0.15}
          distort={0.15}
          speed={1}
        />
      </mesh>
      <mesh scale={0.92}>
        <sphereGeometry args={[1, 32, 32]} />
        <meshBasicMaterial
          ref={innerMatRef}
          color={BASE}
          transparent
          opacity={0.5}
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </mesh>
    </group>
  );
}
