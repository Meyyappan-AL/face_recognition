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
        try {
            const res = await fetch("/train_status");
            const data = await res.json();
            trainProgress.style.width = data.progress + "%";
            trainProgress.textContent = data.progress + "%";
            trainMsg.textContent = data.message || "Status unavailable";

            if (!data.running || data.progress >= 100) {
                if (trainPollingInterval) clearInterval(trainPollingInterval);
                trainPollingInterval = null;
                trainBtn.disabled = false;
            }
            return data.running;
        } catch (e) {
            console.error("Error polling train status:", e);
            trainMsg.textContent = "Error fetching status.";
            if (trainPollingInterval) clearInterval(trainPollingInterval);
            trainPollingInterval = null;
            trainBtn.disabled = false;
            return false;
        }
    }

    trainBtn.addEventListener("click", async () => {
        trainBtn.disabled = true;
        trainMsg.textContent = "Requesting training start...";
        try {
            const res = await fetch("/train_model");
            const data = await res.json();
            if (res.ok && data.status === 'started') {
                trainMsg.textContent = "Training started... polling status.";
                if (trainPollingInterval) clearInterval(trainPollingInterval);
                trainPollingInterval = setInterval(pollTrainStatus, 2000);
            } else if (res.status === 202 && data.status === 'already_running') {
                 trainMsg.textContent = "Training is already running.";
                 if (trainPollingInterval) clearInterval(trainPollingInterval);
                 trainPollingInterval = setInterval(pollTrainStatus, 2000);
            } else {
                trainMsg.textContent = `Failed to start training: ${data.message || 'Server error'}`;
                trainBtn.disabled = false;
            }
        } catch (e) {
            console.error("Error starting training:", e);
            trainMsg.textContent = "Network error starting training.";
            trainBtn.disabled = false;
        }
    });

    // --- Monitoring Control Functions ---
    async function updateMonitorStatus() {
        try {
            const res = await fetch("/cctv_status");
            if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
            const data = await res.json();
            const cameraSource = data.camera_url === '0' ? 'Local Webcam' : `Stream: ${data.camera_url}`;

            if (data.running) {
                monitorStatus.className = "alert alert-success mt-2 small";
                monitorStatus.innerHTML = `✅ System Active (${cameraSource}). **Monitoring in progress...**`;
                startMonitorBtn.disabled = true;
                stopMonitorBtn.disabled = false;
            } else {
                monitorStatus.className = "alert alert-secondary mt-2 small";
                monitorStatus.innerHTML = `⏹️ System Stopped. Using ${cameraSource}. Click Start to begin.`;
                startMonitorBtn.disabled = false;
                stopMonitorBtn.disabled = true;
            }
        } catch (err) {
            monitorStatus.className = "alert alert-danger mt-2 small";
            monitorStatus.innerText = "Error fetching system status.";
            console.error("Monitor Status Error:", err);
            startMonitorBtn.disabled = false;
            stopMonitorBtn.disabled = true;
        }
    }

    startMonitorBtn.addEventListener("click", async () => {
        startMonitorBtn.disabled = true;
        monitorStatus.textContent = "Sending start command...";
        try {
            const res = await fetch("/cctv_control", {
                method: "POST", headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'start' })
            });
             if (!res.ok) {
                 const errorData = await res.json();
                 alert(`Failed to start: ${errorData.message || 'Check server logs.'}`);
             }
             setTimeout(updateMonitorStatus, 500);
        } catch (e) {
            alert("Network error sending start command.");
            console.error("Start command error:", e);
            setTimeout(updateMonitorStatus, 500);
        }
    });

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

    // --- Chart Functions ---
    async function updateChart() {
        try {
            const res = await fetch("/attendance_stats");
            const data = await res.json();
            const ctx = attendanceChartCanvas.getContext("2d");
            if (!attendanceChart) {
                attendanceChart = new Chart(ctx, {
                    type: "bar",
                    data: {
                        labels: data.dates,
                        datasets: [{ label: "Attendance Count", data: data.counts, backgroundColor: "rgba(59, 130, 246, 0.7)", borderRadius: 4 }]
                    },
                    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true } } }
                });
            } else {
                attendanceChart.data.labels = data.dates;
                attendanceChart.data.datasets[0].data = data.counts;
                attendanceChart.update();
            }
        } catch (e) {
            console.error("Error updating chart:", e);
        }
    }

    // --- Initial Load ---
    updateMonitorStatus();
    pollTrainStatus();
    updateChart();
    setInterval(updateMonitorStatus, 5000);
});

