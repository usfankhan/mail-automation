IMAP-> Internet Message Access Protocol
GMAIL API -> Generate App Password
SMTP-> Simple Mail Transfer Protocol

    User Sends to "mail" with Attachment
            |       
            ▽
        IMAP Unread Mails are Read
            |       
            ▽
    Filter's Mail Domain with "Gmail, Outlook, Yahoo"
    Blocked Mail Domain "Muraii" internal communication
            |
            ▽
   Uses SMTP to send Acknowledgment Mail
            |       
            ▽
 Mail Templete with Properties - {date} {company name} {time} {walkin_location} {reply_subject} {Role for Hiring}
            |       
            ▽
    Send's Back Acknowledgment Mail to user

Logs are Recorded

For Execution
python mail_agent.py