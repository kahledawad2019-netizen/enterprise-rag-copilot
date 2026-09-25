import { useRef, useState } from "react";

import { CopilotApiError, uploadDocument } from "../api";
import type { MetaResponse } from "../types";
import Documents from "./Documents";

const ACCEPT = ".md,.markdown,.txt,.pdf,.docx";

interface UploadItem {
  id: string;
  name: string;
  state: "indexing" | "done" | "error";
  message: string;
}

interface Props {
  meta: MetaResponse | null;
  persona: string;
  personaLabel: string;
}

export default function KnowledgeBase({ meta, persona, personaLabel }: Props) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const busy = items.some((i) => i.state === "indexing");

  const upload = async (files: FileList | File[]) => {
    // Sequential on purpose: the backend indexes one document at a time.
    for (const file of Array.from(files)) {
      const id = crypto.randomUUID();
      setItems((prev) => [{ id, name: file.name, state: "indexing", message: "Reading and indexing…" }, ...prev]);

      const maxBytes = (meta?.max_upload_mb ?? 5) * 1024 * 1024;
      if (file.size > maxBytes) {
        setItems((prev) =>
          prev.map((i) => (i.id === id ? { ...i, state: "error", message: `Larger than ${meta?.max_upload_mb ?? 5} MB.` } : i)),
        );
        continue;
      }

      try {
        const res = await uploadDocument(file);
        setItems((prev) =>
          prev.map((i) =>
            i.id === id
              ? { ...i, state: "done", message: `Indexed as ${res.doc_id} · ${res.chunks_written} passages. Ready to ask about.` }
              : i,
          ),
        );
        setRefresh((n) => n + 1);
      } catch (err) {
        setItems((prev) =>
          prev.map((i) =>
            i.id === id
              ? { ...i, state: "error", message: err instanceof CopilotApiError ? err.message : "Upload failed." }
              : i,
          ),
        );
      }
    }
  };

  return (
    <div className="kb">
      <div className="kb-intro">
        <h2>Knowledge base</h2>
        <p className="muted">
          Everything the copilot answers from.
          {meta ? ` ${meta.document_count} documents, ${meta.chunk_count} indexed passages.` : ""}
        </p>
      </div>

      {meta?.upload_enabled ? (
        <div
          className={`dropzone${dragging ? " dragging" : ""}${busy ? " busy" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (e.dataTransfer.files.length) void upload(e.dataTransfer.files);
          }}
        >
          <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
            <path d="M12 16V4m0 0-5 5m5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <p>
            <strong>Drop documents here</strong> or{" "}
            <button type="button" className="link-button" onClick={() => input.current?.click()}>
              browse
            </button>
          </p>
          <p className="muted small">
            Markdown, text, PDF or Word · up to {meta.max_upload_mb} MB · visible to everyone using this deployment
          </p>
          <input
            ref={input}
            type="file"
            accept={ACCEPT}
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files?.length) void upload(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
      ) : (
        meta && <p className="muted small">Uploads are disabled on this deployment.</p>
      )}

      {items.length > 0 && (
        <ul className="upload-list">
          {items.map((i) => (
            <li key={i.id} className={`upload-item ${i.state}`}>
              {i.state === "indexing" ? <span className="spinner" aria-hidden="true" /> : <span className="upload-dot" aria-hidden="true" />}
              <span className="upload-name">{i.name}</span>
              <span className="upload-message">{i.message}</span>
            </li>
          ))}
        </ul>
      )}

      <Documents key={refresh} persona={persona} personaLabel={personaLabel} />
    </div>
  );
}
