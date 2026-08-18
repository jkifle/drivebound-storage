export type ApiProblem = {
  detail?: string | { code?: string; message?: string };
  message?: string;
};

export function apiProblemMessage(value: unknown, fallback: string): string {
  if (!value || typeof value !== "object") return fallback;
  const body = value as ApiProblem;
  if (typeof body.detail === "string") return body.detail;
  if (body.detail && typeof body.detail === "object" && typeof body.detail.message === "string") {
    return body.detail.message;
  }
  return typeof body.message === "string" ? body.message : fallback;
}

export function apiProblemCode(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const detail = (value as ApiProblem).detail;
  return detail && typeof detail === "object" && typeof detail.code === "string" ? detail.code : null;
}
