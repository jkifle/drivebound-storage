import { TokenAction } from "../token-action";
export default async function VerifyEmailPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const { token } = await searchParams;
  return <TokenAction kind="verify" token={token ?? ""} />;
}
