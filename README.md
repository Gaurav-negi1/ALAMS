# A-LAMS v3 — Autonomous Academic Leave Management System

![NLP](https://img.shields.io/badge/NLP-Multi--Model-blue)
![AI](https://img.shields.io/badge/AI-Groq%20%7C%20Gemini-purple)
![UI](https://img.shields.io/badge/UI-Tailwind%20Dark%20Mode-green)

**A‑LAMS v3** is an intelligent, autonomous email processing system designed for university academic offices. It fetches unread student emails, understands their content using AI, applies configurable academic policies, creates professional replies, and optionally sends them – all with full human override capability.

---

## ✨ Key Features

- **Fully Autonomous Pipeline**  
  Background poller → Gmail fetch → AI perception → policy reasoning → reply generation → auto‑send → log everything.

- **Multi‑AI Engine**  
  - **Groq (LLaMA 3.3 70B)** – intent classification, reply generation, decision refinement  
  - **Google Gemini Flash** – vision analysis of medical certificates, event letters, images

- **10+ NLP Techniques**  
  Text classification, NER, summarisation, sentiment analysis, NLG, semantic similarity, keyword extraction, document understanding, text normalisation, contextual reasoning.

- **Policy‑Based Decision Maker**  
  Editable policies via **Policy Manager** page (no hardcoded rules). Attendance thresholds, leave limits, automation booleans, poll interval – all configurable through the UI.

- **Human‑in‑the‑Loop – Professor Override**  
  Every email detail page contains an **Override Console** that lets an admin change the decision, add notes, and instantly send a revised reply.

- **Automated Attendance Tracking**  
  On APPROVE decisions, attendance log and student attendance percentage are automatically updated. Excel import syncs with USN‑based student records.

- **Attachment Intelligence**  
  - PDFs → pdfplumber text extraction  
  - Images → Gemini Vision (medical certificates, event invitations) or pytesseract OCR fallback  
  - DOCX → python‑docx reading

- **Excel Import / Export**  
  Bulk upload student data (USN, name, email, department, attendance) from `.xlsx` files. Export current student records anytime.

- **Comprehensive Database View**  
  Searchable table merging emails + student attendance details. Click any row to see a student’s full attendance history.

- **Premium Dark‑Mode UI**  
  Glass‑morphism, animated status indicators, responsive single‑page app built with Tailwind CSS and vanilla JavaScript.

---

## 🧠 Architecture
┌───────────────┐ ┌──────────────┐ ┌───────────────┐
│ Frontend │◄────►│ Flask API │◄────►│ SQLite DB │
│ (HTML/JS) │ │ (app.py) │ │ (WAL mode) │
└───────────────┘ └──────┬───────┘ └───────────────┘
│
┌───────────────┼───────────────┐
│ │ │
Perception Reasoning Execution
Agent Agent Agent
│ │ │
Groq+Gemini DB Policies Gmail API

text

*Perception → Reasoning → Action pipeline continuously processes unread emails.*

---

## 📁 Project Structure
A-LAMS/
├── backend/
│ ├── app.py # Main Flask server & API routes
│ ├── database.py # SQLite operations (WAL, retry, policies, Excel)
│ ├── gmail_service.py # Gmail API wrapper
│ ├── gmail_oauth_setup.py # OAuth 2.0 setup script
│ ├── agents/
│ │ ├── perception_agent.py # Email understanding & attachment analysis
│ │ ├── reasoning_agent.py # Policy‑driven decision logic
│ │ └── execution_agent.py # Reply generation & DB updates
│ ├── credentials.json # Google OAuth credentials (user provided)
│ └── token.json # Generated Gmail token
├── frontend/
│ └── index.html # Single‑page application (Dashboard, Inbox, Students,
│ Settings, Policy Manager, Database)
├── data/
│ ├── alams.db # SQLite database (auto‑created)
│ └── config.json # API keys & settings (auto‑created)
└── requirements.txt

text

---

## 🛠️ Technology Stack

| Layer       | Technologies |
|-------------|--------------|
| **Backend** | Python 3, Flask, SQLite3, threading |
| **AI/ML**   | Groq API (LLaMA 3.3 70B), Google Gemini Flash, pdfplumber, pytesseract, Pillow |
| **Frontend**| HTML5, Tailwind CSS (CDN), vanilla JavaScript, Chart.js, Font Awesome |
| **External**| Gmail API (OAuth 2.0) |

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone <your-repo-url>
cd A-LAMS/backend
pip install -r requirements.txt
If requirements.txt is missing, run:

bash
pip install flask flask-cors requests google-api-python-client google-auth-httplib2 google-auth-oauthlib pdfplumber pytesseract Pillow python-docx pandas openpyxl
(Optional: install tesseract-ocr system package for offline image OCR)

2. Gmail API Setup
Go to Google Cloud Console → Create project → enable Gmail API.

Credentials → Create OAuth 2.0 Client ID → Desktop app.

Download JSON, rename it to credentials.json, place it inside backend/.

Run the OAuth flow:

bash
cd backend
python gmail_oauth_setup.py
Copy the printed token.json content.
In the app, go to Settings → Gmail Connection, paste it into the textarea, and press Save Token.

3. AI API Keys
Groq: console.groq.com → create API key

Gemini: aistudio.google.com/app/apikey → create API key

4. Start the Server
bash
python app.py
Open http://localhost:5000 in your browser.

5. Configure the App
Settings → enter Groq & Gemini keys.

Toggle Auto‑fetch emails and Auto‑reply as desired.

Set poll interval (minutes).

Click Save All Settings.

6. Add Student Records
Student Hub → Load Demo Data (5 sample students).

Database → Import Excel to upload your own class list (auto‑detects USN/Name/Email/Dept/Attendance columns).

7. Start Processing
Manual: Click Fetch Now / Quick Fetch Emails.

Automatic: The background poller runs every N minutes.

All processed emails appear in Smart Inbox. You can click any email to view details, attachments, AI analysis, and the Professor Override Console.

⚙️ Policy Manager
Navigate to Policies in the sidebar to edit:

Minimum Attendance (%) for leave approval

Critical Attendance (%) – auto‑reject below this

Maximum Medical Leave days

Maximum Personal Leave days

Enable/disable auto‑fetch and auto‑reply

Poll interval (minutes)

Changes are saved to the database and immediately reloaded into the reasoning engine. No restart required.

📧 Email Detail & Professor Override
Every processed email shows:

Original body, attachments (preview & download)

AI Analysis: intent, decision, reason

Professor Override Console

Select new decision (APPROVE / REJECT / PENDING)

Add admin notes

Live reply preview

Option to send the updated reply via Gmail

Automatically updates attendance records on APPROVE

Override history is stored permanently and visible in the Database page.

🗄️ Database & Attendance
The Database page provides a searchable table of all processed emails joined with student records (USN, department, current attendance). Click any row to see the student’s full attendance history (leave approvals, attendance log entries).

Attendance is automatically maintained:

On APPROVE decision, the student’s attended_classes is decremented and percentage recalculated.

Excel import pre‑loads attendance data (USN → email mapping).

Manual override updates attendance identically.

📊 Dashboard
Real‑time cards show total emails processed, approved, pending, and with attachments. Recent activity list with quick navigation. Status indicators for Groq, Gemini, and Gmail connections.

🔒 Security
Gmail uses OAuth 2.0 – no password stored.

API keys are kept in data/config.json (excluded from version control).

Override actions are logged with admin notes.

🧪 NLP Techniques (Deep Dive)
Technique	Implementation
Text Classification	Groq LLaMA 3.3 (zero‑shot) + rule‑based keyword fallback
Named Entity Recognition	Date patterns, student names, institution names
Summarisation	Gemini Vision for documents, Groq for email body
Natural Language Generation	Groq for replies, template‑based fallback
Sentiment / Urgency	Keyword‑based (urgent, emergency, ASAP)
Semantic Similarity	Signal word scoring for attachment type detection
Document Understanding	pdfplumber, pytesseract, Gemini Vision
Text Normalisation	HTML stripping, whitespace cleanup, email extraction
Contextual Reasoning	Policy engine with configurable thresholds
OCR	pytesseract (fallback when Gemini unavailable)
🛠️ Customisation
All academic policies can be adjusted from the Policy Manager page without touching code. For deeper changes, you can modify:

reasoning_agent.py – add new policy rules

execution_agent.py – change reply templates

perception_agent.py – refine intent keywords

📄 License
This project is provided for educational and internal use. Adapt policies, data retention, and privacy practices to your institution’s requirements.

