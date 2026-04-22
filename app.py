import os
import base64
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

from connections import SessionLocal
from models import Payment, Package, Customer, Session

load_dotenv()

app = Flask(__name__)

# =========================
# ENV CONFIG - CORRECTED FOR RENDER
# =========================
CONSUMER_KEY = os.getenv("CONSUMER_KEY", "9jf0Jix7qGut9sTgTfS4Y0Bw07dVJYFjwtu3ITG3L8gnRK9O")
CONSUMER_SECRET = os.getenv("CONSUMER_SECRET", "BgRrGavVoRBuLXkyvjEubmGOiRRWfj0SG9l7bQGGMSU8WXgjgmOawiueO2DeVp5V")
BUSINESS_SHORTCODE = os.getenv("BUSINESS_SHORTCODE", "174379")
PASSKEY = os.getenv("PASSKEY", "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919")
CALLBACK_URL = os.getenv("CALLBACK_URL", "https://hotspot-vcja.onrender.com/mpesa/callback")

# Test phone number from Daraja docs
TEST_PHONE = "254708374149"

# MikroTik config
ROUTER_IP = os.getenv("ROUTER_IP", "192.168.88.1")
ROUTER_USERNAME = os.getenv("ROUTER_USERNAME", "admin")
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "")
ROUTER_PORT = int(os.getenv("ROUTER_PORT", "8728"))

OAUTH_URL = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_URL = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
QUERY_URL = "https://sandbox.safaricom.co.ke/mpesa/stkpushquery/v1/query"

# Rate limiting
last_query_time = {}
TOKEN_CACHE = {"token": None, "expires_at": 0}


# =========================
# MIKROTIK HELPERS
# =========================
def get_mikrotik_connection():
    try:
        from librouteros import connect
        api = connect(
            username=ROUTER_USERNAME,
            password=ROUTER_PASSWORD,
            host=ROUTER_IP,
            port=ROUTER_PORT
        )
        return api
    except Exception as e:
        print("MIKROTIK CONNECTION ERROR:", str(e))
        return None


def allow_customer_on_mikrotik(customer):
    if not customer or not customer.mac_address:
        print("MIKROTIK SKIPPED: customer or MAC missing")
        return False

    api = get_mikrotik_connection()
    if not api:
        return False

    try:
        bindings = list(api.path("ip", "hotspot", "ip-binding"))
        for item in bindings:
            if item.get("mac-address") == customer.mac_address:
                print(f"MIKROTIK: MAC already allowed {customer.mac_address}")
                return True

        api.path("ip", "hotspot", "ip-binding").add(
            mac_address=customer.mac_address,
            type="bypassed",
            comment=f"Paid hotspot user {customer.phone}"
        )
        print(f"MIKROTIK: allowed MAC {customer.mac_address}")
        return True
    except Exception as e:
        print("MIKROTIK MAC ALLOW ERROR:", str(e))
        return False


def remove_customer_from_mikrotik(customer):
    if not customer or not customer.mac_address:
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
                    print(f"MIKROTIK: removed MAC {customer.mac_address}")
                    return True
        return False
    except Exception as e:
        print("MIKROTIK REMOVE ERROR:", str(e))
        return False


def expire_finished_sessions():
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        expired_sessions = db.query(Session).filter(
            Session.status == "active",
            Session.end_time <= now
        ).all()

        for session in expired_sessions:
            customer = db.query(Customer).filter_by(id=session.customer_id).first()
            if customer:
                remove_customer_from_mikrotik(customer)
            session.status = "expired"
            print(f"SESSION EXPIRED: customer_id={session.customer_id}")

        db.commit()
    except Exception as e:
        db.rollback()
        print("SESSION EXPIRY ERROR:", str(e))
    finally:
        db.close()


# =========================
# MPESA HELPERS
# =========================
def get_mpesa_access_token():
    """Get cached token or fetch new one"""
    current_time = time.time()
    
    if TOKEN_CACHE["token"] and current_time < TOKEN_CACHE["expires_at"]:
        return TOKEN_CACHE["token"]
    
    try:
        response = requests.get(
            OAUTH_URL,
            auth=(CONSUMER_KEY, CONSUMER_SECRET),
            timeout=30
        )
        print(f"TOKEN STATUS: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            token = data["access_token"]
            TOKEN_CACHE["token"] = token
            TOKEN_CACHE["expires_at"] = current_time + 3000
            print("✅ Token obtained successfully")
            return token
        else:
            print(f"❌ Token error: {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error getting token: {str(e)}")
        return None


def generate_password(shortcode, passkey, timestamp):
    raw_string = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(raw_string.encode()).decode()


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

    print(f"📤 Sending STK Push to {phone} for KES {amount}")
    print(f"📝 Callback URL: {CALLBACK_URL}")
    
    response = requests.post(STK_PUSH_URL, json=payload, headers=headers, timeout=30)
    result = response.json()
    print(f"📊 STK Response: {result}")
    return result


def query_mpesa_status(checkout_request_id):
    """Query M-Pesa API with rate limiting"""
    current_time = time.time()
    if checkout_request_id in last_query_time:
        time_since_last = current_time - last_query_time[checkout_request_id]
        if time_since_last < 15:
            print(f"⏳ Rate limited: waiting {15 - time_since_last:.0f}s")
            return "pending", None
    
    last_query_time[checkout_request_id] = current_time
    
    try:
        token = get_mpesa_access_token()
        if not token:
            return "pending", None
            
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
            "CheckoutRequestID": checkout_request_id
        }
        
        print(f"🔍 Querying M-Pesa for: {checkout_request_id}")
        response = requests.post(QUERY_URL, json=payload, headers=headers, timeout=30)
        result = response.json()
        
        print(f"📊 M-PESA Query Response: {result}")
        
        result_code = result.get("ResultCode")
        
        if result_code == "0":
            return "paid", result
        elif result_code == "1037":
            return "pending", None
        elif result_code == "1032":
            return "failed", None
        else:
            return "pending", None
            
    except Exception as e:
        print(f"❌ Error querying M-Pesa: {str(e)}")
        return "pending", None


def activate_payment_and_session(payment, query_result=None):
    """Activate payment and create session"""
    db = SessionLocal()
    try:
        if payment.status == "paid":
            return True
            
        # Extract receipt number
        receipt_number = None
        if query_result and query_result.get("CallbackMetadata"):
            for item in query_result["CallbackMetadata"].get("Item", []):
                if item.get("Name") == "MpesaReceiptNumber":
                    receipt_number = item.get("Value")
                    break
        
        payment.status = "paid"
        if receipt_number:
            payment.receipt_number = receipt_number
        db.commit()
        
        # Get or create customer
        customer = db.query(Customer).filter_by(phone=payment.phone).first()
        if not customer:
            customer = Customer(phone=payment.phone)
            db.add(customer)
            db.flush()
        
        # Get package
        package = db.query(Package).filter_by(id=payment.package_id).first()
        if package:
            # End any existing active session
            existing_active = db.query(Session).filter_by(
                customer_id=customer.id,
                status="active"
            ).first()
            if existing_active:
                existing_active.status = "expired"
            
            start_time = datetime.utcnow()
            end_time = start_time + timedelta(hours=package.duration_hours)
            
            new_session = Session(
                customer_id=customer.id,
                package_id=package.id,
                start_time=start_time,
                end_time=end_time,
                status="active"
            )
            db.add(new_session)
            db.commit()
            
            # Enable on MikroTik
            allow_customer_on_mikrotik(customer)
        
        print(f"✅ Payment activated: {payment.checkout_request_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error activating payment: {str(e)}")
        db.rollback()
        return False
    finally:
        db.close()


# =========================
# ROUTES
# =========================
@app.route('/')
def home():
    expire_finished_sessions()
    db = SessionLocal()
    try:
        mac = request.args.get("mac", "")
        ip = request.args.get("ip", "")

        db_packages = db.query(Package).all()
        packages = []

        for pkg in db_packages:
            packages.append({
                "id": pkg.id,
                "name": pkg.name,
                "price": pkg.price,
                "duration_hours": pkg.duration_hours,
                "speed": f"{pkg.duration_hours} hour access"
            })

        return render_template("index.html", packages=packages, mac=mac, ip=ip)
    finally:
        db.close()


@app.route('/pay', methods=['POST'])
def pay():
    db = SessionLocal()
    try:
        data = request.get_json()
        phone = normalize_kenyan_phone(data.get("phone", ""))
        package_name = data.get("package_name")
        amount = data.get("amount")
        mac_address = data.get("mac_address")
        ip_address = data.get("ip_address")

        if not phone or not package_name or not amount:
            return jsonify({"success": False, "message": "Missing payment details"}), 400

        package = db.query(Package).filter_by(name=package_name).first()
        if not package:
            return jsonify({"success": False, "message": "Package not found"}), 404

        response = stk_push(
            phone=phone,
            amount=amount,
            account_reference=package.name,
            transaction_desc=f"Hotspot {package.name}"
        )

        checkout_request_id = response.get("CheckoutRequestID")
        response_code = response.get("ResponseCode")

        if response_code == "0" and checkout_request_id:
            payment = Payment(
                checkout_request_id=checkout_request_id,
                phone=phone,
                package_id=package.id,
                amount=float(amount),
                status="pending",
                receipt_number=None
            )
            db.add(payment)

            customer = db.query(Customer).filter_by(phone=phone).first()
            if not customer:
                customer = Customer(phone=phone, ip_address=ip_address, mac_address=mac_address)
                db.add(customer)
            db.commit()

            return jsonify({
                "success": True,
                "message": response.get("ResponseDescription"),
                "checkout_request_id": checkout_request_id
            })

        return jsonify({"success": False, "message": response.get("ResponseDescription")}), 400

    except Exception as e:
        db.rollback()
        print(f"PAY ERROR: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        db.close()


@app.route('/waiting/<checkout_request_id>')
def waiting(checkout_request_id):
    return render_template("waiting.html", checkout_request_id=checkout_request_id)


@app.route('/payment-status/<checkout_request_id>')
def payment_status(checkout_request_id):
    """Check payment status with rate limiting"""
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()

        if not payment:
            return jsonify({"status": "pending"})
        
        if payment.status == "paid":
            return jsonify({"status": "paid"})
        
        if payment.status == "failed":
            return jsonify({"status": "failed"})
        
        # Query M-Pesa
        status, query_result = query_mpesa_status(checkout_request_id)
        
        if status == "paid":
            activate_payment_and_session(payment, query_result)
            return jsonify({"status": "paid"})
        elif status == "failed":
            payment.status = "failed"
            db.commit()
            return jsonify({"status": "failed"})
        else:
            return jsonify({"status": "pending"})
        
    except Exception as e:
        print(f"Status error: {str(e)}")
        return jsonify({"status": "pending"})
    finally:
        db.close()


@app.route('/success/<checkout_request_id>')
def success(checkout_request_id):
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()
        
        # Get the original URL the user wanted to visit
        original_url = request.args.get('url', request.args.get('link-orig', 'https://www.google.com'))
        
        return render_template("success.html", payment=payment, original_url=original_url)
    finally:
        db.close()

@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    callback_data = request.get_json(force=True)
    print("📞 CALLBACK RECEIVED:", callback_data)

    db = SessionLocal()
    try:
        stk_callback = callback_data["Body"]["stkCallback"]
        checkout_request_id = stk_callback.get("CheckoutRequestID")
        result_code = stk_callback.get("ResultCode")

        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()

        if payment and result_code == 0:
            # Extract receipt from callback
            callback_items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            for item in callback_items:
                if item.get("Name") == "MpesaReceiptNumber":
                    payment.receipt_number = item.get("Value")
                    break
            
            payment.status = "paid"
            db.commit()
            
            # Activate session
            customer = db.query(Customer).filter_by(phone=payment.phone).first()
            if not customer:
                customer = Customer(phone=payment.phone)
                db.add(customer)
                db.flush()

            package = db.query(Package).filter_by(id=payment.package_id).first()
            if package:
                start_time = datetime.utcnow()
                end_time = start_time + timedelta(hours=package.duration_hours)
                new_session = Session(
                    customer_id=customer.id,
                    package_id=package.id,
                    start_time=start_time,
                    end_time=end_time,
                    status="active"
                )
                db.add(new_session)
                db.commit()
                allow_customer_on_mikrotik(customer)
            
            print(f"✅ Callback activated: {checkout_request_id}")
        elif payment:
            payment.status = "failed"
            db.commit()

        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

    except Exception as e:
        db.rollback()
        print(f"CALLBACK ERROR: {str(e)}")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    finally:
        db.close()


@app.route('/admin')
def admin_dashboard():
    db = SessionLocal()
    try:
        payments = db.query(Payment).order_by(Payment.id.desc()).all()
        customers = db.query(Customer).order_by(Customer.id.desc()).all()
        sessions = db.query(Session).order_by(Session.id.desc()).all()

        active_sessions = [s for s in sessions if s.status == "active"]
        expired_sessions = [s for s in sessions if s.status == "expired"]
        total_amount = sum([p.amount for p in payments if p.status == "paid"])

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


@app.route('/test-mpesa')
def test_mpesa():
    """Test endpoint to verify M-Pesa credentials work"""
    token = get_mpesa_access_token()
    if token:
        return jsonify({
            "status": "success", 
            "message": "✅ M-Pesa credentials working!",
            "token_preview": token[:50] + "..."
        })
    else:
        return jsonify({
            "status": "error", 
            "message": "❌ Failed to get token. Check your Consumer Key and Secret"
        }), 500


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)