export type TransferSession = {
  upload_url: string;
  offset: number;
  chunk_size: number;
  status: string;
  duplicate?: boolean;
};

type Request = (url: string, init: RequestInit) => Promise<Response>;

/** Receiving all bytes is not completion: an empty PATCH retries publication. */
export async function finishTransfer(
  file: Blob,
  initial: TransferSession,
  request: Request,
  onProgress: (session: TransferSession) => void,
  pause: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
): Promise<TransferSession> {
  let upload = { ...initial };
  if (upload.status === "complete" && upload.offset !== file.size) {
    throw new Error("The server reported completion before receiving the entire file.");
  }
  let stalledAttempts = 0;
  while (upload.status !== "complete") {
    if (upload.status !== "active") throw new Error(`Upload is ${upload.status}. Add the file again to restart.`);
    const offset = Number(upload.offset);
    const chunkSize = Number(upload.chunk_size);
    if (!Number.isSafeInteger(offset) || offset < 0 || offset > file.size || !Number.isSafeInteger(chunkSize) || chunkSize <= 0) {
      throw new Error("The server returned invalid upload progress.");
    }
    let response: Response | null = null;
    try {
      response = await request(upload.upload_url, {
        method: "PATCH",
        headers: { "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" },
        body: file.slice(offset, Math.min(offset + chunkSize, file.size)),
      });
    } catch { /* Recover authoritative progress after a lost response. */ }
    if (response?.ok) {
      upload = await response.json();
    } else {
      let status: Response | null = null;
      try { status = await request(upload.upload_url, { method: "HEAD" }); } catch { /* Retry below. */ }
      if (status?.ok) {
        const value = status.headers.get("Upload-Offset");
        if (value !== null) upload.offset = Number(value);
        upload.status = status.headers.get("Upload-Status") ?? upload.status;
      }
    }
    if (!upload || !Number.isSafeInteger(upload.offset) || upload.offset < offset || upload.offset > file.size) {
      throw new Error("The server returned invalid upload progress.");
    }
    onProgress(upload);
    if (upload.status === "complete") {
      if (upload.offset !== file.size) throw new Error("The server reported completion before receiving the entire file.");
      return upload;
    }
    if (upload.offset > offset) stalledAttempts = 0;
    else if (++stalledAttempts >= 3) {
      const problem = await response?.json().catch(() => null);
      throw new Error(typeof problem?.detail === "string" ? problem.detail :
        "Saving to your drive did not finish. Retry to resume; receiving all bytes does not mean the file is saved.");
    }
    if (!response?.ok || upload.offset === offset) await pause(500 * Math.max(1, stalledAttempts));
  }
  return upload;
}
