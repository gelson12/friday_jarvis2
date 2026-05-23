'use client';

import { useRef } from 'react';
import { Sparkles } from '@react-three/drei';
import { useFrame } from '@react-three/fiber';
import type * as THREE from 'three';
import type { SceneSignalsRefs } from './use-scene-signals';

interface ParticlesProps {
  signals: SceneSignalsRefs;
  count?: number;
}

// Two concentric shells of glowing particles orbiting the core. Uses
// drei's <Sparkles> instead of a hand-rolled buffer-attribute point
// cloud — Sparkles ships its own shader, is well-tested in production
// builds, and avoids r3f attribute-update quirks.
//
// Reactivity: the parent group rotation speed responds to the LiveKit
// agent state and mic volume, so the swarm tightens and accelerates
// when the agent is listening / thinking / speaking.
export function Particles({ signals, count = 280 }: ParticlesProps) {
  const innerRef = useRef<THREE.Group>(null);
  const outerRef = useRef<THREE.Group>(null);

  useFrame((_, dt) => {
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;

    let innerSpeed = 0.4;
    let outerSpeed = 0.2;
    switch (state) {
      case 'listening':
      case 'pre-connect-buffering':
        innerSpeed = 0.9 + volume * 0.8;
        outerSpeed = 0.4 + volume * 0.4;
        break;
      case 'thinking':
        innerSpeed = 1.5;
        outerSpeed = 0.7;
        break;
      case 'speaking':
        innerSpeed = 1.1 + volume * 1.0;
        outerSpeed = 0.5 + volume * 0.6;
        break;
    }

    if (innerRef.current) {
      innerRef.current.rotation.y += dt * innerSpeed;
      innerRef.current.rotation.x += dt * innerSpeed * 0.3;
    }
    if (outerRef.current) {
      outerRef.current.rotation.y -= dt * outerSpeed;
      outerRef.current.rotation.z += dt * outerSpeed * 0.2;
    }
  });

  return (
    <>
      <group ref={innerRef}>
        <Sparkles
          count={Math.floor(count * 0.6)}
          scale={[4, 4, 4]}
          size={3}
          speed={0.4}
          opacity={0.9}
          color="#9CF3FF"
          noise={1}
        />
      </group>
      <group ref={outerRef}>
        <Sparkles
          count={Math.floor(count * 0.4)}
          scale={[8, 6, 8]}
          size={2}
          speed={0.2}
          opacity={0.6}
          color="#3CDFFF"
          noise={1.5}
        />
      </group>
    </>
  );
}
