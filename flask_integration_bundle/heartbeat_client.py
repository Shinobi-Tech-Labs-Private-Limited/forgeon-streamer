from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

RecordingFormat = Literal["csv", "jsonl"]


class HeartbeatServiceError(RuntimeError):
    """Raised when the Heartbeat sidecar cannot complete a request."""


@dataclass(frozen=True)
class HeartbeatClient:
    base_url: str
    timeout_seconds: float = 5.0

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                json=json_body,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return payload
        except (httpx.HTTPError, ValueError) as error:
            raise HeartbeatServiceError(str(error)) from error

    def latest(self) -> dict[str, Any]:
        return self._request("GET", "/hr")

    def device(self) -> dict[str, Any]:
        return self._request("GET", "/device")

    def recording_status(self) -> dict[str, Any]:
        return self._request("GET", "/recording")

    def start_recording(self, recording_format: RecordingFormat) -> dict[str, Any]:
        return self._request(
            "POST", "/recording/start", {"format": recording_format}
        )

    def stop_recording(self) -> dict[str, Any]:
        return self._request("POST", "/recording/stop")
