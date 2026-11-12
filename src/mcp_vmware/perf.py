"""vCenter 效能數據查詢(vSphere Web Services / SOAP API,透過 pyvmomi)。

vCenter 7 的 vSphere Automation REST API 沒有效能指標端點,因此改走
PerformanceManager。本模組只呼叫下列唯讀方法,不涉及任何資源異動:
  - SessionManager.Login / Logout          session 生命週期
  - ServiceInstance.CurrentTime            取得 vCenter 伺服器時間
  - PropertyCollector(屬性讀取)          perfCounter、historicalInterval、VM 名稱與 vCPU 數
  - PerformanceManager.QueryAvailablePerfMetric / QueryPerf

pyvmomi 為同步函式庫,呼叫端應以 `asyncio.to_thread` 包裝;本 client 內部以鎖序列化存取。
"""

from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .config import Config

logger = logging.getLogger(__name__)


class PerfError(RuntimeError):
    """效能數據查詢失敗。"""


@dataclass(frozen=True)
class MetricDef:
    """要查詢的效能指標(皆取彙總 instance "")。"""

    counter: str  # group.name.rollup,例 cpu.usage.average
    description: str


#: 預設查詢的指標。大多屬統計層級 1;層級不足的指標會列在 unavailable_metrics。
METRICS: tuple[MetricDef, ...] = (
    MetricDef("cpu.usage.average", "CPU 使用率"),
    MetricDef("cpu.usagemhz.average", "CPU 使用量"),
    MetricDef("cpu.ready.summation", "CPU Ready(每 vCPU 平均等待排程時間比例)"),
    MetricDef("mem.usage.average", "記憶體使用率(以 active 計)"),
    MetricDef("mem.consumed.average", "記憶體實際佔用(consumed)"),
    MetricDef("mem.active.average", "記憶體活躍量(active)"),
    MetricDef("mem.vmmemctl.average", "記憶體 balloon 量"),
    MetricDef("mem.swapped.average", "記憶體 swap 量"),
    MetricDef("disk.usage.average", "磁碟總吞吐量(讀+寫)"),
    MetricDef("disk.maxTotalLatency.latest", "磁碟最大延遲"),
    MetricDef("net.usage.average", "網路總吞吐量(收+送)"),
)


@dataclass
class MetricResult:
    counter: str
    description: str
    unit: str
    samples: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class PerfResult:
    vm_name: str
    vcpu_count: int | None
    start: datetime
    end: datetime
    interval_seconds: int
    stats_level: int | None
    metrics: list[MetricResult]
    unavailable: list[str]
    notes: list[str]


def _split_host(host: str) -> tuple[str, int]:
    """VCENTER_HOST 允許帶 port(例 vc.example.com:8443)。"""
    if host.count(":") == 1:
        name, port = host.split(":")
        if port.isdigit():
            return name, int(port)
    return host, 443


class PerfClient:
    """以 pyvmomi 讀取 VM 歷史效能數據的唯讀 client。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._si: Any = None
        self._counters: dict[str, Any] | None = None  # 名稱 -> PerfCounterInfo

    # --- session ------------------------------------------------------------

    def _connect(self) -> Any:
        host, port = _split_host(self._config.host)
        try:
            si = SmartConnect(
                host=host,
                port=port,
                user=self._config.user,
                pwd=self._config.password,
                disableSslCertValidation=self._config.insecure,
            )
        except vim.fault.InvalidLogin as exc:
            raise PerfError("vCenter 認證失敗,請檢查帳號密碼。") from exc
        except Exception as exc:  # 連線 / TLS 錯誤
            raise PerfError(f"無法連線 vCenter SOAP API: {exc}") from exc
        self._counters = None
        return si

    def close(self) -> None:
        with self._lock:
            si, self._si = self._si, None
        if si is not None:
            try:
                Disconnect(si)
            except Exception as exc:  # 登出失敗不應中斷關閉流程
                logger.warning("vCenter SOAP 登出失敗: %s", exc)

    # --- 查詢 ---------------------------------------------------------------

    def query_vm(self, vm_id: str, days: int) -> PerfResult:
        """查詢 VM 近 `days` 天的效能數據;session 過期時自動重新登入一次。"""
        with self._lock:
            for attempt in (1, 2):
                if self._si is None:
                    self._si = self._connect()
                try:
                    return self._query_vm(self._si, vm_id, days)
                except vim.fault.NotAuthenticated:
                    self._si = None
                    if attempt == 2:
                        raise PerfError("vCenter session 重試後仍失敗。") from None
                except vmodl.fault.ManagedObjectNotFound as exc:
                    raise PerfError(f"找不到 VM: {vm_id}") from exc
                except vim.fault.NoPermission as exc:
                    raise PerfError(f"權限不足,無法讀取 {vm_id}: {exc.msg}") from exc
                except vmodl.MethodFault as exc:
                    raise PerfError(f"vCenter 效能查詢失敗: {exc.msg}") from exc
                except PerfError:
                    raise
                except Exception as exc:  # 連線中斷等傳輸層錯誤:丟棄 session,下次重新登入
                    self._si = None
                    raise PerfError(f"vCenter SOAP 請求失敗: {exc}") from exc
        raise PerfError("vCenter session 重試後仍失敗。")  # pragma: no cover

    def _counter_map(self, perf_manager: Any) -> dict[str, Any]:
        if self._counters is None:
            self._counters = {
                f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}": c
                for c in perf_manager.perfCounter
            }
        return self._counters

    def _query_vm(self, si: Any, vm_id: str, days: int) -> PerfResult:
        content = si.RetrieveContent()
        perf = content.perfManager
        counters = self._counter_map(perf)
        notes: list[str] = []

        vm = vim.VirtualMachine(vm_id, si._stub)
        vm_name = vm.name  # VM 不存在時在此拋出 ManagedObjectNotFound
        summary_config = vm.summary.config
        vcpu = summary_config.numCpu if summary_config else None

        # 選擇能涵蓋查詢範圍、且取樣最細的已啟用歷史統計間隔。
        wanted = days * 86400
        intervals = sorted(
            (i for i in perf.historicalInterval if i.enabled),
            key=lambda i: i.samplingPeriod,
        )
        if not intervals:
            raise PerfError("vCenter 未啟用任何歷史統計間隔,無歷史效能數據可查。")
        chosen = next((i for i in intervals if i.length >= wanted), None)
        if chosen is None:
            chosen = max(intervals, key=lambda i: i.length)
            notes.append(
                f"已啟用的統計間隔最長只保留 {chosen.length // 86400} 天,查詢範圍已縮短。"
            )
            wanted = chosen.length

        end = si.CurrentTime()
        start = end - timedelta(seconds=wanted)

        by_id = {counters[m.counter].key: m for m in METRICS if m.counter in counters}
        available = perf.QueryAvailablePerfMetric(
            entity=vm, beginTime=start, endTime=end, intervalId=chosen.samplingPeriod
        )
        metric_ids = [
            vim.PerformanceManager.MetricId(counterId=a.counterId, instance="")
            for a in available
            if a.instance == "" and a.counterId in by_id
        ]
        queried = {mid.counterId for mid in metric_ids}
        unavailable = [m.counter for m in METRICS if counters.get(m.counter) is None
                       or counters[m.counter].key not in queried]

        results: dict[int, MetricResult] = {}
        if metric_ids:
            spec = vim.PerformanceManager.QuerySpec(
                entity=vm,
                metricId=metric_ids,
                intervalId=chosen.samplingPeriod,
                startTime=start,
                endTime=end,
                format="normal",
            )
            for entity_metric in perf.QueryPerf(querySpec=[spec]) or []:
                times = [s.timestamp for s in entity_metric.sampleInfo]
                for series in entity_metric.value:
                    mdef = by_id.get(series.id.counterId)
                    if mdef is None or series.id.instance != "":
                        continue
                    info = counters[mdef.counter]
                    res = results.setdefault(
                        series.id.counterId,
                        MetricResult(mdef.counter, mdef.description, _unit(mdef, info)),
                    )
                    for ts, raw in zip(times, series.value):
                        if raw < 0:  # -1 表示該時間點無資料
                            continue
                        res.samples.append(
                            (ts, _convert(mdef, info, raw, chosen.samplingPeriod, vcpu))
                        )

        # 依 METRICS 順序輸出有樣本的指標;可用但整段期間全是 -1(無資料)的也算 unavailable。
        ordered = [r for r in results.values() if r.samples]
        rank = {m.counter: i for i, m in enumerate(METRICS)}
        ordered.sort(key=lambda r: rank[r.counter])
        returned = {r.counter for r in ordered}
        unavailable += [
            m.counter
            for m in METRICS
            if m.counter not in returned and m.counter not in unavailable
        ]
        if unavailable and not any(n.startswith("部分指標") for n in notes):
            notes.append(
                f"部分指標在統計層級 {chosen.level} 下未收集(或 VM 於此期間無資料)。"
            )
        return PerfResult(
            vm_name=vm_name,
            vcpu_count=vcpu,
            start=start,
            end=end,
            interval_seconds=chosen.samplingPeriod,
            stats_level=chosen.level,
            metrics=ordered,
            unavailable=unavailable,
            notes=notes,
        )


# --- 單位換算 -----------------------------------------------------------------


def _unit(mdef: MetricDef, info: Any) -> str:
    if mdef.counter == "cpu.ready.summation":
        return "percent"
    key = info.unitInfo.key
    return {
        "percent": "percent",
        "kiloBytes": "MiB",
        "kiloBytesPerSecond": "KBps",
        "megaHertz": "MHz",
    }.get(key, key)


def _convert(
    mdef: MetricDef, info: Any, raw: int, interval: int, vcpu: int | None
) -> float:
    if mdef.counter == "cpu.ready.summation":
        # summation 為該取樣期間所有 vCPU 累計等待毫秒數,換算成每 vCPU 平均百分比。
        return raw / (interval * 1000) * 100 / max(vcpu or 1, 1)
    key = info.unitInfo.key
    if key == "percent":
        return raw / 100  # vCenter 以百分之一為單位
    if key == "kiloBytes":
        return raw / 1024
    return float(raw)


# --- 統計 ---------------------------------------------------------------------


def summarize(
    samples: list[tuple[datetime, float]], tz: tzinfo | None = None
) -> dict[str, Any]:
    """計算 min / max / avg / p95 / latest 與峰值時間。"""
    values = sorted(v for _, v in samples)
    peak_ts, peak = max(samples, key=lambda s: s[1])
    p95 = values[max(math.ceil(0.95 * len(values)) - 1, 0)]
    return {
        "min": round(values[0], 2),
        "max": round(peak, 2),
        "avg": round(sum(values) / len(values), 2),
        "p95": round(p95, 2),
        "latest": round(samples[-1][1], 2),
        "peak_time": peak_ts.astimezone(tz).isoformat(timespec="minutes"),
    }


def daily(
    samples: list[tuple[datetime, float]], tz: tzinfo | None = None
) -> list[dict[str, Any]]:
    """依指定時區(未指定時用本機時區)的日期分組,回傳每日平均、最大值與最大值發生時間。"""
    groups: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for ts, v in samples:
        local = ts.astimezone(tz)
        groups[local.date().isoformat()].append((local, v))
    result = []
    for day, pts in sorted(groups.items()):
        vals = [v for _, v in pts]
        peak_ts, peak = max(pts, key=lambda p: p[1])  # 同值取最早出現的時間
        result.append(
            {
                "date": day,
                "avg": round(sum(vals) / len(vals), 2),
                "max": round(peak, 2),
                "peak_time": peak_ts.isoformat(timespec="minutes"),
            }
        )
    return result
