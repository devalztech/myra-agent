import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { LogOut, Menu, Plus, SendHorizontal, Trash2, User, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  createSession,
  deleteSession,
  fetchModelStatus,
  fetchSession,
  fetchSessions,
  streamMessage,
} from "@/api/sessions";
import { MyraLogo } from "@/components/myra/logo";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import type { ChatMessage, ModelStatus, SessionSummary } from "@/types";

export const Route = createFileRoute("/chat")({
  head: () => ({
    meta: [
      { title: "Sessions — Myra" },
      {
        name: "description",
        content: "Your Myra chat sessions, powered by a local Llama model.",
      },
      { property: "og:title", content: "Sessions — Myra" },
      {
        property: "og:description",
        content: "Your Myra chat sessions, powered by a local Llama model.",
      },
    ],
  }),
  component: ChatPage,
});

function ChatPage() {
  const navigate = useNavigate();
  const { token, user, ready, signOut } = useAuth();

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState("");
  const [sending, setSending] = useState(false);
  const [draft, setDraft] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState<ModelStatus | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // --- guard ----------------------------------------------------------
  useEffect(() => {
    if (ready && !token) navigate({ to: "/login" });
  }, [ready, token, navigate]);

  // --- initial load ----------------------------------------------------
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    fetchSessions(token)
      .then((rows) => {
        if (cancelled) return;
        setSessions(rows);
        setActiveId((current) => current ?? rows[0]?.id ?? null);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Could not load sessions."),
      );
    fetchModelStatus()
      .then((status) => !cancelled && setModel(status))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [token]);

  // --- load the active conversation ------------------------------------
  useEffect(() => {
    if (!token || !activeId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    fetchSession(token, activeId)
      .then((session) => !cancelled && setMessages(session.messages))
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Could not load this session."),
      );
    return () => {
      cancelled = true;
    };
  }, [token, activeId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streaming]);

  const newSession = useCallback(async () => {
    if (!token) return;
    try {
      const session = await createSession(token);
      setSessions((prev) => [
        { id: session.id, title: session.title, updatedAt: session.updatedAt },
        ...prev,
      ]);
      setActiveId(session.id);
      setMessages([]);
      setMenuOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create a session.");
    }
  }, [token]);

  const removeSession = useCallback(
    async (id: string) => {
      if (!token) return;
      try {
        await deleteSession(token, id);
        setSessions((prev) => {
          const next = prev.filter((s) => s.id !== id);
          setActiveId((current) => (current === id ? (next[0]?.id ?? null) : current));
          return next;
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not delete this session.");
      }
    },
    [token],
  );

  const handleSend = async (event: FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    if (!content || !token || sending) return;

    setError(null);
    setDraft("");
    setSending(true);
    setStreaming("");

    let sessionId = activeId;
    try {
      if (!sessionId) {
        const created = await createSession(token);
        sessionId = created.id;
        setSessions((prev) => [
          { id: created.id, title: created.title, updatedAt: created.updatedAt },
          ...prev,
        ]);
        setActiveId(created.id);
      }

      const optimistic: ChatMessage = {
        id: `local-${Date.now()}`,
        role: "user",
        content,
        createdAt: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimistic]);

      await streamMessage(token, sessionId, content, {
        onSession: ({ id, title }) =>
          setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s))),
        onToken: (piece) => setStreaming((prev) => prev + piece),
        onDone: (assistant) => {
          setStreaming("");
          setMessages((prev) => [
            ...prev,
            {
              id: assistant.id,
              role: "assistant",
              content: assistant.content,
              createdAt: assistant.createdAt,
            },
          ]);
        },
        onError: (message) => setError(message),
      });

      // Refresh the sidebar so the auto-generated title/order is accurate.
      const rows = await fetchSessions(token);
      setSessions(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Message failed to send.");
      setDraft(content);
    } finally {
      setStreaming("");
      setSending(false);
    }
  };

  const handleSignOut = () => {
    signOut();
    navigate({ to: "/login" });
  };

  const activeTitle = sessions.find((s) => s.id === activeId)?.title ?? "No session selected";

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
        {sessions.length === 0 ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">No sessions yet.</p>
        ) : (
          <ul className="space-y-1">
            {sessions.map((session) => (
              <li key={session.id} className="group relative">
                <button
                  type="button"
                  onClick={() => {
                    setActiveId(session.id);
                    setMenuOpen(false);
                  }}
                  className={cn(
                    "w-full rounded-md px-3 py-2.5 pr-9 text-left text-sm transition-colors",
                    session.id === activeId
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-secondary hover:text-foreground",
                  )}
                >
                  <span className="block truncate">{session.title}</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {new Date(session.updatedAt).toLocaleString()}
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={`Delete ${session.title}`}
                  onClick={() => removeSession(session.id)}
                  className="absolute top-2.5 right-2 rounded p-1 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:text-destructive focus:opacity-100"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="border-t border-border px-4 py-4">
        {model && (
          <p className="px-2 pb-3 text-xs text-muted-foreground">
            {model.model ?? "no model"} · {model.tier} · {model.ramGb} GB RAM
          </p>
        )}
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-3 rounded-md px-2 py-2 text-sm">
            <span className="flex size-7 items-center justify-center rounded-full bg-primary text-primary-foreground">
              <User className="size-4" />
            </span>
            <span className="max-w-[8rem] truncate">{user?.name ?? "Myra User"}</span>
          </span>
          <button
            type="button"
            onClick={handleSignOut}
            aria-label="Log out"
            className="ml-auto rounded-md p-2 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <LogOut className="size-4" />
          </button>
        </div>
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
            onClick={() => setMenuOpen(false)}
            className="absolute inset-0 bg-black/50"
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
          <h1 className="truncate text-sm text-muted-foreground">{activeTitle}</h1>
        </header>

        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-5 py-10">
            {messages.length === 0 && !streaming ? (
              <div className="pt-20 text-center">
                <p className="text-lg text-foreground">Start a session</p>
                <p className="mt-2 text-sm text-muted-foreground">
                  Ask Myra a coding question — everything runs on your local model.
                </p>
              </div>
            ) : (
              <ul className="space-y-8">
                {messages.map((message) => (
                  <li key={message.id}>
                    <p className="mb-2 text-xs tracking-wide text-muted-foreground uppercase">
                      {message.role === "user" ? "You" : "Myra"}
                    </p>
                    <p
                      className={cn(
                        "text-sm leading-relaxed whitespace-pre-wrap",
                        message.role === "user" ? "text-foreground" : "text-muted-foreground",
                      )}
                    >
                      {message.content}
                    </p>
                  </li>
                ))}
                {streaming && (
                  <li>
                    <p className="mb-2 text-xs tracking-wide text-muted-foreground uppercase">
                      Myra
                    </p>
                    <p className="text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                      {streaming}
                    </p>
                  </li>
                )}
                {sending && !streaming && (
                  <li className="text-sm text-muted-foreground">Myra is thinking…</li>
                )}
              </ul>
            )}

            {error && (
              <p role="alert" className="mt-8 text-sm text-destructive">
                {error}
              </p>
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
                disabled={sending}
                className="min-w-0 flex-1 bg-transparent py-3 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-60"
              />
              <button
                type="submit"
                aria-label="Send message"
                disabled={!draft.trim() || sending}
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
