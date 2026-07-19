// charts.js — hand-built SVG daily-spend chart (line + area, faint grid,
// emphasized endpoint, hover tooltip). Ported from docs/mockup.html's own
// renderDaily(), adapted to the real GET /usage/data by_day field names
// (day / cost_usd / requests) instead of the mockup's placeholder shorthand.
let _lastDailyChartData = null;

function renderDailyChart(days) {
  const svg = document.getElementById('daily-svg');
  const tooltip = document.getElementById('chart-tooltip');
  if (!svg || !tooltip) return;

  _lastDailyChartData = days;
  if (!days || days.length === 0) {
    svg.innerHTML = '';
    return;
  }

  const rect = svg.getBoundingClientRect();
  const W = rect.width || 900;
  const padL = 40, padR = 12, padT = 16, padB = 26;
  const chartW = W - padL - padR, chartH = 130;
  svg.setAttribute('viewBox', `0 0 ${W} ${chartH + padT + padB}`);
  svg.setAttribute('height', chartH + padT + padB);

  const maxCost = Math.max(...days.map((d) => d.cost_usd), 0.01);
  const niceMax = Math.ceil(maxCost * 1.15 * 10) / 10;
  const n = days.length;
  const slotW = n > 1 ? chartW / (n - 1) : 0;

  const styles = getComputedStyle(document.documentElement);
  const gridColor = styles.getPropertyValue('--border-soft').trim();
  const axisText = styles.getPropertyValue('--text-muted').trim();
  const brand = styles.getPropertyValue('--primary').trim();

  let svgHtml = '';
  const gridCount = 3;
  for (let i = 0; i <= gridCount; i++) {
    const val = (niceMax / gridCount) * i;
    const y = padT + chartH - (val / niceMax) * chartH;
    svgHtml += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="${gridColor}" stroke-width="1"/>`;
    svgHtml += `<text x="${padL - 8}" y="${y + 3}" text-anchor="end" font-size="9.5" fill="${axisText}">$${val.toFixed(2)}</text>`;
  }

  const pts = days.map((d, i) => {
    const x = padL + i * slotW;
    const y = padT + chartH - (d.cost_usd / niceMax) * chartH;
    return { x, y, d };
  });

  const areaPath = `M${pts[0].x},${padT + chartH} ` + pts.map((p) => `L${p.x},${p.y}`).join(' ') + ` L${pts[n - 1].x},${padT + chartH} Z`;
  svgHtml += `<path d="${areaPath}" fill="${brand}" opacity="0.10"/>`;

  const linePath = 'M' + pts.map((p) => `${p.x},${p.y}`).join(' L');
  svgHtml += `<path d="${linePath}" fill="none" stroke="${brand}" stroke-width="2"/>`;

  pts.forEach((p, i) => {
    const isLast = i === n - 1;
    const r = isLast ? 5 : 3;
    svgHtml += `<circle class="pt" cx="${p.x}" cy="${p.y}" r="${r + 4}" fill="transparent" data-i="${i}" style="cursor:pointer"/>`;
    svgHtml += `<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${isLast ? brand : 'var(--surface)'}" stroke="${brand}" stroke-width="${isLast ? 0 : 2}"/>`;
    if (i % 2 === 0 || isLast) {
      svgHtml += `<text x="${p.x}" y="${padT + chartH + 16}" text-anchor="middle" font-size="9.5" fill="${axisText}">${p.d.day.slice(5)}</text>`;
    }
  });

  svg.innerHTML = svgHtml;
  svg.querySelectorAll('.pt').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      const d = days[+el.dataset.i];
      tooltip.innerHTML = `<div class="tt-date">${d.day}</div><div class="tt-cost">$${d.cost_usd.toFixed(4)} · ${d.requests} req</div>`;
      tooltip.style.display = 'block';
      tooltip.style.left = (e.clientX + 14) + 'px';
      tooltip.style.top = (e.clientY - 34) + 'px';
    });
    el.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
  });
}

window.addEventListener('resize', () => {
  if (_lastDailyChartData) renderDailyChart(_lastDailyChartData);
});

// renderModelDonut — draws the cost-by-model donut arcs imperatively rather
// than via Alpine's `x-for`/`<template>` inside an <svg>: Alpine clones
// <template> content with document.importNode(), and browsers are
// inconsistent about giving cloned SVG-foreign-content the right namespace,
// which surfaced as real (if visually self-healing) console errors. Setting
// .innerHTML directly on an SVG <g> — the same technique already used for
// the daily chart above — sidesteps that entirely: the fragment parser
// correctly namespaces child elements when the context node is itself SVG.
function renderModelDonut(segments) {
  const g = document.getElementById('donut-arcs');
  if (!g) return;
  g.innerHTML = (segments || []).map((seg) => `
    <circle cx="56" cy="56" r="42" fill="none" stroke="${seg.color}" stroke-width="16"
      stroke-dasharray="${seg.dashArray}" stroke-dashoffset="${seg.dashOffset}" transform="rotate(-90 56 56)"/>
  `).join('');
}
