import os, smtplib, ssl
from email.message import EmailMessage

GMAIL_USER = os.environ["GMAIL_USER"]       # pełny adres Gmail
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

def main():
    msg = EmailMessage()
    msg["Subject"] = "Letterboxd Watch – hello from GitHub Actions"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content("If you can read this, Gmail from Actions works.")

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls(context=context)
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

if __name__ == "__main__":
    main()
