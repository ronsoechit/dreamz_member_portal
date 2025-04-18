import os
import re
from datetime import datetime
from app import app, db
from models import Member

REPORT_PATH = os.path.join("instance", "Report.txt")

def parse_report(path):
    with open(path, encoding="latin-1") as f:
        lines = f.readlines()

    members = []
    for idx, line in enumerate(lines[3:], start=4):  # Start at line 4
        line = line.strip()
        if not line:
            continue

        parts = re.split(r'\s{2,}', line)
        if len(parts) < 1 or not parts[0].isdigit():
            print(f"⚠️ Line {idx} skipped: invalid or missing member_id")
            continue

        # Ensure parts has 14 elements
        while len(parts) < 14:
            parts.append(None)

        # Unpack parts safely
        (
            member_id, name, plan_type, contract_type, last_payment_amount,
            next_payment, birthdate, start_date, end_date,
            signup_date, last_payment, balance, email, outstanding
        ) = parts[:14]

        def parse_date(value):
            if value in [None, "", "00/00/0000"]:
                return None
            try:
                return datetime.strptime(value.strip(), "%d/%m/%Y").date()
            except:
                return None

        def parse_float(value):
            try:
                return float(value.replace(",", ".")) if value else 0.0
            except:
                return 0.0

        try:
            members.append({
                "member_id": int(member_id),
                "name": name.strip() if name else None,
                "email": email.strip() if email else None,
                "birthdate": parse_date(birthdate),
                "phone": None,
                "plan_type": plan_type.strip() if plan_type else None,
                "contract_type": contract_type.strip() if contract_type else None,
                "start_date": parse_date(start_date),
                "end_date": parse_date(end_date),
                "signup_date": parse_date(signup_date),
                "last_payment": parse_date(last_payment),
                "next_payment": parse_date(next_payment),
                "last_payment_amount": parse_float(last_payment_amount),
                "balance": parse_float(balance),
                "is_active": True,
                "form_path": None,
                "contract_path": None,
                "mandate_path": None,
                "cancellation_policy": None
            })
        except Exception as e:
            print(f"⚠️ Line {idx} skipped: {e}")

    return members

def sync_members(parsed):
    with app.app_context():
        new_count = 0
        updated_count = 0

        for m in parsed:
            existing = Member.query.filter_by(member_id=m["member_id"]).first()
            if existing:
                for key, val in m.items():
                    setattr(existing, key, val)
                updated_count += 1
            else:
                db.session.add(Member(**m))
                new_count += 1

        db.session.commit()
        return new_count, updated_count

if __name__ == "__main__":
    members = parse_report(REPORT_PATH)
    print(f"✅ Parsed {len(members)} members from {REPORT_PATH}")
    new_count, updated_count = sync_members(members)
    print(f"✅ Members imported and updated. New: {new_count}, Updated: {updated_count}")
