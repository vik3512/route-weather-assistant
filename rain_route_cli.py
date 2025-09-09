#!/usr/bin/env python
import os
import requests
import polyline
from datetime import datetime, timedelta
from dotenv import load_dotenv
import streamlit as st

# ================= 靈敏度設定（維持高敏感） =================
OPEN_WEATHER_MIN_RAIN_MM = 0.0
OPEN_METEO_MIN_RAIN_MM   = 0.0
OPEN_METEO_NEXT_HOUR_PROB_THRESHOLD = 30
MAX_SAMPLE_POINTS = 16
# ===========================================================

# ---------- 載入環境變數 ----------
load_dotenv()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")

# ---------- 地址 → place_id ----------
def resolve_place(query: str):
    if not GOOGLE_MAPS_API_KEY:
        return None, None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": query, "key": GOOGLE_MAPS_API_KEY,
        "language": "zh-TW", "region": "tw", "components": "country:TW",
    }
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") == "OK" and resp.get("results"):
        top = resp["results"][0]
        return f"place_id:{top['place_id']}", top.get("formatted_address", query)
    return None, None

# ---------- 座標 → 行政區｜主要幹道路名 ----------
def reverse_geocode_district_and_road(lat: float, lon: float) -> str:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lon}", "key": GOOGLE_MAPS_API_KEY, "language": "zh-TW", "region": "tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") != "OK" or not resp.get("results"):
        return f"{lat:.4f},{lon:.4f}"

    best_admin3 = best_locality = best_admin2 = best_route = None
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
        "origin": origin_pid, "destination": dest_pid, "mode": mode,
        "key": GOOGLE_MAPS_API_KEY, "language": "zh-TW", "region": "tw",
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
        or ("rain" in main_lower) or ("雨" in desc_lower)
        or ("雷陣雨" in desc_lower) or ("毛毛雨" in desc_lower)
        or ("陣雨" in desc_lower) or ("濛濛雨" in desc_lower) or ("小雨" in desc_lower)
    )
    return desc, is_rain, rain_mm

# ---------- OpenWeather：預估多久雨停（3h 粒度） ----------
def ow_forecast_rain_stop_hours(lat, lon):
    """回傳：預估幾小時後雨停（None 表示找不到持續雨段或無法估算）"""
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if "list" not in resp:
        return None
    now = datetime.now()
    rain_hours = 0
    for item in resp["list"]:
        t = datetime.fromtimestamp(item["dt"])
        if t < now:
            continue
        w = item["weather"][0]
        desc = w.get("description", "").lower()
        main = w.get("main", "").lower()
        rain_mm = 0.0
        if isinstance(item.get("rain"), dict):
            rain_mm = float(item["rain"].get("3h") or item["rain"].get("1h") or 0.0)
        is_rain = (rain_mm > 0) or ("rain" in main) or ("雨" in desc)
        if is_rain:
            rain_hours += 3
        else:
            break
    return rain_hours if rain_hours > 0 else None

# ---------- Open-Meteo：即時 + 下一小時機率 + code ----------
def om_now_prob_precip_code(lat, lon):
    """
    回傳 (is_rain_now, prob_next_percent, current_precip_mm, weather_code_now)
    """
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "precipitation,weather_code",
        "hourly": "precipitation_probability,precipitation,weather_code",
        "forecast_days": 1, "timezone": "auto",
    }
    resp = requests.get(base, params=params, timeout=20).json()

    def code_is_rain(code: int) -> bool:
        return (51 <= code <= 67) or (80 <= code <= 99)

    cur = resp.get("current", {})
    cur_prec = float(cur.get("precipitation", 0.0) or 0.0)
    cur_code = int(cur.get("weather_code") or 0)
    is_now = (cur_prec > OPEN_METEO_MIN_RAIN_MM) or code_is_rain(cur_code)

    hourly = resp.get("hourly", {})
    prob_list = hourly.get("precipitation_probability", []) or []
    prob_next = int(prob_list[0]) if prob_list else 0

    return is_now, prob_next, cur_prec, cur_code

# ---------- 用語分級（直觀版） ----------
def classify_rain_phrase(mm_per_hr: float, prob_next: int, weather_code_now: int) -> str:
    """
    文字用語優先序：
      1) 95–99 → 雷陣雨
      2) mm ≥ 30 → 豪雨
      3) 15 ≤ mm < 30 → 大雨
      4) 7 ≤ mm < 15 → 陣雨（較大）
      5) 2 ≤ mm < 7 → 陣雨
      6) 0 < mm < 2 → 短暫陣雨
      7) mm == 0 且 機率 ≥ 50% → 短暫陣雨（可能）
      8) 其餘 → 無降雨
    """
    if 95 <= weather_code_now <= 99:
        return "雷陣雨"
    if mm_per_hr >= 30:
        return "豪雨"
    if mm_per_hr >= 15:
        return "大雨"
    if mm_per_hr >= 7:
        return "陣雨（較大）"
    if mm_per_hr >= 2:
        return "陣雨"
    if mm_per_hr > 0:
        return "短暫陣雨"
    if prob_next >= 50:
        return "短暫陣雨（可能）"
    return "無降雨"

# ---------- 沿途：現在是否下雨（共識） ----------
def is_rain_now_consensus(lat, lon):
    _, ow_now, ow_mm = ow_current(lat, lon)
    try:
        om_now, _, _, _ = om_now_prob_precip_code(lat, lon)
    except Exception:
        om_now = False
    return ow_now or (ow_mm > OPEN_WEATHER_MIN_RAIN_MM) or om_now

# ========== Streamlit UI ==========
st.set_page_config(page_title="路線天氣助手", page_icon="🌦", layout="centered")
st.title("🌦 路線天氣助手")

# 金鑰檢查
if not GOOGLE_MAPS_API_KEY or not OPENWEATHER_API_KEY:
    st.error("⚠️ 請先在 `.env` 檔設定 GOOGLE_MAPS_API_KEY 與 OPENWEATHER_API_KEY")
    st.stop()

# seed 與狀態
if "reset_seed" not in st.session_state:
    st.session_state.reset_seed = 0
if "query_done" not in st.session_state:
    st.session_state.query_done = False

# 動態 keys
origin_key = f"origin_{st.session_state.reset_seed}"
dest_key   = f"dest_{st.session_state.reset_seed}"
mode_key   = f"mode_{st.session_state.reset_seed}"

# 輸入區
st.subheader("輸入查詢資訊")
origin_q = st.text_input("出發地（A）", key=origin_key)
dest_q   = st.text_input("目的地（B）", key=dest_key)
mode_label = st.selectbox("交通方式", ["機車","汽車","腳踏車","大眾運輸","走路"], index=0, key=mode_key)
mode = {"機車":"driving","汽車":"driving","腳踏車":"bicycling","大眾運輸":"transit","走路":"walking"}[mode_label]

# 查詢
if st.button("查詢"):
    if not origin_q or not dest_q:
        st.warning("請輸入出發地與目的地"); st.stop()

    with st.spinner("解析地點中…"):
        origin_pid, _ = resolve_place(origin_q)
        if not origin_pid: st.error("無法識別出發地"); st.stop()
        dest_pid, _ = resolve_place(dest_q)
        if not dest_pid: st.error("無法識別目的地"); st.stop()

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

    # 沿途天氣（行政區｜路名）
    st.subheader("沿途天氣檢查")
    if duration_sec <= 15*60: n_points = 6
    elif duration_sec <= 30*60: n_points = 9
    elif duration_sec <= 60*60: n_points = 12
    else: n_points = MAX_SAMPLE_POINTS

    step = max(1, int(len(coords) / n_points))
    sample_points = coords[::step]
    if sample_points[-1] != coords[-1]:
        sample_points[-1] = coords[-1]

    rainy_labels, seen = [], set()
    for (lat, lon) in sample_points:
        if is_rain_now_consensus(lat, lon):
            label = reverse_geocode_district_and_road(lat, lon)
            if label not in seen:
                seen.add(label); rainy_labels.append(label)

    if rainy_labels:
        st.error("沿途下雨區域：\n- " + "\n- ".join(rainy_labels))
    else:
        st.success("沿途多半無雨。")

    # 目的地天氣（文字用語 + 機率 + 雨量 + 何時雨停）
    st.subheader("目的地天氣")
    dest_lat, dest_lon = coords[-1]

    # 即時雨量與機率
    _, ow_now, ow_mm = ow_current(dest_lat, dest_lon)
    try:
        om_now, prob_next, om_mm, code_now = om_now_prob_precip_code(dest_lat, dest_lon)
    except Exception:
        om_now, prob_next, om_mm, code_now = (False, 0, 0.0, 0)

    mm_est = max(float(ow_mm or 0.0), float(om_mm or 0.0))
    phrase = classify_rain_phrase(mm_est, prob_next, code_now)
    now_rain = (phrase != "無降雨" and not phrase.endswith("（可能）")) or ow_now or om_now

    # 預估雨停
    rain_stop_hours = ow_forecast_rain_stop_hours(dest_lat, dest_lon)

    if now_rain or phrase.startswith("短暫陣雨"):
        st.error(f"抵達時段：{phrase}")
        st.write(f"**降雨機率**：{prob_next}%")
        st.write(f"**雨量**：約 {mm_est:.1f} mm/h")
        if rain_stop_hours:
            st.write(f"**預估雨停時間**：約 {rain_stop_hours} 小時後")
    else:
        st.success(f"抵達時段：{phrase}")

    st.session_state.query_done = True

# 單一重置鍵（確保 key 改變）
if st.session_state.query_done:
    st.markdown("---")
    if st.button("重置"):
        st.session_state.reset_seed = st.session_state.get("reset_seed", 0) + 1
        for k in list(st.session_state.keys()):
            if k != "reset_seed":
                del st.session_state[k]
        st.rerun()
