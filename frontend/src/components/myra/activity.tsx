import {
  AlertTriangle,
  Check,
  ChevronDown,
  FileEdit,
  Globe,
  Lightbulb,
  Loader2,
  Save,
  ShieldAlert,
  ShieldQuestion,
  SquareTerminal,
  TextSearch,
} from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";
import type { ActivityKind, ActivityStep } from "@/types";

/**
 * "Myra is working…" — the live agent timeline.
 *
 * Each row's icon is driven by what the step actually IS (kind: thought,
 * read, edit, command, network…), while its color/ring is driven by what
 * HAPPENED to it (status: running, done, error, blocked). Rows are collapsed
 * to one truncated line by default; tapping a row opens it to show the full
 * thought, command, output, or error, and tapping again closes it.
 */

const KIND_ICON: Record<ActivityKind, typeof Lightbulb> = {
  think: Lightbulb,
  read: TextSearch,
  edit: FileEdit,
  run: SquareTerminal,
  network: Globe,
  memory: Save,
  done: Check,
};

function StepIcon({ kind, status }: { kind: ActivityKind; status: ActivityStep["status"] }) {
  if (status === "running") {
    return (
      <span className="z-10 flex size-[1.375rem] shrink-0 items-center justify-center rounded-full bg-royal/25">
        <Loader2 className="size-3 animate-spin text-royal" strokeWidth={2.5} />
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="z-10 flex size-[1.375rem] shrink-0 items-center justify-center rounded-full bg-destructive/20">
        <AlertTriangle className="size-3 text-destructive" strokeWidth={2.5} />
      </span>
    );
  }
  if (status === "needs_approval") {
    return (
      <span className="z-10 flex size-[1.375rem] shrink-0 items-center justify-center rounded-full bg-amber-500/20">
        <ShieldQuestion className="size-3 text-amber-500" strokeWidth={2.5} />
      </span>
    );
  }
  if (status === "unsafe" || status === "blocked") {
    return (
      <span className="z-10 flex size-[1.375rem] shrink-0 items-center justify-center rounded-full bg-muted">
        <ShieldAlert className="size-3 text-muted-foreground" strokeWidth={2.5} />
      </span>
    );
  }
  if (status === "pending") {
    return (
      <span className="z-10 size-[1.375rem] shrink-0 rounded-full bg-background ring-1 ring-border" />
    );
  }
  // done
  const Icon = KIND_ICON[kind] ?? Check;
  return (
    <span className="z-10 flex size-[1.375rem] shrink-0 items-center justify-center rounded-full bg-royal">
      <Icon className="size-3 text-primary-foreground" strokeWidth={2.5} />
    </span>
  );
}

function StatusLabel({ status }: { status: ActivityStep["status"] }) {
  if (status === "done") return <span className="text-[0.8125rem] text-royal-soft">Done</span>;
  if (status === "running")
    return <span className="text-[0.8125rem] text-royal-soft">Working…</span>;
  if (status === "needs_approval")
    return <span className="text-[0.8125rem] text-amber-500">Needs approval</span>;
  if (status === "unsafe")
    return <span className="text-[0.8125rem] text-muted-foreground">Blocked (unsafe path)</span>;
  if (status === "blocked")
    return <span className="text-[0.8125rem] text-muted-foreground">Blocked</span>;
  if (status === "error")
    return <span className="text-[0.8125rem] text-destructive">Failed</span>;
  return <span className="text-[0.8125rem] text-muted-foreground">Pending</span>;
}

function StepRow({
  step,
  isLast,
  open,
  onToggle,
}: {
  step: ActivityStep;
  isLast: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const expandable = Boolean(step.body || (step.args && Object.keys(step.args).length > 0));

  return (
    <li className="relative">
      <div className="relative flex items-start gap-3">
        {!isLast && (
          <span
            aria-hidden="true"
            className="absolute top-[1.375rem] left-[0.65rem] h-[calc(100%+0.75rem-1.375rem)] w-px bg-border"
          />
        )}
        <StepIcon kind={step.kind} status={step.status} />
        <button
          type="button"
          onClick={expandable ? onToggle : undefined}
          disabled={!expandable}
          aria-expanded={expandable ? open : undefined}
          className={cn(
            "focus-royal flex min-w-0 flex-1 items-baseline justify-between gap-3 rounded-lg text-left",
            expandable && "cursor-pointer",
          )}
        >
          <span className="min-w-0 flex-1">
            <span className="block break-words text-[0.9375rem] leading-snug text-foreground/90">
              {step.label}
            </span>
            {step.detail && !open && (
              <span className="mt-0.5 block break-words font-mono text-[0.75rem] leading-snug text-muted-foreground">
                {step.detail}
              </span>
            )}
          </span>
          <span className="flex shrink-0 items-center gap-1.5">
            <StatusLabel status={step.status} />
            {expandable && (
              <ChevronDown
                className={cn(
                  "size-3.5 text-muted-foreground transition-transform",
                  open && "rotate-180",
                )}
              />
            )}
          </span>
        </button>
      </div>

      {expandable && open && (
        <div className="mt-2 ml-[2.125rem] space-y-2 rounded-xl bg-background/60 px-3 py-2.5 ring-1 ring-hairline">
          {step.args && Object.keys(step.args).length > 0 && (
            <pre className="scroll-slim overflow-x-auto font-mono text-[0.75rem] text-muted-foreground">
              {Object.entries(step.args)
                .map(([key, value]) => `${key}: ${String(value)}`)
                .join("\n")}
            </pre>
          )}
          {step.body && (
            <pre
              className={cn(
                "scroll-slim max-h-64 overflow-auto font-mono text-[0.75rem] whitespace-pre-wrap",
                step.status === "error" ? "text-destructive" : "text-foreground/80",
              )}
            >
              {step.body}
            </pre>
          )}
        </div>
      )}
    </li>
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
  const [openId, setOpenId] = useState<string | null>(null);

  if (steps.length === 0) return null;

  return (
    <div className="rounded-2xl bg-bubble-agent px-4 py-3.5">
      <p className={cn("mb-3 text-[0.9375rem]", running ? "text-shimmer" : "text-royal-soft")}>
        {title ?? (running ? "Myra is working…" : "Myra's steps")}
      </p>

      <ol className="relative space-y-3">
        {steps.map((step, index) => (
          <StepRow
            key={step.id}
            step={step}
            isLast={index === steps.length - 1}
            open={openId === step.id}
            onToggle={() => setOpenId((current) => (current === step.id ? null : step.id))}
          />
        ))}
      </ol>
    </div>
  );
}
