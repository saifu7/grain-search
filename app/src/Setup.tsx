// The first-run setup window of the installed app: downloads Python, the libraries and PyTorch
// (setup.rs does the work; this shows the progress). Closes by itself when done and opens Settings.
import { useEffect, useState } from "react";
import { onSetup, setupCancel, setupStart, setupStatus } from "./platform";
import type { SetupStatus, SetupStep } from "./types";

export default function Setup() {
  const [st, setSt] = useState<SetupStatus | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Setting up Grain Search";
    const stop = onSetup(setSt);
    setupStatus()
      .then((s) => {
        if (s) setSt(s);
        // start by itself the first time (a failed or cancelled run waits for Retry)
        if (s && !s.running && !s.done && !s.error) setupStart().catch((e) => setStartError(String(e)));
      })
      .catch((e) => setStartError(String(e)));
    return stop;
  }, []);

  const retry = () => {
    setStartError(null);
    setupStart().catch((e) => setStartError(String(e)));
  };
  const error = startError ?? st?.error ?? null;
  const running = st?.running ?? false;
  const current = st?.steps.find((s) => s.state === "running");
  const downloading = current && ["python", "libraries", "pytorch"].includes(current.id);

  return (
    <div className="setup-page">
      <h1>Setting up Grain Search</h1>
      <p className="muted">
        Grain Search needs Python and PyTorch to understand your files. This is a one-time download: about
        3 GB with an NVIDIA GPU, much less without one.
      </p>

      {st?.build && (
        <p className={"setup-gpu " + st.build}>
          {st.build === "cuda"
            ? `${st.gpu ?? "NVIDIA GPU"} found: using the fast GPU version.`
            : "No NVIDIA GPU found: using the smaller CPU version (indexing will be slower)."}
        </p>
      )}

      <ol className="setup-steps">
        {(st?.steps ?? []).map((s) => (
          <li key={s.id} className={`setup-step ${s.state}`}>
            <StepIcon state={s.state} />
            <span className="setup-label">{s.label}</span>
            {s.state === "running" && downloading && s.id === current?.id && (
              <span className="setup-mb">{formatMb(st?.downloaded_mb ?? 0)}</span>
            )}
            {s.state === "skipped" && <span className="setup-mb">already done</span>}
          </li>
        ))}
      </ol>

      {running && (
        <div className="progress indeterminate" role="progressbar" aria-label="Setting up">
          <div />
        </div>
      )}

      {error && (
        <p className="setup-error" role="alert">
          {error}
        </p>
      )}

      {st?.done && <p className="setup-done">All set! Opening Settings to download the AI model…</p>}

      <div className="setup-actions">
        {running ? (
          <button className="btn" onClick={() => setupCancel()}>
            Cancel
          </button>
        ) : (
          !st?.done && (
            <button className="btn primary" onClick={retry}>
              {error ? "Try again" : "Start"}
            </button>
          )
        )}
      </div>
      {error && !running && (
        <p className="muted small">
          What has already downloaded is kept, so trying again continues where it stopped.
        </p>
      )}
    </div>
  );
}

function StepIcon({ state }: { state: SetupStep["state"] }) {
  const sign = { pending: "○", running: "", done: "✓", failed: "✕", skipped: "✓" }[state];
  return (
    <span className={`setup-icon ${state}`} aria-hidden="true">
      {state === "running" ? <span className="spinner" /> : sign}
    </span>
  );
}

function formatMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB downloaded` : `${mb} MB downloaded`;
}
