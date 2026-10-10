import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from "react";
import {
  MAX_IMAGE_BYTES,
  STORAGE_KEY,
  parseEndpoint,
  parseImage,
  readConnection,
  validateFile,
  validateImageUrl,
  type Connection,
  type ParseResult,
} from "./api";
import Icon from "./Icon";
import ResultViewer from "./ResultViewer";

type InputImage = { file: File; preview: string };
type ResponseState = {
  result: ParseResult;
  source: string;
  filename: string;
  elapsed: number;
  httpStatus: number;
  receivedAt: string;
};

export default function App() {
  const [connection, setConnection] = useState<Connection>(readConnection);
  const [storageAvailable, setStorageAvailable] = useState(true);
  const [showToken, setShowToken] = useState(false);
  const [mode, setMode] = useState<"file" | "url">("file");
  const [input, setInput] = useState<InputImage | null>(null);
  const [imageUrl, setImageUrl] = useState("");
  const [response, setResponse] = useState<ResponseState | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [dragging, setDragging] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    return () => {
      if (input) URL.revokeObjectURL(input.preview);
    };
  }, [input]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!running) return;
    const start = performance.now();
    const timer = window.setInterval(
      () => setElapsed((performance.now() - start) / 1000),
      100,
    );
    return () => window.clearInterval(timer);
  }, [running]);

  function updateConnection(next: Connection) {
    setConnection(next);
    try {
      if (!next.endpoint && !next.token) sessionStorage.removeItem(STORAGE_KEY);
      else sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      setStorageAvailable(true);
    } catch {
      setStorageAvailable(false);
    }
  }
  function chooseFile(file: File | undefined) {
    if (!file) return;
    setError("");
    try {
      validateFile(file);
      setInput({ file, preview: URL.createObjectURL(file) });
      setResponse(null);
    } catch (error) {
      setError((error as Error).message);
    }
  }
  function inputChanged(event: ChangeEvent<HTMLInputElement>) {
    chooseFile(event.target.files?.[0]);
    event.target.value = "";
  }
  function changeMode(next: "file" | "url") {
    setMode(next);
    setError("");
    setResponse(null);
  }
  async function run(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (controller.current) return;
    setError("");
    setResponse(null);
    let source: string;
    let filename: string;
    const body = new FormData();
    try {
      parseEndpoint(connection.endpoint);
      if (!connection.token.trim())
        throw new Error("Enter your OCR bearer token.");
      if (mode === "file") {
        if (!input) throw new Error("Choose a PNG or JPEG image.");
        validateFile(input.file);
        body.set("file", input.file, input.file.name);
        source = input.preview;
        filename = input.file.name;
      } else {
        source = validateImageUrl(imageUrl);
        body.set("image_url", source);
        filename = new URL(source).pathname.split("/").pop() || "remote-image";
      }
    } catch (error) {
      setError((error as Error).message);
      return;
    }
    const abort = new AbortController();
    controller.current = abort;
    const started = performance.now();
    setRunning(true);
    setElapsed(0);
    const timeout = window.setTimeout(() => abort.abort("timeout"), 145_000);
    try {
      const data = await parseImage(connection, body, abort.signal);
      setResponse({
        ...data,
        source,
        filename,
        elapsed: (performance.now() - started) / 1000,
        receivedAt: new Date().toISOString(),
      });
    } catch (error) {
      setError(
        abort.signal.aborted
          ? abort.signal.reason === "timeout"
            ? "The browser stopped waiting after 145 seconds. OCR may still be running on Modal. Check your deployment before retrying."
            : "Stopped waiting for the response. OCR may still be running on Modal."
          : (error as Error).message,
      );
    } finally {
      window.clearTimeout(timeout);
      controller.current = null;
      setRunning(false);
    }
  }

  const canRun = Boolean(
    connection.endpoint.trim() &&
      connection.token.trim() &&
      (mode === "file" ? input : imageUrl.trim()),
  );
  const source =
    response?.source ?? (mode === "file" ? (input?.preview ?? null) : null);

  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>OCR Studio</h1>
        <p>Test your Modal deployment</p>
      </header>
      <div className="app-body">
        <aside className="control-rail" aria-label="Request settings">
          <form onSubmit={run}>
            <fieldset disabled={running} className="connection-fields">
              <legend>Connection</legend>
              <p className="section-description">
                Enter your deployed Modal endpoint and credentials.
              </p>
              <label className="field">
                <span>Endpoint URL</span>
                <input
                  type="url"
                  value={connection.endpoint}
                  onChange={(event) =>
                    updateConnection({
                      ...connection,
                      endpoint: event.target.value,
                    })
                  }
                  placeholder="https://workspace--api.modal.run"
                  autoComplete="off"
                  spellCheck={false}
                  required
                />
                <small>Your Modal OCR HTTP endpoint URL.</small>
              </label>
              <div className="field">
                <label htmlFor="bearer-token">Bearer token</label>
                <div className="secret-field">
                  <input
                    id="bearer-token"
                    aria-describedby="token-help"
                    type={showToken ? "text" : "password"}
                    value={connection.token}
                    onChange={(event) =>
                      updateConnection({
                        ...connection,
                        token: event.target.value,
                      })
                    }
                    placeholder="Your OCR API token"
                    autoComplete="off"
                    spellCheck={false}
                    required
                  />
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={showToken ? "Hide token" : "Show token"}
                    aria-pressed={showToken}
                    onClick={() => setShowToken(!showToken)}
                  >
                    <Icon name="eye" />
                  </button>
                </div>
                <small id="token-help">
                  {storageAvailable
                    ? "Saved for this tab. Not stored permanently."
                    : "Tab storage is unavailable. Credentials stay in memory."}
                </small>
              </div>
              <button
                type="button"
                className="clear-button"
                disabled={!connection.token}
                onClick={() => {
                  updateConnection({ ...connection, token: "" });
                  setShowToken(false);
                }}
              >
                <Icon name="trash" />
                Clear credentials
              </button>
            </fieldset>
            <fieldset disabled={running} className="input-fields">
              <legend>Input</legend>
              <p className="section-description">
                Provide an image file or a public HTTPS image URL.
              </p>
              <div
                className="input-modes"
                role="group"
                aria-label="Image source"
              >
                <button
                  type="button"
                  aria-pressed={mode === "file"}
                  onClick={() => changeMode("file")}
                >
                  Upload image
                </button>
                <button
                  type="button"
                  aria-pressed={mode === "url"}
                  onClick={() => changeMode("url")}
                >
                  Image URL
                </button>
              </div>
              {mode === "file" ? (
                <div
                  className={`drop-zone${dragging ? " dragging" : ""}`}
                  onDragOver={(event) => {
                    event.preventDefault();
                    if (!running) setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={(event) => {
                    event.preventDefault();
                    setDragging(false);
                    if (!running) chooseFile(event.dataTransfer.files[0]);
                  }}
                >
                  <Icon name="image" />
                  <strong>
                    {input ? input.file.name : "Drop an image here"}
                  </strong>
                  <p>PNG or JPEG · up to {MAX_IMAGE_BYTES / 1024 / 1024} MiB</p>
                  <button
                    type="button"
                    className="small-button"
                    onClick={() => fileInput.current?.click()}
                  >
                    {input ? "Change file" : "Choose image"}
                  </button>
                  <input
                    ref={fileInput}
                    aria-label="Image file"
                    type="file"
                    accept="image/png,image/jpeg,.png,.jpg,.jpeg"
                    onChange={inputChanged}
                    className="visually-hidden"
                    tabIndex={-1}
                  />
                </div>
              ) : (
                <div className="field image-url-field">
                  <label htmlFor="image-url">Image URL</label>
                  <input
                    id="image-url"
                    aria-describedby="image-url-help"
                    type="url"
                    value={imageUrl}
                    onChange={(event) => {
                      setImageUrl(event.target.value);
                      setResponse(null);
                    }}
                    placeholder="https://example.com/document.png"
                    required
                  />
                  <small id="image-url-help">
                    Use a public HTTPS URL returning a PNG or JPEG image.
                  </small>
                </div>
              )}
            </fieldset>
            {error ? (
              <div className="notice error" role="alert">
                {error}
              </div>
            ) : null}
            <button
              type="submit"
              className="run-button"
              disabled={!canRun || running}
            >
              {running ? (
                <>
                  <span className="spinner" />
                  Running OCR · {elapsed.toFixed(1)} s
                </>
              ) : (
                <>
                  <Icon name="play" />
                  Run OCR
                </>
              )}
            </button>
            {running ? (
              <div className="running-state" role="status">
                <p>
                  {elapsed >= 20
                    ? "Modal may be starting the GPU. Please keep this page open."
                    : "Waiting for your Modal deployment…"}
                </p>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => controller.current?.abort("cancelled")}
                >
                  Stop waiting
                </button>
              </div>
            ) : null}
          </form>
        </aside>
        <ResultViewer
          key={response?.receivedAt ?? source ?? "empty"}
          result={response?.result ?? null}
          source={source}
          filename={response?.filename ?? input?.file.name ?? "ocr-result"}
          httpStatus={response?.httpStatus ?? null}
          elapsed={response?.elapsed ?? 0}
          receivedAt={response?.receivedAt ?? null}
        />
      </div>
    </div>
  );
}
