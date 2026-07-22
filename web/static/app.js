const form = document.getElementById("job-form");
const submitBtn = document.getElementById("submit-btn");
const statusCard = document.getElementById("status-card");
const jobIdEl = document.getElementById("job-id");
const jobStatusEl = document.getElementById("job-status");
const jobMessageEl = document.getElementById("job-message");
const jobErrorEl = document.getElementById("job-error");
const jobLogEl = document.getElementById("job-log");
const downloadLink = document.getElementById("download-link");
const resumeBtn = document.getElementById("resume-btn");
const progressWrap = document.getElementById("progress-wrap");
const progressFill = document.getElementById("progress-fill");
const progressText = document.getElementById("progress-text");
const jobsListEl = document.getElementById("jobs-list");
const refreshJobsBtn = document.getElementById("refresh-jobs-btn");

const STORAGE_KEY = "roleswap_active_job_id";
let pollTimer = null;
let listTimer = null;
let activeJobId = null;

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  submitBtn.disabled = true;
  submitBtn.textContent = "提交中…";

  const formData = new FormData(form);
  try {
    const resp = await fetch("/api/jobs", { method: "POST", body: formData });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "提交失败");
    setActiveJob(data.job_id);
    await loadJobsList();
  } catch (err) {
    alert(String(err.message || err));
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "提交后台任务";
  }
});

resumeBtn.addEventListener("click", async () => {
  if (!activeJobId) return;
  resumeBtn.disabled = true;
  try {
    const resp = await fetch(`/api/jobs/${activeJobId}/resume`, { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "续传失败");
    startPolling(activeJobId);
    await loadJobsList();
  } catch (err) {
    alert(String(err.message || err));
  } finally {
    resumeBtn.disabled = false;
  }
});

refreshJobsBtn.addEventListener("click", () => loadJobsList());

function setActiveJob(jobId) {
  activeJobId = jobId;
  localStorage.setItem(STORAGE_KEY, jobId);
  statusCard.classList.remove("hidden");
  startPolling(jobId);
}

function startPolling(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => pollJob(jobId), 3000);
  pollJob(jobId);
}

async function pollJob(jobId) {
  const resp = await fetch(`/api/jobs/${jobId}`);
  const data = await resp.json();
  if (!resp.ok) return;

  renderJobDetail(data);

  if (data.status === "completed" || data.status === "failed" || data.status === "interrupted") {
    clearInterval(pollTimer);
    await loadJobsList();
  }
}

function renderJobDetail(data) {
  jobIdEl.textContent = data.job_id;
  jobStatusEl.textContent = data.status;
  jobStatusEl.className = `badge ${data.status}`;
  jobMessageEl.textContent = data.message || "";

  if (data.segments_total > 0) {
    progressWrap.classList.remove("hidden");
    progressFill.style.width = `${data.progress_percent || 0}%`;
    progressText.textContent = `${data.segments_done} / ${data.segments_total} 段 (${data.progress_percent || 0}%)`;
  } else {
    progressWrap.classList.add("hidden");
  }

  if (data.log_tail) {
    jobLogEl.textContent = data.log_tail;
    jobLogEl.classList.remove("hidden");
  }

  if (data.status === "completed") {
    downloadLink.href = data.download_url;
    downloadLink.classList.remove("hidden");
    jobErrorEl.classList.add("hidden");
    resumeBtn.classList.add("hidden");
  } else if (data.status === "failed" || data.status === "interrupted") {
    jobErrorEl.textContent = data.error || "任务未完成";
    jobErrorEl.classList.remove("hidden");
    downloadLink.classList.add("hidden");
    resumeBtn.classList.remove("hidden");
  } else {
    jobErrorEl.classList.add("hidden");
    downloadLink.classList.add("hidden");
    resumeBtn.classList.add("hidden");
  }
}

async function loadJobsList() {
  const resp = await fetch("/api/jobs");
  const data = await resp.json();
  if (!resp.ok) {
    jobsListEl.innerHTML = `<p class="muted">加载失败</p>`;
    return;
  }

  const jobs = data.jobs || [];
  if (!jobs.length) {
    jobsListEl.innerHTML = `<p class="muted">暂无任务</p>`;
    return;
  }

  jobsListEl.innerHTML = jobs.map((j) => {
    const pct = j.progress_percent || 0;
    const bar = j.segments_total > 0
      ? `<div class="progress-bar small"><div class="progress-fill" style="width:${pct}%"></div></div>
         <span class="muted">${j.segments_done}/${j.segments_total} 段</span>`
      : "";
    const actions = [];
    actions.push(`<button type="button" class="link-btn" data-view="${j.job_id}">查看</button>`);
    if (j.status === "completed" && j.download_url) {
      actions.push(`<a class="link-btn" href="${j.download_url}">下载</a>`);
    }
    if (j.status === "failed" || j.status === "interrupted") {
      actions.push(`<button type="button" class="link-btn" data-resume="${j.job_id}">续传</button>`);
    }
    return `
      <div class="job-row">
        <div class="job-row-main">
          <strong>${j.video_name || "video"}</strong>
          <span class="badge ${j.status}">${j.status}</span>
          <span class="muted">${formatTime(j.created_at)} · ${j.duration}s</span>
          <p class="muted">${escapeHtml(j.message || "")}</p>
          ${bar}
        </div>
        <div class="job-row-actions">${actions.join(" ")}</div>
      </div>`;
  }).join("");

  jobsListEl.querySelectorAll("[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setActiveJob(btn.dataset.view));
  });
  jobsListEl.querySelectorAll("[data-resume]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      setActiveJob(btn.dataset.resume);
      resumeBtn.click();
    });
  });
}

function formatTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("zh-CN");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

async function init() {
  await loadJobsList();
  listTimer = setInterval(loadJobsList, 8000);

  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {
    setActiveJob(saved);
  }
}

init();
