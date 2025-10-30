from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session
from flask_wtf import FlaskForm
from functools import wraps
from wtforms import StringField, DateTimeField, SelectField, SubmitField
from wtforms.validators import DataRequired, Length, ValidationError
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Date
from sqlalchemy.orm import sessionmaker, declarative_base
# from dotenv import load_dotenv
import requests
import json
import os


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-key")

# --- Database setup (SQLite) ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "appointments.db")
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
db_session = Session()
Base = declarative_base()

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    client_name = Column(String(30), nullable=False)
    phone = Column(String(13), nullable=False)
    service = Column(String(30), nullable=False)
    start_at = Column(DateTime, nullable=False, index=True)
    end_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class BlockedDate(Base):
    __tablename__ = "blocked_dates"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, unique=True)
    reason = Column(String(100), nullable=True)  # опціонально, для пояснення

Base.metadata.create_all(engine)

def phone_digits_only(form, field):
    # дозволяємо лише цифри
    if not field.data.isdigit():
        raise ValidationError("Номер телефону повинен містити лише цифри")
    # перевірка довжини (наприклад, 10 або 12-13 цифр)
    if len(field.data) not in [10, 12, 13]:
        raise ValidationError("Неправильна довжина номера телефону")

# --- Forms ---
class BookingForm(FlaskForm):
    client_name = StringField("Ім'я", validators=[DataRequired(), Length(max=30)])
    phone = StringField("Телефон", validators=[DataRequired(), phone_digits_only])
    # phone = IntegerField("Телефон", validators=[DataRequired(), Length(max=13)])
    service = SelectField("Послуга", choices=[("haircut","Стрижка"),("beard","Борода"),("combo","Стрижка+борода")])
    start_at = DateTimeField("Дата і час", format="%Y-%m-%d %H:%M", validators=[DataRequired()])
    submit = SubmitField("Записати")

# тривалість послуг
SERVICE_DURATIONS = {
    "haircut": 60,
    "beard": 30,
    "combo": 60
}

def get_service_duration(service):
    return SERVICE_DURATIONS.get(service, 30)

# --- Helper: check overlap (simple) ---
def is_slot_free(start_at, duration_minutes):
    end_at = start_at + timedelta(minutes=duration_minutes)
    overlaps = db_session.query(Appointment).filter(
        Appointment.start_at < end_at,
        Appointment.end_at > start_at
    ).all()
    return len(overlaps) == 0

def available_slots():
    service = request.args.get("service")
    date_str = request.args.get("date")
    if not service or not date_str:
        return jsonify([])

    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    slots = get_available_slots(service, day)
    return jsonify(slots)

def get_available_slots(service, day):
    """Повертає список вільних слотів для заданої дати та послуги"""

    # перевірка чи день заблокований
    blocked = db_session.query(BlockedDate).filter_by(date=day).first()
    if blocked:
        return []  # немає доступних слотів

    duration = get_service_duration(service)
    work_start = datetime.combine(day, datetime.strptime("10:00", "%H:%M").time())
    work_end = datetime.combine(day, datetime.strptime("18:00", "%H:%M").time())

    slots = []
    current = work_start
    while current + timedelta(minutes=duration) <= work_end:
        if is_slot_free(current, duration) and current >= datetime.now():
            slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=30)  # крок 30 хв
    return slots



# --- SMS notifier ---
# твій токен TurboSMS
TURBOSMS_TOKEN = "94af002aebefdab7465aaea6c6ff9eebc0481c92"
SENDER_NAME = "+380683883788"  # або ім'я відправника, зареєстроване у TurboSMS


def send_sms_turbosms(phone, message):
    """
    Надсилає SMS на номер phone через TurboSMS API.
    phone: str, у форматі '380XXXXXXXXX'
    message: str, текст повідомлення
    """
    url = "https://api.turbosms.ua/message/send.json"

    headers = {
        "Authorization": f"Bearer {TURBOSMS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "recipients": [phone],
        "sms": {
            "sender": SENDER_NAME,
            "text": message
        }
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        resp_json = response.json()
        if resp_json.get("response_code") == 0:
            print(f"SMS на {phone} успішно надіслано")
            return True
        else:
            print(f"Помилка відправки SMS: {resp_json}")
            return False
    except Exception as e:
        print(f"Помилка при відправці SMS: {e}")
        return False

# --- Routes ---
@app.route("/")
def index():
    # show upcoming appointments
    now = datetime.now()
    appts = db_session.query(Appointment).filter(Appointment.start_at >= now).order_by(Appointment.start_at).all()
    return render_template("index.html", appts=appts)


@app.route("/available_slots")
def available_slots():
    service = request.args.get("service")
    date_str = request.args.get("date")  # формат YYYY-MM-DD
    if not service or not date_str:
        return jsonify([])

    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        slots = get_available_slots(service, day)
        return jsonify(slots)
    except Exception as e:
        print(e)
        return jsonify([])


@app.route("/book", methods=["GET", "POST"])
def book():
    form = BookingForm()

    if request.method == "POST":
        name = (request.form.get("client_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        date_str = (request.form.get("date") or "").strip()
        time_str = (request.form.get("time") or "").strip()
        service = (request.form.get("service") or "").strip()

        if not name or not phone or not date_str or not time_str or not service:
            flash("Будь ласка, заповніть усі поля!", "danger")
            return render_template("book.html", form=form)

        start_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        duration = get_service_duration(service)
        end_at = start_at + timedelta(minutes=duration)

        if start_at < datetime.now():
            flash("Не можна обрати минулий час!", "danger")
            return render_template("book.html", form=form)

        if not is_slot_free(start_at, duration):
            flash("Обраний час вже зайнятий. Оберіть інший слот.", "danger")
            return render_template("book.html", form=form)

        appt = Appointment(
            client_name=name,
            phone=phone,
            service=service,
            start_at=start_at,
            end_at=end_at
        )
        db_session.add(appt)
        db_session.commit()

        sms_text = f"Дякуємо, {name}! Ваш запис на {start_at.strftime('%d.%m.%Y %H:%M')} підтверджено."
        send_sms_turbosms(phone, sms_text)

        flash("Запис створено та SMS надіслано!", "success")
        return redirect(url_for("index"))

    return render_template("book.html", form=form)

# ---- Простий логін ----
ADMIN_PASSWORD = "1234"

def login_required(view_func):
    """Декоратор для захисту сторінок адмінки"""
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("logged_in"):
            flash("Будь ласка, увійдіть для доступу до адмінки.", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped_view


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["logged_in"] = True
            flash("Вхід виконано успішно!", "success")
            return redirect(url_for("admin"))
        else:
            flash("Невірний пароль.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вихід виконано.", "info")
    return redirect(url_for("index"))

@app.route("/admin")
@login_required
def admin():
    sort_by = request.args.get("sort_by", "start_at")
    order = request.args.get("order", "asc")

    column = getattr(Appointment, sort_by, Appointment.start_at)
    if order == "desc":
        column = column.desc()

    appts = db_session.query(Appointment).order_by(column).all()

    def next_order(col):
        if col == sort_by:
            return "desc" if order == "asc" else "asc"
        return "asc"

    return render_template("admin.html", appts=appts, next_order=next_order)

@app.route("/admin/blocked_dates", methods=["GET", "POST"])
def blocked_dates():
    if request.method == "POST":
        date_from_str = request.form.get("date_from")
        date_to_str = request.form.get("date_to")
        reason = request.form.get("reason", "").strip()

        if date_from_str and date_to_str:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d").date()

            current = date_from
            added = 0
            while current <= date_to:
                # перевірка на існуючий блок
                existing = db_session.query(BlockedDate).filter_by(date=current).first()
                if not existing:
                    bd = BlockedDate(date=current, reason=reason)
                    db_session.add(bd)
                    added += 1
                current += timedelta(days=1)

            # commit із обробкою помилок
            try:
                db_session.commit()
                flash(f"Заблоковано {added} днів", "success")
            except Exception as e:
                db_session.rollback()
                flash(f"Помилка при блокуванні дат: {e}", "danger")

        else:
            flash("Вкажіть обидві дати 'з' і 'до'", "danger")

        return redirect(url_for("blocked_dates"))

    blocked = db_session.query(BlockedDate).order_by(BlockedDate.date).all()
    return render_template("blocked_dates.html", blocked=blocked)


@app.route("/admin/blocked_dates/delete/<int:bd_id>", methods=["POST"])
def delete_blocked(bd_id):
    bd = db_session.query(BlockedDate).get(bd_id)
    if bd:
        db_session.delete(bd)
        db_session.commit()
        flash("Вихідний скасовано.", "success")
    else:
        flash("День не знайдено.", "warning")
    return redirect(url_for("blocked_dates"))


@app.route("/delete/<int:appt_id>", methods=["POST"])
def delete(appt_id):
    appt = db_session.query(Appointment).get(appt_id)
    if appt:
        db_session.delete(appt)
        db_session.commit()
        flash("Запис видалено.", "success")
    else:
        flash("Запис не знайдено.", "warning")
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True)
