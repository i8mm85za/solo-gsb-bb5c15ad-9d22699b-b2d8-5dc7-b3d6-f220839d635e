/* 法规条款变更审阅 —— 原生 JS 单页前端 */
"use strict";

const TYPE_LABEL = {
  unchanged: "未变更", rewrite: "改写", added: "新增", deleted: "删除",
  split: "拆分", merged: "合并", multi_rewrite: "多段改写",
};
const DECISION_LABEL = {
  pending: "待审阅", confirmed: "确认", disputed: "有异议", ignored: "忽略",
};

const state = {
  view: "list",           // list | upload | workspace
  doc: null,              // 当前文档详情
  filterType: "",         // 变更类型筛选
  filterDecision: "",     // 决定筛选
  search: "",
};

const $app = document.getElementById("app");

// ---------------- 工具 ----------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
  return data;
}

function fmtTime(s) {
  return s ? s.replace("T", " ").slice(0, 16) : "";
}

// ---------------- 视图：文档列表 ----------------

async function renderList() {
  state.view = "list";
  document.getElementById("nav-list").classList.add("active");
  document.getElementById("nav-upload").classList.remove("active");
  $app.innerHTML = "";

  let docs = [];
  try { docs = await api("/api/documents"); } catch (e) { toast(e.message, true); }

  $app.append(
    h("div", { class: "panel" },
      h("h2", {}, "审阅文档"),
      docs.length === 0
        ? h("div", { class: "empty" },
            h("p", {}, "还没有审阅任务"),
            h("button", { class: "btn primary", onclick: renderUpload }, "＋ 上传旧版 / 新版文本，开始审阅"))
        : docs.map(d => h("div", {
            class: "doc-item",
            onclick: () => openDoc(d.id),
          },
            h("div", {},
              h("h3", {}, d.title),
              h("div", { class: "doc-meta" },
                h("span", {}, `旧版 ${d.n_old} 段`),
                h("span", {}, `新版 ${d.n_new} 段`),
                h("span", {}, `对齐 ${d.n_align} 条`),
                h("span", {}, `已审阅 ${d.n_reviewed}/${d.n_align}`),
                h("span", {}, `创建于 ${fmtTime(d.created_at)}`))),
            h("button", {
              class: "btn danger sm",
              onclick: async (e) => {
                e.stopPropagation();
                if (!confirm(`确定删除「${d.title}」及其全部审阅记录？`)) return;
                try { await api(`/api/documents/${d.id}`, { method: "DELETE" }); toast("已删除"); renderList(); }
                catch (err) { toast(err.message, true); }
              },
            }, "删除")))),
  );
}

// ---------------- 视图：上传 ----------------

function renderUpload() {
  state.view = "upload";
  document.getElementById("nav-list").classList.remove("active");
  document.getElementById("nav-upload").classList.add("active");
  $app.innerHTML = "";

  const titleEl = h("input", { type: "text", placeholder: "例如：员工手册 2024 → 2025" });
  const oldEl = h("textarea", { placeholder: "粘贴旧版法规/制度全文（纯文本，段落之间建议空行分隔）……" });
  const newEl = h("textarea", { placeholder: "粘贴新版法规/制度全文（纯文本，段落之间建议空行分隔）……" });

  $app.append(
    h("div", { class: "panel" },
      h("h2", {}, "新建审阅任务"),
      h("label", { class: "field" }, "文档名称"),
      titleEl,
      h("div", { class: "grid-2", style: "margin-top:16px" },
        h("div", {},
          h("label", { class: "field" }, "旧版文本"),
          oldEl,
          h("div", { class: "hint" }, "按空行分块；“第一条 / 1. / （一）”等编号会自动识别为段落边界")),
        h("div", {},
          h("label", { class: "field" }, "新版文本"),
          newEl,
          h("div", { class: "hint" }, "系统按段落顺序 + 字符串相似度生成对齐候选，并给出变更类型与置信度"))),
      h("div", { class: "actions" },
        h("button", {
          class: "btn primary",
          onclick: async () => {
            const title = titleEl.value.trim();
            if (!title) return toast("请填写文档名称", true);
            if (!oldEl.value.trim() && !newEl.value.trim()) return toast("旧版与新版不能同时为空", true);
            try {
              const r = await api("/api/documents", {
                method: "POST",
                body: JSON.stringify({ title, old_text: oldEl.value, new_text: newEl.value }),
              });
              toast(`已生成 ${r.aligned} 条对齐候选`);
              openDoc(r.id);
            } catch (e) { toast(e.message, true); }
          },
        }, "生成对齐候选"),
        h("button", {
          class: "btn",
          onclick: () => {
            titleEl.value = "示例：考勤制度 v1 → v2";
            oldEl.value = DEMO_OLD;
            newEl.value = DEMO_NEW;
          },
        }, "填入示例文本"))))
}

// ---------------- 视图：审阅工作区 ----------------

async function openDoc(id) {
  try {
    state.doc = await api(`/api/documents/${id}`);
  } catch (e) { return toast(e.message, true); }
  state.filterType = "";
  state.filterDecision = "";
  state.search = "";
  state.view = "workspace";
  document.getElementById("nav-list").classList.remove("active");
  document.getElementById("nav-upload").classList.remove("active");
  renderWorkspace();
}

function typeCounts() {
  const c = {};
  for (const a of state.doc.alignments) c[a.change_type] = (c[a.change_type] || 0) + 1;
  return c;
}

function renderWorkspace() {
  const d = state.doc;
  $app.innerHTML = "";

  const counts = typeCounts();
  const reviewed = d.alignments.filter(a => a.decision !== "pending").length;

  // 统计 chips（点击按类型筛选）；圆点颜色由 CSS 类控制
  const chips = Object.entries(TYPE_LABEL).map(([key, label]) => {
    const n = counts[key] || 0;
    return h("span", {
      class: `chip clickable ${key} ${state.filterType === key ? "" : "off"}`,
      onclick: () => { state.filterType = state.filterType === key ? "" : key; renderWorkspace(); },
    }, h("span", { class: "dot" }), `${label} ${n}`);
  });

  const head = h("div", { class: "panel" },
    h("div", { class: "breadcrumb", onclick: renderList }, "← 返回文档列表"),
    h("div", { class: "workspace-head" },
      h("div", {},
        h("h2", {}, d.title),
        h("div", { class: "doc-meta" },
          h("span", {}, `旧版 ${d.paragraphs.old.length} 段`),
          h("span", {}, `新版 ${d.paragraphs.new.length} 段`),
          h("span", {}, `对齐 ${d.alignments.length} 条`),
          h("span", {}, `已审阅 ${reviewed}/${d.alignments.length}`),
          d.aligned_at ? h("span", {}, `对齐时间 ${fmtTime(d.aligned_at)}`) : null)),
      h("div", { class: "actions", style: "margin:0" },
        h("button", { class: "btn sm", onclick: () => openNewMapping() }, "＋ 新建映射"),
        h("button", { class: "btn sm", onclick: () => realign(d.id) }, "↻ 重新自动对齐"),
        exportMenu(d.id))),
    h("div", { class: "stats" }, chips),
    h("div", { class: "toolbar" },
      h("input", {
        type: "text", placeholder: "搜索原文 / 备注关键字…", value: state.search,
        oninput: (e) => { state.search = e.target.value; applyListFilter(); },
      }),
      h("select", {
        onchange: (e) => { state.filterDecision = e.target.value; renderWorkspace(); },
      },
        h("option", { value: "" }, "全部审阅状态"),
        ...Object.entries(DECISION_LABEL).map(([v, l]) =>
          h("option", { value: v, selected: state.filterDecision === v }, l))),
      h("span", { class: "foot-label", id: "visible-count" })));

  $app.append(head);

  const list = h("div", { id: "alignment-list" });
  d.alignments.forEach(a => list.appendChild(renderCard(a)));
  $app.append(list);
  applyListFilter();
}

function exportMenu(docId) {
  const root = h("div", { style: "position:relative" });
  const menu = h("div", {
    style: "position:absolute;right:0;top:110%;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:var(--shadow);z-index:30;display:none;min-width:200px",
  },
    ...["csv", "md", "json"].map(fmt => h("button", {
      class: "btn sm",
      style: "width:100%;border:none;border-radius:0;justify-content:flex-start;text-align:left",
      onclick: () => doExport(docId, fmt),
    }, { csv: "⬇ 导出 CSV（Excel）", md: "⬇ 导出 Markdown 报告", json: "⬇ 导出 JSON" }[fmt])));
  const btn = h("button", { class: "btn primary sm" }, "⬇ 导出");
  btn.onclick = (e) => { e.stopPropagation(); menu.style.display = menu.style.display === "none" ? "block" : "none"; };
  // 点击页面其他位置时关闭菜单（仅在菜单打开期间监听一次）
  document.addEventListener("click", function closeMenu(ev) {
    if (!root.contains(ev.target)) {
      menu.style.display = "none";
      document.removeEventListener("click", closeMenu);
    }
  });
  root.append(btn, menu);
  return root;
}

function doExport(docId, fmt) {
  const p = new URLSearchParams();
  if (state.filterType) p.set("change_type", state.filterType);
  if (state.filterDecision) p.set("decision", state.filterDecision);
  window.location.href = `/api/documents/${docId}/export?format=${fmt}&${p.toString()}`;
}

async function realign(id) {
  if (!confirm("将清除全部对齐结果（含人工调整与审阅决定）并重新计算，确定继续？")) return;
  try {
    await api(`/api/documents/${id}/realign`, { method: "POST" });
    toast("已重新对齐");
    openDoc(id);
  } catch (e) { toast(e.message, true); }
}

function applyListFilter() {
  const kw = state.search.trim().toLowerCase();
  let n = 0;
  for (const card of document.querySelectorAll(".align-card")) {
    const aid = Number(card.dataset.aid);
    const a = state.doc.alignments.find(x => x.id === aid);
    let show = true;
    if (state.filterType && a.change_type !== state.filterType) show = false;
    if (state.filterDecision && a.decision !== state.filterDecision) show = false;
    if (kw && !(a.old_group_text + a.new_group_text + a.explanation + a.decision_note)
        .toLowerCase().includes(kw)) show = false;
    card.style.display = show ? "" : "none";
    if (show) n++;
  }
  const el = document.getElementById("visible-count");
  if (el) el.textContent = `显示 ${n} / ${state.doc.alignments.length} 条` +
    (state.filterType || state.filterDecision || kw ? "（已筛选）" : "");
}

// ---------------- 对齐记录卡片 ----------------

function renderCard(a) {
  const d = state.doc;
  const oldLabel = a.old_indices.map(i => `第 ${i + 1} 段`).join("、");
  const newLabel = a.new_indices.map(i => `第 ${i + 1} 段`).join("、");

  const oldBox = a.old_indices.length
    ? h("div", { class: "text-box old" },
        h("h4", {}, `旧版 · ${oldLabel}`),
        h("div", {}, ...boxDiff(a, "old")))
    : h("div", { class: "text-box empty-box" }, h("h4", {}, "旧版 · 无对应段落"), "（新增内容）");

  const newBox = a.new_indices.length
    ? h("div", { class: "text-box new" },
        h("h4", {}, `新版 · ${newLabel}`),
        boxDiff(a, "new"))
    : h("div", { class: "text-box empty-box" }, h("h4", {}, "新版 · 无对应段落"), "（删除内容）");

  const card = h("div", {
    class: `align-card ${a.change_type} ${a.decision !== "pending" ? "reviewed" : ""}`,
    "data-aid": a.id,
  },
    h("div", { class: "card-head" },
      h("span", { class: `badge ${a.change_type}` }, TYPE_LABEL[a.change_type]),
      h("span", { class: "conf" },
        `置信度 ${(a.confidence * 100).toFixed(0)}%`,
        h("span", { class: "conf-bar" },
          h("span", { class: "conf-fill", style: `width:${a.confidence * 100}%;background:${confColor(a.confidence)}` })),
        a.similarity != null ? ` · 相似度 ${(a.similarity * 100).toFixed(0)}%` : ""),
      a.is_manual ? h("span", { class: "manual-tag" }, "人工调整") : null,
      h("span", { class: "spacer" }),
      h("span", { class: `decision-pill ${a.decision}` }, DECISION_LABEL[a.decision])),
    h("div", { class: "card-body" },
      h("div", { class: "explain" }, h("b", {}, "判定依据："), a.explanation),
      h("div", { class: "side-by-side" }, oldBox, newBox)),
    h("div", { class: "card-foot" },
      h("span", { class: "foot-label" }, "审阅决定"),
      decisionSelect(a),
      h("input", {
        type: "text", placeholder: "审阅备注（判定理由 / 疑问 …）", value: a.decision_note,
        "data-role": "note",
        onchange: (e) => saveDecision(a.id, { decision_note: e.target.value }, false),
      }),
      h("button", { class: "btn sm", onclick: () => openRemap(a.id) }, "✎ 调整映射"),
      h("button", { class: "btn sm danger", onclick: () => removeAlignment(a.id) }, "删除映射")));

  return card;
}

// 对同一组 diff ops，只渲染属于该侧的片段
function boxDiff(a, side) {
  const out = [];
  for (const [tag, i1, i2, j1, j2] of a.diff_ops) {
    if (side === "old" && (tag === "equal" || tag === "delete" || tag === "replace"))
      out.push(h("span", { class: tag === "equal" ? "" : "del" }, a.old_group_text.slice(i1, i2)));
    if (side === "new" && (tag === "equal" || tag === "insert" || tag === "replace"))
      out.push(h("span", { class: tag === "equal" ? "" : "ins" }, a.new_group_text.slice(j1, j2)));
  }
  if (!a.diff_ops.length) out.push(document.createTextNode(side === "old" ? a.old_group_text : a.new_group_text));
  return out;
}

function confColor(c) {
  if (c >= 0.85) return "#15803d";
  if (c >= 0.65) return "#b45309";
  return "#b91c1c";
}

function decisionSelect(a) {
  const sel = h("select", {
    onchange: (e) => saveDecision(a.id, { decision: e.target.value }, true),
  }, ...Object.entries(DECISION_LABEL).map(([v, l]) =>
    h("option", { value: v, selected: a.decision === v }, l)));
  return sel;
}

async function saveDecision(id, patch, reload) {
  try {
    const updated = await api(`/api/documents/${state.doc.id}/alignments/${id}`, {
      method: "PATCH", body: JSON.stringify(patch),
    });
    const a = state.doc.alignments.find(x => x.id === id);
    Object.assign(a, updated);
    if (reload) renderWorkspace(); else {
      // 不重绘以保留输入焦点，仅更新状态徽标/卡片描边
      const card = document.querySelector(`.align-card[data-aid="${id}"]`);
      if (card) card.classList.toggle("reviewed", a.decision !== "pending");
      const pill = card && card.querySelector(".decision-pill");
      if (pill) { pill.className = `decision-pill ${a.decision}`; pill.textContent = DECISION_LABEL[a.decision]; }
    }
  } catch (e) { toast(e.message, true); }
}

async function removeAlignment(id) {
  if (!confirm("删除该条映射？段落会变为未映射状态，可通过「新建映射」重新关联。")) return;
  try {
    await api(`/api/documents/${state.doc.id}/alignments/${id}`, { method: "DELETE" });
    state.doc.alignments = state.doc.alignments.filter(a => a.id !== id);
    renderWorkspace();
    toast("已删除");
  } catch (e) { toast(e.message, true); }
}

// ---------------- 调整映射模态框 ----------------

function openRemap(alignId) {
  const a = state.doc.alignments.find(x => x.id === alignId);
  openMappingModal({
    mode: "remap",
    oldSel: new Set(a.old_indices),
    newSel: new Set(a.new_indices),
    alignId,
  });
}

function openNewMapping() {
  openMappingModal({ mode: "create", oldSel: new Set(), newSel: new Set() });
}

function openMappingModal(opts) {
  const d = state.doc;
  // 已被其他映射占用的段落（重映射时排除自身占用）
  const usedOld = new Set(), usedNew = new Set();
  for (const x of d.alignments) {
    if (opts.mode === "remap" && x.id === opts.alignId) continue;
    x.old_indices.forEach(i => usedOld.add(i));
    x.new_indices.forEach(i => usedNew.add(i));
  }

  const oldSel = new Set(opts.oldSel);
  const newSel = new Set(opts.newSel);

  const typeSel = h("select", {},
    h("option", { value: "" }, "按内容自动推断"),
    ...Object.entries(TYPE_LABEL).map(([v, l]) => h("option", { value: v }, l)));

  const refreshCounts = () => {
    const oc = modal.querySelector('[data-role="old-count"]');
    const nc = modal.querySelector('[data-role="new-count"]');
    if (oc) oc.textContent = `旧版段落（已选 ${oldSel.size} 项）`;
    if (nc) nc.textContent = `新版段落（已选 ${newSel.size} 项）`;
  };

  const oldList = h("div", { class: "pick-list" },
    ...d.paragraphs.old.map(p => h("div", {
      class: `pick-item ${oldSel.has(p.idx) ? "picked" : ""} ${usedOld.has(p.idx) ? "disabled" : ""}`,
      onclick: function () {
        if (usedOld.has(p.idx)) return;
        oldSel.has(p.idx) ? oldSel.delete(p.idx) : oldSel.add(p.idx);
        this.classList.toggle("picked");
        refreshCounts();
      },
    }, h("span", { class: "pidx" }, `${p.idx + 1}`), h("span", {}, p.content.slice(0, 120) + (p.content.length > 120 ? "…" : "")))));

  const newList = h("div", { class: "pick-list" },
    ...d.paragraphs.new.map(p => h("div", {
      class: `pick-item ${newSel.has(p.idx) ? "picked" : ""} ${usedNew.has(p.idx) ? "disabled" : ""}`,
      onclick: function () {
        if (usedNew.has(p.idx)) return;
        newSel.has(p.idx) ? newSel.delete(p.idx) : newSel.add(p.idx);
        this.classList.toggle("picked");
        refreshCounts();
      },
    }, h("span", { class: "pidx" }, `${p.idx + 1}`), h("span", {}, p.content.slice(0, 120) + (p.content.length > 120 ? "…" : "")))));

  const root = document.getElementById("modal-root");
  const close = () => { root.innerHTML = ""; };

  const modal = h("div", { class: "modal-mask", onclick: (e) => { if (e.target === e.currentTarget) close(); } },
    h("div", { class: "modal" },
      h("div", { class: "modal-head" },
        h("h3", {}, opts.mode === "remap" ? "手动调整段落映射" : "新建映射"),
        h("button", { class: "close-x", onclick: close }, "✕")),
      h("div", { class: "modal-body" },
        h("p", { class: "hint", style: "margin-top:0" },
          "点击勾选段落建立对应（可多选，多旧→一新为合并，一旧→多新为拆分）。灰色条目已被其他映射占用。"),
        h("label", { class: "field" }, "变更类型（可选）"),
        typeSel,
        h("div", { class: "grid-2", style: "margin-top:14px" },
          h("div", {}, h("label", { class: "field", "data-role": "old-count" }, "旧版段落"), oldList),
          h("div", {}, h("label", { class: "field", "data-role": "new-count" }, "新版段落"), newList))),
      h("div", { class: "modal-foot" },
        h("button", { class: "btn", onclick: close }, "取消"),
        h("button", {
          class: "btn primary",
          onclick: async () => {
            const oi = [...oldSel].sort((x, y) => x - y);
            const ni = [...newSel].sort((x, y) => x - y);
            if (!oi.length && !ni.length) return toast("至少选择一侧的段落", true);
            const body = {
              old_indices: oi, new_indices: ni,
              change_type: typeSel.value || null,
            };
            try {
              if (opts.mode === "remap") {
                await api(`/api/documents/${d.id}/alignments/${opts.alignId}`, {
                  method: "PUT", body: JSON.stringify(body),
                });
                toast("映射已更新");
              } else {
                await api(`/api/documents/${d.id}/alignments`, {
                  method: "POST", body: JSON.stringify({ ...body, decision: "confirmed" }),
                });
                toast("映射已建立");
              }
              close();
              openDoc(d.id);
            } catch (e) { toast(e.message, true); }
          },
        }, "保存映射"))));
  root.append(modal);
  refreshCounts();
}

// ---------------- 示例文本 ----------------
const DEMO_OLD = `第一条 为了规范公司管理，制定本制度。本制度适用于全体员工。

第二条 员工应当遵守考勤规定，按时上下班。

第三条 请假应当提前申请，经主管批准后方可休假。
未经批准不到岗的，视为旷工。

第四条 工资于每月十日发放。

第五条 本制度自发布之日起施行。`;

const DEMO_NEW = `第一条 为规范公司经营管理，保障员工合法权益，根据有关法律，制定本制度。本制度适用于全体员工。

第二条 员工应当遵守考勤管理规定，按时上下班，不得迟到早退。

第三条 员工请假应当至少提前一个工作日通过系统提交申请。

第四条 请假经直接主管批准后方可休假；未经批准不到岗的，视为旷工。

第五条 工资于每月十五日发放，遇节假日顺延。

第六条 公司为员工缴纳社会保险和住房公积金。

第七条 本制度自发布之日起施行。`;

// ---------------- 启动 ----------------
document.getElementById("nav-list").addEventListener("click", renderList);
document.getElementById("nav-upload").addEventListener("click", renderUpload);
renderList();
