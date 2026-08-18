import { GoogleMfaCompletion } from "./google-mfa-completion";
import type { GoogleMfaMode } from "../../google-mfa-api";

export default async function GoogleMfaPage({ searchParams }: { searchParams: Promise<{ mode?: string }> }) {
  const { mode: value } = await searchParams;
  const mode: GoogleMfaMode | null = value === "login" || value === "reauthenticate" ? value : null;
  return <GoogleMfaCompletion mode={mode} />;
}
