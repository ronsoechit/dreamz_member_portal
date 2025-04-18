from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import os

# --- FLASK APP ---
app = Flask(__name__)
app.secret_key = 'supersecretkey'
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, 'instance', 'members.db')

# Zorg dat de instance-map bestaat
os.makedirs(os.path.dirname(db_path), exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


db = SQLAlchemy(app)

# --- MODEL ---
class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(50))
    birthdate = db.Column(db.Date)
    plan_type = db.Column(db.String(100))
    contract_type = db.Column(db.String(50))
    billing_type = db.Column(db.String(50))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    signup_date = db.Column(db.Date)
    last_payment = db.Column(db.Date)
    next_payment = db.Column(db.Date)
    last_payment_amount = db.Column(db.Float)
    balance = db.Column(db.Float)
    is_active = db.Column(db.Boolean)
    form_path = db.Column(db.String(200))
    contract_path = db.Column(db.String(200))
    mandate_path = db.Column(db.String(200))
    cancellation_policy = db.Column(db.Text)

# --- HELPERS ---
def calculate_time_remaining(end_date):
    if end_date:
        today = date.today()
        delta = end_date - today
        return f"{delta.days} days" if delta.days >= 0 else "Expired"
    return "N/A"

# --- ROUTES ---
@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        member_id = request.form.get('member_id')
        member = Member.query.filter_by(member_id=member_id).first()
        if member:
            return redirect(url_for('dashboard', id=member_id))
        flash('Member ID not found.')
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    member_id = request.args.get('id')
    if not member_id:
        flash("No member ID provided.")
        return redirect(url_for('login'))

    member = Member.query.filter_by(member_id=member_id).first()
    if not member:
        flash("Member not found.")
        return redirect(url_for('login'))

    # DOCUMENT STATUS
    has_form = os.path.exists(f'static/forms/{member.member_id}_form.pdf')
    has_contract = os.path.exists(f'static/contracts/{member.member_id}_contract.pdf')
    has_mandate = os.path.exists(f'static/mandates/{member.member_id}_mandate.pdf')

    # CONTRACT TYPE
    contract_type = member.contract_type if has_contract else "None"

    # CANCELLATION INFO
    cancellation_message = ""
    if contract_type != "None" and member.end_date:
        cancellation_start = member.end_date.replace(month=member.end_date.month - 1)
        cancellation_message = (
            f"<p>📅 Your contract was automatically renewed. "
            f"You may cancel from <strong>{cancellation_start.strftime('%d %B %Y')}</strong> "
            f"until <strong>{member.end_date.strftime('%d %B %Y')}</strong>.</p>"
        )
    # Converteer string naar datetime.date als nodig
    if isinstance(member.end_date, str):
        try:
            member.end_date = datetime.strptime(member.end_date, "%Y-%m-%d").date()
        except Exception as e:
            print("❌ Ongeldige end_date:", member.end_date, "→", e)
            member.end_date = None
            
    return render_template(
        'dashboard.html',
        member=member,
        contract_type=contract_type,
        has_form=has_form,
        has_contract=has_contract,
        has_mandate=has_mandate,
        cancellation_info=cancellation_message,
        calculate_time_remaining=calculate_time_remaining
    )

@app.route('/logout')
def logout():
    return redirect(url_for('login'))  # Of een andere pagina, zoals home of login

# --- MAIN ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
