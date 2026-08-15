/* ====== AI 小说精修工作台 - 前端逻辑 v2（三需求升级） ====== */
const API = "";

// ====== 全局状态 ======
const state = {
  novels: [],
  novelId: null,
  novelMeta: null,
  toc: [],
  filteredToc: [],
  chapterIdx: null,
  chapter: null,
  cards: [],
  selectedCardIdx: -1,
  chatHistory: [],
  viewMode: "card",
  origEditor: null,
  modEditor: null,
  diffEditor: null,
  // 新增：Prompt / 模型 / GPU 心跳 / 批量进度
  ui: {
    cfgTab: "conn",
    promptCat: "all",
    ppkCat: "all",
    ppkTag: "",
    progressCollapsed: false,
    gpuHeartbeatTimer: null
  },
  progress: {
    running: false,
    cur: 0, total: 0, percent: 0,
    curInfo: "",
    log: []
  },
  // 读书模式状态
  study: {
    running: false,
    pollTimer: null,
    selectedChapters: null, // null=全部, [] = 手动选择的章节索引列表
    showChSel: false
  },
  // 世界书总结模板（详细/简洁/剧情）
  wbTemplate: "detailed",
  // 总体设定修改状态
  bulk: {
    running: false,
    pollTimer: null,
    pending: [],      // 待确认修改的章节列表 [{index,title,keywords,modified_preview}]
    status: null
  },
  // AI 供应商
  aiProvider: "local",
  // SSE 管理
  _sseXhr: null
};

const CAT_LABEL = { instruction: "📝 修改要求", global: "🌐 全局指令", agent: "🎭 Agent设定" };

// ====== 工具函数 ======
function toast(msg, type = "") {
  const t = document.getElementById("toast");
  t.className = "toast " + type;
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(window.__toast_t);
  window.__toast_t = setTimeout(() => t.style.display = "none", 2600);
}
async function api(url, opts = {}) {
  const res = await fetch(API + url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    let msg = "HTTP " + res.status;
    try { const j = JSON.parse(txt); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return res.json();
}
function sseRequest(url, payload, onChunk, onDone) {
  const xhr = new XMLHttpRequest();
  xhr.open("POST", API + url);
  xhr.setRequestHeader("Content-Type", "application/json");
  xhr.responseType = "text";
  let lastIdx = 0;
  let _finished = false;  // 防止 onDone 被多次调用

  const finish = (data) => {
    if (_finished) return;
    _finished = true;
    state._sseXhr = null;
    onDone && onDone(data);
    xhr.abort();
  };

  state._sseXhr = xhr;  // 保存引用，允许外部取消

  xhr.onprogress = () => {
    if (_finished) return;
    const txt = xhr.responseText;
    while (true) {
      const nl = txt.indexOf("\n\n", lastIdx);
      if (nl === -1) break;
      const line = txt.slice(lastIdx, nl).trim();
      lastIdx = nl + 2;
      if (!line.startsWith("data:")) continue;
      try {
        const obj = JSON.parse(line.slice(5).trim());
        onChunk(obj);
        if (obj.done || obj.type === "done") { finish(obj); return; }
      } catch (e) { /* ignore */ }
    }
  };
  xhr.onload = () => {
    if (_finished) return;
    const txt = xhr.responseText;
    while (true) {
      const nl = txt.indexOf("\n\n", lastIdx);
      if (nl === -1) {
        const rest = txt.slice(lastIdx).trim();
        if (rest.startsWith("data:")) {
          try {
            const obj = JSON.parse(rest.slice(5).trim());
            onChunk(obj);
            if (obj.done || obj.type === "done") { finish(obj); return; }
          } catch {}
        }
        break;
      }
      const line = txt.slice(lastIdx, nl).trim();
      lastIdx = nl + 2;
      if (line.startsWith("data:")) {
        try {
          const obj = JSON.parse(line.slice(5).trim());
          onChunk(obj);
          if (obj.done || obj.type === "done") { finish(obj); return; }
        } catch {}
      }
    }
    finish({ done: true });
  };
  xhr.onerror = () => {
    if (!_finished) {
      finish({ error: "网络错误", done: true });
    }
  };
  xhr.send(JSON.stringify(payload));
}
function escapeHtml(s) {
  return (s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtSize(bytes) {
  if (!bytes) return "?";
  const units = ["B","KB","MB","GB","TB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length-1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}

// ====== 初始化 ======
document.addEventListener("DOMContentLoaded", () => {
  // CodeMirror
  state.origEditor = CodeMirror.fromTextArea(document.getElementById("origEditor"), {
    mode: "markdown", lineWrapping: true, readOnly: true, theme: "eclipse",
    indentUnit: 2, lineNumbers: true
  });
  state.modEditor = CodeMirror.fromTextArea(document.getElementById("modEditor"), {
    mode: "markdown", lineWrapping: true, theme: "eclipse", indentUnit: 2, lineNumbers: true
  });
  state.modEditor.on("change", () => syncFromModEditor());
  state.diffEditor = CodeMirror.fromTextArea(document.getElementById("diffEditor"), {
    mode: "markdown", lineWrapping: true, readOnly: true, theme: "eclipse", lineNumbers: true
  });

  bindEvents();
  loadSettings().then(() => {
    // 按供应商显示连接状态：外部 API 则静默检查外部连接
    if (state.aiProvider === "external") checkExternalConnectionSilent();
    else checkOllama();
  });
  refreshNovelList();
  // 启动后自动跑一次 GPU 心跳（不阻塞）
  setTimeout(() => doHealthzGpu(true), 800);
  // 每 3 分钟做一次静默心跳刷新连接灯
  state.ui.gpuHeartbeatTimer = setInterval(() => doHealthzGpu(true), 3 * 60 * 1000);
});

function bindEvents() {
  // ============== 顶部/原有按钮 ==============
  document.getElementById("btnImport").onclick = () => document.getElementById("fileInput").click();
  document.getElementById("fileInput").onchange = onUploadNovel;
  document.getElementById("btnExport").onclick = onExportNovel;
  document.getElementById("btnSettings").onclick = openSettings;
  document.getElementById("btnCloseSettings").onclick = closeSettings;
  document.getElementById("btnFullscreen").onclick = () => document.body.classList.toggle("focused");

  document.getElementById("novelSelect").onchange = onNovelChange;
  document.getElementById("chapterSearch").oninput = renderToc;

  // ============== 新增：删除小说 ==============
  document.getElementById("btnDeleteNovel").onclick = deleteNovel;

  // ============== 新增：Agent 各区域自动保存 + 槽位保存/读取/管理 ==============
  document.querySelectorAll(".slot-save").forEach(btn => {
    btn.onclick = () => saveAgentSlot(btn.dataset.field);
  });
  document.querySelectorAll(".slot-load").forEach(sel => {
    sel.onchange = () => {
      if (sel.value) { loadAgentSlot(sel.dataset.field, parseInt(sel.value)); sel.value = ""; }
    };
  });
  document.querySelectorAll(".slot-manage").forEach(btn => {
    btn.onclick = () => toggleSlotManage(btn.dataset.field);
  });
  document.getElementById("btnAddStyleBlock").onclick = addStyleBlock;
  // 世界书生成模型选择：变化即保存到全局（跨小说）
  document.getElementById("wbModelSel").addEventListener("change", () => {
    const cfg = _agentCfg();
    cfg.worldbook_model = worldbookModelOverride();
    saveAgentConfigQuiet(cfg);
  });
  // 批量修改模型选择：变化即保存到全局（跨小说）
  document.getElementById("bulkModelSel").addEventListener("change", () => {
    const cfg = _agentCfg();
    cfg.bulk_model = bulkModelOverride();
    saveAgentConfigQuiet(cfg);
  });
  // 文风块列表：事件委托（开关 / 编辑 / 删除）
  document.getElementById("styleBlocks").addEventListener("click", e => {
    const el = e.target.closest("[data-sb]");
    if (!el) return;
    const id = el.dataset.sb;
    if (e.target.closest("[data-sb-toggle]")) toggleStyleBlock(id);
    else if (e.target.closest("[data-sb-edit]")) editStyleBlock(id);
    else if (e.target.closest("[data-sb-del]")) deleteStyleBlock(id);
  });
  // 通用编辑弹窗（文风块 / 槽位）
  document.getElementById("btnConfirmSlotEdit").onclick = confirmSlotEdit;
  document.getElementById("btnCancelSlotEdit").onclick = () => closeSlotEdit();
  document.getElementById("btnCloseSlotEdit").onclick = () => closeSlotEdit();
  // 自动保存：blur 时触发
  const _doAutoSave = () => autoSaveAgentSettings();
  const _markDirty = () => { state._agentDirty = true; };
  document.getElementById("globalPrompt").addEventListener("blur", _doAutoSave);
  document.getElementById("globalPrompt").addEventListener("input", _markDirty);
  document.getElementById("customAgent").addEventListener("blur", _doAutoSave);
  document.getElementById("customAgent").addEventListener("input", _markDirty);
  document.getElementById("instruction").addEventListener("blur", _doAutoSave);
  document.getElementById("instruction").addEventListener("input", _markDirty);
  // 也做定期自动保存（每30秒，只保存有变化的）
  setInterval(() => { if (state.novelId && state._agentDirty) autoSaveAgentSettings(); }, 30000);

  document.getElementById("btnMemQuery").onclick = doMemoryRetrieve;
  document.getElementById("memQuery").onkeydown = e => { if (e.key === "Enter") doMemoryRetrieve(); };

  document.querySelectorAll(".view-tabs .vt").forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll(".view-tabs .vt").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      switchView(btn.dataset.mode);
    };
  });

  document.getElementById("btnReview").onclick = doReviewChapter;
  document.getElementById("btnReindex").onclick = doReindexChapter;
  document.getElementById("btnSave").onclick = saveChapter;
  document.getElementById("btnAddCard").onclick = addNewCard;
  document.getElementById("btnEditChapterTitle").onclick = editChapterTitle;

  document.getElementById("quickCmd").onchange = function () {
    if (this.value) {
      const ins = document.getElementById("instruction");
      ins.value = (ins.value ? ins.value + "\n" : "") + this.value;
      this.value = "";
    }
  };
  document.getElementById("btnApplyOne").onclick = doApplyOne;
  document.getElementById("btnApplyAll").onclick = doApplyAll;
  document.getElementById("instruction").onkeydown = e => {
    if (e.ctrlKey && (e.key === "Enter")) doApplyOne();
  };

  document.querySelectorAll(".side-tabs .st").forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll(".side-tabs .st").forEach(t => t.classList.remove("active"));
      document.querySelectorAll(".side-panel").forEach(p => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById("tab-" + tab.dataset.tab).classList.add("active");
    };
  });
  document.getElementById("btnChatSend").onclick = sendChat;
  document.getElementById("chatInput").onkeydown = e => {
    if (e.ctrlKey && e.key === "Enter") sendChat();
  };

  // ============== 新增：设置 Tab 切换 ==============
  document.querySelectorAll(".cfg-tab").forEach(tab => {
    tab.onclick = () => switchCfgTab(tab.dataset.tab);
  });

  // ============== 新增：设置区模型 & GPU 心跳按钮 ==============
  document.getElementById("btnTestConn").onclick = testOllamaConn;
  document.getElementById("btnTestGpu").onclick = () => doHealthzGpu(false);
  document.getElementById("btnRefreshModels").onclick = refreshModelDropdown;
  document.getElementById("cfgModel").onchange = onModelDropdownChange;
  document.getElementById("cfgModelManual").oninput = onModelManualInput;

  document.getElementById("btnSaveCfg").onclick = saveSettings;
  document.getElementById("btnResetCfg").onclick = () => {
    if (confirm("确定要恢复默认设置吗？")) resetSettings();
  };

  // ============== 新增：Prompt 仓库（设置 Tab 3）==============
  document.querySelectorAll(".cfg-tab-prompt-btn").forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll(".cfg-tab-prompt-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.ui.promptCat = btn.dataset.cat;
      renderPromptsInSettings();
    };
  });
  document.getElementById("promptsSearch").oninput = renderPromptsInSettings;
  document.getElementById("btnNewPrompt").onclick = () => openPromptEditor();

  // Prompt 编辑器
  document.getElementById("btnClosePem").onclick = closePromptEditor;
  document.getElementById("btnCancelPem").onclick = closePromptEditor;
  document.getElementById("btnSavePem").onclick = savePromptEditor;
  document.getElementById("btnDeletePem").onclick = deletePromptEditor;

  // ============== 新增：底部「存模板 / 取模板」按钮 ==============
  document.getElementById("btnSaveInstr").onclick = openSaveInstrAsPrompt;
  document.getElementById("btnOpenPrompts").onclick = openPromptPicker;
  document.getElementById("btnClosePromptPicker").onclick = closePromptPicker;
  document.getElementById("btnCloseSavePrompt").onclick = closeSavePromptModal;
  document.getElementById("btnCancelSavePrompt").onclick = closeSavePromptModal;
  document.getElementById("btnConfirmSavePrompt").onclick = confirmSaveQuickPrompt;

  // Prompt Picker 分类
  document.querySelectorAll(".ppk-cat-btn").forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(".ppk-cat-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      state.ui.ppkCat = b.dataset.cat;
      renderPromptPicker();
    };
  });
  document.getElementById("ppkKw").oninput = renderPromptPicker;
  document.getElementById("ppkTag").onchange = e => { state.ui.ppkTag = e.target.value; renderPromptPicker(); };

  // ============== 新增：进度面板 ==============
  document.getElementById("ppToggle").onclick = () => {
    state.ui.progressCollapsed = !state.ui.progressCollapsed;
    document.getElementById("progressPanel").classList.toggle("collapsed", state.ui.progressCollapsed);
    document.getElementById("ppToggle").textContent = state.ui.progressCollapsed ? "▴" : "▾";
  };
  document.getElementById("ppClearLog").onclick = () => {
    state.progress.log = [];
    document.getElementById("ppLogContent").innerHTML = "";
  };
  document.getElementById("ppCancel").onclick = () => cancelRewrite();

  // ============== 新增三需求：读书模式 ==============
  document.getElementById("btnStudyStart").onclick = startStudy;
  document.getElementById("btnStudyStop").onclick = stopStudy;
  document.getElementById("btnToggleStudySel").onclick = toggleStudyChapterSelect;
  document.getElementById("btnStudySelAll").onclick = () => studySelectAllChapters(true);
  document.getElementById("btnStudySelNone").onclick = () => studySelectAllChapters(false);
  document.getElementById("btnStudySelConfirm").onclick = confirmStudyChapterSelect;
  document.getElementById("studyRangeInput").onkeydown = e => { if (e.key === "Enter") applyStudyRange(); };
  document.getElementById("btnStudyRangeApply").onclick = applyStudyRange;

  // ============== 新增：文本检索 ==============
  document.getElementById("btnTextSearch").onclick = doTextSearch;
  document.getElementById("textSearchKw").onkeydown = e => { if (e.key === "Enter") doTextSearch(); };
  document.getElementById("tsStart").onkeydown = e => { if (e.key === "Enter") doTextSearch(); };
  document.getElementById("tsEnd").onkeydown = e => { if (e.key === "Enter") doTextSearch(); };

  // ============== 新增：文本查找替换 ==============
  document.getElementById("btnRepPreview").onclick = doReplacePreview;
  document.getElementById("btnRepExec").onclick = doReplaceExec;
  document.getElementById("btnRepAll").onclick = setReplaceRangeAll;
  document.getElementById("repFind").onkeydown = e => { if (e.key === "Enter") doReplacePreview(); };
  document.getElementById("repStart").onkeydown = e => { if (e.key === "Enter") doReplacePreview(); };
  document.getElementById("repEnd").onkeydown = e => { if (e.key === "Enter") doReplacePreview(); };

  // ============== 新增：单章重新修改弹窗 ==============
  document.getElementById("btnCloseRework").onclick = closeReworkModal;
  document.getElementById("btnCancelRework").onclick = closeReworkModal;
  document.getElementById("btnConfirmRework").onclick = confirmRework;

  // ============== 新增：总体设定修改 ==============
  document.getElementById("btnBulkStart").onclick = startBulkModify;
  document.getElementById("btnBulkStop").onclick = stopBulkModify;
  document.getElementById("btnBulkConfirm").onclick = confirmBulkModify;
  document.getElementById("btnBulkDiscard").onclick = discardBulkModify;
  document.getElementById("btnBulkRangeAll").onclick = setBulkRangeAll;
  document.getElementById("btnBulkHistory").onclick = openBulkHistory;
  document.getElementById("btnCloseBulkHistory").onclick = closeBulkHistory;
  document.getElementById("bulkKeywords").onkeydown = e => { if (e.key === "Enter") startBulkModify(); };
  // 逐章选择保留/放弃
  document.getElementById("btnBulkConfirmSel").onclick = confirmSelectedBulk;
  document.getElementById("btnBulkDiscardSel").onclick = discardSelectedBulk;
  document.getElementById("bulkSelAll").onchange = function () {
    const checked = this.checked;
    document.querySelectorAll(".bmi-cb").forEach(cb => { cb.checked = checked; });
    updateBulkSelCount();
  };

  // ============== 新增：设定摘要（世界书） ==============
  document.getElementById("btnRefreshSummary").onclick = () => loadSettingsSummary();
  document.getElementById("btnCloseWb").onclick = closeWorldbookModal;
  document.getElementById("btnCancelWb").onclick = closeWorldbookModal;
  document.getElementById("btnSaveWb").onclick = saveWorldbookEntry;
  // 世界书总结模板切换
  const wbTplSel = document.getElementById("wbTemplateSel");
  if (wbTplSel) wbTplSel.onchange = () => {
    state.wbTemplate = wbTplSel.value;
    toast(`世界书总结模板已切换：${wbTplSel.options[wbTplSel.selectedIndex].text}`, "success");
  };
  // 世界书关键词定向总结
  const btnWbKw = document.getElementById("btnWbKw");
  if (btnWbKw) {
    btnWbKw.onclick = summarizeWorldbookKeyword;
    const wbKwIn = document.getElementById("wbKwInput");
    if (wbKwIn) wbKwIn.onkeydown = e => { if (e.key === "Enter") summarizeWorldbookKeyword(); };
  }

  // ============== 新增：AI 供应商切换 ==============
  document.getElementById("btnProviderLocal").onclick = () => switchAIProvider("local");
  document.getElementById("btnProviderExternal").onclick = () => switchAIProvider("external");

  // ============== 新增：外部 API 设置 ==============
  document.getElementById("btnTestExtConn").onclick = testExternalConnection;
  document.getElementById("btnListExtModels").onclick = listExternalModels;
  document.getElementById("btnAddExtSlot").onclick = addExtSlot;
  document.getElementById("btnDelExtSlot").onclick = delExtSlot;
  document.getElementById("cfgExtSlot").onchange = onExtSlotChange;
}

// ==================== Ollama 检查 & GPU 心跳 ====================
async function checkOllama() {
  try {
    const r = await api("/api/ollama/check");
    const pill = document.getElementById("statusPill");
    const txt = document.getElementById("statusText");
    if (!pill || !txt) return;
    pill.classList.remove("ok");
    if (r.ok && r.model_available) {
      txt.textContent = `已连接 · ${r.latency_ms}ms · ${r.target_model}`;
      pill.classList.add("ok");
    } else if (r.ok) {
      txt.textContent = `Ollama在线，但模型${r.target_model}未下载`;
      pill.classList.add("ok");
    } else {
      txt.textContent = r.error ? ("Ollama异常: " + r.error) : "Ollama未连接";
    }
  } catch (e) {
    const t = document.getElementById("statusText");
    if (t) t.textContent = "服务未就绪";
  }
}

async function doHealthzGpu(silent) {
  const led = document.getElementById("gpuLed");
  const statusEl = document.getElementById("gpuStatus");
  const detailEl = document.getElementById("gpuDetail");
  const ppDot = document.getElementById("ppGpuDot");
  const ppText = document.getElementById("ppGpuText");
  const setCls = (c) => { if (led) { led.className = "gpu-led " + c; } if (ppDot) { ppDot.className = "gpu-dot " + c; } };
  const setTxt = (s, d) => { if (statusEl) statusEl.textContent = s; if (detailEl) detailEl.innerHTML = d || ""; if (ppText) ppText.textContent = s; };
  if (!silent) setCls("load"), setTxt("正在测试 AI 真实运行（约 2-15 秒）...", "");
  try {
    const r = await api("/api/ai/healthz");
    if (r.ok) {
      setCls("ok");
      const s1 = `✅ AI 在运行（${r.model}）首Token ${r.first_token_ms || "?"}ms，共 ${r.total_ms || "?"}ms`;
      setTxt(s1, (r.hint || "") + `<br>输出字符数：${r.output_chars || 0}`);
    } else {
      setCls("err");
      const stageMap = { connect: "❌ Ollama 未连接", model_missing: "❌ 模型未下载", generate: "❌ 生成失败" };
      setTxt((stageMap[r.stage] || "❌ 未知错误") + ": " + (r.error || ""),
        r.stage === "model_missing" && r.models
          ? "本地已有模型：" + r.models.join(", ")
          : "请检查 Ollama 是否启动 / 模型是否 pull 成功 / 显存是否充足。");
    }
  } catch (e) {
    setCls("err");
    setTxt("❌ 心跳请求失败: " + e.message, "请确认后端 Flask 服务仍在运行。");
  }
}

// ==================== 模型下拉与切换 ====================
async function refreshModelDropdown() {
  const sel = document.getElementById("cfgModel");
  const listEl = document.getElementById("modelList");
  const btn = document.getElementById("btnRefreshModels");
  btn.disabled = true; btn.textContent = "⏳";
  try {
    const r = await api("/api/models/available");
    // 填充下拉
    const prevVal = sel.value || document.getElementById("cfgModelManual").value || "";
    sel.innerHTML = "";
    const emptyOpt = document.createElement("option");
    emptyOpt.value = ""; emptyOpt.textContent = r.ok ? "（选择本地已下载模型）" : "（加载失败，可手动输入）";
    sel.appendChild(emptyOpt);
    if (r.ok && r.models && r.models.length) {
      r.models.forEach(m => {
        const o = document.createElement("option");
        o.value = m.name;
        const size = fmtSize(m.size);
        o.textContent = `${m.name}  ·  ${size}`;
        sel.appendChild(o);
      });
      // 匹配当前
      if (prevVal) {
        const found = r.models.find(m => m.name === prevVal || (m.model && m.model === prevVal));
        if (found) sel.value = found.name;
        else document.getElementById("cfgModelManual").value = prevVal;
      }
      // 底部列表展示
      listEl.innerHTML = r.models.map(m => `
        <div class="m-item ${(r.current_model && (m.name === r.current_model || m.model === r.current_model)) ? 'target' : ''}">
          🐳 <strong>${escapeHtml(m.name)}</strong>
          <span style="color:var(--muted);margin-left:8px">${fmtSize(m.size)} · ${(m.details && m.details.parameter_size) || ''}</span>
        </div>`).join("");
    } else {
      listEl.innerHTML = `<div class="m-item" style="color:var(--danger)">⚠️ ${escapeHtml(r.error || "本地无模型，可执行：ollama pull deepseek")}</div>`;
    }
  } catch (e) {
    listEl.innerHTML = `<div class="m-item" style="color:var(--danger)">❌ 刷新失败：${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "🔄";
  }
}
function onModelDropdownChange() {
  const sel = document.getElementById("cfgModel");
  const manual = document.getElementById("cfgModelManual");
  if (sel.value) manual.value = sel.value;
}
function onModelManualInput() {
  const sel = document.getElementById("cfgModel");
  const manual = document.getElementById("cfgModelManual");
  // 手动输入时若匹配下拉则自动选中
  for (let i = 0; i < sel.options.length; i++) {
    if (sel.options[i].value === manual.value) { sel.value = manual.value; return; }
  }
  sel.value = "";
}

// ==================== 设置弹窗 ====================
async function loadSettings() {
  try {
    const cfg = await api("/api/config", { method: "GET" });
    document.getElementById("cfgBaseUrl").value = cfg.ollama.base_url;
    document.getElementById("cfgModelManual").value = cfg.ollama.model;
    document.getElementById("cfgTimeout").value = cfg.ollama.timeout;
    document.getElementById("cfgTemp").value = cfg.ollama.temperature;
    document.getElementById("cfgW").value = cfg.tri_ai.writer_temperature;
    document.getElementById("cfgR").value = cfg.tri_ai.reviewer_temperature;
    document.getElementById("cfgC").value = cfg.tri_ai.chat_temperature;
    document.getElementById("cfgK").value = cfg.tri_ai.retrieve_top_k;
    document.getElementById("cfgAllowRename").checked = !!(cfg.tri_ai && cfg.tri_ai.allow_ai_rename_chapter);
    document.getElementById("cfgEmb").value = cfg.embedding.model_name;
    document.getElementById("cfgChunk").value = cfg.embedding.chunk_size;
    document.getElementById("cfgOverlap").value = cfg.embedding.chunk_overlap;
    // 外部 API 配置（多槽位：显示槽位列表，加载当前槽位配置到表单）
    const ext = cfg.external_api || {};
    const slots = ext.slots || {};
    const active = ext.active_slot || "default";
    state._extSlots = slots;
    state._extActive = active;
    renderExtSlotSelect(slots, active);
    const cur = slots[active] || {};
    const pick = (k, d) => {
      let v = cur[k];
      if (v === undefined || v === null || v === "") v = ext[k];
      return (v === undefined || v === null || v === "") ? d : v;
    };
    document.getElementById("cfgExtEnabled").checked = ext.enabled || false;
    document.getElementById("cfgExtBaseUrl").value = pick("base_url", "https://api.openai.com/v1");
    document.getElementById("cfgExtApiKey").value = pick("api_key", "");
    document.getElementById("cfgExtModel").value = pick("model", "gpt-4o-mini");
    document.getElementById("cfgExtTimeout").value = pick("timeout", 120);
    document.getElementById("cfgExtTemp").value = pick("temperature", 0.7);
    // AI 供应商
    const provider = (cfg.ai_provider || {}).provider || "local";
    state.aiProvider = provider;
    updateProviderToggleUI(provider);
    // 本地存储持久化（双持久化的前端部分）
    localStorage.setItem("ns.config", JSON.stringify(cfg));
    // 进入设置时刷新一次模型下拉
    await refreshModelDropdown();
    // 如果匹配就选中
    const sel = document.getElementById("cfgModel");
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === cfg.ollama.model) { sel.value = cfg.ollama.model; break; }
    }
  } catch { /* ignore */ }
}
function switchCfgTab(tabName) {
  state.ui.cfgTab = tabName;
  document.querySelectorAll(".cfg-tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tabName));
  document.querySelectorAll(".cfg-tab-pane").forEach(p => p.classList.toggle("active", p.dataset.pane === tabName));
  if (tabName === "prompts") renderPromptsInSettings();
}
function openSettings() {
  loadSettings();
  document.getElementById("settingsModal").style.display = "flex";
}
function closeSettings() {
  document.getElementById("settingsModal").style.display = "none";
}
async function testOllamaConn() {
  // 先保存一次配置（因为用户可能改了 Base URL），再测试
  const cfg = collectCfgFromForm();
  await api("/api/config", { method: "POST", body: cfg });
  localStorage.setItem("ns.config", JSON.stringify(cfg));
  const st = document.getElementById("connStatus");
  st.textContent = "连通测试中...";
  st.className = "";
  try {
    const r = await api("/api/ollama/check");
    await refreshModelDropdown();
    if (r.ok && r.model_available) {
      st.textContent = `✅ 连通 · ${r.latency_ms}ms · 模型「${r.target_model}」可用`;
      st.className = "ok";
    } else if (r.ok) {
      st.textContent = `⚠️ 连通 · 但模型「${r.target_model}」未下载`;
      st.className = "err";
    } else {
      st.textContent = "❌ 失败：" + (r.error || "");
      st.className = "err";
    }
  } catch (e) {
    st.textContent = "❌ 请求异常：" + e.message;
    st.className = "err";
  }
}
function collectCfgFromForm() {
  const num = id => parseFloat(document.getElementById(id).value);
  const modelDropdown = document.getElementById("cfgModel").value;
  const modelManual = (document.getElementById("cfgModelManual").value || "").trim();
  // 下拉优先于手动输入
  const finalModel = modelDropdown || modelManual || "deepseek";
  return {
    ollama: {
      base_url: document.getElementById("cfgBaseUrl").value.trim() || "http://127.0.0.1:11434",
      model: finalModel,
      timeout: Math.max(5, num("cfgTimeout") || 300),
      temperature: Math.min(2, Math.max(0, num("cfgTemp") || 0.7))
    },
    external_api: {
      enabled: document.getElementById("cfgExtEnabled").checked,
      base_url: document.getElementById("cfgExtBaseUrl").value.trim() || "https://api.openai.com/v1",
      api_key: document.getElementById("cfgExtApiKey").value.trim(),
      model: document.getElementById("cfgExtModel").value.trim() || "gpt-4o-mini",
      timeout: Math.max(10, parseInt(document.getElementById("cfgExtTimeout").value) || 120),
      temperature: Math.min(2, Math.max(0, num("cfgExtTemp") || 0.7))
    },
    ai_provider: {
      provider: state.aiProvider
    },
    embedding: {
      model_name: document.getElementById("cfgEmb").value.trim() || "all-MiniLM-L6-v2",
      chunk_size: Math.max(100, parseInt(document.getElementById("cfgChunk").value) || 500),
      chunk_overlap: Math.max(0, parseInt(document.getElementById("cfgOverlap").value) || 50)
    },
    tri_ai: {
      writer_temperature: Math.min(2, Math.max(0, num("cfgW") || 0.8)),
      reviewer_temperature: Math.min(2, Math.max(0, num("cfgR") || 0.3)),
      chat_temperature: Math.min(2, Math.max(0, num("cfgC") || 0.5)),
      retrieve_top_k: Math.max(1, Math.min(30, parseInt(document.getElementById("cfgK").value) || 8)),
      allow_ai_rename_chapter: document.getElementById("cfgAllowRename").checked
    }
  };
}
async function saveSettings() {
  const cfg = collectCfgFromForm();
  try {
    await api("/api/config", { method: "POST", body: cfg });
    localStorage.setItem("ns.config", JSON.stringify(cfg));
    toast("设置已保存（双持久化：后端 config.json + 前端 localStorage）", "success");
    checkOllama();
    // 模型改变后立即触发一次 GPU 心跳（静默）
    setTimeout(() => doHealthzGpu(true), 300);
    closeSettings();
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}
async function resetSettings() {
  try {
    await fetch(API + "/api/config/_reset", { method: "POST" }).catch(() => null);
    loadSettings();
    toast("已恢复默认", "success");
  } catch { loadSettings(); }
}

// ==================== 小说 & 章节（原有） ====================
async function refreshNovelList() {
  try {
    const list = await api("/api/novels", { method: "GET" });
    state.novels = list;
    const sel = document.getElementById("novelSelect");
    sel.innerHTML = "";
    if (list.length === 0) {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "（请先导入TXT小说）";
      sel.appendChild(o);
      return;
    }
    list.forEach(n => {
      const o = document.createElement("option");
      o.value = n.id;
      o.textContent = `${n.title} (${n.chapters}章)`;
      sel.appendChild(o);
    });
    const prev = state.novelId;
    if (prev && list.find(n => n.id === prev)) {
      sel.value = prev;
    } else {
      sel.value = list[0].id;
      onNovelChange();
    }
  } catch (e) { toast("加载小说列表失败：" + e.message, "error"); }
}

async function onUploadNovel(e) {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    toast("正在导入并建立索引，请稍候...", "");
    const res = await fetch(API + "/api/novels/upload", { method: "POST", body: fd });
    const rawText = await res.text();
    let json;
    try { json = JSON.parse(rawText); }
    catch (parseErr) {
      const snippet = rawText.replace(/<[^>]+>/g, "").trim().slice(0, 120);
      if (res.status >= 500) throw new Error(`服务器错误 ${res.status}：${snippet || "请检查后端日志"}`);
      if (res.status === 413) throw new Error("文件太大，无法上传");
      if (!res.ok) throw new Error(`请求失败 (HTTP ${res.status})：${snippet || res.statusText}`);
      throw new Error("响应格式异常：后端未返回 JSON。请刷新后重试。");
    }
    if (json.error) throw new Error(json.error);
    if (!res.ok) throw new Error(json.error || `请求失败 (HTTP ${res.status})`);
    let msg = `导入成功！共 ${json.chapters} 章${json.indexed ? "，已建立记忆索引" : ""}`;
    if (json.index_warning) { toast(msg, "success"); toast("提示：" + json.index_warning, "warn"); }
    else toast(msg, "success");
    await refreshNovelList();
    const sel = document.getElementById("novelSelect");
    sel.value = json.id;
    onNovelChange();
  } catch (err) { toast("导入失败：" + err.message, "error"); }
  finally { e.target.value = ""; }
}

async function onExportNovel() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  try {
    const r = await api(`/api/novels/${state.novelId}/export`, { method: "GET" });
    if (!r.ok) throw new Error(r.error || "失败");
    toast(`已导出到：${r.file}`, "success");
    if (confirm("打开导出目录？")) {
      try { await fetch(API + "/api/novels/_open_dir", { method: "POST", body: JSON.stringify({ path: r.file }) }); } catch { /* ignore */ }
    }
  } catch (e) { toast("导出失败：" + e.message, "error"); }
}

async function onNovelChange() {
  const id = document.getElementById("novelSelect").value;
  if (!id) return;
  // 切换前保存当前小说的 Agent 设置
  if (state.novelId && state._agentDirty) await autoSaveAgentSettings();
  state.novelId = id;
  state.chapterIdx = null;
  state.chapter = null;
  state.cards = [];
  state.selectedCardIdx = -1;
  state._agentDirty = false;
  // 重置批量修改轮询并加载待确认列表
  clearTimeout(state.bulk.pollTimer);
  state.bulk.running = false;
  try {
    const r = await api(`/api/novels/${id}`, { method: "GET" });
    state.novelMeta = r.meta;
    state.toc = r.toc || [];
    state.filteredToc = [...state.toc];
    renderNovelMeta();
    renderToc();
    renderMemStats();
    // 加载该小说的 Agent 设置
    loadAgentSettings();
    // 加载总体设定修改的待确认列表
    loadBulkPending();
  } catch (e) { toast("加载小说失败：" + e.message, "error"); }
}

function renderNovelMeta() {
  const m = state.novelMeta;
  const el = document.getElementById("novelMeta");
  if (!m) { el.innerHTML = ""; return; }
  el.innerHTML = `
    <div class="kv"><span>书名：</span><strong>${escapeHtml(m.title)}</strong></div>
    <div class="kv"><span>章节数：</span><strong>${m.chapters || 0}</strong></div>
    <div class="kv"><span>总段落：</span><strong>${m.total_paragraphs || 0}</strong></div>
    <div class="kv"><span>记忆索引：</span><span class="tag ${m.indexed ? 'tag-success' : 'tag-warn'}">${m.indexed ? "已构建" : "未构建"}</span></div>
  `;
}

async function renderMemStats() {
  const el = document.getElementById("memStats");
  if (!state.novelId) { el.textContent = "-"; return; }
  try {
    const s = await api(`/api/novels/${state.novelId}/memory/stats`);
    const indexed = state.novelMeta ? state.novelMeta.indexed : false;
    const chunks = s.total_chunks || 0;
    if (chunks > 0) {
      el.textContent = `${chunks} 块`;
      el.className = "tag tag-success";
    } else if (indexed === false) {
      el.textContent = "未索引";
      el.className = "tag tag-warn";
    } else {
      el.textContent = "0 块";
      el.className = "tag tag-warn";
    }
  } catch { el.textContent = "N/A"; el.className = "tag tag-warn"; }
}

function renderToc() {
  const q = (document.getElementById("chapterSearch")?.value || "").trim().toLowerCase();
  const list = q ? state.toc.filter(c => String(c.title).toLowerCase().includes(q) || String(c.index).includes(q)) : state.toc;
  state.filteredToc = list;
  const el = document.getElementById("tocList");
  el.innerHTML = "";
  if (list.length === 0) {
    el.innerHTML = `<div style="padding:20px;color:var(--muted);text-align:center">无匹配章节</div>`;
    return;
  }
  list.forEach(c => {
    const div = document.createElement("div");
    const isMod = String(c.title).includes("【已修改】");
    div.className = "toc-item" + (state.chapterIdx === c.index ? " active" : "") + (isMod ? " modified" : "");
    div.innerHTML = `<span><span class="idx">${String(c.index).padStart(3, "0")}</span>${escapeHtml(c.title)}</span><span class="tag tag-muted">${c.paragraph_count || 0}段</span>`;
    div.onclick = (e) => {
      // 读书模式选取章节中
      if (state.study.showChSel) {
        // 点击复选框本身：交给浏览器原生行为处理，不额外翻转
        if (e.target.classList.contains("study-cb")) return;
        // 点击 toc-item 其他区域：翻转复选框
        const cb = div.querySelector(".study-cb");
        if (cb) cb.checked = !cb.checked;
        return;
      }
      loadChapter(c.index);
    };
    el.appendChild(div);
  });
}

// 从服务端重新拉取章节目录并刷新列表（用于标题被修改后同步显示）
async function refreshToc() {
  if (!state.novelId) return;
  try {
    const r = await api(`/api/novels/${state.novelId}`, { method: "GET" });
    if (r.toc) {
      state.toc = r.toc;
      state.filteredToc = [...r.toc];
      renderToc();
    }
  } catch { /* 忽略刷新失败，保持现状 */ }
}

async function loadChapter(idx) {
  if (!state.novelId) return;
  state.chapterIdx = idx;
  renderToc();
  try {
    const ch = await api(`/api/novels/${state.novelId}/chapters/${idx}`);
    state.chapter = ch;
    buildCardsFromChapter();
    document.getElementById("curChapterTitle").textContent = `第${ch.index}章  ${ch.title}`;
    if (ch.pending_mod) {
      document.getElementById("curChapterTitle").innerHTML = `第${ch.index}章  ${escapeHtml(ch.title)} <span class="tag tag-warn" style="font-size:11px;vertical-align:middle">📝 待确认修改</span>`;
    }
    document.getElementById("btnEditChapterTitle").style.display = "inline-flex";
    state.origEditor.setValue(ch.content || "");
    state.modEditor.setValue(ch.modified_content || ch.content || "");
    renderCards();
    renderDiffView();
    document.getElementById("selectedSegTag").textContent = "未选段落";
  } catch (e) { toast("加载章节失败：" + e.message, "error"); }
}

// ====== 修改章节名称 ======
async function editChapterTitle() {
  if (!state.novelId || !state.chapterIdx || !state.chapter) return;
  const cur = state.chapter.title || "";
  const newTitle = prompt("请输入新的章节名称：", cur);
  if (newTitle === null) return;
  const t = newTitle.trim();
  if (!t) return toast("章节名称不能为空", "warn");
  if (t === cur) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/chapters/${state.chapterIdx}`, {
      method: "PUT", body: { title: t }
    });
    if (r.ok) {
      state.chapter.title = t;
      // 更新本地 toc
      const item = state.toc.find(c => c.index === state.chapterIdx);
      if (item) item.title = t;
      renderToc();
      document.getElementById("curChapterTitle").textContent = `第${state.chapterIdx}章  ${t}`;
      toast(`✅ 章节名称已改为「${t}」`, "success");
    } else {
      toast("修改失败", "error");
    }
  } catch (e) { toast("修改失败：" + e.message, "error"); }
}

function buildCardsFromChapter() {
  const ch = state.chapter;
  if (!ch) return;
  const origPs = ch.paragraphs && ch.paragraphs.length ? ch.paragraphs : splitParagraphs(ch.content);
  const modPs = ch.modified_paragraphs && ch.modified_paragraphs.length ? ch.modified_paragraphs : splitParagraphs(ch.modified_content || ch.content);
  const n = Math.max(origPs.length, modPs.length);
  state.cards = [];
  for (let i = 0; i < n; i++) {
    state.cards.push({
      orig: origPs[i] || "",
      mod: modPs[i] || "",
      modified: (origPs[i] || "") !== (modPs[i] || ""),
      reviewed: false,
      review: null
    });
  }
  if (state.cards.length === 0) {
    state.cards.push({ orig: "", mod: "", modified: false, reviewed: false, review: null });
  }
}
function splitParagraphs(text) {
  return (text || "").split(/\n\s*\n|\n/).map(s => s.trim()).filter(s => s);
}

// ====== 渲染卡片视图（多选 + 性能优化） ======
let cardEditors = [];
let _cardsRendering = false;
function renderCards() {
  if (_cardsRendering) return;
  _cardsRendering = true;
  setTimeout(() => { _cardsRendering = false; }, 50);

  // 清除旧的 editors
  cardEditors.forEach(c => { try { c.toTextArea && c.toTextArea(); } catch {} });
  cardEditors = [];

  const list = document.getElementById("cardList");
  list.innerHTML = "";

  // 顶部全选栏
  if (state.cards.length > 1) {
    const bar = document.createElement("div");
    bar.className = "card-sel-bar";
    bar.innerHTML = `
      <label class="csb-check"><input type="checkbox" id="cardSelectAll" /> 全选</label>
      <span class="csb-info">共 ${state.cards.length} 段 · 已选 <span id="csbCount">0</span> 段</span>
    `;
    list.appendChild(bar);
    bar.querySelector("#cardSelectAll").onchange = function () {
      const checked = this.checked;
      document.querySelectorAll(".card-check").forEach(cb => { cb.checked = checked; });
      updateSelectedCards();
    };
  }

  const fragment = document.createDocumentFragment();
  state.cards.forEach((card, i) => {
    const row = document.createElement("div");
    row.className = "card" + (state.selectedCardIdx === i ? " selected" : "");
    row.dataset.idx = i;
    row.innerHTML = `
      <div class="card-check-col"><input type="checkbox" class="card-check" data-idx="${i}" /></div>
      <div class="idx">${i + 1}</div>
      <div class="cm-wrap"><textarea class="cm-orig">${escapeHtml(card.orig)}</textarea></div>
      <div class="cm-wrap"><textarea class="cm-mod">${escapeHtml(card.mod)}</textarea></div>
      <div class="ops">
        <button class="btn btn-sm btn-rewrite">✨ 改写</button>
        <button class="btn btn-sm btn-review">🔍 审校</button>
        <button class="btn btn-sm btn-accept">${card.modified ? "✅ 接受" : "📎 对齐原文"}</button>
        <button class="btn btn-sm btn-insert">⬇️ 插入</button>
        <button class="btn btn-sm btn-del" style="color:var(--danger)">🗑️ 删除</button>
      </div>
    `;
    fragment.appendChild(row);
    const origTa = row.querySelector(".cm-orig");
    const modTa = row.querySelector(".cm-mod");
    const cmO = CodeMirror.fromTextArea(origTa, { mode: "markdown", lineWrapping: true, readOnly: true, theme: "eclipse", lineNumbers: false });
    const cmM = CodeMirror.fromTextArea(modTa, { mode: "markdown", lineWrapping: true, theme: "eclipse", lineNumbers: false });
    cmO.setSize("100%", "auto");
    cmM.setSize("100%", "auto");
    cardEditors.push(cmO, cmM);
    row.onclick = (e) => {
      if (e.target.closest("button") || e.target.closest(".CodeMirror") || e.target.closest("input")) return;
      selectCard(i);
    };
    row.querySelector(".card-check").onclick = (e) => {
      e.stopPropagation();
      updateSelectedCards();
    };
    cmM.on("focus", () => selectCard(i));
    cmM.on("change", () => {
      state.cards[i].mod = cmM.getValue();
      state.cards[i].modified = state.cards[i].orig !== state.cards[i].mod;
      updateChapterFromCards();
    });
    row.querySelector(".btn-rewrite").onclick = () => { selectCard(i); doApplyOne(i); };
    row.querySelector(".btn-review").onclick = () => doReviewCard(i);
    row.querySelector(".btn-accept").onclick = () => {
      if (!state.cards[i].modified) state.cards[i].mod = state.cards[i].orig;
      state.cards[i].orig = state.cards[i].mod;
      state.cards[i].modified = false;
      renderCards();
      updateChapterFromCards();
    };
    row.querySelector(".btn-insert").onclick = () => {
      state.cards.splice(i + 1, 0, { orig: "", mod: "", modified: false, reviewed: false, review: null });
      renderCards();
      updateChapterFromCards();
    };
    row.querySelector(".btn-del").onclick = () => {
      if (state.cards.length <= 1 && !state.cards[0].orig && !state.cards[0].mod) return;
      if (confirm(`删除第 ${i + 1} 段？`)) {
        state.cards.splice(i, 1);
        if (state.cards.length === 0) state.cards.push({ orig: "", mod: "", modified: false });
        renderCards();
        updateChapterFromCards();
      }
    };
  });
  list.appendChild(fragment);
  // 关键修复：DOM 挂载后强制刷新所有 CodeMirror，避免"需手动点击才显示"的问题
  requestAnimationFrame(() => {
    setTimeout(() => {
      cardEditors.forEach(cm => { try { cm.refresh(); } catch {} });
      // 全文视图的编辑器也需要刷新
      try { state.origEditor.refresh(); } catch {}
      try { state.modEditor.refresh(); } catch {}
      try { state.diffEditor.refresh(); } catch {}
    }, 80);
  });
  updateSelectedCards();
}

function selectCard(i) {
  state.selectedCardIdx = i;
  document.querySelectorAll(".card").forEach((el, idx) => {
    el.classList.toggle("selected", idx === i);
  });
  // 同步渲染到完整视图编辑器
  if (state.viewMode === "full") {
    const ch = state.chapter;
    if (ch) state.modEditor.setValue(ch.modified_content || ch.content || "");
  }
  updateSelectedTag();
}

function updateSelectedCards() {
  const cbs = document.querySelectorAll(".card-check:checked");
  const indices = Array.from(cbs).map(cb => parseInt(cb.dataset.idx));
  state._multiSelected = indices;
  const countEl = document.getElementById("csbCount");
  if (countEl) countEl.textContent = indices.length;
  updateSelectedTag();
}

function updateSelectedTag() {
  const tag = document.getElementById("selectedSegTag");
  if (!tag) return;
  const multi = state._multiSelected;
  if (multi && multi.length > 1) {
    tag.textContent = `已选 ${multi.length} 段 (${multi.map(i => i + 1).join(", ")})`;
    tag.className = "tag tag-success";
  } else if (state.selectedCardIdx >= 0) {
    tag.textContent = `已选第 ${state.selectedCardIdx + 1} 段 / 共 ${state.cards.length} 段`;
    tag.className = "tag tag-success";
  } else {
    tag.textContent = "未选段落（全文模式下将对全文操作）";
    tag.className = "tag tag-muted";
  }
}

function addNewCard() {
  state.cards.push({ orig: "", mod: "", modified: false, reviewed: false, review: null });
  renderCards();
  updateChapterFromCards();
  selectCard(state.cards.length - 1);
  toast("已在末尾插入新段落，请输入内容", "success");
}

function updateChapterFromCards() {
  if (!state.chapter) return;
  const origText = state.cards.map(c => c.orig).join("\n\n").trim();
  const modText = state.cards.map(c => c.mod).join("\n\n").trim();
  state.chapter.modified_content = modText;
  state.chapter.modified_paragraphs = state.cards.map(c => c.mod).filter(s => s);
  state.chapter.content = origText;
  state.chapter.paragraphs = state.cards.map(c => c.orig).filter(s => s);
  if (state.modEditor.getValue() !== modText) state.modEditor.setValue(modText);
  renderDiffView();
}
function syncFromModEditor() {
  if (!state.chapter) return;
  if (state.viewMode !== "full") return;
  const txt = state.modEditor.getValue();
  state.chapter.modified_content = txt;
  const ps = splitParagraphs(txt);
  state.chapter.modified_paragraphs = ps;  // 同步段落，避免重建时使用旧值导致手动修改丢失
  if (ps.length === state.cards.length) {
    ps.forEach((p, i) => {
      state.cards[i].mod = p;
      state.cards[i].modified = state.cards[i].orig !== p;
    });
  } else {
    // 段落数变化：以手动编辑后的内容重建卡片，尽量保留原段落用于对比
    const origPs = state.cards.map(c => c.orig);
    state.cards = ps.map((p, i) => ({
      orig: origPs[i] || p,
      mod: p,
      modified: (origPs[i] || "") !== p,
      reviewed: false,
      review: null
    }));
  }
  renderCards();
  renderDiffView();
}

function renderDiffView() {
  if (!state.chapter) return;
  const orig = state.chapter.content || "";
  const mod = state.chapter.modified_content || orig;
  const origPs = splitParagraphs(orig);
  const modPs = splitParagraphs(mod);
  const lines = [];
  const n = Math.max(origPs.length, modPs.length);
  for (let i = 0; i < n; i++) {
    const o = origPs[i] || "";
    const m = modPs[i] || "";
    if (o === m && o) lines.push(`=== 第${i+1}段（未变）===\n${o}\n`);
    else if (o && !m) lines.push(`--- 第${i+1}段（原文，已删除）---\n${o}\n`);
    else if (!o && m) lines.push(`+++ 第${i+1}段（新增）+++\n${m}\n`);
    else lines.push(`<<< 原文 第${i+1}段 >>>\n${o}\n>>> 修改版 第${i+1}段 <<<\n${m}\n`);
  }
  state.diffEditor.setValue(lines.join("\n"));
}

function switchView(mode) {
  state.viewMode = mode;
  document.getElementById("cardView").style.display = mode === "card" ? "flex" : "none";
  document.getElementById("fullView").style.display = mode === "full" ? "flex" : "none";
  document.getElementById("diffView").style.display = mode === "diff" ? "flex" : "none";
  // 切换视图后刷新编辑器，避免"需手动点击才显示"
  setTimeout(() => {
    cardEditors.forEach(cm => { try { cm.refresh(); } catch {} });
    try { state.origEditor.refresh(); } catch {}
    try { state.modEditor.refresh(); } catch {}
    try { state.diffEditor.refresh(); } catch {}
  }, 100);
}

// ====== 保存 / 重索引 ======
async function saveChapter() {
  if (!state.novelId || !state.chapterIdx) return toast("请先选择章节", "warn");
  let content;
  if (state.viewMode === "full") {
    // 全文视图：以修改版编辑器内容为准（所见即所存），确保手动修改不丢失
    syncFromModEditor();
    content = state.modEditor.getValue();
  } else {
    updateChapterFromCards();
    content = state.chapter.modified_content;
  }
  try {
    const r = await api(`/api/novels/${state.novelId}/chapters/${state.chapterIdx}`, {
      method: "PUT",
      body: { modified_content: content }
    });
    if (r.ok) {
      toast("本章已保存（原文已同步替换为修改版）", "success");
      // 重新加载章节：让原文/修改版编辑器都立即显示已定稿内容，无需手动刷新
      await loadChapter(state.chapterIdx);
    }
    else toast("保存失败", "error");
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}
async function doReindexChapter() {
  if (!state.novelId || !state.chapterIdx) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/chapters/${state.chapterIdx}/reindex`, { method: "POST" });
    if (r.ok) { toast(`已重新索引：${r.chunks} 块`, "success"); renderMemStats(); }
    else toast(r.error || "失败", "error");
  } catch (e) { toast(e.message, "error"); }
}

// ==================== 进度可视化 ====================
function openProgressPanel(titleText) {
  const panel = document.getElementById("progressPanel");
  panel.style.display = "block";
  document.getElementById("ppTitle").textContent = titleText || "AI 改写进行中...";
  state.progress.running = true;
  state.progress.cur = 0;
  state.progress.total = 0;
  state.progress.percent = 0;
  state.progress.log = [];
  updateProgressBar(0, 0, 0);
  setCurInfo("等待 AI 响应...");
  document.getElementById("ppLogContent").innerHTML = "";
  setGpuProgressDot("load");
}
function closeProgressPanel(delayMs) {
  state.progress.running = false;
  setGpuProgressDot("run");
  // 取消所有正在进行的SSE请求，释放AI资源
  if (state._sseXhr) {
    state._sseXhr.abort();
    state._sseXhr = null;
  }
  const close = () => {
    document.getElementById("progressPanel").style.display = "none";
    disableButtons(false);  // 确保按钮恢复
  };
  if (delayMs) setTimeout(close, delayMs); else close();
}

function cancelRewrite() {
  if (state._sseXhr) {
    state._sseXhr.abort();
    state._sseXhr = null;
  }
  state.progress.running = false;
  disableButtons(false);
  setGpuProgressDot("run");
  appendProgressLog("warn", "⚠️ 用户取消了AI任务");
  updateProgressBar(state.progress.cur, state.progress.total, 100);
  setCurInfo("已取消");
  document.getElementById("progressPanel").style.display = "none";
  toast("已取消AI任务并释放资源", "warn");
}
function updateProgressBar(cur, total, percent) {
  state.progress.cur = cur; state.progress.total = total; state.progress.percent = percent;
  const bar = document.getElementById("progressBar");
  const txt = document.getElementById("progressText");
  const stat = document.getElementById("ppStat");
  const pct = Math.max(0, Math.min(100, percent | 0));
  bar.style.width = pct + "%";
  txt.textContent = pct + "%";
  if (total) stat.textContent = `${cur} / ${total} 段 (${pct}%)`;
  else stat.textContent = `0 / 0 段 (0%)`;
}
function setCurInfo(s) {
  const box = document.getElementById("ppCurInfo").querySelector(".value");
  if (box) box.textContent = s;
  state.progress.curInfo = s;
}
function setGpuProgressDot(stateName) {
  const dot = document.getElementById("ppGpuDot");
  const txt = document.getElementById("ppGpuText");
  if (!dot) return;
  dot.className = "gpu-dot " + (stateName || "");
  if (txt) {
    if (stateName === "run") txt.textContent = "AI 运行中";
    else if (stateName === "err") txt.textContent = "AI 异常";
    else if (stateName === "load") txt.textContent = "模型加载/推理中";
    else txt.textContent = "未检测";
  }
}
function appendProgressLog(cls, msg) {
  const content = document.getElementById("ppLogContent");
  const line = document.createElement("div");
  line.className = "log-line log-" + (cls || "info");
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  line.innerHTML = `<span style="opacity:0.5">[${ts}]</span> ${msg}`;
  content.appendChild(line);
  content.scrollTop = content.scrollHeight;
  state.progress.log.push({ cls, msg, ts });
}

// ==================== AI Writer 改写（单段 + 批量） ====================
function getCommonRewritePayload(extra = {}) {
  return {
    novel_id: state.novelId,
    chapter_idx: state.chapterIdx,
    instruction: document.getElementById("instruction").value.trim() || "请按用户隐含意图润色本段",
    global_prompt: document.getElementById("globalPrompt").value.trim(),
    custom_instruction: document.getElementById("customAgent").value.trim(),
    style_instruction: buildStyleInstruction(),
    stream: true,
    ...extra
  };
}

async function doApplyOne(forceIdx = null) {
  if (!state.novelId || !state.chapterIdx) return toast("请先选择小说和章节", "warn");
  const instruction = document.getElementById("instruction").value.trim();
  if (!instruction) return toast("请先输入修改要求（底部输入框）", "warn");

  // 确定要改写的文本来源
  let targets = []; // [{ text, label, cardIdx, card }]

  // 1. 卡片模式：用 forceIdx、多选、或单选
  if (state.viewMode === "card") {
    if (forceIdx != null && forceIdx >= 0) {
      targets.push({ text: state.cards[forceIdx].orig || state.cards[forceIdx].mod, label: `段${forceIdx + 1}`, cardIdx: forceIdx, card: state.cards[forceIdx] });
    } else if (state._multiSelected && state._multiSelected.length > 1) {
      state._multiSelected.forEach(i => {
        targets.push({ text: state.cards[i].orig || state.cards[i].mod, label: `段${i + 1}`, cardIdx: i, card: state.cards[i] });
      });
    } else if (state.selectedCardIdx >= 0) {
      const i = state.selectedCardIdx;
      targets.push({ text: state.cards[i].orig || state.cards[i].mod, label: `段${i + 1}`, cardIdx: i, card: state.cards[i] });
    } else {
      return toast("请在卡片中点击选中段落，或勾选多选复选框", "warn");
    }
  } else {
    // 2. 全文/对比模式：优先用编辑器选区，其次全文
    const sel = state.modEditor.getSelection().trim();
    const fullText = state.modEditor.getValue().trim();
    if (!fullText) return toast("当前编辑器内容为空", "warn");
    if (sel && sel.length > 0 && sel !== fullText) {
      // 用户手动选中了部分文本
      targets.push({ text: sel, label: `选区 (${sel.length}字)`, cardIdx: -1, card: null });
    } else {
      // 全章
      targets.push({ text: fullText, label: "全章", cardIdx: -1, card: null });
    }
    syncFromModEditor();
  }

  if (!targets.length) return toast("请选择要修改的内容", "warn");

  const multiMode = targets.length > 1;
  const unifyHint = multiMode ? "（统一上下文改写，避免前后不一致）" : "";

  // 单段：走 rewrite（流式）
  if (!multiMode) {
    const t = targets[0];
    openProgressPanel(`${t.label} 单段改写中...`);
    appendProgressLog("info", `开始改写：${t.text.slice(0, 30)}...`);
    disableButtons(true);
    const payload = getCommonRewritePayload({ original_text: t.text });
    sseRequest("/api/ai/writer/rewrite", payload,
      chunk => {
        if (chunk.error) { toast(chunk.error, "error"); appendProgressLog("err", "错误：" + chunk.error); setGpuProgressDot("err"); disableButtons(false); return; }
        if (chunk.content) {
          if (t.cardIdx < 0) {
            const cur = state.modEditor.getValue();
            state.modEditor.setValue(cur + chunk.content);
          } else {
            const modCm = cardEditors[t.cardIdx * 2 + 1];
            if (modCm) modCm.setValue(modCm.getValue() + chunk.content);
            else t.card.mod = (t.card.mod || "") + chunk.content;
          }
          if (chunk.content.trim()) appendProgressLog("chunk", escapeHtml(chunk.content.length > 80 ? chunk.content.slice(0, 80) + "…" : chunk.content));
          setGpuProgressDot("run");
        }
      },
      done => {
        disableButtons(false);
        if (done.error) { appendProgressLog("err", "中断：" + done.error); setGpuProgressDot("err"); return; }
        if (t.cardIdx >= 0) {
          const modCm = cardEditors[t.cardIdx * 2 + 1];
          if (modCm) t.card.mod = modCm.getValue();
          t.card.modified = t.card.orig !== t.card.mod;
        } else {
          syncFromModEditor();
        }
        updateChapterFromCards();
        renderCards();
        if (t.cardIdx >= 0) selectCard(t.cardIdx);
        updateProgressBar(1, 1, 100);
        appendProgressLog("ok", `✅ 改写完成`);
        setGpuProgressDot("run");
        closeProgressPanel(1800);
        toast("改写完成 ✅", "success");
      }
    );
    return;
  }

  // === 多段：统一改写（一次 AI 调用，通读全段上下文） ===
  openProgressPanel(`🔥 自选 ${targets.length} 段 ${unifyHint}`);
  appendProgressLog("info", `启动统一改写：AI 将通读全部 ${targets.length} 段后整体修改`);
  appendProgressLog("info", `段落：${targets.map(t => t.label).join("、")}`);
  disableButtons(true);

  const payload = {
    novel_id: state.novelId,
    chapter_idx: state.chapterIdx,
    chapter_title: state.chapter ? state.chapter.title : "",
    paragraphs: targets.map(t => t.text),
    instruction: instruction,
    global_prompt: document.getElementById("globalPrompt").value.trim(),
    custom_instruction: document.getElementById("customAgent").value.trim(),
    style_instruction: buildStyleInstruction()
  };

  let doneCount = 0;
  const total = targets.length;

  sseRequest("/api/ai/writer/rewrite_batch", payload,
    ev => {
      if (ev.type === "progress") {
        if (ev.paragraph) {
          appendProgressLog("info", `🎯 AI 正在阅读第 ${ev.paragraph_idx + 1}/${total} 段...`);
        } else {
          appendProgressLog("info", ev.percent <= 10 ? "🤖 AI 正在通读全文上下文..." : "🤖 AI 已完成通读，正在改写...");
        }
        updateProgressBar(doneCount, total, ev.percent || 5);
      } else if (ev.type === "result") {
        const t = targets[ev.paragraph_idx];
        if (t && t.cardIdx >= 0) {
          t.card.mod = ev.final_text || "";
          t.card.modified = ev.changed;
        } else if (!t) {
          // 新增段落（AI返回了比输入更多的段）
          targets.push({ text: ev.final_text || "", label: `新增段${ev.paragraph_idx + 1}`, cardIdx: -1, card: null });
        }
        doneCount++;
        updateProgressBar(doneCount, Math.max(total, ev.paragraph_idx + 1), Math.round(doneCount / Math.max(total, doneCount) * 100));
        const preview = (ev.final_text || "").slice(0, 60);
        appendProgressLog("ok", `✅ ${(t || targets[targets.length - 1]).label} 完成 ${ev.changed ? "（已修改）" : t ? "（无变化）" : "（新增）"} | ${preview}${preview.length >= 60 ? "…" : ""}`);
      } else if (ev.type === "error") {
        appendProgressLog("err", `❌ ${(targets[ev.paragraph_idx] || {}).label || "?"} 出错：${escapeHtml(ev.error || "")}`);
        doneCount++;
        setGpuProgressDot("err");
      }
      if (ev.percent >= 5 && ev.percent < 90) setGpuProgressDot("run");
      updateChapterFromCards();
    },
    done => {
      disableButtons(false);
      updateProgressBar(total, total, 100);
      if (done.error) {
        appendProgressLog("err", "❌ 统一改写异常：" + done.error);
        toast("改写异常：" + done.error, "error");
        setGpuProgressDot("err");
      } else {
        appendProgressLog("ok", `🎉 统一改写完成！AI 已通读全部 ${total} 段上下文并改写`);
        toast(`${total} 段统一改写完成 ✅`, "success");
        setGpuProgressDot("run");
      }
      updateChapterFromCards();
      renderCards();
      closeProgressPanel(2400);
    }
  );
}

// 新版：使用 /api/ai/writer/rewrite_batch 统一改写整章（一次 AI 调用，通读全文）
async function doApplyAll() {
  if (!state.novelId || !state.chapterIdx) return toast("请先选择小说和章节", "warn");
  const instruction = document.getElementById("instruction").value.trim();
  if (!instruction) return toast("请先输入修改要求", "warn");
  const paragraphs = state.cards.map(c => c.orig || c.mod);
  const validCount = paragraphs.filter(s => s && s.trim()).length;
  if (!validCount) return toast("所有段落均为空，无需改写", "warn");
  if (!confirm(`将对本章 ${state.cards.length} 段（实际非空 ${validCount} 段）进行统一改写。\n\nAI 将一次性通读全章内容后再改写，确保上下文一致。确定继续？`)) return;

  const chapterTitle = (state.chapter && state.chapter.title) ? `第${state.chapter.index}章《${state.chapter.title}》` : `第${state.chapterIdx}章`;
  openProgressPanel(`🚀 全章统一改写 · ${chapterTitle}`);
  disableButtons(true);

  const payload = {
    novel_id: state.novelId,
    chapter_idx: state.chapterIdx,
    chapter_title: state.chapter ? state.chapter.title : "",
    paragraphs: paragraphs,
    instruction: instruction,
    global_prompt: document.getElementById("globalPrompt").value.trim(),
    custom_instruction: document.getElementById("customAgent").value.trim(),
    style_instruction: buildStyleInstruction()
  };

  appendProgressLog("info", `启动全章统一改写：AI 将一次通读 ${paragraphs.length} 段后整体修改`);
  appendProgressLog("info", `章节：${chapterTitle} | 修改要求：${instruction.slice(0, 40)}${instruction.length > 40 ? "…" : ""}`);

  let doneCount = 0;

  sseRequest("/api/ai/writer/rewrite_batch", payload,
    ev => {
      if (ev.type === "progress") {
        appendProgressLog("info", ev.percent <= 10 ? "🤖 AI 正在通读全章上下文..." : "✍️ AI 已完成通读，开始生成改写...");
        updateProgressBar(doneCount, state.cards.length, ev.percent || 5);
        setGpuProgressDot("run");
      } else if (ev.type === "result") {
        const idx = ev.paragraph_idx;
        // 动态扩展 cards 数组
        while (idx >= state.cards.length) {
          state.cards.push({ orig: "", mod: "", modified: false });
        }
        state.cards[idx].mod = ev.final_text || "";
        state.cards[idx].modified = ev.changed;
        if (!state.cards[idx].orig) state.cards[idx].orig = ev.final_text || "";
        doneCount++;
        const newTotal = ev.total_paragraphs || state.cards.length;
        updateProgressBar(doneCount, newTotal, Math.round(doneCount / newTotal * 100));
        const preview = (ev.final_text || "").slice(0, 50);
        appendProgressLog("ok", `✅ 段 ${idx + 1} ${ev.changed ? "已修改" : "无变化"} | ${preview}…`);
      } else if (ev.type === "error") {
        appendProgressLog("err", `❌ 段 ${(ev.paragraph_idx + 1) || "?"} 出错：${escapeHtml(ev.error || "")}`);
        doneCount++;
        setGpuProgressDot("err");
      }
      updateChapterFromCards();
    },
    done => {
      disableButtons(false);
      // 如果 AI 返回的段落数不同，调整 cards 到最终数量
      if (done.results && done.results.length !== state.cards.length) {
        const newTotal = done.results.length;
        state.cards.length = newTotal;
        for (let i = 0; i < newTotal; i++) {
          if (!state.cards[i]) state.cards[i] = { orig: "", mod: "", modified: false };
          state.cards[i].mod = done.results[i] || "";
          if (!state.cards[i].orig) state.cards[i].orig = state.cards[i].mod;
          state.cards[i].modified = true;
        }
      }
      updateProgressBar(state.cards.length, state.cards.length, 100);
      setCurInfo("🎉 全章统一改写完成");
      if (done.error) {
        appendProgressLog("err", "❌ 任务异常中断：" + done.error);
        toast("改写异常：" + done.error, "error");
        setGpuProgressDot("err");
      } else {
        const changeNote = done.results && done.results.length !== payload.paragraphs.length
          ? `（段落数 ${payload.paragraphs.length} → ${done.results.length}）` : "";
        appendProgressLog("ok", `🎉 全章统一改写完成！AI 已通读全文并改写 ${state.cards.length} 段${changeNote}`);
        toast("全章统一改写完成 ✅" + changeNote, "success");
        setGpuProgressDot("run");
      }
      // AI 建议的新章节名：自动保存（仅在设置中开启「允许 AI 修改章节名称」时 AI 才会输出）
      if (done.new_title) {
        const nt = String(done.new_title).trim();
        if (nt && state.chapter && nt !== state.chapter.title) {
          api(`/api/novels/${state.novelId}/chapters/${state.chapterIdx}`, { method: "PUT", body: { title: nt } })
            .then(r => {
              if (r.ok) {
                toast(`AI 已将本章标题更新为「${nt}」`, "success");
                if (state.chapter) state.chapter.title = nt;
                refreshToc();
              }
            })
            .catch(() => {});
        }
      }
      updateChapterFromCards();
      renderCards();
      closeProgressPanel(2400);
      if (document.getElementById("autoReview").checked) doReviewChapter();
    }
  );
}

function disableButtons(d) {
  document.querySelectorAll("#centerPane .btn, #bottomBar .btn").forEach(b => b.disabled = d);
}

// ==================== Reviewer & Chat （原有，基本不变） ====================
async function doReviewCard(idx) {
  if (!state.novelId) return;
  const card = state.cards[idx];
  const text = card.mod || card.orig;
  if (!text) return;
  try {
    const r = await api("/api/ai/reviewer/check", {
      method: "POST",
      body: { novel_id: state.novelId, chapter_idx: state.chapterIdx, text }
    });
    card.reviewed = true; card.review = r;
    renderReviewBox();
    if (r.issues) toast(`发现 ${r.issues.length} 个问题，请查看右侧审校结果`, "warn");
    else if (r.pass) toast("审校通过 ✅", "success");
    else toast("审校完成", "success");
  } catch (e) { toast("审校失败：" + e.message, "error"); }
}
async function doReviewChapter() {
  if (!state.chapter) return;
  const text = (state.chapter.modified_content || state.chapter.content || "");
  if (!text) return;
  toast("正在对整章进行审校，请稍候...", "");
  try {
    const r = await api("/api/ai/reviewer/check", {
      method: "POST",
      body: { novel_id: state.novelId, chapter_idx: state.chapterIdx, text }
    });
    document.getElementById("reviewBox").dataset.src = "chapter";
    renderReviewRaw(r);
    if (r.issues) toast(`发现 ${r.issues.length} 个一致性问题`, "warn");
    else if (r.pass) toast("审校通过 ✅", "success");
    else toast("审校完成", "success");
  } catch (e) { toast("审校失败：" + e.message, "error"); }
}
function renderReviewBox() {
  const idx = state.selectedCardIdx >= 0 ? state.selectedCardIdx : 0;
  const card = state.cards[idx];
  if (!card || !card.reviewed) {
    document.getElementById("reviewBox").innerHTML = `已选第 ${idx + 1} 段，尚未审校。点击下方“🔍 Reviewer审校”或段落上的审校按钮。`;
    document.getElementById("reviewBox").className = "review-box";
    return;
  }
  renderReviewRaw(card.review);
}
function renderReviewRaw(r) {
  const box = document.getElementById("reviewBox");
  if (!r) { box.innerHTML = "（无数据）"; return; }
  if (r.error) { box.innerHTML = `❌ 审校调用错误：${escapeHtml(r.error)}`; box.className = "review-box"; return; }
  if (r.pass) { box.innerHTML = `✅ ${escapeHtml(r.comment || "未检测到明显问题")}`; box.className = "review-box ok"; return; }
  if (r.parse_failed) { box.innerHTML = `⚠️ 审校输出解析失败，原文：<pre>${escapeHtml(r.raw || "")}</pre>`; box.className = "review-box"; return; }
  const issues = r.issues || [];
  if (issues.length === 0) { box.innerHTML = "✅ 未发现问题"; box.className = "review-box ok"; return; }
  const sevMap = { high: "高", medium: "中", low: "低" };
  box.className = "review-box";
  box.innerHTML = `<div style="font-weight:600;margin-bottom:6px">🔍 共发现 ${issues.length} 个问题：</div>` +
    issues.map(i => `
      <div class="issue-item sev-${i.severity || 'low'}">
        <strong>[${sevMap[i.severity || 'low']}·${i.type || '?'}] ${escapeHtml(i.position || '')}</strong><br/>
        <span>问题：${escapeHtml(i.problem || '')}</span><br/>
        <span>建议：${escapeHtml(i.suggestion || '')}</span>
      </div>
    `).join("");
}

function appendChatMsg(role, content) {
  const el = document.getElementById("chatLog");
  const div = document.createElement("div");
  div.className = "chat-msg " + (role === "user" ? "u" : "a");
  div.innerHTML = escapeHtml(content).replace(/\n/g, "<br>");
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  return div;
}
async function sendChat() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  const input = document.getElementById("chatInput");
  const q = input.value.trim();
  if (!q) return;
  appendChatMsg("user", q);
  input.value = "";
  const history = state.chatHistory.slice();
  state.chatHistory.push({ role: "user", content: q });
  try {
    const refs = await api("/api/ai/refs", {
      method: "POST", body: { novel_id: state.novelId, query: q, top_k: 5, chapter_idx: state.chapterIdx }
    });
    const refHtml = (refs || []).slice(0, 3).map(r =>
      `<div class="refs">📖 引用 第${r.meta.chapter_idx}章《${escapeHtml(r.meta.chapter_title || '')}》 (相关度${(r.score * 100).toFixed(0)}%)</div>`
    ).join("");
    const aDiv = appendChatMsg("ai", "");
    if (refHtml) aDiv.innerHTML = refHtml + "<div class='ai-body'></div>";
    else aDiv.innerHTML = "<div class='ai-body'></div>";
    const body = aDiv.querySelector(".ai-body");
    let finalContent = "";
    sseRequest("/api/ai/chat/answer", {
      novel_id: state.novelId, question: q, history, stream: true
    }, chunk => {
      if (chunk.error) { body.innerHTML = `<span style="color:var(--danger)">❌ ${escapeHtml(chunk.error)}</span>`; return; }
      if (chunk.content) { finalContent += chunk.content; body.innerHTML = escapeHtml(finalContent).replace(/\n/g, "<br>"); }
      aDiv.parentElement.scrollTop = aDiv.parentElement.scrollHeight;
    }, () => {
      state.chatHistory.push({ role: "assistant", content: finalContent });
    });
  } catch (e) { appendChatMsg("ai", "❌ 错误：" + e.message); }
}

// ====== 记忆检索 ======
async function doMemoryRetrieve() {
  if (!state.novelId) return;
  const q = document.getElementById("memQuery").value.trim();
  if (!q) return;
  try {
    const refs = await api(`/api/novels/${state.novelId}/memory/retrieve`, {
      method: "POST", body: { query: q, top_k: 6, chapter_idx: state.chapterIdx, exclude_chapter: true }
    });
    const el = document.getElementById("memResults");
    if (!refs.length) { el.innerHTML = `<div style="color:var(--muted)">未检索到相关内容</div>`; return; }
    el.innerHTML = refs.map(r => `
      <div class="mem-item" data-chapter="${r.meta.chapter_idx}" data-preview="${encodeURIComponent(r.content)}">
        <div class="src">第${r.meta.chapter_idx}章《${escapeHtml(r.meta.chapter_title || '')}》· 相关度 ${(r.score * 100).toFixed(0)}%</div>
        <div class="text">${escapeHtml(r.content)}</div>
      </div>
    `).join("");
    el.querySelectorAll(".mem-item").forEach(it => {
      it.onclick = () => loadChapter(parseInt(it.dataset.chapter));
    });
  } catch (e) { toast("检索失败：" + e.message, "error"); }
}

// ============================================================
// 新增：Prompt 仓库 - 三端入口（设置管理 / 底部快速取模板 / 存模板）
// ============================================================
function renderPromptCard(p) {
  const tags = (p.tags || []).map(t => `<span class="pc-tag">#${escapeHtml(t)}</span>`).join("");
  return `
    <div class="prompt-card" data-id="${p.id}">
      <div class="pc-head">
        <div class="pc-title">${escapeHtml(p.title)}</div>
        <span class="pc-cat cat-${p.category || 'instruction'}">${CAT_LABEL[p.category] || '📝'}</span>
      </div>
      <div class="pc-content">${escapeHtml(p.content || '')}</div>
      ${tags ? `<div class="pc-tags">${tags}</div>` : ''}
      <div class="pc-foot">
        <div class="pc-note">${escapeHtml(p.note || '—')}</div>
        <div class="pc-actions" data-actions>
          <!-- 按钮由具体调用处插入 -->
        </div>
      </div>
    </div>`;
}

// --- 设置里的 Prompt 管理 ---
async function renderPromptsInSettings() {
  const container = document.getElementById("promptsList");
  if (!container) return;
  const kw = (document.getElementById("promptsSearch").value || "").trim();
  try {
    const list = await api(`/api/prompts?category=${encodeURIComponent(state.ui.promptCat === 'all' ? '' : state.ui.promptCat)}&keyword=${encodeURIComponent(kw)}`);
    if (!list || !list.length) {
      container.innerHTML = `<div class="pc-empty">✨ 还没有 Prompt 模板。<br>点击右上「➕ 新建」创建第一个，或点击底部「💾 存模板」保存当前输入。</div>`;
      return;
    }
    container.innerHTML = list.map(p => {
      const tags = (p.tags || []).map(t => `<span class="pc-tag">#${escapeHtml(t)}</span>`).join("");
      return `
      <div class="prompt-card" data-id="${p.id}">
        <div class="pc-head">
          <div class="pc-title">${escapeHtml(p.title)}</div>
          <span class="pc-cat cat-${p.category || 'instruction'}">${CAT_LABEL[p.category] || '📝'}</span>
        </div>
        <div class="pc-content">${escapeHtml(p.content || '')}</div>
        ${tags ? `<div class="pc-tags">${tags}</div>` : ''}
        <div class="pc-foot">
          <div class="pc-note">${escapeHtml(p.note || '—')}</div>
          <div class="pc-actions">
            <button class="btn btn-xs btn-default" data-act="apply">📌 应用</button>
            <button class="btn btn-xs btn-default" data-act="edit">✏️ 编辑</button>
            <button class="btn btn-xs btn-warn" data-act="del">🗑️</button>
          </div>
        </div>
      </div>`;
    }).join("");
    // 绑定卡片操作
    container.querySelectorAll(".prompt-card").forEach(card => {
      const id = card.dataset.id;
      card.querySelector('[data-act="edit"]').onclick = () => {
        const p = list.find(x => x.id === id); if (p) openPromptEditor(p);
      };
      card.querySelector('[data-act="del"]').onclick = async () => {
        if (!confirm("确定删除该 Prompt 模板？")) return;
        try {
          await api(`/api/prompts/${id}`, { method: "DELETE" });
          toast("已删除", "success");
          renderPromptsInSettings();
        } catch (e) { toast("删除失败：" + e.message, "error"); }
      };
      card.querySelector('[data-act="apply"]').onclick = () => {
        const p = list.find(x => x.id === id); if (p) applyPromptToForm(p);
      };
    });
  } catch (e) {
    container.innerHTML = `<div class="pc-empty" style="color:var(--danger)">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

// --- Prompt 编辑器（新建/编辑）---
function openPromptEditor(p) {
  document.getElementById("pemId").value = p ? p.id : "";
  document.getElementById("pemTitle").textContent = p ? "✏️ 编辑 Prompt" : "📝 新建 Prompt";
  document.getElementById("pemTitleInput").value = p ? p.title || "" : "";
  document.getElementById("pemCategory").value = p ? p.category || "instruction" : "instruction";
  document.getElementById("pemTags").value = p ? (p.tags || []).join(", ") : "";
  document.getElementById("pemNote").value = p ? p.note || "" : "";
  document.getElementById("pemContent").value = p ? p.content || "" : "";
  document.getElementById("btnDeletePem").style.display = p ? "inline-flex" : "none";
  document.getElementById("promptEditModal").style.display = "flex";
}
function closePromptEditor() {
  document.getElementById("promptEditModal").style.display = "none";
}
async function savePromptEditor() {
  const id = document.getElementById("pemId").value || undefined;
  const title = document.getElementById("pemTitleInput").value.trim();
  if (!title) return toast("请填写标题", "warn");
  const content = document.getElementById("pemContent").value.trim();
  if (!content) return toast("Prompt 内容不能为空", "warn");
  const tagsStr = document.getElementById("pemTags").value;
  const tags = tagsStr.split(/[,，]/).map(s => s.trim()).filter(s => s);
  const payload = {
    id: id, title, content,
    category: document.getElementById("pemCategory").value,
    tags,
    note: document.getElementById("pemNote").value.trim()
  };
  try {
    const r = await api("/api/prompts", { method: "POST", body: payload });
    if (r.ok) {
      toast(id ? "已更新 Prompt" : "已保存 Prompt", "success");
      closePromptEditor();
      renderPromptsInSettings();
    }
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}
async function deletePromptEditor() {
  const id = document.getElementById("pemId").value;
  if (!id) return;
  if (!confirm("确定删除？")) return;
  try {
    await api(`/api/prompts/${id}`, { method: "DELETE" });
    toast("已删除", "success");
    closePromptEditor();
    renderPromptsInSettings();
  } catch (e) { toast("删除失败：" + e.message, "error"); }
}

// --- 底部「💾 存模板」弹窗 ---
function openSaveInstrAsPrompt() {
  const src = document.getElementById("instruction").value.trim();
  if (!src) return toast("当前修改要求为空，没东西可保存~", "warn");
  document.getElementById("psmTitle").value = "";
  document.getElementById("psmCategory").value = "instruction";
  document.getElementById("psmTags").value = "";
  document.getElementById("psmNote").value = "";
  document.getElementById("psmContent").value = src;
  document.getElementById("promptSaveModal").style.display = "flex";
}
function closeSavePromptModal() {
  document.getElementById("promptSaveModal").style.display = "none";
}
async function confirmSaveQuickPrompt() {
  const title = document.getElementById("psmTitle").value.trim();
  if (!title) return toast("请填写模板标题", "warn");
  const content = document.getElementById("psmContent").value.trim();
  if (!content) return toast("Prompt 内容不能为空", "warn");
  const tagsStr = document.getElementById("psmTags").value;
  const tags = tagsStr.split(/[,，]/).map(s => s.trim()).filter(s => s);
  const payload = {
    title, content, tags,
    category: document.getElementById("psmCategory").value,
    note: document.getElementById("psmNote").value.trim()
  };
  try {
    const r = await api("/api/prompts", { method: "POST", body: payload });
    if (r.ok) {
      toast(`✅ 已保存模板「${r.prompt.title}」，下次可直接“取模板”调用`, "success");
      closeSavePromptModal();
    }
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}

// --- 底部「📚 取模板」PromptPicker ---
async function openPromptPicker() {
  document.getElementById("promptPicker").style.display = "flex";
  state.ui.ppkCat = "all"; state.ui.ppkTag = "";
  document.getElementById("ppkKw").value = "";
  document.getElementById("ppkTag").value = "";
  // 先填 tags 下拉
  try {
    const tags = await api("/api/prompts/tags");
    const sel = document.getElementById("ppkTag");
    sel.innerHTML = `<option value="">🏷️ 全部标签</option>` +
      tags.map(t => `<option value="${escapeHtml(t)}">#${escapeHtml(t)}</option>`).join("");
  } catch (e) { /* ignore */ }
  renderPromptPicker();
}
function closePromptPicker() {
  document.getElementById("promptPicker").style.display = "none";
}
async function renderPromptPicker() {
  const kw = (document.getElementById("ppkKw").value || "").trim();
  const tag = state.ui.ppkTag;
  const cat = state.ui.ppkCat === "all" ? "" : state.ui.ppkCat;
  const listEl = document.getElementById("ppkList");
  try {
    const list = await api(`/api/prompts?category=${encodeURIComponent(cat)}&tag=${encodeURIComponent(tag)}&keyword=${encodeURIComponent(kw)}`);
    if (!list || !list.length) {
      listEl.innerHTML = `<div class="pc-empty">暂无匹配的 Prompt。<br>可以：在底部输入修改要求 → 点击「💾 存模板」保存；或到设置 → Prompt仓库 新建。</div>`;
      return;
    }
    listEl.innerHTML = list.map(p => {
      const tags = (p.tags || []).map(t => `<span class="pc-tag">#${escapeHtml(t)}</span>`).join("");
      return `
      <div class="prompt-card" data-id="${p.id}">
        <div class="pc-head">
          <div class="pc-title">${escapeHtml(p.title)}</div>
          <span class="pc-cat cat-${p.category || 'instruction'}">${CAT_LABEL[p.category] || '📝'}</span>
        </div>
        <div class="pc-content">${escapeHtml(p.content || '')}</div>
        ${tags ? `<div class="pc-tags">${tags}</div>` : ''}
        <div class="pc-foot">
          <div class="pc-note">${escapeHtml(p.note || '—')}</div>
          <div class="pc-actions">
            <button class="btn btn-xs btn-default" data-act="overwrite">📌 覆盖</button>
            <button class="btn btn-xs btn-primary" data-act="append">➕ 追加到末尾</button>
          </div>
        </div>
      </div>`;
    }).join("");
    listEl.querySelectorAll(".prompt-card").forEach(card => {
      const id = card.dataset.id;
      const p = list.find(x => x.id === id);
      card.querySelector('[data-act="overwrite"]').onclick = () => {
        if (confirm(`将「${p.title}」直接覆盖写入对应输入框（不保留当前内容）？`)) {
          applyPromptToForm(p, true); closePromptPicker();
        } else {
          applyPromptToForm(p, true); closePromptPicker();
        }
      };
      card.querySelector('[data-act="append"]').onclick = () => { applyPromptToForm(p, false); closePromptPicker(); };
    });
  } catch (e) {
    listEl.innerHTML = `<div class="pc-empty" style="color:var(--danger)">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

// --- 把 Prompt 写入对应输入框 ---
function applyPromptToForm(p, overwrite) {
  const cat = p.category || "instruction";
  let targetId = "instruction";
  let label = "修改要求";
  if (cat === "global") { targetId = "globalPrompt"; label = "全局修改指令"; }
  else if (cat === "agent") { targetId = "customAgent"; label = "自定义 Writer Agent"; }
  const el = document.getElementById(targetId);
  if (!el) return;
  if (overwrite) {
    el.value = p.content || "";
  } else {
    el.value = (el.value ? el.value + "\n" : "") + (p.content || "");
  }
  toast(`✅ 已${overwrite ? "覆盖写入" : "追加"}到「${label}」`, "success");
  // 同步到设置弹窗的 Prompt 列表
  renderPromptsInSettings();
}

// ============================================================
// 新增：按小说区分的 Agent 设置自动保存/读取 + 槽位管理 + 删除小说
// ============================================================
async function deleteNovel() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  const title = state.novelMeta ? state.novelMeta.title : state.novelId;
  if (!confirm(`确定要删除小说「${title}」吗？\n\n这将删除该小说的所有章节、修改和向量记忆，操作不可恢复！`)) return;
  try {
    const r = await api(`/api/novels/${state.novelId}`, { method: "DELETE" });
    if (r.ok) {
      toast(`已删除「${title}」`, "success");
      state.novelId = null; state.novelMeta = null;
      state.toc = []; state.filteredToc = [];
      state.chapterIdx = null; state.chapter = null; state.cards = [];
      refreshNovelList();
      document.getElementById("novelMeta").innerHTML = "";
      document.getElementById("tocList").innerHTML = "";
      document.getElementById("curChapterTitle").textContent = "";
      document.getElementById("globalPrompt").value = "";
      document.getElementById("customAgent").value = "";
    } else {
      toast("删除失败", "error");
    }
  } catch (e) { toast("删除异常：" + e.message, "error"); }
}

function _agentCfg() {
  if (!state.agent) state.agent = { style_blocks: [], slots: [], worldbook_model: {}, bulk_model: {} };
  return state.agent;
}

async function loadAgentSettings() {
  // 全局部分：文风块 / 槽位 / 世界书模型 / 批量修改模型（跨小说共享）
  try {
    const r = await api("/api/agent_config", { method: "GET" });
    const cfg = r.config || {};
    state.agent = {
      style_blocks: cfg.style_blocks || [],
      slots: cfg.slots || [],
      worldbook_model: cfg.worldbook_model || {},
      bulk_model: cfg.bulk_model || {}
    };
    renderStyleBlocks();
    updateSlotSelectors();
    renderWorldbookModelSel();
    renderBulkModelSel();
  } catch (e) {
    state.agent = { style_blocks: [], slots: [], worldbook_model: {}, bulk_model: {} };
    renderStyleBlocks();
    updateSlotSelectors();
  }
  state._agentDirty = false;
  // per-novel 部分：全局指令 / 自定义Agent / 最近修改要求（每本小说独立）
  if (state.novelId) {
    try {
      const s = await api(`/api/novels/${state.novelId}/agent_settings`, { method: "GET" });
      document.getElementById("globalPrompt").value = s.global_prompt || "";
      document.getElementById("customAgent").value = s.custom_agent || "";
      if (s.last_instruction) document.getElementById("instruction").value = s.last_instruction;
    } catch (e) {}
  }
}

async function autoSaveAgentSettings() {
  // per-novel：全局指令 / 自定义Agent / 最近修改要求（每本小说独立）
  if (state.novelId) {
    try {
      await api(`/api/novels/${state.novelId}/agent_settings`, {
        method: "POST",
        body: {
          global_prompt: document.getElementById("globalPrompt").value,
          custom_agent: document.getElementById("customAgent").value,
          last_instruction: document.getElementById("instruction").value
        }
      });
    } catch (e) { /* 静默失败 */ }
  }
  // 全局：文风块 / 槽位 / 世界书模型（跨小说共享）
  const cfg = _agentCfg();
  try {
    const r = await api("/api/agent_config", { method: "POST", body: cfg });
    if (r.ok) { state.agent = r.config; state._agentDirty = false; showAutoSaveIndicator(); }
  } catch (e) { /* 静默失败 */ }
  // 标记 dirty 以便下次自动保存
  state._agentDirty = true;
}

function showAutoSaveIndicator() {
  document.querySelectorAll(".auto-save-indicator").forEach(el => { el.style.display = "inline"; });
  clearTimeout(window.__autosave_indicator);
  window.__autosave_indicator = setTimeout(() => {
    document.querySelectorAll(".auto-save-indicator").forEach(el => { el.style.display = "none"; });
  }, 1500);
}

function updateSlotSelectors() {
  const slots = (state.agent && state.agent.slots) || [];
  document.querySelectorAll(".slot-load").forEach(sel => {
    const field = sel.dataset.field;
    const fieldSlots = slots.filter(s => s.field === field);
    sel.innerHTML = `<option value="">📂 读取已存（${fieldSlots.length}个）...</option>` +
      fieldSlots.map((s, i) => `<option value="${i}">${escapeHtml(s.label || '槽位' + (i + 1))} (${new Date(s.saved_at * 1000).toLocaleDateString('zh-CN')})</option>`).join("");
  });
}

async function saveAgentSlot(field) {
  const fieldMap = { global: "globalPrompt", agent: "customAgent" };
  const elId = fieldMap[field];
  if (!elId) return;
  const content = document.getElementById(elId).value.trim();
  if (!content) return toast("当前输入区为空，无法保存到槽位", "warn");
  const label = prompt("给这个槽位起个名字（可选）：", `快照 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`);
  if (label === null) return;
  const cfg = _agentCfg();
  cfg.slots.push({
    id: "slot_" + Date.now(),
    field: field,
    content: content,
    label: label || `槽位 ${Date.now() % 100000}`,
    saved_at: Math.floor(Date.now() / 1000)
  });
  // 最多保留50个槽位（全局）
  if (cfg.slots.length > 50) cfg.slots = cfg.slots.slice(-50);
  try {
    const r = await api("/api/agent_config", { method: "POST", body: cfg });
    state.agent = r.config;
    updateSlotSelectors();
    toast(`✅ 已保存到「${label}」（全局跨小说）`, "success");
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}

async function loadAgentSlot(field, idx) {
  const slots = (state.agent && state.agent.slots) || [];
  const fieldSlots = slots.filter(s => s.field === field);
  if (idx < 0 || idx >= fieldSlots.length) return;
  const slot = fieldSlots[idx];
  const fieldMap = { global: "globalPrompt", agent: "customAgent" };
  const elId = fieldMap[field];
  if (!elId) return;
  if (confirm(`将「${slot.label}」的内容恢复到此输入区？（当前内容将被覆盖）`)) {
    document.getElementById(elId).value = slot.content;
    toast(`✅ 已恢复「${slot.label}」`, "success");
    autoSaveAgentSettings();
  }
}

// ---- 文风要求分块（多个、可开关、同级生效、全局跨小说） ----
function renderStyleBlocks() {
  const cfg = _agentCfg();
  const wrap = document.getElementById("styleBlocks");
  if (!wrap) return;
  if (!cfg.style_blocks.length) {
    wrap.innerHTML = `<div style="font-size:11.5px;color:var(--muted)">暂无文风块，点击「➕ 新增文风块」添加；启用的分块将在每次修改时自动附加。</div>`;
    return;
  }
  wrap.innerHTML = cfg.style_blocks.map((b, i) => `
    <div data-sb="${escapeHtml(b.id)}" style="display:flex;align-items:center;gap:6px;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg2, #fafafa)">
      <label title="启用/停用" style="display:flex;align-items:center;cursor:pointer;flex-shrink:0">
        <input type="checkbox" data-sb-toggle ${b.enabled ? "checked" : ""}>
      </label>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:600;color:var(--text)">${escapeHtml(b.title || ('文风块' + (i + 1)))} ${b.enabled ? "" : "<span style='color:var(--muted);font-weight:400'>(停用)</span>"}</div>
        <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((b.content || '').slice(0, 60))}</div>
      </div>
      <button class="btn btn-xs btn-default" data-sb-edit title="编辑">✏️</button>
      <button class="btn btn-xs btn-default" data-sb-del title="删除" style="color:var(--danger)">🗑️</button>
    </div>`).join("");
}

function addStyleBlock() { openSlotEdit({ mode: "style-add", id: null }); }
function editStyleBlock(id) {
  const b = ((state.agent && state.agent.style_blocks) || []).find(x => x.id === id);
  if (!b) return;
  openSlotEdit({ mode: "style-edit", id, title: b.title, content: b.content });
}
function toggleStyleBlock(id) {
  const cfg = _agentCfg();
  const b = cfg.style_blocks.find(x => x.id === id);
  if (b) { b.enabled = !b.enabled; saveAgentConfigQuiet(cfg); }
}
async function deleteStyleBlock(id) {
  const cfg = _agentCfg();
  const b = cfg.style_blocks.find(x => x.id === id);
  if (!b) return;
  if (!confirm(`删除文风块「${b.title || '未命名'}」？`)) return;
  cfg.style_blocks = cfg.style_blocks.filter(x => x.id !== id);
  try {
    const r = await api("/api/agent_config", { method: "POST", body: cfg });
    state.agent = r.config;
    renderStyleBlocks();
    toast("已删除文风块", "success");
  } catch (e) { toast("删除失败：" + e.message, "error"); }
}
async function saveAgentConfigQuiet(cfg) {
  try {
    const r = await api("/api/agent_config", { method: "POST", body: cfg });
    state.agent = r.config;
    renderStyleBlocks();
  } catch (e) {}
}

// ---- 槽位管理（编辑 / 删除） ----
function toggleSlotManage(field) {
  const list = document.getElementById("slotManageList");
  if (list.dataset.field === field && list.style.display !== "none") {
    list.style.display = "none";
    return;
  }
  renderSlotManageList(field);
  list.dataset.field = field;
  list.style.display = "block";
}
function renderSlotManageList(field) {
  const list = document.getElementById("slotManageList");
  const slots = ((state.agent && state.agent.slots) || []).filter(s => s.field === field);
  const labels = { global: "全局修改指令", agent: "自定义 Writer Agent" };
  if (!slots.length) {
    list.innerHTML = `<div style="font-size:11.5px;color:var(--muted)">「${labels[field] || field}」暂无已存槽位。</div>`;
    return;
  }
  list.innerHTML = `<div style="font-size:11.5px;color:var(--muted);margin-bottom:4px">「${labels[field] || field}」槽位管理（点击编辑/删除）：</div>` +
    slots.map((s, i) => `
      <div style="display:flex;align-items:center;gap:6px;padding:4px 6px;border:1px solid var(--border);border-radius:6px;margin-bottom:4px">
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;font-weight:600">${escapeHtml(s.label || ('槽位' + (i + 1)))}</div>
          <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((s.content || '').slice(0, 50))}</div>
        </div>
        <button class="btn btn-xs btn-default" onclick="editAgentSlot('${escapeHtml(s.id)}')">✏️</button>
        <button class="btn btn-xs btn-default" onclick="deleteAgentSlot('${escapeHtml(s.id)}')" style="color:var(--danger)">🗑️</button>
      </div>`).join("");
}
function editAgentSlot(sid) {
  const s = ((state.agent && state.agent.slots) || []).find(x => x.id === sid);
  if (!s) return;
  openSlotEdit({ mode: "slot-edit", id: sid, title: s.label, content: s.content });
}
async function deleteAgentSlot(sid) {
  const cfg = _agentCfg();
  const s = cfg.slots.find(x => x.id === sid);
  if (!s) return;
  if (!confirm(`删除槽位「${s.label || '未命名'}」？`)) return;
  cfg.slots = cfg.slots.filter(x => x.id !== sid);
  try {
    const r = await api("/api/agent_config", { method: "POST", body: cfg });
    state.agent = r.config;
    updateSlotSelectors();
    const list = document.getElementById("slotManageList");
    if (list.style.display !== "none") renderSlotManageList(list.dataset.field);
    toast("已删除槽位", "success");
  } catch (e) { toast("删除失败：" + e.message, "error"); }
}

// ---- 通用编辑弹窗（文风块 / 槽位） ----
let _slotEditState = null;
function openSlotEdit(st) {
  _slotEditState = st;
  document.getElementById("slotEditModalTitle").textContent =
    st.mode === "style-add" ? "➕ 新增文风块" : st.mode === "style-edit" ? "✏️ 编辑文风块" : "✏️ 编辑槽位";
  document.getElementById("slotEditTitle").value = st.title || "";
  document.getElementById("slotEditContent").value = st.content || "";
  document.getElementById("slotEditModal").style.display = "flex";
}
function closeSlotEdit() {
  document.getElementById("slotEditModal").style.display = "none";
  _slotEditState = null;
}
async function confirmSlotEdit() {
  if (!_slotEditState) return;
  const title = document.getElementById("slotEditTitle").value.trim();
  const content = document.getElementById("slotEditContent").value.trim();
  if (!content) return toast("内容不能为空", "warn");
  const cfg = _agentCfg();
  if (_slotEditState.mode === "style-add") {
    cfg.style_blocks.push({ id: "sb_" + Date.now(), title: title || ("文风块" + (cfg.style_blocks.length + 1)), content, enabled: true });
  } else if (_slotEditState.mode === "style-edit") {
    const b = cfg.style_blocks.find(x => x.id === _slotEditState.id);
    if (b) { b.title = title || b.title; b.content = content; }
  } else if (_slotEditState.mode === "slot-edit") {
    const s = cfg.slots.find(x => x.id === _slotEditState.id);
    if (s) { s.label = title || s.label; s.content = content; }
  }
  try {
    const r = await api("/api/agent_config", { method: "POST", body: cfg });
    state.agent = r.config;
    renderStyleBlocks();
    updateSlotSelectors();
    closeSlotEdit();
    toast("✅ 已保存（全局跨小说）", "success");
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}

// ---- 拼装启用的文风要求（注入到修改请求，多个分块同级生效） ----
function buildStyleInstruction() {
  const blocks = ((state.agent && state.agent.style_blocks) || []).filter(b => b.enabled);
  if (!blocks.length) return "";
  return blocks.map((b, i) => `【文风要求${i + 1}】${b.title ? `《${b.title}》` : ""}\n${b.content}`).join("\n\n---\n\n");
}

// ---- 模型下拉（多模型协作：世界书生成 / 批量修改正文 可选用与单章修改不同的模型） ----
function _renderModelSel(selId, saved) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  saved = saved || {};
  api("/api/ai/model_options", { method: "GET" }).then(r => {
    if (!r || !r.ok) return;
    const curLabel = r.provider === "external" ? "外部API" : "本地Ollama";
    let html = `<option value="">沿用当前模型（${curLabel}）</option>`;
    (r.local_models || []).forEach(m => {
      html += `<option value="local:${m}" ${saved.provider === "local" && saved.model === m ? "selected" : ""}>本地 Ollama · ${m}</option>`;
    });
    (r.external_slots || []).forEach(s => {
      html += `<option value="external:${s.slot}" ${saved.provider === "external" && saved.slot === s.slot ? "selected" : ""}>外部 API · ${s.label}（${s.model}）</option>`;
    });
    sel.innerHTML = html;
  }).catch(() => {});
}
function _modelOverrideFromSel(selId) {
  const sel = document.getElementById(selId);
  if (!sel || !sel.value) return {};
  const idx = sel.value.indexOf(":");
  if (idx < 0) return {};
  const provider = sel.value.slice(0, idx);
  const target = sel.value.slice(idx + 1);
  if (provider === "local") return { provider: "local", model: target };
  if (provider === "external") return { provider: "external", slot: target };
  return {};
}
function renderWorldbookModelSel() {
  _renderModelSel("wbModelSel", (state.agent && state.agent.worldbook_model) || {});
}
function worldbookModelOverride() {
  return _modelOverrideFromSel("wbModelSel");
}
function renderBulkModelSel() {
  _renderModelSel("bulkModelSel", (state.agent && state.agent.bulk_model) || {});
}
function bulkModelOverride() {
  return _modelOverrideFromSel("bulkModelSel");
}

// ============================================================
// 新增三需求：读书模式 · 外部API · 设定摘要
// ============================================================

// ====== 读书模式 ======
function toggleStudyChapterSelect() {
  state.study.showChSel = !state.study.showChSel;
  const panel = document.getElementById("studyChSel");
  const tocList = document.getElementById("tocList");
  panel.style.display = state.study.showChSel ? "block" : "none";
  if (state.study.showChSel) {
    tocList.classList.add("study-sel-mode");
    // 在目录中显示复选框
    document.querySelectorAll(".toc-item").forEach(item => {
      if (!item.querySelector(".study-cb")) {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "study-cb";
        cb.dataset.idx = item.querySelector(".idx") ? item.querySelector(".idx").textContent.trim() : "";
        // 如果已有选择，恢复勾选状态
        if (state.study.selectedChapters && state.study.selectedChapters.length > 0) {
          const idx = parseInt(cb.dataset.idx);
          cb.checked = state.study.selectedChapters.includes(idx);
        } else {
          cb.checked = true;
        }
        item.prepend(cb);
      }
      item.querySelector(".study-cb").style.display = "inline";
    });
  } else {
    tocList.classList.remove("study-sel-mode");
    document.querySelectorAll(".study-cb").forEach(cb => cb.style.display = "none");
  }
}

function studySelectAllChapters(select) {
  document.querySelectorAll(".study-cb").forEach(cb => {
    cb.checked = select;
  });
}

function confirmStudyChapterSelect() {
  const selected = [];
  document.querySelectorAll(".study-cb:checked").forEach(cb => {
    const idx = parseInt(cb.dataset.idx);
    if (!isNaN(idx)) selected.push(idx);
  });
  state.study.selectedChapters = selected.length > 0 ? selected : null;
  state.study.showChSel = false;
  document.getElementById("studyChSel").style.display = "none";
  document.querySelectorAll(".study-cb").forEach(cb => cb.style.display = "none");
  const hint = selected.length > 0 ? `已选取 ${selected.length} 章` : "全部章节";
  document.getElementById("studyInfo").textContent = hint;
  toast(`读书范围：${hint}`, "success");
}

async function startStudy() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  if (state.study.running) return toast("读书已在运行中", "warn");

  try {
    const cats = Array.from(document.querySelectorAll(".study-cat:checked")).map(c => c.value);
    const payload = { chapters: state.study.selectedChapters, template: state.wbTemplate, categories: cats, model_override: worldbookModelOverride() };
    const r = await api(`/api/novels/${state.novelId}/study/start`, { method: "POST", body: payload });
    if (!r.ok) {
      if (r.message) { toast(r.message, "warn"); return; }
      throw new Error("启动失败");
    }
    state.study.running = true;
    document.getElementById("btnStudyStart").disabled = true;
    document.getElementById("btnStudyStop").disabled = false;
    document.getElementById("studyProgress").style.display = "block";
    document.getElementById("studyInfo").textContent = "启动中...";
    toast(`小说转世界书已启动：AI 将分批阅读（每批累积 ≥1 万字）并按所选分类提取设定（${cats.length} 类）`, "success");
    // 开始轮询进度
    pollStudyStatus();
  } catch (e) { toast("启动读书失败：" + e.message, "error"); }
}

async function stopStudy() {
  if (!state.novelId) return;
  try {
    await api(`/api/novels/${state.novelId}/study/stop`, { method: "POST" });
    state.study.running = false;
    document.getElementById("btnStudyStart").disabled = false;
    document.getElementById("btnStudyStop").disabled = true;
    document.getElementById("studyInfo").textContent = "已停止";
    clearTimeout(state.study.pollTimer);
    toast("已发送停止信号，当前章节完成后将停止", "warn");
  } catch (e) { toast("停止失败：" + e.message, "error"); }
}

function pollStudyStatus() {
  if (!state.study.running) return;
  api(`/api/novels/${state.novelId}/study/status`)
    .then(r => {
      if (!r.running || !r.progress) {
        // 已完成或已停止
        state.study.running = false;
        document.getElementById("btnStudyStart").disabled = false;
        document.getElementById("btnStudyStop").disabled = true;
        const p = r.progress;
        if (p && p.status === "done") {
          updateStudyProgress(p);
          document.getElementById("studyInfo").textContent = `完成！已阅读 ${p.total} 章，共总结 ${p.batches_done || 0} 批`;
          toast("小说转世界书完成！世界书设定已生成/更新", "success");
          // 自动加载设定摘要
          loadSettingsSummary();
        } else if (p && p.status === "stopped") {
          document.getElementById("studyInfo").textContent = `已停止（${p.done}/${p.total}）`;
        } else if (p && p.status === "error") {
          document.getElementById("studyInfo").textContent = "出错：" + (p.error || "未知错误");
        }
        return;
      }
      updateStudyProgress(r.progress);
      // 继续轮询
      state.study.pollTimer = setTimeout(pollStudyStatus, 1500);
    })
    .catch(() => {
      state.study.pollTimer = setTimeout(pollStudyStatus, 2000);
    });
}

function updateStudyProgress(p) {
  if (!p) return;
  const total = p.total || 0;
  const done = p.done || 0;
  const pct = total > 0 ? Math.round(done / total * 100) : 0;
  document.getElementById("studyBar").style.width = pct + "%";
  document.getElementById("studyBarText").textContent = pct + "%";
  let info = `${done} / ${total} 章`;
  if (p.batches_done) info += ` | 已总结 ${p.batches_done} 批`;
  const results = p.results || [];
  if (results.length) {
    const last = results[results.length - 1];
    if (last.ok) info += ` | 最近批次 +${last.added} 条`;
    else if (last.error) info += ` | 最近批次失败：${String(last.error).slice(0, 30)}`;
  }
  if (p.last_chapter && p.last_title) {
    info += ` | 当前：第${p.last_chapter}章《${p.last_title}》`;
  } else if (p.current) {
    info += ` | 正在读第${p.current}章...`;
  }
  document.getElementById("studyInfo").textContent = info;
  document.getElementById("studyProgress").style.display = "block";
}

// ====== 世界书（Worldbook）设定：关键词触发读取 ======
let _wbEditId = null;

async function loadSettingsSummary() {
  if (!state.novelId) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook`);
    if (!r.ok || !r.book) { renderWorldbookEmpty(); return; }
    state.wb = r.book;
    renderWorldbook();
  } catch (e) {
    document.getElementById("settingsSummaryBox").innerHTML = `
      <div class="ss-empty" style="color:var(--danger)">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

// ====== 世界书关键词定向总结（模式二）：全文检索相关片段 → AI 总结该实体设定写入世界书 ======
async function summarizeWorldbookKeyword() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  const kw = (document.getElementById("wbKwInput").value || "").trim();
  if (!kw) return toast("请输入要总结的实体名/关键词", "warn");
  const btn = document.getElementById("btnWbKw");
  btn.disabled = true;
  btn.textContent = "⏳ 检索并总结中...";
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook/summarize_keyword`, {
      method: "POST", body: { keyword: kw, template: state.wbTemplate, model_override: worldbookModelOverride() }
    });
    if (!r.ok) { toast(r.error || "总结失败", "error"); return; }
    if (r.added) toast(`✅ 已总结「${kw}」相关设定 ${r.added} 条并写入世界书`, "success");
    else toast(r.message || `「${kw}」未产生新条目（可能已存在或未检索到）`, "warn");
    loadSettingsSummary();
  } catch (e) {
    toast("总结失败：" + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "🧠 总结该设定";
  }
}

function renderWorldbookEmpty() {
  document.getElementById("settingsSummaryBox").innerHTML = `
    <div class="ss-empty">
      <div class="ss-icon">📖</div>
      <div class="ss-title">世界书暂无条目</div>
      <div class="ss-tip">用「读书模式」或「批量修改」让 AI 自动提取设定，或点击「＋ 添加条目」手动创建。</div>
      <div style="margin-top:10px;display:flex;gap:6px;justify-content:center">
        <button class="btn btn-sm btn-primary" onclick="loadSettingsSummary()">🔄 刷新</button>
        <button class="btn btn-sm btn-default" onclick="addWorldbookEntry()">＋ 添加条目</button>
      </div>
    </div>`;
}

function renderWorldbook() {
  const book = state.wb || { entries: [] };
  const entries = book.entries || [];
  const lastCh = book.last_chapter ? ` <span class="ss-last">已读至第${book.last_chapter}章</span>` : "";
  let html = `<div class="ss-head">📖 世界书设定（关键词触发）${lastCh}
    <button class="btn btn-xs btn-warn" style="float:right" onclick="clearWorldbook()">🗑 清空</button>
    <button class="btn btn-xs btn-default" style="float:right;margin-right:4px" onclick="dedupeWorldbook()">🧹 整理</button>
    <button class="btn btn-xs btn-default" style="float:right;margin-right:4px" onclick="loadSettingsSummary()">🔄</button>
  </div>
  <div class="wb-tip">AI 修改内容时，正文出现「触发词」才会读取该条设定。可编辑触发词与内容。</div>
  <div class="wb-test">
    <input id="wbTestInput" type="text" placeholder="输入文段，测试哪些条目会被触发读取…" />
    <button class="btn btn-xs btn-primary" onclick="testWorldbook()">🔍 测试读取</button>
  </div>
  <div id="wbTestResult"></div>
  <div class="wb-actions">
    <button class="btn btn-xs btn-default" onclick="addWorldbookEntry()">＋ 添加条目</button>
  </div>
  <div class="wb-list">`;
  if (!entries.length) {
    html += `<div class="ss-empty" style="padding:12px">暂无条目，点击上方「＋ 添加条目」或让 AI 自动提取。</div>`;
  } else {
    const groups = {};
    const CAT_ORDER = ["角色", "种族", "物品", "世界观", "剧情", "关系", "其他"];
    entries.forEach(e => {
      const g = e.category || "其他";
      (groups[g] = groups[g] || []).push(e);
    });
    CAT_ORDER.forEach(g => {
      const list = groups[g];
      if (!list || !list.length) return;
      html += `<div class="ss-section"><div class="ss-sec-title">${g} (${list.length})</div>`;
      list.forEach(e => {
        const keys = (e.keys || []).map(k => `<span class="ss-chip">${escapeHtml(k)}</span>`).join("");
        html += `<div class="wb-item">
          <div class="wb-row">
            <span class="wb-name">${escapeHtml(e.name || "?")}</span>
            <span>
              <button class="btn btn-xs btn-default" onclick="editWorldbookEntry('${e.id}')">✏️ 编辑</button>
              <button class="btn btn-xs btn-warn" onclick="delWorldbookEntry('${e.id}')">🗑</button>
            </span>
          </div>
          <div class="wb-keys">触发词：${keys || '<span style="color:var(--warn)">未设置</span>'}</div>
          <div class="wb-content">${escapeHtml((e.content || "").slice(0, 90))}${(e.content || "").length > 90 ? "…" : ""}</div>
        </div>`;
      });
      html += `</div>`;
    });
  }
  html += `</div>`;
  document.getElementById("settingsSummaryBox").innerHTML = html;
}

function addWorldbookEntry() {
  _wbEditId = null;
  document.getElementById("wbModalName").value = "";
  document.getElementById("wbModalCat").value = "角色";
  document.getElementById("wbModalKeys").value = "";
  document.getElementById("wbModalContent").value = "";
  document.getElementById("wbModalFirst").value = "";
  document.getElementById("wbModal").style.display = "flex";
}

function editWorldbookEntry(id) {
  const e = ((state.wb || {}).entries || []).find(x => x.id === id);
  if (!e) return;
  _wbEditId = id;
  document.getElementById("wbModalName").value = e.name || "";
  document.getElementById("wbModalCat").value = e.category || "其他";
  document.getElementById("wbModalKeys").value = (e.keys || []).join("、");
  document.getElementById("wbModalContent").value = e.content || "";
  document.getElementById("wbModalFirst").value = e.first_appearance || "";
  document.getElementById("wbModal").style.display = "flex";
}

function closeWorldbookModal() {
  document.getElementById("wbModal").style.display = "none";
}

async function saveWorldbookEntry() {
  const entry = {
    id: _wbEditId || undefined,
    name: document.getElementById("wbModalName").value.trim(),
    category: document.getElementById("wbModalCat").value,
    keys: document.getElementById("wbModalKeys").value.split(/[、,，;；]/).map(s => s.trim()).filter(Boolean),
    content: document.getElementById("wbModalContent").value,
    first_appearance: document.getElementById("wbModalFirst").value.trim()
  };
  if (!entry.name) return toast("条目名称不能为空", "warn");
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook/entry`, { method: "PUT", body: { entry } });
    if (r.ok) {
      state.wb = r.book;
      closeWorldbookModal();
      renderWorldbook();
      toast("世界书条目已保存", "success");
    } else toast(r.error || "保存失败", "error");
  } catch (e) { toast("保存失败：" + e.message, "error"); }
}

async function delWorldbookEntry(id) {
  const e = ((state.wb || {}).entries || []).find(x => x.id === id);
  if (!e) return;
  if (!confirm(`删除世界书条目「${e.name}」？`)) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook/entry/${id}`, { method: "DELETE" });
    if (r.ok) { state.wb = r.book; renderWorldbook(); toast("已删除", "success"); }
    else toast(r.error || "删除失败", "error");
  } catch (e2) { toast("删除失败：" + e2.message, "error"); }
}

async function dedupeWorldbook() {
  if (!state.novelId) return;
  if (!confirm("对当前世界书执行整理去重？\n\n将自动合并同一角色的重复/细分条目（如 康桥（哥斯拉）、康桥的岩浆浴 → 康桥），并清理意义不明的触发词。")) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook/dedupe`, { method: "POST" });
    if (r.ok) {
      state.wb = r.book;
      renderWorldbook();
      toast(r.removed > 0 ? `已合并 ${r.removed} 个重复条目` : "没有需要合并的条目", r.removed > 0 ? "success" : "warn");
    } else toast(r.error || "整理失败", "error");
  } catch (e) { toast("整理失败：" + e.message, "error"); }
}

async function clearWorldbook() {
  if (!state.novelId) return;
  if (!confirm("确定清空这本小说的全部世界书条目吗？此操作不可恢复。")) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook`, { method: "DELETE" });
    if (r.ok) {
      state.wb = { entries: [], last_chapter: 0 };
      renderWorldbookEmpty();
      toast("世界书已清空", "success");
    } else toast(r.error || "清空失败", "error");
  } catch (e) { toast("清空失败：" + e.message, "error"); }
}

async function testWorldbook() {
  const text = (document.getElementById("wbTestInput")?.value || "").trim();
  if (!text) return toast("请输入测试文本", "warn");
  const el = document.getElementById("wbTestResult");
  el.innerHTML = `<div style="color:var(--muted);font-size:12px">🔍 测试中...</div>`;
  try {
    const r = await api(`/api/novels/${state.novelId}/worldbook/test`, { method: "POST", body: { text } });
    if (!r.ok) { el.innerHTML = `<div style="color:var(--danger);font-size:12px">测试失败：${escapeHtml(r.error || "")}</div>`; return; }
    if (!r.hit_count) { el.innerHTML = `<div class="wb-test-none">未命中任何条目（检查触发词是否出现在文本中）</div>`; return; }
    el.innerHTML = r.hit.map(h =>
      `<div class="wb-test-hit">✅ [${escapeHtml(h.category)}] ${escapeHtml(h.name)}（触发词：${(h.keys || []).map(escapeHtml).join("、")}）</div>`
    ).join("");
  } catch (e) { el.innerHTML = `<div style="color:var(--danger);font-size:12px">测试失败：${escapeHtml(e.message)}</div>`; }
}

// ====== AI 供应商切换 ======
function updateProviderToggleUI(provider) {
  state.aiProvider = provider;
  document.getElementById("btnProviderLocal").classList.toggle("active", provider === "local");
  document.getElementById("btnProviderExternal").classList.toggle("active", provider === "external");
}

async function switchAIProvider(provider) {
  if (provider === state.aiProvider) return;
  try {
    await api("/api/ai/provider", { method: "POST", body: { provider } });
    updateProviderToggleUI(provider);
    const label = provider === "external" ? "外部API" : "本地Ollama";
    toast(`已切换至 ${label}`, "success");
    // 如果切换到外部API，检查连接
    if (provider === "external") {
      setTimeout(() => checkExternalConnectionSilent(), 500);
    } else {
      checkOllama();
    }
  } catch (e) { toast("切换失败：" + e.message, "error"); }
}

// ====== 外部 API ======
async function testExternalConnection() {
  const st = document.getElementById("extConnStatus");
  st.textContent = "测试中...";
  st.className = "conn-st";
  try {
    // 先保存当前填写的配置
    await saveExternalConfigSilent();
    const r = await api("/api/external/check");
    if (r.ok) {
      const note = r.note ? `<br><span style="font-size:11px">${escapeHtml(r.note)}</span>` : "";
      st.innerHTML = `✅ 连接成功！槽位「${escapeHtml(r.active_slot || "default")}」${r.models ? ` 可用模型：${r.models.length} 个` : ""} | ${r.latency_ms || "?"}ms${note}`;
      st.className = "conn-st ok";
      toast("外部API连接成功！", "success");
    } else {
      st.textContent = "❌ " + (r.error || "连接失败");
      st.className = "conn-st err";
    }
  } catch (e) {
    st.textContent = "❌ " + e.message;
    st.className = "conn-st err";
  }
}

async function checkExternalConnectionSilent() {
  try {
    const r = await api("/api/external/check");
    const pill = document.getElementById("statusPill");
    const txt = document.getElementById("statusText");
    if (!pill || !txt) return;
    pill.classList.remove("ok");
    if (r.ok) {
      txt.textContent = `外部API已连接[${r.active_slot || "default"}] · ${r.latency_ms || "?"}ms`;
      pill.classList.add("ok");
    } else {
      txt.textContent = "外部API异常: " + (r.error || "未知");
    }
  } catch (e) {
    const t = document.getElementById("statusText");
    if (t) t.textContent = "外部API未配置";
  }
}

async function listExternalModels() {
  const listEl = document.getElementById("extModelList");
  listEl.innerHTML = `<div class="ml-empty">查询中...</div>`;
  try {
    await saveExternalConfigSilent();
    const r = await api("/api/external/models");
    if (r.ok && r.models && r.models.length) {
      listEl.innerHTML = r.models.map(m => `
        <div class="m-item">🌐 <strong>${escapeHtml(m.id || m.name || "?")}</strong>
        <span style="color:var(--muted);margin-left:8px">${escapeHtml(m.owned_by || "")}</span></div>`).join("");
    } else {
      listEl.innerHTML = `<div class="ml-empty" style="color:var(--danger)">⚠️ ${escapeHtml(r.error || "未获取到模型列表")}</div>`;
    }
  } catch (e) {
    listEl.innerHTML = `<div class="ml-empty" style="color:var(--danger)">❌ ${escapeHtml(e.message)}</div>`;
  }
}

async function saveExternalConfigSilent() {
  try {
    const extCfg = {
      enabled: document.getElementById("cfgExtEnabled").checked,
      base_url: document.getElementById("cfgExtBaseUrl").value.trim(),
      api_key: document.getElementById("cfgExtApiKey").value.trim(),
      model: document.getElementById("cfgExtModel").value.trim(),
      timeout: parseInt(document.getElementById("cfgExtTimeout").value) || 120,
      temperature: parseFloat(document.getElementById("cfgExtTemp").value) || 0.7
    };
    await api("/api/config/external", { method: "POST", body: extCfg });
  } catch { /* 静默 */ }
}

// ====== 外部 API 多槽位管理 ======
function renderExtSlotSelect(slots, active) {
  const sel = document.getElementById("cfgExtSlot");
  if (!sel) return;
  sel.innerHTML = "";
  for (const name of Object.keys(slots || {})) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  sel.value = active || "";
}

async function onExtSlotChange() {
  const sel = document.getElementById("cfgExtSlot");
  const name = sel.value;
  if (!name || name === state._extActive) return;
  try {
    await saveExternalConfigSilent();           // 先保存当前表单到原槽位
    await api("/api/config/external", { method: "POST", body: { active_slot: name } });
    await loadSettings();                        // 重新加载当前槽位配置到表单
    toast(`已切换到配置槽位「${name}」`, "success");
  } catch (e) {
    toast("切换槽位失败：" + e.message, "error");
    renderExtSlotSelect(state._extSlots, state._extActive);
  }
}

async function addExtSlot() {
  const name = prompt("输入新槽位名称（用于区分不同 API，如：deepseek-官方 / 方舟-Plan）：");
  if (!name || !name.trim()) return;
  try {
    await saveExternalConfigSilent();           // 先保存当前表单到原槽位
    await api("/api/config/external", { method: "POST", body: { add_slot: name.trim() } });
    await loadSettings();
    toast(`已新建并切换到槽位「${name.trim()}」`, "success");
  } catch (e) {
    toast("新建槽位失败：" + e.message, "error");
  }
}

async function delExtSlot() {
  const sel = document.getElementById("cfgExtSlot");
  const name = sel.value;
  if (!name) return;
  if (!confirm(`删除配置槽位「${name}」？该槽位的 API 配置将丢失。`)) return;
  try {
    const r = await api("/api/config/external", { method: "POST", body: { delete_slot: name } });
    await loadSettings();
    toast(`已删除槽位「${name}」，当前槽位「${r.active_slot}」`, "success");
  } catch (e) {
    toast("删除失败：" + e.message, "error");
  }
}

// ============================================================
// 新增：文本检索（纯代码）
// ============================================================
async function doTextSearch() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  const keyword = document.getElementById("textSearchKw").value.trim();
  if (!keyword) return toast("请输入要检索的词汇", "warn");
  const start = parseInt(document.getElementById("tsStart").value) || null;
  const end = parseInt(document.getElementById("tsEnd").value) || null;
  const el = document.getElementById("textSearchResults");
  el.innerHTML = `<div class="mem-empty">⏳ 正在检索「${escapeHtml(keyword)}」...</div>`;
  try {
    const r = await api(`/api/novels/${state.novelId}/search`, {
      method: "POST",
      body: { keyword, start, end, case_sensitive: false }
    });
    const results = r.results || [];
    if (!results.length) {
      el.innerHTML = `<div class="mem-empty" style="color:var(--muted)">全书${start && end ? `（第${start}-${end}章）` : ""}未找到「${escapeHtml(keyword)}」。</div>`;
      return;
    }
    el.innerHTML = results.map(ch => `
      <div class="mem-item ts-item" data-chapter="${ch.index}">
        <div class="src">第${ch.index}章《${escapeHtml(ch.title)}》· 命中 ${ch.match_count} 处${ch.title_hit ? ' <span style="color:var(--success)">（含标题）</span>' : ""}</div>
        ${ch.matches.slice(0, 2).map(m => `<div class="text">…${escapeHtml(m.context)}…</div>`).join("")}
        ${ch.matches.length > 2 ? `<div class="ts-more">+${ch.matches.length - 2} 处更多命中…</div>` : ""}
      </div>
    `).join("");
    el.querySelectorAll(".ts-item").forEach(it => {
      it.onclick = () => loadChapter(parseInt(it.dataset.chapter));
    });
  } catch (e) {
    el.innerHTML = `<div class="mem-empty" style="color:var(--danger)">检索失败：${escapeHtml(e.message)}</div>`;
  }
}

// ============================================================
// 新增：文本查找替换
// ============================================================
function getReplaceRange() {
  const s = parseInt(document.getElementById("repStart").value);
  const e = parseInt(document.getElementById("repEnd").value);
  if (!isNaN(s) || !isNaN(e)) {
    return { start: isNaN(s) ? null : s, end: isNaN(e) ? null : e };
  }
  return {};
}

function setReplaceRangeAll() {
  if (!state.toc.length) return;
  const minIdx = Math.min(...state.toc.map(c => c.index));
  const maxIdx = Math.max(...state.toc.map(c => c.index));
  document.getElementById("repStart").value = minIdx;
  document.getElementById("repEnd").value = maxIdx;
  toast(`替换范围已设为全部（第${minIdx}-${maxIdx}章）`, "success");
}

async function doReplacePreview() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  const oldText = document.getElementById("repFind").value.trim();
  if (!oldText) return toast("请输入要查找的文本", "warn");
  const el = document.getElementById("repResults");
  el.innerHTML = `<div class="mem-empty">⏳ 正在预览「${escapeHtml(oldText)}」的命中情况...</div>`;
  try {
    const r = await api(`/api/novels/${state.novelId}/replace/preview`, {
      method: "POST", body: { old_text: oldText, ...getReplaceRange() }
    });
    const results = r.results || [];
    if (!results.length) {
      el.innerHTML = `<div class="mem-empty" style="color:var(--muted)">未找到「${escapeHtml(oldText)}」，可尝试扩大章节范围。</div>`;
      return;
    }
    el.innerHTML = `<div class="mem-empty" style="color:var(--text-2);background:var(--primary-l);border-style:solid">
      共命中 <strong>${r.total_replacements}</strong> 处 · ${results.length} 个章节（含标题命中）</div>` +
      results.map(ch => `
        <div class="mem-item" data-chapter="${ch.index}" style="cursor:pointer">
          <div class="src">第${ch.index}章《${escapeHtml(ch.title)}》· <span class="score">正文 ${ch.count} 处${ch.title_count ? ` + 标题 ${ch.title_count} 处` : ""}</span></div>
        </div>
      `).join("");
    el.querySelectorAll(".mem-item").forEach(it => {
      it.onclick = () => loadChapter(parseInt(it.dataset.chapter));
    });
  } catch (e) {
    el.innerHTML = `<div class="mem-empty" style="color:var(--danger)">预览失败：${escapeHtml(e.message)}</div>`;
  }
}

async function doReplaceExec() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  const oldText = document.getElementById("repFind").value;
  if (!oldText.trim()) return toast("请输入要查找的文本", "warn");
  const newText = document.getElementById("repReplace").value;
  const range = getReplaceRange();
  const rangeHint = (range.start || range.end)
    ? `第${range.start || "?"}-${range.end || "?"}章范围`
    : "全书";
  if (!confirm(`执行文本替换：\n\n查找：「${oldText}」\n替换为：「${newText || "（删除）"}」\n范围：${rangeHint}\n\n正文与章节标题中的匹配都会被替换（不会自动加【已修改】标记），替换将直接写入章节并同步向量记忆，且不可撤销（但可再替换回去）。确定继续？`)) return;
  const el = document.getElementById("repResults");
  el.innerHTML = `<div class="mem-empty">⏳ 正在替换并更新向量记忆...</div>`;
  try {
    const r = await api(`/api/novels/${state.novelId}/replace`, {
      method: "POST", body: { old_text: oldText, new_text: newText, ...range }
    });
    el.innerHTML = `<div class="mem-empty" style="color:var(--text-2);background:var(--success-l);border-style:solid;border-color:var(--success)">
      ✅ 替换完成：${r.total_replacements} 处 · ${r.replaced_chapters.length} 个章节<br>
      <span style="font-size:11px">章节：${(r.replaced_chapters || []).join("、") || "无"}</span>
    </div>`;
    toast(`✅ 已替换 ${r.total_replacements} 处（${r.replaced_chapters.length} 章），向量记忆已同步`, "success");
    renderMemStats();
    if (state.chapterIdx && (r.replaced_chapters || []).includes(state.chapterIdx)) {
      loadChapter(state.chapterIdx);
    }
  } catch (e) {
    el.innerHTML = `<div class="mem-empty" style="color:var(--danger)">替换失败：${escapeHtml(e.message)}</div>`;
  }
}

// ============================================================
// 章节范围解析工具 + 读书范围输入
// ============================================================
function parseChapterRangeInput(str, maxIdx) {
  // 支持 "1-50, 60, 70-80" / "1~50" / "3" 等格式，返回去重升序索引数组
  const out = [];
  const parts = String(str || "").split(/[,，;；\s]+/);
  for (const part of parts) {
    const p = part.trim();
    if (!p) continue;
    if (/-|~|—|到/.test(p)) {
      const seg = p.split(/-|~|—|到/);
      const a = parseInt(seg[0]), b = parseInt(seg[seg.length - 1]);
      if (isNaN(a) || isNaN(b)) continue;
      const lo = Math.max(1, Math.min(a, b));
      const hi = Math.min(maxIdx || Infinity, Math.max(a, b));
      for (let i = lo; i <= hi; i++) out.push(i);
    } else {
      const n = parseInt(p);
      if (!isNaN(n) && n >= 1 && (!maxIdx || n <= maxIdx)) out.push(n);
    }
  }
  return [...new Set(out)].sort((a, b) => a - b);
}

function applyStudyRange() {
  const maxIdx = state.toc.length ? Math.max(...state.toc.map(c => c.index)) : 0;
  const sel = parseChapterRangeInput(document.getElementById("studyRangeInput").value, maxIdx);
  if (!sel.length) return toast("请输入有效的章节范围，如：1-50, 60", "warn");
  state.study.selectedChapters = sel;
  // 同步勾选目录复选框
  document.querySelectorAll(".study-cb").forEach(cb => {
    const idx = parseInt(cb.dataset.idx);
    cb.checked = sel.includes(idx);
  });
  document.getElementById("studyInfo").textContent = `已选取 ${sel.length} 章（${sel[0]}-${sel[sel.length - 1]}）`;
  toast(`读书范围已设置为 ${sel.length} 章`, "success");
}

// ============================================================
// 新增：总体设定修改（批量）
// ============================================================
function getBulkRangePayload() {
  const s = parseInt(document.getElementById("bulkRangeStart").value);
  const e = parseInt(document.getElementById("bulkRangeEnd").value);
  if (!isNaN(s) || !isNaN(e)) {
    return { start: isNaN(s) ? null : s, end: isNaN(e) ? null : e };
  }
  return {};
}

function setBulkRangeAll() {
  if (!state.toc.length) return;
  const minIdx = Math.min(...state.toc.map(c => c.index));
  const maxIdx = Math.max(...state.toc.map(c => c.index));
  document.getElementById("bulkRangeStart").value = minIdx;
  document.getElementById("bulkRangeEnd").value = maxIdx;
  toast(`章节范围已设为全部（第${minIdx}-${maxIdx}章）`, "success");
}

function setBulkUI(running) {
  state.bulk.running = running;
  document.getElementById("btnBulkStart").disabled = running;
  document.getElementById("btnBulkStop").disabled = !running;
  const hasPending = (state.bulk.pending || []).length > 0;
  document.getElementById("btnBulkConfirm").disabled = !hasPending;
  document.getElementById("btnBulkDiscard").disabled = !hasPending;
  document.getElementById("bulkProgress").style.display = running ? "block" : "block";
  if (!running && !hasPending) document.getElementById("bulkProgress").style.display = "none";
}

async function startBulkModify() {
  if (!state.novelId) return toast("请先选择一本小说", "warn");
  if (state.bulk.running) return toast("批量修改已在运行中", "warn");
  const mode = (document.querySelector('input[name="bulkMode"]:checked') || {}).value || "keyword";
  const kwStr = document.getElementById("bulkKeywords").value.trim();
  const keywords = kwStr.split(/[,，;；]/).map(s => s.trim()).filter(s => s);
  if (mode !== "all" && !keywords.length) return toast("请输入至少一个关键词（逗号分隔）", "warn");
  const instruction = document.getElementById("bulkInstruction").value.trim();
  if (!instruction) return toast("请填写修改要求", "warn");
  const range = getBulkRangePayload();
  const totalHint = state.toc.length;

  const modeDesc = mode === "all"
    ? `全章节逐章：AI 逐章阅读第${range.start || "1"}-${range.end || totalHint}章后自行判断是否修改${keywords.length ? `（参考关键词：${keywords.join("、")}）` : ""}`
    : `关键词触发：只修改命中「${keywords.join("、")}」的章节`;

  if (!confirm(`将对全书（或指定范围）共 ${totalHint} 章执行批量修改：\n\n${modeDesc}\n修改要求：${instruction.slice(0, 60)}${instruction.length > 60 ? "…" : ""}\n\n修改结果先进入「待确认」，由你决定保留或放弃。确定开始？`)) return;

  try {
    const r = await api(`/api/novels/${state.novelId}/bulk/start`, {
      method: "POST",
      body: {
        keywords, instruction, chapter_range: range, mode,
        template: state.wbTemplate,
        sync_worldbook: document.getElementById("bulkWbSync").checked,
        sync_memory: document.getElementById("bulkMemSync").checked,
        global_prompt: document.getElementById("globalPrompt").value.trim(),
        custom_instruction: document.getElementById("customAgent").value.trim(),
        style_instruction: buildStyleInstruction(),
        wb_model_override: worldbookModelOverride(),
        bulk_model_override: bulkModelOverride()
      }
    });
    if (!r.ok) { toast(r.message || "启动失败", "warn"); return; }
    if (r.message && r.message.includes("运行中")) { toast(r.message, "warn"); return; }
    toast(mode === "all" ? "已启动：AI 正在逐章阅读并判断修改" : "总体设定修改已启动！AI 正在逐章检索与修改", "success");
    setBulkUI(true);
    document.getElementById("bulkInfo").textContent = "启动中...";
    pollBulkStatus();
  } catch (e) { toast("启动失败：" + e.message, "error"); }
}

async function stopBulkModify() {
  if (!state.novelId) return;
  try {
    await api(`/api/novels/${state.novelId}/bulk/stop`, { method: "POST" });
    document.getElementById("bulkInfo").textContent = "已发送停止信号";
    toast("已发送停止信号，当前章节完成后将停止", "warn");
  } catch (e) { toast("停止失败：" + e.message, "error"); }
}

function pollBulkStatus() {
  if (!state.novelId) return;
  api(`/api/novels/${state.novelId}/bulk/status`)
    .then(r => {
      state.bulk.status = r;
      const p = r.progress;
      if (r.running && p) {
        updateBulkProgress(p);
        state.bulk.pollTimer = setTimeout(pollBulkStatus, 1200);
        return;
      }
      // 任务结束
      setBulkUI(false);
      if (p && p.status === "done") {
        updateBulkProgress(p);
        const skipTxt = ((p.skipped || []).length) ? `，AI判定无需修改跳过 ${p.skipped.length} 章` : "";
        document.getElementById("bulkInfo").textContent = `✅ 完成！处理 ${p.hit.length} 章，修改 ${p.modified.length} 章${skipTxt}`;
        toast(`总体设定修改完成：修改 ${p.modified.length} 章（待确认）${skipTxt}`, "success");
      } else if (p && p.status === "stopped") {
        document.getElementById("bulkInfo").textContent = `⏹ 已停止（已修改 ${p.modified.length} 章）`;
      } else if (p && p.status === "error") {
        document.getElementById("bulkInfo").textContent = "❌ " + (p.error || "出错");
      }
      loadBulkPending();
    })
    .catch(() => {
      if (state.bulk.running) state.bulk.pollTimer = setTimeout(pollBulkStatus, 2000);
    });
}

function updateBulkProgress(p) {
  if (!p) return;
  const total = p.total || 0;
  const done = p.done || 0;
  const pct = total > 0 ? Math.round(done / total * 100) : 0;
  document.getElementById("bulkBar").style.width = pct + "%";
  document.getElementById("bulkBarText").textContent = pct + "%";
  let info = `进度 ${done}/${total} 章 · ${p.mode === "all" ? "处理" : "命中"} ${p.hit.length} · 已修改 ${p.modified.length}${(p.skipped || []).length ? ` · 跳过 ${p.skipped.length}` : ""}`;
  const phaseMap = { "reading": "📖 整理设定", "planning": "🧠 预思考方案", "modifying": "✍️ 修改中", "skipped": "⏭️ 已跳过" };
  if (p.phase && p.phase !== "done") {
    const [ph, ci] = String(p.phase).split(":");
    if (phaseMap[ph]) info += ` | ${phaseMap[ph]}（第${ci}章）`;
  } else if (p.last_chapter) {
    info += ` | 当前第${p.last_chapter}章${p.current_keyword ? `（命中：${p.current_keyword}）` : ""}`;
  }
  document.getElementById("bulkInfo").textContent = info;
  document.getElementById("bulkProgress").style.display = "block";
}

async function loadBulkPending() {
  if (!state.novelId) return;
  try {
    const r = await api(`/api/novels/${state.novelId}/bulk/pending`);
    state.bulk.pending = r.pending || [];
    renderBulkPending();
  } catch (e) { /* 静默 */ }
}

function renderBulkPending() {
  const el = document.getElementById("bulkModList");
  const list = state.bulk.pending || [];
  const hasPending = list.length > 0;
  document.getElementById("btnBulkConfirm").disabled = !hasPending;
  document.getElementById("btnBulkDiscard").disabled = !hasPending;
  document.getElementById("bulkListToolbar").style.display = hasPending ? "flex" : "none";
  if (!list.length) {
    el.innerHTML = `<div class="mem-empty">暂无待确认修改。设置关键词、章节范围与修改要求后点击「开始修改」。</div>`;
    return;
  }
  el.innerHTML = list.map(it => `
    <div class="bulk-mod-item" data-chapter="${it.index}">
      <div class="bmi-row1">
        <input type="checkbox" class="bmi-cb" data-chapter="${it.index}" title="勾选后可用上方按钮批量保留/放弃" />
        <span class="bmi-idx">#${it.index}</span>
        <span class="bmi-title">${escapeHtml(it.title)}</span>
      </div>
      ${it.new_title && String(it.new_title).trim() !== it.title ? `<div class="bmi-newtitle">✏️ AI 建议新标题：<b>${escapeHtml(it.new_title)}</b>（保留时自动采用）</div>` : ""}
      <div class="bmi-prev">${escapeHtml(it.modified_preview || "")}…</div>
      <div class="bmi-actions">
        <button class="btn btn-xs btn-default bmi-rework" data-chapter="${it.index}" title="用新的修改要求重新修改本章">🔄 重新修改</button>
        <button class="btn btn-xs btn-primary bmi-keep" data-chapter="${it.index}">✅ 保留</button>
        <button class="btn btn-xs btn-warn bmi-drop" data-chapter="${it.index}">🗑 放弃</button>
        <button class="btn btn-xs btn-default bmi-view" data-chapter="${it.index}">📖 阅览</button>
      </div>
    </div>
  `).join("");
  el.querySelectorAll(".bmi-cb").forEach(cb => {
    cb.onclick = (e) => { e.stopPropagation(); updateBulkSelCount(); };
  });
  el.querySelectorAll(".bmi-view").forEach(btn => {
    btn.onclick = (e) => { e.stopPropagation(); loadChapter(parseInt(btn.dataset.chapter)); };
  });
  el.querySelectorAll(".bmi-keep").forEach(btn => {
    btn.onclick = (e) => { e.stopPropagation(); confirmOneBulk(parseInt(btn.dataset.chapter)); };
  });
  el.querySelectorAll(".bmi-drop").forEach(btn => {
    btn.onclick = (e) => { e.stopPropagation(); discardOneBulk(parseInt(btn.dataset.chapter)); };
  });
  el.querySelectorAll(".bmi-rework").forEach(btn => {
    btn.onclick = (e) => { e.stopPropagation(); openReworkModal(parseInt(btn.dataset.chapter)); };
  });
  // 整行点击 = 切换复选框（阅览请点「📖 阅览」按钮）
  el.querySelectorAll(".bulk-mod-item").forEach(item => {
    item.onclick = (e) => {
      if (e.target.closest("button") || e.target.closest(".bmi-cb")) return;
      const cb = item.querySelector(".bmi-cb");
      if (cb) { cb.checked = !cb.checked; updateBulkSelCount(); }
    };
  });
  updateBulkSelCount();
}

function updateBulkSelCount() {
  const cbs = document.querySelectorAll(".bmi-cb:checked");
  const countEl = document.getElementById("bulkSelCount");
  if (countEl) countEl.textContent = `${cbs.length} 章`;
  const all = document.getElementById("bulkSelAll");
  if (all) {
    const total = document.querySelectorAll(".bmi-cb").length;
    all.checked = total > 0 && cbs.length === total;
  }
}

function getSelectedBulkChapters() {
  const cbs = document.querySelectorAll(".bmi-cb:checked");
  return Array.from(cbs).map(cb => parseInt(cb.dataset.chapter)).filter(n => !isNaN(n));
}

// 通用批量操作：op = "confirm" | "discard"
async function bulkOperate(op, chapters, confirmMsg, busyMsg, doneMsg) {
  if (!state.novelId) return;
  if (!chapters.length) return toast(op === "confirm" ? "请先勾选要保留的章节" : "请先勾选要放弃的章节", "warn");
  if (!confirm(confirmMsg)) return;
  try {
    toast(busyMsg, "");
    const r = await api(`/api/novels/${state.novelId}/bulk/${op}`, {
      method: "POST", body: { chapters }
    });
    toast(doneMsg, "success");
    // 若当前浏览的章节被操作，重新加载
    if (state.chapterIdx && chapters.includes(state.chapterIdx)) loadChapter(state.chapterIdx);
    if (op === "confirm") {
      loadSettingsSummary();
      refreshToc();  // 确认后标题可能被自动加上【已修改】标记，同步刷新目录
    }
    loadBulkPending();
  } catch (e) { toast("操作失败：" + e.message, "error"); }
}

async function confirmBulkModify() {
  const all = (state.bulk.pending || []).map(it => it.index);
  await bulkOperate("confirm", all,
    `确定保留全部（${all.length} 章）修改吗？\n\n将立即写入磁盘；向量记忆与设定摘要将在后台自动更新。此操作不可撤销。`,
    `正在保存 ${all.length} 章...`,
    `✅ 已保留 ${all.length} 章修改，向量记忆与设定摘要后台更新中`);
}

async function discardBulkModify() {
  const all = (state.bulk.pending || []).map(it => it.index);
  await bulkOperate("discard", all,
    `确定放弃全部（${all.length} 章）未确认修改吗？\n\n放弃后这些修改将被丢弃，且无法恢复。`,
    "正在放弃...",
    "🗑 已放弃全部未确认修改");
}

async function confirmSelectedBulk() {
  const sel = getSelectedBulkChapters();
  await bulkOperate("confirm", sel,
    `确定保留这 ${sel.length} 章的修改吗？\n\n将立即写入磁盘；向量记忆与设定摘要将在后台自动更新。此操作不可撤销。`,
    `正在保存 ${sel.length} 章...`,
    `✅ 已保留 ${sel.length} 章修改，向量记忆与设定摘要后台更新中`);
}

async function discardSelectedBulk() {
  const sel = getSelectedBulkChapters();
  await bulkOperate("discard", sel,
    `确定放弃这 ${sel.length} 章的修改吗？\n\n放弃后这些修改将被丢弃，且无法恢复。`,
    "正在放弃...",
    `🗑 已放弃 ${sel.length} 章修改`);
}

async function confirmOneBulk(idx) {
  await bulkOperate("confirm", [idx],
    `确定保留第 ${idx} 章的修改吗？\n\n将立即写入磁盘；向量记忆与设定摘要将在后台自动更新。此操作不可撤销。`,
    `正在保存第 ${idx} 章...`,
    `✅ 已保留第 ${idx} 章修改，向量记忆与设定摘要后台更新中`);
}

async function discardOneBulk(idx) {
  await bulkOperate("discard", [idx],
    `确定放弃第 ${idx} 章的修改吗？\n\n放弃后这些修改将被丢弃，且无法恢复。`,
    "正在放弃...",
    `🗑 已放弃第 ${idx} 章修改`);
}

// ====== 单章重新修改 ======
let _reworkChapterIdx = null;

function openReworkModal(idx) {
  _reworkChapterIdx = idx;
  const pend = (state.bulk.pending || []).find(p => p.index === idx);
  const label = pend ? `第${idx}章《${pend.title}》` : `第${idx}章`;
  document.getElementById("reworkChapterLabel").textContent = label;
  // 预填当前批量修改要求
  const cur = document.getElementById("bulkInstruction").value.trim();
  document.getElementById("reworkInstruction").value = cur;
  document.getElementById("reworkModal").style.display = "flex";
  setTimeout(() => document.getElementById("reworkInstruction").focus(), 100);
}

function closeReworkModal() {
  document.getElementById("reworkModal").style.display = "none";
}

async function confirmRework() {
  if (!state.novelId || _reworkChapterIdx == null) return;
  const instruction = document.getElementById("reworkInstruction").value.trim();
  if (!instruction) return toast("请填写新的修改要求", "warn");
  const idx = _reworkChapterIdx;
  closeReworkModal();
  toast(`⏳ 正在重新修改第 ${idx} 章（可能需要一些时间）...`, "");
  try {
    const r = await api(`/api/novels/${state.novelId}/bulk/rework`, {
      method: "POST",
      body: {
        chapter_idx: idx,
        instruction: instruction,
        global_prompt: document.getElementById("globalPrompt").value.trim(),
        custom_instruction: document.getElementById("customAgent").value.trim(),
        style_instruction: buildStyleInstruction()
      }
    });
    if (r.ok) {
      toast(`✅ 第 ${idx} 章已重新修改，向量记忆已更新`, "success");
      loadBulkPending();
      if (state.chapterIdx === idx) loadChapter(idx);
    } else {
      toast(r.message || "重新修改失败", "error");
    }
  } catch (e) {
    toast("重新修改失败：" + e.message, "error");
  }
}

// ====== 批量修改历史 ======
function openBulkHistory() {
  document.getElementById("bulkHistoryModal").style.display = "flex";
  loadBulkHistory();
}
function closeBulkHistory() {
  document.getElementById("bulkHistoryModal").style.display = "none";
}
async function loadBulkHistory() {
  const el = document.getElementById("bulkHistoryList");
  if (!state.novelId) { el.innerHTML = `<div class="pc-empty">请先选择一本小说</div>`; return; }
  try {
    const r = await api(`/api/novels/${state.novelId}/bulk/history`);
    const hist = r.history || [];
    if (!hist.length) {
      el.innerHTML = `<div class="pc-empty">暂无修改历史。完成一次「总体设定修改」并保留后，这里会记录修改的章节范围。</div>`;
      return;
    }
    el.innerHTML = hist.map(h => {
      const t = new Date((h.time || 0) * 1000);
      const chList = (h.chapters || []).join(", ");
      return `
      <div class="bh-item">
        <div class="bh-head">
          <span class="bh-time">${t.toLocaleString("zh-CN", { hour12: false })}</span>
          <span class="tag tag-info" style="font-size:11px">范围：${escapeHtml(h.range || "—")}</span>
          <span class="tag tag-success" style="font-size:11px">${(h.chapters || []).length} 章</span>
        </div>
        <div class="bh-kw">关键词：${escapeHtml((h.keywords || []).join("、") || "—")}</div>
        <div class="bh-ins">要求：${escapeHtml(h.instruction || "—")}</div>
        <div class="bh-ch">章节：${escapeHtml(chList)}</div>
      </div>`;
    }).join("");
  } catch (e) {
    el.innerHTML = `<div class="pc-empty" style="color:var(--danger)">加载失败：${escapeHtml(e.message)}</div>`;
  }
}
