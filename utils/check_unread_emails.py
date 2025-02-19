import imaplib
import email
from email.header import decode_header
from email.utils import getaddresses
from typing import List, Dict
from datetime import datetime
import os

# IMAP server settings for Gmail
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

def parse_email_addresses(header_value: str) -> List[str]:
    """
    Parse email addresses from a header value, handling multiple addresses.
    Returns a list of email addresses.
    """
    if not header_value:
        return []
    # getaddresses handles both "Name <email>" format and multiple addresses
    parsed = getaddresses([header_value])
    # Extract just the email part, lowercase it, and strip whitespace
    return [email.lower().strip() for name, email in parsed]

def check_unread_emails(username=None, password=None) -> List[Dict]:
    """
    Connect to Gmail API to fetch unread emails from user's inbox and mark them as read.
    
    Args:
        username: Gmail address (optional, defaults to env var)
        password: App-specific password (optional, defaults to env var)
    
    Returns:
        List of dicts containing email info:
        - sender: Email address of sender
        - to: Email addresses of To recipients
        - cc: Email addresses of CC recipients
        - bcc: Email addresses of BCC recipients
        - subject: Email subject
        - body: Email body text
        - timestamp: When email was received
        - message_id: Unique identifier of the email
        - in_reply_to: Message ID of the email this one is replying to (if any)
        - references: Thread reference IDs
        - reply_to: Email address for replies if provided
    """
    pass