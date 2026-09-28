import contextlib
import datetime
import hmac
import os
import re
import sqlite3
import time
from datetime import timedelta

import pandas as pd
import requests
import streamlit as st

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Bangkok")
except Exception:  # เช่น Windows ที่ไม่มี tzdata
    TZ = datetime.timezone(timedelta(hours=7))

# ---------------------------------------------------------
# 0. การตั้งค่าทั่วไป
# ---------------------------------------------------------
st.set_page_config(
    page_title="ระบบสนับสนุนที่ปรึกษาเกษตร จ.ฉะเชิงเทรา",
    page_icon="🌾",
    layout="wide",
)

# ตั้ง path ของไฟล์ฐานข้อมูลผ่าน environment variable ได้ (ชี้ไปยังดิสก์ถาวรเมื่อ deploy)
DB_FILE = os.environ.get("RICE_DB_PATH", "rice_records.db")
# เกณฑ์ % ฝนที่ถือว่าไม่เหมาะกับการพ่นยา (เทียบกับค่าที่ได้จากแหล่งพยากรณ์โดยตรง)
RAIN_LIMIT = 60

# แหล่งพยากรณ์อากาศ: ปรับมาใช้ openmeteo เป็นค่าเริ่มต้น (ฟรีและไม่ต้องใช้ API Key)
WEATHER_SOURCE = os.environ.get("WEATHER_SOURCE", "openmeteo").strip().lower()
TMD_URL = "https://data.tmd.go.th/api/WeatherForecast7Days/v2/"
TMD_PROVINCE = "ฉะเชิงเทรา"  # API ของกรมอุตุฯ เป็นระดับจังหวัด (ล่วงหน้า 7 วัน)


def _st_version():
    m = re.match(r"(\d+)\.(\d+)", st.__version__)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# Streamlit รุ่นใหม่ใช้ width="stretch" แทน use_container_width=True
STRETCH = (
    {"width": "stretch"} if _st_version() >= (1, 50) else {"use_container_width": True}
)


def today():
    """วันที่ปัจจุบันตามเวลาไทย (ไม่ขึ้นกับเขตเวลาของเซิร์ฟเวอร์)"""
    return datetime.datetime.now(TZ).date()


def now_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def parse_date(value):
    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return today()


def flash(kind, message):
    """เก็บข้อความไว้แสดงหลัง st.rerun() (kind: success / warning / error / info)"""
    st.session_state["_flash"] = (kind, message)


def show_flash():
    item = st.session_state.pop("_flash", None)
    if item:
        getattr(st, item[0])(item[1])


def fmt_days(n):
    return f"+{n} วัน" if n > 0 else f"{n} วัน"


def get_secret(name):
    """อ่านค่าลับจาก st.secrets ก่อน แล้วค่อยดูจาก environment variable"""
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return value or os.environ.get(name)


def require_login():
    """ถ้าตั้งค่า APP_PASSWORD ใน .streamlit/secrets.toml จะบังคับให้ใส่รหัสผ่านก่อนใช้งาน"""
    try:
        expected = st.secrets.get("APP_PASSWORD")
    except Exception:
        expected = None
    if not expected or st.session_state.get("auth_ok"):
        return
    st.title("🔐 เข้าสู่ระบบ")
    password = st.text_input("รหัสผ่าน", type="password")
    if st.button("เข้าสู่ระบบ", type="primary"):
        if hmac.compare_digest(password, str(expected)):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("รหัสผ่านไม่ถูกต้อง")
    st.stop()


require_login()

# ---------------------------------------------------------
# 1. ข้อมูลอ้างอิง: อำเภอ สายพันธุ์ข้าว กฎกิจกรรม
# ---------------------------------------------------------
district_coords = {
    "เมืองฉะเชิงเทรา": {"lat": 13.690, "lon": 101.070},
    "บางคล้า": {"lat": 13.723, "lon": 101.208},
    "บางน้ำเปรี้ยว": {"lat": 13.847, "lon": 100.970},
    "บางปะกง": {"lat": 13.548, "lon": 100.993},
    "บ้านโพธิ์": {"lat": 13.599, "lon": 101.077},
    "พนมสารคาม": {"lat": 13.745, "lon": 101.346},
    "ราชสาส์น": {"lat": 13.780, "lon": 101.288},
    "สนามชัยเขต": {"lat": 13.658, "lon": 101.438},
    "แปลงยาว": {"lat": 13.583, "lon": 101.283},
    "ท่าตะเกียบ": {"lat": 13.417, "lon": 101.678},
    "คลองเขื่อน": {"lat": 13.792, "lon": 101.162},
}

# รายชื่อพันธุ์ข้าวอยู่ในไฟล์ CSV (ตามเกณฑ์การจัดหมวดเบา/หนักของบริษัท) แก้ไขด้วย Excel ได้โดยไม่ต้องแตะโค้ด
# คอลัมน์: ชื่อพันธุ์ | ประเภท (พันธุ์เบา หรือ พันธุ์หนัก) | ไวแสง (ใส่ "ใช่" ถ้าเป็นพันธุ์ไวแสง ไม่ใช่เว้นว่าง)
# ตั้ง path อื่นได้ผ่าน environment variable RICE_CATALOG_PATH
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE_DIR = os.getcwd()
CATALOG_FILE = os.environ.get(
    "RICE_CATALOG_PATH", os.path.join(BASE_DIR, "data", "rice_catalog.csv")
)
VALID_RICE_TYPES = ("พันธุ์เบา", "พันธุ์หนัก")
TRUE_VALUES = {"ใช่", "1", "y", "yes", "true", "x"}


def load_rice_catalog(path):
    """อ่านและตรวจไฟล์รายชื่อพันธุ์ข้าว คืนค่า (dict ชื่อ -> ประเภท, set ของพันธุ์ไวแสง)"""
    if not os.path.exists(path):
        st.error(f"❌ ไม่พบไฟล์รายชื่อพันธุ์ข้าว: {path}")
        st.stop()
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except Exception as exc:
        st.error(f"❌ อ่านไฟล์รายชื่อพันธุ์ข้าวไม่สำเร็จ: {exc}")
        st.stop()
    missing_cols = {"ชื่อพันธุ์", "ประเภท"} - set(df.columns)
    if missing_cols:
        st.error(f"❌ ไฟล์รายชื่อพันธุ์ข้าวขาดคอลัมน์: {', '.join(sorted(missing_cols))}")
        st.stop()

    df["ชื่อพันธุ์"] = df["ชื่อพันธุ์"].str.strip()
    df["ประเภท"] = df["ประเภท"].str.strip()
    df = df[df["ชื่อพันธุ์"] != ""]

    bad_type = df[~df["ประเภท"].isin(VALID_RICE_TYPES)]
    if not bad_type.empty:
        st.error(
            "❌ ประเภทพันธุ์ไม่ถูกต้อง (ต้องเป็น 'พันธุ์เบา' หรือ 'พันธุ์หนัก'): "
            + ", ".join(bad_type["ชื่อพันธุ์"])
        )
        st.stop()
    duplicated = df[df["ชื่อพันธุ์"].duplicated()]
    if not duplicated.empty:
        st.error("❌ ชื่อพันธุ์ซ้ำในไฟล์: " + ", ".join(duplicated["ชื่อพันธุ์"]))
        st.stop()

    catalog = dict(zip(df["ชื่อพันธุ์"], df["ประเภท"]))
    photoperiod = set()
    if "ไวแสง" in df.columns:
        flags = df["ไวแสง"].str.strip().str.lower().isin(TRUE_VALUES)
        photoperiod = set(df.loc[flags, "ชื่อพันธุ์"])
    return catalog, photoperiod


# PHOTOPERIOD_SENSITIVE: พันธุ์ไวต่อช่วงแสง ออกดอกตามความยาวของวัน ไม่ใช่ตามจำนวนวันหลังหว่าน
rice_catalog, PHOTOPERIOD_SENSITIVE = load_rice_catalog(CATALOG_FILE)

planting_methods = ["หว่านน้ำตม", "หว่านแห้ง / หว่านสำรวย", "ปักดำ / ดำนา"]
TRANSPLANT_METHOD = "ปักดำ / ดำนา"

activity_rules = {
    "พันธุ์เบา": [
        {"day": 0, "start": 0, "end": 0, "activity": "🌾 วันเริ่มเพาะปลูก/หว่านข้าว", "is_spray": False},
        {"day": 2, "start": 0, "end": 4, "activity": "💧 ระยะคุมเลน (0-4 วัน)", "is_spray": True},
        {"day": 9, "start": 7, "end": 12, "activity": "🌿 ระยะคุมฆ่า (7-12 วัน)", "is_spray": True},
        {"day": 16, "start": 15, "end": 18, "activity": "🌱 หว่านปุ๋ยรอบที่ 1 (15-18 วัน)", "is_spray": False},
        {"day": 21, "start": 20, "end": 23, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 1 (20-23 วัน)", "is_spray": True},
        {"day": 32, "start": 30, "end": 35, "activity": "🌾 หว่านปุ๋ยรอบที่ 2 (30-35 วัน)", "is_spray": False},
        {"day": 35, "start": 33, "end": 38, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 2 (33-38 วัน)", "is_spray": True},
        {"day": 47, "start": 45, "end": 50, "activity": "🌾 หว่านปุ๋ยรอบที่ 3 (45-50 วัน)", "is_spray": False},
        {"day": 52, "start": 50, "end": 55, "activity": "🌸 ระยะกัดหางปลาทู (50-55 วัน)", "is_spray": False},
        {"day": 70, "start": 70, "end": 70, "activity": "🌾 ระยะข้าวก้ม (70 วัน)", "is_spray": False},
        {"day": 95, "start": 95, "end": 95, "activity": "🚜 วันเก็บเกี่ยวโดยประมาณ", "is_spray": False},
    ],
    "พันธุ์หนัก": [
        {"day": 0, "start": 0, "end": 0, "activity": "🌾 วันเริ่มเพาะปลูก/หว่านข้าว", "is_spray": False},
        {"day": 2, "start": 0, "end": 4, "activity": "💧 ระยะคุมเลน (0-4 วัน)", "is_spray": True},
        {"day": 9, "start": 7, "end": 12, "activity": "🌿 ระยะคุมฆ่า (7-12 วัน)", "is_spray": True},
        {"day": 22, "start": 20, "end": 25, "activity": "🌱 หว่านปุ๋ยรอบที่ 1 (20-25 วัน)", "is_spray": False},
        {"day": 26, "start": 25, "end": 28, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 1 (25-28 วัน)", "is_spray": True},
        {"day": 47, "start": 45, "end": 50, "activity": "🌾 หว่านปุ๋ยรอบที่ 2 (45-50 วัน)", "is_spray": False},
        {"day": 50, "start": 48, "end": 53, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 2 (48-53 วัน)", "is_spray": True},
        {"day": 72, "start": 70, "end": 75, "activity": "🌾 หว่านปุ๋ยรอบที่ 3 (70-75 วัน)", "is_spray": False},
        {"day": 80, "start": 75, "end": 85, "activity": "🌸 ระยะกัดหางปลาทู (75-85 วัน)", "is_spray": False},
        {"day": 100, "start": 100, "end": 100, "activity": "🌾 ระยะข้าวก้ม (100 วัน)", "is_spray": False},
        {"day": 120, "start": 120, "end": 120, "activity": "🚜 วันเก็บเกี่ยวโดยประมาณ", "is_spray": False},
    ],
}

chachoengsao_climatology = {
    1: 10, 2: 15, 3: 25, 4: 40, 5: 60, 6: 55,
    7: 60, 8: 65, 9: 75, 10: 60, 11: 30, 12: 10,
}

# ---------------------------------------------------------
# 2. ฐานข้อมูล SQLite
# ---------------------------------------------------------
@contextlib.contextmanager
def db():
    """เปิดการเชื่อมต่อ, commit เมื่อสำเร็จ, rollback เมื่อผิดพลาด และปิดการเชื่อมต่อเสมอ"""
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS rice_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                farmer_name TEXT NOT NULL,
                district TEXT NOT NULL,
                field_name TEXT NOT NULL,
                rice_species TEXT NOT NULL,
                planting_method TEXT NOT NULL,
                sow_date TEXT NOT NULL,
                officer_in_charge TEXT DEFAULT 'ไม่ระบุ',
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'ปกติ',
                has_issue INTEGER DEFAULT 0,
                accumulated_shift INTEGER DEFAULT 0,
                last_delayed_activity TEXT DEFAULT 'ไม่มี',
                updated_at TEXT NOT NULL
            )
        """)
        cols = {r[1] for r in c.execute("PRAGMA table_info(rice_records)").fetchall()}
        if "officer_in_charge" not in cols:
            c.execute("ALTER TABLE rice_records ADD COLUMN officer_in_charge TEXT DEFAULT 'ไม่ระบุ'")
        if "has_issue" not in cols:
            c.execute("ALTER TABLE rice_records ADD COLUMN has_issue INTEGER DEFAULT 0")
            # ย้ายข้อมูลเดิม: ถือว่ามีปัญหาเมื่อสถานะไม่ใช่ค่าว่าง/'ปกติ' แบบตรงตัว
            c.execute("""
                UPDATE rice_records SET has_issue = CASE
                    WHEN TRIM(COALESCE(status, '')) IN ('', 'ปกติ', 'None') THEN 0
                    ELSE 1 END
            """)

        # เหตุการณ์การเลื่อนกิจกรรม (เก็บทุกครั้ง แทนตัวเลขรวมเพียงค่าเดียว)
        c.execute("""
            CREATE TABLE IF NOT EXISTS schedule_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL,
                activity TEXT NOT NULL,
                days INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # ประวัติการตรวจแปลง
        c.execute("""
            CREATE TABLE IF NOT EXISTS inspections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL,
                inspected_at TEXT NOT NULL,
                officer TEXT,
                activity TEXT,
                note TEXT,
                has_issue INTEGER DEFAULT 0
            )
        """)

        # ย้ายค่า accumulated_shift แบบเดิมมาเป็นเหตุการณ์ (ทำครั้งเดียว แล้วรีเซ็ตค่าเดิมเป็น 0)
        first_activity = activity_rules["พันธุ์เบา"][0]["activity"]
        c.execute("""
            INSERT INTO schedule_shifts (record_id, activity, days, created_at)
            SELECT id,
                   CASE WHEN last_delayed_activity IS NULL
                             OR last_delayed_activity IN ('', 'ไม่มี')
                        THEN ? ELSE last_delayed_activity END,
                   accumulated_shift,
                   COALESCE(updated_at, created_at)
            FROM rice_records
            WHERE accumulated_shift IS NOT NULL AND accumulated_shift <> 0
        """, (first_activity,))
        c.execute("UPDATE rice_records SET accumulated_shift = 0 WHERE accumulated_shift <> 0")


init_db()


def save_to_db(farmer, district, field, rice, method, date_start, officer):
    sow_date_str = date_start.strftime("%Y-%m-%d")
    ts = now_str()
    officer_str = officer.strip() if officer and officer.strip() else "ไม่ระบุ"
    with db() as conn:
        exists = conn.execute(
            "SELECT id FROM rice_records WHERE farmer_name = ? AND field_name = ? AND sow_date = ?",
            (farmer, field, sow_date_str),
        ).fetchone()
        if exists:
            return False
        conn.execute(
            """
            INSERT INTO rice_records (
                farmer_name, district, field_name, rice_species, planting_method,
                sow_date, officer_in_charge, created_at, status, has_issue, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ปกติ', 0, ?)
            """,
            (farmer, district, field, rice, method, sow_date_str, officer_str, ts, ts),
        )
        return True


def update_record(record_id, farmer, district, field, rice, method, date_start, officer):
    """แก้ไขข้อมูลพื้นฐานของแปลง คืนค่า 'ok' หรือ 'duplicate'"""
    sow_date_str = date_start.strftime("%Y-%m-%d")
    officer_str = officer.strip() if officer and officer.strip() else "ไม่ระบุ"
    with db() as conn:
        dup = conn.execute(
            """SELECT id FROM rice_records
               WHERE farmer_name = ? AND field_name = ? AND sow_date = ? AND id <> ?""",
            (farmer, field, sow_date_str, record_id),
        ).fetchone()
        if dup:
            return "duplicate"
        conn.execute(
            """UPDATE rice_records
               SET farmer_name = ?, district = ?, field_name = ?, rice_species = ?,
                   planting_method = ?, sow_date = ?, officer_in_charge = ?, updated_at = ?
               WHERE id = ?""",
            (farmer, district, field, rice, method, sow_date_str, officer_str, now_str(), record_id),
        )
        return "ok"


def add_inspection(record_id, officer, activity, note, has_issue):
    """บันทึกประวัติการตรวจ และอัปเดตสถานะล่าสุดของแปลง"""
    prefix = f"[{activity}] " if activity and not activity.startswith("ไม่ระบุ") else ""
    text = f"{prefix}{note}".strip()
    ts = now_str()
    officer_str = officer.strip() if officer and officer.strip() else "ไม่ระบุ"
    with db() as conn:
        conn.execute(
            """INSERT INTO inspections (record_id, inspected_at, officer, activity, note, has_issue)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (record_id, ts, officer_str, activity, note, 1 if has_issue else 0),
        )
        conn.execute(
            """UPDATE rice_records
               SET status = ?, has_issue = ?, officer_in_charge = ?, updated_at = ?
               WHERE id = ?""",
            (text if has_issue else "ปกติ", 1 if has_issue else 0, officer_str, ts, record_id),
        )


def get_inspections(record_id, limit=10):
    with db() as conn:
        return pd.read_sql_query(
            """SELECT inspected_at AS "วันที่ตรวจ", officer AS "ผู้ตรวจ",
                      activity AS "กิจกรรม", note AS "รายละเอียด", has_issue
               FROM inspections WHERE record_id = ? ORDER BY id DESC LIMIT ?""",
            conn, params=(record_id, limit),
        )


def add_shift_event(record_id, activity, days):
    with db() as conn:
        conn.execute(
            "INSERT INTO schedule_shifts (record_id, activity, days, created_at) VALUES (?, ?, ?, ?)",
            (record_id, activity, int(days), now_str()),
        )
        conn.execute("UPDATE rice_records SET updated_at = ? WHERE id = ?", (now_str(), record_id))


def delete_shift_event(event_id):
    with db() as conn:
        conn.execute("DELETE FROM schedule_shifts WHERE id = ?", (event_id,))


def get_shift_events(record_id):
    """คืนรายการ (id, activity, days, created_at) เรียงตามลำดับที่บันทึก"""
    with db() as conn:
        return conn.execute(
            "SELECT id, activity, days, created_at FROM schedule_shifts WHERE record_id = ? ORDER BY id",
            (record_id,),
        ).fetchall()


def load_shift_events():
    """คืน dict: record_id -> [(activity, days), ...] สำหรับทุกแปลง"""
    events = {}
    with db() as conn:
        for rid, act, days in conn.execute(
            "SELECT record_id, activity, days FROM schedule_shifts ORDER BY id"
        ).fetchall():
            events.setdefault(rid, []).append((act, days))
    return events


def delete_field_record(record_id):
    with db() as conn:
        conn.execute("DELETE FROM schedule_shifts WHERE record_id = ?", (record_id,))
        conn.execute("DELETE FROM inspections WHERE record_id = ?", (record_id,))
        conn.execute("DELETE FROM rice_records WHERE id = ?", (record_id,))
    return True


def officer_names():
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT officer_in_charge FROM rice_records
               WHERE officer_in_charge IS NOT NULL AND TRIM(officer_in_charge) <> ''
                 AND officer_in_charge <> 'ไม่ระบุ'
               ORDER BY officer_in_charge"""
        ).fetchall()
    return [r[0] for r in rows]


def load_db():
    with db() as conn:
        query = """
            SELECT
                r.id,
                r.farmer_name AS "ชื่อเกษตรกร",
                r.district AS "อำเภอ",
                r.field_name AS "ชื่อแปลง/ที่ตั้ง",
                r.rice_species AS "สายพันธุ์ข้าว",
                r.planting_method AS "วิธีการปลูก",
                r.sow_date AS "วันที่เริ่มเพาะปลูก",
                r.officer_in_charge AS "ผู้รับผิดชอบแปลง",
                r.created_at AS "วันบันทึกข้อมูล",
                r.status AS "สถานะ/ปัญหาที่พบ",
                r.has_issue AS "มีปัญหา",
                COALESCE(s.total_days, 0) AS "จำนวนวันที่ปรับเลื่อนสะสม",
                COALESCE(s.last_act, 'ไม่มี') AS "กิจกรรมล่าสุดที่เลื่อน",
                r.updated_at AS "วันที่อัปเดตล่าสุด"
            FROM rice_records r
            LEFT JOIN (
                SELECT record_id,
                       SUM(days) AS total_days,
                       (SELECT activity FROM schedule_shifts s2
                         WHERE s2.record_id = s1.record_id ORDER BY s2.id DESC LIMIT 1) AS last_act
                FROM schedule_shifts s1
                GROUP BY record_id
            ) s ON s.record_id = r.id
            ORDER BY r.id
        """
        return pd.read_sql_query(query, conn)


# ---------------------------------------------------------
# 3. พยากรณ์อากาศ และการคำนวณปฏิทินกิจกรรม
# ---------------------------------------------------------
def _tmd_date(text):
    """แปลงวันที่ dd/mm/yyyy (รองรับปี พ.ศ.) เป็น 'YYYY-MM-DD'"""
    d, m, y = [int(x) for x in str(text).strip().split("/")]
    if y > 2400:
        y -= 543
    return datetime.date(y, m, d).strftime("%Y-%m-%d")


def parse_tmd_forecast(data, province=TMD_PROVINCE):
    """
    แปลงผลตอบกลับของ WeatherForecast7Days (v2, json) เป็น {'YYYY-MM-DD': % ฝนปกคลุมพื้นที่}
    รองรับทั้งกรณี Province เป็น list (หลายจังหวัด) และ dict (จังหวัดเดียว)
    """
    provinces = data["Provinces"]["Province"]
    if isinstance(provinces, dict):
        provinces = [provinces]
    match = next(
        (p for p in provinces if str(p.get("ProvinceNameThai", "")).strip() == province), None
    )
    if match is None:
        raise ValueError(f"ไม่พบจังหวัด {province} ในข้อมูลของกรมอุตุฯ")
    forecast = match["SevenDaysForecast"]
    return {
        _tmd_date(d): int(float(r))
        for d, r in zip(forecast["ForecastDate"], forecast["PercentRainCover"])
    }


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_tmd(uid, ukey, province):
    # ถ้าเรียก API ไม่สำเร็จจะ raise เพื่อไม่ให้ผลว่างถูกแคชไว้ 1 ชั่วโมง
    res = requests.get(
        TMD_URL,
        params={"uid": uid, "ukey": ukey, "format": "json", "Province": province},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    res.raise_for_status()
    return parse_tmd_forecast(res.json(), province)


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_openmeteo(lat, lon):
    res = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "precipitation_probability_max",
            "forecast_days": 14,
            "timezone": "Asia/Bangkok",
        },
        timeout=5,
    )
    res.raise_for_status()
    data = res.json()
    return dict(zip(data["daily"]["time"], data["daily"]["precipitation_probability_max"]))


def fetch_district_weather(district_name):
    """
    คืน {วันที่: % ฝน} ถ้าดึงไม่ได้คืน {} (ระบบใช้สถิติรายเดือนแทน)
    และเก็บสาเหตุไว้แสดงท้ายหน้า (ไม่ใส่ URL เพื่อไม่ให้รหัส API หลุดขึ้นหน้าจอ)
    """
    try:
        if WEATHER_SOURCE == "openmeteo":
            coords = district_coords.get(district_name, district_coords["เมืองฉะเชิงเทรา"])
            result = _fetch_openmeteo(coords["lat"], coords["lon"])
        else:
            uid, ukey = get_secret("TMD_UID"), get_secret("TMD_UKEY")
            if not uid or not ukey:
                raise RuntimeError("ยังไม่ได้ตั้งค่า TMD_UID / TMD_UKEY")
            result = _fetch_tmd(str(uid), str(ukey), TMD_PROVINCE)
        st.session_state.pop("_weather_err", None)
        return result
    except requests.HTTPError as exc:
        st.session_state["_weather_err"] = f"HTTP {exc.response.status_code}"
    except requests.RequestException as exc:
        st.session_state["_weather_err"] = type(exc).__name__
    except Exception as exc:
        st.session_state["_weather_err"] = f"{type(exc).__name__}: {exc}"
    return {}


def format_date_range(d_start, d_end):
    if d_start == d_end:
        return d_start.strftime("%d/%m/%Y")
    if d_start.month == d_end.month and d_start.year == d_end.year:
        return f"{d_start.strftime('%d')}-{d_end.strftime('%d/%m/%Y')}"
    if d_start.year == d_end.year:
        return f"{d_start.strftime('%d/%m')}-{d_end.strftime('%d/%m/%Y')}"
    return f"{d_start.strftime('%d/%m/%Y')}-{d_end.strftime('%d/%m/%Y')}"


def compute_shifts(rules, events):
    """
    คำนวณจำนวนวันที่เลื่อนของแต่ละกิจกรรมจากรายการเหตุการณ์ [(activity, days), ...]
    เหตุการณ์หนึ่งครั้งกระทบกิจกรรมที่เลือกและกิจกรรมถัดไปทั้งหมด และเหตุการณ์หลายครั้งรวมกันได้
    """
    names = [r["activity"] for r in rules]
    shifts = [0] * len(rules)
    origins = [None] * len(rules)
    for act, days in events:
        start = names.index(act) if act in names else 0
        for i in range(start, len(rules)):
            shifts[i] += days
            origins[i] = act
    return shifts, origins


def rain_for(day, forecast):
    """คืน (โอกาสฝน %, เป็นพยากรณ์สดหรือไม่)"""
    key = day.strftime("%Y-%m-%d")
    if key in forecast:
        return forecast[key], True
    return chachoengsao_climatology[day.month], False


def suggest_dry_day(start, end, forecast, extra_days=5):
    """หาวันแรกที่ฝนไม่เกินเกณฑ์ ในช่วงกิจกรรมและอีก extra_days วันถัดไป (เฉพาะวันที่มีพยากรณ์จริง)"""
    d = max(start, today())
    last = end + timedelta(days=extra_days)
    while d <= last:
        pct = forecast.get(d.strftime("%Y-%m-%d"))
        if pct is not None and pct <= RAIN_LIMIT:
            return d, pct
        d += timedelta(days=1)
    return None


def rain_window(start, end, forecast):
    """โอกาสฝนรายวันตลอดช่วงกิจกรรม [(วันที่, % ฝน, เป็นพยากรณ์สดหรือไม่)] เฉพาะวันที่ยังไม่ผ่านมา"""
    days = []
    d = max(start, today())
    while d <= end:
        pct, live = rain_for(d, forecast)
        days.append((d, pct, live))
        d += timedelta(days=1)
    return days


def describe_rain(days, district_name):
    pcts = [x[1] for x in days]
    low, high = min(pcts), max(pcts)
    span = f"{low}%" if low == high else f"{low}-{high}%"
    live_count = sum(1 for x in days if x[2])
    if live_count == len(days):
        if WEATHER_SOURCE == "openmeteo":
            return f"⚡ {span} (พยากรณ์สด อ.{district_name})"
        return f"⚡ {span} (พยากรณ์กรมอุตุฯ จ.{TMD_PROVINCE})"
    if live_count == 0:
        return f"📊 {span} (สถิติรายเดือน)"
    return f"⚡ {span} (มีพยากรณ์ {live_count}/{len(days)} วัน ที่เหลือใช้สถิติ)"


def assess_rain(rule, days, n_start, n_end, forecast, district_name):
    """
    ประเมินฝนตลอดช่วงกิจกรรม คืน (ข้อความสถานะ, คำแนะนำ, ต้องเตือนสีแดงหรือไม่)
    - ไม่ใช่กิจกรรมพ่นยา: แสดงช่วงโอกาสฝนเฉยๆ
    - พ่นยา + ฝนมากทุกวันในช่วง: เตือนแดง และแนะนำวันแห้งที่ใกล้ที่สุด
    - พ่นยา + ฝนมากบางวัน: ไม่เตือนแดง แต่แนะนำวันที่ฝนน้อยที่สุดในช่วง
    """
    if not days:
        return "⏳ ช่วงกิจกรรมนี้ผ่านมาแล้ว", "-", False
    status = describe_rain(days, district_name)
    if not rule["is_spray"]:
        return status, "-", False

    rainy = [x for x in days if x[1] > RAIN_LIMIT]
    if not rainy:
        return status, "-", False

    if len(rainy) == len(days):
        status += " ⚠️ ฝนมากตลอดช่วง"
        if any(x[2] for x in days):
            found = suggest_dry_day(n_start, n_end, forecast)
            if found:
                advice = f"💡 แนะนำทำวันที่ {found[0].strftime('%d/%m')} (ฝน {found[1]}%)"
            else:
                advice = "💡 ฝนสูงต่อเนื่อง ควรติดตามพยากรณ์ก่อนพ่นยา"
        else:
            advice = "💡 ช่วงนี้ฝนชุก ควรติดตามพยากรณ์ใกล้วันทำงาน"
        return status, advice, True

    dry = [x for x in days if x[1] <= RAIN_LIMIT]
    best = min(dry, key=lambda x: (x[1], not x[2]))  # ฝนน้อยสุด ถ้าเท่ากันเลือกวันที่มีพยากรณ์สด
    source = "" if best[2] else " สถิติ"
    status += " (บางวันฝนมาก)"
    advice = f"💡 ทำได้ในช่วงนี้ แนะนำวันที่ {best[0].strftime('%d/%m')} (ฝน {best[1]}%{source})"
    return status, advice, False


SCHEDULE_COLS = [
    "กิจกรรม", "วันตามกำหนดเดิม", "วันที่ปรับใหม่",
    "การปรับเปลี่ยน", "โอกาสเกิดฝนและการประเมิน", "คำแนะนำ",
]


def get_rice_schedule(sow_date, rice_name, district_name="เมืองฉะเชิงเทรา",
                      events=None, extra_event=None):
    """
    สร้างตารางกิจกรรม
    - วันที่ปรับใหม่: อิงเฉพาะเหตุการณ์การเลื่อนที่ 'บันทึกไว้' เท่านั้น (ไม่เปลี่ยนตามพยากรณ์รายวัน)
    - ฝน: ประเมินทุกวันในช่วงกิจกรรม แสดงเป็น 'คำแนะนำ' ไม่เลื่อนกำหนดการอัตโนมัติ
    - extra_event: (activity, days) สำหรับพรีวิวก่อนบันทึก
    """
    rules = activity_rules[rice_catalog.get(rice_name, "พันธุ์เบา")]
    evs = list(events or [])
    if extra_event and extra_event[1]:
        evs.append(extra_event)
    shifts, origins = compute_shifts(rules, evs)
    forecast = fetch_district_weather(district_name)
    rows = []

    for i, rule in enumerate(rules):
        shift = shifts[i]
        o_start = sow_date + timedelta(days=rule["start"])
        o_end = sow_date + timedelta(days=rule["end"])
        n_start = o_start + timedelta(days=shift)
        n_end = o_end + timedelta(days=shift)

        note = "ตรงตามกำหนดเดิม"
        if shift != 0:
            note = f"{fmt_days(shift)} (เริ่มเลื่อนที่: {origins[i]})"

        days = rain_window(n_start, n_end, forecast)
        status_text, advice, danger = assess_rain(
            rule, days, n_start, n_end, forecast, district_name
        )

        rows.append({
            "กิจกรรม": rule["activity"],
            "วันตามกำหนดเดิม": format_date_range(o_start, o_end),
            "วันที่ปรับใหม่": format_date_range(n_start, n_end),
            "การปรับเปลี่ยน": note,
            "โอกาสเกิดฝนและการประเมิน": status_text,
            "คำแนะนำ": advice,
            "_danger": danger,
            "_shifted": shift != 0,
            "_start": n_start,
            "_end": n_end,
        })
    return pd.DataFrame(rows)


def style_schedule(df):
    """ไฮไลต์: แดง = ฝนมากในวันพ่นยา, เหลือง = กิจกรรมที่ถูกเลื่อน"""
    def _highlight(row):
        if df.loc[row.name, "_danger"]:
            css = "background-color: #ffebee; font-weight: bold"
        elif df.loc[row.name, "_shifted"]:
            css = "background-color: #fff9c4; font-weight: bold"
        else:
            css = ""
        return [css] * len(row)

    return df[SCHEDULE_COLS].style.apply(_highlight, axis=1)


def calculate_rice_age(sow_date_str):
    try:
        sow = datetime.datetime.strptime(str(sow_date_str), "%Y-%m-%d").date()
    except Exception:
        return "-"
    age_days = (today() - sow).days
    if age_days < 0:
        return f"ยังไม่ถึงวันปลูก (อีก {-age_days} วัน)"
    return f"{age_days} วัน"


def warn_unknown_species(df):
    """เตือนเมื่อมีแปลงที่ชื่อสายพันธุ์ไม่อยู่ใน catalog (เช่น ถูกแก้ชื่อในไฟล์ CSV ภายหลัง)"""
    unknown = sorted(set(df["สายพันธุ์ข้าว"]) - set(rice_catalog))
    if unknown:
        st.warning(
            "⚠️ พบแปลงที่ใช้ชื่อสายพันธุ์ซึ่งไม่อยู่ใน catalog ปัจจุบัน "
            "(ระบบใช้ตารางพันธุ์เบาแทนชั่วคราว): " + ", ".join(unknown)
            + " — กรุณาแก้ชื่อในไฟล์ CSV ให้ตรงกัน หรือแก้ข้อมูลแปลงในแท็บ 4"
        )


NEW_OFFICER = "➕ เพิ่มชื่อผู้รับผิดชอบใหม่..."


def officer_input(label, key, current=None):
    """เลือกผู้รับผิดชอบจากรายชื่อที่มีอยู่ (ลดปัญหาพิมพ์ชื่อไม่ตรงกัน) หรือเพิ่มชื่อใหม่"""
    options = ["ไม่ระบุ"] + officer_names()
    if current and current not in options:
        options.append(current)
    options.append(NEW_OFFICER)
    index = options.index(current) if current in options else 0
    choice = st.selectbox(label, options, index=index, key=f"{key}_sel")
    if choice == NEW_OFFICER:
        return st.text_input("ชื่อผู้รับผิดชอบใหม่:", key=f"{key}_new").strip() or "ไม่ระบุ"
    return choice


def rice_warnings(rice_name, method):
    """แสดงคำเตือนเมื่อตารางมาตรฐานอาจไม่ตรงกับพันธุ์/วิธีปลูกที่เลือก"""
    if method == TRANSPLANT_METHOD:
        st.info(
            "ℹ️ ตารางกิจกรรมนี้อ้างอิงแบบข้าวหว่าน (เช่น คุมเลน/คุมฆ่า) "
            "สำหรับข้าวปักดำให้ใช้เป็นแนวทางเบื้องต้น และปรับตามคำแนะนำของเจ้าหน้าที่"
        )
    if rice_name in PHOTOPERIOD_SENSITIVE:
        st.info(
            "ℹ️ พันธุ์นี้ไวต่อช่วงแสง วันออกดอกและเก็บเกี่ยวขึ้นกับช่วงเวลาของปี "
            "ไม่ได้ขึ้นกับจำนวนวันหลังหว่านเพียงอย่างเดียว ควรใช้วันที่ในตารางช่วงท้ายเป็นค่าประมาณ"
        )


# ---------------------------------------------------------
# 4. ส่วนติดต่อผู้ใช้ (4 แท็บ)
# ---------------------------------------------------------
st.markdown(
    "<h1 style='text-align: center; color: #1e7e34;'>🌾 ระบบสนับสนุนการตัดสินใจและวางแผนตรวจเยี่ยมแปลงนา จ.ฉะเชิงเทรา</h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='text-align: center; color: #555;'>เครื่องมือส่วนกลางสำหรับเจ้าหน้าที่และที่ปรึกษาทางการเกษตรประจำแผนก</p>",
    unsafe_allow_html=True,
)
show_flash()

tab1, tab2, tab3, tab4 = st.tabs([
    "📝 ขึ้นทะเบียนแปลงในความดูแล",
    "🩺 บันทึกการตรวจแปลงและพบปัญหา",
    "🔄 ปรับเลื่อนวันทำกิจกรรม/ปฏิทิน",
    "📊 แดชบอร์ดสรุปภาพรวมและจัดการแปลง",
])

# --- TAB 1: ขึ้นทะเบียนแปลง ---
with tab1:
    st.subheader("บันทึกข้อมูลแปลงนาเกษตรกรรายใหม่")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        farmer_name = st.text_input(
            "👤 ชื่อเกษตรกรผู้รับคำปรึกษา:", placeholder="ตัวอย่าง: นายสมศักดิ์ รักดี"
        )
        officer_in_charge = officer_input("👨‍🌾 ผู้รับผิดชอบแปลง:", key="tab1_officer")

    with col2:
        field_name = st.text_input(
            "📍 ชื่อแปลง / ตำบล / หมู่บ้าน:", placeholder="ตัวอย่าง: แปลงบางขนาก A"
        )
        selected_district = st.selectbox(
            "📍 อำเภอ (จ.ฉะเชิงเทรา):", list(district_coords.keys())
        )

    with col3:
        rice_name = st.selectbox(
            f"🌾 สายพันธุ์ข้าวในแปลง ({len(rice_catalog)} สายพันธุ์):", list(rice_catalog.keys())
        )
        planting_method = st.selectbox("🚜 วิธีการปลูกข้าว:", planting_methods)

    with col4:
        sow_date = st.date_input("📅 วันที่เริ่มเพาะปลูก (วันหว่าน):", today())

    rice_warnings(rice_name, planting_method)

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("📅 คำนวณปฏิทินและบันทึกข้อมูลลงระบบ", type="primary", **STRETCH):
        if not farmer_name.strip() or not field_name.strip():
            st.error("⚠️ กรุณากรอก 'ชื่อเกษตรกร' และ 'ชื่อแปลง' ก่อนกดบันทึก")
        else:
            saved = save_to_db(
                farmer_name.strip(), selected_district, field_name.strip(),
                rice_name, planting_method, sow_date, officer_in_charge,
            )
            # เก็บผลไว้ใน session_state เพื่อไม่ให้ตารางหายเมื่อผู้ใช้แตะ widget อื่น
            st.session_state["tab1_last"] = {
                "saved": saved,
                "farmer": farmer_name.strip(),
                "field": field_name.strip(),
                "district": selected_district,
                "rice": rice_name,
                "sow_date": sow_date,
                "officer": officer_in_charge,
            }

    last = st.session_state.get("tab1_last")
    if last:
        if last["saved"]:
            st.success(f"💾 บันทึกและขึ้นทะเบียนแปลงในระบบเรียบร้อย! (อ.{last['district']})")
        else:
            st.warning("⚠️ แปลงนี้ถูกลงทะเบียนไว้แล้วในระบบ (ชื่อเกษตรกร + ชื่อแปลง + วันหว่านซ้ำ)")
        st.markdown(f"### 📋 ปฏิทินแนะนำสำหรับ: {last['farmer']} | {last['field']} (อ.{last['district']})")
        if last["officer"] and last["officer"] != "ไม่ระบุ":
            st.caption(f"👤 **ผู้รับผิดชอบแปลง:** {last['officer']}")
        new_df = get_rice_schedule(last["sow_date"], last["rice"], district_name=last["district"])
        st.dataframe(style_schedule(new_df), hide_index=True, **STRETCH)

history_df = load_db()

# --- TAB 2: บันทึกการตรวจแปลง ---
with tab2:
    st.subheader("🩺 บันทึกอาการ ปัญหา หรือข้อสังเกตจากการตรวจแปลง")
    if history_df.empty:
        st.info("ℹ️ ยังไม่มีข้อมูลแปลงในระบบ กรุณาขึ้นทะเบียนแปลงในแท็บแรกก่อน")
    else:
        record_options = {
            row["id"]: (
                f"{row['ชื่อเกษตรกร']} - อ.{row['อำเภอ']} - {row['ชื่อแปลง/ที่ตั้ง']}"
                f" (ผู้รับผิดชอบ: {row['ผู้รับผิดชอบแปลง']})"
            )
            for _, row in history_df.iterrows()
        }
        selected_id = st.selectbox(
            "เลือกแปลงนาที่ต้องการบันทึกอาการ/ปัญหา:",
            options=list(record_options.keys()),
            format_func=lambda x: record_options[x],
            key="tab2_select",
        )
        target = history_df[history_df["id"] == selected_id].iloc[0]
        cur_status = target["สถานะ/ปัญหาที่พบ"]
        cur_has_issue = bool(target["มีปัญหา"])
        cur_officer = target["ผู้รับผิดชอบแปลง"] or "ไม่ระบุ"

        st.info(
            f"📌 **สถานะปัจจุบัน:** {'⚠️ ' + str(cur_status) if cur_has_issue else '✅ ปกติ'}"
            f" | 👨‍🌾 **ผู้รับผิดชอบ:** {cur_officer}"
        )

        rice_type = rice_catalog.get(target["สายพันธุ์ข้าว"], "พันธุ์เบา")
        available_activities = [r["activity"] for r in activity_rules[rice_type]]
        v2 = st.session_state.get("tab2_ver", 0)  # เปลี่ยนค่าหลังบันทึก เพื่อล้างช่องกรอก

        col_a, col_b = st.columns(2)
        with col_a:
            problem_activity = st.selectbox(
                "📌 เลือกกิจกรรมที่เกี่ยวข้อง:",
                options=["ไม่ระบุ (ภาพรวมแปลง)"] + available_activities,
                key=f"tab2_problem_act_{selected_id}_{v2}",
            )
            has_issue = st.checkbox(
                "🚨 พบปัญหาในการตรวจครั้งนี้",
                value=cur_has_issue,
                key=f"tab2_issue_{selected_id}_{v2}",
                help="ไม่ติ๊ก = แปลงปกติ (สถานะจะถูกรีเซ็ตเป็น 'ปกติ')",
            )
            note_detail = st.text_area(
                "✍️ รายละเอียดอาการ/ปัญหา หรือข้อสังเกตการตรวจแปลง:",
                placeholder="ตัวอย่าง: พบเพลี้ยกระโดดสีน้ำตาลระบาดเล็กน้อยบริเวณกลางแปลง",
                key=f"tab2_note_{selected_id}_{v2}",
            )
        with col_b:
            updated_officer = officer_input(
                "👨‍🌾 ผู้รับผิดชอบแปลง (เปลี่ยนได้หากมีการเปลี่ยนมือ):",
                key=f"tab2_officer_{selected_id}_{v2}",
                current=cur_officer,
            )

        btn1, btn2, _ = st.columns([1, 1, 2])
        with btn1:
            if st.button("💾 บันทึกผลการตรวจแปลง", type="primary", **STRETCH):
                if has_issue and not note_detail.strip():
                    st.error("⚠️ กรุณากรอกรายละเอียดปัญหาที่พบ")
                else:
                    add_inspection(selected_id, updated_officer, problem_activity,
                                   note_detail.strip(), has_issue)
                    st.session_state["tab2_ver"] = v2 + 1
                    flash("success", "✅ บันทึกผลการตรวจเรียบร้อยแล้ว")
                    st.rerun()
        with btn2:
            if cur_has_issue and st.button("✅ ปัญหาแก้ไขแล้ว (รีเซ็ตเป็นปกติ)", **STRETCH):
                add_inspection(selected_id, updated_officer, "ไม่ระบุ (ภาพรวมแปลง)",
                               "แก้ไขปัญหาเรียบร้อย", False)
                st.session_state["tab2_ver"] = v2 + 1
                flash("success", "✅ รีเซ็ตสถานะแปลงเป็นปกติแล้ว")
                st.rerun()

        st.markdown("---")
        st.markdown("**🗂️ ประวัติการตรวจล่าสุดของแปลงนี้ (10 รายการ)**")
        insp_df = get_inspections(selected_id)
        if insp_df.empty:
            st.caption("ยังไม่มีประวัติการตรวจ")
        else:
            insp_df["ผล"] = insp_df["has_issue"].apply(lambda v: "⚠️ พบปัญหา" if v else "✅ ปกติ")
            st.dataframe(insp_df.drop(columns=["has_issue"]), hide_index=True, **STRETCH)

# --- TAB 3: ปรับเลื่อนวันทำกิจกรรม ---
with tab3:
    st.subheader("🔄 ปรับเลื่อนวันทำกิจกรรมและคำนวณปฏิทินใหม่")
    if history_df.empty:
        st.info("ℹ️ ยังไม่มีข้อมูลแปลงในระบบ กรุณาขึ้นทะเบียนแปลงในแท็บแรกก่อน")
    else:
        record_options_tab3 = {
            row["id"]: (
                f"{row['ชื่อเกษตรกร']} - อ.{row['อำเภอ']} - {row['ชื่อแปลง/ที่ตั้ง']}"
                f" (ข้าว: {row['สายพันธุ์ข้าว']})"
            )
            for _, row in history_df.iterrows()
        }
        selected_id_tab3 = st.selectbox(
            "เลือกแปลงนาที่ต้องการปรับเลื่อนวันทำกิจกรรม:",
            options=list(record_options_tab3.keys()),
            format_func=lambda x: record_options_tab3[x],
            key="tab3_select",
        )
        warn_unknown_species(history_df)
        t3 = history_df[history_df["id"] == selected_id_tab3].iloc[0]
        sow_d = parse_date(t3["วันที่เริ่มเพาะปลูก"])
        events3 = get_shift_events(selected_id_tab3)
        event_pairs = [(act, days) for _, act, days, _ in events3]

        if events3:
            st.warning(f"⏱️ **แปลงนี้มีการเลื่อนกิจกรรมแล้ว {len(events3)} ครั้ง** (แต่ละครั้งมีผลกับกิจกรรมนั้นและกิจกรรมถัดไป)")
            for ev_id, act, days, created in events3:
                c_info, c_btn = st.columns([5, 1])
                c_info.write(f"• `{act}` → {fmt_days(days)} (บันทึกเมื่อ {created})")
                if c_btn.button("↩️ ยกเลิก", key=f"undo_shift_{ev_id}"):
                    delete_shift_event(ev_id)
                    flash("success", "↩️ ยกเลิกการเลื่อนรายการนั้นแล้ว")
                    st.rerun()
        else:
            st.success("✅ แปลงนี้ยังไม่มีการเลื่อนกิจกรรม")

        rice_type3 = rice_catalog.get(t3["สายพันธุ์ข้าว"], "พันธุ์เบา")
        acts3 = [r["activity"] for r in activity_rules[rice_type3]]

        v3 = st.session_state.get("tab3_ver", 0)
        st.markdown("**➕ เพิ่มการเลื่อนกิจกรรมครั้งใหม่**")
        c1, c2 = st.columns(2)
        with c1:
            delayed_activity = st.selectbox(
                "📌 เลือกกิจกรรมที่เริ่มล่าช้ากว่ากำหนด:",
                options=acts3,
                index=1 if len(acts3) > 1 else 0,
                key=f"tab3_delayed_act_{selected_id_tab3}_{v3}",
            )
        with c2:
            shift_days = st.number_input(
                "🔄 เลื่อนออกไป (วัน) — มีผลกับกิจกรรมนี้และกิจกรรมถัดไป:",
                min_value=0, max_value=60, value=0,
                help="เช่น ใส่ 2 จะทำให้กิจกรรมที่เลือกและกิจกรรมถัดไปทั้งหมดเลื่อนช้าออกไป 2 วัน",
                key=f"tab3_shift_days_{selected_id_tab3}_{v3}",
            )

        preview_df = get_rice_schedule(
            sow_d, t3["สายพันธุ์ข้าว"], district_name=t3["อำเภอ"],
            events=event_pairs, extra_event=(delayed_activity, int(shift_days)),
        )
        st.markdown("---")
        if shift_days > 0:
            st.warning(f"🔍 **ตัวอย่างผลกระทบจากการเลื่อน '{delayed_activity}' ออกไป +{int(shift_days)} วัน:**")
        else:
            st.markdown("📋 **ปฏิทินกิจกรรมปัจจุบัน:**")
        st.dataframe(style_schedule(preview_df), hide_index=True, **STRETCH)

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🚀 ยืนยันการปรับเลื่อนปฏิทินกิจกรรม", type="primary", disabled=(shift_days == 0)):
            add_shift_event(selected_id_tab3, delayed_activity, int(shift_days))
            st.session_state["tab3_ver"] = v3 + 1
            flash("success", f"🔄 บันทึกการเลื่อนเรียบร้อย ('{delayed_activity}' ออกไป +{int(shift_days)} วัน)")
            st.rerun()

# --- TAB 4: แดชบอร์ด ---
with tab4:
    st.subheader("ภาพรวม")

    if history_df.empty:
        st.info("ℹ️ ปัจจุบันยังไม่มีข้อมูลแปลงนาในฐานข้อมูลส่วนกลาง")
    else:
        history_df["is_alert"] = history_df["มีปัญหา"].fillna(0).astype(int) == 1
        warn_unknown_species(history_df)

        # ตัวกรอง: ผู้รับผิดชอบ + ค้นหาชื่อเกษตรกร/แปลง
        ALL_OFFICERS = "ทั้งหมด (ทุกผู้รับผิดชอบ)"
        officers_list = [ALL_OFFICERS] + sorted(
            {x for x in history_df["ผู้รับผิดชอบแปลง"].dropna().unique() if str(x).strip()}
        )
        f1, f2, _ = st.columns([1, 1, 1])
        with f1:
            selected_manager = st.selectbox("ผู้รับผิดชอบ:", options=officers_list, key="tab4_manager_filter")
        with f2:
            keyword = st.text_input("🔎 ค้นหาชื่อเกษตรกร/ชื่อแปลง:", key="tab4_keyword").strip()

        df_filtered = history_df.copy()
        if selected_manager != ALL_OFFICERS:
            df_filtered = df_filtered[df_filtered["ผู้รับผิดชอบแปลง"] == selected_manager]
        if keyword:
            mask = (
                df_filtered["ชื่อเกษตรกร"].str.contains(keyword, case=False, na=False, regex=False)
                | df_filtered["ชื่อแปลง/ที่ตั้ง"].str.contains(keyword, case=False, na=False, regex=False)
            )
            df_filtered = df_filtered[mask]

        if df_filtered.empty:
            st.warning("ไม่พบข้อมูลแปลงตามเงื่อนไขที่เลือก")
        else:
            k1, k2, k3 = st.columns(3)
            k1.metric("👥 จำนวนเกษตรกร", f"{df_filtered['ชื่อเกษตรกร'].nunique()} คน")
            k2.metric("🌾 จำนวนแปลงทั้งหมด", f"{len(df_filtered)} แปลง")
            k3.metric("🚨 แปลงที่มีปัญหา", f"{int(df_filtered['is_alert'].sum())} แปลง")

            csv_bytes = df_filtered.drop(columns=["is_alert"]).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "⬇️ ดาวน์โหลดข้อมูลแปลง (CSV)", csv_bytes,
                file_name=f"rice_records_{today():%Y%m%d}.csv", mime="text/csv",
            )

            events_map = load_shift_events()
            st.markdown("---")

            # --- กิจกรรมที่ต้องทำในช่วงข้างหน้า (ข้ามทุกแปลงตามตัวกรอง) ---
            st.subheader("📆 กิจกรรมที่ต้องทำในช่วงข้างหน้า")
            horizon = st.slider("ดูล่วงหน้า (วัน)", min_value=3, max_value=30, value=7, key="tab4_horizon")
            limit_day = today() + timedelta(days=horizon)
            upcoming = []
            for _, r in df_filtered.iterrows():
                sched = get_rice_schedule(
                    parse_date(r["วันที่เริ่มเพาะปลูก"]), r["สายพันธุ์ข้าว"],
                    district_name=r["อำเภอ"], events=events_map.get(r["id"], []),
                )
                due = sched[(sched["_end"] >= today()) & (sched["_start"] <= limit_day)]
                for _, s in due.iterrows():
                    upcoming.append({
                        "_sort": s["_start"],
                        "เกษตรกร": r["ชื่อเกษตรกร"],
                        "แปลง": r["ชื่อแปลง/ที่ตั้ง"],
                        "อำเภอ": r["อำเภอ"],
                        "กิจกรรม": s["กิจกรรม"],
                        "ช่วงวันที่": s["วันที่ปรับใหม่"],
                        "ฝน/คำแนะนำ": s["โอกาสเกิดฝนและการประเมิน"]
                        + ("" if s["คำแนะนำ"] == "-" else f" | {s['คำแนะนำ']}"),
                    })
            if upcoming:
                up_df = pd.DataFrame(upcoming).sort_values("_sort").drop(columns=["_sort"])
                st.dataframe(up_df, hide_index=True, **STRETCH)
            else:
                st.caption(f"ไม่มีกิจกรรมที่ตรงกับช่วง {horizon} วันข้างหน้า")

            st.markdown("---")

            # --- รายละเอียดแปลงของเกษตรกรที่เลือก ---
            if st.session_state.get("selected_farmer_tab4") not in set(df_filtered["ชื่อเกษตรกร"]):
                st.session_state["selected_farmer_tab4"] = None

            target_farmer = st.session_state["selected_farmer_tab4"]
            if target_farmer:
                st.markdown(f"### 📋 รายละเอียดแปลงเพาะปลูก: {target_farmer}")
                farmer_df = df_filtered[df_filtered["ชื่อเกษตรกร"] == target_farmer].copy()
                farmer_df["อายุข้าว"] = farmer_df["วันที่เริ่มเพาะปลูก"].apply(calculate_rice_age)
                farmer_df["วันที่ปรับเลื่อน"] = farmer_df["จำนวนวันที่ปรับเลื่อนสะสม"].apply(
                    lambda x: fmt_days(int(x)) if pd.notna(x) and int(x) != 0 else "-"
                )
                farmer_df["ปัญหาที่พบ"] = farmer_df.apply(
                    lambda row: f"⚠️ {row['สถานะ/ปัญหาที่พบ']}" if row["is_alert"] else "✅ ปกติ", axis=1
                )
                display_df = farmer_df[[
                    "ชื่อแปลง/ที่ตั้ง", "อำเภอ", "สายพันธุ์ข้าว", "วันที่เริ่มเพาะปลูก",
                    "อายุข้าว", "วันที่ปรับเลื่อน", "กิจกรรมล่าสุดที่เลื่อน", "ปัญหาที่พบ",
                ]].copy()
                display_df.columns = [
                    "1. แปลงที่ (Plot No.)", "2. อำเภอ", "3. สายพันธุ์ข้าว", "4. วันเริ่มเพาะปลูก",
                    "5. อายุข้าว", "6. วันที่ปรับเลื่อน", "7. กิจกรรมที่เลื่อนล่าสุด", "8. ปัญหาที่พบ",
                ]
                st.dataframe(display_df, hide_index=True, **STRETCH)
                if st.button("❌ ปิดหน้าต่างรายละเอียด", type="secondary"):
                    st.session_state["selected_farmer_tab4"] = None
                    st.rerun()
                st.markdown("---")

            # --- การ์ดรายชื่อเกษตรกร ---
            st.subheader("แปลง (รายชื่อเกษตรกร)")
            cols = st.columns(3)
            for col_idx, (farmer_key, group) in enumerate(df_filtered.groupby("ชื่อเกษตรกร")):
                issue_count = int(group["is_alert"].sum())
                with cols[col_idx % 3]:
                    with st.container(border=True):
                        st.markdown(f"#### 👤 {farmer_key}")
                        st.write(f"**จำนวนแปลง:** {len(group)} แปลง")
                        if issue_count > 0:
                            st.markdown(
                                f"<div style='background-color: #fee2e2; color: #b91c1c; padding: 4px 8px; border-radius: 6px; display: inline-block; font-size: 14px; font-weight: bold;'>⚠️ พบปัญหา {issue_count} แปลง</div>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                "<div style='background-color: #d1fae5; color: #047857; padding: 4px 8px; border-radius: 6px; display: inline-block; font-size: 14px; font-weight: bold;'>✅ ปกติ</div>",
                                unsafe_allow_html=True,
                            )
                        st.write("")
                        if st.button("🔍 ดูรายละเอียดแปลง", key=f"btn_farmer_{farmer_key}", **STRETCH):
                            st.session_state["selected_farmer_tab4"] = farmer_key
                            st.rerun()

            st.markdown("---")

            # --- ค้นหาและจัดการแปลงเชิงลึก ---
            st.subheader("🔎 ค้นหาและจัดการแปลงนาเชิงลึก (รายแปลง)")
            record_map = {
                row["id"]: (
                    f"{row['ชื่อเกษตรกร']} - อ.{row['อำเภอ']} - {row['ชื่อแปลง/ที่ตั้ง']}"
                    f" (ผู้รับผิดชอบ: {row['ผู้รับผิดชอบแปลง']})"
                )
                for _, row in df_filtered.iterrows()
            }
            selected_dashboard_id = st.selectbox(
                "เลือกแปลงนาเพื่อเรียกดูปฏิทิน แก้ไข หรือลบข้อมูล:",
                options=list(record_map.keys()),
                format_func=lambda x: record_map[x],
                key="dashboard_select",
            )
            row = history_df[history_df["id"] == selected_dashboard_id].iloc[0]
            pid = int(selected_dashboard_id)
            sow_p = parse_date(row["วันที่เริ่มเพาะปลูก"])
            events_p = events_map.get(pid, [])

            st.markdown("### 🔔 รายงานกิจกรรมที่เกิดการเลื่อนล่าช้า")
            if events_p:
                lines = "\n".join(f"- **{act}** → {fmt_days(days)}" for act, days in events_p)
                st.warning(
                    f"⚠️ **แปลงนี้มีการแจ้งปรับเลื่อนปฏิทิน {len(events_p)} ครั้ง**\n\n{lines}\n\n"
                    "กิจกรรมตั้งแต่จุดที่เลื่อนเป็นต้นไปถูกปรับวันทำกิจกรรม (ไฮไลต์เหลืองด้านล่าง)"
                )
            else:
                st.success("✅ **แปลงนี้ดำเนินกิจกรรมตรงตามกำหนดเดิมทุกขั้นตอน (ไม่มีกิจกรรมเลื่อนวัน)**")

            st.markdown(
                f"🔮 **ตารางปฏิทินกิจกรรมปัจจุบัน (อำเภอ: {row['อำเภอ']} | ผู้รับผิดชอบ: {row['ผู้รับผิดชอบแปลง']})**"
            )
            rice_warnings(row["สายพันธุ์ข้าว"], row["วิธีการปลูก"])
            sched_p = get_rice_schedule(sow_p, row["สายพันธุ์ข้าว"], district_name=row["อำเภอ"], events=events_p)
            st.dataframe(style_schedule(sched_p), hide_index=True, **STRETCH)

            with st.expander("✏️ แก้ไขข้อมูลแปลง"):
                with st.form(f"edit_form_{pid}"):
                    e_farmer = st.text_input("ชื่อเกษตรกร", value=row["ชื่อเกษตรกร"])
                    e_field = st.text_input("ชื่อแปลง / ตำบล / หมู่บ้าน", value=row["ชื่อแปลง/ที่ตั้ง"])
                    d_list = list(district_coords.keys())
                    e_district = st.selectbox(
                        "อำเภอ", d_list, index=d_list.index(row["อำเภอ"]) if row["อำเภอ"] in d_list else 0)
                    r_list = list(rice_catalog.keys())
                    if row["สายพันธุ์ข้าว"] not in r_list:  # คงชื่อเดิมไว้เป็นตัวเลือกแรก ไม่เปลี่ยนโดยไม่ตั้งใจ
                        r_list = [row["สายพันธุ์ข้าว"]] + r_list
                    e_rice = st.selectbox(
                        "สายพันธุ์ข้าว", r_list,
                        index=r_list.index(row["สายพันธุ์ข้าว"]) if row["สายพันธุ์ข้าว"] in r_list else 0)
                    e_method = st.selectbox(
                        "วิธีการปลูก", planting_methods,
                        index=planting_methods.index(row["วิธีการปลูก"]) if row["วิธีการปลูก"] in planting_methods else 0)
                    e_sow = st.date_input("วันที่เริ่มเพาะปลูก", value=sow_p)
                    e_officer = st.text_input("ผู้รับผิดชอบแปลง", value=row["ผู้รับผิดชอบแปลง"] or "")
                    submitted = st.form_submit_button("💾 บันทึกการแก้ไข", type="primary")
                if submitted:
                    if not e_farmer.strip() or not e_field.strip():
                        st.error("⚠️ กรุณากรอกชื่อเกษตรกรและชื่อแปลง")
                    else:
                        result = update_record(pid, e_farmer.strip(), e_district, e_field.strip(),
                                               e_rice, e_method, e_sow, e_officer)
                        if result == "duplicate":
                            st.error("⚠️ มีแปลงอื่นที่ใช้ชื่อเกษตรกร + ชื่อแปลง + วันหว่านเดียวกันอยู่แล้ว")
                        else:
                            flash("success", "✅ แก้ไขข้อมูลแปลงเรียบร้อยแล้ว")
                            st.rerun()

            with st.expander("⚠️ การจัดการขั้นสูง"):
                st.warning("การลบจะลบประวัติการตรวจและการเลื่อนกิจกรรมของแปลงนี้ด้วย และไม่สามารถกู้คืนได้")
                confirm_delete = st.checkbox("ฉันยืนยันว่าต้องการลบแปลงนี้", key=f"confirm_del_{pid}")
                if st.button("🗑️ ลบแปลงนี้", type="primary", disabled=not confirm_delete, key=f"del_{pid}"):
                    delete_field_record(pid)
                    flash("success", f"ลบแปลงของ {row['ชื่อเกษตรกร']} แล้ว")
                    st.rerun()

                if os.path.exists(DB_FILE):
                    with open(DB_FILE, "rb") as f:
                        st.download_button("💽 สำรองฐานข้อมูล (.db)", f.read(),
                                           file_name=f"rice_records_backup_{today():%Y%m%d}.db")

# --- สถานะและเครดิตแหล่งข้อมูลพยากรณ์อากาศ ---
st.divider()
weather_err = st.session_state.get("_weather_err")
if weather_err:
    st.warning(f"⚠️ ดึงพยากรณ์อากาศไม่สำเร็จ ({weather_err}) ระบบจึงใช้สถิติรายเดือนแทน")
if WEATHER_SOURCE == "openmeteo":
    st.caption("ข้อมูลพยากรณ์อากาศ: [Open-Meteo.com](https://open-meteo.com/) (สัญญาอนุญาต CC BY 4.0)")
else:
    st.caption(
        "ข้อมูลพยากรณ์อากาศ: กรมอุตุนิยมวิทยา (data.tmd.go.th) ระดับจังหวัด ล่วงหน้า 7 วัน "
        "ค่าที่ใช้คือสัดส่วนพื้นที่ที่คาดว่ามีฝน (PercentRainCover) เป็นตัวแทนโอกาสฝน"
    )
