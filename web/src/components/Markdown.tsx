import { useState } from "react";
import type { ReactElement, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";
// Syntax colours are themed in index.css (.metano-md .hljs-*) for light/dark.

/** Recursively extract plain text from a React node tree (e.g. the already
 *  syntax-highlighted <code> children) so the copy button gets raw code. */
function toText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(toText).join("");
  if (node && typeof node === "object" && "props" in node) {
    const el = node as { props?: { children?: ReactNode } };
    return toText(el.props?.children);
  }
  return "";
}

/**
 * Protocol whitelist for markdown links (XSS hardening, M-10 / 全检7).
 *
 * The custom `a` component below bypasses react-markdown's default
 * `urlTransform` (which strips `javascript:` / `data:` hrefs), so we must
 * sanitize here. Only http/https/mailto survive; any other scheme (javascript:,
 * data:, vbscript:, ...) is dropped and the link renders as inert plain text.
 * Relative URLs resolve against the current origin and pass (same-origin nav).
 */
const SAFE_LINK_PROTOCOLS = ["http:", "https:", "mailto:"];

function sanitizeHref(href: string | undefined): string | null {
  if (!href || /^\s*$/.test(href)) return null;
  try {
    const url = new URL(href, window.location.href);
    return SAFE_LINK_PROTOCOLS.includes(url.protocol) ? href : null;
  } catch {
    return null;
  }
}

function CodeBlock({ code, lang, children }: { code: string; lang: string; children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (non-secure context) — ignore */
    }
  };
  return (
    <div className="metano-md my-3 overflow-hidden rounded-lg border border-border/60 bg-muted">
      <div className="flex items-center justify-between border-b border-border/50 bg-muted/60 px-3 py-1.5">
        <span className="text-[11px] font-mono text-muted-foreground">{lang || "code"}</span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 text-[13px] leading-relaxed">{children}</pre>
    </div>
  );
}

/** Render AI chat markdown with GFM + syntax highlighting + copyable code.
 *  Code blocks use a fixed dark background (like most AI chat UIs) so the
 *  highlight palette stays stable across light/dark themes. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="metano-md break-words text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          pre({ children }) {
            const el = (Array.isArray(children) ? children[0] : children) as
              | ReactElement<{ className?: string; children?: ReactNode }>
              | undefined;
            const className = el?.props?.className ?? "";
            const match = /language-(\w+)/.exec(className);
            const codeText = toText(el?.props?.children ?? children);
            return <CodeBlock code={codeText} lang={match?.[1] ?? ""}>{children}</CodeBlock>;
          },
          code({ children }) {
            // Inline code only — block code is handled by `pre` above.
            return (
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]">{children}</code>
            );
          },
          p({ children }) {
            return <p className="my-2 last:mb-0">{children}</p>;
          },
          h1: ({ children }) => <h1 className="mb-2 mt-4 text-xl font-bold">{children}</h1>,
          h2: ({ children }) => <h2 className="mb-2 mt-4 text-lg font-bold">{children}</h2>,
          h3: ({ children }) => <h3 className="mb-2 mt-3 text-base font-semibold">{children}</h3>,
          h4: ({ children }) => <h4 className="mb-2 mt-3 text-sm font-semibold">{children}</h4>,
          ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
          li: ({ children }) => <li className="leading-relaxed">{children}</li>,
          a({ href, children }) {
            const safe = sanitizeHref(href);
            if (!safe) {
              // Blocked scheme (javascript:/data:/...) — render as plain text so
              // a hostile markdown link can't navigate or execute anything.
              return <span className="text-primary underline underline-offset-2">{children}</span>;
            }
            return (
              <a
                href={safe}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline underline-offset-2"
              >
                {children}
              </a>
            );
          },
          blockquote: ({ children }) => (
            <blockquote className="my-2 border-l-2 border-border pl-3 text-muted-foreground">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-3 border-border" />,
          table: ({ children }) => (
            <div className="my-3 overflow-x-auto rounded-lg border border-border">
              <table className="w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-muted/60">{children}</thead>,
          th: ({ children }) => (
            <th className="border-b border-border px-3 py-1.5 text-left font-semibold">{children}</th>
          ),
          td: ({ children }) => (
            <td className="border-b border-border/60 px-3 py-1.5 align-top">{children}</td>
          ),
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          em: ({ children }) => <em>{children}</em>,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
