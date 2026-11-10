# -*- coding: utf-8 -*-
"""
(주)도스코 전산실 관리자 전용 (/api/admin/*) API 처리 모듈 (dosko_admin_api.py)
- 반드시 role_code == 'IT_ADMIN' 인 경우에만 dosko_sync_server.py 가 라우팅합니다.
- 모든 명부/CSV 가져오기에서 휴대전화·생년월일·이메일·주소 등 개인정보(PII)를 즉시 배제합니다.
"""

import csv
import datetime
import io
import json
import secrets

import dosko_auth
import dosko_db


def _generate_temp_pw(emp_no, gw_login_id=""):
    up = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lo = "abcdefghijkmnopqrstuvwxyz"
    dg = "23456789"
    sp = "!@#$%&*"
    all_ch = up + lo + dg + sp
    while True:
        chars = [secrets.choice(up), secrets.choice(lo), secrets.choice(dg), secrets.choice(sp)] + [
            secrets.choice(all_ch) for _ in range(6)
        ]
        secrets.SystemRandom().shuffle(chars)
        pw = "".join(chars)
        ok, _ = dosko_auth.validate_password_strength(pw, emp_no, gw_login_id)
        if ok:
            return pw


def _format_user_admin_row(r):
    raw_lines = dosko_auth.parse_json_field(r.get("allowed_lines_json"), [])
    mods = dosko_auth.get_effective_module_perms(r)
    return {
        "user_id": int(r["user_id"]),
        "emp_no": r["emp_no"],
        "gw_login_id": r.get("gw_login_id") or "",
        "display_name": r["display_name"],
        "org_level3": r.get("org_level3") or "",
        "org_level4": r.get("org_level4") or "",
        "org_level5": r.get("org_level5") or "",
        "job_title": r.get("job_title") or "",
        "job_role_type": r.get("job_role_type") or "",
        "work_type": r.get("work_type") or "",
        "employment_status": r.get("employment_status") or "정상",
        "role_code": r["role_code"],
        "is_pending_it_setup": bool(r.get("is_pending_it_setup")),
        "can_login": bool(r.get("can_login")),
        "is_active": bool(r.get("is_active")),
        "raw_allowed_lines": raw_lines,
        "allowed_lines": dosko_auth.get_effective_lines(r),
        "module_perms": mods,
        "can_view_price": bool(r.get("can_view_price")),
        "can_edit_confirmed": bool(r.get("can_edit_confirmed")),
        "can_export_excel": bool(r.get("can_export_excel")),
        "can_use_ecount_rpa": bool(r.get("can_use_ecount_rpa")),
        "allowed_ip_cidr": r.get("allowed_ip_cidr") or "ALL",
        "session_ttl_min": int(r.get("session_ttl_min") or 480),
        "must_change_pw": bool(r.get("must_change_pw")),
        "failed_attempts": int(r.get("failed_attempts") or 0),
        "is_locked": bool(r.get("is_locked")) or (int(r.get("failed_attempts") or 0) >= 6),
        "locked_until": r.get("locked_until"),
        "birth_date": r.get("birth_date") or "",
        "last_login_at": r.get("last_login_at"),
        "phone_no": r.get("phone_no") or "",
        "tel_no": r.get("tel_no") or "",
        "ecount_emp_code": r.get("ecount_emp_code") or "",
        "pw_rule_type": r.get("pw_rule_type") or "WORKER_4DIGIT",
        "pw_expire_months": int(r.get("pw_expire_months") or 3),
        "pw_change_type": r.get("pw_change_type") or "MOBILE_QR",
    }


def list_all_users():
    def _q():
        with dosko_db.get_db_connection() as conn:
            rows = dosko_db.fetch_table_dicts(
                conn.cursor(),
                """
                SELECT user_id, emp_no, gw_login_id, display_name, org_level3, org_level4, org_level5,
                       job_title, job_role_type, work_type, employment_status,
                       role_code, is_pending_it_setup, can_login, is_active,
                       allowed_tabs_json, allowed_lines_json, allowed_wh_json, module_perms_json,
                       can_view_price, can_edit_confirmed, can_export_excel, can_use_ecount_rpa,
                       allowed_ip_cidr, session_ttl_min, must_change_pw, failed_attempts, is_locked, locked_until, birth_date, last_login_at,
                       phone_no, tel_no, ecount_emp_code, pw_rule_type, pw_expire_months, pw_change_type
                FROM dbo.sec_users
                ORDER BY
                    CASE role_code
                        WHEN N'IT_ADMIN' THEN 1
                        WHEN N'EXEC_READ' THEN 2
                        WHEN N'BIZ_EXEC' THEN 3
                        WHEN N'SALES_MGR' THEN 4
                        WHEN N'PROD_GLOBAL' THEN 5
                        WHEN N'QC_PROD_MGR' THEN 6
                        WHEN N'PROD_OP' THEN 7
                        WHEN N'RESTRICTED' THEN 8
                        ELSE 9
                    END,
                    emp_no ASC
                """,
            )
            return [_format_user_admin_row(r) for r in rows]
    return {"status": "ok", "users": dosko_db.with_db_retry(_q)}


def create_user(admin_user, data, client_ip):
    emp_no = str(data.get("emp_no") or "").strip()
    display_name = str(data.get("display_name") or "").strip()
    if not emp_no or not display_name:
        return False, 400, {"error": "사번과 이름은 필수입니다."}
    gw_id = str(data.get("gw_login_id") or "").strip() or None
    role = str(data.get("role_code") or "RESTRICTED").strip()
    if role not in dosko_auth.ROLE_LABELS:
        return False, 400, {"error": f"유효하지 않은 역할군: {role}"}

    temp_pw = str(data.get("temp_password") or "").strip()
    if temp_pw:
        ok, msg = dosko_auth.validate_password_strength(temp_pw, emp_no, gw_id or "")
        if not ok:
            return False, 400, {"error": msg}
    else:
        temp_pw = _generate_temp_pw(emp_no, gw_id or "")

    lines = data.get("allowed_lines") or []
    if not isinstance(lines, list):
        lines = []
    lines = [str(x).upper() for x in lines if str(x).strip()]
    if role not in ("PROD_OP", "RESTRICTED", "LOCKED") and not lines:
        lines = ["ALL"]

    pw_hash = dosko_auth.hash_password(temp_pw, iterations=310000)
    mods = dict(dosko_auth.DEFAULT_ROLE_MODULE_PERMS.get(role, dosko_auth.DEFAULT_ROLE_MODULE_PERMS["RESTRICTED"]))
    if role == "PROD_OP" and not any(l in ("L8", "L9") for l in lines):
        mods["grinding"] = "NONE"

    can_login = 0 if role in ("RESTRICTED", "LOCKED") else 1
    is_active = 0 if role == "LOCKED" else 1
    is_pending = 1 if role == "RESTRICTED" else 0
    can_view_price = 1 if role in ("IT_ADMIN", "EXEC_READ", "BIZ_EXEC", "SALES_MGR") else 0
    can_edit_conf = 1 if role in ("IT_ADMIN", "BIZ_EXEC", "SALES_MGR", "PROD_GLOBAL", "QC_PROD_MGR") else 0
    can_export = 1 if role in ("IT_ADMIN", "EXEC_READ", "BIZ_EXEC", "SALES_MGR", "PROD_GLOBAL", "QC_PROD_MGR") else 0
    can_rpa = 1 if role in ("IT_ADMIN", "BIZ_EXEC", "SALES_MGR", "PROD_GLOBAL", "QC_PROD_MGR") else 0

    def _ins():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            dup = cur.execute("SELECT 1 FROM dbo.sec_users WHERE emp_no = ?", (emp_no,)).fetchone()
            if dup:
                raise ValueError(f"이미 등록된 사번입니다: {emp_no}")
            cur.execute(
                """
                INSERT INTO dbo.sec_users (
                    emp_no, gw_login_id, password_hash, display_name,
                    org_level4, org_level5, job_title, job_role_type, work_type, employment_status,
                    role_code, is_pending_it_setup, can_login, is_active,
                    allowed_tabs_json, allowed_lines_json, module_perms_json,
                    can_view_price, can_edit_confirmed, can_export_excel, can_use_ecount_rpa,
                    must_change_pw, created_by_emp_no
                ) VALUES (?,?,?,?,?,?,?,?,?,N'정상',?,?,?,?,?,?,?,?,?,?,?,1,?);
                """,
                (
                    emp_no, gw_id, pw_hash, display_name,
                    str(data.get("org_level4") or "").strip() or None,
                    str(data.get("org_level5") or "").strip() or None,
                    str(data.get("job_title") or "").strip() or None,
                    str(data.get("job_role_type") or "").strip() or None,
                    str(data.get("work_type") or "").strip() or None,
                    role, is_pending, can_login, is_active,
                    json.dumps(dosko_auth.DEFAULT_ROLE_TABS.get(role, []), ensure_ascii=False),
                    json.dumps(lines, ensure_ascii=False),
                    json.dumps(mods, ensure_ascii=False),
                    can_view_price, can_edit_conf, can_export, can_rpa,
                    admin_user["emp_no"],
                ),
            )

    try:
        dosko_db.with_db_retry(_ins)
    except ValueError as ve:
        return False, 409, {"error": str(ve)}

    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="USER_MOD",
        resource_path="/api/admin/users/create", target_key=emp_no,
        after_obj={"emp_no": emp_no, "display_name": display_name, "role_code": role, "lines": lines},
    )
    return True, 200, {"status": "ok", "emp_no": emp_no, "temp_password": temp_pw}


def update_user(admin_user, data, client_ip):
    emp_no = str(data.get("emp_no") or "").strip()
    if not emp_no:
        return False, 400, {"error": "emp_no 가 필요합니다."}
    role = str(data.get("role_code") or "").strip()
    if role not in dosko_auth.ROLE_LABELS:
        return False, 400, {"error": "유효하지 않은 역할군입니다."}

    lines = data.get("allowed_lines") if isinstance(data.get("allowed_lines"), list) else []
    lines = [str(x).upper() for x in lines if str(x).strip()]
    mods = data.get("module_perms") if isinstance(data.get("module_perms"), dict) else {}
    clean_mods = {}
    for mk, _ in dosko_auth.MODULE_KEYS:
        val = mods.get(mk, "NONE")
        clean_mods[mk] = val if val in ("NONE", "R", "RW") else "NONE"

    # 보안 불변식: 임원(EXEC_READ)은 쓰기 불가, 일시정지/LOCKED는 로그인 차단
    emp_status = str(data.get("employment_status") or "정상")
    can_login = 1 if data.get("can_login") else 0
    is_active = 1 if data.get("is_active") else 0
    is_pending = 1 if data.get("is_pending_it_setup") else 0
    can_edit_conf = 1 if data.get("can_edit_confirmed") else 0

    if emp_status == "일시정지" or role == "LOCKED":
        role = "LOCKED"
        can_login = 0
        is_active = 0
    if role == "EXEC_READ":
        can_edit_conf = 0
        for mk in clean_mods:
            if clean_mods[mk] == "RW":
                clean_mods[mk] = "R"

    # 관리자 본인(1082)의 IT_ADMIN 잠금 자가 사고 방지
    if emp_no == admin_user["emp_no"] and (role != "IT_ADMIN" or not can_login or not is_active):
        return False, 400, {"error": "현재 로그인한 전산실 관리자 본인 계정은 비활성화하거나 역할 강등을 할 수 없습니다."}

    tabs = list(dosko_auth.DEFAULT_ROLE_TABS.get(role, []))
    if role == "RESTRICTED":
        # 모듈 권한에 맞춰 허용 탭 자동 구성
        if clean_mods.get("plc_monitor") != "NONE" or clean_mods.get("prod_easy") != "NONE":
            tabs.append("tab-zones")
        if clean_mods.get("prod_log") != "NONE":
            tabs.append("tab-daily-log")
        if clean_mods.get("grinding") != "NONE":
            tabs.append("tab-grinding")
        if clean_mods.get("outsourcing") != "NONE":
            tabs.append("tab-outsourcing")
        if clean_mods.get("power_report") != "NONE":
            tabs.extend(["tab-overview", "tab-table", "tab-report", "tab-production"])
        if clean_mods.get("sales_ship") != "NONE":
            tabs.extend(["tab-erp-delivery", "tab-erp-packing"])
        if clean_mods.get("ecount_rpa") != "NONE":
            tabs.extend(["tab-erp-slips", "tab-erp-daily-xlsx"])
    elif role == "PROD_OP":
        if any(l in ("L8", "L9") for l in lines) and "tab-grinding" not in tabs:
            tabs.append("tab-grinding")
        if "PACK" in lines and "tab-pack" not in tabs:
            tabs.append("tab-pack")

    ttl_min = max(15, min(1440, int(data.get("session_ttl_min") or 480)))

    def _upd():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            before = dosko_db.fetch_table_dicts(cur, "SELECT user_id, emp_no, display_name, role_code, allowed_lines_json, can_login, is_active FROM dbo.sec_users WHERE emp_no = ?", (emp_no,))
            if not before:
                return None
            cur.execute(
                """
                UPDATE dbo.sec_users SET
                    display_name = COALESCE(NULLIF(?, N''), display_name),
                    org_level4 = ?,
                    org_level5 = ?,
                    job_title = ?,
                    work_type = ?,
                    employment_status = ?,
                    role_code = ?,
                    is_pending_it_setup = ?,
                    can_login = ?,
                    is_active = ?,
                    allowed_tabs_json = ?,
                    allowed_lines_json = ?,
                    module_perms_json = ?,
                    can_view_price = ?,
                    can_edit_confirmed = ?,
                    can_export_excel = ?,
                    can_use_ecount_rpa = ?,
                    must_change_pw = ?,
                    allowed_ip_cidr = ?,
                    session_ttl_min = ?,
                    ecount_emp_code = COALESCE(?, ecount_emp_code),
                    phone_no = COALESCE(?, phone_no),
                    pw_rule_type = COALESCE(?, pw_rule_type),
                    pw_expire_months = COALESCE(?, pw_expire_months),
                    updated_by_emp_no = ?,
                    updated_at = SYSDATETIME()
                WHERE emp_no = ?;
                """,
                (
                    str(data.get("display_name") or "").strip(),
                    str(data.get("org_level4") or "").strip() or None,
                    str(data.get("org_level5") or "").strip() or None,
                    str(data.get("job_title") or "").strip() or None,
                    str(data.get("work_type") or "").strip() or None,
                    emp_status,
                    role, is_pending, can_login, is_active,
                    json.dumps(tabs, ensure_ascii=False),
                    json.dumps(lines, ensure_ascii=False),
                    json.dumps(clean_mods, ensure_ascii=False),
                    1 if data.get("can_view_price") else 0,
                    can_edit_conf,
                    1 if data.get("can_export_excel") else 0,
                    1 if data.get("can_use_ecount_rpa") else 0,
                    1 if data.get("must_change_pw") else 0,
                    str(data.get("allowed_ip_cidr") or "ALL").strip()[:200],
                    ttl_min,
                    str(data.get("ecount_emp_code") or "").strip() or None,
                    str(data.get("phone_no") or "").strip() or None,
                    str(data.get("pw_rule_type") or "").strip() or None,
                    int(data.get("pw_expire_months")) if data.get("pw_expire_months") is not None else None,
                    admin_user["emp_no"],
                    emp_no,
                ),
            )
            if not can_login or not is_active:
                cur.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE emp_no = ? AND is_revoked = 0", (emp_no,))
            return before[0]

    before_row = dosko_db.with_db_retry(_upd)
    if before_row is None:
        return False, 404, {"error": "사번을 찾을 수 없습니다."}

    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="USER_MOD",
        resource_path="/api/admin/users/update", target_key=emp_no,
        before_obj=before_row,
        after_obj={"role_code": role, "lines": lines, "can_login": bool(can_login), "is_active": bool(is_active)},
    )
    return True, 200, {"status": "ok"}


def reset_user_password(admin_user, data, client_ip):
    emp_no = str(data.get("emp_no") or "").strip()
    if not emp_no:
        return False, 400, {"error": "emp_no 필수"}
    custom_pw = str(data.get("temp_password") or "").strip()

    def _query_and_update():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            row = cur.execute("SELECT user_id, birth_date FROM dbo.sec_users WHERE emp_no = ?", (emp_no,)).fetchone()
            if not row:
                return None, None
            uid = row[0]
            bday = row[1] or ""
            # 초기비번 계산: 사번 + 생년월일 6자리 (예: 1204 + 750312 -> 1204750312)
            if custom_pw:
                temp_pw = custom_pw
            else:
                temp_pw = dosko_auth.calc_initial_password(emp_no, bday)

            pw_hash = dosko_auth.hash_password(temp_pw, iterations=310000)
            cur.execute(
                """
                UPDATE dbo.sec_users
                SET password_hash = ?, must_change_pw = 1, failed_attempts = 0, is_locked = 0, locked_until = NULL,
                    updated_by_emp_no = ?, updated_at = SYSDATETIME()
                WHERE emp_no = ?;
                """,
                (pw_hash, admin_user["emp_no"], emp_no),
            )
            cur.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE emp_no = ? AND is_revoked = 0", (emp_no,))
            return uid, temp_pw

    uid, final_pw = dosko_db.with_db_retry(_query_and_update)
    if not uid:
        return False, 404, {"error": "사번을 찾을 수 없습니다."}

    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="USER_MOD",
        resource_path="/api/admin/users/reset-pw", target_key=emp_no, detail={"action": "RESET_TO_BIRTHDAY", "initial_pw": final_pw},
    )
    return True, 200, {"status": "ok", "emp_no": emp_no, "temp_password": final_pw, "message": f"[{emp_no}] 사원의 비밀번호가 초기비번(사번+생일6자리: {final_pw})으로 원복되었습니다."}


def unlock_user(admin_user, data, client_ip):
    emp_no = str(data.get("emp_no") or "").strip()
    if not emp_no:
        return False, 400, {"error": "emp_no 필수"}
    def _run():
        with dosko_db.get_db_connection() as conn:
            conn.execute(
                "UPDATE dbo.sec_users SET failed_attempts = 0, is_locked = 0, locked_until = NULL, updated_by_emp_no = ?, updated_at = SYSDATETIME() WHERE emp_no = ?",
                (admin_user["emp_no"], emp_no),
            )
    dosko_db.with_db_retry(_run)
    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="USER_MOD",
        resource_path="/api/admin/users/unlock", target_key=emp_no, detail={"action": "UNLOCK_CLEAR_FAILS"},
    )
    return True, 200, {"status": "ok", "emp_no": emp_no, "message": f"[{emp_no}] 사원의 6회 오류 잠금이 해제되었습니다."}


def import_hr_csv(admin_user, data, client_ip):
    """PII(전화·이메일·생일·주소·입사일)를 파싱 단계에서 즉시 폐기하고 사번 기준 조직/상태만 동기화."""
    raw_text = str(data.get("csv_text") or "").strip()
    dry_run = bool(data.get("dry_run", True))
    if not raw_text:
        return False, 400, {"error": "csv_text 가 비어 있습니다."}

    delim = "\t" if "\t" in raw_text.splitlines()[0] else ","
    reader = csv.DictReader(io.StringIO(raw_text.lstrip("\ufeff")), delimiter=delim)

    parsed = []
    last_rec = None
    for row in reader:
        emp_no = str(row.get("사번") or row.get("emp_no") or "").strip()
        l5 = str(row.get("조직레벨5") or row.get("org_level5") or "").strip()
        if not emp_no:
            if last_rec is not None and l5:
                if not last_rec["org_level5"]:
                    last_rec["org_level5"] = l5
                elif l5 not in last_rec["org_level5"]:
                    last_rec["org_level5"] = f"{last_rec['org_level5']},{l5}"
            continue
        name = str(row.get("이름") or row.get("display_name") or "").strip()
        if not name:
            continue
        stat = str(row.get("상태") or row.get("employment_status") or "정상").strip()
        parsed.append({
            "emp_no": emp_no,
            "gw_login_id": str(row.get("ID") or row.get("gw_login_id") or "").strip(),
            "display_name": name,
            "work_type": str(row.get("근로형태") or row.get("work_type") or "").strip(),
            "org_level3": str(row.get("조직레벨3") or row.get("org_level3") or "").strip(),
            "org_level4": str(row.get("조직레벨4") or row.get("org_level4") or "").strip(),
            "org_level5": l5,
            "job_title": str(row.get("직위") or row.get("job_title") or "").strip(),
            "job_role_type": str(row.get("직무") or row.get("job_role_type") or "").strip(),
            "employment_status": "일시정지" if stat == "일시정지" else "정상",
        })
        last_rec = parsed[-1]

    created_count = 0
    updated_count = 0
    locked_count = 0
    summary_lines = []

    def _run():
        nonlocal created_count, updated_count, locked_count
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            existing = {
                r["emp_no"]: r
                for r in dosko_db.fetch_table_dicts(cur, "SELECT emp_no, display_name, org_level4, org_level5, job_title, employment_status, role_code FROM dbo.sec_users")
            }
            for rec in parsed:
                eno = rec["emp_no"]
                if eno not in existing:
                    created_count += 1
                    is_paused = rec["employment_status"] == "일시정지"
                    if is_paused:
                        locked_count += 1
                    summary_lines.append(f"+신규 [{eno}] {rec['display_name']} ({'LOCKED' if is_paused else 'RESTRICTED 대기'})")
                    if not dry_run:
                        pw_hash = dosko_auth.hash_password(secrets.token_urlsafe(24), iterations=50000)
                        role_c = "LOCKED" if is_paused else "RESTRICTED"
                        cur.execute(
                            """
                            INSERT INTO dbo.sec_users (
                                emp_no, gw_login_id, password_hash, display_name,
                                org_level3, org_level4, org_level5, job_title, job_role_type, work_type, employment_status,
                                role_code, is_pending_it_setup, can_login, is_active, allowed_lines_json, module_perms_json, created_by_emp_no
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,N'[]',?,?);
                            """,
                            (
                                eno, rec["gw_login_id"] or None, pw_hash, rec["display_name"],
                                rec["org_level3"] or None, rec["org_level4"] or None, rec["org_level5"] or None,
                                rec["job_title"] or None, rec["job_role_type"] or None, rec["work_type"] or None,
                                rec["employment_status"], role_c, 0 if is_paused else 1, 0 if is_paused else 1,
                                json.dumps(dosko_auth.DEFAULT_ROLE_MODULE_PERMS[role_c], ensure_ascii=False),
                                admin_user["emp_no"],
                            ),
                        )
                else:
                    ex = existing[eno]
                    changed = (
                        ex["display_name"] != rec["display_name"]
                        or (ex.get("org_level4") or "") != rec["org_level4"]
                        or (ex.get("org_level5") or "") != rec["org_level5"]
                        or (ex.get("job_title") or "") != rec["job_title"]
                        or ex["employment_status"] != rec["employment_status"]
                    )
                    if changed:
                        updated_count += 1
                        if rec["employment_status"] == "일시정지" and ex["role_code"] != "LOCKED":
                            locked_count += 1
                        summary_lines.append(f"~갱신 [{eno}] {rec['display_name']} ({rec['employment_status']})")
                        if not dry_run:
                            new_role = "LOCKED" if rec["employment_status"] == "일시정지" else ex["role_code"]
                            cur.execute(
                                """
                                UPDATE dbo.sec_users SET
                                    gw_login_id = COALESCE(NULLIF(?, N''), gw_login_id),
                                    display_name = ?,
                                    org_level3 = ?, org_level4 = ?, org_level5 = ?,
                                    job_title = ?, job_role_type = ?, work_type = ?, employment_status = ?,
                                    role_code = ?,
                                    can_login = CASE WHEN ? = N'일시정지' THEN 0 ELSE can_login END,
                                    is_active = CASE WHEN ? = N'일시정지' THEN 0 ELSE is_active END,
                                    updated_by_emp_no = ?, updated_at = SYSDATETIME()
                                WHERE emp_no = ?;
                                """,
                                (
                                    rec["gw_login_id"], rec["display_name"],
                                    rec["org_level3"] or None, rec["org_level4"] or None, rec["org_level5"] or None,
                                    rec["job_title"] or None, rec["job_role_type"] or None, rec["work_type"] or None,
                                    rec["employment_status"], new_role, rec["employment_status"], rec["employment_status"],
                                    admin_user["emp_no"], eno,
                                ),
                            )
                            if rec["employment_status"] == "일시정지":
                                cur.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE emp_no = ?", (eno,))

    dosko_db.with_db_retry(_run)
    if not dry_run:
        dosko_auth.log_audit(
            emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
            user_id=admin_user["user_id"], client_ip=client_ip, action_type="USER_MOD",
            resource_path="/api/admin/import-csv", detail={"parsed": len(parsed), "created": created_count, "updated": updated_count, "locked": locked_count},
        )
    return True, 200, {
        "status": "ok", "dry_run": dry_run, "total_parsed": len(parsed),
        "created_count": created_count, "updated_count": updated_count,
        "locked_count": locked_count, "summary_lines": summary_lines,
    }


def list_partners_and_tokens():
    def _q():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            partners = dosko_db.fetch_table_dicts(
                cur,
                "SELECT partner_code, wh_code, wh_name, serial_prefix, process_types, manager_name, is_active FROM dbo.proc_outsourcing_partners ORDER BY partner_code",
            )
            tokens = dosko_db.fetch_table_dicts(
                cur,
                "SELECT token_id, token_prefix, token_name, scope_type, partner_code, allowed_routes_json, is_active, expires_at, last_used_at, created_by_emp_no, created_at FROM dbo.sec_api_tokens ORDER BY token_id DESC",
            )
            return partners, tokens
    partners, tokens = dosko_db.with_db_retry(_q)
    return {"status": "ok", "partners": partners, "tokens": tokens}


def update_partner(admin_user, data, client_ip):
    pc = str(data.get("partner_code") or "").strip().upper()
    if not pc:
        return False, 400, {"error": "partner_code 필수"}
    new_code = str(data.get("new_access_code") or "").strip()
    is_act = data.get("is_active")

    def _run():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            if new_code:
                if len(new_code) < 4:
                    raise ValueError("협력사 접속코드는 4자 이상이어야 합니다.")
                h = dosko_auth.hash_password(new_code, iterations=100000)
                cur.execute(
                    "UPDATE dbo.proc_outsourcing_partners SET portal_access_code = N'[HASHED]', portal_access_hash = ? WHERE partner_code = ?",
                    (h, pc),
                )
            if is_act is not None:
                cur.execute("UPDATE dbo.proc_outsourcing_partners SET is_active = ? WHERE partner_code = ?", (1 if is_act else 0, pc))
                if not is_act:
                    cur.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE partner_code = ?", (pc,))

    try:
        dosko_db.with_db_retry(_run)
    except ValueError as ve:
        return False, 400, {"error": str(ve)}

    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="TOKEN_MOD",
        resource_path="/api/admin/partners/update", target_key=pc,
        detail={"rotated_code": bool(new_code), "is_active": is_act},
    )
    return True, 200, {"status": "ok"}


def create_api_token(admin_user, data, client_ip):
    name = str(data.get("token_name") or "").strip()
    scope = str(data.get("scope_type") or "ECOUNT_RPA").strip().upper()
    if not name or scope not in ("ECOUNT_RPA", "KIOSK_MONITOR"):
        return False, 400, {"error": "유효한 토큰명과 스코프가 필요합니다."}
    raw_tok = "dsk_" + secrets.token_urlsafe(28)
    tok_hash = dosko_auth.sha256_hex(raw_tok)
    pfx = raw_tok[:12]
    routes = ["/api/ecount/dom-schema", "/api/ecount/overview", "/api/ecount/qc-summary"] if scope == "ECOUNT_RPA" else ["/api/state", "/api/plc-history"]

    def _run():
        with dosko_db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO dbo.sec_api_tokens (
                    token_prefix, token_hash, token_name, scope_type, allowed_routes_json, is_active, created_by_emp_no
                ) VALUES (?, ?, ?, ?, ?, 1, ?);
                """,
                (pfx, tok_hash, name[:100], scope, json.dumps(routes), admin_user["emp_no"]),
            )
    dosko_db.with_db_retry(_run)
    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="TOKEN_MOD",
        resource_path="/api/admin/tokens/create", target_key=pfx, detail={"scope": scope, "name": name},
    )
    return True, 200, {"status": "ok", "raw_token": raw_tok, "token_prefix": pfx}


def revoke_api_token(admin_user, data, client_ip):
    tid = int(data.get("token_id") or 0)
    if not tid:
        return False, 400, {"error": "token_id 필수"}
    def _run():
        with dosko_db.get_db_connection() as conn:
            conn.execute("UPDATE dbo.sec_api_tokens SET is_active = 0 WHERE token_id = ?", (tid,))
    dosko_db.with_db_retry(_run)
    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="TOKEN_MOD",
        resource_path="/api/admin/tokens/revoke", target_key=str(tid),
    )
    return True, 200, {"status": "ok"}


def get_audit_and_sessions(qs):
    act_f = (qs.get("action_type") or [""])[0].strip()
    emp_f = (qs.get("emp_no") or [""])[0].strip()
    conds, params = [], []
    if act_f:
        conds.append("action_type = ?")
        params.append(act_f)
    if emp_f:
        conds.append("emp_no = ?")
        params.append(emp_f)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    def _q():
        with dosko_db.get_db_connection() as conn:
            cur = conn.cursor()
            sessions = dosko_db.fetch_table_dicts(
                cur,
                "SELECT session_token_hash, session_type, emp_no, partner_code, client_ip, issued_at, last_activity_at, expires_at FROM dbo.sec_sessions WHERE is_revoked = 0 AND expires_at > GETDATE() ORDER BY last_activity_at DESC",
            )
            logs = dosko_db.fetch_table_dicts(
                cur,
                f"SELECT TOP (200) audit_id, event_at, emp_no, display_name, role_code, client_ip, action_type, resource_path, target_key, detail_json FROM dbo.sec_audit_logs {where} ORDER BY audit_id DESC",
                tuple(params),
            )
            return sessions, logs

    sessions, logs = dosko_db.with_db_retry(_q)
    return {"status": "ok", "sessions": sessions, "logs": logs}


def revoke_session_by_hash(admin_user, data, client_ip):
    th = str(data.get("token_hash") or "").strip()
    if len(th) != 64:
        return False, 400, {"error": "유효한 session_token_hash가 필요합니다."}
    def _run():
        with dosko_db.get_db_connection() as conn:
            conn.execute("UPDATE dbo.sec_sessions SET is_revoked = 1 WHERE session_token_hash = ?", (th,))
    dosko_db.with_db_retry(_run)
    dosko_auth.log_audit(
        emp_no=admin_user["emp_no"], display_name=admin_user["display_name"], role_code=admin_user["role_code"],
        user_id=admin_user["user_id"], client_ip=client_ip, action_type="TOKEN_MOD",
        resource_path="/api/admin/sessions/revoke", target_key=th[:12],
    )
    return True, 200, {"status": "ok"}
