from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(100))
    birthdate = db.Column(db.Date, nullable=True)
    plan_type = db.Column(db.String(100))
    contract_type = db.Column(db.String(100))
    billing_type = db.Column(db.String(100))  # ✅ voeg deze toe
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.String(50))  # mag ook Date zijn als je geen "Automatic Renewal" nodig hebt
    signup_date = db.Column(db.Date, nullable=True)
    last_payment = db.Column(db.Date, nullable=True)
    next_payment = db.Column(db.Date, nullable=True)
    last_payment_amount = db.Column(db.Float, nullable=True)
    balance = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    form_path = db.Column(db.String(200), nullable=True)
    contract_path = db.Column(db.String(200), nullable=True)
    mandate_path = db.Column(db.String(200), nullable=True)
    cancellation_policy = db.Column(db.Text, nullable=True)
