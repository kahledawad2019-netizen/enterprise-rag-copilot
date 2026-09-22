import { useEffect, useRef } from "react";

interface Props {
  value: string;
  busy: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
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

  return (
    <div className="composer">
      <div className="composer__inner">
        <div className="composer__box">
          <textarea
            ref={textarea}
            className="composer__input"
            rows={1}
            value={value}
            placeholder="Ask about a policy, the data, or both…"
            disabled={busy}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!busy && value.trim()) onSubmit();
              }
            }}
          />
          {busy ? (
            <button className="btn" onClick={onStop}>
              Stop
            </button>
          ) : (
            <button className="btn btn--primary" disabled={!value.trim()} onClick={onSubmit}>
              Ask
            </button>
          )}
        </div>
        <div className="composer__foot">
          <span>
            <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
          </span>
          <span>Answers are grounded in retrieved evidence and cite their sources.</span>
        </div>
      </div>
    </div>
  );
}
