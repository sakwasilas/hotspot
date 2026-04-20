import os
import base64
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

from connections import SessionLocal
from models import Payment, Package, Customer, Session

load_dotenv()

app = Flask(__name__)

# =========================
# ENV CONFIG
# =========================
CONSUMER_KEY = os.getenv("CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("CONSUMER_SECRET")
BUSINESS_SHORTCODE = os.getenv("BUSINESS_SHORTCODE", "174379")
PASSKEY = os.getenv("PASSKEY")
CALLBACK_URL = os.getenv("CALLBACK_URL", "").strip()

# MikroTik config
ROUTER_IP = os.getenv("ROUTER_IP", "192.168.88.1")
ROUTER_USERNAME = os.getenv("ROUTER_USERNAME", "admin")
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "")
ROUTER_PORT = int(os.getenv("ROUTER_PORT", "8728"))

OAUTH_URL = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_URL = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"


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
    """
    Allow paid hotspot user by MAC address using ip-binding.
    """
    if not customer or not customer.mac_address:
        print("MIKROTIK SKIPPED: customer or MAC missing")
        return False

    api = get_mikrotik_connection()
    if not api:
        return False

    try:
        bindings = list(api.path("ip", "hotspot", "ip-binding"))
        existing = None

        for item in bindings:
            if item.get("mac-address") == customer.mac_address:
                existing = item
                break

        if existing:
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
    """
    Remove hotspot bypass for expired user by MAC address.
    """
    if not customer or not customer.mac_address:
        print("MIKROTIK REMOVE SKIPPED: customer or MAC missing")
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

        print(f"MIKROTIK: no binding found for {customer.mac_address}")
        return False

    except Exception as e:
        print("MIKROTIK REMOVE ERROR:", str(e))
        return False


def expire_finished_sessions():
    """
    Find expired active sessions, remove access from MikroTik,
    and mark them as expired.
    """
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
            print(f"SESSION EXPIRED: customer_id={session.customer_id}, session_id={session.id}")

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
    response = requests.get(
        OAUTH_URL,
        auth=(CONSUMER_KEY, CONSUMER_SECRET),
        timeout=30
    )
    print("TOKEN STATUS:", response.status_code)
    print("TOKEN RESPONSE:", response.text)
    response.raise_for_status()
    return response.json()["access_token"]


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

    print("STK PAYLOAD:", payload)

    response = requests.post(
        STK_PUSH_URL,
        json=payload,
        headers=headers,
        timeout=30
    )

    print("STK STATUS CODE:", response.status_code)
    print("STK RESPONSE TEXT:", response.text)

    return response.json()


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
        link_login = request.args.get("link-login", "")
        link_orig = request.args.get("link-orig", "")

        db_packages = db.query(Package).all()

        packages = []
        for pkg in db_packages:
            packages.append({
                "id": pkg.id,
                "name": pkg.name,
                "price": pkg.price,
                "speed": "MAX 5mbps"
            })

        return render_template(
            "index.html",
            packages=packages,
            mac=mac,
            ip=ip,
            link_login=link_login,
            link_orig=link_orig
        )
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
            return jsonify({
                "success": False,
                "message": "Missing or invalid payment details"
            }), 400

        package = db.query(Package).filter_by(name=package_name).first()
        if not package:
            return jsonify({
                "success": False,
                "message": "Selected package not found"
            }), 404

        response = stk_push(
            phone=phone,
            amount=amount,
            account_reference=package.name,
            transaction_desc=f"Hotspot {package.name}"
        )

        print("FULL STK RESPONSE:", response)

        checkout_request_id = response.get("CheckoutRequestID")
        response_code = response.get("ResponseCode")
        response_desc = response.get("ResponseDescription", "Request sent")

        if response_code == "0" and checkout_request_id:
            existing_payment = db.query(Payment).filter_by(
                checkout_request_id=checkout_request_id
            ).first()

            if not existing_payment:
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
                customer = Customer(
                    phone=phone,
                    ip_address=ip_address,
                    mac_address=mac_address
                )
                db.add(customer)
            else:
                customer.ip_address = ip_address
                customer.mac_address = mac_address

            db.commit()

            return jsonify({
                "success": True,
                "message": response_desc,
                "checkout_request_id": checkout_request_id
            })

        return jsonify({
            "success": False,
            "message": response_desc,
            "raw_response": response
        }), 400

    except Exception as e:
        import traceback
        traceback.print_exc()
        db.rollback()
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500
    finally:
        db.close()


@app.route('/waiting/<checkout_request_id>')
def waiting(checkout_request_id):
    return render_template("waiting.html", checkout_request_id=checkout_request_id)


@app.route('/payment-status/<checkout_request_id>')
def payment_status(checkout_request_id):
    db = SessionLocal()
    try:
        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()

        if payment:
            return jsonify({"status": payment.status})

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

        return render_template("success.html", payment=payment)
    finally:
        db.close()


@app.route('/expire-sessions')
def expire_sessions():
    expire_finished_sessions()
    return "Expired session check completed"


@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    callback_data = request.get_json(force=True)
    print("CALLBACK RECEIVED:", callback_data)

    db = SessionLocal()
    try:
        stk_callback = callback_data["Body"]["stkCallback"]
        checkout_request_id = stk_callback.get("CheckoutRequestID")
        result_code = stk_callback.get("ResultCode")
        result_desc = stk_callback.get("ResultDesc")

        payment = db.query(Payment).filter_by(
            checkout_request_id=checkout_request_id
        ).first()

        if not payment:
            return jsonify({
                "ResultCode": 0,
                "ResultDesc": "Accepted"
            })

        print("CALLBACK RESULT CODE:", result_code)
        print("CALLBACK RESULT DESC:", result_desc)

        if result_code == 0:
            callback_items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            parsed_items = {}

            for item in callback_items:
                name = item.get("Name")
                value = item.get("Value")
                parsed_items[name] = value

            payment.status = "paid"
            payment.receipt_number = parsed_items.get("MpesaReceiptNumber")

            customer = db.query(Customer).filter_by(phone=payment.phone).first()
            if not customer:
                customer = Customer(phone=payment.phone)
                db.add(customer)
                db.flush()

            package = db.query(Package).filter_by(id=payment.package_id).first()
            if package:
                start_time = datetime.utcnow()
                end_time = start_time + timedelta(hours=package.duration_hours)

                existing_active_session = db.query(Session).filter_by(
                    customer_id=customer.id,
                    package_id=package.id,
                    status="active"
                ).first()

                if not existing_active_session:
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

        else:
            payment.status = "failed"
            db.commit()

        return jsonify({
            "ResultCode": 0,
            "ResultDesc": "Accepted"
        })

    except Exception as e:
        db.rollback()
        print("CALLBACK PARSING ERROR:", str(e))
        return jsonify({
            "ResultCode": 0,
            "ResultDesc": "Accepted with parsing note"
        })
    finally:
        db.close()

'admin dashboard'
@app.route('/admin')
def admin_dashboard():
    db = SessionLocal()
    try:
        payments = db.query(Payment).order_by(Payment.id.desc()).all()
        customers = db.query(Customer).order_by(Customer.id.desc()).all()
        sessions = db.query(Session).order_by(Session.id.desc()).all()

        active_sessions = [s for s in sessions if s.status == "active"]
        expired_sessions = [s for s in sessions if s.status == "expired"]

        # 🔥 THIS IS THE FIX
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

if __name__ == '__main__':
    app.run(debug=True)