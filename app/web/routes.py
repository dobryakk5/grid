from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))


@router.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={})


@router.get("/profiles/new")
async def profile_editor(request: Request):
    return templates.TemplateResponse(request=request, name="profile_form.html", context={})


@router.get("/analysis")
async def grid_analysis_page(request: Request):
    return templates.TemplateResponse(request=request, name="grid_analysis.html", context={})


@router.get("/fomo")
async def fomo_analysis_page(request: Request):
    return templates.TemplateResponse(request=request, name="fomo_activity.html", context={})


@router.get("/positions")
async def dex_positions_page(request: Request):
    return templates.TemplateResponse(request=request, name="dex_positions.html", context={})


@router.get("/history")
async def dex_history_page(request: Request):
    return templates.TemplateResponse(request=request, name="dex_history.html", context={})


@router.get("/fomo/chain")
async def fomo_chain_page(request: Request):
    return templates.TemplateResponse(request=request, name="fomo_analysis.html", context={})


@router.get("/fomo/coin/{token_address}")
async def fomo_coin_page(request: Request, token_address: str):
    return templates.TemplateResponse(request=request, name="fomo_coin.html", context={})
