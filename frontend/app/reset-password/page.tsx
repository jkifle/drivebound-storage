import { TokenAction } from "../token-action";
export default async function ResetPasswordPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const { token } = await searchParams;
  return <TokenAction kind="reset" token={token ?? ""} />;
}
