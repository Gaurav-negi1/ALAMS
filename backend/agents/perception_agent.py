"""
Perception Agent — True semantic NLP email understanding with full attachment processing.
"""

import re
import json
import base64
import logging
import requests
from typing import Dict, Any

log = logging.getLogger(__name__)

# ── API config ────────────────────────────────────────────────────────────────
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Updated Gemini API endpoint - using gemini-flash-latest as per your example
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"

# ── Rule fallback keyword bank ────────────────────────────────────────────────
KEYWORDS = {
    "medical_leave": [
        "sick","fever","ill","illness","hospital","doctor","medical","health",
        "medicine","treatment","surgery","clinic","unwell","flu","injury","disease",
        "infection","covid","diagnosed","prescription","ward","admitted",
    ],
    "personal_leave": [
        "personal","family","emergency","relative","marriage","wedding","funeral",
        "travel","home town","function","ceremony","engagement","bereavement",
    ],
    "on_duty": [
        "symposium","hackathon","conference","workshop","competition","seminar",
        "fest","internship","industrial visit","on duty","od request","represent",
        "participation","coding contest","cultural","sports","national level",
    ],
    "attendance_correction": [
        "marked absent","attendance error","wrongly marked","present but",
        "attendance issue","mismatch","not marked","discrepancy","rectify",
        "update attendance",
    ],
}

MONTHS = (
    "january|february|march|april|may|june|july|august|september|"
    "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
)
DATE_PATTERNS = [
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    r"\b\d{1,2}\s+(?:" + MONTHS + r")\s+\d{4}\b",
    r"\b(?:" + MONTHS + r")\s+\d{1,2},?\s+\d{4}\b",
    r"\b(?:" + MONTHS + r")\s+\d{1,2}\b",
]

CODE_EXTS = {
    ".py",".java",".c",".cpp",".js",".ts",".zip",".rar",
    ".ipynb",".sql",".html",".css",".xlsx",".pptx",
}


class PerceptionAgent:

    def __init__(self):
        self._groq_key   = ""
        self._gemini_key = ""

    def set_groq_key(self, key: str):
        self._groq_key = key or ""

    def set_gemini_key(self, key: str):
        self._gemini_key = key or ""

    # ── Public entry point ────────────────────────────────────────────────────
    def process(self, email_data: dict) -> dict:
        att_info = self._analyse_attachments(email_data)

        if self._groq_key:
            try:
                return self._groq_classify(email_data, att_info)
            except Exception as e:
                log.warning(f"[Perception] Groq failed ({e}), using rule fallback")

        return self._rule_classify(email_data, att_info)

    # ── Attachment pipeline ───────────────────────────────────────────────────
    def _analyse_attachments(self, email_data: dict) -> dict:
        meta       = email_data.get("attachment_meta", [])
        att_bytes  = email_data.get("attachment_bytes", {})
        legacy_txt = email_data.get("attachment_text", "").strip()

        # No attachments at all
        if not meta and not legacy_txt:
            return {"has_attachment": False, "files": [], "combined_text": ""}

        files = []
        parts = []

        if legacy_txt:
            parts.append(legacy_txt)

        for m in meta:
            fn   = m.get("filename", "unknown")
            mime = m.get("mime_type", "")
            raw  = att_bytes.get(fn, b"")

            content = self._read_attachment(fn, mime, raw)
            files.append({
                "filename":        fn,
                "mime_type":       mime,
                "content_preview": content[:300],
                "content_length":  len(content),
                "readable":        bool(content and not content.startswith("(")),
            })
            if content:
                parts.append(f"[Attachment: {fn}]\n{content}")

        combined = "\n\n".join(parts)

        # Only mark has_attachment=True if there's actually something to work with
        has_meaningful_content = bool(combined.strip())
        return {
            "has_attachment":  bool(meta),
            "has_readable_content": has_meaningful_content,
            "files":           files,
            "combined_text":   combined,
        }

    def _read_attachment(self, filename: str, mime: str, raw: bytes) -> str:
        fn = filename.lower()

        if any(fn.endswith(ext) for ext in CODE_EXTS):
            return f"(binary/code file — {filename})"

        # PDF
        if mime == "application/pdf" or fn.endswith(".pdf"):
            if not raw:
                return "(PDF attachment present but no bytes received)"
            return self._extract_pdf(raw)

        # Images → Gemini vision
        if mime.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            if not raw:
                return "(image attachment present but no bytes received)"
            if self._gemini_key:
                try:
                    result = self._gemini_vision(raw, mime or "image/jpeg", filename)
                    log.info(f"[Perception] Gemini vision extracted {len(result)} chars from {filename}")
                    return result
                except Exception as e:
                    log.warning(f"[Perception] Gemini vision failed for {filename}: {e}")
            # OCR fallback
            ocr = self._ocr_fallback(raw)
            if ocr.strip():
                return ocr
            return "(image attachment — no Gemini key configured, text extraction unavailable)"

        # Word documents
        if fn.endswith((".doc", ".docx")):
            if not raw:
                return "(DOCX attachment present but no bytes received)"
            return self._extract_docx(raw)

        # Plain text
        if mime == "text/plain" or fn.endswith(".txt"):
            return raw.decode("utf-8", errors="ignore")[:2000] if raw else ""

        # Unknown — acknowledge existence
        if raw:
            return f"(attachment present: {filename}, type: {mime or 'unknown'}, size: {len(raw)} bytes)"
        return f"(attachment listed: {filename}, no bytes available)"

    def _gemini_vision(self, raw: bytes, mime: str, filename: str) -> str:
        """
        Sends image to Gemini using the X-goog-api-key header (correct format).
        """
        b64 = base64.b64encode(raw).decode()

        # Proper Gemini REST format with X-goog-api-key header
        payload = {
            "contents": [{
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": mime,
                            "data": b64,
                        }
                    },
                    {
                        "text": (
                            "This image is an attachment from a student email sent to a university academic office. "
                            "Describe what type of document this is (e.g. medical certificate, doctor prescription, "
                            "hospital discharge summary, event/hackathon invitation letter, attendance record, "
                            "assignment, identity card, or other). Then state: who issued it, "
                            "patient/participant name if visible, all dates mentioned, diagnosis or event details. "
                            "Be factual and concise. Max 250 words."
                        )
                    }
                ]
            }],
            "generationConfig": {
                "maxOutputTokens": 400,
                "temperature": 0.0,
            }
        }

        log.info(f"[Perception] Sending {len(raw)} bytes image to Gemini for analysis...")
        
        r = requests.post(
            GEMINI_URL,
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": self._gemini_key  # Correct header format
            },
            json=payload,
            timeout=30,
        )

        if r.status_code != 200:
            error_msg = f"Gemini API error {r.status_code}"
            try:
                error_data = r.json()
                if "error" in error_data:
                    error_msg += f": {error_data['error'].get('message', 'Unknown error')}"
            except:
                error_msg += f": {r.text[:300]}"
            log.error(f"[Perception] {error_msg}")
            r.raise_for_status()

        data = r.json()
        try:
            # Parse the response structure correctly
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info(f"[Perception] Gemini successfully analyzed image: {len(text)} chars")
            return text
        except (KeyError, IndexError) as e:
            log.warning(f"[Perception] Gemini unexpected response structure: {data}")
            raise ValueError(f"Unexpected Gemini response: {e}")

    def _extract_pdf(self, raw: bytes) -> str:
        # Try pdfplumber first
        try:
            import pdfplumber
            import io
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                pages_text = []
                for pg in pdf.pages:
                    t = pg.extract_text()
                    if t:
                        pages_text.append(t)
                result = "\n".join(pages_text)
                if result.strip():
                    log.info(f"[Perception] PDF extracted {len(result)} chars via pdfplumber")
                    return result[:3000]
        except ImportError:
            log.warning("[Perception] pdfplumber not installed — run: pip install pdfplumber")
        except Exception as e:
            log.debug(f"[Perception] pdfplumber failed: {e}")

        # Fallback: try raw text decode
        try:
            text = raw.decode("latin-1", errors="ignore")
            readable = re.findall(r"[a-zA-Z0-9 ,.\-/:()\n]{20,}", text)
            if readable:
                result = " ".join(readable)[:2000]
                log.info(f"[Perception] PDF fallback extracted {len(result)} chars via raw decode")
                return result
        except Exception:
            pass

        return "(PDF attachment present — text extraction failed. Ensure pdfplumber is installed: pip install pdfplumber)"

    def _ocr_fallback(self, raw: bytes) -> str:
        try:
            import pytesseract
            from PIL import Image
            import io
            result = pytesseract.image_to_string(Image.open(io.BytesIO(raw)))
            return result[:2000]
        except ImportError:
            return ""
        except Exception:
            return ""

    def _extract_docx(self, raw: bytes) -> str:
        try:
            import docx
            import io
            doc = docx.Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())[:2000]
        except ImportError:
            log.warning("[Perception] python-docx not installed — run: pip install python-docx")
            return "(DOCX attachment — python-docx not installed)"
        except Exception as e:
            log.debug(f"[Perception] DOCX extract failed: {e}")
            return ""

    # ── Groq semantic classifier ──────────────────────────────────────────────
    def _groq_classify(self, email_data: dict, att_info: dict) -> dict:
        att_text = att_info.get("combined_text", "").strip() or "(no attachments)"

        system = """You are an intelligent email triage agent for a university academic office.

Emails arrive from many sources: students making requests, Google Classroom / LMS notifications,
automated mailers, newsletters, spam, teachers broadcasting announcements, etc.

Your job is to deeply understand the TRUE purpose of each email and determine what action is needed.

=== INTENT VALUES ===
"medical_leave"         — student explicitly requesting leave due to illness/health
"personal_leave"        — student explicitly requesting leave for personal/family reasons  
"on_duty"               — student requesting OD/On-Duty for academic event/competition/internship
"attendance_correction" — student says they were wrongly marked absent, wants records fixed
"general_query"         — direct question from a student needing a human response
"no_action_needed"      — ANY of the following:
    • Automated notifications (Google Classroom, Canvas, Moodle, Teams, etc.)
    • Broadcast announcements from teachers/admin
    • Newsletters, circulars, FYI emails
    • Noreply/mailer sender addresses
    • System alerts (password expiry, storage warning, etc.)
    • Event announcements where the student is NOT requesting anything
    • CC emails not addressed to this office

=== NEEDS_REPLY RULES ===
needs_reply = false when:
  • intent is "no_action_needed"
  • sender is automated/noreply/system
  • email is a broadcast to many recipients
  • email is purely informational with no ask

needs_reply = true when:
  • student is making a specific request that needs approval or action
  • student asked a direct question needing a human answer

=== ATTACHMENT_TYPE ===
Based on actual attachment content analysis provided to you:
  "medical_certificate" — has diagnosis, patient name, doctor/hospital info
  "prescription"        — doctor's prescription with medication
  "event_letter"        — invitation/participation from organising institution
  "assignment"          — student homework/project
  "code_file"           — source code, zip, notebook
  "other_document"      — anything else readable
  "none"                — no attachment"""

        user = f"""Analyse this email completely and return structured JSON.

FROM   : {email_data.get('sender', '')}
SUBJECT: {email_data.get('subject', '')}
DATE   : {email_data.get('date', '')}

BODY:
{email_data.get('body', '')[:2000]}

ATTACHMENT CONTENT (fully extracted and analysed):
{att_text[:3000]}

Return ONLY valid JSON, no markdown fences:
{{
  "intent": "<intent value>",
  "needs_reply": <true|false>,
  "dates": ["<date strings extracted>"],
  "duration_days": <integer, 0 if not a leave request>,
  "reason": "<actual reason stated by student, or 'n/a'>",
  "has_attachment": <true|false>,
  "attachment_type": "<attachment type>",
  "attachment_description": "<what the attachment actually contains, or 'none'>",
  "urgency": "low|medium|high",
  "email_nature": "<one sentence: what this email actually is>",
  "key_entities": {{
    "student_name": "<name if found>",
    "institution_mentioned": "<hospital or event organisation name>"
  }}
}}"""

        r = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {self._groq_key}",
                "Content-Type": "application/json",
            },
            json={
                "model":       GROQ_MODEL,
                "messages":    [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "temperature": 0.0,
                "max_tokens":  700,
            },
            timeout=25,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)

        # Enforce consistency
        if result.get("intent") == "no_action_needed":
            result["needs_reply"]   = False
            result["duration_days"] = 0
            result["dates"]         = []

        # Merge real attachment metadata from our own extraction
        result["has_attachment"]         = att_info.get("has_attachment", False)
        result["has_readable_content"]   = att_info.get("has_readable_content", False)
        result["attachment_files"]       = att_info.get("files", [])

        return result

    # ── Rule-based fallback ───────────────────────────────────────────────────
    def _rule_classify(self, email_data: dict, att_info: dict) -> dict:
        subject = email_data.get("subject", "")
        body    = email_data.get("body", "")
        sender  = email_data.get("sender", "").lower()
        text    = f"{subject} {body}".lower()

        NO_ACTION_SENDERS = [
            "noreply","no-reply","donotreply","notifications@","mailer@",
            "classroom.google","canvas","moodle","automated","newsletter",
        ]
        NO_ACTION_SUBJECTS = [
            "announcement","[notice]","fyi:","reminder:","newsletter",
            "[update]","notification","circular","new assignment posted",
            "assignment due","quiz posted",
        ]
        is_no_action = (
            any(s in sender for s in NO_ACTION_SENDERS) or
            any(s in subject.lower() for s in NO_ACTION_SUBJECTS)
        )
        if is_no_action:
            return self._make_result(
                intent="no_action_needed", needs_reply=False,
                dates=[], duration_days=0, reason="n/a",
                att_info=att_info, urgency="low",
                email_nature="Automated notification — no reply needed.",
            )

        intent, best = "general_query", 0
        for candidate, kws in KEYWORDS.items():
            score = sum(1 for kw in kws if kw in text)
            if score > best:
                best, intent = score, candidate

        dates = []
        for pat in DATE_PATTERNS:
            dates.extend(re.findall(pat, f"{subject} {body}", re.IGNORECASE))
        dates = list(dict.fromkeys(dates))

        duration = len(dates) if dates else (1 if intent not in ("general_query",) else 0)
        m = re.search(r"(\d+)\s+days?", text)
        if m:
            duration = int(m.group(1))

        urgency = "high" if any(w in text for w in ["urgent","emergency","immediately","asap"]) else "medium"

        return self._make_result(
            intent=intent, needs_reply=True,
            dates=dates, duration_days=duration,
            reason=body[:200] or "n/a",
            att_info=att_info, urgency=urgency,
            email_nature=f"Student request: {intent.replace('_',' ')}.",
        )

    def _make_result(self, intent, needs_reply, dates, duration_days,
                     reason, att_info, urgency, email_nature) -> dict:
        files = att_info.get("files", [])
        return {
            "intent":                 intent,
            "needs_reply":            needs_reply,
            "dates":                  dates,
            "duration_days":          duration_days,
            "reason":                 reason,
            "has_attachment":         att_info.get("has_attachment", False),
            "has_readable_content":   att_info.get("has_readable_content", False),
            "attachment_type":        "none",
            "attachment_description": att_info.get("combined_text", "")[:300] or "none",
            "attachment_files":       files,
            "urgency":                urgency,
            "email_nature":           email_nature,
            "key_entities": {"student_name": None, "institution_mentioned": None},
        }