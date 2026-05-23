// Static SVG/CSS HUD backdrop. Used when WebGL is unavailable, GPU tier is
// too low to render the 3D scene smoothly, the user prefers reduced motion,
// or the live scene's WebGL context is lost mid-session.
export function FallbackSvg() {
  return (
    <div aria-hidden className="jarvis-hud-fallback">
      <svg
        className="jarvis-hud-fallback-svg"
        viewBox="0 0 1000 1000"
        preserveAspectRatio="xMidYMid slice"
      >
        <defs>
          <radialGradient id="hud-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#3CDFFF" stopOpacity="0.30" />
            <stop offset="55%" stopColor="#0A6E86" stopOpacity="0.10" />
            <stop offset="100%" stopColor="#000000" stopOpacity="0" />
          </radialGradient>
        </defs>

        <rect width="1000" height="1000" fill="url(#hud-glow)" />

        <g stroke="#3CDFFF" fill="none" strokeWidth="1.5" transform="translate(500 500)">
          <g className="hud-spin-cw">
            <circle r="430" strokeOpacity="0.25" strokeDasharray="2 14" />
            <circle r="360" strokeOpacity="0.35" strokeDasharray="60 28" />
          </g>
          <g className="hud-spin-ccw">
            <circle r="300" strokeOpacity="0.45" strokeDasharray="120 40" />
            <circle r="240" strokeOpacity="0.3" />
            <line x1="-260" y1="0" x2="260" y2="0" strokeOpacity="0.18" />
            <line x1="0" y1="-260" x2="0" y2="260" strokeOpacity="0.18" />
          </g>
          <g className="hud-spin-cw-slow">
            <circle r="170" strokeOpacity="0.5" strokeDasharray="4 10" />
          </g>
          <circle r="90" stroke="#EAFBFF" strokeOpacity="0.4" />
          <circle className="hud-pulse" r="48" fill="#3CDFFF" stroke="none" />
        </g>
      </svg>

      <div className="jarvis-hud-fallback-scanlines" />

      <style>{`
        .jarvis-hud-fallback {
          position: absolute;
          inset: 0;
          overflow: hidden;
          background: #02060b;
        }
        .jarvis-hud-fallback-svg {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          filter: blur(1.5px) brightness(0.7);
        }
        .jarvis-hud-fallback-scanlines {
          position: absolute;
          inset: 0;
          background: repeating-linear-gradient(
            0deg,
            rgba(60, 223, 255, 0.05) 0px,
            rgba(60, 223, 255, 0.05) 1px,
            transparent 1px,
            transparent 4px
          );
          mix-blend-mode: screen;
          opacity: 0.5;
        }
        .hud-spin-cw { animation: hud-rot 60s linear infinite; transform-origin: 0 0; }
        .hud-spin-cw-slow { animation: hud-rot 120s linear infinite; transform-origin: 0 0; }
        .hud-spin-ccw { animation: hud-rot 90s linear infinite reverse; transform-origin: 0 0; }
        .hud-pulse { animation: hud-pulse 4s ease-in-out infinite; transform-origin: 0 0; }
        @keyframes hud-rot { to { transform: rotate(360deg); } }
        @keyframes hud-pulse {
          0%, 100% { opacity: 0.35; transform: scale(0.9); }
          50% { opacity: 0.75; transform: scale(1.08); }
        }
        @media (prefers-reduced-motion: reduce) {
          .hud-spin-cw, .hud-spin-cw-slow, .hud-spin-ccw, .hud-pulse {
            animation: none;
          }
        }
      `}</style>
    </div>
  );
}
