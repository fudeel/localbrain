"""Assembles the HTTP application from the brain's services."""

from brain.container import Brain
from brain.interfaces.http.routes import (
    AgentController,
    ChatController,
    DashboardController,
    DeviceController,
    EventController,
    JobController,
    SystemController,
)
from brain.interfaces.http.security import SecurityPolicy
from brain.interfaces.http.server import Application, BrainHttpServer, build_router


def build_application(brain: Brain) -> Application:
    settings = brain.settings
    security = SecurityPolicy(
        token=brain.token,
        loopback_only=settings.is_loopback_only,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    router = build_router(
        (
            SystemController(
                runtime=brain.runtime,
                hardware=brain.hardware,
                agents=brain.agents,
                jobs=brain.jobs,
                token_required=security.token_required,
            ),
            AgentController(agents=brain.agents, jobs=brain.jobs),
            ChatController(brain.chat),
            DeviceController(brain.devices),
            JobController(brain.jobs),
            EventController(brain.events),
            DashboardController(),
        )
    )
    return Application(router=router, security=security, max_body_bytes=settings.max_body_bytes)


def build_server(brain: Brain) -> BrainHttpServer:
    return BrainHttpServer((brain.settings.host, brain.settings.port), build_application(brain))
