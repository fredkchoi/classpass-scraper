import smtplib
from email.mime.text import MIMEText
from config import SENDER_EMAIL, SENDER_PASSWORD, RECIPIENT_EMAIL

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def send_email(subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
