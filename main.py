"""
Siembra Clara · API de clima
- /api/clima/historico : datos diarios medidos de NASA POWER (hasta 18 meses)
- /api/clima/pronostico: proyección estadística de los próximos N días (máx. 60):
      promedio histórico NASA POWER + anomalía de los últimos 30 días + El Niño / La Niña
- /            : sirve la app (static/index.html)

Variable de entorno opcional: CORS_ORIGINS
"""
from __future__ import annotations

import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

POWER = "https://power.larc.nasa.gov/api/temporal"
POWER_PARAMS = "T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,RH2M,ALLSKY_SFC_SW_DWN,ALLSKY_SFC_PAR_TOT,WS2M"
CR_OFFSET = timedelta(hours=-6)  # Costa Rica, sin horario de verano
LAPSE = 0.006  # °C por metro
MESES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

app = FastAPI(title="Siembra Clara · Clima")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------- caché simple en memoria ----------------
_cache: dict[str, tuple[float, Any]] = {}


def cached(key: str, ttl: int):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def store(key: str, val: Any):
    _cache[key] = (time.time(), val)
    return val


def ok(v) -> float | None:
    return None if v is None or v <= -900 else float(v)


# ---------------- NASA POWER ----------------
async def power_daily(lat: float, lon: float, start: date, end: date) -> dict:
    key = f"pd|{lat:.2f}|{lon:.2f}|{start}|{end}"
    if (c := cached(key, 6 * 3600)) is not None:
        return c
    P: dict[str, dict] = {}
    elev = None
    a = start
    async with httpx.AsyncClient(timeout=60) as cli:
        while a <= end:  # NASA POWER acepta máximo 366 días por consulta
            b = min(end, a + timedelta(days=365))
            url = (f"{POWER}/daily/point?parameters={POWER_PARAMS}&community=AG"
                   f"&longitude={lon}&latitude={lat}&start={a:%Y%m%d}&end={b:%Y%m%d}&format=JSON")
            r = await cli.get(url)
            if r.status_code != 200:
                raise HTTPException(502, f"NASA POWER respondió {r.status_code}: {r.text[:200]}")
            j = r.json()
            for k, v in j["properties"]["parameter"].items():
                P.setdefault(k, {}).update(v)
            if elev is None:
                elev = (j.get("geometry", {}).get("coordinates") or [0, 0, None])[2]
            a = b + timedelta(days=1)
    rows = []
    for k in sorted(P["T2M"].keys()):
        g = lambda p: ok(P[p].get(k))  # noqa: E731
        ws = g("WS2M")
        rows.append({
            "fecha": f"{k[:4]}-{k[4:6]}-{k[6:8]}",
            "lluvia": g("PRECTOTCORR"), "temp": g("T2M"), "tmin": g("T2M_MIN"), "tmax": g("T2M_MAX"),
            "hum": g("RH2M"), "rad": g("ALLSKY_SFC_SW_DWN"), "par": g("ALLSKY_SFC_PAR_TOT"),
            "viento": ws * 3.6 if ws is not None else None,
        })
    return store(key, {"elev": elev, "rows": rows})


async def power_climatology(lat: float, lon: float) -> dict:
    key = f"pc|{lat:.2f}|{lon:.2f}"
    if (c := cached(key, 7 * 86400)) is not None:
        return c
    url = (f"{POWER}/climatology/point?parameters={POWER_PARAMS}&community=AG"
           f"&longitude={lon}&latitude={lat}&format=JSON")
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.get(url)
    if r.status_code != 200:
        raise HTTPException(502, f"NASA POWER (climatología) respondió {r.status_code}")
    j = r.json()
    P = j["properties"]["parameter"]
    elev = (j.get("geometry", {}).get("coordinates") or [0, 0, None])[2]
    clim = []
    for m in MESES:  # valores diarios promedio de cada mes
        ws = ok(P["WS2M"][m])
        clim.append({"lluvia": ok(P["PRECTOTCORR"][m]), "temp": ok(P["T2M"][m]), "tmin": ok(P["T2M_MIN"][m]),
                     "tmax": ok(P["T2M_MAX"][m]), "hum": ok(P["RH2M"][m]), "rad": ok(P["ALLSKY_SFC_SW_DWN"][m]),
                     "par": ok(P["ALLSKY_SFC_PAR_TOT"][m]),
                     "viento": ws * 3.6 if ws is not None else None})
    return store(key, {"elev": elev, "clim": clim})


def adjust_alt(rows: list[dict], elev: float | None, alt: float | None) -> list[dict]:
    if elev is None or alt is None:
        return rows
    dT = (elev - alt) * LAPSE
    out = []
    for r in rows:
        r = dict(r)
        for k in ("temp", "tmin", "tmax"):
            if r.get(k) is not None:
                r[k] = round(r[k] + dT, 2)
        out.append(r)
    return out


# ---------------- Proyección estadística ----------------
ENSO = {  # factor de lluvia (mayo–noviembre / resto) y cambio de temperatura
    "nino": {"pacifico": (0.75, 0.9), "caribe": (1.15, 1.15), "dt": 0.5},
    "nina": {"pacifico": (1.2, 1.05), "caribe": (0.85, 0.85), "dt": -0.3},
}


def project(hist: list[dict], clim: list[dict], start: date, n: int, enso: str, vertiente: str) -> tuple[list[dict], dict]:
    def normal(d: date) -> dict:
        return clim[d.month - 1]

    last = [r for r in hist[-30:]]

    def anomaly(k: str, ratio: bool):
        a = b = c = 0.0
        for r in last:
            if r.get(k) is None:
                continue
            nv = normal(date.fromisoformat(r["fecha"])).get(k)
            if nv is None:
                continue
            a += r[k]; b += nv; c += 1
        if not c:
            return 1.0 if ratio else 0.0
        return max(0.4, min(1.8, a / b)) if ratio else (a - b) / c

    A = {"lluvia": anomaly("lluvia", True), "temp": anomaly("temp", False), "tmin": anomaly("tmin", False),
         "tmax": anomaly("tmax", False), "hum": anomaly("hum", False), "rad": anomaly("rad", True), "par": anomaly("par", True),
         "viento": anomaly("viento", True)}
    out = []
    for k in range(n):
        d = start + timedelta(days=k)
        w = math.exp(-k / 20)  # la anomalía reciente pierde peso con los días
        nv = dict(normal(d))
        if enso in ENSO:
            wet, dry = ENSO[enso][vertiente]
            nv["lluvia"] = (nv["lluvia"] or 0) * (wet if 5 <= d.month <= 11 else dry)
            for t in ("temp", "tmin", "tmax"):
                if nv[t] is not None:
                    nv[t] += ENSO[enso]["dt"]
        row = {"fecha": d.isoformat(), "fuente": "proyeccion"}
        for key in ("lluvia", "rad", "par", "viento"):
            row[key] = None if nv.get(key) is None else nv[key] * (1 + (A[key] - 1) * w)
        for key in ("temp", "tmin", "tmax", "hum"):
            row[key] = None if nv.get(key) is None else nv[key] + A[key] * w
        if row["hum"] is not None:
            row["hum"] = max(30, min(100, row["hum"]))
        out.append(row)
    return out, A


# ---------------- Endpoints ----------------
@app.get("/api/clima/historico")
async def historico(lat: float, lon: float, alt: float | None = None, dias: int = Query(548, le=1100)):
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=dias - 1)
    pd = await power_daily(lat, lon, start, end)
    return {"fuente": "NASA POWER", "desde": start.isoformat(), "hasta": end.isoformat(),
            "dias": adjust_alt(pd["rows"], pd["elev"], alt)}


@app.get("/api/clima/pronostico")
async def pronostico(lat: float, lon: float, alt: float | None = None, dias: int = Query(60, ge=1, le=60),
                     enso: str = "normal", vertiente: str = "pacifico"):
    end_hist = date.today() - timedelta(days=3)
    pd = await power_daily(lat, lon, end_hist - timedelta(days=45), end_hist)
    pc = await power_climatology(lat, lon)
    hist = adjust_alt(pd["rows"], pd["elev"], alt)
    clim = adjust_alt(pc["clim"], pc["elev"], alt)
    tomorrow = date.today() + timedelta(days=1)
    gap = (tomorrow - (end_hist + timedelta(days=1))).days
    proj, anom = project(hist, clim, end_hist + timedelta(days=1), gap + dias, enso, vertiente)
    out = proj
    return {"desde": proj[0]["fecha"], "hasta": proj[-1]["fecha"], "dias": out,
            "fuentes": {"proyeccion_dias": len(out)},
            "anomalia_30d": anom,
            "nota": "Proyección estadística con NASA POWER; no es un pronóstico del tiempo."}


STATIC = Path(__file__).parent / "static" / "index.html"


@app.get("/")
async def index():
    if STATIC.exists():
        return FileResponse(STATIC)
    raise HTTPException(404, "Copie la app en static/index.html")
