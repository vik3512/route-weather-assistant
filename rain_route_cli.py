#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
from urllib.parse import urlencode, quote_plus
from datetime import datetime, timedelta, timezone

import requests, polyline
from dotenv import load_dotenv
import streamlit as st
from geopy.distance import geodesic

# ======================== 常數與參數 ========================
SAMPLES_FAST = 8 # 快掃：決定要不要畫地圖
SAMPLE_INTERVAL_METERS = 500 # 短程路線：每隔多少公尺取樣一次
LONG_ROUTE_THRESHOLD_KM = 30 # 長程路線門檻，從 50 公里改為 30 公里
SHOW_MAP_RISK_THRESHOLD = 0.20
OPEN_WEATHER_MIN_RAIN_MM = 0.0

# 顏色（Google Static Maps RGBA）
COLOR_GREEN = "0x00AA00FF" # 推薦路線無雨段
COLOR_BLUE = "0x0066CCFF" # 任一路線有雨段
COLOR_GRAY = "0x999999FF" # 其他候選基底

# 風險評分
RAIN_RATIO_WEIGHT = 0.7
RAIN_INTENSITY_WEIGHT = 0.3 # 以 30 mm/h 正規化
# =========================================================

load_dotenv()
# 支援本機 .env 與 Streamlit Cloud 的 st.secrets
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY") or st.secrets.get("GOOGLE_MAPS_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY") or st.secrets.get("OPENWEATHER_API_KEY", "")

# ======================== UI 基本設定 ========================
st.set_page_config(page_title="路線天氣助手", page_icon="🌦", layout="centered")
st.title("🌦 路線天氣助手")

# 乾淨 UI：不顯示任何歷史/重置說明文字
st.markdown(
    """
    <style>
    div.stTextInput > label, div.stSelectbox > label { margin-bottom: 6px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ======================== 強力重置：nonce 機制 ========================
if "ui_nonce" not in st.session_state:
    st.session_state["ui_nonce"] = 0

def hard_reset():
    # 1) 提前計算新的 nonce
    new_nonce = int(st.session_state.get("ui_nonce", 0)) + 1
    # 2) 清除 URL 參數
    try:
        for k in list(st.query_params.keys()):
            del st.query_params[k]
    except Exception:
        pass
    # 3) 清除快取（可選）
    try:
        st.cache_data.clear()
    except Exception:
        pass
    # 4) 清除所有 session，再放回新的 nonce
    st.session_state.clear()
    st.session_state["ui_nonce"] = new_nonce
    # 5) 重新執行
    st.rerun()


def soft_reset_inputs():
    # 清空目前 nonce 對應的輸入欄位與結果，不動用完整硬重置
    try:
        # 清空 session state 中的輸入值
        st.session_state["origin_q"] = ""
        st.session_state["dest_q"] = ""
        
        # 清除 URL 參數，避免重新整理後值被帶入
        for k in list(st.query_params.keys()):
            del st.query_params[k]

        # 移除結果旗標與可能的中間狀態
        for kname in ["result_ready", "map_url", "route_data", "analysis_result"]:
            if kname in st.session_state:
                del st.session_state[kname]
    except Exception:
        pass
    # 這裡不需要呼叫 st.rerun()，因為 Streamlit 在按鈕點擊後會自動重新執行腳本

# 產生帶 nonce 的 key（避免瀏覽器或 widget 回填）
nonce = st.session_state["ui_nonce"]

def k(name: str) -> str:
    return f"{name}_{nonce}"

# ======================== 地理/路線 ========================

@st.cache_data(ttl=600)
def geocode(query: str):
    if not GOOGLE_MAPS_API_KEY: return None, None, (None, None)
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": query, "key": GOOGLE_MAPS_API_KEY, "language": "zh-TW", "region": "tw", "components": "country:TW"}
    resp = requests.get(url, params=params, timeout=20).json()
    if resp.get("status") == "OK" and resp.get("results"):
        top = resp["results"][0]
        loc = top.get("geometry", {}).get("location", {})
        lat, lon = loc.get("lat"), loc.get("lng")
        return f"place_id:{top['place_id']}", top.get("formatted_address", query), (lat, lon)
    return None, None, (None, None)


@st.cache_data(ttl=600)
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
        params["avoid"] = avoid # e.g. "highways"
    resp = requests.get(url, params=params, timeout=30).json()
    if resp.get("status") != "OK":
        return []
    routes = []
    for r in resp.get("routes", [])[:max_routes]:
        coords = polyline.decode(r["overview_polyline"]["points"])
        leg = r["legs"][0]
        routes.append((coords, leg["duration"]["value"], leg["distance"]["value"], leg["start_address"], leg["end_address"]))
    return routes


def get_one_route_coords(origin_pid: str, dest_pid: str, *, mode: str = "driving", avoid: str | None = None):
    rs = get_routes_from_place_ids(origin_pid, dest_pid, mode=mode, avoid=avoid, max_routes=1)
    return rs[0][0] if rs else []

# ======================== 氣象 ========================

def round_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def ow_current(lat, lon):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "zh_tw"}
    resp = requests.get(url, params=params, timeout=20).json()
    if "weather" not in resp: return ("查詢失敗", False, 0.0, 0, None)
    weather = resp["weather"][0]
    desc = weather.get("description", "") or "無資料"
    ow_code = int(weather.get("id") or 0)
    rain_mm = 0.0
    if isinstance(resp.get("rain"), dict):
        rain_mm = float(resp["rain"].get("1h") or resp["rain"].get("3h") or 0.0)
    is_rain = (rain_mm > OPEN_WEATHER_MIN_RAIN_MM) or ("雨" in desc) or ("雷" in desc) or ("rain" in desc.lower())
    ow_temp = None
    try:
        ow_temp = float(((resp.get("main") or {}).get("temp")))
    except Exception:
        ow_temp = None
    return desc, is_rain, rain_mm, ow_code, ow_temp


def om_hourly_now_prob_precip_code(lat, lon):
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m",
        "hourly": "time,precipitation,precipitation_probability,weather_code",
        "forecast_days": 1, "timezone": "auto"
    }
    resp = requests.get(base, params=params, timeout=20).json()
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

    # 找到「現在」對應的 hourly 索引
    idx = 0
    try:
        idx = times.index(target)
    except ValueError:
        # 找不到就退而求其次，用最接近中間的那個時段
        if times:
            idx = min(len(times)-1, max(0, len(times)//2))
        else:
            idx = 0

    # 目前值＋下一小時降水
    now_prec = float(precs[idx]) if idx < len(precs) else 0.0
    now_prob = int(probs[idx]) if idx < len(probs) else 0
    now_code = int(codes[idx]) if idx < len(codes) else 0
    next_prec = float(precs[idx+1]) if (idx+1) < len(precs) else 0.0

    # 預估雨停時間（僅用 open-meteo hourly）
    def _is_rain_code(c: int) -> bool:
        # open-meteo WMO code: drizzle 51-57, rain 61-67, showers 80-82, thunder 95-99
        return (51 <= c <= 57) or (61 <= c <= 67) or (80 <= c <= 82) or (95 <= c <= 99)

    stop_time = None
    if now_prec > 0.0 or _is_rain_code(now_code):
        for j in range(idx+1, len(times)):
            p = float(precs[j]) if j < len(precs) else 0.0
            pr = int(probs[j]) if j < len(probs) else 0
            c = int(codes[j]) if j < len(codes) else 0
            # 視為停雨的條件：降水量 ~ 0 且 機率 < 30 且 非雨天氣碼
            if (p <= 0.0) and (pr < 30) and (not _is_rain_code(c)):
                # times[j] already local time "YYYY-MM-DDTHH:00"
                try:
                    stop_time = times[j][11:16] # "HH:MM"
                except Exception:
                    stop_time = None
                break

    return now_prec, now_prob, now_code, next_prec, cur_temp, stop_time


def _grid_key(lat: float, lon: float, step: float = 0.02):
    """Quantize lat/lon to a small grid to improve cache hits and reduce API calls."""
    return (round(lat/step)*step, round(lon/step)*step)

@st.cache_data(ttl=120, show_spinner=False)
def _ow_current_cached(lat, lon): return ow_current(lat, lon)


@st.cache_data(ttl=120, show_spinner=False)
def _om_hourly_cached(lat, lon): return om_hourly_now_prob_precip_code(lat, lon)


def get_weather_bundle(lat, lon):
    glat, glon = _grid_key(lat, lon)
    ow_desc, ow_now, ow_mm, ow_code, ow_temp = _ow_current_cached(glat, glon)
    om_now_prec, om_prob, om_code, om_next_prec, om_temp, om_stop = _om_hourly_cached(glat, glon)
    mm_measured = max(float(ow_mm or 0.0), float(om_now_prec or 0.0))
    mm_est = max(mm_measured, float(om_next_prec or 0.0))
    return {"ow_desc": ow_desc, "ow_now": ow_now, "ow_code": ow_code, "om_prob": om_prob, "om_code": om_code, "mm_est": mm_est, "temp": (om_temp if om_temp is not None else ow_temp), "next_mm": om_next_prec, "stop_time": om_stop}


def _rainfall_label(mm: float) -> str:
    try:
        v = float(mm or 0.0)
    except Exception:
        v = 0.0
    if v >= 50: return "豪大雨"
    if v >= 30: return "豪雨"
    if v >= 15: return "大雨"
    if v >= 7: return "中雨"
    if v > 0: return "小雨"
    return ""

# ======================== 分析與地圖 ========================

def sample_coords_by_distance(coords, interval_m):
    if not coords or len(coords) < 2:
        return coords
    
    samples = [coords[0]]
    last_point = coords[0]
    total_distance = 0
    
    for i in range(1, len(coords)):
        p1 = last_point
        p2 = coords[i]
        distance_segment = geodesic(p1, p2).meters
        total_distance += distance_segment
        
        # 如果新點與上一個取樣點的距離超過間隔，則將其加入
        if total_distance >= interval_m:
            samples.append(p2)
            last_point = p2
            total_distance = 0
    
    # 確保終點一定被包含
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
    if not coords:
        return [], 0.0, 0.0, 0.0
    
    # 動態決定取樣間隔
    if quick_scan_only:
        coords_to_check = sample_coords(coords, SAMPLES_FAST)
    else:
        interval_m = SAMPLE_INTERVAL_METERS
        if total_distance_m > LONG_ROUTE_THRESHOLD_KM * 1000:
            interval_m = 1000
        coords_to_check = sample_coords_by_distance(coords, interval_m)

    segments, cur_state, cur_pts = [], None, []
    rainy_cnt, mm_sum = 0, 0.0
    
    for (lat, lon) in coords_to_check:
        b = get_weather_bundle(lat, lon)
        mm = float(b["mm_est"])
        is_rain = (mm > 0.0) or (95 <= b["om_code"] <= 99)
        mm_sum += mm
        if is_rain:
            rainy_cnt += 1
        
        # 快速掃描模式不需繪圖，只回傳是否有雨
        if quick_scan_only:
            if is_rain:
                return [True], 1.0, 1.0, 1.0
            continue

        # 完整模式才分段繪圖
        if cur_state is None:
            cur_state, cur_pts = is_rain, [(lat, lon)]
        elif is_rain == cur_state:
            cur_pts.append((lat, lon))
        else:
            if len(cur_pts) >= 2:
                segments.append((cur_state, polyline.encode(cur_pts)))
            cur_state, cur_pts = is_rain, [(lat, lon)]
    
    if len(cur_pts) >= 2:
        segments.append((cur_state, polyline.encode(cur_pts)))

    total_points = len(coords_to_check)
    rain_ratio = (rainy_cnt/total_points) if total_points else 0.0
    avg_mm = (mm_sum/total_points) if total_points else 0.0
    score = RAIN_RATIO_WEIGHT*rain_ratio + RAIN_INTENSITY_WEIGHT*(avg_mm/30.0)
    return segments, rain_ratio, avg_mm, score


@st.cache_data(ttl=120)
def need_map_for_route(origin_pid, dest_pid, mode, avoid) -> bool:
    # 先看目的地
    _, _, (dlat, dlon) = geocode(st.session_state.get("dest_q", ""))
    B = get_weather_bundle(dlat, dlon)
    if (B["mm_est"] >= 0.2) or (B["om_prob"] >= 50) or (95 <= B["om_code"] <= 99):
        return True
    # 快掃沿途
    coords = get_one_route_coords(origin_pid, dest_pid, mode=mode, avoid=avoid)
    if not coords:
        return False
    # 這裡的快掃仍維持固定點位，確保快速回饋
    for (lat, lon) in sample_coords(coords, SAMPLES_FAST):
        b = get_weather_bundle(lat, lon)
        risk = max(
            b["om_prob"]/100.0,
            0.6 if b["mm_est"] >= 0.2 else 0.0,
            0.8 if 95 <= b["om_code"] <= 99 else 0.0,
        )
        if risk >= SHOW_MAP_RISK_THRESHOLD:
            return True
    return False


@st.cache_data(ttl=3600)
def build_static_map_url(best_coords, other_coords_list, origin_latlon, dest_latlon, size=(640,640), scale=2):
    base = "https://maps.googleapis.com/maps/api/staticmap"
    params = {"size": f"{size[0]}x{size[1]}", "scale": str(scale), "language": "zh-TW", "key": GOOGLE_MAPS_API_KEY}
    query = []
    # 其他候選：灰色基底 + 藍色雨段
    for coords in other_coords_list:
        enc_all = polyline.encode(coords)
        query.append(("path", f"weight:3|color:{COLOR_GRAY}|enc:{enc_all}"))
        segs, *_ = analyze_route(coords, 0, quick_scan_only=False) # 這裡傳入 0 讓它走預設間隔
        for is_rain, enc in segs:
            if is_rain:
                query.append(("path", f"weight:6|color:{COLOR_BLUE}|enc:{enc}"))
    # 推薦：無雨段綠、雨段藍
    segs, *_ = analyze_route(best_coords, 0, quick_scan_only=False) # 這裡傳入 0 讓它走預設間隔
    for is_rain, enc in segs:
        color = COLOR_BLUE if is_rain else COLOR_GREEN
        query.append(("path", f"weight:7|color:{color}|enc:{enc}"))
    # A/B
    query.append(("markers", f"color:green|label:A|{origin_latlon[0]},{origin_latlon[1]}"))
    query.append(("markers", f"color:red|label:B|{dest_latlon[0]},{dest_latlon[1]}"))
    for k2,v2 in params.items(): query.append((k2,v2))
    return base + "?" + urlencode(query, doseq=True, quote_via=quote_plus)

# ======================== 介面 ========================
# 初始化 Session State
if "origin_q" not in st.session_state:
    st.session_state.origin_q = ""
if "dest_q" not in st.session_state:
    st.session_state.dest_q = ""

origin_q = st.text_input("出發地（A）", key="origin_q", placeholder="輸入出發地")
dest_q = st.text_input("目的地（B）", key="dest_q", placeholder="輸入目的地")
mode_label = st.selectbox("交通方式", ["機車","汽車","腳踏車","大眾運輸","走路"], index=0, key=k("mode"))
mode = {"機車":"driving","汽車":"driving","腳踏車":"bicycling","大眾運輸":"transit","走路":"walking"}[mode_label]
avoid = "highways" if mode_label == "機車" else None # 機車盡量避開國道/快速道路

# ======================== 查詢 ========================
if st.button("查詢", key=k("do_query")):
    if not origin_q or not dest_q:
        st.warning("請輸入出發地與目的地"); st.stop()
    if not GOOGLE_MAPS_API_KEY or not OPENWEATHER_API_KEY:
        st.error("⚠️ 請先在 `.env` 設定 GOOGLE_MAPS_API_KEY 與 OPENWEATHER_API_KEY"); st.stop()

    with st.spinner("解析地點中…"):
        origin_pid, _origin_label, (olat, olon) = geocode(origin_q)
        dest_pid, _dest_label, (dlat, dlon) = geocode(dest_q)
        if not origin_pid or not dest_pid:
            st.error("無法識別出發地或目的地"); st.stop()

        # 目的地當前天氣（更新樣式與文案）
        st.subheader("目的地當前天氣")
        B = get_weather_bundle(dlat, dlon)

        def classify_phrase_and_icon(mm: float, prob: int, code_now: int):
            if (prob < 10) and (mm < 0.2) and not (95 <= code_now <= 99):
                return "☁️ 無降雨", "無降雨"
            if 95 <= code_now <= 99: return "⛈️ 雷陣雨", "雷陣雨"
            if mm >= 30: return "🌧️ 豪雨", "豪雨"
            if mm >= 15: return "🌧️ 大雨", "大雨"
            if mm >= 7: return "🌦️ 陣雨（較大）", "陣雨（較大）"
            if mm >= 2: return "🌦️ 陣雨", "陣雨"
            if mm > 0: return "🌦️ 短暫陣雨", "短暫陣雨"
            if prob >= 50: return "☁️ 短暫陣雨（可能）", "短暫陣雨（可能）"
            return "☁️ 無降雨", "無降雨"

        def sky_icon_and_label(desc: str):
            d = (desc or "").lower()
            if "overcast" in d or "陰" in d:
                return "☁️", "陰天"
            elif "cloud" in d or "雲" in d:
                return "🌤️", "多雲"
            else:
                return "☀️", "天氣晴"

        icon_text_B, B_phrase = classify_phrase_and_icon(B["mm_est"], B["om_prob"], B["om_code"])
        temp_text = f"{B['temp']:.1f}°C" if isinstance(B.get("temp"), (int, float)) else "—"


        if B_phrase == "無降雨":
            icon, sky_label = sky_icon_and_label(B["ow_desc"])
            st.success(f"{icon} {sky_label}｜🌡️ {temp_text}｜降雨機率 {B['om_prob']}%")
            # 下一小時若有雨／雷陣雨／機率偏高 → 提醒
            try:
                cond_next = float(B.get("next_mm") or 0.0) > 0.0
                cond_thunder = 95 <= int(B.get("om_code", 0)) <= 99
                cond_prob = int(B.get("om_prob", 0)) >= 30
                if cond_next or cond_thunder or cond_prob:
                    st.info("提醒：可能有午後雷陣雨" if cond_thunder else "提醒：可能有陣雨")
            except Exception:
                pass
        else:
            st.error(f"{icon_text_B}｜🌡️ {temp_text}｜降雨機率 {B['om_prob']}%")
            if (B.get("mm_est") or 0) > 0:
                st.write(f"估計雨量：{B['mm_est']:.1f} mm/h（{_rainfall_label(B['mm_est'])}）")
            if B.get("stop_time"):
                st.write(f"預估雨停時間：{B['stop_time']}")

        # 是否需要畫地圖
        need_map = (B_phrase != "無降雨")
        if not need_map:
            coords = get_one_route_coords(origin_pid, dest_pid, mode=mode, avoid=avoid)
            if coords:
                for (lat, lon) in sample_coords(coords, SAMPLES_FAST):
                    b = get_weather_bundle(lat, lon)
                    risk = max(
                        b["om_prob"]/100.0,
                        0.6 if b["mm_est"] >= 0.2 else 0.0,
                        0.8 if 95 <= b["om_code"] <= 99 else 0.0,
                    )
                    if risk >= SHOW_MAP_RISK_THRESHOLD:
                        need_map = True
                        break
        if need_map:
            st.subheader("路線雨段地圖")
            progress_bar = st.progress(0, text="準備路線資料中…")

            # Step 1: 取得所有候選路線
            progress_bar.progress(20, text="🗺️ 取得路線資訊…")
            routes = get_routes_from_place_ids(origin_pid, dest_pid, mode=mode, avoid=avoid, max_routes=3)
            
            if routes:
                scored = []
                total_routes = len(routes)
                
                # Step 2: 分析每條路線天氣狀況
                for i, (coords, dur, dist, _, _) in enumerate(routes):
                    progress_bar.progress(20 + int((i+1)/total_routes * 70), text=f"🌦️ 分析路線天氣中… {i+1}/{total_routes}")
                    _segs, _rr, _mm, score = analyze_route(coords, dist)
                    scored.append({"coords": coords, "duration": dur, "distance": dist, "score": score, "segs": _segs})
                
                # Step 3: 篩選最佳路線並生成地圖
                progress_bar.progress(95, text="🎨 產生地圖中…")
                best = sorted(scored, key=lambda x: (round(x["score"], 4), x["duration"]))[0]
                others = [r for r in scored if r is not best]

                url = build_static_map_url(
                    best_coords=best["coords"],
                    other_coords_list=[r["coords"] for r in others],
                    origin_latlon=(olat, olon),
                    dest_latlon=(dlat, dlon),
                )
                
                # Step 4: 顯示結果
                progress_bar.empty()
                st.image(url, caption="藍色：有雨路段｜綠色：推薦路線之無雨段｜灰色：其他候選基底線")

        st.session_state["result_ready"] = True

    # ======================== 重置 ========================
    if st.session_state.get("result_ready"):
        st.markdown("---")
        cols = st.columns([1,3])
        with cols[0]:
            st.button("重置", on_click=soft_reset_inputs)