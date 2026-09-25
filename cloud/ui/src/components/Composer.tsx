import { useEffect, useRef } from "react";

interface Props {
  value: string;
  busy: boolean;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  onStop: () => void;
}

export default function Composer({ value, busy, onChange, onSubmit, onStop }: Props) {
  const textarea = useRef<HTMLTextAreaElement>(null);

  // Grow with the content up to the CSS max-height, then scroll. Reset to auto
  // first or the box can only ever get taller.
  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${element.scrollHeight}px`;
  }, [value]);

  // Return focus to the box when an answer finishes, ready for a follow-up.
  useEffect(() => {
    if (!busy) textarea.current?.focus();
  }, [busy]);

  const send = () => {
    if (!busy && value.trim()) onSubmit(value);
  };

  return (
    <form
      className="composer2"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <textarea
        ref={textarea}
        rows={1}
        value={value}
        aria-label="Your question"
        placeholder="Ask anything about company policy…"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
      />
      {busy ? (
        <button type="button" className="send-button stop" onClick={onStop} aria-label="Stop generating">
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
            <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
          </svg>
        </button>
      ) : (
        <button type="submit" className="send-button" disabled={!value.trim()} aria-label="Send">
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path d="M12 19V5m0 0-6 6m6-6 6 6" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
    </form>
  );
}
