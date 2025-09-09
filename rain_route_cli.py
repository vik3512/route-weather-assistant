#!/usr/bin/env python
import os
import requests
import polyline
from datetime import datetime, timedelta
from dotenv import load_dotenv
import streamlit as st

# ================= 敏感度設定（維持高敏感，但不在 UI 顯示） =================
OPEN_WEATHER_MIN_RAIN_MM = 0.0       # 任何有值就視為雨
OPEN_METEO_MIN_RAIN_MM   = 0.0
OPEN_METEO_NEXT_HOUR_PROB_THRESHOLD = 30
MAX_SAMPLE_POINTS = 16
# ======================================================================

# ---------- 載入環境變數 ----------
load_dotenv()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")

# ---------- Google Geocoding: 地址 → place_id ----------
def resolve_place(query: str):
    """將地址轉換成 place_id 與標準化地址"""
    if not GOOGLE_MAPS_API_KEY:
        return None, None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": query,
        "key": GOOGLE_MAPS_API_KEY,
        "language": "zh-TW",
        "region": "tw",
        "components": "country:TW",
    }
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") == "OK" and resp.get("results"):
        top = resp["results"][0]
        return f"place_id:{top['place_id']}", top.get("formatted_address", query)
    return None, None

# ---------- 座標 → 行政區｜主要幹道路名 ----------
def reverse_geocode_district_and_road(lat: float, lon: float) -> str:
    """
    優先回傳「行政區｜道路名」，例：松山區｜南京東路四段、板橋區｜文化路一段。
    若無路名則只回行政區；都缺時回格式化地址或座標。
    """
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lon}", "key": GOOGLE_MAPS_API_KEY, "language": "zh-TW", "region": "tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") != "OK" or not resp.get("results"):
        return f"{lat:.4f},{lon:.4f}"

    best_admin3 = None   # 區/鎮/市
    best_locality = None # 市/鄉鎮
    best_admin2 = None   # 縣/市
    best_route = None    # 路名/幹道

    # 掃描所有結果，抓到第一個合適的行政區與路名
    for res in resp.get("results", []):
        for c in res.get("address_components", []):
            t = c.get("types", [])
            if "route" in t and not best_route:
                best_route = c.get("long_name")
            if "administrative_area_level_3" in t and not best_admin3:
                best_admin3 = c.get("long_name")
            if "locality" in t and not best_locality:
                best_locality = c.get("long_name")
            if "administrative_area_level_2" in t and not best_admin2:
                best_admin2 = c.get("long_name")

    district = best_admin3 or best_locality or best_admin2
    if district and best_route:
        return f"{district}｜{best_route}"
    if district:
        return district
    return resp["results"][0].get("formatted_address", f"{lat:.4f},{lon:.4f}")

# ---------- 路線規劃 ----------
def get_route_from_place_ids(origin_pid: str, dest_pid: str, mode: str = "driving"):
    if not GOOGLE_MAPS_API_KEY:
        raise Exception("缺少 GOOGLE_MAPS_API_KEY")
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": origin_pid,
        "destination": dest_pid,
        "mode": mode,
        "key": GOOGLE_MAPS_API_KEY,
        "language": "zh-TW",
        "region": "tw",
    }
    if mode == "transit":
        params["departure_time"] = "now"

    resp = requests.get(url, params=params, timeout=30).json()
    status = resp.get("status")
    if status == "ZERO_RESULTS":
        raise Exception("找不到路線，請更換交通方式或地址")
    if status == "REQUEST_DENIED":
        raise Exception(f"API 被拒絕: {resp.get('error_message', '')}")
    if status != "OK":
        raise Exception(f"API 錯誤: {status}")

    route = resp["routes"][0]
    coords = polyline.decode(route["overview_polyline"]["points"])
    leg = route["legs"][0]
    duration_sec = leg["duration"]["value"]
    arrival_time = datetime.now() + timedelta(seconds=duration_sec)
    return coords, duration_sec, arrival_time, leg["start_address"], leg["end_address"]

# ---------- OpenWeather：即時 ----------
def ow_current(lat, lon):
    """回傳 (desc, is_rain, rain_mm_1h)"""
    if not OPENWEATHER_API_KEY:
        raise Exception("缺少 OPENWEATHER_API_KEY")
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if "weather" not in resp:
        return f"查詢失敗：{resp.get('message', 'unknown')}", False, 0.0

    weather = resp["weather"][0]
    desc = weather.get("description", "") or "無資料"
    main_lower = weather.get("main", "").lower()
    desc_lower = desc.lower()

    rain_mm = 0.0
    if isinstance(resp.get("rain"), dict):
        rain_mm = float(resp["rain"].get("1h") or resp["rain"].get("3h") or 0.0)

    is_rain = (
        (rain_mm > OPEN_WEATHER_MIN_RAIN_MM)
        or ("rain" in main_lower)
        or ("雨" in desc_lower)
        or ("雷陣雨" in desc_lower)
        or ("毛毛雨" in desc_lower)
        or ("陣雨" in desc_lower)
        or ("濛濛雨" in desc_lower)
        or ("小雨" in desc_lower)
    )
    return desc, is_rain, rain_mm

# ---------- OpenWeather：1小時內是否會下 ----------
def ow_forecast_next_hours_is_rain(lat, lon, hours=1):
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if "list" not in resp:
        return False
    now = datetime.now()
    limit = now + timedelta(hours=hours)
    for item in resp["list"]:
        t = datetime.fromtimestamp(item["dt"])
        if t > limit:
            break
        w = item["weather"][0]
        main = w.get("main", "").lower()
        desc = w.get("description", "").lower()
        rain_mm = 0.0
        if isinstance(item.get("rain"), dict):
            rain_mm = float(item["rain"].get("3h") or item["rain"].get("1h") or 0.0)
        if (rain_mm > OPEN_WEATHER_MIN_RAIN_MM) or ("rain" in main) or ("雨" in desc):
            return True
    return False

# ---------- Open-Meteo：免金鑰 ----------
def om_now_and_next_hour(lat, lon):
    """回傳 (is_rain_now, is_rain_soon)"""
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "precipitation,weather_code",
        "hourly": "precipitation,precipitation_probability,weather_code",
        "forecast_days": 1,
        "timezone": "auto",
    }
    resp = requests.get(base, params=params, timeout=20).json()

    def code_is_rain(code: int) -> bool:
        return (51 <= code <= 67) or (80 <= code <= 99)

    cur = resp.get("current", {})
    cur_prec = float(cur.get("precipitation", 0.0) or 0.0)
    cur_code = int(cur.get("weather_code") or 0)
    is_now = (cur_prec > OPEN_METEO_MIN_RAIN_MM) or code_is_rain(cur_code)

    hourly = resp.get("hourly", {})
    prec_list = hourly.get("precipitation", []) or []
    prob_list = hourly.get("precipitation_probability", []) or []
    code_list = hourly.get("weather_code", []) or []

    is_soon = False
    if prec_list:
        next_prec = float(prec_list[0] or 0.0)
        next_prob = int((prob_list[0] or 0))
        next_code = int((code_list[0] or 0))
        is_soon = (
            (next_prec > OPEN_METEO_MIN_RAIN_MM)
            or (next_prob >= OPEN_METEO_NEXT_HOUR_PROB_THRESHOLD)
            or code_is_rain(next_code)
        )
    return is_now, is_soon

# ---------- 綜合判斷：任一來源認定即視為雨 ----------
def is_rain_consensus(lat, lon):
    """回傳 (is_rain_now, is_rain_next_hour)"""
    _, ow_now, ow_rain_mm = ow_current(lat, lon)
    ow_soon = ow_forecast_next_hours_is_rain(lat, lon, hours=1)
    try:
        om_now, om_soon = om_now_and_next_hour(lat, lon)
    except Exception:
        om_now, om_soon = (False, False)
    now_rain = ow_now or (ow_rain_mm > OPEN_WEATHER_MIN_RAIN_MM) or om_now
    soon_rain = ow_soon or om_soon
    return now_rain, soon_rain

# ---------- Streamlit 頁面 ----------
st.set_page_config(page_title="路線天氣助手", page_icon="🌦", layout="centered")
st.title("🌦 路線天氣助手")

# ===== API 金鑰檢查 =====
if not GOOGLE_MAPS_API_KEY or not OPENWEATHER_API_KEY:
    st.error("⚠️ 請先在 `.env` 檔設定 GOOGLE_MAPS_API_KEY 與 OPENWEATHER_API_KEY")
    st.stop()

# Reset Seed：控制所有 widget key；硬重置時一併清空
if "reset_seed" not in st.session_state:
    st.session_state.reset_seed = 0

# 控制重置按鈕出現時機
if "query_done" not in st.session_state:
    st.session_state.query_done = False

# 動態 Keys（避免殘留）
origin_key = f"origin_{st.session_state.reset_seed}"
dest_key   = f"dest_{st.session_state.reset_seed}"
mode_key   = f"mode_{st.session_state.reset_seed}"

# ============ 輸入區 ============
st.subheader("輸入查詢資訊")
origin_q = st.text_input("出發地（A）", key=origin_key)
dest_q = st.text_input("目的地（B）", key=dest_key)

mode_label = st.selectbox(
    "交通方式",
    ["機車", "汽車", "腳踏車", "大眾運輸", "走路"],
    index=0,
    key=mode_key,
)
mode_map = {"機車": "driving", "汽車": "driving", "腳踏車": "bicycling", "大眾運輸": "transit", "走路": "walking"}
mode = mode_map[mode_label]

# ===== 查詢 =====
if st.button("查詢"):
    if not origin_q or not dest_q:
        st.warning("請輸入出發地與目的地")
        st.stop()

    with st.spinner("解析地點中…"):
        origin_pid, _ = resolve_place(origin_q)
        if not origin_pid:
            st.error("無法識別出發地"); st.stop()
        dest_pid, _ = resolve_place(dest_q)
        if not dest_pid:
            st.error("無法識別目的地"); st.stop()

    try:
        with st.spinner("規劃路線中…"):
            coords, duration_sec, _, origin_label_full, dest_label_full = \
                get_route_from_place_ids(origin_pid, dest_pid, mode)
    except Exception as e:
        st.error(str(e)); st.stop()

    total_min = int(round(duration_sec / 60))
    st.subheader("查詢結果")
    st.write(f"**路線**：{origin_label_full} → {dest_label_full}（{mode_label}）")
    st.write(f"**預估行程時間**：{total_min} 分鐘")

    # ===== 沿途天氣檢查（列 行政區｜路名）=====
    st.subheader("沿途天氣檢查")
    if duration_sec <= 15 * 60:
        n_points = 6
    elif duration_sec <= 30 * 60:
        n_points = 9
    elif duration_sec <= 60 * 60:
        n_points = 12
    else:
        n_points = MAX_SAMPLE_POINTS

    step = max(1, int(len(coords) / n_points))
    sample_points = coords[::step]
    if sample_points[-1] != coords[-1]:
        sample_points[-1] = coords[-1]

    rainy_labels = []
    seen = set()
    for (lat, lon) in sample_points:
        now_rain, soon_rain = is_rain_consensus(lat, lon)
        if now_rain or soon_rain:
            label = reverse_geocode_district_and_road(lat, lon)  # 行政區｜路名（若無路名→只區名）
            if label not in seen:
                seen.add(label)
                rainy_labels.append(label)

    if rainy_labels:
        st.error("沿途下雨區域：\n- " + "\n- ".join(rainy_labels))
    else:
        st.success("沿途多半無雨。")

    # ===== 目的地天氣（僅顯示結論）=====
    st.subheader("目的地天氣")
    dest_lat, dest_lon = coords[-1]
    now_rain, soon_rain = is_rain_consensus(dest_lat, dest_lon)
    st.write(f"**抵達時段**：{'下雨/可能下雨' if (now_rain or soon_rain) else '多半無雨'}")

    st.session_state.query_done = True

# ===== 單一重置鍵（完全清空）=====
if st.session_state.query_done:
    st.markdown("---")
    if st.button("重置"):
        st.session_state.clear()
        st.rerun()
