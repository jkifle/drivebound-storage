import { apiRequest } from "./api-client";
import { apiProblemCode, apiProblemMessage, type ApiProblem } from "./api-problem";
import type {
  AuthenticationCredentialJSON,
  CreationOptionsJSON,
  RegistrationCredentialJSON,
  RequestOptionsJSON,
} from "./webauthn";

export type PasskeyRecord = {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
};

type OptionsEnvelope<T> = { challenge_id: string; public_key: T };

export class PasskeyApiError extends Error {
  status: number;
  code: string | null;
  reauthenticationRequired: boolean;

  constructor(message: string, status: number, code: string | null = null, reauthenticationRequired = false) {
    super(message === "Passkey verification failed" ? "The passkey could not be verified, or its challenge expired. Start again and retry." : message);
    this.name = "PasskeyApiError";
    this.status = status;
    this.code = code;
    this.reauthenticationRequired = reauthenticationRequired;
  }
}

function hasReauthenticationHeader(response: Response): boolean {
  return response.headers.get("X-Drivebound-Reauthentication")?.toLowerCase() === "required";
}

async function responseBody(response: Response): Promise<ApiProblem> {
  return response.json().catch(() => ({}));
}

async function expectJson<T>(response: Response, fallback: string): Promise<T> {
  const body = await responseBody(response);
  if (!response.ok) {
    throw new PasskeyApiError(apiProblemMessage(body, fallback), response.status, apiProblemCode(body), hasReauthenticationHeader(response));
  }
  return body as T;
}

async function expectEmpty(response: Response, fallback: string): Promise<void> {
  if (response.ok) return;
  const body = await responseBody(response);
  throw new PasskeyApiError(apiProblemMessage(body, fallback), response.status, apiProblemCode(body), hasReauthenticationHeader(response));
}

export async function apiErrorFromResponse(response: Response, fallback: string): Promise<PasskeyApiError> {
  const body = await responseBody(response);
  return new PasskeyApiError(apiProblemMessage(body, fallback), response.status, apiProblemCode(body), hasReauthenticationHeader(response));
}

function jsonRequest(body: object): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export function recentAuthenticationRequired(reason: unknown): reason is PasskeyApiError {
  return reason instanceof PasskeyApiError && reason.status === 428 && (reason.code === "recent_auth_required" || reason.reauthenticationRequired);
}

export async function beginPasskeyLogin(email?: string): Promise<OptionsEnvelope<RequestOptionsJSON>> {
  const normalized = email?.trim();
  const init: RequestInit = normalized ? jsonRequest({ email: normalized }) : { method: "POST" };
  return expectJson(await apiRequest("/api/v1/auth/passkeys/login/options", init, false), "Passkey sign-in could not be started.");
}

export async function finishPasskeyLogin(challengeId: string, credential: AuthenticationCredentialJSON): Promise<void> {
  await expectJson(await apiRequest("/api/v1/auth/passkeys/login/verify", jsonRequest({ challenge_id: challengeId, credential }), false), "This passkey could not sign in.");
}

export async function listPasskeys(): Promise<PasskeyRecord[]> {
  return expectJson(await apiRequest("/api/v1/auth/passkeys"), "Passkeys could not be loaded.");
}

export async function beginPasskeyRegistration(): Promise<OptionsEnvelope<CreationOptionsJSON>> {
  return expectJson(await apiRequest("/api/v1/auth/passkeys/register/options", { method: "POST" }), "Passkey enrollment could not be started.");
}

export async function finishPasskeyRegistration(challengeId: string, credential: RegistrationCredentialJSON, name: string): Promise<PasskeyRecord> {
  return expectJson(await apiRequest("/api/v1/auth/passkeys/register/verify", jsonRequest({ challenge_id: challengeId, credential, name })), "This passkey could not be enrolled.");
}

export async function renamePasskey(credentialId: string, name: string): Promise<PasskeyRecord> {
  return expectJson(await apiRequest(`/api/v1/auth/passkeys/${credentialId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }), "This passkey could not be renamed.");
}

export async function removePasskey(credentialId: string): Promise<void> {
  await expectEmpty(await apiRequest(`/api/v1/auth/passkeys/${credentialId}`, { method: "DELETE" }), "This passkey could not be removed.");
}

export async function reauthenticateWithPassword(password: string, otp?: string): Promise<void> {
  await expectJson(await apiRequest("/api/v1/auth/reauthenticate/password", jsonRequest({ password, otp }), false), "Your identity could not be verified.");
}

export async function beginPasskeyReauthentication(): Promise<OptionsEnvelope<RequestOptionsJSON>> {
  return expectJson(await apiRequest("/api/v1/auth/reauthenticate/passkey/options", { method: "POST" }), "Passkey verification could not be started.");
}

export async function finishPasskeyReauthentication(challengeId: string, credential: AuthenticationCredentialJSON): Promise<void> {
  await expectJson(await apiRequest("/api/v1/auth/reauthenticate/passkey/verify", jsonRequest({ challenge_id: challengeId, credential })), "This passkey could not verify your identity.");
}
