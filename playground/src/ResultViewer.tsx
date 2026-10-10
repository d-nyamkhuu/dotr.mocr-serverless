import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { ParseResult } from "./api";
import Icon from "./Icon";

const tabs = ["Markdown", "Regions", "JSON", "Raw output"] as const;
const colors: Record<string, string> = {
  Title: "#009cff",
  "Section-header": "#ed4a9b",
  Text: "#38ad59",
  Table: "#e8ae14",
  Picture: "#8181e8",
  Formula: "#9a66c5",
  "List-item": "#11a5a3",
};

function saveFile(content: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function ResultViewer({
  result,
  source,
  filename,
  httpStatus,
  elapsed,
  receivedAt,
}: {
  result: ParseResult | null;
  source: string | null;
  filename: string;
  httpStatus: number | null;
  elapsed: number;
  receivedAt: string | null;
}) {
  const [tab, setTab] = useState<(typeof tabs)[number]>("Markdown");
  const [selected, setSelected] = useState<number | null>(null);
  const [zoom, setZoom] = useState(1);
  const [copied, setCopied] = useState(false);
  const [actionError, setActionError] = useState("");
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const imagePanel = useRef<HTMLElement>(null);
  const json = useMemo(
    () => (result ? JSON.stringify(result, null, 2) : ""),
    [result],
  );
  const regionsJson = useMemo(
    () => (result ? JSON.stringify(result.elements, null, 2) : ""),
    [result],
  );
  const text = !result
    ? ""
    : tab === "JSON"
      ? json
      : tab === "Raw output"
        ? result.raw_text
        : tab === "Regions"
          ? regionsJson
          : result.markdown;
  const lines = useMemo(() => text.split("\n"), [text]);
  const elements = result?.elements ?? [];
  const tokens = result?.generation.usage?.total_tokens;

  async function copy() {
    setActionError("");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setActionError(
        "Could not copy. Select the output text or download it instead.",
      );
    }
  }
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await imagePanel.current?.requestFullscreen();
    } catch {
      setActionError(
        "Fullscreen is unavailable in this browser. Use the zoom controls instead.",
      );
    }
  }
  function selectTab(next: (typeof tabs)[number]) {
    setTab(next);
    setCopied(false);
    setActionError("");
  }
  function tabKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next =
      event.key === "ArrowRight"
        ? (index + 1) % tabs.length
        : event.key === "ArrowLeft"
          ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabs.length - 1
              : null;
    if (next === null) return;
    event.preventDefault();
    selectTab(tabs[next]);
    document.getElementById(`tab-${next}`)?.focus();
  }
  function download() {
    if (!result) return;
    const stem = filename.replace(/\.[^.]+$/, "") || "ocr-result";
    saveFile(
      text,
      `${stem}${tab === "JSON" ? ".json" : tab === "Regions" ? "-regions.json" : tab === "Raw output" ? "-raw.txt" : ".md"}`,
      tab === "JSON" || tab === "Regions"
        ? "application/json"
        : "text/plain;charset=utf-8",
    );
  }

  return (
    <main className="workspace" aria-label="OCR results">
      <div className="workspace-header">
        <div>
          <h2>Results</h2>
          <p>
            View the source image, detected layout, and OCR output from your
            Modal deployment.
          </p>
        </div>
        {result ? (
          <div className="response-stats" aria-label="Response details">
            <span>
              <i
                className={
                  result.status === "degraded"
                    ? "status-dot degraded"
                    : "status-dot"
                }
              />
              {httpStatus} {result.status === "degraded" ? "Degraded" : "OK"}
            </span>
            <span>
              <Icon name="clock" />
              {elapsed.toFixed(2)} s
            </span>
            {tokens != null ? (
              <span>
                <Icon name="image" />
                {tokens.toLocaleString()} tokens
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      {result &&
      (result.status === "degraded" || result.warnings.length > 0) ? (
        <div className="notice warning" role="status">
          <strong>
            {result.status === "degraded"
              ? "Recognition is degraded. Raw output is still available."
              : "Review page warnings."}
          </strong>
          {result.warnings.map((warning, index) => (
            <p key={index}>{warning}</p>
          ))}
        </div>
      ) : null}
      <div className="viewer-grid">
        <section
          className="viewer-panel source-panel"
          ref={imagePanel}
          aria-label="Source image"
        >
          <div className="panel-toolbar">
            <h3>Source image</h3>
            <div className="zoom-controls">
              <button
                type="button"
                className="icon-button"
                aria-label="Zoom out"
                disabled={!source || zoom <= 0.5}
                onClick={() => setZoom(Math.max(0.5, zoom - 0.25))}
              >
                <Icon name="minus" />
              </button>
              <button
                type="button"
                className="zoom-reset"
                aria-label="Reset zoom"
                disabled={!source}
                onClick={() => setZoom(1)}
              >
                {Math.round(zoom * 100)}%
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label="Zoom in"
                disabled={!source || zoom >= 3}
                onClick={() => setZoom(Math.min(3, zoom + 0.25))}
              >
                <Icon name="plus" />
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label="Toggle fullscreen"
                disabled={!source}
                onClick={fullscreen}
              >
                <Icon name="expand" />
              </button>
            </div>
          </div>
          <div className="image-stage">
            {source && failedSource !== source ? (
              <div className="source-sheet" style={{ width: `${zoom * 100}%` }}>
                <img
                  src={source}
                  alt="Uploaded document with detected OCR regions"
                  referrerPolicy="no-referrer"
                  onError={() => setFailedSource(source)}
                />
                {result ? (
                  <div className="regions-overlay">
                    {elements.map((element, index) => {
                      const [x1, y1, x2, y2] = element.bbox;
                      const { original_width: width, original_height: height } =
                        result.image;
                      return (
                        <button
                          type="button"
                          key={index}
                          aria-label={`Region ${index + 1}: ${element.category}`}
                          aria-pressed={selected === index}
                          className={`region-box${selected === index ? " selected" : ""}`}
                          title={`${index + 1}. ${element.category}${element.text ? `: ${element.text.slice(0, 100)}` : ""}`}
                          style={{
                            left: `${(x1 / width) * 100}%`,
                            top: `${(y1 / height) * 100}%`,
                            width: `${((x2 - x1) / width) * 100}%`,
                            height: `${((y2 - y1) / height) * 100}%`,
                            borderColor: colors[element.category] ?? "#8181e8",
                          }}
                          onClick={() => {
                            setSelected(index);
                            selectTab("Regions");
                          }}
                        />
                      );
                    })}
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="empty-state">
                <Icon name="image" />
                <strong>
                  {source ? "Preview unavailable" : "Your document goes here"}
                </strong>
                <p>
                  {source
                    ? "The image could not be displayed. You can still inspect the OCR output."
                    : "Choose an image or enter a public image URL to get started."}
                </p>
              </div>
            )}
          </div>
        </section>
        <section className="viewer-panel output-panel" aria-label="OCR output">
          <div className="panel-toolbar output-toolbar">
            <div
              className="output-tabs"
              role="tablist"
              aria-label="Output format"
            >
              {tabs.map((name, index) => (
                <button
                  type="button"
                  role="tab"
                  id={`tab-${index}`}
                  aria-controls="output-content"
                  tabIndex={tab === name ? 0 : -1}
                  aria-selected={tab === name}
                  onKeyDown={(event) => tabKey(event, index)}
                  key={name}
                  onClick={() => selectTab(name)}
                >
                  {name}
                </button>
              ))}
            </div>
            <div className="output-actions">
              <button
                type="button"
                className="small-button"
                onClick={copy}
                disabled={!result}
              >
                <Icon name={copied ? "check" : "copy"} />
                {copied ? "Copied" : "Copy"}
              </button>
              <button
                type="button"
                className="small-button"
                onClick={download}
                disabled={!result}
              >
                <Icon name="download" />
                Download
              </button>
            </div>
          </div>
          {actionError ? (
            <p className="action-error" role="alert">
              {actionError}
            </p>
          ) : null}
          <div
            id="output-content"
            className="output-content"
            role="tabpanel"
            aria-labelledby={`tab-${tabs.indexOf(tab)}`}
            tabIndex={0}
          >
            {!result ? (
              <div className="empty-state">
                <Icon name="copy" />
                <strong>Ready when you are</strong>
                <p>
                  Run OCR to inspect recognized text, layout regions, and the
                  full API response.
                </p>
              </div>
            ) : tab === "Regions" ? (
              <div className="region-list">
                {elements.length === 0 ? (
                  <p className="no-regions">
                    {result.elements === null
                      ? "Layout regions are unavailable for this degraded response. Check Markdown or Raw output."
                      : "No layout regions were returned."}
                  </p>
                ) : (
                  elements.map((element, index) => (
                    <button
                      type="button"
                      key={index}
                      className={
                        selected === index ? "region-row active" : "region-row"
                      }
                      aria-pressed={selected === index}
                      onClick={() => setSelected(index)}
                    >
                      <span className="region-label">
                        <i
                          style={{
                            backgroundColor:
                              colors[element.category] ?? "#8181e8",
                          }}
                        />
                        {index + 1}. {element.category}
                        <small>{element.bbox.join(", ")}</small>
                      </span>
                      <span className="region-text">
                        {element.text || "Image region"}
                      </span>
                    </button>
                  ))
                )}
              </div>
            ) : (
              <div className="code-output">
                {lines.map((line, index) => (
                  <div className="code-line" key={index}>
                    <span className="line-number" aria-hidden="true">
                      {index + 1}
                    </span>
                    <span>{line || "\u00a0"}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </div>
      <footer className="workspace-footer">
        <span>
          {result ? (
            <>
              Request ID: <code>{result.request_id}</code>
            </>
          ) : (
            "Results stay in this page session."
          )}
        </span>
        <span>
          {receivedAt ? (
            <>
              Response received at{" "}
              <time dateTime={receivedAt}>
                {new Date(receivedAt).toLocaleString()}
              </time>
            </>
          ) : (
            ""
          )}
        </span>
      </footer>
    </main>
  );
}
