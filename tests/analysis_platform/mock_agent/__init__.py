"""Mock Analysis Agent — exercises the real Portal agent control plane without Windows/RDP."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import CommandType

logger = logging.getLogger(__name__)


@dataclass
class MockAgentState:
    agent_id: str
    token: str = ""
    workstation_id: str = ""
    registered: bool = False
    heartbeats: int = 0
    commands_completed: list[str] = field(default_factory=list)
    last_error: str = ""
    software: list[dict[str, Any]] = field(default_factory=list)


class MockAnalysisAgent:
    """
    Behaves like the Windows Remote Analysis Agent over HTTP APIs:

    register → heartbeat → inventory → poll commands → complete
    (PREPARE / COLLECT / CLEAN / PING) without Guacamole or RDP.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        hostname: str = "APT-MOCK-PC",
        software_names: list[str] | None = None,
        api: APIClient | None = None,
        base_path: str = "/api/v1/analysis",
        enrollment_key: str | None = None,
    ):
        self.api = api or APIClient()
        self.base = base_path.rstrip("/")
        self.hostname = hostname
        self.enrollment_key = enrollment_key
        self.state = MockAgentState(
            agent_id=agent_id,
            software=[
                {"displayName": n, "version": "1.0", "publisher": "APT", "category": "Analysis"}
                for n in (software_names or ["Notepad", "Origin Test", "MATLAB Test"])
            ],
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_command: Callable[[dict], None] | None = None

    def register(self) -> dict[str, Any]:
        headers = {}
        body = {
            "agentId": self.state.agent_id,
            "hostname": self.hostname,
            "displayName": f"Mock {self.hostname}",
            "cpuCores": 8,
            "memoryGB": 32,
            "storageGB": 500,
            "agentVersion": "mock-1.0.0",
            "operatingSystem": "Windows Mock",
        }
        if self.enrollment_key:
            headers["HTTP_X_ENROLLMENT_KEY"] = self.enrollment_key
            body["enrollmentKey"] = self.enrollment_key
        res = self.api.post(f"{self.base}/register/", body, format="json", **headers)
        data = res.json() if res.content else {}
        if res.status_code not in (200, 201) or not data.get("accepted"):
            self.state.last_error = str(data)
            raise RuntimeError(f"Mock agent register failed: {res.status_code} {data}")
        self.state.token = data.get("token") or ""
        self.state.workstation_id = str(data.get("workstation_id") or "")
        self.state.registered = True
        self._auth()
        return data

    def heartbeat(self) -> dict[str, Any]:
        self._auth()
        res = self.api.post(
            f"{self.base}/heartbeat/",
            {
                "CPU": 12.0,
                "Memory": 35.0,
                "Disk": 40.0,
                "LoggedInUser": "mock-analyst",
                "CurrentStatus": "AVAILABLE",
                "Online": True,
                "Idle": False,
                "IdleTimeMinutes": 0,
                "WindowsUptimeHours": 1,
                "RunningProcesses": 50,
                "SoftwareCount": len(self.state.software),
            },
            format="json",
        )
        data = res.json() if res.content else {}
        if res.status_code == 200 and data.get("accepted"):
            self.state.heartbeats += 1
        else:
            self.state.last_error = str(data)
        return data

    def publish_inventory(self) -> dict[str, Any]:
        self._auth()
        res = self.api.post(
            f"{self.base}/inventory/",
            {"software": self.state.software, "hardware": {"cpu": "MockCPU", "gpu": ""}, "licenses": []},
            format="json",
        )
        return res.json() if res.content else {}

    def poll_commands(self) -> list[dict[str, Any]]:
        self._auth()
        res = self.api.get(f"{self.base}/commands/")
        if res.status_code != 200:
            return []
        data = res.json()
        return data if isinstance(data, list) else data.get("commands") or []

    def complete_command(self, command_id: str, *, success: bool = True, message: str = "mock ok") -> dict:
        self._auth()
        res = self.api.post(
            f"{self.base}/commands/{command_id}/complete/",
            {"success": success, "message": message},
            format="json",
        )
        data = res.json() if res.content else {}
        if res.status_code == 200:
            self.state.commands_completed.append(str(command_id))
        return data

    def handle_command(self, cmd: dict[str, Any]) -> None:
        """Simulate command execution (folders / RAW download / processed upload are mocked)."""
        cmd_id = str(cmd.get("id") or "")
        cmd_type = str(cmd.get("type") or cmd.get("command_type") or "").upper()
        payload = cmd.get("payloadJson") or cmd.get("payload") or {}
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}

        message = f"mock completed {cmd_type}"
        if cmd_type == CommandType.PREPARE_WORKSTATION:
            message = "mock prepared workspace folders + downloaded Input (simulated)"
        elif cmd_type == CommandType.COLLECT_WORKSPACE:
            message = "mock uploaded Processed outputs (simulated)"
        elif cmd_type == CommandType.CLEAN_WORKSTATION:
            message = "mock cleaned session workspace"
        elif cmd_type == CommandType.PING:
            message = "pong"

        if self._on_command:
            self._on_command({"id": cmd_id, "type": cmd_type, "payload": payload})
        self.complete_command(cmd_id, success=True, message=message)

    def process_once(self) -> int:
        """One poll+complete cycle. Returns number of commands handled."""
        if not self.state.registered:
            self.register()
        self.heartbeat()
        cmds = self.poll_commands()
        for cmd in cmds:
            self.handle_command(cmd)
        return len(cmds)

    @classmethod
    def from_seed(cls, seed, *, api: APIClient | None = None) -> "MockAnalysisAgent":
        """Attach to a workstation created by AnalysisPlatformSeeder."""
        agent = cls(
            agent_id=seed.workstation.agent_id,
            hostname=seed.workstation.hostname,
            software_names=[s.name for s in (seed.software or [])],
            api=api,
        )
        agent.state.token = seed.agent_token
        agent.state.workstation_id = str(seed.workstation.id)
        agent.state.registered = True
        agent._auth()
        return agent

    def bootstrap(self, *, re_register: bool = False) -> None:
        if re_register or not self.state.registered or not self.state.token:
            self.register()
        self.heartbeat()
        self.publish_inventory()

    def start_background(self, *, interval_seconds: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        def _loop():
            try:
                self.bootstrap()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Mock agent bootstrap failed: %s", exc)
                self.state.last_error = str(exc)
                return
            while not self._stop.wait(interval_seconds):
                try:
                    self.process_once()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Mock agent loop error: %s", exc)
                    self.state.last_error = str(exc)

        self._stop.clear()
        self._thread = threading.Thread(target=_loop, name=f"mock-agent-{self.state.agent_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _auth(self) -> None:
        if self.state.token:
            self.api.credentials(
                HTTP_AUTHORIZATION=f"Bearer {self.state.token}",
                HTTP_X_AGENT_ID=self.state.agent_id,
            )
