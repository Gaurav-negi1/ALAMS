"""
Execution Agent — DB update + reply generation.
Now updates student attendance on leave approval.
"""

import requests
import re
from datetime import datetime

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

TEMPLATES = {
    ("medical_leave",         "APPROVE"): "Dear {name},\n\nYour medical leave for {dates} has been approved.\n\n{conds}Please submit your medical certificate to the academic office if not already done. Get well soon.\n\nBest regards,\nAcademic Office",
    ("medical_leave",         "REJECT"):  "Dear {name},\n\nYour medical leave request could not be approved.\n\nReason: {reason}\n\n{action}\n\nBest regards,\nAcademic Office",
    ("medical_leave",         "PENDING"): "Dear {name},\n\nYour medical leave is under review.\n\n{conds}Please submit the required documents at the earliest.\n\nBest regards,\nAcademic Office",
    ("personal_leave",        "APPROVE"): "Dear {name},\n\nYour personal leave for {dates} has been approved. Please complete any missed work on return.\n\nBest regards,\nAcademic Office",
    ("personal_leave",        "REJECT"):  "Dear {name},\n\nYour personal leave request could not be approved.\n\nReason: {reason}\n\n{action}\n\nBest regards,\nAcademic Office",
    ("on_duty",               "APPROVE"): "Dear {name},\n\nYour On-Duty (OD) for {dates} has been approved. Your attendance will be marked accordingly. Please submit an event completion report after attending.\n\nBest regards,\nAcademic Office",
    ("on_duty",               "PENDING"): "Dear {name},\n\nYour OD request is pending.\n\n{conds}Once submitted, it will be processed within 24 hours.\n\nBest regards,\nAcademic Office",
    ("attendance_correction", "PENDING"): "Dear {name},\n\nYour attendance correction request is under verification with faculty records.\n\n{conds}We will update you within 2 working days.\n\nBest regards,\nAcademic Office",
    ("general_query",         "PENDING"): "Dear {name},\n\nThank you for reaching out. Your query has been noted and will be reviewed by our academic office. We will get back to you within 2 working days.\n\nBest regards,\nAcademic Office",
}


class ExecutionAgent:

    def __init__(self, db, groq_key: str = ""):
        self.db = db
        self._groq_key = groq_key

    def set_groq_key(self, key: str):
        self._groq_key = key or ""

    def execute(self, email_data: dict, structured: dict, decision: dict, student_info: dict) -> dict:
        actions = []

        if decision.get("decision") == "NO_ACTION":
            actions.append({"type": "skipped_no_action", "status": "success"})
            actions.append({"type": "logged", "timestamp": datetime.now().isoformat(), "status": "success"})
            return {"reply": "", "actions": actions, "skip_reply": True}

        reply = self._reply(email_data, structured, decision, student_info)
        actions.append({"type": "reply_generated", "status": "success"})

        if decision.get("decision") == "APPROVE":
            dates = structured.get("dates", [])
            if dates:
                # Update attendance log
                self.db.update_attendance(email_data["sender"], dates, "excused")
                # Update student attendance percentage
                try:
                    self.db.update_attendance_from_email(email_data["sender"], dates)
                    actions.append({"type": "attendance_updated", "dates": dates, "status": "success"})
                except Exception as e:
                    actions.append({"type": "attendance_update_failed", "error": str(e), "status": "warning"})

        actions.append({"type": "logged", "timestamp": datetime.now().isoformat(), "status": "success"})
        return {"reply": reply, "actions": actions, "skip_reply": False}

    def _reply(self, email_data, structured, decision, student_info):
        if self._groq_key:
            try:
                return self._groq_reply(email_data, structured, decision, student_info)
            except Exception as e:
                print(f"[Execution] Groq reply failed ({e}), using template")
        return self._template_reply(email_data, structured, decision, student_info)

    def _groq_reply(self, email_data, structured, decision, student_info):
        name = (student_info or {}).get("name") or email_data.get("sender_name", "Student")
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {self._groq_key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": f"""Write a professional academic office reply email (body only, no subject line).

Student name   : {name}
Email subject  : {email_data.get('subject', '')}
Request type   : {structured.get('intent', '').replace('_', ' ')}
Dates mentioned: {structured.get('dates', [])}
Decision       : {decision.get('decision')}
Reason         : {decision.get('reason')}
Conditions     : {decision.get('conditions', [])}
Next step      : {decision.get('suggested_action', '')}

Rules: warm but professional, under 120 words, no markdown, sign as "Academic Office"."""}],
                "temperature": 0.3,
                "max_tokens": 300,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _template_reply(self, email_data, structured, decision, student_info):
        intent   = structured.get("intent", "other")
        dec      = decision.get("decision", "PENDING")
        name     = (student_info or {}).get("name") or email_data.get("sender_name", "Student")
        dates    = ", ".join(structured.get("dates", [])) or "the requested period"
        conds    = decision.get("conditions", []) or []
        cond_text = ("Please note:\n" + "\n".join(f"• {c}" for c in conds) + "\n\n") if conds else ""
        action   = decision.get("suggested_action") or "Please contact the academic office."
        reason   = decision.get("reason") or ""

        tpl = TEMPLATES.get((intent, dec))
        if tpl:
            return tpl.format(name=name, dates=dates, reason=reason, conds=cond_text, action=action)

        fallback = {
            "APPROVE": f"Dear {name},\n\nYour request has been approved.\n\n{reason}\n\nBest regards,\nAcademic Office",
            "REJECT":  f"Dear {name},\n\nYour request has been declined.\n\nReason: {reason}\n\n{action}\n\nBest regards,\nAcademic Office",
            "PENDING": f"Dear {name},\n\nYour request is under review.\n\n{cond_text}Best regards,\nAcademic Office",
        }
        return fallback.get(dec, fallback["PENDING"])