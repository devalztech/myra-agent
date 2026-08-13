import { cn } from "@/lib/utils";

/**
 * Myra mark — two mirrored royal-blue blades forming an "M".
 * Used in the header, the empty state and the assistant avatar.
 */
export function MyraMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" className={cn("size-6", className)} fill="none">
      <path d="M3 4.5 13.6 16 3 27.5V4.5Z" fill="currentColor" opacity="0.55" />
      <path d="M29 4.5 18.4 16 29 27.5V4.5Z" fill="currentColor" opacity="0.55" />
      <path d="M6.5 4.5 16 15.2 25.5 4.5 16 27.5 6.5 4.5Z" fill="currentColor" />
    </svg>
  );
}

export function MyraLogo({
  className,
  markClassName,
}: {
  className?: string;
  markClassName?: string;
}) {
  return (
    <span className={cn("flex items-center gap-2.5", className)}>
      <MyraMark className={cn("size-6 text-royal", markClassName)} />
      <span className="text-[1.0625rem] font-semibold tracking-tight text-foreground">Myra</span>
    </span>
  );
}

/** Small circular avatar shown next to assistant messages. */
export function MyraAvatar({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "flex size-8 shrink-0 items-center justify-center rounded-full",
        "bg-royal/12 ring-1 ring-royal/35",
        className,
      )}
    >
      <MyraMark className="size-4 text-royal" />
    </span>
  );
}
