// Live check that the generated client talks to a running service.
//
// Run with the service up:  npm run smoke
//
// It imports from dist/, so it exercises the built artifact a consumer
// installs rather than the TypeScript source.
//
// This is deliberately a real call rather than a mock. The point of generating
// the client is that its types match the service, and only a real round trip
// proves the generated shapes and the wire format agree.

import { createAiApiClient } from "./dist/index.js";

const baseUrl = process.env.API_BASE ?? "http://localhost:8080";
const apiKey = process.env.API_KEY ?? "local-dev-key";

const client = createAiApiClient({
  baseUrl,
  apiKey,
  caller: { callerId: "smoke-user", sessionId: "smoke-session" },
});

const { data: health, error: healthError } = await client.raw.GET("/health");
if (healthError) throw new Error(`health failed: ${JSON.stringify(healthError)}`);
console.log(`health      ${health.service_version} (library ${health.library_version})`);

const { data: completion, error: completionError } = await client.raw.POST(
  "/v1/completions",
  {
    body: {
      engine: "claude",
      model: "claude-haiku-4-5",
      prompt: "Reply with exactly: OK",
      max_response_tokens: 16,
    },
  },
);
if (completionError) throw new Error(`completion failed: ${JSON.stringify(completionError)}`);
console.log(`completion  ${JSON.stringify(completion.text)}`);

const { data: tokens } = await client.raw.POST("/v1/tokens/count", {
  body: { engine: "claude", model: "claude-haiku-4-5", prompt: "How many tokens?" },
});
console.log(`tokens      ${tokens?.token_count}`);

const { data: models } = await client.raw.GET("/v1/models", {
  params: { query: { engine: "claude" } },
});
console.log(`models      ${models?.models.length} listed, ${models?.catalog.length} catalogued`);

let streamed = "";
let chunks = 0;
for await (const chunk of client.streamCompletion({
  engine: "claude",
  model: "claude-haiku-4-5",
  prompt: "Name one primary color.",
})) {
  streamed += chunk;
  chunks += 1;
}
console.log(`streaming   ${chunks} chunks: ${JSON.stringify(streamed.slice(0, 60))}`);
console.log("smoke OK");
