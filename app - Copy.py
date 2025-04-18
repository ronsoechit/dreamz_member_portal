from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from models import db, Member
import os
import re

app = Flask(__name__)
app.secret_key = 'dreamz-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///members.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

with app.app_context():
    db.create_all()

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        member_id = request.form.get('member_id')
        if member_id:
            member = Member.query.filter_by(member_id=member_id).first()
            if member:
                session['member_id'] = member_id
                return redirect(url_for('dashboard'))
            else:
                flash('Member ID not found.')
        else:
            flash('Please enter a Member ID.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def parse_end_date_from_txt(member_id):
    try:
        with open('instance/Report.txt', encoding='latin-1') as f:
            for line in f:
                if line.strip().startswith(member_id):
                    parts = line.split()
                    if len(parts) > 12:
                        raw_date = parts[12]
                        if re.match(r'\d{2}/\d{2}/\d{4}', raw_date):
                            return raw_date
        return None
    except Exception:
        return None

def parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%d/%m/%Y")
    except:
        return None

@app.route('/dashboard')
def dashboard():
    member_id = session.get('member_id')
    if not member_id:
        return redirect(url_for('login'))

    member = Member.query.filter_by(member_id=member_id).first()
    if not member:
        flash("Member not found.")
        return redirect(url_for('login'))

    cancellation_message = get_cancellation_info(member_id, member.end_date)

    # Zorg dat het attribuut altijd beschikbaar is
    if not hasattr(member, 'cancellation_policy') or member.cancellation_policy is None:
        member.cancellation_policy = cancellation_message
    else:
        member.cancellation_policy = cancellation_message

    return render_template('dashboard.html', member=member, cancellation_info=cancellation_message)


def get_cancellation_info(member_id, db_end_date):
    today = datetime.today()

    # probeer eerst end_date van database
    parsed_date = parse_date(db_end_date) if db_end_date else None

    # fallback: probeer end_date uit Report.txt
    if not parsed_date:
        txt_date = parse_end_date_from_txt(member_id)
        parsed_date = parse_date(txt_date)

    if not parsed_date:
        return "⚠️ Unable to determine contract end date."

    if parsed_date < today:
        renewed = parsed_date.replace(year=today.year)
        if renewed < today:
            renewed = renewed.replace(year=today.year + 1)
        cancel_window_start = renewed - timedelta(days=30)
        return f"📅 Your contract was automatically renewed. You may cancel from **{cancel_window_start.strftime('%d %B %Y')}** until **{renewed.strftime('%d %B %Y')}**."
    else:
        cancel_window_start = parsed_date - timedelta(days=30)
        return f"📅 You may cancel from **{cancel_window_start.strftime('%d %B %Y')}** until **{parsed_date.strftime('%d %B %Y')}**."

if __name__ == '__main__':
    app.run(debug=True)
