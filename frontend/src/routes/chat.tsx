import { createFileRoute, useNavigate } from "@tanstack/react-router";
import {
  CheckCheck,
  ChevronRight,
  Cpu,
  FileText,
  Loader2,
  LogOut,
  Menu,
  MoreVertical,
  Paperclip,
  Plus,
  RotateCw,
  SendHorizontal,
  ShieldCheck,
  Square,
  SquarePen,
  Trash2,
  Wand2,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import {
  fetchAgentSettings,
  fetchProviderConfigs,
  fetchProviders,
  fetchSessionEvents,
  previewHealth,
  previewIframeUrl,
  runAgent,
  saveProviderConfig as saveProviderConfigApi,
  startPreview,
  stopAgent,
  stopPreview,
  updateAgentSettings,
  uploadWorkspaceFile,
} from "@/api/agent";
import {
  createSession,
  deleteSession,
  fetchModelStatus,
  fetchSession,
  fetchSessions,
  streamMessage,
} from "@/api/sessions";
import { ActivityTimeline } from "@/components/myra/activity";
import { MyraAvatar, MyraLogo, MyraMark } from "@/components/myra/logo";
import { Markdown } from "@/components/myra/markdown";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import type {
  ActivityStep,
  AgentEvent,
  AgentSettings,
  ChatMessage,
  ModelStatus,
  ProviderInfo,
  SessionSummary,
} from "@/types";

export const Route = createFileRoute("/chat")({
  head: () => ({
    meta: [
      { title: "Chat — Myra" },
      {
        name: "description",
        content: "Talk to Myra, your local AI coding agent, and watch every step she takes.",
      },
      { property: "og:title", content: "Chat — Myra" },
      {
        property: "og:description",
        content: "Talk to Myra, your local AI coding agent, and watch every step she takes.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: ChatPage,
});

function clock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Which lucide icon a step gets is driven by this, not by success/failure. */
const TOOL_KIND: Record<string, ActivityStep["kind"]> = {
  list_files: "read",
  read_file: "read",
  search_code: "read",
  read_image: "read",
  recall: "memory",
  get_skill: "read",
  write_file: "edit",
  edit_file: "edit",
  delete_path: "edit",
  move_path: "edit",
  zip_paths: "edit",
  unzip_archive: "edit",
  remember: "memory",
  run_command: "run",
  run_tests: "run",
  http_fetch: "network",
  web_search: "network",
  browser: "network",
  // Deprecated aliases — still mapped so history from before the browser
  // tool consolidation still renders with the right icon.
  browse_page: "network",
  screenshot_page: "network",
  screenshot_file: "network",
};

function kindForTool(tool: string): ActivityStep["kind"] {
  return TOOL_KIND[tool] ?? "run";
}

/** Turns raw agent SSE frames into an ordered activity timeline. */
function reduceSteps(previous: ActivityStep[], event: AgentEvent): ActivityStep[] {
  const steps = [...previous];
  // Server keep-alive heartbeat — not a real step. Silently ignore so it
  // never renders as a bogus "Working…" row in the activity timeline.
  if (event.type === "ping") return steps;
  const label = (event["label"] as string) || (event["tool"] as string) || "Working";

  if (event.type === "thought") {
    const text = String(event["text"] ?? "").trim();
    if (!text) return steps;
    return [
      ...steps,
      {
        id: `t${steps.length}`,
        label: text.length > 80 ? `${text.slice(0, 80)}…` : text,
        kind: "think",
        status: "done",
        body: text.length > 80 ? text : undefined,
      },
    ];
  }

  if (event.type === "tool_start") {
    const tool = String(event["tool"] ?? "");
    const args = (event["arguments"] ?? {}) as Record<string, unknown>;
    const hint =
      (args["path"] as string) ||
      (args["command"] as string) ||
      (args["query"] as string) ||
      (args["url"] as string) ||
      undefined;
    return [
      ...steps,
      {
        id: `s${steps.length}`,
        label,
        kind: kindForTool(tool),
        ...(hint ? { detail: String(hint).slice(0, 120) } : {}),
        ...(Object.keys(args).length > 0 ? { args } : {}),
        status: "running",
      },
    ];
  }

  if (event.type === "tool_end" || event.type === "tool_error") {
    const rawStatus = event["status"] as string | undefined;
    const KNOWN_STATUSES = new Set(["blocked", "needs_approval", "unsafe"]);
    const status =
      event.type === "tool_error"
        ? "error"
        : rawStatus === "ok"
          ? "done"
          : rawStatus && KNOWN_STATUSES.has(rawStatus)
            ? rawStatus
            : "error";
    const body = String(event["result"] ?? event["message"] ?? "").trim() || undefined;
    for (let i = steps.length - 1; i >= 0; i -= 1) {
      const step = steps[i];
      if (step && step.status === "running") {
        steps[i] = { ...step, status: status as ActivityStep["status"], ...(body ? { body } : {}) };
        return steps;
      }
    }
    // tool_error for an unknown tool never got a tool_start row — add one.
    if (event.type === "tool_error") {
      return [
        ...steps,
        {
          id: `s${steps.length}`,
          label,
          kind: "run",
          status: "error",
          ...(body ? { body } : {}),
        },
      ];
    }
    return steps;
  }

  if (event.type === "final") {
    // A plain answer with no thoughts/tools needs no timeline at all.
    if (steps.length === 0) return steps;
    return [...steps, { id: `done${steps.length}`, label: "Done", kind: "done", status: "done" }];
  }

  return steps;
}

function ChatPage() {
  const navigate = useNavigate();
  const { token, user, ready, signOut } = useAuth();

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stepsByMessage, setStepsByMessage] = useState<Record<string, ActivityStep[]>>({});
  const [liveSteps, setLiveSteps] = useState<ActivityStep[]>([]);
  const [streaming, setStreaming] = useState("");
  const [sending, setSending] = useState(false);
  const [draft, setDraft] = useState("");
  const [drawer, setDrawer] = useState(false);
  const [menu, setMenu] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [showApiKeys, setShowApiKeys] = useState(false);
  const [showPreview, setShowPreview] = useState(false);
  const [previewPath, setPreviewPath] = useState<string>("");
  const [previewRunning, setPreviewRunning] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string>("");
  const [providerConfigs, setProviderConfigs] = useState<Record<string, { apiKey?: string | null; baseUrl?: string | null; model?: string | null; hasKey: boolean }>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = useState<{
    tool: string;
    message: string;
    content: string;
  } | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [stopping, setStopping] = useState(false);
  const [attachedPath, setAttachedPath] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  // A send that failed before any events came back (e.g. offline, tunnel
  // down). Instead of dumping the text back into the input box — which
  // silently discards it the moment the user types anything else, and gives
  // no indication a retry is even possible — the failed message is held
  // here and offered back as an explicit "Retry" action.
  const [pendingRetry, setPendingRetry] = useState<{ content: string } | null>(null);
  const [autoRetrying, setAutoRetrying] = useState(false);
  const autoRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear a pending auto-retry timer on unmount so it can't fire send()
  // against an unmounted chat view.
  useEffect(() => {
    return () => {
      if (autoRetryRef.current) clearTimeout(autoRetryRef.current);
    };
  }, []);

  // --- guard ------------------------------------------------------------
  useEffect(() => {
    if (ready && !token) navigate({ to: "/login" });
  }, [ready, token, navigate]);

  // --- sessions ---------------------------------------------------------
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
    return () => {
      cancelled = true;
    };
  }, [token]);

  // --- providers + settings --------------------------------------------
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    fetchProviders()
      .then((data) => !cancelled && setProviders(data.providers))
      .catch(() => undefined);
    fetchAgentSettings(token)
      .then((data) => !cancelled && setSettings(data))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [token]);

  // --- local model readiness -------------------------------------------
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = () => {
      fetchModelStatus()
        .then((status) => {
          if (cancelled) return;
          setModel(status);
          if (status.status && status.status !== "ready" && status.status !== "error")
            timer = setTimeout(poll, 4000);
        })
        .catch(() => {
          if (!cancelled) timer = setTimeout(poll, 8000);
        });
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // --- active conversation + its recorded agent steps -------------------
  useEffect(() => {
    // A pending retry (or an in-flight auto-retry timer) belongs to the
    // session it failed in — don't carry it across when the user switches
    // conversations, or a stale "Retry" pill could resend into the wrong
    // session.
    if (autoRetryRef.current) {
      clearTimeout(autoRetryRef.current);
      autoRetryRef.current = null;
    }
    setAutoRetrying(false);
    setPendingRetry(null);
    if (!token || !activeId) {
      setMessages([]);
      setStepsByMessage({});
      return;
    }
    let cancelled = false;
    fetchSession(token, activeId)
      .then((session) => !cancelled && setMessages(session.messages))
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Could not load this session."),
      );
    fetchSessionEvents(token, activeId)
      .then(({ events }) => {
        if (cancelled) return;
        const grouped: Record<string, ActivityStep[]> = {};
        for (const event of events) {
          const id = event.messageId;
          if (!id) continue;
          grouped[id] = reduceSteps(grouped[id] ?? [], event);
        }
        setStepsByMessage(grouped);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [token, activeId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streaming, liveSteps]);

  // Keep the composer focused during normal use.
  useEffect(() => {
    if (!sending) inputRef.current?.focus();
  }, [sending, activeId]);

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
      setLiveSteps([]);
      setDrawer(false);
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

  const switchProvider = async (provider: string) => {
    if (!token) return;
    try {
      setSettings(await updateAgentSettings(token, { provider }));
      setNotice(`Provider switched to ${providers.find((p) => p.id === provider)?.name ?? provider}.`);
      setMenu(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not switch provider.");
    }
  };

  const openApiKeys = async () => {
    setShowApiKeys(true);
    if (!token) return;
    try {
      const { providers: cfg } = await fetchProviderConfigs(token);
      setProviderConfigs(cfg);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load provider settings.");
    }
  };

  const saveProviderConfig = async (provider: string) => {
    if (!token) return;
    const cfg = providerConfigs[provider] ?? {};
    try {
      await saveProviderConfigApi(token, provider, {
        apiKey: cfg.apiKey ?? undefined,
        baseUrl: cfg.baseUrl ?? undefined,
        model: cfg.model ?? undefined,
      });
      setNotice(`Saved settings for ${providers.find((p) => p.id === provider)?.name ?? provider}.`);
      setProviderConfigs((prev) => ({
        ...prev,
        [provider]: { ...(prev[provider] ?? {}), hasKey: Boolean(cfg.apiKey) },
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save provider settings.");
    }
  };

  const openPreview = async () => {
    setShowPreview((v) => !v);
    if (!token) return;
    try {
      const h = await previewHealth(token, previewPath);
      setPreviewRunning(Boolean(h.running));
      if (h.running && previewPath) setPreviewUrl(previewIframeUrl(previewPath));
    } catch {
      /* ignore */
    }
  };

  const launchPreview = async () => {
    if (!token) return;
    const p = previewPath.trim();
    if (!p) {
      setError("Enter a workspace path to preview (e.g. site or landing).");
      return;
    }
    try {
      await startPreview(token, p);
      setPreviewRunning(true);
      setPreviewUrl(previewIframeUrl(p));
      setNotice(`Preview started for ${p}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start preview.");
    }
  };

  const closePreview = async () => {
    if (!token || !previewPath) return;
    try {
      await stopPreview(token, previewPath.trim());
    } catch {
      /* ignore */
    }
    setPreviewRunning(false);
    setPreviewUrl("");
    setShowPreview(false);
  };

  const toggleAgentMode = async () => {
    if (!token || !settings) return;
    try {
      setSettings(await updateAgentSettings(token, { agentMode: !settings.agentMode }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update agent mode.");
    }
  };

  const toggleApprovals = async () => {
    if (!token || !settings) return;
    try {
      setSettings(await updateAgentSettings(token, { approvalRequired: !settings.approvalRequired }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update approvals.");
    }
  };

  // Whether the run got far enough that the backend actually started work
  // (so it's still going in the background and reconnect/poll is the right
  // move) versus never reaching the server at all (so a manual retry is the
  // right move — there's nothing running to reconnect to).
  const gotAnyEvent = (steps: ActivityStep[], text: string) => steps.length > 0 || text.length > 0;

  // Auto-retries a failed send once, 15s after the failure, before falling
  // back to asking the user to tap Retry. Mirrors the "prompt-level" timer:
  // a short, bounded, automatic reconnect attempt for the single message,
  // distinct from the "session-level" reconnect pollForCompletion() does
  // (which runs indefinitely once a run is actually in flight server-side).
  const scheduleAutoRetry = useCallback(
    (sessionId: string, content: string, options?: { approved?: boolean }) => {
      if (autoRetryRef.current) clearTimeout(autoRetryRef.current);
      setAutoRetrying(true);
      autoRetryRef.current = setTimeout(() => {
        setAutoRetrying(false);
        autoRetryRef.current = null;
        // Only auto-retry if the user hasn't already retried/edited/sent
        // something else in the meantime.
        setPendingRetry((current) => {
          if (current?.content !== content) return current;
          void send(undefined, {
            approved: options?.approved,
            overrideContent: content,
            isRetry: true,
            targetSessionId: sessionId,
          });
          return current;
        });
      }, 15_000);
    },
    [], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const retryNow = useCallback(() => {
    if (!pendingRetry || !activeId) return;
    if (autoRetryRef.current) {
      clearTimeout(autoRetryRef.current);
      autoRetryRef.current = null;
    }
    setAutoRetrying(false);
    void send(undefined, {
      overrideContent: pendingRetry.content,
      isRetry: true,
      targetSessionId: activeId,
    });
  }, [pendingRetry, activeId]); // eslint-disable-line react-hooks/exhaustive-deps

  const dismissRetry = useCallback(() => {
    if (autoRetryRef.current) {
      clearTimeout(autoRetryRef.current);
      autoRetryRef.current = null;
    }
    setAutoRetrying(false);
    setPendingRetry(null);
    setError(null);
  }, []);

  const send = async (
    event?: FormEvent,
    options?: {
      approved?: boolean;
      overrideContent?: string;
      isRetry?: boolean;
      targetSessionId?: string;
    },
  ) => {
    event?.preventDefault();
    let content = (options?.overrideContent ?? draft).trim();
    if (attachedPath) {
      content = `${content}\n\n[Uploaded file: ${attachedPath}]`;
    }
    if (!content || !token || sending) return;

    setError(null);
    setNotice(null);
    if (options?.isRetry) {
      setPendingRetry(null);
    } else {
      setDraft("");
    }
    setSending(true);
    setStopping(false);
    setStreaming("");
    setLiveSteps([]);
    setAttachedPath(null);
    setPendingApproval(null);

    const controller = new AbortController();
    abortRef.current = controller;
    let capturedSteps: ActivityStep[] = [];
    let capturedText = "";

    let sessionId = options?.targetSessionId ?? activeId;
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

      if (!options?.approved && !options?.isRetry) {
        setMessages((prev) => [
          ...prev,
          {
            id: `local-${Date.now()}`,
            role: "user",
            content,
            createdAt: new Date().toISOString(),
          },
        ]);
      }

      if (settings?.agentMode !== false) {
        let steps: ActivityStep[] = [];
        let sawApprovalPrompt = false;
        await runAgent(
          token,
          sessionId,
          content,
          {
            ...(settings?.provider ? { provider: settings.provider } : {}),
            // Only true on the deliberate resend after the user taps
            // Approve (see approveAndContinue below) — sending true on every
            // ordinary message would make MYRA_APPROVAL_REQUIRED a no-op,
            // since the backend would always see a pre-approved request.
            approved: options?.approved ?? false,
            signal: controller.signal,
          },
          (frame) => {
            if (frame.type === "error") {
              setError(String(frame["message"] ?? "Agent run failed."));
              return;
            }
            if (frame.type === "needs_approval") {
              sawApprovalPrompt = true;
              setPendingApproval({
                tool: String(frame["tool"] ?? ""),
                message: String(frame["message"] ?? "This step needs your approval."),
                content,
              });
            }
            if (frame.type === "final") {
              capturedText = String(frame["text"] ?? "");
              setStreaming(capturedText);
            }
            if (frame.type === "done") {
              const message = frame["message"] as ChatMessage | undefined;
              if (message) {
                setMessages((prev) => [...prev, message]);
                setStepsByMessage((prev) => ({ ...prev, [message.id]: steps }));
              }
              if (!sawApprovalPrompt) setPendingApproval(null);
              setStreaming("");
              setLiveSteps([]);
              return;
            }
            steps = reduceSteps(steps, frame);
            capturedSteps = steps;
            setLiveSteps(steps);
          },
        );
      } else {
        await streamMessage(token, sessionId, content, {
          onSession: ({ id, title }) =>
            setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s))),
          onStatus: ({ message }) =>
            setLiveSteps([{ id: "warmup", label: message, kind: "think", status: "running" }]),
          onToken: (piece) => {
            setLiveSteps([]);
            capturedText += piece;
            setStreaming((prev) => prev + piece);
          },
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
        }, controller.signal);
      }

      const rows = await fetchSessions(token);
      setSessions(rows);
    } catch (err) {
      if (controller.signal.aborted) {
        setNotice("Stopped.");
        // Restore the text to the input on a deliberate stop so nothing is
        // lost. Only for genuinely user-authored text: a fresh message, or
        // a stopped retry (which also carries overrideContent and used to
        // be excluded here by mistake, silently dropping the text). An
        // approval-continue resend isn't new user-authored text — it's the
        // original message replaying — so it's still excluded.
        if (!options?.overrideContent || options?.isRetry) setDraft(content);
        // Keep whatever Myra had done so far visible as a finished (not
        // "running") record instead of wiping it — stopping shouldn't hide
        // the steps that already happened.
        if (capturedSteps.length > 0) {
          const frozen = capturedSteps.map((step) =>
            step.status === "running"
              ? { ...step, status: "blocked" as const, detail: step.detail ?? "Stopped" }
              : step,
          );
          const stoppedId = `stopped-${Date.now()}`;
          setMessages((prev) => [
            ...prev,
            {
              id: stoppedId,
              role: "assistant",
              content: capturedText.trim() || "_Stopped before finishing._",
              createdAt: new Date().toISOString(),
            },
          ]);
          setStepsByMessage((prev) => ({ ...prev, [stoppedId]: frozen }));
        }
      } else if (gotAnyEvent(capturedSteps, capturedText)) {
        // The run actually started server-side (we saw at least one event)
        // before the connection dropped — the backend keeps it going, so
        // there's nothing to "retry": resync and keep polling until it
        // finishes, same as before.
        setError(err instanceof Error ? err.message : "Connection lost. Reconnecting…");
        void resyncSession().then(() => pollForCompletion(controller));
      } else {
        // The send never even reached the backend (e.g. offline before the
        // first byte came back). Nothing is running server-side, so silently
        // resyncing/polling would just spin forever. Hold the message and
        // offer an explicit retry instead of quietly dropping it into the
        // input box, where it's easy to lose the moment the user types
        // anything else. Applies whether this was the original send or a
        // previous retry that failed again — either way there's still
        // nothing running server-side to reconnect to.
        if (!options?.approved) {
          setPendingRetry({ content });
          // Only auto-retry once: if this attempt was itself an auto/manual
          // retry and it failed again, stop looping silently and leave it
          // on the explicit Retry button so a bad connection can't retry
          // forever unattended.
          if (!options?.isRetry && sessionId) scheduleAutoRetry(sessionId, content, options);
        }
        setError(err instanceof Error ? err.message : "Couldn't reach Myra.");
      }
    } finally {
      setStreaming("");
      setLiveSteps([]);
      setSending(false);
      setStopping(false);
      abortRef.current = null;
    }
  };

  const stop = useCallback(() => {
    if (!abortRef.current) return;
    setStopping(true);
    // Tell the backend this is a deliberate stop *before* aborting the
    // fetch — once the fetch is aborted the server only sees a dropped
    // connection, which on its own no longer halts the run (so a flaky
    // connection can't strand a task mid-way). This call is what actually
    // makes the Stop button stop it. Fire-and-forget: stopAgent() swallows
    // its own errors, and we abort immediately after regardless so the UI
    // doesn't wait on it.
    if (token && activeId) {
      void stopAgent(token, activeId);
    }
    abortRef.current.abort();
  }, [token, activeId]);

  // Re-sends the prompt that triggered a `needs_approval` pause, this time
  // with approved: true, so the same tool call that stopped the run gets
  // past the guardrail on retry instead of hitting the same wall again. No
  // new user bubble is added (see send()'s options.approved check) since
  // this isn't a new message from the user's point of view — it's
  // continuing the one that paused.
  const approveAndContinue = useCallback(() => {
    if (!pendingApproval) return;
    void send(undefined, { approved: true, overrideContent: pendingApproval.content });
  }, [pendingApproval]); // eslint-disable-line react-hooks/exhaustive-deps

  const dismissApproval = useCallback(() => setPendingApproval(null), []);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  const upload = () => fileRef.current?.click();

  const onFile = (files: FileList | null) => {
    const file = files?.[0];
    if (!file || !token) return;
    setUploading(true);
    setNotice(`Uploading ${file.name}…`);
    uploadWorkspaceFile(token, file)
      .then(({ path }) => {
        setAttachedPath(path);
        setNotice(`Attached ${file.name} → ${path}. Myra can open it in the workspace.`);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Upload failed."),
      )
      .finally(() => {
        setUploading(false);
        if (fileRef.current) fileRef.current.value = "";
      });
  };

  const activeProvider = useMemo(
    () => providers.find((p) => p.id === settings?.provider) ?? null,
    [providers, settings],
  );

  // Re-fetch the active session + its recorded steps. Used to resync the UI
  // after the connection drops: the backend keeps the agent working while
  // we're offline, so on reconnect we pull back whatever it finished.
  const resyncSession = useCallback(async () => {
    if (!token || !activeId) return;
    try {
      const session = await fetchSession(token, activeId);
      setMessages(session.messages);
      const { events } = await fetchSessionEvents(token, activeId);
      const grouped: Record<string, ActivityStep[]> = {};
      // Events persisted live during an in-flight run have no messageId yet.
      // Show them under a "live" group so a user who drops and reconnects
      // still sees the steps myra is currently working on (not just history).
      let live: ActivityStep[] = [];
      for (const event of events) {
        const id = event.messageId;
        if (!id) {
          live = reduceSteps(live, event);
          continue;
        }
        grouped[id] = reduceSteps(grouped[id] ?? [], event);
      }
      setStepsByMessage(grouped);
      setLiveSteps(live);
    } catch {
      /* ignore — keep whatever we have */
    }
  }, [token, activeId]);

  // After a mid-run disconnect the backend keeps working. Poll /events until
  // the run finishes so the UI stays live (steps keep updating in real time)
  // instead of freezing on the last frame we saw before the drop.
  //
  // This is the "session-level" reconnect: unlike the single failed-send
  // retry above (one 15s auto-attempt, then a manual Retry button), a run
  // that's already in flight server-side has no "give up" state that makes
  // sense — the agent is working whether or not this tab can currently
  // reach it. So a transient poll failure (tunnel bounce, brief network
  // drop) backs off and keeps trying rather than abandoning the run's live
  // view after a single bad request; only an explicit stop or the run
  // actually finishing ends the loop.
  const pollForCompletion = useCallback(async (runController: AbortController) => {
    if (!token || !activeId) return;
    let consecutiveFailures = 0;
    for (;;) {
      // Bail if the user started a new run or left the session. Checks the
      // controller captured when THIS poll started, not the shared
      // abortRef — that ref gets reassigned to a fresh (unaborted)
      // controller the moment any new send() begins, which would otherwise
      // make an old poll loop mistake a brand new run for "still mine" and
      // never stop.
      if (runController.signal.aborted) return;
      try {
        const { events } = await fetchSessionEvents(token, activeId);
        consecutiveFailures = 0;
        setNotice(null);
        let live: ActivityStep[] = [];
        const grouped: Record<string, ActivityStep[]> = {};
        for (const event of events) {
          const id = event.messageId;
          if (!id) {
            live = reduceSteps(live, event);
            continue;
          }
          grouped[id] = reduceSteps(grouped[id] ?? [], event);
        }
        setStepsByMessage(grouped);
        setLiveSteps(live);
        if (events.some((e) => e.type === "done")) {
          setLiveSteps([]);
          return;
        }
      } catch {
        consecutiveFailures += 1;
        // Let the user know reconnection is still happening rather than
        // silently retrying forever with no feedback — but only after a
        // couple of misses, so one blip doesn't flash a message.
        if (consecutiveFailures >= 2) {
          setNotice("Reconnecting to Myra…");
        }
      }
      if (runController.signal.aborted) return;
      // 20s ceiling on the backoff between reconnect attempts: quick at
      // first (3s, matching the healthy-poll cadence), then widening so a
      // sustained outage doesn't hammer the backend, capped so it never
      // waits so long the UI feels stuck.
      const delayMs = consecutiveFailures === 0 ? 3000 : Math.min(20_000, 3000 * 2 ** consecutiveFailures);
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }, [token, activeId]);

  const empty = messages.length === 0 && !streaming && liveSteps.length === 0;

  // ---------------------------------------------------------------- render
  const drawerBody = (
    <div className="flex h-full flex-col bg-sidebar">
      <div className="flex items-center justify-between px-4 py-4">
        <MyraLogo />
        <button
          type="button"
          aria-label="Close sessions"
          onClick={() => setDrawer(false)}
          className="focus-royal rounded-lg p-1.5 text-muted-foreground transition-colors hover:text-foreground md:hidden"
        >
          <X className="size-5" />
        </button>
      </div>

      <div className="px-3">
        <button
          type="button"
          onClick={newSession}
          className="focus-royal flex w-full items-center gap-2.5 rounded-xl bg-surface px-3.5 py-2.5 text-left text-[0.9375rem] text-foreground transition-colors hover:bg-surface-raised"
        >
          <Plus className="size-4 text-royal" />
          New chat
        </button>
      </div>

      <div className="scroll-slim mt-5 flex-1 overflow-y-auto px-2">
        <p className="px-3 pb-2 text-[0.7rem] tracking-[0.14em] text-muted-foreground uppercase">
          Recent
        </p>
        {sessions.length === 0 ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">No chats yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {sessions.map((session) => (
              <li key={session.id} className="group relative flex items-center">
                <button
                  type="button"
                  onClick={() => {
                    setActiveId(session.id);
                    setLiveSteps([]);
                    setDrawer(false);
                  }}
                  className={cn(
                    "focus-royal min-w-0 flex-1 rounded-xl px-3 py-2.5 pr-8 text-left transition-colors",
                    session.id === activeId
                      ? "bg-sidebar-accent text-foreground"
                      : "text-muted-foreground hover:bg-surface hover:text-foreground",
                  )}
                >
                  <span className="block truncate text-[0.9375rem]">{session.title}</span>
                </button>
                <button
                  type="button"
                  aria-label={`Delete ${session.title}`}
                  onClick={() => removeSession(session.id)}
                  className="focus-royal absolute right-1.5 rounded-lg p-1.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:text-destructive focus-visible:opacity-100"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="hairline-t px-4 py-4">
        <p className="flex items-center gap-2 text-[0.8125rem] text-muted-foreground">
          <Cpu className="size-3.5 text-royal" />
          <span className="truncate">
            {activeProvider?.name ?? "Local Llama"}
            {model?.model ? ` · ${model.model}` : ""}
          </span>
        </p>
        <button
          type="button"
          onClick={() => {
            signOut();
            navigate({ to: "/login" });
          }}
          className="focus-royal mt-3 flex w-full items-center gap-3 rounded-xl px-1 py-1.5 text-left text-[0.9375rem] text-foreground transition-colors hover:text-royal-soft"
        >
          <span className="flex size-7 items-center justify-center rounded-full bg-royal/15 text-[0.75rem] font-semibold text-royal-soft">
            {(user?.name ?? "M").slice(0, 1).toUpperCase()}
          </span>
          <span className="min-w-0 flex-1 truncate">{user?.name ?? "Myra user"}</span>
          <LogOut className="size-4 text-muted-foreground" />
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex h-dvh bg-background text-foreground">
      <aside className="hidden w-[16.5rem] shrink-0 md:block">{drawerBody}</aside>

      {drawer && (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="Close sessions"
            onClick={() => setDrawer(false)}
            className="absolute inset-0 bg-black/60"
          />
          <div className="absolute inset-y-0 left-0 w-[17rem]">{drawerBody}</div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        {/* header */}
        <header className="hairline-b relative flex items-center gap-2 px-3 py-3 sm:px-4">
          <button
            type="button"
            aria-label="Open sessions"
            onClick={() => setDrawer(true)}
            className="focus-royal rounded-lg p-1.5 text-foreground/80 transition-colors hover:text-foreground md:hidden"
          >
            <Menu className="size-5" />
          </button>
          <MyraLogo />
          <div className="flex-1" />
          <button
            type="button"
            aria-label="New chat"
            onClick={newSession}
            className="focus-royal rounded-lg p-1.5 text-foreground/80 transition-colors hover:text-royal-soft"
          >
            <SquarePen className="size-[1.15rem]" />
          </button>
          <button
            type="button"
            aria-label="Myra options"
            onClick={() => setMenu((v) => !v)}
            className="focus-royal rounded-lg p-1.5 text-foreground/80 transition-colors hover:text-foreground"
          >
            <MoreVertical className="size-[1.15rem]" />
          </button>

          {menu && (
            <>
              <button
                type="button"
                aria-label="Close menu"
                className="fixed inset-0 z-40 cursor-default"
                onClick={() => setMenu(false)}
              />
              <div className="absolute top-[3.25rem] right-3 z-50 w-[17rem] overflow-hidden rounded-2xl bg-popover p-1.5 shadow-2xl shadow-black/60">
                <p className="px-3 pt-2 pb-1.5 text-[0.7rem] tracking-[0.14em] text-muted-foreground uppercase">
                  Provider
                </p>
                {providers.map((provider) => (
                  <button
                    key={provider.id}
                    type="button"
                    onClick={() => void switchProvider(provider.id)}
                    className="focus-royal flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[0.9375rem] transition-colors hover:bg-surface-raised"
                  >
                    <Cpu
                      className={cn(
                        "size-4",
                        provider.id === settings?.provider ? "text-royal" : "text-muted-foreground",
                      )}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-foreground">{provider.name}</span>
                      <span className="block truncate text-[0.75rem] text-muted-foreground">
                        {provider.model ?? provider.kind}
                        {provider.available ? "" : " · unavailable"}
                      </span>
                    </span>
                    {provider.id === settings?.provider && (
                      <CheckCheck className="size-4 text-royal" />
                    )}
                  </button>
                ))}

                <div className="my-1.5 h-px bg-hairline" />

                <button
                  type="button"
                  onClick={toggleAgentMode}
                  className="focus-royal flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[0.9375rem] transition-colors hover:bg-surface-raised"
                >
                  <Wand2 className="size-4 text-royal" />
                  <span className="flex-1">Agent mode</span>
                  <span className="text-[0.8125rem] text-muted-foreground">
                    {settings?.agentMode === false ? "Off" : "On"}
                  </span>
                </button>

                <button
                  type="button"
                  onClick={() => void openPreview()}
                  className="focus-royal flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[0.9375rem] transition-colors hover:bg-surface-raised"
                >
                  <FileText className="size-4 text-royal" />
                  <span className="flex-1">Preview</span>
                  <span className="text-[0.8125rem] text-muted-foreground">
                    {previewRunning ? "Live" : ""}
                  </span>
                </button>

                <button
                  type="button"
                  onClick={() => void openApiKeys()}
                  className="focus-royal flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[0.9375rem] transition-colors hover:bg-surface-raised"
                >
                  <Cpu className="size-4 text-royal" />
                  <span className="flex-1">API Keys & models</span>
                </button>
                <button
                  type="button"
                  onClick={toggleApprovals}
                  className="focus-royal flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[0.9375rem] transition-colors hover:bg-surface-raised"
                >
                  <ShieldCheck className="size-4 text-royal" />
                  <span className="flex-1">Ask before risky tools</span>
                  <span className="text-[0.8125rem] text-muted-foreground">
                    {settings?.approvalRequired ? "On" : "Off"}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMenu(false);
                    setDrawer(true);
                  }}
                  className="focus-royal flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[0.9375rem] transition-colors hover:bg-surface-raised md:hidden"
                >
                  <Menu className="size-4 text-muted-foreground" />
                  <span className="flex-1">All chats</span>
                  <ChevronRight className="size-4 text-muted-foreground" />
                </button>
              </div>
            </>
          )}
        </header>

        {/* transcript */}
        <div ref={scrollRef} className="scroll-slim flex-1 overflow-y-auto overflow-x-hidden">
          <div className="mx-auto w-full max-w-3xl px-4 pt-6 pb-4 sm:px-6">
            {empty ? (
              <div className="flex flex-col items-center pt-[16vh] text-center">
                <MyraMark className="size-9 text-royal" />
                <h1 className="mt-4 text-[2.25rem] leading-tight font-semibold tracking-tight">
                  Myra
                </h1>
                <p className="mt-1.5 text-[0.9375rem] text-muted-foreground">
                  Your AI coding agent
                </p>
              </div>
            ) : (
              <ul className="space-y-5">
                {messages.map((message) =>
                  message.role === "user" ? (
                    <li key={message.id} className="flex min-w-0 justify-end">
                      <div className="max-w-[85%] min-w-0 rounded-2xl bg-bubble-user px-4 py-3 sm:max-w-[75%]">
                        <p className="text-[0.9375rem] leading-[1.6] break-words whitespace-pre-wrap text-bubble-user-foreground">
                          {message.content}
                        </p>
                        <p className="mt-1 flex items-center justify-end gap-1.5 text-[0.7rem] text-bubble-user-foreground/60">
                          {clock(message.createdAt)}
                          <CheckCheck className="size-3.5 text-royal-soft" />
                        </p>
                      </div>
                    </li>
                  ) : (
                    <li key={message.id} className="flex items-start gap-3">
                      <MyraAvatar />
                      <div className="min-w-0 flex-1 space-y-2.5">
                        {stepsByMessage[message.id]?.length ? (
                          <ActivityTimeline
                            steps={stepsByMessage[message.id] ?? []}
                            running={false}
                          />
                        ) : null}
                        <div className="max-w-full min-w-0 rounded-2xl bg-bubble-agent px-4 py-3">
                          <Markdown
                            content={message.content}
                            className="text-bubble-agent-foreground"
                            token={token}
                          />
                          <p className="mt-1.5 text-[0.7rem] text-muted-foreground">
                            {clock(message.createdAt)}
                          </p>
                        </div>
                      </div>
                    </li>
                  ),
                )}

                {(liveSteps.length > 0 || streaming || sending) && (
                  <li className="flex items-start gap-3">
                    <MyraAvatar />
                    <div className="min-w-0 flex-1 space-y-2.5">
                      {liveSteps.length > 0 && (
                        <ActivityTimeline steps={liveSteps} running={sending && !stopping} />
                      )}
                      {streaming ? (
                        <div className="rounded-2xl bg-bubble-agent px-4 py-3">
                          <Markdown
                            content={streaming}
                            className="text-bubble-agent-foreground"
                            token={token}
                          />
                        </div>
                      ) : (
                        liveSteps.length === 0 &&
                        sending && (
                          <p className="px-1 py-1.5 text-[0.9375rem] text-shimmer">
                            {stopping ? "Stopping…" : "Thinking…"}
                          </p>
                        )
                      )}
                    </div>
                  </li>
                )}
              </ul>
            )}

            {pendingRetry ? (
              <div
                role="alert"
                className="mt-5 flex items-center justify-between gap-3 rounded-xl bg-surface px-4 py-2.5 text-[0.875rem]"
              >
                <span className="text-destructive">
                  {error ?? "Couldn't reach Myra."}
                  {autoRetrying && (
                    <span className="text-muted-foreground"> — retrying automatically…</span>
                  )}
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <button
                    type="button"
                    onClick={retryNow}
                    className="flex items-center gap-1.5 rounded-lg bg-foreground/10 px-2.5 py-1.5 text-foreground transition hover:bg-foreground/15"
                  >
                    <RotateCw className={cn("size-3.5", autoRetrying && "animate-spin")} />
                    Retry
                  </button>
                  <button
                    type="button"
                    onClick={dismissRetry}
                    aria-label="Dismiss"
                    className="rounded-lg p-1.5 text-muted-foreground transition hover:bg-foreground/10 hover:text-foreground"
                  >
                    <X className="size-3.5" />
                  </button>
                </span>
              </div>
            ) : (
              (error || notice) && (
                <p
                  role={error ? "alert" : "status"}
                  className={cn(
                    "mt-5 rounded-xl bg-surface px-4 py-2.5 text-[0.875rem]",
                    error ? "text-destructive" : "text-muted-foreground",
                  )}
                >
                  {error ?? notice}
                </p>
              )
            )}

            {pendingApproval && !sending && (
              <div
                role="alert"
                className="mt-5 space-y-2.5 rounded-xl bg-amber-500/10 px-4 py-3 ring-1 ring-amber-500/30"
              >
                <p className="text-[0.875rem] text-foreground/90">
                  <span className="font-medium text-amber-500">Needs approval:</span>{" "}
                  {pendingApproval.message}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={approveAndContinue}
                    className="focus-royal rounded-lg bg-amber-500 px-3 py-1.5 text-[0.8125rem] font-medium text-white"
                  >
                    Approve &amp; continue
                  </button>
                  <button
                    type="button"
                    onClick={dismissApproval}
                    className="focus-royal rounded-lg px-3 py-1.5 text-[0.8125rem] text-muted-foreground ring-1 ring-hairline"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* composer */}
        <div className="px-3 pt-1 pb-4 sm:px-6 sm:pb-6">
          {model?.status && model.status !== "ready" && (
            <p className="mx-auto mb-2.5 w-full max-w-3xl px-1 text-[0.75rem] text-muted-foreground">
              {model.status === "error"
                ? (model.detail ?? "The local model could not be loaded.")
                : (model.detail ?? "Preparing the local model — the first reply may take a moment.")}
            </p>
          )}
          {(attachedPath || uploading) && (
            <div className="mx-auto mb-2 flex w-full max-w-3xl items-center gap-2 px-1">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-royal/15 px-3 py-1 text-[0.75rem] text-royal">
                {uploading ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <FileText className="size-3" />
                )}
                {uploading ? "Uploading…" : attachedPath}
              </span>
              {attachedPath && !uploading && (
                <button
                  type="button"
                  onClick={() => setAttachedPath(null)}
                  className="text-[0.75rem] text-muted-foreground hover:text-foreground"
                >
                  ✕
                </button>
              )}
            </div>
          )}
          <form onSubmit={send} className="mx-auto flex w-full max-w-3xl items-end gap-2.5">
            <input
              ref={fileRef}
              type="file"
              className="hidden"
              onChange={(e) => onFile(e.target.files)}
            />
            <button
              type="button"
              onClick={upload}
              aria-label="Attach a file"
              className="focus-royal flex size-[3.25rem] shrink-0 items-center justify-center rounded-2xl bg-surface text-muted-foreground transition-colors hover:text-foreground"
            >
              <Paperclip className="size-5" />
            </button>

            <div className="flex min-w-0 flex-1 items-end gap-2 rounded-2xl bg-surface px-4 py-2.5">
              <label htmlFor="chat-input" className="sr-only">
                Message Myra
              </label>
              <textarea
                id="chat-input"
                ref={inputRef}
                rows={1}
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  const el = e.target;
                  el.style.height = "auto";
                  el.style.height = `${Math.min(el.scrollHeight, 176)}px`;
                }}
                onKeyDown={onKeyDown}
                placeholder="Message Myra…"
                className="scroll-slim max-h-44 min-w-0 flex-1 resize-none bg-transparent py-1.5 text-[0.9375rem] leading-[1.5] outline-none placeholder:text-muted-foreground"
              />
              {sending ? (
                <button
                  type="button"
                  onClick={stop}
                  disabled={stopping}
                  aria-label="Stop Myra"
                  className="focus-royal flex size-8 shrink-0 items-center justify-center rounded-xl bg-royal text-primary-foreground transition-opacity hover:opacity-80 disabled:opacity-50"
                >
                  <Square className="size-3.5" fill="currentColor" />
                </button>
              ) : (
                <button
                  type="submit"
                  aria-label="Send message"
                  disabled={!draft.trim()}
                  className="focus-royal flex size-8 shrink-0 items-center justify-center rounded-xl text-royal transition-opacity hover:opacity-80 disabled:opacity-35"
                >
                  <SendHorizontal className="size-[1.35rem]" />
                </button>
              )}
            </div>
          </form>
        </div>

        {/* API keys & model settings modal */}
        {showApiKeys && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
            <button
              type="button"
              aria-label="Close"
              className="fixed inset-0 cursor-default bg-black/70 backdrop-blur-sm"
              onClick={() => setShowApiKeys(false)}
            />
            <div className="relative z-10 w-full max-w-lg max-h-[85vh] overflow-y-auto scroll-slim rounded-2xl bg-popover p-5 shadow-2xl shadow-black/60">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-lg font-semibold tracking-tight text-foreground">
                  API Keys & models
                </h2>
                <button
                  type="button"
                  onClick={() => setShowApiKeys(false)}
                  className="rounded-lg p-1.5 text-muted-foreground hover:text-foreground"
                >
                  <X className="size-5" />
                </button>
              </div>

              <p className="mb-4 text-[0.8125rem] text-muted-foreground">
                Set your own API key, base URL and model per provider. Keys are
                stored on your server, never shown in full.
              </p>

              <div className="space-y-4">
                {providers
                  .filter((p) => p.kind !== "mock")
                  .map((provider) => {
                    const cfg = providerConfigs[provider.id] ?? {};
                    return (
                      <div
                        key={provider.id}
                        className="rounded-xl border border-hairline bg-surface-raised/40 p-3.5"
                      >
                        <div className="mb-2 flex items-center justify-between">
                          <span className="font-medium text-foreground">{provider.name}</span>
                          <span className="text-[0.7rem] text-muted-foreground">
                            {cfg.hasKey ? "✓ key set" : "no key"}
                          </span>
                        </div>
                        <div className="space-y-2">
                          <input
                            type="password"
                            placeholder={cfg.hasKey ? "Key set — leave blank to keep" : "API key"}
                            value={cfg.apiKey ?? ""}
                            onChange={(e) =>
                              setProviderConfigs((prev) => ({
                                ...prev,
                                [provider.id]: { ...(prev[provider.id] ?? {}), apiKey: e.target.value },
                              }))
                            }
                            className="w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-[0.8125rem] text-foreground outline-none focus:border-royal/50"
                          />
                          <input
                            type="text"
                            placeholder="Base URL (optional)"
                            value={cfg.baseUrl ?? ""}
                            onChange={(e) =>
                              setProviderConfigs((prev) => ({
                                ...prev,
                                [provider.id]: { ...(prev[provider.id] ?? {}), baseUrl: e.target.value },
                              }))
                            }
                            className="w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-[0.8125rem] text-foreground outline-none focus:border-royal/50"
                          />
                          <input
                            type="text"
                            placeholder="Model (optional — defaults used if blank)"
                            value={cfg.model ?? ""}
                            onChange={(e) =>
                              setProviderConfigs((prev) => ({
                                ...prev,
                                [provider.id]: { ...(prev[provider.id] ?? {}), model: e.target.value },
                              }))
                            }
                            className="w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-[0.8125rem] text-foreground outline-none focus:border-royal/50"
                          />
                          <button
                            type="button"
                            onClick={() => void saveProviderConfig(provider.id)}
                            className="w-full rounded-lg bg-royal px-3 py-2 text-[0.8125rem] font-medium text-primary-foreground transition-opacity hover:opacity-90"
                          >
                            Save {provider.name}
                          </button>
                        </div>
                      </div>
                    );
                  })}
              </div>
            </div>
          </div>
        )}

        {showPreview && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
            <div className="flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-hairline bg-popover shadow-2xl">
              <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
                <div className="flex items-center gap-2">
                  <FileText className="size-4 text-royal" />
                  <h2 className="text-sm font-semibold tracking-tight text-foreground">Preview</h2>
                  {previewRunning && (
                    <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[0.7rem] font-medium text-emerald-500">
                      Live
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setShowPreview(false)}
                  className="rounded-lg p-1.5 text-muted-foreground hover:text-foreground"
                  aria-label="Close preview"
                >
                  <X className="size-5" />
                </button>
              </div>

              <div className="flex items-center gap-2 border-b border-hairline px-4 py-3">
                <input
                  value={previewPath}
                  onChange={(e) => setPreviewPath(e.target.value)}
                  placeholder="Workspace path to preview (e.g. site, landing)"
                  className="w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-[0.8125rem] text-foreground outline-none focus:border-royal/50"
                />
                <button
                  type="button"
                  onClick={() => void launchPreview()}
                  disabled={!previewPath.trim()}
                  className="shrink-0 rounded-lg bg-royal px-4 py-2 text-[0.8125rem] font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  Start
                </button>
                {previewRunning && (
                  <button
                    type="button"
                    onClick={() => void closePreview()}
                    className="shrink-0 rounded-lg bg-surface px-4 py-2 text-[0.8125rem] font-medium text-muted-foreground hover:text-foreground"
                  >
                    Stop
                  </button>
                )}
              </div>

              <div className="min-h-0 flex-1 bg-white">
                {previewUrl ? (
                  <iframe
                    src={previewUrl}
                    title="Preview"
                    className="h-full w-full border-0"
                    sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                  />
                ) : (
                  <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
                    Start a preview to see the hosted site here.
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
