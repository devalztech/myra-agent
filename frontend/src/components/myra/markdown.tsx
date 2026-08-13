import { useState, type ReactNode } from "react";
import { Check, Copy } from "lucide-react";

import { cn } from "@/lib/utils";

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

/** Inline: `code`, **bold**, *italic*, [text](href). */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    const id = `${keyPrefix}-i${key++}`;
    if (token.startsWith("`")) {
      nodes.push(
        <code
          key={id}
          className="rounded-[5px] bg-surface-raised px-1.5 py-0.5 font-mono text-[0.85em] text-royal-soft"
        >
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("**")) {
      nodes.push(
        <strong key={id} className="font-semibold text-foreground">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("[")) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
      nodes.push(
        <a
          key={id}
          href={link?.[2] ?? "#"}
          target="_blank"
          rel="noreferrer"
          className="text-royal-soft underline decoration-royal/40 underline-offset-2"
        >
          {link?.[1] ?? token}
        </a>,
      );
    } else {
      nodes.push(
        <em key={id} className="italic">
          {token.slice(1, -1)}
        </em>,
      );
    }
    last = match.index + token.length;
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

export function Markdown({ content, className }: { content: string; className?: string }) {
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
              {renderInline(block.text, key)}
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
                  {renderInline(item, `${key}-${j}`)}
                </li>
              ))}
            </ul>
          );
        if (block.kind === "quote")
          return (
            <p key={key} className="my-3 pl-3 text-muted-foreground shadow-[inset_2px_0_0_var(--royal)]">
              {renderInline(block.text, key)}
            </p>
          );
        return (
          <p key={key} className="my-2 first:mt-0 last:mb-0">
            {renderInline(block.text, key)}
          </p>
        );
      })}
    </div>
  );
}
