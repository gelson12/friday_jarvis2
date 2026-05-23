'use client';

// Holographic command-deck HUD. Used as the static fallback when WebGL or
// the 3D scene fails. Aims for a "looks great even without 3D" baseline:
//   - 9 concentric rings with different dash patterns, rotation directions,
//     and speeds — feels like a real reactor scaffold, not just two circles.
//   - 4 corner brackets with telemetry labels.
//   - A pulsing center reactor with halo and radial pulse waves.
//   - A sweeping radar beam that rotates clockwise around the core.
//   - Animated cyan grid floor in receding perspective.
//   - 60 tick marks around the outer ring with cardinal labels.
// Pure SVG + CSS; no JS state, no canvas, no external assets.
export function FallbackSvg() {
  return (
    <div aria-hidden className="jarvis-hud-fallback">
      <div className="jh-grid" />

      <svg
        className="jh-svg"
        viewBox="-500 -500 1000 1000"
        preserveAspectRatio="xMidYMid slice"
      >
        <defs>
          <radialGradient id="jh-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#9CF3FF" stopOpacity="0.95" />
            <stop offset="35%" stopColor="#3CDFFF" stopOpacity="0.60" />
            <stop offset="100%" stopColor="#3CDFFF" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="jh-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#3CDFFF" stopOpacity="0.28" />
            <stop offset="55%" stopColor="#0A6E86" stopOpacity="0.10" />
            <stop offset="100%" stopColor="#000000" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="jh-sweep" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#3CDFFF" stopOpacity="0" />
            <stop offset="80%" stopColor="#3CDFFF" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#9CF3FF" stopOpacity="0.85" />
          </linearGradient>
        </defs>

        <rect x="-500" y="-500" width="1000" height="1000" fill="url(#jh-glow)" />

        <g className="jh-sweep">
          <path d="M 0 0 L 460 -34 A 462 462 0 0 1 460 34 Z" fill="url(#jh-sweep)" />
        </g>

        <g stroke="#3CDFFF" fill="none" strokeLinecap="round">
          <g className="jh-spin-cw-slow">
            <circle r="470" strokeOpacity="0.22" strokeWidth="1" strokeDasharray="1 12" />
            <circle r="420" strokeOpacity="0.45" strokeWidth="1.2" strokeDasharray="80 30 4 30" />
          </g>
          <g className="jh-spin-ccw">
            <circle r="370" strokeOpacity="0.30" strokeWidth="1" strokeDasharray="140 22" />
            <circle r="320" strokeOpacity="0.55" strokeWidth="1.5" />
          </g>
          <g className="jh-spin-cw">
            <circle r="270" strokeOpacity="0.35" strokeWidth="1" strokeDasharray="6 18" />
            <circle r="225" strokeOpacity="0.65" strokeWidth="1.2" strokeDasharray="50 14" />
          </g>
          <g className="jh-spin-ccw-slow">
            <circle r="180" strokeOpacity="0.40" strokeWidth="0.8" strokeDasharray="3 8" />
            <line x1="-200" y1="0" x2="200" y2="0" strokeOpacity="0.20" strokeDasharray="2 6" />
            <line x1="0" y1="-200" x2="0" y2="200" strokeOpacity="0.20" strokeDasharray="2 6" />
          </g>
          <circle r="135" stroke="#EAFBFF" strokeOpacity="0.55" strokeWidth="0.8" />
          <circle
            r="100"
            strokeOpacity="0.85"
            strokeWidth="1"
            strokeDasharray="2 4"
            className="jh-spin-cw"
          />
        </g>

        <g stroke="#3CDFFF" strokeOpacity="0.45" strokeWidth="1.2">
          {Array.from({ length: 60 }).map((_, i) => {
            const a = (i / 60) * Math.PI * 2;
            const r1 = 440;
            const r2 = i % 5 === 0 ? 460 : 450;
            return (
              <line
                key={i}
                x1={Math.cos(a) * r1}
                y1={Math.sin(a) * r1}
                x2={Math.cos(a) * r2}
                y2={Math.sin(a) * r2}
              />
            );
          })}
        </g>

        <g
          fill="#3CDFFF"
          fontFamily="ui-monospace, SFMono-Regular, monospace"
          fontSize="14"
          fontWeight="700"
          textAnchor="middle"
          opacity="0.75"
        >
          <text x="0" y="-405">N</text>
          <text x="0" y="423">S</text>
          <text x="-405" y="5">W</text>
          <text x="405" y="5">E</text>
        </g>

        <g stroke="#3CDFFF" fill="none">
          <circle r="60" strokeOpacity="0.85" strokeWidth="1.2" className="jh-pulse-a" />
          <circle r="60" strokeOpacity="0.55" strokeWidth="1" className="jh-pulse-b" />
          <circle r="60" strokeOpacity="0.35" strokeWidth="0.8" className="jh-pulse-c" />
        </g>

        <circle r="60" fill="url(#jh-core)" className="jh-core" />
        <circle r="22" fill="#9CF3FF" opacity="0.95" className="jh-core-bright" />

        <g fill="#9CF3FF">
          <g className="jh-spin-cw-slow">
            <circle cx="320" cy="0" r="3" opacity="0.95" />
            <circle cx="-320" cy="0" r="2.5" opacity="0.7" />
          </g>
          <g className="jh-spin-ccw">
            <circle cx="0" cy="225" r="2.2" opacity="0.85" />
            <circle cx="0" cy="-225" r="2.2" opacity="0.85" />
          </g>
          <g className="jh-spin-cw">
            <circle cx="190" cy="120" r="1.8" opacity="0.6" />
            <circle cx="-190" cy="-120" r="1.8" opacity="0.6" />
            <circle cx="-120" cy="190" r="1.6" opacity="0.5" />
            <circle cx="120" cy="-190" r="1.6" opacity="0.5" />
          </g>
        </g>
      </svg>

      <div className="jh-bracket jh-tl">
        <span className="jh-label">SYS</span>
        <span className="jh-val">F.R.I.D.A.Y</span>
        <span className="jh-sub">core::online</span>
      </div>
      <div className="jh-bracket jh-tr">
        <span className="jh-label">LK</span>
        <span className="jh-val">LIVEKIT</span>
        <span className="jh-sub">link::secure</span>
      </div>
      <div className="jh-bracket jh-bl">
        <span className="jh-label">RTC</span>
        <span className="jh-val jh-flicker">STREAMING</span>
        <span className="jh-sub">audio::active</span>
      </div>
      <div className="jh-bracket jh-br">
        <span className="jh-label">SVC</span>
        <span className="jh-val">NOMINAL</span>
        <span className="jh-sub">draw::stable</span>
      </div>

      <div className="jh-scanlines" />

      <style>{`
        .jarvis-hud-fallback {
          position: absolute;
          inset: 0;
          overflow: hidden;
          background:
            radial-gradient(ellipse at center, #03101a 0%, #02060b 60%, #000308 100%);
          color: #3CDFFF;
        }
        .jh-svg {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
        }
        .jh-grid {
          position: absolute;
          left: -10%;
          right: -10%;
          bottom: -10%;
          height: 55%;
          background-image:
            linear-gradient(to right, rgba(60,223,255,0.18) 1px, transparent 1px),
            linear-gradient(to top, rgba(60,223,255,0.18) 1px, transparent 1px);
          background-size: 60px 60px;
          transform: perspective(600px) rotateX(60deg) translateZ(-20px);
          transform-origin: center bottom;
          mask-image: linear-gradient(to top, black 25%, transparent 85%);
          -webkit-mask-image: linear-gradient(to top, black 25%, transparent 85%);
          animation: jh-grid-pan 12s linear infinite;
          opacity: 0.55;
        }
        @keyframes jh-grid-pan {
          from { background-position: 0 0; }
          to   { background-position: 0 60px; }
        }
        .jh-scanlines {
          position: absolute;
          inset: 0;
          background: repeating-linear-gradient(
            0deg,
            rgba(60, 223, 255, 0.045) 0px,
            rgba(60, 223, 255, 0.045) 1px,
            transparent 1px,
            transparent 3px
          );
          mix-blend-mode: screen;
          opacity: 0.6;
          pointer-events: none;
        }
        .jh-spin-cw       { animation: jh-rot 60s linear infinite; transform-origin: 0 0; }
        .jh-spin-cw-slow  { animation: jh-rot 140s linear infinite; transform-origin: 0 0; }
        .jh-spin-ccw      { animation: jh-rot 90s linear infinite reverse; transform-origin: 0 0; }
        .jh-spin-ccw-slow { animation: jh-rot 180s linear infinite reverse; transform-origin: 0 0; }
        .jh-sweep {
          animation: jh-rot 6s linear infinite;
          transform-origin: 0 0;
          opacity: 0.75;
          mix-blend-mode: screen;
        }
        @keyframes jh-rot { to { transform: rotate(360deg); } }

        .jh-core {
          transform-origin: center;
          transform-box: fill-box;
          animation: jh-core-pulse 3.6s ease-in-out infinite;
        }
        .jh-core-bright {
          filter: blur(0.5px) drop-shadow(0 0 16px #3CDFFF);
          animation: jh-core-bright 2.4s ease-in-out infinite;
        }
        @keyframes jh-core-pulse {
          0%, 100% { transform: scale(0.92); opacity: 0.78; }
          50%      { transform: scale(1.08); opacity: 1; }
        }
        @keyframes jh-core-bright {
          0%, 100% { opacity: 0.85; }
          50%      { opacity: 1; }
        }

        .jh-pulse-a, .jh-pulse-b, .jh-pulse-c { transform-origin: 0 0; }
        .jh-pulse-a { animation: jh-pulse 3.5s ease-out infinite; }
        .jh-pulse-b { animation: jh-pulse 3.5s ease-out 0.7s infinite; }
        .jh-pulse-c { animation: jh-pulse 3.5s ease-out 1.4s infinite; }
        @keyframes jh-pulse {
          0%   { transform: scale(0.6); opacity: 0; }
          15%  { opacity: 1; }
          100% { transform: scale(4.2); opacity: 0; }
        }

        .jh-bracket {
          position: absolute;
          padding: 10px 14px;
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 11px;
          letter-spacing: 0.12em;
          line-height: 1.4;
          color: #9CF3FF;
          text-shadow: 0 0 6px rgba(60, 223, 255, 0.6);
          display: flex;
          flex-direction: column;
          gap: 2px;
          opacity: 0.85;
          pointer-events: none;
        }
        .jh-bracket::before,
        .jh-bracket::after {
          content: '';
          position: absolute;
          width: 22px;
          height: 22px;
          border: 1px solid #3CDFFF;
          opacity: 0.85;
        }
        .jh-tl { top: 18px;    left: 18px;  align-items: flex-start; }
        .jh-tr { top: 18px;    right: 18px; align-items: flex-end;   text-align: right; }
        .jh-bl { bottom: 100px; left: 18px;  align-items: flex-start; }
        .jh-br { bottom: 100px; right: 18px; align-items: flex-end;   text-align: right; }
        .jh-tl::before { top: 0;    left: 0;   border-right: 0; border-bottom: 0; }
        .jh-tl::after  { bottom: 0; right: 0;  border-top: 0;   border-left: 0;   }
        .jh-tr::before { top: 0;    right: 0;  border-left: 0;  border-bottom: 0; }
        .jh-tr::after  { bottom: 0; left: 0;   border-top: 0;   border-right: 0;  }
        .jh-bl::before { top: 0;    left: 0;   border-right: 0; border-bottom: 0; }
        .jh-bl::after  { bottom: 0; right: 0;  border-top: 0;   border-left: 0;   }
        .jh-br::before { top: 0;    right: 0;  border-left: 0;  border-bottom: 0; }
        .jh-br::after  { bottom: 0; left: 0;   border-top: 0;   border-right: 0;  }
        .jh-label { font-size: 9px; opacity: 0.55; letter-spacing: 0.25em; }
        .jh-val   { font-size: 14px; font-weight: 700; }
        .jh-sub   { font-size: 9px; opacity: 0.55; }
        .jh-flicker { animation: jh-flicker 0.18s steps(2, end) infinite; }
        @keyframes jh-flicker {
          0%, 100% { opacity: 1; }
          50%      { opacity: 0.78; }
        }

        @media (prefers-reduced-motion: reduce) {
          .jh-spin-cw, .jh-spin-cw-slow, .jh-spin-ccw, .jh-spin-ccw-slow,
          .jh-sweep, .jh-core, .jh-core-bright,
          .jh-pulse-a, .jh-pulse-b, .jh-pulse-c,
          .jh-grid, .jh-flicker {
            animation: none;
          }
        }
      `}</style>
    </div>
  );
}
