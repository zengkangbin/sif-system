const state = {
  jobId: null,
  result: null,
  pollTimer: null,
  trackedJobIds: new Set(),
  historyRows: [],
  historyPage: 1,
  historyPageSize: 10,
  productTitle: '',
  qaPage: 1,
  qaPageSize: 10,
  reviewPage: 1,
  reviewPageSize: 10,
};

const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url;
  const token = localStorage.getItem('sif_token');

  // 调试日志
  console.log('[Fetch Debug] URL:', url, 'Has token:', !!token, 'Matches /api/:', url.startsWith('/api/'));

  if (token && url.startsWith('/api/') && !url.startsWith('/api/auth/')) {
    const headers = new Headers(init.headers || {});
    headers.set('Authorization', `Bearer ${token}`);
    init = { ...init, headers };
    console.log('[Fetch Debug] Added Authorization header');
  }
  return nativeFetch(input, init).then(response => {
    console.log('[Fetch Debug] Response status:', response.status, 'for URL:', url);
    if (response.status === 401 && !location.pathname.endsWith('/auth.html')) {
      console.log('[Fetch Debug] 401 Unauthorized - redirecting to login');
      localStorage.removeItem('sif_token');
      location.href = '/static/auth.html';
    }
    return response;
  });
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const listValue = (value) => Array.isArray(value) ? value : (value ? [value] : []);
const cleanReviewText = (value) => String(value ?? '').replace(/^\s*\[(?:internal\s+simulated\s+buyer\s+feedback|内部模拟买家(?:反馈|评论))[^\]]*\]\s*/i, '').trim();
function normalizeReviewEntry(value) {
  if (value && typeof value === 'object') {
    return {
      title: String(value.title || value.review_title || value.headline || 'Buyer Experience').trim(),
      title_zh: String(value.title_zh || value.review_title_zh || value.headline_zh || '买家使用体验').trim(),
      content: cleanReviewText(value.content || value.review || value.review_content || value.comment || value.text || value.body || ''),
      content_zh: cleanReviewText(value.content_zh || value.review_zh || value.review_content_zh || value.comment_zh || value.text_zh || value.body_zh || ''),
    };
  }
  return {title: 'Buyer Experience', title_zh: '买家使用体验', content: cleanReviewText(value), content_zh: ''};
}

async function responseData(response) {
  const text = await response.text();
  if (!text) return {};
  try { return JSON.parse(text); } catch (_) { return {detail: text}; }
}

function downloadTextFile(filename, content) {
  const safeName = String(filename || 'sif-analysis').replace(/[\\/:*?"<>|]/g, '_').slice(0, 100) || 'sif-analysis';
  const blob = new Blob([`\ufeff${String(content || '')}`], {type: 'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${safeName}.txt`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 100);
}

function currentProductFileStem() {
  const title = state.productTitle || $('product_name')?.value || 'sif-analysis';
  return String(title).replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 70) || 'sif-analysis';
}

function textForDownload(kind) {
  const analysis = state.result?.analysis || {};
  if (kind === 'personas') {
    return listValue(analysis.consumer_profiles).map((row, index) => [
      `消费者画像 ${index + 1}：${row.name || ''}`,
      `痛点：${listValue(row.pain_points).join('；')}`,
      `购买动机：${listValue(row.purchase_motivations).join('；')}`,
      `使用场景：${listValue(row.usage_scenarios).join('；')}`,
      `购买顾虑：${listValue(row.purchase_concerns).join('；')}`,
      `证据：${listValue(row.evidence).join('；')}`,
    ].join('\n')).join('\n\n');
  }
  const listing = analysis.listing || {};
  if (kind === 'title') return `标题（英文）\n${listing.title || ''}\n\n标题（中文翻译）\n${listing.title_zh || ''}`;
  if (kind === 'bullets') return `五点（英文）\n${listValue(listing.bullets).map((item, index) => `${index + 1}. ${item}`).join('\n')}\n\n五点（中文翻译）\n${listValue(listing.bullets_zh).map((item, index) => `${index + 1}. ${item}`).join('\n')}`;
  if (kind === 'qa') {
    return `Q&A（英文）\n${listValue(listing.qa).map((row, index) => `Q${index + 1}：${row.question || ''}\nA：${row.answer || ''}`).join('\n\n')}\n\nQ&A（中文翻译）\n${listValue(listing.qa).map((row, index) => `问${index + 1}：${row.question_zh || ''}\n答：${row.answer_zh || ''}`).join('\n\n')}`;
  }
  if (kind === 'reviews') return `模拟买家评论\n${listValue(listing.internal_review_drafts).map((item, index) => { const review = normalizeReviewEntry(item); return `${index + 1}. 标题（英文）：${review.title}\n标题（中文）：${review.title_zh}\n评论（英文）：${review.content}\n评论（中文）：${review.content_zh}`; }).join('\n\n')}`;
  if (kind === 'shopping') {
    return listValue(analysis.shopping_intents).map((row, index) => [
      `购物场景 ${index + 1}：${row.intent || ''}`,
      `可能的购物问题：${listValue(row.sample_queries).join('；')}`,
      `依据：${listValue(row.evidence).join('；')}`,
    ].join('\n')).join('\n\n');
  }
  return '';
}

function downloadAnalysisText(kind) {
  if (!state.result) return;
  const labels = {personas: '消费者画像', title: '标题', bullets: '五点', qa: 'QA', reviews: '模拟买家评论', shopping: '购物场景'};
  downloadTextFile(`${currentProductFileStem()}-${labels[kind] || '分析文本'}`, textForDownload(kind));
}
const strategyHelp = {
  comprehensive: '综合分析关键词、消费者需求、Listing 内容和广告策略。',
  seo_growth: '重点扩大高价值关键词覆盖，识别 P0-P4、关键词缺口和 SEO/广告联动。',
  conversion_first: '围绕消费者问题、期望结果、产品利益和购买异议，优先提升转化率。',
  new_product: '识别竞品未解决的问题，把新品差异化转化为可验证的消费者利益。',
  listing_refresh: '区分流量、CTR、CVR、价格、评价、竞争和广告问题，规划老 Listing 刷新。',
};
const gptProviderHelp = {
  official: '直接调用配置文件中的 OpenAI 官网接口和密钥。',
  middleman: '只调用配置文件中的中转站接口和密钥。',
  auto: '先调用中转站；遇到超时、连接异常、限流或 5xx 后自动切换官网。',
};

function asinValues() {
  return $('asins').value.split(/[\s,;，；]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
}

function updateAsinCount() {
  const count = new Set(asinValues()).size;
  $('asin-count').textContent = `${count} / 10`;
  $('asin-count').style.color = count >= 3 && count <= 10 ? 'var(--green)' : 'var(--muted)';
}

function productPayload() {
  const fields = ['product_name','operator_notes'];
  return Object.fromEntries(fields.map((id) => [id, $(id)?.value?.trim() || '']));
}

function selectedStrategy() {
  return $('strategy')?.value || 'comprehensive';
}

function updateStrategyHelp() {
  const strategy = selectedStrategy();
  if ($('strategy-help')) $('strategy-help').textContent = strategyHelp[strategy] || strategyHelp.comprehensive;
}

function selectedGptProvider() {
  return $('gpt-provider')?.value || 'official';
}

function selectedGptModel() {
  return $('gpt-model')?.value?.trim() || 'gpt-5.6-sol';
}

function updateGptProviderHelp() {
  const provider = selectedGptProvider();
  if ($('gpt-provider-help')) $('gpt-provider-help').textContent = gptProviderHelp[provider] || gptProviderHelp.official;
}

async function loadHealth() {
  try {
    const response = await fetch('/api/health');
    const data = await response.json();
    const gptLabel = data.openai_failover_enabled
      ? 'GPT中转站+官网已配置'
      : data.openai_primary_configured
        ? 'GPT中转站已配置'
        : data.openai_official_configured
          ? 'GPT官网已配置'
          : '本地分析';
    const parts = [data.sif_configured ? 'SIF已配置' : 'SIF演示模式', gptLabel];
    $('health-badge').textContent = parts.join(' · ');
    $('health-badge').style.color = data.sif_configured && data.openai_configured ? '#d9f6eb' : '#ffe1b8';
  } catch (error) {
    $('health-badge').textContent = '后端未连接';
  }
}

async function initAuth() {
  const token = localStorage.getItem('sif_token');
  if (!token) { location.href = '/static/auth.html'; return false; }
  try {
    const response = await nativeFetch('/api/auth/me', { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error('登录已过期');
    const data = await response.json();
    $('current-user').textContent = `${data.user.username}${data.user.role === 'admin' ? ' · 管理员' : ''}`;
    $('logout-button').classList.remove('hidden');
    loadHistory(true);
    if (data.user.role === 'admin') $('admin-link').classList.remove('hidden');
    $('logout-button').addEventListener('click', async () => {
      await nativeFetch('/api/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` } });
      localStorage.removeItem('sif_token'); location.href = '/static/auth.html';
    });
    return true;
  } catch (error) {
    localStorage.removeItem('sif_token'); location.href = '/static/auth.html'; return false;
  }
}

function formatDateTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
}

function historyStatusLabel(status) {
  return status === 'completed' ? '已完成' : status === 'failed' ? '失败' : status === 'waiting_selection' ? '待选择卖点' : status === 'running' ? '处理中' : '排队中';
}

function renderHistoryPage() {
  const list = $('history-list');
  const pagination = $('history-pagination');
  const pageInfo = $('history-page-info');
  const previous = $('history-prev');
  const next = $('history-next');
  if (!list) return;

  const rows = Array.isArray(state.historyRows) ? state.historyRows : [];
  const totalPages = Math.max(1, Math.ceil(rows.length / state.historyPageSize));
  state.historyPage = Math.min(Math.max(1, state.historyPage), totalPages);
  const start = (state.historyPage - 1) * state.historyPageSize;
  const visibleRows = rows.slice(start, start + state.historyPageSize);

  list.innerHTML = visibleRows.length ? visibleRows.map((row) => {
    const productTitle = row.product_title || '未命名产品';
    const canOpen = row.status === 'completed' || row.status === 'waiting_selection';
    const canDownload = row.status === 'completed';
    const canDelete = row.status !== 'queued' && row.status !== 'running';
    const jobId = esc(row.job_id);
    return `
    <div class="history-row">
      <div class="history-main">
        <strong title="${esc(productTitle)}">${esc(productTitle)}</strong>
        <span>${formatDateTime(row.created_at)} · ${listValue(row.asins).length} 个 ASIN · ${esc(row.strategy_name || '综合增长')} · ${esc(row.gpt_provider_name || '官网')} · ${esc(row.gpt_model || 'gpt-5.6-sol')} · ${esc(historyStatusLabel(row.status))} · ID ${esc(String(row.job_id || '').slice(0, 12))}</span>
      </div>
      <div class="history-actions">
        <button class="history-view history-action secondary-button" type="button" data-job-id="${jobId}" ${canOpen ? '' : 'disabled title="任务完成或等待卖点选择时可查看"'}>查看</button>
        <button class="history-download history-action secondary-button" type="button" data-job-id="${jobId}" data-product-title="${esc(productTitle)}" ${canDownload ? '' : 'disabled title="任务完成后可下载"'}>下载</button>
        <button class="history-delete history-action secondary-button" type="button" data-job-id="${jobId}" data-product-title="${esc(productTitle)}" ${canDelete ? '' : 'disabled title="任务处理中，完成后可删除"'}>删除</button>
      </div>
    </div>`;
  }).join('') : '<p class="muted">暂无历史分析记录</p>';

  if (pagination) pagination.classList.toggle('hidden', rows.length <= state.historyPageSize);
  if (pageInfo) pageInfo.textContent = `第 ${state.historyPage} / ${totalPages} 页`;
  if (previous) previous.disabled = state.historyPage <= 1;
  if (next) next.disabled = state.historyPage >= totalPages;
}

async function loadHistory(resetPage = false) {
  const list = $('history-list');
  const count = $('history-count');
  if (!list || !count) return;
  if (resetPage) state.historyPage = 1;
  try {
    const response = await fetch('/api/history');
    const data = await responseData(response);
    if (!response.ok) throw new Error(data.detail || '历史记录加载失败');
    state.historyRows = Array.isArray(data.data) ? data.data : [];
    count.textContent = `${Number(data.total ?? state.historyRows.length)} 条`;
    renderHistoryPage();
  } catch (error) {
    count.textContent = '加载失败';
    list.innerHTML = `<p class="muted">${esc(error.message || '历史记录加载失败')}</p>`;
    $('history-pagination')?.classList.add('hidden');
  }
}

async function viewHistoryJob(jobId) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
  const job = await response.json();
  if (!response.ok) throw new Error(job.detail || '历史任务读取失败');
  if (!['completed', 'waiting_selection'].includes(job.status) || !job.result) throw new Error('该任务尚未进入可查看阶段');
  state.jobId = job.id;
  state.result = job.result;
  state.productTitle = job.request?.product?.product_name || '';
  state.qaPage = 1;
  state.reviewPage = 1;
  setProgress(job);
  if (job.status === 'waiting_selection') {
    $('results').classList.add('hidden');
    renderSellingPointSelection(job.result.analysis || {}, false);
  } else {
    $('selling-point-panel').classList.add('hidden');
    renderResults(job.result, false);
  }
}

function setProgress(job) {
  $('progress-bar').style.width = `${job.progress || 0}%`;
  $('job-message').textContent = job.message || '';
  $('job-status').textContent = job.status === 'completed' ? '已完成' : job.status === 'failed' ? '失败' : job.status === 'waiting_selection' ? '待选择卖点' : job.status === 'running' ? '处理中' : '排队中';
  document.querySelectorAll('.stage-list [data-stage]').forEach((node) => {
    node.classList.toggle('current', node.dataset.stage === job.stage);
  });
}

function trackJob(jobId) {
  if (!jobId) return;
  state.trackedJobIds.add(jobId);
  if (!state.pollTimer) state.pollTimer = setInterval(pollTrackedJobs, 1500);
  pollTrackedJobs();
}

function updateSellingPointCount() {
  const count = document.querySelectorAll('#selling-point-list input[type="checkbox"]:checked').length;
  if ($('selling-point-count')) $('selling-point-count').textContent = `已选择 ${count} 项`;
}

function renderSellingPointSelection(analysis, scroll = true) {
  const candidates = listValue(analysis?.selling_point_candidates);
  const selectedIds = new Set(listValue(analysis?.selected_selling_points).map((item) => String(item?.id || '')));
  $('selling-point-list').innerHTML = candidates.map((item, index) => `
    <label class="selling-point-option">
      <input type="checkbox" name="selling-point" value="${esc(item.id || `point-${index + 1}`)}" ${selectedIds.has(String(item.id || '')) ? 'checked' : ''}>
      <span class="selling-point-copy">
        <strong>${index + 1}. ${esc(item.title || '候选卖点')}</strong>
        <p>${esc(item.description || '')}</p>
        <small>依据：${listValue(item.evidence).map(esc).join(' · ') || '运营资料与 SIF 数据'}</small>
      </span>
    </label>`).join('') || '<p class="muted">未能生成候选卖点，请检查任务数据后重试。</p>';
  $('selling-point-panel').classList.remove('hidden');
  updateSellingPointCount();
  if (scroll) $('selling-point-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function pollTrackedJobs() {
  const jobIds = [...state.trackedJobIds];
  if (!jobIds.length) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    return;
  }
  let historyChanged = false;
  await Promise.all(jobIds.map(async (jobId) => {
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      const job = await response.json();
      if (!response.ok) {
        state.trackedJobIds.delete(jobId);
        historyChanged = true;
        return;
      }
      if (jobId === state.jobId) {
        setProgress(job);
        if (job.status === 'completed' && job.result) {
          state.result = job.result;
          $('selling-point-panel').classList.add('hidden');
          renderResults(job.result);
        } else if (job.status === 'waiting_selection' && job.result) {
          state.result = job.result;
          $('results').classList.add('hidden');
          renderSellingPointSelection(job.result.analysis || {});
        } else if (job.status === 'failed') {
          $('job-message').textContent = job.error || '任务失败';
        }
      }
      if (job.status === 'completed' || job.status === 'failed' || job.status === 'waiting_selection') {
        state.trackedJobIds.delete(jobId);
        historyChanged = true;
      }
    } catch (error) {
      // Keep polling transient network failures; the next cycle can recover.
    }
  }));
  if (historyChanged) loadHistory();
  if (!state.trackedJobIds.size) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function renderCandidateSellingPoints(analysis) {
  const archive = $('selling-point-archive');
  if (!archive) return;
  const candidates = listValue(analysis?.selling_point_candidates);
  if (!candidates.length) {
    archive.classList.add('hidden');
    return;
  }
  const selectedIds = new Set(listValue(analysis?.selected_selling_points).map((item) => String(item?.id || '')));
  const summary = $('selling-point-archive-summary');
  if (summary) summary.textContent = `候选卖点（已保留 ${candidates.length} 项，可重新勾选后再次生成）`;
  $('selling-point-archive-list').innerHTML = candidates.map((item, index) => `
    <label class="selling-point-option">
      <input type="checkbox" name="archived-selling-point" value="${esc(item.id || `point-${index + 1}`)}" ${selectedIds.has(String(item.id || '')) ? 'checked' : ''}>
      <span class="selling-point-copy">
        <strong>${index + 1}. ${esc(item.title || '候选卖点')}</strong>
        <p>${esc(item.description || '')}</p>
        <small>依据：${listValue(item.evidence).map(esc).join(' · ') || '运营资料与 SIF 数据'}</small>
      </span>
    </label>`).join('');
  archive.classList.remove('hidden');
}

async function pollJob() {
  await pollTrackedJobs();
}

function renderKeywords(rows) {
  const safeRows = Array.isArray(rows) ? rows : [];
    $('tab-keywords').innerHTML = `
    <div class="keyword-toolbar"><input id="keyword-filter" placeholder="搜索关键词"><select id="keyword-priority"><option value="">全部层级</option><option>P0</option><option>P1</option><option>P2</option><option>P3</option><option>P4</option></select><select id="keyword-type"><option value="">全部类型</option>${['核心词','长尾词','功能词','场景词','人群词'].map((x) => `<option>${x}</option>`).join('')}</select></div>
    <div class="table-wrap"><table><thead><tr><th>关键词</th><th>类型</th><th>层级</th><th>搜索量</th><th>来源 ASIN</th><th>理由</th></tr></thead><tbody id="keyword-body"></tbody></table></div>`;
  const render = () => {
    const q = $('keyword-filter').value.toLowerCase(); const priority = $('keyword-priority').value; const type = $('keyword-type').value;
    const filtered = safeRows.filter((row) => (!q || row.term.toLowerCase().includes(q)) && (!priority || row.priority === priority) && (!type || row.type === type));
    $('keyword-body').innerHTML = filtered.map((row) => `<tr><td><strong>${esc(row.term)}</strong></td><td>${esc(row.type)}</td><td><span class="priority ${esc(row.priority)}">${esc(row.priority)}</span></td><td>${Number(row.search_volume || 0).toLocaleString()}</td><td>${listValue(row.source_asins).map(esc).join(', ')}</td><td>${esc(row.reason)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">没有匹配结果</td></tr>';
  };
  ['keyword-filter','keyword-priority','keyword-type'].forEach((id) => $(id).addEventListener('input', render));
  render();
}

function renderPersonas(rows) {
  $('tab-personas').innerHTML = `
    <div class="content-heading result-download-heading"><h3>消费者画像</h3><button class="secondary-button download-text-button" type="button" data-download-kind="personas">下载文本</button></div>
    <div class="cards">${listValue(rows).map((row) => `<article class="result-card"><h3>${esc(row.name)}</h3><p><strong>痛点</strong></p><ul>${listValue(row.pain_points).map((x) => `<li>${esc(x)}</li>`).join('')}</ul><p><strong>购买动机</strong></p><ul>${listValue(row.purchase_motivations).map((x) => `<li>${esc(x)}</li>`).join('')}</ul><p><strong>使用场景</strong></p><ul>${listValue(row.usage_scenarios).map((x) => `<li>${esc(x)}</li>`).join('')}</ul><p><strong>购买顾虑</strong></p><ul>${listValue(row.purchase_concerns).map((x) => `<li>${esc(x)}</li>`).join('')}</ul><p class="muted">证据：${listValue(row.evidence).map(esc).join(', ') || '未提供'}</p></article>`).join('') || '<p class="muted">暂无画像结果</p>'}</div>`;
}

function renderListing(listing, risks) {
  const title = String(listing?.title || '暂无').slice(0, 75);
  const titleZh = String(listing?.title_zh || '');
  const bullets = listValue(listing?.bullets).slice(0, 5);
  const bulletsZh = listValue(listing?.bullets_zh).slice(0, 5);
  const qa = listValue(listing?.qa);
  const totalQaPages = Math.max(1, Math.ceil(qa.length / state.qaPageSize));
  state.qaPage = Math.min(Math.max(1, state.qaPage), totalQaPages);
  const qaStart = (state.qaPage - 1) * state.qaPageSize;
  const visibleQa = qa.slice(qaStart, qaStart + state.qaPageSize);
  const reviews = listValue(listing?.internal_review_drafts).map(normalizeReviewEntry).filter((item) => item.content || item.content_zh);
  const totalReviewPages = Math.max(1, Math.ceil(reviews.length / state.reviewPageSize));
  state.reviewPage = Math.min(Math.max(1, state.reviewPage), totalReviewPages);
  const reviewStart = (state.reviewPage - 1) * state.reviewPageSize;
  const visibleReviews = reviews.slice(reviewStart, reviewStart + state.reviewPageSize);
  $('tab-listing').innerHTML = `
    <div class="copy-block listing-section">
      <div class="content-heading"><h3>标题 <span class="char-count">${title.length}/75</span></h3><div class="section-actions"><button class="secondary-button download-text-button" type="button" data-download-kind="title">下载文本</button><button class="secondary-button listing-action" type="button" data-regenerate-section="title">重新生成标题</button></div></div>
      <p class="listing-title">${esc(title)}</p>
      <p class="listing-translation"><strong>中文翻译：</strong>${esc(titleZh || '暂无中文翻译')}</p>
    </div>
    <div class="copy-block listing-section">
      <div class="content-heading"><h3>五点</h3><div class="section-actions"><button class="secondary-button download-text-button" type="button" data-download-kind="bullets">下载文本</button><button class="secondary-button listing-action" type="button" data-regenerate-section="bullets">重新生成五点</button></div></div>
      <ol>${bullets.map((x, index) => `<li>${esc(x)}<div class="listing-translation"><strong>中文：</strong>${esc(bulletsZh[index] || '暂无翻译')}</div></li>`).join('') || '<li class="muted">暂无五点</li>'}</ol>
    </div>
    <div class="copy-block listing-section">
      <div class="content-heading"><h3>Q&A <span class="muted">${qa.length} 条</span></h3><div class="section-actions"><label class="qa-count-field">生成数量 <input id="qa-regenerate-count" type="number" min="1" max="50" value="${Math.max(1, Math.min(50, qa.length || 5))}"></label><button class="secondary-button download-text-button" type="button" data-download-kind="qa">下载文本</button><button class="secondary-button listing-action" type="button" data-regenerate-section="qa-all">重新生成全部 Q&A</button></div></div>
      ${visibleQa.map((row, localIndex) => { const index = qaStart + localIndex; return `<div class="qa-row" data-qa-index="${index}"><div class="qa-copy"><strong>${esc(row.question || '')}</strong><span>${esc(row.answer || '')}</span><div class="listing-translation"><strong>中文问题：</strong>${esc(row.question_zh || '暂无翻译')}<br><strong>中文回答：</strong>${esc(row.answer_zh || '暂无翻译')}</div></div><div class="qa-actions"><button class="secondary-button qa-action" type="button" data-qa-action="keep" data-qa-index="${index}" ${row._kept ? 'disabled' : ''}>${row._kept ? '已保留' : '保留'}</button><button class="secondary-button qa-action" type="button" data-qa-action="delete" data-qa-index="${index}">删除</button><button class="secondary-button qa-action" type="button" data-qa-action="regenerate" data-qa-index="${index}">重新生成</button></div></div>`; }).join('') || '<p class="muted">暂无 Q&A，可通过重新生成 GPT 内容补充。</p>'}
      ${qa.length > state.qaPageSize ? `<div class="qa-pagination"><button class="secondary-button qa-page-button" type="button" data-qa-page="prev" ${state.qaPage <= 1 ? 'disabled' : ''}>上一页</button><span>第 ${state.qaPage} / ${totalQaPages} 页</span><button class="secondary-button qa-page-button" type="button" data-qa-page="next" ${state.qaPage >= totalQaPages ? 'disabled' : ''}>下一页</button></div>` : ''}
    </div>
    <div class="copy-block listing-section">
      <div class="content-heading"><h3>模拟买家评论 <span class="muted">${reviews.length} 条</span></h3><div class="section-actions"><label class="qa-count-field">生成数量 <input id="review-regenerate-count" type="number" min="1" max="50" value="${Math.max(1, Math.min(50, reviews.length || 5))}"></label><button class="secondary-button download-text-button" type="button" data-download-kind="reviews">下载文本</button><button class="secondary-button listing-action" type="button" data-regenerate-section="reviews">重新生成评论</button></div></div>
      <div class="review-list">${visibleReviews.map((review, localIndex) => { const index = reviewStart + localIndex; return `<article class="review-entry"><div class="review-content"><h4>${index + 1}. ${esc(review.title)}</h4><p class="listing-translation"><strong>中文标题：</strong>${esc(review.title_zh || '暂无翻译')}</p><p>${esc(review.content || '暂无英文评论')}</p><p class="listing-translation"><strong>中文翻译：</strong>${esc(review.content_zh || '暂无翻译')}</p></div><div class="review-actions"><button class="secondary-button upload-review-button" type="button" data-review-index="${index}" title="上传到养号网站">上传到养号网站</button></div></article>`; }).join('') || '<p class="muted">暂无模拟买家评论</p>'}</div>
      ${reviews.length > state.reviewPageSize ? `<div class="qa-pagination"><button class="secondary-button review-page-button" type="button" data-review-page="prev" ${state.reviewPage <= 1 ? 'disabled' : ''}>上一页</button><span>第 ${state.reviewPage} / ${totalReviewPages} 页</span><button class="secondary-button review-page-button" type="button" data-review-page="next" ${state.reviewPage >= totalReviewPages ? 'disabled' : ''}>下一页</button></div>` : ''}
    </div>
    <h3>风险提示</h3>${listValue(risks).map((x) => `<div class="risk">${esc(x)}</div>`).join('')}`;
}

async function regenerateListingSection(section, qaIndex, button, qaCount = null, reviewCount = null) {
  if (!state.jobId || !state.result) return;
  const listing = state.result.analysis?.listing || {};
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '生成中...';
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/regenerate-section`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({section, qa_index: qaIndex, qa_count: qaCount, review_count: reviewCount, current_listing: listing}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '内容重新生成失败');
    state.result = data.result;
    if (section === 'qa' && qaIndex == null) state.qaPage = 1;
    if (section === 'reviews') state.reviewPage = 1;
    renderListing(state.result.analysis?.listing || {}, state.result.analysis?.risks || []);
  } catch (error) {
    alert(error.message || '内容重新生成失败');
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function saveListing(listing) {
  if (!state.jobId || !state.result) return;
  const response = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/listing`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({listing}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '内容保存失败');
  state.result = data.result;
}

function renderShopping(rows) {
  $('tab-shopping').innerHTML = `
    <div class="content-heading result-download-heading"><h3>购物场景</h3><button class="secondary-button download-text-button" type="button" data-download-kind="shopping">下载文本</button></div>
    <div class="cards">${listValue(rows).map((row) => `<article class="result-card"><h3>${esc(row.intent)}</h3><p><strong>可能的购物问题</strong></p><ul>${listValue(row.sample_queries).map((x) => `<li>${esc(x)}</li>`).join('')}</ul><p class="muted">依据：${listValue(row.evidence).map(esc).join(', ')}</p></article>`).join('') || '<p class="muted">暂无购物场景</p>'}</div>`;
}

function renderSif(sifData) {
  const profiles = listValue(sifData?.profiles?.list);
  const rows = listValue(sifData?.items);
  $('tab-sif').innerHTML = `<p class="muted">来源：${esc(sifData?.source || 'SIF MCP')} · 竞品数：${rows.length}</p><div class="table-wrap"><table><thead><tr><th>ASIN</th><th>标题</th><th>品牌</th><th>价格</th><th>评分</th><th>评论数</th><th>关键词数</th></tr></thead><tbody>${profiles.map((profile) => { const item = rows.find((x) => x.asin === profile.asin) || {}; const count = listValue(item?.signals?.top_keywords).length + listValue(item?.footprint?.keywords).length; return `<tr><td>${esc(profile.asin)}</td><td>${esc(profile.title)}</td><td>${esc(profile.brand)}</td><td>${esc(profile.price)}</td><td>${esc(profile.star_rating)}</td><td>${esc(profile.rating_num)}</td><td>${count}</td></tr>`; }).join('')}</tbody></table></div>`;
}

function renderResults(result, scroll = true) {
  const analysis = result.analysis || {};
  $('results').classList.remove('hidden');
  const strategy = analysis.strategy?.name || strategyHelp[analysis.meta?.strategy] || '综合增长';
  const modeLabels = {
    openai: 'GPT中转站',
    'openai-middleman': 'GPT中转站',
    'openai-official': 'GPT官网',
    'openai-official-fallback': 'GPT官网（中转站超时后切换）',
    'local-fallback': '本地兜底',
    'local-demo': '本地演示',
  };
  const mode = modeLabels[analysis.meta?.mode] || analysis.meta?.mode || '分析完成';
  $('result-summary').textContent = `${strategy} · 关键词 ${listValue(analysis.keywords).length} 条 · 画像 ${listValue(analysis.consumer_profiles).length} 组 · ${mode}`;
  const selectedPoints = listValue(analysis.selected_selling_points);
  $('selected-selling-point-summary').innerHTML = selectedPoints.length
    ? `<strong>本次核心卖点</strong>${selectedPoints.map((item) => `<span class="selected-selling-point-chip" title="${esc(item.description || '')}">${esc(item.title || '')}</span>`).join('')}`
    : '';
  renderCandidateSellingPoints(analysis);
  renderKeywords(analysis.keywords); renderPersonas(analysis.consumer_profiles); renderListing(analysis.listing || {}, analysis.risks); renderShopping(analysis.shopping_intents); renderSif(result.sif_data || {});
  if (scroll) $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('asins').addEventListener('input', updateAsinCount);
$('strategy')?.addEventListener('change', updateStrategyHelp);
$('gpt-provider')?.addEventListener('change', updateGptProviderHelp);
$('analysis-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const asins = [...new Set(asinValues())];
  if (asins.length < 3 || asins.length > 10) { alert('请输入 3-10 个不重复的 ASIN'); return; }
  $('results').classList.add('hidden');
  $('selling-point-panel').classList.add('hidden');
  const response = await fetch('/api/analyze', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({product: productPayload(), asins, country: $('country').value, demo: false, strategy: selectedStrategy(), gpt_provider: selectedGptProvider(), gpt_model: selectedGptModel()}) });
  const data = await response.json();
  if (!response.ok) { alert(data.detail || '提交失败'); return; }
  state.jobId = data.job_id;
  state.result = null;
  state.productTitle = $('product_name')?.value?.trim() || '';
  state.qaPage = 1;
  state.reviewPage = 1;
  setProgress({status:'queued',stage:'queued',progress:0,message:data.queue_position > 1 ? `任务已进入队列，前方约 ${data.queue_position - 1} 个任务` : '任务已进入处理队列'});
  trackJob(data.job_id);
  loadHistory(true);
});

$('selling-point-list')?.addEventListener('change', updateSellingPointCount);
$('selling-point-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!state.jobId) return;
  const selectedIds = [...document.querySelectorAll('#selling-point-list input[type="checkbox"]:checked')].map((input) => input.value);
  if (!selectedIds.length) {
    alert('请至少选择一个商品卖点');
    return;
  }
  const button = $('confirm-selling-points');
  button.disabled = true;
  button.textContent = '提交中...';
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/select-selling-points`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected_ids: selectedIds}),
    });
    const data = await responseData(response);
    if (!response.ok) throw new Error(data.detail || '卖点提交失败');
    $('selling-point-panel').classList.add('hidden');
    setProgress({status: 'queued', stage: 'gpt', progress: 70, message: data.queue_position > 1 ? `卖点已确认，内容生成已排队，前方约 ${data.queue_position - 1} 个任务` : '卖点已确认，正在准备生成内容'});
    trackJob(state.jobId);
    loadHistory();
  } catch (error) {
    alert(error.message || '卖点提交失败');
  } finally {
    button.disabled = false;
    button.textContent = '按所选卖点继续生成';
  }
});

document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => { document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active')); document.querySelectorAll('.tab-content').forEach((x) => x.classList.remove('active')); button.classList.add('active'); $(`tab-${button.dataset.tab}`).classList.add('active'); }));

$('tab-listing')?.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  const qaPageAction = button.dataset.qaPage;
  if (qaPageAction) {
    const totalQaPages = Math.max(1, Math.ceil(listValue(state.result?.analysis?.listing?.qa).length / state.qaPageSize));
    if (qaPageAction === 'prev') state.qaPage = Math.max(1, state.qaPage - 1);
    if (qaPageAction === 'next') state.qaPage = Math.min(totalQaPages, state.qaPage + 1);
    renderListing(state.result?.analysis?.listing || {}, state.result?.analysis?.risks || []);
    return;
  }
  const reviewPageAction = button.dataset.reviewPage;
  if (reviewPageAction) {
    const reviews = listValue(state.result?.analysis?.listing?.internal_review_drafts)
      .map(normalizeReviewEntry)
      .filter((item) => item.content || item.content_zh);
    const totalReviewPages = Math.max(1, Math.ceil(reviews.length / state.reviewPageSize));
    if (reviewPageAction === 'prev') state.reviewPage = Math.max(1, state.reviewPage - 1);
    if (reviewPageAction === 'next') state.reviewPage = Math.min(totalReviewPages, state.reviewPage + 1);
    renderListing(state.result?.analysis?.listing || {}, state.result?.analysis?.risks || []);
    return;
  }
  const qaAction = button.dataset.qaAction;
  const qaIndex = Number(button.dataset.qaIndex);
  const listing = state.result?.analysis?.listing;
  if (!listing) return;
  if (qaAction === 'keep') {
    const qa = listValue(listing.qa);
    if (!qa[qaIndex]) return;
    qa[qaIndex]._kept = true;
    listing.qa = qa;
    renderListing(listing, state.result.analysis?.risks || []);
    try { await saveListing(listing); } catch (error) { alert(error.message || '内容保存失败'); }
    return;
  }
  if (qaAction === 'delete') {
    const qa = listValue(listing.qa);
    if (!qa[qaIndex]) return;
    qa.splice(qaIndex, 1);
    listing.qa = qa;
    state.qaPage = Math.min(state.qaPage, Math.max(1, Math.ceil(qa.length / state.qaPageSize)));
    renderListing(listing, state.result.analysis?.risks || []);
    try { await saveListing(listing); } catch (error) { alert(error.message || '内容保存失败'); }
    return;
  }
  if (qaAction === 'regenerate') {
    await regenerateListingSection('qa', qaIndex, button);
    return;
  }
  const section = button.dataset.regenerateSection;
  if (section) {
    const normalizedSection = section === 'qa-all' ? 'qa' : section;
    let qaCount = null;
    let reviewCount = null;
    if (normalizedSection === 'qa' && section === 'qa-all') {
      const input = $('qa-regenerate-count');
      qaCount = Number(input?.value || 5);
      if (!Number.isInteger(qaCount) || qaCount < 1 || qaCount > 50) {
        alert('Q&A生成数量请输入 1-50 的整数');
        input?.focus();
        return;
      }
    }
    if (normalizedSection === 'reviews') {
      const input = $('review-regenerate-count');
      reviewCount = Number(input?.value || 5);
      if (!Number.isInteger(reviewCount) || reviewCount < 1 || reviewCount > 50) {
        alert('评论生成数量请输入 1-50 的整数');
        input?.focus();
        return;
      }
    }
    await regenerateListingSection(normalizedSection, null, button, qaCount, reviewCount);
  }
});

$('history-pagination')?.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-history-page]');
  if (!button || button.disabled) return;
  if (button.dataset.historyPage === 'prev') state.historyPage -= 1;
  if (button.dataset.historyPage === 'next') state.historyPage += 1;
  renderHistoryPage();
});

document.addEventListener('click', (event) => {
  const button = event.target.closest('.download-text-button');
  if (!button) return;
  downloadAnalysisText(button.dataset.downloadKind);
});

$('history-list')?.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  const jobId = button.dataset.jobId;
  if (!jobId) return;
  if (button.classList.contains('history-view')) {
    button.disabled = true;
    button.textContent = '读取中...';
    try {
      await viewHistoryJob(jobId);
    } catch (error) {
      alert(error.message || '历史任务读取失败');
    } finally {
      button.disabled = false;
      button.textContent = '查看';
    }
    return;
  }
  if (button.classList.contains('history-download')) {
    button.disabled = true;
    button.textContent = '下载中...';
    try {
      const response = await fetch(`/api/history/${encodeURIComponent(jobId)}/export`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || '历史数据下载失败');
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      const safeTitle = String(button.dataset.productTitle || '未命名产品').replace(/[\\/:*?"<>|]/g, '_').slice(0, 80);
      anchor.href = url;
      anchor.download = `${safeTitle || 'sif-analysis'}-${jobId.slice(0, 8)}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) {
      alert(error.message || '历史数据下载失败');
    } finally {
      button.disabled = false;
      button.textContent = '下载';
    }
    return;
  }
  if (button.classList.contains('history-delete')) {
    const title = button.dataset.productTitle || '未命名产品';
    if (!window.confirm(`确定删除历史记录“${title}”吗？删除后无法恢复。`)) return;
    button.disabled = true;
    button.textContent = '删除中...';
    try {
      const response = await fetch(`/api/history/${encodeURIComponent(jobId)}`, {method: 'DELETE'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '历史记录删除失败');
      if (state.jobId === jobId) {
        state.trackedJobIds.delete(jobId);
        state.jobId = null;
        state.result = null;
        $('results').classList.add('hidden');
      }
      await loadHistory();
    } catch (error) {
      alert(error.message || '历史记录删除失败');
      button.disabled = false;
      button.textContent = '删除';
    }
  }
});

$('export-button').addEventListener('click', () => { if (!state.result) return; const blob = new Blob([JSON.stringify(state.result, null, 2)], {type:'application/json'}); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `sif-analysis-${state.jobId}.json`; anchor.click(); URL.revokeObjectURL(url); });

$('regenerate-button').addEventListener('click', async () => {
  if (!state.jobId) return;
  const button = $('regenerate-button');
  button.disabled = true;
  button.textContent = '重新生成中...';
  try {
  const response = await fetch(`/api/jobs/${state.jobId}/regenerate`, {method: 'POST'});
    const data = await responseData(response);
    if (!response.ok) throw new Error(data.detail || '重新生成失败');
    state.jobId = data.job_id;
    state.result = null;
    $('results').classList.add('hidden');
    setProgress({status: 'queued', stage: 'queued', progress: 0, message: data.queue_position > 1 ? `重新生成任务已排队，前方约 ${data.queue_position - 1} 个任务` : '已复用原 SIF 数据，等待 GPT 重新生成'});
    trackJob(data.job_id);
    loadHistory(true);
  } catch (error) {
    alert(error.message || '重新生成失败');
  } finally {
    button.disabled = false;
    button.textContent = '重新生成 GPT 内容';
  }
});

$('reuse-selling-points')?.addEventListener('click', async () => {
  if (!state.jobId || !state.result) return;
  const selectedIds = [...document.querySelectorAll('#selling-point-archive-list input[type="checkbox"]:checked')].map((input) => input.value);
  if (!selectedIds.length) {
    alert('请至少选择一个商品卖点');
    return;
  }
  const button = $('reuse-selling-points');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '提交中...';
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/regenerate`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected_ids: selectedIds}),
    });
    const data = await responseData(response);
    if (!response.ok) throw new Error(data.detail || '重新生成失败');
    state.jobId = data.job_id;
    state.result = null;
    $('results').classList.add('hidden');
    setProgress({status: 'queued', stage: 'queued', progress: 0, message: data.queue_position > 1 ? `已按所选卖点排队重新生成，前方约 ${data.queue_position - 1} 个任务` : '已复用原 SIF 数据，正在按所选卖点重新生成'});
    trackJob(data.job_id);
    loadHistory(true);
  } catch (error) {
    alert(error.message || '重新生成失败');
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});

function showUploadReviewModal(reviewIndex) {
  if (!state.jobId || !state.result) return;
  const listing = state.result.analysis?.listing || {};
  const reviews = listValue(listing.internal_review_drafts).map(normalizeReviewEntry);
  if (reviewIndex >= reviews.length) return;

  const review = reviews[reviewIndex];
  const requestData = state.result.sif_data?.request || {};
  const product = requestData.product || {};
  const asins = requestData.asins || [];
  const mainAsin = asins[0] || '';

  const today = new Date().toISOString().split('T')[0];

  const modal = document.createElement('div');
  modal.className = 'ymx-modal-overlay';
  modal.innerHTML = `
    <div class="ymx-modal">
      <div class="ymx-modal-header">
        <h3>上传到养号网站 - 直评任务</h3>
        <button class="ymx-modal-close" type="button">&times;</button>
      </div>
      <form class="ymx-modal-form" id="ymx-upload-form">
        <div class="ymx-form-row">
          <label>任务日期 <input type="date" name="task_date" value="${today}" required></label>
          <label>录入时间 <input type="date" name="entry_time" value="${today}"></label>
        </div>
        <div class="ymx-form-row">
          <label>产品名称 <input type="text" name="product_name" value="${esc(product.product_name || '')}" maxlength="255"></label>
          <label>品牌 <input type="text" name="brand" value="${esc(product.brand || '')}" maxlength="100"></label>
        </div>
        <div class="ymx-form-row">
          <label>关键词 <input type="text" name="keyword" value="${esc(product.keyword || '')}" maxlength="255" required></label>
          <label>ASIN <input type="text" name="asin" value="${esc(mainAsin)}" maxlength="50" required></label>
        </div>
        <label>星级
          <select name="star_rating" required>
            <option value="5" selected>⭐⭐⭐⭐⭐ 5星</option>
            <option value="4">⭐⭐⭐⭐ 4星</option>
            <option value="3">⭐⭐⭐ 3星</option>
            <option value="2">⭐⭐ 2星</option>
            <option value="1">⭐ 1星</option>
          </select>
        </label>
        <label>评论标题（英文）<input type="text" name="review_title" value="${esc(review.title)}" maxlength="255" required></label>
        <label>评论内容（英文）<textarea name="review_content" rows="6" required>${esc(review.content)}</textarea></label>
        <label>是否带图
          <select name="has_image" id="ymx-has-image">
            <option value="0" selected>否</option>
            <option value="1">是</option>
          </select>
        </label>
        <div id="ymx-image-upload" class="ymx-image-upload hidden">
          <label>上传评论图片（JPG/PNG/GIF，最大5MB）
            <input type="file" name="task_image" accept="image/jpeg,image/jpg,image/png,image/gif" id="ymx-task-image">
          </label>
          <div id="ymx-image-preview" class="ymx-image-preview"></div>
        </div>
        <div class="ymx-screenshot-upload">
          <label>上传产品主图（可选，JPG/PNG/GIF，最大5MB）
            <input type="file" name="screenshot" accept="image/jpeg,image/jpg,image/png,image/gif" id="ymx-screenshot">
          </label>
          <div id="ymx-screenshot-preview" class="ymx-image-preview"></div>
        </div>
        <label>运营人员 <input type="text" name="operator" value="SIF系统" maxlength="100"></label>
        <label>养号网站用户ID <input type="number" name="user_id" placeholder="请输入养号网站的用户ID" min="1" required></label>
        <label>备注 <textarea name="remark" rows="3" placeholder="可选填写备注信息"></textarea></label>
        <div class="ymx-form-actions">
          <button type="button" class="secondary-button ymx-cancel">取消</button>
          <button type="submit" class="primary-button">确认上传</button>
        </div>
      </form>
    </div>
  `;

  document.body.appendChild(modal);

  modal.querySelector('.ymx-modal-close').addEventListener('click', () => modal.remove());
  modal.querySelector('.ymx-cancel').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  // 是否带图切换
  const hasImageSelect = modal.querySelector('#ymx-has-image');
  const imageUploadDiv = modal.querySelector('#ymx-image-upload');
  const taskImageInput = modal.querySelector('#ymx-task-image');
  const imagePreview = modal.querySelector('#ymx-image-preview');
  const screenshotInput = modal.querySelector('#ymx-screenshot');
  const screenshotPreview = modal.querySelector('#ymx-screenshot-preview');

  hasImageSelect.addEventListener('change', (e) => {
    if (e.target.value === '1') {
      imageUploadDiv.classList.remove('hidden');
      taskImageInput.required = true;
    } else {
      imageUploadDiv.classList.add('hidden');
      taskImageInput.required = false;
      taskImageInput.value = '';
      imagePreview.innerHTML = '';
    }
  });

  // 评论图片预览
  taskImageInput.addEventListener('change', (e) => {
    const file = e.target.files[0];
    imagePreview.innerHTML = '';
    if (file) {
      if (file.size > 5 * 1024 * 1024) {
        alert('图片大小不能超过 5MB');
        e.target.value = '';
        return;
      }
      const reader = new FileReader();
      reader.onload = (event) => {
        imagePreview.innerHTML = `<img src="${event.target.result}" alt="评论图片预览" style="max-width: 200px; max-height: 200px; border: 1px solid var(--line); border-radius: 4px;">`;
      };
      reader.readAsDataURL(file);
    }
  });

  // 产品主图预览
  screenshotInput.addEventListener('change', (e) => {
    const file = e.target.files[0];
    screenshotPreview.innerHTML = '';
    if (file) {
      if (file.size > 5 * 1024 * 1024) {
        alert('图片大小不能超过 5MB');
        e.target.value = '';
        return;
      }
      const reader = new FileReader();
      reader.onload = (event) => {
        screenshotPreview.innerHTML = `<img src="${event.target.result}" alt="产品主图预览" style="max-width: 200px; max-height: 200px; border: 1px solid var(--line); border-radius: 4px;">`;
      };
      reader.readAsDataURL(file);
    }
  });

  modal.querySelector('#ymx-upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton.textContent;
    submitButton.disabled = true;
    submitButton.textContent = '上传中...';

    const hasImage = parseInt(formData.get('has_image'));
    let taskImagePath = '';
    let screenshotPath = '';

    // 养号网站 API 配置
    const YMX_BASE_URL = 'http://localhost/ymx.com/api';
    const YMX_API_KEY = 'sif-import-key-2026';

    try {
      // 如果选择带图，直接上传评论图片到养号网站
      if (hasImage === 1) {
        const imageFile = formData.get('task_image');
        if (!imageFile || imageFile.size === 0) {
          throw new Error('请选择要上传的评论图片');
        }

        const imageFormData = new FormData();
        imageFormData.append('task_image', imageFile);

        const imageResponse = await nativeFetch(`${YMX_BASE_URL}/upload_task_image.php`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${YMX_API_KEY}`
          },
          body: imageFormData
        });

        const imageData = await imageResponse.json();
        if (!imageResponse.ok || !imageData.success) {
          throw new Error(imageData.error || '评论图片上传失败');
        }

        taskImagePath = imageData.task_image_path;
      }

      // 如果上传了产品主图，直接上传到养号网站
      const screenshotFile = formData.get('screenshot');
      if (screenshotFile && screenshotFile.size > 0) {
        const screenshotFormData = new FormData();
        screenshotFormData.append('screenshot', screenshotFile);

        const screenshotResponse = await nativeFetch(`${YMX_BASE_URL}/upload_screenshot.php`, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${YMX_API_KEY}`
          },
          body: screenshotFormData
        });

        const screenshotData = await screenshotResponse.json();
        if (!screenshotResponse.ok || !screenshotData.success) {
          throw new Error(screenshotData.error || '产品主图上传失败');
        }

        screenshotPath = screenshotData.screenshot_path;
      }

      // 准备评论数据，直接发送到养号网站
      const uploadData = {
        reviews: [{
          title_en: formData.get('review_title'),
          review_en: formData.get('review_content'),
          asin: formData.get('asin'),
          keyword: formData.get('keyword'),
          product_name: formData.get('product_name'),
          brand: formData.get('brand'),
          rating: parseInt(formData.get('star_rating'))
        }],
        task_date: formData.get('task_date'),
        operator: formData.get('operator'),
        entry_time: formData.get('entry_time'),
        has_image: hasImage,
        task_image_path: taskImagePath || null, // 空字符串改为 null
        screenshot_path: screenshotPath || null, // 空字符串改为 null
        remark: formData.get('remark'),
        user_id: parseInt(formData.get('user_id')) // 添加用户ID
      };

      console.log('[Upload Debug] Upload data:', uploadData);

      // 直接上传到养号网站
      const response = await nativeFetch(`${YMX_BASE_URL}/import_reviews_from_sif.php`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${YMX_API_KEY}`
        },
        body: JSON.stringify(uploadData)
      });

      const data = await response.json();
      if (!response.ok || !data.success) {
        throw new Error(data.error || data.details || '上传失败');
      }

      alert(`✅ 上传成功！\n\n任务ID: ${data.inserted_ids ? data.inserted_ids[0] : '未知'}\n\n直评任务已添加到养号网站，状态为"待处理"。`);
      modal.remove();

    } catch (error) {
      if (error.message.includes('Failed to fetch') || error.message.includes('NetworkError')) {
        alert('连接养号网站失败\n\n请确保养号网站正常运行：http://localhost/ymx.com\n如果使用 XAMPP，请检查 Apache 是否启动。');
      } else {
        alert('上传失败：' + (error.message || '未知错误'));
      }
      submitButton.disabled = false;
      submitButton.textContent = originalText;
    }
  });
}

document.addEventListener('click', (e) => {
  if (e.target.classList.contains('upload-review-button')) {
    const reviewIndex = parseInt(e.target.dataset.reviewIndex);
    showUploadReviewModal(reviewIndex);
  }
});

updateAsinCount(); updateStrategyHelp(); updateGptProviderHelp(); initAuth().then(ok => { if (ok) loadHealth(); });
