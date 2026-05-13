import re
import json
from functools import cached_property
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.files import File
from django.db import models
from django.db.models import QuerySet
from django.utils import timezone

import posthoganalytics
import posthoganalytics.ai.openai
from drf_spectacular.utils import OpenApiResponse, extend_schema
from elevenlabs import ElevenLabs
from posthoganalytics.ai.openai import OpenAI
from rest_framework import filters, response, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.request import Request

from posthog.schema import ProductKey

from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.models.sharing_configuration import SharingConfiguration
from posthog.utils import absolute_uri

from .models import EmailWithDisplayNameValidator, IntervieweeContext, UserInterview, UserInterviewTopic

elevenlabs_client = ElevenLabs()


class UserInterviewSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    audio = serializers.FileField(write_only=True)

    class Meta:
        model = UserInterview
        fields = (
            "id",
            "created_by",
            "created_at",
            "interviewee_emails",
            "transcript",
            "summary",
            "audio",
        )
        read_only_fields = ("id", "created_by", "created_at", "transcript")

    def create(self, validated_data: dict) -> UserInterview:
        request = self.context["request"]
        validated_data["created_by"] = request.user
        validated_data["team_id"] = self.context["team_id"]
        audio = validated_data.pop("audio")
        validated_data["transcript"] = self._transcribe_audio(audio, validated_data["interviewee_emails"])
        validated_data["summary"] = self._summarize_transcript(validated_data["transcript"])
        return super().create(validated_data)

    def _transcribe_audio(self, audio: File, interviewee_emails: list[str]) -> str:
        transcript = elevenlabs_client.speech_to_text.convert(
            model_id="scribe_v1",
            file=audio,
            num_speakers=10,  # Maximum number of speakers, not expected one
            diarize=True,
            tag_audio_events=False,
            additional_formats=json.dumps(  # type: ignore
                [
                    {
                        "format": "txt",
                        "include_timestamps": False,
                        "segment_on_silence_longer_than_s": 10,
                    }
                ]
            ),
        )

        transcript_text = transcript.additional_formats[0].content.strip()  # type: ignore

        speaker_mapping = self._attempt_to_map_speaker_names(transcript_text, interviewee_emails)
        if speaker_mapping:
            for speaker_marker, speaker_name in speaker_mapping.items():
                transcript_text = transcript_text.replace(speaker_marker, speaker_name)
            formatted_transcript_text = re.sub(r"\[(.+)\]", "#### \\1", transcript_text)
        else:
            # Always fall back to formatting speaker numbers if we can't map names
            formatted_transcript_text = re.sub(r"\[speaker_(\d+)\]", "#### Speaker \\1", transcript_text)

        return formatted_transcript_text

    def _attempt_to_map_speaker_names(self, transcript: str, interviewee_emails: list[str]) -> dict[str, str] | None:
        participant_emails_joined = "\n".join(f"- {email}" for email in interviewee_emails)
        assignment_response = OpenAI(
            posthog_client=posthoganalytics.default_client, base_url=settings.OPENAI_BASE_URL
        ).responses.create(  # type: ignore
            model="gpt-4.1-mini",
            posthog_trace_id=self._ai_trace_id,
            posthog_distinct_id=self.context["request"].user.distinct_id,
            input=[
                {
                    "role": "system",
                    "content": """
Your task is to map speakers in a transcript to the actual names of the people who spoke. Each speaker is identified by a number.
Use clues such as who the speaker is calling out (e.g. they wouldn't greet themselves) or what they're talking about (e.g. how they use company names).

Your output should be a JSON mapping between "speaker_<n>" and "<speaker name>".

<handling_ambiguity>
- Use just the person's display name if available, otherwise use their email. If two people with the same full name are present, include their email to disambiguate.
- Likely many of the participants have spoken, but not necessarily all of them.
- Keep in mind it's possible there were additional unexpected participants (though not that likely).
- The transcript is not going to be perfect, so some names in the transcript may be slightly mangled compared to display names in participant emails.
  E.g. the transcript may contain that an interviewer greeted "Jon", but if the participant emails only have a "John", it's safe to assume that the interviewer was talking to John.
- If most of the speakers are entirely obvious, but only a small subset isn't, mark the unidentified speakers' names as "Unknown #1 (<candidate_1> or <candidate_2>)" etc. Don't leave any speaker unmarked.
  If however you cannot infer a reliable mapping for most speakers (the transcript has no useful information or is too chaotic), return simply: null.
</handling_ambiguity>

<example>
As an example, for transcript:

<participant_emails>
- Michael F. Doe <michael@x.com>
- Steve Jobs <steve@apple.com>
</participant_emails>
<transcript>
[speaker_0]
Hi Michael! How big is your company?

[speaker_1]
Hey! We're about 200 people.

[speaker_0]
That's great!
</transcript>

Your output should be:

{
"speaker_0": "Steve Jobs",
"speaker_1": "Michael F. Doe"
}
</example>

<output_format>
Output must always be valid JSON - either an object or null.
</output_format>
""".strip(),
                },
                {
                    "role": "user",
                    "content": f"""
Map the speakers in the following transcript:

<participant_emails>
{participant_emails_joined}
</participant_emails>

<transcript>
{transcript}
</transcript>
""".strip(),
                },
            ],
        )
        try:
            return json.loads(assignment_response.output_text)
        except json.JSONDecodeError as e:
            posthoganalytics.capture_exception(e)
            return None

    def _summarize_transcript(self, transcript: str) -> str:
        summary_response = OpenAI(posthog_client=posthoganalytics.default_client).responses.create(  # type: ignore
            model="gpt-4.1-mini",
            posthog_trace_id=self._ai_trace_id,
            posthog_distinct_id=self.context["request"].user.distinct_id,
            input=[
                {
                    "role": "system",
                    "content": """
You are an expert product manager, and your sole task is to summarizes user interviews ran by our team.
""".strip(),
                },
                {
                    "role": "user",
                    "content": f"""
I interviewed a user to gather insights about our product. The goal is to capture the customer's feedback, experiences, and suggestions in a detailed and organized manner.
The notes should be comprehensive but focused, allowing for the detailed documentation of both qualitative insights and actionable items.
Pull out direct quotes and figures whenever relevant.

Because no better transcript is available, you should still do your best to summarize the interview.

<summary_format>
## User background

Capture relevant details about the user, including their role, experience, and how they interact with your product or service.
Note down here any existing solutions or workarounds they use.

## Current product usage

Document how the user is currently using the product, including frequency of use, key features used, and any specific use cases.

## Positive feedback and pain points

Summarize the positive feedback the user provided, as well as any pain points or challenges they are experiencing with the product.

## Impact of the product

Record the impact the product has had on the user's work or life, including any improvements or changes it has enabled.

## Next steps and follow-up

Record the agreed-upon next steps, including any additional actions that need to be taken, follow-up tasks, and who is responsible for them.
</summary_format>

<transcript>
{transcript}
</transcript>
""".strip(),
                },
            ],
        )
        return summary_response.output_text

    @cached_property
    def _ai_trace_id(self) -> str:
        return str(uuid4())


@extend_schema(tags=[ProductKey.USER_INTERVIEWS])
class UserInterviewViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    scope_object = "user_interview_DO_NOT_USE"
    queryset = UserInterview.objects.order_by("-created_at").select_related("created_by").all()
    serializer_class = UserInterviewSerializer
    parser_classes = [MultiPartParser, JSONParser]


class UserInterviewTopicSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    interviewee_cohort = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Optional cohort ID identifying who to target. Not enforced as a foreign key.",
    )
    interviewee_emails = serializers.ListField(
        child=serializers.CharField(max_length=254, validators=[EmailWithDisplayNameValidator()]),
        required=False,
        help_text="Email addresses of people to interview. May be combined with interviewee_cohort and interviewee_distinct_ids.",
    )
    interviewee_distinct_ids = serializers.ListField(
        child=serializers.CharField(max_length=400),
        required=False,
        help_text="PostHog distinct IDs of people to interview. May be combined with interviewee_cohort and interviewee_emails.",
    )
    topic = serializers.CharField(
        help_text="The product, feature, or idea you want to ask interviewees about.",
    )
    agent_context = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional additional system prompt for the voice agent — extra background, tone, or constraints.",
    )
    questions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Ordered list of questions the voice agent should work through during the interview.",
    )

    class Meta:
        model = UserInterviewTopic
        fields = (
            "id",
            "created_by",
            "created_at",
            "interviewee_cohort",
            "interviewee_emails",
            "interviewee_distinct_ids",
            "topic",
            "agent_context",
            "questions",
        )
        read_only_fields = ("id", "created_by", "created_at")

    def validate(self, attrs: dict) -> dict:
        cohort = (
            attrs.get("interviewee_cohort")
            if "interviewee_cohort" in attrs
            else getattr(self.instance, "interviewee_cohort", None)
        )
        emails = (
            attrs.get("interviewee_emails")
            if "interviewee_emails" in attrs
            else getattr(self.instance, "interviewee_emails", [])
        )
        distinct_ids = (
            attrs.get("interviewee_distinct_ids")
            if "interviewee_distinct_ids" in attrs
            else getattr(self.instance, "interviewee_distinct_ids", [])
        )
        if cohort is None and not emails and not distinct_ids:
            raise serializers.ValidationError(
                "At least one of interviewee_cohort, interviewee_emails, or interviewee_distinct_ids must be provided."
            )
        return attrs

    def create(self, validated_data: dict) -> UserInterviewTopic:
        request = self.context["request"]
        team = self.context["get_team"]()
        return UserInterviewTopic.objects.create(
            team=team,
            created_by=request.user,
            **validated_data,
        )


def _parse_identifier(identifier: str) -> tuple[str, str | None]:
    """Split an interviewee identifier into a display name and (optional) email.

    Accepts the same display-name format the topic validator accepts —
    ``"Display Name <email@host>"`` — falling back to a best-effort
    title-cased local-part for raw emails and the identifier as-is for
    distinct IDs.
    """
    display_match = re.match(EmailWithDisplayNameValidator.display_name_regex, identifier)
    if display_match:
        return display_match.group(1).strip(), display_match.group(2).strip()
    if "@" in identifier:
        local_part = identifier.split("@", 1)[0]
        return local_part.replace(".", " ").replace("_", " ").strip().title() or identifier, identifier
    return identifier, None


def _merge_agent_context(topic_context: str, personal_context: str) -> str:
    parts = [p.strip() for p in (topic_context, personal_context) if p and p.strip()]
    return "\n\n".join(parts)


class InterviewLinkSerializer(serializers.Serializer):
    interviewee_identifier = serializers.CharField(
        max_length=400,
        help_text="The original identifier (email or distinct ID) from the topic targeting.",
    )
    user_name = serializers.CharField(
        help_text="Best-effort display name derived from the identifier, used to greet the interviewee.",
    )
    interview_url = serializers.URLField(
        help_text="Public, unauthenticated URL the interviewee opens to start the call. Backed by a SharingConfiguration access token.",
    )
    agent_context = serializers.CharField(
        help_text="The merged topic + per-interviewee context the voice agent will see during the call.",
    )


@extend_schema(tags=[ProductKey.USER_INTERVIEWS])
class UserInterviewTopicViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    """Planned user interview topics: who we want to target (cohort) and what we want to ask about."""

    scope_object = "user_interview_DO_NOT_USE"
    queryset = UserInterviewTopic.objects.select_related("created_by").all()
    serializer_class = UserInterviewTopicSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["topic"]

    @extend_schema(
        request=None,
        responses={200: OpenApiResponse(response=InterviewLinkSerializer(many=True))},
        description=(
            "Generate one public interview link per targeted interviewee. "
            "Materializes an IntervieweeContext row for every identifier on the topic "
            "(without overwriting existing per-person context), and an enabled "
            "SharingConfiguration with a unique access token. The URL resolves to the "
            "public interview viewer with no PostHog auth required."
        ),
    )
    @action(detail=True, methods=["post"], url_path="generate_links")
    def generate_links(self, request: Request, *args: Any, **kwargs: Any) -> response.Response:
        topic = self.get_object()
        team = self.team

        identifiers: list[str] = []
        seen: set[str] = set()
        for raw in [*(topic.interviewee_emails or []), *(topic.interviewee_distinct_ids or [])]:
            if raw and raw not in seen:
                identifiers.append(raw)
                seen.add(raw)

        if not identifiers:
            return response.Response(
                {
                    "error": (
                        "Topic targets a cohort but has no resolved emails or distinct IDs. "
                        "Add interviewee_emails or interviewee_distinct_ids before generating links."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_contexts = {
            ic.interviewee_identifier: ic
            for ic in IntervieweeContext.objects.filter(topic=topic, interviewee_identifier__in=identifiers)
        }

        results: list[dict[str, Any]] = []
        for identifier in identifiers:
            ic = existing_contexts.get(identifier)
            if ic is None:
                ic = IntervieweeContext.objects.create(
                    team=team,
                    topic=topic,
                    interviewee_identifier=identifier,
                    agent_context="",
                    created_by=request.user,
                )

            sharing_config = (
                SharingConfiguration.objects.filter(team=team, interviewee_context=ic, enabled=True)
                .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=timezone.now()))
                .order_by("-created_at")
                .first()
            )
            if sharing_config is None:
                sharing_config = SharingConfiguration.objects.create(
                    team=team,
                    interviewee_context=ic,
                    enabled=True,
                )

            user_name, _email = _parse_identifier(identifier)
            interview_url = absolute_uri(f"/interview/{sharing_config.access_token}")
            results.append(
                {
                    "interviewee_identifier": identifier,
                    "user_name": user_name,
                    "interview_url": interview_url,
                    "agent_context": _merge_agent_context(topic.agent_context or "", ic.agent_context or ""),
                }
            )

        return response.Response(InterviewLinkSerializer(results, many=True).data)


class IntervieweeContextSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    interviewee_identifier = serializers.CharField(
        max_length=400,
        help_text="Identifier for the interviewee — typically an email address or PostHog distinct ID. Must match a value in the parent topic's interviewee_emails or interviewee_distinct_ids.",
    )
    agent_context = serializers.CharField(
        max_length=10000,
        help_text="Extra context the voice agent should know about this specific interviewee — e.g. 'uses the replay product but has never used summarization'.",
    )

    class Meta:
        model = IntervieweeContext
        fields = (
            "id",
            "created_by",
            "created_at",
            "interviewee_identifier",
            "agent_context",
        )
        read_only_fields = ("id", "created_by", "created_at")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        topic_id = self.context["topic_id"]
        team = self.context["get_team"]()

        if not UserInterviewTopic.objects.filter(id=topic_id, team_id=team.id).exists():
            raise serializers.ValidationError({"topic": "Topic not found in this project."})

        if self.instance is None:
            interviewee_identifier = attrs.get("interviewee_identifier")
            if IntervieweeContext.objects.filter(
                topic_id=topic_id, interviewee_identifier=interviewee_identifier
            ).exists():
                raise serializers.ValidationError(
                    {
                        "interviewee_identifier": "A context row for this interviewee already exists on this topic. Update the existing row instead of creating a new one."
                    }
                )
        return attrs

    def create(self, validated_data: dict[str, Any]) -> IntervieweeContext:
        request = self.context["request"]
        team = self.context["get_team"]()
        topic_id = self.context["topic_id"]
        return IntervieweeContext.objects.create(
            team=team,
            topic_id=topic_id,
            created_by=request.user,
            **validated_data,
        )


@extend_schema(tags=[ProductKey.USER_INTERVIEWS])
class IntervieweeContextViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    """Per-interviewee extra context for a user interview topic. At most one row per (topic, interviewee_identifier)."""

    scope_object = "user_interview_DO_NOT_USE"
    serializer_class = IntervieweeContextSerializer
    queryset = IntervieweeContext.objects.select_related("created_by").all()

    def safely_get_queryset(self, queryset: QuerySet) -> QuerySet:
        return queryset.filter(
            topic_id=self.parents_query_dict["topic_id"],
            team_id=self.parents_query_dict["team_id"],
        )

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "topic_id": self.parents_query_dict["topic_id"]}
