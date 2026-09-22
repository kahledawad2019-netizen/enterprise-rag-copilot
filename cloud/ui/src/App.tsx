import { useCallback, useEffect, useRef, useState } from "react";

import { CopilotApiError, ask, getHealth, getMeta } from "./api";
import AnswerCard from "./components/AnswerCard";
import Composer from "./components/Composer";
import Sidebar from "./components/Sidebar";
import type { AskResponse, HealthResponse, MetaResponse, Strategy } from "./types";

type Turn =
  | { kind: "question"; id: string; text: string }
  | { kind: "answer"; id: string; result: AskResponse }
  | { kind: "error"; id: string; message: string; code: string };

type Theme = "light" | "dark" | "system";

const THEME_KEY = "copilot.theme";

export default function App() {
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);

  const [persona, setPersona] = useState("admin");
  const [strategy, setStrategy] = useState<Strategy>("reranked");

  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const [theme, setTheme] = useState<Theme>(readTheme);
  const inFlight = useRef<AbortController | null>(null);
  const threadEnd = useRef<HTMLDivElement>(null);

  // ---- theme ----
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Private windows and blocked site data both throw here. The theme just
      // does not persist, which is not worth failing a render over.
    }
  }, [theme]);

  // ---- boot ----
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const loaded = await getMeta();
        if (cancelled) return;
        setMeta(loaded);
        setStrategy(loaded.default_strategy);
        if (loaded.personas.length && !loaded.personas.some((p) => p.key === "admin")) {
          setPersona(loaded.personas[0].key);
        }
      } catch (err) {
        if (!cancelled) {
          setBootError(
            err instanceof CopilotApiError
              ? err.message
              : "The copilot could not be reached.",
          );
        }
      }
    })();

    getHealth()
      .then((h) => !cancelled && setHealth(h))
      .catch(() => !cancelled && setHealth({ status: "down", checks: [] }));

    return () => {
      cancelled = true;
    };
  }, []);

  // ---- keep the newest turn in view ----
  useEffect(() => {
    threadEnd.current?.scrollIntoView({ block: "end" });
  }, [turns, busy]);

  const submit = useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text || busy) return;

      const id = crypto.randomUUID();
      setTurns((prev) => [...prev, { kind: "question", id, text }]);
      setDraft("");
      setBusy(true);

      const controller = new AbortController();
      inFlight.current = controller;

      try {
        const result = await ask({ question: text, persona, strategy, signal: controller.signal });
        setTurns((prev) => [...prev, { kind: "answer", id: `${id}-a`, result }]);
      } catch (err) {
        if (controller.signal.aborted) {
          setTurns((prev) => [
            ...prev,
            { kind: "error", id: `${id}-e`, message: "Cancelled.", code: "aborted" },
          ]);
        } else {
          const apiError = err instanceof CopilotApiError ? err : null;
          setTurns((prev) => [
            ...prev,
            {
              kind: "error",
              id: `${id}-e`,
              message: apiError?.message ?? "Something went wrong.",
              code: apiError?.code ?? "unknown",
            },
          ]);
        }
      } finally {
        inFlight.current = null;
        setBusy(false);
      }
    },
    [busy, persona, strategy],
  );

  const healthTone =
    health?.status === "ok" ? "badge--ok" : health?.status === "degraded" ? "badge--warn" : "badge--danger";

  return (
    <div className="shell">
      <div className="brand">
        <span className="brand__mark">EC</span>
        <span className="brand__name">{meta?.app_name ?? "Enterprise Copilot"}</span>
      </div>

      <header className="header">
        <div className="header__meta">
          {health && (
            <span className={`badge ${healthTone}`} title={health.checks.map((c) => `${c.name}: ${c.detail}`).join("\n")}>
              <span className="dot" />
              {health.status === "ok" ? "All systems normal" : health.status === "degraded" ? "Degraded" : "Backend down"}
            </span>
          )}
          {meta && <span>{meta.chat_model}</span>}
        </div>

        <div className="header__actions">
          <button
            className="btn btn--ghost"
            title="Switch theme"
            onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark")}
          >
            {theme === "dark" ? "Dark" : theme === "light" ? "Light" : "System"}
          </button>
          <button
            className="btn btn--ghost"
            disabled={!turns.length || busy}
            onClick={() => setTurns([])}
          >
            Clear
          </button>
        </div>
      </header>

      <Sidebar
        meta={meta}
        persona={persona}
        strategy={strategy}
        busy={busy}
        onPersona={setPersona}
        onStrategy={setStrategy}
        onExample={submit}
      />

      <main className="main">
        <div className="thread">
          <div className="thread__inner">
            {bootError && (
              <div className="notice notice--danger">
                <span className="notice__icon">✕</span>
                <span>{bootError}</span>
              </div>
            )}

            {!turns.length && !bootError && <Welcome />}

            {turns.map((turn) => {
              if (turn.kind === "question") {
                return (
                  <div key={turn.id} className="turn turn--user">
                    <div className="bubble">{turn.text}</div>
                  </div>
                );
              }
              if (turn.kind === "answer") {
                return (
                  <div key={turn.id} className="turn">
                    <AnswerCard result={turn.result} />
                  </div>
                );
              }
              return (
                <div key={turn.id} className="turn">
                  <div className={`notice ${turn.code === "aborted" ? "notice--info" : "notice--danger"}`}>
                    <span className="notice__icon">{turn.code === "aborted" ? "i" : "✕"}</span>
                    <span>{turn.message}</span>
                  </div>
                </div>
              );
            })}

            {busy && (
              <div className="turn">
                <div className="thinking">
                  <span className="spinner" />
                  <span>Routing, retrieving evidence and generating an answer…</span>
                </div>
              </div>
            )}

            <div ref={threadEnd} />
          </div>
        </div>

        <Composer
          value={draft}
          busy={busy}
          onChange={setDraft}
          onSubmit={() => submit(draft)}
          onStop={() => inFlight.current?.abort()}
        />
      </main>
    </div>
  );
}

function Welcome() {
  return (
    <div className="welcome">
      <h1 className="welcome__title">Ask about policy, data, or both</h1>
      <p className="welcome__sub">
        Questions are routed before anything is retrieved. Document answers cite the policy,
        version and section they came from. Data answers show the exact query that ran, after it
        passed a read-only guard.
      </p>
      <div className="welcome__grid">
        <Capability
          title="Grounded, not fluent"
          body="Every citation is checked against the evidence that was actually retrieved. An uncited answer is labelled ungrounded."
        />
        <Capability
          title="Refuses by rule"
          body="A destructive request is refused before any SQL exists, rather than being quietly rewritten into something harmless."
        />
        <Capability
          title="Versions matter"
          body="A superseded policy never answers a current question, and conflicting sources are reported rather than silently picked between."
        />
        <Capability
          title="Shows its work"
          body="Generated SQL, retrieval scores and per-stage timings are on every answer."
        />
      </div>
    </div>
  );
}

function Capability({ title, body }: { title: string; body: string }) {
  return (
    <div className="persona-card">
      <div style={{ fontWeight: 620, fontSize: 13 }}>{title}</div>
      <div style={{ fontSize: 12.5, color: "var(--ink-muted)", lineHeight: 1.5 }}>{body}</div>
    </div>
  );
}

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    // Accessing storage can throw outright in some embedded contexts.
  }
  return "system";
}
