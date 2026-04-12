const BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
const SYMBOLS = "!@#%_-+";

/**
 * @typedef {{symbols: ?string, length: number, platform: string}} KeyConfig
 */

/**
 * @type {Object.<string, KeyConfig>}
 */
const ALIAS_MAP = {};
let OFFLINE_MODE = false;
const PAGE_URL = new URL(window.location.href);
const PAGE_AUTH_TOKEN = PAGE_URL.searchParams.get('auth_token');

// DOM Elements
const MAIN_INPUT_DOM = document.getElementById('mk');
const FP_BADGE_DOM = document.getElementById('mk-fingerprint'); // 修正变量名拼写 Badget -> Badge
const TOAST_DOM = document.getElementById('toast');
const CONFIG_NAME_INPUT_DOM = document.getElementById('config-name');
const CONFIG_SUGGESTIONS_DOM = document.getElementById('config-suggestions');
let ACTIVE_SUGGESTION_INDEX = -1;

// 1. Master Key 指纹逻辑
MAIN_INPUT_DOM.addEventListener('input', async e => {
    if (e.target.value.length === 0) {
        FP_BADGE_DOM.style.opacity = '0';
        return;
    }
    const enc = new TextEncoder();
    const hash_buffer = await crypto.subtle.digest('SHA-256', enc.encode(e.target.value));
    const hash_array = Array.from(new Uint8Array(hash_buffer));

    // 视觉生成逻辑
    const r = hash_array[0];
    const g = hash_array[1];
    const b = hash_array[2];
    const code = hash_array.slice(0, 2).map(b => b.toString(16).padStart(2, '0')).join('').toUpperCase();
    const brightness = (r * 299 + g * 587 + b * 114) / 1000;

    FP_BADGE_DOM.innerText = code;
    FP_BADGE_DOM.style.backgroundColor = `rgb(${r},${g},${b})`;
    FP_BADGE_DOM.style.color = brightness > 128 ? '#000' : '#fff';
    FP_BADGE_DOM.style.opacity = '1';
});

// 2. 远程配置获取
const CONFIG_API_ENDPOINT = '/vault/api/key_configs';

function withAuthFetchOptions(options = {}) {
    const nextOptions = {...options};
    const headers = new Headers(options.headers || {});
    if (PAGE_AUTH_TOKEN && !headers.has('auth_token')) {
        headers.set('auth_token', PAGE_AUTH_TOKEN);
    }
    nextOptions.headers = headers;
    return nextOptions;
}

async function fetchKeyConfigs(){
    try {
        // 如果页面加载时就在离线状态（如 ServiceWorker 启动），这里可能直接抛错
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);
        const response = await fetch(CONFIG_API_ENDPOINT, withAuthFetchOptions({
            signal: controller.signal
        }));
        clearTimeout(timeoutId);
        if (!response.ok) {
            console.error(`[Vault] Fetch configs failed: Status ${response.status}`);
            activateOfflineMode(`Auth/config fetch failed (${response.status})`);
            return;
        }
        const remoteConfigs = await response.json();
        Object.assign(ALIAS_MAP, remoteConfigs);
        refreshAliasSuggestions();
        console.log(`[Vault] Successfully loaded ${Object.keys(remoteConfigs).length} configs.`);
    } catch (error) {
        console.error('[Vault] Fetch configs failed:', error);
        activateOfflineMode('Config request failed');
    }
}
// 启动加载
fetchKeyConfigs().then();

// [已删除] 旧的 document.getElementById('site').addEventListener...
// 因为逻辑已迁移至 config-name，且 ALIAS_MAP 的 Key 已变更为 config name

// 3. 生成核心逻辑
document.getElementById('gen-btn').addEventListener('click', async ev => {
    const mk = MAIN_INPUT_DOM.value;
    let site = document.getElementById('site').value.trim();
    if(!mk || !site) return;
    const enc = new TextEncoder();
    const key_data = await crypto.subtle.importKey("raw", enc.encode(mk), {name:"HMAC", hash:"SHA-256"}, false, ["sign"]);
    const signature = await crypto.subtle.sign("HMAC", key_data, enc.encode(site));
    const digest = new Uint8Array(signature);
    let chars = Array.from(digest).map(b => BASE62[b % 62]);
    chars[0] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[digest[0] % 26];
    chars[1] = "abcdefghijklmnopqrstuvwxyz"[digest[1] % 26];
    chars[2] = "0123456789"[digest[2] % 10];
    const sym_enabled = document.getElementById('enable-sym').checked;
    const sym_input = document.getElementById('custom-sym').value;
    if (sym_enabled) {
        const pool = sym_input ? sym_input : SYMBOLS;
        if (pool.length > 0) {
            chars[3] = pool[digest[3] % pool.length];
        }
    }
    const pwd = chars.join('').substring(0, document.getElementById('len').value);
    const out = document.getElementById('output');
    out.innerText = pwd;
    out.onclick = () => {
        navigator.clipboard.writeText(pwd);
        showToast('Copied to clipboard');
    };
    // 自动触发一次点击以复制（可选，某些浏览器可能限制）
    // out.click();
});

// 4. 工具函数
function activateOfflineMode(reason = 'Read-only') {
    if (OFFLINE_MODE) return; // 避免重复执行
    document.body.classList.add('offline-mode');
    console.warn(`[Vault] Switching to offline mode: ${reason}`);
    showToast(reason, true);
    OFFLINE_MODE = true;
}

function refreshAliasSuggestions() {
    if (document.activeElement === CONFIG_NAME_INPUT_DOM || !CONFIG_SUGGESTIONS_DOM.hidden) {
        renderConfigSuggestions(CONFIG_NAME_INPUT_DOM.value, true);
    }
}

function getAliasSuggestions(keyword = '') {
    const normalizedKeyword = keyword.trim().toLowerCase();
    const entries = Object.entries(ALIAS_MAP).sort(([left], [right]) => left.localeCompare(right));
    if (!normalizedKeyword) {
        return entries.slice(0, 8);
    }
    const startsWithMatches = [];
    const includesMatches = [];
    entries.forEach(([name, config]) => {
        const haystacks = [name, config.platform || ''].map(text => text.toLowerCase());
        if (haystacks.some(text => text.startsWith(normalizedKeyword))) {
            startsWithMatches.push([name, config]);
            return;
        }
        if (haystacks.some(text => text.includes(normalizedKeyword))) {
            includesMatches.push([name, config]);
        }
    });
    return startsWithMatches.concat(includesMatches).slice(0, 8);
}

function hideConfigSuggestions() {
    CONFIG_SUGGESTIONS_DOM.hidden = true;
    CONFIG_SUGGESTIONS_DOM.innerHTML = '';
    ACTIVE_SUGGESTION_INDEX = -1;
}

function applyConfigSelection(name) {
    CONFIG_NAME_INPUT_DOM.value = name;
    CONFIG_NAME_INPUT_DOM.dispatchEvent(new Event('input', {bubbles: true}));
    hideConfigSuggestions();
}

function renderConfigSuggestions(keyword = '', forceShow = false) {
    const suggestions = getAliasSuggestions(keyword);
    if (!forceShow && suggestions.length === 0) {
        hideConfigSuggestions();
        return;
    }

    CONFIG_SUGGESTIONS_DOM.innerHTML = '';
    ACTIVE_SUGGESTION_INDEX = -1;
    if (suggestions.length === 0) {
        const emptyState = document.createElement('div');
        emptyState.className = 'autocomplete-empty';
        emptyState.innerText = 'No matching aliases';
        CONFIG_SUGGESTIONS_DOM.appendChild(emptyState);
    } else {
        suggestions.forEach(([name, config], index) => {
            const optionButton = document.createElement('button');
            optionButton.type = 'button';
            optionButton.className = 'autocomplete-item';
            optionButton.dataset.aliasName = name;
            const titleSpan = document.createElement('span');
            titleSpan.className = 'autocomplete-title';
            titleSpan.textContent = name;
            const metaSpan = document.createElement('span');
            metaSpan.className = 'autocomplete-meta';
            metaSpan.textContent = config.platform || '';
            optionButton.appendChild(titleSpan);
            optionButton.appendChild(metaSpan);
            optionButton.addEventListener('mousedown', event => {
                event.preventDefault();
                applyConfigSelection(name);
            });
            optionButton.addEventListener('mouseenter', () => {
                ACTIVE_SUGGESTION_INDEX = index;
                syncActiveSuggestion();
            });
            CONFIG_SUGGESTIONS_DOM.appendChild(optionButton);
        });
    }
    CONFIG_SUGGESTIONS_DOM.hidden = false;
}

function syncActiveSuggestion() {
    const items = CONFIG_SUGGESTIONS_DOM.querySelectorAll('.autocomplete-item');
    items.forEach((item, index) => {
        item.classList.toggle('active', index === ACTIVE_SUGGESTION_INDEX);
    });
}

// [修正] 恢复 CSS Toast
function showToast(msg, isError = false) {
    TOAST_DOM.innerText = msg;
    TOAST_DOM.style.backgroundColor = isError ? '#c0392b' : '#333';
    TOAST_DOM.classList.add('show');

    // 清理逻辑：移除类名以便下次动画能触发
    setTimeout(() => {
        TOAST_DOM.classList.remove('show');
        // 延时重置颜色，避免动画中突变
        setTimeout(() => TOAST_DOM.style.backgroundColor = '', 300);
    }, 2000);
}

function isValidFilename(filename) {
    // 1. 空值检查
    if (!filename || typeof filename !== 'string') {
        return false;
    }
    // 2. 正则白名单: 仅允许 字母, 数字, 下划线
    // 注意: 这会拦截带点号的文件名 (如 data.json)
    const regex = /^[a-zA-Z0-9_]+$/;
    if (!regex.test(filename)) {
        return false;
    }
    // 3. Windows 保留字黑名单 (大小写不敏感)
    // 即使完全符合正则，这些名字在 Windows 下也是非法的
    const reserved = new Set([
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
    ]);
    return !reserved.has(filename.toUpperCase());
}

/**
 * @return {?{name: string, key_config: KeyConfig}}
 */
function buildCurrentConfig() {
    const name = document.getElementById('config-name').value.trim();
    const platform = document.getElementById('site').value.trim();
    const len = parseInt(document.getElementById('len').value, 10);

    const symEnabled = document.getElementById('enable-sym').checked;
    const symInput = document.getElementById('custom-sym').value;
    let symbols;
    if (symEnabled) {
        // [关键] 显式保存默认符号，保证不可变性
        symbols = symInput ? symInput : SYMBOLS;
    } else {
        symbols = ""; // 代表 nosym
    }
    if (!name || !platform) {
        showToast('Name & Platform required', true);
        return null;
    }
    if (!isValidFilename(name)){
        showToast("Invalid Filename", true);
        return null;
    }
    return {name: name, key_config: {
        platform: platform,
        length: len,
        symbols: symbols
    }}
}

// 5. 配置管理逻辑 (CRUD)
document.getElementById('config-save-btn').addEventListener('click', async ev => {
    if(OFFLINE_MODE){
        showToast("Offline Mode: Read-only", true);
        return;
    }
    const config = buildCurrentConfig();
    if (!config) return;
    const response = await fetch(`${CONFIG_API_ENDPOINT}/${encodeURIComponent(config.name)}`, withAuthFetchOptions({
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(config.key_config)
    }));
    if (!response.ok) {
        showToast(`Save failed: ${response.status}`, true);
        console.error(response.status, response.json());
        return;
    }
    ALIAS_MAP[config.name] = config.key_config;
    refreshAliasSuggestions();
    showToast(`Saved: ${config.name}`);
});

document.getElementById('config-del-btn').addEventListener('click', async ev =>  {
    if(OFFLINE_MODE){
        showToast("Offline Mode: Read-only", true);
        return;
    }
    const name = document.getElementById('config-name').value.trim();
    if (!name) {
        showToast('Config Name required', true);
        return;
    }
    if (!confirm(`Delete config "${name}"?`)) return;
    try {
        const response = await fetch(`${CONFIG_API_ENDPOINT}/${encodeURIComponent(name)}`, withAuthFetchOptions({
            method: 'DELETE'
        }));
        if (!response.ok) {
            console.error(response.status);
            showToast(`Delete Error ${response.status}`, true);
        }
        delete ALIAS_MAP[name];
        refreshAliasSuggestions();
        document.getElementById('config-name').value = '';
        // 触发 input 事件以重置下方 UI
        document.getElementById('config-name').dispatchEvent(new Event('input'));
        showToast(`Deleted: ${name}`);
    } catch (e) {

    }
});

// 6. 联动逻辑 (Auto-fill)
CONFIG_NAME_INPUT_DOM.addEventListener('input', (e) => {
    const name = e.target.value;
    const config = ALIAS_MAP[name];

    const siteInput = document.getElementById('site');
    const lenInput = document.getElementById('len');
    const symCheck = document.getElementById('enable-sym');
    const symInput = document.getElementById('custom-sym');

    if (config) {
        // 命中配置：填充
        siteInput.value = config.platform || '';
        if (config.length) lenInput.value = config.length;

        if (!config.symbols) {
            symCheck.checked = false;
            symInput.value = "";
        } else {
            symCheck.checked = true;
            symInput.value = config.symbols;
        }
    } else {
        // [新增] 未命中配置（比如用户清空了输入，或者正在输入新名称）：
        // 这里策略可以是“保持不变”或者“恢复默认”。
        // 为了避免误导，建议不做操作，或者仅当输入为空时恢复默认。
        if (name === '') {
            // siteInput.value = ''; // 可选：清空平台
            lenInput.value = 16;  // 恢复默认长度
            symCheck.checked = true; // 恢复默认勾选
            symInput.value = "";  // 恢复默认占位符状态
        }
    }
    renderConfigSuggestions(name, document.activeElement === CONFIG_NAME_INPUT_DOM);
});

CONFIG_NAME_INPUT_DOM.addEventListener('focus', () => {
    renderConfigSuggestions(CONFIG_NAME_INPUT_DOM.value, true);
});

CONFIG_NAME_INPUT_DOM.addEventListener('click', () => {
    renderConfigSuggestions(CONFIG_NAME_INPUT_DOM.value, true);
});

CONFIG_NAME_INPUT_DOM.addEventListener('keydown', (event) => {
    const items = CONFIG_SUGGESTIONS_DOM.querySelectorAll('.autocomplete-item');
    if (CONFIG_SUGGESTIONS_DOM.hidden || items.length === 0) {
        return;
    }

    if (event.key === 'ArrowDown') {
        event.preventDefault();
        ACTIVE_SUGGESTION_INDEX = (ACTIVE_SUGGESTION_INDEX + 1 + items.length) % items.length;
        syncActiveSuggestion();
        return;
    }
    if (event.key === 'ArrowUp') {
        event.preventDefault();
        ACTIVE_SUGGESTION_INDEX = (ACTIVE_SUGGESTION_INDEX - 1 + items.length) % items.length;
        syncActiveSuggestion();
        return;
    }
    if (event.key === 'Enter' && ACTIVE_SUGGESTION_INDEX >= 0) {
        event.preventDefault();
        applyConfigSelection(items[ACTIVE_SUGGESTION_INDEX].dataset.aliasName || '');
        return;
    }
    if (event.key === 'Escape') {
        hideConfigSuggestions();
    }
});

document.addEventListener('click', (event) => {
    if (CONFIG_SUGGESTIONS_DOM.hidden) {
        return;
    }
    if (event.target === CONFIG_NAME_INPUT_DOM || CONFIG_SUGGESTIONS_DOM.contains(event.target)) {
        return;
    }
    hideConfigSuggestions();
});
