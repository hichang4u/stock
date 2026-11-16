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
const PRICE_FLOW_TICK_WINDOW = 5000;
let _priceFlowTicks = [];
let _priceFlow = [];
let _priceFlowSeed = [];
let _priceFlowSeedMeta = null;
let _priceFlowSeedSrc = null;
let _priceFlowTickSrc = null;
let _priceFlowMinuteSrc = null;
let _priceFlowTicker = null;
let drawRequests = 0;
function requestPriceFlowDraw() { drawRequests++; }
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

// ════════════════════════════════════════════════════════════════════════
// 9~) 2026-11-16: '거래 전체'를 골라도 오전 가격선이 없었다. 서버 메모리는 분 가격을 최근
//     180분만 들고 있었고(13:28 시점 10:28~), 화면은 저장된 분봉(09:00~)을 쓰지 않았다.
//     아래는 저장 분봉 시드(minute_price_seed)를 메모리 분·틱과 합치는 기준의 검증이다.
// ════════════════════════════════════════════════════════════════════════
for (const name of [
  'normalizeFlowRange', 'normalizeFlowScale', 'resetPriceFlow', 'appendPriceFlowTick',
  'normalizeFlowMinutes', 'normalizeFlowTicks', 'priceFlowSinceTs', 'updatePriceFlow',
  'composePriceFlowPoints', 'splitFlowSegments', 'priceFlowHints', 'priceFlowDataText',
]) eval(extract(name));

const SEC = 1000;
const GAP_MS = 5 * MIN;
const D0 = Date.parse('2026-11-16T09:00:00+09:00');   // 개장
const iso = ts => new Date(ts + 9 * 3600 * 1000).toISOString().replace('Z', '+09:00');
const clearFlow = () => resetPriceFlow(null);

// 하루 고정 자료: 저장 분봉 390분(09:00~15:29, 종가 13000+i), 메모리 분 180분(12:30~15:29),
// 틱 5,000건(15:29:50에서 0.8초 간격으로 거슬러 올라가 14:23 무렵부터).
const dayBars = (from, count) => Array.from({length: count}, (_, i) =>
  ({ts: iso(D0 + (from + i) * MIN), price: 13000 + from + i}));
const dayMinutes = (from, count) => Array.from({length: count}, (_, i) =>
  ({ts: iso(D0 + (from + i) * MIN), price: 20000 + from + i, tick_count: 5}));
const LAST_TICK = D0 + 389 * MIN + 50 * SEC;            // 15:29:50
const dayTicks = Array.from({length: 5000}, (_, i) =>
  ({ts: iso(LAST_TICK - (4999 - i) * 800), price: 30000 + (i % 7)}));
const ENTRY_AT = '2026-11-16T09:00:28.498553+09:00';
const ENTRY_TS = Date.parse(ENTRY_AT);

// 9) localStorage 호환 — 저장값(recent/trade/full/trailing)은 그대로, 이름만 바뀌었다
check('저장값 trade → 진입 후 전체', normalizeFlowRange('trade') === 'trade');
check('저장값 recent → 최근 20분', normalizeFlowRange('recent') === 'recent');
check('저장값 없음/모르는 값 → recent',
  normalizeFlowRange(null) === 'recent' && normalizeFlowRange('full') === 'recent');
check('저장값 trailing → 청산선 주변', normalizeFlowScale('trailing') === 'trailing');
check('저장값 full → 전체 가격', normalizeFlowScale('full') === 'full');
check('저장값 없음/모르는 값 → full',
  normalizeFlowScale(undefined) === 'full' && normalizeFlowScale('trade') === 'full');
check('저장 키 이름 유지', /PRICE_FLOW_SCALE_KEY = 'priceFlowScale'/.test(src)
  && /PRICE_FLOW_RANGE_KEY = 'priceFlowRange'/.test(src));

// 10) 화면 마크업 — 가로/세로 구분, 버튼 값·접근성 속성
const html = fs.readFileSync(
  path.join(__dirname, '..', '..', 'docs', 'html', 'index.html'), 'utf8');
const buttonOf = id => {
  const m = html.match(new RegExp(`<button[^>]*id="${id}"[^>]*>([^<]*)</button>`));
  return m ? {tag: m[0], label: m[1].trim()} : null;
};
const labels = {
  'range-recent': ['최근 20분', "setPriceFlowRange('recent')"],
  'range-trade': ['진입 후 전체', "setPriceFlowRange('trade')"],
  'scale-full': ['전체 가격', "setPriceFlowScale('full')"],
  'scale-trailing': ['청산선 주변', "setPriceFlowScale('trailing')"],
};
for (const [id, [label, handler]] of Object.entries(labels)) {
  const b = buttonOf(id);
  check(`버튼 ${id}: 이름 "${label}"`, b && b.label === label);
  check(`버튼 ${id}: 저장값·aria-pressed·type 유지`,
    b && b.tag.includes(handler) && /aria-pressed="(true|false)"/.test(b.tag)
      && b.tag.includes('type="button"'));
}
check('묶음 이름이 가로/세로를 말한다',
  html.includes('aria-label="시간 구간(가로축)"') && html.includes('aria-label="가격 범위(세로축)"'));
check('늘 보이는 도움말·자료 줄이 있다',
  html.includes('id="flow-hint-time"') && html.includes('id="flow-hint-price"')
    && html.includes('id="flow-data"') && html.includes('role="status"'));
check('예전 이름(거래 전체/트레일링 버튼)은 남지 않았다',
  !/>거래 전체<\/button>/.test(html) && !/>트레일링<\/button>/.test(html));

// 11) 분 행 정리 — 못 읽는 행 제거, 같은 분은 뒤 행, 시각순
const cleaned = normalizeFlowMinutes([
  {ts: iso(D0 + 3 * MIN), price: 103},
  {ts: iso(D0 + 1 * MIN + 20 * SEC), price: 101},   // 분으로 내림
  {ts: iso(D0 + 2 * MIN), price: 102},
  {ts: iso(D0 + 1 * MIN), price: 111},              // 같은 분 → 뒤 행
  {ts: 'not-a-time', price: 100},
  {ts: iso(D0 + 4 * MIN), price: 0},
  {ts: iso(D0 + 5 * MIN), price: -3},
  {ts: iso(D0 + 6 * MIN), price: 'abc'},
  {ts: iso(D0 + 7 * MIN), price: null},
  {ts: iso(D0 + 8 * MIN), price: true},
  {ts: iso(D0 + 9 * MIN), price: Infinity},
  null,
]);
check('분 정리: 쓸 수 있는 3분만', cleaned.length === 3);
check('분 정리: 시각순', cleaned.map(p => p.ts).join() === [1, 2, 3].map(m => D0 + m * MIN).join());
check('분 정리: 같은 분은 뒤 행이 이긴다', cleaned[0].price === 111);
check('분 정리: 배열이 아니면 []', normalizeFlowMinutes(null).length === 0);

// 12) 틱 정리 — 역순·중복 제거, 뒤에서 limit개
const tk = normalizeFlowTicks([
  {ts: iso(D0 + 1 * SEC), price: 100},
  {ts: iso(D0 + 3 * SEC), price: 101},
  {ts: iso(D0 + 2 * SEC), price: 999},   // 역순
  {ts: iso(D0 + 3 * SEC), price: 101},   // 중복
  {ts: iso(D0 + 3 * SEC), price: 102},   // 같은 시각 다른 가격은 다른 체결
  {ts: 'bad', price: 100},
  {ts: iso(D0 + 4 * SEC), price: 0},
  {ts: iso(D0 + 5 * SEC), price: 103},
], 5000);
check('틱 정리: 역순·중복·불량 제거', tk.map(p => p.price).join() === '100,101,102,103');
check('틱 정리: 시간이 뒤로 가지 않는다', tk.every((p, i) => i === 0 || p.ts >= tk[i - 1].ts));
check('틱 정리: 뒤에서 limit개', normalizeFlowTicks(dayTicks, 100).length === 100
  && normalizeFlowTicks(dayTicks, 100)[99].ts === LAST_TICK);

// 13) 합성 — 하루 전체(6시간 반, 정오 넘김)
const seedAll = normalizeFlowMinutes(dayBars(0, 210));        // 09:00~12:29
const memAll = normalizeFlowMinutes(dayMinutes(210, 180));    // 12:30~15:29
const tickAll = normalizeFlowTicks(dayTicks, 5000);
const NOW_CLOSE = D0 + 392 * MIN;                             // 15:32
const day = composePriceFlowPoints(seedAll, memAll, tickAll, NOW_CLOSE);
const daySeed = day.filter(p => p.src === 'seed');
const dayMin = day.filter(p => p.src === 'minute');
const dayTick = day.filter(p => !p.src);
check('하루: 저장 분봉 210분이 모두 선에 들어간다', daySeed.length === 210);
check('하루: 첫 점은 09:00 봉의 종가를 09:01에', day[0].ts === D0 + 1 * MIN && day[0].price === 13000);
check('하루: 첫 점이 매수(09:00:28)보다 앞에 놓이지 않는다', day[0].ts >= ENTRY_TS);
check('하루: 정오를 넘어 이어진다',
  day.some(p => p.ts === D0 + 180 * MIN) && day.some(p => p.ts === D0 + 181 * MIN));
check('하루: 저장 분봉 → 메모리 분 이음매가 1분 간격',
  dayMin[0].ts - daySeed[daySeed.length - 1].ts === MIN && dayMin[0].price === 20210);
check('하루: 틱은 5,000건 그대로', dayTick.length === 5000 && day[day.length - 1].ts === LAST_TICK);
check('하루: 첫 틱 뒤에는 분 단위 점이 없다',
  day.every(p => !p.src || p.ts < tickAll[0].ts));
check('하루: 시간순(뒤로 가는 점 없음)', day.every((p, i) => i === 0 || p.ts >= day[i - 1].ts));
check('하루: 같은 시각에 분 단위 점이 겹치지 않는다',
  new Set(day.filter(p => p.src).map(p => p.ts)).size === day.filter(p => p.src).length);
check('하루: 09:01~15:29:50 사이 5분 넘는 빈 곳이 없다',
  splitFlowSegments(day, GAP_MS).length === 1);
check('하루: 점 수 = 입력 수 이하(만들어 낸 점 없음)',
  day.length <= seedAll.length + memAll.length + tickAll.length);

// 13-1) 겹치는 분은 메모리 분이 우선 — 시드가 메모리 구간까지 들고 있어도 쓰지 않는다
const overlapSeed = normalizeFlowMinutes(dayBars(0, 260));    // 09:00~13:19 (메모리와 50분 겹침)
const ov = composePriceFlowPoints(overlapSeed, memAll, [], NOW_CLOSE);
check('겹침: 저장 분봉은 메모리 첫 분 앞까지만', ov.filter(p => p.src === 'seed').length === 210);
check('겹침: 겹친 분의 값은 메모리 분', ov.find(p => p.ts === D0 + 211 * MIN).price === 20210);
check('겹침: 분마다 점 하나', ov.length === 390);

// 13-2) 재시작 직후(메모리·틱 없음) — 저장 분봉만으로 하루가 선다
const only = composePriceFlowPoints(normalizeFlowMinutes(dayBars(0, 390)), [], [], NOW_CLOSE);
check('시드만: 390분 전부', only.length === 390 && only[389].ts === D0 + 390 * MIN);

// 13-3) 진행 중인 분은 '지금'에 찍는다 — 미래 시각에 점을 두지 않는다
const nowMid = D0 + 10 * MIN + 20 * SEC;
const prog = composePriceFlowPoints([], normalizeFlowMinutes(dayMinutes(9, 2)), [], nowMid);
check('진행 중인 분: 끝난 분은 분 끝에', prog[0].ts === D0 + 10 * MIN);
check('진행 중인 분: 지금을 넘지 않는다', prog[1].ts === nowMid);
check('자료 없음: 빈 배열', composePriceFlowPoints([], [], [], nowMid).length === 0);

// 13-4) 빈 분을 채우지 않는다 — 수집이 빠진 구간은 점도 없고 선도 끊긴다
const holed = normalizeFlowMinutes(dayBars(0, 60).filter((_, i) => !(i >= 10 && i < 20) && i !== 30));
const holedPts = composePriceFlowPoints(holed, [], [], NOW_CLOSE);
check('빈 분: 점을 만들어 넣지 않는다', holedPts.length === 49);
check('빈 분: 빠진 10분 구간에 점이 없다',
  !holedPts.some(p => p.ts > D0 + 10 * MIN && p.ts <= D0 + 20 * MIN));
const holedSegs = splitFlowSegments(holedPts, GAP_MS);
check('빈 분: 10분 구멍에서 선이 끊긴다', holedSegs.length === 2
  && holedSegs[0][holedSegs[0].length - 1].ts === D0 + 10 * MIN && holedSegs[1][0].ts === D0 + 21 * MIN);
check('빈 분: 1분 구멍은 잇는다(체결 없는 분)', holedSegs[1].length === 39);
check('선 끊기: 빈 입력', splitFlowSegments([], GAP_MS).length === 0);
check('선 끊기: 정확히 5분 간격은 잇는다',
  splitFlowSegments([{ts: 0, price: 1}, {ts: GAP_MS, price: 1}], GAP_MS).length === 1);

// 14) 창 — 진입 후 전체 / 최근 20분 클리핑
Date.now = () => NOW_CLOSE;
_priceFlowSeed = seedAll; _priceFlow = memAll; _priceFlowTicks = tickAll;
const dayMarks = parseTradeMarks({ticker: '010170', trade_marks: [
  {order_type: 'BUY', order_phase: 'FIRST_BUY', ticker: '010170',
    fill_price: 13670, filled_at: '2026-11-16T09:00:28+09:00'},
  {order_type: 'SELL', order_phase: 'CLOSE_SELL', ticker: '010170',
    fill_price: 13900, filled_at: '2026-11-16T09:47:10+09:00'},
]});
const closedDay = {position_status: 'CLOSED', ticker: '010170', entry_at: ENTRY_AT};
const wTrade = priceFlowViewWindow(closedDay, dayMarks, 'trade');
const inTrade = day.filter(p => p.ts >= wTrade.startTs && p.ts <= wTrade.endTs);
check('진입 후 전체: 매수부터 시작', wTrade.startTs <= ENTRY_TS && wTrade.startTs >= ENTRY_TS - 31 * SEC);
check('진입 후 전체: 매도(09:47) 뒤 관측 끝(15:29:50)까지', wTrade.endTs >= LAST_TICK);
check('진입 후 전체: 하루의 모든 점이 창 안', inTrade.length === day.length);
const wRecent = priceFlowViewWindow(closedDay, dayMarks, 'recent');
const inRecent = day.filter(p => p.ts >= wRecent.startTs && p.ts <= wRecent.endTs);
check('최근 20분: 창 크기 20분', wRecent.endTs - wRecent.startTs === 20 * MIN);
check('최근 20분: 창 앞의 점은 모두 잘린다',
  inRecent.length > 0 && inRecent.every(p => p.ts >= wRecent.startTs));
check('최근 20분: 오전 저장 분봉은 화면에 없다', !inRecent.some(p => p.src === 'seed'));
// 재시작 뒤 CLOSED: 시드만 있어도 창이 마지막 봉 끝(15:30)까지 선다
_priceFlow = []; _priceFlowTicks = []; _priceFlowSeed = normalizeFlowMinutes(dayBars(0, 390));
const wSeedOnly = priceFlowViewWindow(closedDay, dayMarks, 'trade');
check('시드만 있는 CLOSED: 끝이 마지막 봉의 끝 이후', wSeedOnly.endTs >= D0 + 390 * MIN);
// 미보유 관측: 진입 시각이 없으면 가장 이른 자료(시드)에서 시작한다 — 첫 틱이 아니다
_priceFlowSeed = seedAll; _priceFlow = memAll; _priceFlowTicks = tickAll;
const wObserve = priceFlowViewWindow(
  {position_status: 'IDLE', observe_ticker: '010170'}, [], 'trade');
check('미보유 관측 전체: 가장 이른 자료에서 시작', wObserve.startTs === D0);
clearFlow();

// 15) updatePriceFlow — 새로고침 뒤 시드와 SSE 틱이 함께 남는다
const NOW_1328 = D0 + 268 * MIN + 30 * SEC;                   // 13:28:30
Date.now = () => NOW_1328;
const payloadAt = (lastMinute, seedCount, extra = {}) => ({
  position_status: 'CLOSED', ticker: '010170', entry_at: ENTRY_AT,
  tick_history: [
    {ts: iso(D0 + lastMinute * MIN - 30 * SEC), price: 13950},
    {ts: iso(D0 + lastMinute * MIN + 5 * SEC), price: 13960},
  ],
  minute_price_history: dayMinutes(lastMinute - 179, 180),
  minute_price_seed: dayBars(0, seedCount),
  minute_price_seed_meta: {source: 'file', count: seedCount, invalid: 0, stale: false},
  ...extra,
});
clearFlow();
const P1 = payloadAt(268, 89);                                // 메모리 10:29~13:28, 시드 09:00~10:28
updatePriceFlow(P1);
check('새로고침: 시드 89분 반영', _priceFlowSeed.length === 89 && _priceFlowSeed[0].ts === D0);
check('새로고침: 메모리 분 180분', _priceFlow.length === 180 && _priceFlow[0].ts === D0 + 89 * MIN);
check('새로고침: 시드 메타 보관', _priceFlowSeedMeta.source === 'file');
// SSE 틱 3건(같은 분 2건 + 다음 분 1건) 뒤 같은 payload가 다시 적용된다(applyStatus(_lastStatus))
appendPriceFlowTick(iso(D0 + 268 * MIN + 40 * SEC), 13970, '010170');
appendPriceFlowTick(iso(D0 + 268 * MIN + 50 * SEC), 13980, '010170');
appendPriceFlowTick(iso(D0 + 269 * MIN + 2 * SEC), 13990, '010170');
const seedRef = _priceFlowSeed;
updatePriceFlow(P1);
check('SSE 뒤: 시드가 그대로다(재파싱도 없음)', _priceFlowSeed === seedRef && _priceFlowSeed.length === 89);
check('SSE 뒤: SSE 틱이 지난 payload에 덮이지 않는다',
  _priceFlowTicks.length === 5 && _priceFlowTicks[4].price === 13990);
check('SSE 뒤: SSE가 만든 분(13:29)이 남는다',
  _priceFlow[_priceFlow.length - 1].ts === D0 + 269 * MIN);
check('SSE 뒤: 같은 분의 SSE 갱신값이 payload 값으로 되돌아가지 않는다',
  _priceFlow[_priceFlow.length - 2].price === 13980);
// 역순·중복 SSE 틱
appendPriceFlowTick(iso(D0 + 269 * MIN + 2 * SEC), 13990, '010170');   // 중복
appendPriceFlowTick(iso(D0 + 268 * MIN + 10 * SEC), 11111, '010170');  // 역순
check('SSE 중복·역순 틱은 버린다',
  _priceFlowTicks.length === 5 && !_priceFlowTicks.some(p => p.price === 11111));
check('역순 틱이 분 이력을 되감지 않는다', _priceFlow[_priceFlow.length - 1].price === 13990);
// 늦게 도착한 옛 응답(stale poll) — 분 이력도 시드도 갈아끼우지 않는다
const P0 = payloadAt(260, 81);
updatePriceFlow(P0);
check('옛 응답: 시드 유지', _priceFlowSeed.length === 89);
check('옛 응답: 분·틱 유지',
  _priceFlow[_priceFlow.length - 1].ts === D0 + 269 * MIN && _priceFlowTicks.length === 5);
// 새 응답 — 메모리가 한 분 밀리면 시드가 한 분 늘어 받아진다
Date.now = () => D0 + 280 * MIN;
const P2 = payloadAt(270, 91);
updatePriceFlow(P2);
check('새 응답: 시드 91분으로 교체', _priceFlowSeed.length === 91);
check('새 응답: 시드 끝과 메모리 첫 분이 맞닿는다',
  _priceFlow[0].ts - _priceFlowSeed[_priceFlowSeed.length - 1].ts === MIN);
const joined = composePriceFlowPoints(_priceFlowSeed, _priceFlow, _priceFlowTicks, Date.now());
check('새 응답: 합친 선에 5분 넘는 구멍이 없다', splitFlowSegments(joined, GAP_MS).length === 1);
// 읽기 실패 응답은 가진 시드를 지우지 않는다. '파일 없음'은 비운다.
updatePriceFlow(payloadAt(271, 0, {minute_price_seed_meta: {source: 'error', count: 0}}));
check('읽기 실패 응답: 시드 유지, 메타는 error', _priceFlowSeed.length === 91
  && _priceFlowSeedMeta.source === 'error');
// 시드 필드가 없는 응답(옛 서버) — 건드리지 않는다
const legacy = payloadAt(272, 0);
delete legacy.minute_price_seed; delete legacy.minute_price_seed_meta;
updatePriceFlow(legacy);
check('시드 필드 없는 응답: 시드 유지', _priceFlowSeed.length === 91);
updatePriceFlow(payloadAt(273, 0, {minute_price_seed_meta: {source: 'none', count: 0}}));
check('파일 없음 응답: 시드 비움', _priceFlowSeed.length === 0 && _priceFlowSeedMeta.source === 'none');
// 잘못된 가격이 섞인 시드
updatePriceFlow(payloadAt(274, 0, {minute_price_seed: [
  {ts: iso(D0), price: 13000}, {ts: iso(D0 + MIN), price: 0}, {ts: iso(D0 + 2 * MIN), price: 'x'},
  {ts: 'bad', price: 13003}, {ts: iso(D0 + 4 * MIN), price: 13004},
]}));
check('잘못된 시드 행은 버린다', _priceFlowSeed.map(p => p.price).join() === '13000,13004');

// 16) 종목·거래일·진입 전환
updatePriceFlow(payloadAt(275, 95));
updatePriceFlow({position_status: 'HOLDING', ticker: '065450',
  entry_at: iso(D0 + 276 * MIN), tick_history: [], minute_price_history: []});
check('종목 전환: 이전 종목의 시드·분·틱을 모두 버린다',
  _priceFlowTicker === '065450' && !_priceFlowSeed.length && !_priceFlow.length
    && !_priceFlowTicks.length && _priceFlowSeedMeta === null);
updatePriceFlow({position_status: 'HOLDING', ticker: '065450', entry_at: iso(D0 + 276 * MIN),
  tick_history: [], minute_price_history: dayMinutes(276, 1),
  minute_price_seed: [], minute_price_seed_meta: {source: 'skipped'}});
check('종목 전환 뒤: 새 종목 자료만', _priceFlow.length === 1 && _priceFlowSeed.length === 0);
// SSE 틱이 다른 종목이면 그 틱부터 새로 쌓는다
appendPriceFlowTick(iso(D0 + 277 * MIN), 4000, '030960');
check('SSE 종목 전환: 버퍼 초기화 후 그 틱만',
  _priceFlowTicker === '030960' && _priceFlowTicks.length === 1 && !_priceFlowSeed.length);

// 미보유 관측 중 날이 바뀜 — 빈 payload라도 어제 행은 버린다
clearFlow();
const yesterday = D0 - 24 * 60 * MIN;
Date.now = () => yesterday + 300 * MIN;
updatePriceFlow({position_status: 'IDLE', observe_ticker: '010170',
  tick_history: [{ts: iso(yesterday + 100 * MIN), price: 9000}],
  minute_price_history: [{ts: iso(yesterday + 100 * MIN), price: 9000, tick_count: 1}],
  minute_price_seed: [{ts: iso(yesterday + 10 * MIN), price: 8900}],
  minute_price_seed_meta: {source: 'file'}});
check('관측(어제): 자료가 들어 있다',
  _priceFlowTicks.length === 1 && _priceFlow.length === 1 && _priceFlowSeed.length === 1);
Date.now = () => D0 - 10 * MIN;                               // 다음 날 08:50, 아직 틱 없음
updatePriceFlow({position_status: 'IDLE', observe_ticker: '010170',
  tick_history: [], minute_price_history: [],
  minute_price_seed: [], minute_price_seed_meta: {source: 'none'}});
check('거래일 전환: 같은 종목이어도 어제 선이 남지 않는다',
  !_priceFlowTicks.length && !_priceFlow.length && !_priceFlowSeed.length);
check('오늘 0시(KST) 기준', priceFlowSinceTs({observe_ticker: 'x'}, D0)
  === Date.parse('2026-11-16T00:00:00+09:00'));
check('보유·청산은 진입 분 기준', priceFlowSinceTs({ticker: 'x', entry_at: ENTRY_AT}, D0) === D0);
check('기준을 모르면 거르지 않는다',
  priceFlowSinceTs({ticker: 'x', entry_at: null}, D0) === 0 && priceFlowSinceTs({}, D0) === 0);

// 같은 종목에 다시 진입 — 새 진입 분 앞의 행은 버린다(진입 전 자료를 그리지 않는다)
clearFlow();
Date.now = () => NOW_1328;
updatePriceFlow(payloadAt(268, 89));
updatePriceFlow({position_status: 'HOLDING', ticker: '010170',
  entry_at: iso(D0 + 200 * MIN + 12 * SEC), tick_history: [], minute_price_history: [],
  minute_price_seed: dayBars(0, 89), minute_price_seed_meta: {source: 'file'}});
check('재진입: 새 진입 분 앞의 분·시드가 없다',
  _priceFlow.every(p => p.ts >= D0 + 200 * MIN) && _priceFlowSeed.length === 0);
check('그리기 요청은 갱신마다 한 번', drawRequests > 0);
clearFlow();

// 17) 도움말 문구 — 가로/세로, 매도 후 관측 포함, 청산선 없음 대체, 범위 밖 잘림
const hint = o => priceFlowHints({rangeMode: 'recent', scaleMode: 'full', appliedScale: 'full',
  observing: false, clipped: false, viewMin: 20, ...o});
check('도움말 시간: 최근 20분은 앞이 화면 밖임을 말한다',
  hint({}).time === '최근 20분만 표시 · 그 앞은 화면 밖');
check('도움말 시간: 진입 후 전체는 매도 후 관측 포함을 말한다',
  hint({rangeMode: 'trade'}).time === '매수부터 최신 관측까지 · 매도 후 관측 포함');
check('도움말 시간: 미보유 관측은 관측 시작부터',
  hint({rangeMode: 'trade', observing: true}).time === '오늘 관측 시작부터 최신 관측까지 · 미보유');
check('도움말 가격: 전체 가격', hint({}).price === '보이는 구간의 가격 전체' && !hint({}).fallback);
const zoom = hint({scaleMode: 'trailing', appliedScale: 'trailing'});
check('도움말 가격: 청산선 주변은 표시 전용 확대이며 잘릴 수 있음',
  zoom.price.includes('청산선·현재가·보유 고가 주변만 확대(표시 전용)')
    && zoom.price.includes('범위 밖 선은 잘릴 수 있음') && !zoom.fallback);
check('도움말 가격: 실제로 잘렸으면 그렇게 말한다',
  hint({scaleMode: 'trailing', appliedScale: 'trailing', clipped: true}).price
    .includes('범위 밖 선이 잘려 있음'));
const fb = hint({scaleMode: 'trailing', appliedScale: 'full'});
check('도움말 가격: 청산선이 없으면 전체 가격으로 대체됐다고 알린다',
  fb.fallback && fb.price === '청산선(Trail Stop)이 아직 없어 전체 가격으로 표시 중');

// 17-1) 세로 범위 — 청산선 주변은 오전 가격을 범위 밖으로 밀고, 청산선이 없으면 전체로 돌아간다
const dayPrices = day.map(p => p.price);                      // 13000~30006
const zoomDom = priceFlowYDomain('trailing', dayPrices,
  {trailStop: 29800, price: 30003, high: 30100});
check('청산선 주변: 적용됨', zoomDom.mode === 'trailing');
check('청산선 주변: 오전 선(13000대)은 범위 밖 → 잘림',
  day.some(p => p.price < zoomDom.min) && day[0].price < zoomDom.min);
check('청산선 주변: 청산선·현재가·고가는 범위 안',
  [29800, 30003, 30100].every(v => v > zoomDom.min && v < zoomDom.max));
const noStop = priceFlowYDomain('trailing', dayPrices, {trailStop: 0, price: 30003, high: 30100});
check('청산선 없음: 전체 가격으로 대체되어 아무 점도 잘리지 않는다',
  noStop.mode === 'full' && day.every(p => p.price >= noStop.min && p.price <= noStop.max));
const fullDom = priceFlowYDomain('full', dayPrices, {trailStop: 29800, price: 30003, high: 30100});
check('전체 가격: 모든 점이 범위 안', day.every(p => p.price >= fullDom.min && p.price <= fullDom.max));

// 18) 자료 구성 줄 — 저장 분봉으로 메운 구간과 비어 있는 구간을 숨기지 않는다
const dataText = o => priceFlowDataText({seed: 0, minute: 0, tick: 0, seedSpan: '', headGap: '',
  seedSource: 'file', seedStale: false, seedInvalid: 0, breaks: 0, ...o});
check('자료 줄: 세 출처를 나눠 적는다',
  dataText({seed: 88, minute: 53, tick: 5000, seedSpan: '09:00~10:27'})
    === '선: 저장 분봉 88개(09:00~10:27, 1분 종가) + 메모리 분 53개 + 틱 5,000개');
check('자료 줄: 틱만 있으면 틱만', dataText({tick: 312}) === '선: 틱 312개');
check('자료 줄: 자료 없음', dataText({}) === '선: 표시할 가격 자료 없음');
check('자료 줄: 앞 구간이 비면 이유와 함께 적는다(저장 분봉 없음)',
  dataText({minute: 180, tick: 5000, headGap: '09:00~10:29', seedSource: 'none'})
    .endsWith(' · 앞 09:00~10:29 자료 없음(저장 분봉 없음)'));
check('자료 줄: 읽기 실패',
  dataText({minute: 180, headGap: '09:00~10:29', seedSource: 'error'}).includes('(저장 분봉 읽기 실패)'));
check('자료 줄: 봉은 읽었는데 앞이 비면 수집 기록 없음',
  dataText({seed: 40, seedSpan: '09:30~10:09', headGap: '09:00~09:31', seedSource: 'file'})
    .includes('(수집 기록 없음)'));
check('자료 줄: 시드 정보가 없는 서버',
  dataText({minute: 180, headGap: '09:00~10:29', seedSource: undefined}).includes('(저장 분봉 미확인)'));
check('자료 줄: 끊긴 곳·제외한 봉·직전 판독본',
  dataText({seed: 49, seedSpan: '09:00~09:59', breaks: 1, seedInvalid: 2, seedStale: true})
    === '선: 저장 분봉 49개(09:00~09:59, 1분 종가) · 5분 넘게 빈 구간 1곳은 잇지 않음'
      + ' · 못 읽는 저장 분봉 2개 제외 · 저장 분봉은 직전 판독본');

Date.now = realNow;
console.log(failures === 0 ? '\nALL CHECKS PASSED' : `\n${failures} CHECK(S) FAILED`);
process.exit(failures ? 1 : 0);
