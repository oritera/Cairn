export async function postJson(url, token, payload, options = {}) {
  const timeoutMs = Number(options.timeoutMs ?? 2000);
  const maxPayloadBytes = Number(options.maxPayloadBytes ?? 65536);
  const body = JSON.stringify(payload);
  const bytes = Buffer.byteLength(body, "utf8");
  if (bytes > maxPayloadBytes) {
    throw new Error(`payload_too_large:${bytes}`);
  }

  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const signal = options.signal && typeof AbortSignal.any === "function"
    ? AbortSignal.any([options.signal, timeoutSignal])
    : timeoutSignal;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-Cairn-Safety-Token": token,
    },
    body,
    signal,
  });
  if (!response.ok) {
    throw new Error(`safety_http_${response.status}`);
  }
  const value = await response.json();
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_safety_response");
  }
  return value;
}
