// static/js/student_dashboard.js

document.addEventListener("DOMContentLoaded", () => {
    const personalList = document.getElementById("personalNotificationList");

    async function updatePersonalNotifications() {
        try {
            const res = await fetch("/student_notifications");
            if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
            const data = await res.json();

            personalList.innerHTML = '';

            if (!data.notifications || data.notifications.length === 0) {
                personalList.innerHTML = '<li class="list-group-item text-muted small">No recent activity recorded for you.</li>';
                return;
            }

            data.notifications.forEach(n => {
                const li = document.createElement('li');
                const actionText = n.action === 'IN' ? 'Entered' : 'Exited';
                const bgColor = n.action === 'IN' ? 'list-group-item-success-light' : 'list-group-item-info-light';
                const textColor = n.action === 'IN' ? 'text-success-emphasis' : 'text-info-emphasis';
                const borderColor = n.action === 'IN' ? 'border-success-subtle' : 'border-info-subtle';

                li.className = `list-group-item small d-flex justify-content-between align-items-center ${bgColor} ${borderColor}`;
                li.style.borderLeftWidth = '4px';

                li.innerHTML = `
                    <span>You <strong class="${textColor}">${actionText}</strong> at ${n.timestamp.split(' ')[1]}</span>
                    <span class="badge bg-secondary rounded-pill">${n.timestamp.split(' ')[0]}</span>`;
                personalList.appendChild(li);
            });
        } catch (err) {
            console.error("Error fetching personal notifications:", err);
            personalList.innerHTML = '<li class="list-group-item text-danger small">Error loading your records.</li>';
        }
    }

    updatePersonalNotifications();
    setInterval(updatePersonalNotifications, 10000); // Poll every 10 seconds
});
