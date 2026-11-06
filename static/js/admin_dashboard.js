// static/js/admin_dashboard.js

document.addEventListener("DOMContentLoaded", () => {
    // --- Elements ---
    const trainBtn = document.getElementById("trainBtn");
    const trainProgress = document.getElementById("trainProgress");
    const trainMsg = document.getElementById("trainMsg");
    const startMonitorBtn = document.getElementById("startMonitorBtn");
    const stopMonitorBtn = document.getElementById("stopMonitorBtn");
    const monitorStatus = document.getElementById("monitorStatus");
    const attendanceChartCanvas = document.getElementById("attendanceChart");

    let statusPollingInterval = null;
    let trainPollingInterval = null;
    let attendanceChart = null;

    // --- Training Functions ---
    async function pollTrainStatus() {
        if (!trainProgress || !trainMsg) return false;
        try {
            const res = await fetch("/train_status");
            if (res.status === 401 || res.status === 403) {
                trainMsg.textContent = "Unauthorized. Please log in as admin.";
                if (trainBtn) trainBtn.disabled = false;
                return false;
            }
            const data = await res.json();
            const progress = typeof data.progress === "number" ? data.progress : 0;
            trainProgress.style.width = progress + "%";
            trainProgress.textContent = progress + "%";
            trainMsg.textContent = data.message || "Status unavailable";

            if (!data.running || progress >= 100) {
                if (trainPollingInterval) clearInterval(trainPollingInterval);
                trainPollingInterval = null;
                if (trainBtn) trainBtn.disabled = false;
                if (progress >= 100) updateChart();
            }
            return !!data.running;
        } catch (e) {
            console.error("Error polling train status:", e);
            trainMsg.textContent = "Error fetching status.";
            if (trainPollingInterval) clearInterval(trainPollingInterval);
            trainPollingInterval = null;
            if (trainBtn) trainBtn.disabled = false;
            return false;
        }
    }

    if (trainBtn && trainProgress && trainMsg) {
        trainBtn.addEventListener("click", async () => {
            trainBtn.disabled = true;
            trainMsg.textContent = "Requesting training start...";
            try {
                const res = await fetch("/train_model");
                let data = {};
                try { data = await res.json(); } catch (_) {}
                if (res.ok && data.status === 'started') {
                    trainMsg.textContent = "Training started... polling status.";
                    if (trainPollingInterval) clearInterval(trainPollingInterval);
                    trainPollingInterval = setInterval(pollTrainStatus, 2000);
                    pollTrainStatus();
                } else if (res.status === 202 && data.status === 'already_running') {
                    trainMsg.textContent = "Training is already running.";
                    if (trainPollingInterval) clearInterval(trainPollingInterval);
                    trainPollingInterval = setInterval(pollTrainStatus, 2000);
                } else {
                    trainMsg.textContent = `Failed to start training: ${data.message || data.error || res.statusText || 'Server error'}`;
                    trainBtn.disabled = false;
                }
            } catch (e) {
                console.error("Error starting training:", e);
                trainMsg.textContent = "Network error starting training.";
                trainBtn.disabled = false;
            }
        });
    }

    // --- Monitoring Control Functions ---
    const sourceType = document.getElementById("sourceType");
    const webcamSourceWrap = document.getElementById("webcamSourceWrap");
    const videoSourceWrap = document.getElementById("videoSourceWrap");
    const cameraIndex = document.getElementById("cameraIndex");
    const videoFile = document.getElementById("videoFile");
    const uploadVideoBtn = document.getElementById("uploadVideoBtn");
    const uploadStatus = document.getElementById("uploadStatus");
    const uploadedVideoName = document.getElementById("uploadedVideoName");

    function syncSourceUI() {
        if (!sourceType) return;
        const type = sourceType.value;
        if (webcamSourceWrap) webcamSourceWrap.style.display = type === "webcam" ? "" : "none";
        if (videoSourceWrap) videoSourceWrap.style.display = type === "video" ? "" : "none";
    }
    if (sourceType) {
        sourceType.addEventListener("change", syncSourceUI);
        syncSourceUI();
    }

    if (uploadVideoBtn && videoFile) {
        uploadVideoBtn.addEventListener("click", async () => {
            if (!videoFile.files || !videoFile.files[0]) {
                if (uploadStatus) uploadStatus.textContent = "Select a video file first.";
                return;
            }
            uploadVideoBtn.disabled = true;
            if (uploadStatus) uploadStatus.textContent = "Uploading...";
            try {
                const fd = new FormData();
                fd.append("video", videoFile.files[0]);
                const res = await fetch("/upload_video", { method: "POST", body: fd });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || data.status !== "ok") {
                    if (uploadStatus) uploadStatus.textContent = data.message || "Upload failed.";
                } else {
                    if (uploadedVideoName) uploadedVideoName.value = data.filename || "";
                    if (uploadStatus) uploadStatus.textContent = `Uploaded: ${data.filename}`;
                }
            } catch (e) {
                console.error("Upload error:", e);
                if (uploadStatus) uploadStatus.textContent = "Network error during upload.";
            } finally {
                uploadVideoBtn.disabled = false;
            }
        });
    }

    async function updateMonitorStatus() {
        if (!monitorStatus) return;
        try {
            const res = await fetch("/cctv_status");
            if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
            const data = await res.json();
            const type = data.source_type || "webcam";
            const sourceLabel = type === "webcam" ? `Local Webcam (${data.source ?? data.camera_url ?? "0"})`
                : type === "video" ? `Video (${data.source || "file"})`
                : `CCTV/RTSP (${data.source || data.camera_url || ""})`;

            if (data.running) {
                monitorStatus.className = "alert alert-success mt-2 mb-0 small py-2";
                monitorStatus.textContent = `Active: ${sourceLabel}. Marking attendance in progress...`;
                if (startMonitorBtn) startMonitorBtn.disabled = true;
                if (stopMonitorBtn) stopMonitorBtn.disabled = false;
            } else {
                monitorStatus.className = "alert alert-secondary mt-2 mb-0 small py-2";
                const msg = data.message ? ` ${data.message}` : "";
                monitorStatus.textContent = `Stopped: ${sourceLabel}.${msg}`;
                if (startMonitorBtn) startMonitorBtn.disabled = false;
                if (stopMonitorBtn) stopMonitorBtn.disabled = true;
            }
        } catch (err) {
            monitorStatus.className = "alert alert-danger mt-2 mb-0 small py-2";
            monitorStatus.textContent = "Error fetching system status.";
            console.error("Monitor Status Error:", err);
            if (startMonitorBtn) startMonitorBtn.disabled = false;
            if (stopMonitorBtn) stopMonitorBtn.disabled = true;
        }
    }

    if (startMonitorBtn && monitorStatus) {
        startMonitorBtn.addEventListener("click", async () => {
            const type = sourceType ? sourceType.value : "webcam";
            let payload = { action: "start", source_type: type };
            if (type === "webcam") {
                payload.source = cameraIndex ? (cameraIndex.value || "0") : "0";
            } else if (type === "video") {
                const fname = uploadedVideoName ? uploadedVideoName.value : "";
                if (!fname) {
                    alert("Upload a video first, then click Start.");
                    return;
                }
                payload.source = fname;
            } else {
                alert("CCTV/RTSP will be enabled later.");
                return;
            }
            startMonitorBtn.disabled = true;
            monitorStatus.textContent = "Sending start command...";
            try {
                const res = await fetch("/cctv_control", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                let data = {};
                try { data = await res.json(); } catch (_) {}
                if (!res.ok || data.status === "error") {
                    alert(`Failed to start: ${data.message || "Check server logs."}`);
                }
                setTimeout(updateMonitorStatus, 500);
            } catch (e) {
                alert("Network error sending start command.");
                console.error("Start command error:", e);
                setTimeout(updateMonitorStatus, 500);
            }
        });
    }

    if (stopMonitorBtn && monitorStatus) {
        stopMonitorBtn.addEventListener("click", async () => {
            stopMonitorBtn.disabled = true;
            monitorStatus.textContent = "Sending stop command...";
            try {
                await fetch("/cctv_control", {
                    method: "POST", headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'stop' })
                });
            } catch (e) {
                alert("Network error sending stop command.");
                console.error("Stop command error:", e);
            } finally {
                setTimeout(updateMonitorStatus, 500);
            }
        });
    }

    // --- Chart Functions ---
    async function updateChart() {
        if (!attendanceChartCanvas || typeof Chart === "undefined") return;
        try {
            const res = await fetch("/attendance_stats");
            if (!res.ok) return;
            const data = await res.json();
            const ctx = attendanceChartCanvas.getContext("2d");
            if (!attendanceChart) {
                attendanceChart = new Chart(ctx, {
                    type: "bar",
                    data: {
                        labels: data.dates || [],
                        datasets: [{ label: "Attendance Count", data: data.counts || [], backgroundColor: "rgba(59, 130, 246, 0.7)", borderRadius: 4 }]
                    },
                    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true } } }
                });
            } else {
                attendanceChart.data.labels = data.dates || [];
                attendanceChart.data.datasets[0].data = data.counts || [];
                attendanceChart.update();
            }
        } catch (e) {
            console.error("Error updating chart:", e);
        }
    }

    // --- Manual attendance (admin/staff) ---
    const manualStudent = document.getElementById("manualStudent");
    const manualDate = document.getElementById("manualDate");
    const manualStatus = document.getElementById("manualStatus");
    const manualNote = document.getElementById("manualNote");
    const manualSubmitBtn = document.getElementById("manualSubmitBtn");
    const manualMsg = document.getElementById("manualMsg");

    async function loadManualStudents() {
        if (!manualStudent) return;
        try {
            const res = await fetch("/students");
            const data = await res.json().catch(()=>({}));
            if (!res.ok) { manualStudent.innerHTML = '<option value="">Unable to load students</option>'; return; }
            const list = data.students || [];
            if (!list.length) { manualStudent.innerHTML = '<option value="">No students found</option>'; return; }
            manualStudent.innerHTML = '<option value="">Select student...</option>' + list.map(s => `<option value="${s.id}">${s.name} (ID ${s.id}${s.roll ? ', '+s.roll : ''})</option>`).join('');
        } catch (e) {
            manualStudent.innerHTML = '<option value="">Network error</option>';
        }
    }
    if (manualDate && !manualDate.value) {
        try { manualDate.value = new Date().toISOString().slice(0,10); } catch (_) {}
    }
    if (manualSubmitBtn) {
        manualSubmitBtn.addEventListener("click", async () => {
            if (!manualStudent || !manualStudent.value) { if (manualMsg) manualMsg.textContent = "Select a student."; return; }
            manualSubmitBtn.disabled = true;
            if (manualMsg) manualMsg.textContent = "Saving...";
            try {
                const res = await fetch("/manual_attendance", {
                    method: "POST", headers: {"Content-Type":"application/json"},
                    body: JSON.stringify({ student_id: manualStudent.value, status: manualStatus ? manualStatus.value : "Present", date: manualDate ? manualDate.value : "", note: manualNote ? manualNote.value : "" })
                });
                const data = await res.json().catch(()=>({}));
                if (res.ok && data.ok) {
                    if (manualMsg) manualMsg.textContent = `Saved: ${data.name} marked ${data.status} on ${data.date}.`;
                    if (manualNote) manualNote.value = "";
                } else {
                    if (manualMsg) manualMsg.textContent = data.error || "Failed to save.";
                }
            } catch (e) {
                if (manualMsg) manualMsg.textContent = "Network error.";
            } finally { manualSubmitBtn.disabled = false; }
        });
    }

    // --- Initial Load ---
    if (manualStudent) loadManualStudents();
    if (monitorStatus) updateMonitorStatus();
    if (trainBtn && trainProgress && trainMsg) pollTrainStatus();
    if (attendanceChartCanvas) updateChart();
    if (monitorStatus) setInterval(updateMonitorStatus, 5000);
});

