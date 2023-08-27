from datetime import datetime
from typing import Optional, Annotated
from fastapi import APIRouter, Request, status, HTTPException, Depends, File, UploadFile
from fastapi_pagination.ext.sqlalchemy import paginate
from fastapi_pagination.cursor import CursorPage, CursorParams
from fastapi.responses import FileResponse
from pydantic import BaseModel, BaseConfig

from stat import S_IFREG
from stream_zip import ZIP_64, stream_zip  # type: ignore[import]

from logger.logger import log
from handler import dbh
from utils import fs, get_file_name_with_no_tags
from utils.fs import build_artwork_path, build_upload_roms_path
from exceptions.fs_exceptions import RomNotFoundError, RomAlreadyExistsException
from utils.oauth import protected_route
from models import Rom, Platform
from config import LIBRARY_BASE_PATH

from .utils import CustomStreamingResponse

router = APIRouter()


class RomSchema(BaseModel):
    id: int

    r_igdb_id: str
    p_igdb_id: str
    r_sgdb_id: str
    p_sgdb_id: str

    p_slug: str
    p_name: str

    file_name: str
    file_name_no_tags: str
    file_extension: str
    file_path: str
    file_size: float
    file_size_units: str

    r_name: str
    r_slug: str

    summary: str

    path_cover_s: str
    path_cover_l: str
    has_cover: bool
    url_cover: str

    region: str
    revision: str
    tags: list

    multi: bool
    files: list

    url_screenshots: list
    path_screenshots: list

    full_path: str
    download_path: str

    class Config(BaseConfig):
        orm_mode = True


@protected_route(router.get, "/platforms/{p_slug}/roms/{id}", ["roms.read"])
def rom(request: Request, id: int) -> RomSchema:
    """Returns one rom data of the desired platform"""

    return dbh.get_rom(id)


@protected_route(router.put, "/platforms/{p_slug}/roms/upload", ["roms.write"])
def upload_roms(request: Request, p_slug: str, roms: list[UploadFile] = File(...)):
    platform_fs_slug = dbh.get_platform(p_slug).fs_slug
    log.info(f"Uploading files to: {platform_fs_slug}")
    if roms is not None:
        roms_path = build_upload_roms_path(platform_fs_slug)
        for rom in roms:
            log.info(f" - Uploading {rom.filename}")
            file_location = f"{roms_path}/{rom.filename}"
            with open(file_location, "wb+") as file_object:
                file_object.write(rom.file.read())
        dbh.update_n_roms(p_slug)
        return {"msg": f"{len(roms)} roms uploaded successfully!"}


@protected_route(router.get, "/platforms/{p_slug}/roms/{id}/download", ["roms.read"])
def download_rom(request: Request, id: int, files: str):
    """Downloads a rom or a zip file with multiple roms"""
    rom = dbh.get_rom(id)
    rom_path = f"{LIBRARY_BASE_PATH}/{rom.full_path}"

    if not rom.multi:
        return FileResponse(path=rom_path, filename=rom.file_name)

    # Builds a generator of tuples for each member file
    def local_files():
        def contents(file_name):
            with open(f"{rom_path}/{file_name}", "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return [
            (file_name, datetime.now(), S_IFREG | 0o600, ZIP_64, contents(file_name))
            for file_name in files.split(",")
        ]

    zipped_chunks = stream_zip(local_files())

    # Streams the zip file to the client
    return CustomStreamingResponse(
        zipped_chunks,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={rom.r_name}.zip"},
        emit_body={"id": rom.id},
    )


@protected_route(router.get, "/platforms/{p_slug}/roms", ["roms.read"])
def roms(
    request: Request,
    p_slug: str,
    size: int = 60,
    cursor: str = "",
    search_term: str = "",
) -> CursorPage[RomSchema]:
    """Returns all roms of the desired platform"""
    with dbh.session.begin() as session:
        cursor_params = CursorParams(size=size, cursor=cursor)
        qq = dbh.get_roms(p_slug)

        if search_term:
            return paginate(
                session,
                qq.filter(Rom.file_name.ilike(f"%{search_term}%")),
                cursor_params,
            )

        return paginate(session, qq, cursor_params)


class RomUpdateForm:
    def __init__(
        self,
        r_name: Optional[str] = None,
        file_name: Optional[str] = None,
        summary: Optional[str] = None,
        artwork: Optional[UploadFile] = File(None)
    ):
        self.r_name = r_name
        self.file_name = file_name
        self.summary = summary
        self.artwork = artwork


@protected_route(router.patch, "/platforms/{p_slug}/roms/{id}", ["roms.write"])
async def update_rom(
    request: Request,
    p_slug: str,
    id: int,
    form_data: Annotated[RomUpdateForm, Depends()],
):
    """Updates rom details"""

    db_rom: Rom = dbh.get_rom(id)
    db_platform: Platform = dbh.get_platform(p_slug)

    cleaned_data = {}
    cleaned_data["r_name"] = form_data.r_name
    cleaned_data["file_name"] = form_data.file_name
    cleaned_data["summary"] = form_data.summary

    try:
        if cleaned_data["file_name"] != db_rom.file_name:
            fs.rename_rom(db_platform.fs_slug, db_rom.file_name, cleaned_data["file_name"])
    except RomAlreadyExistsException as e:
        log.error(str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )

    cleaned_data["file_name_no_tags"] = get_file_name_with_no_tags(cleaned_data["file_name"])
    cleaned_data.update(
        fs.get_cover(
            overwrite=True,
            p_slug=db_platform.slug,
            r_name=cleaned_data["file_name_no_tags"],
            url_cover=cleaned_data.get("url_cover", ""),
        )
    )
    cleaned_data.update(
        fs.get_screenshots(
            p_slug=db_platform.slug,
            r_name=cleaned_data["file_name_no_tags"],
            url_screenshots=cleaned_data.get("url_screenshots", ""),
        ),
    )

    if form_data.artwork is not None:
        file_ext = form_data.artwork.filename.split('.')[-1]
        path_cover_l, path_cover_s, artwork_path = build_artwork_path(form_data.r_name, db_platform.fs_slug, file_ext)
        cleaned_data['path_cover_l'] = path_cover_l
        cleaned_data['path_cover_s'] = path_cover_s
        file_location_l = f"{artwork_path}/big.{file_ext}"
        file_location_s = f"{artwork_path}/small.{file_ext}"
        artwork_file = form_data.artwork.file.read()
        with open(file_location_s, "wb+") as artwork_s:
            artwork_s.write(artwork_file)
        with open(file_location_l, "wb+") as artwork_l:
            artwork_l.write(artwork_file)

    dbh.update_rom(id, cleaned_data)

    return {
        "rom": db_rom,
        "msg": f"Rom updated successfully!",
    }


def _delete_single_rom(rom_id: int, p_slug: str, filesystem: bool = False):
    rom = dbh.get_rom(rom_id)
    if not rom:
        error = f"Rom with id {rom_id} not found"
        log.error(error)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error)

    log.info(f"Deleting {rom.file_name} from database")
    dbh.delete_rom(rom_id)

    if filesystem:
        log.info(f"Deleting {rom.file_name} from filesystem")
        try:
            platform: Platform = dbh.get_platform(p_slug)
            fs.remove_rom(platform.fs_slug, rom.file_name)
        except RomNotFoundError as e:
            error = f"Couldn't delete from filesystem: {str(e)}"
            log.error(error)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error)

    return rom


@protected_route(router.delete, "/platforms/{p_slug}/roms/{id}", ["roms.write"])
def delete_rom(
    request: Request, p_slug: str, id: int, filesystem: bool = False
) -> dict:
    """Detele rom from database [and filesystem]"""

    rom = _delete_single_rom(id, p_slug, filesystem)
    dbh.update_n_roms(p_slug)

    return {"msg": f"{rom.file_name} deleted successfully!"}


@protected_route(router.post, "/platforms/{p_slug}/roms/delete", ["roms.write"])
async def mass_delete_roms(
    request: Request,
    p_slug: str,
    filesystem: bool = False,
) -> dict:
    """Detele multiple roms from database [and filesystem]"""

    data: dict = await request.json()
    roms_ids: list = data["roms"]

    for rom_id in roms_ids:
        _delete_single_rom(rom_id, p_slug, filesystem)

    dbh.update_n_roms(p_slug)

    return {"msg": f"{len(roms_ids)} roms deleted successfully!"}
