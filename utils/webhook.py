import json
import logging
import httpx
from models.candidate import ScrapeResult

logger = logging.getLogger(__name__)

# CV-bearing payloads can be 1-5 MB; give the remote side time to persist.
WEBHOOK_TIMEOUT_SECONDS = 120.0


async def send_webhook(url: str, result: ScrapeResult) -> bool:
    """POST ScrapeResult to n8n webhook. Logs payload size + result."""
    payload = result.model_dump()
    payload_bytes = len(json.dumps(payload).encode("utf-8"))
    payload_kb = payload_bytes / 1024
    n_cand = len(payload.get("candidates", []))
    logger.info(f"Webhook POST -> {url}  candidates={n_cand}  payload={payload_kb:.1f} KB  timeout={WEBHOOK_TIMEOUT_SECONDS}s")

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
