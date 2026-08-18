export type Base64Url = string;

export type CreationOptionsJSON = Omit<PublicKeyCredentialCreationOptions, "challenge" | "user" | "excludeCredentials"> & {
  challenge: Base64Url;
  user: Omit<PublicKeyCredentialUserEntity, "id"> & { id: Base64Url };
  excludeCredentials?: Array<Omit<PublicKeyCredentialDescriptor, "id"> & { id: Base64Url }>;
};

export type RequestOptionsJSON = Omit<PublicKeyCredentialRequestOptions, "challenge" | "allowCredentials"> & {
  challenge: Base64Url;
  allowCredentials?: Array<Omit<PublicKeyCredentialDescriptor, "id"> & { id: Base64Url }>;
};

export type RegistrationCredentialJSON = {
  id: string;
  rawId: Base64Url;
  type: PublicKeyCredential["type"];
  authenticatorAttachment: string | null;
  clientExtensionResults: AuthenticationExtensionsClientOutputs;
  response: {
    attestationObject: Base64Url;
    clientDataJSON: Base64Url;
    transports: string[];
  };
};

export type AuthenticationCredentialJSON = {
  id: string;
  rawId: Base64Url;
  type: PublicKeyCredential["type"];
  authenticatorAttachment: string | null;
  clientExtensionResults: AuthenticationExtensionsClientOutputs;
  response: {
    authenticatorData: Base64Url;
    clientDataJSON: Base64Url;
    signature: Base64Url;
    userHandle: Base64Url | null;
  };
};

export function base64UrlToArrayBuffer(value: Base64Url): ArrayBuffer {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(normalized.length + ((4 - normalized.length % 4) % 4), "=");
  const decoded = window.atob(padded);
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index += 1) bytes[index] = decoded.charCodeAt(index);
  return bytes.buffer;
}

export function arrayBufferToBase64Url(value: ArrayBuffer): Base64Url {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + 0x8000, bytes.length)));
  }
  return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/u, "");
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined"
    && window.isSecureContext
    && typeof PublicKeyCredential !== "undefined"
    && typeof navigator.credentials?.create === "function"
    && typeof navigator.credentials?.get === "function";
}

export async function platformAuthenticatorAvailable(): Promise<boolean> {
  if (!passkeysSupported() || typeof PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable !== "function") return false;
  try {
    return await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

export function decodeCreationOptions(options: CreationOptionsJSON): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: base64UrlToArrayBuffer(options.challenge),
    user: { ...options.user, id: base64UrlToArrayBuffer(options.user.id) },
    excludeCredentials: options.excludeCredentials?.map((credential) => ({
      ...credential,
      id: base64UrlToArrayBuffer(credential.id),
    })),
  };
}

export function decodeRequestOptions(options: RequestOptionsJSON): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: base64UrlToArrayBuffer(options.challenge),
    allowCredentials: options.allowCredentials?.map((credential) => ({
      ...credential,
      id: base64UrlToArrayBuffer(credential.id),
    })),
  };
}

export async function createPasskey(options: CreationOptionsJSON): Promise<RegistrationCredentialJSON> {
  if (!passkeysSupported()) throw new DOMException("Passkeys are unavailable in this browser or context.", "NotSupportedError");
  const credential = await navigator.credentials.create({ publicKey: decodeCreationOptions(options) });
  if (!(credential instanceof PublicKeyCredential) || !(credential.response instanceof AuthenticatorAttestationResponse)) {
    throw new DOMException("The authenticator returned an unexpected credential.", "UnknownError");
  }
  return {
    id: credential.id,
    rawId: arrayBufferToBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      attestationObject: arrayBufferToBase64Url(credential.response.attestationObject),
      clientDataJSON: arrayBufferToBase64Url(credential.response.clientDataJSON),
      transports: credential.response.getTransports?.() ?? [],
    },
  };
}

export async function getPasskey(options: RequestOptionsJSON): Promise<AuthenticationCredentialJSON> {
  if (!passkeysSupported()) throw new DOMException("Passkeys are unavailable in this browser or context.", "NotSupportedError");
  const credential = await navigator.credentials.get({ publicKey: decodeRequestOptions(options) });
  if (!(credential instanceof PublicKeyCredential) || !(credential.response instanceof AuthenticatorAssertionResponse)) {
    throw new DOMException("The authenticator returned an unexpected credential.", "UnknownError");
  }
  return {
    id: credential.id,
    rawId: arrayBufferToBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      authenticatorData: arrayBufferToBase64Url(credential.response.authenticatorData),
      clientDataJSON: arrayBufferToBase64Url(credential.response.clientDataJSON),
      signature: arrayBufferToBase64Url(credential.response.signature),
      userHandle: credential.response.userHandle ? arrayBufferToBase64Url(credential.response.userHandle) : null,
    },
  };
}

export function describePasskeyError(reason: unknown): string {
  if (!(reason instanceof DOMException)) return reason instanceof Error ? reason.message : "The passkey request could not be completed.";
  if (reason.name === "NotAllowedError" || reason.name === "AbortError") return "Passkey use was cancelled or the challenge expired. Start again when you are ready.";
  if (reason.name === "InvalidStateError") return "This passkey is already registered to the account. Use it to sign in or choose another authenticator.";
  if (reason.name === "NotSupportedError") return "Passkeys are not supported by this browser or secure connection. Use password or Google sign-in instead.";
  if (reason.name === "SecurityError") return "This passkey request does not match the current Drivebound address. Reload the correct secure site and try again.";
  if (reason.name === "ConstraintError") return "This authenticator cannot satisfy Drivebound's verification requirements. Try another passkey device.";
  return reason.message || "The authenticator could not complete the passkey request.";
}
