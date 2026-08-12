import { AuthPage } from "./auth-page";

export default async function AuthenticationPage({ searchParams }: { searchParams: Promise<{ mode?: string; error?: string }> }) {
  const { mode, error } = await searchParams;
  return <AuthPage initialMode={mode === "register" ? "register" : "login"} initialError={error ?? null} />;
}
