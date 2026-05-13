"""Public, unauthenticated webhooks for the user_interviews product.

Currently exposes the Vapi end-of-call-report receiver. Vapi POSTs once a
call wraps up; we resolve the originating SharingConfiguration via the
access token round-tripped in the call ``metadata``, then persist a
:class:`UserInterview` row attributed to the topic creator.
"""

import hmac
import hashlib
from typing import Any

from django.conf import settings
from django.db import transaction

import structlog
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from posthog.models.sharing_configuration import SharingConfiguration

from .models import UserInterview

logger = structlog.get_logger(__name__)


def _verify_signature(secret: str, raw_body: bytes, provided_signature: str | None) -> bool:
    if not secret or not provided_signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_signature, expected)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def vapi_webhook(request: Request) -> Response:
    """Receive a Vapi ``end-of-call-report`` and persist it as a UserInterview.

    Signature verification is enforced when ``VAPI_WEBHOOK_SECRET`` is set.
    The Vapi call metadata must include ``sharing_access_token`` (the token
    embedded in the public interview URL); this token resolves both the
    target team and the originating topic + interviewee.
    """
    if settings.VAPI_WEBHOOK_SECRET:
        provided = request.headers.get("x-vapi-signature") or request.headers.get("X-Vapi-Signature")
        if not _verify_signature(settings.VAPI_WEBHOOK_SECRET, request.body, provided):
            return Response({"error": "invalid signature"}, status=status.HTTP_401_UNAUTHORIZED)

    payload = request.data if isinstance(request.data, dict) else {}
    message: dict[str, Any] = payload.get("message", {})
    if message.get("type") != "end-of-call-report":
        # Other event types (status updates, transcripts mid-call) are ignored.
        return Response({"status": "ignored"})

    call: dict[str, Any] = message.get("call", {}) or {}
    metadata: dict[str, Any] = call.get("metadata", {}) or {}
    access_token = metadata.get("sharing_access_token") or metadata.get("access_token")

    if not access_token:
        return Response(
            {"error": "missing sharing_access_token in call.metadata"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        sharing_config = SharingConfiguration.objects.select_related(
            "team",
            "interviewee_context",
            "interviewee_context__topic",
            "interviewee_context__topic__created_by",
        ).get(access_token=access_token, enabled=True)
    except SharingConfiguration.DoesNotExist:
        return Response({"error": "unknown access_token"}, status=status.HTTP_404_NOT_FOUND)

    interviewee_context = sharing_config.interviewee_context
    if interviewee_context is None:
        return Response(
            {"error": "access_token does not belong to a user interview share"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    topic = interviewee_context.topic
    recording_url = (message.get("recording") or {}).get("url", "") or message.get("recordingUrl", "") or ""

    with transaction.atomic():
        interview = UserInterview.objects.create(
            team=sharing_config.team,
            topic=topic,
            interviewee_identifier=interviewee_context.interviewee_identifier,
            interviewee_emails=[interviewee_context.interviewee_identifier]
            if "@" in interviewee_context.interviewee_identifier
            else [],
            transcript=message.get("transcript", "") or "",
            summary=message.get("summary", "") or "",
            recording_url=recording_url,
            call_metadata=call,
            created_by=topic.created_by,
        )

    logger.info(
        "user_interviews_vapi_webhook_stored",
        team_id=sharing_config.team_id,
        topic_id=str(topic.id),
        interview_id=str(interview.id),
        interviewee=interviewee_context.interviewee_identifier,
    )
    return Response({"status": "created", "interview_id": str(interview.id)}, status=status.HTTP_201_CREATED)
