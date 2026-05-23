// Connecting state shown while the auto-connect handshake completes.
// No "Talk to Friday" button: the session connects on mount (see
// view-controller.tsx auto-connect effect), so a click target would
// just be noise.
//
// The HUD's center arc-reactor is the visual focal point during the
// connect; the status text sits at the bottom of the viewport so it
// doesn't overlap the glowing core.
export const WelcomeView = ({
  ref,
}: React.ComponentProps<'div'> & {
  // Kept for API compatibility with ViewController; unused.
  startButtonText: string;
  onStartCall: () => void;
}) => {
  return (
    <div ref={ref} className="contents">
      <div className="pointer-events-none fixed bottom-24 left-1/2 z-10 -translate-x-1/2 text-center">
        <p className="font-mono text-xs tracking-[0.35em] text-cyan-300/90 uppercase animate-pulse">
          initializing
        </p>
        <p className="mt-2 font-mono text-[10px] tracking-[0.25em] text-cyan-300/50 uppercase">
          friday.uplink
        </p>
      </div>
    </div>
  );
};
