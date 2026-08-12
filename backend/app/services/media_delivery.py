from pathlib import Path

from fastapi.responses import FileResponse, StreamingResponse

from app.models.asset import Asset
from app.models.user import User
from app.services.encryption import iter_decrypted_chunks
from app.services.ingestion import user_media_key


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
    if not asset.encryption_version:
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            content_disposition_type=content_disposition_type,
        )
    headers: dict[str, str] = {}
    if filename:
        safe_filename = filename.replace('"', "")
        headers["Content-Disposition"] = f'{content_disposition_type}; filename="{safe_filename}"'
    if filename and media_type == asset.mime_type:
        headers["Content-Length"] = str(asset.file_size)
    return StreamingResponse(iter_decrypted_chunks(path, user_media_key(owner)), media_type=media_type, headers=headers)
