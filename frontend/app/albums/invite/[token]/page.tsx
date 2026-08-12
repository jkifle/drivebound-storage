import { AlbumInvitation } from "./invitation";
export default async function Page({ params }: { params: Promise<{ token: string }> }) { return <AlbumInvitation token={(await params).token} />; }
