import os
import json
import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps
from dotenv import load_dotenv
# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore, auth

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
# FLASK_SECRET_KEY is still good practice for internal Flask security
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev_key_fallback')

# --- FIREBASE SETUP ---
# In production (Cloud Run), we use the JSON string from the environment variable.
# In local dev, we can fallback to a file if you prefer, or just set the ENV var locally.
firebase_creds_json = os.getenv('FIREBASE_CREDENTIALS')

if firebase_creds_json:
    cred = credentials.Certificate(json.loads(firebase_creds_json))
else:
    # If no env var, try looking for a local file (for local testing convenience)
    # You should download your key and name it 'service-account-file.json' for local dev
    try:
        cred = credentials.Certificate('service-account-file.json')
    except Exception:
        print("WARNING: No Firebase Credentials found. App will crash on DB access.")
        cred = None

if cred:
    firebase_admin.initialize_app(cred)
    db = firestore.client()
else:
    db = None

# --- DECORATORS ---

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS': return jsonify({}), 200
        
        token_header = request.headers.get('Authorization')
        if not token_header or not token_header.startswith('Bearer '):
            return jsonify({'message': 'Token missing'}), 401
        
        token = token_header.split(" ")[1]
        try:
            # Verify the ID token sent by the frontend (Firebase Client SDK)
            decoded_token = auth.verify_id_token(token)
            uid = decoded_token['uid']
            
            # Fetch user details from Firestore 'users' collection
            # We store extra data (role, phone) that Auth doesn't hold natively
            user_doc = db.collection('users').document(uid).get()
            
            if user_doc.exists:
                user_data = user_doc.to_dict()
                user_data['user_id'] = uid # Normalize ID access
            else:
                # Fallback if user registered via Auth but not DB (shouldn't happen)
                user_data = {'user_id': uid, 'role': 'user', 'username': 'Unknown'}

            request.user_data = user_data
            
        except Exception as e:
            return jsonify({'message': 'Invalid Token', 'error': str(e)}), 401
            
        return f(*args, **kwargs)
    return decorated

def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if request.method == 'OPTIONS': return jsonify({}), 200
            
            # Re-use the logic from token_required or assume it's chained
            # For safety, we'll re-verify or rely on request.user_data set by token_required
            if not hasattr(request, 'user_data'):
                return jsonify({'message': 'Auth required'}), 401
            
            user_role = request.user_data.get('role', 'user')
            if user_role not in allowed_roles:
                return jsonify({'message': 'Access denied'}), 403
            
            return f(*args, **kwargs)
        return decorated
    return decorator

# --- AUTH ROUTES ---

# Note: Login is handled entirely on the Frontend using Firebase SDK.
# This route creates the User Document in Firestore after they sign up.
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    uid = data.get('uid') # Passed from frontend after successful Firebase Auth creation
    email = data.get('email')
    
    if not uid:
        return jsonify({"message": "UID missing"}), 400

    try:
        # Create user document
        user_data = {
            'full_name': data.get('fullName'),
            'username': data.get('username'),
            'phone_number': data.get('phone_number'),
            'email': email,
            'role': 'user', # Default role
            'created_at': firestore.SERVER_TIMESTAMP
        }
        
        # Use UID as the document ID for easy lookup
        db.collection('users').document(uid).set(user_data)
        
        return jsonify({"message": "Registered successfully"}), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 400

# --- USER PROFILE ---

@app.route('/api/user/me', methods=['GET'])
@token_required
def get_user_profile():
    # request.user_data is already populated by the decorator
    return jsonify(request.user_data), 200

@app.route('/api/user/me', methods=['PUT'])
@token_required
def update_user_profile():
    uid = request.user_data['user_id']
    data = request.json
    
    try:
        update_data = {
            'full_name': data.get('full_name'),
            'email': data.get('email'),
            'phone_number': data.get('phone_number')
        }
        # Remove None values
        update_data = {k: v for k, v in update_data.items() if v is not None}
        
        db.collection('users').document(uid).update(update_data)
        return jsonify({"message": "Profile updated"}), 200
    except Exception as e:
        return jsonify({"message": f"Update error: {e}"}), 400

# --- CATALOG & PRODUCTS ---

@app.route('/api/products', methods=['GET'])
def get_products():
    try:
        # Query: status == 'available'
        docs = db.collection('products').where('status', '==', 'available').stream()
        
        products = []
        for doc in docs:
            p = doc.to_dict()
            p['id'] = doc.id # Firestore ID is a string
            products.append(p)
            
        return jsonify(products), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/borrow', methods=['POST'])
@token_required
def borrow_product():
    data = request.json
    product_id = data.get('product_id') # String ID
    returned_date_str = data.get('returned_date')
    uid = request.user_data['user_id']
    
    try:
        # 1. Fetch Limits
        settings_ref = db.collection('system_settings').document('config').get()
        settings = settings_ref.to_dict() if settings_ref.exists else {}
        max_items = int(settings.get('max_borrow_items', 3))
        max_days = int(settings.get('max_borrow_days', 14))

        # 2. Check User's Current Borrow Count
        # Firestore cannot easily do "count" without reading, but 'count()' aggregation is available in newer SDKs.
        # For simplicity, we stream query.
        active_requests = db.collection('borrow_requests')\
            .where('user_id', '==', uid)\
            .where('status', 'in', ['pending', 'approved', 'confirmation_pending'])\
            .get()
            
        if len(active_requests) >= max_items:
            return jsonify({"message": f"הגעת למכסת ההשאלות שלך ({max_items} פריטים)."}), 400

        # 3. Validate Date
        return_date = datetime.datetime.strptime(returned_date_str, "%Y-%m-%d").date()
        today = datetime.datetime.now().date()
        
        if return_date <= today:
            return jsonify({"message": "תאריך ההחזרה חייב להיות עתידי"}), 400
            
        if (return_date - today).days > max_days:
            return jsonify({"message": f"תקופת ההשאלה חורגת מהמותר ({max_days} ימים)."}), 400

        # 4. Transactional Update (Ensure product is still available)
        product_ref = db.collection('products').document(product_id)
        
        # We use a transaction to ensure atomicity
        transaction = db.transaction()
        
        @firestore.transactional
        def borrow_in_transaction(transaction, product_ref):
            snapshot = product_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise Exception("Product not found")
            
            if snapshot.get('status') != 'available':
                raise Exception("Product not available")
            
            # Update product
            transaction.update(product_ref, {'status': 'unavailable'})
            
            # Create request
            new_req_ref = db.collection('borrow_requests').document()
            transaction.set(new_req_ref, {
                'user_id': uid,
                'product_id': product_id,
                'product_name': snapshot.get('product_name'), # Denormalize name for easier display
                'returned_date': returned_date_str,
                'request_date': firestore.SERVER_TIMESTAMP,
                'status': 'pending'
            })
            
        borrow_in_transaction(transaction, product_ref)
        return jsonify({"message": "בקשתך נשלחה בהצלחה!"}), 200

    except Exception as e:
        return jsonify({"message": str(e)}), 400

@app.route('/api/my-requests', methods=['GET'])
@token_required
def get_my_requests():
    uid = request.user_data['user_id']
    try:
        docs = db.collection('borrow_requests')\
            .where('user_id', '==', uid)\
            .order_by('request_date', direction=firestore.Query.DESCENDING)\
            .stream()
            
        requests = []
        for doc in docs:
            r = doc.to_dict()
            # Handle date formatting
            req_date = r.get('request_date')
            if req_date:
                r['date'] = req_date.strftime("%Y-%m-%d %H:%M")
            
            r['id'] = doc.id
            r['product'] = r.get('product_name', 'Unknown') # Use denormalized name
            requests.append(r)
            
        return jsonify(requests), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/donate', methods=['POST'])
@token_required
def request_donation():
    data = request.json
    try:
        db.collection('donation_requests').add({
            'product_name': data.get('product_name'),
            'category': data.get('category'),
            'description': data.get('description'),
            'donator_username': data.get('donator_username'),
            'status': 'donation_pending',
            'created_at': firestore.SERVER_TIMESTAMP
        })
        return jsonify({"message": "Donation request submitted"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- EMPLOYEE ROUTES ---

@app.route('/api/employee/products', methods=['GET'])
@token_required
@role_required(['admin', 'employee'])
def get_all_products_employee():
    # Firestore doesn't support complex JOINs easily. 
    # For a small dataset, we fetch items and populate borrower info if needed.
    # For this simplified migration, we will fetch products and their statuses.
    try:
        docs = db.collection('products').order_by('publish_date', direction=firestore.Query.DESCENDING).stream()
        products = []
        for doc in docs:
            p = doc.to_dict()
            p['id'] = doc.id
            products.append(p)
        return jsonify(products), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/employee/products', methods=['POST'])
@token_required
@role_required(['admin', 'employee'])
def create_product():
    data = request.json
    try:
        new_prod = {
            'product_name': data.get('product_name'),
            'category': data.get('category'),
            'description': data.get('description'),
            'donator_username': data.get('donator_username'),
            'status': 'available',
            'publish_date': firestore.SERVER_TIMESTAMP
        }
        update_time, ref = db.collection('products').add(new_prod)
        return jsonify({"message": "Product created", "id": ref.id}), 201
    except Exception as e:
        return jsonify({"message": str(e)}), 400

@app.route('/api/employee/products/<product_id>', methods=['PUT'])
@token_required
@role_required(['admin', 'employee'])
def update_product(product_id):
    data = request.json
    try:
        db.collection('products').document(product_id).update({
            'product_name': data.get('product_name'),
            'category': data.get('category'),
            'description': data.get('description'),
            'donator_username': data.get('donator_username'),
            'status': data.get('status')
        })
        return jsonify({"message": "Product updated"}), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 400

@app.route('/api/employee/products/<product_id>', methods=['DELETE'])
@token_required
@role_required(['admin', 'employee'])
def delete_product(product_id):
    try:
        db.collection('products').document(product_id).delete()
        return jsonify({"message": "Product deleted"}), 204
    except Exception as e:
        return jsonify({"message": str(e)}), 500

@app.route('/api/employee/products/<product_id>', methods=['GET'])
@token_required
@role_required(['admin', 'employee'])
def get_single_product(product_id):
    try:
        doc = db.collection('products').document(product_id).get()
        if doc.exists:
            data = doc.to_dict()
            data['id'] = doc.id
            return jsonify(data), 200
        return jsonify({"message": "Not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- EMPLOYEE REQUESTS MANAGEMENT ---

@app.route('/api/employee/requests', methods=['GET'])
@token_required
@role_required(['admin', 'employee'])
def get_all_requests():
    try:
        # Get pending requests
        docs = db.collection('borrow_requests').where('status', '==', 'pending').stream()
        requests = []
        for doc in docs:
            r = doc.to_dict()
            r['id'] = doc.id
            
            # Fetch Username (Manual Join)
            user_doc = db.collection('users').document(r['user_id']).get()
            username = user_doc.to_dict().get('username') if user_doc.exists else 'Unknown'
            
            requests.append({
                'id': doc.id,
                'username': username,
                'product': r.get('product_name'),
                'status': r.get('status'),
                'date': str(r.get('request_date')),
                'returned_date': r.get('returned_date')
            })
        return jsonify(requests), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/employee/requests/<req_id>', methods=['PUT'])
@token_required
@role_required(['admin', 'employee'])
def update_request_status(req_id):
    data = request.json
    new_status = data.get('status')
    
    try:
        req_ref = db.collection('borrow_requests').document(req_id)
        req_doc = req_ref.get()
        if not req_doc.exists: return jsonify({"message": "Not found"}), 404
        
        req_data = req_doc.to_dict()
        product_id = req_data['product_id']
        
        batch = db.batch()
        
        if new_status == 'rejected':
            batch.update(req_ref, {'status': 'rejected', 'returned_date': None})
            batch.update(db.collection('products').document(product_id), {'status': 'available'})
        elif new_status == 'approved':
            batch.update(req_ref, {'status': 'approved'})
            batch.update(db.collection('products').document(product_id), {'status': 'borrowed'})
            
        batch.commit()
        return jsonify({"message": "Status updated"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- EXTENSIONS ---

@app.route('/api/extensions', methods=['POST'])
@token_required
def request_extension():
    data = request.json
    borrow_id = data.get('borrow_id')
    new_date = data.get('new_returned_date')
    
    try:
        # Check existing
        existing = db.collection('extension_requests')\
            .where('borrow_id', '==', borrow_id)\
            .where('status', '==', 'extension_pending').get()
            
        if len(existing) > 0:
             return jsonify({"message": "בקשה ממתינה כבר קיימת"}), 400
             
        db.collection('extension_requests').add({
            'borrow_id': borrow_id,
            'new_returned_date': new_date,
            'status': 'extension_pending',
            'user_id': request.user_data['user_id'] # Add user_id for easier querying
        })
        return jsonify({"message": "Request sent"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/employee/extensions', methods=['GET'])
@token_required
@role_required(['admin', 'employee'])
def get_extension_requests_emp():
    try:
        docs = db.collection('extension_requests').where('status', '==', 'extension_pending').stream()
        exts = []
        for doc in docs:
            e = doc.to_dict()
            # Fetch related details (Manual Join)
            borrow_doc = db.collection('borrow_requests').document(e['borrow_id']).get()
            if borrow_doc.exists:
                b_data = borrow_doc.to_dict()
                user_doc = db.collection('users').document(b_data['user_id']).get()
                username = user_doc.to_dict().get('username') if user_doc.exists else 'Unknown'
                
                exts.append({
                    'id': doc.id,
                    'username': username,
                    'product_name': b_data.get('product_name'),
                    'current_return_date': b_data.get('returned_date'),
                    'new_return_date': e.get('new_returned_date')
                })
        return jsonify(exts), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/employee/extensions/<ext_id>', methods=['PUT'])
@token_required
@role_required(['admin', 'employee'])
def update_extension_status(ext_id):
    data = request.json
    status = data.get('status') # 'approved' or 'rejected'
    new_status = f"extension_{status}"
    
    try:
        ext_ref = db.collection('extension_requests').document(ext_id)
        ext_doc = ext_ref.get()
        if not ext_doc.exists: return jsonify({"message": "Not found"}), 404
        
        batch = db.batch()
        batch.update(ext_ref, {'status': new_status})
        
        if status == 'approved':
            borrow_id = ext_doc.to_dict()['borrow_id']
            new_date = ext_doc.to_dict()['new_returned_date']
            batch.update(db.collection('borrow_requests').document(borrow_id), {'returned_date': new_date})
            
        batch.commit()
        return jsonify({"message": "Updated"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- DONATIONS EMPLOYEE ---

@app.route('/api/employee/donations', methods=['GET'])
@token_required
@role_required(['admin', 'employee'])
def get_donations_emp():
    try:
        docs = db.collection('donation_requests').where('status', '==', 'donation_pending').stream()
        res = []
        for doc in docs:
            d = doc.to_dict()
            d['id'] = doc.id
            d['created_at'] = str(d.get('created_at'))
            res.append(d)
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/employee/donations/<don_id>/approve', methods=['POST'])
@token_required
@role_required(['admin', 'employee'])
def approve_donation(don_id):
    data = request.json
    try:
        # Create product
        new_prod = {
            'product_name': data.get('product_name'),
            'category': data.get('category'),
            'description': data.get('description'),
            'donator_username': data.get('donator_username'),
            'status': 'available',
            'publish_date': firestore.SERVER_TIMESTAMP
        }
        db.collection('products').add(new_prod)
        
        # Mark donation approved
        db.collection('donation_requests').document(don_id).update({'status': 'approved'})
        
        return jsonify({"message": "Donation approved"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/employee/donations/<don_id>/reject', methods=['DELETE'])
@token_required
@role_required(['admin', 'employee'])
def reject_donation(don_id):
    try:
        db.collection('donation_requests').document(don_id).delete()
        return jsonify({"message": "Rejected"}), 204
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- RETURN PRODUCT ---

@app.route('/api/return', methods=['POST'])
@token_required
def return_product():
    data = request.json
    borrow_id = data.get('borrow_id')
    uid = request.user_data['user_id']
    
    try:
        req_ref = db.collection('borrow_requests').document(borrow_id)
        req = req_ref.get()
        
        if not req.exists or req.to_dict()['user_id'] != uid or req.to_dict()['status'] != 'approved':
             return jsonify({"message": "Invalid request"}), 404
             
        product_id = req.to_dict()['product_id']
        
        batch = db.batch()
        batch.update(req_ref, {'status': 'returned'})
        batch.update(db.collection('products').document(product_id), {'status': 'available'})
        batch.commit()
        
        return jsonify({"message": "Returned"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ADMIN USER MANAGEMENT ---

@app.route('/api/admin/users', methods=['GET'])
@token_required
@role_required(['admin'])
def get_all_users():
    try:
        # 1. Fetch from Firestore (metadata)
        docs = db.collection('users').stream()
        users = []
        for doc in docs:
            u = doc.to_dict()
            u['id'] = doc.id # Use UID as ID
            users.append(u)
        return jsonify(users), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users/<uid>/role', methods=['PUT'])
@token_required
@role_required(['admin'])
def update_user_role(uid):
    data = request.json
    try:
        db.collection('users').document(uid).update({'role': data.get('role')})
        return jsonify({"message": "Role updated"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/users/<uid>', methods=['DELETE'])
@token_required
@role_required(['admin'])
def delete_user(uid):
    try:
        # Delete from Firestore
        db.collection('users').document(uid).delete()
        # Delete from Firebase Auth
        auth.delete_user(uid)
        return jsonify({"message": "User deleted"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- CONFIG ---

@app.route('/api/config', methods=['GET'])
def get_config():
    try:
        doc = db.collection('system_settings').document('config').get()
        if doc.exists:
            return jsonify(doc.to_dict()), 200
        return jsonify({"max_borrow_days": 14, "max_borrow_items": 3}), 200
    except Exception:
        return jsonify({"max_borrow_days": 14, "max_borrow_items": 3}), 200

@app.route('/api/admin/config', methods=['POST'])
@token_required
@role_required(['admin'])
def update_config():
    data = request.json
    try:
        db.collection('system_settings').document('config').set({
            'max_borrow_days': data.get('max_borrow_days'),
            'max_borrow_items': data.get('max_borrow_items')
        }, merge=True)
        return jsonify({"message": "Config updated"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/borrow-status', methods=['GET'])
@token_required
def get_borrow_status():
    uid = request.user_data['user_id']
    try:
        # Get Config
        settings_ref = db.collection('system_settings').document('config').get()
        settings = settings_ref.to_dict() if settings_ref.exists else {}
        max_items = int(settings.get('max_borrow_items', 3))
        max_days = int(settings.get('max_borrow_days', 14))
        
        # Count active loans
        # Note: Streaming all is inefficient for large scale, but fine for MVP
        docs = db.collection('borrow_requests')\
            .where('user_id', '==', uid)\
            .where('status', 'in', ['pending', 'approved', 'confirmation_pending'])\
            .stream()
            
        current_count = sum(1 for _ in docs)
        
        return jsonify({
            "current_borrowed": current_count,
            "max_items": max_items,
            "remaining_slots": max_items - current_count,
            "max_days": max_days
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Cloud Run will set PORT env var, defaults to 8080
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)