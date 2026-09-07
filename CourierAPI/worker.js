const ORIGIN = "https://courier-api-bq5c.onrender.com";

export default {
  async fetch(request) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "Authorization,Content-Type,Accept,X-Blink-App-Role",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const incomingUrl = new URL(request.url);
    const upstreamUrl = new URL(incomingUrl.pathname + incomingUrl.search, ORIGIN);

    const headers = new Headers();
    const authorization = request.headers.get("Authorization");
    const contentType = request.headers.get("Content-Type");
    const accept = request.headers.get("Accept");
    const appRole = request.headers.get("X-Blink-App-Role");

    if (authorization) headers.set("Authorization", authorization);
    if (contentType) headers.set("Content-Type", contentType);
    if (accept) headers.set("Accept", accept);
    if (appRole) headers.set("X-Blink-App-Role", appRole);
    headers.set("X-Forwarded-Host", incomingUrl.host);
    headers.set("X-Forwarded-Proto", "https");

    const init = {
      method: request.method,
      headers,
      redirect: "follow",
    };

    if (!["GET", "HEAD"].includes(request.method)) {
      init.body = request.body;
    }

    const response = await fetch(upstreamUrl, init);
    const responseHeaders = new Headers(response.headers);
    for (const [key, value] of Object.entries(corsHeaders)) {
      responseHeaders.set(key, value);
    }

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  },
};
