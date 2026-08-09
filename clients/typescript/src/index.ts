// clients/typescript/src/index.ts
//
// Typed client for ai-api-unified-http.
//
// Everything endpoint-shaped in here comes from `schema.ts`, which is
// generated from the service's OpenAPI document. Nothing describes a request
// or response by hand, so a change to the service's shapes turns into a
// TypeScript error here rather than a runtime surprise in a consumer.
//
// What this file does add is the part a spec cannot express: how to attach the
// API key, how to carry the caller identifiers that split cost per end user,
// and how to read a Server-Sent Events stream, which OpenAPI has no vocabulary
// for.

import createClient from "openapi-fetch";
import type { paths } from "./schema.ts";

export type { paths, components } from "./schema.ts";

/** Request and response bodies, named for the endpoints they belong to. */
export type CompletionRequest =
  paths["/v1/completions"]["post"]["requestBody"]["content"]["application/json"];
export type CompletionResponse =
  paths["/v1/completions"]["post"]["responses"][200]["content"]["application/json"];
export type StructuredRequest =
  paths["/v1/structured"]["post"]["requestBody"]["content"]["application/json"];
export type ConversationTurnRequest =
  paths["/v1/conversations/turn"]["post"]["requestBody"]["content"]["application/json"];
export type EmbeddingsRequest =
  paths["/v1/embeddings"]["post"]["requestBody"]["content"]["application/json"];
export type TokenCountRequest =
  paths["/v1/tokens/count"]["post"]["requestBody"]["content"]["application/json"];

/**
 * Identifiers that split provider spend below the API key.
 *
 * The key names a calling application, so an app serving many users produces
 * one undifferentiated total without these. They are attribution rather than
 * authorization: the service cannot verify them, and access control stays with
 * the key.
 */
export interface CallerContext {
  /** Stable, opaque id for the end user. Reaches the cost record, so avoid
   *  names and email addresses. */
  callerId?: string;
  sessionId?: string;
  workflowId?: string;
}

export interface ClientOptions {
  /** Base URL of the service, without a trailing slash. */
  baseUrl: string;
  /** API key presented as `Authorization: Bearer`. */
  apiKey: string;
  /** Applied to every request; per-call context overrides it. */
  caller?: CallerContext;
  /** Injected in environments without a global fetch. */
  fetch?: typeof globalThis.fetch;
}

function callerHeaders(caller?: CallerContext): Record<string, string> {
  const headers: Record<string, string> = {};
  if (caller?.callerId) headers["X-Caller-Id"] = caller.callerId;
  if (caller?.sessionId) headers["X-Session-Id"] = caller.sessionId;
  if (caller?.workflowId) headers["X-Workflow-Id"] = caller.workflowId;
  return headers;
}

/**
 * Build a typed client for the service.
 *
 * Every method is `openapi-fetch`'s, so paths, bodies, and responses are
 * checked against the generated schema.
 */
export function createAiApiClient(options: ClientOptions) {
  const client = createClient<paths>({
    baseUrl: options.baseUrl.replace(/\/$/, ""),
    fetch: options.fetch,
    headers: {
      Authorization: `Bearer ${options.apiKey}`,
      ...callerHeaders(options.caller),
    },
  });

  return {
    /** The underlying typed client, for any endpoint not wrapped below. */
    raw: client,

    /** Headers for one call, merging per-call caller context over the default. */
    headersFor(caller?: CallerContext): Record<string, string> {
      return callerHeaders({ ...options.caller, ...caller });
    },

    /**
     * Stream a completion, yielding text as it arrives.
     *
     * SSE is outside what OpenAPI describes, so this is written rather than
     * generated. It yields each chunk's text and returns once the stream ends.
     *
     * A stream that fails mid-flight arrives as a terminal `error` event even
     * though the response began with 200 — the status line was already sent
     * when the failure happened. That case throws, so a caller cannot mistake
     * a broken stream for a short one.
     */
    async *streamCompletion(
      body: Omit<CompletionRequest, "stream">,
      caller?: CallerContext,
    ): AsyncGenerator<string, void, unknown> {
      const doFetch = options.fetch ?? globalThis.fetch;
      const response = await doFetch(`${options.baseUrl}/v1/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${options.apiKey}`,
          ...callerHeaders({ ...options.caller, ...caller }),
        },
        body: JSON.stringify({ ...body, stream: true }),
      });

      if (!response.ok) {
        throw new Error(
          `stream request failed: HTTP ${response.status} ${await response.text()}`,
        );
      }
      if (!response.body) {
        throw new Error("stream response carried no body");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() ?? "";

        for (const block of blocks) {
          let event = "";
          let data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event: ")) event = line.slice(7);
            else if (line.startsWith("data: ")) data = line.slice(6);
          }
          if (!event) continue;
          const payload = data ? JSON.parse(data) : {};

          if (event === "chunk") {
            yield payload.text as string;
          } else if (event === "error") {
            throw new Error(
              `stream failed after ${payload.chunks_delivered} chunks: ${payload.detail}`,
            );
          } else if (event === "done") {
            return;
          }
        }
      }
    },
  };
}

export type AiApiClient = ReturnType<typeof createAiApiClient>;
