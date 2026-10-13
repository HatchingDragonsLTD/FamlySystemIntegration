"""Web handler for the queue_timeline action.

Accepts a timeline for UNATTENDED creation: the same payload as
`create_timeline` (plus `auto_round`), validated in full with NO Famly call,
stored as a job, and answered at once with the job id. The worker
(`python main.py queue-worker`, see worker.py) builds it later.

Nothing here calls Famly. Enqueueing is ONE atomic INSERT and the database settles
races between simultaneous webhooks (two gunicorn workers at once):

    identical request (same child, start date, allow-listed payload)
        -> 200 with the EXISTING job's id and `already_queued: true`
    a different request for a child that already has an active job
        -> 409 with that job's id, and a Slack notice saying so, with the
           `queue-cancel` command to replace it

The router knows only this module's ACTION_NAME and `handle`.
"""

import logging

from actions.create_timeline import engine
from actions.plan_write import timeline
from actions.plan_write.hubspot_intake import unwrap_payload

from integrations import slack

from . import jobs, worker

logger = logging.getLogger(__name__)

ACTION_NAME = "queue_timeline"


def _blank() -> dict:
    return {
        "job_id": None,
        "status": None,
        "already_queued": False,
        "segments": [],
        "warnings": [],
        "errors": [],
    }


def handle(payload: dict) -> tuple[dict, int]:
    """Validate, store and acknowledge. 200 with the job; 422 when it cannot be queued."""
    data = _blank()

    if worker.mode() == worker.MODE_OFF:
        data["errors"] = ["the queue is off (QUEUE_MODE=off); nothing was queued"]
        return data, 503

    raw = unwrap_payload(payload)
    if not isinstance(raw, dict):
        data["errors"] = ["body must be a JSON object"]
        return data, 422

    parsed = engine.parse_request(raw)
    if parsed.request is None:
        data["errors"] = parsed.errors
        return data, 422
    request = parsed.request

    try:
        segments, timeline_warnings = timeline.build_segments(
            request.start, request.leave, funding=request.funding, advanced=request.advanced
        )
    except timeline.TimelineError as exc:
        data["errors"] = [str(exc)]
        return data, 422

    # Every plan is validated now, so a job that can never run is refused here
    # instead of failing hours later with no one watching.
    problems = engine.validate_segments(request.payload, segments)
    if problems:
        data["errors"] = problems
        return data, 422

    enqueued = jobs.create(request.child_id, request.start, request.payload, segments)
    job = enqueued.job
    data.update(
        job_id=job.job_id,
        status=job.status,
        already_queued=enqueued.outcome == jobs.ALREADY_QUEUED,
        segments=[{k: v for k, v in s.items() if k != "plan_id"} for s in job.segments],
        warnings=list(timeline_warnings),
    )

    if enqueued.outcome == jobs.ACTIVE_CONFLICT:
        # Nothing was created. The notice uses the bare child id: looking the
        # name up would be a call to Famly, and this path makes none.
        message = (
            f"⚠️ queue_timeline for child `{request.child_id}`: the child already has "
            f"an active job, `{job.job_id}` ({job.status}). Nothing was queued. To "
            f"replace it run `python main.py queue-cancel {job.job_id}` and send the "
            f"request again."
        )
        logger.warning("QUEUE_TIMELINE CONFLICT child_id=%s active job_id=%s", request.child_id, job.job_id)
        slack.post_notice(message)
        data["errors"] = [message]
        return data, 409

    if enqueued.outcome == jobs.ALREADY_QUEUED:
        logger.info("QUEUE_TIMELINE already queued as job_id=%s", job.job_id)
    return data, 200
