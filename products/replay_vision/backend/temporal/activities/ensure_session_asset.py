import hashlib
import datetime as dt

from django.db import connection, transaction
from django.utils.timezone import now

from asgiref.sync import sync_to_async
from temporalio import activity

from posthog.models.exported_asset import ExportedAsset

from products.replay_vision.backend.temporal.types import EnsureSessionAssetInputs, EnsureSessionAssetOutput

# Render params match the session-summary path so the rasterize fingerprint cache hits across products.
_EXPORT_FORMAT = "video/mp4"
_PLAYBACK_SPEED = 8
_RECORDING_FPS = 3
_SHOW_METADATA_FOOTER = True
_ASSET_EXPIRES_AFTER_DAYS = 90

# Namespace for `pg_advisory_xact_lock(int4, int4)`. Keeps replay-vision's locks in their own
# 32-bit space so unrelated callers can't collide on the same hash.
_ADVISORY_LOCK_NAMESPACE = 0x52567869  # "RVxi" — replay-vision exported-asset


def _advisory_lock_key(team_id: int, session_id: str) -> int:
    digest = hashlib.blake2b(f"{team_id}:{session_id}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


def _get_or_create(team_id: int, session_id: str) -> int:
    """Acquire a transaction-scoped advisory lock for `(team_id, session_id)` so concurrent
    observations serialize on the get-or-create. `SELECT FOR UPDATE` alone wouldn't help here —
    it can't lock a row that doesn't exist yet, so two concurrent callers would both miss and INSERT.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_ADVISORY_LOCK_NAMESPACE, _advisory_lock_key(team_id, session_id)],
        )
        existing = (
            ExportedAsset.objects.filter(
                team_id=team_id,
                export_format=_EXPORT_FORMAT,
                export_context__session_recording_id=session_id,
                is_system=True,
            )
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing.id

        created_at = now()
        asset = ExportedAsset.objects.create(
            team_id=team_id,
            export_format=_EXPORT_FORMAT,
            export_context={
                "session_recording_id": session_id,
                "playback_speed": _PLAYBACK_SPEED,
                "recording_fps": _RECORDING_FPS,
                "show_metadata_footer": _SHOW_METADATA_FOOTER,
            },
            created_at=created_at,
            expires_after=created_at + dt.timedelta(days=_ASSET_EXPIRES_AFTER_DAYS),
            is_system=True,
        )
        return asset.id


@activity.defn
async def ensure_session_asset_activity(inputs: EnsureSessionAssetInputs) -> EnsureSessionAssetOutput:
    """Get-or-create the ExportedAsset that `RasterizeRecordingWorkflow` needs as input.

    Returns any existing system-owned asset for `(team, session)` as-is so its stored
    `render_fingerprint` + `content_location` can serve a cache hit on subsequent runs.
    Param changes only apply to assets created after the deploy. We deliberately don't
    refresh `export_context` on reuse — the asset is shared with `session_summary` via
    `is_system=True`, and rewriting params here would invalidate the other product's
    `render_fingerprint` cache.
    """
    asset_id = await sync_to_async(_get_or_create)(inputs.team_id, inputs.session_id)
    return EnsureSessionAssetOutput(asset_id=asset_id)
