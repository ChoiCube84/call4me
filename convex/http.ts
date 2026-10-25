import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { answer, callPath, calls, companies, metrics, tool, webhook } from "./phone";

const http = httpRouter();

http.route({
  path: "/health",
  method: "GET",
  handler: httpAction(async () => Response.json({ status: "ok" })),
});

http.route({ path: "/calls", method: "GET", handler: calls });
http.route({ path: "/calls", method: "POST", handler: calls });
http.route({ pathPrefix: "/calls/", method: "GET", handler: callPath });
http.route({ pathPrefix: "/calls/", method: "POST", handler: callPath });
http.route({ pathPrefix: "/requests/", method: "POST", handler: answer });
http.route({ path: "/companies", method: "GET", handler: companies });
http.route({ path: "/metrics", method: "GET", handler: metrics });
http.route({ path: "/cartesia/webhook", method: "POST", handler: webhook });
http.route({ pathPrefix: "/cartesia/tools/", method: "POST", handler: tool });

// The static Next.js export is uploaded to Convex file storage during deployment.
// Serving it from the same .convex.site origin as the API avoids another host.
http.route({
  pathPrefix: "/",
  method: "GET",
  handler: httpAction(async (ctx, request) => {
    const requested = new URL(request.url).pathname;
    const path = requested === "/" ? "/index.html"
      : requested.endsWith("/") ? `${requested}index.html` : requested;
    const asset = await ctx.runQuery(internal.site.asset, { path });
    if (!asset) return new Response("Not found", { status: 404 });
    const blob = await ctx.storage.get(asset.storage_id);
    if (!blob) return new Response("Not found", { status: 404 });
    return new Response(blob, {
      headers: {
        "Content-Type": asset.content_type,
        "Cache-Control": path.endsWith(".html") || path.endsWith(".txt")
          ? "no-cache" : "public, max-age=31536000, immutable",
      },
    });
  }),
});

export default http;
