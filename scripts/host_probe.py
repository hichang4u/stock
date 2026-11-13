"""장중 PC 상태 프로브 — 봇 밖에서 타이머 지연·파일 쓰기·CPU·디스크·TCP 연결 시간을 남긴다.

`docs/EVENT_LOOP_DELAY_FOLLOWUP_20261112.md` 6절, `docs/REST_LATENCY_FOLLOWUP_20261111.md` 9절.
봇의 하트비트가 늦거나 REST가 느린 날, 그때 이 PC와 회선이 어땠는지 가르려고 둔다.

- 봇과 다른 프로세스다. 운영 트리와 운영 매매 동작을 건드리지 않는다.
- KIS API를 부르지 않는다. KIS 호스트에는 TCP 연결만 하고 바로 닫는다(인증·요청 없음).
- 자기 기록 파일(`data/host_probe/<YYYYMMDD>.jsonl`)만 쓴다.

기준값은 2026-11-13에 미리 정했다. 결과를 보고 바꾸지 않는다.

    python scripts/host_probe.py                      # 15:35까지 잰다
    python scripts/host_probe.py --report 20261113 --root D:/Private/stock-prod
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import socket
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

LATE_TICK_MS = 1000  # 1초 타이머가 이만큼 넘게 늦으면 늦은 틱
SLOW_WRITE_MS = 1000  # 한 줄 쓰기가 이만큼 넘게 걸리면 느린 쓰기
SLOW_CONNECT_MS = 3000  # TCP 연결이 이만큼 넘게 걸리면 느린 연결
HEARTBEAT_LIMIT_SEC = 35.0  # 봇 하트비트(30초) 간격이 이만큼 넘으면 늦은 하트비트

DEFAULT_TARGETS = (
    "openapivts.koreainvestment.com:29443",  # KIS 모의 REST
    "ops.koreainvestment.com:31000",  # KIS 모의 WS
    "api.telegram.org:443",  # KIS가 아닌 호스트
    "1.1.1.1:443",  # DNS를 거치지 않는 호스트
)

Record = dict[str, Any]
CpuTimes = tuple[int, int, int]  # (idle, kernel, user) — kernel에 idle이 들어 있다
DiskTimes = tuple[int, int, int]  # (idle, query, queue depth)


def parse_target(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host, int(port)


def cpu_percent(prev: CpuTimes, cur: CpuTimes) -> float | None:
    total = (cur[1] - prev[1]) + (cur[2] - prev[2])
    if total <= 0:
        return None
    return round(100.0 * (total - (cur[0] - prev[0])) / total, 1)


def disk_busy_percent(prev: tuple[int, int], cur: tuple[int, int]) -> float | None:
    span = cur[1] - prev[1]
    if span <= 0:
        return None
    return max(0.0, round(100.0 * (span - (cur[0] - prev[0])) / span, 1))


def append_line(path: Path, record: Record) -> float:
    """봇 로거처럼 줄마다 열고 덧붙이고 닫는다. 걸린 시간(ms)을 돌려준다."""
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    started = time.perf_counter()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        f.write(data)
    return round((time.perf_counter() - started) * 1000, 1)


def _connect(addr: tuple[str, int], timeout: float) -> None:
    socket.create_connection(addr, timeout=timeout).close()


def tcp_probe(
    host: str,
    port: int,
    timeout: float,
    resolve: Callable[..., Any] = socket.getaddrinfo,
    connect: Callable[[tuple[str, int], float], None] = _connect,
) -> Record:
    """이름 풀이와 TCP 연결 시간을 따로 잰다. 연결되면 바로 닫는다."""
    out: Record = {"target": f"{host}:{port}", "dns_ms": None, "connect_ms": None}
    started = time.perf_counter()
    try:
        infos = resolve(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        addr = (str(infos[0][4][0]), port)
    except OSError as exc:
        return {**out, "result": "dns_fail", "error": repr(exc)[:80]}
    out["dns_ms"] = round((time.perf_counter() - started) * 1000, 1)
    started = time.perf_counter()
    try:
        connect(addr, timeout)
        result = "ok"
    except ConnectionRefusedError:
        result = "refused"  # 거절도 왕복은 됐다는 뜻이라 시간을 남긴다
    except TimeoutError:
        return {**out, "result": "timeout"}
    except OSError as exc:
        return {**out, "result": "error", "error": repr(exc)[:80]}
    out["connect_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {**out, "result": result}


class _Filetime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class _DiskPerformance(ctypes.Structure):
    _fields_ = [
        ("BytesRead", ctypes.c_longlong),
        ("BytesWritten", ctypes.c_longlong),
        ("ReadTime", ctypes.c_longlong),
        ("WriteTime", ctypes.c_longlong),
        ("IdleTime", ctypes.c_longlong),
        ("ReadCount", ctypes.c_ulong),
        ("WriteCount", ctypes.c_ulong),
        ("QueueDepth", ctypes.c_ulong),
        ("SplitCount", ctypes.c_ulong),
        ("QueryTime", ctypes.c_longlong),
        ("StorageDeviceNumber", ctypes.c_ulong),
        ("StorageManagerName", ctypes.c_wchar * 8),
    ]


def system_times() -> CpuTimes | None:
    """PC 전체 CPU 시간 (GetSystemTimes). Windows가 아니면 None."""
    if sys.platform != "win32":
        return None
    idle, kernel, user = _Filetime(), _Filetime(), _Filetime()
    ok = ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )
    if not ok:
        return None
    return (
        (idle.high << 32) | idle.low,
        (kernel.high << 32) | kernel.low,
        (user.high << 32) | user.low,
    )


def disk_times(drive: str) -> DiskTimes | None:
    """드라이브의 유휴 시간·조회 시각·대기열 (IOCTL_DISK_PERFORMANCE). 못 읽으면 None."""
    if sys.platform != "win32" or not drive:
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        perf = _DiskPerformance()
        returned = ctypes.c_ulong(0)
        ok = kernel32.DeviceIoControl(
            handle, 0x70020, None, 0, ctypes.byref(perf), ctypes.sizeof(perf),
            ctypes.byref(returned), None,
        )
        if not ok:
            return None
        return int(perf.IdleTime), int(perf.QueryTime), int(perf.QueueDepth)
    finally:
        kernel32.CloseHandle(handle)


def run(
    out_dir: Path,
    until: datetime,
    interval: float = 1.0,
    sample_every: int = 10,
    net_every: int = 30,
    targets: Iterable[tuple[str, int]] = (),
    net_timeout: float = 10.0,
    max_ticks: int | None = None,
    mono: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    read_cpu: Callable[[], CpuTimes | None] = system_times,
    read_disk: Callable[[], DiskTimes | None] | None = None,
    probe: Callable[[str, int, float], Record] = tcp_probe,
    threaded: bool = True,
) -> None:
    """interval마다 깨어 얼마나 늦게 깼는지 재고, 주기적으로 표본과 TCP 결과를 쓴다."""
    out_dir = Path(out_dir)
    targets = list(targets)
    if read_disk is None:
        drive = out_dir.resolve().drive
        read_disk = lambda: disk_times(drive)  # noqa: E731
    results: queue.Queue[Record] = queue.Queue()
    worker: threading.Thread | None = None
    max_write = 0.0

    def write(kind: str, **fields: Any) -> None:
        nonlocal max_write
        now = wall()
        record = {"ts": now.astimezone().isoformat(timespec="milliseconds"), "kind": kind, **fields}
        max_write = max(max_write, append_line(out_dir / f"{now:%Y%m%d}.jsonl", record))

    def net_round() -> None:
        for host, port in targets:
            results.put(probe(host, port, net_timeout))

    write("start", pid=os.getpid(), interval=interval, sample_every=sample_every,
          net_every=net_every, targets=[f"{h}:{p}" for h, p in targets])
    prev_cpu, prev_disk = read_cpu(), read_disk()
    ticks = period_ticks = late_ticks = 0
    max_lag = 0.0
    reason = "until"
    try:
        deadline = mono() + interval
        while True:
            sleep(max(0.0, deadline - mono()))
            woke = mono()
            lag_ms = round(max(0.0, woke - deadline) * 1000, 1)
            deadline = woke + interval  # 한 번 멈춘 것은 한 번만 센다
            ticks += 1
            period_ticks += 1
            max_lag = max(max_lag, lag_ms)
            if lag_ms >= LATE_TICK_MS:
                late_ticks += 1
                write("late", lag_ms=lag_ms)
            if ticks % sample_every == 0:
                cpu, disk = read_cpu(), read_disk()
                write(
                    "sample", ticks=period_ticks, max_lag_ms=max_lag, late_ticks=late_ticks,
                    write_ms=max_write,
                    cpu_pct=cpu_percent(prev_cpu, cpu) if prev_cpu and cpu else None,
                    disk_busy_pct=(
                        disk_busy_percent(prev_disk[:2], disk[:2]) if prev_disk and disk else None
                    ),
                    disk_queue=disk[2] if disk else None,
                )
                prev_cpu, prev_disk = cpu, disk
                period_ticks = late_ticks = 0
                max_lag = max_write = 0.0
            if net_every and targets and ticks % net_every == 0:
                if not threaded:
                    net_round()
                elif worker is None or not worker.is_alive():  # 앞 회차가 안 끝났으면 건너뛴다
                    worker = threading.Thread(target=net_round, daemon=True)
                    worker.start()
            while not results.empty():
                write("tcp", **results.get())
            if max_ticks is not None and ticks >= max_ticks:
                reason = "max_ticks"
                break
            if wall() >= until:
                break
    except KeyboardInterrupt:
        reason = "interrupt"
    write("stop", reason=reason, ticks=ticks)


def load_jsonl(path: Path) -> list[Record]:
    rows: list[Record] = []
    if not path.exists():
        return rows
    for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _max(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return max(kept) if kept else None


def summarize(records: list[Record]) -> Record:
    samples = [r for r in records if r.get("kind") == "sample"]
    lates = [r for r in records if r.get("kind") == "late"]
    tcp: dict[str, Record] = {}
    for r in (r for r in records if r.get("kind") == "tcp"):
        t = tcp.setdefault(str(r.get("target")), {"n": 0, "ok": 0, "failed": Counter(), "ms": []})
        t["n"] += 1
        if r.get("result") == "ok":
            t["ok"] += 1
        else:
            t["failed"][str(r.get("result"))] += 1
        if r.get("connect_ms") is not None:
            t["ms"].append(float(r["connect_ms"]))
    return {
        "samples": len(samples),
        "first": samples[0]["ts"] if samples else None,
        "last": samples[-1]["ts"] if samples else None,
        "ticks": sum(int(s.get("ticks", 0)) for s in samples),
        "late_ticks": sum(int(s.get("late_ticks", 0)) for s in samples),
        "max_lag_ms": _max(
            [s.get("max_lag_ms") for s in samples] + [r.get("lag_ms") for r in lates]
        ),
        "slow_writes": sum(1 for s in samples if (s.get("write_ms") or 0) >= SLOW_WRITE_MS),
        "max_write_ms": _max(s.get("write_ms") for s in samples),
        "max_cpu_pct": _max(s.get("cpu_pct") for s in samples),
        "max_disk_busy_pct": _max(s.get("disk_busy_pct") for s in samples),
        "tcp": {
            target: {
                "n": t["n"], "ok": t["ok"], "failed": dict(t["failed"]),
                "slow": sum(1 for ms in t["ms"] if ms >= SLOW_CONNECT_MS),
                "median_ms": round(statistics.median(t["ms"]), 1) if t["ms"] else None,
                "max_ms": max(t["ms"]) if t["ms"] else None,
            }
            for target, t in tcp.items()
        },
    }


def heartbeat_gaps(
    rows: list[Record], limit: float = HEARTBEAT_LIMIT_SEC
) -> list[tuple[str, str, float]]:
    """봇 로그에서 `F4_HEARTBEAT` 간격이 limit을 넘은 곳."""
    beats = [str(r["ts"]) for r in rows if r.get("event") == "F4_HEARTBEAT"]
    out = []
    for a, b in zip(beats, beats[1:]):
        gap = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        if gap > limit:
            out.append((a, b, round(gap, 1)))
    return out


def worst_inside(records: list[Record], start: str, end: str) -> Record:
    """start < ts <= end 인 프로브 기록의 가장 나쁜 값."""
    t0, t1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
    inside = [r for r in records if "ts" in r and t0 < datetime.fromisoformat(r["ts"]) <= t1]
    samples = [r for r in inside if r.get("kind") == "sample"]
    tcp = [r for r in inside if r.get("kind") == "tcp"]
    return {
        "samples": len(samples),
        "max_lag_ms": _max(
            [s.get("max_lag_ms") for s in samples]
            + [r.get("lag_ms") for r in inside if r.get("kind") == "late"]
        ),
        "max_write_ms": _max(s.get("write_ms") for s in samples),
        "max_cpu_pct": _max(s.get("cpu_pct") for s in samples),
        "max_disk_busy_pct": _max(s.get("disk_busy_pct") for s in samples),
        "tcp_n": len(tcp),
        "tcp_failed": sum(1 for r in tcp if r.get("result") != "ok"),
        "max_connect_ms": _max(r.get("connect_ms") for r in tcp),
    }


def report(date: str, out_dir: Path, root: Path | None) -> None:
    records = load_jsonl(out_dir / f"{date}.jsonl")
    if records:
        _print_summary(date, records)
    else:
        print(f"프로브 기록 없음: {out_dir / (date + '.jsonl')}")
    if root is None:
        return
    gaps = heartbeat_gaps(load_jsonl(root / "data" / "logs" / f"{date}.jsonl"))
    print(f"봇 하트비트 간격 > {HEARTBEAT_LIMIT_SEC:g}초: {len(gaps)}건")
    for a, b, gap in gaps:
        w = worst_inside(records, a, b)
        print(f"  {a[11:19]} -> {b[11:19]} {gap}초 | 프로브 표본 {w['samples']}"
              f", 타이머 {w['max_lag_ms']}ms, 쓰기 {w['max_write_ms']}ms, CPU {w['max_cpu_pct']}%"
              f", 디스크 {w['max_disk_busy_pct']}%, TCP {w['tcp_n']}회 중 실패 {w['tcp_failed']}"
              f", 연결 최대 {w['max_connect_ms']}ms")


def _print_summary(date: str, records: list[Record]) -> None:
    s = summarize(records)
    runs = sum(1 for r in records if r.get("kind") == "start")
    span = f"{str(s['first'])[11:19]} ~ {str(s['last'])[11:19]}"
    print(f"프로브 {date} | 표본 {s['samples']} ({span}) | 틱 {s['ticks']} | 실행 {runs}회")
    print(f"늦은 틱(>= {LATE_TICK_MS}ms) {s['late_ticks']} | 최대 지연 {s['max_lag_ms']}ms")
    print(f"느린 쓰기(>= {SLOW_WRITE_MS}ms) {s['slow_writes']} | 최대 쓰기 {s['max_write_ms']}ms")
    print(f"CPU 최대 {s['max_cpu_pct']}% | 디스크 사용 최대 {s['max_disk_busy_pct']}%")
    for target, t in s["tcp"].items():
        print(f"TCP {target} | {t['n']}회, 성공 {t['ok']}, 실패 {t['failed']}"
              f" | 느린 연결(>= {SLOW_CONNECT_MS}ms) {t['slow']}"
              f" | 중앙 {t['median_ms']}ms, 최대 {t['max_ms']}ms")
    for r in records:
        if r.get("kind") == "late":
            print(f"  늦은 틱 {r['ts'][11:23]} {r['lag_ms']}ms")


def main() -> None:
    default_out = Path(__file__).resolve().parents[1] / "data" / "host_probe"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=default_out)
    parser.add_argument("--until", default="15:35", help="끝낼 시각 HH:MM (오늘)")
    parser.add_argument("--tcp", action="append", help="host:port (여러 번). 없으면 기본 목록")
    parser.add_argument("--report", metavar="YYYYMMDD", help="그날 기록을 요약한다")
    parser.add_argument("--root", type=Path, help="봇 트리. 주면 하트비트 지연 구간과 대조한다")
    args = parser.parse_args()
    if args.report:
        report(args.report, args.out, args.root)
        return
    hour, minute = (int(x) for x in args.until.split(":"))
    until = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    run(args.out, until, targets=[parse_target(t) for t in (args.tcp or DEFAULT_TARGETS)])


if __name__ == "__main__":
    main()
