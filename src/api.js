// Local bridge requests must not leave the UI waiting forever when a sensor or
// the Python process is stuck. Starting a resumed pass can legitimately take
// longer because its saved keyframes are replayed.
export function requestTimeout(path) {
  if (path === "/api/health") return 3000;
  if (path.startsWith("/api/frame")) return 7000;
  if (path === "/api/scan/start" || path === "/api/scan/passes/select") return 120000;
  return 15000;
}

export async function apiRequest(path, options = {}) {
  // signal позволяет вызывающему отменить запрос (например, при уходе из
  // стереорежима или скрытии окна), не дожидаясь таймаута.
  const { timeoutMs = requestTimeout(path), signal: externalSignal, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const forwardAbort = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener("abort", forwardAbort, { once: true });
  }
  try {
    const response = await fetch(path, {
      cache: "no-store",
      ...fetchOptions,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(fetchOptions.headers || {}),
      },
      body: fetchOptions.body && typeof fetchOptions.body !== "string"
        ? JSON.stringify(fetchOptions.body)
        : fetchOptions.body,
    });
    let payload = {};
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        const value = await response.json();
        if (value && typeof value === "object") payload = value;
      } catch (error) {
        if (controller.signal.aborted) throw error;
      }
    }
    if (!response.ok) {
      const base = payload.error || (response.status >= 500
        ? "Python-мост Depth Studio недоступен. Деморежим работает без него; чтобы подключить датчик, завершите настройку среды Depth Studio."
        : `Ошибка локального моста (${response.status}).`);
      const hint = typeof payload.hint === "string" && payload.hint ? ` ${payload.hint}` : "";
      const error = new Error(`${base}${hint}`);
      error.code = payload.code || "";
      error.hint = payload.hint || "";
      error.isBridgeResponseError = true;
      throw error;
    }
    return payload;
  } catch (error) {
    if (controller.signal.aborted) {
      const timeout = new Error(`Python-мост не ответил за ${Math.ceil(timeoutMs / 1000)} с. Откройте журнал моста и повторите; операция может ещё выполняться.`);
      timeout.code = "timeout";
      throw timeout;
    }
    if (error?.isBridgeResponseError) throw error;
    throw new Error("Python-мост Depth Studio недоступен. Деморежим работает без него; чтобы подключить датчик, завершите настройку среды Depth Studio.");
  } finally {
    clearTimeout(timer);
    if (externalSignal && typeof externalSignal.removeEventListener === "function") {
      externalSignal.removeEventListener("abort", forwardAbort);
    }
  }
}
