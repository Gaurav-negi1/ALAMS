"""
Reasoning Agent — Policy decisions with needs_reply awareness.
Now uses database-driven policies instead of hardcoded values.
"""

import re
import json
import logging
import requests

log = logging.getLogger(__name__)

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

MEDICAL_CERT_SIGNALS = [
    "medical certificate", "doctor", "hospital", "clinic", "diagnosis",
    "patient", "prescription", "advised rest", "discharge", "physician",
    "fever", "illness", "treatment", "medicine",
]
EVENT_LETTER_SIGNALS = [
    "event", "hackathon", "symposium", "competition", "conference",
    "organis", "invited", "participate", "fest", "workshop", "institution",
]


class ReasoningAgent:

    def __init__(self, db=None):
        self._groq_key = ""
        self.db = db
        self._cached_policies = None

    def set_groq_key(self, key: str):
        self._groq_key = key or ""

    def get_policy(self):
        if self._cached_policies:
            return self._cached_policies
            
        if self.db:
            try:
                db_policies = self.db.reload_policies()
                if db_policies:
                    self._cached_policies = db_policies
                    return db_policies
            except Exception as e:
                log.warning(f"Failed to load policies from DB: {e}")
        
        # fallback defaults
        return {
            "min_attendance": 75.0,
            "critical_attendance": 65.0,
            "max_medical_days": 5,
            "max_personal_days": 3,
        }

    def update_policies(self, new_policies):
        self._cached_policies = new_policies
        log.info("Policies updated in reasoning agent")

    def decide(self, structured: dict, student_info: dict) -> dict:
        intent      = structured.get("intent", "other")
        needs_reply = structured.get("needs_reply", True)

        if intent == "no_action_needed" or not needs_reply:
            return {
                "decision":         "NO_ACTION",
                "reason":           structured.get("email_nature", "Automated or informational email — no reply needed."),
                "conditions":       [],
                "suggested_action": "No action required.",
                "policy_flags":     [],
                "confidence":       1.0,
                "needs_reply":      False,
            }

        POLICY = self.get_policy()
        result = self._rules(structured, student_info, POLICY)
        result["needs_reply"] = needs_reply

        if (
            self._groq_key
            and result["decision"] == "PENDING"
            and result.get("policy_flags")
            and intent in ("medical_leave", "personal_leave", "on_duty")
        ):
            try:
                refined = self._groq_refine(structured, student_info, result)
                refined["needs_reply"] = needs_reply
                return refined
            except Exception as e:
                log.warning(f"[Reasoning] Groq refinement skipped: {e}")

        return result

    def _classify_attachment(self, structured: dict):
        has_att = bool(structured.get("has_attachment", False))
        if not has_att:
            return False, "none"

        has_readable = bool(structured.get("has_readable_content", False))
        att_type     = structured.get("attachment_type", "") or ""
        description  = (structured.get("attachment_description") or "").lower()

        if description and description not in ("none", "n/a", ""):
            med_score   = sum(1 for s in MEDICAL_CERT_SIGNALS if s in description)
            event_score = sum(1 for s in EVENT_LETTER_SIGNALS if s in description)
            if med_score >= 2:
                return True, "medical_certificate"
            if event_score >= 2:
                return True, "event_letter"

        if att_type in ("medical_certificate", "prescription"):
            return True, "medical_certificate"
        if att_type == "event_letter":
            return True, "event_letter"
        if att_type in ("assignment", "code_file"):
            return True, "other"
        if has_att and not has_readable:
            return True, "unreadable"
        if has_att:
            return True, "other"
        return False, "none"

    def _rules(self, s: dict, info: dict, POLICY: dict) -> dict:
        intent   = s.get("intent", "other")
        has_att, att_type = self._classify_attachment(s)
        days     = s.get("duration_days", 0) or 0
        att_pct  = (info.get("attendance_percentage") or 75.0) if info else 75.0

        if att_pct < POLICY.get("critical_attendance", 65.0) and intent in (
            "medical_leave", "personal_leave", "on_duty"
        ):
            return {
                "decision":         "REJECT",
                "reason":           f"Attendance {att_pct:.1f}% is critically low (below {POLICY['critical_attendance']}%). Leave cannot be granted.",
                "conditions":       [],
                "suggested_action": "Speak with your academic advisor urgently.",
                "policy_flags":     ["CRITICAL_LOW_ATTENDANCE"],
                "confidence":       0.99,
            }

        if intent == "medical_leave":
            flags, conds = [], []
            cert_valid = has_att and att_type == "medical_certificate"
            cert_present_unreadable = has_att and att_type == "unreadable"

            if not has_att:
                flags.append("MISSING_CERTIFICATE")
                conds.append("Submit a medical certificate from a registered doctor within 3 days.")
            elif cert_present_unreadable:
                flags.append("CERTIFICATE_UNREADABLE")
                conds.append("Attached file could not be read. Please re-submit as a clear PDF or image.")
            elif not cert_valid:
                flags.append("MISSING_CERTIFICATE")
                conds.append("Submit a medical certificate from a registered doctor within 3 days.")

            if days > POLICY.get("max_medical_days", 5):
                flags.append("EXCEEDS_MAX_DAYS")
                conds.append(f"Duration ({days} days) exceeds {POLICY['max_medical_days']} days — HOD approval required.")

            if not flags:
                return {
                    "decision": "APPROVE",
                    "reason": "Valid medical certificate; within permitted duration.",
                    "conditions": [],
                    "suggested_action": "Keep certificate copy for records.",
                    "policy_flags": [],
                    "confidence": 0.95,
                }
            if "MISSING_CERTIFICATE" in flags and att_pct >= POLICY.get("min_attendance", 75.0):
                return {
                    "decision": "PENDING",
                    "reason": "Medical leave conditionally noted — certificate required.",
                    "conditions": conds,
                    "suggested_action": "Submit certificate to academic office immediately.",
                    "policy_flags": flags,
                    "confidence": 0.80,
                }
            if "CERTIFICATE_UNREADABLE" in flags and att_pct >= POLICY.get("min_attendance", 75.0):
                return {
                    "decision": "PENDING",
                    "reason": "Certificate received but could not be read — please re-submit.",
                    "conditions": conds,
                    "suggested_action": "Re-submit a clear scan of your medical certificate.",
                    "policy_flags": flags,
                    "confidence": 0.75,
                }
            return {
                "decision": "REJECT",
                "reason": f"No valid certificate and attendance ({att_pct:.1f}%) below {POLICY.get('min_attendance', 75)}%.",
                "conditions": [],
                "suggested_action": "Submit medical certificate to request reconsideration.",
                "policy_flags": flags,
                "confidence": 0.90,
            }

        elif intent == "personal_leave":
            if att_pct < POLICY.get("min_attendance", 75.0):
                return {
                    "decision": "REJECT",
                    "reason": f"Attendance {att_pct:.1f}% below {POLICY['min_attendance']}% — personal leave not permitted.",
                    "conditions": [],
                    "suggested_action": "Improve attendance before applying.",
                    "policy_flags": ["LOW_ATTENDANCE"],
                    "confidence": 0.92,
                }
            if days > POLICY.get("max_personal_days", 3):
                return {
                    "decision": "REJECT",
                    "reason": f"Requested {days} days exceeds max {POLICY['max_personal_days']} days.",
                    "conditions": [],
                    "suggested_action": f"Limit to {POLICY['max_personal_days']} days or get HOD approval.",
                    "policy_flags": ["EXCEEDS_MAX_DAYS"],
                    "confidence": 0.90,
                }
            return {
                "decision": "APPROVE",
                "reason": "Personal leave within permitted limits.",
                "conditions": [],
                "suggested_action": "Complete missed coursework on return.",
                "policy_flags": [],
                "confidence": 0.85,
            }

        elif intent == "on_duty":
            has_od_doc = has_att and att_type in ("event_letter", "other", "unreadable")
            if not has_od_doc:
                return {
                    "decision": "PENDING",
                    "reason": "OD requires official event participation/invitation letter.",
                    "conditions": ["Submit official letter from the organising institution on their letterhead."],
                    "suggested_action": "Obtain event letter and resubmit.",
                    "policy_flags": ["MISSING_OD_LETTER"],
                    "confidence": 0.88,
                }
            return {
                "decision": "APPROVE",
                "reason": "OD approved — event documentation present.",
                "conditions": [],
                "suggested_action": "Submit an event completion report after attending.",
                "policy_flags": [],
                "confidence": 0.92,
            }

        elif intent == "attendance_correction":
            return {
                "decision": "PENDING",
                "reason": "Attendance correction requires faculty verification.",
                "conditions": [
                    "Contact subject teacher to confirm presence on the disputed date(s).",
                    "Ask faculty to submit written confirmation to the academic office.",
                ],
                "suggested_action": "Get faculty sign-off and resubmit.",
                "policy_flags": ["NEEDS_VERIFICATION"],
                "confidence": 0.75,
            }

        elif intent == "general_query":
            return {
                "decision": "PENDING",
                "reason": "General query — requires manual review.",
                "conditions": [],
                "suggested_action": "Academic office will respond within 2 working days.",
                "policy_flags": ["GENERAL_QUERY"],
                "confidence": 0.60,
            }

        else:
            return {
                "decision": "PENDING",
                "reason": "Email type unclear — manual review needed.",
                "conditions": [],
                "suggested_action": "Resubmit with a clear subject.",
                "policy_flags": ["UNCLASSIFIED"],
                "confidence": 0.40,
            }

    def _groq_refine(self, structured: dict, student_info: dict, rule_result: dict) -> dict:
        ctx = "Unknown student" if not student_info else (
            f"Name: {student_info.get('name')}, Dept: {student_info.get('department')}, "
            f"Attendance: {student_info.get('attendance_percentage')}%"
        )
        att_quality = "none"
        if structured.get("has_attachment"):
            if structured.get("has_readable_content"):
                att_quality = f"readable — type: {structured.get('attachment_type', 'unknown')}"
            else:
                att_quality = "present but unreadable/empty"

        prompt = f"""Academic leave policy review. Rule engine returned PENDING.

Student: {ctx}
Request: intent={structured.get('intent')}, days={structured.get('duration_days')}, 
  attachment_quality={att_quality}
Attachment description: {structured.get('attachment_description', 'none')}
Reason given: {structured.get('reason')}
Rule engine reason: {rule_result.get('reason')}
Policy flags: {rule_result.get('policy_flags')}

Should this be APPROVE, REJECT, or stay PENDING? Return ONLY valid JSON:
{{"decision":"APPROVE"|"REJECT"|"PENDING","reason":"<why>","conditions":[],"suggested_action":"<next step>","policy_flags":[],"confidence":0.0}}"""

        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {self._groq_key}", "Content-Type": "application/json"},
            json={
                "model":       GROQ_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens":  300,
            },
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        refined = json.loads(raw)
        if refined.get("decision") not in ("APPROVE", "REJECT", "PENDING"):
            return rule_result
        return refined