import os
import re
from datetime import datetime, timedelta
from app import app, db, Member

REPORT_PATH = os.path.join("instance", "Report.txt")

def parse_date(date_str):
    try:
        return datetime.strptime(date_str.strip("* "), "%d/%m/%Y").date()
    except Exception:
        return None

def calculate_next_payment(last_payment_date, billing_cycle):
    date_obj = parse_date(last_payment_date)
    if not date_obj:
        return None
    if "month" in billing_cycle.lower():
        return (date_obj + timedelta(days=30)).replace(day=1)
    elif "semi" in billing_cycle.lower():
        return date_obj + timedelta(days=182)
    elif "year" in billing_cycle.lower() or "annual" in billing_cycle.lower():
        return date_obj + timedelta(days=365)
    return None

def get_document_path(folder, member_id):
    path = os.path.join("static", folder, f"{member_id}.pdf")
    return path if os.path.exists(path) else None

def load_members():
    with open(REPORT_PATH, encoding="latin1") as f:
        lines = f.readlines()

    print(f"Total lines in Report.txt: {len(lines)}")

    members = []
    for idx, line in enumerate(lines[3:], start=4):  # Skip headers (line 0-2)
        parts = re.split(r'\s{2,}', line.strip())
        if len(parts) not in (13, 14):
            print(f"⚠️ Line {idx} skipped: {len(parts)} columns")
            continue
        try:
            member_id = parts[0].strip()
            if not member_id.isdigit():
                print(f"⚠️ Line {idx} skipped: invalid or missing member_id")
                continue

            name = parts[1].strip()
            membership_type = parts[2].strip()
            status = parts[3].strip()
            billing_cycle = parts[4].strip()
            last_payment_amount = float(parts[5].replace(",", "."))

            start_date = parts[6]
            end_date = parts[7]
            last_payment = parts[8]
            signup_date = parts[9]
            next_payment = parts[10]

            email = parts[11] if len(parts) == 14 else ""
            current_balance = float(parts[12 if len(parts) == 14 else 11].replace(",", "."))

            members.append({
                "member_id": member_id,
                "name": name,
                "email": email or None,
                "phone": None,
                "birthdate": None,
                "plan_type": membership_type,
                "contract_type": None,
                "start_date": parse_date(start_date),
                "end_date": None if end_date == "00/00/0000" else parse_date(end_date),
                "signup_date": parse_date(signup_date),
                "last_payment": parse_date(last_payment),
                "next_payment": calculate_next_payment(last_payment, billing_cycle),
                "last_payment_amount": last_payment_amount,
                "balance": current_balance,
                "is_active": status == "ACTIVE",
                "form_path": None,
                "contract_path": get_document_path("contracts", member_id),
                "mandate_path": get_document_path("mandates", member_id),
                "cancellation_policy": None
            })

        except Exception as e:
            print(f"⚠️ Line {idx} skipped due to error: {e}")
            continue

    print(f"✅ Parsed {len(members)} members from {REPORT_PATH}")
    return members

def sync_members(members):
    with app.app_context():
        new_count = 0
        updated_count = 0
        for m in members:
            existing = Member.query.filter_by(member_id=m["member_id"]).first()
            if existing:
                for key, value in m.items():
                    setattr(existing, key, value)
                updated_count += 1
            else:
                db.session.add(Member(**m))
                new_count += 1
        db.session.commit()
        print(f"✅ Members imported and updated. New: {new_count}, Updated: {updated_count}")

if __name__ == "__main__":
    members = load_members()
    sync_members(members)
