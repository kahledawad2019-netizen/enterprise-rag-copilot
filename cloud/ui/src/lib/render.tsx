import type { JSX, ReactNode } from "react";

import type { SourceRef } from "../types";

/**
 * A deliberately small renderer for generated answers.
 *
 * The backend returns plain text with light Markdown and inline citations like
 * `[D1]`, `[S1]` or `[D1, 3.1]`. A full Markdown library would be a larger
 * dependency and a larger attack surface than this needs: the text comes from
 * a model, and `dangerouslySetInnerHTML` on model output is exactly the thing
 * not to do. Everything here produces React elements, so nothing in the
 * answer can inject markup.
 *
 * Supported: paragraphs, `-` and `1.` lists, **bold**, `code`, and citations.
 * Anything else renders as literal text, which is the safe failure.
 */

const CITATION = /\[([DSG]\d+(?:\s*,\s*[^\]]+)?)\]/g;
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

export function renderAnswer(text: string, sources: SourceRef[]): ReactNode {
  const byId = new Map(sources.map((s) => [s.id, s]));
  const blocks: ReactNode[] = [];

  // Blank lines separate blocks. Consecutive list lines form one list.
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(<p key={`p${blocks.length}`}>{inline(paragraph.join(" "), byId)}</p>);
    paragraph = [];
  };

  const flushList = () => {
    if (!list) return;
    const items = list.items.map((item, i) => <li key={i}>{inline(item, byId)}</li>);
    blocks.push(
      list.ordered ? (
        <ol key={`l${blocks.length}`}>{items}</ol>
      ) : (
        <ul key={`l${blocks.length}`}>{items}</ul>
      ),
    );
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);

    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      const content = (bullet?.[1] ?? numbered?.[1]) as string;
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { ordered, items: [] };
      }
      list.items.push(content);
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();

  return blocks;
}

/** Bold, inline code and citations inside one line of text. */
function inline(text: string, byId: Map<string, SourceRef>): ReactNode[] {
  const out: ReactNode[] = [];
  let key = 0;

  for (const chunk of text.split(INLINE)) {
    if (!chunk) continue;

    if (chunk.startsWith("**") && chunk.endsWith("**") && chunk.length > 4) {
      out.push(<strong key={key++}>{chunk.slice(2, -2)}</strong>);
      continue;
    }
    if (chunk.startsWith("`") && chunk.endsWith("`") && chunk.length > 2) {
      out.push(<code key={key++}>{chunk.slice(1, -1)}</code>);
      continue;
    }

    // Citations inside the remaining plain text.
    let cursor = 0;
    CITATION.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = CITATION.exec(chunk)) !== null) {
      if (match.index > cursor) out.push(chunk.slice(cursor, match.index));

      const label = match[1];
      const id = (/^[DSG]\d+/.exec(label) ?? [""])[0];
      const source = byId.get(id);
      out.push(
        <span
          key={key++}
          className={`cite${id.startsWith("S") ? " cite--sql" : ""}`}
          title={
            source
              ? [source.title, source.version && `v${source.version}`, source.section]
                  .filter(Boolean)
                  .join(" · ")
              : "This citation does not match any evidence that was retrieved."
          }
        >
          {label}
        </span>,
      );
      cursor = match.index + match[0].length;
    }
    if (cursor < chunk.length) out.push(chunk.slice(cursor));
  }

  return out;
}

/**
 * Token-level SQL highlighting.
 *
 * Strings and comments are matched first so a keyword inside a literal is not
 * coloured as a keyword - the same reason the backend guard parses instead of
 * pattern-matching, applied to a much less important problem.
 */
const SQL_KEYWORDS = new Set(
  `select from where group by order having join inner left right full outer on as and or not in
   exists between like is null distinct top limit offset fetch next rows only union all case when
   then else end with over partition asc desc count sum avg min max cast convert coalesce nullif
   inner cross apply desc date dateadd datediff getdate year month day`
    .split(/\s+/)
    .filter(Boolean),
);

const SQL_TOKENS =
  /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)/g;

export function highlightSql(sql: string): JSX.Element[] {
  const out: JSX.Element[] = [];
  let cursor = 0;
  let key = 0;
  let match: RegExpExecArray | null;

  SQL_TOKENS.lastIndex = 0;
  while ((match = SQL_TOKENS.exec(sql)) !== null) {
    if (match.index > cursor) {
      out.push(<span key={key++}>{sql.slice(cursor, match.index)}</span>);
    }
    const [text, comment, str, num, word] = match;

    if (comment) out.push(<span key={key++} className="sql-tok--com">{text}</span>);
    else if (str) out.push(<span key={key++} className="sql-tok--str">{text}</span>);
    else if (num) out.push(<span key={key++} className="sql-tok--num">{text}</span>);
    else if (word && SQL_KEYWORDS.has(word.toLowerCase()))
      out.push(<span key={key++} className="sql-tok--kw">{text}</span>);
    else out.push(<span key={key++}>{text}</span>);

    cursor = match.index + text.length;
  }
  if (cursor < sql.length) out.push(<span key={key++}>{sql.slice(cursor)}</span>);

  return out;
}

export function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

export function formatCell(value: unknown): { text: string; numeric: boolean } {
  if (value === null || value === undefined) return { text: "—", numeric: false };
  if (typeof value === "number") {
    return {
      text: Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2),
      numeric: true,
    };
  }
  if (typeof value === "boolean") return { text: value ? "true" : "false", numeric: false };
  return { text: String(value), numeric: false };
}
