import os
import base64
import time
import logging
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from connections import SessionLocal
from models import Payment, Package, Customer, Session as DBSession, Admin

# Load environment variables
load_dotenv()

# =========================
# LOGGING CONFIGURATION
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('hotspot_payments.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# =========================
# FLASK APP CONFIGURATION
# =========================
app = Flask(__name__)

# Secret key (required)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    log.error("SECRET_KEY not set!")
    raise ValueError("SECRET_KEY required. Generate with: python -c 'import secrets; print(secrets.token_hex(32))'")

# CORS – keep as is
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,https://yourdomain.com").split(",")
CORS(app, resources={
    r"/pay": {"origins": ALLOWED_ORIGINS},
    r"/mpesa/callback/*": {"origins": []},
    r"/admin/*": {"origins": ALLOWED_ORIGINS}
})

# =========================
# ENVIRONMENT VALIDATION
# =========================
CONSUMER_KEY = os.getenv("CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("CONSUMER_SECRET", "")
BUSINESS_SHORTCODE = os.getenv("BUSINESS_SHORTCODE", "174379")
PASSKEY = os.getenv("PASSKEY", "")
CALLBACK_URL = os.getenv("CALLBACK_URL")

if not CALLBACK_URL:
    raise ValueError("CALLBACK_URL required (HTTPS for production)")
elif not CALLBACK_URL.startswith("https://"):
    log.warning("CALLBACK_URL is not HTTPS – M-Pesa may reject")

# MikroTik config
ROUTER_IP = os.getenv("ROUTER_IP", "192.168.88.1")
ROUTER_USERNAME = os.getenv("ROUTER_USERNAME", "admin")
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "")
ROUTER_PORT = int(os.getenv("ROUTER_PORT", "8728"))

# M-Pesa endpoints – can be overridden for production
OAUTH_URL = os.getenv("MPESA_OAUTH_URL", "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials")
STK_PUSH_URL = os.getenv("MPESA_STK_PUSH_URL", "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest")

# Caches
TOKEN_CACHE = {"token": None, "expires_at": 0}
MIKROTIK_API = None

log.info("Application starting with config:")
log.info(f"  - Callback URL: {CALLBACK_URL}")
log.info(f"  - Business Shortcode: {BUSINESS_SHORTCODE}")
log.info(f"  - Router IP: {ROUTER_IP}")
log.info(f"  - Allowed Origins: {ALLOWED_ORIGINS}")
log.info(f"  - M-Pesa Env: {'Sandbox' if 'sandbox' in OAUTH_URL else 'Production'}")

# Validate M-Pesa keys (warn but don't crash if missing)
if not CONSUMER_KEY or not CONSUMER_SECRET:
    log.warning("CONSUMER_KEY or CONSUMER_SECRET not set – M-Pesa will fail")
if not PASSKEY:
    log.warning("PASSKEY not set – M-Pesa password generation will fail")

# =========================
# MIKROTIK HELPERS (with auto-reconnect)
# =========================
def get_mikrotik_connection():
    global MIKROTIK_API
    # Test cached connection
    if MIKROTIK_API:
        try:
            # Quick test – try to list something simple
            MIKROTIK_API.path("system", "identity").select()
            return MIKROTIK_API
        except Exception:
            log.warning("MikroTik connection lost, reconnecting...")
            MIKROTIK_API = None
    
    try:
        from librouteros import connect
        MIKROTIK_API = connect(
            username=ROUTER_USERNAME,
            password=ROUTER_PASSWORD,
            host=ROUTER_IP,
            port=ROUTER_PORT
        )
        log.info("Connected to MikroTik router")
        return MIKROTIK_API
    except Exception as e:
        log.error(f"MikroTik connection error: {e}")
        return None

def reset_mikrotik_connection():
    global MIKROTIK_API
    MIKROTIK_API = None
    log.info("MikroTik connection reset")

def allow_customer_on_mikrotik(customer):
    if not customer or not customer.mac_address:
        log.warning("Skipped: customer or MAC missing")
        return False
    api = get_mikrotik_connection()
    if not api:
        return False
    try:
        bindings = list(api.path("ip", "hotspot", "ip-binding"))
        for item in bindings:
            if item.get("mac-address") == customer.mac_address:
                log.info(f"MAC already allowed: {customer.mac_address}")
                return True
        api.path("ip", "hotspot", "ip-binding").add(
            mac_address=customer.mac_address,
            type="bypassed",
            comment=f"Paid hotspot user {customer.phone}"
        )
        log.info(f"Allowed MAC: {customer.mac_address}")
        return True
    except Exception as e:
        log.error(f"MikroTik allow error: {e}")
        return False

def remove_customer_from_mikrotik(customer):
    if not customer or not customer.mac_address:
        log.warning("Skipped removal: MAC missing")
        return False
    api = get_mikrotik_connection()
    if not api:
        return False
    try:
        bindings = list(api.path("ip", "hotspot", "ip-binding"))
        for item in bindings:
            if item.get("mac-address") == customer.mac_address:
                item_id = item.get(".id")
                if item_id:
                    api.path("ip", "hotspot", "ip-binding").remove(item_id)
                    log.info(f"Removed MAC: {customer.mac_address}")
                    return True
        log.info(f"MAC not found: {customer.mac_address}")
        return False
    except Exception as e:
        log.error(f"MikroTik remove error: {e}")
        return False

# =========================
# SESSION EXPIRY
# =========================
def expire_finished_sessions():
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        expired = db.query(DBSession).filter(
            DBSession.status == "active",
            DBSession.end_time <= now
        ).all()
        for sess in expired:
            customer = db.query(Customer).filter_by(id=sess.customer_id).first()
            if customer:
                remove_customer_from_mikrotik(customer)
            sess.status = "expired"
        if expired:
            db.commit()
            log.info(f"Expired {len(expired)} sessions")
    except Exception as e:
        db.rollback()
        log.error(f"Session expiry error: {e}")
    finally:
        db.close()

# =========================
# SCHEDULER – runs only once (no duplicates)
# =========================
scheduler = None

def start_scheduler():
    global scheduler
    # Avoid starting in Flask reloader child process
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or os.environ.get('RUN_MAIN') == 'true':
        log.debug("Skipping scheduler start in reloader process")
        return
    if scheduler is None:
        scheduler = BackgroundScheduler()
        scheduler.add_job(expire_finished_sessions, 'interval', minutes=1)
        scheduler.start()
        log.info("Session expiry scheduler started")

def shutdown_scheduler():
    global scheduler
    if scheduler:
        scheduler.shutdown()
        scheduler = None
        log.info("Scheduler shut down")

# Start scheduler when app loads (works with Gunicorn --preload too)
start_scheduler()

# =========================
# MPESA HELPERS
# =========================
def get_mpesa_access_token():
    current_time = time.time()
    if TOKEN_CACHE["token"] and current_time < TOKEN_CACHE["expires_at"]:
        return TOKEN_CACHE["token"]
    try:
        response = requests.get(OAUTH_URL, auth=(CONSUMER_KEY, CONSUMER_SECRET), timeout=30)
        if response.status_code == 200:
            data = response.json()
            token = data["access_token"]
            TOKEN_CACHE["token"] = token
            TOKEN_CACHE["expires_at"] = current_time + 3000
            log.info("M-Pesa token obtained")
            return token
        log.error(f"Token error: {response.text}")
        return None
    except Exception as e:
        log.error(f"Token request error: {e}")
        return None

def generate_password(shortcode, passkey, timestamp):
    raw = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(raw.encode()).decode()

def normalize_kenyan_phone(phone):
    phone = phone.strip().replace(" ", "")
    if phone.startswith("+254"):
        phone = phone[1:]
    if phone.startswith("07") or phone.startswith("01"):
        phone = "254" + phone[1:]
    if phone.startswith("254") and len(phone) == 12:
        return phone
    return None

def stk_push(phone, amount, account_reference, transaction_desc):
    token = get_mpesa_access_token()
    if not token:
        return {"ResponseCode": "1", "ResponseDescription": "Failed to get access token"}
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = generate_password(BUSINESS_SHORTCODE, PASSKEY, timestamp)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "BusinessShortCode": BUSINESS_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(float(amount)),
        "PartyA": phone,
        "PartyB": BUSINESS_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": str(account_reference)[:12],
        "TransactionDesc": str(transaction_desc)[:13]
    }
    log.info(f"STK Push to {phone} for KES {amount}")
    try:
        response = requests.post(STK_PUSH_URL, json=payload, headers=headers, timeout=30)
        result = response.json()
        log.info(f"STK response: {result}")
        return result
    except Exception as e:
        log.error(f"STK push error: {e}")
        return {"ResponseCode": "1", "ResponseDescription": str(e)}

# =========================
# ROUTES
# =========================
@app.route('/')
def home():
    expire_finished_sessions()  # immediate check on homepage
    db = SessionLocal()
    try:
        mac = request.args.get("mac", "")
        ip = request.args.get("ip", "")
        link_orig = request.args.get("link-orig", "")
        link_login = request.args.get("link-login", "")
        packages = [{"id": p.id, "name": p.name, "price": p.price, "duration_hours": p.duration_hours} 
                    for p in db.query(Package).all()]
        return render_template("index.html", packages=packages, mac=mac, ip=ip, 
                               link_orig=link_orig, link_login=link_login)
    finally:
        db.close()

@app.route('/admin')
def admin_dashboard():
    if "admin_id" not in session:
        return redirect(url_for('admin_login_page'))
    db = SessionLocal()
    try:
        payments = db.query(Payment).order_by(Payment.id.desc()).all()
        customers = db.query(Customer).order_by(Customer.id.desc()).all()
        sessions = db.query(DBSession).order_by(DBSession.id.desc()).all()
        active = [s for s in sessions if s.status == "active"]
        expired = [s for s in sessions if s.status == "expired"]
        total = sum(p.amount for p in payments if p.status == "paid")
        return render_template("admin.html", payments=payments, customers=customers,
                               active_sessions=active, expired_sessions=expired, total_amount=total)
    finally:
        db.close()

@app.route('/admin/login', methods=['POST'])
def admin_login():
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return jsonify({"success": False, "message": "Missing credentials"}), 400
        admin = db.query(Admin).filter_by(username=username).first()
        if not admin or not check_password_hash(admin.password, password):
            return jsonify({"success": False, "message": "Invalid credentials"}), 401
        session["admin_id"] = admin.id
        log.info(f"Admin login: {username}")
        return jsonify({"success": True, "message": "Login successful"})
    finally:
        db.close()

@app.route('/admin/login-page')
def admin_login_page():
    return render_template("admin_login.html")

@app.route('/admin/logout')
def admin_logout():
    session.pop("admin_id", None)
    return jsonify({"success": True, "message": "Logged out"})

@app.route('/pay', methods=['POST'])
def pay():
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        phone = normalize_kenyan_phone(data.get("phone", ""))
        package_name = data.get("package_name")
        mac = data.get("mac_address")
        ip = data.get("ip_address")
        if not phone or not package_name:
            return jsonify({"success": False, "message": "Missing payment details"}), 400
        package = db.query(Package).filter_by(name=package_name).first()
        if not package:
            return jsonify({"success": False, "message": "Package not found"}), 404
        response = stk_push(phone, package.price, package.name, f"Hotspot {package.name}")
        checkout_id = response.get("CheckoutRequestID")
        if response.get("ResponseCode") == "0" and checkout_id:
            if not db.query(Payment).filter_by(checkout_request_id=checkout_id).first():
                payment = Payment(checkout_request_id=checkout_id, phone=phone, 
                                  package_id=package.id, amount=float(package.price), status="pending")
                db.add(payment)
            customer = db.query(Customer).filter_by(phone=phone).first()
            if not customer:
                customer = Customer(phone=phone, ip_address=ip, mac_address=mac)
                db.add(customer)
            else:
                if mac:
                    customer.mac_address = mac
                if ip:
                    customer.ip_address = ip
            db.commit()
            return jsonify({"success": True, "message": response.get("ResponseDescription"), 
                            "checkout_request_id": checkout_id})
        else:
            return jsonify({"success": False, "message": response.get("ResponseDescription", "STK push failed")}), 400
    except Exception as e:
        db.rollback()
        log.error(f"Pay error: {e}", exc_info=True)
        return jsonify({"success": False, "message": "Internal error"}), 500
    finally:
        db.close()

@app.route('/waiting/<checkout_request_id>')
def waiting(checkout_request_id):
    return render_template("waiting.html", checkout_request_id=checkout_request_id)

@app.route('/payment-status/<checkout_request_id>')
def payment_status(checkout_request_id):
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(checkout_request_id=checkout_request_id).first()
        return jsonify({"status": payment.status if payment else "pending"})
    except:
        return jsonify({"status": "pending"})
    finally:
        db.close()

@app.route('/success/<checkout_request_id>')
def success(checkout_request_id):
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(checkout_request_id=checkout_request_id).first()
        original_url = request.args.get('url', request.args.get('link-orig', 'https://www.google.com'))
        return render_template("success.html", payment=payment, original_url=original_url)
    finally:
        db.close()

# ======================================================
# SECURE CALLBACK ROUTE – uses secret in URL path
# ======================================================
CALLBACK_SECRET = os.getenv("CALLBACK_SECRET")
if not CALLBACK_SECRET:
    raise ValueError("CALLBACK_SECRET required. Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'")

@app.route(f'/mpesa/callback/{CALLBACK_SECRET}', methods=['POST'])
def mpesa_callback():
    # No IP verification – the secret in the URL authenticates the callback
    callback_data = request.get_json(force=True)
    log.info(f"Callback received (valid secret in URL)")
    
    if not callback_data or "Body" not in callback_data:
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    stk = callback_data["Body"].get("stkCallback")
    if not stk:
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    
    db = SessionLocal()
    try:
        checkout_id = stk.get("CheckoutRequestID")
        result_code = stk.get("ResultCode")
        if not checkout_id:
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
        payment = db.query(Payment).filter_by(checkout_request_id=checkout_id).first()
        if not payment:
            log.warning(f"Payment not found: {checkout_id}")
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
        if payment.status in ["paid", "failed"]:
            log.info(f"Already processed: {checkout_id}")
            return jsonify({"ResultCode": 0, "ResultDesc": "Already processed"})
        if result_code == 0:
            items = stk.get("CallbackMetadata", {}).get("Item", [])
            for item in items:
                if item.get("Name") == "MpesaReceiptNumber":
                    payment.receipt_number = item.get("Value")
                    break
            payment.status = "paid"
            log.info(f"Payment paid: {checkout_id}, receipt {payment.receipt_number}")
            customer = db.query(Customer).filter_by(phone=payment.phone).first()
            if not customer:
                customer = Customer(phone=payment.phone)
                db.add(customer)
                db.flush()
            pkg = db.query(Package).filter_by(id=payment.package_id).first()
            if pkg:
                for old in db.query(DBSession).filter_by(customer_id=customer.id, status="active").all():
                    old.status = "expired"
                start = datetime.utcnow()
                end = start + timedelta(hours=pkg.duration_hours)
                db.add(DBSession(customer_id=customer.id, package_id=pkg.id, start_time=start, end_time=end, status="active"))
            db.commit()
            allow_customer_on_mikrotik(customer)
        else:
            payment.status = "failed"
            db.commit()
            log.warning(f"Payment failed: {checkout_id}")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    except Exception as e:
        db.rollback()
        log.error(f"Callback error: {e}", exc_info=True)
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    finally:
        db.close()

# =========================
# ERROR HANDLERS
# =========================
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    log.error(f"500 error: {e}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500

# =========================
# MAIN – for local dev only
# =========================
if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    # For production, use Gunicorn with --preload instead.
    app.run(host="0.0.0.0", port=10000, debug=debug_mode)

# Ensure scheduler shuts down on exit (for Gunicorn)
import atexit
atexit.register(shutdown_scheduler)