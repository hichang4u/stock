"""H5 판정 기준 합성 점검.

스펙: docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md §4.5
판정 경로가 아니다. numpy 가 필요해 시스템 파이썬으로 실행한다:
    python scripts/h5_gate_calibration.py

스펙 §4.5의 두 표(가짜 통과율·검출력, G4 변형 비교)를 같은 시드로 재현한다.

KRX 실데이터는 한 줄도 읽지 않는다. 변동성·급변 확률은 KRX 결과와 무관한
일반 가정값이다. 보는 것: (1) 효과 0일 때 가짜 통과율 (2) 효과가 있을 때 검출력
(3) G1~G4 중 무엇이 병목인가.

구조: 거래 i 는 i+1~i+20 거래일을 보유한다 → 이웃 거래끼리 시장 구간이 겹친다.
거래 종목은 날마다 달라 고유 요인은 독립이다. 대조군은 동일가중 유니버스라
고유 요인이 상쇄돼 시장 구간 수익과 같다고 둔다.
"""

import numpy as np

SEED = 20260930
T = 20                 # 보유 거래일
L = 20                 # 블록 길이 (스펙 3절)
N = 3000               # 완료 거래 수 가정
COST = 0.0035          # 왕복 비용 (스펙 2-2)
SIG_M = 0.011          # 시장 일간 로그수익 표준편차 (연 ~17%)
SIG_I = 0.027          # 종목 고유 일간 로그수익 표준편차 (연 ~43%)
JUMP_P = 0.01          # 거래당 급변 확률 — 하락·상승 각각
JUMP_DOWN, JUMP_UP = 0.40, 1.60   # 기댓값 1이 되게 대칭 배치
SPLIT = (5 / 15.7, 10 / 15.7)     # 2011-15 / 2016-20 / 2021-26 거래 비중
B = 1000               # 시뮬레이션용 부트스트랩 (실제 판정은 10,000)
D = 400                # 시나리오당 합성 데이터셋 수

rng = np.random.default_rng(SEED)


def simulate(mkt20: float, edge20: float, sig_i: float = SIG_I):
    mu_m = np.log1p(mkt20) / T - SIG_M**2 / 2
    cs = np.concatenate([[0.0], np.cumsum(rng.normal(mu_m, SIG_M, N + T))])
    market = np.exp(cs[T + 1:N + T + 1] - cs[1:N + 1]) - 1          # 겹치는 20일 창

    mu_i = np.log1p(edge20) / T - sig_i**2 / 2
    idio = np.exp(rng.normal(mu_i * T, sig_i * np.sqrt(T), N)) - 1  # 거래마다 독립
    u = rng.random(N)
    jump = np.where(u < JUMP_P, JUMP_DOWN, np.where(u < 2 * JUMP_P, JUMP_UP, 1.0))

    gross = (1 + market) * (1 + idio) * jump - 1
    return gross - COST, gross - market          # 순수익 A, 초과수익 X


def block_lo(x):
    k = len(x) // L
    cs = np.concatenate([[0.0], np.cumsum(x)])
    sums = cs[L:] - cs[:-L]
    means = sums[rng.integers(0, len(sums), size=(B, k))].sum(axis=1) / (k * L)
    return np.percentile(means, 2.5)


def iid_lo(x):
    # n=3000 에서 백분위 부트스트랩과 사실상 같다
    return x.mean() - 1.96 * x.std(ddof=1) / np.sqrt(len(x))


def trimmed(x):
    k = int(np.ceil(len(x) * 0.01))
    return np.sort(x)[:-k].mean()


def thirds(x):
    a, b = int(len(x) * SPLIT[0]), int(len(x) * SPLIT[1])
    return x[:a], x[a:b], x[b:]


def gates(a, x):
    g1 = block_lo(a) > 0
    g2 = block_lo(x) > 0
    g3 = trimmed(a) > 0 and trimmed(x) > 0
    g4 = all(p.mean() > 0 for p in thirds(a)) and all(p.mean() > 0 for p in thirds(x))
    return g1, g2, g3, g4, g1 and g2 and g3 and g4


def rate(flags):
    return f"{100 * np.mean(flags):5.1f}%"


print("■ 1. 가짜 통과율 — 효과 0 (명목 2.5%)\n")
print("   G1 절대: 시장 20일 기대수익을 비용과 같게(0.35%) 둬 순수익 기댓값 0")
blk, iid, wb, wi = [], [], [], []
for _ in range(D):
    a, _x = simulate(mkt20=COST, edge20=0.0)
    lo_b, lo_i = block_lo(a), iid_lo(a)
    blk.append(lo_b > 0)
    iid.append(lo_i > 0)
    wb.append(a.mean() - lo_b)
    wi.append(a.mean() - lo_i)
print(f"     블록 부트스트랩 {rate(blk)}   (CI 반폭 평균 {100*np.mean(wb):.2f}%p)")
print(f"     독립 가정       {rate(iid)}   (CI 반폭 평균 {100*np.mean(wi):.2f}%p)")

print("\n   G2 초과: 돌파 효과 0")
blk, iid = [], []
for _ in range(D):
    _a, x = simulate(mkt20=0.003, edge20=0.0)
    blk.append(block_lo(x) > 0)
    iid.append(iid_lo(x) > 0)
print(f"     블록 부트스트랩 {rate(blk)}")
print(f"     독립 가정       {rate(iid)}")

print("\n■ 2. 검출력 — 조건별 통과율\n")
print(f"   {'시나리오':34} {'G1':>7} {'G2':>7} {'G3':>7} {'G4':>7} {'전체':>7}")
scenarios = [
    ("시장 +0.3%/20일, 효과 0",      0.003, 0.000, SIG_I),
    ("시장 +0.3%/20일, 효과 +0.5%",  0.003, 0.005, SIG_I),
    ("시장 +0.3%/20일, 효과 +1%",    0.003, 0.010, SIG_I),
    ("시장 +0.3%/20일, 효과 +2%",    0.003, 0.020, SIG_I),
    ("시장 +0.3%/20일, 효과 +3%",    0.003, 0.030, SIG_I),
    ("시장 0 (횡보장), 효과 +1%",    0.000, 0.010, SIG_I),
    ("시장 0 (횡보장), 효과 +2%",    0.000, 0.020, SIG_I),
    ("고변동 종목, 효과 +1%",        0.003, 0.010, 0.035),
    ("고변동 종목, 효과 +2%",        0.003, 0.020, 0.035),
]
for label, mkt, edge, sig in scenarios:
    res = np.array([gates(*simulate(mkt, edge, sig)) for _ in range(D)])
    cols = " ".join(f"{rate(res[:, j]):>7}" for j in range(5))
    print(f"   {label:34} {cols}")


def gates_both_vs_excess(a, x):
    """G4를 절대+초과에 걸 때와 초과에만 걸 때 — 다섯 조건 동시 통과 여부."""
    g1, g2, g3 = block_lo(a) > 0, block_lo(x) > 0, trimmed(a) > 0 and trimmed(x) > 0
    g4_both = (all(p.mean() > 0 for p in thirds(a))
               and all(p.mean() > 0 for p in thirds(x)))
    g4_excess = all(p.mean() > 0 for p in thirds(x))
    return (g1 and g2 and g3 and g4_both), (g1 and g2 and g3 and g4_excess)


print()
print("■ 3. G4 변형 비교 — 다섯 조건 동시 통과율 (스펙 §4.5 두 번째 표)")
print()
rng = np.random.default_rng(SEED)   # 이 표는 독립 실행과 같은 난수열로 재현한다
print(f"   {'시나리오':28} {'G4 절대+초과':>12} {'G4 초과만':>10}")
for label, mkt, edge in [
    ("효과 0 (가짜 통과율)", 0.003, 0.0),
    ("효과 +0.5%", 0.003, 0.005),
    ("효과 +1%", 0.003, 0.010),
    ("효과 +1.5%", 0.003, 0.015),
    ("효과 +2%", 0.003, 0.020),
    ("횡보장, 효과 +1%", 0.0, 0.010),
    ("횡보장, 효과 +1.5%", 0.0, 0.015),
]:
    res = np.array([gates_both_vs_excess(*simulate(mkt, edge)) for _ in range(D)])
    print(f"   {label:28} {rate(res[:, 0]):>12} {rate(res[:, 1]):>10}")


# ── 4. 등록 후 수정 검토 (2026-09-30, 데이터 보기 전) ─────────────────────────
# (가) G1 을 "CI 하한 > 0" 에서 "평균 > 0" 으로.
# (나) 대조군을 거래대금 맞춤(같은 날 거래대금 상위 10% 중 신고가가 아닌 종목)으로.
#
# 관심 종목(거래대금 상위)은 시장과 별개로 함께 움직이는 요인 G 를 갖는다고 둔다.
# attn20 = 그 요인의 20일 기대수익. 부호는 문헌에서도 갈린다 — 고거래량 프리미엄과
# 관심 뒤 부진이 둘 다 보고됐다. 거래(거래대금 1위)는 G 를 온전히 받고, 동일가중
# 대조군에는 관심 종목이 ATTN_SHARE 만큼만 섞인다. 맞춤 대조군은 G 를 그대로 품고
# MATCHED_N 종목 평균이라 고유 잡음이 조금 남는다. G4 는 채택안(초과만)으로 고정.

SIG_G = 0.006          # 관심 종목 공통 요인 일간 표준편차 (가정)
ATTN_SHARE = 0.10      # 동일가중 유니버스 중 관심 종목 비중
MATCHED_N = 150        # 맞춤 대조군 종목 수 (가정)


def simulate_attention(mkt20: float, edge20: float, attn20: float):
    mu_m = np.log1p(mkt20) / T - SIG_M**2 / 2
    cs_m = np.concatenate([[0.0], np.cumsum(rng.normal(mu_m, SIG_M, N + T))])
    market = np.exp(cs_m[T + 1:N + T + 1] - cs_m[1:N + 1]) - 1

    mu_g = np.log1p(attn20) / T - SIG_G**2 / 2
    cs_g = np.concatenate([[0.0], np.cumsum(rng.normal(mu_g, SIG_G, N + T))])
    group = np.exp(cs_g[T + 1:N + T + 1] - cs_g[1:N + 1]) - 1

    mu_i = np.log1p(edge20) / T - SIG_I**2 / 2
    idio = np.exp(rng.normal(mu_i * T, SIG_I * np.sqrt(T), N)) - 1
    u = rng.random(N)
    jump = np.where(u < JUMP_P, JUMP_DOWN, np.where(u < 2 * JUMP_P, JUMP_UP, 1.0))

    gross = (1 + market) * (1 + group) * (1 + idio) * jump - 1
    ctrl_equal = (1 + market) * (1 + ATTN_SHARE * group) - 1
    noise = rng.normal(0.0, SIG_I * np.sqrt(T) / np.sqrt(MATCHED_N), N)
    ctrl_matched = (1 + market) * (1 + group) * (1 + noise) - 1
    return gross - COST, gross - ctrl_equal, gross - ctrl_matched


def four_designs(a_net, x_eq, x_m):
    """현행 / (가) G1 평균 / (나) 맞춤 대조군 / (가)+(나) — 다섯 조건 동시 통과 여부."""
    lo_a, lo_eq, lo_m = block_lo(a_net), block_lo(x_eq), block_lo(x_m)
    mean_a = a_net.mean() > 0
    g3_eq = trimmed(a_net) > 0 and trimmed(x_eq) > 0
    g3_m = trimmed(a_net) > 0 and trimmed(x_m) > 0
    g4_eq = all(p.mean() > 0 for p in thirds(x_eq))
    g4_m = all(p.mean() > 0 for p in thirds(x_m))
    return (
        lo_a > 0 and lo_eq > 0 and g3_eq and g4_eq,
        mean_a and lo_eq > 0 and g3_eq and g4_eq,
        lo_a > 0 and lo_m > 0 and g3_m and g4_m,
        mean_a and lo_m > 0 and g3_m and g4_m,
    )


print()
print("■ 4. 등록 후 수정 검토 — 다섯 조건 동시 통과율")
print("   돌파 효과 0 인 행은 가짜 통과율, 나머지는 검출력이다.")
print()
rng = np.random.default_rng(SEED)
header = f"{'현행':>8} {'(가)G1평균':>10} {'(나)맞춤':>9} {'(가)+(나)':>9}"
print(f"   {'관심요인':>8} {'돌파효과':>8} {'시장':>6}  {header}")
for attn in (-0.01, 0.0, 0.01):
    for mkt, edge in ((0.0035, 0.0), (0.003, 0.005), (0.003, 0.01),
                      (0.003, 0.015), (0.003, 0.02)):
        res = np.array([four_designs(*simulate_attention(mkt, edge, attn))
                        for _ in range(D)])
        cols = " ".join(f"{rate(res[:, j]):>9}" for j in range(4))
        print(f"   {attn:>+8.1%} {edge:>+8.1%} {mkt:>+6.2%}  {cols}")
    print()
