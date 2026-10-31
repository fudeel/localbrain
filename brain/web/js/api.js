import { storage } from "./dom.js";

const BASE = "/api/v1";
const RECONNECT_MS = 2000;

export class ApiError extends Error {
  constructor(status, code, message, hint) {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
  }
}

export function agentPath(name, suffix = "") {
  return `/agents/${encodeURIComponent(name)}${suffix}`;
}

export class BrainApi {
  onUnauthorized = () => {};

  get token() {
    return storage.get("token");
  }

  set token(value) {
    storage.set("token", value);
  }

  get(path) {
    return this.#json("GET", path);
  }

  post(path, body = {}) {
    return this.#json("POST", path, body);
  }

  patch(path, body) {
    return this.#json("PATCH", path, body);
  }

  delete(path) {
    return this.#json("DELETE", path);
  }

  // Streams newline-delimited JSON. Aborting the signal also stops the agent on the server.
  async ndjson(path, body, onEvent, signal) {
    const response = await this.#send("POST", path, body, signal);
    await readLines(response, signal, (line) => {
      if (line.trim()) onEvent(JSON.parse(line));
    });
  }

  // Follows server events for as long as the page is open, reconnecting after any drop.
  // Uses fetch rather than EventSource, because EventSource cannot send the API token.
  follow(onEvent, onConnectionChange) {
    const run = async () => {
      for (;;) {
        try {
          const response = await this.#send("GET", "/events");
          onConnectionChange(true);
          let type = "message";
          await readLines(response, null, (line) => {
            if (line.startsWith("event: ")) type = line.slice(7);
            else if (line.startsWith("data: ")) onEvent(type, JSON.parse(line.slice(6)));
          });
        } catch (_) {
          // Falls through to the reconnect below.
        }
        onConnectionChange(false);
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
      }
    };
    run();
  }

  async #json(method, path, body) {
    const response = await this.#send(method, path, body);
    if (response.status === 204) return null;
    return response.json();
  }

  async #send(method, path, body, signal) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    const response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.ok) return response;
    let details = {};
    try {
      details = (await response.json()).error ?? {};
    } catch (_) {
      // A non-JSON failure, for example from a proxy in front of the brain.
    }
    if (response.status === 401) this.onUnauthorized();
    throw new ApiError(
      response.status,
      details.code ?? "http_error",
      details.message ?? `The server answered with HTTP ${response.status}.`,
      details.hint,
    );
  }
}

async function readLines(response, signal, onLine) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        onLine(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
      }
    }
    if (buffer) onLine(buffer);
  } finally {
    if (signal?.aborted) await reader.cancel().catch(() => {});
  }
}
