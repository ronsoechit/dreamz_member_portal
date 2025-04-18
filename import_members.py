import os
import re
import csv
import sys
from datetime import datetime, timedelta, date
from flask import Flask
from app import db, app, Member

print("🧪 Member model from:", Member.__module__)

# Bestandspad
REPORT_PATH = os.path.join("instance", "Report.txt")

# Kolommen in volgorde (15 vaste kolommen)
COLUMN_NAMES = [
    "Member ID",
    "Name",
    "Membership Type",
    "Status",
    "Billing Type",
    "Monthly Amount",
    "Contract Start Date",
    "Contract End Date",
    "Signup Date",
    "Last Pd Date",
    "Last Pd Amount",
    "E-mail",
    "Current Balance",
    "Placeholder1",
    "Placeholder2"
]

def parse_date(date_str):
    try:
        if date_str in ("", "00/00/0000", "*"):
            return None
        return datetime.strptime(date_str.strip("* "), "%d/%m/%Y").date()
    except Exception:
        return None

def clean_amount(amount):
    try:
        return float(amount.replace(",", "").strip())
    except Exception:
        return 0.0

def determine_contract_type(membership_type):
    type_lower = membership_type.lower()
    if "12" in type_lower and "dreamz" in type_lower:
        return "Dreamz 12M"
    elif "6" in type_lower and "dreamz" in type_lower:
        return "Dreamz 6M"
    elif "contract" in type_lower:
        return "Contract"
    return None

def parse_report(path):
    with open(path, "r", encoding="latin-1") as file:
        lines = file.readlines()[3:]

    members = []
    for idx, line in enumerate(lines, start=4):
        parts = re.split(r'\s{2,}', line.strip())
        if len(parts) < 13:
            continue
        try:
            member_id = parts[0]
            name = parts[1]
            membership_type = parts[2]
            billing_type = parts[4]
            contract_start = parse_date(parts[6])
            contract_end = parse_date(parts[7])
            signup_date = parse_date(parts[8])
            last_payment = parse_date(parts[9])
            last_payment_amount = clean_amount(parts[10])
            email = parts[11].strip()
            balance = clean_amount(parts[12])
            phone = "Not available"
            birthdate = None

            contract_type = determine_contract_type(membership_type)

            member = {
                "member_id": member_id,
                "name": name,
                "plan_type": membership_type,
                "billing_type": billing_type,
                "contract_type": contract_type,
                "start_date": contract_start,
                "end_date": contract_end,
                "signup_date": signup_date,
                "last_payment": last_payment,
                "last_payment_amount": last_payment_amount,
                "email": email,
                "phone": phone,
                "birthdate": birthdate,
                "balance": balance
            }
            members.append(member)
        except Exception as e:
            print(f"⚠️ Line {idx} skipped due to error: {e}")
    return members

def sync_members(members):
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
    return new_count, updated_count

if __name__ == "__main__":
    print(f"Total lines in Report.txt: {sum(1 for _ in open(REPORT_PATH, 'r', encoding='latin-1'))}")
    members = parse_report(REPORT_PATH)
    print(f"✅ Parsed {len(members)} members from {REPORT_PATH}")

    with app.app_context():
        new, updated = sync_members(members)
        print(f"✅ Members imported and updated. New: {new}, Updated: {updated}")