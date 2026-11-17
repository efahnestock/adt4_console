"""Pick results: the open "did the robot pick it up?" questions, their images,
and the operator's answer (picked / retry / give_up)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from ..pick_result_service import PickResultService
from . import services

router = APIRouter(prefix="/api/pick_result")
NO_STORE = {"Cache-Control": "no-store"}


def _svc(request: Request) -> PickResultService:
    svc = services(request).pick_result
    if svc is None:
        raise HTTPException(503, "pick results are not configured")
    return svc


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: Literal["picked", "retry", "give_up"]


@router.get("/pending")
def pending(request: Request):
    return _svc(request).pending()


@router.get("/requests/{identifier}/images/{index}")
def image(identifier: str, index: int, request: Request):
    data, media = _svc(request).get_image(identifier, index)
    return Response(data, media_type=media, headers=NO_STORE)


@router.post("/requests/{identifier}/respond")
def respond(identifier: str, body: AnswerBody, request: Request):
    return _svc(request).respond(identifier, body.result)
