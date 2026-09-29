"""
Run once: python gmail_oauth_setup.py
Opens browser → Google login → saves token.json
"""
import os, json

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        os.system("pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

    if not os.path.exists('credentials.json'):
        print("\n❌ credentials.json not found in backend/ folder")
        print("\nSteps:")
        print("1. console.cloud.google.com → New project")
        print("2. Enable Gmail API")
        print("3. APIs & Services → Credentials → Create OAuth 2.0 Client ID → Desktop app")
        print("4. Download JSON → rename to credentials.json → place in backend/")
        return

    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json','w') as f:
            f.write(creds.to_json())

    print("\n✅ Success! token.json saved.")
    print("\nNext: In the A-LAMS dashboard → Settings → paste the content below into Gmail Token:\n")
    with open('token.json') as f:
        token = json.load(f)
    print(json.dumps(token, indent=2))

if __name__ == '__main__':
    main()
