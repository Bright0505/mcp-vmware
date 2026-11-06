"""Tool 回傳資料模型 — 供 MCP structured output(outputSchema / structuredContent)使用。

欄位名稱刻意與 vSphere REST API 回傳的欄位保持一致(例如 `memory_size_MiB`),
所有欄位皆為選填且允許額外欄位,避免不同 vCenter 版本多回或少回欄位時驗證失敗。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _VCenterModel(BaseModel):
    """共用設定:保留 vCenter 多回的欄位,不因版本差異而失敗。"""

    model_config = ConfigDict(extra="allow")


class VMSummary(_VCenterModel):
    """虛擬機摘要(GET /api/vcenter/vm)。"""

    vm: str | None = None
    name: str | None = None
    power_state: str | None = None
    cpu_count: int | None = None
    memory_size_MiB: int | None = None  # noqa: N815 — 對齊 vCenter 欄位名


class VMDetail(_VCenterModel):
    """單一虛擬機詳情(硬體 / 電源 / 客體 OS)。"""

    vm_id: str
    details: dict[str, Any] | None = None
    power: dict[str, Any] | None = None
    guest_identity: dict[str, Any] | None = None
    guest_identity_note: str | None = None


class HostSummary(_VCenterModel):
    """ESXi 主機摘要(GET /api/vcenter/host)。"""

    host: str | None = None
    name: str | None = None
    connection_state: str | None = None
    power_state: str | None = None


class ClusterSummary(_VCenterModel):
    """叢集摘要(GET /api/vcenter/cluster)。"""

    cluster: str | None = None
    name: str | None = None
    drs_enabled: bool | None = None
    ha_enabled: bool | None = None


class DatastoreSummary(_VCenterModel):
    """Datastore 摘要(GET /api/vcenter/datastore),容量單位為位元組。"""

    datastore: str | None = None
    name: str | None = None
    type: str | None = None
    capacity: int | None = None
    free_space: int | None = None


class NetworkSummary(_VCenterModel):
    """網路 / Port Group 摘要(GET /api/vcenter/network)。"""

    network: str | None = None
    name: str | None = None
    type: str | None = None


class DatacenterSummary(_VCenterModel):
    """資料中心摘要(GET /api/vcenter/datacenter)。"""

    datacenter: str | None = None
    name: str | None = None


class ResourcePoolSummary(_VCenterModel):
    """資源池摘要(GET /api/vcenter/resource-pool)。"""

    resource_pool: str | None = None
    name: str | None = None


class FolderSummary(_VCenterModel):
    """資料夾摘要(GET /api/vcenter/folder)。"""

    folder: str | None = None
    name: str | None = None
    type: str | None = None


# --- 清單結果(附預先算好的彙總) -------------------------------------------
# 數量、合計、比例一律在這裡算好再回傳:LLM 自行計算長清單容易數錯,
# 用戶端只需引用 `total` 與各 `*_counts` 欄位,不必自己數。


class VMListResult(BaseModel):
    total: int
    power_state_counts: dict[str, int]
    total_cpu_count: int
    total_memory_GiB: float  # noqa: N815
    vms: list[VMSummary]


class DatastoreUsage(DatastoreSummary):
    capacity_GiB: float | None = None  # noqa: N815
    free_GiB: float | None = None  # noqa: N815
    used_pct: float | None = None


class DatastoreListResult(BaseModel):
    total: int
    total_capacity_GiB: float  # noqa: N815
    total_free_GiB: float  # noqa: N815
    overall_used_pct: float | None
    used_pct_at_least_90: int
    used_pct_at_least_80: int
    sorted_by: str
    datastores: list[DatastoreUsage]


class HostListResult(BaseModel):
    total: int
    connection_state_counts: dict[str, int]
    power_state_counts: dict[str, int]
    hosts: list[HostSummary]


class ClusterListResult(BaseModel):
    total: int
    drs_enabled_count: int
    ha_enabled_count: int
    clusters: list[ClusterSummary]


class NetworkListResult(BaseModel):
    total: int
    type_counts: dict[str, int]
    networks: list[NetworkSummary]


class DatacenterListResult(BaseModel):
    total: int
    datacenters: list[DatacenterSummary]


class ResourcePoolListResult(BaseModel):
    total: int
    resource_pools: list[ResourcePoolSummary]


class FolderListResult(BaseModel):
    total: int
    type_counts: dict[str, int]
    folders: list[FolderSummary]
