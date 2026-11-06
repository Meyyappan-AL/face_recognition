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
                let actionText = 'UPDATED', bgColor = 'list-group-item-light', textColor = 'text-body', borderColor = 'border-secondary-subtle';
                if (n.action === 'IN') { actionText='ENTERED'; bgColor='list-group-item-success-light'; textColor='text-success-emphasis'; borderColor='border-success-subtle'; }
                else if (n.action === 'OUT') { actionText='EXITED'; bgColor='list-group-item-info-light'; textColor='text-info-emphasis'; borderColor='border-info-subtle'; }
                else if (n.action === 'ABSENT') { actionText='MARKED ABSENT'; bgColor='list-group-item-danger-light'; textColor='text-danger-emphasis'; borderColor='border-danger-subtle'; }

                li.className = `list-group-item small d-flex justify-content-between align-items-center ${bgColor} ${borderColor}`;
                li.style.borderLeftWidth = '4px';
                if (isNew) li.classList.add('new-notification-entry');

                li.innerHTML = `
                    <span><strong class="${textColor}">${n.name}</strong> ${actionText}.</span>
                    <span class="badge bg-secondary rounded-pill">${(n.timestamp||'').split(' ')[1] || ''}</span>`;
                
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
