import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Clock, FilePlus2, FolderOpen, SendHorizontal, TerminalSquare } from "lucide-react";
import { useState, type FormEvent } from "react";

import { AppShell } from "@/components/myra/app-shell";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Myra — Your AI coding agent" },
      {
        name: "description",
        content:
          "Myra is a personal AI coding agent. Think, build and ship from one clean workspace.",
      },
      { property: "og:title", content: "Myra — Your AI coding agent" },
      {
        property: "og:description",
        content:
          "Myra is a personal AI coding agent. Think, build and ship from one clean workspace.",
      },
    ],
  }),
  component: Index,
});

const quickActions = [
  { label: "Create a new project", icon: FilePlus2 },
  { label: "Open a project", icon: FolderOpen },
  { label: "View recent activity", icon: Clock },
  { label: "Open terminal", icon: TerminalSquare },
];

function Index() {
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState("");

  // Frontend-only: any prompt submission sends the visitor to Register.
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!prompt.trim()) return;
    navigate({ to: "/register" });
  };

  return (
    <AppShell>
      <div className="mx-auto flex w-full max-w-3xl flex-col items-center px-5 pt-16 pb-24 md:pt-28">
        <h1 className="text-center text-4xl font-semibold tracking-tight md:text-5xl">
          Hello, <span className="text-primary">Myra User</span>
        </h1>
        <p className="mt-4 text-center text-base text-muted-foreground">
          Your AI coding agent. Think. Build. Ship.
        </p>

        <form onSubmit={handleSubmit} className="mt-12 w-full">
          <label htmlFor="myra-prompt" className="sr-only">
            What do you want to build today?
          </label>
          <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 transition-colors focus-within:border-primary/60">
            <input
              id="myra-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="What do you want to build today?"
              className="min-w-0 flex-1 bg-transparent py-3 text-base outline-none placeholder:text-muted-foreground"
            />
            <button
              type="submit"
              aria-label="Send prompt"
              className="flex size-10 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
              disabled={!prompt.trim()}
            >
              <SendHorizontal className="size-4" />
            </button>
          </div>
        </form>

        <div className="mt-10 flex w-full flex-wrap items-center justify-center gap-x-10 gap-y-4">
          {quickActions.map(({ label, icon: Icon }) => (
            <button
              key={label}
              type="button"
              onClick={() => navigate({ to: "/register" })}
              className="flex items-center gap-2 text-sm text-primary transition-opacity hover:opacity-80"
            >
              <Icon className="size-4 text-muted-foreground" />
              {label}
            </button>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
