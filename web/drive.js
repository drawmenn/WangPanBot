const state = {
  keyword: "",
  type: "all",
  page: 1,
  limit: 8,
  totalPages: 1,
  hasNext: false,
  token: "",
  permissions: {
    is_web_admin: false,
    delete_enabled: false,
    upload_enabled: false,
  },
};

const refs = {
  searchForm: document.getElementById("searchForm"),
  keywordInput: document.getElementById("keywordInput"),
  typeSelect: document.getElementById("typeSelect"),
  pageInput: document.getElementById("pageInput"),
  refreshBtn: document.getElementById("refreshBtn"),
  prevBtn: document.getElementById("prevBtn"),
  nextBtn: document.getElementById("nextBtn"),
  pagerText: document.getElementById("pagerText"),
  tableBody: document.getElementById("fileTableBody"),
  statusText: document.getElementById("statusText"),
  totalCount: document.getElementById("totalCount"),
  totalSize: document.getElementById("totalSize"),
  currentPage: document.getElementById("currentPage"),
  filterLabel: document.getElementById("filterLabel"),
  tokenInput: document.getElementById("tokenInput"),
  saveTokenBtn: document.getElementById("saveTokenBtn"),
  clearTokenBtn: document.getElementById("clearTokenBtn"),
  uploadInput: document.getElementById("uploadInput"),
  uploadBtn: document.getElementById("uploadBtn"),
  uploadHint: document.getElementById("uploadHint"),
};

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatSize(totalBytes) {
  const bytes = Number(totalBytes || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(2)} ${units[unitIndex]}`;
}

async function requestJson(url, options = {}) {
  const requestOptions = { ...options };
  requestOptions.headers = requestOptions.headers || {};
  if (state.token) {
    requestOptions.headers["X-Admin-Token"] = state.token;
  }
  const response = await fetch(url, requestOptions);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    const message = data.detail || data.message || "请求失败";
    throw new Error(message);
  }
  return data;
}

function renderTable(items, permissions) {
  if (!items.length) {
    refs.tableBody.innerHTML = `
      <tr>
        <td colspan="3">没有匹配结果，试试换个关键词或筛选类型。</td>
      </tr>
    `;
    return;
  }

  const allowDelete = permissions.delete_enabled && permissions.is_web_admin;

  refs.tableBody.innerHTML = items
    .map((item) => {
      const id = Number(item.id);
      const name = escapeHtml(String(item.name || ""));
      const command = escapeHtml(String(item.get_command || `/get ${id}`));
      return `
        <tr>
          <td>${id}</td>
          <td class="name-cell">${name}</td>
          <td>
            <div class="action-row">
              <a class="minor" href="/api/files/${id}/download" target="_blank" rel="noopener">下载</a>
              <button class="minor copy-btn" data-command="${command}" type="button">复制命令</button>
              ${
                allowDelete
                  ? `<button class="danger delete-btn" data-id="${id}" type="button">删除</button>`
                  : ""
              }
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  refs.tableBody.querySelectorAll(".copy-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const command = button.getAttribute("data-command") || "";
      try {
        await navigator.clipboard.writeText(command);
        refs.statusText.textContent = `已复制: ${command}`;
      } catch (error) {
        refs.statusText.textContent = "复制失败，请手动复制。";
      }
    });
  });

  refs.tableBody.querySelectorAll(".delete-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.getAttribute("data-id");
      if (!id) return;
      if (!confirm(`确认删除文件 ID ${id} ?`)) return;
      try {
        await requestJson(`/api/files/${id}`, { method: "DELETE" });
        refs.statusText.textContent = `已删除文件 ID ${id}`;
        await loadFiles();
      } catch (error) {
        refs.statusText.textContent = `删除失败: ${error.message}`;
      }
    });
  });
}

function syncUploadStatus() {
  const canUpload = state.permissions.is_web_admin && state.permissions.upload_enabled;
  refs.uploadBtn.disabled = !canUpload;

  if (!state.permissions.upload_enabled) {
    refs.uploadHint.textContent =
      "服务端未配置 WEB_UPLOAD_CHAT_ID 或 ADMIN_ID，当前无法网页上传。";
    return;
  }
  if (!state.permissions.is_web_admin) {
    refs.uploadHint.textContent = "请先填写管理员令牌，才可网页上传。";
    return;
  }
  refs.uploadHint.textContent = "已启用网页上传。上传后会自动写入数据库。";
}

async function loadFilters() {
  const data = await requestJson("/api/filters");
  refs.typeSelect.innerHTML = data.filters
    .map((item) => {
      const value = escapeHtml(item.key);
      const label = escapeHtml(item.label);
      return `<option value="${value}">${label}</option>`;
    })
    .join("");
  refs.typeSelect.value = state.type;
}

async function loadFiles() {
  refs.statusText.textContent = "加载中...";
  const params = new URLSearchParams({
    q: state.keyword,
    type: state.type,
    page: String(state.page),
    limit: String(state.limit),
  });

  try {
    const data = await requestJson(`/api/files?${params.toString()}`);
    const summary = data.summary;
    const pagination = data.pagination;
    state.permissions = data.permissions || state.permissions;
    state.totalPages = Number(pagination.total_pages || 1);
    state.page = Number(pagination.page || 1);
    state.hasNext = Boolean(pagination.has_next);

    refs.totalCount.textContent = String(summary.total_count ?? 0);
    refs.totalSize.textContent = formatSize(summary.total_size_bytes ?? 0);
    refs.currentPage.textContent = `${state.page} / ${state.totalPages}`;
    refs.filterLabel.textContent = summary.filter_label || "-";
    refs.pagerText.textContent = `第 ${state.page} / ${state.totalPages} 页`;
    refs.prevBtn.disabled = state.page <= 1;
    refs.nextBtn.disabled = !state.hasNext;
    refs.pageInput.value = String(state.page);
    refs.statusText.textContent = summary.keyword
      ? `关键词: ${summary.keyword}`
      : "关键词: （全部）";

    renderTable(data.items || [], data.permissions || {});
    syncUploadStatus();
  } catch (error) {
    refs.tableBody.innerHTML = `
      <tr>
        <td colspan="3">请求失败: ${escapeHtml(error.message)}</td>
      </tr>
    `;
    refs.statusText.textContent = "加载失败";
  }
}

function bindEvents() {
  refs.searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    state.keyword = refs.keywordInput.value.trim();
    state.type = refs.typeSelect.value || "all";
    state.page = Math.max(1, Number(refs.pageInput.value || "1"));
    await loadFiles();
  });

  refs.refreshBtn.addEventListener("click", loadFiles);

  refs.prevBtn.addEventListener("click", async () => {
    if (state.page <= 1) return;
    state.page -= 1;
    await loadFiles();
  });

  refs.nextBtn.addEventListener("click", async () => {
    if (!state.hasNext) return;
    state.page += 1;
    await loadFiles();
  });

  refs.saveTokenBtn.addEventListener("click", async () => {
    state.token = refs.tokenInput.value.trim();
    localStorage.setItem("wangpan_web_admin_token", state.token);
    refs.statusText.textContent = state.token
      ? "管理员令牌已保存"
      : "令牌为空，已保存为只读模式";
    await loadFiles();
  });

  refs.clearTokenBtn.addEventListener("click", async () => {
    state.token = "";
    refs.tokenInput.value = "";
    localStorage.removeItem("wangpan_web_admin_token");
    refs.statusText.textContent = "管理员令牌已清空";
    await loadFiles();
  });

  refs.uploadBtn.addEventListener("click", async () => {
    if (!refs.uploadInput.files || refs.uploadInput.files.length === 0) {
      refs.statusText.textContent = "请选择要上传的文件。";
      return;
    }
    const file = refs.uploadInput.files[0];
    const formData = new FormData();
    formData.append("file", file);

    refs.statusText.textContent = "上传中...";
    try {
      const data = await requestJson("/api/upload", {
        method: "POST",
        body: formData,
      });
      const status = data.is_new ? "已收录" : "已更新";
      refs.statusText.textContent = `${status}: ${file.name}`;
      refs.uploadInput.value = "";
      await loadFiles();
    } catch (error) {
      refs.statusText.textContent = `上传失败: ${error.message}`;
    }
  });
}

async function init() {
  state.token = localStorage.getItem("wangpan_web_admin_token") || "";
  refs.tokenInput.value = state.token;
  bindEvents();
  await loadFilters();
  await loadFiles();
}

init();
