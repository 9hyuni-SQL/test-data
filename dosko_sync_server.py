def get_server_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.0.12"

# -*- coding: utf-8 -*-
"""
(주)도스코 열처리 대시보드 실시간 동기화 · MSSQL v2.0 정본 · 통합 사번/RBAC/보안 API 서버
DOSKO Multi-Line & Grinding Live Sync, Outsourcing Partner Portal, MSSQL v2.0 & RBAC Server

포트: 8089 (ThreadingHTTPServer + Tight CORS + HttpOnly Cookie Auth + CSRF + RBAC)
PLC 주소: 192.168.0.3:2004 (LS XGT FEnet Protocol)
정본 DB: MSSQL Server 2019 Standard (127.0.0.1:5539, DOSKO_ERP)
"""

import datetime
import hmac
import http.cookies
import io
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys

import dosko_admin_api
import dosko_auth
import dosko_db

BASE_DIR = r"C:\Users\song9\OneDrive\바탕 화면\test-data"
NETWORK_DASHBOARD_DIR = r"Z:\HDD1\생산일지\대시보드"
LOG_FILE_PATH = os.path.join(BASE_DIR, "dosko_sync_server.log")

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

log_lock = threading.Lock()
worker_cache_lock = threading.Lock()
WORKER_NAME_EMP_MAP = {}

TYPO_NAME_MAP = {
    "정인재": "장인재",
    "서용섭": "서용철",
}


def log_msg(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    with log_lock:
        try:
            if os.path.exists(LOG_FILE_PATH) and os.path.getsize(LOG_FILE_PATH) > 2 * 1024 * 1024:
                bak = LOG_FILE_PATH + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(LOG_FILE_PATH, bak)
            with open(LOG_FILE_PATH, "a", encoding="utf-8") as lf:
                lf.write(line + "\n")
        except Exception:
            pass
        try:
            if sys.stdout and not isinstance(sys.stdout, io.StringIO):
                print(line, flush=True)
        except Exception:
            pass


def _clean_worker_name(name):
    s = str(name or "").strip()
    if not s:
        return ""
    for bad, good in TYPO_NAME_MAP.items():
        if bad in s:
            s = s.replace(bad, good)
    return s


def _scrub_state_dict(obj):
    if isinstance(obj, dict):
        return {k: _scrub_state_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_state_dict(x) for x in obj]
    if isinstance(obj, str):
        for bad, good in TYPO_NAME_MAP.items():
            if bad in obj:
                obj = obj.replace(bad, good)
        return obj
    return obj


def refresh_worker_cache():
    global WORKER_NAME_EMP_MAP
    try:
        def _q():
            with dosko_db.get_db_connection() as conn:
                return dosko_db.fetch_table_dicts(
                    conn.cursor(),
                    """
                    SELECT emp_no, display_name
                    FROM dbo.sec_users
                    WHERE is_active = 1 AND role_code <> N'LOCKED'
                    ORDER BY CASE WHEN can_login = 1 THEN 0 ELSE 1 END, user_id ASC
                    """,
                )
        rows = dosko_db.with_db_retry(_q)
        mp = {}
        for r in rows:
            nm = str(r.get("display_name") or "").strip()
            eno = str(r.get("emp_no") or "").strip()
            if nm and eno and nm not in mp:
                mp[nm] = eno
        with worker_cache_lock:
            WORKER_NAME_EMP_MAP = mp
    except Exception as e:
        log_msg(f"[작업자 사번 캐시 경고] {e}")


def _resolve_emp_no(name, fallback_emp_no=None):
    cleaned = _clean_worker_name(name)
    if not cleaned:
        return fallback_emp_no
    with worker_cache_lock:
        if cleaned in WORKER_NAME_EMP_MAP:
            return WORKER_NAME_EMP_MAP[cleaned]
    return fallback_emp_no


def sync_ecount_db_replicas(primary_path=None):
    # v2.0부터 MSSQL DOSKO_ERP 단일 정본 엔진 사용 (SQLite 다중 SMB 복제 종료)
    return


GRADE_CODE_MAP = {
    "SK-5": ("5", "-5", "-53", "-54"),
    "SK5": ("5", "-5", "-53", "-54"),
    "SK-4": ("4", "-4", "-43", "-44"),
    "SK-3": ("3", "-3", "-33", "-34"),
    "SK-2": ("2", "-2", "-23", "-24"),
    "S75C": ("75", "75", "753", "754"),
    "S70C": ("70", "70", "703", "704"),
    "S65C": ("65", "65", "653", "654"),
    "S60C": ("60", "60", "603", "604"),
    "S55C": ("55", "55", "553", "554"),
    "S50C": ("50", "50", "503", "504"),
    "S45C": ("45", "45", "453", "454"),
    "SCM435": ("43", "43", "433", "434"),
    "SKS-51": ("51", "51", "513", "514"),
    "SKS-7": ("7", "-7", "-73", "-74"),
    "SAE6150": ("61", "61", "613", "614"),
    "50CRV4": ("61", "61", "613", "614"),
    "420J2": ("42", "42", "423", "424"),
    "STS301": ("30", "30", "303", "304"),
    "STS304": ("34", "34", "343", "344"),
}

COLOR_CODE_MAP = {
    "원재료": "00", "백": "20", "백(R면치)": "22", "백(각면치)": "23", "백(SIDE)": "24",
    "청": "30", "청(R면치)": "32", "청(각면치)": "33", "청(SIDE)": "34",
    "연마": "40", "연": "40", "연마(R면치)": "42", "연마(각면치)": "43", "연마(SIDE)": "44", "연마(각R면치)": "45",
    "황도": "50", "착색": "60", "PO": "70", "PO압연": "71", "PA": "80",
    "임(열)": "90", "임(연)": "91", "임(열+연)": "92", "임(열+S)": "93", "임(열+S+연)": "94", "회색": "30"
}


def increment_wip_lot_no(base_lot):
    """재공(WIP) 롯트번호 순차 증가 규칙: LOT -> LOT-1, LOT-1 -> LOT-2, LOT-2 -> LOT-3"""
    lot = (base_lot or "").strip()
    if not lot:
        return ""
    m = re.search(r"^(.*?)-(\d+)$", lot)
    if m:
        pfx = m.group(1)
        nxt = int(m.group(2)) + 1
        return f"{pfx}-{nxt}"
    return f"{lot}-1"


def make_ecount_item_code_and_spec(steel_grade, thickness, width, cut_length=0, color_name="청", hardness="HRC45"):
    t = float(thickness or 0)
    w = float(width or 0)
    c_len = float(cut_length or 0)
    col = (color_name or "청").strip()
    hard = (hardness or "R").strip().upper().replace(" ", "")
    t_code = f"{int(round(t * 1000)):04d}"
    if c_len > 0:
        w_code = f"{int(round(w)):03d}{int(round(c_len)):03d}"
        spec_str = f"{t:.3f}*{w:.0f}*{c_len:.0f}*({col}){hard}"
    else:
        w_code = f"{int(round(w * 100)):05d}"
        w_disp = f"{w:,.2f}" if w >= 1000 else f"{w:.2f}"
        spec_str = f"{t:.3f}*{w_disp}*({col}){hard}"
    g_prefix = GRADE_CODE_MAP.get(steel_grade, ("5", "-5", "-53", "-54"))[0]
    c_code = COLOR_CODE_MAP.get(col, "30")
    if hard == "R":
        h_code = "001"
    elif hard == "S":
        h_code = "000"
    elif hard.startswith("HRC"):
        num_part = re.sub(r"[^0-9]", "", hard[3:])
        h_code = f"{int(num_part or 45):03d}"
    elif hard.startswith("HV"):
        num_part = re.sub(r"[^0-9]", "", hard[2:])
        h_code = f"{int(num_part or 450):03d}"
    else:
        num_part = re.sub(r"[^0-9.]", "", hard)
        h_code = num_part.zfill(3) if num_part else "001"
    item_code = f"{t_code}{w_code}-{g_prefix}{c_code}-{h_code}"
    return item_code, spec_str


def ensure_qc_plc_tables():
    def _chk():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            u_cnt = cur.execute("SELECT COUNT(*) FROM dbo.sec_users").fetchone()[0]
            p_cnt = cur.execute("SELECT COUNT(*) FROM dbo.plc_5min_logs").fetchone()[0]
            return int(u_cnt or 0), int(p_cnt or 0)
    u_cnt, p_cnt = dosko_db.with_db_retry(_chk)
    refresh_worker_cache()
    log_msg(f"[MSSQL 초기화 확인] DOSKO_ERP 연결 성공 (127.0.0.1:5539) — 사원={u_cnt}명, PLC5분로그={p_cnt:,}건")


def get_ecount_overview_payload():
    try:
        return dosko_db.fetch_ecount_overview_mssql(include_prices=True)
    except Exception as e:
        log_msg(f"[Overview 조회 오류] {e}")
        return {"error": str(e), "status": "error"}


def filter_overview_for_partner(ov, partner_code):
    pc = str(partner_code or "").strip().upper()
    out = {
        "status": ov.get("status", "ok"),
        "db_path": ov.get("db_path", ""),
        "partner_scoped": pc,
        "warehouses": [],
        "projects": [],
        "mgmt_items": [],
        "steel_grades": [],
        "colors": [],
        "raw_inbounds": [],
        "heat_treatment": [],
        "grinding_wait_stock": [],
        "grinding_production": [],
        "finished_and_packing": [],
        "erp_slips": [],
        "serial_history": [],
        "serial_extension_json": {},
        "daily_report_details": [],
        "daily_report_summary": [],
        "delivery_slips": [],
        "packing_lists": [],
        "address_book": [],
        "warehouse_stock_summary": [],
        "outsourcing_partners": [p for p in (ov.get("outsourcing_partners") or []) if p.get("partner_code") == pc],
        "outsourcing_orders": [o for o in (ov.get("outsourcing_orders") or []) if o.get("partner_code") == pc],
        "outsourcing_inbounds": [i for i in (ov.get("outsourcing_inbounds") or []) if i.get("partner_code") == pc],
    }
    return out


PORT = 8089
SHARED_FILE_LOCAL = os.path.join(BASE_DIR, "dosko_shared_state.json")
SHARED_FILE_NETWORK = os.path.join(NETWORK_DASHBOARD_DIR, "dosko_shared_state.json")
SHARED_FILE_LEGACY = r"C:\Users\song9\.gemini\antigravity\brain\23a680d4-8e31-4cb3-a873-f48a7c7e79e9\scratch\dosko_shared_state.json"

PLC_IP = "192.168.0.3"
PLC_PORT = 2004
PLC_LOG_INTERVAL_SEC = 300.0

PLC_LINE_LAYOUT = [
    {"lid": "L1", "speed_idx": 0, "heat": (0, 6), "temp": (6, 10), "level": (10, 13), "lead": (13, 15)},
    {"lid": "L2", "speed_idx": 1, "heat": (15, 21), "temp": (21, 24), "level": (24, 26), "lead": (26, 27)},
    {"lid": "L3", "speed_idx": 2, "heat": (27, 31), "temp": (31, 33), "level": (33, 35), "lead": (35, 36)},
    {"lid": "L5", "speed_idx": 3, "heat": (36, 41), "temp": (41, 44), "level": (44, 46), "lead": (46, 46)},
    {"lid": "L6", "speed_idx": 4, "heat": (46, 49), "temp": (49, 51), "level": (51, 54), "lead": (54, 54)},
    {"lid": "L7", "speed_idx": 5, "heat": (54, 57), "temp": (57, 59), "level": (59, 59), "lead": (59, 59)},
]

state_lock = threading.Lock()
file_save_lock = threading.Lock()
last_plc_db_log_ts = 0.0
last_plc_db_log_at = ""

server_state = {
    "version": 1,
    "last_updated": time.time(),
    "specs": {},
    "workers": {"L1": "-", "L5": "-", "L6": "-"},
    "coils": {},
    "statuses": {
        "L1": "정상가동", "L2": "정상가동", "L3": "정상가동",
        "L5": "비가동", "L6": "비가동", "L7": "정상가동",
        "L8": "정상가동", "L9": "정상가동"
    },
    "work_orders": {},
    "line_work_state": {},
    "cleared_specs": ["L5", "L6"],
    "cleared_workers": ["L5", "L6"],
    "live_power": {
        "timestamp": time.time(),
        "total_kwh": 3779.3,
        "peak_kw": 362.0,
        "target_peak": 420.0,
        "lines": {
            "L1": {"cur_kw": 78.4, "tot_kwh": 1413.2},
            "L2": {"cur_kw": 107.1, "tot_kwh": 1162.1},
            "L3": {"cur_kw": 71.7, "tot_kwh": 826.0},
            "L5": {"cur_kw": 7.3, "tot_kwh": 30.9},
            "L6": {"cur_kw": 0.5, "tot_kwh": 0.2},
            "L7": {"cur_kw": 30.5, "tot_kwh": 314.8},
            "L8": {"cur_kw": 38.6, "tot_kwh": 412.5},
            "L9": {"cur_kw": 64.2, "tot_kwh": 628.7}
        }
    },
    "plc_stream": {
        "status": "initializing",
        "timestamp": time.time(),
        "read_ms": 0,
        "consecutive_errors": 0,
        "lines": {},
        "raw_registers": {
            "temperatures": [],
            "speeds": [],
            "svs": []
        }
    }
}

ALL_SV_ADDRS = [
    1005, 1015, 1025, 1035, 1045, 1055, 1063, 1073, 1083, 1093, 1103, 1113, 1123, 1133, 1143,
    2003, 2013, 2023, 2033, 2043, 2053, 2063, 2073, 2083, 2093, 2103, 2113,
    3003, 3013, 3023, 3033, 3043, 3053, 3063, 3073, 3083,
    5003, 5013, 5023, 5033, 5043, 5053, 5063, 5073, 5083, 5093,
    6003, 6013, 6023, 6033, 6043, 6053, 6063, 6073,
    7003, 7013, 7023, 7033, 7043
]


def load_initial_state():
    candidates = []
    decoder = json.JSONDecoder()
    for p in [SHARED_FILE_LOCAL, SHARED_FILE_NETWORK, SHARED_FILE_LEGACY]:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = f.read()
                data, _ = decoder.raw_decode(raw.lstrip())
                if isinstance(data, dict):
                    candidates.append((float(data.get("last_updated") or 0), p, data))
            except Exception as e:
                log_msg(f"[동기화 서버] 상태 파일 파싱 경고 ({p}): {e}")

    if not candidates:
        log_msg("[동기화 서버] 기존 공유 상태 파일 없음 — 기본 상태로 시작")
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_path, best_data = candidates[0]
    cleaned_best = _scrub_state_dict(best_data)
    with state_lock:
        for k in ["specs", "workers", "coils", "statuses", "work_orders", "line_work_state"]:
            if k in cleaned_best and isinstance(cleaned_best[k], dict):
                server_state[k].update(cleaned_best[k])
        if "cleared_workers" in cleaned_best and isinstance(cleaned_best["cleared_workers"], list):
            server_state["cleared_workers"] = list(set(server_state.get("cleared_workers", []) + cleaned_best["cleared_workers"]))
        if "cleared_specs" in cleaned_best and isinstance(cleaned_best["cleared_specs"], list):
            server_state["cleared_specs"] = list(set(server_state.get("cleared_specs", []) + cleaned_best["cleared_specs"]))
        for req_lid in ("L1", "L8", "L9"):
            if req_lid not in server_state["statuses"]:
                server_state["statuses"][req_lid] = "정상가동"
    log_msg(f"[동기화 서버] 공유 상태 로드 및 오기 정정 완료: {best_path} (last_updated={best_data.get('last_updated')})")
    save_state_to_files()


def _write_atomic(target_path, text_payload):
    parent = os.path.dirname(target_path)
    if not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    tmp_path = target_path + f".tmp.{os.getpid()}"
    bak_path = target_path + ".bak"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text_payload)
        try:
            os.replace(tmp_path, target_path)
        except OSError:
            try:
                if os.path.exists(bak_path):
                    os.remove(bak_path)
                if os.path.exists(target_path):
                    os.rename(target_path, bak_path)
                os.rename(tmp_path, target_path)
                if os.path.exists(bak_path):
                    os.remove(bak_path)
            except OSError:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(text_payload)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _save_state_disk_worker(data_to_write):
    with file_save_lock:
        for p in [SHARED_FILE_LOCAL, SHARED_FILE_NETWORK]:
            try:
                _write_atomic(p, data_to_write)
            except Exception as e:
                log_msg(f"[상태 저장 경고] {p}: {e}")


def save_state_to_files():
    with state_lock:
        persistent_data = {
            "version": server_state["version"],
            "last_updated": server_state["last_updated"],
            "specs": dict(server_state["specs"]),
            "workers": dict(server_state["workers"]),
            "coils": dict(server_state["coils"]),
            "statuses": dict(server_state.get("statuses", {})),
            "work_orders": dict(server_state.get("work_orders", {})),
            "line_work_state": dict(server_state.get("line_work_state", {})),
            "cleared_specs": list(server_state.get("cleared_specs", [])),
            "cleared_workers": list(server_state.get("cleared_workers", []))
        }
    data = json.dumps(persistent_data, ensure_ascii=False, indent=2)
    threading.Thread(target=_save_state_disk_worker, args=(data,), daemon=True).start()


def build_plc_lines_dict(raw_t, raw_s, sv_vals):
    lines = {}
    for cfg in PLC_LINE_LAYOUT:
        lid = cfg["lid"]
        si = cfg["speed_idx"]
        h0, h1 = cfg["heat"]
        t0, t1 = cfg["temp"]
        l0, l1 = cfg["level"]
        ld0, ld1 = cfg["lead"]
        spd = int(raw_s[si]) if si < len(raw_s) else 0
        lines[lid] = {
            "speed": spd,
            "zones": {
                "heat": list(raw_t[h0:h1]),
                "heat_sv": list(sv_vals[h0:h1]),
                "temp": list(raw_t[t0:t1]),
                "temp_sv": list(sv_vals[t0:t1]),
                "level": list(raw_t[l0:l1]),
                "level_sv": list(sv_vals[l0:l1]),
                "lead": list(raw_t[ld0:ld1]),
                "lead_sv": list(sv_vals[ld0:ld1]),
            }
        }
    return lines


def _safe_avg(vals):
    valid = [v for v in (vals or []) if v is not None]
    return round(sum(valid) / len(valid), 1) if valid else None


def _get_line_context_snapshot(lid, preferred_date=None):
    with state_lock:
        work_status = server_state.get("statuses", {}).get(lid, "")
        worker = server_state.get("workers", {}).get(lid, "")
        spec = server_state.get("specs", {}).get(lid)
        if not spec and preferred_date:
            spec = server_state.get("specs", {}).get(f"{preferred_date}_{lid}")
        wos = server_state.get("work_orders", {}).get(lid) or []
        lws = server_state.get("line_work_state", {}).get(lid) or {}
        plc_line = (server_state.get("plc_stream", {}).get("lines") or {}).get(lid) or {}

    active_wo = None
    for w in wos:
        if isinstance(w, dict) and w.get("status") == "running":
            active_wo = w
            break
    if not active_wo and lws.get("active_wo_id"):
        for w in wos:
            if isinstance(w, dict) and w.get("id") == lws.get("active_wo_id"):
                active_wo = w
                break

    active_wo_id = str(active_wo.get("id") or lws.get("active_wo_id") or "") if active_wo else str(lws.get("active_wo_id") or "")
    lot_no = ""
    if active_wo:
        lot_no = str(active_wo.get("lot") or active_wo.get("prod_lot") or "")
    if not lot_no and isinstance(spec, dict):
        lot_no = str(spec.get("lot") or spec.get("prod_lot") or "")

    spec_summary = ""
    if isinstance(spec, dict):
        t_val = spec.get("t") or spec.get("target_t") or ""
        w_val = spec.get("w") or spec.get("target_w") or ""
        g_val = spec.get("grade") or spec.get("steel") or ""
        c_val = spec.get("color") or ""
        h_val = spec.get("hardness") or ""
        if t_val or w_val:
            spec_summary = f"{g_val} {t_val}*{w_val} ({c_val}) {h_val}".strip()

    return {
        "work_status": work_status,
        "worker_name": _clean_worker_name(worker),
        "active_wo_id": active_wo_id[:80],
        "lot_no": lot_no[:60],
        "spec_summary": spec_summary[:200],
        "spec": spec,
        "active_wo": active_wo,
        "line_work_state": lws,
        "plc_line": plc_line,
    }


def record_plc_snapshots(trigger_type="INTERVAL_5M", target_lines=None, explicit_lot=None, explicit_worker=None, trigger_replica=False):
    global last_plc_db_log_ts, last_plc_db_log_at
    now_epoch = time.time()
    dt = datetime.datetime.fromtimestamp(now_epoch).replace(microsecond=0)
    logged_at = dt.strftime("%Y-%m-%d %H:%M:%S")
    biz_date_str = dosko_db.calc_business_date(dt).strftime("%Y-%m-%d")
    today_date = logged_at[:10]

    with state_lock:
        plc_stream = dict(server_state.get("plc_stream") or {})
        lines_map = dict(plc_stream.get("lines") or {})

    if not lines_map:
        return 0

    line_ids = list(target_lines) if target_lines else list(lines_map.keys())
    rows_payload = []
    for lid in line_ids:
        l_data = lines_map.get(lid)
        if not l_data:
            continue
        ctx = _get_line_context_snapshot(lid, today_date)
        zones = l_data.get("zones") or {}
        heat_vals = zones.get("heat") or []
        temp_vals = zones.get("temp") or []
        level_vals = zones.get("level") or []
        lead_vals = zones.get("lead") or []
        pv_all = heat_vals + temp_vals + level_vals + lead_vals
        sv_all = (zones.get("heat_sv") or []) + (zones.get("temp_sv") or []) + (zones.get("level_sv") or []) + (zones.get("lead_sv") or [])
        pos_heat = [v for v in heat_vals if isinstance(v, (int, float)) and v > 0]
        raw_spd = int(l_data.get("speed") or 0)
        is_act = 1 if raw_spd > 0 else 0
        w_name = _clean_worker_name(explicit_worker or ctx["worker_name"])[:60]
        w_emp = _resolve_emp_no(w_name)
        final_lot = str(explicit_lot or ctx["lot_no"] or "")[:60]

        rows_payload.append((
            lid[:10],
            logged_at,
            trigger_type[:30],
            # UPDATE set
            now_epoch, biz_date_str, (ctx["work_status"] or "")[:30], ctx["active_wo_id"][:80],
            final_lot, ctx["spec_summary"][:200], w_emp, w_name,
            raw_spd, is_act, 0,
            json.dumps(pv_all, ensure_ascii=False), json.dumps(sv_all, ensure_ascii=False),
            _safe_avg(heat_vals),
            min(pos_heat) if pos_heat else (min(heat_vals) if heat_vals else None),
            max(heat_vals) if heat_vals else None,
            _safe_avg(temp_vals), _safe_avg(level_vals), _safe_avg(lead_vals),
            json.dumps(zones, ensure_ascii=False),
            # INSERT values
            logged_at, now_epoch, biz_date_str, trigger_type[:30], lid[:10],
            (ctx["work_status"] or "")[:30], ctx["active_wo_id"][:80],
            final_lot, ctx["spec_summary"][:200], w_emp, w_name,
            raw_spd, is_act, 0,
            json.dumps(pv_all, ensure_ascii=False), json.dumps(sv_all, ensure_ascii=False),
            _safe_avg(heat_vals),
            min(pos_heat) if pos_heat else (min(heat_vals) if heat_vals else None),
            max(heat_vals) if heat_vals else None,
            _safe_avg(temp_vals), _safe_avg(level_vals), _safe_avg(lead_vals),
            json.dumps(zones, ensure_ascii=False),
        ))

    if not rows_payload:
        return 0

    merge_sql = """
    MERGE dbo.plc_5min_logs WITH (HOLDLOCK) AS tgt
    USING (SELECT ? AS line_id, CAST(? AS DATETIME2(0)) AS logged_at, ? AS trigger_type) AS src
       ON tgt.line_id = src.line_id AND tgt.logged_at = src.logged_at AND tgt.trigger_type = src.trigger_type
    WHEN MATCHED THEN
        UPDATE SET
            logged_epoch = ?, logged_business_date = ?, work_status = ?, active_wo_id = ?,
            lot_no = ?, spec_summary = ?, worker_emp_no = ?, worker_name = ?,
            raw_speed = ?, is_active = ?, is_smoothed = ?,
            temperatures_json = ?, svs_json = ?,
            heat_avg = ?, heat_min = ?, heat_max = ?, temp_avg = ?, level_avg = ?, lead_avg = ?, zones_json = ?
    WHEN NOT MATCHED THEN
        INSERT (
            logged_at, logged_epoch, logged_business_date, trigger_type, line_id,
            work_status, active_wo_id, lot_no, spec_summary, worker_emp_no, worker_name,
            raw_speed, is_active, is_smoothed, temperatures_json, svs_json,
            heat_avg, heat_min, heat_max, temp_avg, level_avg, lead_avg, zones_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
    """

    def _ins():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            for params in rows_payload:
                cur.execute(merge_sql, params)

    try:
        dosko_db.with_db_retry(_ins)
    except Exception as e:
        log_msg(f"[PLC MSSQL 저장 오류] {e}")
        return 0

    if trigger_type == "INTERVAL_5M":
        last_plc_db_log_ts = now_epoch
        last_plc_db_log_at = logged_at
    return len(rows_payload)


def record_work_event(line_id, event_type, source_endpoint, payload=None, trigger_plc=None, lot_no="", worker_name="", actor_emp_no=None, trigger_replica=True):
    now_epoch = time.time()
    dt = datetime.datetime.fromtimestamp(now_epoch).replace(microsecond=0)
    event_at = dt.strftime("%Y-%m-%d %H:%M:%S")
    biz_date_str = dosko_db.calc_business_date(dt).strftime("%Y-%m-%d")
    lid = str(line_id or "SYSTEM")[:20]
    today_date = event_at[:10]
    ctx = _get_line_context_snapshot(lid, today_date) if lid.startswith("L") else {
        "work_status": "", "worker_name": "", "lot_no": "", "plc_line": {}
    }

    final_lot = str(lot_no or ctx.get("lot_no") or "")[:60]
    final_worker = _clean_worker_name(worker_name or ctx.get("worker_name") or "")[:60]
    worker_emp = _resolve_emp_no(final_worker, fallback_emp_no=actor_emp_no)

    def _ins():
        with dosko_db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO dbo.work_event_logs (
                    event_at, event_epoch, business_date, line_id, event_type, source_endpoint,
                    lot_no, worker_emp_no, worker_name, created_by_emp_no, work_status, payload_json, plc_snapshot_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?);
                """,
                (
                    event_at,
                    now_epoch,
                    biz_date_str,
                    lid,
                    str(event_type or "EVENT")[:40],
                    str(source_endpoint or "")[:120],
                    final_lot,
                    worker_emp[:20] if worker_emp else None,
                    final_worker,
                    actor_emp_no[:20] if actor_emp_no else None,
                    str(ctx.get("work_status") or "")[:30],
                    json.dumps(payload or {}, ensure_ascii=False),
                    json.dumps(ctx.get("plc_line") or {}, ensure_ascii=False),
                ),
            )

    try:
        dosko_db.with_db_retry(_ins)
    except Exception as e:
        log_msg(f"[작업이벤트 MSSQL 저장 오류] {e}")

    if trigger_plc and lid in ("L1", "L2", "L3", "L5", "L6", "L7"):
        record_plc_snapshots(
            trigger_type=trigger_plc,
            target_lines=[lid],
            explicit_lot=final_lot,
            explicit_worker=final_worker,
            trigger_replica=False,
        )
    return event_at


def upsert_qc_inspection_draft(data, inspector_user=None):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    work_date = str(data.get("work_date") or data.get("date") or now_str[:10])[:10].replace("/", "-")
    lid = str(data.get("line_id") or data.get("lid") or "").strip().upper()
    if not lid and data.get("project_name"):
        m = re.search(r"(\d+)\s*호기", str(data.get("project_name")))
        if m:
            lid = f"L{m.group(1)}"
        elif "수냉연마" in str(data.get("project_name")):
            lid = "L8"
        elif "연마" in str(data.get("project_name")):
            lid = "L9"
    lid = (lid or "L2")[:10]

    lot_no = str(data.get("lot_no") or data.get("prod_lot") or data.get("prod_lot_no") or data.get("lot") or "").strip()[:60]
    if not lot_no:
        lot_no = f"{work_date.replace('-', '')}-{lid}-DRAFT"

    ctx = _get_line_context_snapshot(lid, work_date)
    spec = ctx.get("spec") or {}
    steel_grade = str(data.get("steel_grade") or data.get("grade") or spec.get("grade") or spec.get("steel") or "SK-5")[:40]
    t_target = data.get("target_t") or spec.get("t") or spec.get("target_t") or ""
    w_target = data.get("target_w") or spec.get("w") or spec.get("target_w") or ""
    h_target = data.get("hardness") or spec.get("hardness") or ""
    target_spec = str(data.get("target_spec") or f"{t_target}*{w_target} {h_target}".strip())[:200]

    thick_m = str(data.get("thickness_meas") or data.get("out_t") or t_target or "")[:40]
    width_m = str(data.get("width_meas") or data.get("out_w") or w_target or "")[:40]
    hard_m = str(data.get("hardness_meas") or data.get("out_h") or h_target or "")[:40]
    appearance = str(data.get("appearance") or "양호")[:40]
    flatness = str(data.get("flatness") or "양호")[:40]
    judgment = str(data.get("judgment") or data.get("pass_fail") or "합격")[:20]
    remarks = str(data.get("remarks") or data.get("note") or "")[:500]
    ecount_status = str(data.get("ecount_status") or "DRAFT")[:20]

    insp_emp = str((inspector_user or {}).get("emp_no") or data.get("inspector_emp_no") or "")[:20]
    insp_name = _clean_worker_name((inspector_user or {}).get("display_name") or data.get("inspector_name") or "")[:60]

    def _run():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            plc_rows = dosko_db.fetch_table_dicts(
                cur,
                """
                SELECT TOP (200) logged_at, trigger_type, raw_speed, heat_avg, heat_min, heat_max, temp_avg, level_avg, lead_avg
                FROM dbo.plc_5min_logs
                WHERE line_id = ? AND (lot_no = ? OR CONVERT(VARCHAR(10), logged_at, 23) = ?)
                ORDER BY logged_epoch DESC
                """,
                (lid, lot_no, work_date),
            )
            plc_rows.reverse()

            event_rows = dosko_db.fetch_table_dicts(
                cur,
                """
                SELECT TOP (100) event_at, event_type, worker_name, work_status, lot_no
                FROM dbo.work_event_logs
                WHERE line_id = ? AND (lot_no = ? OR CONVERT(VARCHAR(10), event_at, 23) = ?)
                ORDER BY event_epoch DESC
                """,
                (lid, lot_no, work_date),
            )
            event_rows.reverse()

            heat_avgs = [r["heat_avg"] for r in plc_rows if r.get("heat_avg") is not None]
            speeds = [r["raw_speed"] for r in plc_rows if r.get("raw_speed") is not None and r["raw_speed"] > 0]
            plc_live = ctx.get("plc_line") or {}
            if not heat_avgs and plc_live.get("zones", {}).get("heat"):
                h_live = plc_live["zones"]["heat"]
                heat_avgs = [round(sum(h_live) / len(h_live), 1)]
            if not speeds and plc_live.get("speed"):
                speeds = [plc_live["speed"]]

            temp_summary = {
                "samples_count": len(plc_rows),
                "heat_avg": _safe_avg(heat_avgs),
                "heat_min": min(heat_avgs) if heat_avgs else None,
                "heat_max": max(heat_avgs) if heat_avgs else None,
                "latest_zones": plc_live.get("zones") or {},
            }
            speed_summary = (
                f"평균 {round(sum(speeds)/len(speeds), 1)} (최소 {min(speeds)} ~ 최대 {max(speeds)})"
                if speeds else str(plc_live.get("speed") or 0)
            )[:120]

            row_tuple = (
                now_str, work_date, lid, lot_no, steel_grade, target_spec,
                thick_m, width_m, hard_m, appearance, flatness,
                json.dumps(temp_summary, ensure_ascii=False),
                speed_summary,
                json.dumps(plc_rows, ensure_ascii=False),
                json.dumps(event_rows, ensure_ascii=False),
                judgment, insp_emp, insp_name, remarks, ecount_status,
            )
            dosko_db.upsert_qc_draft_mssql(cur, row_tuple)

    dosko_db.with_db_retry(_run)
    return {"work_date": work_date, "line_id": lid, "lot_no": lot_no}


def make_plc_read_packet(addrs, seq=1):
    company_id = b'LSIS-XGT\x00\x00'
    plc_info = b'\x00\x00'
    cpu_info = b'\xa0'
    source = b'\x33'
    blocks = [struct.pack('<H', len(f'%DW{a}'.encode('ascii'))) + f'%DW{a}'.encode('ascii') for a in addrs]
    app_data = struct.pack('<HHH', 0x0054, 0x0002, 0x0000) + struct.pack('<H', len(addrs)) + b''.join(blocks)
    header_pre = company_id + plc_info + cpu_info + source + struct.pack('<H', seq) + struct.pack('<H', len(app_data))
    checksum = sum(header_pre) & 0xFF
    return header_pre + bytes([0, checksum]) + app_data


def poll_plc_all():
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((PLC_IP, PLC_PORT))
        pv_chunks = [
            list(range(100, 116)), list(range(116, 132)),
            list(range(132, 148)), list(range(148, 164)), [164]
        ]
        all_pv_vals = []
        for chunk in pv_chunks:
            s.sendall(make_plc_read_packet(chunk))
            resp = s.recv(1024)
            data_r = resp[20:]
            block_cnt = struct.unpack('<H', data_r[8:10])[0]
            offset = 10
            for _ in range(block_cnt):
                dlen = struct.unpack('<H', data_r[offset:offset+2])[0]
                val = struct.unpack('<H', data_r[offset+2:offset+4])[0]
                all_pv_vals.append(val)
                offset += 2 + dlen

        sv_chunks = [
            ALL_SV_ADDRS[0:16], ALL_SV_ADDRS[16:32],
            ALL_SV_ADDRS[32:48], ALL_SV_ADDRS[48:59]
        ]
        all_sv_vals = []
        for chunk in sv_chunks:
            s.sendall(make_plc_read_packet(chunk))
            resp = s.recv(1024)
            data_r = resp[20:]
            block_cnt = struct.unpack('<H', data_r[8:10])[0]
            offset = 10
            for _ in range(block_cnt):
                dlen = struct.unpack('<H', data_r[offset:offset+2])[0]
                val = struct.unpack('<H', data_r[offset+2:offset+4])[0]
                all_sv_vals.append(val)
                offset += 2 + dlen
        s.close()
        elapsed = (time.time() - t0) * 1000
        return all_pv_vals, all_sv_vals, elapsed
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        raise e


last_valid_pv = [None] * 59
zero_streak_pv = [0] * 59
last_valid_spd = [None] * 6
zero_streak_spd = [0] * 6


def plc_collector_loop():
    global last_valid_pv, zero_streak_pv, last_valid_spd, zero_streak_spd, last_plc_db_log_ts, last_plc_db_log_at
    log_msg(f"[PLC 스트리머] {PLC_IP}:{PLC_PORT} 수집 스레드 가동 (MSSQL 저장 주기: {int(PLC_LOG_INTERVAL_SEC)}초)")

    try:
        def _q_last():
            with dosko_db.get_db_connection() as conn:
                return conn.execute(
                    "SELECT MAX(logged_epoch), MAX(logged_at) FROM dbo.plc_5min_logs WHERE trigger_type = N'INTERVAL_5M'"
                ).fetchone()
        row = dosko_db.with_db_retry(_q_last)
        if row and row[0]:
            last_plc_db_log_ts = float(row[0])
            last_plc_db_log_at = str(dosko_db.json_safe(row[1]) or "")
    except Exception as e:
        log_msg(f"[PLC 최근 시각 조회 경고] {e}")

    while True:
        try:
            pv_vals, sv_vals, elapsed = poll_plc_all()
            if len(pv_vals) >= 65 and len(sv_vals) >= 59:
                raw_t = list(pv_vals[:59])
                raw_s = list(pv_vals[59:65])
                for i in range(len(raw_t)):
                    if raw_t[i] == 0:
                        if last_valid_pv[i] and last_valid_pv[i] > 50:
                            zero_streak_pv[i] += 1
                            if zero_streak_pv[i] <= 3:
                                raw_t[i] = last_valid_pv[i]
                            else:
                                last_valid_pv[i] = 0
                        else:
                            last_valid_pv[i] = 0
                    else:
                        zero_streak_pv[i] = 0
                        last_valid_pv[i] = raw_t[i]

                for i in range(len(raw_s)):
                    if raw_s[i] == 0:
                        if last_valid_spd[i] and last_valid_spd[i] > 10:
                            zero_streak_spd[i] += 1
                            if zero_streak_spd[i] <= 2:
                                raw_s[i] = last_valid_spd[i]
                            else:
                                last_valid_spd[i] = 0
                        else:
                            last_valid_spd[i] = 0
                    else:
                        zero_streak_spd[i] = 0
                        last_valid_spd[i] = raw_s[i]

                mapped_lines = build_plc_lines_dict(raw_t, raw_s, sv_vals)
                now_ts = time.time()
                with state_lock:
                    server_state["plc_stream"] = {
                        "status": "connected",
                        "timestamp": now_ts,
                        "read_ms": round(elapsed, 1),
                        "consecutive_errors": 0,
                        "lines": mapped_lines,
                        "raw_registers": {
                            "temperatures": raw_t,
                            "speeds": raw_s,
                            "svs": sv_vals
                        }
                    }

                if last_plc_db_log_ts == 0.0 or (now_ts - last_plc_db_log_ts >= PLC_LOG_INTERVAL_SEC):
                    cnt = record_plc_snapshots("INTERVAL_5M", trigger_replica=True)
                    if cnt > 0:
                        log_msg(f"[PLC 5분 MSSQL 저장] {cnt}개 호기 스냅샷 저장 완료 ({last_plc_db_log_at})")
        except Exception as e:
            with state_lock:
                errs = server_state["plc_stream"].get("consecutive_errors", 0) + 1
                server_state["plc_stream"]["status"] = "warning" if errs < 3 else "offline"
                server_state["plc_stream"]["consecutive_errors"] = errs
            if errs in (1, 10, 50):
                log_msg(f"[PLC 통신 경고] {errs}회 실패: {e}")
        time.sleep(1.5)


class DoskoThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        _, exc_val, _ = sys.exc_info()
        if isinstance(exc_val, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError, socket.timeout)):
            return
        if isinstance(exc_val, OSError) and getattr(exc_val, "winerror", None) in (10053, 10054, 10060):
            return
        log_msg(f"[HTTP 서버 예외] client={client_address} err={exc_val!r}")


_ORIGIN_RE = re.compile(
    r"^(https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d+)?|chrome-extension://[a-zA-Z0-9_-]+)$"
)

PRO_HTML_PATHS = ["/", "/index.html", "/dashboard", "/dosko_dashboard.html", "/pro", "/대시보드", "/도스코_열처리_대시보드.html"]
EASY_HTML_PATHS = ["/easy", "/dosko_easy.html", "/easy.html", "/이지", "/현장", "/field", "/이지모드", "/현장용", "/도스코_열처리_대시보드_이지모드.html", "/도스코_열처리_대시보드_현장용.html"]
PORTAL_HTML_PATHS = ["/portal", "/outsourcing", "/partner", "/파트너", "/외주", "/외주포털", "/외주가공"]
ADMIN_HTML_PATHS = ["/admin", "/admin/", "/admin/users", "/dosko_admin_users.html"]


class SyncHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        try:
            if self.connection:
                self.connection.settimeout(15.0)
        except Exception:
            pass

    def log_message(self, format, *args):
        return

    def _client_ip(self):
        return (self.client_address[0] if self.client_address else "127.0.0.1")

    def _parse_cookies(self):
        c = http.cookies.SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            try:
                c.load(raw)
            except Exception:
                pass
        return c

    def _get_cookie(self, name):
        c = self._parse_cookies()
        return c[name].value if name in c else None

    def _set_cors_headers(self):
        origin = (self.headers.get("Origin") or "").strip()
        if origin and _ORIGIN_RE.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token, X-API-Key, Authorization")

    def _safe_write(self, payload_bytes):
        try:
            self.wfile.write(payload_bytes)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_json(self, data, status_code=200, extra_cookies=None):
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status_code)
            self._set_cors_headers()
            if extra_cookies:
                for ck in extra_cookies:
                    self.send_header("Set-Cookie", ck)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._safe_write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_redirect(self, target_url):
        try:
            self.send_response(302)
            self._set_cors_headers()
            self.send_header("Location", target_url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_html_text(self, html_str, status_code=200):
        payload = html_str.encode("utf-8")
        try:
            self.send_response(status_code)
            self._set_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._safe_write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_forbidden_html(self, title, msg, back_href="/", show_logout=True):
        back_btn = f'<a class="btn" href="{back_href}">허용된 홈으로 이동</a>' if back_href else ""
        logout_btn = (
            '<button class="btn btn-out" onclick="fetch(\'/api/auth/logout\',{method:\'POST\',credentials:\'include\',headers:{\'X-CSRF-Token\':sessionStorage.getItem(\'dosko_csrf_token\')||\'\'}}).finally(()=>location.replace(\'/login\'))">로그아웃 / 사번 전환</button>'
            if show_logout else ""
        )
        html = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
        <title>접근 권한 제한 (403)</title>
        <style>body{{margin:0;min-height:100vh;background:#090e1a;color:#f1f5f9;font-family:Pretendard,'Malgun Gothic',sans-serif;display:flex;align-items:center;justify-content:center;padding:20px;}}
        .box{{max-width:480px;width:100%;background:#111827;border:1px solid #ef4444;border-radius:16px;padding:28px;text-align:center;box-shadow:0 20px 50px rgba(0,0,0,.5);}}
        h2{{color:#fca5a5;margin:0 0 12px;font-size:20px;}}p{{color:#cbd5e1;font-size:14px;line-height:1.6;margin:0 0 22px;}}
        .btns{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;}}.btn{{padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700;font-size:13px;background:#059669;color:#fff;border:none;cursor:pointer;}}.btn-out{{background:#334155;}}</style>
        </head><body><div class="box"><h2>⛔ {title}</h2><p>{msg}</p><div class="btns">{back_btn}{logout_btn}</div></div></body></html>"""
        self._send_html_text(html, status_code=403)

    def _serve_first_html(self, candidate_paths):
        for p in candidate_paths:
            if os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self._set_cors_headers()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self._safe_write(content)
                    return True
                except Exception as e:
                    log_msg(f"[HTML 전송 오류] {p}: {e}")
        return False

    def _build_token_user(self, tok):
        scope = tok.get("scope_type") or "ECOUNT_RPA"
        mods = {k: "NONE" for k, _ in dosko_auth.MODULE_KEYS}
        if scope == "ECOUNT_RPA":
            mods.update({
                "ecount_rpa": "RW", "prod_log": "R", "qc_inspect": "R",
                "grinding": "R", "outsourcing": "R", "sales_ship": "R", "power_report": "R",
            })
        elif scope == "KIOSK_MONITOR":
            mods.update({"plc_monitor": "R"})
        return {
            "user_id": None,
            "emp_no": f"TOKEN:{tok['token_prefix']}",
            "gw_login_id": "",
            "display_name": tok["token_name"],
            "dept_path": f"API_TOKEN ({scope})",
            "job_title": "TOKEN",
            "role_code": "RESTRICTED",
            "role_label": f"API 토큰 ({scope})",
            "can_login": True,
            "is_active": True,
            "must_change_pw": False,
            "can_view_price": False,
            "can_edit_confirmed": False,
            "can_export_excel": False,
            "can_use_ecount_rpa": (scope == "ECOUNT_RPA"),
            "allowed_lines": list(dosko_auth.ALL_FACTORY_LINES),
            "allowed_tabs": [],
            "module_perms": mods,
            "is_api_token": True,
            "token_scope": scope,
            "default_home": "/",
        }

    def _resolve_auth(self, req_path, touch_activity=True):
        """
        인증 해석:
        반환 ('USER' | 'TOKEN' | 'PARTNER' | 'BAD_TOKEN' | 'TOKEN_FORBIDDEN' | None, actor_obj, session_meta)
        """
        raw_api_key = (self.headers.get("X-API-Key") or "").strip()
        if not raw_api_key:
            auth_hdr = (self.headers.get("Authorization") or "").strip()
            if auth_hdr.lower().startswith("bearer "):
                raw_api_key = auth_hdr[7:].strip()

        if raw_api_key:
            tok = dosko_auth.verify_api_token(raw_api_key, required_route=None)
            if not tok:
                return "BAD_TOKEN", None, None
            allowed_r = dosko_auth.parse_json_field(tok.get("allowed_routes_json"), [])
            if allowed_r and req_path not in allowed_r and "*" not in allowed_r:
                return "TOKEN_FORBIDDEN", tok, None
            return "TOKEN", self._build_token_user(tok), {"token_id": tok["token_id"]}

        raw_user_sess = self._get_cookie("dosko_session")
        if raw_user_sess:
            user, meta = dosko_auth.get_session_user(raw_user_sess, client_ip=self._client_ip(), touch_activity=touch_activity)
            if user:
                return "USER", user, meta

        raw_partner_sess = self._get_cookie("dosko_partner_session")
        if raw_partner_sess:
            partner = dosko_auth.get_portal_partner(raw_partner_sess)
            if partner:
                return "PARTNER", partner, {"csrf_token": partner["csrf_token"]}

        return None, None, None

    def _verify_csrf(self, sess_meta):
        expected = (sess_meta or {}).get("csrf_token") or ""
        provided = (self.headers.get("X-CSRF-Token") or "").strip()
        if not expected or not provided or not hmac.compare_digest(expected, provided):
            return False
        return True

    def _normalize_auth_path(self, req_path):
        if req_path in PRO_HTML_PATHS:
            return "/"
        if req_path in EASY_HTML_PATHS:
            return "/easy"
        if req_path in PORTAL_HTML_PATHS or req_path.startswith("/portal") or req_path.startswith("/outsourcing"):
            return "/portal"
        return req_path

    def do_OPTIONS(self):
        try:
            self.send_response(200)
            self._set_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        req_path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        client_ip = self._client_ip()

        if req_path == "/favicon.ico":
            try:
                self.send_response(204)
                self._set_cors_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
            return

        if req_path == "/health":
            try:
                self.send_response(200)
                self._set_cors_headers()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self._safe_write(b"OK")
            except Exception:
                pass
            return

        # 0-0-1. 사번 규칙 및 휴대폰 마스킹 확인 (/api/auth/user-rule)
        if req_path == "/api/auth/user-rule":
            eno = (qs.get("emp_no") or [""])[0].strip()
            if not eno:
                self._send_json({"error": "MISSING_EMP_NO", "message": "사번을 입력하세요."}, 400)
                return
            def _get_ur():
                with dosko_db.get_db_connection() as conn:
                    return dosko_db.fetch_table_dicts(conn.cursor(), """
                        SELECT emp_no, display_name, phone_no, pw_rule_type, role_code, is_active
                        FROM dbo.sec_users
                        WHERE emp_no = ?;
                    """, (eno,))
            rows = dosko_db.with_db_retry(_get_ur)
            if not rows or not rows[0].get("is_active") or rows[0].get("role_code") == "LOCKED":
                self._send_json({"error": "USER_NOT_FOUND", "message": "등록되지 않았거나 비활성화된 사번입니다."}, 404)
                return
            ur = rows[0]
            raw_p = str(ur.get("phone_no") or "")
            # 마스킹: 010-****-1234
            masked_p = ""
            if len(raw_p) >= 8:
                parts = raw_p.split("-")
                if len(parts) == 3:
                    masked_p = f"{parts[0]}-****-{parts[2]}"
                else:
                    masked_p = raw_p[:3] + "****" + raw_p[-4:]
            rule = ur.get("pw_rule_type") or ("WORKER_4DIGIT" if ur.get("role_code") == "PROD_OP" else "OFFICE_COMPLEX")
            self._send_json({
                "status": "ok",
                "emp_no": ur["emp_no"],
                "display_name": ur["display_name"],
                "pw_rule_type": rule,
                "masked_phone": masked_p
            }, 200)
            return

        # 0-0. 모바일 토큰 정보 확인 (/api/auth/token-info)
        if req_path == "/api/auth/token-info":
            tok = (qs.get("token") or [""])[0].strip()
            if not tok:
                self._send_json({"error": "MISSING_TOKEN", "message": "토큰이 전달되지 않았습니다."}, 400)
                return
            import hashlib
            th = hashlib.sha256(tok.encode("utf-8")).hexdigest()
            now = datetime.datetime.now().replace(microsecond=0)
            def _get_tok_info():
                with dosko_db.get_db_connection() as conn:
                    return dosko_db.fetch_table_dicts(conn.cursor(), """
                        SELECT t.emp_no, t.expires_at, t.is_used, u.display_name, u.pw_rule_type
                        FROM dbo.sec_pw_reset_tokens t
                        JOIN dbo.sec_users u ON t.emp_no = u.emp_no
                        WHERE t.token_hash = ? AND t.is_used = 0;
                    """, (th,))
            rows = dosko_db.with_db_retry(_get_tok_info)
            if not rows:
                self._send_json({"error": "INVALID_TOKEN", "message": "유효하지 않거나 이미 사용된 일회용 링크입니다."}, 400)
                return
            r = rows[0]
            exp_dt = datetime.datetime.fromisoformat(str(r["expires_at"]))
            if exp_dt <= now:
                self._send_json({"error": "TOKEN_EXPIRED", "message": "인증 유효시간(5분)이 만료되었습니다."}, 400)
                return
            self._send_json({
                "status": "ok",
                "emp_no": r["emp_no"],
                "display_name": r.get("display_name"),
                "pw_rule_type": r.get("pw_rule_type") or "WORKER_4DIGIT"
            }, 200)
            return

        # 0. 로그인 / 비번변경 / /api/auth/me / /api/portal/me
        if req_path == "/login":
            if self._serve_first_html([
                os.path.join(BASE_DIR, "dosko_login.html"),
                os.path.join(NETWORK_DASHBOARD_DIR, "dosko_login.html"),
            ]):
                return

        if req_path == "/api/auth/me":
            auth_kind, actor, meta = self._resolve_auth(req_path, touch_activity=False)
            if auth_kind == "USER":
                self._send_json({"status": "ok", "authenticated": True, "user": actor, "csrf_token": meta["csrf_token"]})
            elif auth_kind == "TOKEN":
                self._send_json({"status": "ok", "authenticated": True, "user": actor, "csrf_token": ""})
            else:
                self._send_json({"status": "ok", "authenticated": False, "user": None})
            return

        if req_path == "/api/portal/me":
            raw_ps = self._get_cookie("dosko_partner_session")
            partner = dosko_auth.get_portal_partner(raw_ps) if raw_ps else None
            if partner:
                self._send_json({
                    "status": "ok",
                    "authenticated": True,
                    "partner": {
                        "partner_code": partner["partner_code"],
                        "wh_name": partner["wh_name"],
                        "manager_name": partner.get("manager_name") or "",
                    },
                    "csrf_token": partner["csrf_token"],
                })
            else:
                self._send_json({"status": "ok", "authenticated": False, "partner": None})
            return

        # 1. Partner Portal HTML (/portal*, /outsourcing*, ...)
        if req_path in PORTAL_HTML_PATHS or any(req_path.startswith(p) for p in ["/portal", "/outsourcing"]):
            auth_kind, actor, _ = self._resolve_auth(req_path)
            if auth_kind == "USER":
                if actor.get("must_change_pw"):
                    self._send_redirect("/change-password")
                    return
                ok, _, code, msg = dosko_auth.authorize_request(actor, "GET", "/portal", qs)
                if not ok:
                    dosko_auth.log_audit(
                        emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="FORBIDDEN_403",
                        resource_path=req_path, detail={"code": code, "msg": msg},
                    )
                    home = "/easy" if actor["role_code"] == "PROD_OP" else "/"
                    self._send_forbidden_html("접근 권한이 없습니다", msg, back_href=home)
                    return
            if self._serve_first_html([
                os.path.join(BASE_DIR, "도스코_외주가공_파트너포털.html"),
                r"C:\Users\song9\OneDrive\바탕 화면\도스코_외주가공_파트너포털.html",
                os.path.join(NETWORK_DASHBOARD_DIR, "도스코_외주가공_파트너포털.html"),
                r"Z:\HDD1\생산일지\도스코_외주가공_파트너포털.html",
            ]):
                return

        # 2-0. Mobile Reset Password HTML (/mobile/reset-pw)
        if req_path == "/mobile/reset-pw":
            if self._serve_first_html([
                os.path.join(BASE_DIR, "dosko_mobile_reset_pw.html"),
                os.path.join(NETWORK_DASHBOARD_DIR, "dosko_mobile_reset_pw.html"),
            ]):
                return

        # 2. Password Change HTML
        if req_path == "/change-password":
            auth_kind, _, _ = self._resolve_auth(req_path)
            if auth_kind != "USER":
                self._send_redirect("/login?next=/change-password")
                return
            if self._serve_first_html([
                os.path.join(BASE_DIR, "dosko_change_password.html"),
                os.path.join(NETWORK_DASHBOARD_DIR, "dosko_change_password.html"),
            ]):
                return

        # 3. IT Admin Console HTML (/admin/users)
        if req_path in ADMIN_HTML_PATHS:
            auth_kind, actor, _ = self._resolve_auth(req_path)
            if auth_kind != "USER":
                self._send_redirect("/login?next=/admin/users")
                return
            if actor.get("must_change_pw"):
                self._send_redirect("/change-password")
                return
            if actor["role_code"] != "IT_ADMIN":
                dosko_auth.log_audit(
                    emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"reason": "ADMIN_ONLY"},
                )
                home = "/easy" if actor["role_code"] == "PROD_OP" else "/"
                self._send_forbidden_html("전산실 관리자 전용 메뉴", "사용자 등록 및 권한·보안 설정은 전산실 총 관리자만 접근할 수 있습니다.", back_href=home)
                return
            if self._serve_first_html([
                os.path.join(BASE_DIR, "dosko_admin_users.html"),
                os.path.join(NETWORK_DASHBOARD_DIR, "dosko_admin_users.html"),
            ]):
                return

        # 4. Pro Dashboard HTML
        if req_path in PRO_HTML_PATHS:
            auth_kind, actor, _ = self._resolve_auth(req_path)
            if auth_kind != "USER":
                self._send_redirect("/login?next=" + urllib.parse.quote(req_path))
                return
            if actor.get("must_change_pw"):
                self._send_redirect("/change-password")
                return
            if actor["role_code"] == "PROD_OP":
                self._send_redirect("/easy")
                return
            ok, _, code, msg = dosko_auth.authorize_request(actor, "GET", "/", qs)
            if not ok:
                dosko_auth.log_audit(
                    emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"code": code, "msg": msg},
                )
                self._send_forbidden_html("접근 권한 설정 대기", msg, back_href="")
                return
            if self._serve_first_html([
                os.path.join(BASE_DIR, "도스코_열처리_대시보드.html"),
                r"C:\Users\song9\OneDrive\바탕 화면\도스코_열처리_대시보드.html",
                os.path.join(NETWORK_DASHBOARD_DIR, "도스코_열처리_대시보드.html"),
                r"Z:\HDD1\생산일지\도스코_열처리_대시보드.html",
            ]):
                return

        # 5. Easy Mode HTML
        if req_path in EASY_HTML_PATHS:
            auth_kind, actor, _ = self._resolve_auth(req_path)
            if auth_kind != "USER":
                self._send_redirect("/login?next=/easy")
                return
            if actor.get("must_change_pw"):
                self._send_redirect("/change-password")
                return
            ok, _, code, msg = dosko_auth.authorize_request(actor, "GET", "/easy", qs)
            if not ok:
                dosko_auth.log_audit(
                    emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"code": code, "msg": msg},
                )
                self._send_forbidden_html("접근 권한이 없습니다", msg, back_href="")
                return
            if self._serve_first_html([
                os.path.join(BASE_DIR, "도스코_열처리_대시보드_이지모드.html"),
                r"C:\Users\song9\OneDrive\바탕 화면\도스코_열처리_대시보드_이지모드.html",
                os.path.join(NETWORK_DASHBOARD_DIR, "도스코_열처리_대시보드_이지모드.html"),
                r"Z:\HDD1\생산일지\도스코_열처리_대시보드_이지모드.html",
            ]):
                return

        # =====================================================================
        # 보호 API (GET /api/*) — 인증 + 역할/호기/마스킹 검증 필수
        # =====================================================================
        if req_path.startswith("/api/"):
            auth_kind, actor, meta = self._resolve_auth(req_path)
            if auth_kind == "BAD_TOKEN":
                self._send_json({"error": "INVALID_API_TOKEN", "message": "유효하지 않거나 폐기된 API 토큰입니다."}, 401)
                return
            if auth_kind == "TOKEN_FORBIDDEN":
                dosko_auth.log_audit(
                    emp_no=f"TOKEN:{actor['token_prefix']}", role_code="API_TOKEN", client_ip=client_ip,
                    action_type="FORBIDDEN_403", resource_path=req_path, detail={"reason": "TOKEN_ROUTE_DENIED"},
                )
                self._send_json({"error": "TOKEN_ROUTE_DENIED", "message": "해당 API 토큰에 허용되지 않은 경로입니다."}, 403)
                return
            if auth_kind is None:
                self._send_json({"error": "AUTH_REQUIRED", "message": "사번 로그인이 필요합니다."}, 401)
                return

            if auth_kind == "PARTNER":
                if req_path == "/api/ecount/overview":
                    ov = get_ecount_overview_payload()
                    self._send_json(filter_overview_for_partner(ov, actor["partner_code"]))
                    return
                dosko_auth.log_audit(
                    emp_no=f"PARTNER:{actor['partner_code']}", display_name=actor.get("wh_name"),
                    role_code="PARTNER", client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"reason": "PARTNER_FORBIDDEN"},
                )
                self._send_json({"error": "PARTNER_FORBIDDEN", "message": "협력사 계정은 외주가공 포털 API만 접근할 수 있습니다."}, 403)
                return

            # USER or TOKEN
            ok, st_code, err_code, err_msg = dosko_auth.authorize_request(actor, "GET", req_path, qs)
            if not ok:
                dosko_auth.log_audit(
                    emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"code": err_code, "msg": err_msg},
                )
                self._send_json({"error": err_code, "message": err_msg}, st_code)
                return

            # /api/users/workers
            if req_path == "/api/users/workers":
                try:
                    def _qw():
                        with dosko_db.get_db_connection() as conn:
                            return dosko_db.fetch_table_dicts(
                                conn.cursor(),
                                """
                                SELECT emp_no, display_name, org_level4, org_level5, job_title, work_type, role_code, allowed_lines_json
                                FROM dbo.sec_users
                                WHERE is_active = 1 AND can_login = 1 AND employment_status = N'정상'
                                  AND role_code IN (N'PROD_OP', N'PROD_GLOBAL', N'QC_PROD_MGR')
                                ORDER BY CASE WHEN role_code = N'PROD_OP' THEN 1 ELSE 2 END, emp_no ASC
                                """,
                            )
                    rows = dosko_db.with_db_retry(_qw)
                    workers = []
                    for r in rows:
                        workers.append({
                            "emp_no": r["emp_no"],
                            "display_name": r["display_name"],
                            "org_level5": r.get("org_level5") or "",
                            "job_title": r.get("job_title") or "",
                            "role_code": r["role_code"],
                            "allowed_lines": dosko_auth.get_effective_lines(r),
                        })
                    self._send_json({"status": "ok", "workers": workers})
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
                return

            # /api/admin/* (GET)
            if req_path == "/api/admin/users":
                self._send_json(dosko_admin_api.list_all_users())
                return
            if req_path == "/api/admin/tokens":
                self._send_json(dosko_admin_api.list_partners_and_tokens())
                return
            if req_path == "/api/admin/audit-logs":
                self._send_json(dosko_admin_api.get_audit_and_sessions(qs))
                return

            if req_path == "/api/ecount/overview":
                ov = get_ecount_overview_payload()
                if ov.get("status") == "ok":
                    ov = dosko_auth.filter_overview_for_user(ov, actor)
                self._send_json(ov)
                return

            if req_path == "/api/state":
                with state_lock:
                    resp_data = {
                        "status": "ok",
                        "version": server_state["version"],
                        "timestamp": server_state["last_updated"],
                        "specs": dict(server_state["specs"]),
                        "workers": dict(server_state["workers"]),
                        "coils": dict(server_state["coils"]),
                        "statuses": dict(server_state.get("statuses", {})),
                        "cleared_specs": list(server_state.get("cleared_specs", [])),
                        "cleared_workers": list(server_state.get("cleared_workers", [])),
                        "work_orders": dict(server_state.get("work_orders", {})),
                        "line_work_state": dict(server_state.get("line_work_state", {})),
                        "live_power": server_state.get("live_power", {}),
                        "plc_stream": server_state.get("plc_stream", {}),
                        "plc_last_5min_logged_at": last_plc_db_log_at,
                    }
                self._send_json(dosko_auth.filter_state_for_user(resp_data, actor))
                return

            if req_path == "/api/plc-history":
                line_id = (qs.get("line_id") or [""])[0].strip().upper()
                lot_no = (qs.get("lot_no") or [""])[0].strip()
                date_f = (qs.get("date") or [""])[0].strip()
                limit = min(500, max(1, int((qs.get("limit") or ["120"])[0])))
                conds, params = [], []
                if line_id:
                    conds.append("line_id = ?")
                    params.append(line_id)
                elif actor["role_code"] == "PROD_OP" or (
                    actor["role_code"] == "RESTRICTED"
                    and actor.get("allowed_lines")
                    and len(actor["allowed_lines"]) < len(dosko_auth.ALL_FACTORY_LINES)
                ):
                    allowed = actor.get("allowed_lines") or []
                    if not allowed:
                        self._send_json({"status": "ok", "count": 0, "interval_sec": PLC_LOG_INTERVAL_SEC, "rows": []})
                        return
                    ph = ",".join("?" for _ in allowed)
                    conds.append(f"line_id IN ({ph})")
                    params.extend(allowed)
                if lot_no:
                    conds.append("lot_no = ?")
                    params.append(lot_no)
                if date_f:
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_f):
                        conds.append("logged_at >= CAST(? AS DATETIME2(0)) AND logged_at < DATEADD(day, 1, CAST(? AS DATETIME2(0)))")
                        params.extend([date_f, date_f])
                    else:
                        conds.append("CONVERT(VARCHAR(19), logged_at, 120) LIKE ?")
                        params.append(f"{date_f}%")
                where = ("WHERE " + " AND ".join(conds)) if conds else ""

                def _qp():
                    with dosko_db.get_db_connection() as conn:
                        return dosko_db.fetch_table_dicts(
                            conn.cursor(),
                            f"SELECT TOP ({limit}) * FROM dbo.plc_5min_logs {where} ORDER BY logged_epoch DESC",
                            tuple(params) if params else None,
                        )
                rows = dosko_db.with_db_retry(_qp)
                self._send_json({"status": "ok", "count": len(rows), "interval_sec": PLC_LOG_INTERVAL_SEC, "rows": rows})
                return

            if req_path == "/api/work-events":
                line_id = (qs.get("line_id") or [""])[0].strip().upper()
                lot_no = (qs.get("lot_no") or [""])[0].strip()
                date_f = (qs.get("date") or [""])[0].strip()
                limit = min(500, max(1, int((qs.get("limit") or ["120"])[0])))
                conds, params = [], []
                if line_id:
                    conds.append("line_id = ?")
                    params.append(line_id)
                elif actor["role_code"] == "PROD_OP" or (
                    actor["role_code"] == "RESTRICTED"
                    and actor.get("allowed_lines")
                    and len(actor["allowed_lines"]) < len(dosko_auth.ALL_FACTORY_LINES)
                ):
                    allowed = actor.get("allowed_lines") or []
                    if not allowed:
                        self._send_json({"status": "ok", "count": 0, "rows": []})
                        return
                    ph = ",".join("?" for _ in allowed)
                    conds.append(f"line_id IN ({ph})")
                    params.extend(allowed)
                if lot_no:
                    conds.append("lot_no = ?")
                    params.append(lot_no)
                if date_f:
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_f):
                        conds.append("event_at >= CAST(? AS DATETIME2(0)) AND event_at < DATEADD(day, 1, CAST(? AS DATETIME2(0)))")
                        params.extend([date_f, date_f])
                    else:
                        conds.append("CONVERT(VARCHAR(19), event_at, 120) LIKE ?")
                        params.append(f"{date_f}%")
                where = ("WHERE " + " AND ".join(conds)) if conds else ""

                def _qe():
                    with dosko_db.get_db_connection() as conn:
                        return dosko_db.fetch_table_dicts(
                            conn.cursor(),
                            f"SELECT TOP ({limit}) * FROM dbo.work_event_logs {where} ORDER BY event_epoch DESC",
                            tuple(params) if params else None,
                        )
                rows = dosko_db.with_db_retry(_qe)
                self._send_json({"status": "ok", "count": len(rows), "rows": rows})
                return

            if req_path == "/api/ecount/qc-summary":
                default_line = (actor.get("allowed_lines") or ["L1"])[0]
                if default_line == "PACK":
                    default_line = "L1"
                line_id = (qs.get("line_id") or [""])[0].strip().upper() or default_line
                if actor["role_code"] == "PROD_OP" and line_id not in (actor.get("allowed_lines") or []):
                    self._send_json({"error": "LINE_NOT_ASSIGNED", "message": f"담당호기({line_id}) 외 품질 조회는 차단됩니다."}, 403)
                    return
                lot_no = (qs.get("lot_no") or [""])[0].strip()
                date_f = ((qs.get("date") or [""])[0].strip() or datetime.datetime.now().strftime("%Y-%m-%d"))[:10]

                def _qqc():
                    with dosko_db.get_db_connection() as conn:
                        cur = conn.cursor()
                        draft_conds = ["work_date = CAST(? AS DATE)", "line_id = ?"]
                        draft_params = [date_f, line_id]
                        if lot_no:
                            draft_conds.append("lot_no = ?")
                            draft_params.append(lot_no)
                        drafts = dosko_db.fetch_table_dicts(
                            cur,
                            f"SELECT TOP (20) * FROM dbo.qc_inspection_drafts WHERE {' AND '.join(draft_conds)} ORDER BY draft_id DESC",
                            tuple(draft_params),
                        )
                        if not drafts:
                            drafts = dosko_db.fetch_table_dicts(
                                cur,
                                "SELECT TOP (10) * FROM dbo.qc_inspection_drafts WHERE line_id = ? ORDER BY draft_id DESC",
                                (line_id,),
                            )
                        plc_logs = dosko_db.fetch_table_dicts(
                            cur,
                            """
                            SELECT TOP (60) logged_at, trigger_type, raw_speed, heat_avg, heat_min, heat_max, temp_avg, level_avg, lead_avg, lot_no
                            FROM dbo.plc_5min_logs
                            WHERE line_id = ? AND logged_at >= CAST(? AS DATETIME2(0)) AND logged_at < DATEADD(day, 1, CAST(? AS DATETIME2(0)))
                            ORDER BY logged_epoch DESC
                            """,
                            (line_id, date_f, date_f),
                        )
                        events = dosko_db.fetch_table_dicts(
                            cur,
                            """
                            SELECT TOP (30) event_at, event_type, worker_name, work_status, lot_no
                            FROM dbo.work_event_logs
                            WHERE line_id = ? AND event_at >= CAST(? AS DATETIME2(0)) AND event_at < DATEADD(day, 1, CAST(? AS DATETIME2(0)))
                            ORDER BY event_epoch DESC
                            """,
                            (line_id, date_f, date_f),
                        )
                        return drafts, plc_logs, events

                drafts, plc_logs, events = dosko_db.with_db_retry(_qqc)
                ctx = _get_line_context_snapshot(line_id, date_f)
                latest_draft = drafts[0] if drafts else {}
                spec = ctx.get("spec") or {}
                plc_live = ctx.get("plc_line") or {}
                heat_list = [r["heat_avg"] for r in plc_logs if r.get("heat_avg") is not None]
                if not heat_list and plc_live.get("zones", {}).get("heat"):
                    heat_list = [round(sum(plc_live["zones"]["heat"]) / len(plc_live["zones"]["heat"]), 1)]
                spd_list = [r["raw_speed"] for r in plc_logs if r.get("raw_speed") is not None and r["raw_speed"] > 0]
                if not spd_list and plc_live.get("speed"):
                    spd_list = [plc_live["speed"]]

                t_target = str(spec.get("t") or spec.get("target_t") or "")
                w_target = str(spec.get("w") or spec.get("target_w") or "")
                h_target = str(spec.get("hardness") or "")
                t_meas = str(latest_draft.get("thickness_meas") or t_target)
                w_meas = str(latest_draft.get("width_meas") or w_target)
                h_meas = str(latest_draft.get("hardness_meas") or h_target)
                appearance = str(latest_draft.get("appearance") or "양호")
                flatness = str(latest_draft.get("flatness") or "양호")
                judgment = str(latest_draft.get("judgment") or "합격")
                avg_heat = round(sum(heat_list) / len(heat_list), 1) if heat_list else None
                avg_spd = round(sum(spd_list) / len(spd_list), 1) if spd_list else (plc_live.get("speed") or 0)
                lot_disp = lot_no or latest_draft.get("lot_no") or ctx.get("lot_no") or ""
                line_num = re.sub(r"[^0-9]", "", line_id)
                proj_name = "수냉연마" if line_id == "L8" else ("연마" if line_id == "L9" else f"{line_num}호기")

                mid_log_text = " / ".join(
                    f"{str(r['logged_at'])[11:16]}({r['trigger_type']}):가열{r['heat_avg']}℃,속도{r['raw_speed']}"
                    for r in reversed(plc_logs[:12])
                )

                self._send_json({
                    "status": "ok",
                    "work_date": date_f,
                    "line_id": line_id,
                    "project_name": proj_name,
                    "lot_no": lot_disp,
                    "worker_name": ctx.get("worker_name") or "",
                    "drafts": drafts,
                    "plc_5min_logs": plc_logs,
                    "work_events": events,
                    "ecount_qc_payload": {
                        "work_date": date_f,
                        "line_id": line_id,
                        "project_name": proj_name,
                        "lot_no": lot_disp,
                        "steel_grade": latest_draft.get("steel_grade") or spec.get("grade") or spec.get("steel") or "SK-5",
                        "target_spec": latest_draft.get("target_spec") or ctx.get("spec_summary") or "",
                        "judgment": judgment,
                        "mid_inspection_summary": mid_log_text,
                        "items": [
                            {"name": "두께", "unit": "mm", "target": t_target, "measured": t_meas, "result": judgment},
                            {"name": "폭", "unit": "mm", "target": w_target, "measured": w_meas, "result": judgment},
                            {"name": "경도", "unit": "HRC/HV", "target": h_target, "measured": h_meas, "result": judgment},
                            {"name": "외관", "unit": "육안", "target": "이상무", "measured": appearance, "result": judgment},
                            {"name": "평탄도", "unit": "mm", "target": "기준이내", "measured": flatness, "result": judgment},
                            {"name": "온도", "unit": "℃", "target": str(plc_live.get("zones", {}).get("heat_sv") or ""), "measured": str(avg_heat if avg_heat is not None else ""), "min_val": min(heat_list) if heat_list else None, "max_val": max(heat_list) if heat_list else None, "result": judgment},
                            {"name": "속도", "unit": "m/min", "target": str(avg_spd), "measured": str(avg_spd), "min_val": min(spd_list) if spd_list else None, "max_val": max(spd_list) if spd_list else None, "result": judgment},
                            {"name": "중간검사(5분변동)", "unit": "LOG", "target": f"5분간격 {len(plc_logs)}건", "measured": mid_log_text, "result": judgment},
                        ]
                    }
                })
                return

            if req_path == "/api/ecount/dom-schema":
                prg_id = (qs.get("prg_id") or [""])[0].strip()
                def _qds():
                    with dosko_db.get_db_connection() as conn:
                        if prg_id:
                            return dosko_db.fetch_table_dicts(conn.cursor(), "SELECT * FROM dbo.ecount_dom_schemas WHERE prg_id = ?", (prg_id,))
                        return dosko_db.fetch_table_dicts(conn.cursor(), "SELECT * FROM dbo.ecount_dom_schemas ORDER BY captured_at DESC")
                rows = dosko_db.with_db_retry(_qds)
                for r in rows:
                    try:
                        r["schema"] = json.loads(r.pop("schema_json") or "{}")
                    except Exception:
                        pass
                self._send_json({"status": "ok", "count": len(rows), "schemas": rows})
                return

        self.send_response(404)
        self._set_cors_headers()
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        req_path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        client_ip = self._client_ip()
        ua = self.headers.get("User-Agent") or ""

        # ---------------------------------------------------------------------
        # 0. 공개 로그인 엔드포인트 (/api/auth/login, /api/portal/login)
        # ---------------------------------------------------------------------

        # ---------------------------------------------------------------------
        # 0-0. 초기 로그인 전 비밀번호 생성/재설정 (/api/auth/public-reset-pw)
        # 사번 + 등록된 본인 휴대폰 번호(뒤 4자리 또는 전체) 인증 후 직군별 비밀번호 신규 생성
        if req_path == "/api/auth/public-reset-pw":
            try:
                data = json.loads(body)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return
            eno = str(data.get("emp_no") or "").strip()
            phone = str(data.get("phone_no") or "").replace("-", "").replace(" ", "").strip()
            new_pw = str(data.get("new_password") or "").strip()
            if not eno or not phone or not new_pw:
                self._send_json({"error": "MISSING_FIELDS", "message": "사번, 휴대폰 번호, 새 비밀번호를 모두 입력하세요."}, 400)
                return

            def _find_user():
                with dosko_db.get_db_connection() as conn:
                    return dosko_db.fetch_table_dicts(conn.cursor(), """
                        SELECT user_id, emp_no, display_name, phone_no, pw_rule_type, gw_login_id, is_active, role_code
                        FROM dbo.sec_users
                        WHERE emp_no = ? AND is_active = 1 AND role_code <> N'LOCKED';
                    """, (eno,))
            rows = dosko_db.with_db_retry(_find_user)
            if not rows:
                self._send_json({"error": "USER_NOT_FOUND", "message": "등록되지 않았거나 차단된 사번입니다. 전산실에 문의하세요."}, 404)
                return
            u = rows[0]
            db_phone = str(u.get("phone_no") or "").replace("-", "").replace(" ", "").strip()
            
            # 전화번호 검증: 전체 일치 또는 뒤 4자리 일치
            phone_matched = False
            if db_phone and (phone == db_phone or (len(phone) == 4 and db_phone.endswith(phone))):
                phone_matched = True
            elif not db_phone and phone == "0000": # 폰번호 미등록 비상코드
                phone_matched = True

            if not phone_matched:
                self._send_json({"error": "PHONE_MISMATCH", "message": "등록된 휴대전화 번호 정보와 일치하지 않습니다. (본인 인증 실패)"}, 400)
                return

            rule = u.get("pw_rule_type") or ("WORKER_4DIGIT" if u.get("role_code") == "PROD_OP" else "OFFICE_COMPLEX")
            ok_pw, pw_msg = dosko_auth.validate_password_strength(new_pw, eno, u.get("gw_login_id", ""), rule)
            if not ok_pw:
                self._send_json({"error": "WEAK_PASSWORD", "message": pw_msg}, 400)
                return

            new_hash = dosko_auth.hash_password(new_pw, iterations=310000)
            def _apply_public_pw():
                with dosko_db.get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE dbo.sec_users
                        SET password_hash = ?, must_change_pw = 0, failed_attempts = 0, is_locked = 0, locked_until = NULL,
                            pw_updated_at = SYSUTCDATETIME(), updated_by_emp_no = ?, updated_at = SYSUTCDATETIME()
                        WHERE emp_no = ?;
                    """, (new_hash, eno, eno))
                    cur.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE emp_no = ? AND is_revoked = 0;", (eno,))
            dosko_db.with_db_retry(_apply_public_pw)
            uname = u.get("display_name")
            self._send_json({"status": "ok", "message": f"[{uname}] 님의 비밀번호가 성공적으로 생성/변경되었습니다. 새 비밀번호로 바로 로그인하세요."}, 200)
            return

        # 0-1. 모바일 QR 비밀번호 변경 토큰 생성 (/api/auth/qr-token)
        # ---------------------------------------------------------------------
        if req_path == "/api/auth/qr-token":
            auth_kind, actor, meta = self._resolve_auth(req_path, touch_activity=False)
            eno = (qs.get("emp_no") or [""])[0].strip()
            
            # 1. 로그인 세션이 있는 경우
            if auth_kind == "USER" and actor:
                eno = actor["emp_no"]
                u_row = actor
            # 2. 비로그인 상태에서 사번으로 QR 요청한 경우 (로그인 초기화면)
            elif eno:
                def _find_emp():
                    with dosko_db.get_db_connection() as conn:
                        return dosko_db.fetch_table_dicts(conn.cursor(), """
                            SELECT emp_no, display_name, pw_rule_type, is_active, role_code
                            FROM dbo.sec_users WHERE emp_no = ?;
                        """, (eno,))
                rows = dosko_db.with_db_retry(_find_emp)
                if not rows or not rows[0].get("is_active") or rows[0].get("role_code") == "LOCKED":
                    self._send_json({"error": "USER_NOT_FOUND", "message": "등록되지 않았거나 차단된 사번입니다."}, 404)
                    return
                u_row = rows[0]
            else:
                self._send_json({"error": "LOGIN_REQUIRED", "message": "로그인이 필요하거나 사번을 입력해야 합니다."}, 401)
                return

            import secrets, hashlib
            raw_token = secrets.token_urlsafe(32)
            th = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            now = datetime.datetime.now().replace(microsecond=0)
            exp_dt = now + datetime.timedelta(minutes=5)
            def _save_qr():
                with dosko_db.get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO dbo.sec_pw_reset_tokens
                        (token_hash, emp_no, expires_at, is_used, created_at, created_ip)
                        VALUES (?, ?, ?, 0, SYSUTCDATETIME(), ?);
                    """, (th, eno, exp_dt, str(client_ip)[:45]))
            dosko_db.with_db_retry(_save_qr)

            # 스마트폰 스캔용 URL 생성: 127.0.0.1이나 localhost는 스마트폰에서 자기 자신을 가리키므로
            # 서버의 실제 내부망 IP(192.168.0.12)로 자동 치환합니다!
            lan_ip = get_server_lan_ip()
            host_header = self.headers.get("Host") or ""
            if not host_header or "127.0.0.1" in host_header or "localhost" in host_header:
                effective_host = f"{lan_ip}:8089"
            else:
                effective_host = host_header

            mobile_url = f"http://{effective_host}/mobile/reset-pw?token={raw_token}"
            self._send_json({
                "status": "ok",
                "emp_no": eno,
                "user_name": u_row.get("display_name"),
                "pw_rule_type": u_row.get("pw_rule_type", "WORKER_4DIGIT"),
                "token": raw_token,
                "reset_url": mobile_url,
                "expires_at": exp_dt.strftime("%Y-%m-%d %H:%M:%S")
            }, 200)
            return

        # ---------------------------------------------------------------------
        # 0-2. 모바일 QR 비밀번호 실제 변경 (/api/auth/mobile-reset-pw)
        # ---------------------------------------------------------------------
        if req_path == "/api/auth/mobile-reset-pw":
            try:
                data = json.loads(body)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return
            raw_tok = str(data.get("token") or "").strip()
            new_pw = str(data.get("new_password") or "").strip()
            if not raw_tok or not new_pw:
                self._send_json({"error": "MISSING_FIELDS", "message": "토큰과 새 비밀번호를 입력하세요."}, 400)
                return
            import hashlib
            th = hashlib.sha256(raw_tok.encode("utf-8")).hexdigest()
            now = datetime.datetime.now().replace(microsecond=0)
            def _check_tok():
                with dosko_db.get_db_connection() as conn:
                    rows = dosko_db.fetch_table_dicts(conn.cursor(), """
                        SELECT t.*, u.user_id, u.display_name, u.pw_rule_type, u.gw_login_id
                        FROM dbo.sec_pw_reset_tokens t
                        JOIN dbo.sec_users u ON t.emp_no = u.emp_no
                        WHERE t.token_hash = ? AND t.is_used = 0;
                    """, (th,))
                    return rows[0] if rows else None
            tok_row = dosko_db.with_db_retry(_check_tok)
            if not tok_row:
                self._send_json({"error": "INVALID_TOKEN", "message": "유효하지 않거나 이미 사용된 변경 링크입니다."}, 400)
                return
            exp_dt = datetime.datetime.fromisoformat(str(tok_row["expires_at"]))
            if exp_dt <= now:
                self._send_json({"error": "TOKEN_EXPIRED", "message": "인증 유효시간(5분)이 만료되었습니다. 현장 PC에서 다시 시도하세요."}, 400)
                return
            ok_pw, pw_msg = dosko_auth.validate_password_strength(
                new_pw, tok_row["emp_no"], tok_row.get("gw_login_id", ""), tok_row.get("pw_rule_type", "WORKER_4DIGIT")
            )
            if not ok_pw:
                self._send_json({"error": "WEAK_PASSWORD", "message": pw_msg}, 400)
                return
            new_hash = dosko_auth.hash_password(new_pw, iterations=310000)
            def _apply_pw():
                with dosko_db.get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE dbo.sec_users
                        SET password_hash = ?, must_change_pw = 0, failed_attempts = 0, is_locked = 0, locked_until = NULL,
                            pw_updated_at = SYSUTCDATETIME(), updated_by_emp_no = ?, updated_at = SYSUTCDATETIME()
                        WHERE emp_no = ?;
                    """, (new_hash, tok_row["emp_no"], tok_row["emp_no"]))
                    cur.execute("""
                        UPDATE dbo.sec_pw_reset_tokens
                        SET is_used = 1, used_at = SYSUTCDATETIME()
                        WHERE token_hash = ?;
                    """, (th,))
                    cur.execute("""
                        UPDATE dbo.sec_sessions
                        SET is_revoked = 1
                        WHERE emp_no = ? AND is_revoked = 0;
                    """, (tok_row["emp_no"],))
            dosko_db.with_db_retry(_apply_pw)
            uname = tok_row.get("display_name")
            self._send_json({"status": "ok", "message": f"[{uname}] 님의 비밀번호가 안전하게 변경되었습니다. PC에서 새 비밀번호로 로그인하세요."}, 200)
            return

        if req_path == "/api/auth/login":
            try:
                data = json.loads(body)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return
            ok, st_code, msg, profile, meta = dosko_auth.authenticate_user_login(
                data.get("emp_no"), data.get("password"), client_ip, ua
            )
            if not ok:
                self._send_json({"error": "LOGIN_FAILED", "message": msg}, st_code)
                return
            max_age = max(900, int(profile.get("session_ttl_min") or 480) * 60)
            cookie_val = f"dosko_session={meta['raw_token']}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}"
            self._send_json(
                {"status": "ok", "user": profile, "csrf_token": meta["csrf_token"], "expires_at": meta["expires_at"]},
                200,
                extra_cookies=[cookie_val],
            )
            return

        if req_path == "/api/portal/login":
            try:
                data = json.loads(body)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return
            ok, st_code, msg, partner_info, meta = dosko_auth.authenticate_partner_login(
                data.get("partner_code"), data.get("access_code"), client_ip, ua
            )
            if not ok:
                self._send_json({"error": "PARTNER_LOGIN_FAILED", "message": msg}, st_code)
                return
            cookie_val = f"dosko_partner_session={meta['raw_token']}; HttpOnly; SameSite=Lax; Path=/; Max-Age=10800"
            self._send_json(
                {"status": "ok", "partner": partner_info, "csrf_token": meta["csrf_token"]},
                200,
                extra_cookies=[cookie_val],
            )
            return

        if req_path == "/api/auth/logout":
            raw_tok = self._get_cookie("dosko_session")
            if raw_tok:
                auth_kind, actor, meta = self._resolve_auth(req_path, touch_activity=False)
                if auth_kind == "USER" and not self._verify_csrf(meta):
                    self._send_json({"error": "CSRF_INVALID", "message": "보안 토큰(CSRF)이 일치하지 않습니다."}, 403)
                    return
                dosko_auth.revoke_session(raw_tok)
                if actor:
                    dosko_auth.log_audit(
                        emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="LOGOUT", resource_path=req_path,
                    )
            clr = "dosko_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
            self._send_json({"status": "ok"}, 200, extra_cookies=[clr])
            return

        if req_path == "/api/portal/logout":
            raw_ptok = self._get_cookie("dosko_partner_session")
            if raw_ptok:
                partner = dosko_auth.get_portal_partner(raw_ptok)
                if partner and not self._verify_csrf({"csrf_token": partner["csrf_token"]}):
                    self._send_json({"error": "CSRF_INVALID", "message": "보안 토큰(CSRF)이 일치하지 않습니다."}, 403)
                    return
                dosko_auth.revoke_session(raw_ptok)
            clr = "dosko_partner_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
            self._send_json({"status": "ok"}, 200, extra_cookies=[clr])
            return

        # ---------------------------------------------------------------------
        # 1. 인증 해석 + CSRF 검증 (API Token 제외 모든 세션 POST 필수)
        # ---------------------------------------------------------------------
        auth_kind, actor, meta = self._resolve_auth(req_path)
        if auth_kind == "BAD_TOKEN":
            self._send_json({"error": "INVALID_API_TOKEN", "message": "유효하지 않거나 폐기된 API 토큰입니다."}, 401)
            return
        if auth_kind == "TOKEN_FORBIDDEN":
            dosko_auth.log_audit(
                emp_no=f"TOKEN:{actor['token_prefix']}", role_code="API_TOKEN", client_ip=client_ip,
                action_type="FORBIDDEN_403", resource_path=req_path, detail={"reason": "TOKEN_ROUTE_DENIED"},
            )
            self._send_json({"error": "TOKEN_ROUTE_DENIED", "message": "해당 API 토큰에 허용되지 않은 경로입니다."}, 403)
            return
        if auth_kind is None:
            self._send_json({"error": "AUTH_REQUIRED", "message": "사번 로그인이 필요합니다."}, 401)
            return

        if auth_kind in ("USER", "PARTNER") and not self._verify_csrf(meta):
            dosko_auth.log_audit(
                emp_no=(actor["emp_no"] if auth_kind == "USER" else f"PARTNER:{actor['partner_code']}"),
                role_code=(actor["role_code"] if auth_kind == "USER" else "PARTNER"),
                user_id=(actor.get("user_id") if auth_kind == "USER" else None),
                client_ip=client_ip, action_type="FORBIDDEN_403", resource_path=req_path,
                detail={"reason": "CSRF_INVALID"},
            )
            self._send_json({"error": "CSRF_INVALID", "message": "보안 토큰(CSRF)이 유효하지 않습니다. 새로고침 후 다시 시도하세요."}, 403)
            return

        try:
            data = json.loads(body) if body else {}
        except Exception as e:
            self._send_json({"error": str(e)}, 400)
            return

        # ---------------------------------------------------------------------
        # 2. Partner Portal 격리 POST (/api/ecount/outsourcing-inbound 한정)
        # ---------------------------------------------------------------------
        partner_ctx = None
        if auth_kind == "PARTNER":
            if req_path != "/api/ecount/outsourcing-inbound":
                dosko_auth.log_audit(
                    emp_no=f"PARTNER:{actor['partner_code']}", display_name=actor.get("wh_name"),
                    role_code="PARTNER", client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"reason": "PARTNER_POST_FORBIDDEN"},
                )
                self._send_json({"error": "PARTNER_FORBIDDEN", "message": "협력사 계정은 외주가공 입고 등록만 가능합니다."}, 403)
                return
            partner_ctx = actor
        else:
            # USER / TOKEN: RBAC 검사
            ok, st_code, err_code, err_msg = dosko_auth.authorize_request(actor, "POST", req_path, None, data)
            if not ok:
                dosko_auth.log_audit(
                    emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="FORBIDDEN_403",
                    resource_path=req_path, detail={"code": err_code, "msg": err_msg},
                )
                self._send_json({"error": err_code, "message": err_msg}, st_code)
                return

        # ---------------------------------------------------------------------
        # 3. 개인 비밀번호 변경 (/api/auth/change-password)
        # ---------------------------------------------------------------------
        if req_path == "/api/auth/change-password":
            if auth_kind != "USER":
                self._send_json({"error": "USER_SESSION_ONLY"}, 403)
                return
            cur_pw = str(data.get("current_password") or "")
            new_pw = str(data.get("new_password") or "")
            if not cur_pw or not new_pw:
                self._send_json({"error": "MISSING_FIELDS", "message": "현재 비밀번호와 새 비밀번호를 모두 입력하세요."}, 400)
                return
            if cur_pw == new_pw:
                self._send_json({"error": "SAME_PASSWORD", "message": "새 비밀번호는 현재 비밀번호와 다르게 설정해야 합니다."}, 400)
                return
            ok_pw, pw_msg = dosko_auth.validate_password_strength(new_pw, actor["emp_no"], actor.get("gw_login_id", ""), actor.get("pw_rule_type", "OFFICE_COMPLEX"))
            if not ok_pw:
                self._send_json({"error": "WEAK_PASSWORD", "message": pw_msg}, 400)
                return

            def _chg():
                with dosko_db.get_db_connection() as conn:
                    cur = conn.cursor()
                    row = cur.execute("SELECT password_hash FROM dbo.sec_users WHERE user_id = ?", (actor["user_id"],)).fetchone()
                    if not row or not dosko_auth.verify_password(cur_pw, row[0]):
                        return False
                    new_hash = dosko_auth.hash_password(new_pw, iterations=310000)
                    cur.execute(
                        """
                        UPDATE dbo.sec_users
                        SET password_hash = ?, must_change_pw = 0, failed_attempts = 0, is_locked = 0, locked_until = NULL,
                            updated_by_emp_no = ?, updated_at = SYSDATETIME()
                        WHERE user_id = ?;
                        """,
                        (new_hash, actor["emp_no"], actor["user_id"]),
                    )
                    cur.execute(
                        "UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE user_id = ? AND session_token_hash <> ? AND is_revoked = 0",
                        (actor["user_id"], meta["token_hash"]),
                    )
                    return True

            if not dosko_db.with_db_retry(_chg):
                self._send_json({"error": "CURRENT_PASSWORD_INVALID", "message": "현재 비밀번호가 올바르지 않습니다."}, 400)
                return
            dosko_auth.log_audit(
                emp_no=actor["emp_no"], display_name=actor["display_name"], role_code=actor["role_code"],
                user_id=actor["user_id"], client_ip=client_ip, action_type="PW_CHANGED", resource_path=req_path,
            )
            self._send_json({"status": "ok", "message": "비밀번호가 성공적으로 변경되었습니다."})
            return

        # ---------------------------------------------------------------------
        # 4. 전산실 관리자 전용 API (/api/admin/*)
        # ---------------------------------------------------------------------
        if req_path.startswith("/api/admin/"):
            if auth_kind != "USER" or actor["role_code"] != "IT_ADMIN":
                self._send_json({"error": "ADMIN_ONLY", "message": "전산실 관리자 전용 메뉴입니다."}, 403)
                return
            if req_path == "/api/admin/users/create":
                ok, st_c, res = dosko_admin_api.create_user(actor, data, client_ip)
                if ok:
                    refresh_worker_cache()
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/users/update":
                ok, st_c, res = dosko_admin_api.update_user(actor, data, client_ip)
                if ok:
                    refresh_worker_cache()
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/users/reset-pw":
                ok, st_c, res = dosko_admin_api.reset_user_password(actor, data, client_ip)
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/users/unlock":
                ok, st_c, res = dosko_admin_api.unlock_user(actor, data, client_ip)
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/import-csv":
                ok, st_c, res = dosko_admin_api.import_hr_csv(actor, data, client_ip)
                if ok and not data.get("dry_run", True):
                    refresh_worker_cache()
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/partners/update":
                ok, st_c, res = dosko_admin_api.update_partner(actor, data, client_ip)
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/tokens/create":
                ok, st_c, res = dosko_admin_api.create_api_token(actor, data, client_ip)
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/tokens/revoke":
                ok, st_c, res = dosko_admin_api.revoke_api_token(actor, data, client_ip)
                self._send_json(res, st_c)
                return
            if req_path == "/api/admin/sessions/revoke":
                ok, st_c, res = dosko_admin_api.revoke_session_by_hash(actor, data, client_ip)
                self._send_json(res, st_c)
                return
            self._send_json({"error": "UNKNOWN_ADMIN_ROUTE"}, 404)
            return

        actor_emp_no = f"PARTNER:{partner_ctx['partner_code']}" if partner_ctx else actor["emp_no"]
        actor_name = partner_ctx["wh_name"] if partner_ctx else actor["display_name"]
        actor_role = "PARTNER" if partner_ctx else actor["role_code"]

        # ---------------------------------------------------------------------
        # 5. ECOUNT DOM 스키마 저장
        # ---------------------------------------------------------------------
        if req_path == "/api/ecount/dom-schema":
            try:
                prg_id = str(data.get("prgId") or data.get("prg_id") or "").strip()[:60]
                if not prg_id:
                    self._send_json({"error": "prgId is required"}, 400)
                    return
                page_title = str(data.get("pageTitle") or data.get("page_title") or "")[:120]
                url_hash = str(data.get("urlHash") or data.get("url_hash") or "")[:200]
                captured_at = dosko_db.upsert_dom_schema_mssql(prg_id, page_title, url_hash, json.dumps(data, ensure_ascii=False))
                dosko_auth.log_audit(
                    emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                    user_id=(None if partner_ctx else actor.get("user_id")), client_ip=client_ip,
                    action_type="DATA_WRITE", resource_path=req_path, target_key=prg_id,
                )
                log_msg(f"[ECOUNT 스키마 저장] prgId={prg_id} title={page_title}")
                self._send_json({"status": "ok", "prg_id": prg_id, "captured_at": captured_at})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 6. QC 초안 / 최종 검사 등록
        # ---------------------------------------------------------------------
        if req_path == "/api/ecount/qc-summary":
            try:
                res = upsert_qc_inspection_draft(data, inspector_user=actor)
                lid = res["line_id"]
                record_work_event(
                    lid, "QC_DRAFT_UPDATE", req_path, data, trigger_plc="EVENT_INPUT",
                    lot_no=res["lot_no"], worker_name=actor_name, actor_emp_no=actor_emp_no, trigger_replica=False,
                )
                dosko_auth.log_audit(
                    emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                    resource_path=req_path, target_key=res["lot_no"], detail={"ecount_status": data.get("ecount_status", "DRAFT")},
                )
                self._send_json({"status": "ok", **res})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 7. Heat Production (1~7호기)
        # ---------------------------------------------------------------------
        if req_path == "/api/ecount/heat-production":
            try:
                work_date = str(data.get("work_date") or time.strftime("%Y-%m-%d"))[:10].replace("/", "-")
                biz_date = dosko_db.calc_business_date(work_date).strftime("%Y-%m-%d")
                date_compact = work_date.replace("-", "")
                doc_date_no = f"{work_date.replace('-', '/')} -1"
                project_name = str(data.get("project_name") or "2호기")[:60]
                worker_name = _clean_worker_name(data.get("worker_name") or actor_name)[:60]
                worker_emp_no = _resolve_emp_no(worker_name, fallback_emp_no=actor_emp_no)
                grade = str(data.get("steel_grade") or "SK-5")[:40]
                in_t = float(data.get("in_t") or data.get("target_t") or 1.2)
                in_w = float(data.get("in_w") or data.get("target_w") or 100.0)
                target_t = float(data.get("target_t") or in_t)
                target_w = float(data.get("target_w") or in_w)
                color = str(data.get("color") or "청")[:40]
                hardness = (data.get("hardness") or "HRC46").replace(" ", "")[:30]
                raw_lot = str(data.get("in_lot") or f"{date_compact}-R01")[:60]
                in_kg = float(data.get("in_kg") or 0)
                prod_kg = float(data.get("prod_kg") or 0)
                qty_ea = int(data.get("coils_count") or data.get("qty_ea") or 1)
                unit_kg = round(prod_kg / max(1, qty_ea), 1) if prod_kg > 0 else 0.0
                wip_kg = float(data.get("wip_kg") or 0)
                scrap_kg = float(data.get("scrap_kg") or max(0.0, round(in_kg - prod_kg - wip_kg, 1)))
                prod_mgmt = str(data.get("prod_mgmt") or ("수냉열처리" if ("6" in project_name or "7" in project_name) else "열처리"))[:60]
                in_wh = str(data.get("in_wh") or ("01.소폭" if ("6" in project_name or "7" in project_name) and "연" in color else ("01.연마" if "연" in color else "01.완제품")))[:60]
                next_route = "NARROW_WAIT" if in_wh == "01.소폭" else ("GRIND_WAIT" if in_wh == "01.연마" else "FINISHED")
                disc = re.sub(r"[^0-9]", "", project_name) or "1"
                rec_key = f"{date_compact}-{disc}"
                raw_item_cd, raw_spec = make_ecount_item_code_and_spec(grade, in_t, in_w, 0, "원재료", "R")
                prod_item_cd, prod_spec = make_ecount_item_code_and_spec(grade, target_t, target_w, 0, color, hardness)

                def _tx():
                    with dosko_db.get_db_connection() as conn:
                        cur = conn.cursor()
                        row_sh = cur.execute(
                            "SELECT last_suffix FROM dbo.ecount_serial_issue_history WHERE history_type = N'generalSerialIssueHistory' AND record_key = ?",
                            (rec_key,),
                        ).fetchone()
                        next_suffix = (int(row_sh[0]) + 1) if row_sh else (int(disc) * 100 + 1)
                        prod_lot = str(data.get("prod_lot") or f"{date_compact}-{next_suffix}")[:60]
                        now_ms = int(time.time() * 1000)
                        dosko_db.upsert_serial_history_mssql(cur, "generalSerialIssueHistory", rec_key, next_suffix, prod_lot, now_ms)

                        wip_lot = None
                        if wip_kg > 0:
                            wip_lot = increment_wip_lot_no(raw_lot)[:60]
                            base_pfx = re.sub(r"-\d+$", "", raw_lot)
                            w_suf_m = re.search(r"-(\d+)$", wip_lot)
                            w_suf = int(w_suf_m.group(1)) if w_suf_m else 1
                            dosko_db.upsert_serial_history_mssql(cur, "wipSerialIssueHistory", base_pfx, w_suf, wip_lot, now_ms)

                        cur.execute(
                            """
                            INSERT INTO dbo.proc_2_heat_treatment (
                                doc_date_no, work_date, business_date, project_name, worker_emp_no, worker_name, created_by_emp_no,
                                out_wh, consume_mgmt, raw_item_code, raw_steel_grade, raw_thickness, raw_width, raw_cut_length,
                                raw_spec_full, raw_lot_no, input_unit_kg, input_qty_ea, input_total_kg, wip_return_kg, wip_lot_no,
                                next_route, in_wh, prod_mgmt, prod_item_code, prod_steel_grade, prod_thickness, prod_width, prod_cut_length,
                                prod_color, prod_hardness, prod_spec_full, prod_lot_no, unit_weight_kg, qty_ea, total_weight_kg, scrap_kg, remarks
                            ) OUTPUT INSERTED.heat_id
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, work_date, biz_date, project_name, worker_emp_no, worker_name, actor_emp_no,
                                "0.도스코", "투입", raw_item_cd, grade, in_t, in_w, 0,
                                raw_spec, raw_lot, in_kg, 1, in_kg, (-wip_kg if wip_kg > 0 else 0), wip_lot,
                                next_route, in_wh, prod_mgmt, prod_item_cd, grade, target_t, target_w, 0,
                                color, hardness, prod_spec, prod_lot, unit_kg, qty_ea, prod_kg, scrap_kg, str(data.get("remarks") or ""),
                            ),
                        )
                        heat_id = int(cur.fetchone()[0])

                        if in_wh in ("01.연마", "01.소폭"):
                            rem_note = f"{project_name} 열처리 생산 -> 연마대기 입고"
                            cur.execute(
                                """
                                MERGE dbo.proc_3_grinding_wait_stock WITH (HOLDLOCK) AS tgt
                                USING (SELECT ? AS heat_lot_no) AS src ON tgt.heat_lot_no = src.heat_lot_no
                                WHEN MATCHED THEN
                                    UPDATE SET
                                        wh_code=?, source_heat_id=?, source_project=?, prod_date=?, item_code=?,
                                        steel_grade=?, thickness=?, width=?, cut_length=?, color_name=?, hardness_display=?, spec_full=?,
                                        unit_weight_kg=?, init_qty_ea=?, remain_qty_ea=?, init_weight_kg=?, used_weight_kg=?, remain_weight_kg=?,
                                        status=N'WAITING', remarks=?
                                WHEN NOT MATCHED THEN
                                    INSERT (
                                        wh_code, source_heat_id, source_project, prod_date, heat_lot_no, item_code,
                                        steel_grade, thickness, width, cut_length, color_name, hardness_display, spec_full,
                                        unit_weight_kg, init_qty_ea, remain_qty_ea, init_weight_kg, used_weight_kg, remain_weight_kg, status, remarks
                                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,N'WAITING',?);
                                """,
                                (
                                    prod_lot,
                                    in_wh, heat_id, project_name, work_date, prod_item_cd,
                                    grade, target_t, target_w, 0, color, hardness, prod_spec,
                                    unit_kg, qty_ea, qty_ea, prod_kg, 0, prod_kg, rem_note,
                                    in_wh, heat_id, project_name, work_date, prod_lot, prod_item_cd,
                                    grade, target_t, target_w, 0, color, hardness, prod_spec,
                                    unit_kg, qty_ea, qty_ea, prod_kg, 0, prod_kg, rem_note,
                                ),
                            )
                        else:
                            stage = "EXPORT_WAIT" if in_wh == "02.포장반" else "FINISHED_STOCK"
                            cur.execute(
                                """
                                INSERT INTO dbo.proc_5_finished_and_packing (
                                    doc_date_no, business_date, process_stage, project_name, mgmt_item, out_wh, in_wh,
                                    source_type, source_lot_no, pack_lot_no, item_code, steel_grade, thickness, width, cut_length,
                                    color_name, hardness_display, spec_full, unit_weight_kg, qty_ea, total_weight_kg, remain_weight_kg,
                                    worker_emp_no, worker_name, created_by_emp_no, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, biz_date, stage, project_name, prod_mgmt, "0.도스코", in_wh,
                                    "HEAT", prod_lot, (f"{date_compact}-P01" if in_wh == "02.포장반" else None), prod_item_cd, grade, target_t, target_w, 0,
                                    color, hardness, prod_spec, unit_kg, qty_ea, prod_kg, prod_kg,
                                    worker_emp_no, worker_name, actor_emp_no, str(data.get("remarks") or ""),
                                ),
                            )

                        cur.execute(
                            """
                            INSERT INTO dbo.ecount_erp_slips (
                                doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                consume_item_name, consume_spec, consume_serial_lot, consume_item_code,
                                unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, actor_emp_no, actor_name, project_name, "투입", worker_emp_no, worker_name,
                                grade, raw_spec, raw_lot, raw_item_cd,
                                in_kg, 1, in_kg, 0, in_kg, "0.도스코", in_wh, 0, "원소재 투입",
                            ),
                        )
                        if wip_kg > 0 and wip_lot:
                            cur.execute(
                                """
                                INSERT INTO dbo.ecount_erp_slips (
                                    doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                    consume_item_name, consume_spec, consume_serial_lot, consume_item_code,
                                    unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, actor_emp_no, actor_name, project_name, "재공", worker_emp_no, worker_name,
                                    grade, raw_spec, wip_lot, raw_item_cd,
                                    wip_kg, -1, -wip_kg, 0, -wip_kg, "0.도스코", "0.도스코", 0, f"재공환원(수량-1, 단위환산량+{wip_kg}, 롯트:{wip_lot})",
                                ),
                            )
                        cur.execute(
                            """
                            INSERT INTO dbo.ecount_erp_slips (
                                doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                prod_item_code, prod_item_name, prod_spec, color_name, prod_serial_lot,
                                unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, actor_emp_no, actor_name, project_name, prod_mgmt, worker_emp_no, worker_name,
                                prod_item_cd, grade, prod_spec, color, prod_lot,
                                unit_kg, qty_ea, prod_kg, prod_kg, round(in_kg - wip_kg, 1), "0.도스코", in_wh, scrap_kg, f"{in_wh} 입고",
                            ),
                        )
                        return prod_lot, wip_lot

                prod_lot, wip_lot = dosko_db.with_db_retry(_tx)
                lid = f"L{disc}"
                record_work_event(
                    lid, "HEAT_PRODUCTION", req_path, data, trigger_plc="EVENT_END",
                    lot_no=prod_lot, worker_name=worker_name, actor_emp_no=actor_emp_no, trigger_replica=False,
                )
                upsert_qc_inspection_draft({**data, "line_id": lid, "work_date": work_date, "lot_no": prod_lot}, inspector_user=actor)
                dosko_auth.log_audit(
                    emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                    resource_path=req_path, target_key=prod_lot, detail={"line_id": lid, "in_wh": in_wh, "prod_kg": prod_kg},
                )
                self._send_json({"status": "ok", "prod_lot": prod_lot, "wip_lot": wip_lot, "in_wh": in_wh, "prod_item_cd": prod_item_cd})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 8. Grinding Production (수냉연마 L8 / 연마 L9)
        # ---------------------------------------------------------------------
        if req_path == "/api/ecount/grinding-production":
            try:
                work_date = str(data.get("work_date") or time.strftime("%Y-%m-%d"))[:10].replace("/", "-")
                biz_date = dosko_db.calc_business_date(work_date).strftime("%Y-%m-%d")
                date_compact = work_date.replace("-", "")
                doc_date_no = f"{work_date.replace('-', '/')} -3"
                project_name = str(data.get("project_name") or "연마")[:60]
                is_water_grind = ("수냉" in project_name or str(data.get("line_id") or "").upper() == "L8")
                if is_water_grind:
                    project_name = "수냉연마"
                worker_name = _clean_worker_name(data.get("worker_name") or actor_name)[:60]
                worker_emp_no = _resolve_emp_no(worker_name, fallback_emp_no=actor_emp_no)
                wait_id_in = data.get("wait_id")
                out_wh = str(data.get("out_wh") or ("01.소폭" if is_water_grind else "01.연마"))[:60]
                in_wh = str(data.get("in_wh") or "01.완제품")[:60]
                grade = str(data.get("steel_grade") or "SK-5")[:40]
                in_t = float(data.get("in_t") or data.get("target_t") or 1.2)
                in_w = float(data.get("in_w") or data.get("target_w") or 121.0)
                prod_t = float(data.get("target_t") or in_t)
                prod_w = float(data.get("target_w") or in_w)
                prod_color = str(data.get("color") or "연마(SIDE)")[:40]
                hardness = (data.get("hardness") or "HRC45").replace(" ", "")[:30]
                heat_lot = str(data.get("heat_lot_no") or data.get("in_lot") or f"{date_compact}-301")[:80]
                in_kg = float(data.get("in_kg") or 0)
                prod_kg = float(data.get("prod_kg") or 0)
                qty_ea = int(data.get("qty_ea") or data.get("coils_count") or 1)
                unit_kg = round(prod_kg / max(1, qty_ea), 1) if prod_kg > 0 else 0.0
                wip_kg = float(data.get("wip_kg") or 0)
                scrap_kg = float(data.get("scrap_kg") or max(0.0, round(in_kg - prod_kg - wip_kg, 1)))
                cems_kwh = float(data.get("cems_power_kwh") or (38.6 if is_water_grind else 64.2))
                disc = "8" if is_water_grind else "9"
                rec_key = f"{date_compact}-{disc}"
                consume_item_cd, consume_spec = make_ecount_item_code_and_spec(grade, in_t, in_w, 0, "청", hardness)
                prod_item_cd, prod_spec = make_ecount_item_code_and_spec(grade, prod_t, prod_w, 0, prod_color, hardness)

                def _tx_grind():
                    with dosko_db.get_db_connection() as conn:
                        cur = conn.cursor()
                        row_sh = cur.execute(
                            "SELECT last_suffix FROM dbo.ecount_serial_issue_history WHERE history_type = N'generalSerialIssueHistory' AND record_key = ?",
                            (rec_key,),
                        ).fetchone()
                        next_suffix = (int(row_sh[0]) + 1) if row_sh else (int(disc) * 100 + 1)
                        prod_lot = str(data.get("prod_lot") or f"{date_compact}-{next_suffix}")[:80]
                        now_ms = int(time.time() * 1000)
                        dosko_db.upsert_serial_history_mssql(cur, "generalSerialIssueHistory", rec_key, next_suffix, prod_lot, now_ms)

                        wip_lot = None
                        if wip_kg > 0:
                            wip_lot = increment_wip_lot_no(heat_lot)[:80]
                            base_pfx = re.sub(r"-\d+$", "", heat_lot)
                            w_suf_m = re.search(r"-(\d+)$", wip_lot)
                            w_suf = int(w_suf_m.group(1)) if w_suf_m else 1
                            dosko_db.upsert_serial_history_mssql(cur, "wipSerialIssueHistory", base_pfx, w_suf, wip_lot, now_ms)

                        wait_id = wait_id_in
                        if wait_id:
                            w_row = cur.execute("SELECT remain_weight_kg, used_weight_kg FROM dbo.proc_3_grinding_wait_stock WHERE wait_id = ?", (wait_id,)).fetchone()
                            rem_w = float(w_row[0] or 0) if w_row else None
                            used_w = float(w_row[1] or 0) if w_row else None
                        else:
                            w_row = cur.execute("SELECT wait_id, remain_weight_kg, used_weight_kg FROM dbo.proc_3_grinding_wait_stock WHERE heat_lot_no = ?", (heat_lot,)).fetchone()
                            if w_row:
                                wait_id = int(w_row[0])
                                rem_w = float(w_row[1] or 0)
                                used_w = float(w_row[2] or 0)
                            else:
                                rem_w, used_w = None, None

                        if w_row and rem_w is not None:
                            actual_used = max(0.0, in_kg - wip_kg)
                            new_rem = max(0.0, round(rem_w - actual_used, 1))
                            new_used = round(used_w + actual_used, 1)
                            new_st = "CONSUMED" if new_rem <= 0 else "PARTIAL"
                            cur.execute(
                                "UPDATE dbo.proc_3_grinding_wait_stock SET remain_weight_kg = ?, used_weight_kg = ?, status = ? WHERE wait_id = ?",
                                (new_rem, new_used, new_st, wait_id),
                            )

                        if wip_kg > 0 and wip_lot:
                            rem_txt = f"{project_name} 재공 복귀 (수량 1, {wip_kg}kg, 창고:{out_wh})"
                            cur.execute(
                                """
                                MERGE dbo.proc_3_grinding_wait_stock WITH (HOLDLOCK) AS tgt
                                USING (SELECT ? AS heat_lot_no) AS src ON tgt.heat_lot_no = src.heat_lot_no
                                WHEN MATCHED THEN
                                    UPDATE SET
                                        wh_code=?, source_heat_id=?, source_project=?, prod_date=?, item_code=?,
                                        steel_grade=?, thickness=?, width=?, cut_length=?, color_name=?, hardness_display=?, spec_full=?,
                                        unit_weight_kg=?, init_qty_ea=1, remain_qty_ea=1, init_weight_kg=?, used_weight_kg=0, remain_weight_kg=?,
                                        status=N'WAITING', remarks=?
                                WHEN NOT MATCHED THEN
                                    INSERT (
                                        wh_code, source_heat_id, source_project, prod_date, heat_lot_no, item_code,
                                        steel_grade, thickness, width, cut_length, color_name, hardness_display, spec_full,
                                        unit_weight_kg, init_qty_ea, remain_qty_ea, init_weight_kg, used_weight_kg, remain_weight_kg, status, remarks
                                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    wip_lot,
                                    out_wh, wait_id, project_name, work_date, consume_item_cd,
                                    grade, in_t, in_w, 0, "청", hardness, consume_spec,
                                    wip_kg, wip_kg, wip_kg, rem_txt,
                                    out_wh, wait_id, project_name, work_date, wip_lot, consume_item_cd,
                                    grade, in_t, in_w, 0, "청", hardness, consume_spec,
                                    wip_kg, 1, 1, wip_kg, 0, wip_kg, "WAITING", rem_txt,
                                ),
                            )

                        cur.execute(
                            """
                            INSERT INTO dbo.proc_4_grinding_production (
                                doc_date_no, work_date, business_date, project_name, worker_emp_no, worker_name, created_by_emp_no,
                                source_wait_id, out_wh, consume_mgmt, consume_item_code, consume_steel_grade, consume_spec_full,
                                consume_heat_lot_no, consume_unit_kg, consume_qty_ea, consume_total_kg,
                                in_wh, prod_mgmt, prod_item_code, prod_steel_grade, prod_thickness, prod_width, prod_cut_length,
                                prod_color, prod_hardness, prod_spec_full, prod_lot_no, unit_weight_kg, qty_ea, total_weight_kg,
                                scrap_kg, cems_power_kwh, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, work_date, biz_date, project_name, worker_emp_no, worker_name, actor_emp_no,
                                wait_id, out_wh, "투입", consume_item_cd, grade, consume_spec,
                                heat_lot, in_kg, 1, round(in_kg - wip_kg, 1),
                                in_wh, project_name, prod_item_cd, grade, prod_t, prod_w, 0,
                                prod_color, hardness, prod_spec, prod_lot, unit_kg, qty_ea, prod_kg,
                                scrap_kg, cems_kwh, str(data.get("remarks") or "연마 완제품 생산 -> 완제품 창고 입고"),
                            ),
                        )

                        stage = "EXPORT_WAIT" if in_wh == "02.포장반" else "FINISHED_STOCK"
                        cur.execute(
                            """
                            INSERT INTO dbo.proc_5_finished_and_packing (
                                doc_date_no, business_date, process_stage, project_name, mgmt_item, out_wh, in_wh,
                                source_type, source_lot_no, pack_lot_no, item_code, steel_grade, thickness, width, cut_length,
                                color_name, hardness_display, spec_full, unit_weight_kg, qty_ea, total_weight_kg, remain_weight_kg,
                                worker_emp_no, worker_name, created_by_emp_no, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, biz_date, stage, project_name, project_name, out_wh, in_wh,
                                "GRIND", prod_lot, (f"{date_compact}-P01" if in_wh == "02.포장반" else None),
                                prod_item_cd, grade, prod_t, prod_w, 0,
                                prod_color, hardness, prod_spec, unit_kg, qty_ea, prod_kg, prod_kg,
                                worker_emp_no, worker_name, actor_emp_no, str(data.get("remarks") or "연마 완제품 입고"),
                            ),
                        )

                        cur.execute(
                            """
                            INSERT INTO dbo.ecount_erp_slips (
                                doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                consume_item_name, consume_spec, consume_serial_lot, consume_item_code,
                                prod_item_code, prod_item_name, prod_spec, color_name, prod_serial_lot,
                                unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, actor_emp_no, actor_name, project_name, project_name, worker_emp_no, worker_name,
                                grade, consume_spec, heat_lot, consume_item_cd,
                                prod_item_cd, grade, prod_spec, prod_color, prod_lot,
                                unit_kg, qty_ea, in_kg, prod_kg, round(in_kg - wip_kg, 1), out_wh, in_wh, scrap_kg, f"{out_wh} -> {in_wh}",
                            ),
                        )
                        if wip_kg > 0 and wip_lot:
                            cur.execute(
                                """
                                INSERT INTO dbo.ecount_erp_slips (
                                    doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                    consume_item_name, consume_spec, consume_serial_lot, consume_item_code,
                                    unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, actor_emp_no, actor_name, project_name, "재공", worker_emp_no, worker_name,
                                    grade, consume_spec, wip_lot, consume_item_cd,
                                    wip_kg, -1, -wip_kg, 0, -wip_kg, out_wh, out_wh, 0, f"연마 재공환원(수량-1, 단위환산량+{wip_kg}, 롯트:{wip_lot})",
                                ),
                            )
                        return prod_lot, wip_lot

                prod_lot, wip_lot = dosko_db.with_db_retry(_tx_grind)
                lid = f"L{disc}"
                record_work_event(
                    lid, "GRINDING_PRODUCTION", req_path, data,
                    lot_no=prod_lot, worker_name=worker_name, actor_emp_no=actor_emp_no, trigger_replica=False,
                )
                upsert_qc_inspection_draft({**data, "line_id": lid, "work_date": work_date, "lot_no": prod_lot}, inspector_user=actor)
                dosko_auth.log_audit(
                    emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                    resource_path=req_path, target_key=prod_lot, detail={"line_id": lid, "prod_kg": prod_kg},
                )
                self._send_json({"status": "ok", "prod_lot": prod_lot, "wip_lot": wip_lot, "prod_item_cd": prod_item_cd})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 9. Outsourcing Partner Slitting Inbound & Settlement
        # ---------------------------------------------------------------------
        if req_path == "/api/ecount/outsourcing-inbound":
            try:
                work_date = str(data.get("work_date") or time.strftime("%Y-%m-%d"))[:10].replace("/", "-")
                doc_date_no = f"{work_date.replace('-', '/')} -{int(time.time()) % 100 + 10}"
                if partner_ctx:
                    partner_code = partner_ctx["partner_code"]
                    out_wh = partner_ctx["wh_name"]
                    submitted_by = f"{out_wh} 외주포털"
                else:
                    partner_code = str(data.get("partner_code") or "E01").strip().upper()
                    out_wh = str(data.get("out_wh_name") or "4.이지철강")[:100]
                    submitted_by = str(data.get("submitted_by") or f"{actor_name} (내부등록)")[:80]

                in_wh = str(data.get("in_wh_name") or "0.도스코")[:100]
                raw_lot = str(data.get("raw_lot_no") or "").strip()[:80]
                raw_grade = str(data.get("raw_steel_grade") or "SK-5")[:40]
                raw_t = float(data.get("raw_thickness") or 1.6)
                raw_w = float(data.get("raw_width") or 1211.0)
                raw_spec = str(data.get("raw_spec_full") or f"{raw_t:.3f}*{raw_w:.2f}*(원재료)R")[:200]
                raw_item_cd, _ = make_ecount_item_code_and_spec(raw_grade, raw_t, raw_w, 0, "원재료", "R")

                input_kg = float(data.get("input_weight_kg") or 0)
                slit_items = data.get("slit_items") or []
                prod_total_ea = int(data.get("prod_total_ea") or sum(int(it.get("ea") or 1) for it in slit_items))
                prod_total_kg = float(data.get("prod_total_kg") or sum(float(it.get("ea", 1)) * float(it.get("unit_kg", 0)) for it in slit_items))
                wip_kg = float(data.get("wip_return_kg") or 0)
                wip_lot = str(data.get("wip_lot_no") or (increment_wip_lot_no(raw_lot) if wip_kg > 0 else ""))[:80]
                loss_kg = float(data.get("loss_weight_kg") or max(0.0, round(input_kg - prod_total_kg - wip_kg, 1)))
                proc_type = str(data.get("process_type") or "슬리팅")[:60]

                def _tx_os():
                    with dosko_db.get_db_connection() as conn:
                        cur = conn.cursor()
                        p_row = dosko_db.fetch_table_dicts(
                            cur,
                            "SELECT partner_code, wh_code, wh_name, slitting_price_kg, shearing_price_kg, edgemill_price_kg FROM dbo.proc_outsourcing_partners WHERE partner_code = ?",
                            (partner_code,),
                        )
                        p_info = p_row[0] if p_row else {}
                        eff_out_wh_code = str(p_info.get("wh_code") or partner_code)[:20]
                        eff_out_wh = str(p_info.get("wh_name") or out_wh)[:100]

                        if partner_ctx and raw_lot:
                            ord_owners = [
                                r[0]
                                for r in cur.execute("SELECT DISTINCT partner_code FROM dbo.proc_outsourcing_orders WHERE raw_lot_no = ?", (raw_lot,)).fetchall()
                            ]
                            if ord_owners and partner_code not in ord_owners:
                                raise PermissionError("타 협력사의 원소재 LOT에 대한 입고 등록은 차단됩니다.")

                        if partner_ctx and p_info:
                            if "면치" in proc_type or "에지" in proc_type:
                                eff_price = float(p_info.get("edgemill_price_kg") or 120.0)
                            elif "절단" in proc_type or "샤링" in proc_type:
                                eff_price = float(p_info.get("shearing_price_kg") or 80.0)
                            else:
                                eff_price = float(p_info.get("slitting_price_kg") or 65.0)
                            eff_sup = int(round(prod_total_kg * eff_price))
                            eff_vat = int(round(eff_sup * 0.1))
                            eff_tot = eff_sup + eff_vat
                        else:
                            eff_price = float(data.get("unit_price_kg") or p_info.get("slitting_price_kg") or 65.0)
                            eff_sup = int(data.get("supply_amt") or round(prod_total_kg * eff_price))
                            eff_vat = int(data.get("vat_amt") or round(eff_sup * 0.1))
                            eff_tot = eff_sup + eff_vat

                        first_lot = str(slit_items[0].get("lot") if slit_items else f"{work_date.replace('-', '')}-{partner_code}")[:80]
                        summary_spec = (
                            f"{raw_grade} {raw_t:.3f}*{float(slit_items[0].get('width', 100)):.2f} 외 {len(slit_items)}건"
                            if slit_items else raw_spec
                        )[:300]

                        cur.execute(
                            """
                            INSERT INTO dbo.proc_outsourcing_inbounds (
                                doc_date_no, work_date, project_code, project_name, mgmt_item,
                                partner_code, out_wh_code, out_wh_name, in_wh_code, in_wh_name, process_type,
                                raw_item_code, raw_steel_grade, raw_thickness, raw_width, raw_spec_full, raw_lot_no, input_weight_kg,
                                slit_items_json, prod_summary_spec, prod_first_lot_no, prod_total_ea, prod_total_kg,
                                wip_return_kg, wip_lot_no, loss_item_code, loss_item_name, loss_weight_kg,
                                unit_price_kg, settlement_basis, billable_weight_kg, supply_amt, vat_amt, total_amt,
                                settle_status, submitted_by, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, work_date, "20", "외주가공", "외주가공",
                                partner_code, eff_out_wh_code, eff_out_wh, "001", in_wh, proc_type,
                                raw_item_cd, raw_grade, raw_t, raw_w, raw_spec, raw_lot, input_kg,
                                json.dumps(slit_items, ensure_ascii=False), summary_spec, first_lot, prod_total_ea, prod_total_kg,
                                wip_kg, wip_lot, "0099", "단가조정", loss_kg,
                                eff_price, "PROD_KG", prod_total_kg, eff_sup, eff_vat, eff_tot,
                                "CONFIRMED", submitted_by, str(data.get("remarks") or "외주 슬리팅 완료 입고 및 가공비 정산"),
                            ),
                        )

                        ord_row = cur.execute(
                            "SELECT order_id, remain_weight_kg FROM dbo.proc_outsourcing_orders WHERE raw_lot_no = ? AND partner_code = ?",
                            (raw_lot, partner_code),
                        ).fetchone()
                        if ord_row:
                            oid, rem_kg = int(ord_row[0]), float(ord_row[1] or 0)
                            actual_deduct = max(0.0, input_kg - wip_kg)
                            new_ord_rem = max(0.0, round(rem_kg - actual_deduct, 1))
                            new_st = "COMPLETED" if new_ord_rem <= 0 else "PARTIAL"
                            cur.execute("UPDATE dbo.proc_outsourcing_orders SET remain_weight_kg = ?, status = ? WHERE order_id = ?", (new_ord_rem, new_st, oid))

                        for item in slit_items:
                            sw = float(item.get("width") or 100.0)
                            sea = int(item.get("ea") or 1)
                            sukg = float(item.get("unit_kg") or 500.0)
                            stotkg = round(sea * sukg, 1)
                            slot = str(item.get("lot") or f"{work_date.replace('-', '')}-D01")[:60]
                            s_item_cd, s_spec = make_ecount_item_code_and_spec(raw_grade, raw_t, sw, 0, "원재료", "R")
                            cur.execute(
                                """
                                INSERT INTO dbo.proc_1_raw_inbounds (
                                    doc_date_no, inbound_type, project_name, mgmt_item, partner_or_out_wh, in_wh,
                                    item_code, steel_grade, thickness, width, cut_length, color_name, hardness_display,
                                    spec_full, lot_no, unit_weight_kg, qty_ea, total_weight_kg, remain_weight_kg,
                                    worker_emp_no, worker_name, created_by_emp_no, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, "OUTSOURCING", "외주가공", "외주가공", eff_out_wh, "0.도스코",
                                    s_item_cd, raw_grade, raw_t, sw, 0, "원재료", "R",
                                    s_spec, slot, sukg, sea, stotkg, stotkg,
                                    actor_emp_no, actor_name, actor_emp_no, f"{eff_out_wh} 슬리팅 가공 입고 (원소재: {raw_lot})",
                                ),
                            )

                        cur.execute(
                            """
                            INSERT INTO dbo.ecount_erp_slips (
                                doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                consume_item_name, consume_spec, consume_serial_lot, consume_item_code,
                                unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                            """,
                            (
                                doc_date_no, actor_emp_no, actor_name, "외주가공", "투입", actor_emp_no, actor_name,
                                raw_grade, raw_spec, raw_lot, raw_item_cd,
                                input_kg, 1, input_kg, 0, input_kg, eff_out_wh, in_wh, 0, "외주 원소재 투입",
                            ),
                        )
                        if wip_kg > 0 and wip_lot:
                            cur.execute(
                                """
                                INSERT INTO dbo.ecount_erp_slips (
                                    doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                    consume_item_name, consume_spec, consume_serial_lot, consume_item_code,
                                    unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, actor_emp_no, actor_name, "외주가공", "재공", actor_emp_no, actor_name,
                                    raw_grade, raw_spec, wip_lot, raw_item_cd,
                                    wip_kg, -1, -wip_kg, 0, -wip_kg, eff_out_wh, eff_out_wh, 0, f"외주 재공환원(수량-1, 단위환산량+{wip_kg}, 창고:{eff_out_wh})",
                                ),
                            )
                        for item in slit_items:
                            sw = float(item.get("width") or 100.0)
                            sea = int(item.get("ea") or 1)
                            sukg = float(item.get("unit_kg") or 500.0)
                            stotkg = round(sea * sukg, 1)
                            slot = str(item.get("lot") or f"{work_date.replace('-', '')}-D01")[:80]
                            s_item_cd, s_spec = make_ecount_item_code_and_spec(raw_grade, raw_t, sw, 0, "원재료", "R")
                            cur.execute(
                                """
                                INSERT INTO dbo.ecount_erp_slips (
                                    doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                    prod_item_code, prod_item_name, prod_spec, color_name, prod_serial_lot,
                                    unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, actor_emp_no, actor_name, "외주가공", "외주가공", actor_emp_no, actor_name,
                                    s_item_cd, raw_grade, s_spec, "원재료", slot,
                                    sukg, sea, stotkg, stotkg, stotkg, eff_out_wh, in_wh, 0, f"{eff_out_wh} 슬리팅 입고",
                                ),
                            )
                        if loss_kg > 0:
                            cur.execute(
                                """
                                INSERT INTO dbo.ecount_erp_slips (
                                    doc_date_no, writer_emp_no, writer_name, project_name, mgmt_item, manager_emp_no, manager_name,
                                    prod_item_code, prod_item_name, prod_spec, color_name, prod_serial_lot,
                                    unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks
                                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    doc_date_no, actor_emp_no, actor_name, "외주가공", "단가조정", actor_emp_no, actor_name,
                                    "0099", "단가조정", "단가조정", "원재료", "단가조정",
                                    loss_kg, 1, loss_kg, loss_kg, loss_kg, eff_out_wh, in_wh, loss_kg, f"슬리팅 스크랩/로스 정산 ({loss_kg}kg)",
                                ),
                            )

                        cur.execute(
                            """
                            INSERT INTO dbo.ecount_daily_report_details (
                                report_date, section_type, seq_no, orig_gubun, material_grade, spec_normalized,
                                division_or_color, wage_or_fin_qty_kg, partner_name, remarks
                            ) VALUES (?, N'PURCH', 1, N'임가공', ?, ?, N'원재료', ?, ?, ?);
                            """,
                            (
                                work_date, raw_grade, f"{raw_t:.3f}*{raw_w:.2f}", prod_total_kg, eff_out_wh, f"슬리팅:{prod_total_kg}kg,단가조정:{loss_kg}kg",
                            ),
                        )
                        return eff_tot

                try:
                    eff_tot = dosko_db.with_db_retry(_tx_os)
                except PermissionError as pe:
                    self._send_json({"error": "PARTNER_ORDER_MISMATCH", "message": str(pe)}, 403)
                    return

                record_work_event("OUTSOURCING", "OUTSOURCING_INBOUND", req_path, data, lot_no=raw_lot, worker_name=actor_name, actor_emp_no=actor_emp_no)
                dosko_auth.log_audit(
                    emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                    user_id=(None if partner_ctx else actor.get("user_id")), client_ip=client_ip,
                    action_type="DATA_WRITE", resource_path=req_path, target_key=f"{partner_code}:{raw_lot}",
                    detail={"forced_partner_code": partner_code, "prod_total_kg": prod_total_kg, "total_amt": eff_tot},
                )
                self._send_json({"status": "ok", "partner_code": partner_code, "doc_date_no": doc_date_no, "prod_total_kg": prod_total_kg, "total_amt": eff_tot})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 10. Warehouse & Shipment Action
        # ---------------------------------------------------------------------
        if req_path == "/api/ecount/warehouse-action":
            try:
                act = str(data.get("action") or "").upper()
                work_date = str(data.get("work_date") or time.strftime("%Y-%m-%d"))[:10].replace("/", "-")
                biz_date = dosko_db.calc_business_date(work_date).strftime("%Y-%m-%d")
                date_compact = work_date.replace("-", "")

                def _tx_wh():
                    with dosko_db.get_db_connection() as conn:
                        cur = conn.cursor()
                        if act == "RAW_INBOUND":
                            grade = str(data.get("steel_grade") or "SK-5")[:40]
                            t = float(data.get("thickness") or 1.2)
                            w = float(data.get("width") or 121.0)
                            partner = str(data.get("partner_name") or "4.디아이스틸")[:100]
                            inbound_type = str(data.get("inbound_type") or "PURCHASE")[:30]
                            unit_kg = float(data.get("unit_weight_kg") or 1500)
                            qty_ea = int(data.get("qty_ea") or 1)
                            tot_kg = round(unit_kg * qty_ea, 1)
                            lot_no = str(data.get("lot_no") or f"{date_compact}-D01")[:60]
                            item_cd, spec_full = make_ecount_item_code_and_spec(grade, t, w, 0, "원재료", "R")
                            cur.execute(
                                """
                                INSERT INTO dbo.proc_1_raw_inbounds (
                                    doc_date_no, inbound_type, project_name, mgmt_item, partner_or_out_wh, in_wh,
                                    item_code, steel_grade, thickness, width, cut_length, color_name, hardness_display,
                                    spec_full, lot_no, unit_weight_kg, qty_ea, total_weight_kg, remain_weight_kg,
                                    worker_emp_no, worker_name, created_by_emp_no, remarks
                                ) VALUES (?,?,?,?,?,N'0.도스코',?,?,?,?,0,N'원재료',N'R',?,?,?,?,?,?,?,?,?,?);
                                """,
                                (
                                    f"{work_date.replace('-', '/')} -1", inbound_type, "외주가공", "외주가공", partner,
                                    item_cd, grade, t, w, spec_full, lot_no, unit_kg, qty_ea, tot_kg, tot_kg,
                                    actor_emp_no, actor_name, actor_emp_no, str(data.get("remarks") or "0.도스코 원재료/외주 입고"),
                                ),
                            )
                            cur.execute(
                                """
                                INSERT INTO dbo.ecount_daily_report_details (
                                    report_date, section_type, seq_no, orig_gubun, material_grade, spec_normalized,
                                    division_or_color, raw_qty_kg, wage_or_fin_qty_kg, partner_name, remarks
                                ) VALUES (?, N'PURCH', 1, ?, ?, ?, N'원재료', ?, ?, ?, ?);
                                """,
                                (
                                    work_date, ("임가공" if inbound_type != "PURCHASE" else "매입"), grade, f"{t:.3f}*{w:.2f}",
                                    (tot_kg if inbound_type == "PURCHASE" else 0), (tot_kg if inbound_type != "PURCHASE" else 0), partner, f"LOT:{lot_no}",
                                ),
                            )
                        elif act == "TRANSFER_PACKING":
                            pack_id = data.get("pack_id")
                            pack_lot = str(data.get("pack_lot_no") or f"{date_compact}-P01")[:80]
                            dest = str(data.get("customer_or_dest") or "수출 바이어")[:120]
                            packing_spec = str(data.get("packing_spec") or "수출 우든박스/방청포장")[:300]
                            if pack_id:
                                cur.execute(
                                    """
                                    UPDATE dbo.proc_5_finished_and_packing
                                    SET process_stage=N'EXPORT_WAIT', project_name=N'포장반', mgmt_item=N'포장반',
                                        out_wh=N'01.완제품', in_wh=N'02.포장반', pack_lot_no=?, customer_or_dest=?, packing_spec=?,
                                        worker_emp_no=COALESCE(worker_emp_no, ?), worker_name=COALESCE(worker_name, ?),
                                        created_by_emp_no=COALESCE(created_by_emp_no, ?)
                                    WHERE pack_id = ?;
                                    """,
                                    (pack_lot, dest, packing_spec, actor_emp_no, actor_name, actor_emp_no, int(pack_id)),
                                )
                        elif act == "CREATE_SHIPMENT":
                            recipient = str(data.get("recipient_name") or "(주)도스코 거래처")[:120]
                            out_wh = str(data.get("out_wh_name") or "01.완제품")[:80]
                            serial_no = str(data.get("slip_serial_no") or f"{date_compact}-1")[:40]
                            items = data.get("items") or []
                            doc_tot_ea = sum(int(it.get("qty_ea") or 1) for it in items)
                            doc_tot_kg = sum(float(it.get("total_weight_kg") or 0) for it in items)
                            for it in items:
                                grade = str(it.get("steel_grade") or "SK-5")[:40]
                                spec_disp = str(it.get("spec_display") or "1.200*120.00")[:200]
                                cat_name = str(it.get("category_name") or "연마(SIDE)")[:60]
                                lot_no = str(it.get("lot_no") or "")[:80]
                                u_kg = float(it.get("unit_weight_kg") or 200)
                                q_ea = int(it.get("qty_ea") or 1)
                                t_kg = float(it.get("total_weight_kg") or (u_kg * q_ea))
                                price = float(it.get("unit_price") or 4200)
                                sup = round(t_kg * price)
                                vat = round(sup * 0.1)
                                cur.execute(
                                    """
                                    INSERT INTO dbo.ecount_delivery_slips (
                                        slip_date, slip_serial_no, recipient_name, out_wh_name, sales_gubun,
                                        steel_grade, spec_display, category_name, lot_no, unit_weight_kg, qty_ea,
                                        total_weight_kg, unit_price, supply_amt, vat_amt, page_no, created_by_emp_no
                                    ) VALUES (?,?,?,?,N'제품',?,?,?,?,?,?,?,?,?,?,1,?);
                                    """,
                                    (work_date, serial_no, recipient, out_wh, grade, spec_disp, cat_name, lot_no, u_kg, q_ea, t_kg, price, sup, vat, actor_emp_no),
                                )
                                weights_arr = it.get("coil_weights") or [round(u_kg, 1)] * max(1, q_ea)
                                cur.execute(
                                    """
                                    INSERT INTO dbo.ecount_packing_lists (
                                        packing_date, serial_num, recipient_name, out_wh_name, layout_id,
                                        spec_clean, steel_grade, category_name, coil_count_ea, coil_weights_json,
                                        subtotal_weight_kg, doc_total_qty_ea, doc_total_weight_kg, created_by_emp_no, remarks
                                    ) VALUES (?,?,?,?,N'_fixed_4dan',?,?,?,?,?,?,?,?,?,?);
                                    """,
                                    (
                                        work_date, serial_no, recipient, out_wh, spec_disp, grade, cat_name, q_ea,
                                        json.dumps(weights_arr, ensure_ascii=False), t_kg, doc_tot_ea, doc_tot_kg,
                                        actor_emp_no, str(data.get("remarks") or ""),
                                    ),
                                )
                                if it.get("pack_id"):
                                    st_new = "EXPORT_SHIPPED" if out_wh == "02.포장반" else "DOMESTIC_OUT"
                                    cur.execute(
                                        "UPDATE dbo.proc_5_finished_and_packing SET process_stage = ?, remain_weight_kg = 0, customer_or_dest = ? WHERE pack_id = ?",
                                        (st_new, recipient, int(it.get("pack_id"))),
                                    )
                                cur.execute(
                                    """
                                    INSERT INTO dbo.ecount_daily_report_details (
                                        report_date, section_type, seq_no, orig_gubun, material_grade, spec_normalized,
                                        division_or_color, wage_or_fin_qty_kg, partner_name, remarks
                                    ) VALUES (?, N'SALES', 1, N'제품', ?, ?, ?, ?, ?, ?);
                                    """,
                                    (work_date, grade, spec_disp.split("*(")[0][:80], cat_name, t_kg, recipient, f"전표:{serial_no}"),
                                )
                        elif act == "SAVE_ADDRESS":
                            p_nm = str(data.get("partner_name") or "")[:120]
                            r_nm = str(data.get("receiver_name") or "")[:120]
                            ph_no = str(data.get("phone_no") or "")[:60]
                            zp = str(data.get("zip_code") or "")[:20]
                            rd = str(data.get("road_address") or "")[:300]
                            dt_addr = str(data.get("detail_address") or "")[:300]
                            wh_f = str(data.get("wh_filter") or "01.완제품")[:60]
                            cur.execute(
                                """
                                MERGE dbo.ecount_address_book WITH (HOLDLOCK) AS tgt
                                USING (SELECT ? AS partner_name, ? AS receiver_name, ? AS zip_code, ? AS detail_address) AS src
                                   ON tgt.partner_name = src.partner_name
                                  AND tgt.receiver_name = src.receiver_name
                                  AND ISNULL(tgt.zip_code, N'') = ISNULL(src.zip_code, N'')
                                  AND ISNULL(tgt.detail_address, N'') = ISNULL(src.detail_address, N'')
                                WHEN MATCHED THEN
                                    UPDATE SET saved_date = ?, phone_no = ?, road_address = ?, wh_filter = ?
                                WHEN NOT MATCHED THEN
                                    INSERT (saved_date, partner_name, receiver_name, phone_no, zip_code, road_address, detail_address, wh_filter)
                                    VALUES (?,?,?,?,?,?,?,?);
                                """,
                                (p_nm, r_nm, zp, dt_addr, work_date, ph_no, rd, wh_f, work_date, p_nm, r_nm, ph_no, zp, rd, dt_addr, wh_f),
                            )

                dosko_db.with_db_retry(_tx_wh)
                record_work_event(
                    ("PACK" if act == "TRANSFER_PACKING" else "WAREHOUSE"),
                    f"WAREHOUSE_{act or 'ACTION'}", req_path, data, worker_name=actor_name, actor_emp_no=actor_emp_no,
                )
                dosko_auth.log_audit(
                    emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                    user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                    resource_path=req_path, target_key=act,
                )
                self._send_json({"status": "ok", "action": act, "business_date": biz_date})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 11. Production Coils / Logs Sync (/api/coils)
        # ---------------------------------------------------------------------
        if req_path == "/api/coils":
            try:
                lid = str(data.get("lid") or data.get("line_id") or "").strip().upper()
                date_str = str(data.get("date") or datetime.datetime.now().strftime("%Y-%m-%d"))[:10]
                logs = data.get("logs") if "logs" in data else data.get("coils")
                if lid and isinstance(logs, list):
                    logs = _scrub_state_dict(logs)
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["coils"][f"{date_str}_{lid}"] = logs
                    save_state_to_files()
                    latest = logs[-1] if logs and isinstance(logs[-1], dict) else {}
                    lot_no = str(latest.get("lot") or latest.get("prod_lot") or "")
                    worker_name = _clean_worker_name(latest.get("worker") or actor_name)
                    record_work_event(
                        lid, "COILS_SAVE", req_path, {"date": date_str, "count": len(logs), "latest": latest},
                        trigger_plc="EVENT_INPUT", lot_no=lot_no, worker_name=worker_name, actor_emp_no=actor_emp_no, trigger_replica=False,
                    )
                    if latest:
                        upsert_qc_inspection_draft({
                            "work_date": date_str,
                            "line_id": lid,
                            "lot_no": lot_no,
                            "steel_grade": latest.get("grade"),
                            "target_t": latest.get("target_t") or latest.get("in_t"),
                            "target_w": latest.get("target_w") or latest.get("in_w"),
                            "hardness": f"{latest.get('hardness_type', '')} {latest.get('target_h', '')}".strip(),
                            "out_t": latest.get("out_t"),
                            "out_w": latest.get("out_w"),
                            "out_h": latest.get("out_h"),
                            "pass_fail": latest.get("pass_fail"),
                            "remarks": latest.get("note") or latest.get("remarks") or "",
                        }, inspector_user=actor)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{date_str}_{lid}", detail={"count": len(logs)},
                    )
                self._send_json({"status": "ok"})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 12. Spec / Mixed Update (/api/spec, /api/update_spec, /api/update)
        # ---------------------------------------------------------------------
        if req_path in ["/api/spec", "/api/update_spec", "/api/update"]:
            try:
                lid = str(data.get("lid") or data.get("line_id") or "").strip().upper()
                date_str = str(data.get("date") or datetime.datetime.now().strftime("%Y-%m-%d"))[:10]

                if "coils" in data and lid:
                    coils = _scrub_state_dict(data.get("coils") or [])
                    worker = _clean_worker_name(data.get("worker"))
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["coils"][f"{date_str}_{lid}"] = coils
                        if worker and worker != "-":
                            server_state["workers"][lid] = worker
                            server_state["workers"][f"{date_str}_{lid}"] = worker
                            if lid in server_state.get("cleared_workers", []):
                                server_state["cleared_workers"].remove(lid)
                    save_state_to_files()
                    latest = coils[-1] if coils and isinstance(coils[-1], dict) else {}
                    lot_no = str(latest.get("lot") or latest.get("prod_lot") or "")
                    record_work_event(
                        lid, "COIL_UPDATE", req_path, {"date": date_str, "count": len(coils), "latest": latest},
                        trigger_plc="EVENT_INPUT", lot_no=lot_no, worker_name=(worker or actor_name), actor_emp_no=actor_emp_no, trigger_replica=False,
                    )
                    if latest:
                        upsert_qc_inspection_draft({
                            "work_date": date_str,
                            "line_id": lid,
                            "lot_no": lot_no,
                            "steel_grade": latest.get("grade"),
                            "target_t": latest.get("target_t") or latest.get("in_t"),
                            "target_w": latest.get("target_w") or latest.get("in_w"),
                            "hardness": f"{latest.get('hardness_type', '')} {latest.get('target_h', '')}".strip(),
                            "out_t": latest.get("out_t"),
                            "out_w": latest.get("out_w"),
                            "out_h": latest.get("out_h"),
                            "pass_fail": latest.get("pass_fail"),
                            "remarks": latest.get("note") or latest.get("remarks") or "",
                        }, inspector_user=actor)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{date_str}_{lid}",
                    )
                    self._send_json({"status": "ok"})
                    return

                spec = _scrub_state_dict(data.get("spec") or data)
                if lid and spec:
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["specs"][lid] = spec
                        server_state["specs"][f"{date_str}_{lid}"] = spec
                        if "cleared_specs" in server_state and lid in server_state["cleared_specs"]:
                            server_state["cleared_specs"].remove(lid)
                    save_state_to_files()
                    lot_no = str(spec.get("lot") or spec.get("prod_lot") or "") if isinstance(spec, dict) else ""
                    record_work_event(
                        lid, "SPEC_UPDATE", req_path, spec, trigger_plc="EVENT_INPUT",
                        lot_no=lot_no, worker_name=actor_name, actor_emp_no=actor_emp_no,
                    )
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{date_str}_{lid}",
                    )
                self._send_json({"status": "ok"})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        # ---------------------------------------------------------------------
        # 13. Unified Action Sync (/api/sync)
        # ---------------------------------------------------------------------
        if req_path == "/api/sync":
            try:
                action = str(data.get("action") or "").strip().upper()
                payload = _scrub_state_dict(data.get("payload") or {})
                lid = str(payload.get("line_id") or payload.get("lid") or data.get("line_id") or data.get("lid") or "").strip().upper()
                date_str = str(payload.get("date") or data.get("date") or datetime.datetime.now().strftime("%Y-%m-%d"))[:10]

                if action == "UPDATE_WORKER" and lid:
                    worker = _clean_worker_name(payload.get("worker"))
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        if worker and worker != "-":
                            server_state["workers"][lid] = worker
                            server_state["workers"][f"{date_str}_{lid}"] = worker
                            if lid in server_state.get("cleared_workers", []):
                                server_state["cleared_workers"].remove(lid)
                        else:
                            server_state["workers"].pop(lid, None)
                            server_state["workers"].pop(f"{date_str}_{lid}", None)
                    save_state_to_files()
                    record_work_event(lid, "WORKER_UPDATE", req_path, payload, worker_name=worker, actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{action}:{lid}", detail={"worker": worker},
                    )
                    self._send_json({"status": "ok"})
                    return

                if action == "CLEAR_SPEC_AND_WORKER" and lid:
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["specs"].pop(lid, None)
                        server_state["specs"].pop(f"{date_str}_{lid}", None)
                        server_state["workers"].pop(lid, None)
                        server_state["workers"].pop(f"{date_str}_{lid}", None)
                        if lid not in server_state["cleared_specs"]:
                            server_state["cleared_specs"].append(lid)
                        if lid not in server_state["cleared_workers"]:
                            server_state["cleared_workers"].append(lid)
                    save_state_to_files()
                    record_work_event(lid, "CLEAR_SPEC_AND_WORKER", req_path, payload, trigger_plc="EVENT_END", actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{action}:{lid}",
                    )
                    self._send_json({"status": "ok"})
                    return

                if action == "UPDATE_WORK_ORDERS" and lid:
                    wos = payload.get("work_orders") if isinstance(payload.get("work_orders"), list) else []
                    with state_lock:
                        prev_wos = server_state.get("work_orders", {}).get(lid) or []
                        prev_running = {w.get("id") for w in prev_wos if isinstance(w, dict) and w.get("status") == "running"}
                        prev_completed = {w.get("id") for w in prev_wos if isinstance(w, dict) and w.get("status") == "completed"}
                        new_running = {w.get("id") for w in wos if isinstance(w, dict) and w.get("status") == "running"}
                        new_completed = {w.get("id") for w in wos if isinstance(w, dict) and w.get("status") == "completed"}
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["work_orders"][lid] = wos
                    save_state_to_files()
                    trig = "EVENT_START" if (new_running - prev_running) else ("EVENT_END" if (new_completed - prev_completed) else "EVENT_INPUT")
                    active_wo = next((w for w in wos if isinstance(w, dict) and w.get("status") == "running"), None)
                    lot_no = str(active_wo.get("lot") or "") if active_wo else ""
                    record_work_event(lid, "WORK_ORDERS_UPDATE", req_path, payload, trigger_plc=trig, lot_no=lot_no, actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{action}:{lid}",
                    )
                    self._send_json({"status": "ok"})
                    return

                if action == "START_WORK_ORDER" and lid:
                    wo_id = payload.get("wo_id")
                    start_time = payload.get("start_time") or datetime.datetime.now().strftime("%H:%M")
                    lot_no = ""
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["statuses"][lid] = "정상가동"
                        wos = server_state.get("work_orders", {}).get(lid) or []
                        for w in wos:
                            if not isinstance(w, dict):
                                continue
                            if w.get("id") == wo_id:
                                w["status"] = "running"
                                w["started_at_time"] = start_time
                                lot_no = str(w.get("lot") or "")
                            elif w.get("status") == "running":
                                w["status"] = "completed"
                                w["completed_at_time"] = start_time
                        lws = server_state.setdefault("line_work_state", {}).get(lid) or {}
                        lws.update({
                            "status": "running",
                            "work_start_time": start_time,
                            "current_coil_start_time": start_time,
                            "active_wo_id": wo_id or lws.get("active_wo_id", ""),
                        })
                        server_state["line_work_state"][lid] = lws
                    save_state_to_files()
                    record_work_event(lid, "WORK_START", req_path, payload, trigger_plc="EVENT_START", lot_no=lot_no, actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{action}:{lid}",
                    )
                    self._send_json({"status": "ok"})
                    return

                if action == "UPDATE_LINE_WORK_STATE" and lid:
                    st_obj = payload.get("state") or {}
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state.setdefault("line_work_state", {})[lid] = st_obj
                    save_state_to_files()
                    st_name = str(st_obj.get("status") or "").lower()
                    trig = "EVENT_START" if st_name == "running" else ("EVENT_END" if st_name in ("completed", "stopped", "idle") else "EVENT_INPUT")
                    record_work_event(lid, f"WORK_STATE_{st_name.upper() or 'UPDATE'}", req_path, payload, trigger_plc=trig, actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=f"{action}:{lid}",
                    )
                    self._send_json({"status": "ok"})
                    return

                self._send_json({"status": "ok", "ignored_action": action})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        if req_path == "/api/worker":
            try:
                lid = str(data.get("lid") or data.get("line_id") or "").strip().upper()
                worker = _clean_worker_name(data.get("worker"))
                date_str = data.get("date")
                if lid:
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        if worker and worker != "-":
                            server_state["workers"][lid] = worker
                            if date_str:
                                server_state["workers"][f"{str(date_str)[:10]}_{lid}"] = worker
                            if "cleared_workers" in server_state and lid in server_state["cleared_workers"]:
                                server_state["cleared_workers"].remove(lid)
                        else:
                            server_state["workers"].pop(lid, None)
                    save_state_to_files()
                    record_work_event(lid, "WORKER_UPDATE", req_path, data, worker_name=worker, actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=lid, detail={"worker": worker},
                    )
                self._send_json({"status": "ok"})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        if req_path == "/api/status":
            try:
                lid = str(data.get("lid") or data.get("line_id") or "").strip().upper()
                status = data.get("status")
                if lid and status:
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["statuses"][lid] = status
                    save_state_to_files()
                    trig = "EVENT_START" if status == "정상가동" else ("EVENT_END" if status == "비가동" else "EVENT_INPUT")
                    record_work_event(lid, "STATUS_CHANGE", req_path, data, trigger_plc=trig, actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=lid, detail={"status": status},
                    )
                self._send_json({"status": "ok"})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        if req_path in ["/api/clear-spec", "/api/clear_spec", "/api/clear-all"]:
            try:
                lid = str(data.get("lid") or data.get("line_id") or "").strip().upper()
                date_str = data.get("date")
                if lid:
                    with state_lock:
                        server_state["version"] += 1
                        server_state["last_updated"] = time.time()
                        server_state["specs"].pop(lid, None)
                        server_state["workers"].pop(lid, None)
                        if date_str:
                            server_state["specs"].pop(f"{str(date_str)[:10]}_{lid}", None)
                            server_state["workers"].pop(f"{str(date_str)[:10]}_{lid}", None)
                        server_state["statuses"][lid] = "비가동"
                        if lid not in server_state.get("cleared_specs", []):
                            server_state.setdefault("cleared_specs", []).append(lid)
                        if lid not in server_state.get("cleared_workers", []):
                            server_state.setdefault("cleared_workers", []).append(lid)
                    save_state_to_files()
                    record_work_event(lid, "CLEAR_SPEC", req_path, data, trigger_plc="EVENT_END", actor_emp_no=actor_emp_no)
                    dosko_auth.log_audit(
                        emp_no=actor_emp_no, display_name=actor_name, role_code=actor_role,
                        user_id=actor.get("user_id"), client_ip=client_ip, action_type="DATA_WRITE",
                        resource_path=req_path, target_key=lid,
                    )
                self._send_json({"status": "ok", "cleared": True})
                return
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
                return

        self.send_response(404)
        self._set_cors_headers()
        self.end_headers()


def _is_port_actively_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.8)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def run_server():
    if _is_port_actively_listening(PORT):
        log_msg(f"[동기화 서버] 포트 {PORT}에 이미 동작 중인 서버가 있어 중복 기동을 종료합니다.")
        return
    ensure_qc_plc_tables()
    load_initial_state()
    plc_thread = threading.Thread(target=plc_collector_loop, daemon=True)
    plc_thread.start()
    server_address = ("0.0.0.0", PORT)
    try:
        httpd = DoskoThreadingHTTPServer(server_address, SyncHandler)
    except OSError as e:
        log_msg(f"[동기화 서버 포트 바인딩 실패] port={PORT} err={e} — 기존 인스턴스 동작 중일 수 있어 종료합니다.")
        return
    log_msg(f"[동기화 서버 v2.0] http://0.0.0.0:{PORT} (MSSQL DOSKO_ERP + 사번로그인 + RBAC/호기격리 + PLC 5분 DB저장) 기동 시작")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log_msg("[동기화 서버] 정상 종료")


if __name__ == "__main__":
    run_server()
