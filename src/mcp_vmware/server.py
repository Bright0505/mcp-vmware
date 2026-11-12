"""VMware MCP server — 暴露 vCenter 唯讀查詢 tool。

遵循 MCP 2026-07-28 規範(MCP Python SDK 2.x):
  - 以 `MCPServer` 註冊 tool,並標註 `readOnlyHint`,明確宣告本 server 不會異動環境。
  - 每個 tool 都有 outputSchema / structuredContent(見 `models.py`)。
  - `tools/list` 提供 cache hint,讓用戶端可快取工具清單。

inventory 查詢走 REST GET;效能數據走 SOAP 的 PerformanceManager 唯讀查詢(見 `perf.py`)。
不提供任何開關機、修改、刪除操作。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Literal

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .config import load_config
from .models import (
    ClusterListResult,
    ClusterSummary,
    DailyStat,
    DatacenterListResult,
    DatacenterSummary,
    DatastoreListResult,
    DatastoreUsage,
    FolderListResult,
    FolderSummary,
    HostListResult,
    HostSummary,
    MetricPerformance,
    MetricStats,
    NetworkListResult,
    NetworkSummary,
    ResourcePoolListResult,
    ResourcePoolSummary,
    VMDetail,
    VMListResult,
    VMPerformance,
    VMSummary,
)
from .perf import PerfClient, PerfError, daily, summarize
from .vcenter import VCenterClient, VCenterError

#: 所有 tool 共用的標註 — 唯讀、不具破壞性、可重複呼叫、查詢外部系統。
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


@dataclass
class AppContext:
    """Server 生命週期內共用的資源。"""

    vcenter: VCenterClient
    perf: PerfClient
    tz: tzinfo | None  # VCENTER_TIMEZONE;None 表示用行程本機時區
    tz_name: str | None  # VCENTER_TIMEZONE 的 IANA 名稱


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    """建立 vCenter client(REST 與效能查詢用的 SOAP),並在 server 結束時登出。"""
    config = load_config()
    client = VCenterClient(config)
    perf = PerfClient(config)  # 延遲登入,第一次查效能時才連線
    try:
        yield AppContext(vcenter=client, perf=perf, tz=config.tz, tz_name=config.timezone)
    finally:
        await client.aclose()
        await asyncio.to_thread(perf.close)


mcp = MCPServer(
    name="vmware",
    title="VMware vCenter(唯讀)",
    instructions=(
        "查詢 VMware vCenter 環境的唯讀工具集,涵蓋虛擬機、ESXi 主機、叢集、"
        "Datastore、網路、資料中心、資源池與資料夾,以及 VM 歷史效能數據。所有工具皆為唯讀,"
        "不會開關機或修改任何 vCenter 資源。"
    ),
    version=__version__,
    lifespan=lifespan,
    # inventory 清單本身不常變動,允許用戶端短時間快取工具清單。
    cache_hints={
        "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),
        "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
    },
)

VMwareContext = Context[AppContext, Any]


async def _get(ctx: VMwareContext, path: str, params: dict[str, Any] | None = None) -> Any:
    """代 tool 發出 vCenter GET 請求,並把錯誤轉成 MCP tool 錯誤結果。"""
    client = ctx.request_context.lifespan_context.vcenter
    try:
        return await client.get(path, params)
    except VCenterError as exc:
        raise ToolError(str(exc)) from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    """vSphere REST 回傳的清單;相容舊版包在 `value` 之下的格式。"""
    if isinstance(payload, dict):
        payload = payload.get("value", [])
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _counts(values: list[str | None]) -> dict[str, int]:
    """依值分組計數(None 記為 UNKNOWN),鍵依字母排序,方便比對。"""
    out: dict[str, int] = {}
    for v in values:
        key = v or "UNKNOWN"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


_GIB = 1024**3


def _gib(value: int | None) -> float | None:
    return None if value is None else round(value / _GIB, 2)


# --- 虛擬機 -----------------------------------------------------------------


@mcp.tool(title="列出虛擬機", annotations=READ_ONLY)
async def list_vms(
    ctx: VMwareContext,
    power_state: Literal["POWERED_ON", "POWERED_OFF", "SUSPENDED"] | None = None,
    name: str | None = None,
    name_contains: str | None = None,
) -> VMListResult:
    """列出 vCenter 中的虛擬機(VM),並附上算好的彙總。

    回傳:
        total: 符合條件的 VM 台數(要回答「幾台」請直接引用,不要自己數 vms)。
        power_state_counts: 依電源狀態分組的台數。
        total_cpu_count / total_memory_GiB: 符合條件 VM 的 vCPU 與記憶體配置合計。
        vms: 每台 VM 的 id、名稱、電源狀態、CPU 數與記憶體(MiB),依名稱排序。

    參數:
        power_state: 選填,依電源狀態過濾。
        name: 選填,VM 名稱完全比對。
        name_contains: 選填,名稱包含此關鍵字(不分大小寫)。只知道部分名稱時用這個。
    """
    params: dict[str, Any] = {}
    if power_state:
        params["power_states"] = [power_state]
    if name:
        params["names"] = [name]
    payload = await _get(ctx, "/api/vcenter/vm", params or None)
    vms = [VMSummary.model_validate(row) for row in _rows(payload)]
    if name_contains:
        needle = name_contains.casefold()
        vms = [v for v in vms if v.name and needle in v.name.casefold()]
    vms.sort(key=lambda v: (v.name or "").casefold())
    return VMListResult(
        total=len(vms),
        power_state_counts=_counts([v.power_state for v in vms]),
        total_cpu_count=sum(v.cpu_count or 0 for v in vms),
        total_memory_GiB=round(sum(v.memory_size_MiB or 0 for v in vms) / 1024, 2),
        vms=vms,
    )


@mcp.tool(title="取得虛擬機詳情", annotations=READ_ONLY)
async def get_vm(ctx: VMwareContext, vm_id: str) -> VMDetail:
    """取得單一虛擬機的詳細資訊。

    彙整硬體設定(CPU、記憶體、磁碟、網卡)、電源狀態,以及客體 OS 身分資訊
    (需 VM 開機且 VMware Tools 執行中才有客體資訊)。

    參數:
        vm_id: VM 的 managed object id,例如 "vm-123"(可由 list_vms 取得)。
    """
    encoded = VCenterClient.encode_id(vm_id)
    detail = VMDetail(
        vm_id=vm_id,
        details=await _get(ctx, f"/api/vcenter/vm/{encoded}"),
        power=await _get(ctx, f"/api/vcenter/vm/{encoded}/power"),
    )
    try:
        detail.guest_identity = await _get(
            ctx, f"/api/vcenter/vm/{encoded}/guest/identity"
        )
    except ToolError as exc:
        detail.guest_identity = None
        detail.guest_identity_note = (
            f"無法取得客體資訊(VM 可能未開機或未安裝 VMware Tools): {exc}"
        )
    return detail


@mcp.tool(title="取得虛擬機效能數據", annotations=READ_ONLY)
async def get_vm_performance(
    ctx: VMwareContext,
    vm_id: str,
    days: int = 7,
    include_series: bool = False,
) -> VMPerformance:
    """取得單一虛擬機近幾天的歷史效能數據與統計。

    從 vCenter PerformanceManager 讀取 CPU(使用率、MHz、Ready)、記憶體(使用率、
    consumed、active、balloon、swap)、磁碟(吞吐量、最大延遲)、網路吞吐量,
    回傳每個指標的 min / max / avg / p95 / 最新值、峰值時間與每日統計。

    取樣間隔依 vCenter 統計間隔設定自動選擇(預設:1 天內 5 分鐘、1 週內 30 分鐘、
    1 個月內 2 小時、1 年內 1 天)。統計層級不足未收集、或期間內全無資料的指標
    會列在 unavailable_metrics。分日與時間以 VCENTER_TIMEZONE(未設定則本機時區)為準。

    參數:
        vm_id: VM 的 managed object id,例如 "vm-123"(可由 list_vms 取得)。
        days: 查詢最近幾天,1~365,預設 7。
        include_series: 是否附上完整時間序列(資料量較大),預設 false。
    """
    if not 1 <= days <= 365:
        raise ToolError("days 必須介於 1 到 365 之間。")

    app = ctx.request_context.lifespan_context
    tz = app.tz
    try:
        result = await asyncio.to_thread(app.perf.query_vm, vm_id, days)
    except PerfError as exc:
        raise ToolError(str(exc)) from exc

    def fmt(ts: datetime) -> str:
        return ts.astimezone(tz).isoformat(timespec="minutes")

    local_end = result.end.astimezone(tz)
    metrics = [
        MetricPerformance(
            metric=m.counter,
            description=m.description,
            unit=m.unit,
            samples=len(m.samples),
            stats=MetricStats(**summarize(m.samples, tz)),
            daily=[DailyStat(**d) for d in daily(m.samples, tz)],
            series=[(fmt(ts), round(v, 2)) for ts, v in m.samples] if include_series else None,
        )
        for m in result.metrics
    ]
    return VMPerformance(
        vm_id=vm_id,
        vm_name=result.vm_name,
        vcpu_count=result.vcpu_count,
        start=fmt(result.start),
        end=fmt(result.end),
        timezone=f"{app.tz_name or local_end.tzname()} (UTC{local_end.strftime('%z')})",
        interval_seconds=result.interval_seconds,
        stats_level=result.stats_level,
        metrics=metrics,
        unavailable_metrics=result.unavailable,
        notes=result.notes,
    )


# --- 主機 / 叢集 ------------------------------------------------------------


@mcp.tool(title="列出 ESXi 主機", annotations=READ_ONLY)
async def list_hosts(
    ctx: VMwareContext,
    connection_state: Literal["CONNECTED", "DISCONNECTED", "NOT_RESPONDING"] | None = None,
) -> HostListResult:
    """列出 vCenter 管理的 ESXi 主機,並附上依狀態分組的台數。

    回傳 total(主機台數,直接引用)、connection_state_counts、power_state_counts,
    以及每台主機的 id、名稱、連線狀態與電源狀態(依名稱排序)。

    參數:
        connection_state: 選填,依連線狀態過濾。
    """
    params: dict[str, Any] = {}
    if connection_state:
        params["connection_states"] = [connection_state]
    payload = await _get(ctx, "/api/vcenter/host", params or None)
    hosts = sorted(
        (HostSummary.model_validate(row) for row in _rows(payload)),
        key=lambda h: (h.name or "").casefold(),
    )
    return HostListResult(
        total=len(hosts),
        connection_state_counts=_counts([h.connection_state for h in hosts]),
        power_state_counts=_counts([h.power_state for h in hosts]),
        hosts=hosts,
    )


@mcp.tool(title="列出叢集", annotations=READ_ONLY)
async def list_clusters(ctx: VMwareContext) -> ClusterListResult:
    """列出 vCenter 中的叢集(Cluster)。

    回傳 total、啟用 DRS／HA 的叢集數,以及每個叢集的 id、名稱與 DRS／HA 設定。
    """
    payload = await _get(ctx, "/api/vcenter/cluster")
    clusters = sorted(
        (ClusterSummary.model_validate(row) for row in _rows(payload)),
        key=lambda c: (c.name or "").casefold(),
    )
    return ClusterListResult(
        total=len(clusters),
        drs_enabled_count=sum(1 for c in clusters if c.drs_enabled),
        ha_enabled_count=sum(1 for c in clusters if c.ha_enabled),
        clusters=clusters,
    )


# --- 儲存 / 網路 ------------------------------------------------------------


@mcp.tool(title="列出 Datastore", annotations=READ_ONLY)
async def list_datastores(ctx: VMwareContext) -> DatastoreListResult:
    """列出 vCenter 中的 Datastore(儲存),附上算好的容量與使用率。

    回傳:
        total: Datastore 數量(直接引用,不要自己數)。
        total_capacity_GiB / total_free_GiB / overall_used_pct: 全部合計。
        used_pct_at_least_90 / used_pct_at_least_80: 使用率達 90%／80% 以上的數量。
        datastores: 每個 datastore 的 id、名稱、類型、capacity/free_space(位元組)、
            capacity_GiB、free_GiB、used_pct(已用百分比),**已依 used_pct 由高到低排序**
            (容量為 0 或未知者排最後)。
    """
    payload = await _get(ctx, "/api/vcenter/datastore")
    rows: list[DatastoreUsage] = []
    for row in _rows(payload):
        ds = DatastoreUsage.model_validate(row)
        ds.capacity_GiB = _gib(ds.capacity)
        ds.free_GiB = _gib(ds.free_space)
        if ds.capacity and ds.free_space is not None:
            ds.used_pct = round(100 * (ds.capacity - ds.free_space) / ds.capacity, 2)
        rows.append(ds)
    rows.sort(key=lambda d: (d.used_pct is None, -(d.used_pct or 0), (d.name or "").casefold()))
    cap = sum(d.capacity or 0 for d in rows)
    free = sum(d.free_space or 0 for d in rows if d.capacity)
    return DatastoreListResult(
        total=len(rows),
        total_capacity_GiB=round(cap / _GIB, 2),
        total_free_GiB=round(free / _GIB, 2),
        overall_used_pct=round(100 * (cap - free) / cap, 2) if cap else None,
        used_pct_at_least_90=sum(1 for d in rows if (d.used_pct or 0) >= 90),
        used_pct_at_least_80=sum(1 for d in rows if (d.used_pct or 0) >= 80),
        sorted_by="used_pct desc",
        datastores=rows,
    )


@mcp.tool(title="列出網路", annotations=READ_ONLY)
async def list_networks(ctx: VMwareContext) -> NetworkListResult:
    """列出 vCenter 中的網路 / Port Group。

    回傳 total、依類型分組的數量(STANDARD_PORTGROUP、DISTRIBUTED_PORTGROUP 等),
    以及每個網路的 id、名稱與類型。
    """
    payload = await _get(ctx, "/api/vcenter/network")
    nets = sorted(
        (NetworkSummary.model_validate(row) for row in _rows(payload)),
        key=lambda n: (n.name or "").casefold(),
    )
    return NetworkListResult(total=len(nets), type_counts=_counts([n.type for n in nets]), networks=nets)


# --- 資料中心 / 資源池 / 資料夾 --------------------------------------------


@mcp.tool(title="列出資料中心", annotations=READ_ONLY)
async def list_datacenters(ctx: VMwareContext) -> DatacenterListResult:
    """列出 vCenter 中的資料中心(Datacenter):total 與每個資料中心的 id、名稱。"""
    payload = await _get(ctx, "/api/vcenter/datacenter")
    dcs = [DatacenterSummary.model_validate(row) for row in _rows(payload)]
    return DatacenterListResult(total=len(dcs), datacenters=dcs)


@mcp.tool(title="列出資源池", annotations=READ_ONLY)
async def list_resource_pools(ctx: VMwareContext) -> ResourcePoolListResult:
    """列出 vCenter 中的資源池(Resource Pool):total 與每個資源池的 id、名稱。"""
    payload = await _get(ctx, "/api/vcenter/resource-pool")
    pools = [ResourcePoolSummary.model_validate(row) for row in _rows(payload)]
    return ResourcePoolListResult(total=len(pools), resource_pools=pools)


@mcp.tool(title="列出資料夾", annotations=READ_ONLY)
async def list_folders(ctx: VMwareContext) -> FolderListResult:
    """列出 vCenter 中的資料夾(Folder)。

    回傳 total、依類型分組的數量(DATACENTER、VIRTUAL_MACHINE、HOST、DATASTORE、NETWORK),
    以及每個資料夾的 id、名稱與類型。
    """
    payload = await _get(ctx, "/api/vcenter/folder")
    folders = [FolderSummary.model_validate(row) for row in _rows(payload)]
    return FolderListResult(total=len(folders), type_counts=_counts([f.type for f in folders]), folders=folders)
