import os
import re
from datetime import datetime
from app import app, db, Member

def parse_date(date_str):
    try:
        if date_str and date_str != '00/00/0000':
            return datetime.strptime(date_str.strip(), '%d/%m/%Y').date()
    except Exception:
        pass
    return None
    
def parse_report(filepath):
    with open(filepath, "r", encoding="latin-1") as f:
        lines = f.readlines()

    members = []
    skipped = 0

    for idx, line in enumerate(lines[3:], start=4):  # sla eerste 3 regels over
        parts = re.split(r'\s{2,}', line.strip())

        # accepteer alleen regels met minimaal 13 kolommen en een getal als member_id
        if len(parts) < 13 or not parts[0].isdigit():
            print(f"⚠️ Line {idx} skipped: invalid or missing member_id")
            skipped += 1
            continue

        # veilige parsing
        try:
            member_id = parts[0].strip()
            name = parts[1].strip()
            membership_type = parts[2].strip()
            status = parts[3].strip().upper()
            billing_cycle = parts[4].strip()
            amount_due = parts[5].strip()
            due_date = parts[6].strip()
            start_date = parts[7].strip()
            end_date = parts[8].strip()
            signup_date = parts[9].strip()
            last_payment = parts[10].strip()
            last_payment_amount = try_float(parts[11])
            email = parts[12].strip() if len(parts) >= 13 else None
            current_balance = try_float(parts[13]) if len(parts) >= 14 else 0.0

            members.append({
                "member_id": member_id,
                "name": name,
                "email": email or None,
                "phone": None,
                "birthdate": None,
                "plan_type": membership_type,
                "contract_type": None,  # wordt afgeleid elders
                "start_date": start_date,
                "end_date": "Automatic Renewal" if end_date == "00/00/0000" else end_date,
                "signup_date": signup_date,
                "last_payment": last_payment,
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
            print(f"⚠️ Line {idx} skipped due to parsing error: {e}")
            skipped += 1

    print(f"✅ Parsed {len(members)} members from {filepath}")
    if skipped:
        print(f"⚠️ Skipped {skipped} line(s) that didn't match expected format.")
    return members

def try_float(value):
    try:
        return float(value.replace(",", "."))  # just in case
    except:
        return 0.0

def calculate_next_payment(last_payment, billing_cycle):
    try:
        if billing_cycle.lower() == "monthly":
            dt = datetime.strptime(last_payment, "%d/%m/%Y")
            month = dt.month + 1
            year = dt.year + (1 if month > 12 else 0)
            month = 1 if month > 12 else month
            return dt.replace(month=month, year=year).strftime("%d/%m/%Y")
    except:
        pass
    return ""

def get_document_path(folder, member_id):
    path = os.path.join("static", folder, f"{member_id}.pdf")
    return path if os.path.isfile(path) else None

def sync_members(members):
    new_count = 0
    updated_count = 0
    with app.app_context():
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
    return new_count, updated_count

# run
if __name__ == "__main__":
    filepath = os.path.join("instance", "Report.txt")
    members = parse_report(filepath)
    sync_members(members)
