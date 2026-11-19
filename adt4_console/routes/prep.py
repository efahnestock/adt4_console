"""``/api/prep`` -- mission-prep edit files on the live map (mission_prep.service).

``GET /api/prep`` the files applied to the current map; ``POST /api/prep/check`` resolve
+ dry run (nothing written); ``POST /api/prep/apply`` commit one file as one map edit;
``POST /api/prep/reset`` undo the applied files (``mode: files``, confirm "reset") or
reseed the prior (``mode: prior``, confirm "reseed").
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..mission_prep.core import PrepError
from . import services

router = APIRouter(prefix="/api/prep")


class FileBody(BaseModel):
    file: dict[str, Any]
    src: str | None = None


class ResetBody(BaseModel):
    mode: str = "files"
    confirm: str = ""
    src: str | None = None


def _svc(request: Request):
    svc = getattr(services(request), "prep", None)
    if svc is None:
        raise HTTPException(503, "mission-prep files are not available")
    return svc


def _call(fn, *a, **kw):
    from ..edit_service import EditUnavailable
    from ..graph_service import GraphUnavailable, NotFound

    try:
        return fn(*a, **kw)
    except PrepError as e:
        raise HTTPException(400, str(e)) from None
    except NotFound as e:
        raise HTTPException(404, str(e)) from None
    except (EditUnavailable, GraphUnavailable) as e:
        raise HTTPException(503, str(e)) from None


@router.get("")
def status(request: Request, src: str | None = None):
    return _call(_svc(request).status, src)


@router.get("/files")
def files(request: Request):
    """The mission-prep files on the base station (``settings.prep_dir``)."""
    return _call(_svc(request).list_files)


@router.get("/file")
def file(path: str, request: Request):
    return _call(_svc(request).read_file, path)


@router.post("/check")
def check(body: FileBody, request: Request):
    return _call(_svc(request).check, body.file, body.src)


@router.post("/apply")
def apply(body: FileBody, request: Request):
    return _call(_svc(request).apply, body.file, body.src, via="ui")


@router.post("/reset")
def reset(body: ResetBody, request: Request):
    svc = _svc(request)
    out = _call(svc.reset, body.mode, body.confirm, body.src)
    if out.get("ok"):
        forget_launch_prep(services(request))
    return out


def forget_launch_prep(svc) -> None:
    """After a reset the Launch form's remembered files go too, so a relaunch does
    not put them back."""
    mem = getattr(svc, "launch_memory", None)
    if mem is not None and hasattr(mem, "clear_field"):
        mem.clear_field("prep")
