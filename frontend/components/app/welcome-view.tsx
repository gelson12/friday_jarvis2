import { Button } from '@/components/ui/button';

function WelcomeImage() {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/arc-reactor.svg"
      alt="Friday"
      className="mb-4 size-40 drop-shadow-[0_0_28px_rgba(60,223,255,0.55)]"
    />
  );
}

interface WelcomeViewProps {
  startButtonText: string;
  onStartCall: () => void;
}

export const WelcomeView = ({
  startButtonText,
  onStartCall,
  ref,
}: React.ComponentProps<'div'> & WelcomeViewProps) => {
  return (
    <div ref={ref}>
      <section className="flex flex-col items-center justify-center text-center">
        <WelcomeImage />

        <p className="text-foreground max-w-prose pt-1 leading-6 font-medium">
          chat with the advanced Friday agent
        </p>

        <Button
          size="lg"
          onClick={onStartCall}
          className="mt-6 w-64 rounded-full font-mono text-xs font-bold tracking-wider uppercase"
        >
          {startButtonText}
        </Button>

        <p className="text-muted-foreground mt-3 text-xs">
          connecting&hellip; then say &ldquo;Hey Friday&rdquo; to begin
        </p>
      </section>
    </div>
  );
};
