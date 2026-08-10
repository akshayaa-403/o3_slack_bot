"""
Onboarding as an explicit, recoverable sequence of steps.

Every step here can fail independently — Slack can time out, DynamoDB can
throttle, S3 can reject a bucket name. Left unguarded, a failure halfway
through strands a tenant in a partial state: a row exists, so the "already
onboarded" lookup short-circuits forever, but the provisioning that row
implies was never finished. We hit exactly that during development, when an
S3 permission error left a tenant with no bucket that no later attempt would
ever repair.

The model (per design review) is retry-then-roll-back, not resume:

  * each step retries a few times on transient failure
  * if it still fails, every completed step is undone in reverse order
  * the user is told to try again, and starts cleanly

Resume-from-failure was considered and rejected: without a completed tenant
record there is no durable handle to resume against, and the number of
partial states to reason about grows with every connector added.

A note on retry timing. The design review said "retry every minute, three
times". That assumes a long-lived process; this runs in Lambda behind a
browser redirect, where the function is capped at 15s and the user is
staring at a spinner. Minute-spaced retries would guarantee a timeout and
turn a recoverable blip into a hard failure. So the *shape* is preserved —
three attempts, then roll back — while the spacing is compressed to
sub-second backoff, which is what actually rides out throttles and
transient socket errors. Genuinely slow recovery (an AWS partial outage)
is not something a synchronous install can wait for either way; that needs
the queued/asynchronous provisioning the architecture doc anticipates.
"""
import logging
import time

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.25


class OnboardingError(RuntimeError):
    """Onboarding failed and everything it created has been rolled back."""

    def __init__(self, step_name, original):
        super().__init__(f"Onboarding failed at step '{step_name}': {original}")
        self.step_name = step_name
        self.original = original


def run_with_retries(fn, *, step_name, max_attempts=MAX_ATTEMPTS):
    """Call fn(), retrying transient failures with exponential backoff.

    Returns fn()'s value, or raises the final exception once attempts are
    exhausted so the caller can trigger a rollback.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Onboarding step '%s' failed (attempt %d/%d): %s — retrying in %.2fs",
                step_name, attempt, max_attempts, exc.__class__.__name__, delay,
            )
            time.sleep(delay)

    logger.error(
        "Onboarding step '%s' failed after %d attempts: %s",
        step_name, max_attempts, last_error.__class__.__name__,
    )
    raise last_error


class Saga:
    """Runs steps in order, remembering how to undo each one.

    Each step supplies a `compensate` callable that reverses it. On failure
    the recorded compensations run in reverse, so cleanup happens in the
    opposite order to creation (drop the bucket before the tenant row that
    names it, not after).
    """

    def __init__(self):
        self._completed = []

    def run(self, step_name, action, compensate=None):
        result = run_with_retries(action, step_name=step_name)
        if compensate is not None:
            self._completed.append((step_name, compensate))
        return result

    def rollback(self):
        """Undo completed steps, newest first.

        A failing compensation is logged and skipped rather than raised: we
        are already on the error path, and one stubborn resource must not
        stop the rest of the cleanup. Whatever is left behind is reported.
        """
        leftovers = []
        for step_name, compensate in reversed(self._completed):
            try:
                compensate()
                logger.info("Rolled back onboarding step '%s'", step_name)
            except Exception as exc:
                leftovers.append(step_name)
                logger.error(
                    "Could not roll back step '%s': %s — may need manual cleanup",
                    step_name, exc.__class__.__name__,
                )
        self._completed = []
        return leftovers
