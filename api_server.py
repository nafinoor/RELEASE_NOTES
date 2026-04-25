import os
import json
from datetime import datetime
from flask import Flask, jsonify, request, make_response, stream_with_context
from dotenv import load_dotenv
from flask_cors import CORS, cross_origin
import logging
import queue
import threading

load_dotenv()

from release_note_generator import run_generator, get_week_range

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

progress_queue = queue.Queue()


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


RELEASE_NOTES_FILE = "release_notes.json"


def save_to_file(data, filename):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_from_file(filename):
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def generate_progress():
    while True:
        try:
            msg = progress_queue.get(timeout=30)
            yield f"data: {json.dumps(msg)}\n\n"
        except queue.Empty:
            yield f"data: {json.dumps({'status': 'done'})}\n\n"
            break


@app.route('/api/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/api/release-notes', methods=['GET'])
def get_release_notes():
    week_start = request.args.get('week_start')
    week_end = request.args.get('week_end')
    
    token = os.environ.get("GITHUB_TOKEN", "")
    fe_repo = os.environ.get("FE_REPO", "")
    be_repo = os.environ.get("BE_REPO", "")
    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_url = os.environ.get("LLM_API_URL", "")
    
    logger.info(f"GET /api/release-notes called - FE_REPO: {fe_repo}")
    
    if not token or not fe_repo or not be_repo:
        logger.error("Missing required environment variables")
        return jsonify({"error": "Missing required environment variables"}), 500
    
    try:
        logger.info("Starting run_generator...")
        message = run_generator(token, fe_repo, be_repo, llm_key, llm_url)
        logger.info(f"Generator returned message length: {len(message)}")
        
        week_start_dt, week_end_dt = get_week_range()
        week_key = week_start_dt.strftime('%Y-%m-%d')
        
        data = {
            "week_start": week_key,
            "week_end": week_end_dt.strftime('%Y-%m-%d'),
            "generated_at": datetime.now().isoformat(),
            "content": message
        }
        
        all_notes = load_from_file(RELEASE_NOTES_FILE)
        all_notes[week_key] = data
        save_to_file(all_notes, RELEASE_NOTES_FILE)
        logger.info(f"Saved release note to {week_key}")
        
        return jsonify(data)
    except Exception as e:
        import traceback
        logger.error(f"Error: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route('/api/release-notes/<week_start>', methods=['GET'])
def get_release_note_by_week(week_start):
    all_notes = load_from_file(RELEASE_NOTES_FILE)
    
    if week_start in all_notes:
        return jsonify(all_notes[week_start])
    
    return jsonify({"error": "Release notes not found for this week"}), 404


@app.route('/api/release-notes', methods=['POST'])
def generate_release_notes():
    data = request.get_json() or {}
    
    token = data.get('github_token') or os.environ.get("GITHUB_TOKEN", "")
    fe_repo = data.get('fe_repo') or os.environ.get("FE_REPO", "")
    be_repo = data.get('be_repo') or os.environ.get("BE_REPO", "")
    llm_key = data.get('llm_api_key') or os.environ.get("LLM_API_KEY", "")
    llm_url = data.get('llm_api_url') or os.environ.get("LLM_API_URL", "")
    
    logger.info(f"POST /api/release-notes called - FE_REPO: {fe_repo}")
    
    if not token or not fe_repo or not be_repo:
        logger.error("Missing required parameters")
        return jsonify({"error": "Missing required parameters"}), 400
    
    try:
        logger.info("Starting run_generator...")
        message = run_generator(token, fe_repo, be_repo, llm_key, llm_url)
        logger.info(f"Generator returned message length: {len(message)}")
        
        week_start_dt, week_end_dt = get_week_range()
        week_key = week_start_dt.strftime('%Y-%m-%d')
        
        result = {
            "week_start": week_key,
            "week_end": week_end_dt.strftime('%Y-%m-%d'),
            "generated_at": datetime.now().isoformat(),
            "content": message
        }
        
        all_notes = load_from_file(RELEASE_NOTES_FILE)
        all_notes[week_key] = result
        save_to_file(all_notes, RELEASE_NOTES_FILE)
        logger.info(f"Saved release note to {week_key}")
        
        return jsonify(result)
    except Exception as e:
        import traceback
        logger.error(f"Error: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")


@app.route('/api/v1/admin/release-notes', methods=['POST'])
def admin_get_release_notes():
    data = request.get_json() or {}
    password = data.get("password", "")
    
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    all_notes = load_from_file(RELEASE_NOTES_FILE)
    return jsonify({"data": all_notes})


@app.route('/api/v1/admin/release-notes/update', methods=['POST'])
def admin_update_release_note():
    data = request.get_json() or {}
    password = data.get("password", "")
    week_start = data.get("week_start", "")
    content = data.get("content", "")
    
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    if not week_start:
        return jsonify({"error": "Missing week_start"}), 400
    
    all_notes = load_from_file(RELEASE_NOTES_FILE)
    
    if week_start in all_notes:
        all_notes[week_start]["content"] = content
        save_to_file(all_notes, RELEASE_NOTES_FILE)
        return jsonify({"data": all_notes[week_start]})
    
    return jsonify({"error": "Release note not found"}), 404


@app.route('/api/v1/admin/release-notes/generate', methods=['POST'])
def admin_generate_release_note():
    data = request.get_json() or {}
    password = data.get("password", "")
    
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    
    token = os.environ.get("GITHUB_TOKEN", "")
    fe_repo = os.environ.get("FE_REPO", "")
    be_repo = os.environ.get("BE_REPO", "")
    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_url = os.environ.get("LLM_API_URL", "")
    
    if not token or not fe_repo or not be_repo:
        return jsonify({"error": "Missing GitHub configuration"}), 500
    
    import threading
    import time
    
    result_container = {"data": None, "error": None}
    
    def run_generator_in_thread():
        try:
            message = run_generator(token, fe_repo, be_repo, "", llm_key, llm_url)
            week_start_dt, week_end_dt = get_week_range()
            week_key = week_start_dt.strftime('%Y-%m-%d')
            
            result_data = {
                "week_start": week_key,
                "week_end": week_end_dt.strftime('%Y-%m-%d'),
                "generated_at": datetime.now().isoformat(),
                "content": message
            }
            
            all_notes = load_from_file(RELEASE_NOTES_FILE)
            all_notes[week_key] = result_data
            save_to_file(all_notes, RELEASE_NOTES_FILE)
            
            result_container["data"] = result_data
        except Exception as e:
            result_container["error"] = str(e)
    
    thread = threading.Thread(target=run_generator_in_thread)
    thread.start()
    
    def generate():
        statuses = [
            "Connecting to GitHub...",
            "Fetching PRs...",
            "Processing changes...",
            "Formatting...",
            "Saving...",
        ]
        
        for status in statuses:
            if not thread.is_alive():
                break
            yield f"data: {json.dumps({'status': status})}\n\n"
            time.sleep(1.5)
        
        thread.join()
        
        if result_container.get("error"):
            yield f"data: {json.dumps({'status': 'error', 'error': result_container['error']})}\n\n"
        elif result_container.get("data"):
            yield f"data: {json.dumps({'status': 'Ready!', 'data': result_container['data']})}\n\n"
        else:
            yield f"data: {json.dumps({'status': 'error', 'error': 'Unknown error'})}\n\n"
    
    return app.response_class(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)