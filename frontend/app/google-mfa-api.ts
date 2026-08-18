import { apiRequest } from "./api-client";
import { apiProblemCode, apiProblemMessage } from "./api-problem";

export type GoogleMfaMode = "login" | "reauthenticate";

export class GoogleMfaCompletionError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "GoogleMfaCompletionError";
    this.status = status;
    this.code = code;
  }
}

export async function completeGoogleMfa(mode: GoogleMfaMode, code: string): Promise<void> {
  const response = await apiRequest(`/api/v1/auth/google/mfa/${mode}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  }, false);
  const body: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new GoogleMfaCompletionError(
      apiProblemMessage(body, "Google verification could not be completed."),
      response.status,
      apiProblemCode(body),
    );
  }
}
