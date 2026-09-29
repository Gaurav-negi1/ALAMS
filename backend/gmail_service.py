"""
Gmail Service — OAuth2 email fetch, attachment parsing, send reply.
"""

import base64
import re
import logging
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GOOGLE_OK = True
except ImportError:
    GOOGLE_OK = False
    log.warning("[Gmail] google-api-python-client not installed.")

try:
    import pdfplumber
    import io as _pdf_io
    PDF_OK = True
except ImportError:
    PDF_OK = False

try:
    import pytesseract
    from PIL import Image
    import io as _ocr_io
    OCR_OK = True
except ImportError:
    OCR_OK = False


class GmailService:

    def __init__(self, token_dict: dict):
        if not GOOGLE_OK:
            raise RuntimeError("Install: pip install google-api-python-client google-auth google-auth-oauthlib")
        if not token_dict:
            raise ValueError("Gmail token is empty. Reconnect Gmail in Settings.")

        try:
            creds = Credentials.from_authorized_user_info(token_dict)
        except Exception as e:
            raise ValueError(f"Invalid Gmail token: {e}") from e

        if creds.expired:
            if creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise RuntimeError(f"Token refresh failed: {e}. Reconnect Gmail.") from e
            else:
                raise RuntimeError("Token expired with no refresh_token. Reconnect Gmail.")

        self.svc = build("gmail", "v1", credentials=creds)

    def _decode_b64(self, data: str) -> bytes:
        """Helper to fix Google's unpadded URL-safe base64 strings."""
        if not data:
            return b""
        # Pad the base64 string to be a multiple of 4
        data += "=" * ((4 - len(data) % 4) % 4)
        return base64.urlsafe_b64decode(data)

    # ── Fetch ─────────────────────────────────────────────────────────────────
    def fetch_unread_emails(self, max_results=20, newer_than_days=7):
        import time
        cutoff = int(time.time()) - (newer_than_days * 86400)
        query  = f"is:unread in:inbox after:{cutoff}"
        try:
            res = self.svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        except Exception as e:
            raise RuntimeError(f"Failed to list Gmail messages: {e}") from e

        emails = []
        for m in res.get("messages", []):
            try:
                parsed = self._parse(m["id"])
                if parsed:
                    emails.append(parsed)
            except Exception as ex:
                log.warning(f"[Gmail] Failed to parse {m['id']}: {ex}")

        log.info(f"[Gmail] Fetched {len(emails)} unread emails")
        return emails

    # ── Parse one message ─────────────────────────────────────────────────────
    def _parse(self, msg_id: str) -> dict:
        msg  = self.svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        hdrs = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

        sender_raw   = hdrs.get("From", "")
        m            = re.search(r"<(.+?)>", sender_raw)
        sender_email = m.group(1) if m else sender_raw.strip()
        sender_name  = re.sub(r"\s*<.*?>", "", sender_raw).strip().strip('"')

        att_text, att_meta, att_bytes = self._attachments(msg_id, msg["payload"])

        return {
            "message_id":        msg_id,
            "thread_id":         msg.get("threadId", msg_id),
            "sender":            sender_email,
            "sender_name":       sender_name,
            "subject":           hdrs.get("Subject", "(no subject)"),
            "date":              hdrs.get("Date", ""),
            "message_id_header": hdrs.get("Message-ID", ""),
            "body":              self._body(msg["payload"]),
            "attachment_text":   "\n".join(att_text),   
            "attachment_meta":   att_meta,              
            "attachment_bytes":  att_bytes,             
        }

    # ── Body extraction ───────────────────────────────────────────────────────
    def _body(self, payload: dict) -> str:
        plain, html = "", ""

        def walk(part):
            nonlocal plain, html
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if mime == "text/plain" and data:
                plain += self._decode_b64(data).decode("utf-8", errors="ignore")
            elif mime == "text/html" and data:
                html  += self._decode_b64(data).decode("utf-8", errors="ignore")
            for sub in part.get("parts", []):
                walk(sub)

        walk(payload)
        if plain.strip():
            return plain.strip()
        if html.strip():
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"&nbsp;", " ", text)
            return re.sub(r"\s+", " ", text).strip()
        return ""

    # ── Attachment extraction ─────────────────────────────────────────────────
    def _attachments(self, msg_id: str, payload: dict):
        legacy_texts = []
        meta         = []
        att_bytes    = {}

        def walk(parts):
            for p in parts:
                fn   = p.get("filename", "")
                mime = p.get("mimeType", "")
                body = p.get("body", {})
                
                if fn:
                    meta.append({"filename": fn, "mime_type": mime})
                    att_id = body.get("attachmentId")
                    
                    if att_id:
                        try:
                            att  = self.svc.users().messages().attachments().get(
                                userId="me", messageId=msg_id, id=att_id
                            ).execute()
                            # Properly decode padded base64
                            raw  = self._decode_b64(att["data"])
                            att_bytes[fn] = raw
                            
                            log.info(f"[Gmail] Fetched attachment: {fn} ({len(raw)} bytes, {mime})")

                            if mime == "application/pdf" and PDF_OK:
                                try:
                                    import io
                                    with pdfplumber.open(io.BytesIO(raw)) as pdf:
                                        text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
                                        if text.strip():
                                            legacy_texts.append(text)
                                            log.info(f"[Gmail] PDF extracted: {fn} ({len(text)} chars)")
                                except Exception as e:
                                    log.debug(f"[Gmail] PDF pre-extract failed for {fn}: {e}")

                            elif mime.startswith("image/") and OCR_OK:
                                try:
                                    import io
                                    img = Image.open(io.BytesIO(raw))
                                    text = pytesseract.image_to_string(img)
                                    if text.strip():
                                        legacy_texts.append(text)
                                        log.info(f"[Gmail] OCR extracted: {fn} ({len(text)} chars)")
                                except Exception as e:
                                    log.debug(f"[Gmail] OCR pre-extract failed for {fn}: {e}")

                        except Exception as e:
                            log.warning(f"[Gmail] Failed to fetch attachment {fn}: {e}")
                else:
                    # Check if this part has nested parts
                    if "parts" in p:
                        walk(p["parts"])

        if "parts" in payload:
            walk(payload["parts"])

        return [t for t in legacy_texts if t.strip()], meta, att_bytes

    # ── Send ──────────────────────────────────────────────────────────────────
    def send_reply(self, thread_id, to, subject, body, in_reply_to=""):
        """Send a reply email"""
        import base64
        from email.mime.text import MIMEText
        
        log.info(f"[Gmail] Preparing to send reply to: {to}")
        log.info(f"[Gmail] Subject: {subject}")
        log.info(f"[Gmail] Body length: {len(body)} chars")
        
        # Create message
        msg = MIMEText(body, 'plain', 'utf-8')
        msg["to"] = to
        msg["subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        
        # Encode the message properly
        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        
        # Create the message body
        message_body = {
            'raw': raw_message,
            'threadId': thread_id
        }
        
        log.info(f"[Gmail] Sending via Gmail API...")
        
        try:
            # Send the message
            result = self.svc.users().messages().send(
                userId="me", 
                body=message_body
            ).execute()
            
            log.info(f"[Gmail] ✅ Reply sent successfully! Message ID: {result.get('id')}")
            return result
            
        except Exception as e:
            log.error(f"[Gmail] ❌ Failed to send reply: {e}")
            raise

    def mark_as_read(self, msg_id):
        try:
            self.svc.users().messages().modify(
                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception as e:
            log.warning(f"[Gmail] mark_as_read failed: {e}")