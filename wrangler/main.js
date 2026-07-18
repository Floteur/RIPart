export default {
    async fetch(request, env, ctx) {
      if (request.method !== "POST") {
        return new Response("Use POST /v1/chat/completions", { status: 405 });
      }
  
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return new Response(JSON.stringify({ error: "Invalid JSON" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }
  
      const id = "chatcmpl-" + crypto.randomUUID();
      const created = Math.floor(Date.now() / 1000);
      const model = body.model || "echo-model";
      const echoContent = JSON.stringify(body, null, 2);
  
      // Non-streaming: return the whole received JSON as the message content
      if (!body.stream) {
        const responseObj = {
          id,
          object: "chat.completion",
          created,
          model,
          choices: [
            {
              index: 0,
              message: {
                role: "assistant",
                content: echoContent,
              },
              finish_reason: "stop",
            },
          ],
          usage: {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0,
          },
        };
        return new Response(JSON.stringify(responseObj), {
          headers: { "Content-Type": "application/json" },
        });
      }
  
      // Streaming: chunk the echoed JSON into SSE delta events
      const { readable, writable } = new TransformStream();
      const writer = writable.getWriter();
      const encoder = new TextEncoder();
  
      ctx.waitUntil(
        (async () => {
          const chunkSize = 40; // characters per SSE chunk
          try {
            for (let i = 0; i < echoContent.length; i += chunkSize) {
              const piece = echoContent.slice(i, i + chunkSize);
              const chunk = {
                id,
                object: "chat.completion.chunk",
                created,
                model,
                choices: [
                  {
                    index: 0,
                    delta: { content: piece },
                    finish_reason: null,
                  },
                ],
              };
              await writer.write(
                encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`)
              );
            }
  
            const finalChunk = {
              id,
              object: "chat.completion.chunk",
              created,
              model,
              choices: [
                { index: 0, delta: {}, finish_reason: "stop" },
              ],
            };
            await writer.write(
              encoder.encode(`data: ${JSON.stringify(finalChunk)}\n\n`)
            );
            await writer.write(encoder.encode("data: [DONE]\n\n"));
          } catch (err) {
            console.error(err);
          } finally {
            await writer.close();
          }
        })()
      );
  
      return new Response(readable, {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    },
  };