import { useEffect, useState, type ReactNode } from "react";
import { Check, Copy, Download, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { downloadWorkspaceFile } from "@/api/agent";

/**
 * Dependency-free markdown renderer, tuned for agent replies:
 * fenced code blocks, headings, lists, inline code, bold/italic and links.
 * Deliberately small — Myra ships offline, so no remote parser bundle.
 */

type Block =
  | { kind: "code"; lang: string; code: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "quote"; text: string }
  | { kind: "para"; text: string };

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index] ?? "";

    const fence = /^```(\S*)\s*$/.exec(line);
    if (fence) {
      const lang = fence[1] ?? "";
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index] ?? "")) {
        body.push(lines[index] ?? "");
        index += 1;
      }
      index += 1;
      blocks.push({ kind: "code", lang, code: body.join("\n") });
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: (heading[1] ?? "#").length,
        text: heading[2] ?? "",
      });
      index += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const body: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index] ?? "")) {
        body.push((lines[index] ?? "").replace(/^>\s?/, ""));
        index += 1;
      }
      blocks.push({ kind: "quote", text: body.join(" ") });
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || ordered) {
      const isOrdered = Boolean(ordered);
      const items: string[] = [];
      while (index < lines.length) {
        const current = lines[index] ?? "";
        const match = isOrdered
          ? /^\s*\d+[.)]\s+(.*)$/.exec(current)
          : /^\s*[-*+]\s+(.*)$/.exec(current);
        if (!match) break;
        items.push(match[1] ?? "");
        index += 1;
      }
      blocks.push({ kind: "list", ordered: isOrdered, items });
      continue;
    }

    if (line.trim() === "") {
      index += 1;
      continue;
    }

    const body: string[] = [];
    while (index < lines.length) {
      const current = lines[index] ?? "";
      if (
        current.trim() === "" ||
        /^```/.test(current) ||
        /^#{1,4}\s/.test(current) ||
        /^\s*[-*+]\s+/.test(current) ||
        /^\s*\d+[.)]\s+/.test(current) ||
        /^>\s?/.test(current)
      )
        break;
      body.push(current);
      index += 1;
    }
    blocks.push({ kind: "para", text: body.join(" ") });
  }

  return blocks;
}

function DownloadLink({ label, path, token }: { label: string; path: string; token?: string | null }) {
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");

  const handleClick = async () => {
    if (!token || state === "loading") return;
    setState("loading");
    try {
      await downloadWorkspaceFile(token, path);
      setState("idle");
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 2000);
    }
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={!token || state === "loading"}
      className="inline-flex items-center gap-1.5 rounded-lg border border-royal/30 bg-surface-raised px-2.5 py-1 font-mono text-[0.85em] text-royal-soft transition-colors hover:bg-royal/10 disabled:cursor-not-allowed disabled:opacity-60"
    >
      {state === "loading" ? (
        <Loader2 className="size-3.5 animate-spin" />
      ) : (
        <Download className="size-3.5" />
      )}
      {state === "error" ? "Download failed — retry" : label}
    </button>
  );
}

/** Renders a workspace image via the authenticated download endpoint. */
function WorkspaceImage({ path, token, alt }: { path: string; token?: string | null; alt?: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    if (!token) return;
    void (async () => {
      try {
        const res = await fetch(
          `${import.meta.env.VITE_API_URL ?? "http://localhost:8000"}/workspace/download?path=${encodeURIComponent(path)}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!res.ok) throw new Error("not ok");
        const blob = await res.blob();
        objectUrl = URL.createObjectURL(blob);
        if (active) setUrl(objectUrl);
      } catch {
        if (active) setFailed(true);
      }
    })();
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, token]);

  if (failed) {
    return (
      <span className="inline-block rounded-lg border border-destructive/30 px-2 py-1 font-mono text-[0.8em] text-muted-foreground">
        Image unavailable
      </span>
    );
  }
  if (!url) {
    return <span className="inline-block h-8 w-24 animate-pulse rounded-lg bg-surface-raised" />;
  }
  return (
    <a href={url} target="_blank" rel="noreferrer" title={alt || path}>
      <img
        src={url}
        alt={alt || path}
        className="my-2 max-h-80 max-w-full rounded-xl border border-hairline object-contain"
      />
    </a>
  );
}

/** Inline: `code`, **bold**, *italic*, [text](href), [file](download:path), ![img](download:path). */
function renderInline(text: string, keyPrefix: string, token?: string | null): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern =
    /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(!\[[^\]]*\]\([^)\s]+\))|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token_ = match[0];
    const id = `${keyPrefix}-i${key++}`;
    if (token_.startsWith("`")) {
      nodes.push(
        <code
          key={id}
          className="rounded-[5px] bg-surface-raised px-1.5 py-0.5 font-mono text-[0.85em] text-royal-soft"
        >
          {token_.slice(1, -1)}
        </code>,
      );
    } else if (token_.startsWith("**")) {
      nodes.push(
        <strong key={id} className="font-semibold text-foreground">
          {token_.slice(2, -2)}
        </strong>,
      );
    } else if (token_.startsWith("![")) {
      const link = /^!\[([^\]]*)\]\(([^)\s]+)\)$/.exec(token_);
      const href = link?.[2] ?? "#";
      if (href.startsWith("download:")) {
        nodes.push(
          <WorkspaceImage
            key={id}
            path={href.slice("download:".length)}
            token={token}
            alt={link?.[1] || undefined}
          />,
        );
      } else {
        nodes.push(
          <img
            key={id}
            src={href}
            alt={link?.[1] || ""}
            className="my-2 max-h-80 max-w-full rounded-xl border border-hairline object-contain"
          />
        );
      }
    } else if (token_.startsWith("[")) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token_);
      const href = link?.[2] ?? "#";
      if (href.startsWith("download:")) {
        nodes.push(
          <DownloadLink key={id} label={link?.[1] ?? "Download"} path={href.slice("download:".length)} token={token} />,
        );
      } else {
        nodes.push(
          <a
            key={id}
            href={href}
            target="_blank"
            rel="noreferrer"
            className="text-royal-soft underline decoration-royal/40 underline-offset-2"
          >
            {link?.[1] ?? token_}
          </a>,
        );
      }
    } else {
      nodes.push(
        <em key={id} className="italic">
          {token_.slice(1, -1)}
        </em>,
      );
    }
    last = match.index + token_.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    void navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    });
  };

  return (
    <div className="group my-3 overflow-hidden rounded-xl bg-surface-raised">
      <div className="flex items-center justify-between px-3.5 pt-2.5 pb-1">
        <span className="font-mono text-[0.7rem] tracking-wide text-muted-foreground">
          {lang || "code"}
        </span>
        <button
          type="button"
          onClick={copy}
          aria-label="Copy code"
          className="focus-royal rounded-md p-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          {copied ? <Check className="size-3.5 text-royal-soft" /> : <Copy className="size-3.5" />}
        </button>
      </div>
      <pre className="scroll-slim overflow-x-auto px-3.5 pb-3.5">
        <code className="font-mono text-[0.8125rem] leading-relaxed text-foreground/90">
          {code}
        </code>
      </pre>
    </div>
  );
}

export function Markdown({
  content,
  className,
  token,
}: {
  content: string;
  className?: string;
  token?: string | null;
}) {
  const blocks = parseBlocks(content);

  return (
    <div className={cn("text-[0.9375rem] leading-[1.65]", className)}>
      {blocks.map((block, i) => {
        const key = `b${i}`;
        if (block.kind === "code") return <CodeBlock key={key} lang={block.lang} code={block.code} />;
        if (block.kind === "heading") {
          const size =
            block.level <= 1
              ? "text-[1.05rem]"
              : block.level === 2
                ? "text-[1rem]"
                : "text-[0.9375rem]";
          return (
            <p key={key} className={cn("mt-4 mb-1.5 font-semibold text-foreground first:mt-0", size)}>
              {renderInline(block.text, key, token)}
            </p>
          );
        }
        if (block.kind === "list")
          return (
            <ul
              key={key}
              className={cn("my-2 space-y-1 pl-5", block.ordered ? "list-decimal" : "list-disc")}
            >
              {block.items.map((item, j) => (
                <li key={`${key}-${j}`} className="marker:text-royal/60">
                  {renderInline(item, `${key}-${j}`, token)}
                </li>
              ))}
            </ul>
          );
        if (block.kind === "quote")
          return (
            <p key={key} className="my-3 pl-3 text-muted-foreground shadow-[inset_2px_0_0_var(--royal)]">
              {renderInline(block.text, key, token)}
            </p>
          );
        return (
          <p key={key} className="my-2 first:mt-0 last:mb-0">
            {renderInline(block.text, key, token)}
          </p>
        );
      })}
    </div>
  );
}
