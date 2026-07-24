"""Application service container."""

from uuid import UUID

from apps.server.src.core.action_lifecycle_manager import ActionLifecycleManager
from apps.server.src.core.action_lifecycle_repository import (
    ActionLifecycleRepository,
    InMemoryActionLifecycleRepository,
)
from apps.server.src.core.action_queue import ActionQueue
from apps.server.src.core.approvals import (
    InMemoryPendingApprovalRegistry,
    PendingApprovalRegistry,
)
from apps.server.src.core.config import get_settings
from apps.server.src.core.events import (
    BaseContextResolver,
    EventInbox,
    EventLifecycleManager,
    EventLifecycleState,
    EventProcessingPipeline,
    EventRepository,
    EventStore,
    EventWorkflowService,
    RuleBasedEventClassifier,
)
from apps.server.src.core.events.classifier import EventClassifier
from apps.server.src.core.events.context import ContextResolver
from apps.server.src.core.permission import (
    BasePermissionEngine,
    PermissionEngine,
    PermissionEngineRuntime,
)
from apps.server.src.core.planner import BasePlanner, Planner
from apps.server.src.integrations.calendar import (
    CALENDAR_ACCOUNT_CONTEXT,
    CalendarWorkerExecutor,
)
from apps.server.src.integrations.calendar_ingress import (
    CalendarEventNormalizer,
    CalendarIngressAdapter,
)
from apps.server.src.integrations.gmail import GMAIL_ACCOUNT_CONTEXT, GmailWorkerExecutor
from apps.server.src.workers.executor import (
    NoOpWorkerExecutor,
    WorkerExecutor,
    WorkerExecutorRegistry,
)
from apps.server.src.workers.runtime import (
    InMemoryWorkerExecutionObserver,
    WorkerRuntime,
    WorkerRuntimeInvocationService,
)


class ApplicationContainer:
    """Wires current in-process application services."""

    GMAIL_ACCOUNT_CONTEXT = GMAIL_ACCOUNT_CONTEXT
    CALENDAR_ACCOUNT_CONTEXT = CALENDAR_ACCOUNT_CONTEXT

    def __init__(self) -> None:
        self.event_repository: EventRepository = EventStore()
        self.event_inbox = EventInbox()
        self.event_lifecycle_manager = EventLifecycleManager()
        self.event_lifecycle_states: dict[UUID, EventLifecycleState] = {}
        self.action_queue = ActionQueue()
        self.action_lifecycle_manager = ActionLifecycleManager()
        self.action_lifecycle_repository: ActionLifecycleRepository = (
            InMemoryActionLifecycleRepository()
        )
        self.pending_approval_registry: PendingApprovalRegistry = (
            InMemoryPendingApprovalRegistry()
        )
        self.permission_engine: PermissionEngine = BasePermissionEngine()
        self.permission_runtime = PermissionEngineRuntime(
            permission_engine=self.permission_engine,
            action_lifecycle_manager=self.action_lifecycle_manager,
            lifecycle_repository=self.action_lifecycle_repository,
            pending_approval_registry=self.pending_approval_registry,
        )
        self.worker_executor: WorkerExecutor = NoOpWorkerExecutor()
        self.worker_executor_registry = WorkerExecutorRegistry(
            fallback_executor=self.worker_executor,
        )
        self.gmail_worker_executor = GmailWorkerExecutor()
        self.worker_executor_registry.register_manifest(
            self.gmail_worker_executor.provider_manifest
        )
        self.calendar_worker_executor = CalendarWorkerExecutor()
        self.worker_executor_registry.register_manifest(
            self.calendar_worker_executor.provider_manifest
        )
        self.worker_execution_observer = InMemoryWorkerExecutionObserver()
        self.worker_runtime = WorkerRuntime(
            action_queue=self.action_queue,
            action_lifecycle_manager=self.action_lifecycle_manager,
            worker_executor=self.worker_executor,
            executor_registry=self.worker_executor_registry,
            execution_observer=self.worker_execution_observer,
            lifecycle_repository=self.action_lifecycle_repository,
            max_transient_retries=get_settings().max_transient_retries,
        )
        self.worker_runtime_invocation = WorkerRuntimeInvocationService(
            worker_runtime=self.worker_runtime,
        )
        self.event_classifier: EventClassifier = RuleBasedEventClassifier()
        self.context_resolver: ContextResolver = BaseContextResolver()
        self.event_processing_pipeline = EventProcessingPipeline(
            classifier=self.event_classifier,
            context_resolver=self.context_resolver,
        )
        self.planner: Planner = BasePlanner()
        self.event_workflow_service = EventWorkflowService(
            event_repository=self.event_repository,
            event_inbox=self.event_inbox,
            event_lifecycle_manager=self.event_lifecycle_manager,
            event_lifecycle_states=self.event_lifecycle_states,
            event_processing_pipeline=self.event_processing_pipeline,
            planner=self.planner,
            permission_runtime=self.permission_runtime,
            action_queue=self.action_queue,
        )
        self.calendar_event_normalizer = CalendarEventNormalizer()
        self.calendar_ingress_adapter = CalendarIngressAdapter(
            normalizer=self.calendar_event_normalizer,
            workflow_service=self.event_workflow_service,
        )


_container: ApplicationContainer | None = None


def get_container() -> ApplicationContainer:
    """Return the process-wide application container singleton."""
    global _container

    if _container is None:
        _container = ApplicationContainer()

    return _container
