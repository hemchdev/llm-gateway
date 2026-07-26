const gatewayUrl = process.env.GATEWAY_INTERNAL_URL || process.env.NEXT_PUBLIC_GATEWAY_URL || "http://backend:8000";

const forwardedHeaders = [
  "authorization",
  "content-type",
  "x-admin-key",
  "x-feature",
  "x-request-id",
  "x-tenant-id"
];

async function proxy(request, context) {
  const params = await context.params;
  const path = (params.path || []).join("/");
  const url = new URL(request.url);
  const target = new URL(`/${path}${url.search}`, gatewayUrl);
  const headers = new Headers();

  for (const header of forwardedHeaders) {
    const value = request.headers.get(header);
    if (value) {
      headers.set(header, value);
    }
  }

  const init = {
    method: request.method,
    headers,
    cache: "no-store"
  };

  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = await request.text();
  }

  const response = await fetch(target, init);
  const body = await response.arrayBuffer();
  const responseHeaders = new Headers();
  const contentType = response.headers.get("content-type");
  if (contentType) {
    responseHeaders.set("content-type", contentType);
  }

  return new Response(body, {
    status: response.status,
    headers: responseHeaders
  });
}

export const dynamic = "force-dynamic";
export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
