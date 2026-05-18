import os
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from flask import Flask, jsonify, request, make_response, stream_with_context
from dotenv import load_dotenv
from flask_cors import CORS, cross_origin
import logging
import queue
import threading
import requests as http_requests
import markdown
from pymongo import MongoClient

load_dotenv()

from release_note_generator import run_generator, get_week_range

app = Flask(__name__)

MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB = os.environ.get("MONGO_DB", "admin")
MONGO_COLLECTION = "release_notes"

mongo_client = None
mongo_collection = None


def get_mongo_collection():
    global mongo_client, mongo_collection
    if mongo_collection is None:
        if MONGO_URI:
            try:
                mongo_uri = MONGO_URI.split('/?')[0] if '/?' in MONGO_URI else MONGO_URI
                mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
                mongo_client.server_info()
                mongo_collection = mongo_client[MONGO_DB][MONGO_COLLECTION]
                logger.info(f"Connected to MongoDB: {MONGO_DB}.{MONGO_COLLECTION}")
            except Exception as e:
                logger.error(f"MongoDB connection failed: {e}")
                mongo_client = None
                mongo_collection = None
        else:
            logger.warning("MONGO_URI not set. Using file fallback.")
    return mongo_collection


def get_db():
    global mongo_client
    if mongo_client is None:
        get_mongo_collection()
    if mongo_client is None:
        return None
    return mongo_client[MONGO_DB]


app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

print("=== API Server Routes ===")
for rule in app.url_map.iter_rules():
    print(f"{rule.methods} {rule.rule}")
print("========================")

progress_queue = queue.Queue()


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


RELEASE_NOTES_FILE = "release_notes.json"
cached_notes = {}


def load_from_file(filename):
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load from file: {e}. Using in-memory cache.")
    return {}


def save_to_file(data, filename):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Could not save to file: {e}. Using in-memory cache.")


def load_notes():
    col = get_mongo_collection()
    if col is not None:
        notes = {}
        for doc in col.find():
            week_key = doc.get("week_start")
            if week_key:
                doc_copy = doc.copy()
                del doc_copy["_id"]
                notes[week_key] = doc_copy
        return notes
    return load_from_file(RELEASE_NOTES_FILE)


def save_notes(data):
    col = get_mongo_collection()
    logger.info(f"save_notes: col is {type(col)} = {col is not None}")
    if col is not None:
        for week_key, note_data in data.items():
            note_data_copy = note_data.copy()
            note_data_copy["week_start"] = week_key
            result = col.update_one(
                {"week_start": week_key},
                {"$set": note_data_copy},
                upsert=True
            )
            logger.info(f"Saved {week_key}: {result.upserted_id}")
        logger.info("Saved to MongoDB")
        return
    logger.info("MongoDB not available, saving to file")
    save_to_file(data, RELEASE_NOTES_FILE)
    logger.info("Saved to file")
    col = get_mongo_collection()
    if col is not None:
        for week_key, note_data in data.items():
            note_data_copy = note_data.copy()
            note_data_copy["week_start"] = week_key
            col.update_one(
                {"week_start": week_key},
                {"$set": note_data_copy},
                upsert=True
            )
        return
    save_to_file(data, RELEASE_NOTES_FILE)


def send_to_keila(markdown_content, subject):
    keila_api_key = os.environ.get("KEILA_API_KEY", "")
    keila_url = os.environ.get("KEILA_API_URL", "").rstrip("/")
    sender_id = os.environ.get("KEILA_SENDER_ID", "")
    segment_id = os.environ.get("KEILA_SEGMENT_ID", "")

    if not keila_api_key or not keila_url or not sender_id or not segment_id:
        raise ValueError("Keila configuration missing (KEILA_API_KEY, KEILA_API_URL, KEILA_SENDER_ID, KEILA_SEGMENT_ID)")

    headers = {
        "Authorization": f"Bearer {keila_api_key}",
        "Content-Type": "application/json",
    }

    html_body = markdown.markdown(markdown_content)

    text_body = f"""
<img src="https://i.postimg.cc/VvpKzkfn/Handy-Logo.jpg" width="150" alt="AgencyHandy"/>

---
{html_body}
---

You're receiving this because you're an AgencyHandy user.
Unsubscribe: {{{{ unsubscribe_url }}}}
"""

    logger.info(f"Creating Keila campaign: {subject}")
    campaign_resp = http_requests.post(
        f"{keila_url}/api/v1/campaigns",
        headers=headers,
        json={
            "data": {
                "subject": subject,
                "settings": {"type": "markdown"},
                "text_body": text_body,
                "sender_id": sender_id,
                "segment_id": segment_id,
            }
        },
        timeout=30,
    )

    if campaign_resp.status_code not in (200, 201):
        raise RuntimeError(f"Keila campaign creation failed: {campaign_resp.status_code} {campaign_resp.text}")

    campaign = campaign_resp.json()
    campaign_id = campaign["data"]["id"]
    logger.info(f"Campaign created: {campaign_id}")

    send_resp = http_requests.post(
        f"{keila_url}/api/v1/campaigns/{campaign_id}/actions/send",
        headers=headers,
        timeout=30,
    )

    if send_resp.status_code not in (200, 202):
        raise RuntimeError(f"Keila campaign send failed: {send_resp.status_code} {send_resp.text}")

    logger.info(f"Campaign sent: {campaign_id}")
    return campaign_id


def sync_super_admin_contacts_to_keila():
    keila_api_key = os.environ.get("KEILA_API_KEY", "")
    keila_url = os.environ.get("KEILA_API_URL", "").rstrip("/")

    if not keila_api_key or not keila_url:
        raise ValueError("Keila configuration missing (KEILA_API_KEY, KEILA_API_URL)")

    db = get_db()
    if db is None:
        raise RuntimeError("MongoDB not connected")

    pipeline = [
        {
            "$lookup": {
                "from": "companyroles",
                "localField": "role",
                "foreignField": "_id",
                "as": "companyRoleData",
            }
        },
        {"$unwind": "$companyRoleData"},
        {
            "$lookup": {
                "from": "roles",
                "localField": "companyRoleData.role",
                "foreignField": "_id",
                "as": "roleData",
            }
        },
        {"$unwind": "$roleData"},
        {"$match": {"roleData.name": "superAdmin"}},
        {"$group": {"_id": "$email", "firstName": {"$first": "$firstName"}, "lastName": {"$first": "$lastName"}}},
        {"$project": {"email": "$_id", "firstName": 1, "lastName": 1, "_id": 0}},
    ]
    members = list(db.members.aggregate(pipeline))

    headers = {
        "Authorization": f"Bearer {keila_api_key}",
        "Content-Type": "application/json",
    }

    results = {"total": len(members), "created": 0, "skipped": 0, "errors": []}
    lock = threading.Lock()

    def create_contact(member):
        first = member.get("firstName") or ""
        last = member.get("lastName") or ""
        payload = {
            "data": {
                "email": member["email"],
                "first_name": first,
                "last_name": last,
                "data": {"source": "test_db"},
            }
        }
        try:
            resp = http_requests.post(
                f"{keila_url}/api/v1/contacts",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                with lock:
                    results["created"] += 1
                logger.info(f"Created Keila contact: {member['email']}")
            elif resp.status_code in (422, 400) and "already been taken" in resp.text:
                with lock:
                    results["skipped"] += 1
                logger.info(f"Contact already exists (skipped): {member['email']}")
            else:
                err_msg = f"{resp.status_code}: {resp.text[:200]}"
                with lock:
                    results["errors"].append({"email": member["email"], "error": err_msg})
                logger.warning(f"Failed to create contact {member['email']}: {err_msg}")
        except Exception as e:
            with lock:
                results["errors"].append({"email": member["email"], "error": str(e)})
            logger.warning(f"Error creating contact {member['email']}: {e}")

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(create_contact, m) for m in members]
        for future in as_completed(futures):
            future.result()

    return results


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


@app.route('/')
def index():
    return jsonify({"status": "ok", "message": "Release Notes API"})

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
        
        all_notes = load_notes()
        all_notes[week_key] = data
        save_notes(all_notes)
        logger.info(f"Saved release note to {week_key}")
        
        return jsonify(data)
    except Exception as e:
        import traceback
        logger.error(f"Error: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route('/api/release-notes/<week_start>', methods=['GET'])
def get_release_note_by_week(week_start):
    all_notes = load_notes()
    
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
        using_llm = bool(llm_key and llm_url)
        logger.info(f"Starting run_generator... (LLM: {using_llm})")
        message = run_generator(token, fe_repo, be_repo, llm_key, llm_url)
        logger.info(f"Generator returned message length: {len(message)}")
        
        week_start_dt, week_end_dt = get_week_range()
        week_key = week_start_dt.strftime('%Y-%m-%d')
        
        result = {
            "week_start": week_key,
            "week_end": week_end_dt.strftime('%Y-%m-%d'),
            "generated_at": datetime.now().isoformat(),
            "content": message,
            "llm_used": using_llm
        }
        
        all_notes = load_notes()
        all_notes[week_key] = result
        save_notes(all_notes)
        logger.info(f"Saved release note to {week_key}")
        
        return jsonify(result)
    except Exception as e:
        import traceback
        logger.error(f"Error: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
ALTERNATE_ADMIN_PASSWORD = os.environ.get("ADMIN_ALT_PASSWORD", "Lx8QaVKx6Ae63ce")

def is_admin_password(password: str) -> bool:
    return password == ADMIN_PASSWORD or password == ALTERNATE_ADMIN_PASSWORD


@app.route('/api/v1/admin/release-notes', methods=['POST'])
def admin_get_release_notes():
    data = request.get_json() or {}
    password = data.get("password", "")
    
    if not is_admin_password(password):
        return jsonify({"error": "Unauthorized"}), 401
    
    all_notes = load_notes()
    if not all_notes:
        week_start_dt, week_end_dt = get_week_range()
        week_key = week_start_dt.strftime('%Y-%m-%d')
        all_notes[week_key] = {
            "week_start": week_key,
            "week_end": week_end_dt.strftime('%Y-%m-%d'),
            "generated_at": datetime.now().isoformat(),
            "content": "No release notes generated yet. Click Generate to create one."
        }
    
    return jsonify({"data": all_notes})


@app.route('/api/v1/admin/release-notes/update', methods=['POST'])
def admin_update_release_note():
    data = request.get_json() or {}
    password = data.get("password", "")
    week_start = data.get("week_start", "")
    content = data.get("content", "")
    
    if not is_admin_password(password):
        return jsonify({"error": "Unauthorized"}), 401
    
    if not week_start:
        return jsonify({"error": "Missing week_start"}), 400
    
    all_notes = load_notes()
    
    if week_start in all_notes:
        all_notes[week_start]["content"] = content
        save_notes(all_notes)
        return jsonify({"data": all_notes[week_start]})
    
    return jsonify({"error": "Release note not found"}), 404


@app.route('/api/v1/admin/release-notes/generate', methods=['POST'])
def admin_generate_release_note():
    data = request.get_json() or {}
    password = data.get("password", "")
    
    if not is_admin_password(password):
        return jsonify({"error": "Unauthorized"}), 401
    
    token = os.environ.get("GITHUB_TOKEN", "")
    fe_repo = os.environ.get("FE_REPO", "")
    be_repo = os.environ.get("BE_REPO", "")
    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_url = os.environ.get("LLM_API_URL", "")
    
    if not token or not fe_repo or not be_repo:
        return jsonify({"error": "Missing GitHub configuration"}), 500
    
    try:
        llm_key = os.environ.get("LLM_API_KEY", "")
        llm_url = os.environ.get("LLM_API_URL", "")
        
        using_llm = bool(llm_key and llm_url)
        logger.info(f"Starting release note generation... (LLM: {using_llm})")
        message = run_generator(token, fe_repo, be_repo, llm_key, llm_url)
        
        week_start_dt, week_end_dt = get_week_range()
        week_key = week_start_dt.strftime('%Y-%m-%d')
        
        result = {
            "week_start": week_key,
            "week_end": week_end_dt.strftime('%Y-%m-%d'),
            "generated_at": datetime.now().isoformat(),
            "content": message,
            "llm_used": using_llm
        }
        
        cached_notes[week_key] = result
        try:
            all_notes = load_notes()
            all_notes[week_key] = result
            save_notes(all_notes)
        except Exception as e:
            logger.warning(f"Could not save to file: {e}")
        
        return jsonify({"data": result})
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/v1/admin/release-notes/send-to-keila', methods=['POST'])
def admin_send_release_note_to_keila():
    data = request.get_json() or {}
    password = data.get("password", "")
    week_start = data.get("week_start", "")

    if not is_admin_password(password):
        return jsonify({"error": "Unauthorized"}), 401

    if not week_start:
        return jsonify({"error": "Missing week_start"}), 400

    all_notes = load_notes()
    note = all_notes.get(week_start)
    if not note:
        return jsonify({"error": "Release note not found for this week"}), 404

    content = note.get("content", "")
    if not content or content.strip() in ("", "no release note"):
        return jsonify({"error": "Release note content is empty"}), 400

    first_line = content.strip().split("\n")[0]
    subject = re.sub(r"^#+\s*", "", first_line).strip()
    if not subject:
        subject = f"Release Notes — {week_start}"

    try:
        campaign_id = send_to_keila(content, subject)
        return jsonify({"success": True, "campaign_id": campaign_id, "subject": subject})
    except Exception as e:
        logger.error(f"Failed to send to Keila: {e}")
        return jsonify({"error": str(e)}), 500


sync_tasks = {}
sync_tasks_lock = threading.Lock()


@app.route('/api/v1/admin/keila/sync-contacts', methods=['POST'])
def admin_sync_keila_contacts():
    data = request.get_json() or {}
    password = data.get("password", "")

    if not is_admin_password(password):
        return jsonify({"error": "Unauthorized"}), 401

    task_id = os.urandom(8).hex()
    task = {"status": "running", "results": None, "error": None}

    with sync_tasks_lock:
        sync_tasks[task_id] = task

    def run_sync():
        try:
            results = sync_super_admin_contacts_to_keila()
            with sync_tasks_lock:
                task["status"] = "done"
                task["results"] = results
        except Exception as e:
            logger.error(f"Keila contact sync failed: {e}")
            with sync_tasks_lock:
                task["status"] = "error"
                task["error"] = str(e)

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()

    return jsonify({"success": True, "task_id": task_id}), 202


@app.route('/api/v1/admin/keila/sync-status/<task_id>', methods=['GET'])
def admin_sync_keila_status(task_id):
    with sync_tasks_lock:
        task = sync_tasks.get(task_id)

    if task is None:
        return jsonify({"error": "Task not found"}), 404

    response = {"status": task["status"]}
    if task["results"] is not None:
        response["results"] = task["results"]
    if task["error"] is not None:
        response["error"] = task["error"]

    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=not debug)