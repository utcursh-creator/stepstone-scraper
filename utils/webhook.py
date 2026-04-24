import json
import logging
import httpx
from models.candidate import ScrapeResult

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 120.0


async def send_webhook(url: str, result: ScrapeResult) -> bool:
    """POST ScrapeResult to n8n webhook.

    cv_base64 is stripped before serialisation regardless of upstream state —
    Recruitee direct-upload makes base64 unnecessary in the webhook payload.
    """
    payload = result.model_dump()
    # Strip cv_base64 from every candidate: already uploaded to Recruitee,
    # no need to carry 7 MB of PDF through the webhook.
    for cand in payload.get("candidates", []):
        cand["cv_base64"] = None

    payload_bytes = len(json.dumps(payload).encode("utf-8"))
    payload_kb = payload_bytes / 1024
    n_cand = len(payload.get("candidates", []))
    logger.info(
        f"Webhook POST -> {url}  candidates={n_cand}  "
        f"payload={payload_kb:.1f} KB  timeout={WEBHOOK_TIMEOUT_SECONDS}s"
    )

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
            logger.info(f"Webhook response: HTTP {response.status_code}  body[:200]={response.text[:200]!r}")
            response.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Webhook HTTPStatusError: {e.response.status_code}  body={e.response.text[:300]!r}")
            return False
        except httpx.TimeoutException:
            logger.error(f"Webhook timeout after {WEBHOOK_TIMEOUT_SECONDS}s (payload {payload_kb:.1f} KB)")
            return False
        except httpx.HTTPError as e:
            logger.error(f"Webhook HTTPError: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            logger.error(f"Webhook unexpected error: {type(e).__name__}: {e}")
            return False
