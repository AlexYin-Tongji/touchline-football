export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new Error(
      response.ok
        ? "服务返回了无法读取的内容。"
        : "网站暂时没有响应，请稍后重新连接。",
    );
  }
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("touchline-auth-required"));
    const detail = (value as { detail?: unknown })?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((x) => x.msg || "输入内容不符合要求").join("；")
          : `请求失败（${response.status}）`;
    throw new ApiError(message, response.status);
  }
  return value as T;
}
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "操作没有完成，请稍后重试。";
