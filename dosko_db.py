# -*- coding: utf-8 -*-
"""
(주)도스코 통합 데이터베이스 어댑터 모듈 v2.0 (dosko_db.py)
------------------------------------------------------------------------------
- Microsoft SQL Server 2019 Standard (DOSKO_ERP, TCP 5539) 단일 정본 엔진
- pyodbc ODBC 커넥션 풀링(pyodbc.pooling=True) + 자동 헬스체크/지수 백오프 재연결
- 최소권한 서비스 계정(dosko_srv_app) 우선 사용 / 실패 시 Windows 통합인증 폴백
- JSON 직렬화 호환(Decimal -> int/float, date/datetime -> 'YYYY-MM-DD[ HH:MM:SS]')
"""

import datetime
import decimal
import json
import os
import re
import sqlite3
import threading
import time

try:
    import pyodbc
    pyodbc.pooling = True
    HAS_PYODBC = True
except ImportError:
    pyodbc = None
    HAS_PYODBC = False

try:
    import pymssql
    HAS_PYMSSQL = True
except ImportError:
    pymssql = None
    HAS_PYMSSQL = False

USE_MSSQL = True

MSSQL_CONFIG = {
    "server": "127.0.0.1",
    "port": 5539,
    "database": "DOSKO_ERP",
    "charset": "utf8",
    "login_timeout": 5,
    "query_timeout": 15,
}

APP_CRED_PATH = r"C:\Users\song9\.dosko_mssql_app.json"

SQLITE_DB_PATHS = [
    r"C:\Users\song9\OneDrive\바탕 화면\test-data\dosko_ecount.db",
    r"C:\Users\song9\OneDrive\바탕 화면\dosko_ecount.db",
    r"Z:\HDD1\생산일지\dosko_ecount.db",
]

_ODBC_DRIVER_CACHE = None
_ODBC_LOCK = threading.Lock()
_TRANSIENT_SQLSTATES = {"08S01", "08001", "08003", "08004", "08007", "40001", "40197", "40501", "40613", "HYT00", "HYT01"}


def get_primary_sqlite_db():
    for p in SQLITE_DB_PATHS:
        if os.path.exists(p):
            return p
    return SQLITE_DB_PATHS[0]


def _pick_odbc_driver():
    global _ODBC_DRIVER_CACHE
    if _ODBC_DRIVER_CACHE:
        return _ODBC_DRIVER_CACHE
    with _ODBC_LOCK:
        if _ODBC_DRIVER_CACHE:
            return _ODBC_DRIVER_CACHE
        if not HAS_PYODBC:
            return None
        installed = set(pyodbc.drivers())
        for cand in (
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server",
        ):
            if cand in installed:
                _ODBC_DRIVER_CACHE = cand
                return cand
    return None


def _load_app_sql_creds():
    if os.path.exists(APP_CRED_PATH):
        try:
            with open(APP_CRED_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("username") and data.get("password"):
                return data["username"], data["password"]
        except Exception:
            pass
    return None, None


def _build_pyodbc_conn_str(use_app_login=True):
    drv = _pick_odbc_driver()
    if not drv:
        raise RuntimeError("SQL Server ODBC 드라이버(17/18)를 찾을 수 없습니다.")
    srv = f"{MSSQL_CONFIG['server']},{MSSQL_CONFIG['port']}"
    db = MSSQL_CONFIG["database"]
    parts = [
        f"DRIVER={{{drv}}}",
        f"SERVER={srv}",
        f"DATABASE={db}",
        "APP=DOSKO_SYNC_V2",
    ]
    if "18" in drv or "17" in drv:
        parts.extend(["Encrypt=no", "TrustServerCertificate=yes"])
    uid, pwd = _load_app_sql_creds() if use_app_login else (None, None)
    if uid and pwd:
        parts.extend([f"UID={uid}", f"PWD={pwd}"])
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts) + ";"


def calc_business_date(dt=None):
    """[D-1 16:00:00, D 16:00:00) 영업일 귀속 계산 (D 반환)"""
    if dt is None:
        dt = datetime.datetime.now()
    elif isinstance(dt, str):
        s = dt.strip()[:19]
        if len(s) == 10:
            return datetime.date.fromisoformat(s)
        dt = datetime.datetime.fromisoformat(s.replace("/", "-"))
    if dt.hour >= 16:
        return (dt + datetime.timedelta(days=1)).date()
    return dt.date()


def json_safe(val):
    if val is None:
        return None
    if isinstance(val, decimal.Decimal):
        if val == val.to_integral_value():
            return int(val)
        return float(val)
    if isinstance(val, datetime.datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, datetime.date):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


def is_transient_db_error(exc):
    msg = repr(exc).upper()
    for code in _TRANSIENT_SQLSTATES:
        if code in msg:
            return True
    for needle in ("COMMUNICATION LINK FAILURE", "TCP PROVIDER", "TIMED OUT", "CONNECTION IS BUSY", "BROKEN PIPE", "10054", "10053", "233"):
        if needle in msg:
            return True
    return False


def with_db_retry(fn, max_attempts=3):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1 and is_transient_db_error(e):
                time.sleep(0.15 * (2 ** attempt))
                continue
            raise
    if last_exc:
        raise last_exc


class UnifiedCursor:
    """pyodbc / pymssql / sqlite3 공용 커서 (기존 SQLite 호출 호환 + T-SQL 직접 실행 지원)"""
    def __init__(self, raw_cursor, engine_kind):
        self._cur = raw_cursor
        self._engine = engine_kind  # 'pyodbc' | 'pymssql' | 'sqlite'
        self._is_mssql = engine_kind in ("pyodbc", "pymssql")
        self._last_id = None

    def _translate_sql(self, query):
        if not self._is_mssql:
            return query
        sql = query

        # 1) 동적/정적 LIMIT 변환 -> OFFSET 0 ROWS FETCH NEXT n ROWS ONLY (ORDER BY 있는 경우)
        #    ORDER BY 없으면 간단 SELECT TOP n 형태 지원
        if re.search(r'\bLIMIT\s+(\?|\d+)\b', sql, flags=re.IGNORECASE):
            if re.search(r'\bORDER\s+BY\b', sql, flags=re.IGNORECASE):
                sql = re.sub(
                    r'\bLIMIT\s+\?\b',
                    'OFFSET 0 ROWS FETCH NEXT CAST(? AS INT) ROWS ONLY',
                    sql,
                    flags=re.IGNORECASE,
                )
                sql = re.sub(
                    r'\bLIMIT\s+(\d+)\b',
                    r'OFFSET 0 ROWS FETCH NEXT \1 ROWS ONLY',
                    sql,
                    flags=re.IGNORECASE,
                )
            else:
                sql = re.sub(r'\bLIMIT\s+(\d+)\b', '', sql, flags=re.IGNORECASE)

        if self._engine == "pymssql":
            sql = sql.replace("?", "%s")
        return sql

    def execute(self, query, params=None):
        sql = self._translate_sql(query)
        if params is None:
            res = self._cur.execute(sql)
        else:
            clean_params = tuple(None if v is None else v for v in params)
            res = self._cur.execute(sql, clean_params)

        if self._is_mssql:
            st = sql.lstrip().upper()
            if st.startswith("INSERT") and "OUTPUT INSERTED" not in st:
                try:
                    self._cur.execute("SELECT CAST(SCOPE_IDENTITY() AS BIGINT)")
                    row = self._cur.fetchone()
                    if row and row[0] is not None:
                        self._last_id = int(row[0])
                except Exception:
                    pass
        return res

    def executemany(self, query, seq_of_parameters):
        sql = self._translate_sql(query)
        if self._engine == "pyodbc" and hasattr(self._cur, "fast_executemany"):
            try:
                self._cur.fast_executemany = True
            except Exception:
                pass
        return self._cur.executemany(sql, seq_of_parameters)

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    def nextset(self):
        if hasattr(self._cur, "nextset"):
            return self._cur.nextset()
        return False

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        if self._is_mssql:
            return self._last_id
        return getattr(self._cur, "lastrowid", None)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._cur, "close"):
            try:
                self._cur.close()
            except Exception:
                pass
        return False


class UnifiedConnection:
    """pyodbc 풀링 우선 -> pymssql 폴백 / SQLite 겸용 커넥션 래퍼"""
    def __init__(self, is_mssql=None, db_path=None, use_app_login=True, autocommit=False):
        self.is_mssql = USE_MSSQL if is_mssql is None else bool(is_mssql)
        self._engine = "sqlite"
        if self.is_mssql:
            last_err = None
            if HAS_PYODBC:
                for attempt_app in ([True, False] if use_app_login else [False]):
                    try:
                        cs = _build_pyodbc_conn_str(use_app_login=attempt_app)
                        self._conn = pyodbc.connect(
                            cs,
                            timeout=MSSQL_CONFIG["login_timeout"],
                            autocommit=autocommit,
                        )
                        self._conn.timeout = MSSQL_CONFIG["query_timeout"]
                        self._engine = "pyodbc"
                        return
                    except Exception as e:
                        last_err = e
            if HAS_PYMSSQL:
                uid, pwd = _load_app_sql_creds() if use_app_login else (None, None)
                try:
                    kwargs = dict(
                        server=MSSQL_CONFIG["server"],
                        port=MSSQL_CONFIG["port"],
                        database=MSSQL_CONFIG["database"],
                        timeout=MSSQL_CONFIG["query_timeout"],
                        login_timeout=MSSQL_CONFIG["login_timeout"],
                        autocommit=autocommit,
                    )
                    if uid and pwd:
                        kwargs["user"] = uid
                        kwargs["password"] = pwd
                    self._conn = pymssql.connect(**kwargs)
                    self._engine = "pymssql"
                    return
                except Exception as e:
                    if uid and pwd:
                        try:
                            kwargs.pop("user", None)
                            kwargs.pop("password", None)
                            self._conn = pymssql.connect(**kwargs)
                            self._engine = "pymssql"
                            return
                        except Exception as e2:
                            last_err = e2
                    else:
                        last_err = e
            raise RuntimeError(f"MSSQL DOSKO_ERP 연결 실패: {last_err}")
        else:
            path = db_path or get_primary_sqlite_db()
            self._conn = sqlite3.connect(path, timeout=15.0)
            self._engine = "sqlite"

    def cursor(self):
        return UnifiedCursor(self._conn.cursor(), self._engine)

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                try:
                    self.rollback()
                except Exception:
                    pass
            else:
                self.commit()
        finally:
            self.close()


def get_db_connection(use_app_login=True, autocommit=False):
    """활성 DB 커넥션 반환 (일시 끊김 시 지수 백오프 재시도)"""
    return with_db_retry(lambda: UnifiedConnection(is_mssql=USE_MSSQL, use_app_login=use_app_login, autocommit=autocommit))


def get_admin_db_connection(autocommit=False):
    """DDL/관리 작업용 Windows 통합인증 커넥션"""
    return UnifiedConnection(is_mssql=True, use_app_login=False, autocommit=autocommit)


def fetch_table_dicts(cur, query, params=()):
    cur.execute(query, params if params else None)
    if not cur.description:
        return []
    cols = [d[0] for d in cur.description]
    return [{c: json_safe(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def _consume_current_resultset_dicts(cur):
    if not cur.description:
        return []
    cols = [d[0] for d in cur.description]
    return [{c: json_safe(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def fetch_ecount_overview_mssql(include_prices=True):
    """
    단일 T-SQL 배치(다중 ResultSet)로 21개 ERP/공정/외주 뷰 일괄 조회 (~14.5ms).
    include_prices=False 시 단가/매출 관련 컬럼 마스킹 적용은 호출부/RBAC에서도 이중 보장.
    """
    batch_sql = """
    SELECT wh_code, ecount_code, wh_name, wh_type, process_stage, serial_prefix, default_unit_price_kg, description FROM dbo.ecount_warehouses ORDER BY wh_code;
    SELECT project_code, ecount_proj_code, project_name, line_id, cems_meter_no, cems_meter_name, serial_start_digit, default_out_wh, default_out_wh_code, default_in_wh, default_in_wh_code FROM dbo.ecount_projects ORDER BY project_code;
    SELECT mgmt_code, mgmt_name, io_direction, target_process, description FROM dbo.ecount_mgmt_items ORDER BY mgmt_code;
    SELECT grade_name, raw_code_prefix, raw_group_code, heat_group_code, grind_group_code, hardness_unit_default, description FROM dbo.ecount_steel_grades ORDER BY grade_name;
    SELECT color_code, color_name, process_stage, description FROM dbo.ecount_colors ORDER BY color_code;
    SELECT TOP (100) inbound_id, doc_date_no, inbound_type, project_name, mgmt_item, partner_or_out_wh, in_wh, item_code, steel_grade, thickness, width, cut_length, color_name, hardness_display, spec_full, lot_no, unit_weight_kg, qty_ea, total_weight_kg, remain_weight_kg, worker_name, remarks, created_at FROM dbo.proc_1_raw_inbounds ORDER BY inbound_id DESC;
    SELECT TOP (120) heat_id, doc_date_no, work_date, project_name, worker_name, out_wh, consume_mgmt, raw_item_code, raw_steel_grade, raw_thickness, raw_width, raw_cut_length, raw_spec_full, raw_lot_no, input_unit_kg, input_qty_ea, input_total_kg, wip_return_kg, wip_lot_no, next_route, in_wh, prod_mgmt, prod_item_code, prod_steel_grade, prod_thickness, prod_width, prod_cut_length, prod_color, prod_hardness, prod_spec_full, prod_lot_no, unit_weight_kg, qty_ea, total_weight_kg, scrap_kg, power_kwh, remarks, created_at FROM dbo.proc_2_heat_treatment ORDER BY heat_id DESC;
    SELECT TOP (100) wait_id, wh_code, source_heat_id, source_project, prod_date, heat_lot_no, item_code, steel_grade, thickness, width, cut_length, color_name, hardness_display, spec_full, unit_weight_kg, init_qty_ea, remain_qty_ea, init_weight_kg, used_weight_kg, remain_weight_kg, status, remarks, created_at FROM dbo.proc_3_grinding_wait_stock ORDER BY wait_id DESC;
    SELECT TOP (100) grind_id, doc_date_no, work_date, project_name, worker_name, source_wait_id, out_wh, consume_mgmt, consume_item_code, consume_steel_grade, consume_spec_full, consume_heat_lot_no, consume_unit_kg, consume_qty_ea, consume_total_kg, in_wh, prod_mgmt, prod_item_code, prod_steel_grade, prod_thickness, prod_width, prod_cut_length, prod_color, prod_hardness, prod_spec_full, prod_lot_no, unit_weight_kg, qty_ea, total_weight_kg, scrap_kg, cems_power_kwh, remarks, created_at FROM dbo.proc_4_grinding_production ORDER BY grind_id DESC;
    SELECT TOP (150) pack_id, doc_date_no, process_stage, project_name, mgmt_item, out_wh, in_wh, source_type, source_lot_no, pack_lot_no, item_code, steel_grade, thickness, width, cut_length, color_name, hardness_display, spec_full, unit_weight_kg, qty_ea, total_weight_kg, remain_weight_kg, customer_or_dest, packing_spec, worker_name, remarks, created_at FROM dbo.proc_5_finished_and_packing ORDER BY pack_id DESC;
    SELECT TOP (250) slip_id, doc_date_no, writer_name, project_name, mgmt_item, manager_name, consume_item_name, consume_spec, consume_serial_lot, consume_item_code, prod_item_code, prod_item_name, prod_spec, color_name, prod_serial_lot, unit_weight_kg, qty_ea, input_kg, prod_kg, consume_kg, out_wh_name, in_wh_name, scrap_kg, remarks, created_at FROM dbo.ecount_erp_slips ORDER BY slip_id DESC;
    SELECT history_type, record_key, last_suffix, last_serial_full, updated_at FROM dbo.ecount_serial_issue_history ORDER BY updated_at DESC;
    SELECT TOP (400) detail_id, report_date, section_type, seq_no, orig_gubun, material_grade, spec_normalized, division_or_color, heat_qty_kg, cool_qty_kg, grind_qty_kg, raw_qty_kg, wage_or_fin_qty_kg, raw_ret_qty_kg, wage_or_fin_ret_kg, partner_name, remarks, created_at FROM dbo.ecount_daily_report_details ORDER BY report_date DESC, seq_no ASC;
    SELECT TOP (60) report_date, sheet_name, day_prod_heat_cool_kg, mon_prod_heat_cool_kg, day_prod_grind_kg, mon_prod_grind_kg, day_in_raw_net_kg, mon_in_raw_net_kg, day_in_wage_net_kg, mon_in_wage_net_kg, day_out_fin_net_kg, mon_out_fin_net_kg, day_out_raw_net_kg, mon_out_raw_net_kg, approval_status, updated_at FROM dbo.ecount_daily_report_summary ORDER BY report_date DESC;
    SELECT TOP (150) delivery_id, slip_date, slip_serial_no, recipient_name, out_wh_name, sales_gubun, steel_grade, spec_display, category_name, lot_no, unit_weight_kg, qty_ea, total_weight_kg, unit_price, supply_amt, vat_amt, page_no, created_at FROM dbo.ecount_delivery_slips ORDER BY delivery_id DESC;
    SELECT TOP (150) packing_id, packing_date, serial_num, recipient_name, out_wh_name, layout_id, spec_clean, steel_grade, category_name, coil_count_ea, coil_weights_json, subtotal_weight_kg, doc_total_qty_ea, doc_total_weight_kg, remarks, created_at FROM dbo.ecount_packing_lists ORDER BY packing_id DESC;
    SELECT addr_id, saved_date, partner_name, receiver_name, phone_no, zip_code, road_address, detail_address, wh_filter FROM dbo.ecount_address_book ORDER BY addr_id DESC;
    SELECT partner_code, wh_code, wh_name, serial_prefix, process_types, slitting_price_kg, shearing_price_kg, edgemill_price_kg, loss_item_code, loss_item_name, manager_name, phone, is_active, remarks FROM dbo.proc_outsourcing_partners ORDER BY partner_code;
    SELECT order_id, order_no, order_date, partner_code, partner_wh_name, process_type, steel_grade, raw_thickness, raw_width, raw_spec_full, raw_item_code, raw_lot_no, raw_weight_kg, remain_weight_kg, target_slit_plan, unit_price_kg, target_in_wh_code, target_in_wh_name, status, remarks, created_at FROM dbo.proc_outsourcing_orders ORDER BY order_id DESC;
    SELECT os_inbound_id, doc_date_no, work_date, project_code, project_name, mgmt_item, partner_code, out_wh_code, out_wh_name, in_wh_code, in_wh_name, process_type, raw_item_code, raw_steel_grade, raw_thickness, raw_width, raw_spec_full, raw_lot_no, input_weight_kg, slit_items_json, prod_summary_spec, prod_first_lot_no, prod_total_ea, prod_total_kg, wip_return_kg, wip_lot_no, loss_item_code, loss_item_name, loss_weight_kg, unit_price_kg, settlement_basis, billable_weight_kg, supply_amt, vat_amt, total_amt, settle_status, submitted_by, remarks, created_at FROM dbo.proc_outsourcing_inbounds ORDER BY os_inbound_id DESC;
    SELECT wh_name, lot_count, stock_kg FROM dbo.v_ecount_warehouse_stock_summary;
    """
    keys = [
        "warehouses", "projects", "mgmt_items", "steel_grades", "colors",
        "raw_inbounds", "heat_treatment", "grinding_wait_stock", "grinding_production",
        "finished_and_packing", "erp_slips", "serial_history", "daily_report_details",
        "daily_report_summary", "delivery_slips", "packing_lists", "address_book",
        "outsourcing_partners", "outsourcing_orders", "outsourcing_inbounds",
        "warehouse_stock_summary",
    ]

    def _run():
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(batch_sql)
            sets = [_consume_current_resultset_dicts(cur)]
            while cur.nextset():
                sets.append(_consume_current_resultset_dicts(cur))
            return sets

    sets = with_db_retry(_run)
    res = {k: (sets[i] if i < len(sets) else []) for i, k in enumerate(keys)}
    general_map, wip_map = {}, {}
    for sh in res.get("serial_history", []):
        if sh["history_type"] == "generalSerialIssueHistory":
            general_map[sh["record_key"]] = {"lastSerial": sh["last_suffix"], "updatedAt": sh["updated_at"]}
        elif sh["history_type"] == "wipSerialIssueHistory":
            wip_map[sh["record_key"]] = {"lastSuffix": sh["last_suffix"], "updatedAt": sh["updated_at"]}

    return {
        "status": "ok",
        "db_path": f"MSSQL://{MSSQL_CONFIG['server']}:{MSSQL_CONFIG['port']}/{MSSQL_CONFIG['database']}",
        **res,
        "serial_extension_json": {
            "version": 1,
            "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "generalSerialIssueHistory": general_map,
            "wipSerialIssueHistory": wip_map,
        },
    }


def upsert_serial_history_mssql(cur, history_type, record_key, last_suffix, last_serial_full, updated_at_ms):
    cur.execute(
        """
        MERGE dbo.ecount_serial_issue_history WITH (HOLDLOCK) AS tgt
        USING (SELECT ? AS history_type, ? AS record_key) AS src
           ON tgt.history_type = src.history_type AND tgt.record_key = src.record_key
        WHEN MATCHED THEN
            UPDATE SET last_suffix = ?, last_serial_full = ?, updated_at = ?
        WHEN NOT MATCHED THEN
            INSERT (history_type, record_key, last_suffix, last_serial_full, updated_at)
            VALUES (?, ?, ?, ?, ?);
        """,
        (
            history_type, record_key,
            last_suffix, last_serial_full, updated_at_ms,
            history_type, record_key, last_suffix, last_serial_full, updated_at_ms,
        ),
    )


def upsert_dom_schema_mssql(prg_id, page_title, url_hash, schema_json):
    now_dt = datetime.datetime.now().replace(microsecond=0)
    def _run():
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                MERGE dbo.ecount_dom_schemas WITH (HOLDLOCK) AS tgt
                USING (SELECT ? AS prg_id) AS src ON tgt.prg_id = src.prg_id
                WHEN MATCHED THEN
                    UPDATE SET page_title = ?, url_hash = ?, captured_at = ?, schema_json = ?
                WHEN NOT MATCHED THEN
                    INSERT (prg_id, page_title, url_hash, captured_at, schema_json)
                    VALUES (?, ?, ?, ?, ?);
                """,
                (prg_id, page_title, url_hash, now_dt, schema_json, prg_id, page_title, url_hash, now_dt, schema_json),
            )
    with_db_retry(_run)
    return now_dt.strftime("%Y-%m-%d %H:%M:%S")


def upsert_qc_draft_mssql(cur, row_tuple):
    (
        updated_at, work_date, lid, lot_no, steel_grade, target_spec,
        thick_m, width_m, hard_m, appearance, flatness,
        temp_summary_json, speed_summary, plc_timeline_json, event_timeline_json,
        judgment, inspector_emp_no, inspector_name, remarks, ecount_status
    ) = row_tuple
    cur.execute(
        """
        MERGE dbo.qc_inspection_drafts WITH (HOLDLOCK) AS tgt
        USING (SELECT CAST(? AS DATE) AS work_date, ? AS line_id, ? AS lot_no) AS src
           ON tgt.work_date = src.work_date AND tgt.line_id = src.line_id AND tgt.lot_no = src.lot_no
        WHEN MATCHED THEN
            UPDATE SET
                updated_at = ?,
                steel_grade = COALESCE(NULLIF(?, N''), tgt.steel_grade),
                target_spec = COALESCE(NULLIF(?, N''), tgt.target_spec),
                thickness_meas = COALESCE(NULLIF(?, N''), tgt.thickness_meas),
                width_meas = COALESCE(NULLIF(?, N''), tgt.width_meas),
                hardness_meas = COALESCE(NULLIF(?, N''), tgt.hardness_meas),
                appearance = COALESCE(NULLIF(?, N''), tgt.appearance),
                flatness = COALESCE(NULLIF(?, N''), tgt.flatness),
                temp_summary_json = ?,
                speed_summary = ?,
                plc_5min_timeline_json = ?,
                event_timeline_json = ?,
                judgment = COALESCE(NULLIF(?, N''), tgt.judgment),
                inspector_emp_no = COALESCE(NULLIF(?, N''), tgt.inspector_emp_no),
                inspector_name = COALESCE(NULLIF(?, N''), tgt.inspector_name),
                remarks = CASE WHEN ? <> N'' THEN ? ELSE tgt.remarks END,
                ecount_status = ?
        WHEN NOT MATCHED THEN
            INSERT (
                updated_at, work_date, line_id, lot_no, steel_grade, target_spec,
                thickness_meas, width_meas, hardness_meas, appearance, flatness,
                temp_summary_json, speed_summary, plc_5min_timeline_json, event_timeline_json,
                judgment, inspector_emp_no, inspector_name, remarks, ecount_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
        """,
        (
            work_date, lid, lot_no,
            updated_at, steel_grade, target_spec, thick_m, width_m, hard_m,
            appearance, flatness, temp_summary_json, speed_summary,
            plc_timeline_json, event_timeline_json, judgment,
            inspector_emp_no or "", inspector_name or "",
            remarks or "", remarks or "", ecount_status,
            updated_at, work_date, lid, lot_no, steel_grade, target_spec,
            thick_m, width_m, hard_m, appearance, flatness,
            temp_summary_json, speed_summary, plc_timeline_json, event_timeline_json,
            judgment, inspector_emp_no, inspector_name, remarks or "", ecount_status,
        ),
    )


if __name__ == "__main__":
    print("dosko_db.py v2.0 check:", _pick_odbc_driver(), "USE_MSSQL=", USE_MSSQL)
    ov = fetch_ecount_overview_mssql()
    print("Overview status:", ov["status"], "items=", len(ov.get("warehouses", [])), "projects=", len(ov.get("projects", [])))
