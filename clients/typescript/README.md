# @davecthomas/ai-api-unified-http-client

Typed TypeScript client for
[ai-api-unified-http](https://github.com/davecthomas/ai-api-unified-http),
generated from the service's OpenAPI document.

Nothing here describes a request or response by hand. `src/schema.ts` is
generated, and CI regenerates it on every pull request and fails when the
committed output has moved, so the client cannot fall behind the service.

## Install

The package ships TypeScript source, so a bundler compiles it with your app.

```bash
npm install github:davecthomas/ai-api-unified-http#main
```

## Use

```ts
import { createAiApiClient } from "@davecthomas/ai-api-unified-http-client";

const client = createAiApiClient({
  baseUrl: "https://your-service.run.app",
  apiKey: process.env.API_KEY!,
  caller: { callerId: "user-42" },   // splits provider spend per end user
});

const { data, error } = await client.raw.POST("/v1/completions", {
  body: { engine: "claude", prompt: "Name three primary colors." },
});
```

Paths, bodies, and responses are checked against the generated schema, so a
wrong field name or a missing required property is a compile error.

### Streaming

Server-sent events are outside what OpenAPI describes, so this part is
hand-written:

```ts
for await (const chunk of client.streamCompletion({
  engine: "claude",
  prompt: "Count to five.",
})) {
  process.stdout.write(chunk);
}
```

A stream that fails mid-flight throws. The service sends a terminal `error`
event even though the response began with 200 — the status line was already
sent when the failure happened — and this client raises it so a broken stream
cannot read as a short one.

### Cost attribution

`callerId`, `sessionId`, and `workflowId` become `X-Caller-Id`, `X-Session-Id`,
and `X-Workflow-Id`, which the service records against each call's cost. Pass
them once when constructing the client, or per call:

```ts
await client.raw.POST("/v1/completions", {
  body: { engine: "claude", prompt: "..." },
  headers: client.headersFor({ callerId: "user-99" }),
});
```

## Regenerate

From the repository root, after changing the service's shapes:

```bash
make client
```

That dumps the spec from the app object, regenerates the types, and
typechecks. Commit the result; CI compares against it.

## Live check

With the service running:

```bash
npm run smoke                                  # localhost:8080, local-dev-key
API_BASE=https://... API_KEY=... npm run smoke
```

It calls health, a completion, a token count, the model catalog, and a stream,
because only a real round trip proves the generated types and the wire format
agree.
