"""Communication service for sending and logging all communications."""

import logging
import os
import re
from datetime import datetime
from typing import Optional, Dict, Any, List, Union

from django.conf import settings
from django.core.mail import send_mail, EmailMessage, EmailMultiAlternatives
from django.utils import timezone

from .models import CommunicationLog, CommunicationTemplate, CommunicationWebhookEventLog
from .utils import get_current_user

logger = logging.getLogger(__name__)


class CommunicationService:
    """Service for sending and logging all communications."""

    @staticmethod
    def get_template(
        template: Optional[Union[CommunicationTemplate, int, str]] = None,
        template_id: Optional[int] = None,
        template_code: Optional[str] = None,
        communication_type: Optional[str] = None,
    ) -> Optional[CommunicationTemplate]:
        """
        Get a communication template by ID, code, or object.
        
        Args:
            template: CommunicationTemplate instance, template ID (int), or template code (str)
            template_id: Optional template ID
            template_code: Optional template code
            communication_type: Optional communication type filter (for code lookup)
            
        Returns:
            CommunicationTemplate instance or None
        """
        # If template is already a CommunicationTemplate instance, return it
        if isinstance(template, CommunicationTemplate):
            return template
        
        # If template is an int, treat it as template_id
        if isinstance(template, int):
            template_id = template
        
        # If template is a str, treat it as template_code
        if isinstance(template, str):
            template_code = template
        
        # Get by ID
        if template_id:
            try:
                template_obj = CommunicationTemplate.objects.get(
                    id=template_id,
                    is_active=True
                )
                return template_obj
            except CommunicationTemplate.DoesNotExist:
                logger.warning(f"Template with ID {template_id} not found or inactive")
                return None
        
        # Get by code (case-insensitive lookup)
        if template_code:
            try:
                # First try exact match
                filters = {'code__iexact': template_code, 'is_active': True}
                if communication_type:
                    filters['communication_type'] = communication_type
                template_obj = CommunicationTemplate.objects.get(**filters)
                return template_obj
            except CommunicationTemplate.DoesNotExist:
                # Try case-insensitive match without communication_type filter
                try:
                    filters = {'code__iexact': template_code, 'is_active': True}
                    template_obj = CommunicationTemplate.objects.get(**filters)
                    if communication_type and template_obj.communication_type != communication_type:
                        logger.warning(
                            f"Template with code '{template_code}' found but has wrong communication_type. "
                            f"Expected: {communication_type}, Got: {template_obj.communication_type}"
                        )
                        return None
                    return template_obj
                except CommunicationTemplate.DoesNotExist:
                    logger.warning(
                        f"Template with code '{template_code}' not found or inactive. "
                        f"Available templates: {list(CommunicationTemplate.objects.filter(is_active=True).values_list('code', flat=True))}"
                    )
                    return None
        
        return None

    @staticmethod
    def render_template(
        template: CommunicationTemplate,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """
        Render a communication template with variable substitution.
        
        Supports {{ variable_name }} syntax for variable substitution.
        
        Args:
            template: CommunicationTemplate instance
            context: Dictionary of variables to substitute in the template
            
        Returns:
            Dictionary with rendered 'subject', 'message', 'html_message', etc.
        """
        context = context or {}
        
        def render_text(text: str) -> str:
            """Render template text with variable substitution."""
            if not text:
                return ""
            
            # Replace {{ variable_name }} with context values
            def replace_var(match):
                var_name = match.group(1).strip()
                return str(context.get(var_name, match.group(0)))
            
            # Pattern to match {{ variable_name }} or {{variable_name}}
            pattern = r'\{\{\s*(\w+)\s*\}\}'
            return re.sub(pattern, replace_var, text)
        
        result = {}
        
        # Render subject
        if template.subject:
            result['subject'] = render_text(template.subject)
        
        # Render body_text
        if template.body_text:
            result['message'] = render_text(template.body_text)
        
        # Render body_html
        if template.body_html:
            result['html_message'] = render_text(template.body_html)
        
        # Render SMS body
        if template.communication_type == CommunicationTemplate.CommunicationType.SMS:
            if template.sms_body:
                result['message'] = render_text(template.sms_body)
            elif template.body_text:
                result['message'] = render_text(template.body_text)
        
        # For push notifications, subject is title
        if template.communication_type == CommunicationTemplate.CommunicationType.PUSH_NOTIFICATION:
            if template.subject:
                result['title'] = render_text(template.subject)
            if template.body_text:
                result['message'] = render_text(template.body_text)
        
        return result

    @staticmethod
    def send_email(
        recipient,
        template: Optional[Union[CommunicationTemplate, int, str]] = None,
        template_context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by=None,
        cc_emails: Optional[List[str]] = None,
    ) -> Optional[CommunicationLog]:
        """
        Send an email and log it to CommunicationLog.
        
        Args:
            recipient: User instance or email address string
            template: Optional CommunicationTemplate instance, ID (int), or code (str)
            template_context: Optional context dict for template variable substitution
            metadata: Optional metadata dict to store
            created_by: Optional User instance who triggered this communication
            cc_emails: Optional list of CC addresses (e.g. office inbox)
            
        Returns:
            CommunicationLog instance if logging succeeded, None otherwise
        """
        from iic_booking.users.models import User
        
        # Get User instance if recipient is an email string
        if isinstance(recipient, str):
            try:
                user = User.objects.get(email=recipient)
            except User.DoesNotExist:
                logger.error(f"User not found for email: {recipient}")
                raise ValueError(f"User not found for email: {recipient}")
        else:
            user = recipient
        
        # Get template if provided
        if not template:
            raise ValueError(
                "Template is required. Provide template (CommunicationTemplate instance, ID, or code)"
            )
        
        template_obj = CommunicationService.get_template(
            template=template,
            communication_type=CommunicationTemplate.CommunicationType.EMAIL,
        )
        
        # If template is provided, render it and use its content
        if template_obj:
            rendered = CommunicationService.render_template(
                template_obj,
                context=template_context or {}
            )
            subject = rendered.get('subject', '')
            message = rendered.get('message', '')
            html_message = rendered.get('html_message', '')
            
            if not subject or not message:
                raise ValueError(
                    f"Template '{template_obj.code}' is missing required content. "
                    f"Subject: {bool(subject)}, Message: {bool(message)}"
                )
        else:
            template_identifier = (
                f"ID {template}" if isinstance(template, int)
                else f"code '{template}'" if isinstance(template, str)
                else "provided template"
            )
            available_templates = CommunicationTemplate.objects.filter(
                is_active=True,
                communication_type=CommunicationTemplate.CommunicationType.EMAIL
            ).values_list('code', flat=True)
            raise ValueError(
                f"Template with {template_identifier} not found or inactive for communication type: email. "
                f"Available email templates: {list(available_templates)}"
            )
        
        meta = dict(metadata or {})
        if cc_emails:
            meta["cc_emails"] = cc_emails
        meta.setdefault("email_backend", getattr(settings, "EMAIL_BACKEND", ""))
        meta.setdefault(
            "email_provider",
            "aws_ses" if getattr(settings, "USE_AWS_SES_API", False) else "smtp",
        )

        # Create communication log entry
        communication_log = CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.EMAIL,
            recipient=user,
            recipient_email=getattr(user, "email", "") or "",
            template=template_obj,
            subject=subject,
            message=message,
            status=CommunicationLog.CommunicationStatus.PENDING,
            metadata=meta,
            created_by=created_by or get_current_user(),
        )
        
        # Send email
        try:
            if cc_emails:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[user.email],
                    cc=[e for e in cc_emails if e and str(e).strip()],
                )
                if html_message:
                    msg.attach_alternative(html_message, "text/html")
                msg.send(fail_silently=False)
            else:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    html_message=html_message,
                    fail_silently=False,
                )
            
            # Update log status to sent
            # Note: Provider message ID will be updated when webhook events are received
            communication_log.status = CommunicationLog.CommunicationStatus.SENT
            communication_log.sent_at = timezone.now()
            communication_log.save(update_fields=['status', 'sent_at'])
            
            logger.info(f"Email sent and logged to {user.email}: {subject}")
            return communication_log
            
        except Exception as e:
            # Update log status to failed
            error_message = str(e)
            communication_log.status = CommunicationLog.CommunicationStatus.FAILED
            communication_log.error_message = error_message
            communication_log.save(update_fields=['status', 'error_message'])

            logger.error(
                "Failed to send email to %s: %s",
                user.email,
                error_message,
                exc_info=True,
            )
            return communication_log

    @staticmethod
    def send_email_with_attachments(
        recipient,
        template: Optional[Union[CommunicationTemplate, int, str]] = None,
        template_context: Optional[Dict[str, Any]] = None,
        attachment_paths: Optional[List[tuple]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by=None,
    ) -> Optional[CommunicationLog]:
        """
        Send an email with file attachments and log it.
        attachment_paths: list of (file_path, display_filename) or (file_path,) for each attachment.
        """
        from iic_booking.users.models import User
        attachment_paths = attachment_paths or []
        if isinstance(recipient, str):
            try:
                user = User.objects.get(email=recipient)
            except User.DoesNotExist:
                logger.error(f"User not found for email: {recipient}")
                raise ValueError(f"User not found for email: {recipient}")
        else:
            user = recipient
        if not template:
            raise ValueError("Template is required.")
        template_obj = CommunicationService.get_template(
            template=template,
            communication_type=CommunicationTemplate.CommunicationType.EMAIL,
        )
        if not template_obj:
            raise ValueError(f"Template not found: {template}")
        rendered = CommunicationService.render_template(
            template_obj,
            context=template_context or {}
        )
        subject = rendered.get('subject', '')
        message = rendered.get('message', '')
        html_message = rendered.get('html_message', '')
        if not subject or not message:
            raise ValueError(f"Template '{template_obj.code}' missing subject or message.")
        meta = dict(metadata or {})
        meta.setdefault("email_backend", getattr(settings, "EMAIL_BACKEND", ""))
        meta.setdefault(
            "email_provider",
            "aws_ses" if getattr(settings, "USE_AWS_SES_API", False) else "smtp",
        )
        communication_log = CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.EMAIL,
            recipient=user,
            recipient_email=getattr(user, "email", "") or "",
            template=template_obj,
            subject=subject,
            message=message,
            status=CommunicationLog.CommunicationStatus.PENDING,
            metadata=meta,
            created_by=created_by or get_current_user(),
        )
        try:
            email = EmailMessage(
                subject=subject,
                body=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.email],
            )
            if html_message:
                email.content_subtype = 'html'
                email.body = html_message
            for item in attachment_paths:
                path = item[0] if isinstance(item, (list, tuple)) else item
                name = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None
                if not path or not os.path.exists(path):
                    continue
                with open(path, 'rb') as f:
                    content = f.read()
                filename = name or os.path.basename(path)
                email.attach(filename, content)
            email.send()
            communication_log.status = CommunicationLog.CommunicationStatus.SENT
            communication_log.sent_at = timezone.now()
            communication_log.save(update_fields=['status', 'sent_at'])
            logger.info(f"Email with {len(attachment_paths)} attachment(s) sent to {user.email}: {subject}")
            return communication_log
        except Exception as e:
            communication_log.status = CommunicationLog.CommunicationStatus.FAILED
            communication_log.error_message = str(e)
            communication_log.save(update_fields=['status', 'error_message'])
            logger.error(f"Failed to send email with attachments to {user.email}: {e}", exc_info=True)
            return communication_log

    @staticmethod
    def send_sms(
        recipient,
        message: Optional[str] = None,
        template: Optional[Union[CommunicationTemplate, int, str]] = None,
        template_context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by=None,
    ) -> Optional[CommunicationLog]:
        """
        Send an SMS and log it to CommunicationLog.
        
        Args:
            recipient: User instance
            message: SMS message text (required if template not provided)
            template: Optional CommunicationTemplate instance, ID (int), or code (str)
            template_context: Optional context dict for template variable substitution
            metadata: Optional metadata dict to store
            created_by: Optional User instance who triggered this communication
            
        Returns:
            CommunicationLog instance if logging succeeded, None otherwise
        """
        from iic_booking.users.models import User
        
        if not isinstance(recipient, User):
            raise ValueError("recipient must be a User instance for SMS")
        
        # Get template if provided
        template_obj = CommunicationService.get_template(
            template=template,  
            communication_type=CommunicationTemplate.CommunicationType.SMS,
        )
        
        # If template is provided, render it and use its content
        if template_obj:
            rendered = CommunicationService.render_template(
                template_obj,
                context=template_context or {}
            )
            message = message or rendered.get('message', '')
        elif not message:
            raise ValueError(
                "Either template (template) or message must be provided"
            )
        
        # Create communication log entry
        communication_log = CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.SMS,
            recipient=recipient,
            recipient_email=getattr(recipient, "email", "") or "",
            template=template_obj,
            message=message,
            status=CommunicationLog.CommunicationStatus.PENDING,
            metadata=metadata or {},
            created_by=created_by or get_current_user(),
        )
        
        # TODO: Implement actual SMS sending logic (e.g., Twilio, AWS SNS)
        # For now, just log it
        try:
            # Placeholder for SMS sending
            # sms_provider.send(recipient.phone_number, message)
            
            # Update log status to sent
            communication_log.status = CommunicationLog.CommunicationStatus.SENT
            communication_log.sent_at = timezone.now()
            communication_log.save(update_fields=['status', 'sent_at'])
            
            logger.info(f"SMS logged for {recipient.email}: {message[:50]}...")
            return communication_log
            
        except Exception as e:
            error_message = str(e)
            communication_log.status = CommunicationLog.CommunicationStatus.FAILED
            communication_log.error_message = error_message
            communication_log.save(update_fields=['status', 'error_message'])
            
            logger.error(f"Failed to send SMS to {recipient.email}: {error_message}")
            raise

    @staticmethod
    def send_push_notification(
        recipient,
        title: Optional[str] = None,
        message: Optional[str] = None,
        template: Optional[Union[CommunicationTemplate, int, str]] = None,
        template_context: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by=None,
    ) -> Optional[CommunicationLog]:
        """
        Send a push notification and log it to CommunicationLog.
        
        Args:
            recipient: User instance
            title: Push notification title (required if template not provided)
            message: Push notification message (required if template not provided)
            template: Optional CommunicationTemplate instance, ID (int), or code (str)
            template_context: Optional context dict for template variable substitution
            metadata: Optional metadata dict to store
            created_by: Optional User instance who triggered this communication
            
        Returns:
            CommunicationLog instance if logging succeeded, None otherwise
        """
        from iic_booking.users.models import User
        
        if not isinstance(recipient, User):
            raise ValueError("recipient must be a User instance for push notifications")
        
        # Get template if provided
        template_obj = CommunicationService.get_template(
            template=template,
            communication_type=CommunicationTemplate.CommunicationType.PUSH_NOTIFICATION,
        )
        
        # If template is provided, render it and use its content
        if template_obj:
            rendered = CommunicationService.render_template(
                template_obj,
                context=template_context or {}
            )
            title = title or rendered.get('title', '')
            message = message or rendered.get('message', '')
        elif not title or not message:
            raise ValueError(
                "Either template (template) or both title and message must be provided"
            )
        
        # Create communication log entry
        communication_log = CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
            recipient=recipient,
            recipient_email=getattr(recipient, "email", "") or "",
            template=template_obj,
            subject=title,
            message=message,
            status=CommunicationLog.CommunicationStatus.PENDING,
            metadata=metadata or {},
            created_by=created_by or get_current_user(),
        )
        
        # TODO: Implement actual push notification sending logic (e.g., FCM, APNS)
        # For now, just log it
        try:
            # Placeholder for push notification sending
            # push_provider.send(recipient, title, message, push_data)
            
            # Update log status to sent
            communication_log.status = CommunicationLog.CommunicationStatus.SENT
            communication_log.sent_at = timezone.now()
            communication_log.save(update_fields=['status', 'sent_at'])
            
            # Send WebSocket notification for real-time delivery
            try:
                from .websocket_utils import send_notification_from_communication_log
                # Determine notification type from metadata
                notification_type = (metadata or {}).get("notification_type", "info")
                link = (metadata or {}).get("link")
                send_notification_from_communication_log(
                    communication_log,
                    notification_type=notification_type,
                    link=link,
                )
            except Exception as ws_error:
                # Don't fail the push notification if WebSocket fails
                logger.warning(f"Failed to send WebSocket notification: {str(ws_error)}")
            
            logger.info(f"Push notification logged for {recipient.email}: {title}")
            return communication_log
            
        except Exception as e:
            error_message = str(e)
            communication_log.status = CommunicationLog.CommunicationStatus.FAILED
            communication_log.error_message = error_message
            communication_log.save(update_fields=['status', 'error_message'])
            
            logger.error(f"Failed to send push notification to {recipient.email}: {error_message}")
            raise

    @staticmethod
    def update_communication_status(
        communication_log: CommunicationLog,
        status: str,
        provider_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CommunicationLog:
        """
        Update the status of a communication log entry.
        
        Args:
            communication_log: CommunicationLog instance
            status: New status (from CommunicationLog.CommunicationStatus)
            provider_message_id: Optional provider message ID
            metadata: Optional additional metadata to merge
            
        Returns:
            Updated CommunicationLog instance
        """
        update_fields = ['status', 'updated_at']
        
        if status == CommunicationLog.CommunicationStatus.SENT and not communication_log.sent_at:
            communication_log.sent_at = timezone.now()
            update_fields.append('sent_at')
        elif status == CommunicationLog.CommunicationStatus.DELIVERED and not communication_log.delivered_at:
            communication_log.delivered_at = timezone.now()
            update_fields.append('delivered_at')
        elif status == CommunicationLog.CommunicationStatus.READ and not communication_log.read_at:
            communication_log.read_at = timezone.now()
            update_fields.append('read_at')
        
        communication_log.status = status
        
        if provider_message_id:
            communication_log.provider_message_id = provider_message_id
            update_fields.append('provider_message_id')
        
        if metadata:
            if communication_log.metadata:
                communication_log.metadata.update(metadata)
            else:
                communication_log.metadata = metadata
            update_fields.append('metadata')
        
        communication_log.save(update_fields=update_fields)
        return communication_log

    @staticmethod
    def process_webhook_event(
        webhook_event_type: str,
        provider_type: str,
        provider_message_id: Optional[str] = None,
        recipient_email: Optional[str] = None,
        recipient_phone: Optional[str] = None,
        webhook_payload: Optional[Dict[str, Any]] = None,
        event_timestamp: Optional[datetime] = None,
    ) -> Optional[CommunicationWebhookEventLog]:
        """
        Process a webhook event and update related communication log.
        
        This method:
        1. Creates a CommunicationWebhookEventLog entry
        2. Tries to match it with an existing CommunicationLog
        3. Updates the CommunicationLog status based on the webhook event
        
        Args:
            webhook_event_type: Type of webhook event (from CommunicationWebhookEventLog.WebhookEventType)
            provider_type: Provider type (from CommunicationWebhookEventLog.ProviderType)
            provider_message_id: Optional provider message ID
            recipient_email: Optional recipient email
            recipient_phone: Optional recipient phone
            webhook_payload: Optional full webhook payload
            event_timestamp: Optional event timestamp from provider
            
        Returns:
            CommunicationWebhookEventLog instance
        """
        from iic_booking.users.models import User
        
        # Try to find matching user
        recipient_user = None
        if recipient_email:
            try:
                recipient_user = User.objects.get(email=recipient_email)
            except User.DoesNotExist:
                pass
        
        # Try to find matching communication log
        communication_log = None
        if provider_message_id:
            try:
                communication_log = CommunicationLog.objects.filter(
                    provider_message_id=provider_message_id
                ).first()
            except Exception:
                pass
        
        # If no match by message ID, try to match by recipient and recent timestamp
        if not communication_log and recipient_user:
            # Look for recent pending/queued/sent communications for this user
            communication_log = CommunicationLog.objects.filter(
                recipient=recipient_user,
                status__in=[
                    CommunicationLog.CommunicationStatus.PENDING,
                    CommunicationLog.CommunicationStatus.QUEUED,
                    CommunicationLog.CommunicationStatus.SENT,
                ]
            ).order_by('-created_at').first()
        
        # Create webhook event log
        webhook_event = CommunicationWebhookEventLog.objects.create(
            webhook_event_type=webhook_event_type,
            provider_type=provider_type,
            communication_log=communication_log,
            provider_message_id=provider_message_id or '',
            recipient_email=recipient_email or '',
            recipient_phone=recipient_phone or '',
            recipient_user=recipient_user,
            webhook_payload=webhook_payload or {},
            event_timestamp=event_timestamp,
        )
        
        # Update communication log status based on webhook event
        if communication_log:
            status_mapping = {
                CommunicationWebhookEventLog.WebhookEventType.EMAIL_SENT: CommunicationLog.CommunicationStatus.SENT,
                CommunicationWebhookEventLog.WebhookEventType.EMAIL_DELIVERED: CommunicationLog.CommunicationStatus.DELIVERED,
                CommunicationWebhookEventLog.WebhookEventType.EMAIL_OPENED: CommunicationLog.CommunicationStatus.READ,
                CommunicationWebhookEventLog.WebhookEventType.EMAIL_BOUNCED: CommunicationLog.CommunicationStatus.BOUNCED,
                CommunicationWebhookEventLog.WebhookEventType.EMAIL_FAILED: CommunicationLog.CommunicationStatus.FAILED,
                CommunicationWebhookEventLog.WebhookEventType.EMAIL_COMPLAINED: CommunicationLog.CommunicationStatus.REJECTED,
                CommunicationWebhookEventLog.WebhookEventType.PUSH_SENT: CommunicationLog.CommunicationStatus.SENT,
                CommunicationWebhookEventLog.WebhookEventType.PUSH_DELIVERED: CommunicationLog.CommunicationStatus.DELIVERED,
                CommunicationWebhookEventLog.WebhookEventType.PUSH_OPENED: CommunicationLog.CommunicationStatus.READ,
                CommunicationWebhookEventLog.WebhookEventType.PUSH_FAILED: CommunicationLog.CommunicationStatus.FAILED,
                CommunicationWebhookEventLog.WebhookEventType.SMS_SENT: CommunicationLog.CommunicationStatus.SENT,
                CommunicationWebhookEventLog.WebhookEventType.SMS_DELIVERED: CommunicationLog.CommunicationStatus.DELIVERED,
                CommunicationWebhookEventLog.WebhookEventType.SMS_FAILED: CommunicationLog.CommunicationStatus.FAILED,
            }
            
            new_status = status_mapping.get(webhook_event_type)
            if new_status:
                CommunicationService.update_communication_status(
                    communication_log,
                    new_status,
                    provider_message_id=provider_message_id,
                )
            
            # Update provider message ID if not set
            if provider_message_id and not communication_log.provider_message_id:
                communication_log.provider_message_id = provider_message_id
                communication_log.save(update_fields=['provider_message_id'])
        
        # Mark webhook as processed
        webhook_event.processed = True
        webhook_event.processed_at = timezone.now()
        webhook_event.save(update_fields=['processed', 'processed_at'])
        
        return webhook_event

