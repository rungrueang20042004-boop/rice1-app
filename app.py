import datetime
from datetime import timedelta
import sqlite3
import pandas as pd
import requests
import streamlit as st
import time

# ---------------------------------------------------------
# 1. การตั้งค่าหน้า Streamlit และฐานข้อมูล SQLite
# ---------------------------------------------------------
st.set_page_config(
    page_title="ระบบสนับสนุนที่ปรึกษาเกษตร จ.ฉะเชิงเทรา",
    page_icon="🌾",
    layout="wide",
)

DB_FILE = "rice_records.db"

def get_db_connection():
    """เชื่อมต่อฐานข้อมูลโดยกำหนด Timeout เพื่อป้องกันปัญหา Database is locked"""
    return sqlite3.connect(DB_FILE, timeout=10.0)

def init_db():
    """สร้าง/อัปเดตตารางในฐานข้อมูล SQLite ให้รองรับ field officer_in_charge"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
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
                accumulated_shift INTEGER DEFAULT 0,
                last_delayed_activity TEXT DEFAULT 'ไม่มี',
                updated_at TEXT NOT NULL
            )
        """)

        cursor.execute("PRAGMA table_info(rice_records)")
        columns = [column[1] for column in cursor.fetchall()]
        if "officer_in_charge" not in columns:
            cursor.execute(
                "ALTER TABLE rice_records ADD COLUMN officer_in_charge TEXT"
                " DEFAULT 'ไม่ระบุ'"
            )
        conn.commit()

init_db()

def save_to_db(farmer, district, field, rice, method, date_start, officer):
    sow_date_str = date_start.strftime("%Y-%m-%d")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    officer_str = officer.strip() if officer.strip() else "ไม่ระบุ"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM rice_records 
            WHERE farmer_name = ? AND field_name = ? AND sow_date = ?
            """,
            (farmer, field, sow_date_str),
        )

        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO rice_records (
                    farmer_name, district, field_name, rice_species, planting_method,
                    sow_date, officer_in_charge, created_at, status, accumulated_shift, last_delayed_activity, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ปกติ', 0, 'ไม่มี', ?)
                """,
                (
                    farmer, district, field, rice, method, sow_date_str,
                    officer_str, now_str, now_str,
                ),
            )
            conn.commit()
            return True
        return False

def update_field_status_only(record_id, new_status, new_officer=None):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT officer_in_charge FROM rice_records WHERE id = ?",
            (record_id,),
        )
        row = cursor.fetchone()
        if row:
            current_officer = row[0] or "ไม่ระบุ"
            officer_to_save = (
                new_officer.strip()
                if new_officer and new_officer.strip()
                else current_officer
            )

            cursor.execute(
                """
                UPDATE rice_records 
                SET status = ?, officer_in_charge = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, officer_to_save, now_str, record_id),
            )
            conn.commit()
            return True
    return False

def update_field_schedule(record_id, delayed_act_name, extra_shift_days):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT accumulated_shift FROM rice_records WHERE id = ?",
            (record_id,),
        )
        row = cursor.fetchone()
        if row:
            current_shift = row[0] or 0
            new_shift = current_shift + int(extra_shift_days)

            cursor.execute(
                """
                UPDATE rice_records 
                SET accumulated_shift = ?, last_delayed_activity = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_shift, delayed_act_name, now_str, record_id),
            )
            conn.commit()
            return True
    return False

def delete_field_record(record_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rice_records WHERE id = ?", (record_id,))
        conn.commit()
        return True

def load_db():
    with get_db_connection() as conn:
        query = """
            SELECT 
                id,
                farmer_name AS "ชื่อเกษตรกร",
                district AS "อำเภอ",
                field_name AS "ชื่อแปลง/ที่ตั้ง",
                rice_species AS "สายพันธุ์ข้าว",
                planting_method AS "วิธีการปลูก",
                sow_date AS "วันที่เริ่มเพาะปลูก",
                officer_in_charge AS "ผู้รับผิดชอบแปลง",
                created_at AS "วันบันทึกข้อมูล",
                status AS "สถานะ/ปัญหาที่พบ",
                accumulated_shift AS "จำนวนวันที่ปรับเลื่อนสะสม",
                last_delayed_activity AS "กิจกรรมล่าสุดที่เลื่อน",
                updated_at AS "วันที่อัปเดตล่าสุด"
            FROM rice_records
        """
        df = pd.read_sql_query(query, conn)
        return df

# ---------------------------------------------------------
# 2. ข้อมูลเชิงภูมิศาสตร์ และฐานข้อมูล 123 สายพันธุ์ข้าว (กรมการข้าว)
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

rice_catalog = {
    'กข1 (RD1)': 'พันธุ์หนัก', 'กข3 (RD3)': 'พันธุ์หนัก', 'กข5 (RD5)': 'พันธุ์หนัก',
    'กข7 (RD7)': 'พันธุ์หนัก', 'กข9 (RD9)': 'พันธุ์หนัก', 'กข11 (RD11)': 'พันธุ์หนัก',
    'กข13 (RD13)': 'พันธุ์หนัก', 'กข15 (RD15)': 'พันธุ์หนัก', 'กข17 (RD17)': 'พันธุ์หนัก',
    'กข19 (RD19)': 'พันธุ์หนัก', 'กข21 (RD21)': 'พันธุ์เบา', 'กข23 (RD23)': 'พันธุ์เบา',
    'กข25 (RD25)': 'พันธุ์หนัก', 'กข27 (RD27)': 'พันธุ์เบา', 'กข29 (ชัยนาท 80)': 'พันธุ์เบา',
    'กข31 (ปทุมธานี 80)': 'พันธุ์เบา', 'กข33 (หอมอุบล 80)': 'พันธุ์เบา', 'กข35 (RD35)': 'พันธุ์เบา',
    'กข37 (RD37)': 'พันธุ์เบา', 'กข39 (RD39)': 'พันธุ์หนัก', 'กข41 (RD41)': 'พันธุ์เบา',
    'กข43 (RD43)': 'พันธุ์เบา', 'กข45 (RD45)': 'พันธุ์เบา', 'กข47 (RD47)': 'พันธุ์เบา',
    'กข49 (RD49)': 'พันธุ์เบา', 'กข51 (RD51)': 'พันธุ์เบา', 'กข53 (RD53)': 'พันธุ์เบา',
    'กข55 (RD55)': 'พันธุ์เบา', 'กข57 (RD57)': 'พันธุ์เบา', 'กข59 (RD59)': 'พันธุ์เบา',
    'กข61 (RD61)': 'พันธุ์เบา', 'กข63 (RD63)': 'พันธุ์เบา', 'กข65 (RD65)': 'พันธุ์เบา',
    'กข67 (RD67)': 'พันธุ์เบา', 'กข69 (RD69)': 'พันธุ์เบา', 'กข71 (RD71)': 'พันธุ์เบา',
    'กข73 (RD73)': 'พันธุ์เบา', 'กข75 (RD75)': 'พันธุ์เบา', 'กข77 (RD77)': 'พันธุ์เบา',
    'กข79 (RD79)': 'พันธุ์หนัก', 'กข81 (RD81)': 'พันธุ์เบา', 'กข83 (RD83)': 'พันธุ์หนัก',
    'กข85 (RD85)': 'พันธุ์หนัก', 'กข87 (RD87)': 'พันธุ์เบา', 'กข89 (RD89)': 'พันธุ์เบา',
    'กข91 (RD91)': 'พันธุ์เบา', 'กข93 (RD93)': 'พันธุ์เบา', 'กข95 (RD95)': 'พันธุ์เบา',
    'กข97 (RD97)': 'พันธุ์เบา', 'กข99 (RD99)': 'พันธุ์เบา', 'กข101 (RD101)': 'พันธุ์เบา',
    'กข103 (RD103)': 'พันธุ์เบา', 'กข105 (RD105)': 'พันธุ์เบา', 'กข107 (RD107)': 'พันธุ์เบา',
    'ขาวดอกมะลิ 105 (KDML 105)': 'พันธุ์หนัก', 'ปทุมธานี 1': 'พันธุ์เบา', 'สุพรรณบุรี 1': 'พันธุ์หนัก',
    'สุพรรณบุรี 2': 'พันธุ์เบา', 'สุพรรณบุรี 3': 'พันธุ์เบา', 'สุพรรณบุรี 60': 'พันธุ์หนัก',
    'สุพรรณบุรี 90': 'พันธุ์หนัก', 'ชัยนาท 1': 'พันธุ์หนัก', 'ชัยนาท 2': 'พันธุ์เบา',
    'พิษณุโลก 1': 'พันธุ์หนัก', 'พิษณุโลก 2': 'พันธุ์หนัก', 'พิษณุโลก 60-1': 'พันธุ์หนัก',
    'พิษณุโลก 80': 'พันธุ์เบา', 'พัทลุง 60': 'พันธุ์หนัก', 'ปราจีนบุรี 1': 'พันธุ์หนัก',
    'ปราจีนบุรี 2': 'พันธุ์หนัก', 'ชุมแพ 60': 'พันธุ์หนัก', 'เชียงใหม่ 60': 'พันธุ์หนัก',
    'แก่นจันทร์': 'พันธุ์หนัก', 'คลองหลวง 1': 'พันธุ์เบา', 'หอมสุพรรณบุรี': 'พันธุ์หนัก',
    'หอมคลองหลวง 1': 'พันธุ์เบา', 'หอมปทุม': 'พันธุ์เบา', 'หอมจันท์': 'พันธุ์หนัก',
    'หอมนางแก้ว': 'พันธุ์หนัก', 'หอมชลสิทธิ์': 'พันธุ์หนัก', 'หอมมาลี': 'พันธุ์หนัก',
    'ขาวตาแห้ง 17': 'พันธุ์หนัก', 'ขาวปากหม้อ 148': 'พันธุ์หนัก', 'นางพญา 132': 'พันธุ์หนัก',
    'พลายงาม พธ.60': 'พันธุ์หนัก', 'เหนียวกระทัง 148': 'พันธุ์หนัก', 'ตะเภาแก้ว 161': 'พันธุ์หนัก',
    'เจ๊กเชย 1 เสาไห้': 'พันธุ์หนัก', 'ปิ่นแก้ว 56': 'พันธุ์หนัก', 'สังข์หยดพัทลุง': 'พันธุ์หนัก',
    'เล็บนกปัตตานี': 'พันธุ์หนัก', 'เฉี้ยงพัทลุง': 'พันธุ์หนัก', 'พวงไร่ 2': 'พันธุ์หนัก',
    'พวงเงินพวงทอง': 'พันธุ์หนัก', 'อัลฮัมดุลิลลาฮ์ 4': 'พันธุ์หนัก', 'เจ้าฮ่อ': 'พันธุ์หนัก',
    'ทับทิมชุมพร': 'พันธุ์เบา', 'ไรซ์เบอร์รี่': 'พันธุ์หนัก', 'หอมดอย': 'พันธุ์หนัก',
    'บือโป๊ะโละ': 'พันธุ์หนัก', 'เจ้าลอย': 'พันธุ์หนัก', 'แดงดอ': 'พันธุ์เบา',
    'ก้องกลาง': 'พันธุ์เบา', 'เหลืองทอง': 'พันธุ์หนัก', 'ข้าวเจ้าหอมมะลิทุ่งกุลา': 'พันธุ์หนัก',
    'เบอร์ 5451': 'พันธุ์เบา'
}

planting_methods = ["หว่านน้ำตม", "หว่านแห้ง / หว่านสำรวย", "ปักดำ / ดำนา"]

activity_rules = {
    "พันธุ์เบา": [
        {"day": 0, "activity": "🌾 วันเริ่มเพาะปลูก/หว่านข้าว", "is_spray": False},
        {"day": 2, "activity": "💧 ระยะคุมเลน (0-4 วัน)", "is_spray": True},
        {"day": 9, "activity": "🌿 ระยะคุมฆ่า (7-12 วัน)", "is_spray": True},
        {"day": 16, "activity": "🌱 หว่านปุ๋ยรอบที่ 1 (15-18 วัน)", "is_spray": False},
        {"day": 21, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 1 (20-23 วัน)", "is_spray": True},
        {"day": 32, "activity": "🌾 หว่านปุ๋ยรอบที่ 2 (30-35 วัน)", "is_spray": False},
        {"day": 35, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 2 (33-38 วัน)", "is_spray": True},
        {"day": 47, "activity": "🌾 หว่านปุ๋ยรอบที่ 3 (45-50 วัน)", "is_spray": False},
        {"day": 52, "activity": "🌸 ระยะกัดหางปลาทู (50-55 วัน)", "is_spray": False},
        {"day": 70, "activity": "🌾 ระยะข้าวก้ม (70 วัน)", "is_spray": False},
        {"day": 95, "activity": "🚜 วันเก็บเกี่ยวโดยประมาณ", "is_spray": False},
    ],
    "พันธุ์หนัก": [
        {"day": 0, "activity": "🌾 วันเริ่มเพาะปลูก/หว่านข้าว", "is_spray": False},
        {"day": 2, "activity": "💧 ระยะคุมเลน (0-4 วัน)", "is_spray": True},
        {"day": 9, "activity": "🌿 ระยะคุมฆ่า (7-12 วัน)", "is_spray": True},
        {"day": 22, "activity": "🌱 หว่านปุ๋ยรอบที่ 1 (20-25 วัน)", "is_spray": False},
        {"day": 26, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 1 (25-28 วัน)", "is_spray": True},
        {"day": 47, "activity": "🌾 หว่านปุ๋ยรอบที่ 2 (45-50 วัน)", "is_spray": False},
        {"day": 50, "activity": "🐛 พ่นยาหลังปุ๋ยรอบที่ 2 (48-53 วัน)", "is_spray": True},
        {"day": 72, "activity": "🌾 หว่านปุ๋ยรอบที่ 3 (70-75 วัน)", "is_spray": False},
        {"day": 80, "activity": "🌸 ระยะกัดหางปลาทู (75-85 วัน)", "is_spray": False},
        {"day": 100, "activity": "🌾 ระยะข้าวก้ม (100 วัน)", "is_spray": False},
        {"day": 120, "activity": "🚜 วันเก็บเกี่ยวโดยประมาณ", "is_spray": False},
    ],
}

chachoengsao_climatology = {
    1: 10, 2: 15, 3: 25, 4: 40, 5: 60, 6: 55,
    7: 60, 8: 65, 9: 75, 10: 60, 11: 30, 12: 10,
}

# ---------------------------------------------------------
# 3. ฟังก์ชันดึงข้อมูลพยากรณ์อากาศ และคำนวณตารางกิจกรรม
# ---------------------------------------------------------
@st.cache_data(ttl=3600)
def fetch_district_weather(district_name):
    coords = district_coords.get(
        district_name, {"lat": 13.690, "lon": 101.070}
    )
    url = f"https://api.open-meteo.com/v1/forecast?latitude={coords['lat']}&longitude={coords['lon']}&daily=precipitation_probability_max&forecast_days=14&timezone=Asia%2FBangkok"
    try:
        res = requests.get(url, timeout=5)
        res.raise_for_status() # ตรวจสอบ Status Code ป้องกันหน้าเว็บตอบกลับแปลกๆ
        data = res.json()
        return {
            data["daily"]["time"][i]: data["daily"]["precipitation_probability_max"][i]
            for i in range(len(data["daily"]["time"]))
        }
    except Exception:
        return {}

def get_rice_schedule_advanced(
    sow_date,
    rice_name,
    district_name="เมืองฉะเชิงเทรา",
    base_accum_shift=0,
    delayed_act_name="ไม่มี",
    new_delay_days=0,
):
    rice_type = rice_catalog.get(rice_name, "พันธุ์เบา")
    rules = activity_rules[rice_type]
    weather_forecast = fetch_district_weather(district_name)
    schedule = []

    all_act_names = [r["activity"] for r in rules]
    target_idx = (
        all_act_names.index(delayed_act_name)
        if delayed_act_name in all_act_names
        else -1
    )

    weather_carryover = 0

    for idx, rule in enumerate(rules):
        original_act_date = sow_date + timedelta(days=rule["day"])

        if target_idx != -1 and idx >= target_idx:
            active_shift = base_accum_shift + new_delay_days
        else:
            active_shift = 0 if target_idx != -1 else base_accum_shift

        active_shift += weather_carryover
        is_shifted = active_shift > 0

        act_date = sow_date + timedelta(days=rule["day"] + active_shift)
        date_str = act_date.strftime("%Y-%m-%d")

        shift_note = (
            "ตรงตามกำหนดเดิม" if active_shift == 0 else f"+{active_shift} วัน"
        )
        if is_shifted and active_shift > 0:
            shift_note += f" (เริ่มเลื่อนที่: {delayed_act_name if target_idx != -1 else 'ขยับวัน'})"

        if date_str in weather_forecast:
            rain_chance = weather_forecast[date_str]
            if rule["is_spray"] and rain_chance > 60:
                act_date = act_date + timedelta(days=2)
                active_shift += 2
                weather_carryover += 2
                shift_note = f"+{active_shift} วัน (เลื่อนหลบฝน +2 วัน)"
                status_text = f"⚡ ฝนสด {rain_chance}% ({district_name}) ⚠️ ฝนตกหนัก เลื่อนหลบฝน"
                schedule.append({
                    "กิจกรรม": rule["activity"],
                    "วันตามกำหนดเดิม": original_act_date.strftime("%d/%m/%Y"),
                    "วันที่ปรับใหม่": act_date.strftime("%d/%m/%Y"),
                    "การปรับเปลี่ยน": shift_note,
                    "โอกาสเกิดฝนและการประเมิน": status_text,
                    "_danger": True,
                    "_shifted": True,
                })
                continue
            else:
                status_text = (
                    f"⚡ {rain_chance}% [พยากรณ์สด อ.{district_name}]"
                )
        else:
            rain_chance = chachoengsao_climatology[act_date.month]
            status_text = f"📊 {rain_chance}% (สถิติรายเดือน)"
            if rule["is_spray"] and rain_chance > 60:
                status_text += " ⚠️ ช่วงนี้ฝนชุก"
                schedule.append({
                    "กิจกรรม": rule["activity"],
                    "วันตามกำหนดเดิม": original_act_date.strftime("%d/%m/%Y"),
                    "วันที่ปรับใหม่": act_date.strftime("%d/%m/%Y"),
                    "การปรับเปลี่ยน": shift_note,
                    "โอกาสเกิดฝนและการประเมิน": status_text,
                    "_danger": True,
                    "_shifted": is_shifted,
                })
                continue

        schedule.append({
            "กิจกรรม": rule["activity"],
            "วันตามกำหนดเดิม": original_act_date.strftime("%d/%m/%Y"),
            "วันที่ปรับใหม่": act_date.strftime("%d/%m/%Y"),
            "การปรับเปลี่ยน": shift_note,
            "โอกาสเกิดฝนและการประเมิน": status_text,
            "_danger": False,
            "_shifted": is_shifted,
        })
    return pd.DataFrame(schedule)

def is_alert_status(status_val):
    status_str = str(status_val).strip()
    return (
        status_str != ""
        and "ปกติ" not in status_str
        and "None" not in status_str
        and status_str.lower() != "nan"
    )

def calculate_rice_age(sow_date_str):
    try:
        sow_date = datetime.datetime.strptime(
            str(sow_date_str), "%Y-%m-%d"
        ).date()
        today = datetime.date.today()
        age_days = (today - sow_date).days
        if age_days < 0:
            return f"ยังไม่ถึงวันปลูก (อีก {-age_days} วัน)"
        return f"{age_days} วัน"
    except Exception:
        return "-"

# ---------------------------------------------------------
# 4. ส่วนเชื่อมต่อผู้ใช้ (User Interface - 4 Tabs)
# ---------------------------------------------------------
st.markdown(
    "<h1 style='text-align: center; color: #1e7e34;'>🌾 ระบบสนับสนุนการตัดสินใจและวางแผนตรวจเยี่ยมแปลงนา จ.ฉะเชิงเทรา</h1>",
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='text-align: center; color: #555;'>เครื่องมือส่วนกลางสำหรับเจ้าหน้าที่และที่ปรึกษาทางการเกษตรประจำแผนก</p>",
    unsafe_allow_html=True,
)

tab1, tab2, tab3, tab4 = st.tabs([
    "📝 ขึ้นทะเบียนแปลงในความดูแล",
    "🩺 บันทึกการตรวจแปลงและพบปัญหา",
    "🔄 ปรับเลื่อนวันทำกิจกรรม/ปฏิทิน",
    "📊 แดชบอร์ดสรุปภาพรวมและจัดการแปลง",
])

# --- TAB 1: บันทึกข้อมูลแปลงนา ---
with tab1:
    st.subheader("บันทึกข้อมูลแปลงนาเกษตรกรรายใหม่")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        farmer_name = st.text_input(
            "👤 ชื่อเกษตรกรผู้รับคำปรึกษา:",
            placeholder="ตัวอย่าง: นายสมศักดิ์ รักดี",
        )
        officer_in_charge = st.text_input(
            "👨‍🌾 ผู้รับผิดชอบแปลง:",
            placeholder="ตัวอย่าง: นายรุ่งเรือง จรรยา",
        )

    with col2:
        field_name = st.text_input(
            "📍 ชื่อแปลง / ตำบล / หมู่บ้าน:",
            placeholder="ตัวอย่าง: แปลงบางขนาก A",
        )
        selected_district = st.selectbox(
            "📍 อำเภอ (จ.ฉะเชิงเทรา):", list(district_coords.keys())
        )

    with col3:
        rice_name = st.selectbox(
            "🌾 สายพันธุ์ข้าวในแปลง (123 สายพันธุ์):", list(rice_catalog.keys())
        )
        planting_method = st.selectbox(
            "🚜 วิธีการปลูกข้าว:", planting_methods
        )

    with col4:
        sow_date = st.date_input(
            "📅 วันที่เริ่มเพาะปลูก (วันหว่าน):", datetime.date.today()
        )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button(
        "📅 คำนวณปฏิทินและบันทึกข้อมูลลงระบบ",
        type="primary",
        use_container_width=True,
    ):
        if not farmer_name or not field_name:
            st.error("⚠️ กรุณากรอก 'ชื่อเกษตรกร' และ 'ชื่อแปลง' ก่อนกดบันทึก")
        else:
            if save_to_db(
                farmer_name,
                selected_district,
                field_name,
                rice_name,
                planting_method,
                sow_date,
                officer_in_charge,
            ):
                st.success(
                    f"💾 บันทึกและขึ้นทะเบียนแปลงในระบบเรียบร้อย! (อ.{selected_district})"
                )
            else:
                st.warning("⚠️ แปลงนานี้ถูกลงทะเบียนไว้แล้วในระบบ")

            df = get_rice_schedule_advanced(
                sow_date, rice_name, district_name=selected_district
            )
            st.markdown(
                f"### 📋 ปฏิทินแนะนำสำหรับผู้ใช้: {farmer_name} | {field_name} (อ.{selected_district})"
            )
            if officer_in_charge.strip():
                st.caption(
                    f"👤 **ผู้รับผิดชอบแปลง:** {officer_in_charge.strip()}"
                )

            show_df = df.drop(columns=["_danger", "_shifted"])

            def highlight_rows(row):
                return [
                    "background-color: #ffebee; font-weight: bold"
                    if df.loc[row.name, "_danger"]
                    else ""
                    for _ in row
                ]

            st.dataframe(
                show_df.style.apply(highlight_rows, axis=1),
                use_container_width=True,
                hide_index=True,
            )

history_df = load_db()

# --- TAB 2: บันทึกการตรวจแปลงและพบปัญหา ---
with tab2:
    st.subheader("🩺 บันทึกอาการ ปัญหา หรือข้อสังเกตจากการตรวจแปลง")
    if history_df.empty:
        st.info(
            "ℹ️ ยังไม่มีข้อมูลแปลงในระบบ กรุณาขึ้นทะเบียนแปลงในแท็บแรกก่อน"
        )
    else:
        record_options = {
            row["id"]: (
                f"{row['ชื่อเกษตรกร']} - อ.{row['อำเภอ']} -"
                f" {row['ชื่อแปลง/ที่ตั้ง']} (ผู้รับผิดชอบ: {row['ผู้รับผิดชอบแปลง']})"
            )
            for _, row in history_df.iterrows()
        }
        selected_id = st.selectbox(
            "เลือกแปลงนาที่ต้องการบันทึกอาการ/ปัญหา:",
            options=list(record_options.keys()),
            format_func=lambda x: record_options[x],
            key="tab2_select",
        )

        target_field = history_df[history_df["id"] == selected_id].iloc[0]
        current_status = target_field["สถานะ/ปัญหาที่พบ"]
        current_officer = target_field.get("ผู้รับผิดชอบแปลง", "ไม่ระบุ")

        st.info(
            f"📌 **สถานะ/ปัญหาปัจจุบัน:** {current_status} | 👨‍🌾 **ผู้รับผิดชอบ:**"
            f" {current_officer}"
        )

        rice_type = rice_catalog.get(target_field["สายพันธุ์ข้าว"], "พันธุ์เบา")
        available_activities = [
            item["activity"] for item in activity_rules[rice_type]
        ]

        col_a, col_b = st.columns(2)
        with col_a:
            problem_activity = st.selectbox(
                "📌 เลือกกิจกรรมที่พบอาการ/ปัญหา:",
                options=["ไม่ระบุ (ภาพรวมแปลง)"] + available_activities,
                key="tab2_problem_act",
            )
            note_detail = st.text_area(
                "✍️ รายละเอียดอาการ/ปัญหา หรือข้อสังเกตการตรวจแปลง:",
                placeholder="ตัวอย่าง: พบเพลี้ยกระโดดสีน้ำตาลระบาดเล็กน้อยบริเวณกลางแปลง",
                key="tab2_note",
            )

            act_prefix = (
                f"[{problem_activity}] "
                if problem_activity != "ไม่ระบุ (ภาพรวมแปลง)"
                else ""
            )
            final_status_text = (
                f"{act_prefix}{note_detail}".strip()
                if note_detail
                else current_status
            )
            st.caption(
                f"📝 **ข้อความที่จะถูกบันทึกลงฐานข้อมูล:** `{final_status_text}`"
            )

        with col_b:
            updated_officer = st.text_input(
                "👨‍🌾 เปลี่ยนผู้รับผิดชอบแปลง (หากมีการเปลี่ยนมือ):",
                value=current_officer,
                key="tab2_officer",
            )

        if st.button("💾 บันทึกผลการตรวจแปลง", type="primary"):
            if update_field_status_only(
                selected_id, final_status_text, updated_officer
            ):
                st.success("✅ บันทึกอาการและสถานะปัญหาเรียบร้อยแล้ว!")
                st.rerun()

# --- TAB 3: ปรับเลื่อนวันทำกิจกรรม/ปฏิทิน ---
with tab3:
    st.subheader("🔄 ปรับเลื่อนวันทำกิจกรรมและคำนวณปฏิทินใหม่")
    if history_df.empty:
        st.info(
            "ℹ️ ยังไม่มีข้อมูลแปลงในระบบ กรุณาขึ้นทะเบียนแปลงในแท็บแรกก่อน"
        )
    else:
        record_options_tab3 = {
            row["id"]: (
                f"{row['ชื่อเกษตรกร']} - อ.{row['อำเภอ']} -"
                f" {row['ชื่อแปลง/ที่ตั้ง']} (ข้าว: {row['สายพันธุ์ข้าว']})"
            )
            for _, row in history_df.iterrows()
        }
        selected_id_tab3 = st.selectbox(
            "เลือกแปลงนาที่ต้องการปรับเลื่อนวันทำกิจกรรม:",
            options=list(record_options_tab3.keys()),
            format_func=lambda x: record_options_tab3[x],
            key="tab3_select",
        )

        target_field_tab3 = history_df[
            history_df["id"] == selected_id_tab3
        ].iloc[0]
        raw_shift_val = target_field_tab3["จำนวนวันที่ปรับเลื่อนสะสม"]
        current_shift = 0 if pd.isna(raw_shift_val) else int(raw_shift_val)
        last_delayed_act = target_field_tab3.get(
            "กิจกรรมล่าสุดที่เลื่อน", "ไม่มี"
        )

        st.warning(
            f"⏱️ **วันเลื่อนสะสมปัจจุบัน:** +{current_shift} วัน | 📌 **กิจกรรมที่เริ่มเลื่อนล่าสุด:** `{last_delayed_act}`"
        )

        try:
            sow_d = datetime.datetime.strptime(
                str(target_field_tab3["วันที่เริ่มเพาะปลูก"]), "%Y-%m-%d"
            ).date()
        except Exception:
            sow_d = datetime.date.today()

        rice_type_tab3 = rice_catalog.get(target_field_tab3["สายพันธุ์ข้าว"], "พันธุ์เบา")
        available_activities_tab3 = [
            item["activity"] for item in activity_rules[rice_type_tab3]
        ]

        col_shift1, col_shift2 = st.columns(2)
        with col_shift1:
            delayed_activity = st.selectbox(
                "📌 เลือกกิจกรรมที่เริ่มล่าช้ากว่ากำหนด:",
                options=available_activities_tab3,
                index=1,
                key="tab3_delayed_act",
            )
        with col_shift2:
            shift_days = st.number_input(
                "🔄 ปรับขยับวันเพิ่มเฉพาะกิจกรรมนี้และกิจกรรมถัดไป (วัน):",
                min_value=-30,
                max_value=60,
                value=0,
                help=(
                    "เช่น ใส่เลข 2"
                    " จะทำให้กิจกรรมที่เลือกและกิจกรรมถัดไปเลื่อนช้าออกไป 2 วัน"
                ),
                key="tab3_shift_days",
            )

        preview_df = get_rice_schedule_advanced(
            sow_d,
            target_field_tab3["สายพันธุ์ข้าว"],
            district_name=target_field_tab3["อำเภอ"],
            base_accum_shift=current_shift,
            delayed_act_name=delayed_activity
            if shift_days != 0
            else last_delayed_act,
            new_delay_days=shift_days,
        )

        st.markdown("---")
        if shift_days != 0:
            st.warning(
                "🔍 **ตัวอย่างผลกระทบจากการเลื่อนกิจกรรม"
                f" '{delayed_activity}' ออกไป +{shift_days} วัน:**"
            )
        else:
            st.markdown("📋 **ปฏิทินกิจกรรมปัจจุบัน:**")

        show_preview = preview_df[[
            "กิจกรรม",
            "วันตามกำหนดเดิม",
            "วันที่ปรับใหม่",
            "การปรับเปลี่ยน",
            "โอกาสเกิดฝนและการประเมิน",
        ]]

        def highlight_preview(row):
            is_shifted = preview_df.loc[row.name, "_shifted"]
            if is_shifted and (shift_days != 0 or current_shift > 0):
                return [
                    "background-color: #fff9c4; font-weight: bold" for _ in row
                ]
            return ["" for _ in row]

        st.dataframe(
            show_preview.style.apply(highlight_preview, axis=1),
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🚀 ยืนยันการปรับเลื่อนปฏิทินกิจกรรม", type="primary"):
            if update_field_schedule(
                selected_id_tab3,
                delayed_activity if shift_days != 0 else last_delayed_act,
                shift_days,
            ):
                st.success(
                    "🔄 บันทึกการเลื่อนกิจกรรมเรียบร้อย!"
                    f" ('{delayed_activity}' ออกไปอีก +{shift_days} วัน)"
                )
                st.rerun()

# --- TAB 4: แดชบอร์ดสรุปภาพรวมและจัดการแปลง ---
with tab4:
    st.subheader("ภาพรวม")
    
    if history_df.empty:
        st.info("ℹ️ ปัจจุบันยังไม่มีข้อมูลแปลงนาในฐานข้อมูลส่วนกลาง")
    else:
        # เตรียมคอลัมน์ประเมินปัญหา
        history_df["is_alert"] = history_df["สถานะ/ปัญหาที่พบ"].apply(is_alert_status)
        
        # 1. Responsible Person Filter (ผู้รับผิดชอบ)
        officers_list = ["ทั้งหมด (ทุกผู้รับผิดชอบ)"] + sorted(
            [x for x in history_df["ผู้รับผิดชอบแปลง"].unique() if x and x.strip() != ""]
        )
        
        col_filter, _ = st.columns([1, 2])
        with col_filter:
            selected_manager = st.selectbox("ผู้รับผิดชอบ:", options=officers_list, key="tab4_manager_filter")

        # กรองข้อมูลตามที่เลือก
        if selected_manager != "ทั้งหมด (ทุกผู้รับผิดชอบ)":
            df_filtered = history_df[history_df["ผู้รับผิดชอบแปลง"] == selected_manager].copy()
        else:
            df_filtered = history_df.copy()

        # กรณีค้นหาแล้วไม่มีข้อมูลเลย
        if df_filtered.empty:
            st.warning("ไม่มีข้อมูลแปลงสำหรับผู้รับผิดชอบรายนี้")
        else:
            # 2. KPI Overview Cards (3 Cards)
            total_farmers = df_filtered["ชื่อเกษตรกร"].nunique()
            total_plots = len(df_filtered)
            plots_with_issues = df_filtered["is_alert"].sum()

            kpi1, kpi2, kpi3 = st.columns(3)
            with kpi1:
                st.metric("👥 จำนวนเกษตรกร", f"{total_farmers} คน")
            with kpi2:
                st.metric("🌾 จำนวนแปลงทั้งหมด", f"{total_plots} แปลง")
            with kpi3:
                st.metric("🚨 แปลงที่มีปัญหา", f"{plots_with_issues} แปลง", delta_color="inverse")

            st.markdown("---")
            
            # --- 3. Interactive Modal Detail View (พื้นที่สำหรับแสดงตารางรายละเอียดเมื่อกดดู) ---
            if "selected_farmer_tab4" not in st.session_state:
                st.session_state.selected_farmer_tab4 = None

            if st.session_state.selected_farmer_tab4:
                target_farmer = st.session_state.selected_farmer_tab4
                st.markdown(f"### 📋 รายละเอียดแปลงเพาะปลูก: {target_farmer}")
                
                # กรองข้อมูลเฉพาะเกษตรกรที่ถูกเลือก
                farmer_df = df_filtered[df_filtered["ชื่อเกษตรกร"] == target_farmer].copy()
                
                # เตรียม 8 คอลัมน์ตาม Wireframe
                farmer_df["อายุข้าว"] = farmer_df["วันที่เริ่มเพาะปลูก"].apply(calculate_rice_age)
                farmer_df["วันที่ปรับเลื่อน"] = farmer_df["จำนวนวันที่ปรับเลื่อนสะสม"].apply(
                    lambda x: f"+{int(x)} วัน" if pd.notna(x) and int(x) > 0 else "-"
                )
                farmer_df["ปัญหาที่พบ"] = farmer_df.apply(
                    lambda row: f"⚠️ {row['สถานะ/ปัญหาที่พบ']}" if row["is_alert"] else "✅ ปกติ", axis=1
                )
                
                display_cols = [
                    "ชื่อแปลง/ที่ตั้ง", "อำเภอ", "สายพันธุ์ข้าว", 
                    "วันที่เริ่มเพาะปลูก", "อายุข้าว", "วันที่ปรับเลื่อน", 
                    "กิจกรรมล่าสุดที่เลื่อน", "ปัญหาที่พบ"
                ]
                
                display_df = farmer_df[display_cols].copy()
                display_df.columns = [
                    "1. แปลงที่ (Plot No.)", "2. อำเภอ", "3. สายพันธุ์ข้าว", 
                    "4. วันเริ่มเพาะปลูก", "5. อายุข้าว", "6. วันที่ปรับเลื่อน", 
                    "7. กิจกรรมที่เลื่อน", "8. ปัญหาที่พบ"
                ]
                
                st.dataframe(display_df, use_container_width=True, hide_index=True)
                
                if st.button("❌ ปิดหน้าต่างรายละเอียด", type="secondary"):
                    st.session_state.selected_farmer_tab4 = None
                    st.rerun()
                    
                st.markdown("---")

            # --- 4. Farmer Grid List (แปลง) ---
            st.subheader("แปลง (รายชื่อเกษตรกร)")
            
            # จัดกลุ่มตามเกษตรกร
            farmers_group = df_filtered.groupby("ชื่อเกษตรกร")
            
            cols = st.columns(3)
            col_idx = 0
            
            for farmer_name_group, group in farmers_group:
                plot_count = len(group)
                issue_count = group["is_alert"].sum()
                
                with cols[col_idx % 3]:
                    # สร้าง UI แบบ Card
                    with st.container(border=True):
                        st.markdown(f"#### 👤 {farmer_name_group}")
                        st.write(f"**จำนวนแปลง:** {plot_count} แปลง")
                        
                        if issue_count > 0:
                            # Warning badge HTML style
                            st.markdown(f"<div style='background-color: #fee2e2; color: #b91c1c; padding: 4px 8px; border-radius: 6px; display: inline-block; font-size: 14px; font-weight: bold;'>⚠️ พบปัญหา {issue_count} แปลง</div>", unsafe_allow_html=True)
                        else:
                            # Normal badge HTML style
                            st.markdown(f"<div style='background-color: #d1fae5; color: #047857; padding: 4px 8px; border-radius: 6px; display: inline-block; font-size: 14px; font-weight: bold;'>✅ ปกติ</div>", unsafe_allow_html=True)
                        
                        st.write("") # เว้นบรรทัด
                        if st.button("🔍 ดูรายละเอียดแปลง", key=f"btn_farmer_{farmer_name_group}", use_container_width=True):
                            st.session_state.selected_farmer_tab4 = farmer_name_group
                            st.rerun()
                col_idx += 1

            st.markdown("---")
            
            # --- 5. ค้นหาและจัดการแปลงนาเชิงลึก (ผสานจาก Code 2) ---
            st.subheader("🔎 ค้นหาและจัดการแปลงนาเชิงลึก (รายแปลง)")
            
            col_select, col_action = st.columns([3, 1])

            record_map = {
                row["id"]: (
                    f"{row['ชื่อเกษตรกร']} - อ.{row['อำเภอ']} -"
                    f" {row['ชื่อแปลง/ที่ตั้ง']} (ผู้รับผิดชอบ: {row['ผู้รับผิดชอบแปลง']})"
                )
                for _, row in df_filtered.iterrows()
            }

            with col_select:
                selected_dashboard_id = st.selectbox(
                    "เลือกแปลงนาเพื่อเรียกดูปฏิทินที่ถูกปรับหรือลบข้อมูล:",
                    options=list(record_map.keys()),
                    format_func=lambda x: record_map[x],
                    key="dashboard_select",
                )

            if selected_dashboard_id is not None:
                target_row = history_df[
                    history_df["id"] == selected_dashboard_id
                ].iloc[0]

                with col_action:
                    st.write("")
                    st.write("")
                    # ซ่อนปุ่มลบไว้ใน expander เพื่อความปลอดภัย
                    with st.expander("⚠️ การจัดการขั้นสูง"):
                        st.warning("หากลบแล้วจะไม่สามารถกู้คืนได้")
                        if st.button(
                            "🗑️ ยืนยันการลบ",
                            type="primary",
                            use_container_width=True,
                        ):
                            if delete_field_record(selected_dashboard_id):
                                st.success(f"ลบแปลงของ {target_row['ชื่อเกษตรกร']} แล้ว")
                                time.sleep(1) # หน่วงเวลาให้เห็นข้อความสำเร็จ
                                st.rerun()

                try:
                    old_sow_date = datetime.datetime.strptime(
                        str(target_row["วันที่เริ่มเพาะปลูก"]), "%Y-%m-%d"
                    ).date()
                except Exception:
                    old_sow_date = datetime.date.today()

                old_rice = target_row["สายพันธุ์ข้าว"]
                old_district = target_row.get("อำเภอ", "เมืองฉะเชิงเทรา")
                accumulated_shift = int(
                    target_row.get("จำนวนวันที่ปรับเลื่อนสะสม", 0)
                )
                delayed_act_saved = str(
                    target_row.get("กิจกรรมล่าสุดที่เลื่อน", "ไม่มี")
                )

                old_df = get_rice_schedule_advanced(
                    old_sow_date,
                    old_rice,
                    district_name=old_district,
                    base_accum_shift=accumulated_shift,
                    delayed_act_name=delayed_act_saved,
                )

                st.markdown("### 🔔 รายงานกิจกรรมที่เกิดการเลื่อนล่าช้า")
                if accumulated_shift > 0 and delayed_act_saved != "ไม่มี":
                    st.warning(f"""
                    ⚠️ **แปลงนี้มีการแจ้งปรับเลื่อนปฏิทิน!**
                    - **จุดตั้งต้นที่เกิดการล่าช้า:** กิจกรรม **"{delayed_act_saved}"**
                    - **จำนวนวันที่ขยับเลื่อน:** **+{accumulated_shift} วัน**
                    - **ผลกระทบ:** กิจกรรมตั้งแต่ *"{delayed_act_saved}"* เป็นต้นไป ถูกปรับเลื่อนวันทำกิจกรรมออกไปทั้งหมด (ไฮไลต์ด้วยสีเหลืองด้านล่าง)
                    """)
                elif accumulated_shift > 0:
                    st.warning(
                        "⚠️ แปลงนี้มีการขยับวันเลื่อนรวม"
                        f" **+{accumulated_shift} วัน**"
                    )
                else:
                    st.success(
                        "✅"
                        " **แปลงนี้ดำเนินกิจกรรมตรงตามกำหนดเดิมทุกขั้นตอน"
                        " (ไม่มีกิจกรรมเลื่อนวัน)**"
                    )

                st.markdown(
                    f"🔮 **ตารางปฏิทินกิจกรรมปัจจุบัน (อำเภอ: {old_district} |"
                    f" ผู้รับผิดชอบ: {target_row.get('ผู้รับผิดชอบแปลง', 'ไม่ระบุ')})**"
                )

                show_old_df = old_df.drop(columns=["_danger", "_shifted"])

                def highlight_rows_dashboard(row):
                    is_danger = old_df.loc[row.name, "_danger"]
                    is_shifted = old_df.loc[row.name, "_shifted"]
                    if is_danger:
                        return [
                            "background-color: #ffebee; font-weight: bold"
                            for _ in row
                        ]
                    elif is_shifted and accumulated_shift > 0:
                        return [
                            "background-color: #fff9c4; font-weight: bold"
                            for _ in row
                        ]
                    return ["" for _ in row]

                st.dataframe(
                    show_old_df.style.apply(highlight_rows_dashboard, axis=1),
                    use_container_width=True,
                    hide_index=True,
                )