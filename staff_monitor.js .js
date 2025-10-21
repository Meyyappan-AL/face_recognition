// static/js/staff_monitor.js

document.addEventListener("DOMContentLoaded", () => {
    const notificationList = document.getElementById("notificationList");

    async function updateNotificationList() {
        try {
            const res = await fetch("/notifications");
            if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
            const data = await res.json();

            notificationList.innerHTML = ''; 

            if (!data.notifications || data.notifications.length === 0) {
                notificationList.innerHTML = '<li class="list-group-item text-muted small">Awaiting student activity...</li>';
                return;
            }

            const latestTimestampShown = notificationList.dataset.latestTimestamp || '';
            let currentLatestTimestamp = data.notifications[0].timestamp; 
            
            data.notifications.forEach(n => {
                const li = document.createElement('li');
                const isNew = n.timestamp > latestTimestampShown && latestTimestampShown;
                const actionText = n.action === 'IN' ? 'ENTERED' : 'EXITED';
                const bgColor = n.action === 'IN' ? 'list-group-item-success-light' : 'list-group-item-info-light';
                const textColor = n.action === 'IN' ? 'text-success-emphasis' : 'text-info-emphasis';
                const borderColor = n.action === 'IN' ? 'border-success-subtle' : 'border-info-subtle';

                li.className = `list-group-item small d-flex justify-content-between align-items-center ${bgColor} ${borderColor}`;
                li.style.borderLeftWidth = '4px';
                if (isNew) li.classList.add('new-notification-entry');

                li.innerHTML = `
                    <span><strong class="${textColor}">${n.name}</strong> has ${actionText}.</span>
                    <span class="badge bg-secondary rounded-pill">${n.timestamp.split(' ')[1]}</span>`;
                
                notificationList.appendChild(li);
            });
            
            notificationList.dataset.latestTimestamp = currentLatestTimestamp;

        } catch (err) {
            console.error("Error fetching notifications:", err);
        }
    }
    
    updateNotificationList(); 
    setInterval(updateNotificationList, 3000);

    const style = document.createElement('style');
    style.innerHTML = `@keyframes flashIn { 0% { background-color: #a7f3d0; } 100% { background-color: var(--bs-success-bg-subtle); } } .new-notification-entry { animation: flashIn 0.8s ease-out; }`;
    document.head.appendChild(style);
});
