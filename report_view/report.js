const viewport = document.querySelector('.report-scroll');
const content = document.querySelector('.report-content');
const rail = document.querySelector('.report-scrollbar');
const thumb = document.querySelector('.report-scrollbar-thumb');
const pageIndicator = document.querySelector('.report-page-indicator');
const pageMarkers = [];
// Read rendered comments, not code examples containing escaped comment text.
const walker = document.createTreeWalker(content, NodeFilter.SHOW_COMMENT);
const comments = [];
while (walker.nextNode()) comments.push(walker.currentNode);
for (const comment of comments) {
  const match = comment.data.match(/^\s*PDF_PAGE:\s*(\d+)\s*\/\s*(\d+)\s*$/);
  if (!match) continue;
  const marker = document.createElement('span');
  marker.className = 'pdf-page-marker';
  marker.setAttribute('aria-hidden', 'true');
  comment.parentNode.insertBefore(marker, comment);
  pageMarkers.push({ element: marker, page: Number(match[1]), total: Number(match[2]) + 1 });
}
function updatePageIndicator() {
  if (!pageMarkers.length) return;
  const top = viewport.getBoundingClientRect().top + 1;
  // The last marker above the reading area's top identifies the current page.
  let low = 0;
  let high = pageMarkers.length - 1;
  let current = 0;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (pageMarkers[middle].element.getBoundingClientRect().top <= top) {
      current = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  const label = `第${pageMarkers[current].page}页/共${pageMarkers[pageMarkers.length - 1].total}页`;
  if (pageIndicator.textContent !== label) pageIndicator.textContent = label;
  pageIndicator.hidden = false;
}
let thumbHeight = 0;
let drag = null;
function updateScrollbar() {
  const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  thumbHeight = Math.min(rail.clientHeight, Math.max(48,
    rail.clientHeight * viewport.clientHeight / Math.max(1, viewport.scrollHeight)));
  const travel = rail.clientHeight - thumbHeight;
  thumb.style.height = thumbHeight + 'px';
  thumb.style.top = (maxScroll ? viewport.scrollTop / maxScroll * travel : 0) + 'px';
  rail.setAttribute('aria-valuemax', String(maxScroll));
  rail.setAttribute('aria-valuenow', String(Math.round(viewport.scrollTop)));
  updatePageIndicator();
}
rail.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  const travel = rail.clientHeight - thumbHeight;
  if (event.target !== thumb && travel > 0) {
    viewport.scrollTop = (event.clientY - rail.getBoundingClientRect().top
      - thumbHeight / 2) / travel * maxScroll;
  }
  drag = { y: event.clientY, scrollTop: viewport.scrollTop };
  rail.setPointerCapture(event.pointerId);
});
rail.addEventListener('pointermove', (event) => {
  if (!drag) return;
  const travel = rail.clientHeight - thumbHeight;
  if (travel > 0) viewport.scrollTop = drag.scrollTop + (event.clientY - drag.y)
    / travel * (viewport.scrollHeight - viewport.clientHeight);
});
rail.addEventListener('lostpointercapture', () => { drag = null; });
rail.addEventListener('pointerup', () => { drag = null; });
rail.addEventListener('pointercancel', () => { drag = null; });
rail.addEventListener('keydown', (event) => {
  const moves = { ArrowDown: 40, ArrowUp: -40,
    PageDown: viewport.clientHeight, PageUp: -viewport.clientHeight,
    Home: -viewport.scrollHeight, End: viewport.scrollHeight };
  if (Object.hasOwn(moves, event.key)) {
    event.preventDefault();
    viewport.scrollTop += moves[event.key];
  }
});
rail.addEventListener('wheel', (event) => {
  event.preventDefault();
  viewport.scrollTop += event.deltaY * (event.deltaMode === 1 ? 16
    : event.deltaMode === 2 ? viewport.clientHeight : 1);
}, { passive: false });
viewport.addEventListener('scroll', updateScrollbar, { passive: true });
const observer = new ResizeObserver(updateScrollbar);
observer.observe(viewport);
observer.observe(content);
window.addEventListener('load', updateScrollbar);
updateScrollbar();

// Navigation metadata stays hidden in the document; expose only safe local links.
(() => {
  const panel = document.querySelector('.report-navigation');
  const toggle = panel.querySelector('button');
  const navigation = panel.querySelector('nav');
  const metadata = comments.find(comment => /^\s*REPORT_NAVIGATION:/.test(comment.data));
  if (!metadata) return;
  const template = document.createElement('template');
  template.innerHTML = metadata.data.replace(/^\s*REPORT_NAVIGATION:/, '');
  const sections = [];
  const seen = new Set();
  for (const source of template.content.querySelectorAll('a[href]')) {
    const href = source.getAttribute('href');
    if (!href.startsWith('#__reportanchor-') || seen.has(href)) continue;
    const target = document.getElementById(href.slice(1));
    if (!target || !content.contains(target)) continue;
    const label = source.textContent.trim();
    if (!label) continue;
    const link = document.createElement('a');
    link.setAttribute('href', href);
    link.textContent = label.replace(/[.．…·]{2,}\s*/g, ' · ').replace(/\s*↗\s*$/, '');
    link.title = label;
    navigation.append(link);
    sections.push({ link, target });
    seen.add(href);
  }
  if (!sections.length) return;
  function setExpanded(expanded) {
    toggle.setAttribute('aria-expanded', String(expanded));
    navigation.hidden = !expanded;
  }
  const narrow = () => window.matchMedia('(max-width: 800px)').matches;
  setExpanded(!narrow());
  panel.hidden = false;
  let panelDrag = null;
  let suppressToggle = false;
  function movePanel(left, top) {
    const bounds = panel.getBoundingClientRect();
    const x = Math.max(8, Math.min(left, window.innerWidth - 30 - bounds.width));
    const y = Math.max(8, Math.min(top, window.innerHeight - bounds.height - 8));
    panel.style.left = `${x}px`;
    panel.style.right = 'auto';
    panel.style.top = `${y}px`;
    panel.style.setProperty('--navigation-top', `${y}px`);
  }
  toggle.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) return;
    const bounds = panel.getBoundingClientRect();
    panelDrag = { id: event.pointerId, x: event.clientX, y: event.clientY,
      left: bounds.left, top: bounds.top };
    suppressToggle = false;
    toggle.setPointerCapture(event.pointerId);
  });
  toggle.addEventListener('pointermove', event => {
    if (!panelDrag || panelDrag.id !== event.pointerId) return;
    const dx = event.clientX - panelDrag.x;
    const dy = event.clientY - panelDrag.y;
    if (!suppressToggle && Math.hypot(dx, dy) < 5) return;
    suppressToggle = true;
    event.preventDefault();
    panel.classList.add('is-dragging');
    movePanel(panelDrag.left + dx, panelDrag.top + dy);
  });
  function finishPanelDrag() {
    panelDrag = null;
    panel.classList.remove('is-dragging');
  }
  toggle.addEventListener('pointerup', finishPanelDrag);
  toggle.addEventListener('pointercancel', finishPanelDrag);
  toggle.addEventListener('lostpointercapture', finishPanelDrag);
  toggle.addEventListener('click', event => {
    if (suppressToggle && event.detail !== 0) {
      suppressToggle = false;
      return;
    }
    setExpanded(navigation.hidden);
  });
  function constrainPanel() {
    if (!panel.style.left) return;
    const bounds = panel.getBoundingClientRect();
    movePanel(bounds.left, bounds.top);
  }
  window.addEventListener('resize', constrainPanel);
  new ResizeObserver(constrainPanel).observe(panel);
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      setExpanded(false);
      toggle.focus();
    }
  });
  navigation.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link) return;
    // Native fragment navigation also updates the existing page indicator.
    if (narrow()) setExpanded(false);
  });
  function updateActiveSection() {
    const top = viewport.getBoundingClientRect().top + 2;
    let active = null;
    let nearest = -Infinity;
    for (const section of sections) {
      const position = section.target.getBoundingClientRect().top;
      if (position <= top && position >= nearest) {
        active = section;
        nearest = position;
      }
    }
    for (const section of sections) {
      if (section === active) section.link.setAttribute('aria-current', 'location');
      else section.link.removeAttribute('aria-current');
    }
  }
  let scheduled = false;
  function scheduleUpdate() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; updateActiveSection(); });
  }
  viewport.addEventListener('scroll', scheduleUpdate, { passive: true });
  window.addEventListener('resize', scheduleUpdate);
  window.addEventListener('load', scheduleUpdate);
  updateActiveSection();
})();
