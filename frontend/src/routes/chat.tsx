import { Link, createFileRoute } from "@tanstack/react-router";
import { Menu, Plus, SendHorizontal, User, X } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { mockSessions } from "@/api/sessions";
import { MyraLogo } from "@/components/myra/logo";
import { cn } from "@/lib/utils";
import type { ChatMessage, ChatSession } from "@/types";

export const Route = createFileRoute("/chat")({
  head: () => ({
    meta: [
      { title: "Sessions — Myra" },
      {
        name: "description",
        content: "Your Myra coding sessions and agent conversation workspace.",
      },
      { property: "og:title", content: "Sessions — Myra" },
      {
        property: "og:description",
        content: "Your Myra coding sessions and agent conversation workspace.",
      },
    ],
  }),
  component: ChatPage,
});

function ChatPage() {
  const [sessions, setSessions] = useState<ChatSession[]>(mockSessions);
  const [activeId, setActiveId] = useState<string>(mockSessions[0]?.id ?? "");
  const [draft, setDraft] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);

  const active = useMemo(
    () => sessions.find((s) => s.id === activeId) ?? null,
    [sessions, activeId],
  );

  const newSession = () => {
    const session: ChatSession = {
      id: `s${Date.now()}`,
      title: "New session",
      updatedAt: "Just now",
      messages: [],
    };
    setSessions((prev) => [session, ...prev]);
    setActiveId(session.id);
    setMenuOpen(false);
  };

  // Frontend-only: appends the message locally. No AI call is made.
  const handleSend = (event: FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    if (!content || !active) return;
    const message: ChatMessage = {
      id: `m${Date.now()}`,
      role: "user",
      content,
      createdAt: "Just now",
    };
    setSessions((prev) =>
      prev.map((s) =>
        s.id === active.id
          ? {
              ...s,
              title: s.messages.length === 0 ? content.slice(0, 40) : s.title,
              messages: [...s.messages, message],
            }
          : s,
      ),
    );
    setDraft("");
  };

  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="px-6 py-6">
        <Link to="/">
          <MyraLogo />
        </Link>
      </div>

      <div className="px-4">
        <button
          type="button"
          onClick={newSession}
          className="flex w-full items-center gap-2 rounded-md border border-border px-3 py-2.5 text-sm text-foreground transition-colors hover:bg-secondary"
        >
          <Plus className="size-4" />
          New session
        </button>
      </div>

      <div className="mt-6 flex-1 overflow-y-auto px-3">
        <p className="px-3 pb-2 text-xs tracking-wide text-muted-foreground uppercase">Sessions</p>
        <ul className="space-y-1">
          {sessions.map((session) => (
            <li key={session.id}>
              <button
                type="button"
                onClick={() => {
                  setActiveId(session.id);
                  setMenuOpen(false);
                }}
                className={cn(
                  "w-full rounded-md px-3 py-2.5 text-left text-sm transition-colors",
                  session.id === activeId
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                )}
              >
                <span className="block truncate">{session.title}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  {session.updatedAt}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="border-t border-border px-4 py-4">
        <button
          type="button"
          className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-sm transition-colors hover:bg-secondary"
        >
          <span className="flex size-7 items-center justify-center rounded-full bg-primary text-primary-foreground">
            <User className="size-4" />
          </span>
          <span className="flex-1 text-left">Myra User</span>
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex h-screen bg-background">
      <aside className="hidden w-[17rem] shrink-0 border-r border-border bg-sidebar md:block">
        {sidebar}
      </aside>

      {menuOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-background/80"
            onClick={() => setMenuOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 w-[17rem] border-r border-border bg-sidebar">
            {sidebar}
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-border px-4 py-4 md:px-8">
          <button
            type="button"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            onClick={() => setMenuOpen((v) => !v)}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:text-foreground md:hidden"
          >
            {menuOpen ? <X className="size-5" /> : <Menu className="size-5" />}
          </button>
          <h1 className="truncate text-sm text-muted-foreground">
            {active?.title ?? "No session selected"}
          </h1>
        </header>

        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-5 py-10">
            {!active || active.messages.length === 0 ? (
              <div className="pt-20 text-center">
                <p className="text-lg text-foreground">Start a session</p>
                <p className="mt-2 text-sm text-muted-foreground">
                  Describe the task and Myra will work through it with you.
                </p>
              </div>
            ) : (
              <ul className="space-y-8">
                {active.messages.map((message) => (
                  <li key={message.id}>
                    <p className="mb-2 text-xs tracking-wide text-muted-foreground uppercase">
                      {message.role === "user" ? "You" : "Myra"}
                    </p>
                    <p
                      className={cn(
                        "text-sm leading-relaxed",
                        message.role === "user" ? "text-foreground" : "text-muted-foreground",
                      )}
                    >
                      {message.content}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="px-5 pb-8">
          <form onSubmit={handleSend} className="mx-auto w-full max-w-3xl">
            <label htmlFor="chat-input" className="sr-only">
              Message Myra
            </label>
            <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-2 transition-colors focus-within:border-primary/60">
              <input
                id="chat-input"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Message Myra…"
                className="min-w-0 flex-1 bg-transparent py-3 text-sm outline-none placeholder:text-muted-foreground"
              />
              <button
                type="submit"
                aria-label="Send message"
                disabled={!draft.trim()}
                className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                <SendHorizontal className="size-4" />
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
