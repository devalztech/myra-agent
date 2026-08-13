import { AlertTriangle, Check, ShieldAlert } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ActivityStep } from "@/types";

/**
 * "Myra is working…" — the live agent timeline.
 * ✓ Reading file → ✓ Editing → ● Running tests → ✓ Done
 */

function StatusLabel({ status }: { status: ActivityStep["status"] }) {
  if (status === "done")
    return <span className="text-[0.8125rem] text-royal-soft">Done</span>;
  if (status === "running")
    return <span className="text-[0.8125rem] text-royal-soft">In progress</span>;
  if (status === "blocked")
    return <span className="text-[0.8125rem] text-muted-foreground">Blocked</span>;
  if (status === "error")
    return <span className="text-[0.8125rem] text-destructive">Failed</span>;
  return <span className="text-[0.8125rem] text-muted-foreground">Pending</span>;
}

function StepDot({ status }: { status: ActivityStep["status"] }) {
  if (status === "done")
    return (
      <span className="z-10 flex size-[1.375rem] items-center justify-center rounded-full bg-royal">
        <Check className="size-3 text-primary-foreground" strokeWidth={3} />
      </span>
    );
  if (status === "running")
    return (
      <span className="z-10 flex size-[1.375rem] items-center justify-center rounded-full bg-royal/25">
        <span className="size-2 rounded-full bg-royal caret-pulse" />
      </span>
    );
  if (status === "error")
    return (
      <span className="z-10 flex size-[1.375rem] items-center justify-center rounded-full bg-destructive/20">
        <AlertTriangle className="size-3 text-destructive" />
      </span>
    );
  if (status === "blocked")
    return (
      <span className="z-10 flex size-[1.375rem] items-center justify-center rounded-full bg-muted">
        <ShieldAlert className="size-3 text-muted-foreground" />
      </span>
    );
  return (
    <span className="z-10 size-[1.375rem] rounded-full bg-background ring-1 ring-border" />
  );
}

export function ActivityTimeline({
  steps,
  running,
  title,
}: {
  steps: ActivityStep[];
  running: boolean;
  title?: string;
}) {
  if (steps.length === 0) return null;

  return (
    <div className="rounded-2xl bg-bubble-agent px-4 py-3.5">
      <p className={cn("mb-3 text-[0.9375rem]", running ? "text-shimmer" : "text-royal-soft")}>
        {title ?? (running ? "Myra is working…" : "Myra's steps")}
      </p>

      <ol className="relative space-y-3">
        {steps.map((step, index) => (
          <li key={step.id} className="relative flex items-start gap-3">
            {index < steps.length - 1 && (
              <span
                aria-hidden="true"
                className="absolute top-[1.375rem] left-[0.65rem] h-[calc(100%+0.75rem-1.375rem)] w-px bg-border"
              />
            )}
            <StepDot status={step.status} />
            <span className="flex min-w-0 flex-1 items-baseline justify-between gap-3">
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[0.9375rem] text-foreground/90">
                  {step.label}
                </span>
                {step.detail && (
                  <span className="mt-0.5 block truncate font-mono text-[0.75rem] text-muted-foreground">
                    {step.detail}
                  </span>
                )}
              </span>
              <StatusLabel status={step.status} />
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
