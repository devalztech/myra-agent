import { cn } from "@/lib/utils";

export function MyraMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={cn("size-5", className)} fill="none">
      <path d="M12 3 20.5 18h-6.2L12 13.4 9.7 18H3.5L12 3Z" fill="currentColor" opacity="0.9" />
      <circle cx="6.2" cy="20" r="1.9" fill="currentColor" />
      <circle cx="17.8" cy="20" r="1.9" fill="currentColor" />
    </svg>
  );
}

export function MyraLogo({ className }: { className?: string }) {
  return (
    <span className={cn("flex items-center gap-2 text-primary", className)}>
      <span className="text-xl font-semibold tracking-tight">Myra</span>
      <MyraMark />
    </span>
  );
}
