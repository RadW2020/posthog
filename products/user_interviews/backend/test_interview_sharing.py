from posthog.test.base import APIBaseTest

from django.test import override_settings

from rest_framework import status

from posthog.models.sharing_configuration import SharingConfiguration

from products.user_interviews.backend.models import IntervieweeContext, UserInterview, UserInterviewTopic


class TestGenerateInterviewLinks(APIBaseTest):
    def _create_topic(self, **overrides) -> UserInterviewTopic:
        defaults: dict = {
            "team": self.team,
            "created_by": self.user,
            "interviewee_emails": ["Alex <alex@example.com>", "jordan@example.com"],
            "interviewee_distinct_ids": ["distinct-abc"],
            "topic": "Session replay adoption",
            "agent_context": "Researching adoption of session replay",
            "questions": ["What blocks adoption?"],
        }
        defaults.update(overrides)
        return UserInterviewTopic.objects.create(**defaults)

    def _generate_links_url(self, topic_id: str) -> str:
        return f"/api/environments/{self.team.id}/user_interview_topics/{topic_id}/generate_links/"

    def test_generate_links_materializes_contexts_and_sharing_configs(self):
        topic = self._create_topic()

        response = self.client.post(self._generate_links_url(str(topic.id)))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        identifiers = sorted(link["interviewee_identifier"] for link in body)
        self.assertEqual(
            identifiers,
            sorted(["Alex <alex@example.com>", "jordan@example.com", "distinct-abc"]),
        )

        for link in body:
            self.assertTrue(link["interview_url"].endswith(link["interview_url"].rsplit("/", 1)[-1]))
            self.assertIn("/interview/", link["interview_url"])

        self.assertEqual(IntervieweeContext.objects.filter(topic=topic).count(), 3)
        self.assertEqual(
            SharingConfiguration.objects.filter(team=self.team, interviewee_context__topic=topic, enabled=True).count(),
            3,
        )

    def test_generate_links_is_idempotent(self):
        topic = self._create_topic(interviewee_emails=["alex@example.com"], interviewee_distinct_ids=[])
        first = self.client.post(self._generate_links_url(str(topic.id))).json()
        second = self.client.post(self._generate_links_url(str(topic.id))).json()
        self.assertEqual(first[0]["interview_url"], second[0]["interview_url"])
        self.assertEqual(IntervieweeContext.objects.filter(topic=topic).count(), 1)
        self.assertEqual(SharingConfiguration.objects.filter(interviewee_context__topic=topic).count(), 1)

    def test_generate_links_preserves_existing_personal_context(self):
        topic = self._create_topic(interviewee_emails=["alex@example.com"], interviewee_distinct_ids=[])
        IntervieweeContext.objects.create(
            team=self.team,
            topic=topic,
            interviewee_identifier="alex@example.com",
            agent_context="heavy user, churned last quarter",
            created_by=self.user,
        )
        response = self.client.post(self._generate_links_url(str(topic.id)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        link = response.json()[0]
        self.assertIn("heavy user, churned last quarter", link["agent_context"])
        self.assertIn("Researching adoption of session replay", link["agent_context"])

    def test_generate_links_rejects_topic_with_only_cohort(self):
        topic = self._create_topic(
            interviewee_emails=[],
            interviewee_distinct_ids=[],
            interviewee_cohort=123,
        )
        response = self.client.post(self._generate_links_url(str(topic.id)))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TestInterviewPublicViewer(APIBaseTest):
    def _create_share(self) -> SharingConfiguration:
        topic = UserInterviewTopic.objects.create(
            team=self.team,
            created_by=self.user,
            interviewee_emails=["alex@example.com"],
            topic="Session replay adoption",
            agent_context="adoption research",
            questions=["q1"],
        )
        ic = IntervieweeContext.objects.create(
            team=self.team,
            topic=topic,
            interviewee_identifier="alex@example.com",
            agent_context="heavy user",
            created_by=self.user,
        )
        return SharingConfiguration.objects.create(team=self.team, interviewee_context=ic, enabled=True)

    @override_settings(VAPI_PUBLIC_KEY="pk_test", VAPI_ASSISTANT_ID="asst_test")
    def test_public_viewer_renders_interview_payload(self):
        share = self._create_share()
        self.client.logout()
        response = self.client.get(f"/interview/{share.access_token}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.content.decode()
        # The exporter payload is JSON-encoded inside a JSON script tag, so the keys appear
        # backslash-escaped (e.g. `\\"type\\": \\"interview\\"`). Match on the unique values.
        self.assertIn("interview", body)
        self.assertIn(share.access_token, body)
        self.assertIn("Session replay adoption", body)
        self.assertIn("pk_test", body)
        self.assertIn("asst_test", body)

    def test_public_viewer_rejects_disabled_share(self):
        share = self._create_share()
        share.enabled = False
        share.save()
        self.client.logout()
        response = self.client.get(f"/interview/{share.access_token}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TestVapiWebhook(APIBaseTest):
    def _create_share(self) -> SharingConfiguration:
        topic = UserInterviewTopic.objects.create(
            team=self.team,
            created_by=self.user,
            interviewee_emails=["alex@example.com"],
            topic="Replay adoption",
            agent_context="ctx",
            questions=[],
        )
        ic = IntervieweeContext.objects.create(
            team=self.team,
            topic=topic,
            interviewee_identifier="alex@example.com",
            agent_context="",
            created_by=self.user,
        )
        return SharingConfiguration.objects.create(team=self.team, interviewee_context=ic, enabled=True)

    def _end_of_call_payload(self, access_token: str) -> dict:
        return {
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "call_abc",
                    "metadata": {"sharing_access_token": access_token},
                    "duration": 120,
                },
                "transcript": "Hi! ...",
                "summary": "User talked about replay.",
                "recording": {"url": "https://vapi.example/recording.mp3"},
            }
        }

    @override_settings(VAPI_WEBHOOK_SECRET="")
    def test_webhook_creates_user_interview(self):
        share = self._create_share()
        self.client.logout()
        response = self.client.post(
            "/api/user_interviews/vapi_webhook/",
            data=self._end_of_call_payload(share.access_token),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        interview = UserInterview.objects.get(team=self.team)
        self.assertEqual(interview.topic, share.interviewee_context.topic)
        self.assertEqual(interview.interviewee_identifier, "alex@example.com")
        self.assertEqual(interview.recording_url, "https://vapi.example/recording.mp3")
        self.assertEqual(interview.transcript, "Hi! ...")

    @override_settings(VAPI_WEBHOOK_SECRET="")
    def test_webhook_rejects_unknown_token(self):
        self.client.logout()
        response = self.client.post(
            "/api/user_interviews/vapi_webhook/",
            data=self._end_of_call_payload("does-not-exist"),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(VAPI_WEBHOOK_SECRET="topsecret")
    def test_webhook_requires_valid_signature_when_secret_set(self):
        share = self._create_share()
        self.client.logout()
        response = self.client.post(
            "/api/user_interviews/vapi_webhook/",
            data=self._end_of_call_payload(share.access_token),
            content_type="application/json",
            HTTP_X_VAPI_SIGNATURE="wrong",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(VAPI_WEBHOOK_SECRET="")
    def test_webhook_ignores_non_end_of_call_events(self):
        self.client.logout()
        response = self.client.post(
            "/api/user_interviews/vapi_webhook/",
            data={"message": {"type": "status-update"}},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "ignored")


class TestSharingConfigurationCanAccess(APIBaseTest):
    def test_can_access_interviewee_context(self):
        topic = UserInterviewTopic.objects.create(
            team=self.team,
            created_by=self.user,
            interviewee_emails=["alex@example.com"],
            topic="t",
        )
        ic = IntervieweeContext.objects.create(
            team=self.team,
            topic=topic,
            interviewee_identifier="alex@example.com",
            agent_context="",
            created_by=self.user,
        )
        share = SharingConfiguration.objects.create(team=self.team, interviewee_context=ic, enabled=True)
        self.assertTrue(share.can_access_object(ic))
