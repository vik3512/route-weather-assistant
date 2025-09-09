#!/usr/bin/env python
import os
import requests
import polyline
from datetime import datetime, timedelta
from dotenv import load_dotenv
import streamlit as st

# ================= 敏感度設定（可自行微調） =================
# 任何來源只要 > 這個降雨量（mm）就算下雨；越小越敏感
OPEN_WEATHER_MIN_RAIN_MM = 0.0   # 0.0 代表只要有數值就視為雨
OPEN_METEO_MIN_RAIN_MM   = 0.0   # 同上

# 下一小時「可能會下雨」的機率門檻（%）；越小越敏感
OPEN_METEO_NEXT_HOUR_PROB_THRESHOLD = 30   # 原先 50 → 改 30

# 沿途取樣點數上限（越大越密，API 會多打一些）
MAX_SAMPLE_POINTS = 16
# ==========================================================

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

# ---------- 座標 → 區名｜主要幹道路名 ----------
def reverse_geocode_road_with_district(lat: float, lon: float) -> str:
    """
    優先回傳「行政區｜道路名」，例：松山區｜南京東路四段、桃園區｜國道1號。
    若缺少道路名，退回「行政區」；都缺才用格式化地址或座標。
    """
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lon}", "key": GOOGLE_MAPS_API_KEY, "language": "zh-TW", "region": "tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") != "OK" or not resp.get("results"):
        return f"{lat:.4f},{lon:.4f}"

    best_admin3 = None   # 區/鎮/市
    best_locality = None # 市/鄉鎮
    best_admin2 = None   # 縣/市
    best_route = None

    for res in resp.get("results", []):
        for c in res.get("address_components", []):
            types = c.get("types", [])
            if "route" in types and not best_route:
                best_route = c.get("long_name")
            if "administrative_area_level_3" in types and not best_admin3:
                best_admin3 = c.get("long_name")
            if "locality" in types and not best_locality:
                best_locality = c.get("long_name")
            if "administrative_area_level_2" in types and not best_admin2:
                best_admin2 = c.get("long_name")

    district = best_admin3 or best_locality or best_admin2
    if best_route and district:
        return f"{district}｜{best_route}"
    if best_route:
        return best_route
    if district:
        return district

    first = resp["results"][0]
    return first.get("formatted_address", f"{lat:.4f},{lon:.4f}")

# ---------- 路線規劃 ----------
def get_route_from_place_ids(origin_pid: str, dest_pid: str, mode: str = "driving"):
    """根據 place_id 規劃路線"""
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

# ---------- OpenWeather：即時天氣 ----------
def ow_current(lat, lon):
    """OpenWeather 現況：回傳 (desc, is_rain, rain_mm_1h)"""
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

    # 更敏感：只要有降雨量 or 文案含雨就算雨
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

# ---------- OpenWeather：3小時預報（目的地用來估未來是否仍雨） ----------
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

# ---------- Open-Meteo：免金鑰第二來源 ----------
def om_now_and_next_hour(lat, lon):
    """
    回傳 (is_rain_now, is_rain_soon)
    依據 current.precipitation、current.weather_code 以及下一小時的 precipitation/probability。
    """
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
        # 51-67（毛毛雨/雨夾雪/凍雨），80-99（陣雨/雷雨/冰雹/對流）視為雨
        return (51 <= code <= 67) or (80 <= code <= 99)

    # 現在
    cur = resp.get("current", {})
    cur_prec = float(cur.get("precipitation", 0.0) or 0.0)
    cur_code = int(cur.get("weather_code") or 0)
    is_now = (cur_prec > OPEN_METEO_MIN_RAIN_MM) or code_is_rain(cur_code)

    # 下一小時（取第一筆未來小時資料）
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

# ---------- 綜合判斷：任一來源認定為雨即視為下雨 ----------
def is_rain_consensus(lat, lon):
    """
    回傳 (desc_text, is_rain_now, is_rain_next_hour)
    - 來源A：OpenWeather 現況 + 1小時內預報
    - 來源B：Open-Meteo 現況 + 下一小時
    """
    desc, ow_now, ow_rain_mm = ow_current(lat, lon)
    ow_soon = ow_forecast_next_hours_is_rain(lat, lon, hours=1)

    try:
        om_now, om_soon = om_now_and_next_hour(lat, lon)
    except Exception:
        om_now, om_soon = (False, False)

    now_rain = ow_now or (ow_rain_mm > OPEN_WEATHER_MIN_RAIN_MM) or om_now
    soon_rain = ow_soon or om_soon

    detail = []
    detail.append(f"OpenWeather：{desc}{'｜有降雨量' if ow_rain_mm>OPEN_WEATHER_MIN_RAIN_MM else ''}")
    detail.append(f"Open-Meteo：{'下雨' if om_now else '無雨'}；1小時內{'可能' if om_soon else '不太可能'}下雨")
    return "；".join(detail), now_rain, soon_rain

# ---------- Streamlit 頁面設定 ----------
st.set_page_config(page_title="路線天氣助手", page_icon="🌦", layout="centered")
st.title("🌦 路線天氣助手（高敏感版）")

# ===== API 金鑰檢查（先擋，避免後續操作）=====
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

# ============ 出發地與目的地輸入框 ============
st.subheader("輸入查詢資訊")
origin_q = st.text_input("出發地（A）", key=origin_key)
dest_q = st.text_input("目的地（B）", key=dest_key)

# ============ 交通方式選擇 ============
mode_label = st.selectbox(
    "交通方式",
    ["機車", "汽車", "腳踏車", "大眾運輸", "走路"],
    index=0,
    key=mode_key,
)
mode_map = {
    "機車": "driving",
    "汽車": "driving",
    "腳踏車": "bicycling",
    "大眾運輸": "transit",
    "走路": "walking",
}
mode = mode_map[mode_label]

# ===== 查詢按鈕 =====
if st.button("查詢"):
    if not origin_q or not dest_q:
        st.warning("請輸入出發地與目的地")
        st.stop()

    with st.spinner("解析地點中…"):
        origin_pid, origin_label = resolve_place(origin_q)
        if not origin_pid:
            st.error("無法識別出發地")
            st.stop()
        dest_pid, dest_label = resolve_place(dest_q)
        if not dest_pid:
            st.error("無法識別目的地")
            st.stop()

    try:
        with st.spinner("規劃路線中…"):
            coords, duration_sec, arrival_time, origin_label_full, dest_label_full = \
                get_route_from_place_ids(origin_pid, dest_pid, mode)
    except Exception as e:
        st.error(str(e))
        st.stop()

    total_min = int(round(duration_sec / 60))
    st.subheader("查詢結果")
    st.write(f"**路線**：{origin_label_full} → {dest_label_full}（{mode_label}）")
    st.write(f"**預估行程時間**：{total_min} 分鐘")

    # 沿途天氣檢查（更密集）
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

    rainy_segments = []
    seen = set()
    for (lat, lon) in sample_points:
        detail, now_rain, soon_rain = is_rain_consensus(lat, lon)
        if now_rain or soon_rain:
            label = reverse_geocode_road_with_district(lat, lon)
            key = label or f"{lat:.4f},{lon:.4f}"
            if key not in seen:
                seen.add(key)
                rainy_segments.append(f"{label}（{ '正在下雨' if now_rain else '可能將下雨' }｜{detail}）")

    if rainy_segments:
        st.error("沿途下雨路段：\n- " + "\n- ".join(rainy_segments))
    else:
        st.success("沿途多半無雨（高敏感模式仍未偵測到顯著降雨）。")

    # 目的地天氣（即時 + 下一小時）
    st.subheader("目的地天氣")
    dest_lat, dest_lon = coords[-1]
    detail, now_rain, soon_rain = is_rain_consensus(dest_lat, dest_lon)
    st.write(f"**抵達時段**：{ '下雨/可能下雨' if (now_rain or soon_rain) else '多半無雨' }")
    st.caption(detail)

    st.session_state.query_done = True

# ===== 查詢完成後才顯示重置按鈕 =====
if st.session_state.query_done:
    st.markdown("---")
    cols = st.columns(2)
    with cols[0]:
        if st.button("重置（硬重置，建議）"):
            st.session_state.clear()   # 全清
            st.rerun()
    with cols[1]:
        if st.button("重置（只清 A/B）"):
            for key in list(st.session_state.keys()):
                if key.startswith(("origin_", "dest_")):
                    del st.session_state[key]
            st.session_state.reset_seed = st.session_state.get("reset_seed", 0) + 1
            st.session_state.query_done = False
            st.rerun()
