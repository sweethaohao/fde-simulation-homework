/* 单页前端逻辑：原生 JS，fetch 统一响应，无框架、无外链。 */
"use strict"; // 严格模式，避免隐式全局

/** 角色代码转中文名。 */
function roleName(code) { // 入参：角色代码
  const map = { admin: "系统管理员", ops_director: "运营总监", loss_manager: "商品与损耗经理", planner: "补货计划员", store_manager: "门店店长", logistics: "区域物流经理", data_engineer: "数据工程师" }; // 映射
  return map[code] || code || "—"; // 未知代码原样返回
}

/* ========== 全局状态 ========== */
const state = { // 应用状态
  token: sessionStorage.getItem("token") || "", // 会话内 token（关标签页即失效）
  user: null, // 当前用户
  scope: null, // 数据域
  actions: [], // 可执行动作
  options: null, // 补货下拉选项缓存
  view: "dashboard", // 当前视图
};

const $ = (sel) => document.querySelector(sel); // 单元素查询
const $$ = (sel) => Array.from(document.querySelectorAll(sel)); // 多元素查询

/** 转义 HTML，防止数据里的尖括号破坏页面。 */
function esc(value) { // 入参：任意值；出参：转义后的字符串
  return String(value == null ? "" : value) // 空值统一为空串
    .replaceAll("&", "&amp;") // 转义 and
    .replaceAll("<", "&lt;") // 转义小于号
    .replaceAll(">", "&gt;") // 转义大于号
    .replaceAll('"', "&quot;") // 转义双引号
    .replaceAll("'", "&#39;"); // 转义单引号
}

/** 百分比格式化：0.1203 → 12.0%。 */
function pct(rate, digits = 1) { // 入参：比率与小数位
  if (rate == null || Number.isNaN(Number(rate))) return "—"; // 无值不编造
  return (Number(rate) * 100).toFixed(digits) + "%"; // 转百分比
}

/** 时间格式化：ISO 字符串截到分钟。 */
function fmtTime(value) { // 入参：ISO 字符串
  if (!value) return "—"; // 无值
  return String(value).replace("T", " ").slice(0, 19); // 去掉时区后缀便于阅读
}

/** 统一的接口调用：自动带 token、解析统一响应。 */
async function api(path, options = {}) { // 入参：路径与 fetch 选项
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {}); // 默认 JSON
  if (state.token) headers.Authorization = "Bearer " + state.token; // 附加 token
  const res = await fetch(path, Object.assign({}, options, { headers })); // 发起请求
  let body = null; // 响应体
  try { body = await res.json(); } catch { body = { ok: false, error: { code: "INTERNAL", msg: "响应不是 JSON" } }; } // 容错
  if (res.status === 401 && !path.includes("/auth/login")) { // token 失效
    clearSession(); // 清理会话
  }
  body._status = res.status; // 附带 HTTP 状态
  return body; // 返回统一信封
}

/** 读取 data，失败时抛出带错误码的异常（供写操作使用）。 */
function must(body) { // 入参：统一信封
  if (body && body.ok) return body.data; // 成功直接返回 data
  const err = new Error((body && body.error && body.error.msg) || "请求失败"); // 组装错误
  err.code = body && body.error && body.error.code; // 错误码
  err.data = body && body.data; // 错误附加数据（统一信封放在顶层 data）
  err.status = body && body._status; // HTTP 状态
  throw err; // 交给调用方展示
}

/* ========== 轻提示与弹窗 ========== */
/** 显示一条轻提示。 */
function toast(message, type = "") { // 入参：消息与类型
  const node = document.createElement("div"); // 创建节点
  node.className = "toast " + type; // 类型样式
  node.textContent = message; // 纯文本，避免注入
  $("#toast-stack").appendChild(node); // 入栈
  setTimeout(() => node.remove(), 4200); // 4.2 秒后移除
}

/** 二次确认弹窗（可选输入框），返回 Promise。 */
function ask(title, body, opts = {}) { // 入参：标题、正文、{label,value,placeholder,okText}
  return new Promise((resolve) => { // 返回 Promise，等待用户选择
    $("#modal-title").textContent = title; // 标题
    const inputHtml = opts.label // 是否带输入框
      ? `<label class="field" style="margin-top:10px"><span>${esc(opts.label)}</span><input id="modal-input" value="${esc(opts.value || "")}" placeholder="${esc(opts.placeholder || "")}" /></label>`
      : ""; // 无输入框
    $("#modal-body").innerHTML = esc(body) + inputHtml; // 正文（先转义）
    $("#modal-ok").textContent = opts.okText || "确认"; // 确认按钮文案
    $("#modal-backdrop").hidden = false; // 显示遮罩
    const done = (ok) => { // 收尾函数
      $("#modal-backdrop").hidden = true; // 隐藏遮罩
      const value = opts.label ? ($("#modal-input").value || "") : ""; // 读取输入
      resolve({ ok, value }); // 返回结果
    };
    $("#modal-ok").onclick = () => done(true); // 确认
    $("#modal-cancel").onclick = () => done(false); // 取消
  });
}

/* ========== 会话 ========== */
/** 清理本地会话并回到登录页。 */
function clearSession() { // 无入参
  state.token = ""; // 清 token
  state.user = null; // 清用户
  sessionStorage.removeItem("token"); // 清存储
  $("#shell").hidden = true; // 隐藏主壳体
  $("#userbox").hidden = true; // 隐藏用户区
  $("#login-view").hidden = false; // 显示登录页
}

/** 登录成功后初始化界面。 */
function enterApp(payload) { // 入参：登录返回 data
  state.token = payload.token || state.token; // 保存 token
  state.user = payload.user; // 用户
  state.scope = payload.scope; // 数据域
  sessionStorage.setItem("token", state.token); // 会话存储
  $("#login-view").hidden = true; // 隐藏登录
  $("#shell").hidden = false; // 显示主壳
  $("#userbox").hidden = false; // 显示用户区
  $("#user-name").textContent = state.user.display_name + "（" + state.user.username + "）"; // 姓名
  $("#user-role").textContent = state.user.role_name; // 角色
  loadMe().then(() => switchView("dashboard")); // 拉取动作权限后进看板
}

/** 拉取 /auth/me，补齐可用动作。 */
async function loadMe() { // 无入参
  const body = await api("/api/auth/me"); // 请求
  if (!body.ok) { clearSession(); return; } // 失效则退出
  state.user = body.data.user; // 刷新用户
  state.scope = body.data.scope; // 刷新数据域
  state.actions = body.data.actions || []; // 动作权限
  $("#user-name").textContent = state.user.display_name + "（" + state.user.username + "）"; // 姓名
  $("#user-role").textContent = state.user.role_name; // 角色
}

/** 是否有某个动作权限（仅用于前端隐藏按钮，后端仍硬校验）。 */
function can(action) { // 入参：动作名
  return state.actions.includes(action); // 是否包含
}

/* ========== 视图切换 ========== */
/** 切换视图并触发对应加载。 */
function switchView(view) { // 入参：视图名
  state.view = view; // 记录
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === view)); // 标签态
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === "view-" + view)); // 视图态
  if (view === "dashboard") loadDashboard(); // 看板
  if (view === "trace") loadTrace(); // 溯源
  if (view === "replenish") loadReplenish(); // 补货
  if (view === "admin") loadAdmin(); // 管理
}

/* ========== 视图一：诊断看板 ========== */
/** 加载看板数据。 */
async function loadDashboard() { // 无入参
  $("#dashboard-meta").textContent = "加载中…"; // 加载态
  const body = await api("/api/dashboard/summary"); // 请求汇总
  if (!body.ok) { $("#dashboard-meta").textContent = "加载失败：" + (body.error && body.error.msg); return; } // 失败提示
  const data = body.data; // 数据
  $("#dashboard-meta").textContent = "业务日 " + (data.as_of || "—") + " · 合成数据"; // 元信息
  const scopeStores = data.scope_stores || []; // 可见门店（后端只返回 ID 列表）
  const scopeMeta = state.scope || {}; // 当前数据域元信息
  $("#dashboard-scope").textContent = "数据域：" + (scopeMeta.role_name || state.user.role_name) + " · " + (scopeMeta.type || "—") +
    (scopeMeta.value ? "（" + scopeMeta.value + "）" : "") + " · 可见门店 " + scopeStores.length + " 家：" + scopeStores.join("、"); // 数据域说明
  const snap = data.stockout.snapshot; // 当日截面
  const tw = data.stockout.time_weighted; // 时段加权
  const diff = data.account_diff; // 账实差异
  const orders = data.orders; // 单据
  const cards = [ // 数字卡配置
    { label: "缺货率 · 当日截面", value: pct(snap.rate), sub: snap.numerator + " / " + snap.denominator + " · " + (snap.date || ""), tone: "bad", go: "stockout_rate" },
    { label: "缺货率 · 时段加权", value: pct(tw.rate), sub: "窗口 " + tw.window_days + " 天 · 差 " + data.stockout.gap_pp + "pp", tone: "warn", go: "stockout_rate" },
    { label: "账实差异率", value: pct(diff.rate), sub: "真差异 " + diff.true_diff + " · 编码伪差异 " + diff.code_pseudo, tone: "warn", go: "account_diff_rate" },
    { label: "待签核单据", value: String(orders.pending_sign), sub: "草稿 " + orders.draft + " · 已签 " + orders.signed + " · 兜底 " + orders.stopped, tone: "accent", go: "" },
  ];
  $("#dashboard-cards").innerHTML = cards.map((c) => ` <!-- 数字卡片 -->
    <div class="stat-card ${c.tone} ${c.go ? "clickable" : ""}" ${c.go ? `data-metric="${c.go}"` : ""}>
      <div class="label">${esc(c.label)}</div>
      <div class="value">${esc(c.value)}</div>
      <div class="sub">${esc(c.sub)}</div>
    </div>`).join(""); // 渲染卡片
  $$("#dashboard-cards .clickable").forEach((node) => { // 卡片点击溯源
    node.onclick = () => { // 点击事件
      switchView("trace"); // 切到溯源
      $("#metric-trace-code").value = node.dataset.metric; // 选指标
      runMetricTrace(); // 立即查询
    };
  });
  renderBlocked(data.blocked_metrics || []); // 拦截指标
  renderDiff(diff); // 账实拆分
  renderReadiness(data.readiness || {}); // 就绪度
  renderGuardrailHits(data.guardrail_hits || []); // 兜底命中
  renderOrderStats(orders); // 单据状态
  renderStockout(data.stockout); // 缺货两种分母
}

/** 渲染被拦截指标。 */
function renderBlocked(items) { // 入参：blocked_metrics 数组
  if (!items.length) { $("#dashboard-blocked").innerHTML = '<p class="muted">当前没有拦截项。</p>'; return; } // 空态
  $("#dashboard-blocked").innerHTML = items.map((item) => ` <!-- 拦截卡片 -->
    <div class="blocked-item">
      <div class="title">${esc(item.name || item.code)} <span class="badge bad">${esc(item.reason_code)}</span></div>
      <div class="reason">${esc(item.reason)}</div>
      <div class="next">需要签发人：${esc(item.required_signer || "—")} · 未签发/数据不足时不填估算值（R10）</div>
    </div>`).join(""); // 渲染
}

/** 渲染账实差异拆分。 */
function renderDiff(diff) { // 入参：account_diff
  const total = Math.max(1, diff.total); // 防止除零
  const trueW = Math.round((diff.true_diff / total) * 100); // 真差异占比条
  const pseudoW = Math.round((diff.code_pseudo / total) * 100); // 伪差异占比条
  $("#dashboard-diff").innerHTML = ` <!-- 账实拆分 -->
    <div class="kv">
      <dt>差异行 / 总行</dt><dd>${diff.flagged} / ${diff.total}（${pct(diff.rate)}）</dd>
      <dt>真差异</dt><dd>${diff.true_diff} 行（${pct(diff.true_diff / total)}）</dd>
      <dt>编码伪差异</dt><dd>${diff.code_pseudo} 行（${pct(diff.code_pseudo / total)}）</dd>
    </div>
    <div style="margin-top:10px" class="bar"><i style="width:${trueW}%;background:var(--bad)"></i></div>
    <div class="bar" style="margin-top:4px"><i style="width:${pseudoW}%;background:var(--warn)"></i></div>
    <p class="muted small" style="margin-top:8px">${esc(diff.note)}</p>`; // 渲染
}

/** 渲染就绪度摘要。 */
function renderReadiness(summary) { // 入参：{ready, partial, not_ready}
  const ready = summary.ready || 0; // 就绪
  const partial = summary.partial || 0; // 部分
  const notReady = summary.not_ready || 0; // 未就绪
  const total = Math.max(1, ready + partial + notReady); // 总数
  $("#dashboard-readiness").innerHTML = ` <!-- 就绪度 -->
    <div class="pill-row">
      <span class="badge ok">就绪 ${ready}</span>
      <span class="badge warn">部分 ${partial}</span>
      <span class="badge bad">未就绪 ${notReady}</span>
    </div>
    <div class="bar" style="margin-top:12px"><i style="width:${Math.round((ready / total) * 100)}%;background:var(--ok)"></i></div>
    <p class="muted small" style="margin-top:8px">就绪度按指标拆项，详见“管理与审计 → 数据就绪度”。</p>`; // 渲染
}

/** 渲染兜底命中分布。 */
function renderGuardrailHits(items) { // 入参：[{reason,count}]
  if (!items.length) { $("#dashboard-guardrail").innerHTML = '<p class="muted">暂无兜底命中；可在“建议与签核”用样例触发。</p>'; return; } // 空态
  $("#dashboard-guardrail").innerHTML = ` <!-- 兜底列表 -->
    <div class="kv">${items.map((item) => `<dt>${esc(item.reason)}</dt><dd>${item.count} 张</dd>`).join("")}</div>
    <p class="muted small" style="margin-top:8px">f44：命中即停，只落 STOPPED 单，交商品与损耗经理人工确认。</p>`; // 渲染
}

/** 渲染单据状态。 */
function renderOrderStats(orders) { // 入参：orders 计数
  $("#dashboard-orders").innerHTML = ` <!-- 单据状态 -->
    <div class="pill-row">
      <span class="badge muted">草稿 ${orders.draft}</span>
      <span class="badge warn">待签核 ${orders.pending_sign}</span>
      <span class="badge ok">已签核 ${orders.signed}</span>
      <span class="badge info">已驳回 ${orders.rejected}</span>
      <span class="badge bad">兜底 ${orders.stopped}</span>
    </div>
    <p class="muted small" style="margin-top:8px">共 ${orders.total} 张；不存在下单接口，签核后才进入后续履约。</p>`; // 渲染
}

/** 渲染缺货率两种分母。 */
function renderStockout(stockout) { // 入参：{snapshot,time_weighted,gap_pp,note}
  const rows = [ // 两行
    ["当日截面", stockout.snapshot.definition || "最新业务日货架可售为 0 的 SKU 数 ÷ 当日在售 SKU 数", stockout.snapshot],
    ["时段加权", stockout.time_weighted.definition || "窗口内货架可售为 0 的 SKU·天 ÷ 窗口内在售 SKU·天", stockout.time_weighted],
  ];
  $("#dashboard-stockout").innerHTML = ` <!-- 两种分母 -->
    <div class="table-wrap"><table class="table">
      <thead><tr><th>口径</th><th>定义</th><th>分子 / 分母</th><th>结果</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td><strong>${esc(r[0])}</strong></td><td style="white-space:normal">${esc(r[1])}</td>
        <td>${r[2].numerator} / ${r[2].denominator}</td><td><strong>${pct(r[2].rate)}</strong></td></tr>`).join("")}</tbody>
    </table></div>
    <p class="muted small" style="margin-top:8px">${esc(stockout.note)}；两口径差 ${stockout.gap_pp}pp，不抹平。</p>`; // 渲染
}

/* ========== 视图二：图谱溯源 ========== */
/** 加载溯源视图（下拉与单据节点）。 */
async function loadTrace() { // 无入参
  const select = $("#path-src"); // 起点下拉
  const preset = [ // 固定可选节点
    ["metric:stockout_rate", "指标 · 缺货率"],
    ["metric:turnover_days", "指标 · 库存周转天数"],
    ["rule:f44", "规则 · f44 兜底五类"],
    ["role:loss_manager", "角色 · 商品与损耗经理（傅景）"],
    ["role:ops_director", "角色 · 运营总监（顾明岚）"],
    ["role:planner", "角色 · 补货计划员（唐映秋）"],
  ];
  let orderNodes = []; // 单据建议节点
  const body = await api("/api/replenishment/orders"); // 拉单据（部分角色无权限）
  if (body.ok) orderNodes = body.data.items.slice(0, 30).map((o) => ["suggestion:" + o.order_id, "建议 · " + o.order_id + "（" + o.store_id + " / " + o.sku_id + "）"]); // 组装节点
  const current = select.value; // 保留当前选择
  select.innerHTML = preset.concat(orderNodes).map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join(""); // 渲染选项
  if (current) select.value = current; // 还原选择
  if (!$("#metric-trace-result").dataset.loaded) { runMetricTrace(); $("#metric-trace-result").dataset.loaded = "1"; } // 首次自动溯源
}

/** 执行路径查询。 */
async function runPath() { // 无入参
  const src = $("#path-src").value; // 起点
  const depth = $("#path-depth").value; // 跳数
  const box = $("#path-result"); // 容器
  box.innerHTML = '<p class="muted">查询中…</p>'; // 加载态
  const body = await api(`/api/graph/path?src=${encodeURIComponent(src)}&depth=${depth}`); // 请求
  if (!body.ok) { box.innerHTML = `<p class="badge bad">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const data = body.data; // 数据
  const kinds = { rule: "rule", role: "role", metric: "metric", evidence: "evidence", signal: "evidence", suggestion: "info" }; // 节点配色
  const title = data.src_node ? data.src_node.name + " · " + data.src_node.kind : src; // 起点标题
  const hops = data.hops || []; // 跳
  box.innerHTML = ` <!-- 路径结果 -->
    <div class="pill-row" style="margin-bottom:6px"><span class="badge info">${esc(title)}</span>
      <span class="badge muted">${hops.length} 跳边 · ${(data.paths || []).length} 条路径</span></div>
    ${hops.length ? hops.map((h) => `<div class="chain-hop">
      <span class="node-chip ${kinds[h.src_kind] || ""}">${esc(h.src_name)}</span>
      <span class="rel">—${esc(h.rel)}→</span>
      <span class="node-chip ${kinds[h.dst_kind] || ""}">${esc(h.dst_name)}</span>
      <span class="muted small">第 ${h.depth} 跳 · ${esc(h.dst_kind)}</span>
    </div>`).join("") : '<p class="muted">没有匹配的边；换个起点或放宽跳数。</p>'}`; // 渲染
}

/** 执行指标口径溯源。 */
async function runMetricTrace() { // 无入参
  const code = $("#metric-trace-code").value; // 指标
  const box = $("#metric-trace-result"); // 容器
  box.innerHTML = '<p class="muted">查询中…</p>'; // 加载态
  const body = await api(`/api/metrics/${encodeURIComponent(code)}/trace`); // 请求
  if (!body.ok) { box.innerHTML = `<p class="badge bad">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const data = body.data; // 数据
  const sig = data.signature || {}; // 签发状态
  const signers = Array.isArray(sig.signed_by) // 兼容数组与字符串两种存量格式
    ? sig.signed_by.map((n) => (typeof n === "string" ? n : n.name)).join("、") // 数组取姓名
    : (sig.signed_by || "—"); // 字符串直接用
  const hops = (data.path && data.path.hops) || []; // 路径
  box.innerHTML = ` <!-- 指标溯源 -->
    <div class="result-box">
      <div class="pill-row">
        <span class="badge ${sig.signed ? "ok" : "bad"}">${sig.signed ? "口径已签发" : "口径未签发"}</span>
        <span class="badge muted">签发人：${esc(signers)}</span>
        <span class="badge muted">节点：metric:${esc(code)}</span>
      </div>
      <p class="muted small">口径未签发则不出数（R10），只展示定义与责任人。</p>
    </div>
    <div class="chain">${hops.length ? hops.slice(0, 12).map((h) => `<div class="chain-hop">
      <span class="node-chip">${esc(h.src_name)}</span>
      <span class="rel">—${esc(h.rel)}→</span>
      <span class="node-chip">${esc(h.dst_name)}</span>
    </div>`).join("") : '<p class="muted">该指标暂无图谱边。</p>'}</div>`; // 渲染
}

/** 执行混合检索。 */
async function runSearch() { // 无入参
  const q = $("#search-q").value.trim(); // 关键词
  if (!q) { toast("请输入检索关键词"); return; } // 空输入
  const box = $("#search-result"); // 容器
  box.innerHTML = '<p class="muted">检索中…</p>'; // 加载态
  const body = await api(`/api/graph/search?q=${encodeURIComponent(q)}`); // 请求
  if (!body.ok) { box.innerHTML = `<p class="badge bad">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const data = body.data; // 数据
  $("#search-mode").textContent = "检索模式：" + data.mode + (data.degraded ? "（已降级到 FTS5）" : ""); // 模式
  box.innerHTML = (data.items || []).map((item) => ` <!-- 检索项 -->
    <div class="search-item">
      <div><strong>${esc(item.name)}</strong> <span class="badge muted">${esc(item.kind)}</span>
        ${item.f_code ? `<span class="badge info">${esc(item.f_code)}</span>` : ""}</div>
      <div class="meta">${esc(item.id)} · 综合得分 ${Number(item.score || 0).toFixed(3)}</div>
    </div>`).join("") || '<p class="muted">没有命中。</p>'; // 渲染
}

/* ========== 视图三：建议与签核 ========== */
/** 加载补货视图。 */
async function loadReplenish() { // 无入参
  $("#suggest-actor").textContent = "当前身份：" + (state.user ? state.user.display_name + " · " + state.user.role_name : ""); // 身份
  const generate = can("suggestion.generate"); // 是否计划员
  $("#suggest-run").disabled = !generate; // 非计划员禁用
  $("#suggest-run").title = generate ? "" : "只有补货计划员可生成建议"; // 提示
  await loadOptions(); // 选项
  await loadOrders(); // 单据
}

/** 加载下拉选项与兜底样例。 */
async function loadOptions() { // 无入参
  const body = await api("/api/replenishment/options"); // 请求
  if (!body.ok) return; // 失败静默（列表区会提示）
  state.options = body.data; // 缓存
  $("#suggest-store").innerHTML = body.data.stores.map((s) => `<option value="${esc(s.store_id)}">${esc(s.store_id)} · ${esc(s.name)}</option>`).join(""); // 门店
  $("#suggest-sku").innerHTML = body.data.skus.map((s) => `<option value="${esc(s.sku_id)}">${esc(s.sku_id)} · ${esc(s.name)}</option>`).join(""); // 商品
  $("#suggest-date").innerHTML = '<option value="">最新业务日</option>' + body.data.dates.slice(0, 12).map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join(""); // 日期
  $("#demo-cases").innerHTML = body.data.demo_cases.map((c, index) => `<button type="button" class="demo-case" data-index="${index}">${esc(c.label)} · ${esc(c.store_id)}/${esc(c.sku_id)}</button>`).join(""); // 样例
  $$("#demo-cases .demo-case").forEach((node) => { // 样例点击
    node.onclick = () => { // 事件
      const c = body.data.demo_cases[Number(node.dataset.index)]; // 当前样例
      $("#suggest-store").value = c.store_id; // 填门店
      $("#suggest-sku").value = c.sku_id; // 填商品
      $("#suggest-date").value = ""; // 最新业务日
      toast("已填入样例：" + c.label + "（" + c.hint + "）"); // 提示
    };
  });
}

/** 生成建议。 */
async function runSuggest() { // 无入参
  const storeId = $("#suggest-store").value; // 门店
  const skuId = $("#suggest-sku").value; // 商品
  const bizDate = $("#suggest-date").value; // 业务日
  const box = $("#suggest-result"); // 容器
  box.innerHTML = '<p class="muted">生成中（先过兜底，再检索，再调本地模型）…</p>'; // 加载态
  const payload = { store_id: storeId, sku_id: skuId }; // 请求体
  if (bizDate) payload.biz_date = bizDate; // 可选日期
  let body; // 响应
  try {
    body = await api("/api/replenishment/suggest", { method: "POST", body: JSON.stringify(payload) }); // 调用
  } catch (err) { box.innerHTML = `<div class="result-box stopped">网络错误：${esc(err.message)}</div>`; return; } // 网络异常
  if (!body.ok && body.error && body.error.code === "GUARDRAIL_STOPPED") { // 兜底停止（200 + ok=false）
    const d = body.data || {}; // 停止详情（统一信封放在顶层 data）
    const hits = (d.hits || []).map((h) => h.title || h.rule_code || h).join("、"); // 命中类别
    box.innerHTML = ` <!-- 兜底结果 -->
      <div class="result-box stopped">
        <div class="pill-row"><span class="badge bad">GUARDRAIL_STOPPED</span>
          <span class="badge muted">${esc(d.order_id || "")}</span><span class="badge muted">f44</span></div>
        <div><strong>${esc(d.reason || body.error.msg)}</strong></div>
        <dl class="kv">
          <dt>命中类别</dt><dd>${esc(hits || "—")}</dd>
          <dt>接手人</dt><dd>${esc((d.handoff && (d.handoff.name || d.handoff.owner_name)) || "—")} · ${esc(roleName(d.handoff && (d.handoff.role || d.handoff.owner_role)))}</dd>
          <dt>人工确认时限</dt><dd>${esc((d.handoff && d.handoff.manual_confirm_at) || "—")}（${esc(((d.hits && d.hits[0] && d.hits[0].manual_confirm_minutes) || (d.handoff && d.handoff.minutes)) || "—")} 分钟）</dd>
        </dl>
        <p class="muted small">兜底命中后不返回建议量，只落 STOPPED 单留痕；这是服务端硬拦截，不是模型判断。</p>
      </div>`;
    loadOrders(); // 刷新列表
    return; // 结束
  }
  if (!body.ok) { // 其他错误
    box.innerHTML = `<div class="result-box stopped"><span class="badge bad">${esc(body.error && body.error.code)}</span> ${esc(body.error && body.error.msg)}</div>`; // 提示
    return; // 结束
  }
  const d = body.data; // 正常建议
  const s = d.suggestion || {}; // 建议
  const r = d.rounding || {}; // 取整
  box.innerHTML = ` <!-- 建议结果 -->
    <div class="result-box">
      <div class="pill-row">
        <span class="badge ok">${esc(d.order_id)}</span>
        <span class="badge muted">状态 ${esc(d.status)}</span>
        <span class="badge ${d.degraded ? "warn" : "info"}">${d.degraded ? "规则降级（degraded）" : "模型建议（" + esc(d.source) + "）"}</span>
        <span class="badge muted">检索 ${esc((d.retrieval && d.retrieval.mode) || "—")}</span>
      </div>
      <div><strong>建议量 ${esc(s.suggestion_qty)} 件</strong> → 整箱取整 ${esc(r.rounded)} → 最终 ${esc(r.final)} 件（箱规 ${esc(r.case_size)}，起订 ${esc(r.moq_units)}，仓容上限 ${esc(r.cap)}）</div>
      <div class="muted small">置信 ${esc(s.confidence)} · 依据 ${esc((s.evidence_refs || []).join("、") || "—")}</div>
      <p>${esc(s.rationale || "")}</p>
      ${(s.risks || []).length ? `<div class="pill-row">${s.risks.map((risk) => `<span class="badge warn">${esc(risk)}</span>`).join("")}</div>` : ""}
      ${r.note ? `<p class="muted small">${esc(r.note)}</p>` : ""}
    </div>`; // 渲染
  loadOrders(); // 刷新列表
}

/** 加载单据列表。 */
async function loadOrders() { // 无入参
  const status = $("#orders-status").value; // 过滤
  const body = await api("/api/replenishment/orders" + (status ? "?status=" + encodeURIComponent(status) : "")); // 请求
  const tbody = $("#orders-table tbody"); // 表体
  if (!body.ok) { tbody.innerHTML = `<tr><td colspan="8" class="muted">${esc(body.error && body.error.msg)}</td></tr>`; return; } // 失败
  const items = body.data.items || []; // 单据
  if (!items.length) { tbody.innerHTML = '<tr><td colspan="8" class="muted">没有单据。</td></tr>'; return; } // 空态
  tbody.innerHTML = items.map((o) => ` <!-- 单据行 -->
    <tr>
      <td class="code">${esc(o.order_id)}</td>
      <td>${esc(o.store_id)}</td>
      <td>${esc(o.sku_id)}</td>
      <td>${esc(o.biz_date)}</td>
      <td>${statusBadge(o.status)}</td>
      <td>${o.suggestion_qty == null ? "—" : esc(o.suggestion_qty)}</td>
      <td>${o.final_qty == null ? "—" : esc(o.final_qty)}</td>
      <td><div class="actions">
        <button class="btn btn-ghost btn-sm" data-act="detail" data-id="${esc(o.order_id)}" type="button">详情/重建</button>
        ${o.status === "DRAFT" && can("order.submit") ? `<button class="btn btn-ghost btn-sm" data-act="submit" data-id="${esc(o.order_id)}" type="button">提交</button>` : ""}
        ${o.status === "DRAFT" && can("order.update") ? `<button class="btn btn-ghost btn-sm" data-act="edit" data-id="${esc(o.order_id)}" data-qty="${esc(o.final_qty ?? 0)}" type="button">改量</button>` : ""}
        ${o.status === "PENDING_SIGN" && can("order.sign") ? `<button class="btn btn-primary btn-sm" data-act="sign" data-id="${esc(o.order_id)}" type="button">签核</button>` : ""}
        ${o.status === "PENDING_SIGN" && can("order.reject") ? `<button class="btn btn-danger btn-sm" data-act="reject" data-id="${esc(o.order_id)}" type="button">驳回</button>` : ""}
      </div></td>
    </tr>`).join(""); // 渲染
  $$("#orders-table [data-act]").forEach((btn) => { btn.onclick = () => handleOrderAction(btn); }); // 绑定操作
}

/** 状态徽章。 */
function statusBadge(status) { // 入参：状态码
  const map = { DRAFT: ["muted", "草稿"], PENDING_SIGN: ["warn", "待签核"], SIGNED: ["ok", "已签核"], REJECTED: ["info", "已驳回"], STOPPED: ["bad", "兜底停止"] }; // 映射
  const [tone, label] = map[status] || ["muted", status]; // 默认
  return `<span class="badge ${tone}">${esc(label)}</span>`; // 徽章
}

/** 处理单据操作（提交 / 改量 / 签核 / 驳回 / 详情）。 */
async function handleOrderAction(btn) { // 入参：按钮
  const act = btn.dataset.act; // 动作
  const id = btn.dataset.id; // 单号
  try {
    if (act === "detail") { await showOrderDetail(id); return; } // 重建视图
    if (act === "submit") { // 提交
      const confirm = await ask("提交待签核", `单据 ${id} 将进入待签核，签核前不可修改。`, { okText: "提交" }); // 确认
      if (!confirm.ok) return; // 取消
      must(await api(`/api/replenishment/orders/${encodeURIComponent(id)}/submit`, { method: "POST" })); // 调用
      toast("已提交待签核", "success"); // 提示
    }
    if (act === "edit") { // 改量
      const res = await ask("修改建议量", "只允许修改草稿状态的最终数量（0–10000）。", { label: "最终数量", value: btn.dataset.qty, okText: "保存" }); // 输入
      if (!res.ok) return; // 取消
      const qty = Number(res.value); // 转数字
      if (!Number.isInteger(qty) || qty < 0 || qty > 10000) { toast("数量必须是 0–10000 的整数", "error"); return; } // 校验
      must(await api(`/api/replenishment/orders/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ final_qty: qty }) })); // 调用
      toast("已更新建议量", "success"); // 提示
    }
    if (act === "sign") { // 签核
      const confirm = await ask("签核确认", `确认签核 ${id}？签核会写入审计，且不能重复签。`, { okText: "签核" }); // 二次确认（无需口令）
      if (!confirm.ok) return; // 取消
      must(await api(`/api/replenishment/orders/${encodeURIComponent(id)}/sign`, { method: "POST" })); // 调用
      toast("签核成功", "success"); // 提示
    }
    if (act === "reject") { // 驳回
      const res = await ask("驳回单据", "驳回必须填写原因（至少 2 个字），会写入审计。", { label: "驳回原因", placeholder: "例如：临时促销期不建议补货", okText: "驳回" }); // 输入
      if (!res.ok) return; // 取消
      must(await api(`/api/replenishment/orders/${encodeURIComponent(id)}/reject`, { method: "POST", body: JSON.stringify({ reason: res.value }) })); // 调用
      toast("已驳回", "success"); // 提示
    }
  } catch (err) { // 统一错误展示
    toast(err.code ? err.code + "：" + err.message : err.message, "error"); // 错误提示
  }
  loadOrders(); // 刷新
}

/** 展示单据重建视图。 */
async function showOrderDetail(id) { // 入参：单号
  const panel = $("#order-detail-panel"); // 面板
  const box = $("#order-detail"); // 内容
  panel.hidden = false; // 显示
  box.innerHTML = '<p class="muted">加载处置现场…</p>'; // 加载态
  const body = await api(`/api/replenishment/orders/${encodeURIComponent(id)}/rebuild`); // 请求
  if (!body.ok) { box.innerHTML = `<p class="badge bad">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const d = body.data; // 数据
  const o = d.order; // 单据
  const s = d.suggestion; // 建议
  const g = d.guardrail; // 兜底
  const sv = d.snapshot || {}; // 快照
  const v = d.versions || {}; // 版本
  box.innerHTML = ` <!-- 重建视图 -->
    <div class="result-box">
      <div class="pill-row"><span class="badge info">${esc(o.order_id)}</span>${statusBadge(o.status)}
        <span class="badge muted">${esc(o.store_id)} / ${esc(o.sku_id)} · ${esc(o.biz_date)}</span>
        ${o.degraded ? '<span class="badge warn">degraded</span>' : ""}</div>
      <dl class="kv">
        <dt>建议量 / 最终量</dt><dd>${o.suggestion_qty ?? "—"} / ${o.final_qty ?? "—"}</dd>
        <dt>创建 / 提交</dt><dd>${fmtTime(o.created_at)} / ${fmtTime(o.submitted_at)}</dd>
        <dt>签核 / 驳回</dt><dd>${fmtTime(o.signed_at)} · ${esc(o.signed_by || "—")}</dd>
        <dt>原因</dt><dd>${esc(o.reason || "—")}${o.reject_reason ? " / 驳回：" + esc(o.reject_reason) : ""}</dd>
      </dl>
    </div>
    ${g ? `<div class="result-box stopped"><strong>兜底详情（f44）</strong>
      <div class="muted small">${esc(JSON.stringify(g))}</div></div>` : ""}
    ${s ? `<div class="result-box"><strong>建议依据</strong>
      <div class="muted small">${esc(s.rationale || "")}</div>
      <div class="pill-row">${(s.evidence_refs || []).map((ref) => `<span class="badge info">${esc(ref)}</span>`).join("")}
        <span class="badge muted">置信 ${esc(s.confidence)}</span></div></div>` : ""}
    <div class="grid grid-2" style="margin:0">
      <div class="result-box"><strong>当次快照</strong>
        <dl class="kv"><dt>账面库存</dt><dd>${esc(sv.book_stock ?? "—")}</dd>
          <dt>货架可售</dt><dd>${esc(sv.shelf_available ?? "—")}</dd>
          <dt>后仓</dt><dd>${esc(sv.backroom_stock ?? "—")}</dd>
          <dt>在途</dt><dd>${esc(sv.in_transit ?? "—")}</dd></dl></div>
      <div class="result-box"><strong>四类版本</strong>
        <dl class="kv"><dt>促销版本</dt><dd>${esc(v.promo_version || "—")}</dd>
          <dt>箱规版本</dt><dd>${esc(v.case_spec_version || "—")}</dd>
          <dt>配送日历</dt><dd>${esc(v.delivery_calendar_version || "—")}</dd>
          <dt>规则版本</dt><dd>${esc(v.rule_version || "—")}</dd></dl></div>
    </div>
    <div class="result-box"><strong>时间线</strong><div class="timeline">
      ${(d.timeline || []).map((t) => `<div class="timeline-item"><div class="at">${fmtTime(t.at)} · ${esc(t.actor || "")}</div>
        <div>${esc(t.event)}${t.reason ? " · " + esc(t.reason) : ""}</div></div>`).join("")}</div></div>
    <div class="result-box"><strong>审计留痕（${(d.audit_trail || []).length} 条）</strong>
      <div class="muted small">${(d.audit_trail || []).map((a) => `${fmtTime(a.created_at)} · ${esc(a.actor_name || "—")} · ${esc(a.action)} · ${esc(a.decision)} · ${esc(a.error_code || "")}`).join("<br>") || "暂无"}</div></div>
    <div class="result-box"><strong>事件流（${(d.events || []).length} 条，含空时间戳）</strong>
      <div class="muted small">${(d.events || []).slice(0, 15).map((e) => `${fmtTime(e.occurred_at)} · ${esc(e.event_type)} · ${esc(e.channel || "")}`).join("<br>") || "暂无"}</div></div>`; // 渲染
  panel.scrollIntoView({ behavior: "smooth" }); // 滚动到面板
}

/* ========== 视图四：管理与审计 ========== */
/** 加载管理视图（按权限分块）。 */
async function loadAdmin() { // 无入参
  if (can("users.manage")) { await loadUsers(); } else { $("#users-table tbody").innerHTML = '<tr><td colspan="6" class="muted">当前角色无账号管理权限。</td></tr>'; $("#create-user-form").style.display = "none"; } // 账号
  if (can("audit.view")) { await loadAudit(); await loadRetention(); } else { $("#audit-table tbody").innerHTML = '<tr><td colspan="8" class="muted">当前角色无审计查看权限。</td></tr>'; } // 审计
  await loadReadinessDetail(); // 就绪度（所有角色可见）
  if (can("agents.view")) { await loadAgents(); } // Agent 注册表
  if (can("guardrail.view")) { await loadGuardrailRules(); } // 兜底规则
}

/** 加载账号列表。 */
async function loadUsers() { // 无入参
  const body = await api("/api/admin/users"); // 请求
  const tbody = $("#users-table tbody"); // 表体
  if (!body.ok) { tbody.innerHTML = `<tr><td colspan="6" class="muted">${esc(body.error && body.error.msg)}</td></tr>`; return; } // 失败
  tbody.innerHTML = body.data.items.map((u) => ` <!-- 账号行 -->
    <tr>
      <td class="code">${esc(u.username)}</td><td>${esc(u.display_name)}</td><td>${esc(u.role_name)}</td>
      <td>${esc(u.scope_type)}${u.scope_value ? " · " + esc(u.scope_value) : ""}</td>
      <td>${u.active ? '<span class="badge ok">启用</span>' : '<span class="badge muted">停用</span>'}</td>
      <td><button class="btn btn-ghost btn-sm" data-user="${esc(u.user_id)}" data-active="${u.active}" type="button">${u.active ? "停用" : "启用"}</button></td>
    </tr>`).join(""); // 渲染
  $$("#users-table [data-user]").forEach((btn) => { // 启停按钮
    btn.onclick = async () => { // 点击
      const active = btn.dataset.active !== "1"; // 取反
      try {
        must(await api(`/api/admin/users/${encodeURIComponent(btn.dataset.user)}`, { method: "PATCH", body: JSON.stringify({ active }) })); // 调用
        toast(active ? "已启用" : "已停用", "success"); // 提示
        loadUsers(); // 刷新
      } catch (err) { toast(err.message, "error"); } // 错误
    };
  });
}

/** 新建账号。 */
async function createUser(event) { // 入参：提交事件
  event.preventDefault(); // 阻止默认提交
  const form = event.currentTarget; // 表单
  const fd = new FormData(form); // 收集
  const payload = { // 请求体
    username: String(fd.get("username") || "").trim(), // 账号
    display_name: String(fd.get("display_name") || "").trim(), // 姓名
    email: String(fd.get("email") || "").trim(), // 邮箱
    role: String(fd.get("role") || ""), // 角色
    scope_type: String(fd.get("scope_type") || "store"), // 数据域类型
    scope_value: String(fd.get("scope_value") || "").trim() || null, // 数据域值
    password: String(fd.get("password") || ""), // 初始口令
  };
  const errBox = $("#create-user-error"); // 错误区
  errBox.hidden = true; // 先隐藏
  try {
    must(await api("/api/admin/users", { method: "POST", body: JSON.stringify(payload) })); // 调用
    toast("账号已创建", "success"); // 提示
    form.reset(); // 清空
    loadUsers(); // 刷新
  } catch (err) {
    errBox.textContent = (err.code ? err.code + "：" : "") + err.message; // 展示错误
    errBox.hidden = false; // 显示
  }
}

/** 加载审计日志。 */
async function loadAudit() { // 无入参
  const decision = $("#audit-decision").value; // 过滤
  const body = await api("/api/audit/logs?limit=200" + (decision ? "&decision=" + encodeURIComponent(decision) : "")); // 请求
  const tbody = $("#audit-table tbody"); // 表体
  if (!body.ok) { tbody.innerHTML = `<tr><td colspan="8" class="muted">${esc(body.error && body.error.msg)}</td></tr>`; return; } // 失败
  const items = body.data.items || []; // 日志
  if (!items.length) { tbody.innerHTML = '<tr><td colspan="8" class="muted">暂无日志。</td></tr>'; return; } // 空态
  tbody.innerHTML = items.map((a) => ` <!-- 审计行 -->
    <tr>
      <td class="code">${fmtTime(a.created_at)}</td>
      <td>${esc(a.actor_name || a.actor || "—")}</td>
      <td>${esc(a.role || "—")}</td>
      <td class="code">${esc(a.action)}</td>
      <td class="code">${esc(a.resource || "")}${a.resource_id ? "/" + esc(a.resource_id) : ""}</td>
      <td>${esc(a.decision || "")}</td>
      <td class="code">${esc(a.error_code || "")}</td>
      <td class="muted small" style="white-space:normal;max-width:320px">${esc(String(a.detail || "").slice(0, 160))}</td>
    </tr>`).join(""); // 渲染
}

/** 加载审计留存状态。 */
async function loadRetention() { // 无入参
  const body = await api("/api/audit/retention"); // 请求
  if (!body.ok) return; // 静默
  const d = body.data; // 数据
  $("#audit-retention").textContent = `留存 ${d.retention_days} 天 · 最早记录 ${fmtTime(d.earliest)} · 剩余 ${d.remaining_days} 天 · 到期归档需 ${d.archive_requires || "loss_manager"} 确认，不自动续期`; // 文案
}

/** 导出审计 CSV（带 token，走 blob 下载）。 */
async function exportAudit() { // 无入参
  const res = await fetch("/api/audit/export", { headers: { Authorization: "Bearer " + state.token } }); // 直接请求
  if (!res.ok) { toast("导出失败：" + res.status, "error"); return; } // 失败
  const blob = await res.blob(); // 二进制
  const url = URL.createObjectURL(blob); // 临时地址
  const a = document.createElement("a"); // 下载链接
  a.href = url; // 地址
  a.download = "audit-export.csv"; // 文件名
  a.click(); // 触发下载
  URL.revokeObjectURL(url); // 释放
}

/** 加载就绪度明细与导入批次。 */
async function loadReadinessDetail() { // 无入参
  const body = await api("/api/admin/import"); // 请求
  if (!body.ok) { $("#readiness-detail").innerHTML = `<p class="muted">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const d = body.data; // 数据
  const items = (d.readiness && d.readiness.items) || []; // 明细
  const tone = { ready: "ok", partial: "warn", not_ready: "bad" }; // 状态配色
  $("#readiness-detail").innerHTML = items.map((item) => ` <!-- 就绪项 -->
    <div class="blocked-item" style="border-color:var(--line);background:#fcfdff">
      <div class="title">${esc(item.label)} <span class="badge ${tone[item.status] || "muted"}">${esc(item.status)}</span>
        <span class="muted small">完整度 ${pct(item.completeness)}</span></div>
      <div class="reason">${esc(item.impact)}</div>
      ${(item.missing_fields || []).length ? `<div class="next">缺：${esc(item.missing_fields.join("；"))}</div>` : ""}
    </div>`).join(""); // 渲染
  $("#imports-detail").innerHTML = ` <!-- 导入批次 -->
    <div class="table-wrap"><table class="table"><thead><tr><th>批次</th><th>来源文件</th><th>行数</th><th>状态</th><th>时间</th></tr></thead>
    <tbody>${(d.imports || []).map((i) => `<tr><td class="code">#${esc(i.import_id)}</td><td>${esc(i.source_file || "CSV")}</td>
      <td>${esc(i.rows ?? i.row_count ?? "—")}</td><td>${esc(i.status || "—")}</td><td>${fmtTime(i.imported_at || i.created_at)}</td></tr>`).join("")}</tbody></table></div>
    <p class="muted small" style="margin-top:8px">${esc(d.note)}</p>`; // 渲染
}

/** 加载 Agent 注册表。 */
async function loadAgents() { // 无入参
  const body = await api("/api/admin/agents"); // 请求
  if (!body.ok) { $("#agents-detail").innerHTML = `<p class="muted">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const d = body.data; // 数据
  $("#agents-detail").innerHTML = ` <!-- Agent 列表 -->
    ${d.agents.map((a) => `<div class="result-box" style="margin-bottom:8px">
      <strong>${esc(a.label)}</strong> <span class="badge muted">${esc(a.name)}</span>
      <div class="muted small">${esc(a.purpose)}</div>
      <div class="pill-row">${(a.tools || []).map((t) => `<span class="badge info">${esc(t)}</span>`).join("")}</div>
    </div>`).join("")}
    <p class="muted small">Harness 落点：${esc(d.harness.registry)}；留痕 ${esc(d.harness.tracing)}；未 import 任何 harness 包（${esc(d.harness.sdk_imported)}）。</p>`; // 渲染
}

/** 加载兜底规则。 */
async function loadGuardrailRules() { // 无入参
  const body = await api("/api/admin/guardrail/rules"); // 请求
  if (!body.ok) { $("#guardrail-detail").innerHTML = `<p class="muted">${esc(body.error && body.error.msg)}</p>`; return; } // 失败
  const d = body.data; // 数据
  $("#guardrail-detail").innerHTML = ` <!-- 规则列表 -->
    ${d.rules.map((r) => `<div class="result-box" style="margin-bottom:8px">
      <strong>${esc(r.title)}</strong> <span class="badge bad">${esc(r.rule_code)}</span>
      <div class="muted small">${esc(r.condition)}</div>
      <div class="muted small">动作：${esc(r.action)}</div>
      <div class="pill-row"><span class="badge muted">接手：${esc(r.owner_name || "—")}</span>
        <span class="badge muted">${esc(d.manual_confirm_minutes || "")} 分钟人工确认</span></div>
    </div>`).join("")}
    <p class="muted small">命中即停（${esc(d.stop_actions_on_hit)}）：不返回建议量，只落 STOPPED 单并留痕；不做模型判定。</p>`; // 渲染
}

/* ========== 事件绑定与启动 ========== */
/** 绑定所有静态事件。 */
function bindEvents() { // 无入参
  $("#login-form").onsubmit = async (event) => { // 登录
    event.preventDefault(); // 阻止默认
    const errBox = $("#login-error"); // 错误区
    errBox.hidden = true; // 先隐藏
    const payload = { username: $("#login-username").value.trim(), password: $("#login-password").value }; // 请求体
    const body = await api("/api/auth/login", { method: "POST", body: JSON.stringify(payload) }); // 调用
    if (!body.ok) { errBox.textContent = (body.error && body.error.msg) || "登录失败"; errBox.hidden = false; return; } // 失败
    enterApp(body.data); // 进入应用
  };
  $$("#demo-accounts .chip").forEach((chip) => { // 演示账号填充
    chip.onclick = () => { $("#login-username").value = chip.dataset.user; $("#login-password").value = "Demo@2026"; }; // 填入
  });
  $("#logout-btn").onclick = async () => { // 退出
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* 忽略网络错误 */ }
    clearSession(); // 清会话
  };
  $$(".tab").forEach((tab) => { tab.onclick = () => switchView(tab.dataset.view); }); // 标签切换
  $("#dashboard-refresh").onclick = loadDashboard; // 看板刷新
  $("#path-run").onclick = runPath; // 路径查询
  $("#metric-trace-run").onclick = runMetricTrace; // 指标溯源
  $("#search-run").onclick = runSearch; // 检索
  $("#suggest-run").onclick = runSuggest; // 生成建议
  $("#orders-refresh").onclick = loadOrders; // 刷新单据
  $("#orders-status").onchange = loadOrders; // 状态过滤
  $("#order-detail-close").onclick = () => { $("#order-detail-panel").hidden = true; }; // 关闭详情
  $("#create-user-form").onsubmit = createUser; // 新建账号
  $("#audit-refresh").onclick = loadAudit; // 刷新审计
  $("#audit-decision").onchange = loadAudit; // 审计过滤
  $("#audit-export").onclick = exportAudit; // 导出
}

/** 启动：有 token 则校验会话，否则停在登录页。 */
async function boot() { // 无入参
  bindEvents(); // 绑定
  if (!state.token) return; // 未登录
  const body = await api("/api/auth/me"); // 校验
  if (!body.ok) { clearSession(); return; } // 失效
  state.user = body.data.user; // 用户
  state.scope = body.data.scope; // 数据域
  state.actions = body.data.actions || []; // 动作
  $("#login-view").hidden = true; // 隐藏登录
  $("#shell").hidden = false; // 显示主壳
  $("#userbox").hidden = false; // 用户区
  $("#user-name").textContent = state.user.display_name + "（" + state.user.username + "）"; // 姓名
  $("#user-role").textContent = state.user.role_name; // 角色
  switchView("dashboard"); // 进看板
}

boot(); // 执行启动
