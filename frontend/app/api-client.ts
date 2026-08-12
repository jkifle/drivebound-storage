export const API_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export async function apiRequest(path: string, init?: RequestInit, retry = true): Promise<Response> {
  const url = path.startsWith("http") ? path : `${API_URL}${path}`;
  const response = await fetch(url, { ...init, credentials: "include" });
  if (response.status === 401 && retry && !path.includes("/auth/refresh") && !path.includes("/auth/login")) {
    const refreshed = await fetch(`${API_URL}/api/v1/auth/refresh`, { method: "POST", credentials: "include" });
    if (refreshed.ok) return apiRequest(path, init, false);
  }
  return response;
}
