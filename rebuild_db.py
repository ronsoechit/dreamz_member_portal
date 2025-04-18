import os
from app import app, db
from models import Member

db_path = os.path.join('instance', 'members.db')

try:
    if os.path.exists(db_path):
        os.remove(db_path)
        print("🗑️ Oude database verwijderd.")
    else:
        print("ℹ️ Geen bestaande database gevonden. Er wordt een nieuwe aangemaakt.")
except PermissionError:
    print("⚠️ Kan databasebestand niet verwijderen. Bestand is mogelijk in gebruik.")
    print("➡️ Probeer handmatig te verwijderen of herstart je systeem opnieuw.")
    exit(1)

# ⏬ Verplaats de import NA het verwijderen van het bestand
from app import app, db

with app.app_context():
    db.create_all()
    print("✅ Database succesvol aangemaakt.")
    print([column.name for column in Member.__table__.columns])

