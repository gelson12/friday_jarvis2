// Connecting state shown while the auto-connect handshake completes.
// No "Talk to Friday" button: the session connects on mount (see
// view-controller.tsx auto-connect effect), so a click target would be
// noise that the user has to mentally ignore.
//
// Just a minimal, in-character status line under the HUD's center
// reactor. The HUD itself provides the visual focal point.
export const WelcomeView = ({
  ref,
}: React.ComponentProps<'div'> & {
  // Props kept for API compatibility with ViewController; unused here.
  startButtonText: string;
  onStartCall: () => void;
}) => {
  return (
    <div ref={ref}>
      <section className="flex flex-col items-center justify-center text-center">
        <p className="font-mono text-xs tracking-[0.25em] text-cyan-300/80 uppercase">
          initializing
        </p>
        <p className="mt-2 font-mono text-[10px] tracking-[0.2em] text-cyan-300/40 uppercase">
          friday.uplink&nbsp;&middot;&nbsp;say &ldquo;hey friday&rdquo; once connected
        </p>
      </section>
    </div>
  );
};
