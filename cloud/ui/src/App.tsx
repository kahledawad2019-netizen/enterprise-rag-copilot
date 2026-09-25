import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CopilotApiError, askStream, getHealth, getMeta } from "./api";
import ChatMessage, { type Turn } from "./components/ChatMessage";
import Composer from "./components/Composer";
import Evaluation from "./components/Evaluation";
import KnowledgeBase from "./components/KnowledgeBase";
import RetrievalDebugger from "./components/RetrievalDebugger";
import System from "./components/System";
import type { HealthResponse, MetaResponse, Strategy } from "./types";

type View = "chat" | "knowledge" | "retrieval" | "evaluation" | "system";
type Theme = "light" | "dark" | "system";

interface Conversation {
  id: string;
  title: string;
  turns: Turn[];
  updatedAt: number;
}

const THEME_KEY = "copilot.theme";
const CONVERSATIONS_KEY = "copilot.conversations.v1";
const PREFS_KEY = "copilot.prefs.v1";
const MAX_CONVERSATIONS = 30;

export default function App({ account, persistHistory }: { account?: ReactNode; persistHistory: boolean }) {
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);

  const [view, setView] = useState<View>("chat");
  const [navOpen, setNavOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const [prefs, setPrefs] = useState(readPrefs);
  const [theme, setTheme] = useState<Theme>(readTheme);

  const [conversations, setConversations] = useState<Conversation[]>(() => persistHistory ? readConversations() : []);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const inFlight = useRef<AbortController | null>(null);
  const streamingConversationId = useRef<string | null>(null);
  const savedHistoryRevision = useRef(0);
  const threadEnd = useRef<HTMLDivElement>(null);

  const active = conversations.find((c) => c.id === activeId) ?? null;
  const turns = active?.turns ?? [];

  // ---- persistence (per browser; never required for the app to work) ----
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    safeSet(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => safeSet(PREFS_KEY, JSON.stringify(prefs)), [prefs]);

  useEffect(() => {
    if (!persistHistory) return;
    const save = () => safeSet(CONVERSATIONS_KEY, JSON.stringify(conversations.slice(0, MAX_CONVERSATIONS)));
    // Structural changes must survive a closed tab even while an answer streams.
    if (!busy || savedHistoryRevision.current !== historyRevision) {
      savedHistoryRevision.current = historyRevision;
      save();
      return;
    }
    const timer = window.setTimeout(save, 800);
    return () => window.clearTimeout(timer);
  }, [conversations, busy, historyRevision, persistHistory]);

  // ---- boot ----
  useEffect(() => {
    let cancelled = false;
    getMeta()
      .then((loaded) => {
        if (cancelled) return;
        setMeta(loaded);
        setPrefs((p) => ({
          persona: loaded.personas.some((x) => x.key === p.persona)
            ? p.persona
            : (loaded.personas.find((x) => x.key === "admin") ?? loaded.personas[0])?.key ?? "guest",
          strategy: loaded.strategies.includes(p.strategy) ? p.strategy : loaded.default_strategy,
        }));
      })
      .catch((err) => {
        if (!cancelled) {
          setBootError(
            err instanceof CopilotApiError ? err.message : "The copilot could not be reached.",
          );
        }
      });
    getHealth()
      .then((h) => !cancelled && setHealth(h))
      .catch(() => !cancelled && setHealth({ status: "down", checks: [] }));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: "end" });
  }, [turns.length, busy, activeId]);

  // ---- conversation updates ----
  const patchTurn = useCallback((conversationId: string, turnId: string, patch: Partial<Turn>) => {
    setConversations((prev) =>
      prev.map((c) =>
        c.id !== conversationId
          ? c
          : { ...c, turns: c.turns.map((t) => (t.id === turnId ? { ...t, ...patch } : t)) },
      ),
    );
  }, []);

  const submit = useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text || busy || streamingConversationId.current) return;

      let conversationId = activeId;
      const turn: Turn = { id: crypto.randomUUID(), question: text, text: "", phase: "searching" };
      if (!conversationId) {
        conversationId = crypto.randomUUID();
        const created: Conversation = {
          id: conversationId,
          title: text.length > 60 ? `${text.slice(0, 57)}…` : text,
          turns: [turn],
          updatedAt: Date.now(),
        };
        setConversations((prev) => [created, ...prev].slice(0, MAX_CONVERSATIONS));
        setActiveId(conversationId);
      } else {
        const id = conversationId;
        setConversations((prev) => {
          const current = prev.find((c) => c.id === id);
          if (!current) return prev;
          const updated = { ...current, turns: [...current.turns, turn], updatedAt: Date.now() };
          return [updated, ...prev.filter((c) => c.id !== id)];
        });
      }

      streamingConversationId.current = conversationId;
      setHistoryRevision((revision) => revision + 1);
      setDraft("");
      setBusy(true);
      setView("chat");
      const controller = new AbortController();
      inFlight.current = controller;
      const cid = conversationId;
      let streamed = "";

      try {
        const result = await askStream(
          { question: text, persona: prefs.persona, strategy: prefs.strategy, signal: controller.signal },
          (event) => {
            if (event.type === "sources") {
              patchTurn(cid, turn.id, { phase: "writing", sources: event.sources, route: event.route });
            } else if (event.type === "token") {
              streamed += event.text;
              patchTurn(cid, turn.id, { text: streamed });
            }
          },
        );
        patchTurn(cid, turn.id, { phase: "done", text: result.answer, result, sources: result.sources });
      } catch (err) {
        if ((err as Error).name === "AbortError") {
          patchTurn(cid, turn.id, { phase: "stopped" });
        } else {
          patchTurn(cid, turn.id, {
            phase: "error",
            error:
              err instanceof CopilotApiError ? err.message : "Something went wrong. Please try again.",
          });
        }
      } finally {
        setHistoryRevision((revision) => revision + 1);
        setBusy(false);
        inFlight.current = null;
        streamingConversationId.current = null;
      }
    },
    [activeId, busy, patchTurn, prefs.persona, prefs.strategy],
  );

  const stop = useCallback(() => inFlight.current?.abort(), []);

  const newChat = useCallback(() => {
    if (busy || streamingConversationId.current) return;
    setActiveId(null);
    setView("chat");
    setNavOpen(false);
  }, [busy]);

  const deleteConversation = useCallback(
    (id: string) => {
      if (id === streamingConversationId.current) return;
      setConversations((prev) => prev.filter((c) => c.id !== id));
      setHistoryRevision((revision) => revision + 1);
      if (id === activeId) setActiveId(null);
    },
    [activeId],
  );

  const personaLabel = useMemo(
    () => meta?.personas.find((p) => p.key === prefs.persona)?.label ?? prefs.persona,
    [meta, prefs.persona],
  );

  const go = (next: View) => {
    setView(next);
    setNavOpen(false);
  };

  return (
    <div className={`app-shell${navOpen ? " nav-open" : ""}`}>
      <aside className="side" aria-label="Navigation">
        <div className="nav-brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="18" height="18">
              <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 2.3 6.8 3.8L12 11.9 5.2 8.1 12 4.3Z" fill="currentColor" />
            </svg>
          </span>
          <span className="brand-name">Northwind Copilot</span>
        </div>

        <button type="button" className="new-chat" onClick={newChat} disabled={busy}>
          <Icon name="plus" /> New chat
        </button>

        <nav className="nav-section" aria-label="Conversations">
          <div className="nav-heading">Conversations</div>
          {conversations.length === 0 ? (
            <p className="nav-empty">Your questions will appear here.</p>
          ) : (
            <ul className="conversation-list">
              {conversations.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className={`conversation${c.id === activeId && view === "chat" ? " active" : ""}`}
                    onClick={() => {
                      setActiveId(c.id);
                      go("chat");
                    }}
                    title={c.title}
                  >
                    {c.title}
                  </button>
                  <button
                    type="button"
                    className="conversation-delete"
                    disabled={c.id === streamingConversationId.current}
                    aria-label={c.id === streamingConversationId.current
                      ? `Cannot delete conversation while streaming: ${c.title}`
                      : `Delete conversation: ${c.title}`}
                    onClick={() => deleteConversation(c.id)}
                  >
                    <Icon name="x" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </nav>

        <nav className="nav-section nav-links" aria-label="Workspace">
          <NavLink icon="book" label="Knowledge base" active={view === "knowledge"} onClick={() => go("knowledge")} />
          <div className="nav-heading">Developer</div>
          <NavLink icon="search" label="Retrieval lab" active={view === "retrieval"} onClick={() => go("retrieval")} />
          <NavLink icon="chart" label="Evaluation" active={view === "evaluation"} onClick={() => go("evaluation")} />
          <NavLink icon="pulse" label="System health" active={view === "system"} onClick={() => go("system")} />
        </nav>

        <div className="nav-footer">
          <ProviderBadge meta={meta} health={health} />
          <div className="nav-footer-row">
            <button
              type="button"
              className="settings-button"
              aria-expanded={settingsOpen}
              onClick={() => setSettingsOpen((v) => !v)}
            >
              <Icon name="gear" /> Settings
            </button>
            {account}
          </div>
          {settingsOpen && meta && (
            <div className="settings-panel" role="group" aria-label="Settings">
              <label>
                <span>Theme</span>
                <select value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
                  <option value="system">System</option>
                  <option value="light">Light</option>
                  <option value="dark">Dark</option>
                </select>
              </label>
              {meta.personas.length > 1 && (
                <label>
                  <span>Ask as</span>
                  <select
                    value={prefs.persona}
                    onChange={(e) => setPrefs((p) => ({ ...p, persona: e.target.value }))}
                  >
                    {meta.personas.map((p) => (
                      <option key={p.key} value={p.key}>
                        {p.label}
                      </option>
                    ))}
                  </select>
                  <small>{meta.personas.find((p) => p.key === prefs.persona)?.description}</small>
                </label>
              )}
              <label>
                <span>Retrieval</span>
                <select
                  value={prefs.strategy}
                  onChange={(e) => setPrefs((p) => ({ ...p, strategy: e.target.value as Strategy }))}
                >
                  {meta.strategies.map((s) => (
                    <option key={s} value={s}>
                      {STRATEGY_LABEL[s] ?? s}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </div>
      </aside>

      <button
        type="button"
        className="nav-scrim"
        aria-label="Close navigation"
        tabIndex={navOpen ? 0 : -1}
        onClick={() => setNavOpen(false)}
      />

      <main className="app-main">
        <header className="topbar">
          <button type="button" className="icon-button nav-toggle" aria-label="Open navigation" onClick={() => setNavOpen(true)}>
            <Icon name="menu" />
          </button>
          <h1 className="topbar-title">
            {view === "chat" ? (active?.title ?? "New chat") : VIEW_TITLE[view]}
          </h1>
          <span className="topbar-persona" title="Answers are limited to what this role may read">
            {personaLabel}
          </span>
        </header>

        {bootError && view === "chat" ? (
          <div className="boot-error" role="alert">
            <h2>The copilot is not reachable</h2>
            <p>{bootError}</p>
            <p className="muted">If you are running locally, check that the backend is started and Ollama is running.</p>
          </div>
        ) : view === "chat" ? (
          <>
            <section className="chat-scroll" aria-live="polite">
              {turns.length === 0 ? (
                <EmptyState meta={meta} onPick={(q) => void submit(q)} />
              ) : (
                <div className="thread-inner">
                  {turns.map((t) => (
                    <ChatMessage key={t.id} turn={t} />
                  ))}
                  <div ref={threadEnd} />
                </div>
              )}
            </section>
            <div className="composer-dock">
              <Composer value={draft} busy={busy} onChange={setDraft} onSubmit={submit} onStop={stop} />
              <p className="composer-note">
                Answers cite the company documents they come from. Check the sources for anything important.
              </p>
            </div>
          </>
        ) : (
          <section className="app-page">
            {view === "knowledge" && <KnowledgeBase meta={meta} persona={prefs.persona} personaLabel={personaLabel} />}
            {view === "retrieval" && meta && <RetrievalDebugger meta={meta} persona={prefs.persona} />}
            {view === "evaluation" && <Evaluation />}
            {view === "system" && <System />}
          </section>
        )}
      </main>
    </div>
  );
}

const VIEW_TITLE: Record<View, string> = {
  chat: "Chat",
  knowledge: "Knowledge base",
  retrieval: "Retrieval lab",
  evaluation: "Evaluation",
  system: "System health",
};

const STRATEGY_LABEL: Record<string, string> = {
  reranked: "Best quality (hybrid + rerank)",
  hybrid: "Hybrid (semantic + keyword)",
  dense: "Semantic only",
  sparse: "Keyword only",
};

function EmptyState({ meta, onPick }: { meta: MetaResponse | null; onPick: (q: string) => void }) {
  return (
    <div className="welcome">
      <div className="empty-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="28" height="28">
          <path d="M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 2.3 6.8 3.8L12 11.9 5.2 8.1 12 4.3Z" fill="currentColor" />
        </svg>
      </div>
      <h2>What would you like to know?</h2>
      <p className="empty-sub">
        Ask about company policy, procedures and incidents.
        {meta ? ` Answers are grounded in ${meta.document_count} documents, with citations.` : ""}
      </p>
      {meta && (
        <div className="suggestions">
          {meta.example_questions.map((q) => (
            <button key={q} type="button" className="suggestion" onClick={() => onPick(q)}>
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ProviderBadge({ meta, health }: { meta: MetaResponse | null; health: HealthResponse | null }) {
  if (!meta) return <div className="provider-badge skeleton">Connecting…</div>;
  const local = meta.deployment_mode === "local";
  const provider = local ? "OLLAMA" : meta.llm_provider === "groq" ? "GROQ" : meta.llm_provider.toUpperCase();
  const state = health?.status ?? "unknown";
  return (
    <div className={`provider-badge ${local ? "local" : "cloud"}`} title={`Model: ${meta.chat_model}`}>
      <span className={`dot ${state}`} aria-hidden="true" />
      <span className="provider-mode">
        {local ? "LOCAL" : "CLOUD"} — {provider}
      </span>
      <span className="provider-model">{meta.chat_model}</span>
    </div>
  );
}

function NavLink({ icon, label, active, onClick }: { icon: IconName; label: string; active: boolean; onClick: () => void }) {
  return (
    <button type="button" className={`nav-link${active ? " active" : ""}`} aria-current={active ? "page" : undefined} onClick={onClick}>
      <Icon name={icon} /> {label}
    </button>
  );
}

type IconName = "plus" | "x" | "book" | "search" | "chart" | "pulse" | "gear" | "menu";

const ICON_PATHS: Record<IconName, string> = {
  plus: "M12 5v14M5 12h14",
  x: "M6 6l12 12M18 6 6 18",
  book: "M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5Zm0 14a2 2 0 0 1 2-2h13",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm9 16-4.3-4.3",
  chart: "M4 20V10m6 10V4m6 16v-7m4 7H3",
  pulse: "M3 12h4l2-6 4 12 2-6h6",
  gear: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm7.4 3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.3 7.3 0 0 0-2-1.2L14.5 2h-5l-.4 2.6a7.3 7.3 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.3 7.3 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.3 7.3 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z",
  menu: "M4 7h16M4 12h16M4 17h16",
};

function Icon({ name }: { name: IconName }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path d={ICON_PATHS[name]} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ---- storage helpers: every access can throw in a private window ----

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Not persisting is acceptable; failing the render is not.
  }
}

function readTheme(): Theme {
  const value = safeGet(THEME_KEY);
  return value === "light" || value === "dark" ? value : "system";
}

function readPrefs(): { persona: string; strategy: Strategy } {
  try {
    const parsed = JSON.parse(safeGet(PREFS_KEY) ?? "{}") as Partial<{ persona: string; strategy: Strategy }>;
    return { persona: parsed.persona ?? "admin", strategy: parsed.strategy ?? "reranked" };
  } catch {
    return { persona: "admin", strategy: "reranked" };
  }
}

function readConversations(): Conversation[] {
  try {
    const parsed = JSON.parse(safeGet(CONVERSATIONS_KEY) ?? "[]") as Conversation[];
    if (!Array.isArray(parsed)) return [];
    // A turn saved mid-stream (tab closed) cannot resume; mark it stopped.
    return parsed.map((c) => ({
      ...c,
      turns: c.turns.map((t) =>
        t.phase === "searching" || t.phase === "writing" ? { ...t, phase: "stopped" as const } : t,
      ),
    }));
  } catch {
    return [];
  }
}
