const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "mc_seed_finder_settings_v1";
const WORLD_SEARCH_RADIUS = 30000000;
const MAP_TILE_SIZE = 65;
const MAP_TILE_INTERVALS = MAP_TILE_SIZE - 1;
const MAP_TILE_CONCURRENCY = 4;
const MAP_TILE_CACHE_LIMIT = 120;
const MAP_TILE_RETRY_MS = 8000;

let currentSettings = null;
let loggedIn = false;
let loadedLocalSettings = false;
let lastMapCenter = null;
let progressTimer = null;
let activeProgressId = null;
let progressStartedAt = 0;
let lastProgressSnapshot = null;
let searchBusy = false;
let lastCompletedSearch = null;
const mapState = {
  map: null,
  mapInfo: null,
  centerX: 0,
  centerZ: 0,
  radius: 2048,
  dragging: false,
  lastX: 0,
  lastY: 0,
  reloadTimer: null,
  lastTileCheck: 0,
  markers: [],
  layers: {},
  results: [],
  tiles: new Map(),
  pendingTiles: new Map(),
  failedTiles: new Map(),
  tileQueue: [],
  queuedTileKeys: new Set(),
  visibleTileKeys: new Set(),
  activeTileRequests: 0,
  generation: 0,
  dataKey: "",
  currentStep: null,
  fallbackStep: null,
};

const persistedFields = ["baseUrl", "model", "seed", "version", "centerX", "centerZ"];
const legacyModels = {
  "deepseek-chat": "deepseek-v4-flash",
  "deepseek-pro": "deepseek-v4-pro",
  "deepseek-reasoner": "deepseek-v4-pro",
};

const biomeNamesZh = {
  plains: "平原",
  sunflower_plains: "向日葵平原",
  snowy_plains: "雪原",
  ice_spikes: "冰刺平原",
  cherry_grove: "樱花林",
  meadow: "草甸",
  forest: "森林",
  flower_forest: "繁花森林",
  birch_forest: "桦木森林",
  old_growth_birch_forest: "原始桦木森林",
  dark_forest: "黑森林",
  taiga: "针叶林",
  snowy_taiga: "积雪针叶林",
  old_growth_pine_taiga: "原始松木针叶林",
  old_growth_spruce_taiga: "原始云杉针叶林",
  jungle: "丛林",
  sparse_jungle: "稀疏丛林",
  bamboo_jungle: "竹林",
  desert: "沙漠",
  badlands: "恶地",
  wooded_badlands: "疏林恶地",
  eroded_badlands: "风蚀恶地",
  swamp: "沼泽",
  mangrove_swamp: "红树林沼泽",
  mushroom_fields: "蘑菇岛",
  ocean: "海洋",
  deep_ocean: "深海",
  warm_ocean: "暖水海洋",
  lukewarm_ocean: "温水海洋",
  deep_lukewarm_ocean: "温水深海",
  cold_ocean: "冷水海洋",
  deep_cold_ocean: "冷水深海",
  frozen_ocean: "冻洋",
  deep_frozen_ocean: "冰冻深海",
  river: "河流",
  frozen_river: "冻河",
  beach: "沙滩",
  snowy_beach: "积雪沙滩",
  stony_shore: "石岸",
  savanna: "热带草原",
  savanna_plateau: "热带高原",
  windswept_hills: "风袭丘陵",
  windswept_forest: "风袭森林",
  windswept_gravelly_hills: "风袭沙砾丘陵",
  grove: "雪林",
  snowy_slopes: "积雪山坡",
  jagged_peaks: "尖峭山峰",
  frozen_peaks: "冰封山峰",
  stony_peaks: "裸岩山峰",
  lush_caves: "繁茂洞穴",
  dripstone_caves: "溶洞",
  deep_dark: "深暗之域",
};

const structureNamesZh = {
  village: "村庄",
  witch_hut: "女巫小屋",
  swamp_hut: "女巫小屋",
  pillager_outpost: "掠夺者前哨站",
  outpost: "掠夺者前哨站",
  desert_pyramid: "沙漠神殿",
  desert_temple: "沙漠神殿",
  jungle_pyramid: "丛林神庙",
  jungle_temple: "丛林神庙",
  igloo: "雪屋",
  ocean_monument: "海底神殿",
  monument: "海底神殿",
  woodland_mansion: "林地府邸",
  mansion: "林地府邸",
  ruined_portal: "废弃传送门",
  ancient_city: "远古城市",
  shipwreck: "沉船",
  nether_fortress: "下界要塞",
  fortress: "下界要塞",
  bastion_remnant: "堡垒遗迹",
  bastion: "堡垒遗迹",
  end_city: "末地城",
};

const layerColors = {
  site: "#d95f2a",
  structure: "#256f77",
  biome: "#d83f8c",
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => `${item.loc?.join(".") || "字段"}: ${item.msg}`).join("；")
      : data.detail;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

function showNotice(text, type = "info") {
  const el = $("notice");
  el.textContent = text;
  el.className = `notice ${type}`;
  el.hidden = false;
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => {
    el.hidden = true;
  }, 4200);
}

function addMessage(role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  $("messages").appendChild(wrap);
  $("messages").scrollTop = $("messages").scrollHeight;
}

function localPayload() {
  const coordX = parseInteger($("centerX").value, 0);
  const coordZ = parseInteger($("centerZ").value, 0);
  return {
    deepseek_base_url: $("baseUrl").value || "https://api.deepseek.com",
    deepseek_model: normalizeModel($("model").value || "deepseek-v4-flash"),
    seed: $("seed").value || "0",
    version: $("version").value || "26.2",
    center_x: coordX,
    center_z: coordZ,
    search_radius: WORLD_SEARCH_RADIUS,
    max_results: 1,
  };
}

function normalizeModel(model) {
  return legacyModels[String(model || "").trim()] || String(model || "deepseek-v4-flash").trim();
}

function parseInteger(value, fallback) {
  const parsed = Number.parseInt(String(value).trim(), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function humanizeId(value) {
  return String(value || "未知").replaceAll("_", " ");
}

function biomeNameZh(name) {
  return biomeNamesZh[name] || humanizeId(name);
}

function structureNameZh(id) {
  return structureNamesZh[id] || humanizeId(id);
}

function targetNameZh(target) {
  const label = String(target?.label || "").trim();
  if (label && !/^[a-z0-9_\-\s/]+$/i.test(label)) return label;
  return target?.kind === "biome" ? biomeNameZh(target.id) : structureNameZh(target?.id);
}

function kindNameZh(kind) {
  if (kind === "biome") return "生物群系";
  if (kind === "structure") return "结构";
  if (kind === "site") return "推荐坐标";
  return "地点";
}

function stageNameZh(stage) {
  if (stage === "prepare") return "准备";
  if (stage === "ai") return "DeepSeek";
  if (stage === "search") return "搜索核心";
  if (stage === "anchor") return "锚点扫描";
  if (stage === "radius") return "范围扩展";
  if (stage === "done") return "完成";
  if (stage === "error") return "失败";
  return humanizeId(stage);
}

function makeRequestId() {
  const random = Math.random().toString(36).slice(2, 10);
  return `req_${Date.now()}_${random}`;
}

function formatBlocks(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "--";
  if (n >= 10000) {
    const wan = n / 10000;
    const digits = wan >= 100 ? 0 : wan >= 10 ? 1 : 2;
    const raw = wan.toFixed(digits);
    const text = raw.includes(".") ? raw.replace(/0+$/, "").replace(/\.$/, "") : raw;
    return `${text}万格`;
  }
  return `${Math.round(n).toLocaleString("zh-CN")} 格`;
}

function formatSquareBlocks(value) {
  const area = Math.max(0, Number(value) || 0);
  if (area >= 100000000) return `${(area / 100000000).toFixed(area >= 1000000000 ? 1 : 2).replace(/\.0+$/, "")} 亿平方格`;
  if (area >= 10000) return `${(area / 10000).toFixed(area >= 1000000 ? 1 : 2).replace(/\.0+$/, "")} 万平方格`;
  return `${Math.round(area).toLocaleString("zh-CN")} 平方格`;
}

function showProgressShell(text = "DeepSeek 正在解析") {
  progressStartedAt = Date.now();
  lastProgressSnapshot = null;
  $("searchProgress").hidden = false;
  $("progressStage").textContent = "DeepSeek";
  $("progressTitle").textContent = "正在搜索";
  $("progressMessage").textContent = text;
  $("progressRadius").textContent = "等待扫描";
  $("progressMeta").textContent = "正在理解地点与约束";
  $("progressFill").style.width = "30%";
  $("progressFill").parentElement.classList.add("indeterminate");
}

function updateProgressUI(progress) {
  if (!progress || progress.status === "missing") return;
  lastProgressSnapshot = { ...(lastProgressSnapshot || {}), ...progress };
  $("searchProgress").hidden = false;
  $("progressStage").textContent = stageNameZh(progress.stage);
  $("progressTitle").textContent = progress.status === "done" ? "搜索完成" : "正在搜索";
  const isDeepSeekStage = progress.stage === "ai";
  const hasRadius = Number(progress.radius) > 0;
  const waitingForScan = !hasRadius && ["prepare", "ai", "search"].includes(progress.stage);
  $("progressMessage").textContent = isDeepSeekStage
    ? "DeepSeek 正在解析"
    : progress.message || (waitingForScan ? "尚未开始 Minecraft 坐标扫描" : "正在搜索");
  $("progressRadius").textContent = hasRadius ? formatBlocks(progress.radius) : "等待扫描";

  const checked = Number(progress.checked);
  const total = Number(progress.total);
  const elapsed = progressStartedAt ? Math.max(0, Math.round((Date.now() - progressStartedAt) / 1000)) : 0;
  const elapsedText = elapsed ? `已用 ${elapsed} 秒 · ` : "";
  const rangeText = hasRadius ? `当前范围 ${formatBlocks(progress.radius)} · ` : waitingForScan ? "尚未开始坐标扫描 · " : "";
  if (isDeepSeekStage) {
    $("progressFill").parentElement.classList.add("indeterminate");
    $("progressMeta").textContent = elapsed ? `已用 ${elapsed} 秒` : "正在理解地点与约束";
    return;
  }
  if (Number.isFinite(checked) && Number.isFinite(total) && total > 0) {
    const pct = Math.max(4, Math.min(100, (checked / total) * 100));
    $("progressFill").parentElement.classList.remove("indeterminate");
    $("progressFill").style.width = `${pct}%`;
    const countText = `已检查 ${Math.min(checked, total)} / ${total} 个候选`;
    $("progressMeta").textContent = `${elapsedText}${rangeText}${countText}`;
  } else {
    $("progressFill").parentElement.classList.add("indeterminate");
    $("progressMeta").textContent = hasRadius
      ? `${elapsedText}${rangeText}搜索仍在进行`
      : `${elapsedText}${rangeText}搜索仍在进行`;
  }
}

function startProgressPolling(requestId) {
  activeProgressId = requestId;
  clearInterval(progressTimer);
  showProgressShell();
  const poll = async () => {
    try {
      const data = await api(`/progress/${encodeURIComponent(requestId)}`);
      if (activeProgressId !== requestId) return;
      updateProgressUI(data);
      if (data.status === "done" || data.status === "error") {
        clearInterval(progressTimer);
      }
    } catch {
      // Progress polling is best-effort; the search request itself still owns error reporting.
    }
  };
  poll();
  progressTimer = setInterval(poll, 800);
}

function stopProgressPolling(message = "搜索完成", status = "done") {
  const requestId = activeProgressId;
  clearInterval(progressTimer);
  progressTimer = null;
  if (!requestId) return;
  updateProgressUI({ ...(lastProgressSnapshot || {}), status, stage: status, message });
  setTimeout(() => {
    if (activeProgressId === requestId) {
      $("searchProgress").hidden = true;
      activeProgressId = null;
      lastProgressSnapshot = null;
    }
  }, status === "error" ? 2200 : 1200);
}

function setSearchBusy(value) {
  searchBusy = value;
  $("sendBtn").disabled = value;
  $("sendBtn").classList.toggle("is-loading", value);
  $("sendLabel").textContent = value ? "正在搜索" : "开始搜索";
  $("chatInput").disabled = value;
  document.querySelectorAll(".preset").forEach((button) => {
    button.disabled = value;
  });
}

function setPlannerStatus(text, state = "idle") {
  const pill = $("plannerPill");
  pill.textContent = text;
  pill.dataset.state = state;
}

function markerColor(kind, id = null) {
  if (kind === "biome" && id) return colorForBiome(id);
  return layerColors[kind] || "#335c67";
}

function modeNameZh(mode) {
  if (mode === "exact") return "精确";
  if (mode === "compatibility") return "兼容";
  return humanizeId(mode);
}

function coordText(point) {
  return `X ${Number(point?.x)}, Z ${Number(point?.z)}`;
}

function netherEquivalent(point) {
  const x = Number(point?.x);
  const z = Number(point?.z);
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  return { x: Math.round(x / 8), z: Math.round(z / 8) };
}

function coordinateChips(point, { isNether = false, overworldEquivalent = null } = {}) {
  const chips = [];
  if (isNether && overworldEquivalent) {
    chips.push(`<span class="chip">主世界约 ${coordText(overworldEquivalent)}</span>`);
  } else if (!isNether) {
    const nether = netherEquivalent(point);
    if (nether) chips.push(`<span class="chip">下界约 ${coordText(nether)}</span>`);
  }
  return chips.join("");
}

function coordinateHint(point, { isNether = false, overworldEquivalent = null } = {}) {
  if (isNether && overworldEquivalent) return ` · 主世界约 ${coordText(overworldEquivalent)}`;
  if (!isNether) {
    const nether = netherEquivalent(point);
    if (nether) return ` · 下界约 ${coordText(nether)}`;
  }
  return "";
}

function plannerNameZh(planner) {
  if (planner === "deepseek_json") return "DeepSeek 解析";
  if (planner === "deepseek_tool") return "DeepSeek 工具调用";
  if (planner === "needs_api_key") return "等待 API Key";
  if (planner === "deepseek_error") return "DeepSeek 失败";
  return humanizeId(planner);
}

function normalizeInputs() {
  const payload = localPayload();
  $("centerX").value = payload.center_x;
  $("centerZ").value = payload.center_z;
}

function readSettings() {
  return {
    ...localPayload(),
    deepseek_api_key: $("apiKey").value || null,
  };
}

function hasUsableApiKey() {
  return Boolean($("apiKey").value.trim() || currentSettings?.deepseek_api_key_set);
}

function saveLocalSettings({ normalize = false } = {}) {
  if (normalize) normalizeInputs();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(localPayload()));
  updateSummary();
}

function loadLocalSettings() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return false;
  try {
    fillSettings(JSON.parse(raw), { preserveKeyState: true });
    loadedLocalSettings = true;
    return true;
  } catch {
    return false;
  }
}

function fillSettings(s, options = {}) {
  currentSettings = { ...(currentSettings || {}), ...s };
  $("baseUrl").value = s.deepseek_base_url || s.baseUrl || "https://api.deepseek.com";
  setSelectedModel(normalizeModel(s.deepseek_model || s.model || "deepseek-v4-flash"));
  $("seed").value = s.seed ?? "0";
  $("version").value = s.version ?? "26.2";
  $("centerX").value = s.center_x ?? s.centerX ?? 0;
  $("centerZ").value = s.center_z ?? s.centerZ ?? 0;

  if (!options.preserveKeyState) {
    $("apiKey").placeholder = s.deepseek_api_key_set ? "已保存；留空继续使用已保存 key" : "必填；用于 DeepSeek 解析需求";
  }
  saveLocalSettings();
  updateKeyState();
}

function setSelectedModel(model) {
  const normalized = normalizeModel(model);
  if (![...$("model").options].some((option) => option.value === normalized)) {
    $("model").appendChild(new Option(normalized, normalized));
  }
  $("model").value = normalized;
}

function setModelOptions(models, selected) {
  const selectedModel = normalizeModel(selected || $("model").value || "deepseek-v4-flash");
  $("model").innerHTML = "";
  for (const item of models) {
    const id = normalizeModel(item.id || item.name);
    $("model").appendChild(new Option(id, id));
  }
  setSelectedModel(selectedModel);
}

async function loadModels({ quiet = true } = {}) {
  try {
    const data = await api("/ai/models", {
      method: "POST",
      body: JSON.stringify({
        deepseek_api_key: $("apiKey").value || null,
        deepseek_base_url: $("baseUrl").value || "https://api.deepseek.com",
      }),
    });
    setModelOptions(data.models || [], $("model").value);
    if (data.warnings?.length && !quiet) showNotice(data.warnings[0], "warn");
  } catch (e) {
    setModelOptions([{ id: "deepseek-v4-flash" }, { id: "deepseek-v4-pro" }], $("model").value);
    if (!quiet) showNotice(`模型列表获取失败：${e.message}`, "warn");
  }
}

function updateSummary() {
  $("currentSummary").textContent = `Seed ${$("seed").value || "0"} · X ${$("centerX").value || 0} · Z ${$("centerZ").value || 0}`;
}

function updateKeyState() {
  const typed = $("apiKey").value.trim();
  if (typed) {
    $("keyStorage").textContent = loggedIn ? "待保存" : "本次使用";
    $("keyStorage").dataset.state = "ready";
    return;
  }
  if (currentSettings?.deepseek_api_key_set) {
    $("keyStorage").textContent = "账号已保存";
    $("keyStorage").dataset.state = "ready";
    return;
  }
  $("keyStorage").textContent = "未设置";
  $("keyStorage").dataset.state = "idle";
}

function setLoggedIn(me) {
  loggedIn = Boolean(me);
  document.body.classList.toggle("logged-in", loggedIn);
  $("meLine").textContent = me ? `已登录：${me.username}` : "未登录";
  $("saveState").textContent = loggedIn ? "搜索前会自动保存到账号" : "会自动保存在本机浏览器";
  updateKeyState();
}

function setResultFeedback(context = null) {
  lastCompletedSearch = context;
  const root = $("resultFeedback");
  const button = $("unmetFeedbackBtn");
  root.hidden = !context;
  button.disabled = false;
  button.textContent = "标记未解决";
}

function renderResults(payload) {
  setPlannerStatus(plannerNameZh(payload.planner), payload.results?.length ? "ready" : "idle");
  $("warnings").innerHTML = "";
  for (const warning of payload.warnings || []) {
    const div = document.createElement("div");
    div.className = "warning";
    div.textContent = warning;
    $("warnings").appendChild(div);
  }
  const root = $("results");
  root.className = "result-list";
  root.innerHTML = "";
  setMapResultOverlay(payload.results || []);
  if (!payload.results?.length) {
    root.className = "results-empty";
    root.innerHTML = `
      <div class="empty-state">
        <span class="empty-mark" aria-hidden="true"></span>
        <strong>没有候选结果</strong>
        <p>可以调整地点条件后再次搜索</p>
      </div>
    `;
    return;
  }
  payload.results.forEach((r, idx) => {
    const card = document.createElement("article");
    card.className = "result-card";
    const reasons = r.failure_reasons?.length
      ? `<div class="warning">${r.failure_reasons.map(escapeHtml).join("<br>")}</div>`
      : "";
    const countChecks = r.count_checks?.length
      ? `<div class="meta">${r.count_checks.map((check) => `
          <span class="chip">${escapeHtml(check.label || check.target)} ${Number(check.found || 0)} / ${Number(check.min || 0)} · ${formatBlocks(check.within)}</span>
        `).join("")}</div>`
      : "";
    const isNetherResult = r.dimension === "nether" || Boolean(r.suggested_overworld);
    const dimensionMeta = coordinateChips(r.suggested, {
      isNether: isNetherResult,
      overworldEquivalent: r.suggested_overworld,
    });
    const coordinateNote = r.coordinate_note ? `<div class="muted">${escapeHtml(r.coordinate_note)}</div>` : "";
    const layoutChecks = r.layout_checks?.length
      ? `<div class="layout-check-list">${r.layout_checks.map((check) => `
          <div class="layout-check ${check.passed ? "passed" : "failed"}">
            <div class="layout-check-head">
              <strong>前后布局</strong>
              <span>${check.passed ? "已通过" : "未通过"}</span>
            </div>
            <div class="layout-axis">
              <span>${escapeHtml(check.back_label)}</span>
              <b>← ${escapeHtml(check.center_label)} →</b>
              <span>${escapeHtml(check.front_label)}</span>
            </div>
            <div class="muted">实测夹角 ${Number(check.angle || 0).toFixed(1)}° · 要求至少 ${Number(check.min_angle || 120)}°</div>
          </div>
        `).join("")}</div>`
      : "";
    const adjacencyChecks = r.adjacency_checks?.length
      ? `<div class="constraint-check-list">
          <div class="constraint-check-title">距离审核</div>
          ${r.adjacency_checks.map((check) => `
            <div class="constraint-check ${check.passed ? "passed" : "failed"}">
              <span><i></i>${escapeHtml(check.a_label)} ↔ ${escapeHtml(check.b_label)}</span>
              <strong>${Number(check.distance).toFixed(1)} / ${Number(check.max_distance || 0)} 格</strong>
            </div>
          `).join("")}
        </div>`
      : "";
    const biomeAreaChecks = r.biome_area_checks?.length
      ? `<div class="biome-area-check-list">
          <div class="constraint-check-title">群系面积</div>
          ${r.biome_area_checks.map((check) => `
            <div class="biome-area-check ${check.passed ? "passed" : "failed"}">
              <div>
                <strong>${escapeHtml(check.label || biomeNameZh(check.target))}</strong>
                <span>${check.truncated ? "至少" : "约"} ${formatSquareBlocks(check.area)}</span>
              </div>
              <div class="muted">${check.min_area ? `要求至少 ${formatSquareBlocks(check.min_area)} · ` : ""}${check.closed ? "边界已闭合" : "区域超出测量边界"} · Y=${Number(check.sample_y || 63)} 投影 · ${Number(check.step || 0)} 格精度</div>
            </div>
          `).join("")}
          ${r.area_search ? `<div class="muted area-search-meta">确定性近似排名 · 全域抽样 ${Number(r.area_search.samples_checked || 0).toLocaleString("zh-CN")} 点 · 复测 ${Number(r.area_search.candidate_count || 0)} 个候选</div>` : ""}
        </div>`
      : "";
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="muted">候选 ${idx + 1}</div>
          <div class="coord">${coordText(r.suggested)}</div>
          ${coordinateNote}
        </div>
        <strong class="${r.satisfied ? "ok" : "bad"}">${r.satisfied ? "满足" : "未满足"}</strong>
      </div>
      <div class="meta">
        <span class="chip">${escapeHtml(modeNameZh(r.mode))}</span>
        ${r.area_rank ? `<span class="chip">面积排名 #${Number(r.area_rank)}</span>` : ""}
        <span class="chip">距玩家 ${r.distance} 格</span>
        <span class="chip">目标最远 ${r.max_pairwise_distance} 格</span>
        ${dimensionMeta}
      </div>
      ${reasons}
      ${countChecks}
      ${layoutChecks}
      ${adjacencyChecks}
      ${biomeAreaChecks}
      <div class="target-list">
        ${r.targets.map(t => `
          <div class="target">
            <div><strong>${escapeHtml(targetNameZh(t))}</strong><div class="muted">${escapeHtml(kindNameZh(t.kind))}</div></div>
            <div>${coordText(t)}<div class="muted">距站点 ${t.distance_to_site} 格${t.biome_area ? ` · ${formatSquareBlocks(t.biome_area)}` : ""}${coordinateHint(t, { isNether: Boolean(t.overworld_equivalent), overworldEquivalent: t.overworld_equivalent })}</div></div>
          </div>
        `).join("")}
      </div>
      <p class="map-link"><a href="${r.map_url}" target="_blank" rel="noreferrer">打开 Chunkbase 核对</a></p>
    `;
    root.appendChild(card);
  });
  const best = payload.results[0];
  if (best?.suggested) {
    lastMapCenter = { x: best.suggested.x, z: best.suggested.z };
    loadMap(lastMapCenter).catch((e) => showNotice(`地图加载失败：${e.message}`, "warn"));
  }
}

function setMapResultOverlay(results) {
  mapState.results = results || [];
  mapState.markers = [];
  const layerDefs = new Map();
  const addLayer = (key, label, kind, id = null) => {
    if (!layerDefs.has(key)) layerDefs.set(key, { key, label, kind, color: markerColor(kind, id) });
    if (mapState.layers[key] === undefined) mapState.layers[key] = true;
  };

  for (const [resultIndex, result] of mapState.results.entries()) {
    if (result?.suggested) {
      addLayer("site", "推荐坐标", "site");
      mapState.markers.push({
        key: "site",
        kind: "site",
        name: "推荐坐标",
        label: `候选 ${resultIndex + 1} 推荐坐标`,
        x: result.suggested.x,
        z: result.suggested.z,
        candidate: resultIndex + 1,
      });
    }

    for (const target of result?.targets || []) {
      if (target.verified_at) continue;
      const label = targetNameZh(target);
      const key = `${target.kind}:${target.id}`;
      addLayer(key, label, target.kind, target.id);
      mapState.markers.push({
        key,
        kind: target.kind,
        id: target.id,
        name: label,
        label: `候选 ${resultIndex + 1} ${label}`,
        x: target.x,
        z: target.z,
        candidate: resultIndex + 1,
      });
    }
  }

  renderMapLayers([...layerDefs.values()]);
  drawMap();
}

function renderMapLayers(layerDefs) {
  const root = $("mapLayers");
  root.innerHTML = "";
  if (!layerDefs.length) {
    root.innerHTML = `<span class="layer-empty">搜索后可选择显示推荐点和目标地点</span>`;
    return;
  }
  for (const layer of layerDefs) {
    const label = document.createElement("label");
    label.className = "layer-toggle";
    label.innerHTML = `
      <input type="checkbox" ${mapState.layers[layer.key] ? "checked" : ""} data-layer="${escapeHtml(layer.key)}" />
      <span class="layer-swatch" style="background:${layer.color}"></span>
      <span>${escapeHtml(layer.label)}</span>
    `;
    root.appendChild(label);
  }
  root.querySelectorAll("input").forEach((input) => {
    input.addEventListener("change", () => {
      mapState.layers[input.dataset.layer] = input.checked;
      drawMap();
    });
  });
}

const biomeColors = {
  plains: "#8db360",
  sunflower_plains: "#b5db88",
  cherry_grove: "#ff9bcf",
  swamp: "#4f7d46",
  mangrove_swamp: "#456b4a",
  forest: "#4f8f47",
  flower_forest: "#6fbf65",
  birch_forest: "#75a85a",
  dark_forest: "#2d5a35",
  desert: "#e7d18b",
  jungle: "#3c9a3e",
  bamboo_jungle: "#51aa47",
  badlands: "#c97945",
  wooded_badlands: "#b86d3e",
  eroded_badlands: "#d08a55",
  snowy_plains: "#f1f5f4",
  snowy_tundra: "#f1f5f4",
  snowy_taiga: "#c9ddd9",
  meadow: "#9ccf78",
  mushroom_fields: "#b47ac2",
  ocean: "#3579bd",
  deep_ocean: "#245a95",
  warm_ocean: "#41b6c4",
  lukewarm_ocean: "#4aa3c7",
  cold_ocean: "#3c74ad",
  frozen_ocean: "#a9d9e8",
  river: "#4f91c9",
  frozen_river: "#b8e4ee",
  taiga: "#5f8a65",
  savanna: "#c6b45c",
  windswept_hills: "#8d967a",
  mountains: "#8d967a",
  stony_peaks: "#aaa58e",
  jagged_peaks: "#d8ddd8",
  frozen_peaks: "#e8eeee",
  lush_caves: "#5aa657",
  dripstone_caves: "#9b8b76",
  deep_dark: "#24303a",
};

const biomeRgbCache = new Map();

function colorForBiome(name) {
  if (biomeColors[name]) return biomeColors[name];
  if (name?.includes("ocean")) return "#3e7fba";
  if (name?.includes("forest")) return "#5c9653";
  if (name?.includes("snow") || name?.includes("frozen")) return "#edf4f4";
  if (name?.includes("desert")) return "#dfc87e";
  if (name?.includes("badlands")) return "#c97848";
  if (name?.includes("jungle")) return "#3d9942";
  if (name?.includes("river")) return "#5a9bd0";
  return "#9cad8c";
}

function rgbForBiome(name) {
  const color = colorForBiome(name);
  if (biomeRgbCache.has(color)) return biomeRgbCache.get(color);
  const normalized = color.replace("#", "");
  const expanded = normalized.length === 3
    ? normalized.split("").map((value) => value + value).join("")
    : normalized;
  const value = Number.parseInt(expanded, 16);
  const rgb = Number.isFinite(value)
    ? [(value >> 16) & 255, (value >> 8) & 255, value & 255]
    : [156, 173, 140];
  biomeRgbCache.set(color, rgb);
  return rgb;
}

function terrainShade(map, row, col, biomeName) {
  if (!Array.isArray(map.heights) || map.heights.length !== map.size * map.size) return 1;
  const index = row * map.size + col;
  const right = map.heights[row * map.size + Math.min(map.size - 1, col + 1)];
  const down = map.heights[Math.min(map.size - 1, row + 1) * map.size + col];
  const step = Math.max(1, Number(map.step) || 1);
  const gradientX = (right - map.heights[index]) / step;
  const gradientZ = (down - map.heights[index]) / step;
  const normalLength = Math.hypot(gradientX, 1, gradientZ) || 1;
  const illumination = (gradientX * 0.52 + 0.78 + gradientZ * 0.35) / normalLength;
  const elevation = Math.max(-0.05, Math.min(0.1, (map.heights[index] - 63) / 520));
  let shade = Math.max(0.64, Math.min(1.13, 0.76 + illumination * 0.32 + elevation));
  if (biomeName?.includes("ocean") || biomeName?.includes("river")) {
    shade = 0.94 + (shade - 0.94) * 0.55;
  }
  return shade;
}

function mapDataKey(settings) {
  return JSON.stringify([String(settings.seed), String(settings.version), "surface-v1"]);
}

function tileSpecForRadius(radius = mapState.radius) {
  const step = Math.max(1, Math.round(Math.max(1, radius) / MAP_TILE_INTERVALS));
  const span = step * MAP_TILE_INTERVALS;
  return { step, span, radius: span / 2, size: MAP_TILE_SIZE };
}

function mapTileKey(dataKey, step, tileX, tileZ) {
  return `${dataKey}|${step}|${tileX}|${tileZ}`;
}

function setMapWarning(text = "") {
  $("mapWarning").textContent = text;
  $("mapWarning").hidden = !text;
}

function resetMapTiles(settings) {
  mapState.generation += 1;
  for (const request of mapState.pendingTiles.values()) request.controller.abort();
  mapState.tiles.clear();
  mapState.pendingTiles.clear();
  mapState.failedTiles.clear();
  mapState.tileQueue = [];
  mapState.queuedTileKeys.clear();
  mapState.visibleTileKeys.clear();
  mapState.dataKey = mapDataKey(settings);
  mapState.map = null;
  mapState.mapInfo = null;
  mapState.currentStep = null;
  mapState.fallbackStep = null;
  setMapWarning();
  $("mapLegend").textContent = "区块加载后显示";
}

function syncMapDataSource(settings, { force = false } = {}) {
  const nextKey = mapDataKey(settings);
  if (force || mapState.dataKey !== nextKey) resetMapTiles(settings);
}

function canvasMetrics() {
  const canvas = $("mapCanvas");
  const rect = canvas.getBoundingClientRect();
  return {
    width: Math.max(1, rect.width || canvas.clientWidth || 900),
    height: Math.max(1, rect.height || canvas.clientHeight || 720),
  };
}

function cameraScale(cssWidth, cssHeight) {
  return (Math.max(1, mapState.radius) * 2) / Math.max(1, cssWidth, cssHeight);
}

function worldToScreen(x, z, cssWidth, cssHeight) {
  const scale = cameraScale(cssWidth, cssHeight);
  return {
    x: cssWidth / 2 + (x - mapState.centerX) / scale,
    y: cssHeight / 2 + (z - mapState.centerZ) / scale,
  };
}

function screenToWorld(x, y, cssWidth, cssHeight) {
  const scale = cameraScale(cssWidth, cssHeight);
  return {
    x: mapState.centerX + (x - cssWidth / 2) * scale,
    z: mapState.centerZ + (y - cssHeight / 2) * scale,
  };
}

function viewportTileRange(spec, cssWidth, cssHeight, padding = 0) {
  const scale = cameraScale(cssWidth, cssHeight);
  const halfWidth = cssWidth * scale / 2 + padding;
  const halfHeight = cssHeight * scale / 2 + padding;
  const minX = mapState.centerX - halfWidth;
  const maxX = mapState.centerX + halfWidth;
  const minZ = mapState.centerZ - halfHeight;
  const maxZ = mapState.centerZ + halfHeight;
  return {
    minTileX: Math.floor(minX / spec.span),
    maxTileX: Math.floor((maxX - 0.000001) / spec.span),
    minTileZ: Math.floor(minZ / spec.span),
    maxTileZ: Math.floor((maxZ - 0.000001) / spec.span),
  };
}

function tileIsInRange(tileX, tileZ, range) {
  return tileX >= range.minTileX && tileX <= range.maxTileX
    && tileZ >= range.minTileZ && tileZ <= range.maxTileZ;
}

function hasTilesAtStep(step) {
  for (const tile of mapState.tiles.values()) {
    if (tile.dataKey === mapState.dataKey && tile.stepLevel === step) return true;
  }
  return false;
}

function hasAnyMapTile() {
  for (const tile of mapState.tiles.values()) {
    if (tile.dataKey === mapState.dataKey) return true;
  }
  return false;
}

async function loadMap(center = null, { force = false } = {}) {
  const settings = localPayload();
  syncMapDataSource(settings, { force });
  const mapCenter = center || lastMapCenter || { x: settings.center_x, z: settings.center_z };
  mapState.centerX = Number(mapCenter.x) || 0;
  mapState.centerZ = Number(mapCenter.z) || 0;
  ensureVisibleMapTiles(settings);
}

function ensureVisibleMapTiles(settings = localPayload()) {
  syncMapDataSource(settings);
  const { width, height } = canvasMetrics();
  const spec = tileSpecForRadius();
  const coreRange = viewportTileRange(spec, width, height);
  const requestRange = viewportTileRange(spec, width, height, spec.span * 0.18);
  const generation = mapState.generation;
  const wantedKeys = new Set();
  const visibleKeys = new Set();
  const candidates = [];

  mapState.currentStep = spec.step;
  for (let tileZ = requestRange.minTileZ; tileZ <= requestRange.maxTileZ; tileZ++) {
    for (let tileX = requestRange.minTileX; tileX <= requestRange.maxTileX; tileX++) {
      const key = mapTileKey(mapState.dataKey, spec.step, tileX, tileZ);
      const core = tileIsInRange(tileX, tileZ, coreRange);
      wantedKeys.add(key);
      if (core) visibleKeys.add(key);
      const tile = mapState.tiles.get(key);
      if (tile) {
        tile.lastUsed = Date.now();
        continue;
      }
      const failed = mapState.failedTiles.get(key);
      if (failed && Date.now() - failed.at < MAP_TILE_RETRY_MS) continue;
      const worldCenterX = (tileX + 0.5) * spec.span;
      const worldCenterZ = (tileZ + 0.5) * spec.span;
      candidates.push({
        key,
        dataKey: mapState.dataKey,
        generation,
        tileX,
        tileZ,
        core,
        distance: Math.hypot(worldCenterX - mapState.centerX, worldCenterZ - mapState.centerZ),
        settings: { seed: settings.seed, version: settings.version },
        ...spec,
      });
    }
  }

  mapState.visibleTileKeys = visibleKeys;
  mapState.tileQueue = mapState.tileQueue.filter((item) => item.generation === generation && wantedKeys.has(item.key));
  mapState.queuedTileKeys = new Set(mapState.tileQueue.map((item) => item.key));
  candidates
    .filter((item) => !mapState.pendingTiles.has(item.key) && !mapState.queuedTileKeys.has(item.key))
    .forEach((item) => {
      mapState.tileQueue.push(item);
      mapState.queuedTileKeys.add(item.key);
    });
  mapState.tileQueue.sort((a, b) => Number(b.core) - Number(a.core) || a.distance - b.distance);

  updateMapMeta();
  updateMapLegend();
  updateMapLoadingUi();
  drawMap();
  pumpMapTileQueue();
}

function pumpMapTileQueue() {
  while (mapState.activeTileRequests < MAP_TILE_CONCURRENCY && mapState.tileQueue.length) {
    const item = mapState.tileQueue.shift();
    mapState.queuedTileKeys.delete(item.key);
    if (item.generation !== mapState.generation || mapState.tiles.has(item.key) || mapState.pendingTiles.has(item.key)) continue;
    fetchMapTile(item);
  }
  updateMapLoadingUi();
}

async function fetchMapTile(item) {
  const controller = new AbortController();
  mapState.activeTileRequests += 1;
  mapState.pendingTiles.set(item.key, { controller, generation: item.generation });
  updateMapLoadingUi();
  try {
    const data = await api("/map", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        seed: item.settings.seed,
        version: item.settings.version,
        center_x: Math.round((item.tileX + 0.5) * item.span),
        center_z: Math.round((item.tileZ + 0.5) * item.span),
        radius: item.radius,
        size: item.size,
      }),
    });
    if (item.generation !== mapState.generation) return;
    if (!data.map?.ok) throw new Error(data.warnings?.[0] || "地图区块不可用");
    renderMapTile(item, data.map, data.warnings || []);
  } catch (error) {
    if (error.name !== "AbortError" && item.generation === mapState.generation) {
      const message = error.message || "地图区块加载失败";
      mapState.failedTiles.set(item.key, { at: Date.now(), message });
      setMapWarning(`部分地图区块暂时不可用：${message}`);
      if (!hasAnyMapTile() && mapState.lastMapError !== message) {
        mapState.lastMapError = message;
        showNotice(`地图加载失败：${message}`, "warn");
      }
    }
  } finally {
    const pending = mapState.pendingTiles.get(item.key);
    if (pending?.controller === controller) mapState.pendingTiles.delete(item.key);
    mapState.activeTileRequests = Math.max(0, mapState.activeTileRequests - 1);
    updateMapLoadingUi();
    pumpMapTileQueue();
  }
}

function buildTileRaster(map) {
  const columns = Math.min(MAP_TILE_INTERVALS, Math.max(1, map.size - 1));
  const rows = columns;
  const raster = document.createElement("canvas");
  raster.width = columns;
  raster.height = rows;
  const ctx = raster.getContext("2d");
  const names = map.biomes.map((biome) => biome.name);
  const image = ctx.createImageData(columns, rows);
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < columns; col++) {
      const name = names[map.cells[row * map.size + col]] || "unknown";
      const rgb = rgbForBiome(name);
      const shade = terrainShade(map, row, col, name);
      const offset = (row * columns + col) * 4;
      image.data[offset] = Math.max(0, Math.min(255, Math.round(rgb[0] * shade)));
      image.data[offset + 1] = Math.max(0, Math.min(255, Math.round(rgb[1] * shade)));
      image.data[offset + 2] = Math.max(0, Math.min(255, Math.round(rgb[2] * shade)));
      image.data[offset + 3] = 255;
    }
  }
  ctx.putImageData(image, 0, 0);
  return { raster, names, columns, rows };
}

function renderMapTile(item, map, warnings = []) {
  const raster = buildTileRaster(map);
  const tile = {
    ...raster,
    key: item.key,
    dataKey: item.dataKey,
    stepLevel: item.step,
    span: item.span,
    tileX: item.tileX,
    tileZ: item.tileZ,
    x0: map.x0,
    z0: map.z0,
    map,
    highlightRasters: new Map(),
    lastUsed: Date.now(),
  };
  mapState.tiles.set(item.key, tile);
  mapState.failedTiles.delete(item.key);
  mapState.lastMapError = null;
  mapState.map = map;
  mapState.mapInfo = {
    mode: map.mode,
    version: map.version,
    mappedVersion: map.mapped_version,
    projection: map.projection,
    heightMode: map.height_mode,
  };
  if (warnings.length) setMapWarning(warnings[0]);
  else if (!mapState.failedTiles.size) setMapWarning();
  pruneMapTileCache();
  updateMapLoadingUi();
  updateMapMeta();
  updateMapLegend();
  drawMap();
}

function pruneMapTileCache() {
  if (mapState.tiles.size <= MAP_TILE_CACHE_LIMIT) return;
  const protectedKeys = new Set([
    ...mapState.visibleTileKeys,
    ...mapState.pendingTiles.keys(),
    ...mapState.queuedTileKeys,
  ]);
  const removable = [...mapState.tiles.values()]
    .filter((tile) => !protectedKeys.has(tile.key))
    .sort((a, b) => a.lastUsed - b.lastUsed);
  while (mapState.tiles.size > MAP_TILE_CACHE_LIMIT && removable.length) {
    mapState.tiles.delete(removable.shift().key);
  }
}

function updateMapLoadingUi() {
  let loaded = 0;
  let outstanding = 0;
  for (const key of mapState.visibleTileKeys) {
    if (mapState.tiles.has(key)) loaded += 1;
    else if (mapState.pendingTiles.has(key) || mapState.queuedTileKeys.has(key)) outstanding += 1;
  }
  const total = mapState.visibleTileKeys.size;
  const hasTile = hasAnyMapTile();
  $("mapLoading").hidden = hasTile || outstanding === 0;
  $("mapTileStatus").hidden = !hasTile || loaded >= total || outstanding === 0;
  $("mapTileStatusText").textContent = `新区块 ${loaded}/${total}`;
  $("loadMapBtn").setAttribute("aria-busy", outstanding > 0 ? "true" : "false");
  if (total > 0 && loaded === total) mapState.fallbackStep = null;
}

function updateMapMeta() {
  const info = mapState.mapInfo;
  const projection = info?.projection === "surface" ? "地表俯视" : "群系切片";
  const source = info
    ? `${projection} · ${modeNameZh(info.mode)} · ${info.version}${info.mappedVersion !== info.version ? ` 按 ${info.mappedVersion}` : ""}`
    : "区块地图";
  $("mapMeta").textContent = `${source} · 中心 X ${Math.round(mapState.centerX)}, Z ${Math.round(mapState.centerZ)} · 半径 ${Math.round(mapState.radius)}`;
}

function updateMapLegend() {
  const { width, height } = canvasMetrics();
  let tiles = visibleTilesForStep(mapState.currentStep, width, height);
  if (!tiles.length && mapState.fallbackStep) tiles = visibleTilesForStep(mapState.fallbackStep, width, height);
  const used = new Set();
  for (const tile of tiles) {
    for (const biomeIndex of tile.map.cells) {
      const name = tile.names[biomeIndex];
      if (name) used.add(name);
      if (used.size >= 18) break;
    }
    if (used.size >= 18) break;
  }
  if (!used.size) {
    $("mapLegend").textContent = "区块加载后显示";
    return;
  }
  $("mapLegend").innerHTML = [...used].map((name) => `<span><i style="background:${colorForBiome(name)}"></i>${escapeHtml(biomeNameZh(name))}</span>`).join("");
}

function resizeMapCanvas() {
  const canvas = $("mapCanvas");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  const pixelWidth = Math.floor(width * dpr);
  const pixelHeight = Math.floor(height * dpr);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  canvas.dataset.cssWidth = width;
  canvas.dataset.cssHeight = height;
}

function isLayerVisible(key) {
  return mapState.layers[key] !== false;
}

function biomeMatchesTarget(name, targetId) {
  if (name === targetId) return true;
  if (targetId === "ocean") return name.includes("ocean");
  if (targetId === "forest") return name.includes("forest");
  if (targetId === "jungle") return name.includes("jungle");
  if (targetId === "badlands") return name.includes("badlands");
  if (targetId === "swamp") return name.includes("swamp");
  if (targetId === "plains") return name.includes("plains");
  return false;
}

function activeBiomeTargets() {
  const targets = new Map();
  for (const marker of mapState.markers) {
    if (marker.kind === "biome" && marker.id && isLayerVisible(marker.key)) {
      targets.set(marker.id, marker.name || biomeNameZh(marker.id));
    }
  }
  return targets;
}

function tileIntersectsViewport(tile, cssWidth, cssHeight) {
  const topLeft = worldToScreen(tile.x0, tile.z0, cssWidth, cssHeight);
  const bottomRight = worldToScreen(tile.x0 + tile.span, tile.z0 + tile.span, cssWidth, cssHeight);
  return bottomRight.x >= 0 && bottomRight.y >= 0 && topLeft.x <= cssWidth && topLeft.y <= cssHeight;
}

function visibleTilesForStep(step, cssWidth, cssHeight) {
  if (!step) return [];
  const visible = [];
  for (const tile of mapState.tiles.values()) {
    if (tile.dataKey !== mapState.dataKey || tile.stepLevel !== step) continue;
    if (!tileIntersectsViewport(tile, cssWidth, cssHeight)) continue;
    tile.lastUsed = Date.now();
    visible.push(tile);
  }
  return visible.sort((a, b) => a.tileZ - b.tileZ || a.tileX - b.tileX);
}

function drawTilePlaceholders(ctx, spec, cssWidth, cssHeight) {
  ctx.fillStyle = "#dce5df";
  ctx.fillRect(0, 0, cssWidth, cssHeight);
  const range = viewportTileRange(spec, cssWidth, cssHeight);
  for (let tileZ = range.minTileZ; tileZ <= range.maxTileZ; tileZ++) {
    for (let tileX = range.minTileX; tileX <= range.maxTileX; tileX++) {
      const topLeft = worldToScreen(tileX * spec.span, tileZ * spec.span, cssWidth, cssHeight);
      const bottomRight = worldToScreen((tileX + 1) * spec.span, (tileZ + 1) * spec.span, cssWidth, cssHeight);
      ctx.fillStyle = (tileX + tileZ) % 2 === 0 ? "#dbe4dd" : "#d6e0d9";
      ctx.fillRect(topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y);
      ctx.strokeStyle = "rgba(57, 73, 62, 0.12)";
      ctx.lineWidth = 1;
      ctx.strokeRect(Math.floor(topLeft.x) + 0.5, Math.floor(topLeft.y) + 0.5, Math.ceil(bottomRight.x - topLeft.x), Math.ceil(bottomRight.y - topLeft.y));
    }
  }
}

function highlightRasterForTile(tile, targets) {
  if (!targets.size) return null;
  const targetIds = [...targets.keys()].sort();
  const key = targetIds.join("|");
  if (tile.highlightRasters.has(key)) return tile.highlightRasters.get(key);
  const raster = document.createElement("canvas");
  raster.width = tile.columns;
  raster.height = tile.rows;
  const ctx = raster.getContext("2d");
  ctx.fillStyle = "rgba(255, 255, 255, 0.22)";
  for (let row = 0; row < tile.rows; row++) {
    for (let col = 0; col < tile.columns; col++) {
      const name = tile.names[tile.map.cells[row * tile.map.size + col]] || "unknown";
      if (targetIds.some((targetId) => biomeMatchesTarget(name, targetId))) ctx.fillRect(col, row, 1, 1);
    }
  }
  tile.highlightRasters.set(key, raster);
  return raster;
}

function drawTileLayer(ctx, tiles, cssWidth, cssHeight, targets) {
  for (const tile of tiles) {
    const topLeft = worldToScreen(tile.x0, tile.z0, cssWidth, cssHeight);
    const bottomRight = worldToScreen(tile.x0 + tile.span, tile.z0 + tile.span, cssWidth, cssHeight);
    const left = Math.floor(topLeft.x);
    const top = Math.floor(topLeft.y);
    const width = Math.ceil(bottomRight.x) - left;
    const height = Math.ceil(bottomRight.y) - top;
    ctx.drawImage(tile.raster, left, top, width, height);
    const highlight = highlightRasterForTile(tile, targets);
    if (highlight) ctx.drawImage(highlight, left, top, width, height);
  }
}

function markerScreenPosition(marker, cssWidth, cssHeight) {
  return worldToScreen(marker.x, marker.z, cssWidth, cssHeight);
}

function rectanglesOverlap(a, b, padding = 3) {
  return !(
    a.right + padding <= b.left
    || a.left >= b.right + padding
    || a.bottom + padding <= b.top
    || a.top >= b.bottom + padding
  );
}

function drawMapMarkers(ctx, cssWidth, cssHeight, reservedLabels = []) {
  const markers = mapState.markers.filter((marker) => isLayerVisible(marker.key));
  if (!markers.length) return;
  const placedLabels = [...reservedLabels];
  ctx.save();
  ctx.font = "800 12px system-ui, -apple-system, BlinkMacSystemFont, sans-serif";
  ctx.textBaseline = "middle";
  for (const marker of markers) {
    const point = markerScreenPosition(marker, cssWidth, cssHeight);
    if (point.x < -80 || point.y < -40 || point.x > cssWidth + 80 || point.y > cssHeight + 40) continue;

    const color = markerColor(marker.kind, marker.id);
    ctx.lineWidth = 2;
    ctx.fillStyle = color;
    ctx.strokeStyle = "#ffffff";
    ctx.beginPath();
    if (marker.kind === "site") {
      ctx.moveTo(point.x, point.y - 9);
      ctx.lineTo(point.x + 9, point.y);
      ctx.lineTo(point.x, point.y + 9);
      ctx.lineTo(point.x - 9, point.y);
      ctx.closePath();
    } else {
      ctx.arc(point.x, point.y, 7, 0, Math.PI * 2);
    }
    ctx.fill();
    ctx.stroke();

    const text = marker.label;
    const textWidth = Math.min(170, ctx.measureText(text).width + 12);
    const xCandidates = [point.x + 11, point.x - textWidth - 11];
    const yOffsets = [0, -27, 27, -54, 54];
    let labelRect = null;
    for (const rawX of xCandidates) {
      for (const offset of yOffsets) {
        const labelX = Math.max(5, Math.min(cssWidth - textWidth - 5, rawX));
        const labelY = Math.max(14, Math.min(cssHeight - 14, point.y + offset));
        const candidate = { left: labelX, top: labelY - 11, right: labelX + textWidth, bottom: labelY + 11 };
        if (!placedLabels.some((placed) => rectanglesOverlap(candidate, placed))) {
          labelRect = candidate;
          break;
        }
      }
      if (labelRect) break;
    }
    if (!labelRect) continue;
    placedLabels.push(labelRect);
    const labelX = labelRect.left;
    const labelY = labelRect.top + 11;
    ctx.fillStyle = "rgba(255, 255, 255, 0.92)";
    ctx.strokeStyle = "rgba(29, 48, 36, 0.16)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(labelX, labelY - 11, textWidth, 22, 3);
    else ctx.rect(labelX, labelY - 11, textWidth, 22);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "#24352b";
    ctx.fillText(text, labelX + 6, labelY, textWidth - 10);
  }
  ctx.restore();
}

function drawRelativeLayoutGuides(ctx, cssWidth, cssHeight) {
  const labelRects = [];
  for (const result of mapState.results) {
    for (const check of result?.layout_checks || []) {
      const center = result.targets?.find((target) => target.id === check.center);
      const back = result.targets?.find((target) => target.id === check.back);
      const front = result.targets?.find((target) => target.id === check.front);
      if (!center || !back || !front) continue;
      if (![center, back, front].every((target) => isLayerVisible(`${target.kind}:${target.id}`))) continue;
      const centerPoint = markerScreenPosition(center, cssWidth, cssHeight);
      const backPoint = markerScreenPosition(back, cssWidth, cssHeight);
      const frontPoint = markerScreenPosition(front, cssWidth, cssHeight);

      ctx.save();
      ctx.lineCap = "square";
      ctx.lineJoin = "miter";
      ctx.setLineDash([7, 5]);
      ctx.beginPath();
      ctx.moveTo(backPoint.x, backPoint.y);
      ctx.lineTo(centerPoint.x, centerPoint.y);
      ctx.lineTo(frontPoint.x, frontPoint.y);
      ctx.strokeStyle = "rgba(24, 31, 26, 0.74)";
      ctx.lineWidth = 5;
      ctx.stroke();
      ctx.strokeStyle = check.passed ? "rgba(244, 224, 112, 0.96)" : "rgba(212, 82, 69, 0.96)";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);

      if (centerPoint.x >= 0 && centerPoint.x <= cssWidth && centerPoint.y >= 0 && centerPoint.y <= cssHeight) {
        const label = `${Number(check.angle || 0).toFixed(1)}°`;
        ctx.font = "800 11px ui-monospace, monospace";
        const width = ctx.measureText(label).width + 10;
        const x = Math.max(4, Math.min(cssWidth - width - 4, centerPoint.x - width / 2));
        const y = Math.max(18, Math.min(cssHeight - 6, centerPoint.y + 24));
        ctx.fillStyle = "rgba(24, 31, 26, 0.84)";
        ctx.fillRect(x, y - 14, width, 18);
        ctx.fillStyle = "#f7e77c";
        ctx.fillText(label, x + 5, y - 2);
        labelRects.push({ left: x, top: y - 14, right: x + width, bottom: y + 4 });
      }
      ctx.restore();
    }
  }
  return labelRects;
}

function drawMap() {
  const canvas = $("mapCanvas");
  resizeMapCanvas();
  const cssWidth = Number(canvas.dataset.cssWidth);
  const cssHeight = Number(canvas.dataset.cssHeight);
  const dpr = window.devicePixelRatio || 1;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const spec = tileSpecForRadius();
  const targets = activeBiomeTargets();
  drawTilePlaceholders(ctx, spec, cssWidth, cssHeight);
  if (mapState.fallbackStep && mapState.fallbackStep !== mapState.currentStep) {
    drawTileLayer(ctx, visibleTilesForStep(mapState.fallbackStep, cssWidth, cssHeight), cssWidth, cssHeight, targets);
  }
  drawTileLayer(ctx, visibleTilesForStep(mapState.currentStep, cssWidth, cssHeight), cssWidth, cssHeight, targets);

  const centerX = cssWidth / 2;
  const centerY = cssHeight / 2;
  ctx.strokeStyle = "rgba(255,255,255,0.95)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(centerX - 10, centerY);
  ctx.lineTo(centerX + 10, centerY);
  ctx.moveTo(centerX, centerY - 10);
  ctx.lineTo(centerX, centerY + 10);
  ctx.stroke();
  const layoutLabelRects = drawRelativeLayoutGuides(ctx, cssWidth, cssHeight);
  drawMapMarkers(ctx, cssWidth, cssHeight, layoutLabelRects);
  ctx.strokeStyle = "rgba(20,32,24,0.35)";
  ctx.strokeRect(0.5, 0.5, cssWidth - 1, cssHeight - 1);
}

function markerAtCanvasPoint(x, y, cssWidth, cssHeight) {
  let closest = null;
  let closestDistance = Infinity;
  for (const marker of mapState.markers) {
    if (!isLayerVisible(marker.key)) continue;
    const point = markerScreenPosition(marker, cssWidth, cssHeight);
    const distance = Math.hypot(point.x - x, point.y - y);
    if (distance < 14 && distance < closestDistance) {
      closest = marker;
      closestDistance = distance;
    }
  }
  return closest;
}

function cachedTileAtWorld(x, z, step) {
  if (!step) return null;
  const span = step * MAP_TILE_INTERVALS;
  const tileX = Math.floor(x / span);
  const tileZ = Math.floor(z / span);
  return mapState.tiles.get(mapTileKey(mapState.dataKey, step, tileX, tileZ)) || null;
}

function biomeAtCanvasPoint(x, y, cssWidth, cssHeight) {
  const world = screenToWorld(x, y, cssWidth, cssHeight);
  const tile = cachedTileAtWorld(world.x, world.z, mapState.currentStep)
    || cachedTileAtWorld(world.x, world.z, mapState.fallbackStep);
  if (!tile) return null;
  const col = Math.floor((world.x - tile.x0) / tile.map.step);
  const row = Math.floor((world.z - tile.z0) / tile.map.step);
  if (col < 0 || row < 0 || col >= tile.columns || row >= tile.rows) return null;
  const index = row * tile.map.size + col;
  const name = tile.names[tile.map.cells[index]] || "unknown";
  const surfaceY = Array.isArray(tile.map.heights) ? Number(tile.map.heights[index]) : null;
  return { name, surfaceY, x: Math.round(world.x), z: Math.round(world.z) };
}

function showMapTooltip(event, html) {
  const tooltip = $("mapTooltip");
  const surface = $("mapCanvas").parentElement;
  const surfaceRect = surface.getBoundingClientRect();
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const width = tooltip.offsetWidth || 160;
  const height = tooltip.offsetHeight || 60;
  let left = event.clientX - surfaceRect.left + 14;
  let top = event.clientY - surfaceRect.top + 14;
  if (left + width > surfaceRect.width - 8) left = event.clientX - surfaceRect.left - width - 14;
  if (top + height > surfaceRect.height - 8) top = event.clientY - surfaceRect.top - height - 14;
  tooltip.style.left = `${Math.max(8, left)}px`;
  tooltip.style.top = `${Math.max(8, top)}px`;
}

function hideMapTooltip() {
  $("mapTooltip").hidden = true;
}

function updateMapTooltip(event) {
  const canvas = $("mapCanvas");
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const marker = markerAtCanvasPoint(x, y, rect.width, rect.height);
  if (marker) {
    showMapTooltip(event, `
      <strong>${escapeHtml(marker.name || marker.label)}</strong>
      <div>${escapeHtml(kindNameZh(marker.kind))} · X ${marker.x}, Z ${marker.z}</div>
      <div>来自候选 ${marker.candidate}</div>
    `);
    return;
  }
  const biome = biomeAtCanvasPoint(x, y, rect.width, rect.height);
  if (!biome) return hideMapTooltip();
  showMapTooltip(event, `
    <strong>${escapeHtml(biomeNameZh(biome.name))}</strong>
    <div>地表群系 · X ${biome.x}, Z ${biome.z}</div>
    ${Number.isFinite(biome.surfaceY) ? `<div>估算地表高度 · Y≈${Math.round(biome.surfaceY)}</div>` : ""}
  `);
}

function runVisibleTileCheck() {
  mapState.lastTileCheck = Date.now();
  try {
    ensureVisibleMapTiles();
  } catch (error) {
    showNotice(`地图加载失败：${error.message}`, "warn");
  }
}

function scheduleMapReload(delay = 90) {
  const elapsed = Date.now() - mapState.lastTileCheck;
  if (elapsed >= 120) {
    clearTimeout(mapState.reloadTimer);
    runVisibleTileCheck();
    return;
  }
  clearTimeout(mapState.reloadTimer);
  mapState.reloadTimer = setTimeout(runVisibleTileCheck, delay);
}

function panMap(deltaX, deltaY) {
  const { width, height } = canvasMetrics();
  const blocksPerPx = cameraScale(width, height);
  mapState.centerX -= deltaX * blocksPerPx;
  mapState.centerZ -= deltaY * blocksPerPx;
  updateMapMeta();
  drawMap();
  scheduleMapReload();
}

function zoomMap(multiplier) {
  const previousStep = tileSpecForRadius().step;
  const next = Math.max(128, Math.min(65536, mapState.radius / multiplier));
  if (next === mapState.radius) return;
  mapState.radius = next;
  const nextStep = tileSpecForRadius().step;
  if (nextStep !== previousStep && hasTilesAtStep(previousStep)) mapState.fallbackStep = previousStep;
  mapState.currentStep = nextStep;
  updateMapMeta();
  drawMap();
  scheduleMapReload(60);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

async function refreshMe() {
  try {
    const me = await api("/me");
    setLoggedIn(me);
    const serverSettings = await api("/settings");
    if (loadedLocalSettings) {
      currentSettings = { ...(currentSettings || {}), ...serverSettings, ...localPayload(), deepseek_api_key_set: serverSettings.deepseek_api_key_set };
      $("apiKey").placeholder = serverSettings.deepseek_api_key_set ? "已保存；留空继续使用已保存 key" : "必填；用于 DeepSeek 解析需求";
      updateKeyState();
    } else {
      fillSettings(serverSettings);
    }
    await loadModels();
  } catch {
    setLoggedIn(null);
    await loadModels();
  }
}

async function saveSettings({ quiet = false } = {}) {
  saveLocalSettings({ normalize: true });
  if (!loggedIn) {
    $("saveState").textContent = "已保存到本机浏览器";
    if (!quiet) addMessage("assistant", "已保存到本机浏览器。登录后可保存到账号。");
    if (!quiet) showNotice("已保存到本机浏览器", "ok");
    return null;
  }
  const saved = await api("/settings", { method: "PUT", body: JSON.stringify(readSettings()) });
  $("apiKey").value = "";
  fillSettings(saved);
  $("saveState").textContent = "已保存到账号";
  if (!quiet) addMessage("assistant", "配置已保存到账号。");
  if (!quiet) showNotice("配置已保存到账号", "ok");
  return saved;
}

async function runSearch(message) {
  if (searchBusy) {
    showNotice("已有搜索正在进行，请等待当前搜索完成", "warn");
    return;
  }
  setResultFeedback(null);
  addMessage("user", message);
  if (!hasUsableApiKey()) {
    setPlannerStatus("等待 API Key", "warning");
    const text = "请先填写并保存 DeepSeek API Key。这个项目现在不启用本地关键词兜底，没有 AI 解析就不会运行搜索。";
    addMessage("assistant", text);
    showNotice(text, "warn");
    return;
  }
  saveLocalSettings({ normalize: true });
  setSearchBusy(true);
  setPlannerStatus("搜索中", "searching");
  const requestId = makeRequestId();
  startProgressPolling(requestId);
  try {
    let payload;
    if (loggedIn) {
      try {
        await saveSettings({ quiet: true });
        payload = await api("/chat", { method: "POST", body: JSON.stringify({ message, request_id: requestId }) });
      } catch (saveOrChatError) {
        showNotice(`账号保存/聊天失败，已改用直接搜索：${saveOrChatError.message}`, "warn");
        payload = await api("/search", { method: "POST", body: JSON.stringify({ query: message, request_id: requestId, ...readSettings() }) });
      }
    } else {
      payload = await api("/search", { method: "POST", body: JSON.stringify({ query: message, request_id: requestId, ...readSettings() }) });
    }
    addMessage("assistant", payload.reply);
    renderResults(payload);
    setResultFeedback({
      query: message,
      request_id: requestId,
      planner: payload.planner,
      plan: payload.plan,
    });
    showNotice("搜索完成", "ok");
    stopProgressPolling("搜索完成", "done");
  } catch (e) {
    setPlannerStatus("失败", "error");
    addMessage("assistant", e.message);
    showNotice(`搜索失败：${e.message}`, "error");
    stopProgressPolling("搜索失败", "error");
  } finally {
    setSearchBusy(false);
    updateKeyState();
  }
}

$("unmetFeedbackBtn").onclick = async () => {
  if (!lastCompletedSearch) return;
  const button = $("unmetFeedbackBtn");
  button.disabled = true;
  button.textContent = "正在记录";
  try {
    await api("/feedback/unmet", {
      method: "POST",
      body: JSON.stringify({ ...lastCompletedSearch, reason: "not_solved" }),
    });
    button.textContent = "已记录";
    showNotice("已记录这次未解决的需求", "ok");
  } catch (error) {
    button.disabled = false;
    button.textContent = "重新提交";
    showNotice(`记录失败：${error.message}`, "error");
  }
};

$("loginBtn").onclick = async () => {
  try {
    const me = await api("/auth/login", { method: "POST", body: JSON.stringify({ username: $("username").value, password: $("password").value }) });
    setLoggedIn(me);
    await refreshMe();
    addMessage("assistant", "已登录。登录框已收起。");
  } catch (e) {
    addMessage("assistant", e.message);
    showNotice(`登录失败：${e.message}`, "error");
  }
};

$("registerBtn").onclick = async () => {
  try {
    const me = await api("/auth/register", { method: "POST", body: JSON.stringify({ username: $("username").value, password: $("password").value }) });
    setLoggedIn(me);
    await refreshMe();
    addMessage("assistant", "注册并登录成功。");
  } catch (e) {
    addMessage("assistant", e.message);
    showNotice(`注册失败：${e.message}`, "error");
  }
};

$("logoutBtn").onclick = async () => {
  await api("/auth/logout", { method: "POST", body: "{}" }).catch(() => null);
  setLoggedIn(null);
  addMessage("assistant", "已退出登录。本机缓存的世界参数仍保留。");
};

$("saveBtn").onclick = () => {
  saveSettings().catch((e) => {
    addMessage("assistant", e.message);
    showNotice(`保存失败：${e.message}`, "error");
  });
};

$("chatForm").onsubmit = async (event) => {
  event.preventDefault();
  const message = $("chatInput").value.trim();
  if (!message) return;
  $("chatInput").value = "";
  await runSearch(message);
};

document.querySelectorAll(".preset").forEach((button) => {
  button.addEventListener("click", () => {
    const query = button.dataset.query;
    $("chatInput").value = query;
    runSearch(query);
  });
});

persistedFields.forEach((id) => {
  const eventName = id === "model" ? "change" : "input";
  $(id).addEventListener(eventName, () => {
    saveLocalSettings();
    $("saveState").textContent = loggedIn ? "有改动，搜索前自动保存" : "已保存到本机浏览器";
  });
});

["seed", "version", "centerX", "centerZ"].forEach((id) => {
  $(id).addEventListener("change", () => {
    clearTimeout(mapState.settingsTimer);
    mapState.settingsTimer = setTimeout(() => {
      mapState.radius = 2048;
      lastMapCenter = null;
      loadMap().catch((e) => showNotice(`地图加载失败：${e.message}`, "warn"));
    }, 180);
  });
});

$("apiKey").addEventListener("input", () => {
  updateKeyState();
  clearTimeout($("apiKey").modelTimer);
  $("apiKey").modelTimer = setTimeout(() => loadModels({ quiet: false }), 700);
});
$("baseUrl").addEventListener("change", () => loadModels({ quiet: false }));
$("loadMapBtn").addEventListener("click", () => {
  saveLocalSettings({ normalize: true });
  loadMap(null, { force: true }).catch((e) => {
    $("loadMapBtn").disabled = false;
    showNotice(`地图加载失败：${e.message}`, "error");
  });
});
$("zoomInBtn").addEventListener("click", () => zoomMap(1.35));
$("zoomOutBtn").addEventListener("click", () => zoomMap(1 / 1.35));
$("resetMapBtn").addEventListener("click", () => {
  const settings = localPayload();
  const previousStep = tileSpecForRadius().step;
  mapState.radius = 2048;
  const nextStep = tileSpecForRadius().step;
  if (nextStep !== previousStep && hasTilesAtStep(previousStep)) mapState.fallbackStep = previousStep;
  loadMap({ x: settings.center_x, z: settings.center_z }).catch((e) => showNotice(`地图加载失败：${e.message}`, "error"));
});
$("mapCanvas").addEventListener("wheel", (event) => {
  event.preventDefault();
  hideMapTooltip();
  zoomMap(event.deltaY < 0 ? 1.35 : 1 / 1.35);
}, { passive: false });
$("mapCanvas").addEventListener("pointerdown", (event) => {
  mapState.dragging = true;
  mapState.lastX = event.clientX;
  mapState.lastY = event.clientY;
  hideMapTooltip();
  $("mapCanvas").setPointerCapture(event.pointerId);
});
$("mapCanvas").addEventListener("pointermove", (event) => {
  if (mapState.dragging) {
    panMap(event.clientX - mapState.lastX, event.clientY - mapState.lastY);
    mapState.lastX = event.clientX;
    mapState.lastY = event.clientY;
    return;
  }
  updateMapTooltip(event);
});
$("mapCanvas").addEventListener("pointerup", (event) => {
  mapState.dragging = false;
  runVisibleTileCheck();
  updateMapTooltip(event);
});
$("mapCanvas").addEventListener("pointercancel", () => {
  mapState.dragging = false;
  hideMapTooltip();
  runVisibleTileCheck();
});
$("mapCanvas").addEventListener("pointerleave", () => {
  const wasDragging = mapState.dragging;
  mapState.dragging = false;
  hideMapTooltip();
  if (wasDragging) runVisibleTileCheck();
});
window.addEventListener("resize", () => {
  drawMap();
  scheduleMapReload(120);
});

async function initialize() {
  loadLocalSettings();
  updateSummary();
  await refreshMe();
  await loadMap();
}

initialize().catch((e) => {
  $("mapLoading").hidden = true;
  $("mapMeta").textContent = "地图暂时不可用";
  showNotice(`地图加载失败：${e.message}`, "warn");
});
