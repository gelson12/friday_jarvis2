'use client';

import { useRef } from 'react';
import { BlendFunction, KernelSize } from 'postprocessing';
import * as THREE from 'three';
import { useFrame } from '@react-three/fiber';
import { Bloom, ChromaticAberration, EffectComposer, Vignette } from '@react-three/postprocessing';
import type { SceneSignalsRefs } from './use-scene-signals';

interface PostFxProps {
  signals: SceneSignalsRefs;
  /** Drop expensive effects (ChromaticAberration, large bloom kernel) on weaker GPUs */
  highTier: boolean;
}

// Post-processing stack:
//   Bloom: hot pass that makes the emissive core actually *glow*. Intensity
//     animates with agent state + mic volume.
//   Vignette: dark falloff at edges — focuses the eye on the core.
//   ChromaticAberration (high-tier only): subtle RGB split at corners,
//     "broadcast HUD" feel.
//
// Why no Noise effect: postprocessing's Noise is a full-screen pass and on
// integrated GPUs the cost wasn't worth the look; bloom alone already gives
// the holographic vibe.
export function PostFx({ signals, highTier }: PostFxProps) {
  const bloomRef = useRef<{ intensity: number } | null>(null);
  const intensityRef = useRef(0.9);

  useFrame((_, dt) => {
    const state = signals.stateRef.current;
    const volume = signals.volumeRef.current;
    let target = 0.8;
    switch (state) {
      case 'listening':
      case 'pre-connect-buffering':
        target = 1.0 + volume * 0.5;
        break;
      case 'thinking':
        target = 1.4;
        break;
      case 'speaking':
        target = 1.6 + volume * 0.7;
        break;
      case 'idle':
        target = 0.9;
        break;
      case 'connecting':
      case 'initializing':
        target = 0.5;
        break;
    }
    const k = Math.min(1, dt * 4);
    intensityRef.current += (target - intensityRef.current) * k;
    if (bloomRef.current) bloomRef.current.intensity = intensityRef.current;
  });

  if (highTier) {
    return (
      <EffectComposer multisampling={0}>
        <Bloom
          ref={bloomRef as unknown as React.Ref<unknown>}
          intensity={0.9}
          luminanceThreshold={0.35}
          luminanceSmoothing={0.4}
          mipmapBlur
          kernelSize={KernelSize.LARGE}
        />
        <ChromaticAberration
          blendFunction={BlendFunction.NORMAL}
          offset={new THREE.Vector2(0.0008, 0.0008)}
          radialModulation={false}
          modulationOffset={0}
        />
        <Vignette eskil={false} offset={0.15} darkness={0.85} />
      </EffectComposer>
    );
  }
  return (
    <EffectComposer multisampling={0}>
      <Bloom
        ref={bloomRef as unknown as React.Ref<unknown>}
        intensity={0.9}
        luminanceThreshold={0.35}
        luminanceSmoothing={0.4}
        mipmapBlur
        kernelSize={KernelSize.SMALL}
      />
      <Vignette eskil={false} offset={0.15} darkness={0.85} />
    </EffectComposer>
  );
}
