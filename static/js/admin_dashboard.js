// static/js/admin_dashboard.js

document.addEventListener("DOMContentLoaded", () => {
    const trainBtn = document.getElementById("trainBtn");
    const trainProgress = document.getElementById("trainProgress");
    const trainMsg = document.getElementById("trainMsg");
    const attendanceChartCanvas = document.getElementById("attendanceChart");

    let trainPollingInterval = null;
    let attendanceChart = null;

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

    if (trainBtn && trainProgress && trainMsg) pollTrainStatus();
    if (attendanceChartCanvas) updateChart();
});
