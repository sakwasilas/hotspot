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
from models import Payment, Package, Customer, Session as DBSession, Admin  # ✅ FIX 1: Renamed Session to DBSession

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

# ✅ CRITICAL FIX A: Secret Key (Required for sessions)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    log.error(" SECRET_KEY not set in environment variables!")
    raise ValueError("SECRET_KEY is required for Flask sessions. Generate one with: secrets.token_hex(32)")

# ✅ FIX 5: Restrict CORS for security - only allow specific origins
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,https://yourdomain.com").split(",")
CORS(app, resources={
    r"/pay": {"origins": ALLOWED_ORIGINS},  # Restrict to known domains
    r"/mpesa/callback": {"origins": []},  # No CORS needed for callbacks
    r"/admin/*": {"origins": ALLOWED_ORIGINS}  # Restrict admin access
})

# =========================
# ENVIRONMENT VALIDATION
# =========================
CONSUMER_KEY = os.getenv("CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("CONSUMER_SECRET", "")
BUSINESS_SHORTCODE = os.getenv("BUSINESS_SHORTCODE", "174379")
PASSKEY = os.getenv("PASSKEY", "")
CALLBACK_URL = os.getenv("CALLBACK_URL")

# ✅ CRITICAL FIX D: Validate Callback URL
if not CALLBACK_URL:
    log.error(" CALLBACK_URL not set in environment variables!")
    raise ValueError("CALLBACK_URL is required for M-Pesa callbacks. Must be a publicly accessible HTTPS URL")
elif not CALLBACK_URL.startswith("https://"):
    log.warning(" CALLBACK_URL is not HTTPS. M-Pesa may reject callbacks in production!")
else:
    log.info(f" CALLBACK_URL configured: {CALLBACK_URL}")

# ✅ FIX 5: Safaricom IP ranges for callback security
SAFARICOM_IP_RANGES = [
    "196.201.214.0/24",
    "196.201.215.0/24", 
    "196.201.216.0/24",
    "197.248.96.0/24",
    "197.248.97.0/24"
]

# MikroTik configuration
ROUTER_IP = os.getenv("ROUTER_IP", "192.168.88.1")
ROUTER_USERNAME = os.getenv("ROUTER_USERNAME", "admin")
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "")
ROUTER_PORT = int(os.getenv("ROUTER_PORT", "8728"))

# M-Pesa endpoints
OAUTH_URL = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_URL = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

# Token cache
TOKEN_CACHE = {"token": None, "expires_at": 0}

# ✅ FIX 3: MikroTik connection cache
MIKROTIK_API = None

log.info(" Application starting with configuration:")
log.info(f"   - Callback URL: {CALLBACK_URL}")
log.info(f"   - Business Shortcode: {BUSINESS_SHORTCODE}")
log.info(f"   - Router IP: {ROUTER_IP}")
log.info(f"   - Allowed Origins: {ALLOWED_ORIGINS}")


# =========================
# SECURITY HELPERS
# =========================
def is_safaricom_callback():
    """✅ FIX 5: Verify callback comes from Safaricom"""
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    # In development, allow localhost
    if os.getenv("ENVIRONMENT") == "development":
        log.warning(f" Development mode: allowing callback from {client_ip}")
        return True
    
    # Check if IP is in Safaricom ranges (simplified - you'd need proper IP range checking)
    # For production, implement proper CIDR matching
    if client_ip.startswith("196.201.") or client_ip.startswith("197.248."):
        log.info(f" Callback from valid Safaricom IP: {client_ip}")
        return True
    
    log.error(f" Callback from unauthorized IP: {client_ip}")
    return False


# =========================
# MIKROTIK HELPERS
# =========================
def get_mikrotik_connection():
    """✅ FIX 3: Establish and cache connection to MikroTik router."""
    global MIKROTIK_API
    
    # Return cached connection if available
    if MIKROTIK_API:
        return MIKROTIK_API
    
    try:
        from librouteros import connect
        MIKROTIK_API = connect(
            username=ROUTER_USERNAME,
            password=ROUTER_PASSWORD,
            host=ROUTER_IP,
            port=ROUTER_PORT
        )
        log.info("Connected to MikroTik router (connection cached)")
        return MIKROTIK_API
    except Exception as e:
        log.error(f" MIKROTIK CONNECTION ERROR: {str(e)}")
        return None


def reset_mikrotik_connection():
    """Reset MikroTik connection cache (useful for reconnection)."""
    global MIKROTIK_API
    MIKROTIK_API = None
    log.info(" MikroTik connection cache reset")


def allow_customer_on_mikrotik(customer):
    """Allow customer's MAC address on MikroTik hotspot."""
    if not customer or not customer.mac_address:
        log.warning(" MIKROTIK SKIPPED: customer or MAC missing")
        return False

    api = get_mikrotik_connection()
    if not api:
        log.error(" MikroTik connection failed while allowing customer")
        return False

    try:
        bindings = list(api.path("ip", "hotspot", "ip-binding"))
        
        # Check if MAC already allowed
        for item in bindings:
            if item.get("mac-address") == customer.mac_address:
                log.info(f" MIKROTIK: MAC already allowed {customer.mac_address}")
                return True
        
        # Add new MAC binding
        api.path("ip", "hotspot", "ip-binding").add(
            mac_address=customer.mac_address,
            type="bypassed",
            comment=f"Paid hotspot user {customer.phone}"
        )
        log.info(f" MIKROTIK: allowed MAC {customer.mac_address}")
        return True
    except Exception as e:
        log.error(f" MIKROTIK MAC ALLOW ERROR: {str(e)}")
        return False


def remove_customer_from_mikrotik(customer):
    """Remove customer's MAC address from MikroTik hotspot."""
    if not customer or not customer.mac_address:
        log.warning(" MIKROTIK REMOVE SKIPPED: customer or MAC missing")
        return False

    api = get_mikrotik_connection()
    if not api:
        log.error(" MikroTik connection failed while removing customer")
        return False

    try:
        bindings = list(api.path("ip", "hotspot", "ip-binding"))
        
        for item in bindings:
            if item.get("mac-address") == customer.mac_address:
                item_id = item.get(".id")
                if item_id:
                    api.path("ip", "hotspot", "ip-binding").remove(item_id)
                    log.info(f" MIKROTIK: removed MAC {customer.mac_address}")
                    return True
        
        log.info(f"ℹ MIKROTIK: MAC not found for removal {customer.mac_address}")
        return False
    except Exception as e:
        log.error(f" MIKROTIK REMOVE ERROR: {str(e)}")
        return False


def expire_finished_sessions():
    """Expire sessions that have reached their end time."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        
        # ✅ FIX 1: Using DBSession instead of Session
        expired_sessions = db.query(DBSession).filter(
            DBSession.status == "active",
            DBSession.end_time <= now
        ).all()
        
        for session_obj in expired_sessions:
            customer = db.query(Customer).filter_by(id=session_obj.customer_id).first()
            if customer:
                remove_customer_from_mikrotik(customer)
            
            session_obj.status = "expired"
            log.info(f" SESSION EXPIRED: customer_id={session_obj.customer_id}")
        
        if expired_sessions:
            db.commit()
            log.info(f" Expired {len(expired_sessions)} sessions")
    except Exception as e:
        db.rollback()
        log.error(f"SESSION EXPIRY ERROR: {str(e)}")
    finally:
        db.close()


# =========================
# MPESA HELPERS
# =========================
def get_mpesa_access_token():
    """Get M-Pesa access token with caching."""
    current_time = time.time()
    
    # Return cached token if still valid
    if TOKEN_CACHE["token"] and current_time < TOKEN_CACHE["expires_at"]:
        log.debug("Using cached access token")
        return TOKEN_CACHE["token"]
    
    try:
        response = requests.get(
            OAUTH_URL,
            auth=(CONSUMER_KEY, CONSUMER_SECRET),
            timeout=30
        )
        
        log.info(f"Token request status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            token = data["access_token"]
            TOKEN_CACHE["token"] = token
            TOKEN_CACHE["expires_at"] = current_time + 3000  # 50 minutes
            log.info(" Token obtained successfully")
            return token
        
        log.error(f" Token error: {response.text}")
        return None
    except Exception as e:
        log.error(f" Error getting token: {str(e)}")
        return None


def generate_password(shortcode, passkey, timestamp):
    """Generate M-Pesa password."""
    raw_string = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(raw_string.encode()).decode()


def normalize_kenyan_phone(phone):
    """Normalize Kenyan phone number to 254 format."""
    phone = phone.strip().replace(" ", "")
    
    if phone.startswith("+254"):
        phone = phone[1:]
    
    if phone.startswith("07") or phone.startswith("01"):
        phone = "254" + phone[1:]
    
    if phone.startswith("254") and len(phone) == 12:
        return phone
    
    return None


def stk_push(phone, amount, account_reference, transaction_desc):
    """Initiate STK push to customer's phone."""
    token = get_mpesa_access_token()
    if not token:
        return {"ResponseCode": "1", "ResponseDescription": "Failed to get access token"}
    
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = generate_password(BUSINESS_SHORTCODE, PASSKEY, timestamp)
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
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
    
    log.info(f" Sending STK Push to {phone} for KES {amount}")
    log.info(f" Callback URL: {CALLBACK_URL}")
    
    try:
        response = requests.post(STK_PUSH_URL, json=payload, headers=headers, timeout=30)
        result = response.json()
        log.info(f" STK Response: {result}")
        return result
    except Exception as e:
        log.error(f" STK PUSH ERROR: {str(e)}")
        return {"ResponseCode": "1", "ResponseDescription": str(e)}


# =========================
# SCHEDULER MANAGEMENT ✅ FIX 2
# =========================
scheduler = None

def start_scheduler():
    """Start background scheduler for session expiry."""
    global scheduler
    if scheduler is None:
        scheduler = BackgroundScheduler()
        scheduler.add_job(expire_finished_sessions, 'interval', minutes=1)
        scheduler.start()
        log.info(" Session expiry scheduler started")
    else:
        log.info("Scheduler already running")


def shutdown_scheduler():
    """Shutdown background scheduler gracefully."""
    global scheduler
    if scheduler:
        scheduler.shutdown()
        log.info("Scheduler shutdown")
        scheduler = None


# =========================
# ROUTES
# =========================
@app.route('/')
def home():
    """Home page - hotspot login/payment page."""
    expire_finished_sessions()
    
    db = SessionLocal()
    try:
        # Get query parameters
        mac = request.args.get("mac", "")
        ip = request.args.get("ip", "")
        link_orig = request.args.get("link-orig", "")
        link_login = request.args.get("link-login", "")
        
        # Get all packages
        db_packages = db.query(Package).all()
        packages = [{
            "id": pkg.id,
            "name": pkg.name,
            "price": pkg.price,
            "duration_hours": pkg.duration_hours
        } for pkg in db_packages]
        
        return render_template(
            "index.html",
            packages=packages,
            mac=mac,
            ip=ip,
            link_orig=link_orig,
            link_login=link_login
        )
    finally:
        db.close()


@app.route('/admin')
def admin_dashboard():
    """Admin dashboard - requires authentication."""
    if "admin_id" not in session:
        return redirect(url_for('admin_login_page'))
    
    db = SessionLocal()
    try:
        payments = db.query(Payment).order_by(Payment.id.desc()).all()
        customers = db.query(Customer).order_by(Customer.id.desc()).all()
        # ✅ FIX 1: Using DBSession
        sessions = db.query(DBSession).order_by(DBSession.id.desc()).all()
        
        active_sessions = [s for s in sessions if s.status == "active"]
        expired_sessions = [s for s in sessions if s.status == "expired"]
        total_amount = sum(p.amount for p in payments if p.status == "paid")
        
        return render_template(
            "admin.html",
            payments=payments,
            customers=customers,
            active_sessions=active_sessions,
            expired_sessions=expired_sessions,
            total_amount=total_amount
        )
    finally:
        db.close()


@app.route('/admin/login', methods=['POST'])
def admin_login():
    """Admin login API endpoint."""
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        username = data.get("username")
        password = data.get("password")
        
        if not username or not password:
            return jsonify({
                "success": False,
                "message": "Missing username or password"
            }), 400
        
        admin = db.query(Admin).filter_by(username=username).first()
        
        if not admin or not check_password_hash(admin.password, password):
            return jsonify({
                "success": False,
                "message": "Invalid username or password"
            }), 401
        
        session["admin_id"] = admin.id
        log.info(f" Admin logged in: {username}")
        return jsonify({"success": True, "message": "Login successful"})
    finally:
        db.close()


@app.route('/admin/login-page')
def admin_login_page():
    """Admin login page."""
    return render_template("admin_login.html")


@app.route('/admin/logout')
def admin_logout():
    """Admin logout endpoint."""
    session.pop("admin_id", None)
    log.info("Admin logged out")
    return jsonify({"success": True, "message": "Logged out"})


@app.route('/pay', methods=['POST'])
def pay():
    """Initiate payment for a package."""
    db = SessionLocal()
    try:
        data = request.get_json() or {}
        
        # Get and normalize inputs
        phone = normalize_kenyan_phone(data.get("phone", ""))
        package_name = data.get("package_name")
        mac_address = data.get("mac_address")
        ip_address = data.get("ip_address")
        
        # Validate required fields
        if not phone or not package_name:
            log.warning(f"Missing payment details: phone={phone}, package={package_name}")
            return jsonify({
                "success": False,
                "message": "Missing payment details"
            }), 400
        
        # Get package from database (source of truth for pricing)
        package = db.query(Package).filter_by(name=package_name).first()
        if not package:
            log.warning(f"Package not found: {package_name}")
            return jsonify({
                "success": False,
                "message": "Package not found"
            }), 404
        
        amount = package.price  # Locked price from database
        
        # Initiate STK push
        response = stk_push(
            phone=phone,
            amount=amount,
            account_reference=package.name,
            transaction_desc=f"Hotspot {package.name}"
        )
        
        checkout_request_id = response.get("CheckoutRequestID")
        response_code = response.get("ResponseCode")
        
        # Handle successful STK initiation
        if response_code == "0" and checkout_request_id:
            # Create payment record if not exists
            existing_payment = db.query(Payment).filter_by(
                checkout_request_id=checkout_request_id
            ).first()
            
            if not existing_payment:
                payment = Payment(
                    checkout_request_id=checkout_request_id,
                    phone=phone,
                    package_id=package.id,
                    amount=float(package.price),
                    status="pending",
                    receipt_number=None
                )
                db.add(payment)
                log.info(f" Payment record created: {checkout_request_id}")
            
            # Create or update customer
            customer = db.query(Customer).filter_by(phone=phone).first()
            
            if not customer:
                customer = Customer(
                    phone=phone,
                    ip_address=ip_address,
                    mac_address=mac_address
                )
                db.add(customer)
                log.info(f" New customer created: {phone}")
            else:
                if mac_address:
                    customer.mac_address = mac_address
                if ip_address:
                    customer.ip_address = ip_address
                log.info(f" Customer updated: {phone}")
            
            db.commit()
            
            return jsonify({
                "success": True,
                "message": response.get("ResponseDescription", "STK Push sent"),
                "checkout_request_id": checkout_request_id
            })
        
        # Handle failed STK push
        log.error(f"STK Push failed: {response}")
        return jsonify({
            "success": False,
            "message": response.get("ResponseDescription", "Payment request failed")
        }), 400
        
    except Exception as e:
        db.rollback()
        log.error(f" PAY ERROR: {str(e)}", exc_info=True)
        return jsonify({
            "success": False,
            "message": "Internal server error"
        }), 500
    finally:
        db.close()


@app.route('/waiting/<checkout_request_id>')
def waiting(checkout_request_id):
    """Payment waiting page."""
    return render_template("waiting.html", checkout_request_id=checkout_request_id)


@app.route('/payment-status/<checkout_request_id>')
def payment_status(checkout_request_id):
    """Check payment status."""
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()
        
        if not payment:
            return jsonify({"status": "pending"})
        
        return jsonify({"status": payment.status})
    except Exception as e:
        log.error(f" STATUS ERROR: {str(e)}")
        return jsonify({"status": "pending"})
    finally:
        db.close()


@app.route('/success/<checkout_request_id>')
def success(checkout_request_id):
    """Payment success page."""
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()
        
        original_url = request.args.get(
            'url',
            request.args.get('link-orig', 'https://www.google.com')
        )
        
        return render_template("success.html", payment=payment, original_url=original_url)
    finally:
        db.close()


@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """✅ FIX 4 & 5: M-Pesa callback endpoint with proper validation."""
    
    # ✅ FIX 5: Verify callback source
    if not is_safaricom_callback():
        log.error(" Unauthorized callback attempt")
        return jsonify({"error": "Forbidden"}), 403
    
    callback_data = request.get_json(force=True)
    log.info(f" CALLBACK RECEIVED: {callback_data}")
    
    # ✅ FIX 4: Validate callback structure
    if not callback_data or "Body" not in callback_data:
        log.error(" Invalid callback structure - missing Body")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    
    body = callback_data.get("Body", {})
    if "stkCallback" not in body:
        log.error(" Invalid callback structure - missing stkCallback")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    
    db = SessionLocal()
    try:
        stk_callback = body.get("stkCallback", {})
        checkout_request_id = stk_callback.get("CheckoutRequestID")
        result_code = stk_callback.get("ResultCode")
        
        if not checkout_request_id:
            log.error("No CheckoutRequestID in callback")
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
        
        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()
        
        if not payment:
            log.warning(f" Callback payment not found: {checkout_request_id}")
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
        
        # ✅ CRITICAL FIX H: Double callback protection
        if payment.status in ["paid", "failed"]:
            log.info(f"Callback already processed: {checkout_request_id} (status: {payment.status})")
            return jsonify({"ResultCode": 0, "ResultDesc": "Already processed"})
        
        # Handle successful payment
        if result_code == 0:
            # Extract receipt number
            callback_items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            for item in callback_items:
                if item.get("Name") == "MpesaReceiptNumber":
                    payment.receipt_number = item.get("Value")
                    break
            
            payment.status = "paid"
            log.info(f"✅ Payment marked as paid: {checkout_request_id}, Receipt: {payment.receipt_number}")
            
            # Get or create customer
            customer = db.query(Customer).filter_by(phone=payment.phone).first()
            if not customer:
                customer = Customer(phone=payment.phone)
                db.add(customer)
                db.flush()
                log.info(f" Customer created from callback: {payment.phone}")
            
            # Create new session and expire old ones
            package = db.query(Package).filter_by(id=payment.package_id).first()
            if package:
                # ✅ FIX 1: Using DBSession
                existing_active_sessions = db.query(DBSession).filter_by(
                    customer_id=customer.id,
                    status="active"
                ).all()
                
                for old_session in existing_active_sessions:
                    old_session.status = "expired"
                    log.info(f"Expired old session: {old_session.id}")
                
                # Create new session
                start_time = datetime.utcnow()
                end_time = start_time + timedelta(hours=package.duration_hours)
                
                new_session = DBSession(
                    customer_id=customer.id,
                    package_id=package.id,
                    start_time=start_time,
                    end_time=end_time,
                    status="active"
                )
                db.add(new_session)
                log.info(f" New session created: {new_session.id}, expires at {end_time}")
            
            db.commit()
            
            # Allow customer on MikroTik
            allow_customer_on_mikrotik(customer)
            log.info(f" Callback activated successfully: {checkout_request_id}")
        
        # Handle failed payment
        else:
            payment.status = "failed"
            db.commit()
            log.warning(f" Payment failed in callback: {checkout_request_id}, ResultCode: {result_code}")
        
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
        
    except Exception as e:
        db.rollback()
        log.error(f" CALLBACK ERROR: {str(e)}", exc_info=True)
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    finally:
        db.close()


# =========================
# TEST ENDPOINTS
# =========================
@app.route('/test-mpesa')
def test_mpesa():
    """Test M-Pesa credentials."""
    token = get_mpesa_access_token()
    if token:
        return jsonify({
            "status": "success",
            "message": "✅ M-Pesa credentials working!",
            "token_preview": token[:50] + "..."
        })
    
    return jsonify({
        "status": "error",
        "message": "❌ Failed to get token. Check your Consumer Key and Secret"
    }), 500


@app.route('/test-mikrotik')
def test_mikrotik():
    """Test MikroTik connection."""
    reset_mikrotik_connection()  # Force fresh connection for test
    api = get_mikrotik_connection()
    if not api:
        return jsonify({
            "status": "error",
            "message": "❌ MikroTik connection failed"
        }), 500
    
    try:
        identities = list(api.path("system", "identity").select())
        return jsonify({
            "status": "success",
            "message": "✅ MikroTik connected successfully",
            "data": identities
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"❌ MikroTik connected but query failed: {str(e)}"
        }), 500


# =========================
# ERROR HANDLERS
# =========================
@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    log.warning(f"404 error: {request.path}")
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    log.error(f"500 error: {str(error)}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


# =========================
# MAIN APPLICATION ✅ FIX 2
# =========================
if __name__ == "__main__":
    # Start scheduler safely
    start_scheduler()
    
    log.info(" Starting Flask application on 0.0.0.0:10000")
    
    try:
        # Run Flask application
        app.run(host="0.0.0.0", port=10000, debug=True)
    except KeyboardInterrupt:
        log.info(" Shutting down gracefully...")
        shutdown_scheduler()