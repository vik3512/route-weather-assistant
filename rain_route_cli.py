#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
from urllib.parse import urlencode, quote_plus
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import polyline
from dotenv import load_dotenv
import streamlit as st
from geopy.distance import geodesic

# ======================== 常數與參數 ========================
SAMPLES_FAST = 8  # 快掃：決定要不要畫地圖
SAMPLE_INTERVAL_METERS = 500  # 短程路線：每隔多少公尺取樣一次
LONG_ROUTE_THRESHOLD_KM = 30  # 長程路線門檻，從 50 公里改為 30 公里
SHOW_MAP_RISK_THRESHOLD = 0.20  # 顯示地圖的風險臨界值
OPEN_WEATHER_MIN_RAIN_MM = 0.0

# 顏色（Google Static Maps RGBA）
COLOR_GREEN = "0x00AA00FF"  # 推薦路線無雨段
COLOR_BLUE  = "0x0066CCFF"  # 任一路線有雨段
COLOR_GRAY  = "0x999999FF"  # 其他候選基底

# 風險評分
RAIN_RATIO_WEIGHT = 0.7
RAIN_INTENSITY_WEIGHT = 0.3  # 以 30 mm/h 正規化

# 風險統一邏輯常數
NEXT_MM_WEIGHT = 0.5         # 將「下一小時雨量」以 0.5 權重納入
NEXT_MM_MIN_MM = 0.2         # 視為有降雨風險的 mm 門檻（現在/下一小時）
THUNDER_MIN = 95             # 雷暴代碼下界
THUNDER_MAX = 99             # 雷暴代碼上界
# =========================================================

load_dotenv()
# 支援本機 .env 與 Streamlit Cloud 的 st.secrets
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY") or st.secrets.get("GOOGLE_MAPS_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY") or st.secrets.get("OPENWEATHER_API_KEY", "")

# ======================== 建立全域 session（連線重用 + 重試） ========================
def _make_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.4,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET"}),  # urllib3 2.x
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "route-weather-assistant/1.0"})
    return s

SESSION = _make_session()

# ======================== Rerun 旗標（避免 callback 內 rerun 警告） ========================
if st.session_state.pop("_do_rerun", False):
    st.rerun()

# ======================== UI 基本設定 ========================
st.set_page_config(page_title="路線天氣助手", page_icon="🌦", layout="centered")
st.title("🌦 路線天氣助手")

# 乾淨 UI：隱藏 Running 提示 + 控制表單標籤間距
st.markdown(
    """
    <style>
      /* 隱藏 cache 運行提示（Running xxx(...)) */
      [data-testid="stStatusWidget"], .stStatusWidget { display: none !important; }
      /* 表單標籤間距 */
      div.stTextInput > label, div.stSelectbox > label { margin-bottom: 6px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# 【優化】將 API 金鑰檢查移至頂部，改善使用者體驗
if not GOOGLE_MAPS_API_KEY or not OPENWEATHER_API_KEY:
    st.error("⚠️ 缺少必要的 API 金鑰設定，請檢查您的 .env 檔案或 Streamlit secrets。")
    st.stop()

# ======================== Reset 與 widget key ========================
if "ui_nonce" not in st.session_state:
    st.session_state["ui_nonce"] = 0
if "result_ready" not in st.session_state:
    st.session_state["result_ready"] = False

nonce = st.session_state["ui_nonce"]
def k(name: str) -> str:
    return f"{name}_{nonce}"

def _clear_query_params():
    # 穩健清理 query params（不同 Streamlit 版本相容）
    try:
        st.query_params.clear()
    except Exception:
        try:
            for qk in list(st.query_params.keys()):
                del st.query_params[qk]
        except Exception:
            pass

def soft_reset_inputs():
    """軟重置：清 UI 內容與結果相關狀態，不動 cache；透過旗標於頂端 rerun。"""
    _clear_query_params()
    st.session_state["origin_q"] = ""
    st.session_state["dest_q"] = ""
    st.session_state[k("mode")] = "機車"
    st.session_state["result_ready"] = False
    # 清除可能殘留的中間狀態
    for kname in ["map_url", "route_data", "analysis_result"]:
        st.session_state.pop(kname, None)
    # 由頂端的旗標進行 rerun，避免 callback 內 no-op 警告
    st.session_state["_do_rerun"] = True

# ======================== 地理/路線 ========================
@st.cache_data(ttl=3600, show_spinner=False)
def geocode(query: str):
    if not GOOGLE_MAPS_API_KEY:
        return None, None, (None, None)
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": query, "key": GOOGLE_MAPS_API_KEY,
        "language": "zh-TW", "region": "tw", "components": "country:TW"
    }
    try:
        r = SESSION.get(url, params=params, timeout=20)
        resp = r.json()
    except Exception:
        return None, None, (None, None)

    if resp.get("status") == "OK" and resp.get("results"):
        top = resp["results"][0]
        loc = top.get("geometry", {}).get("location", {})
        lat, lon = loc.get("lat"), loc.get("lng")
        return f"place_id:{top['place_id']}", top.get("formatted_address", query), (lat, lon)
    return None, None, (None, None)

@st.cache_data(ttl=900, show_spinner=False)
def get_routes_from_place_ids(origin_pid: str, dest_pid: str, *, mode: str = "driving", avoid: str | None = None, max_routes: int = 3):
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin_pid,
        "destination": dest_pid,
        "mode": mode,
        "alternatives": "true",
        "key": GOOGLE_MAPS_API_KEY,
        "language": "zh-TW",
        "region": "tw",
    }
    if mode == "transit":
        params["departure_time"] = "now"
    if avoid:
        params["avoid"] = avoid

    try:
        r = SESSION.get(url, params=params, timeout=20)
        resp = r.json()
    except Exception:
        return []

    if resp.get("status") != "OK":
        return []
    routes = []
    for r in resp.get("routes", [])[:max_routes]:
        try:
            coords = polyline.decode(r["overview_polyline"]["points"])
            leg = r["legs"][0]
            routes.append((coords, leg["duration"]["value"], leg["distance"]["value"], leg["start_address"], leg["end_address"]))
        except Exception:
            continue
    return routes

# ======================== 氣象 ========================
def round_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)

def ow_current(lat, lon):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    try:
        r = SESSION.get(url, params=params, timeout=20)
        resp = r.json()
    except Exception:
        return ("查詢失敗", False, 0.0, 0, None)

    if "weather" not in resp:
        return ("查詢失敗", False, 0.0, 0, None)
    weather = resp["weather"][0]
    desc = weather.get("description", "") or "無資料"
    ow_code = int(weather.get("id") or 0)
    rain_mm = 0.0
    if isinstance(resp.get("rain"), dict):
        r = resp["rain"]
        try:
            rain_mm = float(r.get("1h") or r.get("3h") or 0.0)
        except Exception:
            rain_mm = 0.0
    ow_temp = None
    try:
        ow_temp = float(((resp.get("main") or {}).get("temp")))
    except Exception:
        ow_temp = None
    return desc, (rain_mm > OPEN_WEATHER_MIN_RAIN_MM), rain_mm, ow_code, ow_temp

def om_hourly_now_prob_precip_code(lat, lon):
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m",
        "hourly": "time,precipitation,precipitation_probability,weather_code",
        "forecast_days": 1, "timezone": "auto"
    }
    try:
        r = SESSION.get(base, params=params, timeout=20)
        resp = r.json()
    except Exception:
        return 0.0, 0, 0, 0.0, None, None

    hourly = resp.get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    precs = hourly.get("precipitation", []) or []
    probs = hourly.get("precipitation_probability", []) or []
    codes = hourly.get("weather_code", []) or []
    cur_temp = None
    try:
        cur_temp = float((resp.get("current") or {}).get("temperature_2m"))
    except Exception:
        cur_temp = None

    tz_offset_minutes = int((resp.get("utc_offset_seconds") or 0) / 60)
    now = datetime.now(timezone(timedelta(minutes=tz_offset_minutes)))
    target = round_to_hour(now).strftime("%Y-%m-%dT%H:00")

    idx = 0
    try:
        idx = times.index(target)
    except ValueError:
        idx = 0 if times else 0

    now_prec  = float(precs[idx]) if idx < len(precs) else 0.0
    now_prob  = int(probs[idx])   if idx < len(probs)   else 0
    now_code  = int(codes[idx])   if idx < len(codes)   else 0
    next_prec = float(precs[idx+1]) if (idx+1) < len(precs) else 0.0

    def _is_rain_code(c: int) -> bool:
        return (51 <= c <= 57) or (61 <= c <= 67) or (80 <= c <= 82) or (95 <= c <= 99)

    stop_time = None
    if now_prec > 0.0 or _is_rain_code(now_code):
        for j in range(idx+1, len(times)):
            p  = float(precs[j]) if j < len(precs) else 0.0
            pr = int(probs[j])   if j < len(probs)   else 0
            c  = int(codes[j])   if j < len(codes)   else 0
            if (p <= 0.0) and (pr < 30) and (not _is_rain_code(c)):
                try:
                    stop_time = times[j][11:16]
                except Exception:
                    stop_time = None
                break

    return now_prec, now_prob, now_code, next_prec, cur_temp, stop_time

def _grid_key(lat: float, lon: float, step: float = 0.02):
    return (round(lat/step)*step, round(lon/step)*step)

@st.cache_data(ttl=120, show_spinner=False)
def _ow_current_cached(lat, lon): return ow_current(lat, lon)

@st.cache_data(ttl=300, show_spinner=False)
def _om_hourly_cached(lat, lon): return om_hourly_now_prob_precip_code(lat, lon)

def get_weather_bundle(lat, lon):
    """整合目前與下一小時的氣象資訊。"""
    glat, glon = _grid_key(lat, lon)
    ow_desc, ow_now, ow_mm, ow_code, ow_temp = _ow_current_cached(glat, glon)
    om_now_prec, om_prob, om_code, om_next_prec, om_temp, om_stop = _om_hourly_cached(glat, glon)
    mm_now  = max(float(ow_mm or 0.0), float(om_now_prec or 0.0))
    mm_next = float(om_next_prec or 0.0)
    return {
        "ow_desc": ow_desc, "ow_now": ow_now, "ow_code": ow_code,
        "om_prob": om_prob, "om_code": om_code,
        "mm_now": mm_now, "mm_next": mm_next,
        "temp": (om_temp if om_temp is not None else ow_temp),
        "stop_time": om_stop
    }

# ======================== 風險邏輯（統一供各處使用） ========================
def is_thunder(code: int) -> bool:
    return THUNDER_MIN <= int(code) <= THUNDER_MAX

def effective_mm(mm_now: float, mm_next: float) -> float:
    """綜合雨量：以現在 mm_now 為主，加入下一小時 mm_next 的權重。"""
    try:
        return max(float(mm_now or 0.0), NEXT_MM_WEIGHT * float(mm_next or 0.0))
    except Exception:
        return float(mm_now or 0.0)

def bundle_is_rainy(b: dict) -> bool:
    """是否視為『有雨/需注意』的狀態（切段用）。"""
    return (float(b.get("mm_now", 0.0)) >= NEXT_MM_MIN_MM) \
        or (float(b.get("mm_next", 0.0)) >= NEXT_MM_MIN_MM) \
        or is_thunder(int(b.get("om_code", 0)))

def bundle_instant_risk(b: dict) -> float:
    """即時風險分數（0~1）：用於決定是否需要顯示地圖等。"""
    try:
        prob = float(b.get("om_prob", 0)) / 100.0
        r_now = 0.6 if float(b.get("mm_now", 0.0)) >= NEXT_MM_MIN_MM else 0.0
        r_next = 0.6 if float(b.get("mm_next", 0.0)) >= NEXT_MM_MIN_MM else 0.0
        r_thunder = 0.8 if is_thunder(int(b.get("om_code", 0))) else 0.0
        return max(prob, r_now, r_next, r_thunder)
    except Exception:
        return 0.0

def _rainfall_label(mm: float) -> str:
    try:
        v = float(mm or 0.0)
    except Exception:
        v = 0.0
    if v >= 50: return "豪大雨"
    if v >= 30: return "豪雨"
    if v >= 15: return "大雨"
    if v >= 7:  return "中雨"
    if v > 0:   return "小雨"
    return ""

# ======================== 分析與地圖 ========================
def sample_coords_by_distance(coords, interval_m):
    """依距離插值採樣，確保長段也能多取樣。"""
    if not coords or len(coords) < 2:
        return coords
    samples = [coords[0]]
    carry = 0.0
    last = coords[0]
    for i in range(1, len(coords)):
        p = coords[i]
        seg = geodesic(last, p).meters
        dist = carry + seg
        while dist >= interval_m and seg > 0:
            ratio = (interval_m - carry) / seg
            lat = last[0] + (p[0] - last[0]) * ratio
            lon = last[1] + (p[1] - last[1]) * ratio
            samples.append((lat, lon))
            last = (lat, lon)
            dist -= interval_m
            seg = geodesic(last, p).meters
        carry = dist
        last = p
    if samples[-1] != coords[-1]:
        samples.append(coords[-1])
    return samples

def sample_coords(coords, n_points):
    if len(coords) <= 2:
        return coords
    step = max(1, int(len(coords)/n_points))
    pts = coords[::step]
    if pts and pts[-1] != coords[-1]:
        pts[-1] = coords[-1]
    return pts

@st.cache_data(ttl=120, show_spinner=False)
def analyze_route(coords, total_distance_m, quick_scan_only=False):
    """以採樣點查天氣，再用完整軌跡切段；
    - 分段與分數邏輯統一：同時考慮 mm_now 與 mm_next（mm_next 以權重納入）。"""
    if not coords:
        return [], 0.0, 0.0, 0.0

    # 1) 採樣點
    if quick_scan_only:
        coords_to_check = sample_coords(coords, SAMPLES_FAST)
    else:
        interval_m = SAMPLE_INTERVAL_METERS if total_distance_m <= LONG_ROUTE_THRESHOLD_KM * 1000 else 1000
        coords_to_check = sample_coords_by_distance(coords, interval_m)

    # 2) 併發查天氣（僅採樣點）
    grid_points = {_grid_key(lat, lon): (lat, lon) for (lat, lon) in coords_to_check}
    weather_results_by_grid = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(get_weather_bundle, lat, lon): key for key, (lat, lon) in grid_points.items()}
        for f in as_completed(futs):
            key = futs[f]
            try:
                weather_results_by_grid[key] = f.result()
            except Exception:
                pass
    
    if not weather_results_by_grid:
        return [], 0.0, 0.0, 0.0

    # 3) 以採樣點計算風險（統一 mm_now/mm_next）
    rainy_cnt, mm_eff_sum = 0, 0.0
    for lat, lon in coords_to_check:
        b = weather_results_by_grid.get(_grid_key(lat, lon))
        if not b:
            continue
        mm_eff = effective_mm(b.get("mm_now", 0.0), b.get("mm_next", 0.0))
        mm_eff_sum += mm_eff
        if bundle_is_rainy(b):
            rainy_cnt += 1

    if quick_scan_only:
        return ([True] if rainy_cnt > 0 else []), 1.0 if rainy_cnt > 0 else 0.0, 0.0, 0.0

    # 4) 用完整軌跡切段（統一邏輯）
    segments = []
    cur_state = None
    cur_pts = []

    def get_weather_state_for_coord(lat, lon):
        grid = _grid_key(lat, lon)
        b = weather_results_by_grid.get(grid)
        if b is None:
            return cur_state if cur_state is not None else False
        return bundle_is_rainy(b)

    for (lat, lon) in coords:
        is_rain = get_weather_state_for_coord(lat, lon)
        if cur_state is None:
            cur_state = is_rain
            cur_pts.append((lat, lon))
        elif is_rain == cur_state:
            cur_pts.append((lat, lon))
        else:
            if len(cur_pts) >= 2:
                segments.append((cur_state, polyline.encode(cur_pts)))
            last_point = cur_pts[-1]
            cur_state = is_rain
            cur_pts = [last_point, (lat, lon)]

    if len(cur_pts) >= 2:
        segments.append((cur_state, polyline.encode(cur_pts)))
    
    # 5) 分數（雨段比例 + 綜合雨量 mm_eff）
    total_points = len(coords_to_check)
    rain_ratio = (rainy_cnt / total_points) if total_points else 0.0
    avg_mm_eff = (mm_eff_sum / total_points) if total_points else 0.0
    score = RAIN_RATIO_WEIGHT * rain_ratio + RAIN_INTENSITY_WEIGHT * (avg_mm_eff / 30.0)
    score = max(0.0, min(1.0, score))

    return segments, rain_ratio, avg_mm_eff, score

@st.cache_data(ttl=120)
def need_map_for_route(origin_pid, dest_pid, mode, avoid, *, dlat=None, dlon=None) -> bool:
    """是否值得顯示地圖（採用統一的 bundle_instant_risk 邏輯）"""
    coords = None
    if dlat is None or dlon is None:
        coords = get_one_route_coords(origin_pid, dest_pid, mode=mode, avoid=avoid)
        if not coords:
            return False
        dlat, dlon = coords[-1]

    B = get_weather_bundle(dlat, dlon)
    if bundle_instant_risk(B) >= SHOW_MAP_RISK_THRESHOLD:
        return True

    if coords is None:
        coords = get_one_route_coords(origin_pid, dest_pid, mode=mode, avoid=avoid)
        if not coords:
            return False

    for (lat, lon) in sample_coords(coords, SAMPLES_FAST):
        b = get_weather_bundle(lat, lon)
        if bundle_instant_risk(b) >= SHOW_MAP_RISK_THRESHOLD:
            return True
    return False

def get_one_route_coords(origin_pid: str, dest_pid: str, *, mode: str = "driving", avoid: str | None = None):
    rs = get_routes_from_place_ids(origin_pid, dest_pid, mode=mode, avoid=avoid, max_routes=1)
    return rs[0][0] if rs else []

@st.cache_data(ttl=3600)
def build_static_map_url(best_segs, origin_latlon, dest_latlon,
                         size=(640,640), scale=2, other_list=None, max_url_len=8192):
    """建立 Static Maps URL：
    - 先分組 paths（other_paths / best_paths / markers），最後再組合。
    - Fallback 直接重用已分組清單，避免重複生成。
    """
    base = "https://maps.googleapis.com/maps/api/staticmap"
    params = {"size": f"{size[0]}x{size[1]}", "scale": str(scale), "language": "zh-TW", "key": GOOGLE_MAPS_API_KEY}

    # 分組：其他路線 path
    other_paths = []
    other_list = other_list or []
    for coords, segs in other_list:
        try:
            enc_all = polyline.encode(coords)
            other_paths.append(("path", f"weight:3|color:{COLOR_GRAY}|enc:{enc_all}"))
            for is_rain, enc in segs:
                if is_rain:
                    other_paths.append(("path", f"weight:6|color:{COLOR_BLUE}|enc:{enc}"))
        except Exception:
            continue

    # 分組：最佳路線 path
    best_paths = []
    for is_rain, enc in best_segs:
        color = COLOR_BLUE if is_rain else COLOR_GREEN
        best_paths.append(("path", f"weight:7|color:{color}|enc:{enc}"))

    # 分組：標記
    markers = [
        ("markers", f"color:green|label:A|{origin_latlon[0]},{origin_latlon[1]}"),
        ("markers", f"color:red|label:B|{dest_latlon[0]},{dest_latlon[1]}"),
    ]

    # 組合主查詢
    query = other_paths + best_paths + markers + list(params.items())
    final_url = base + "?" + urlencode(query, doseq=True, quote_via=quote_plus)
    if len(final_url) <= max_url_len:
        return final_url

    # Fallback：只保留最佳路線與標記（直接重用已分組的列表），避免重複生成。
    fallback_query = best_paths + markers + list(params.items())
    return base + "?" + urlencode(fallback_query, doseq=True, quote_via=quote_plus)

def fetch_static_map_image(url: str):
    """安全抓圖（避免把 key 放到前端）"""
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        return r.content  # bytes
    except Exception:
        return None

# ======================== 介面 ========================
if "origin_q" not in st.session_state:
    st.session_state.origin_q = ""
if "dest_q" not in st.session_state:
    st.session_state.dest_q = ""
if k("mode") not in st.session_state:
    st.session_state[k("mode")] = "機車"

origin_q   = st.text_input("出發地（A）", key="origin_q", placeholder="輸入出發地")
dest_q     = st.text_input("目的地（B）", key="dest_q", placeholder="輸入目的地")
mode_label = st.selectbox("交通方式", ["機車","汽車","腳踏車","大眾運輸","走路"], key=k("mode"))
mode  = {"機車":"driving","汽車":"driving","腳踏車":"bicycling","大眾運輸":"transit","走路":"walking"}[mode_label]
avoid = "highways" if mode_label == "機車" else None

# 只有「查詢」按鈕在上方；重置待查詢後顯示在頁面底部
do_query = st.button("查詢", key=k("do_query"))

# ======================== 查詢 ========================
if do_query:
    if not origin_q or not dest_q:
        st.warning("請輸入出發地與目的地"); st.stop()

    with st.spinner("解析地點中…"):
        origin_pid, origin_label, (olat, olon) = geocode(origin_q)
        dest_pid,   dest_label,   (dlat, dlon)   = geocode(dest_q)
        if not origin_pid or not dest_pid:
            st.error("無法識別出發地或目的地"); st.stop()

        # 顯示標準化地址，避免定位誤差
        with st.container():
            st.caption(f"出發地：{origin_label}")
            st.caption(f"目的地：{dest_label}")

        st.subheader("目的地當前天氣")
        B = get_weather_bundle(dlat, dlon)

        def classify_phrase_and_icon(mm_now: float, prob: int, code_now: int, mm_next: float):
            """以現在 mm_now 為主，mm_next 作為補充提示（避免誤導當下天氣）。"""
            if (prob < 10) and (mm_now < NEXT_MM_MIN_MM) and not is_thunder(code_now):
                return "☁️ 無降雨", "無降雨"
            if is_thunder(code_now): return "⛈️ 雷陣雨", "雷陣雨"
            if mm_now >= 30: return "🌧️ 豪雨", "豪雨"
            if mm_now >= 15: return "🌧️ 大雨", "大雨"
            if mm_now >= 7:  return "🌦️ 陣雨（較大）", "陣雨（較大）"
            if mm_now >= 2:  return "🌦️ 陣雨", "陣雨"
            if mm_now > 0:   return "🌦️ 短暫陣雨", "短暫陣雨"
            if prob >= 50 or mm_next >= NEXT_MM_MIN_MM:
                return "☁️ 短暫陣雨（可能）", "短暫陣雨（可能）"
            return "☁️ 無降雨", "無降雨"

        def sky_icon_and_label(desc: str):
            if not desc:
                return "❓", "天氣資料暫不可用"
            d = desc.lower()
            if "查詢失敗" in desc:
                return "❓", "天氣資料暫不可用"
            if "overcast" in d or "陰" in d:
                return "☁️", "陰天"
            elif "cloud" in d or "雲" in d:
                return "🌤️", "多雲"
            else:
                return "☀️", "天氣晴"

        icon_text_B, B_phrase = classify_phrase_and_icon(B["mm_now"], B["om_prob"], B["om_code"], B["mm_next"])
        temp_text = f"{B['temp']:.1f}°C" if isinstance(B.get("temp"), (int, float)) else "—"

        if B_phrase == "無降雨":
            icon, sky_label = sky_icon_and_label(B["ow_desc"])
            st.success(f"{icon} {sky_label}｜🌡️ {temp_text}｜降雨機率 {B['om_prob']}%")
            # 可能有午後雷陣雨/下一小時降雨的提醒
            try:
                if bundle_instant_risk(B) >= SHOW_MAP_RISK_THRESHOLD:
                    st.info("提醒：稍後可能有陣雨或雷陣雨，請留意。")
            except Exception:
                pass
        else:
            st.error(f"{icon_text_B}｜🌡️ {temp_text}｜降雨機率 {B['om_prob']}%")
            if (B.get("mm_now") or 0) > 0:
                st.write(f"估計雨量：{B['mm_now']:.1f} mm/h（{_rainfall_label(B['mm_now'])}）")
            if B.get("stop_time"):
                st.write(f"預估雨停時間：{B['stop_time']}")

        need_map = need_map_for_route(origin_pid, dest_pid, mode=mode, avoid=avoid, dlat=dlat, dlon=dlon)
        if need_map:
            st.subheader("路線雨段地圖")
            progress_bar = st.progress(0, text="準備路線資料中…")

            progress_bar.progress(20, text="🗺️ 取得路線資訊…")
            routes = get_routes_from_place_ids(origin_pid, dest_pid, mode=mode, avoid=avoid, max_routes=3)

            if routes:
                scored = []
                total_routes = len(routes)
                for i, (coords, dur, dist, _, _) in enumerate(routes):
                    progress_bar.progress(20 + int((i+1)/total_routes * 70), text=f"🌦️ 分析路線天氣中… {i+1}/{total_routes}")
                    _segs, _rr, _mm_eff, score = analyze_route(coords, dist)
                    scored.append({"coords": coords, "duration": dur, "distance": dist, "score": score, "segs": _segs})

                progress_bar.progress(95, text="🎨 產生地圖中…")
                best   = sorted(scored, key=lambda x: (round(x["score"], 4), x["duration"]))[0]
                others = [r for r in scored if r is not best]

                url = build_static_map_url(
                    best_segs=best["segs"],
                    other_list=[(r["coords"], r["segs"]) for r in others],
                    origin_latlon=(olat, olon),
                    dest_latlon=(dlat, dlon),
                )
                img_bytes = fetch_static_map_image(url)
                progress_bar.empty()
                if img_bytes:
                    st.image(img_bytes, caption="藍色：有雨路段｜綠色：推薦路線之無雨段｜灰色：其他候選基底線")
                else:
                    st.warning("地圖載入失敗，請稍後再試")
            else:
                progress_bar.empty()
                st.warning("查不到可用路線，請更換交通方式或地點。")

        # 標記：已有結果（讓底部顯示「重置」按鈕）
        st.session_state["result_ready"] = True

# ======================== 重置（置於頁面最下方，僅在查詢後顯示） ========================
if st.session_state.get("result_ready"):
    st.markdown("---")
    cols = st.columns([1, 3])
    with cols[0]:
        st.button("重置", on_click=soft_reset_inputs)
