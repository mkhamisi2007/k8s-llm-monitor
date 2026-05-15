from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from enum import Enum
import uuid


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Issue(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    severity: Severity
    title: str
    description: str
    solution: str
    resource: Optional[str] = None
    namespace: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class NodeInfo(BaseModel):
    name: str
    status: str
    roles: List[str]
    conditions: dict
    allocatable: dict
    capacity: dict


class ClusterStatus(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    nodes: List[NodeInfo] = []
    issues: List[Issue] = []
    total_pods: int = 0
    running_pods: int = 0
    failed_pods: int = 0
    pending_pods: int = 0
    total_deployments: int = 0
    healthy_deployments: int = 0
    llm_available: bool = False
    error: Optional[str] = None
