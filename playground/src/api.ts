export type LayoutElement = {
  bbox: [number, number, number, number];
  category: string;
  text?: string | null;
};

export type ParseResult = {
  request_id: string;
  status: "ok" | "degraded";
  warnings: string[];
  model: string;
  image: {
    original_width: number;
    original_height: number;
    model_input_width: number;
    model_input_height: number;
  };
  elements: LayoutElement[] | null;
  markdown: string;
  raw_text: string;
  generation: {
    finish_reason?: string | null;
    usage?: {
      prompt_tokens?: number | null;
      completion_tokens?: number | null;
      total_tokens?: number | null;
    } | null;
  };
};

export type Connection = { endpoint: string; token: string };
export const STORAGE_KEY = "dots-mocr-studio-connection-v1";
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

export function readConnection(): Connection {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "null");
    if (
      saved &&
      typeof saved.endpoint === "string" &&
      typeof saved.token === "string"
    ) {
      return { endpoint: saved.endpoint, token: saved.token };
    }
  } catch {
    /* Storage can be disabled by the browser. */
  }
  return { endpoint: "", token: "" };
}

export function parseEndpoint(value: string): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new Error("Enter a valid HTTPS Modal endpoint URL.");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new Error(
      "Use an HTTPS endpoint URL without credentials, query parameters, or fragments.",
    );
  }
  url.pathname = `${url.pathname.replace(/\/+$/, "").replace(/\/v1\/parse$/, "")}/v1/parse`;
  return url.href;
}

export function validateImageUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new Error("Enter a public HTTPS image URL.");
  }
  if (url.protocol !== "https:" || url.username || url.password) {
    throw new Error(
      "Enter a public HTTPS image URL without embedded credentials.",
    );
  }
  return url.href;
}

export function validateFile(file: File) {
  if (!["image/png", "image/jpeg"].includes(file.type))
    throw new Error("Choose a PNG or JPEG image.");
  if (file.size === 0) throw new Error("The selected image is empty.");
  if (file.size > MAX_IMAGE_BYTES)
    throw new Error("The image exceeds the 20 MiB limit.");
}

function isParseResult(value: unknown): value is ParseResult {
  if (!value || typeof value !== "object") return false;
  const result = value as Partial<ParseResult>;
  return (
    typeof result.request_id === "string" &&
    typeof result.model === "string" &&
    ["ok", "degraded"].includes(result.status ?? "") &&
    Array.isArray(result.warnings) &&
    result.warnings.every((warning) => typeof warning === "string") &&
    typeof result.markdown === "string" &&
    typeof result.raw_text === "string" &&
    !!result.image &&
    Number.isFinite(result.image.original_width) &&
    result.image.original_width > 0 &&
    Number.isFinite(result.image.original_height) &&
    result.image.original_height > 0 &&
    !!result.generation &&
    typeof result.generation === "object" &&
    (result.elements === null ||
      (Array.isArray(result.elements) &&
        result.elements.every(
          (element) =>
            element &&
            typeof element.category === "string" &&
            Array.isArray(element.bbox) &&
            element.bbox.length === 4 &&
            element.bbox.every(Number.isFinite) &&
            (element.text == null || typeof element.text === "string"),
        )))
  );
}

export async function parseImage(
  connection: Connection,
  body: FormData,
  signal: AbortSignal,
): Promise<{ result: ParseResult; httpStatus: number }> {
  const endpoint = parseEndpoint(connection.endpoint);
  if (!connection.token.trim()) throw new Error("Enter your OCR bearer token.");
  let response: Response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: { Authorization: `Bearer ${connection.token.trim()}` },
      body,
      signal,
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error(
      "Could not read the API response. Check the endpoint, network, and CORS configuration. A cold start or request over 150 seconds can also cause this. OCR may still be running; retry only when ready.",
    );
  }
  const text = await response.text();
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    /* Handle non-JSON server errors below. */
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403)
      throw new Error(
        `Authentication failed (HTTP ${response.status}). Check your OCR bearer token.`,
      );
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? payload.detail
        : null;
    const message =
      typeof detail === "string"
        ? detail
        : response.status === 413
          ? "The image exceeds the API size limit."
          : response.status === 422
            ? "The API rejected this image or URL."
            : "The OCR request failed.";
    throw new Error(`${message} (HTTP ${response.status})`);
  }
  if (!isParseResult(payload))
    throw new Error(
      "The endpoint returned an unexpected response. Use the dots.mocr /v1/parse API.",
    );
  return { result: payload, httpStatus: response.status };
}
