// static/js/student_dashboard.js

document.addEventListener("DOMContentLoaded", () => {
    const personalList = document.getElementById("personalNotificationList");
    const errBox = document.getElementById("studentError");
    const daysPresent = document.getElementById("daysPresent");
    const totalMarks = document.getElementById("totalMarks");
    const lastOut = document.getElementById("lastOut");

    async function updateSummary() {
        try {
            const res = await fetch("/student_summary");
            const data = await res.json().catch(()=>({}));
            if (!res.ok || !data.ok) return;
            if (daysPresent) daysPresent.textContent = data.days_present ?? 0;
            if (totalMarks) totalMarks.textContent = data.total_attendance_marks ?? 0;
            if (lastOut) lastOut.textContent = data.last_out || '-';
        } catch (e) {}
    }

    async function updatePersonalNotifications() {
        if (!personalList) return;
        try {
            const res = await fetch("/student_notifications");
            const data = await res.json().catch(()=>({}));
            if (!res.ok) {
                if (errBox) { errBox.classList.remove('d-none'); errBox.textContent = data.error || 'Could not load your records.'; }
                personalList.innerHTML = '<li class="list-group-item text-muted small">No linked activity.</li>';
                return;
            }
            if (errBox) errBox.classList.add('d-none');
            personalList.innerHTML = '';
            if (!data.notifications || data.notifications.length === 0) {
                personalList.innerHTML = '<li class="list-group-item text-muted small">No recent activity recorded for you.</li>';
                return;
            }
            data.notifications.forEach(n => {
                const li = document.createElement('li');
                const actionText = n.action === 'IN' ? 'Entered' : 'Exited';
                const badge = n.action === 'IN' ? 'bg-success' : 'bg-secondary';
                li.className = 'list-group-item small d-flex justify-content-between align-items-center';
                const time = (n.timestamp || '').split(' ')[1] || '';
                const date = (n.timestamp || '').split(' ')[0] || '';
                li.innerHTML = `<span><span class="badge ${badge} me-2">${actionText}</span>${time}</span><span class="text-muted">${date}</span>`;
                personalList.appendChild(li);
            });
        } catch (err) {
            console.error("Error fetching personal notifications:", err);
            personalList.innerHTML = '<li class="list-group-item text-danger small">Error loading your records.</li>';
        }
    }

    updateSummary();
    updatePersonalNotifications();
    setInterval(updatePersonalNotifications, 10000);
    setInterval(updateSummary, 30000);
});
