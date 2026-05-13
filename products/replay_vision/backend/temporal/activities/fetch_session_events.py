import datetime as dt
from typing import Any

from asgiref.sync import sync_to_async
from temporalio import activity
from temporalio.exceptions import ApplicationError

from posthog.models import Team
from posthog.session_recordings.queries.session_replay_events import SessionReplayEvents

from products.replay_vision.backend.temporal.state import (
    StateActivitiesEnum,
    get_data_str_from_redis,
    get_redis_state_client,
    store_data_in_redis,
)
from products.replay_vision.backend.temporal.types import FetchSessionEventsInputs, LensLlmInputs

# Mirrors session_summary's pagination shape so long sessions aren't silently truncated to the
# `LimitContext.QUERY` default of 100.
_EVENTS_PER_PAGE = 3000
_MAX_EVENT_PAGES = 100


@activity.defn
async def fetch_session_events_activity(inputs: FetchSessionEventsInputs) -> None:
    """Fetch analytics events for a session, stash in Redis for downstream activities.

    Idempotent — second call against the same observation finds the key and returns.
    """
    redis_client, redis_key = get_redis_state_client(
        label=StateActivitiesEnum.SESSION_EVENTS,
        state_id=str(inputs.observation_id),
    )
    if redis_key is None:
        raise RuntimeError("Redis state key construction failed for session events")

    existing = await get_data_str_from_redis(redis_client, redis_key)
    if existing is not None:
        return

    payload = await sync_to_async(_fetch_payload)(inputs.team_id, inputs.session_id)
    if payload is None:
        raise ApplicationError(
            f"Session {inputs.session_id} has no events to analyze",
            non_retryable=True,
        )

    await store_data_in_redis(redis_client, redis_key, payload.model_dump_json())


def _row_to_jsonable(row: tuple[Any, ...]) -> list[Any]:
    """Normalize datetimes to ISO strings up-front so the JSON round-trip is stable
    (Pydantic's default datetime serializer would emit `Z` while the producer side stays naive)."""
    return [value.isoformat() if isinstance(value, dt.datetime | dt.date) else value for value in row]


def _fetch_payload(team_id: int, session_id: str) -> LensLlmInputs | None:
    team = Team.objects.get(pk=team_id)
    events_obj = SessionReplayEvents()
    metadata = events_obj.get_metadata(session_id=session_id, team=team)
    if metadata is None:
        raise ApplicationError(f"No replay metadata found for session {session_id}", non_retryable=True)

    columns: list[str] | None = None
    all_rows: list[list[Any]] = []
    for page in range(_MAX_EVENT_PAGES):
        page_columns, page_rows = events_obj.get_events(
            session_id=session_id,
            team=team,
            metadata=metadata,
            limit=_EVENTS_PER_PAGE,
            page=page,
        )
        if page_columns and columns is None:
            columns = list(page_columns)
        if not page_rows:
            break
        all_rows.extend(_row_to_jsonable(row) for row in page_rows)
        if len(page_rows) < _EVENTS_PER_PAGE:
            break

    if columns is None or not all_rows:
        return None

    return LensLlmInputs(
        session_id=session_id,
        team_id=team_id,
        session_start_time=metadata["start_time"].isoformat(),
        session_end_time=metadata["end_time"].isoformat(),
        duration_seconds=float(metadata["duration"]),
        columns=columns,
        events=all_rows,
    )
