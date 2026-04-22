import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
CORS(app, origins=["*"], allow_headers=["*"], methods=["POST", "GET", "OPTIONS"])

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'images')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico'}
UPLOAD_TOKEN = os.getenv('GOV_UPLOAD_TOKEN', '')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def check_token():
    if not UPLOAD_TOKEN:
        return True
    auth_header = request.headers.get('Authorization', '')
    token_param = request.args.get('token') or request.form.get('token', '')
    provided = auth_header.replace('Bearer ', '').strip() or token_param.strip()
    return provided == UPLOAD_TOKEN

@app.route('/upload_receiver', methods=['POST', 'OPTIONS'])
def upload_receiver():
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    
    if not check_token():
        return jsonify({'success': False, 'error': 'Unauthorised'}), 401
    
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'success': False, 'error': 'No file provided'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'File type not allowed'}), 400
    
    timestamp = int(datetime.now().timestamp())
    safe_name = secure_filename(file.filename)
    filename  = f"{timestamp}_{safe_name}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    try:
        file.save(save_path)
        print(f"[upload_receiver] File saved: {filename}")
    except Exception as e:
        print(f"[upload_receiver] Error saving file: {e}")
        return jsonify({'success': False, 'error': f'Could not save file: {str(e)}'}), 500
    
    return jsonify({'success': True, 'filename': filename}), 200

@app.route('/health', methods=['GET', 'OPTIONS'])
def health():
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    
    return jsonify({'status': 'ok', 'upload_folder': UPLOAD_FOLDER}), 200

@app.errorhandler(400)
def bad_request(e):
    return jsonify({'success': False, 'error': 'Bad request'}), 400

@app.errorhandler(401)
def unauthorized(e):
    return jsonify({'success': False, 'error': 'Unauthorized'}), 401

@app.errorhandler(404)
def not_found(e):
    return jsonify({'success': False, 'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({'success': False, 'error': 'Internal server error'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
