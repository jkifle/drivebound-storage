from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse, StreamingResponse

from app.models.asset import Asset
from app.models.user import User
from app.services.encryption import iter_decrypted_chunks
from app.services.ingestion import user_media_key


def _content_disposition(filename: str, disposition: str) -> str:
    if disposition not in {"attachment", "inline"}:
        raise ValueError("Unsupported content disposition")
    # Headers must be ASCII-safe, even when the original name is Unicode. Do
    # not carry control characters into either the header or its decoded name.
    filename = "".join(character for character in filename if ord(character) >= 32 and ord(character) != 127)
    filename = filename or "download"
    encoded = quote(filename, safe="")
    if encoded != filename:
        return f"{disposition}; filename*=utf-8''{encoded}"
    return f'{disposition}; filename="{filename}"'


def media_response(
    asset: Asset,
    owner: User,
    path: Path,
    *,
    media_type: str,
    filename: str | None = None,
    content_disposition_type: str = "attachment",
):
    """Return plaintext only after account authorization has already succeeded."""
    headers: dict[str, str] = {}
    if filename:
        headers["Content-Disposition"] = _content_disposition(filename, content_disposition_type)
    if not asset.encryption_version:
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            content_disposition_type=content_disposition_type,
            headers=headers,
        )
    if filename and media_type == asset.mime_type:
        headers["Content-Length"] = str(asset.file_size)
    return StreamingResponse(iter_decrypted_chunks(path, user_media_key(owner)), media_type=media_type, headers=headers)
