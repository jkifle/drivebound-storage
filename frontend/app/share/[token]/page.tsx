import { ShareView } from "./share-view";

export default async function SharedAssetPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <ShareView token={token} />;
}
