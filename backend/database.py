import sqlite3, json, os, base64
from datetime import datetime
import time
import pandas as pd
import io

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'alams.db')

class Database:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.init_db()

    def conn(self):
        c = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    def init_db(self):
        with self.conn() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE,
                    thread_id TEXT,
                    student_email TEXT,
                    student_name TEXT,
                    subject TEXT,
                    body_snippet TEXT,
                    intent TEXT,
                    structured_json TEXT,
                    decision TEXT,
                    decision_reason TEXT,
                    decision_json TEXT,
                    reply_text TEXT,
                    has_attachments INTEGER DEFAULT 0,
                    attachment_meta TEXT,
                    attachment_text TEXT,
                    replied INTEGER DEFAULT 0,
                    timestamp TEXT
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
                    filename TEXT,
                    mime_type TEXT,
                    file_size INTEGER,
                    file_data BLOB,
                    extracted_text TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (message_id) REFERENCES processed_emails(message_id)
                );

                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT UNIQUE,
                    name TEXT,
                    email TEXT UNIQUE,
                    department TEXT,
                    attendance_percentage REAL DEFAULT 75.0,
                    total_classes INTEGER DEFAULT 100,
                    attended_classes INTEGER DEFAULT 75
                );

                CREATE TABLE IF NOT EXISTS attendance_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_email TEXT,
                    date TEXT,
                    status TEXT,
                    reason TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS policy_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    policy_key TEXT UNIQUE,
                    policy_value TEXT,
                    policy_type TEXT DEFAULT 'number',
                    description TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id);
                CREATE INDEX IF NOT EXISTS idx_emails_timestamp ON processed_emails(timestamp);
                CREATE INDEX IF NOT EXISTS idx_students_email ON students(email);
                CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance_log(student_email);

                -- Insert default policies if not exist
                INSERT OR IGNORE INTO policy_config (policy_key, policy_value, policy_type, description) VALUES 
                    ('min_attendance', '75.0', 'number', 'Minimum attendance percentage required for leave approval'),
                    ('critical_attendance', '65.0', 'number', 'Critical attendance threshold - auto reject below this'),
                    ('max_medical_days', '5', 'number', 'Maximum days for medical leave without HOD approval'),
                    ('max_personal_days', '3', 'number', 'Maximum days for personal leave per semester'),
                    ('auto_reply_enabled', 'false', 'boolean', 'Enable automatic email replies'),
                    ('auto_poll_enabled', 'false', 'boolean', 'Enable automatic email fetching'),
                    ('poll_interval_minutes', '5', 'number', 'Minutes between automatic email checks');
            ''')

    # ── existing methods unchanged ──

    def is_processed(self, message_id):
        with self.conn() as c:
            row = c.execute('SELECT id FROM processed_emails WHERE message_id=?', (message_id,)).fetchone()
            return row is not None

    def log_request(self, d):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    c.execute('''INSERT OR IGNORE INTO processed_emails
                        (message_id, thread_id, student_email, student_name, subject, body_snippet,
                         intent, structured_json, decision, decision_reason, decision_json,
                         reply_text, has_attachments, attachment_meta, attachment_text, replied, timestamp)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (d.get('message_id'), d.get('thread_id',''),
                         d.get('student_email'), d.get('student_name',''),
                         d.get('subject',''), d.get('body_snippet',''),
                         d.get('intent'), d.get('structured_json','{}'),
                         d.get('decision'), d.get('decision_reason',''),
                         d.get('decision_json','{}'),
                         d.get('reply_text',''), 
                         d.get('has_attachments', 0),
                         d.get('attachment_meta', '[]'),
                         d.get('attachment_text', ''),
                         0, d.get('timestamp')))
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e

    def save_attachment(self, message_id, filename, mime_type, file_data, extracted_text=""):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    c.execute('''INSERT OR REPLACE INTO attachments 
                                (message_id, filename, mime_type, file_size, file_data, extracted_text)
                                VALUES (?,?,?,?,?,?)''',
                             (message_id, filename, mime_type, len(file_data), file_data, extracted_text))
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e

    def get_attachments(self, message_id):
        with self.conn() as c:
            rows = c.execute('''SELECT id, filename, mime_type, file_size, extracted_text, created_at 
                               FROM attachments WHERE message_id=?''', 
                            (message_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_attachment_data(self, attachment_id):
        with self.conn() as c:
            row = c.execute('SELECT filename, mime_type, file_data FROM attachments WHERE id=?', 
                           (attachment_id,)).fetchone()
            return dict(row) if row else None

    def mark_replied(self, message_id):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    c.execute('UPDATE processed_emails SET replied=1 WHERE message_id=?', (message_id,))
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e

    def get_inbox(self, page=1, limit=20, intent_filter='', decision_filter=''):
        offset = (page-1)*limit
        where, params = self._filters(intent_filter, decision_filter)
        with self.conn() as c:
            rows = c.execute(f'''SELECT * FROM processed_emails {where}
                                 ORDER BY timestamp DESC LIMIT ? OFFSET ?''',
                             params+[limit, offset]).fetchall()
        emails = []
        for r in rows:
            email = dict(r)
            att_count = c.execute('SELECT COUNT(*) FROM attachments WHERE message_id=?', 
                                 (email['message_id'],)).fetchone()[0]
            email['attachment_count'] = att_count
            emails.append(email)
        return emails

    def get_inbox_count(self, intent_filter='', decision_filter=''):
        where, params = self._filters(intent_filter, decision_filter)
        with self.conn() as c:
            return c.execute(f'SELECT COUNT(*) FROM processed_emails {where}', params).fetchone()[0]

    def _filters(self, intent, decision):
        clauses, params = [], []
        if intent:
            clauses.append('intent=?'); params.append(intent)
        if decision:
            clauses.append('decision=?'); params.append(decision)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        return where, params

    def get_email_detail(self, message_id):
        with self.conn() as c:
            row = c.execute('SELECT * FROM processed_emails WHERE message_id=?', (message_id,)).fetchone()
        if row:
            d = dict(row)
            try: d['structured'] = json.loads(d.get('structured_json','{}'))
            except: d['structured'] = {}
            try: d['decision_detail'] = json.loads(d.get('decision_json','{}'))
            except: d['decision_detail'] = {}
            try: d['attachment_meta'] = json.loads(d.get('attachment_meta','[]'))
            except: d['attachment_meta'] = []
            d['attachments'] = self.get_attachments(message_id)
            d['message_id_header'] = d.get('message_id', '')
            return d
        return None

    def get_stats(self):
        with self.conn() as c:
            total = c.execute('SELECT COUNT(*) FROM processed_emails').fetchone()[0]
            approved = c.execute("SELECT COUNT(*) FROM processed_emails WHERE decision='APPROVE'").fetchone()[0]
            rejected = c.execute("SELECT COUNT(*) FROM processed_emails WHERE decision='REJECT'").fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM processed_emails WHERE decision='PENDING'").fetchone()[0]
            no_action = c.execute("SELECT COUNT(*) FROM processed_emails WHERE decision='NO_ACTION'").fetchone()[0]
            replied = c.execute("SELECT COUNT(*) FROM processed_emails WHERE replied=1").fetchone()[0]
            with_attachments = c.execute("SELECT COUNT(*) FROM processed_emails WHERE has_attachments=1").fetchone()[0]
            students = c.execute('SELECT COUNT(*) FROM students').fetchone()[0]
            intents = c.execute('SELECT intent, COUNT(*) as c FROM processed_emails GROUP BY intent').fetchall()
            recent = c.execute('''SELECT message_id, student_email, student_name, subject, intent, decision, 
                                  has_attachments, timestamp, replied
                                  FROM processed_emails ORDER BY timestamp DESC LIMIT 5''').fetchall()
        return {
            "total": total, "approved": approved, "rejected": rejected,
            "pending": pending, "no_action": no_action,
            "replied": replied, "with_attachments": with_attachments,
            "students": students,
            "approval_rate": round(approved/total*100 if total else 0, 1),
            "intent_breakdown": [{"intent": r[0], "count": r[1]} for r in intents],
            "recent": [dict(r) for r in recent]
        }

    def get_student_by_email(self, email):
        with self.conn() as c:
            r = c.execute('SELECT * FROM students WHERE email=?', (email,)).fetchone()
        return dict(r) if r else None

    def get_all_students(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute('SELECT * FROM students ORDER BY name').fetchall()]

    def add_student(self, d):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    c.execute('''INSERT OR REPLACE INTO students
                        (student_id,name,email,department,attendance_percentage,total_classes,attended_classes)
                        VALUES (?,?,?,?,?,?,?)''',
                        (d.get('student_id'), d.get('name'), d.get('email'), d.get('department'),
                         float(d.get('attendance_percentage',75)), int(d.get('total_classes',100)),
                         int(d.get('attended_classes',75))))
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e

    def update_attendance(self, email, dates, status="excused"):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    for date in dates:
                        c.execute('INSERT OR IGNORE INTO attendance_log (student_email,date,status) VALUES (?,?,?)',
                                  (email, date, status))
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e

    def override_decision(self, message_id, new_decision, admin_notes, new_reply=""):
        retries = 5
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    email = c.execute('SELECT * FROM processed_emails WHERE message_id=?', (message_id,)).fetchone()
                    if not email:
                        return False, "Email not found", None
                    
                    email_dict = dict(email)
                    
                    c.execute('''UPDATE processed_emails 
                                SET decision = ?, decision_reason = ?, decision_json = ?, reply_text = ?
                                WHERE message_id = ?''',
                            (new_decision, f"[OVERRIDE by Admin] {admin_notes}", 
                            json.dumps({"decision": new_decision, "override": True, "notes": admin_notes}),
                            new_reply, message_id))
                    
                    if new_decision == "APPROVE":
                        try:
                            structured = json.loads(email_dict.get('structured_json', '{}'))
                            dates = structured.get('dates', [])
                            student_email = email_dict.get('student_email')
                            if dates and student_email:
                                for date in dates:
                                    c.execute('INSERT OR IGNORE INTO attendance_log (student_email,date,status) VALUES (?,?,?)',
                                            (student_email, date, "excused"))
                                self._update_student_attendance(c, student_email, len(dates))
                        except Exception as e:
                            print(f"Attendance update failed: {e}")
                    
                    return True, "Override successful", email_dict
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return False, str(e), None
            except Exception as e:
                return False, str(e), None
        
        return False, "Database locked after multiple attempts", None

    def _update_student_attendance(self, c, student_email, days_absent):
        student = c.execute('SELECT * FROM students WHERE email=?', (student_email,)).fetchone()
        if student:
            student_dict = dict(student)
            new_attended = max(0, student_dict['attended_classes'] - days_absent)
            new_percentage = (new_attended / student_dict['total_classes']) * 100 if student_dict['total_classes'] > 0 else 0
            c.execute('''UPDATE students 
                        SET attended_classes = ?, attendance_percentage = ?
                        WHERE email = ?''',
                     (new_attended, round(new_percentage, 2), student_email))

    # ── POLICY MANAGEMENT ──
    def get_all_policies(self):
        with self.conn() as c:
            rows = c.execute('SELECT * FROM policy_config ORDER BY policy_key').fetchall()
            return [dict(r) for r in rows]

    def get_policy(self, policy_key):
        with self.conn() as c:
            row = c.execute('SELECT policy_value, policy_type FROM policy_config WHERE policy_key=?', 
                           (policy_key,)).fetchone()
            if row:
                if row['policy_type'] == 'number':
                    return float(row['policy_value'])
                elif row['policy_type'] == 'boolean':
                    return row['policy_value'].lower() == 'true'
                return row['policy_value']
            return None

    def update_policy(self, policy_key, policy_value):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    c.execute('''UPDATE policy_config 
                                SET policy_value = ?, updated_at = ?
                                WHERE policy_key = ?''',
                             (str(policy_value), datetime.now().isoformat(), policy_key))
                return True
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e
        return False

    def bulk_update_policies(self, policies_dict):
        success = True
        for key, value in policies_dict.items():
            if not self.update_policy(key, value):
                success = False
        return success

    def reload_policies(self):
        policies = self.get_all_policies()
        policy_dict = {}
        for p in policies:
            if p['policy_type'] == 'number':
                policy_dict[p['policy_key']] = float(p['policy_value'])
            elif p['policy_type'] == 'boolean':
                policy_dict[p['policy_key']] = p['policy_value'].lower() == 'true'
            else:
                policy_dict[p['policy_key']] = p['policy_value']
        return policy_dict

    # ── EXCEL IMPORT/EXPORT ──
    def import_students_from_excel(self, file_data):
        import numpy as np

        def normalize(s):
            return str(s).strip().lower()

        try:
            xls = pd.ExcelFile(io.BytesIO(file_data))
            all_sheet_names = xls.sheet_names

            # Only process sheets that are likely attendance sheets (ignore CIE/consolidated)
            skip_sheets = {'cie', 'consolidated', 'att & cie'}
            sheets_to_import = [
                name for name in all_sheet_names
                if not any(s in name.lower() for s in skip_sheets)
            ]

            total_imported = 0
            errors = []

            for sheet in sheets_to_import:
                df_raw = pd.read_excel(xls, sheet_name=sheet, header=None)

                # Find the row that contains the actual column headers ("USN" and "Student Name")
                header_row_idx = None
                for idx, row in df_raw.iterrows():
                    vals = [normalize(v) for v in row if isinstance(v, str)]
                    if any('usn' in v for v in vals) and any('name' in v for v in vals):
                        header_row_idx = idx
                        break

                if header_row_idx is None:
                    errors.append(f"Sheet '{sheet}': could not find USN/Name header row")
                    continue

                # Re-read the sheet using the found header row
                df = pd.read_excel(xls, sheet_name=sheet, header=header_row_idx)
                # Drop completely empty rows and columns
                df.dropna(how='all', inplace=True)
                df.dropna(axis=1, how='all', inplace=True)

                # Auto-detect columns from string column names
                str_columns = [c for c in df.columns if isinstance(c, str)]

                usn_col = next((c for c in str_columns if 'usn' in c.lower() or 'roll' in c.lower()), None)
                name_col = next((c for c in str_columns if 'name' in c.lower()), None)
                email_col = next((c for c in str_columns if 'email' in c.lower() or 'mail' in c.lower()), None)
                dept_col = next((c for c in str_columns if 'dept' in c.lower() or 'department' in c.lower() or 'branch' in c.lower()), None)

                # Attendance column – look for "%" or "attendance" / "percentage"
                att_col = None
                for c in str_columns:
                    if c.strip() == '%' or 'attendance' in c.lower() or 'percentage' in c.lower():
                        att_col = c
                        break

                if not usn_col or not name_col:
                    errors.append(f"Sheet '{sheet}': missing USN/Name columns (found {str_columns})")
                    continue
                if not att_col:
                    errors.append(f"Sheet '{sheet}': missing attendance column (found {str_columns})")
                    continue

                imported = 0
                with self.conn() as c:
                    for idx, row in df.iterrows():
                        try:
                            student_id = str(row[usn_col]).strip()
                            name = str(row[name_col]).strip()

                            # Skip rows that are not actual student records
                            if student_id.lower() in ('', 'nan', 'usn', 'sno.') or \
                            name.lower() in ('', 'nan', 'student name', 'number of absentees'):
                                continue

                            # Email: use existing column or generate from USN
                            if email_col and pd.notna(row[email_col]):
                                email = str(row[email_col]).strip()
                            else:
                                email = f"{student_id.lower()}@student.edu"

                            # Department
                            if dept_col and pd.notna(row[dept_col]):
                                department = str(row[dept_col]).strip()
                            else:
                                department = "General"

                            # Attendance – must be a valid number
                            attendance = None
                            if pd.notna(row[att_col]):
                                try:
                                    attendance = float(row[att_col])
                                except (ValueError, TypeError):
                                    pass   # skip rows with text like "AB"
                            if attendance is None:
                                continue   # skip if no valid attendance

                            total_classes = 100
                            attended_classes = int(attendance)

                            c.execute('''INSERT OR REPLACE INTO students
                                        (student_id, name, email, department,
                                        attendance_percentage, total_classes, attended_classes)
                                        VALUES (?,?,?,?,?,?,?)''',
                                    (student_id, name, email, department,
                                    attendance, total_classes, attended_classes))
                            imported += 1
                        except Exception:
                            continue

                total_imported += imported
                if imported == 0:
                    errors.append(f"Sheet '{sheet}': no valid student rows imported")

            msg = f"Imported {total_imported} students from {len(sheets_to_import)} sheets."
            if errors:
                msg += " Errors:\n" + "\n".join(errors)
            return True, msg

        except Exception as e:
            return False, f"Excel error: {str(e)}"
        

    def export_students_to_excel(self):
        try:
            students = self.get_all_students()
            if not students:
                return None
            df = pd.DataFrame(students)
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Students', index=False)
            return output.getvalue()
        except Exception as e:
            print(f"Export error: {e}")
            return None

    # ── ATTENDANCE & DATABASE VIEWS ──
    def update_attendance_from_email(self, student_email, dates):
        retries = 3
        for attempt in range(retries):
            try:
                with self.conn() as c:
                    self._update_student_attendance(c, student_email, len(dates))
                return True
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise e
        return False

    def get_student_attendance_history(self, student_email):
        with self.conn() as c:
            student = c.execute('SELECT * FROM students WHERE email=?', (student_email,)).fetchone()
            logs = c.execute('''SELECT * FROM attendance_log 
                               WHERE student_email=? 
                               ORDER BY date DESC LIMIT 50''', 
                            (student_email,)).fetchall()
            leaves = c.execute('''SELECT * FROM processed_emails 
                                 WHERE student_email=? AND decision='APPROVE'
                                 ORDER BY timestamp DESC LIMIT 10''', 
                              (student_email,)).fetchall()
            return {
                "student": dict(student) if student else None,
                "attendance_log": [dict(r) for r in logs],
                "leave_history": [dict(r) for r in leaves]
            }

    def get_email_database(self, search="", page=1, limit=50):
        offset = (page-1)*limit
        with self.conn() as c:
            query = '''SELECT 
                        e.message_id, e.student_email, e.student_name, 
                        e.subject, e.intent, e.decision, e.timestamp, e.replied,
                        s.student_id, s.attendance_percentage, s.department
                      FROM processed_emails e
                      LEFT JOIN students s ON e.student_email = s.email
                      WHERE 1=1'''
            
            params = []
            if search:
                query += ' AND (e.student_email LIKE ? OR e.student_name LIKE ? OR e.subject LIKE ? OR s.student_id LIKE ?)'
                search_term = f"%{search}%"
                params.extend([search_term, search_term, search_term, search_term])
            
            query += ' ORDER BY e.timestamp DESC LIMIT ? OFFSET ?'
            params.extend([limit, offset])
            
            rows = c.execute(query, params).fetchall()
            
            count_query = '''SELECT COUNT(*) FROM processed_emails e
                           LEFT JOIN students s ON e.student_email = s.email
                           WHERE 1=1'''
            count_params = []
            if search:
                count_query += ' AND (e.student_email LIKE ? OR e.student_name LIKE ? OR e.subject LIKE ? OR s.student_id LIKE ?)'
                count_params = [f"%{search}%"] * 4
                total = c.execute(count_query, count_params).fetchone()[0]
            else:
                total = c.execute(count_query).fetchone()[0]
        
        return {
            "emails": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "limit": limit
        }

    def get_database_stats(self):
        with self.conn() as c:
            stats = {
                "total_emails": c.execute('SELECT COUNT(*) FROM processed_emails').fetchone()[0],
                "total_students": c.execute('SELECT COUNT(*) FROM students').fetchone()[0],
                "total_attachments": c.execute('SELECT COUNT(*) FROM attachments').fetchone()[0],
                "total_attendance_logs": c.execute('SELECT COUNT(*) FROM attendance_log').fetchone()[0],
                "emails_by_decision": {},
                "emails_by_intent": {},
                "attendance_summary": {}
            }
            
            decisions = c.execute('''SELECT decision, COUNT(*) as count 
                                    FROM processed_emails GROUP BY decision''').fetchall()
            for d in decisions:
                stats["emails_by_decision"][d['decision']] = d['count']
            
            intents = c.execute('''SELECT intent, COUNT(*) as count 
                                  FROM processed_emails GROUP BY intent''').fetchall()
            for i in intents:
                stats["emails_by_intent"][i['intent']] = i['count']
            
            attendance = c.execute('''SELECT 
                                        COUNT(*) as total,
                                        AVG(attendance_percentage) as avg_attendance,
                                        SUM(CASE WHEN attendance_percentage < 65 THEN 1 ELSE 0 END) as critical,
                                        SUM(CASE WHEN attendance_percentage >= 65 AND attendance_percentage < 75 THEN 1 ELSE 0 END) as low,
                                        SUM(CASE WHEN attendance_percentage >= 75 THEN 1 ELSE 0 END) as good
                                     FROM students''').fetchone()
            if attendance:
                stats["attendance_summary"] = dict(attendance)
        
        return stats

    def seed_demo_data(self):
        demo_students = [
            ("CS001","Arjun Sharma","arjun.sharma@student.edu","Computer Science",82,100,82),
            ("CS002","Priya Nair","priya.nair@student.edu","Computer Science",71,100,71),
            ("EC001","Rohan Mehta","rohan.mehta@student.edu","Electronics",90,100,90),
            ("ME001","Sneha Iyer","sneha.iyer@student.edu","Mechanical",62,100,62),
            ("CS003","Karthik Rajan","karthik.rajan@student.edu","Computer Science",78,100,78),
        ]
        for s in demo_students:
            self.add_student({"student_id":s[0],"name":s[1],"email":s[2],"department":s[3],
                              "attendance_percentage":s[4],"total_classes":s[5],"attended_classes":s[6]})