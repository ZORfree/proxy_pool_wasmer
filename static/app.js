/* =========================================================================
   Proxy Pool Dashboard - Application Logic
   ========================================================================= */

const API = '/api';

// --- Tab Navigation ---
function switchTab(tabName) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-' + tabName).classList.add('active');
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');

    if (tabName === 'overview') loadStats();
    if (tabName === 'proxies') loadProxies();
    if (tabName === 'sources') loadSources();
    if (tabName === 'settings') loadSettings();
}

// --- Toast Notifications ---
function toast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
        el.style.opacity = '0';
        el.style.transform = 'translateX(30px)';
        el.style.transition = 'all 0.3s ease';
        setTimeout(() => el.remove(), 300);
    }, 3500);
}

// --- Modal ---
function openModal(id) {
    document.getElementById(id).classList.add('active');
}
function closeModal(id) {
    document.getElementById(id).classList.remove('active');
}

// Close modal on overlay click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.classList.remove('active');
    });
});

// --- API Helpers ---
async function apiGet(path) {
    const resp = await fetch(API + path);
    return resp.json();
}
async function apiPost(path, body) {
    const resp = await fetch(API + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    return resp.json();
}
async function apiPut(path, body) {
    const resp = await fetch(API + path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    return resp.json();
}
async function apiDelete(path) {
    const resp = await fetch(API + path, { method: 'DELETE' });
    return resp.json();
}

function decodeUrlPart(value) {
    try {
        return decodeURIComponent(value || '');
    } catch (e) {
        return value || '';
    }
}

function escapeAttr(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    }[ch]));
}

function proxyHasAuth(proxy) {
    return Boolean((proxy.username || '') && (proxy.password || ''));
}

function formatProxyUrl(proxy) {
    const proto = (proxy.protocol || 'http').toLowerCase();
    const username = proxy.username || '';
    const password = proxy.password || '';
    const auth = (username || password)
        ? `${encodeURIComponent(username)}:${encodeURIComponent(password)}@`
        : '';
    return `${proto}://${auth}${proxy.ip}:${proxy.port}`;
}

function parseProxyInputLine(line, defaultProtocol) {
    const raw = line.trim();
    if (!raw) return null;

    const urlText = raw.includes('://') ? raw : `${defaultProtocol}://${raw}`;
    try {
        const url = new URL(urlText);
        const protocol = url.protocol.replace(':', '').toLowerCase();
        const port = parseInt(url.port, 10);
        const ip = url.hostname.replace(/^\[|\]$/g, '');
        if (!ip || !Number.isInteger(port) || port < 1 || port > 65535) return null;
        if (!['http', 'https', 'socks4', 'socks5'].includes(protocol)) return null;
        return {
            ip,
            port,
            protocol,
            username: decodeUrlPart(url.username),
            password: decodeUrlPart(url.password),
        };
    } catch (e) {
        return null;
    }
}

// =========================================================================
// Overview / Stats
// =========================================================================

const CHART_COLORS = [
    'var(--accent-indigo)', 'var(--accent-emerald)', 'var(--accent-amber)',
    'var(--accent-cyan)', 'var(--accent-violet)', 'var(--accent-rose)',
    '#38bdf8', '#a78bfa', '#fb923c', '#e879f9',
];

async function loadStats() {
    try {
        const stats = await apiGet('/stats');

        document.getElementById('statTotal').textContent = stats.total || 0;

        const protos = stats.by_protocol || {};
        const countries = stats.by_country || {};

        document.getElementById('statProtocols').textContent = Object.keys(protos).length;
        document.getElementById('statCountries').textContent = Object.keys(countries).length;

        renderBarChart('chartProtocol', protos);
        renderBarChart('chartCountry', countries, 10);
    } catch (e) {
        console.error('loadStats error:', e);
    }
}

function renderBarChart(containerId, data, limit) {
    const container = document.getElementById(containerId);
    let entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
    if (limit) entries = entries.slice(0, limit);

    if (entries.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="icon">📭</div><p>暂无数据</p></div>';
        return;
    }

    const max = entries[0][1];
    container.innerHTML = entries.map(([label, count], i) => {
        const pct = max > 0 ? (count / max * 100) : 0;
        const color = CHART_COLORS[i % CHART_COLORS.length];
        return `
            <div class="bar-row">
                <div class="bar-label">${label || '—'}</div>
                <div class="bar-track">
                    <div class="bar-fill" style="width:${pct}%;background:${color};">${count}</div>
                </div>
            </div>`;
    }).join('');
}

// =========================================================================
// Proxy List
// =========================================================================

let allProxies = [];
let currentPage = 1;
let pageSize = 20;
let proxySortState = { key: '', direction: 'asc' };

const PROXY_SORT_FIELDS = {
    country: 'string',
    latency: 'number',
    score: 'number',
    added_time: 'date',
    last_check: 'date',
};

function getProxySortValue(proxy, key) {
    const type = PROXY_SORT_FIELDS[key];
    const raw = proxy ? proxy[key] : undefined;

    if (raw === undefined || raw === null || raw === '') {
        return { missing: true, value: null };
    }

    if (type === 'number') {
        const value = Number(raw);
        return Number.isFinite(value) && value >= 0
            ? { missing: false, value }
            : { missing: true, value: null };
    }

    if (type === 'date') {
        const value = Date.parse(String(raw).replace(' ', 'T'));
        return Number.isFinite(value)
            ? { missing: false, value }
            : { missing: true, value: null };
    }

    return { missing: false, value: String(raw).toUpperCase() };
}

function sortProxies(proxies, sortState = proxySortState) {
    if (!Array.isArray(proxies) || !sortState.key || !PROXY_SORT_FIELDS[sortState.key]) {
        return Array.isArray(proxies) ? proxies.slice() : [];
    }

    const direction = sortState.direction === 'desc' ? -1 : 1;
    return proxies
        .map((proxy, index) => ({ proxy, index }))
        .sort((a, b) => {
            const left = getProxySortValue(a.proxy, sortState.key);
            const right = getProxySortValue(b.proxy, sortState.key);

            if (left.missing && right.missing) return a.index - b.index;
            if (left.missing) return 1;
            if (right.missing) return -1;

            let result = 0;
            if (PROXY_SORT_FIELDS[sortState.key] === 'string') {
                result = left.value.localeCompare(right.value);
            } else {
                result = left.value - right.value;
            }

            return result === 0 ? a.index - b.index : result * direction;
        })
        .map(item => item.proxy);
}

function updateProxySortHeaders() {
    document.querySelectorAll('[data-sort-key]').forEach(th => {
        const key = th.dataset.sortKey;
        const active = key === proxySortState.key;
        const direction = active ? proxySortState.direction : '';
        const label = th.dataset.sortLabel || th.textContent.trim();
        const title = active
            ? `当前按${label}${direction === 'asc' ? '升序' : '降序'}排序，点击切换为${direction === 'asc' ? '降序' : '升序'}`
            : `点击按${label}升序排序`;

        th.classList.toggle('is-sorted', active);
        th.classList.toggle('is-sort-asc', direction === 'asc');
        th.classList.toggle('is-sort-desc', direction === 'desc');
        th.setAttribute('aria-sort', active ? (direction === 'asc' ? 'ascending' : 'descending') : 'none');

        const button = th.querySelector('.sort-header');
        if (button) button.title = title;
    });
}

function toggleProxySort(key) {
    if (!PROXY_SORT_FIELDS[key]) return;

    proxySortState = {
        key,
        direction: proxySortState.key === key && proxySortState.direction === 'asc' ? 'desc' : 'asc',
    };
    currentPage = 1;
    renderProxyPage();
}

async function loadProxies() {
    const protocol = document.getElementById('filterProtocol').value;
    const country = document.getElementById('filterCountry').value.trim();
    const maxLatency = document.getElementById('filterMaxLatency').value;
    const maxScore = document.getElementById('filterMaxScore').value;
    const hasAuth = document.getElementById('filterHasAuth').value;

    let qs = [];
    if (protocol) qs.push(`protocol=${encodeURIComponent(protocol)}`);
    if (country) qs.push(`country=${encodeURIComponent(country)}`);
    if (maxLatency) qs.push(`max_latency=${encodeURIComponent(maxLatency)}`);
    if (maxScore) qs.push(`max_score=${encodeURIComponent(maxScore)}`);
    if (hasAuth) qs.push(`has_auth=${encodeURIComponent(hasAuth)}`);
    const query = qs.length ? '?' + qs.join('&') : '';

    try {
        const proxies = await apiGet('/all' + query);
        allProxies = proxies;
        currentPage = 1;
        renderProxyPage();
    } catch (e) {
        console.error('loadProxies error:', e);
        document.getElementById('proxyTableBody').innerHTML =
            '<tr><td colspan="12" class="empty-state"><p>加载失败</p></td></tr>';
    }
}

function renderProxyPage() {
    const total = allProxies.length;
    document.getElementById('totalProxies').textContent = total;
    
    const pageSizeSelect = document.getElementById('pageSize');
    if (pageSizeSelect) {
        pageSize = parseInt(pageSizeSelect.value) || 20;
    }
    
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    document.getElementById('totalPages').textContent = totalPages;
    
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;
    document.getElementById('pageInput').value = currentPage;
    
    const sortedProxies = sortProxies(allProxies);
    const start = (currentPage - 1) * pageSize;
    const end = start + pageSize;
    const pageData = sortedProxies.slice(start, end);
    
    document.getElementById('selectAll').checked = false;
    updateProxySortHeaders();
    renderProxyTable(pageData);
}

function changePageSize() {
    currentPage = 1;
    renderProxyPage();
}

function prevPage() {
    if (currentPage > 1) {
        currentPage--;
        renderProxyPage();
    }
}

function nextPage() {
    const total = allProxies.length;
    const totalPages = Math.ceil(total / pageSize);
    if (currentPage < totalPages) {
        currentPage++;
        renderProxyPage();
    }
}

function jumpPage() {
    let page = parseInt(document.getElementById('pageInput').value);
    const total = allProxies.length;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    
    if (isNaN(page) || page < 1) page = 1;
    if (page > totalPages) page = totalPages;
    
    currentPage = page;
    renderProxyPage();
}

function renderProxyTable(proxies) {
    const tbody = document.getElementById('proxyTableBody');

    if (!proxies || proxies.length === 0) {
        tbody.innerHTML = '<tr><td colspan="12" class="empty-state"><div class="icon">🔍</div><p>暂无代理数据，请先抓取代理</p></td></tr>';
        return;
    }

    tbody.innerHTML = proxies.map(p => {
        const proto = (p.protocol || 'http').toLowerCase();
        const badgeClass = `badge-${proto}`;
        const hasAuth = p.has_auth !== undefined ? Boolean(p.has_auth) : proxyHasAuth(p);
        const authBadge = hasAuth
            ? '<span class="badge badge-https">有</span>'
            : '<span class="badge badge-socks4">无</span>';
        const score = p.score || 0;
        const latency = p.latency >= 0 ? `${Math.round(p.latency)}ms` : '—';
        const latencyColor = p.latency < 0 ? '' :
                            p.latency < 500 ? 'color:var(--accent-emerald)' :
                            p.latency < 2000 ? 'color:var(--accent-amber)' : 'color:var(--accent-rose)';

        let scoreStyle = '';
        let scoreText = '';
        if (score <= 10) {
            scoreStyle = 'background:var(--accent-emerald);color:#fff;border:none';
            scoreText = `${score} 纯净`;
        } else if (score <= 30) {
            scoreStyle = 'background:#10b981;color:#fff;border:none'; // teal/emerald variant
            scoreText = `${score} 低风险`;
        } else if (score <= 60) {
            scoreStyle = 'background:var(--accent-amber);color:#fff;border:none';
            scoreText = `${score} 中风险`;
        } else if (score <= 80) {
            scoreStyle = 'background:#f97316;color:#fff;border:none'; // orange
            scoreText = `${score} 高风险`;
        } else {
            scoreStyle = 'background:var(--accent-rose);color:#fff;border:none';
            scoreText = `${score} 极度风险`;
        }

        return `<tr>
            <td><input type="checkbox" class="proxy-checkbox" data-ip="${escapeAttr(p.ip)}" data-port="${escapeAttr(p.port)}" data-protocol="${escapeAttr(proto)}" data-username="${escapeAttr(p.username || '')}" data-password="${escapeAttr(p.password || '')}"></td>
            <td style="color:var(--text-primary);font-weight:500;font-family:monospace;">${p.ip}</td>
            <td style="font-family:monospace;">${p.port}</td>
            <td><span class="badge ${badgeClass}">${proto.toUpperCase()}</span></td>
            <td>${authBadge}</td>
            <td>${p.country ? `<span class="badge badge-country">${p.country}</span>` : '—'}</td>
            <td style="${latencyColor}">${latency}</td>
            <td><span class="badge" style="${scoreStyle}">${scoreText}</span></td>
            <td style="font-size:0.75rem;">${p.added_time || '—'}</td>
            <td style="font-size:0.75rem;">${p.last_check || '—'}</td>
            <td style="font-size:0.75rem;max-width:120px;overflow:hidden;text-overflow:ellipsis;" title="${p.source || ''}">${p.source || '—'}</td>
            <td>
                <button class="btn btn-sm btn-danger" data-ip="${escapeAttr(p.ip)}" data-port="${escapeAttr(p.port)}" data-protocol="${escapeAttr(proto)}" data-username="${escapeAttr(p.username || '')}" data-password="${escapeAttr(p.password || '')}" onclick="deleteProxyFromButton(this)">删除</button>
            </td>
        </tr>`;
    }).join('');
}

function toggleSelectAll(el) {
    document.querySelectorAll('.proxy-checkbox').forEach(cb => cb.checked = el.checked);
}

function getSelectedProxies() {
    const selected = [];
    document.querySelectorAll('.proxy-checkbox:checked').forEach(cb => {
        selected.push({
            ip: cb.dataset.ip,
            port: parseInt(cb.dataset.port),
            protocol: cb.dataset.protocol,
            username: cb.dataset.username || '',
            password: cb.dataset.password || '',
        });
    });
    return selected;
}

async function batchDelete() {
    const selected = getSelectedProxies();
    if (selected.length === 0) {
        toast('请先选择要删除的代理', 'warning');
        return;
    }

    if (!confirm(`确认批量删除选中的 ${selected.length} 个代理？`)) return;

    try {
        const result = await apiPost('/batch-delete', { proxies: selected });
        if (result.success) {
            toast(`成功删除 ${result.count} 个代理`, 'success');
            loadProxies();
            loadStats();
        } else {
            toast('批量删除失败', 'error');
        }
    } catch (e) {
        toast('批量删除请求失败', 'error');
    }
}

async function batchValidate() {
    const selected = getSelectedProxies();
    if (selected.length === 0) {
        toast('请先选择要验证的代理', 'warning');
        return;
    }

    try {
        const result = await apiPost('/batch-check', { proxies: selected });
        if (result.success) {
            toast(result.message, 'success');
            // Reset selection
            document.getElementById('selectAll').checked = false;
            document.querySelectorAll('.proxy-checkbox').forEach(cb => cb.checked = false);
        } else {
            toast('批量验证提交失败: ' + result.message, 'error');
        }
    } catch (e) {
        toast('批量验证请求失败', 'error');
    }
}

async function addProxy() {
    const listText = document.getElementById('addProxyList').value.trim();
    const protocol = document.getElementById('addProxyProtocol').value;

    if (!listText) {
        toast('请填写代理列表', 'error');
        return;
    }

    const lines = listText.split('\n').map(l => l.trim()).filter(l => l);
    const proxies = [];
    for (const line of lines) {
        const proxy = parseProxyInputLine(line, protocol);
        if (proxy) proxies.push(proxy);
    }

    if (proxies.length === 0) {
        toast('没有解析到有效的代理，请检查格式', 'error');
        return;
    }

    const btn = document.querySelector('#addProxyModal .btn-primary');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 验证并添加中...';

    try {
        const result = await apiPost('/batch-add', { proxies });
        if (result.success) {
            const data = result.data;
            toast(`完成: 成功 ${data.valid} 个，失败 ${data.invalid} 个`, 'success');
            closeModal('addProxyModal');
            document.getElementById('addProxyList').value = '';
            loadProxies();
            loadStats();
        } else {
            toast('添加失败: ' + (result.message || '未知错误'), 'error');
        }
    } catch (e) {
        toast('添加请求失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function deleteProxy(ip, port, protocol, username = '', password = '') {
    if (!confirm(`确认删除 ${ip}:${port} ?`)) return;
    try {
        const qs = new URLSearchParams({
            ip,
            port,
            protocol,
            username,
            password,
        });
        await apiDelete(`/proxy?${qs.toString()}`);
        toast('已删除', 'success');
        loadProxies();
        loadStats();
    } catch (e) {
        toast('删除失败', 'error');
    }
}

function deleteProxyFromButton(button) {
    deleteProxy(
        button.dataset.ip,
        button.dataset.port,
        button.dataset.protocol,
        button.dataset.username || '',
        button.dataset.password || '',
    );
}

function exportProxies() {
    const selected = getSelectedProxies();
    const lines = [];

    if (selected.length > 0) {
        // Export selected only
        selected.forEach(p => {
            lines.push(formatProxyUrl(p));
        });
    } else {
        // Export all on current page
        const rows = document.querySelectorAll('#proxyTableBody tr');
        rows.forEach(row => {
            const cb = row.querySelector('.proxy-checkbox');
            if (cb) {
                const ip = cb.dataset.ip;
                const port = cb.dataset.port;
                const proto = cb.dataset.protocol;
                const username = cb.dataset.username || '';
                const password = cb.dataset.password || '';
                lines.push(formatProxyUrl({ ip, port, protocol: proto, username, password }));
            }
        });
    }

    if (lines.length === 0) {
        toast('没有可导出的代理', 'error');
        return;
    }

    const text = lines.join('\n');
    navigator.clipboard.writeText(text).then(() => {
        toast(`已复制 ${lines.length} 条代理到剪贴板`, 'success');
    }).catch(() => {
        // Fallback: download file
        const blob = new Blob([text], { type: 'text/plain' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'proxies.txt';
        a.click();
        toast(`已下载 ${lines.length} 条代理`, 'success');
    });
}

// =========================================================================
// Sources
// =========================================================================

async function loadSources() {
    try {
        const sources = await apiGet('/sources?active_only=false');
        window.currentSources = sources;
        renderSourceTable(sources);
    } catch (e) {
        console.error('loadSources error:', e);
    }
}

function renderSourceTable(sources) {
    const tbody = document.getElementById('sourceTableBody');

    if (!sources || sources.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state"><div class="icon">🔗</div><p>暂无自定义代理源<br><small style="color:var(--text-muted)">系统内置源会在抓取时自动使用</small></p></td></tr>';
        return;
    }

    tbody.innerHTML = sources.map(s => {
        const statusBadge = s.status
            ? '<span class="badge badge-https">启用</span>'
            : '<span class="badge badge-socks4">禁用</span>';
        const protoBadge = `<span class="badge badge-country">${s.protocol || 'auto'}</span>`;
        return `<tr>
            <td>${s.id}</td>
            <td style="color:var(--text-primary);font-weight:500;">${s.name || '—'}</td>
            <td style="font-size:0.75rem;max-width:250px;overflow:hidden;text-overflow:ellipsis;" title="${s.url}">${s.url}</td>
            <td><span class="badge badge-http">${s.type}</span></td>
            <td>${protoBadge}</td>
            <td>${statusBadge}</td>
            <td>
                <button class="btn btn-sm" onclick="editSource(${s.id})">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteSource(${s.id})">删除</button>
            </td>
        </tr>`;
    }).join('');
}

async function addSource() {
    const name = document.getElementById('addSourceName').value.trim();
    const url = document.getElementById('addSourceUrl').value.trim();
    const type = document.getElementById('addSourceType').value;
    const pattern = document.getElementById('addSourcePattern').value.trim();
    const protocol = document.getElementById('addSourceProtocol').value;
    const delimiter = document.getElementById('addSourceDelimiter').value;

    if (!url) {
        toast('请填写 URL', 'error');
        return;
    }

    try {
        await apiPost('/sources', { name, url, type, pattern, protocol, delimiter, status: 1 });
        toast('代理源添加成功', 'success');
        closeModal('addSourceModal');
        document.getElementById('addSourceName').value = '';
        document.getElementById('addSourceUrl').value = '';
        document.getElementById('addSourcePattern').value = '';
        loadSources();
    } catch (e) {
        toast('添加失败: ' + e.message, 'error');
    }
}

function editSource(id) {
    const source = window.currentSources.find(s => s.id === id);
    if (!source) return;
    
    document.getElementById('editSourceId').value = source.id;
    document.getElementById('editSourceName').value = source.name || '';
    document.getElementById('editSourceUrl').value = source.url || '';
    document.getElementById('editSourceType').value = source.type || 'web';
    document.getElementById('editSourcePattern').value = source.pattern || '';
    document.getElementById('editSourceProtocol').value = source.protocol || 'auto';
    document.getElementById('editSourceDelimiter').value = source.delimiter || 'newline';
    document.getElementById('editSourceStatus').value = source.status !== undefined ? source.status : 1;
    
    openModal('editSourceModal');
}

async function saveEditSource() {
    const id = parseInt(document.getElementById('editSourceId').value);
    const name = document.getElementById('editSourceName').value.trim();
    const url = document.getElementById('editSourceUrl').value.trim();
    const type = document.getElementById('editSourceType').value;
    const pattern = document.getElementById('editSourcePattern').value.trim();
    const protocol = document.getElementById('editSourceProtocol').value;
    const delimiter = document.getElementById('editSourceDelimiter').value;
    const status = parseInt(document.getElementById('editSourceStatus').value);

    if (!url) {
        toast('请填写 URL', 'error');
        return;
    }

    try {
        await apiPut(`/sources/${id}`, { name, url, type, pattern, protocol, delimiter, status });
        toast('代理源已更新', 'success');
        closeModal('editSourceModal');
        loadSources();
    } catch (e) {
        toast('更新失败: ' + e.message, 'error');
    }
}

async function deleteSource(id) {
    if (!confirm('确认删除此代理源？')) return;
    try {
        await apiDelete(`/sources/${id}`);
        toast('已删除', 'success');
        loadSources();
    } catch (e) {
        toast('删除失败', 'error');
    }
}

// =========================================================================
// Settings
// =========================================================================

const SETTING_FIELDS = {
    validate_url: 'settingValidateUrl',
    validate_timeout: 'settingValidateTimeout',
    max_concurrency: 'settingMaxConcurrency',
    fetch_interval: 'settingFetchInterval',
    validate_interval: 'settingValidateInterval',
};

async function loadSettings() {
    try {
        const settings = await apiGet('/settings');
        for (const [key, inputId] of Object.entries(SETTING_FIELDS)) {
            const el = document.getElementById(inputId);
            if (el && settings[key] !== undefined) {
                el.value = settings[key];
            }
        }
    } catch (e) {
        console.error('loadSettings error:', e);
    }
}

async function saveSettings() {
    const settings = {};
    for (const [key, inputId] of Object.entries(SETTING_FIELDS)) {
        const el = document.getElementById(inputId);
        if (el && el.value) {
            settings[key] = el.value;
        }
    }

    try {
        await apiPut('/settings', { settings });
        toast('设置已保存', 'success');
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
}

// =========================================================================
// Fetch / Validate Triggers
// =========================================================================

async function triggerFetch() {
    const btn = document.getElementById('btnFetch');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 抓取中...';

    try {
        const result = await apiGet('/fetch');
        if (result.success) {
            toast(result.message, 'success');
        } else {
            toast('抓取失败: ' + (result.message || '未知错误'), 'error');
        }
    } catch (e) {
        toast('抓取请求失败: ' + e.message, 'error');
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.innerHTML = '🔄 抓取代理';
            loadStats();
            loadProxies();
        }, 3000);
    }
}

async function triggerValidate() {
    const btn = document.getElementById('btnValidate');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 验证中...';

    try {
        const result = await apiGet('/check');
        if (result.success) {
            toast(result.message, 'success');
        } else {
            toast('验证失败: ' + (result.message || '未知错误'), 'error');
        }
    } catch (e) {
        toast('验证请求失败: ' + e.message, 'error');
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.innerHTML = '✅ 验证代理';
            loadStats();
            loadProxies();
        }, 3000);
    }
}

// =========================================================================
// DB Status
// =========================================================================

async function checkDbStatus() {
    const el = document.getElementById('dbStatusContent');
    el.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">检查中...</p>';

    try {
        const result = await apiGet('/db-status');
        if (result.status === 'connected') {
            el.innerHTML = `
                <div style="display:flex;align-items:center;gap:0.5rem;">
                    <span style="color:var(--accent-emerald);font-size:1.5rem;">●</span>
                    <div>
                        <div style="color:var(--accent-emerald);font-weight:600;">已连接</div>
                        <div style="font-size:0.8rem;color:var(--text-muted);">代理数量: ${result.proxy_count}</div>
                    </div>
                </div>`;
        } else {
            el.innerHTML = `
                <div style="display:flex;align-items:center;gap:0.5rem;">
                    <span style="color:var(--accent-rose);font-size:1.5rem;">●</span>
                    <div>
                        <div style="color:var(--accent-rose);font-weight:600;">连接失败</div>
                        <div style="font-size:0.8rem;color:var(--text-muted);">${result.message || ''}</div>
                    </div>
                </div>`;
        }
    } catch (e) {
        el.innerHTML = `<p style="color:var(--accent-rose);font-size:0.85rem;">请求失败: ${e.message}</p>`;
    }
}

// =========================================================================
// Initialize
// =========================================================================

document.addEventListener('DOMContentLoaded', () => {
    loadStats();
});
