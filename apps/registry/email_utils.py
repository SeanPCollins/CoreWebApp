"""Email utilities for EDiT"""
import logging
from django.core.mail import EmailMessage
from django.conf import settings
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

def send_email(subject, message, recipient_list, sender=None, is_html=False, reply_to=None):
    """
    Send email with error handling.
    """
    sender = sender or settings.DEFAULT_FROM_EMAIL

    try:
        email = EmailMessage(subject, message, sender, recipient_list, reply_to=reply_to)
        email.content_subtype = 'html' if is_html else 'plain'
        email.send(fail_silently=False)
        logger.info(f"Email sent successfully to {recipient_list}")
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        raise

def send_activation_email(user, activation_url, reason):
    """Send account activation email to admin with the user's reply-to address"""
    subject = '[EDiT] Account Activation Request'
    
    # We can use a template or fallback cleanly if template isn't built yet
    message = (
        f"A new account has been requested on EDiT.\n\n"
        f"Name: {user.first_name} {user.last_name}\n"
        f"Username: {user.username}\n"
        f"Email: {user.email}\n"
        f"Reason: {reason}\n\n"
        f"Click the link below to review and activate this account:\n"
        f"{activation_url}\n\n"
        f"--- \n"
        f"Note: Replying to this email will reply directly to {user.email}."
    )
    
    send_email(subject, message, [settings.ADMIN_EMAIL], reply_to=[user.email])

def send_activation_confirmation(user_email):
    """Send confirmation email to user that their request is under review"""
    subject = 'EDiT Account Activation Request Received'
    message = (
        "Your EDiT (Endocrine Disruption Information Tracker) account activation request "
        "has been received and is under review. Please wait while your account is verified. "
        "We will email you when it has been approved."
    )
    send_email(subject, message, [user_email])
