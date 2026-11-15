"""Trials: start / stop a recording on the whole fleet, flags (API.md §23)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..trials import TrialError
from . import services

router = APIRouter(prefix="/api/trials")


def _trials(request: Request):
    svc = services(request).trials
    if svc is None:
        raise HTTPException(503, "trial recording is not set up in this console")
    return svc


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except TrialError as e:
        raise HTTPException(e.status, e.detail) from e


@router.get("")
def state(request: Request):
    """The active trial (machines, their state and the age of their last
    answer) and the last finished one."""
    return _trials(request).payload()


@router.get("/preflight")
def preflight(request: Request):
    """Per machine: can it record, free disk, minutes at its last trial's rate."""
    return _call(_trials(request).preflight)


@router.get("/events")
def events(request: Request, n: int = 50, trial: str | None = None):
    """The last ``n`` lines of the (active, else last) trial's events.jsonl."""
    return {"events": _trials(request).events_tail(n, trial)}


class StartBody(BaseModel):
    description: str
    machines: list[str] | None = None  # None: every online robot and this machine


@router.post("/start")
def start(body: StartBody, request: Request):
    return _call(_trials(request).start, body.description, body.machines)


class FlagBody(BaseModel):
    text: str


@router.post("/flag")
def flag(body: FlagBody, request: Request):
    """A timestamped operator note in the trial's event log."""
    return {"ok": True, "event": _call(_trials(request).flag, body.text)}


class UiBody(BaseModel):
    events: list


@router.post("/ui")
def ui(body: UiBody, request: Request):
    """Operator interactions on the page (clicks, page changes, dialogs),
    logged as ``ui`` events while a trial is active."""
    return {"logged": _trials(request).log_ui(body.events)}


class StopBody(BaseModel):
    force: bool = (
        False  # a second SIGINT then SIGTERM for a recorder that will not exit
    )


@router.post("/stop")
def stop(request: Request, body: StopBody | None = None):
    return _call(_trials(request).stop, bool(body and body.force))


@router.post("/close")
def close(request: Request):
    """End a stopping trial although a machine was not confirmed stopped."""
    return _call(_trials(request).close)


@router.post("/machines/{name}/start")
def start_machine(name: str, request: Request):
    """Retry one machine in the active trial."""
    return _call(_trials(request).start_machine, name)
