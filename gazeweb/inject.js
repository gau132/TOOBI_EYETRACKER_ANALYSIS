// 注入每個頁面與 iframe（document_start）：用 rrweb 錄下 DOM 變化，並回報視窗幾何給 Python
// 以便把螢幕上的視線座標換算成「視窗座標」與「整頁座標」。
(() => {
  if (window.__gazewebInstalled) return;
  window.__gazewebInstalled = true;
  // rrweb 與本檔串在同一段 init script（Playwright 會把 init script 包進函式，var 不會變成全域）
  const R = typeof rrweb !== 'undefined' ? rrweb : window.rrweb;
  if (!R) return;
  const isTop = window.top === window;
  const RECORD_OPTS = {
    recordAfter: 'DOMContentLoaded',
    sampling: { scroll: 50, mousemove: 50, input: 'last' },
    inlineStylesheet: true,
    collectFonts: false,
    maskAllInputs: false,
    recordCrossOriginIframes: true, // 跨網域 iframe 的內容由子框架錄製後 postMessage 給頂層
  };

  // ---- iframe 內的點擊：逐層往上傳，每層加上該 iframe 在父框架中的位置 ----
  const MSG = '__gazewebClick';
  const forwardUp = (cx, cy, t) => {
    try { window.parent.postMessage({ [MSG]: true, cx, cy, t }, '*'); } catch (_) {}
  };
  let reportClick = null; // 頂層才會設定
  addEventListener('message', (e) => {
    const d = e.data;
    if (!d || d[MSG] !== true || typeof d.cx !== 'number') return;
    const fr = Array.from(document.querySelectorAll('iframe, frame')).find((f) => f.contentWindow === e.source);
    if (!fr) return;
    const r = fr.getBoundingClientRect();
    const cx = r.left + fr.clientLeft + d.cx, cy = r.top + fr.clientTop + d.cy;
    if (isTop) { if (reportClick) reportClick(cx, cy, d.t); } else forwardUp(cx, cy, d.t);
  });

  if (!isTop) {
    addEventListener('pointerdown', (e) => forwardUp(e.clientX, e.clientY, Date.now()), { passive: true, capture: true });
    try { R.record({ ...RECORD_OPTS, emit() {} }); } catch (_) {}
    return;
  }

  const sendGeom = window.__gazewebGeom;
  const sendEvents = window.__gazewebEvents;
  if (!sendGeom || !sendEvents) return;

  let lastMouse = null;
  let buf = [];
  // 最後一次「明顯的」DOM 變動時間（一次增刪 3 個以上節點），給截圖判斷畫面是否已穩定
  let lastMutation = Date.now();
  new MutationObserver((records) => {
    let n = 0;
    for (const r of records) n += r.addedNodes.length + r.removedNodes.length;
    if (n >= 3) lastMutation = Date.now();
  }).observe(document, { childList: true, subtree: true });
  // 每個分頁都持續錄製；哪段時間該顯示哪個分頁，由 Python 依「造訪段落」在產生回放時挑選。
  // （不在頁面裡依焦點丟棄事件：焦點會隨作業系統視窗切換而變，丟掉的初始快照與 iframe 內容就補不回來）

  // 主要捲動容器：很多網站（HackMD、Notion、Gmail…）不捲動整個視窗，而是捲動頁面裡一個大的 div，
  // window.scrollY 永遠是 0。偵測「覆蓋可視區一半以上、且可捲動」的元素，把它的捲動量算進整頁座標。
  let mainScroller = null;
  const isMainScroller = (el) => {
    if (!el || el.nodeType !== 1 || !el.isConnected) return false;
    if (el.scrollHeight <= el.clientHeight + 10 && el.scrollWidth <= el.clientWidth + 10) return false;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    return w * h >= 0.5 * innerWidth * innerHeight;
  };
  const scroller = () => (mainScroller && mainScroller.isConnected ? mainScroller : null);

  const geom = (reason) => {
    const de = document.documentElement || {};
    const ms = scroller();
    return {
      t: Date.now(), reason, url: location.href, title: document.title,
      dpr: window.devicePixelRatio, iw: window.innerWidth, ih: window.innerHeight,
      sx: window.scrollX + (ms ? ms.scrollLeft : 0), sy: window.scrollY + (ms ? ms.scrollTop : 0),
      ms: ms ? 1 : 0,
      scrX: window.screenX, scrY: window.screenY, ow: window.outerWidth, oh: window.outerHeight,
      dw: Math.max(de.scrollWidth || 0, document.body ? document.body.scrollWidth : 0, ms ? ms.scrollWidth : 0),
      dh: Math.max(de.scrollHeight || 0, document.body ? document.body.scrollHeight : 0,
        ms ? ms.scrollHeight + Math.max(0, ms.getBoundingClientRect().top) : 0),
      focus: document.hasFocus(), vis: document.visibilityState, mouse: lastMouse, rs: document.readyState, lm: lastMutation,
    };
  };
  const report = (reason) => { try { sendGeom(geom(reason)); } catch (_) {} };

  const flush = () => {
    if (!buf.length) return;
    const batch = buf; buf = [];
    try { sendEvents(batch); } catch (_) {}
  };

  const activate = () => report('focus');

  R.record({ ...RECORD_OPTS, emit(e) { buf.push(e); } });

  setInterval(flush, 500);
  setInterval(() => { activate(); }, 250); // 也能偵測視窗被拖動
  addEventListener('focus', activate, true);
  addEventListener('blur', activate, true);
  document.addEventListener('visibilitychange', activate);
  addEventListener('pagehide', flush);
  addEventListener('resize', () => report('resize'));

  let scrollQueued = false;
  addEventListener('scroll', (e) => {
    const t = e.target;
    if (t && t !== document && t !== mainScroller && isMainScroller(t)) mainScroller = t;
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => { scrollQueued = false; report('scroll'); });
  }, { passive: true, capture: true });

  let lastMoveReport = 0;
  addEventListener('mousemove', (e) => {
    lastMouse = { cx: e.clientX, cy: e.clientY, sx: e.screenX, sy: e.screenY };
    const now = performance.now();
    if (now - lastMoveReport > 200) { lastMoveReport = now; report('mouse'); }
  }, { passive: true, capture: true });

  // 點擊位置：攝影機來源會把它當作「使用者正看著這裡」的校正參考
  reportClick = (cx, cy, t) => {
    const g = geom('click');
    g.click = { cx, cy, t };
    try { sendGeom(g); } catch (_) {}
  };
  addEventListener('pointerdown', (e) => {
    lastMouse = { cx: e.clientX, cy: e.clientY, sx: e.screenX, sy: e.screenY };
    reportClick(e.clientX, e.clientY, Date.now());
  }, { passive: true, capture: true });

  const start = () => { activate(); report('load'); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
