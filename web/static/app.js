const form = document.getElementById("job-form");
const submitBtn = document.getElementById("submit-btn");
const statusCard = document.getElementById("status-card");
const jobIdEl = document.getElementById("job-id");
const jobStatusEl = document.getElementById("job-status");
const jobMessageEl = document.getElementById("job-message");
const jobErrorEl = document.getElementById("job-error");
const downloadLink = document.getElementById("download-link");

let pollTimer = null;

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearInterval(pollTimer);

  submitBtn.disabled = true;
  submitBtn.textContent = "提交中…";

  const formData = new FormData(form);
  try {
    const resp = await fetch("/api/jobs", { method: "POST", body: formData });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "提交失败");
    }
    showStatus(data.job_id, "pending", "任务已提交");
    pollTimer = setInterval(() => pollJob(data.job_id), 3000);
    await pollJob(data.job_id);
  } catch (err) {
    showError(String(err.message || err));
    submitBtn.disabled = false;
    submitBtn.textContent = "开始生成";
  }
});

async function pollJob(jobId) {
  const resp = await fetch(`/api/jobs/${jobId}`);
  const data = await resp.json();
  if (!resp.ok) {
    showError(data.error || "查询失败");
    clearInterval(pollTimer);
    submitBtn.disabled = false;
    submitBtn.textContent = "开始生成";
    return;
  }

  showStatus(data.job_id, data.status, data.message);

  if (data.status === "completed") {
    clearInterval(pollTimer);
    downloadLink.href = data.download_url;
    downloadLink.classList.remove("hidden");
    jobErrorEl.classList.add("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = "开始生成";
  } else if (data.status === "failed") {
    clearInterval(pollTimer);
    jobErrorEl.textContent = data.error || "未知错误";
    jobErrorEl.classList.remove("hidden");
    downloadLink.classList.add("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = "开始生成";
  } else {
    downloadLink.classList.add("hidden");
    jobErrorEl.classList.add("hidden");
  }
}

function showStatus(jobId, status, message) {
  statusCard.classList.remove("hidden");
  jobIdEl.textContent = jobId;
  jobStatusEl.textContent = status;
  jobStatusEl.className = `badge ${status}`;
  jobMessageEl.textContent = message || "";
}

function showError(msg) {
  statusCard.classList.remove("hidden");
  jobErrorEl.textContent = msg;
  jobErrorEl.classList.remove("hidden");
}
