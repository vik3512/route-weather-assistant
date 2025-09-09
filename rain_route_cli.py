#!/usr/bin/env python
import os
import requests
import polyline
from datetime import datetime, timedelta
from dotenv import load_dotenv
import streamlit as st

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

# ---------- Google Geocoding: 座標 → 簡短地區名 ----------
def reverse_geocode_short(lat: float, lon: float) -> str:
    """座標 → 簡短地區名"""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lon}", "key": GOOGLE_MAPS_API_KEY, "language": "zh-TW", "region": "tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") != "OK" or not resp.get("results"):
        return f"{lat:.4f},{lon:.4f}"

    comps = resp["results"][0].get("address_components", [])
    prefer = [
        "sublocality_level_1", "sublocality", "locality",
        "administrative_area_level_3", "administrative_area_level_2", "route"
    ]
    found = {k: None for k in prefer}
    for c in comps:
        for t in c.get("types", []):
            if t in found and not found[t]:
                found[t] = c.get("long_name")
    for key in prefer:
        if found.get(key):
            return found[key]
    return resp["results"][0].get("formatted_address", f"{lat:.4f},{lon:.4f}")

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

# ---------- 天氣查詢 ----------
def check_weather(lat, lon):
    """回傳 (天氣描述, 是否下雨)"""
    if not OPENWEATHER_API_KEY:
        raise Exception("缺少 OPENWEATHER_API_KEY")
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if "weather" not in resp:
        return f"查詢失敗：{resp.get('message', 'unknown')}", False
    weather = resp["weather"][0]
    desc = weather.get("description", "無資料")
    is_rain = "rain" in weather.get("main", "").lower()
    return desc, is_rain

# ---------- 預測目的地降雨時間 ----------
def forecast_rain_duration(lat, lon):
    """查詢未來幾小時是否會持續下雨"""
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    now = datetime.now()
    rain_hours = 0
    for item in resp.get("list", []):
        t = datetime.fromtimestamp(item["dt"])
        if t < now:
            continue
        main = item["weather"][0]["main"].lower()
        if "rain" in main:
            rain_hours += 3
        else:
            break
    return "目前降雨，可能很快就停。" if rain_hours == 0 else f"預估降雨將持續約 {rain_hours} 小時後停止。"

# ---------- Streamlit 頁面設定 ----------
st.set_page_config(page_title="路線天氣助手", page_icon="🌦", layout="centered")
st.title("🌦 路線天氣助手")

# Reset Seed：用於強制清空輸入框
if "reset_seed" not in st.session_state:
    st.session_state.reset_seed = 0

# 動態 Keys
origin_key = f"origin_{st.session_state.reset_seed}"
dest_key   = f"dest_{st.session_state.reset_seed}"

# ============ 出發地與目的地輸入框 ============
st.subheader("輸入查詢資訊")
origin_q = st.text_input("出發地（A）", key=origin_key)
dest_q = st.text_input("目的地（B）", key=dest_key)

# ============ 交通方式選擇 ============
mode_label = st.selectbox(
    "交通方式",
    ["機車", "汽車", "腳踏車", "大眾運輸", "走路"],
    index=0
)
mode_map = {
    "機車": "driving",
    "汽車": "driving",
    "腳踏車": "bicycling",
    "大眾運輸": "transit",
    "走路": "walking",
}
mode = mode_map[mode_label]

# ===== API 金鑰檢查 =====
if not GOOGLE_MAPS_API_KEY or not OPENWEATHER_API_KEY:
    st.error("⚠️ 請先在 `.env` 檔設定 GOOGLE_MAPS_API_KEY 與 OPENWEATHER_API_KEY")
    st.stop()

# ===== 查詢按鈕 =====
if st.button("查詢"):
    if not origin_q or not dest_q:
        st.warning("請輸入出發地與目的地")
        st.stop()

    # 解析地址
    with st.spinner("解析地點中…"):
        origin_pid, origin_label = resolve_place(origin_q)
        if not origin_pid:
            st.error("無法識別出發地")
            st.stop()
        dest_pid, dest_label = resolve_place(dest_q)
        if not dest_pid:
            st.error("無法識別目的地")
            st.stop()

    # 規劃路線
    try:
        with st.spinner("規劃路線中…"):
            coords, duration_sec, arrival_time, origin_label_full, dest_label_full = \
                get_route_from_place_ids(origin_pid, dest_pid, mode)
    except Exception as e:
        st.error(str(e))
        st.stop()

    # 計算預估時間
    total_min = int(round(duration_sec / 60))
    st.subheader("查詢結果")
    st.write(f"**路線**：{origin_label_full} → {dest_label_full}（{mode_label}）")
    st.write(f"**預估行程時間**：{total_min} 分鐘")

    # 沿途天氣檢查（每段取樣）
    st.subheader("沿途天氣檢查")
    if duration_sec <= 15 * 60:
        n_points = 3
    elif duration_sec <= 30 * 60:
        n_points = 5
    elif duration_sec <= 60 * 60:
        n_points = 7
    else:
        n_points = 10

    step = max(1, int(len(coords) / n_points))
    sample_points = coords[::step]
    if sample_points[-1] != coords[-1]:
        sample_points[-1] = coords[-1]

    rainy_places = []
    seen = set()
    for (lat, lon) in sample_points:
        _, rflag = check_weather(lat, lon)
        if rflag:
            place = reverse_geocode_short(lat, lon)
            if place not in seen:
                seen.add(place)
                rainy_places.append(place)

    if rainy_places:
        st.error("沿途下雨地區：" + "、".join(rainy_places))
    else:
        st.success("沿途天氣晴朗。")

    # 目的地天氣
    st.subheader("目的地天氣")
    dest_lat, dest_lon = coords[-1]
    dest_desc, dest_rain = check_weather(dest_lat, dest_lon)
    st.write(f"**抵達當下天氣**：{dest_desc}")
    if dest_rain:
        st.info("目的地正在下雨，" + forecast_rain_duration(dest_lat, dest_lon))

    # ===== 重置按鈕 =====
    st.markdown("---")
    if st.button("重置"):
        st.session_state.reset_seed += 1
        for k in list(st.session_state.keys()):
            if k.startswith(("origin_", "dest_")):
                del st.session_state[k]
        try:
            st.rerun()
        except:
            st.experimental_rerun()
