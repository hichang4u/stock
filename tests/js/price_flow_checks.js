// 가격흐름 차트 순수 로직 검증 — app.js에서 함수를 추출해 실제 실행한다.
// 실행: node tests\js\price_flow_checks.js
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(
  path.join(__dirname, '..', '..', 'docs', 'html', 'assets', 'app.js'), 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  const open = src.indexOf('{', start);
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`${name} unbalanced`);
}

const PRICE_FLOW_VIEW_MIN = 20;
let _priceFlowTicks = [];
let _priceFlow = [];
eval(extract('parseTradeMarks'));
eval(extract('priceFlowViewWindow'));
eval(extract('downsampleFlowPoints'));

const MIN = 60 * 1000;
const T0 = Date.parse('2026-07-14T09:10:30+09:00');
let failures = 0;
const check = (label, cond) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`);
  if (!cond) failures++;
};

// 1) 보유 중 · 진입 5분 경과 → 창은 [진입, 지금]
_priceFlowTicks = [{ts: T0, price: 10000}, {ts: T0 + 5 * MIN, price: 10100}];
const realNow = Date.now;
Date.now = () => T0 + 5 * MIN;
let w = priceFlowViewWindow({position_status: 'HOLDING', entry_at: '2026-07-14T09:10:30+09:00'}, []);
check('보유 5분: 시작=진입시각', w.startTs === T0);
check('보유 5분: 끝=지금', w.endTs === T0 + 5 * MIN);

// 2) 보유 중 · 진입 40분 경과 → 최근 20분 슬라이딩
Date.now = () => T0 + 40 * MIN;
w = priceFlowViewWindow({position_status: 'HOLDING', entry_at: '2026-07-14T09:10:30+09:00'}, []);
check('보유 40분: 창 크기=20분', w.endTs - w.startTs === 20 * MIN);
check('보유 40분: 끝=지금', w.endTs === T0 + 40 * MIN);

// 3) 청산 후 → 진입~마지막 체결 고정(양끝 30초 여백), 매도 마커 포함
const marks = parseTradeMarks({
  ticker: '005930',
  trade_marks: [
    {order_type: 'BUY', order_phase: 'FIRST_BUY', ticker: '005930',
      fill_price: 10000, filled_at: '2026-07-14T09:10:31+09:00'},
    {order_type: 'BUY', order_phase: 'PYRAMID_BUY', ticker: '005930',
      fill_price: 10050, filled_at: '2026-07-14T09:10:45+09:00'},
    {order_type: 'SELL', order_phase: 'CLOSE_SELL', ticker: '005930',
      fill_price: 10200, filled_at: '2026-07-14T09:55:00+09:00'},
  ],
});
check('마커 파싱: 3건', marks.length === 3);
check('마커 파싱: 매도 가격', marks[2].price === 10200 && marks[2].side === 'SELL');

w = priceFlowViewWindow(
  {position_status: 'CLOSED', entry_at: '2026-07-14T09:10:30+09:00'}, marks, 'trade');
const sellTs = Date.parse('2026-07-14T09:55:00+09:00');
check('청산 후 거래 전체: 시작 <= 진입-여백', w.startTs <= T0 - 29 * 1000);
check('청산 후 거래 전체: 끝 >= 매도체결+여백', w.endTs >= sellTs + 29 * 1000);

// 3-1) 청산 후 기본값('recent')은 오른쪽 끝에 붙는 20분 슬라이딩 창.
//      사후 관측이 15:15까지 이어져 '거래 전체'가 하루로 늘어나는 것을 막는다.
//      2026-08-28에 09:01 청산 후 11:25 시점 구간이 이미 2시간 24분이었다.
_priceFlowTicks = [{ts: T0, price: 10000}, {ts: T0 + 140 * MIN, price: 10500}];
w = priceFlowViewWindow(
  {position_status: 'CLOSED', entry_at: '2026-07-14T09:10:30+09:00'}, marks);
check('청산 후 최근창: 크기=20분', w.endTs - w.startTs === 20 * MIN);
check('청산 후 최근창: 오른쪽 끝이 마지막 데이터', w.endTs === T0 + 140 * MIN + 30 * 1000);
check('청산 후 최근창: 진입 시각을 지나 스크롤됐다', w.startTs > T0);
const wide = priceFlowViewWindow(
  {position_status: 'CLOSED', entry_at: '2026-07-14T09:10:30+09:00'}, marks, 'trade');
check('같은 데이터에서 거래 전체는 훨씬 넓다', wide.endTs - wide.startTs > 2 * (20 * MIN));

// 3-2) 청산 직후처럼 구간이 20분보다 짧으면 최근창도 전체를 그대로 보여준다
_priceFlowTicks = [{ts: T0, price: 10000}, {ts: T0 + 5 * MIN, price: 10100}];
const shortMarks = parseTradeMarks({ticker: '005930', trade_marks: [
  {order_type: 'BUY', order_phase: 'FIRST_BUY', ticker: '005930',
    fill_price: 10000, filled_at: '2026-07-14T09:10:31+09:00'},
  {order_type: 'SELL', order_phase: 'CLOSE_SELL', ticker: '005930',
    fill_price: 10200, filled_at: '2026-07-14T09:13:00+09:00'},
]});
w = priceFlowViewWindow(
  {position_status: 'CLOSED', entry_at: '2026-07-14T09:10:30+09:00'}, shortMarks);
check('짧은 거래: 최근창이 진입 앞을 잘라내지 않는다', w.startTs <= T0 - 29 * 1000);

// 3-3) 보유 중 '거래 전체'는 진입부터 지금까지
_priceFlowTicks = [{ts: T0, price: 10000}];
Date.now = () => T0 + 40 * MIN;
w = priceFlowViewWindow(
  {position_status: 'HOLDING', entry_at: '2026-07-14T09:10:30+09:00'}, [], 'trade');
check('보유 거래 전체: 시작=진입시각', w.startTs === T0);
check('보유 거래 전체: 40분 전부', w.endTs - w.startTs === 40 * MIN);
_priceFlowTicks = [{ts: T0, price: 10000}, {ts: T0 + 5 * MIN, price: 10100}];

// 4) 같은 날 다른 종목의 체결은 현재 종목 마커에서 제외
const filtered = parseTradeMarks({ticker: '005930', trade_marks: [
  {order_type: 'BUY', order_phase: 'FIRST_BUY', ticker: '000660',
    fill_price: 9000, filled_at: '2026-07-14T09:10:31+09:00'},
  {order_type: 'BUY', order_phase: 'FIRST_BUY', ticker: '005930',
    fill_price: 10000, filled_at: '2026-07-14T09:10:31+09:00'},
]});
check('타 종목 마커 필터링: 현재 종목만 유지', filtered.length === 1);
check('타 종목 마커 필터링: 남은 마커는 현재 종목', filtered[0].ticker === '005930');

// 5) 재시작 후 CLOSED(tick 없음) → 마커만으로 창 성립
_priceFlowTicks = []; _priceFlow = [];
w = priceFlowViewWindow(
  {position_status: 'CLOSED', entry_at: '2026-07-14T09:10:30+09:00'}, marks, 'trade');
check('재시작 CLOSED: 매도까지 포함', w.endTs >= sellTs);

// 6) 다운샘플링: 크기 제한 + 스파이크(min/max) 보존 + 시간순 유지
const big = [];
for (let i = 0; i < 10000; i++) {
  big.push({ts: T0 + i * 100, price: 10000 + Math.round(200 * Math.sin(i / 50))});
}
big[7321] = {ts: big[7321].ts, price: 12000};  // 스파이크 최고가
big[2222] = {ts: big[2222].ts, price: 9000};   // 스파이크 최저가
const ds = downsampleFlowPoints(big, 700);
check('다운샘플: 크기 <= 1400', ds.length <= 1400);
check('다운샘플: 최고 스파이크 보존', ds.some(p => p.price === 12000));
check('다운샘플: 최저 스파이크 보존', ds.some(p => p.price === 9000));
check('다운샘플: 시간순 유지', ds.every((p, i) => i === 0 || p.ts >= ds[i - 1].ts));
const small = big.slice(0, 100);
check('다운샘플: 소량은 원본 그대로', downsampleFlowPoints(small, 700) === small);

// 7) VI 이벤트 파싱 — 발동~해제 밴드, 진행 중(미해제)은 end=0
eval(extract('parseViEvents'));
const viParsed = parseViEvents({vi_events: [
  {ts: '2026-07-16T09:13:45+09:00', released_ts: '2026-07-16T09:15:50+09:00', vi_prc: '7700'},
  {ts: '2026-07-16T09:40:00+09:00', released_ts: null, vi_prc: '8400'},
]});
check('VI 파싱: 2건', viParsed.length === 2);
check('VI 파싱: 발동 ts', viParsed[0].start === Date.parse('2026-07-16T09:13:45+09:00'));
check('VI 파싱: 해제 ts', viParsed[0].end === Date.parse('2026-07-16T09:15:50+09:00'));
check('VI 파싱: 미해제는 end=0', viParsed[1].end === 0);
check('VI 파싱: vi_events 없음 → []', parseViEvents({}).length === 0);
check('VI 파싱: ts 불량 행 제거',
  parseViEvents({vi_events: [{ts: null, vi_prc: '1'}]}).length === 0);

// 8) priceFlowYDomain — 세로 스케일 순수 도메인 헬퍼 (전체/트레일링 + fallback)
eval(extract('priceFlowYDomain'));
// 전체: 아웃라이어(5000) 포함해 범위가 늘어난다
const full = priceFlowYDomain('full', [5000, 10000, 10400, 10600], {});
check('도메인 전체: mode=full', full.mode === 'full');
check('도메인 전체: 아웃라이어 포함(min<5000)', full.min < 5000);
check('도메인 전체: min<max', full.min < full.max);
// 트레일링: trail_stop·현재가·최고가 밴드에 집중, 아웃라이어 5000 제외
const tr = priceFlowYDomain('trailing', [5000, 10000, 10400, 10600],
  {trailStop: 10000, price: 10400, high: 10600});
check('도메인 트레일링: mode=trailing', tr.mode === 'trailing');
check('도메인 트레일링: 아웃라이어 제외(min>9000)', tr.min > 9000);
check('도메인 트레일링: 상단은 최고가 위(max>10600)', tr.max > 10600);
check('도메인 트레일링: min<max', tr.min < tr.max);
// fallback: trail_stop 무효(0) → 전체로 대체
const fb1 = priceFlowYDomain('trailing', [5000, 10000, 10400, 10600],
  {trailStop: 0, price: 10400, high: 10600});
check('도메인 fallback: trail_stop 무효 → full', fb1.mode === 'full' && fb1.min < 6000);
// fallback: 밴드 점 2개 미만 → 전체로 대체
const fb2 = priceFlowYDomain('trailing', [5000, 10000, 10400, 10600],
  {trailStop: 10000, price: 0, high: 0});
check('도메인 fallback: 밴드<2점 → full', fb2.mode === 'full');
// degenerate: 단일 값도 NaN 없이 min<max
const one = priceFlowYDomain('full', [10000], {});
check('도메인 degenerate: 단일 값 min<max(NaN 없음)',
  one.min < one.max && Number.isFinite(one.min) && Number.isFinite(one.max));

// 미보유 관측: 진입 실패 뒤 ticker는 비고 observe_ticker로 관측이 이어진다(2026-10-23).
eval(extract('flowTicker'));
eval(extract('isObservingFlow'));
check('관측 종목: 보유 종목이 우선', flowTicker({ticker: '005930', observe_ticker: '065450'}) === '005930');
check('관측 종목: 미보유면 observe_ticker', flowTicker({ticker: null, observe_ticker: '065450'}) === '065450');
check('관측 종목: 둘 다 없으면 null', flowTicker({}) === null);
check('관측 중: 미보유 + observe_ticker', isObservingFlow({position_status: 'IDLE', observe_ticker: '065450'}));
check('관측 중 아님: 보유 중', !isObservingFlow({position_status: 'HOLDING', ticker: '065450', observe_ticker: null}));
check('관측 중 아님: 종목 없음', !isObservingFlow({position_status: 'IDLE'}));

Date.now = realNow;
console.log(failures === 0 ? '\nALL CHECKS PASSED' : `\n${failures} CHECK(S) FAILED`);
process.exit(failures ? 1 : 0);
