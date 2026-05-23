'use client';

import { useRef } from 'react';
import * as THREE from 'three';
import { useFrame } from '@react-three/fiber';

const BASE = new THREE.Color('#3CDFFF');

// Three thin tilted rings — a 3D homage to the original SVG HUD scaffolding.
// Cheap (TorusGeometry, basic material), rotate slowly on three different
// axes for a parallax effect. Not state-reactive — they're the steady visual
// anchor that keeps the Jarvis identity even when the core goes quiet.
export function Rings() {
  const a = useRef<THREE.Mesh>(null);
  const b = useRef<THREE.Mesh>(null);
  const c = useRef<THREE.Mesh>(null);

  useFrame((_, dt) => {
    if (a.current) a.current.rotation.z += dt * 0.08;
    if (b.current) b.current.rotation.y += dt * 0.06;
    if (c.current) c.current.rotation.x += dt * 0.04;
  });

  return (
    <group>
      <mesh ref={a} rotation={[Math.PI / 2.3, 0, 0]}>
        <torusGeometry args={[2.6, 0.008, 8, 128]} />
        <meshBasicMaterial color={BASE} transparent opacity={0.55} />
      </mesh>
      <mesh ref={b} rotation={[0, Math.PI / 2.2, Math.PI / 3]}>
        <torusGeometry args={[3.4, 0.006, 8, 128]} />
        <meshBasicMaterial color={BASE} transparent opacity={0.35} />
      </mesh>
      <mesh ref={c} rotation={[Math.PI / 3.5, Math.PI / 5, 0]}>
        <torusGeometry args={[4.2, 0.005, 8, 128]} />
        <meshBasicMaterial color={BASE} transparent opacity={0.22} />
      </mesh>
    </group>
  );
}
