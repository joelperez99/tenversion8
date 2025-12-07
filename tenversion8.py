# -*- coding: utf-8 -*-
# tennis_ai_plus_streamlit.py — Momios sintéticos (api-tennis.com) en Streamlit
# - Batch por múltiples match_key
# - Exportación a Excel (solo manual, SIN auto-guardado)
# - FIX: búsqueda por match_key con ventanas pequeñas (y fecha estimada opcional)
# - UI en Streamlit con log, progreso y velocidad promedio por match
# - Guarda ganador y marcador final de sets (JSON y Excel)
# - Columna "Acerto pronostico" en Excel (Si/No/"")
# - Integra cuotas Bet365 (ganador partido Home/Away) → JSON y Excel
# - Backtesting: stats SOLO hasta el día anterior al partido
# - Botón de calibración de pesos desde Excel (regresión logística sobre diff_*)
# - Columna "Coincide_favorito_Bet365" (Si/No/"")
# - Integra momios Bet365 de marcador de sets (2-0, 2-1, 1-2, 0-2)
# - Columnas Pick_VIP_90 y Pick_Fuerte_85 en Excel (reglas de alta confianza)
# - Batch paralelo con ThreadPoolExecutor + caché para acelerar requests
# - Slider para seleccionar cuántos hilos concurrentes usar en el batch
# - Soporte para usar hasta 20 API keys en round-robin
# - Features avanzados (power_score, sharpe_score, dominance_index, upset_risk,
#   clutch_score y master_score)
# - Timer y velocidad promedio del lote en pantalla (pero SIN auto-guardar Excel)

import os
import io
import json
import math
import threading
import time
import random
import string
from datetime import datetime, timedelta
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from unidecode import unidecode

import pandas as pd
import numpy as np  # para regresión, tiers y features avanzados

import streamlit as st

# ===================== CONFIGURACIÓN GLOBAL =====================

BASE_URL = "https://api.api-tennis.com/tennis/"

RANK_BUCKETS = {
    "GS": 1.30,      # Grand Slam
    "ATP/WTA": 1.15,
    "Challenger": 1.00,
    "ITF": 0.85
}
RANK_BUCKETS.setdefault("Other", 0.95)

# ========= manejo de múltiples API keys (hasta 20) =========

def _load_api_keys_from_env():
    keys = []
    single = (os.getenv("API_TENNIS_KEY") or "").strip()
    if single:
        keys.append(single)
    for i in range(1, 20):
        k = (os.getenv(f"API_TENNIS_KEY_{i}") or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys

API_KEYS = _load_api_keys_from_env()
_API_IDX = 0
_API_IDX_LOCK = threading.Lock()


def set_api_keys_from_string(s: str):
    """Lee API keys de un string (separadas por coma o punto y coma) y llena API_KEYS."""
    global API_KEYS, _API_IDX
    parts = []
    if s:
        tmp = s.replace(";", ",")
        for token in tmp.split(","):
            token = token.strip()
            if token:
                parts.append(token)
    parts = parts[:20]
    if parts:
        API_KEYS = parts
        _API_IDX = 0


def get_next_api_key():
    global _API_IDX
    with _API_IDX_LOCK:
        if not API_KEYS:
            return None
        key = API_KEYS[_API_IDX % len(API_KEYS)]
        _API_IDX += 1
        return key


def ensure_api_keys(text: str):
    """Asegura que haya API keys cargadas (desde input o variables de entorno)."""
    if text:
        set_api_keys_from_string(text)
    else:
        if not API_KEYS:
            keys_env = _load_api_keys_from_env()
            if keys_env:
                API_KEYS[:] = keys_env
    if not API_KEYS:
        raise ValueError(
            "Faltan API keys.\n"
            "Escribe 1–20 keys separadas por coma en el campo de API keys\n"
            "o define las variables API_TENNIS_KEY / API_TENNIS_KEY_1..20."
        )

# ===================== UTILIDADES =====================

def normalize(s: str) -> str:
    return unidecode(s or "").strip().lower()


def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default


def logistic(x):
    return 1.0 / (1.0 + math.exp(-x))


def clamp(v, a, b):
    return max(a, min(b, v))


def make_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


SESSION = make_session()
HTTP_TIMEOUT = 25

# ===================== API WRAPPER =====================

def call_api(method: str, params: dict):
    params = {k: v for k, v in params.items() if v is not None}
    params.pop("APIkey", None)

    api_key = get_next_api_key()
    if not api_key:
        raise RuntimeError("No hay API keys configuradas")

    params["APIkey"] = api_key
    r = SESSION.get(BASE_URL, params={"method": method, **params}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if str(data.get("success")) == "1":
        return data.get("result", {})

    if str(data.get("error")) == "1":
        try:
            detail = (data.get("result") or [{}])[0]
            cod = detail.get("cod")
            msg = detail.get("msg") or "API error"
        except Exception:
            cod, msg = None, "API error"
        raise RuntimeError(f"{method} → {msg} (cod={cod})")

    raise RuntimeError(f"{method} → Respuesta no esperada: {data}")

# ===================== ODDS HELPERS =====================

def get_bet365_odds_for_match(api_key: str, match_key: int):
    try:
        res = call_api("get_odds", {"match_key": match_key}) or {}
        m = res.get(str(match_key)) or res.get(int(match_key))
        if not isinstance(m, dict):
            return (None, None)

        ha = m.get("Home/Away") or {}
        home = ha.get("Home", {})
        away = ha.get("Away", {})

        def pick_b365(d):
            if not isinstance(d, dict):
                return None
            for k in d:
                if str(k).lower() == "bet365":
                    try:
                        return float(d[k])
                    except Exception:
                        return None
            return None

        return pick_b365(home), pick_b365(away)
    except Exception:
        return (None, None)


def get_bet365_setscore_odds_for_match(api_key: str, match_key: int):
    out = {"2:0": None, "2:1": None, "1:2": None, "0:2": None}
    try:
        res = call_api("get_odds", {"match_key": match_key}) or {}
        m = res.get(str(match_key)) or res.get(int(match_key))
        if not isinstance(m, dict):
            return out

        for market_name, market_data in m.items():
            if not isinstance(market_data, dict):
                continue
            for sel_name, sel_data in market_data.items():
                if not isinstance(sel_data, dict):
                    continue
                price = None
                for bk, val in sel_data.items():
                    if str(bk).lower() == "bet365":
                        try:
                            price = float(val)
                        except Exception:
                            price = None
                if price is None:
                    continue
                name_clean = sel_name.lower().replace(" ", "").replace(":", "-")
                if "2-0" in name_clean:
                    out["2:0"] = price
                elif "2-1" in name_clean:
                    out["2:1"] = price
                elif "1-2" in name_clean:
                    out["1:2"] = price
                elif "0-2" in name_clean:
                    out["0:2"] = price

        return out
    except Exception:
        return out

# ===================== FIXTURE HELPERS =====================

def list_fixtures(api_key: str, date_start: str, date_stop: str, tz: str, player_key=None):
    params = {"date_start": date_start, "date_stop": date_stop, "timezone": tz}
    if player_key:
        params["player_key"] = player_key
    return call_api("get_fixtures", params) or []

# ===================== CACHÉ DE HISTORIAL POR JUGADOR =====================

@lru_cache(maxsize=3000)
def cached_player_history(api_key: str, player_key: int, days_back: int = 180):
    stop = datetime.utcnow().date()
    start = stop - timedelta(days=days_back)

    res = list_fixtures(
        api_key,
        start.strftime("%Y-%m-%d"),
        stop.strftime("%Y-%m-%d"),
        "Europe/Berlin",
        player_key=player_key
    ) or []

    return tuple(res)

# ===================== FIXTURE POR MATCH_KEY =====================

def get_fixture_by_key(api_key: str, match_key: int, tz: str = "Europe/Berlin", center_date: str | None = None):
    # 1) Intento directo con get_events
    try:
        res = call_api("get_events", {"event_key": match_key}) or []
        if isinstance(res, list):
            for m in res:
                if safe_int(m.get("event_key")) == int(match_key):
                    return m
        elif isinstance(res, dict) and safe_int(res.get("event_key")) == int(match_key):
            return res
    except Exception:
        pass

    # 2) Fallback escaneando ventanas de fixtures
    if center_date:
        try:
            base = datetime.strptime(center_date, "%Y-%m-%d").date()
        except Exception:
            base = datetime.utcnow().date()
    else:
        base = datetime.utcnow().date()

    CHUNK_SIZES = [7, 3, 1]
    RINGS = [14, 28, 56, 112, 200]

    for ring in RINGS:
        start_global = base - timedelta(days=ring)
        stop_global = base + timedelta(days=10)
        cur_start = start_global
        while cur_start <= stop_global:
            hit_this_window = False
            for chunk in CHUNK_SIZES:
                cur_stop = min(cur_start + timedelta(days=chunk - 1), stop_global)
                try:
                    fixtures = list_fixtures(
                        api_key,
                        cur_start.strftime("%Y-%m-%d"),
                        cur_stop.strftime("%Y-%m-%d"),
                        tz
                    ) or []
                    for m in fixtures:
                        if safe_int(m.get("event_key")) == int(match_key):
                            return m
                    hit_this_window = True
                    break
                except requests.HTTPError as http_err:
                    if http_err.response is not None and http_err.response.status_code == 500:
                        continue
                    else:
                        raise
                except Exception:
                    continue
            step = max(CHUNK_SIZES) if hit_this_window else 1
            cur_start = cur_start + timedelta(days=step)

    if center_date:
        raise ValueError(
            f"No se encontró el match_key={match_key} alrededor de {base}."
        )
    else:
        raise ValueError(
            f"No se encontró el match_key={match_key} en get_events/get_results/fixtures recientes "
            f"(aprox. últimos 200 días). "
            "Si es un partido viejo, escribe una Fecha estimada (YYYY-MM-DD) en el campo "
            "'Fecha estimada' y vuelve a intentar."
        )

# ===================== FEATURE ENGINEERING =====================

def get_player_matches(api_key: str, player_key: int, days_back=180, ref_date: str | None = None):
    all_matches = list(cached_player_history(api_key, player_key, days_back))

    if ref_date:
        try:
            ref = datetime.strptime(ref_date, "%Y-%m-%d").date()
        except Exception:
            ref = datetime.utcnow().date()
    else:
        ref = datetime.utcnow().date()

    stop = ref - timedelta(days=1)

    clean = []
    for m in all_matches:
        d = m.get("event_date")
        if not d:
            continue
        try:
            md = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if md <= stop:
            status = (m.get("event_status") or "").lower()
            if "finished" in status or m.get("event_winner") in ("First Player", "Second Player"):
                clean.append(m)
    return clean


def is_win_for_name(match, player_name_norm: str):
    fp = normalize(match.get("event_first_player"))
    sp = normalize(match.get("event_second_player"))
    w = match.get("event_winner")
    if w == "First Player":
        return fp == player_name_norm
    if w == "Second Player":
        return sp == player_name_norm
    res = (match.get("event_final_result") or "").strip().lower()
    if fp == player_name_norm and (res.startswith("2 - 0") or res.startswith("2 - 1")):
        return True
    if sp == player_name_norm and (res.startswith("0 - 2") or res.startswith("1 - 2")):
        return True
    return False


def winrate_60d_and_lastN(matches, player_name_norm: str, N=10, days=60, ref_date: str | None = None):
    if ref_date:
        try:
            base_dt = datetime.strptime(ref_date, "%Y-%m-%d")
        except Exception:
            base_dt = datetime.utcnow()
    else:
        base_dt = datetime.utcnow()

    def days_ago(m):
        try:
            d = datetime.strptime(m["event_date"], "%Y-%m-%d")
            return (base_dt - d).days
        except Exception:
            return 10 ** 6

    recent = [m for m in matches if days_ago(m) <= days]
    wr60 = (sum(is_win_for_name(m, player_name_norm) for m in recent) / len(recent)) if recent else 0.5

    sorted_all = sorted(
        matches,
        key=lambda x: (x.get("event_date") or "", x.get("event_time") or "00:00"),
        reverse=True
    )
    lastN = sorted_all[:N]
    wrN = (sum(is_win_for_name(m, player_name_norm) for m in lastN) / len(lastN)) if lastN else 0.5

    last_date = sorted_all[0]["event_date"] if sorted_all else None
    return wr60, wrN, last_date, sorted_all


def compute_momentum(sorted_matches, player_name_norm: str):
    streak = 0
    for m in sorted_matches:
        w = is_win_for_name(m, player_name_norm)
        if w:
            streak = +1 if streak < 0 else streak + 1
        else:
            streak = -1 if streak > 0 else -1
        if streak >= 4:
            return +1
        if streak <= -3:
            return -1
    return 0


def rest_days(last_date_str: str | None, ref_date_str: str | None = None):
    if not last_date_str:
        return None
    try:
        d = datetime.strptime(last_date_str, "%Y-%m-%d").date()
    except Exception:
        return None

    if ref_date_str:
        try:
            base = datetime.strptime(ref_date_str, "%Y-%m-%d").date()
        except Exception:
            base = datetime.utcnow().date()
    else:
        base = datetime.utcnow().date()

    return (base - d).days


def rest_score(days):
    if days is None:
        return 0.0
    return clamp(1.0 - abs(days - 7) / 21.0, 0.0, 1.0)


def league_bucket(league_name: str):
    s = (league_name or "").lower()
    if any(k in s for k in ["grand slam", "roland", "wimbledon", "us open", "australian open"]):
        return "GS"
    if any(k in s for k in ["atp", "wta"]):
        return "ATP/WTA"
    if "challenger" in s:
        return "Challenger"
    if "itf" in s:
        return "ITF"
    return "Other"


def surface_winrate(matches, player_name_norm: str, surface: str):
    if not surface:
        return 0.5
    sur = surface.lower()
    hist = [m for m in matches if (m.get("event_tournament_surface") or "").lower() == sur]
    if not hist:
        return 0.5
    return sum(is_win_for_name(m, player_name_norm) for m in hist) / len(hist)


def travel_penalty(last_match_country, current_country, days_since):
    if not last_match_country or not current_country or days_since is None:
        return 0.0
    if last_match_country.strip().lower() == current_country.strip().lower():
        return 0.0
    if days_since <= 3:
        return 0.15
    if days_since <= 5:
        return 0.07
    return 0.0


def elo_synth_from_opposition(matches, player_name_norm: str):
    if not matches:
        return 0.0
    score = 0.0
    for m in matches[:20]:
        bucket = league_bucket(m.get("league_name", ""))
        weight = RANK_BUCKETS.get(bucket, 1.0)
        w = is_win_for_name(m, player_name_norm)
        score += (1.0 if w else -1.0) * weight
    score = score / (20.0 * 1.30)
    return clamp(score, -1.0, 1.0)

# ===================== H2H =====================

def compute_h2h(api_key, player_key_a, player_key_b, years_back=3,
                ref_date: str | None = None, current_match_key: int | None = None):
    days_back = 365 * years_back

    hist_a = list(cached_player_history(api_key, player_key_a, days_back=days_back))
    hist_b = list(cached_player_history(api_key, player_key_b, days_back=days_back))

    cutoff = None
    if ref_date:
        try:
            ref_dt = datetime.strptime(ref_date, "%Y-%m-%d").date()
            cutoff = ref_dt - timedelta(days=1)
        except Exception:
            cutoff = None

    def valid_before_cutoff(m):
        if cutoff is None:
            return True
        d = m.get("event_date")
        if not d:
            return False
        try:
            md = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return False
        return md <= cutoff

    def is_same_match(m):
        if current_match_key is None:
            return False
        try:
            return safe_int(m.get("event_key")) == int(current_match_key)
        except Exception:
            return False

    hist_a = [m for m in hist_a if valid_before_cutoff(m) and not is_same_match(m)]
    hist_b = [m for m in hist_b if valid_before_cutoff(m) and not is_same_match(m)]

    def key_of(m):
        return (
            normalize(m.get("event_first_player")),
            normalize(m.get("event_second_player")),
            m.get("event_date"),
        )

    idx_b = {key_of(m): m for m in hist_b}
    wins_a = wins_b = 0

    for ma in hist_a:
        k = key_of(ma)
        mb = idx_b.get(k)
        if not mb:
            continue
        w = ma.get("event_winner")
        if w == "First Player":
            wins_a += 1
        elif w == "Second Player":
            wins_b += 1

    total = wins_a + wins_b
    pct_a = wins_a / total if total else 0.5
    return wins_a, wins_b, pct_a, total


@lru_cache(maxsize=2000)
def cached_h2h(api_key: str, player_key_a: int, player_key_b: int,
               years_back: int = 3, ref_date: str | None = None,
               current_match_key: int | None = None):
    return compute_h2h(
        api_key,
        player_key_a,
        player_key_b,
        years_back=years_back,
        ref_date=ref_date,
        current_match_key=current_match_key,
    )


@lru_cache(maxsize=5000)
def cached_bet365_match(api_key: str, match_key: int):
    return get_bet365_odds_for_match(api_key, match_key)


@lru_cache(maxsize=5000)
def cached_bet365_sets(api_key: str, match_key: int):
    return get_bet365_setscore_odds_for_match(api_key, match_key)

# ===================== MODELO Y SALIDA =====================

def calibrate_probability(diff, weights, gamma=3.0, bias=0.0, bonus=0.0, malus=0.0):
    wsum = sum(weights.values()) or 1.0
    w = {k: v / wsum for k, v in weights.items()}
    z = (
        w.get("wr60", 0) * diff.get("wr60", 0)
        + w.get("wr10", 0) * diff.get("wr10", 0)
        + w.get("h2h", 0) * diff.get("h2h", 0)
        + w.get("rest", 0) * diff.get("rest", 0)
        + w.get("surface", 0) * diff.get("surface", 0)
        + w.get("elo", 0) * diff.get("elo", 0)
        + w.get("momentum", 0) * diff.get("momentum", 0)
        - w.get("travel", 0) * diff.get("travel", 0)
        + bias
    )
    p = logistic(gamma * z + bonus - malus)
    return clamp(p, 0.05, 0.95)


def invert_bo3_set_prob(pm):
    lo, hi = 0.05, 0.95
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        pm_mid = mid * mid * (3 - 2 * mid)
        if pm_mid < pm:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bo3_distribution(p_set):
    s = p_set
    q = 1 - s
    p20 = s * s
    p21 = 2 * s * s * q
    p12 = 2 * q * q * s
    p02 = q * q
    tot = p20 + p21 + p12 + p02
    return {"2:0": p20 / tot, "2:1": p21 / tot, "1:2": p12 / tot, "0:2": p02 / tot}


def to_decimal(p):
    p = clamp(p, 0.01, 0.99)
    return round(1.0 / p, 3)

# ========= Reglas de Tiers (Pick_VIP_90 / Pick_Fuerte_85) =========

def aplicar_reglas_tiers(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ["p_player1", "p_player2", "diff_elo", "diff_wr10"]:
        if col not in df.columns:
            raise ValueError(f"El DataFrame final no tiene la columna requerida '{col}' para Tiers.")

    df["p_fav"] = df[["p_player1", "p_player2"]].max(axis=1)
    df["diff_elo_abs"] = df["diff_elo"].astype(float).abs()
    df["diff_wr10_abs"] = df["diff_wr10"].astype(float).abs()

    vip_mask = (
        (df["p_fav"] >= 0.65)
        & (df["diff_elo_abs"] >= 0.4)
        & (df["diff_wr10_abs"] >= 0.2)
    )
    fuerte_mask = (
        (~vip_mask)
        & (df["p_fav"] >= 0.60)
        & (df["diff_elo_abs"] >= 0.4)
    )

    df["Pick_VIP_90"] = np.where(vip_mask, "Si", "No")
    df["Pick_Fuerte_85"] = np.where(fuerte_mask, "Si", "No")
    return df

# ========= Features avanzados =========

def agregar_features_avanzados(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    required = [
        "diff_wr60", "diff_wr10", "diff_h2h", "diff_rest",
        "diff_surface", "diff_elo", "diff_momentum", "diff_travel",
        "p_player1", "p_player2",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas para features avanzados: {missing}")

    diffs = df[
        ["diff_wr60", "diff_wr10", "diff_h2h", "diff_rest",
         "diff_surface", "diff_elo", "diff_momentum", "diff_travel"]
    ].astype(float).fillna(0.0)

    df["p_fav"] = df[["p_player1", "p_player2"]].max(axis=1)

    df["power_score"] = (
        0.25 * diffs["diff_wr60"]
        + 0.15 * diffs["diff_wr10"]
        + 0.05 * diffs["diff_h2h"]
        + 0.10 * diffs["diff_rest"]
        + 0.05 * diffs["diff_surface"]
        + 0.25 * diffs["diff_elo"]
        + 0.05 * diffs["diff_momentum"]
        - 0.05 * diffs["diff_travel"]
    )

    df["sharpe_score"] = df["power_score"] / (0.001 + diffs["diff_momentum"].abs())

    def _sigmoid_series(x: pd.Series) -> pd.Series:
        return 1.0 / (1.0 + np.exp(-10.0 * x))

    df["dominance_index"] = (
        df["p_fav"].astype(float) * 0.50
        + _sigmoid_series(diffs["diff_elo"]) * 0.30
        + _sigmoid_series(diffs["diff_wr10"]) * 0.20
    )

    df["upset_risk"] = (
        (-diffs["diff_rest"]) * 0.30
        + diffs["diff_travel"] * 0.30
        + (-diffs["diff_momentum"]) * 0.30
        + diffs["diff_surface"].abs() * 0.10
    )

    df["clutch_score"] = (
        0.40 * diffs["diff_wr10"]
        + 0.30 * diffs["diff_momentum"]
        + 0.30 * diffs["diff_h2h"]
    )

    df["master_score"] = (
        0.40 * df["power_score"]
        + 0.20 * df["sharpe_score"]
        + 0.20 * df["dominance_index"]
        - 0.10 * df["upset_risk"]
        + 0.10 * df["clutch_score"]
    )

    return df

# ===================== CÁLCULO PRINCIPAL =====================

def compute_from_fixture(api_key: str, meta: dict, surface_hint: str,
                         weights: dict, gamma: float, bias: float):

    match_key = safe_int(meta.get("event_key"))
    tz = meta.get("timezone") or "Europe/Berlin"
    date_str = meta.get("event_date") or datetime.utcnow().strftime("%Y-%m-%d")

    p1 = meta.get("event_first_player")
    p2 = meta.get("event_second_player")
    p1n = normalize(p1)
    p2n = normalize(p2)

    p1_key = safe_int(meta.get("first_player_key"))
    p2_key = safe_int(meta.get("second_player_key"))

    surface_api = (meta.get("event_tournament_surface") or "").strip() or None
    surface_final = (surface_hint or "").strip().lower() or (surface_api.lower() if surface_api else None)

    lastA = get_player_matches(api_key, p1_key, days_back=180, ref_date=date_str) if p1_key else []
    lastB = get_player_matches(api_key, p2_key, days_back=180, ref_date=date_str) if p2_key else []

    wr60_A, wr10_A, lastA_date, sortedA = winrate_60d_and_lastN(lastA, p1n, N=10, days=60, ref_date=date_str)
    wr60_B, wr10_B, lastB_date, sortedB = winrate_60d_and_lastN(lastB, p2n, N=10, days=60, ref_date=date_str)

    momA = compute_momentum(sortedA, p1n)
    momB = compute_momentum(sortedB, p2n)

    rA_days = rest_days(lastA_date, ref_date_str=date_str)
    rB_days = rest_days(lastB_date, ref_date_str=date_str)
    rA = rest_score(rA_days)
    rB = rest_score(rB_days)

    surf_wrA = surface_winrate(lastA, p1n, surface_final)
    surf_wrB = surface_winrate(lastB, p2n, surface_final)

    lastA_country = lastA and (lastA[0].get("country") or lastA[0].get("event_tournament_country"))
    lastB_country = lastB and (lastB[0].get("country") or lastB[0].get("event_tournament_country"))
    tourn_country = meta.get("country") or meta.get("event_tournament_country")

    travA = travel_penalty(lastA_country, tourn_country, rA_days or 999)
    travB = travel_penalty(lastB_country, tourn_country, rB_days or 999)

    h2h_n = 0
    if p1_key and p2_key:
        _, _, h2h_pct_a, h2h_n = cached_h2h(
            api_key,
            p1_key,
            p2_key,
            3,
            date_str,
            match_key,
        )
    else:
        h2h_pct_a = 0.5
    h2h_pct_b = 1.0 - h2h_pct_a

    eloA = elo_synth_from_opposition(sortedA, p1n)
    eloB = elo_synth_from_opposition(sortedB, p2n)

    total_obs = len(sortedA) + len(sortedB)
    if total_obs < 6:
        reg_alpha = 0.60
    elif total_obs < 12:
        reg_alpha = 0.35
    elif total_obs < 20:
        reg_alpha = 0.20
    else:
        reg_alpha = 0.0

    wr60_A = (1 - reg_alpha) * wr60_A + reg_alpha * 0.5
    wr60_B = (1 - reg_alpha) * wr60_B + reg_alpha * 0.5
    wr10_A = (1 - reg_alpha) * wr10_A + reg_alpha * 0.5
    wr10_B = (1 - reg_alpha) * wr10_B + reg_alpha * 0.5
    surf_wrA = (1 - reg_alpha) * surf_wrA + reg_alpha * 0.5
    surf_wrB = (1 - reg_alpha) * surf_wrB + reg_alpha * 0.5
    h2h_pct_a = (1 - reg_alpha) * h2h_pct_a + reg_alpha * 0.5
    h2h_pct_b = 1 - h2h_pct_a
    eloA = (1 - reg_alpha) * eloA
    eloB = (1 - reg_alpha) * eloB

    diff = {
        "wr60": wr60_A - wr60_B,
        "wr10": wr10_A - wr10_B,
        "h2h": h2h_pct_a - h2h_pct_b,
        "rest": rA - rB,
        "surface": surf_wrA - surf_wrB,
        "elo": eloA - eloB,
        "momentum": (0.03 if momA > 0 else (-0.03 if momA < 0 else 0.0))
                    - (0.03 if momB > 0 else (-0.03 if momB < 0 else 0.0)),
        "travel": travA - travB
    }

    pA = calibrate_probability(diff=diff, weights=weights, gamma=gamma, bias=bias)
    pB = 1 - pA

    p_set_A = invert_bo3_set_prob(pA)
    dist = bo3_distribution(p_set_A)

    event_status = (meta.get("event_status") or "").strip()
    event_winner_side = meta.get("event_winner")
    if event_winner_side == "First Player":
        winner_name = p1
    elif event_winner_side == "Second Player":
        winner_name = p2
    else:
        winner_name = None

    final_sets_str = (meta.get("event_final_result") or "").strip() or None

    if match_key:
        b365_home, b365_away = cached_bet365_match(api_key, match_key)
        bet365_cs = cached_bet365_sets(api_key, match_key)
    else:
        b365_home, b365_away = None, None
        bet365_cs = {"2:0": None, "2:1": None, "1:2": None, "0:2": None}

    return {
        "match_key": match_key,
        "inputs": {
            "date": date_str,
            "player1": p1,
            "player2": p2,
            "timezone": tz,
            "surface_used": surface_final or "(no especificada)"
        },
        "notes": [
            "Momios sintéticos (decimales) = 1 / prob.",
            "Backtesting: solo datos hasta el día anterior.",
        ],
        "features": {
            "player1": {
                "wr60": round(wr60_A, 3),
                "wr10": round(wr10_A, 3),
                "h2h": round(h2h_pct_a, 3),
                "rest_days": rA_days,
                "rest_score": round(rA, 3),
                "surface_wr": round(surf_wrA, 3),
                "elo_synth": round(eloA, 3),
                "momentum": momA,
                "travel_penalty": round(travA, 3),
            },
            "player2": {
                "wr60": round(wr60_B, 3),
                "wr10": round(wr10_B, 3),
                "h2h": round(h2h_pct_b, 3),
                "rest_days": rB_days,
                "rest_score": round(rB, 3),
                "surface_wr": round(surf_wrB, 3),
                "elo_synth": round(eloB, 3),
                "momentum": momB,
                "travel_penalty": round(travB, 3),
            },
            "diff_A_minus_B": {k: round(v, 4) for k, v in diff.items()},
        },
        "debug": {
            "max_hist_date_p1": lastA_date,
            "max_hist_date_p2": lastB_date,
            "h2h_matches_used": h2h_n,
        },
        "weights_used": {k: round(v, 3) for k, v in weights.items()},
        "gamma": gamma,
        "bias": bias,
        "regularization_alpha": reg_alpha,
        "probabilities": {
            "match": {"player1": round(pA, 4), "player2": round(pB, 4)},
            "final_sets": {k: round(v, 4) for k, v in dist.items()},
        },
        "synthetic_odds_decimal": {
            "player1": to_decimal(pA),
            "player2": to_decimal(pB),
            "2:0": to_decimal(dist["2:0"]),
            "2:1": to_decimal(dist["2:1"]),
            "1:2": to_decimal(dist["1:2"]),
            "0:2": to_decimal(dist["0:2"]),
        },
        "bet365_odds_decimal": {
            "player1": b365_home,
            "player2": b365_away,
        },
        "bet365_setscore_odds_decimal": bet365_cs,
        "official_result": {
            "status": event_status,
            "winner_side": event_winner_side,
            "winner_name": winner_name,
            "final_sets": final_sets_str,
        },
    }

# ===================== HELPERS PARA STREAMLIT =====================

def find_match_by_names(api_key, date_str, p1, p2, tz):
    p1n, p2n = normalize(p1), normalize(p2)
    base = datetime.strptime(date_str, "%Y-%m-%d").date()

    def scan_day(d):
        fixtures = list_fixtures(api_key, d, d, tz)
        cand = []
        for m in fixtures:
            fp = normalize(m.get("event_first_player"))
            sp = normalize(m.get("event_second_player"))
            if (p1n in fp and p2n in sp) or (p1n in sp and p2n in fp):
                cand.append(m)
        if not cand:
            for m in fixtures:
                fp = normalize(m.get("event_first_player"))
                sp = normalize(m.get("event_second_player"))
                if any(x in fp for x in p1n.split()) and any(x in sp for x in p2n.split()):
                    cand.append(m)
                elif any(x in sp for x in p1n.split()) and any(x in fp for x in p2n.split()):
                    cand.append(m)
        return cand[0] if cand else None

    m = scan_day(date_str)
    if not m:
        for k in [1]:
            for dd in [base - timedelta(days=k), base + timedelta(days=k)]:
                hit = scan_day(dd.strftime("%Y-%m-%d"))
                if hit:
                    m = hit
                    break
            if m:
                break

    if not m:
        raise ValueError(f"No se encontró el partido '{p1}' vs '{p2}' cerca de {date_str} (tz {tz}).")
    return m


def parse_batch_keys(raw: str):
    parts = [p.strip() for p in raw.replace(",", " ").replace("\n", " ").split(" ") if p.strip()]
    keys = []
    for p in parts:
        if p.isdigit():
            keys.append(int(p))
    seen = set()
    dedup = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            dedup.append(k)
    return dedup


def build_export_dataframe(results_batch: list) -> pd.DataFrame:
    if not results_batch:
        raise ValueError("No hay resultados de lote para exportar.")

    rows = []
    for r in results_batch:
        mk = r.get("match_key")
        inp = r.get("inputs", {})
        probs = r.get("probabilities", {}).get("match", {})
        odds = r.get("synthetic_odds_decimal", {})
        feats = r.get("features", {})
        off = r.get("official_result", {})
        b365 = r.get("bet365_odds_decimal", {}) or {}
        b365_cs = r.get("bet365_setscore_odds_decimal", {}) or {}
        f1 = feats.get("player1", {})
        f2 = feats.get("player2", {})
        diff = feats.get("diff_A_minus_B", {})

        dbg = r.get("debug", {}) or {}
        max_hist_date_p1 = dbg.get("max_hist_date_p1")
        max_hist_date_p2 = dbg.get("max_hist_date_p2")
        h2h_matches_used = dbg.get("h2h_matches_used")

        cutoff_backtesting = None
        date_val = inp.get("date")
        if date_val:
            try:
                d = datetime.strptime(str(date_val), "%Y-%m-%d").date()
                cutoff_backtesting = (d - timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception:
                cutoff_backtesting = None

        odds_p1 = odds.get("player1")
        odds_p2 = odds.get("player2")
        winner_side = off.get("winner_side")

        favored_side_synth = None
        try:
            if odds_p1 is not None and odds_p2 is not None:
                if float(odds_p1) < float(odds_p2):
                    favored_side_synth = "First Player"
                elif float(odds_p2) < float(odds_p1):
                    favored_side_synth = "Second Player"
        except Exception:
            favored_side_synth = None

        if favored_side_synth and winner_side in ("First Player", "Second Player"):
            acerto = "Si" if favored_side_synth == winner_side else "No"
        else:
            acerto = ""

        bet365_p1 = b365.get("player1")
        bet365_p2 = b365.get("player2")

        favored_side_b365 = None
        try:
            if bet365_p1 is not None and bet365_p2 is not None:
                if float(bet365_p1) < float(bet365_p2):
                    favored_side_b365 = "First Player"
                elif float(bet365_p2) < float(bet365_p1):
                    favored_side_b365 = "Second Player"
        except Exception:
            favored_side_b365 = None

        if favored_side_synth and favored_side_b365:
            if favored_side_synth == favored_side_b365:
                coincide_fav = "Si"
            else:
                coincide_fav = "No"
        else:
            coincide_fav = ""

        row = {
            "match_key": mk,
            "date": inp.get("date"),
            "player1": inp.get("player1"),
            "player2": inp.get("player2"),
            "surface_used": inp.get("surface_used"),

            "p_player1": probs.get("player1"),
            "p_player2": probs.get("player2"),
            "odds_player1": odds_p1,
            "odds_player2": odds_p2,
            "odds_2_0": odds.get("2:0"),
            "odds_2_1": odds.get("2:1"),
            "odds_1_2": odds.get("1:2"),
            "odds_0_2": odds.get("0:2"),

            "bet365_player1": bet365_p1,
            "bet365_player2": bet365_p2,
            "bet365_cs_2_0": b365_cs.get("2:0"),
            "bet365_cs_2_1": b365_cs.get("2:1"),
            "bet365_cs_1_2": b365_cs.get("1:2"),
            "bet365_cs_0_2": b365_cs.get("0:2"),

            "p1_wr60": f1.get("wr60"),
            "p1_wr10": f1.get("wr10"),
            "p1_h2h": f1.get("h2h"),
            "p1_rest_days": f1.get("rest_days"),
            "p1_surface_wr": f1.get("surface_wr"),
            "p1_elo": f1.get("elo_synth"),
            "p1_momentum": f1.get("momentum"),
            "p1_travel": f1.get("travel_penalty"),

            "p2_wr60": f2.get("wr60"),
            "p2_wr10": f2.get("wr10"),
            "p2_h2h": f2.get("h2h"),
            "p2_rest_days": f2.get("rest_days"),
            "p2_surface_wr": f2.get("surface_wr"),
            "p2_elo": f2.get("elo_synth"),
            "p2_momentum": f2.get("momentum"),
            "p2_travel": f2.get("travel_penalty"),

            "diff_wr60": diff.get("wr60"),
            "diff_wr10": diff.get("wr10"),
            "diff_h2h": diff.get("h2h"),
            "diff_rest": diff.get("rest"),
            "diff_surface": diff.get("surface"),
            "diff_elo": diff.get("elo"),
            "diff_momentum": diff.get("momentum"),
            "diff_travel": diff.get("travel"),

            "hist_max_date_p1": max_hist_date_p1,
            "hist_max_date_p2": max_hist_date_p2,
            "h2h_matches_used": h2h_matches_used,
            "cutoff_backtesting_date": cutoff_backtesting,

            "status": off.get("status"),
            "winner_name": off.get("winner_name"),
            "final_sets": off.get("final_sets"),
            "Acerto pronostico": acerto,
            "Coincide_favorito_Bet365": coincide_fav,
        }

        rows.append(row)

    df = pd.DataFrame(rows).sort_values(
        by=["date", "match_key"],
        ascending=True,
        na_position="last",
    )

    df = agregar_features_avanzados(df)
    df = aplicar_reglas_tiers(df)

    return df

# ===================== STREAMLIT APP =====================

st.set_page_config(page_title="Tenis AI+ Momios sintéticos", layout="wide")
st.title("🎾 Tenis AI+ — Momios sintéticos (Streamlit)")

if "batch_results" not in st.session_state:
    st.session_state["batch_results"] = []
if "batch_errors" not in st.session_state:
    st.session_state["batch_errors"] = []
if "batch_stats" not in st.session_state:
    st.session_state["batch_stats"] = {}

with st.sidebar:
    st.header("⚙️ Configuración global")

    default_api_text = ",".join(API_KEYS) if API_KEYS else (os.getenv("API_TENNIS_KEY") or "")
    api_text = st.text_input(
        "API Keys (1–20, separadas por coma)",
        value=default_api_text,
        type="password",
    )

    st.markdown("---")
    st.subheader("Pesos del modelo (se normalizan a 1)")

    w_wr60 = st.slider("wr60 (forma 60 días)", 0.0, 1.0, 0.12, 0.01)
    w_wr10 = st.slider("wr10 (últimos 10)", 0.0, 1.0, 0.33, 0.01)
    w_h2h = st.slider("h2h", 0.0, 1.0, 0.01, 0.01)
    w_rest = st.slider("rest (descanso)", 0.0, 1.0, 0.19, 0.01)
    w_surf = st.slider("surface", 0.0, 1.0, 0.00, 0.01)
    w_elo = st.slider("elo sintético", 0.0, 1.0, 0.31, 0.01)
    w_mom = st.slider("momentum", 0.0, 1.0, 0.05, 0.01)
    w_trav = st.slider("travel (malus)", 0.0, 1.0, 0.00, 0.01)

    gamma = st.slider("gamma (agresividad)", 0.5, 5.0, 3.0, 0.1)
    bias = st.slider("bias (sesgo)", -0.5, 0.5, 0.0, 0.01)

    st.markdown("---")
    max_workers = st.slider("Hilos simultáneos (batch)", 1, 16, 4, 1)

weights = {
    "wr60": w_wr60,
    "wr10": w_wr10,
    "h2h": w_h2h,
    "rest": w_rest,
    "surface": w_surf,
    "elo": w_elo,
    "momentum": w_mom,
    "travel": w_trav,
}

tab_single, tab_batch, tab_calib = st.tabs(["🔹 Individual", "🔁 Lote por match_key", "📈 Calibrar pesos desde Excel"])

# ---------- TAB INDIVIDUAL ----------

with tab_single:
    st.subheader("Cálculo individual")

    col1, col2, col3 = st.columns(3)
    with col1:
        date_str = st.text_input("Fecha (YYYY-MM-DD)", value=datetime.utcnow().strftime("%Y-%m-%d"))
        tz = st.text_input("Timezone (IANA)", value="America/Mexico_City")
    with col2:
        player1 = st.text_input("Jugador 1 (Home)", value="Okamura")
        player2 = st.text_input("Jugador 2 (Away)", value="Morvayova")
    with col3:
        surface_hint = st.text_input("Superficie (hard/clay/grass/indoor)", "")
        manual_match_key = st.text_input("Match key (opcional)", "")
        center_date_for_key = st.text_input("Fecha estimada para match_key (YYYY-MM-DD, opcional)", "")

    if st.button("Calcular (individual)"):
        try:
            ensure_api_keys(api_text)
            api_key_cache = API_KEYS[0]

            if manual_match_key.strip().isdigit():
                meta = get_fixture_by_key(
                    api_key_cache,
                    int(manual_match_key.strip()),
                    tz=tz.strip() or "Europe/Berlin",
                    center_date=center_date_for_key.strip() or None,
                )
            else:
                meta = find_match_by_names(
                    api_key_cache,
                    date_str.strip(),
                    player1.strip(),
                    player2.strip(),
                    tz.strip() or "Europe/Berlin",
                )

            res = compute_from_fixture(
                api_key_cache,
                meta,
                surface_hint.strip().lower() or None,
                weights,
                gamma,
                bias,
            )
            st.json(res)

            json_bytes = json.dumps(res, ensure_ascii=False, indent=2).encode("utf-8")
            st.download_button(
                "💾 Descargar JSON individual",
                data=json_bytes,
                file_name="resultado_tennis_single.json",
                mime="application/json",
            )
        except Exception as e:
            st.error(f"Error en cálculo individual: {e}")

# ---------- TAB BATCH ----------

with tab_batch:
    st.subheader("Cálculo por lote (match_keys)")

    st.markdown(
        "Introduce **match_key** uno por línea, separados por coma o espacios. "
        "Se eliminarán duplicados automáticamente."
    )
    raw_keys = st.text_area("match_keys", height=150, placeholder="12035106\n12035138\n12035140 ...")

    colb1, colb2, colb3 = st.columns(3)
    with colb1:
        tz_batch = st.text_input("Timezone lote (IANA)", value="Europe/Berlin", key="tz_batch")
    with colb2:
        center_date_batch = st.text_input(
            "Fecha estimada (YYYY-MM-DD, opcional)", value="", key="center_date_batch"
        )
    with colb3:
        st.markdown("")

    log_placeholder = st.empty()
    progress_bar = st.progress(0.0)
    summary_placeholder = st.empty()

    if st.button("Calcular lote"):
        keys = parse_batch_keys(raw_keys)
        if not keys:
            st.warning("No se encontraron match_keys válidos.")
        else:
            try:
                ensure_api_keys(api_text)
                api_key_cache = API_KEYS[0]
            except Exception as e:
                st.error(f"Error con API keys: {e}")
            else:
                log_lines = []
                def log(msg):
                    log_lines.append(msg)
                    log_placeholder.text("\n".join(log_lines[-50:]))

                total = len(keys)
                errors = []
                results = []
                processing_times = []

                log(f"Iniciando lote con {total} partidos y {max_workers} hilos...")

                start_time = time.perf_counter()

                def process_one(idx, mk):
                    log(f"[{idx}/{total}] Buscando match_key {mk}…")
                    try:
                        meta = get_fixture_by_key(
                            api_key_cache,
                            mk,
                            tz=tz_batch.strip() or "Europe/Berlin",
                            center_date=center_date_batch.strip() or None,
                        )
                        t0 = time.perf_counter()
                        out = compute_from_fixture(
                            api_key_cache,
                            meta,
                            surface_hint.strip().lower() or None,
                            weights,
                            gamma,
                            bias,
                        )
                        elapsed = time.perf_counter() - t0
                        return ("ok", mk, out, None, elapsed)
                    except Exception as e:
                        return ("err", mk, None, str(e), None)

                done_count = 0
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_to_info = {
                            executor.submit(process_one, idx, mk): (idx, mk)
                            for idx, mk in enumerate(keys, start=1)
                        }
                        for future in as_completed(future_to_info):
                            idx, mk = future_to_info[future]
                            try:
                                status, mk_ret, out, err, elapsed = future.result()
                            except Exception as e:
                                status, mk_ret, out, err, elapsed = ("err", mk, None, str(e), None)

                            if status == "ok" and out is not None:
                                results.append(out)
                                if elapsed is not None:
                                    processing_times.append(elapsed)
                                log(
                                    f"   OK [{idx}/{total}]: "
                                    f"{out['inputs']['player1']} vs {out['inputs']['player2']} "
                                    f"(date: {out['inputs']['date']}) "
                                    f"[{elapsed:.2f} s procesado]"
                                )
                            else:
                                errors.append((mk_ret, err))
                                log(f"   ERROR {mk_ret}: {err}")

                            done_count += 1
                            progress_bar.progress(done_count / total)

                    avg_time = (sum(processing_times) / len(processing_times)) if processing_times else None
                    total_elapsed = time.perf_counter() - start_time

                    st.session_state["batch_results"] = results
                    st.session_state["batch_errors"] = errors
                    st.session_state["batch_stats"] = {
                        "avg_time_per_match": avg_time,
                        "total_time_seconds": total_elapsed,
                        "count_ok": len(results),
                        "count_errors": len(errors),
                    }

                    summary_msg = (
                        f"✅ Lote finalizado. Partidos OK: {len(results)}, "
                        f"Errores: {len(errors)}. "
                        f"Tiempo total: {total_elapsed:.1f} s. "
                    )
                    if avg_time is not None:
                        summary_msg += f"Velocidad promedio: {avg_time:.2f} s/match."
                    summary_placeholder.success(summary_msg)

                    payload = {
                        "count": len(results),
                        "results": results,
                        "errors": errors,
                    }
                    if avg_time is not None:
                        payload["avg_match_time_seconds"] = avg_time
                    st.markdown("### JSON resumen del lote")
                    st.json(payload)

                except Exception as e:
                    st.error(f"Error en lote: {e}")

    if st.session_state["batch_results"]:
        st.markdown("---")
        st.subheader("Exportar resultados del lote a Excel")

        try:
            df_export = build_export_dataframe(st.session_state["batch_results"])
            st.dataframe(df_export.head(50))
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df_export.to_excel(writer, index=False, sheet_name="resumen")
                jrows = [
                    {"match_key": r.get("match_key"),
                     "json": json.dumps(r, ensure_ascii=False)}
                    for r in st.session_state["batch_results"]
                ]
                pd.DataFrame(jrows).to_excel(writer, index=False, sheet_name="json")
            buffer.seek(0)
            st.download_button(
                "💾 Descargar Excel del lote",
                data=buffer,
                file_name="momios_sinteticos_batch.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as e:
            st.error(f"Error preparando DataFrame para exportar: {e}")

# ---------- TAB CALIBRACIÓN DESDE EXCEL ----------

with tab_calib:
    st.subheader("Calibrar pesos desde Excel (regresión logística)")
    st.markdown(
        "Sube un Excel con hoja **'resumen'** que contenga al menos las columnas:\n"
        "`winner_name`, `player1`, `player2`, `diff_wr60`, `diff_wr10`, `diff_h2h`, "
        "`diff_rest`, `diff_surface`, `diff_elo`, `diff_momentum`, `diff_travel`."
    )

    uploaded_file = st.file_uploader("Subir archivo Excel", type=["xlsx", "xls"])
    if uploaded_file is not None and st.button("Calibrar pesos desde Excel"):
        try:
            try:
                from sklearn.linear_model import LogisticRegression
                from sklearn.preprocessing import StandardScaler
            except ImportError:
                st.error("Necesitas instalar scikit-learn: `pip install scikit-learn`")
                st.stop()

            df = pd.read_excel(uploaded_file, sheet_name="resumen")

            required_cols = [
                "winner_name", "player1", "player2",
                "diff_wr60", "diff_wr10", "diff_h2h", "diff_rest",
                "diff_surface", "diff_elo", "diff_momentum", "diff_travel",
            ]
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                st.error(f"Faltan columnas en hoja 'resumen': {missing}")
                st.stop()

            df = df[df["winner_name"].notna()].copy()
            mask_valid = (df["winner_name"] == df["player1"]) | (df["winner_name"] == df["player2"])
            df = df[mask_valid].copy()
            if df.empty:
                st.error("No se encontraron filas donde winner_name sea player1 o player2.")
                st.stop()

            df["y"] = np.where(df["winner_name"] == df["player1"], 1, 0)

            features = [
                "diff_wr60",
                "diff_wr10",
                "diff_h2h",
                "diff_rest",
                "diff_surface",
                "diff_elo",
                "diff_momentum",
                "diff_travel",
            ]
            X = df[features].fillna(0.0)
            y = df["y"].values

            if len(df) < 30:
                st.warning(f"Solo hay {len(df)} partidos válidos. La calibración puede ser poco estable.")

            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            model = LogisticRegression(max_iter=5000)
            model.fit(X_scaled, y)

            coefs = model.coef_[0]
            odds_ratios = np.exp(coefs)
            importance_abs = np.abs(coefs)
            if importance_abs.sum() == 0:
                st.error("Los coeficientes resultaron 0; no se puede calibrar pesos.")
                st.stop()
            importance_norm = importance_abs / importance_abs.sum()

            st.markdown("#### Coeficientes de regresión")
            df_coef = pd.DataFrame({
                "feature": features,
                "coef": coefs,
                "odds_ratio": odds_ratios,
                "importancia_norm": importance_norm,
            })
            st.dataframe(df_coef)

            mapping = {
                "wr60": "diff_wr60",
                "wr10": "diff_wr10",
                "h2h": "diff_h2h",
                "rest": "diff_rest",
                "surface": "diff_surface",
                "elo": "diff_elo",
                "momentum": "diff_momentum",
                "travel": "diff_travel",
            }

            recommended = {}
            for slider_name, feat in mapping.items():
                idx = features.index(feat)
                recommended[slider_name] = float(importance_norm[idx])

            total_imp = sum(recommended.values()) or 1.0
            for k in recommended:
                recommended[k] = recommended[k] / total_imp

            st.markdown("#### Pesos sugeridos (normalizados a 1)")
            st.json({k: round(v, 3) for k, v in recommended.items()})

            st.info(
                "Copia manualmente estos pesos a los sliders del sidebar para usarlos en el modelo "
                "(no se pueden actualizar sliders automáticamente desde aquí)."
            )

        except Exception as e:
            st.error(f"Error en calibración: {e}")
