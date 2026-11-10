# -*- coding: utf-8 -*-
"""
(주)도스코 통합 인증 · 사번 로그인 · 역할(RBAC) · 호기/마감일 통제 · 감사로그 모듈 (dosko_auth.py)
------------------------------------------------------------------------------
- 사번(emp_no) 기반 로그인 + 영문/숫자/특수문자 8자 이상(사번 포함 금지) 정책
- PBKDF2-HMAC-SHA256 해시 + 5회 실패 시 15분 계정 잠금 + 최초 로그인 비번 변경 강제
- 9대 역할군 + 개인별 모듈/담당호기 오버라이드(RESTRICTED -> 전산실 설정 후 활성)
- 생산자(PROD_OP) 당일 영업일(16:00 컷오프) 한정 수정 + 서버측 단가/금액 자동 마스킹
"""

import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
from urllib.parse import unquote

from dosko_db import calc_business_date, fetch_table_dicts, get_db_connection, json_safe, with_db_retry

ALL_FACTORY_LINES = ["L1", "L2", "L3", "L5", "L6", "L7", "L8", "L9", "PACK"]
PLC_LINES = ["L1", "L2", "L3", "L5", "L6", "L7"]
GRIND_LINES = ["L8", "L9"]

PRICE_FIELD_KEYS = {
    "unit_price", "unit_price_kg", "supply_amt", "vat_amt", "total_amt",
    "slitting_price_kg", "shearing_price_kg", "edgemill_price_kg",
    "default_unit_price_kg", "billable_weight_kg",
}

ROLE_LABELS = {
    "IT_ADMIN": "전산실 총 관리자",
    "EXEC_READ": "임원 (전체 조회전용)",
    "BIZ_EXEC": "생산·영업 총책임자",
    "SALES_MGR": "영업부 관리자",
    "PROD_GLOBAL": "생산관리 전역관리",
    "QC_PROD_MGR": "품질+생산 전역관리",
    "PROD_OP": "현장 생산자 (담당호기)",
    "RESTRICTED": "전산실 설정 대기",
    "LOCKED": "일시정지 차단",
}

MODULE_KEYS = [
    ("prod_easy", "생산입력(이지모드)"),
    ("prod_log", "생산일지(열처리·수냉)"),
    ("plc_monitor", "실시간 PLC 모니터링"),
    ("qc_inspect", "품질검사·중간검사"),
    ("grinding", "수냉연마·연마(L8/L9)"),
    ("outsourcing", "외주가공·원소재"),
    ("sales_ship", "영업·출하·거래명세서"),
    ("power_report", "전력피크·전기사용량보고서"),
    ("ecount_rpa", "ECOUNT 전표·RPA 연계"),
]

# 기본 역할별 모듈 권한 ('NONE' | 'R' | 'RW')
DEFAULT_ROLE_MODULE_PERMS = {
    "IT_ADMIN":    {k: "RW" for k, _ in MODULE_KEYS},
    "EXEC_READ":   {k: "R"  for k, _ in MODULE_KEYS},
    "BIZ_EXEC":    {k: "RW" for k, _ in MODULE_KEYS},
    "SALES_MGR":   {k: "RW" for k, _ in MODULE_KEYS},
    "PROD_GLOBAL": {"prod_easy": "RW", "prod_log": "RW", "plc_monitor": "RW", "qc_inspect": "R",  "grinding": "RW", "outsourcing": "R", "sales_ship": "R", "power_report": "R", "ecount_rpa": "RW"},
    "QC_PROD_MGR": {"prod_easy": "RW", "prod_log": "RW", "plc_monitor": "RW", "qc_inspect": "RW", "grinding": "RW", "outsourcing": "R", "sales_ship": "R", "power_report": "R", "ecount_rpa": "RW"},
    "PROD_OP":     {"prod_easy": "RW", "prod_log": "R",  "plc_monitor": "R",  "qc_inspect": "NONE", "grinding": "RW", "outsourcing": "NONE", "sales_ship": "NONE", "power_report": "NONE", "ecount_rpa": "NONE"},
    "RESTRICTED":  {k: "NONE" for k, _ in MODULE_KEYS},
    "LOCKED":      {k: "NONE" for k, _ in MODULE_KEYS},
}

DEFAULT_ROLE_TABS = {
    "IT_ADMIN":    ["tab-zones", "tab-easy-mode", "tab-daily-log", "tab-grinding", "tab-outsourcing", "tab-overview", "tab-table", "tab-report", "tab-production", "tab-erp-slips", "tab-erp-delivery", "tab-erp-packing", "tab-erp-daily-xlsx", "admin-users"],
    "EXEC_READ":   ["tab-zones", "tab-easy-mode", "tab-daily-log", "tab-grinding", "tab-outsourcing", "tab-overview", "tab-table", "tab-report", "tab-production", "tab-erp-slips", "tab-erp-delivery", "tab-erp-packing", "tab-erp-daily-xlsx"],
    "BIZ_EXEC":    ["tab-zones", "tab-easy-mode", "tab-daily-log", "tab-grinding", "tab-outsourcing", "tab-overview", "tab-table", "tab-report", "tab-production", "tab-erp-slips", "tab-erp-delivery", "tab-erp-packing", "tab-erp-daily-xlsx"],
    "SALES_MGR":   ["tab-zones", "tab-easy-mode", "tab-daily-log", "tab-grinding", "tab-outsourcing", "tab-overview", "tab-table", "tab-report", "tab-production", "tab-erp-slips", "tab-erp-delivery", "tab-erp-packing", "tab-erp-daily-xlsx"],
    "PROD_GLOBAL": ["tab-zones", "tab-easy-mode", "tab-daily-log", "tab-grinding", "tab-outsourcing", "tab-overview", "tab-table", "tab-report", "tab-production", "tab-erp-slips", "tab-erp-daily-xlsx"],
    "QC_PROD_MGR": ["tab-zones", "tab-easy-mode", "tab-daily-log", "tab-grinding", "tab-outsourcing", "tab-overview", "tab-table", "tab-report", "tab-production", "tab-erp-slips", "tab-erp-daily-xlsx"],
    "PROD_OP":     ["tab-zones", "tab-daily-log"],  # 이지모드 내 허용 탭(연마는 L8/L9 보유 시 자동추가)
    "RESTRICTED":  [],
    "LOCKED":      [],
}



def calc_initial_password(emp_no, birth_date=""):
    """초기 비밀번호 계산: 사번 + 생년월일 6자리 (예: 1204 + 750312 -> 1204750312)"""
    eno = str(emp_no or '').strip()
    b_clean = re.sub(r'[^0-9]', '', str(birth_date or ''))
    if len(b_clean) == 8:
        b6 = b_clean[2:]
    elif len(b_clean) == 6:
        b6 = b_clean
    else:
        b6 = "000000"
    return f"{eno}{b6}"

def validate_password_strength(password, emp_no="", gw_login_id="", pw_rule_type="OFFICE_COMPLEX"):
    pw = str(password or "").strip()
    if re.search(r"\s", pw):
        return False, "비밀번호에 공백 문자를 포함할 수 없습니다."
    eno = str(emp_no or "").strip().lower()
    if eno and len(eno) >= 2 and eno in pw.lower():
        return False, "비밀번호에 본인 사번을 포함할 수 없습니다."

    # 1. 현장 작업자 및 일반 사용자 6자리 숫자 비밀번호
    if pw_rule_type in ("WORKER_4DIGIT", "WORKER_6DIGIT", "PROD_MGR_8DIGIT"):
        if not re.match(r"^\d{6}$", pw):
            return False, "비밀번호는 6자리 숫자여야 합니다. (예: 582914)"
        if pw in ("000000", "111111", "222222", "333333", "444444", "555555", "666666", "777777", "888888", "999999", "123456", "654321"):
            return False, "연속되거나 단순 반복되는 6자리 숫자는 사용할 수 없습니다."
        return True, "OK"

    # 2. 현장 관리책임자 (팀장/품질): 4~8자리 숫자
    elif pw_rule_type == "PROD_MGR_8DIGIT":
        if not re.match(r"^\d{4,8}$", pw):
            return False, "현장 관리책임자 비밀번호는 4~8자리 숫자여야 합니다."
        if pw in ("0000", "1111", "2222", "3333", "12345678", "87654321"):
            return False, "단순 반복되거나 연속되는 숫자는 사용할 수 없습니다."
        return True, "OK"

    # 3. 사무직 / 임원: 8자리 이상 (영문, 숫자, 특수문자 중 2가지 이상 조합)
    else:
        if len(pw) < 8:
            return False, "사무직 비밀번호는 최소 8자 이상이어야 합니다."
        if len(pw) > 72:
            return False, "비밀번호는 72자 이하로 입력하세요."
        types_count = 0
        if re.search(r"[A-Za-z]", pw):
            types_count += 1
        if re.search(r"[0-9]", pw):
            types_count += 1
        if re.search(r"[^A-Za-z0-9\s]", pw):
            types_count += 1
        if types_count < 2:
            return False, "사무직 비밀번호는 영문, 숫자, 특수문자 중 2가지 이상을 조합해야 합니다."
        gwid = str(gw_login_id or "").strip().lower()
        if gwid and len(gwid) >= 3 and gwid in pw.lower():
            return False, "비밀번호에 그룹웨어 ID를 포함할 수 없습니다."
        return True, "OK"


def hash_password(password, iterations=310000):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password, stored_hash):
    if not stored_hash or not password:
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, it_str, salt, digest_hex = stored_hash.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt.encode("ascii"), int(it_str))
            return hmac.compare_digest(dk.hex(), digest_hex)
        except Exception:
            return False
    return hmac.compare_digest(str(password), str(stored_hash))


def sha256_hex(raw_token):
    return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()


def parse_json_field(val, default):
    if val is None or val == "":
        return default
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return default


def check_ip_allowed(client_ip, cidr_spec):
    spec = (cidr_spec or "").strip()
    if not spec or spec.upper() == "ALL":
        return True
    try:
        ip_obj = ipaddress.ip_address(str(client_ip).split("%")[0])
        if ip_obj.is_loopback:
            return True
        for token in re.split(r"[,;\s]+", spec):
            if not token:
                continue
            if "/" in token:
                if ip_obj in ipaddress.ip_network(token, strict=False):
                    return True
            else:
                if str(ip_obj) == token:
                    return True
        return False
    except Exception:
        return True


def mask_sensitive_prices(data):
    """단가/공급가/VAT/임가공단가 필드를 재귀적으로 마스킹(None 치환)"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in PRICE_FIELD_KEYS:
                out[k] = None
            else:
                out[k] = mask_sensitive_prices(v)
        return out
    if isinstance(data, list):
        return [mask_sensitive_prices(x) for x in data]
    return data


def get_effective_module_perms(user_row):
    role_code = user_row.get("role_code") or "RESTRICTED"
    base = dict(DEFAULT_ROLE_MODULE_PERMS.get(role_code, DEFAULT_ROLE_MODULE_PERMS["RESTRICTED"]))
    overrides = parse_json_field(user_row.get("module_perms_json"), {})
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if v in ("NONE", "R", "RW"):
                base[k] = v
    # EXEC_READ는 어떤 경우에도 W 불가 (관리 변경 보호)
    if role_code == "EXEC_READ":
        for k, v in list(base.items()):
            if v == "RW":
                base[k] = "R"
    # L8/L9 없는 PROD_OP는 grinding NONE
    lines = get_effective_lines(user_row)
    if role_code == "PROD_OP" and not any(l in ("L8", "L9") for l in lines):
        base["grinding"] = "NONE"
    return base


def get_effective_lines(user_row):
    role_code = user_row.get("role_code") or "RESTRICTED"
    raw = parse_json_field(user_row.get("allowed_lines_json"), None)
    if raw == ["ALL"] or raw == "ALL":
        return list(ALL_FACTORY_LINES)
    if isinstance(raw, list):
        return [str(x).upper() for x in raw]
    if role_code in ("IT_ADMIN", "EXEC_READ", "BIZ_EXEC", "SALES_MGR", "PROD_GLOBAL", "QC_PROD_MGR"):
        return list(ALL_FACTORY_LINES)
    return []


def get_effective_tabs(user_row):
    role_code = user_row.get("role_code") or "RESTRICTED"
    explicit = parse_json_field(user_row.get("allowed_tabs_json"), None)
    if isinstance(explicit, list) and role_code == "RESTRICTED" and explicit:
        return explicit
    tabs = list(DEFAULT_ROLE_TABS.get(role_code, []))
    if role_code == "PROD_OP":
        lines = get_effective_lines(user_row)
        if any(l in ("L8", "L9") for l in lines) and "tab-grinding" not in tabs:
            tabs.append("tab-grinding")
        if "PACK" in lines and "tab-pack" not in tabs:
            tabs.append("tab-pack")
    # IT_ADMIN 외 어떤 역할도 admin-users 탭 불가
    if role_code != "IT_ADMIN" and "admin-users" in tabs:
        tabs.remove("admin-users")
    return tabs


def build_user_profile(row):
    """세션/프론트 전달용 안전 사용자 프로필 생성 (password_hash 절대 미포함)"""
    lines = get_effective_lines(row)
    mods = get_effective_module_perms(row)
    tabs = get_effective_tabs(row)
    # RESTRICTED 사용자가 전산실 설정으로 최소 1개 모듈 이상 R/RW를 받으면 커스텀 활성
    dept_disp = " > ".join(x for x in [row.get("org_level4"), row.get("org_level5")] if x) or (row.get("org_level3") or "임원")
    return {
        "user_id": int(row["user_id"]),
        "emp_no": row["emp_no"],
        "gw_login_id": row.get("gw_login_id") or "",
        "display_name": row["display_name"],
        "dept_path": dept_disp,
        "org_level4": row.get("org_level4") or "",
        "org_level5": row.get("org_level5") or "",
        "job_title": row.get("job_title") or "",
        "job_role_type": row.get("job_role_type") or "",
        "employment_status": row.get("employment_status") or "정상",
        "role_code": row["role_code"],
        "role_label": ROLE_LABELS.get(row["role_code"], row["role_code"]),
        "is_pending_it_setup": bool(row.get("is_pending_it_setup")),
        "can_login": bool(row.get("can_login")),
        "is_active": bool(row.get("is_active")),
        "must_change_pw": bool(row.get("must_change_pw")),
        "can_view_price": bool(row.get("can_view_price")),
        "can_edit_confirmed": bool(row.get("can_edit_confirmed")),
        "can_export_excel": bool(row.get("can_export_excel")),
        "can_use_ecount_rpa": bool(row.get("can_use_ecount_rpa")),
        "allowed_lines": lines,
        "allowed_tabs": tabs,
        "module_perms": mods,
        "session_ttl_min": int(row.get("session_ttl_min") or 480),
        "default_home": "/easy" if row["role_code"] == "PROD_OP" else "/",
    }


def log_audit(emp_no=None, display_name=None, role_code=None, user_id=None, client_ip=None,
              action_type="INFO", resource_path=None, target_key=None,
              before_obj=None, after_obj=None, detail=None):
    def _run():
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO dbo.sec_audit_logs (
                    user_id, emp_no, display_name, role_code, client_ip,
                    action_type, resource_path, target_key, before_json, after_json, detail_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?);
                """,
                (
                    user_id,
                    emp_no,
                    display_name,
                    role_code,
                    client_ip,
                    action_type,
                    resource_path,
                    target_key,
                    json.dumps(before_obj, ensure_ascii=False, default=str) if before_obj is not None else None,
                    json.dumps(after_obj, ensure_ascii=False, default=str) if after_obj is not None else None,
                    json.dumps(detail, ensure_ascii=False, default=str) if isinstance(detail, (dict, list)) else (str(detail) if detail else None),
                ),
            )
    try:
        with_db_retry(_run)
    except Exception:
        pass


def authenticate_user_login(emp_no, password, client_ip, user_agent=""):
    eno = str(emp_no or "").strip()
    pw = str(password or "")
    if not eno or not pw:
        return False, 400, "사번과 비밀번호를 입력하세요.", None, None

    def _query_user():
        with get_db_connection() as conn:
            rows = fetch_table_dicts(conn.cursor(), "SELECT * FROM dbo.sec_users WHERE emp_no = ?", (eno,))
            return rows[0] if rows else None

    row = with_db_retry(_query_user)
    if not row:
        log_audit(emp_no=eno, client_ip=client_ip, action_type="LOGIN_FAIL", resource_path="/api/auth/login", detail={"reason": "UNKNOWN_EMP_NO"})
        return False, 401, "사번 또는 비밀번호가 올바르지 않습니다.", None, None

    now = datetime.datetime.now().replace(microsecond=0)
    if row["role_code"] == "LOCKED" or not row["is_active"] or row["employment_status"] == "일시정지":
        log_audit(emp_no=eno, display_name=row["display_name"], role_code=row["role_code"], user_id=row["user_id"],
                  client_ip=client_ip, action_type="LOGIN_FAIL", resource_path="/api/auth/login", detail={"reason": "PAUSED_OR_LOCKED"})
        return False, 403, "일시정지 또는 사용 중지된 계정입니다. 전산실에 문의하세요.", None, None

    if not row["can_login"] or row["role_code"] == "RESTRICTED":
        log_audit(emp_no=eno, display_name=row["display_name"], role_code=row["role_code"], user_id=row["user_id"],
                  client_ip=client_ip, action_type="LOGIN_FAIL", resource_path="/api/auth/login", detail={"reason": "PENDING_IT_SETUP"})
        return False, 403, f"전산실 권한 설정 대기 계정입니다. (사번 {eno}: 전산실 문의)", None, None

    if not check_ip_allowed(client_ip, row.get("allowed_ip_cidr")):
        log_audit(emp_no=eno, display_name=row["display_name"], role_code=row["role_code"], user_id=row["user_id"],
                  client_ip=client_ip, action_type="IP_BLOCKED", resource_path="/api/auth/login", detail={"ip": client_ip})
        return False, 403, f"허용되지 않은 IP({client_ip})에서의 접속입니다.", None, None

    # 1. 영구 차단(is_locked) 또는 잠금 상태 검사
    is_blocked = bool(row.get("is_locked")) or int(row.get("failed_attempts") or 0) >= 6
    if is_blocked:
        log_audit(emp_no=eno, display_name=row["display_name"], role_code=row["role_code"], user_id=row["user_id"],
                  client_ip=client_ip, action_type="ACCOUNT_LOCKED", resource_path="/api/auth/login", detail={"reason": "FAILED_ATTEMPTS_6"})
        msg = (
            "⛔ [계정 차단 안내] 비밀번호 6회 연속 오류로 계정이 차단되었습니다.\n"
            "• 전산실(송구현 실장)에 잠금 해제 또는 초기화를 요청하세요.\n"
            "• 긴급 생산 입력이 필요한 경우, 전체 권한이 있는 팀장(조장) 계정으로 생산내용 작성을 요청하세요."
        )
        return False, 423, msg, None, None

    if not verify_password(pw, row["password_hash"]):
        fails = int(row.get("failed_attempts") or 0) + 1
        is_now_locked = (fails >= 6)
        def _upd_fail():
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE dbo.sec_users SET failed_attempts = ?, is_locked = ?, updated_at = SYSDATETIME() WHERE user_id = ?",
                    (fails, 1 if is_now_locked else 0, row["user_id"]),
                )
        with_db_retry(_upd_fail)
        act = "ACCOUNT_LOCKED" if is_now_locked else "LOGIN_FAIL"
        log_audit(emp_no=eno, display_name=row["display_name"], role_code=row["role_code"], user_id=row["user_id"],
                  client_ip=client_ip, action_type=act, resource_path="/api/auth/login", detail={"failed_attempts": fails})

        if is_now_locked:
            msg = (
                "⛔ [계정 차단 안내] 비밀번호 6회 연속 오류로 계정이 영구 차단되었습니다.\n"
                "• 전산실(송구현 실장)에 잠금 해제 또는 초기화를 요청하세요.\n"
                "• 긴급 생산 입력이 필요한 경우, 전체 권한이 있는 팀장(조장) 계정으로 생산내용 작성을 요청하세요."
            )
            return False, 423, msg, None, None
        elif fails >= 4:
            msg = f"⚠️ [비밀번호 오류 경고] 비밀번호가 {fails}회 연속 일치하지 않습니다.\n(6회 오류 시 계정이 완전 차단되오니 주의하세요. 현재: {fails}/6회)"
            return False, 401, msg, None, None
        else:
            return False, 401, f"사번 또는 비밀번호가 올바르지 않습니다. ({fails}/6회 오류)", None, None

    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
    csrf_token = secrets.token_hex(24)
    ttl_min = max(15, int(row.get("session_ttl_min") or 480))
    exp_dt = now + datetime.timedelta(minutes=ttl_min)

    def _issue():
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE dbo.sec_users SET failed_attempts = 0, is_locked = 0, locked_until = NULL, last_login_at = ?, updated_at = SYSDATETIME() WHERE user_id = ?",
                (now, row["user_id"]),
            )
            cur.execute(
                """
                INSERT INTO dbo.sec_sessions (
                    session_token_hash, session_type, user_id, emp_no, partner_code,
                    client_ip, user_agent, csrf_token, issued_at, last_activity_at, expires_at, is_revoked
                ) VALUES (?, N'USER', ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0);
                """,
                (token_hash, row["user_id"], eno, str(client_ip)[:45], str(user_agent or "")[:200], csrf_token, now, now, exp_dt),
            )
    with_db_retry(_issue)
    log_audit(emp_no=eno, display_name=row["display_name"], role_code=row["role_code"], user_id=row["user_id"],
              client_ip=client_ip, action_type="LOGIN_OK", resource_path="/api/auth/login")
    profile = build_user_profile(row)
    return True, 200, "로그인 성공", profile, {"raw_token": raw_token, "csrf_token": csrf_token, "expires_at": exp_dt.strftime("%Y-%m-%d %H:%M:%S")}


def get_session_user(raw_token, client_ip=None, touch_activity=True):
    if not raw_token:
        return None, None
    th = sha256_hex(raw_token)
    now = datetime.datetime.now().replace(microsecond=0)

    def _load():
        with get_db_connection() as conn:
            rows = fetch_table_dicts(
                conn.cursor(),
                """
                SELECT s.session_token_hash, s.session_type, s.csrf_token, s.expires_at, s.is_revoked, s.client_ip AS sess_ip,
                       u.*
                FROM dbo.sec_sessions s
                JOIN dbo.sec_users u ON s.user_id = u.user_id
                WHERE s.session_token_hash = ? AND s.session_type = N'USER'
                """,
                (th,),
            )
            return rows[0] if rows else None

    r = with_db_retry(_load)
    if not r or r.get("is_revoked"):
        return None, None
    exp_dt = datetime.datetime.fromisoformat(str(r["expires_at"]))
    if exp_dt <= now:
        return None, None
    if r.get("role_code") == "LOCKED" or not r.get("is_active") or not r.get("can_login") or r.get("employment_status") == "일시정지":
        return None, None
    if client_ip and not check_ip_allowed(client_ip, r.get("allowed_ip_cidr")):
        return None, None

    if touch_activity:
        ttl_min = max(15, int(r.get("session_ttl_min") or 480))
        new_exp = now + datetime.timedelta(minutes=ttl_min)
        def _touch():
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE dbo.sec_sessions SET last_activity_at = ?, expires_at = ? WHERE session_token_hash = ?",
                    (now, new_exp, th),
                )
        try:
            with_db_retry(_touch)
        except Exception:
            pass

    profile = build_user_profile(r)
    return profile, {"csrf_token": r["csrf_token"], "token_hash": th}


def revoke_session(raw_token):
    if not raw_token:
        return
    th = sha256_hex(raw_token)
    def _rev():
        with get_db_connection() as conn:
            conn.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE session_token_hash = ?", (th,))
    try:
        with_db_retry(_rev)
    except Exception:
        pass


def verify_api_token(raw_key, required_route=None):
    if not raw_key:
        return None
    kh = sha256_hex(raw_key)
    now = datetime.datetime.now().replace(microsecond=0)
    def _q():
        with get_db_connection() as conn:
            rows = fetch_table_dicts(conn.cursor(), "SELECT * FROM dbo.sec_api_tokens WHERE token_hash = ? AND is_active = 1", (kh,))
            if rows:
                conn.execute("UPDATE dbo.sec_api_tokens SET last_used_at = ? WHERE token_id = ?", (now, rows[0]["token_id"]))
            return rows[0] if rows else None
    tok = with_db_retry(_q)
    if not tok:
        return None
    if tok.get("expires_at"):
        if datetime.datetime.fromisoformat(str(tok["expires_at"])) <= now:
            return None
    if required_route:
        allowed_r = parse_json_field(tok.get("allowed_routes_json"), [])
        if allowed_r and required_route not in allowed_r and "*" not in allowed_r:
            return None
    return tok


def authenticate_partner_login(partner_code, access_code, client_ip, user_agent=""):
    pc = str(partner_code or "").strip().upper()
    ac = str(access_code or "").strip()
    if not pc or not ac:
        return False, 400, "협력사 코드와 접속 코드를 입력하세요.", None, None
    def _q():
        with get_db_connection() as conn:
            rows = fetch_table_dicts(conn.cursor(), "SELECT * FROM dbo.proc_outsourcing_partners WHERE partner_code = ?", (pc,))
            return rows[0] if rows else None
    p = with_db_retry(_q)
    if not p or not p.get("is_active", 1):
        return False, 403, "비활성화되었거나 존재하지 않는 협력사입니다.", None, None
    ok = verify_password(ac, p.get("portal_access_hash") or p.get("portal_access_code"))
    if not ok:
        log_audit(emp_no=f"PARTNER:{pc}", role_code="PARTNER", client_ip=client_ip, action_type="LOGIN_FAIL", resource_path="/api/portal/login")
        return False, 401, "접속 코드가 올바르지 않습니다.", None, None
    now = datetime.datetime.now().replace(microsecond=0)
    raw_token = secrets.token_urlsafe(32)
    th = sha256_hex(raw_token)
    csrf = secrets.token_hex(24)
    exp = now + datetime.timedelta(minutes=180)
    def _ins():
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO dbo.sec_sessions (
                    session_token_hash, session_type, user_id, emp_no, partner_code,
                    client_ip, user_agent, csrf_token, issued_at, last_activity_at, expires_at, is_revoked
                ) VALUES (?, N'PARTNER', NULL, ?, ?, ?, ?, ?, ?, ?, ?, 0);
                """,
                (th, f"PARTNER:{pc}", pc, str(client_ip)[:45], str(user_agent or "")[:200], csrf, now, now, exp),
            )
    with_db_retry(_ins)
    log_audit(emp_no=f"PARTNER:{pc}", display_name=p["wh_name"], role_code="PARTNER", client_ip=client_ip, action_type="PARTNER_LOGIN", resource_path="/api/portal/login")
    return True, 200, "협력사 포털 인증 성공", {"partner_code": pc, "wh_name": p["wh_name"], "manager_name": p.get("manager_name") or ""}, {"raw_token": raw_token, "csrf_token": csrf}


def get_portal_partner(raw_token):
    if not raw_token:
        return None
    th = sha256_hex(raw_token)
    now = datetime.datetime.now().replace(microsecond=0)
    def _q():
        with get_db_connection() as conn:
            rows = fetch_table_dicts(
                conn.cursor(),
                """
                SELECT s.session_token_hash, s.csrf_token, s.expires_at, s.is_revoked, p.partner_code, p.wh_name, p.manager_name, p.is_active
                FROM dbo.sec_sessions s
                JOIN dbo.proc_outsourcing_partners p ON s.partner_code = p.partner_code
                WHERE s.session_token_hash = ? AND s.session_type = N'PARTNER'
                """,
                (th,),
            )
            return rows[0] if rows else None
    r = with_db_retry(_q)
    if not r or r.get("is_revoked") or not r.get("is_active", 1):
        return None
    if datetime.datetime.fromisoformat(str(r["expires_at"])) <= now:
        return None
    return r


def is_open_business_date(target_date_str, active_coil_check=False):
    """
    생산자(PROD_OP) 쓰기 허용 날짜 검사:
    - 현재 [D-1 16:00, D 16:00) 영업일(today_biz) 또는 진행 중 작업지시(active_coil_check=True)만 수정 가능.
    - 이미 마감된 과거 일자(D-1 이전)는 차단.
    """
    if active_coil_check:
        return True
    if not target_date_str:
        return True
    try:
        s = str(target_date_str).strip()[:10].replace("/", "-")
        tgt = datetime.date.fromisoformat(s)
        cur_biz = calc_business_date(datetime.datetime.now())
        # 당일 캘린더 날짜 또는 16:00 기준 현재 영업일까지 허용 (16:00 롤오버 과도기 안전 고려)
        cal_today = datetime.date.today()
        return tgt >= min(cur_biz, cal_today)
    except Exception:
        return False


def _infer_line_from_payload(path, data):
    if not isinstance(data, dict):
        return ""
    lid = str(data.get("lid") or data.get("line_id") or "").strip().upper()
    if lid:
        return lid
    for subk in ("payload", "spec"):
        sub = data.get(subk)
        if isinstance(sub, dict):
            lid = str(sub.get("line_id") or sub.get("lid") or "").strip().upper()
            if lid:
                return lid
    if str(data.get("action") or "").upper() == "TRANSFER_PACKING":
        return "PACK"
    proj = str(data.get("project_name") or data.get("project_code") or "")
    m = re.search(r"(\d+)\s*호기", proj)
    if m:
        return f"L{m.group(1)}"
    if "수냉연마" in proj:
        return "L8"
    if "연마" in proj:
        return "L9"
    if "포장" in proj:
        return "PACK"
    return ""


def _check_module_perm(mods, mod_key, is_write):
    lvl = mods.get(mod_key, "NONE")
    if is_write:
        return lvl == "RW"
    return lvl in ("R", "RW")


def authorize_request(user, method, path, qs=None, body_data=None):
    """
    통합 서버 API/화면 접근통제 검사기.
    반환: (allowed: bool, status_code: int, code: str, message: str)
    """
    role = user["role_code"]
    mods = user.get("module_perms") or {}
    lines = user.get("allowed_lines") or []
    is_write = method.upper() in ("POST", "PUT", "DELETE", "PATCH")
    can_edit_conf = bool(user.get("can_edit_confirmed"))

    # 0. 최초 로그인 비번 변경 강제
    if user.get("must_change_pw") and path not in (
        "/change-password", "/login", "/health",
        "/api/auth/me", "/api/auth/change-password", "/api/auth/logout",
    ):
        return False, 403, "MUST_CHANGE_PASSWORD", "최초 로그인 비밀번호를 먼저 변경해야 합니다."

    # 1. 관리메뉴(/admin*, /api/admin/*) -> 오직 IT_ADMIN 전용 (임원/김능환/영업/생산/품질 전원 차단)
    if path.startswith("/admin") or path.startswith("/api/admin"):
        if role == "IT_ADMIN":
            return True, 200, "OK", ""
        return False, 403, "ADMIN_ONLY", "전산실 관리자 전용 메뉴입니다."

    # 2. 인증/자체 서비스 공통
    if path in ("/health", "/login", "/change-password", "/api/auth/me", "/api/auth/change-password", "/api/auth/logout", "/api/users/workers"):
        return True, 200, "OK", ""

    # 3. 임원(EXEC_READ) -> 관리메뉴 외 전 업무 보기(GET) 허용, 모든 입력·수정(POST) 불가
    if role == "EXEC_READ":
        if is_write:
            return False, 403, "EXEC_READ_ONLY", "임원 계정은 전체 조회 전용이며 입력·수정이 차단되어 있습니다."
        return True, 200, "OK", ""

    # 4. IT_ADMIN -> 전체 R/W 허용
    if role == "IT_ADMIN":
        return True, 200, "OK", ""

    # 5. BIZ_EXEC(김능환), SALES_MGR(영업3), PROD_GLOBAL(이영준/김덕수), QC_PROD_MGR(이지한)
    if role in ("BIZ_EXEC", "SALES_MGR", "PROD_GLOBAL", "QC_PROD_MGR"):
        if path == "/api/ecount/warehouse-action" and is_write:
            act = str((body_data or {}).get("action") or "").upper()
            if act == "CREATE_SHIPMENT" and not _check_module_perm(mods, "sales_ship", True):
                return False, 403, "SALES_WRITE_DENIED", "출하 거래명세서 작성 권한이 없습니다."
            if not _check_module_perm(mods, "sales_ship", True) and not _check_module_perm(mods, "prod_log", True):
                return False, 403, "MODULE_DENIED", "창고 처리 쓰기 권한이 없습니다."
        elif path.startswith("/api/ecount/outsourcing") and not _check_module_perm(mods, "outsourcing", is_write):
            return False, 403, "OUTSOURCING_DENIED", "외주가공 정산 입력/수정 권한이 없습니다."
        elif path == "/api/ecount/dom-schema" and not _check_module_perm(mods, "ecount_rpa", is_write):
            return False, 403, "RPA_DENIED", "ECOUNT RPA 스키마 접근 권한이 없습니다."
        elif path == "/api/ecount/qc-summary" and is_write:
            ec_st = str((body_data or {}).get("ecount_status") or "DRAFT").upper()
            if not _check_module_perm(mods, "qc_inspect", True):
                if role == "PROD_GLOBAL" and ec_st == "DRAFT":
                    return True, 200, "OK", ""
                return False, 403, "QC_WRITE_DENIED", "품질검사 최종 판정 등록 권한이 없습니다."
        elif path == "/api/ecount/grinding-production" and is_write and not _check_module_perm(mods, "grinding", True):
            return False, 403, "GRIND_DENIED", "연마 생산 등록 권한이 없습니다."
        elif path == "/api/ecount/heat-production" and is_write and not _check_module_perm(mods, "prod_log", True):
            return False, 403, "PROD_LOG_DENIED", "생산일지 등록 권한이 없습니다."
        return True, 200, "OK", ""

    # 6. RESTRICTED (전산실 수동 설정 후 일부 모듈 열린 커스텀 계정)
    if role == "RESTRICTED":
        if path in ("/", "/index.html", "/dashboard", "/pro", "/dosko_dashboard.html", "/대시보드", "/easy", "/dosko_easy.html", "/easy.html", "/이지", "/현장", "/field", "/이지모드", "/현장용"):
            ok = any(mods.get(m, "NONE") != "NONE" for m in mods)
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "전산실 권한 설정 대기 계정입니다.")
        if path in ("/portal", "/outsourcing", "/partner", "/파트너", "/외주", "/외주포털", "/외주가공") or path.startswith("/portal") or path.startswith("/outsourcing"):
            ok = mods.get("outsourcing", "NONE") != "NONE"
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "외주가공 모듈 권한이 없습니다.")
        if path in ("/api/state", "/api/plc-history", "/api/work-events"):
            ok = mods.get("plc_monitor", "NONE") != "NONE" or mods.get("prod_easy", "NONE") != "NONE"
            if ok and path in ("/api/plc-history", "/api/work-events"):
                q_lid = ((qs or {}).get("line_id") or [""])[0].strip().upper()
                if q_lid and lines and q_lid not in lines:
                    return False, 403, "LINE_NOT_ASSIGNED", f"담당호기({q_lid}) 외 이력 조회는 차단됩니다."
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "허용되지 않은 모니터링 영역입니다.")
        if path == "/api/ecount/overview":
            ok = any(mods.get(m, "NONE") != "NONE" for m in ("prod_log", "grinding", "outsourcing", "sales_ship", "qc_inspect", "ecount_rpa"))
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "허용된 업무 메뉴가 없습니다.")
        if path == "/api/ecount/qc-summary":
            ok = _check_module_perm(mods, "qc_inspect", is_write)
            return (ok, 200 if ok else 403, "OK" if ok else "QC_DENIED", "" if ok else "품질검사 권한이 없습니다.")
        if path == "/api/ecount/grinding-production":
            ok = _check_module_perm(mods, "grinding", is_write)
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "연마 모듈 권한이 없습니다.")
        if path == "/api/ecount/heat-production":
            ok = _check_module_perm(mods, "prod_log", is_write)
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "생산일지 모듈 권한이 없습니다.")
        if path.startswith("/api/ecount/outsourcing"):
            ok = _check_module_perm(mods, "outsourcing", is_write)
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "외주가공 모듈 권한이 없습니다.")
        if path == "/api/ecount/warehouse-action":
            ok = _check_module_perm(mods, "sales_ship", is_write)
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "영업·출하 모듈 권한이 없습니다.")
        if path == "/api/ecount/dom-schema":
            ok = _check_module_perm(mods, "ecount_rpa", is_write)
            return (ok, 200 if ok else 403, "OK" if ok else "MODULE_DENIED", "" if ok else "ECOUNT RPA 권한이 없습니다.")
        if path in ("/api/coils", "/api/spec", "/api/update_spec", "/api/update", "/api/sync", "/api/worker", "/api/status", "/api/clear-spec", "/api/clear_spec", "/api/clear-all"):
            ok = _check_module_perm(mods, "prod_easy", is_write) or _check_module_perm(mods, "prod_log", is_write)
            if not ok:
                return False, 403, "MODULE_DENIED", "생산입력 권한이 없습니다."
            req_lid = _infer_line_from_payload(path, body_data)
            if lines and req_lid and req_lid not in lines:
                return False, 403, "LINE_NOT_ASSIGNED", f"담당호기({req_lid}) 권한이 없습니다."
            return True, 200, "OK", ""
        if is_write:
            return False, 403, "WRITE_DENIED", "쓰기 권한이 설정되지 않았습니다."
        return True, 200, "OK", ""

    # 7. PROD_OP (생산자 15명): 담당호기 + 당일 영업일 + 생산입력만 엄격 격리
    if role == "PROD_OP":
        portal_pages = ("/portal", "/outsourcing", "/partner", "/파트너", "/외주", "/외주포털", "/외주가공")
        if path in portal_pages or path.startswith("/portal") or path.startswith("/outsourcing"):
            return False, 403, "OP_FORBIDDEN_AREA", "생산자 계정에서는 외주 포털에 접근할 수 없습니다."

        blocked_prefixes = ("/api/ecount/outsourcing", "/api/ecount/dom-schema")
        if any(path.startswith(bp) for bp in blocked_prefixes):
            return False, 403, "OP_FORBIDDEN_AREA", "생산자 계정에서는 접근할 수 없는 관리/외주 영역입니다."

        if path == "/api/ecount/overview":
            if any(l in ("L8", "L9", "PACK") for l in lines):
                return True, 200, "OK", ""  # 핸들러/filter_overview_for_user에서 영업/원가/외주/전표 완전 제거
            return False, 403, "OP_FORBIDDEN_OVERVIEW", "생산자 계정에서는 통합 ERP 전표/단가 조회가 차단됩니다."

        if path == "/api/ecount/warehouse-action":
            act = str((body_data or {}).get("action") or "").upper()
            if "PACK" in lines and act == "TRANSFER_PACKING":
                return True, 200, "OK", ""
            return False, 403, "OP_PACK_ONLY", "포장반 담당자만 포장 이관 작업이 가능합니다."

        if path == "/api/ecount/grinding-production":
            req_lid = _infer_line_from_payload(path, body_data) or ("L8" if "수냉" in str((body_data or {}).get("project_name", "")) else "L9")
            if req_lid not in lines:
                return False, 403, "LINE_NOT_ASSIGNED", f"담당호기({req_lid}) 권한이 없습니다."
            work_d = (body_data or {}).get("work_date")
            if work_d and not can_edit_conf and not is_open_business_date(work_d):
                return False, 403, "CLOSED_BUSINESS_DATE", "마감된 과거 영업일은 생산자 계정으로 수정할 수 없습니다(조장/전산실 문의)."
            return True, 200, "OK", ""

        if path == "/api/ecount/heat-production":
            req_lid = _infer_line_from_payload(path, body_data)
            if not req_lid or req_lid not in lines:
                return False, 403, "LINE_NOT_ASSIGNED", f"담당호기({req_lid or '미지정'}) 권한이 없습니다."
            work_d = (body_data or {}).get("work_date")
            if work_d and not can_edit_conf and not is_open_business_date(work_d):
                return False, 403, "CLOSED_BUSINESS_DATE", "마감된 과거 영업일은 생산자 계정으로 수정할 수 없습니다."
            return True, 200, "OK", ""

        if path in ("/api/plc-history", "/api/work-events"):
            q_lid = ((qs or {}).get("line_id") or [""])[0].strip().upper()
            if q_lid and q_lid not in lines:
                return False, 403, "LINE_NOT_ASSIGNED", f"담당호기({q_lid}) 외 이력 조회는 차단됩니다."
            return True, 200, "OK", ""

        if path == "/api/ecount/qc-summary":
            if is_write and not _check_module_perm(mods, "qc_inspect", True):
                return False, 403, "QC_MGR_ONLY", "최종 품질검사 판정 등록은 품질관리자만 가능합니다."
            q_lid = ((qs or {}).get("line_id") or [""])[0].strip().upper()
            if q_lid and q_lid not in lines:
                return False, 403, "LINE_NOT_ASSIGNED", f"담당호기({q_lid}) 외 품질 조회는 차단됩니다."
            return True, 200, "OK", ""

        if path in ("/api/coils", "/api/spec", "/api/update_spec", "/api/update", "/api/sync", "/api/worker", "/api/status", "/api/clear-spec", "/api/clear_spec", "/api/clear-all"):
            req_lid = _infer_line_from_payload(path, body_data)
            if not req_lid or req_lid not in lines:
                return False, 403, "LINE_NOT_ASSIGNED", f"본인 담당호기({','.join(lines) or '없음'})만 입력·수정할 수 있습니다. (요청 호기: {req_lid or '미지정'})"
            payload_obj = (body_data or {}).get("payload") if isinstance((body_data or {}).get("payload"), dict) else (body_data or {})
            target_d = payload_obj.get("date") or (body_data or {}).get("date")
            action_nm = str((body_data or {}).get("action") or "").upper()
            is_live_op = action_nm in ("START_WORK_ORDER", "UPDATE_LINE_WORK_STATE", "UPDATE_WORK_ORDERS", "UPDATE_WORKER")
            if target_d and not is_live_op and not can_edit_conf and not is_open_business_date(target_d):
                return False, 403, "CLOSED_BUSINESS_DATE", f"마감된 일자({target_d}) 실적은 수정할 수 없습니다. 생산관리 책임자에게 문의하세요."
            return True, 200, "OK", ""

        if path in ("/", "/index.html", "/dashboard", "/pro", "/dosko_dashboard.html", "/대시보드"):
            return False, 403, "PRO_FORBIDDEN_FOR_OP", "생산자 계정은 현장 이지모드(/easy)만 사용할 수 있습니다."

        return True, 200, "OK", ""

    return False, 403, "UNKNOWN_ROLE", "권한이 정의되지 않은 계정입니다."


def _key_matches_lines(key_str, lines_set):
    k = str(key_str or "").upper()
    if k in lines_set:
        return True
    if "_" in k:
        suffix = k.rsplit("_", 1)[-1]
        if suffix in lines_set:
            return True
    return False


def filter_state_for_user(resp_data, user):
    """사용자 권한/담당호기 범위대로 /api/state 응답을 서버 측에서 필터링"""
    out = dict(resp_data)
    role = user.get("role_code")
    mods = user.get("module_perms") or {}
    lines_set = set(user.get("allowed_lines") or [])

    if role == "PROD_OP" or (role == "RESTRICTED" and lines_set and len(lines_set) < len(ALL_FACTORY_LINES)):
        out["specs"] = {k: v for k, v in (resp_data.get("specs") or {}).items() if _key_matches_lines(k, lines_set)}
        out["workers"] = {k: v for k, v in (resp_data.get("workers") or {}).items() if _key_matches_lines(k, lines_set)}
        out["coils"] = {k: v for k, v in (resp_data.get("coils") or {}).items() if _key_matches_lines(k, lines_set)}
        out["statuses"] = {k: v for k, v in (resp_data.get("statuses") or {}).items() if _key_matches_lines(k, lines_set)}
        out["work_orders"] = {k: v for k, v in (resp_data.get("work_orders") or {}).items() if _key_matches_lines(k, lines_set)}
        out["line_work_state"] = {k: v for k, v in (resp_data.get("line_work_state") or {}).items() if _key_matches_lines(k, lines_set)}
        out["cleared_specs"] = [x for x in (resp_data.get("cleared_specs") or []) if _key_matches_lines(x, lines_set)]
        out["cleared_workers"] = [x for x in (resp_data.get("cleared_workers") or []) if _key_matches_lines(x, lines_set)]
        ps = dict(resp_data.get("plc_stream") or {})
        if isinstance(ps.get("lines"), dict):
            ps["lines"] = {k: v for k, v in ps["lines"].items() if k.upper() in lines_set}
        ps.pop("raw_registers", None)
        out["plc_stream"] = ps

    if mods.get("power_report", "NONE") == "NONE":
        out["live_power"] = {}

    return out


def filter_overview_for_user(overview_data, user):
    """사용자 권한대로 /api/ecount/overview 민감 데이터(단가/매출/전표/외주) 제거 & 마스킹"""
    out = dict(overview_data)
    role = user.get("role_code")
    mods = user.get("module_perms") or {}
    lines_set = set(user.get("allowed_lines") or [])

    if role == "PROD_OP":
        # 생산자(L8/L9/PACK)에게는 영업·외주·일일업무·전체전표·원소재 완전 제거
        for k in (
            "delivery_slips", "packing_lists", "address_book",
            "outsourcing_partners", "outsourcing_orders", "outsourcing_inbounds",
            "daily_report_details", "daily_report_summary", "erp_slips",
            "raw_inbounds", "heat_treatment", "warehouse_stock_summary",
        ):
            out[k] = []
        if not any(l in ("L8", "L9") for l in lines_set):
            out["grinding_wait_stock"] = []
            out["grinding_production"] = []
        if "PACK" not in lines_set:
            out["finished_and_packing"] = []
    else:
        if mods.get("sales_ship", "NONE") == "NONE":
            for k in ("delivery_slips", "packing_lists", "address_book"):
                out[k] = []
        if mods.get("outsourcing", "NONE") == "NONE":
            for k in ("outsourcing_partners", "outsourcing_orders", "outsourcing_inbounds"):
                out[k] = []
        if mods.get("power_report", "NONE") == "NONE":
            out["daily_report_details"] = []
            out["daily_report_summary"] = []
        if mods.get("ecount_rpa", "NONE") == "NONE" and mods.get("prod_log", "NONE") == "NONE":
            out["erp_slips"] = []

    if not user.get("can_view_price"):
        out = mask_sensitive_prices(out)
    return out
