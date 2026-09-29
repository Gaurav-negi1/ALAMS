"""
A-LAMS v3 — Fully Autonomous Email Processing System
"""

from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
import threading
import time
import json
import os
import logging
import io
from datetime import datetime

from database import Database

from agents.perception_agent import PerceptionAgent
from agents.reasoning_agent import ReasoningAgent
from agents.execution_agent import ExecutionAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="../frontend", static_url_path="")
CORS(app)

db         = Database()
perception = PerceptionAgent()
reasoning  = ReasoningAgent()
execution  = ExecutionAgent(db)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "config.json")

def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("config.json corrupt — using defaults")
    return {
        "groq_api_key":          "",
        "gemini_api_key":        "",
        "gmail_token":           {},
        "poll_interval_minutes": 5,
        "auto_reply":            False,
        "auto_poll":             False,
    }

def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Poller ────────────────────────────────────────────────────────────────────
class Poller:
    def __init__(self):
        self.thread   = None
        self.running  = False
        self.last_run = None
        self.next_run = None
        self.status   = "idle"

    def start(self):
        if self.running and self.thread and self.thread.is_alive():
            return
        self.running = True
        self.thread  = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log.info("Background poller started")

    def stop(self):
        self.running = False
        self.status  = "idle"
        log.info("Background poller stopped")

    def _loop(self):
        while self.running:
            cfg = load_config()
            if not cfg.get("auto_poll"):
                time.sleep(10)
                continue
            interval     = int(cfg.get("poll_interval_minutes", 5)) * 60
            self.status  = "fetching"
            try:
                count        = run_pipeline(cfg)
                self.last_run = datetime.now().isoformat()
                self.next_run = datetime.fromtimestamp(time.time() + interval).isoformat()
                self.status  = f"last run: processed {count} emails"
            except Exception as e:
                self.status = f"error: {str(e)}"
                log.error(f"Poller error: {e}", exc_info=True)
            time.sleep(interval)

poller = Poller()

# ── Pipeline ──────────────────────────────────────────────────────────────────
def run_pipeline(cfg: dict, max_emails: int = 20) -> int:
    from gmail_service import GmailService

    gmail_token = cfg.get("gmail_token", {})
    if not gmail_token:
        raise ValueError("Gmail not configured. Connect Gmail in Settings.")

    groq_key   = cfg.get("groq_api_key", "")
    gemini_key = cfg.get("gemini_api_key", "")

    # Inject keys into agents
    perception.set_groq_key(groq_key)
    perception.set_gemini_key(gemini_key)
    reasoning.set_groq_key(groq_key)
    execution.set_groq_key(groq_key)

    gmail  = GmailService(gmail_token)
    emails = gmail.fetch_unread_emails(max_results=max_emails)

    processed = 0
    for email_data in emails:
        try:
            if db.is_processed(email_data["message_id"]):
                continue

            log.info(f"Processing: {email_data['sender']} — {email_data['subject'][:60]}")
            
            # Log attachment info for debugging
            if email_data.get("attachment_meta"):
                log.info(f"  📎 Attachments: {len(email_data['attachment_meta'])} files")
                for meta in email_data['attachment_meta']:
                    log.info(f"     - {meta['filename']} ({meta['mime_type']})")

            structured   = perception.process(email_data)
            log.info(f"  📋 Intent: {structured.get('intent')}, Has attachment: {structured.get('has_attachment')}, Readable: {structured.get('has_readable_content')}")
            
            student_info = db.get_student_by_email(email_data["sender"])
            decision     = reasoning.decide(structured, student_info)
            log.info(f"  ⚖️ Decision: {decision.get('decision')}")
            
            result       = execution.execute(email_data, structured, decision, student_info)

            # Prepare attachment metadata for database
            attachment_meta_json = json.dumps(email_data.get("attachment_meta", []))
            attachment_text = email_data.get("attachment_text", "")
            has_attachments = len(email_data.get("attachment_meta", [])) > 0

            db.log_request({
                "message_id":       email_data["message_id"],
                "thread_id":        email_data.get("thread_id", ""),
                "student_email":    email_data["sender"],
                "student_name":     email_data.get("sender_name", ""),
                "subject":          email_data["subject"],
                "body_snippet":     email_data["body"][:500],
                "intent":           structured.get("intent", "unknown"),
                "structured_json":  json.dumps(structured),
                "decision":         decision.get("decision", "PENDING"),
                "decision_reason":  decision.get("reason", ""),
                "decision_json":    json.dumps(decision),
                "reply_text":       result.get("reply", ""),
                "has_attachments":  has_attachments,
                "attachment_meta":  attachment_meta_json,
                "attachment_text":  attachment_text[:5000] if attachment_text else "",
                "timestamp":        datetime.now().isoformat(),
                "replied":          False,
            })

            # Save attachments to database
            for filename, file_bytes in email_data.get("attachment_bytes", {}).items():
                mime_type = next((m['mime_type'] for m in email_data.get("attachment_meta", []) 
                                if m['filename'] == filename), "application/octet-stream")
                extracted = ""
                if filename in email_data.get("attachment_text", ""):
                    extracted = email_data["attachment_text"]
                db.save_attachment(email_data["message_id"], filename, mime_type, file_bytes, extracted)

            if (
                cfg.get("auto_reply")
                and result.get("reply")
                and not result.get("skip_reply")
            ):
                try:
                    gmail.send_reply(
                        thread_id=email_data.get("thread_id", email_data["message_id"]),
                        to=email_data["sender"],
                        subject=email_data["subject"],
                        body=result["reply"],
                        in_reply_to=email_data.get("message_id_header", ""),
                    )
                    gmail.mark_as_read(email_data["message_id"])
                    db.mark_replied(email_data["message_id"])
                    log.info(f"  ✅ Auto-replied to {email_data['sender']}")
                except Exception as e:
                    log.error(f"  ❌ Auto-reply failed for {email_data['sender']}: {e}")
            elif result.get("skip_reply"):
                log.info(f"  ⏭️ Skipped reply (no_action) for: {email_data['subject'][:50]}")
                try:
                    gmail.mark_as_read(email_data["message_id"])
                except Exception:
                    pass

            processed += 1

        except Exception as e:
            log.error(f"Failed to process {email_data.get('message_id')}: {e}", exc_info=True)

    return processed

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    for path in ["../frontend/index.html", "index.html"]:
        full = os.path.join(os.path.dirname(__file__), path)
        if os.path.exists(full):
            return send_from_directory(os.path.dirname(full), "index.html")
    return "A-LAMS backend running. Place index.html in frontend/ folder.", 200


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify({
        "groq_key_set":          bool(cfg.get("groq_api_key")),
        "gemini_key_set":        bool(cfg.get("gemini_api_key")),
        "gmail_connected":       bool(cfg.get("gmail_token")),
        "poll_interval_minutes": cfg.get("poll_interval_minutes", 5),
        "auto_reply":            cfg.get("auto_reply", False),
        "auto_poll":             cfg.get("auto_poll", False),
        "poller_status":         poller.status,
        "last_run":              poller.last_run,
        "next_run":              poller.next_run,
    })

@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.json or {}
    cfg  = load_config()

    if "groq_api_key" in data:
        cfg["groq_api_key"]   = data["groq_api_key"]
    if "gemini_api_key" in data:
        cfg["gemini_api_key"] = data["gemini_api_key"]
    if "gmail_token" in data:
        cfg["gmail_token"]    = data["gmail_token"]
    if "poll_interval_minutes" in data:
        cfg["poll_interval_minutes"] = max(1, int(data["poll_interval_minutes"]))
    if "auto_reply" in data:
        cfg["auto_reply"] = bool(data["auto_reply"])
    if "auto_poll" in data:
        cfg["auto_poll"]  = bool(data["auto_poll"])

    save_config(cfg)

    if cfg.get("auto_poll"):
        poller.start()
    else:
        poller.stop()

    return jsonify({"success": True, "message": "Configuration saved"})

@app.route("/api/config/clear-gmail", methods=["POST"])
def clear_gmail():
    cfg = load_config()
    cfg["gmail_token"] = {}
    save_config(cfg)
    return jsonify({"success": True})

@app.route("/api/config/test-gemini", methods=["POST"])
def test_gemini():
    cfg = load_config()
    key = cfg.get("gemini_api_key", "")
    if not key:
        return jsonify({"success": False, "error": "No Gemini API key configured"}), 400
    try:
        import requests as req
        r = req.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent",
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": key
            },
            json={
                "contents": [{
                    "parts": [{
                        "text": "Reply with only the word: OK"
                    }]
                }]
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if "candidates" in data and len(data["candidates"]) > 0:
                return jsonify({"success": True, "message": "Gemini API key is valid ✓"})
            else:
                return jsonify({"success": False, "error": "Invalid response from Gemini API"}), 400
        else:
            error_msg = f"API returned {r.status_code}"
            try:
                error_data = r.json()
                if "error" in error_data:
                    error_msg += f": {error_data['error'].get('message', 'Unknown error')}"
            except:
                error_msg += f": {r.text[:200]}"
            return jsonify({"success": False, "error": error_msg}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/fetch-now", methods=["POST"])
def fetch_now():
    cfg = load_config()
    if not cfg.get("gmail_token"):
        return jsonify({"error": "Gmail not connected. Go to Settings."}), 400
    try:
        count = run_pipeline(cfg)
        poller.last_run = datetime.now().isoformat()
        return jsonify({"success": True, "processed": count, "message": f"Processed {count} new email(s)"})
    except Exception as e:
        log.error(f"fetch_now error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/inbox", methods=["GET"])
def get_inbox():
    page            = int(request.args.get("page", 1))
    limit           = int(request.args.get("limit", 20))
    intent_filter   = request.args.get("intent", "")
    decision_filter = request.args.get("decision", "")
    emails = db.get_inbox(page, limit, intent_filter, decision_filter)
    total  = db.get_inbox_count(intent_filter, decision_filter)
    return jsonify({"emails": emails, "total": total, "page": page, "limit": limit})

@app.route("/api/inbox/<message_id>", methods=["GET"])
def get_email_detail(message_id):
    email = db.get_email_detail(message_id)
    if not email:
        return jsonify({"error": "Not found"}), 404
    return jsonify(email)

@app.route("/api/attachments/<int:attachment_id>", methods=["GET"])
def get_attachment(attachment_id):
    """Download/view an attachment"""
    attachment = db.get_attachment_data(attachment_id)
    if not attachment:
        return jsonify({"error": "Attachment not found"}), 404
    
    file_data = attachment['file_data']
    filename = attachment['filename']
    mime_type = attachment['mime_type']
    
    # For images and PDFs, allow inline viewing
    if mime_type.startswith('image/') or mime_type == 'application/pdf':
        return send_file(
            io.BytesIO(file_data),
            mimetype=mime_type,
            as_attachment=False,
            download_name=filename
        )
    else:
        # For other files, force download
        return send_file(
            io.BytesIO(file_data),
            mimetype=mime_type,
            as_attachment=True,
            download_name=filename
        )

@app.route("/api/attachments/<int:attachment_id>/preview", methods=["GET"])
def preview_attachment(attachment_id):
    """Get attachment metadata and preview text"""
    attachment = db.get_attachment_data(attachment_id)
    if not attachment:
        return jsonify({"error": "Attachment not found"}), 404
    
    with db.conn() as c:
        row = c.execute('SELECT extracted_text FROM attachments WHERE id=?', (attachment_id,)).fetchone()
        extracted_text = row[0] if row else ""
    
    return jsonify({
        "filename": attachment['filename'],
        "mime_type": attachment['mime_type'],
        "preview": extracted_text[:1000] if extracted_text else "No text extracted"
    })

@app.route("/api/inbox/<message_id>/reply", methods=["POST"])
def send_manual_reply(message_id):
    data       = request.json or {}
    reply_text = data.get("reply_text", "")
    cfg        = load_config()
    if not cfg.get("gmail_token"):
        return jsonify({"error": "Gmail not connected"}), 400
    if not reply_text:
        return jsonify({"error": "No reply text"}), 400
    email = db.get_email_detail(message_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404
    try:
        from gmail_service import GmailService
        gmail = GmailService(cfg["gmail_token"])
        gmail.send_reply(
            thread_id=email.get("thread_id", message_id),
            to=email["student_email"],
            subject=email["subject"],
            body=reply_text,
        )
        db.mark_replied(message_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
def get_stats():
    return jsonify(db.get_stats())

@app.route("/api/students", methods=["GET"])
def get_students():
    return jsonify(db.get_all_students())

@app.route("/api/students", methods=["POST"])
def add_student():
    db.add_student(request.json or {})
    return jsonify({"success": True})

@app.route("/api/seed-demo", methods=["POST"])
def seed_demo():
    db.seed_demo_data()
    return jsonify({"success": True})

@app.route("/api/status", methods=["GET"])
def system_status():
    cfg = load_config()
    return jsonify({
        "online":          True,
        "groq_ready":      bool(cfg.get("groq_api_key")),
        "gemini_ready":    bool(cfg.get("gemini_api_key")),
        "gmail_ready":     bool(cfg.get("gmail_token")),
        "auto_poll":       cfg.get("auto_poll", False),
        "auto_reply":      cfg.get("auto_reply", False),
        "poll_interval":   cfg.get("poll_interval_minutes", 5),
        "poller_running":  poller.running,
        "poller_status":   poller.status,
        "last_run":        poller.last_run,
        "next_run":        poller.next_run,
        "timestamp":       datetime.now().isoformat(),
    })
@app.route("/api/inbox/<message_id>/override", methods=["POST"])
def override_decision(message_id):
    """Professor/admin override for AI decision with auto-reply generation"""
    try:
        data = request.json or {}
        new_decision = data.get("new_decision", "")
        admin_notes = data.get("admin_notes", "")
        send_reply = data.get("send_reply", True)
        
        log.info("=" * 60)
        log.info(f"OVERRIDE STARTED for message: {message_id}")
        log.info(f"New Decision: {new_decision}")
        log.info(f"Send Reply Requested: {send_reply}")
        log.info("=" * 60)
        
        if not new_decision:
            return jsonify({"error": "New decision required"}), 400
        
        if not admin_notes or not admin_notes.strip():
            return jsonify({"error": "Admin notes required for override"}), 400
        
        if new_decision not in ["APPROVE", "REJECT", "PENDING"]:
            return jsonify({"error": "Invalid decision value"}), 400
        
        # Get email details first - KEEP THIS ORIGINAL EMAIL VARIABLE
        email = db.get_email_detail(message_id)
        if not email:
            log.error(f"Email not found: {message_id}")
            return jsonify({"error": "Email not found"}), 404
        
        log.info(f"Found email - From: {email.get('student_email')}, Subject: {email.get('subject')}")
        
        # Generate new reply based on the override decision
        new_reply = generate_override_reply(email, new_decision, admin_notes)
        log.info(f"Generated new reply ({len(new_reply)} chars)")
        
        # Update database with new decision and reply
        success, message, _ = db.override_decision(message_id, new_decision, admin_notes, new_reply)
        
        if not success:
            log.error(f"Database update failed: {message}")
            return jsonify({"error": message}), 500
        
        log.info("Database updated successfully")
        
        reply_sent = False
        email_error = None
        
        # Send the new reply via email if requested
        if send_reply:
            log.info("-" * 40)
            log.info("ATTEMPTING TO SEND OVERRIDE REPLY EMAIL")
            
            try:
                cfg = load_config()
                gmail_token = cfg.get("gmail_token", {})
                
                if not gmail_token:
                    email_error = "Gmail not configured in Settings"
                    log.warning(f"❌ {email_error}")
                else:
                    from gmail_service import GmailService
                    
                    # Create Gmail service
                    gmail = GmailService(cfg["gmail_token"])
                    
                    # USE THE ORIGINAL EMAIL VARIABLE, NOT email_dict
                    student_email = email.get('student_email')
                    subject = email.get('subject', '')
                    thread_id = email.get('thread_id', message_id)
                    
                    log.info(f"Student email from 'email' variable: {student_email}")
                    log.info(f"Subject from 'email' variable: {subject}")
                    log.info(f"Thread ID from 'email' variable: {thread_id}")
                    
                    if student_email:
                        log.info(f"Sending override reply to: {student_email}")
                        log.info(f"Subject: Re: {subject}")
                        log.info(f"Thread ID: {thread_id}")
                        log.info(f"Reply body preview: {new_reply[:100]}...")
                        
                        # Send the reply - EXACT SAME AS NORMAL REPLY
                        result = gmail.send_reply(
                            thread_id=thread_id,
                            to=student_email,
                            subject=subject,
                            body=new_reply
                        )
                        
                        log.info(f"Gmail API result: {result}")
                        
                        db.mark_replied(message_id)
                        reply_sent = True
                        log.info(f"✅✅✅ OVERRIDE REPLY SENT SUCCESSFULLY to {student_email} ✅✅✅")
                    else:
                        email_error = f"No student email found in record. Email data: {email.keys()}"
                        log.warning(f"❌ {email_error}")
                        
            except Exception as e:
                email_error = str(e)
                log.error(f"❌ Failed to send override reply: {e}", exc_info=True)
                
            log.info("-" * 40)
        else:
            log.info("Send reply was disabled by user")
        
        log.info("=" * 60)
        log.info(f"OVERRIDE COMPLETED - Reply Sent: {reply_sent}")
        if email_error:
            log.info(f"Email Error: {email_error}")
        log.info("=" * 60)
        
        response_data = {
            "success": True, 
            "message": f"Decision overridden to {new_decision}",
            "new_reply": new_reply,
            "reply_sent": reply_sent
        }
        
        if email_error:
            response_data["email_error"] = email_error
            
        return jsonify(response_data)
            
    except Exception as e:
        log.error(f"Override error for {message_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def generate_override_reply(email_dict, new_decision, admin_notes):
    """Generate an appropriate reply based on the override decision"""
    
    student_name = email_dict.get('student_name', 'Student')
    if not student_name or student_name == 'Unknown':
        student_email = email_dict.get('student_email', '')
        if '@' in student_email:
            student_name = student_email.split('@')[0].split('.')[0].title()
        else:
            student_name = 'Student'
    
    subject = email_dict.get('subject', 'your request')
    intent = email_dict.get('intent', 'request')
    
    if intent:
        intent_display = intent.replace('_', ' ').title()
    else:
        intent_display = 'Request'
    
    if new_decision == "APPROVE":
        return f"""Dear {student_name},

After a thorough review by the academic office, your {intent_display} request regarding "{subject}" has been **approved**.

{admin_notes}

Your attendance records have been updated accordingly.

Best regards,
Academic Office
[Approved by Professor Override]"""

    elif new_decision == "REJECT":
        return f"""Dear {student_name},

After a thorough review by the academic office, your {intent_display} request regarding "{subject}" has been **declined**.

{admin_notes}

If you have questions, please contact the academic office.

Best regards,
Academic Office
[Reviewed by Professor Override]"""

    else:  # PENDING
        return f"""Dear {student_name},

Your {intent_display} request regarding "{subject}" is currently **under review** by the academic office.

{admin_notes}

We will notify you once a final decision has been made.

Best regards,
Academic Office
[Under Review - Professor Override]"""

# ── Policy Management ──
@app.route("/api/policies", methods=["GET"])
def get_policies():
    try:
        policies = db.get_all_policies()
        return jsonify({"policies": policies, "success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/policies", methods=["POST"])
def update_policies():
    try:
        data = request.json or {}
        policies = data.get("policies", {})
        if not policies:
            return jsonify({"error": "No policies provided"}), 400
        success = db.bulk_update_policies(policies)
        if success:
            new_policies = db.reload_policies()
            reasoning.update_policies(new_policies)
            log.info("Policies updated and reloaded")
            return jsonify({"success": True, "message": "Policies updated and reloaded"})
        else:
            return jsonify({"error": "Failed to update some policies"}), 500
    except Exception as e:
        log.error(f"Policy update error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/policies/reload", methods=["POST"])
def reload_policies():
    try:
        new_policies = db.reload_policies()
        reasoning.update_policies(new_policies)
        return jsonify({"success": True, "policies": new_policies})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Excel Import/Export ──
@app.route("/api/students/import-excel", methods=["POST"])
def import_students_excel():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({"error": "Please upload an Excel file (.xlsx or .xls)"}), 400
        file_data = file.read()
        success, message = db.import_students_from_excel(file_data)
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"error": message}), 400
    except Exception as e:
        log.error(f"Excel import error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/students/export-excel", methods=["GET"])
def export_students_excel():
    try:
        excel_data = db.export_students_to_excel()
        if excel_data:
            return send_file(
                io.BytesIO(excel_data),
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=f'students_{datetime.now().strftime("%Y%m%d")}.xlsx'
            )
        return jsonify({"error": "Failed to generate Excel"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Attendance & Database Views ──
@app.route("/api/students/<email>/attendance", methods=["GET"])
def get_student_attendance(email):
    try:
        history = db.get_student_attendance_history(email)
        if history.get('student'):
            return jsonify({"success": True, "data": history})
        return jsonify({"error": "Student not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/database/emails", methods=["GET"])
def get_email_database():
    try:
        search = request.args.get("search", "")
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 50))
        result = db.get_email_database(search, page, limit)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/database/stats", methods=["GET"])
def get_database_stats():
    try:
        stats = db.get_database_stats()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/students/clear", methods=["POST"])
def clear_all_students():
    """Delete all students from the database"""
    try:
        with db.conn() as c:
            c.execute("DELETE FROM students")
        return jsonify({"success": True, "message": "All students deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

if __name__ == "__main__":
    db.init_db()
    cfg = load_config()
    if cfg.get("auto_poll"):
        poller.start()
    log.info("A-LAMS v3 starting on http://localhost:5000")
    app.run(debug=False, port=5000, threaded=True)