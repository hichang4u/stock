"""장중 PC 상태 프로브 — 가짜 시계와 로컬 소켓만 쓴다."""

import json
import socket
from datetime import datetime, timedelta

from scripts.host_probe import (
    LATE_TICK_MS,
    append_line,
    cpu_percent,
    disk_busy_percent,
    heartbeat_gaps,
    parse_target,
    report,
    run,
    summarize,
    tcp_probe,
    worst_inside,
)


class FakeClock:
    """sleep이 부탁받은 만큼만 자되, 정해 둔 회차에는 더 오래 잔다."""

    def __init__(self, oversleep=None):
        self.t = 1000.0
        self.calls = 0
        self.oversleep = oversleep or {}
        self.start = datetime(2026, 11, 13, 9, 0, 0)

    def mono(self):
        return self.t

    def sleep(self, seconds):
        self.calls += 1
        self.t += seconds + self.oversleep.get(self.calls, 0.0)

    def wall(self):
        return self.start + timedelta(seconds=self.t - 1000.0)


def read_records(out_dir):
    (path,) = list(out_dir.glob("*.jsonl"))
    raw = path.read_bytes()
    assert b"\r" not in raw
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def run_fake(tmp_path, clock, **kwargs):
    args = dict(
        out_dir=tmp_path, until=clock.start + timedelta(hours=1), interval=1.0, sample_every=5,
        net_every=0, targets=[], mono=clock.mono, wall=clock.wall, sleep=clock.sleep,
        read_cpu=lambda: None, read_disk=lambda: None,
    )
    args.update(kwargs)
    run(**args)
    return read_records(tmp_path)


def test_parse_target_splits_host_and_port():
    assert parse_target("openapivts.koreainvestment.com:29443") == (
        "openapivts.koreainvestment.com", 29443)


def test_cpu_percent_is_busy_share_of_kernel_plus_user():
    # kernel 시간에는 idle이 들어 있다 (GetSystemTimes)
    assert cpu_percent((100, 300, 100), (150, 400, 200)) == 75.0
    assert cpu_percent((100, 300, 100), (100, 300, 100)) is None


def test_disk_busy_percent_is_non_idle_share_of_query_time():
    assert disk_busy_percent((0, 0), (250, 1000)) == 75.0
    assert disk_busy_percent((5, 10), (5, 10)) is None
    # 유휴 시간이 조회 간격보다 조금 크게 나올 때가 있다 (2026-11-13 실측 -2.2)
    assert disk_busy_percent((0, 0), (1022, 1000)) == 0.0


def test_append_line_writes_one_lf_line_and_returns_elapsed_ms(tmp_path):
    path = tmp_path / "sub" / "x.jsonl"
    ms = append_line(path, {"kind": "sample", "이름": "값"})
    append_line(path, {"kind": "stop"})

    raw = path.read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    assert json.loads(raw.decode("utf-8").splitlines()[0]) == {"kind": "sample", "이름": "값"}
    assert ms >= 0.0


def test_tcp_probe_measures_a_real_local_connect():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        got = tcp_probe("127.0.0.1", port, timeout=2.0)

    assert got["result"] == "ok" and got["target"] == f"127.0.0.1:{port}"
    assert got["connect_ms"] >= 0.0 and got["dns_ms"] >= 0.0


def test_tcp_probe_reports_refused_timeout_and_dns_failure():
    def refuse(addr, timeout):
        raise ConnectionRefusedError()

    def hang(addr, timeout):
        raise TimeoutError()

    def no_dns(host, port, **kwargs):
        raise socket.gaierror(11001, "getaddrinfo failed")

    assert tcp_probe("127.0.0.1", 9, timeout=1.0, connect=refuse)["result"] == "refused"
    assert tcp_probe("127.0.0.1", 9, timeout=1.0, connect=hang)["result"] == "timeout"
    failed = tcp_probe("no.such.host", 443, timeout=1.0, resolve=no_dns)
    assert failed["result"] == "dns_fail" and failed["connect_ms"] is None


def test_run_writes_a_late_record_when_the_timer_fires_late(tmp_path):
    clock = FakeClock(oversleep={3: 40.0})

    records = run_fake(tmp_path, clock, max_ticks=10)

    assert [r["kind"] for r in records] == ["start", "late", "sample", "sample", "stop"]
    late = records[1]
    assert late["lag_ms"] == 40000.0 and LATE_TICK_MS == 1000
    first, second = records[2], records[3]
    assert (first["ticks"], first["max_lag_ms"], first["late_ticks"]) == (5, 40000.0, 1)
    assert (second["ticks"], second["max_lag_ms"], second["late_ticks"]) == (5, 0.0, 0)
    assert records[-1]["reason"] == "max_ticks"


def test_one_stall_counts_as_one_late_tick_not_a_catch_up_burst(tmp_path):
    clock = FakeClock(oversleep={2: 30.0})

    records = run_fake(tmp_path, clock, max_ticks=5)

    assert sum(1 for r in records if r["kind"] == "late") == 1


def test_run_stops_at_the_until_time(tmp_path):
    clock = FakeClock()

    records = run_fake(tmp_path, clock, until=clock.start + timedelta(seconds=7))

    assert records[-1] == {"ts": records[-1]["ts"], "kind": "stop", "reason": "until", "ticks": 7}
    assert sum(1 for r in records if r["kind"] == "sample") == 1


def test_run_records_cpu_and_disk_between_samples(tmp_path):
    clock = FakeClock()
    cpu = iter([(0, 0, 0), (50, 100, 100), (100, 200, 200)])
    disk = iter([(0, 0, 3), (900, 1000, 1), (1000, 2000, 0)])

    records = run_fake(tmp_path, clock, max_ticks=10, read_cpu=lambda: next(cpu),
                       read_disk=lambda: next(disk))

    samples = [r for r in records if r["kind"] == "sample"]
    assert [s["cpu_pct"] for s in samples] == [75.0, 75.0]
    assert [s["disk_busy_pct"] for s in samples] == [10.0, 90.0]
    assert [s["disk_queue"] for s in samples] == [1, 0]


def test_run_writes_tcp_results_from_the_network_round(tmp_path):
    clock = FakeClock()

    def probe(host, port, timeout):
        return {"target": f"{host}:{port}", "dns_ms": 1.0, "connect_ms": 20.0, "result": "ok"}

    records = run_fake(tmp_path, clock, max_ticks=4, net_every=2, targets=[("h", 1)],
                       probe=probe, threaded=False)

    tcp = [r for r in records if r["kind"] == "tcp"]
    assert len(tcp) == 2 and tcp[0]["target"] == "h:1" and tcp[0]["connect_ms"] == 20.0


def test_summarize_counts_late_ticks_slow_writes_and_tcp_results():
    records = [
        {"ts": "2026-11-13T09:00:10.000+09:00", "kind": "sample", "ticks": 10, "max_lag_ms": 3.0,
         "late_ticks": 0, "write_ms": 2.0, "cpu_pct": 10.0, "disk_busy_pct": 5.0},
        {"ts": "2026-11-13T09:00:50.000+09:00", "kind": "late", "lag_ms": 31000.0},
        {"ts": "2026-11-13T09:00:51.000+09:00", "kind": "sample", "ticks": 10,
         "max_lag_ms": 31000.0, "late_ticks": 1, "write_ms": 1500.0, "cpu_pct": 100.0,
         "disk_busy_pct": 99.0},
        {"ts": "2026-11-13T09:00:30.000+09:00", "kind": "tcp", "target": "a:1", "dns_ms": 1.0,
         "connect_ms": 20.0, "result": "ok"},
        {"ts": "2026-11-13T09:01:00.000+09:00", "kind": "tcp", "target": "a:1", "dns_ms": 1.0,
         "connect_ms": 4000.0, "result": "ok"},
        {"ts": "2026-11-13T09:01:30.000+09:00", "kind": "tcp", "target": "a:1", "dns_ms": None,
         "connect_ms": None, "result": "timeout"},
    ]

    got = summarize(records)

    assert got["samples"] == 2 and got["ticks"] == 20
    assert got["late_ticks"] == 1 and got["max_lag_ms"] == 31000.0
    assert got["slow_writes"] == 1 and got["max_write_ms"] == 1500.0
    assert got["max_cpu_pct"] == 100.0 and got["max_disk_busy_pct"] == 99.0
    assert got["tcp"]["a:1"] == {"n": 3, "ok": 2, "failed": {"timeout": 1}, "slow": 1,
                                 "median_ms": 2010.0, "max_ms": 4000.0}


def test_heartbeat_gaps_lists_intervals_over_the_limit():
    def hb(ts):
        return {"ts": f"2026-11-13T{ts}+09:00", "event": "F4_HEARTBEAT"}

    rows = [hb("10:00:00.000"), hb("10:00:30.100"), {"ts": "x", "event": "OTHER"},
            hb("10:01:40.100"), hb("10:02:10.200")]

    assert heartbeat_gaps(rows) == [("2026-11-13T10:00:30.100+09:00",
                                     "2026-11-13T10:01:40.100+09:00", 70.0)]


def test_worst_inside_picks_the_probe_values_within_an_interval():
    records = [
        {"ts": "2026-11-13T10:00:20.000+09:00", "kind": "sample", "max_lag_ms": 9000.0,
         "write_ms": 1.0, "cpu_pct": 5.0, "disk_busy_pct": 1.0},
        {"ts": "2026-11-13T10:00:50.000+09:00", "kind": "sample", "max_lag_ms": 12.0,
         "write_ms": 800.0, "cpu_pct": 97.0, "disk_busy_pct": 100.0},
        {"ts": "2026-11-13T10:01:00.000+09:00", "kind": "tcp", "target": "a:1",
         "connect_ms": 3500.0, "result": "ok"},
        {"ts": "2026-11-13T10:01:20.000+09:00", "kind": "tcp", "target": "a:1", "connect_ms": None,
         "result": "timeout"},
    ]

    got = worst_inside(records, "2026-11-13T10:00:30.100+09:00", "2026-11-13T10:01:40.100+09:00")

    assert got == {"samples": 1, "max_lag_ms": 12.0, "max_write_ms": 800.0,
                   "max_cpu_pct": 97.0, "max_disk_busy_pct": 100.0, "tcp_n": 2,
                   "tcp_failed": 1, "max_connect_ms": 3500.0}


def test_report_counts_late_heartbeats_even_without_probe_records(tmp_path, capsys):
    logs = tmp_path / "prod" / "data" / "logs"
    logs.mkdir(parents=True)
    lines = [
        {"ts": "2026-11-13T10:00:00.000+09:00", "event": "F4_HEARTBEAT"},
        {"ts": "2026-11-13T10:01:10.000+09:00", "event": "F4_HEARTBEAT"},
    ]
    (logs / "20261113.jsonl").write_bytes(
        "".join(json.dumps(x) + "\n" for x in lines).encode("utf-8")
    )

    report("20261113", tmp_path / "probe", tmp_path / "prod")

    out = capsys.readouterr().out
    assert "프로브 기록 없음" in out
    assert "봇 하트비트 간격 > 35초: 1건" in out and "10:00:00 -> 10:01:10 70.0초" in out


def test_summarize_rounds_the_median_connect_time():
    records = [
        {"ts": "2026-11-13T09:00:30.000+09:00", "kind": "tcp", "target": "a:1", "connect_ms": 0.1,
         "result": "ok"},
        {"ts": "2026-11-13T09:01:00.000+09:00", "kind": "tcp", "target": "a:1", "connect_ms": 0.2,
         "result": "ok"},
    ]

    assert summarize(records)["tcp"]["a:1"]["median_ms"] == 0.2  # 0.15000000000000002 이 아니다
